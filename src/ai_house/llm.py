"""Unified OpenAI-compatible Model Router, GPT-5 compatibility, retries/failover, and SQLite usage ledger (`0.3.0`)."""
from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from .onboarding import atomic_write_private_text, mask_secret

JST = dt.timezone(dt.timedelta(hours=9))
_LOG = logging.getLogger("ai_house.llm")
_SK_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_TRANSIENT_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})
VALID_PURPOSES = ("chat", "life", "memory_extract", "diary", "ping")

DEFAULT_TASK_PROFILES: Dict[str, Tuple[float, int, float, int]] = {
    # purpose: (temperature, max_tokens, timeout_s, max_retries)
    "chat": (0.7, 400, 25.0, 1),
    "life": (0.7, 400, 25.0, 1),
    "memory_extract": (0.2, 2000, 45.0, 1),
    "diary": (0.7, 500, 25.0, 1),
    "ping": (0.7, 350, 12.0, 0),
}

# Total wall-clock budget (seconds) for ONE logical call: every retry, fallback hop and tool round shares it.
# Override globally with AI_HOUSE_LLM_TOTAL_BUDGET_S or per purpose with AI_HOUSE_<PURPOSE>_TOTAL_BUDGET_S.
DEFAULT_TOTAL_BUDGET_S: Dict[str, float] = {
    "chat": 45.0,
    "life": 45.0,
    "diary": 45.0,
    "memory_extract": 90.0,
    "ping": 15.0,
}
_MIN_BUDGET_S = 0.5
_MAX_BUDGET_S = 600.0
_DEADLINE_EPSILON_S = 0.05

# Circuit breaker: BREAKER_THRESHOLD failures inside BREAKER_WINDOW_S open it for BREAKER_OPEN_S; afterwards
# exactly one probe request is let through (half-open) until it succeeds or fails.
BREAKER_THRESHOLD = 3
BREAKER_WINDOW_S = 120.0
BREAKER_OPEN_S = 30.0
BREAKER_PROBE_STALE_S = 120.0

USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day_jst TEXT NOT NULL,
    purpose TEXT NOT NULL,
    actor TEXT NOT NULL,
    model TEXT NOT NULL,
    base_url_host TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    error_code TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_day ON llm_usage(day_jst, purpose, actor);
"""


class LLMAPIError(RuntimeError):
    """Structured error raised on HTTP failures from OpenAI-compatible endpoints."""

    def __init__(self, status_code: int, message: str, detail: str = "") -> None:
        self.status_code = int(status_code or 0)
        self.detail = detail
        super().__init__(message)


class LLMDeadlineError(LLMAPIError):
    """The total time budget of a call (retries + fallbacks + tool rounds) is used up."""

    def __init__(self, message: str) -> None:
        super().__init__(504, message, detail="deadline_exceeded")


# Monotonic deadline shared by everything running inside `deadline_scope` (same thread / context).
_DEADLINE: "contextvars.ContextVar[Optional[float]]" = contextvars.ContextVar("ai_house_llm_deadline", default=None)


@contextlib.contextmanager
def deadline_scope(budget_s: float):
    """Bound every LLM/HTTP call made inside the block by `budget_s` seconds (nested scopes take the earliest)."""
    new = time.monotonic() + max(0.0, float(budget_s))
    cur = _DEADLINE.get()
    eff = new if cur is None else min(cur, new)
    token = _DEADLINE.set(eff)
    try:
        yield eff
    finally:
        _DEADLINE.reset(token)


def remaining_budget() -> Optional[float]:
    """Seconds left in the innermost `deadline_scope`, or None when no scope is active."""
    cur = _DEADLINE.get()
    return None if cur is None else cur - time.monotonic()


def clamp_timeout(timeout_s: float) -> float:
    """Return `min(timeout_s, remaining budget)`; raise `LLMDeadlineError` when the budget is already spent."""
    rem = remaining_budget()
    if rem is None:
        return float(timeout_s)
    if rem <= _DEADLINE_EPSILON_S:
        raise LLMDeadlineError("LLM 总时限已耗尽（附件/调用预算用完）")
    return max(_DEADLINE_EPSILON_S, min(float(timeout_s), rem))


def _effective_deadline(deadline: Optional[float]) -> Optional[float]:
    ctx = _DEADLINE.get()
    if deadline is None:
        return ctx
    return deadline if ctx is None else min(deadline, ctx)


def _request_timeout(timeout_s: float, deadline: Optional[float]) -> float:
    eff = _effective_deadline(deadline)
    if eff is None:
        return float(timeout_s)
    rem = eff - time.monotonic()
    if rem <= _DEADLINE_EPSILON_S:
        raise LLMDeadlineError("LLM 总时限已耗尽")
    return max(_DEADLINE_EPSILON_S, min(float(timeout_s), rem))


def mask_secrets(text: str, api_key: str = "") -> str:
    """Redact API keys and sk-* tokens before surfacing errors to logs or UI."""
    cleaned = str(text or "")
    key = str(api_key or "").strip()
    if key:
        cleaned = cleaned.replace(key, "[REDACTED]")
    return _SK_SECRET_RE.sub("[REDACTED]", cleaned)


def _extract_http_error_detail(exc: urllib.error.HTTPError, api_key: str = "") -> str:
    raw = ""
    try:
        if exc.fp is not None:
            raw = exc.fp.read(4096).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        raw = ""
    if raw:
        try:
            doc = json.loads(raw)
            err = doc.get("error")
            if isinstance(err, dict):
                parts = [str(err.get("message") or "").strip()]
                if err.get("param"):
                    parts.append(f"param={err['param']}")
                if err.get("code"):
                    parts.append(f"code={err['code']}")
                raw = "; ".join(p for p in parts if p)
            elif isinstance(err, str):
                raw = err
            elif doc.get("message"):
                raw = str(doc.get("message"))
        except ValueError:
            pass
    detail = " ".join(str(raw or exc.reason or f"HTTP {exc.code}").split())
    return mask_secrets(detail, api_key=api_key)[:280]


def _adapt_reasoning_payload(payload: Mapping[str, Any], *, min_tokens: int = 2048) -> Dict[str, Any]:
    adapted = dict(payload)
    floor = max(1024, int(min_tokens))
    if "max_tokens" in adapted:
        old_max = adapted.pop("max_tokens")
        try:
            n = int(old_max or 0)
        except (TypeError, ValueError):
            n = 0
        adapted["max_completion_tokens"] = max(n * 4, floor)
    elif "max_completion_tokens" in adapted:
        try:
            n = int(adapted["max_completion_tokens"] or 0)
        except (TypeError, ValueError):
            n = 0
        if n < floor:
            adapted["max_completion_tokens"] = floor
    adapted.pop("temperature", None)
    return adapted


def _compat_retry_payload(payload: Mapping[str, Any], detail: str) -> Optional[Dict[str, Any]]:
    low = detail.lower()
    if any(exc_tok in low for exc_tok in ("context_length", "maximum context", "too long")):
        return None
    if not any(tok in low for tok in ("max_tokens", "max_completion_tokens", "temperature")):
        return None
    indicators = (
        "unsupported",
        "not supported",
        "unrecognized",
        "unknown parameter",
        "unsupported_parameter",
        "unsupported_value",
        "only the default",
    )
    if not any(ind in low for ind in indicators):
        return None
    adapted = _adapt_reasoning_payload(payload)
    return adapted if adapted != dict(payload) else None


def _is_empty_reasoning_length(doc: Mapping[str, Any]) -> bool:
    choice = (doc.get("choices") or [{}])[0]
    if str(choice.get("finish_reason") or "").strip().lower() != "length":
        return False
    msg = choice.get("message") or {}
    content = str(msg.get("content") or "").strip()
    tcalls = msg.get("tool_calls") or []
    return (not content) and (not tcalls)


def _chat_completions_url(base_url: str) -> str:
    url = (base_url or "https://api.openai.com").strip().rstrip("/")
    if not url.endswith("/chat/completions"):
        url = (url + "/chat/completions") if url.endswith("/v1") else (url + "/v1/chat/completions")
    return url


def _is_empty_reply(doc: Mapping[str, Any]) -> bool:
    """HTTP 200 whose first choice has neither text nor tool calls (any finish_reason)."""
    choice = (doc.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return (not str(msg.get("content") or "").strip()) and (not (msg.get("tool_calls") or []))


def _doc_tokens(doc: Mapping[str, Any]) -> int:
    usage = doc.get("usage") or {}
    return _safe_int(usage.get("total_tokens"), 0) or (
        _safe_int(usage.get("prompt_tokens"), 0) + _safe_int(usage.get("completion_tokens"), 0)
    )


def _send_chat_request(
    url: str,
    api_key: str,
    payload: Mapping[str, Any],
    timeout_s: float,
    deadline: Optional[float] = None,
) -> Dict[str, Any]:
    req_timeout = _request_timeout(timeout_s, deadline)
    req = urllib.request.Request(
        url,
        data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "ai-house-kit/0.3 (+https://github.com/Gengetau/ai-house-kit)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=req_timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _send_with_length_retry(
    url: str,
    api_key: str,
    payload: Mapping[str, Any],
    timeout_s: float,
    deadline: Optional[float] = None,
    *,
    retry_empty: bool = False,
) -> Dict[str, Any]:
    cur_payload: Mapping[str, Any] = payload
    doc = _send_chat_request(url, api_key, cur_payload, timeout_s, deadline)
    if _is_empty_reasoning_length(doc):
        cur_limit = _safe_int(payload.get("max_completion_tokens") or payload.get("max_tokens"), 2048)
        cur_payload = _adapt_reasoning_payload(payload, min_tokens=max(cur_limit * 2, 4096))
        doc = _send_chat_request(url, api_key, cur_payload, timeout_s, deadline)
        if _is_empty_reasoning_length(doc):
            err = LLMAPIError(
                502,
                "HTTP 502: reasoning tokens exhausted with empty content",
                detail="empty_reasoning_length",
            )
            err.total_tokens = _doc_tokens(doc)  # type: ignore[attr-defined]
            raise err
    if retry_empty and _is_empty_reply(doc):
        # HTTP 200 + finish_reason=stop + nothing to say: one more try, then a loud failure (never a silent success)
        first_tokens = _doc_tokens(doc)
        doc = _send_chat_request(url, api_key, cur_payload, timeout_s, deadline)
        if _is_empty_reply(doc):
            err = LLMAPIError(502, "HTTP 502: model returned an empty reply", detail="empty_reply")
            err.total_tokens = first_tokens + _doc_tokens(doc)  # type: ignore[attr-defined]
            raise err
    return doc


def post_openai_chat_json(
    *,
    base_url: str,
    api_key: str,
    payload: Mapping[str, Any],
    timeout_s: float = 25.0,
) -> Dict[str, Any]:
    """POST to `/v1/chat/completions`, auto-adapting GPT-5/o-series parameter limits on HTTP 400.

    `timeout_s` bounds the WHOLE call (including the internal length/compat retries), and never exceeds
    the budget of an enclosing `deadline_scope`.
    """
    url = _chat_completions_url(base_url)
    deadline = time.monotonic() + max(0.05, float(timeout_s))
    try:
        return _send_with_length_retry(url, api_key, payload, timeout_s, deadline)
    except urllib.error.HTTPError as exc:
        detail = _extract_http_error_detail(exc, api_key=api_key)
        if exc.code == 400:
            retry_payload = _compat_retry_payload(payload, detail)
            if retry_payload is not None:
                try:
                    return _send_with_length_retry(url, api_key, retry_payload, timeout_s, deadline)
                except urllib.error.HTTPError as retry_exc:
                    retry_detail = _extract_http_error_detail(retry_exc, api_key=api_key)
                    raise LLMAPIError(retry_exc.code, f"HTTP {retry_exc.code}: {retry_detail}", detail=retry_detail) from retry_exc
        raise LLMAPIError(exc.code, f"HTTP {exc.code}: {detail}", detail=detail) from exc


def openai_compatible_chat(
    system: str,
    user: str,
    *,
    api_key: str,
    base_url: str = "https://api.openai.com",
    model: str = "gpt-4o-mini",
    timeout_s: float = 25.0,
) -> Tuple[str, int]:
    """Call any OpenAI-compatible `/v1/chat/completions` endpoint using Python stdlib."""
    doc = post_openai_chat_json(
        base_url=base_url,
        api_key=api_key,
        payload={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "max_tokens": 350,
        },
        timeout_s=timeout_s,
    )
    msg = (((doc.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    tokens = int((doc.get("usage") or {}).get("total_tokens") or 0)
    return str(msg).strip(), tokens


@dataclass(frozen=True)
class ModelEndpoint:
    base_url: str
    api_key: str
    model: str


@dataclass(frozen=True)
class TaskProfile:
    temperature: float = 0.7
    max_tokens: int = 400
    timeout_s: float = 25.0
    max_retries: int = 1


@dataclass(frozen=True)
class LLMCallResult:
    message: Dict[str, Any]
    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str
    base_url: str
    fallback_used: bool
    latency_ms: int


def _safe_float(raw: Any, default: float) -> float:
    try:
        v = float(raw)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def _safe_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _actor_env_key(actor: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", str(actor or "").upper())


def _host_label(base_url: str) -> str:
    try:
        p = urlparse(base_url)
        return p.netloc or p.path or base_url
    except ValueError:
        return str(base_url)[:80]


class ModelRouter:
    """Routes chat, life, memory extraction, and diary LLM calls with failover and SQLite usage tracking."""

    CONFIG_FILENAME = "llm_config.json"
    USAGE_DB_FILENAME = "usage.db"

    def __init__(
        self,
        env: Optional[Mapping[str, str]] = None,
        *,
        runtime_dir: Optional[str | Path] = None,
        clock: Any = None,
        retry_delay_s: float = 0.08,
    ) -> None:
        self._lock = threading.RLock()
        self.env: Dict[str, str] = dict(os.environ if env is None else env)
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else None
        self._clock = clock
        self.retry_delay_s = max(0.0, float(retry_delay_s))
        self._reasoning_caps: set[Tuple[str, str]] = set()
        # per (base_url, model): {"fails": [ts, ...], "open_until": float, "probing": bool, "probe_ts": float}
        self._breaker_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._issue_logged: set[Tuple[str, str, str]] = set()
        self._plugin_visitor_calls: Dict[Tuple[str, str], List[float]] = {}
        self._config_path = (self.runtime_dir / self.CONFIG_FILENAME) if self.runtime_dir is not None else None
        self._usage_db = (self.runtime_dir / self.USAGE_DB_FILENAME) if self.runtime_dir is not None else None
        self._overrides: Dict[str, Any] = self._load_overrides()
        if self._usage_db is not None:
            self._init_usage_schema()

    def _now(self) -> float:
        return float(self._clock.now()) if self._clock is not None else time.time()

    def _load_overrides(self) -> Dict[str, Any]:
        if self._config_path is None or not self._config_path.is_file():
            return {"fallback": {}, "resident_models": {}, "task_profiles": {}}
        try:
            doc = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"fallback": {}, "resident_models": {}, "task_profiles": {}}
        if not isinstance(doc, dict):
            return {"fallback": {}, "resident_models": {}, "task_profiles": {}}
        return {
            "fallback": dict(doc.get("fallback") or {}) if isinstance(doc.get("fallback"), dict) else {},
            "resident_models": dict(doc.get("resident_models") or {}) if isinstance(doc.get("resident_models"), dict) else {},
            "task_profiles": dict(doc.get("task_profiles") or {}) if isinstance(doc.get("task_profiles"), dict) else {},
        }

    def _save_overrides(self) -> None:
        if self._config_path is None or self.runtime_dir is None:
            return
        atomic_write_private_text(self._config_path, json.dumps(self._overrides, ensure_ascii=False, indent=2))

    def _init_usage_schema(self) -> None:
        assert self._usage_db is not None
        self._usage_db.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self._usage_db, timeout=5)) as c:
            with c:
                c.executescript(USAGE_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._reasoning_caps.clear()
            self._breaker_state.clear()
            self._issue_logged.clear()
            self._plugin_visitor_calls.clear()

    def update_env(self, env: Mapping[str, str]) -> None:
        with self._lock:
            self.env = dict(env)
            self._reasoning_caps.clear()
            self._breaker_state.clear()
            self._issue_logged.clear()

    def update_overrides(self, spec: Mapping[str, Any], *, persist: bool = True) -> None:
        """Persist optional fallback, per-resident model routes, and task parameter overrides."""
        with self._lock:
            changed = False
            if isinstance(spec.get("fallback"), Mapping):
                fb_in = spec["fallback"]
                fb_cur = dict(self._overrides.get("fallback") or {})
                if fb_in.get("clear_key") is True:
                    fb_cur["api_key"] = ""
                    self.env.pop("AI_HOUSE_FALLBACK_API_KEY", None)
                elif str(fb_in.get("api_key") or "").strip():
                    fb_cur["api_key"] = str(fb_in["api_key"]).strip()
                if "base_url" in fb_in:
                    u = str(fb_in.get("base_url") or "").strip().rstrip("/")
                    if u and not u.startswith(("http://", "https://")):
                        raise ValueError("备用 Base URL 必须以 http:// 或 https:// 开头")
                    if u and ModelRouter._origin_tuple(u) is None:
                        raise ValueError("备用 Base URL 无效（端口必须是数字）")
                    fb_cur["base_url"] = u
                if "model" in fb_in:
                    fb_cur["model"] = str(fb_in.get("model") or "").strip()
                self._overrides["fallback"] = fb_cur
                changed = True

            if isinstance(spec.get("resident_models"), Mapping):
                res_out: Dict[str, Dict[str, str]] = {}
                cur_res = dict(self._overrides.get("resident_models") or {})
                for aid, raw_ent in spec["resident_models"].items():
                    clean_aid = str(aid or "").strip()
                    if not clean_aid or not isinstance(raw_ent, Mapping):
                        continue
                    prev_ent = dict(cur_res.get(clean_aid) or {})
                    mdl = str(raw_ent.get("model") or "").strip()
                    url = str(raw_ent.get("base_url") or "").strip().rstrip("/")
                    if url and not url.startswith(("http://", "https://")):
                        raise ValueError(f"住客 {clean_aid} Base URL 必须以 http:// 或 https:// 开头")
                    if url and ModelRouter._origin_tuple(url) is None:
                        raise ValueError(f"住客 {clean_aid} Base URL 无效（端口必须是数字）")
                    if raw_ent.get("clear_key") is True:
                        key = ""
                    elif str(raw_ent.get("api_key") or "").strip():
                        key = str(raw_ent["api_key"]).strip()
                    else:
                        key = str(prev_ent.get("api_key") or "").strip()
                    if mdl or url or key:
                        res_out[clean_aid] = {"model": mdl, "base_url": url, "api_key": key}
                self._overrides["resident_models"] = res_out
                changed = True

            if isinstance(spec.get("task_profiles"), Mapping):
                tp_out = dict(self._overrides.get("task_profiles") or {})
                for p, raw_tp in spec["task_profiles"].items():
                    clean_p = str(p or "").strip()
                    if clean_p not in VALID_PURPOSES or not isinstance(raw_tp, Mapping):
                        continue
                    prev_tp = dict(tp_out.get(clean_p) or {})
                    ent: Dict[str, Any] = {}
                    if raw_tp.get("temperature") is not None:
                        ent["temperature"] = max(0.0, min(_safe_float(raw_tp["temperature"], 0.7), 2.0))
                    if raw_tp.get("max_tokens") is not None:
                        ent["max_tokens"] = max(32, min(_safe_int(raw_tp["max_tokens"], 400), 16000))
                    if raw_tp.get("timeout_s") is not None:
                        ent["timeout_s"] = max(1.0, min(_safe_float(raw_tp["timeout_s"], 25.0), 180.0))
                    if raw_tp.get("max_retries") is not None:
                        ent["max_retries"] = max(0, min(_safe_int(raw_tp["max_retries"], 1), 3))
                    if raw_tp.get("model"):
                        ent["model"] = str(raw_tp["model"]).strip()
                    if raw_tp.get("base_url"):
                        u = str(raw_tp["base_url"]).strip().rstrip("/")
                        if u and not u.startswith(("http://", "https://")):
                            raise ValueError(f"任务 {clean_p} Base URL 必须以 http:// 或 https:// 开头")
                        if u and ModelRouter._origin_tuple(u) is None:
                            raise ValueError(f"任务 {clean_p} Base URL 无效（端口必须是数字）")
                        ent["base_url"] = u
                    if raw_tp.get("clear_key") is True:
                        pass
                    elif str(raw_tp.get("api_key") or "").strip():
                        ent["api_key"] = str(raw_tp["api_key"]).strip()
                    elif prev_tp.get("api_key"):
                        ent["api_key"] = str(prev_tp["api_key"])
                    tp_out[clean_p] = ent
                self._overrides["task_profiles"] = tp_out
                changed = True

            if changed:
                self._reasoning_caps.clear()
                self._breaker_state.clear()
                self._issue_logged.clear()
                if persist:
                    self._save_overrides()

    def status_view(self) -> Dict[str, Any]:
        with self._lock:
            p_url, p_key, p_model = self._primary_tuple()
            fb = self._fallback_tuple(p_url, p_model)
            res_view: Dict[str, Dict[str, Any]] = {}
            for aid, ent in (self._overrides.get("resident_models") or {}).items():
                if not isinstance(ent, Mapping):
                    continue
                ak = str(ent.get("api_key") or "").strip()
                res_view[str(aid)] = {
                    "model": str(ent.get("model") or ""),
                    "base_url": str(ent.get("base_url") or ""),
                    "api_key_set": bool(ak),
                    "api_key_masked": mask_secret(ak),
                }
            return {
                "fallback": {
                    "enabled": bool(fb and fb[1]),
                    "base_url": fb[0] if fb else p_url,
                    "model": fb[2] if fb else p_model,
                    "api_key_set": bool(fb and fb[1]),
                    "api_key_masked": mask_secret(fb[1] if fb else ""),
                },
                "resident_models": res_view,
                "config_issues": self.config_issues(),
                "task_profiles": {
                    p: {
                        "temperature": prof.temperature,
                        "max_tokens": prof.max_tokens,
                        "timeout_s": prof.timeout_s,
                        "max_retries": prof.max_retries,
                    }
                    for p, prof in ((k, self.resolve_profile(k)) for k in VALID_PURPOSES)
                },
            }

    def collect_secrets(self) -> List[str]:
        """Return all configured primary, fallback, resident, and task API keys for leak-guarding."""
        secrets: List[str] = []
        with self._lock:
            candidates: List[str] = [
                str(self.env.get("AI_HOUSE_FALLBACK_API_KEY") or ""),
                str(self.env.get("AI_HOUSE_MEMORY_API_KEY") or ""),
                str((self._overrides.get("fallback") or {}).get("api_key") or ""),
            ]
            for k, v in self.env.items():
                if k.startswith(("AI_HOUSE_ACTOR_API_KEY_", "AI_HOUSE_LIFE_API_KEY", "AI_HOUSE_DIARY_API_KEY", "AI_HOUSE_PING_API_KEY", "AI_HOUSE_CHAT_API_KEY")):
                    candidates.append(str(v or ""))
            for group_name in ("resident_models", "task_profiles"):
                for ent in (self._overrides.get(group_name) or {}).values():
                    if isinstance(ent, Mapping):
                        candidates.append(str(ent.get("api_key") or ""))
            for item in candidates:
                cleaned = item.strip()
                if cleaned and cleaned not in secrets:
                    secrets.append(cleaned)
        return secrets

    def _primary_tuple(self) -> Tuple[str, str, str]:
        url = (
            self.env.get("AI_HOUSE_BASE_URL")
            or self.env.get("OPENAI_BASE_URL")
            or self.env.get("WORLD_KERNEL_BASE_URL")
            or "https://api.openai.com"
        ).strip().rstrip("/")
        key = (
            self.env.get("AI_HOUSE_API_KEY")
            or self.env.get("OPENAI_API_KEY")
            or self.env.get("WORLD_KERNEL_API_KEY")
            or ""
        ).strip()
        model = (
            self.env.get("AI_HOUSE_MODEL")
            or self.env.get("WORLD_KERNEL_MODEL")
            or "gpt-4o-mini"
        ).strip()
        return url, key, model

    def _fallback_tuple(self, default_url: str, default_model: str) -> Optional[Tuple[str, str, str]]:
        """Fallback layer. Without an address of its own it is addressed at `default_url` (callers pass the PRIMARY url)."""
        fb_ov = self._overrides.get("fallback") or {}
        key = str(fb_ov.get("api_key") or self.env.get("AI_HOUSE_FALLBACK_API_KEY") or "").strip()
        if not key:
            return None
        url = str(fb_ov.get("base_url") or self.env.get("AI_HOUSE_FALLBACK_BASE_URL") or default_url).strip().rstrip("/")
        mdl = str(fb_ov.get("model") or self.env.get("AI_HOUSE_FALLBACK_MODEL") or default_model).strip()
        return url, key, mdl

    def resolve_profile(self, purpose: str = "chat") -> TaskProfile:
        with self._lock:
            p = purpose if purpose in DEFAULT_TASK_PROFILES else "chat"
            t_def, m_def, to_def, r_def = DEFAULT_TASK_PROFILES[p]
            prefix = f"AI_HOUSE_{p.upper()}"
            temp = _safe_float(self.env.get(f"{prefix}_TEMPERATURE") or self.env.get("AI_HOUSE_LLM_TEMPERATURE"), t_def)
            mtok = _safe_int(self.env.get(f"{prefix}_MAX_TOKENS") or self.env.get("AI_HOUSE_LLM_MAX_TOKENS"), m_def)
            tout = _safe_float(self.env.get(f"{prefix}_TIMEOUT_S") or self.env.get("AI_HOUSE_LLM_TIMEOUT_S"), to_def)
            ret = _safe_int(self.env.get(f"{prefix}_MAX_RETRIES") or self.env.get("AI_HOUSE_LLM_MAX_RETRIES"), r_def)

            tp_ov = (self._overrides.get("task_profiles") or {}).get(p) or {}
            if tp_ov.get("temperature") is not None:
                temp = float(tp_ov["temperature"])
            if tp_ov.get("max_tokens") is not None:
                mtok = int(tp_ov["max_tokens"])
            if tp_ov.get("timeout_s") is not None:
                tout = float(tp_ov["timeout_s"])
            if tp_ov.get("max_retries") is not None:
                ret = int(tp_ov["max_retries"])
            return TaskProfile(
                temperature=max(0.0, min(temp, 2.0)),
                max_tokens=max(32, min(mtok, 16000)),
                timeout_s=max(1.0, min(tout, 180.0)),
                max_retries=max(0, min(ret, 3)),
            )

    def _task_endpoint_override(self, purpose: str) -> Tuple[str, str, str]:
        p = purpose if purpose in VALID_PURPOSES else "chat"
        prefix = f"AI_HOUSE_{p.upper()}"
        tp_ov = (self._overrides.get("task_profiles") or {}).get(p) or {}
        if p == "memory_extract":
            url = str(tp_ov.get("base_url") or self.env.get("AI_HOUSE_MEMORY_BASE_URL") or self.env.get(f"{prefix}_BASE_URL") or "").strip().rstrip("/")
            key = str(tp_ov.get("api_key") or self.env.get("AI_HOUSE_MEMORY_API_KEY") or self.env.get(f"{prefix}_API_KEY") or "").strip()
            mdl = str(tp_ov.get("model") or self.env.get("AI_HOUSE_MEMORY_MODEL") or self.env.get(f"{prefix}_MODEL") or "").strip()
        else:
            url = str(tp_ov.get("base_url") or self.env.get(f"{prefix}_BASE_URL") or "").strip().rstrip("/")
            key = str(tp_ov.get("api_key") or self.env.get(f"{prefix}_API_KEY") or "").strip()
            mdl = str(tp_ov.get("model") or self.env.get(f"{prefix}_MODEL") or "").strip()
        return url, key, mdl

    def _resident_endpoint_override(self, actor: str) -> Tuple[str, str, str]:
        if not actor:
            return "", "", ""
        ak = _actor_env_key(actor)
        ov = (self._overrides.get("resident_models") or {}).get(actor) or {}
        url = str(ov.get("base_url") or self.env.get(f"AI_HOUSE_ACTOR_BASE_URL_{ak}") or "").strip().rstrip("/")
        key = str(ov.get("api_key") or self.env.get(f"AI_HOUSE_ACTOR_API_KEY_{ak}") or "").strip()
        mdl = str(ov.get("model") or self.env.get(f"AI_HOUSE_ACTOR_MODEL_{ak}") or "").strip()
        return url, key, mdl

    @staticmethod
    def _origin_tuple(url: str) -> Optional[Tuple[str, str, int]]:
        """(scheme, host, port) of `url`, or None when it cannot be parsed (e.g. a non-numeric port)."""
        try:
            p = urlparse(str(url or "").strip())
            scheme = (p.scheme or "").lower()
            host = (p.hostname or "").strip().lower().rstrip(".")
            port = int(p.port or (443 if scheme == "https" else 80))
        except ValueError:
            return None
        return scheme, host, port

    def _resolve(self, purpose: str, actor: str) -> Tuple[List[Tuple[ModelEndpoint, bool]], Optional[Dict[str, Any]]]:
        """Resolve endpoints plus a config issue (or None). A key is only ever sent to the address of its own layer."""
        with self._lock:
            p_url, p_key, p_model = self._primary_tuple()
            t_url, t_key, t_model = self._task_endpoint_override(purpose)
            r_url, r_key, r_model = self._resident_endpoint_override(actor)

            # Precedence: dedicated memory_extract/ping task override > resident override > primary;
            # all other purposes: resident override > task override > primary.
            if purpose in ("memory_extract", "ping") and (t_url or t_key or t_model):
                layers = [(t_url, t_key, t_model), (r_url, r_key, r_model), (p_url, p_key, p_model)]
            else:
                layers = [(r_url, r_key, r_model), (t_url, t_key, t_model), (p_url, p_key, p_model)]

            sel_url = next((u for u, _, _ in layers if u), p_url)
            sel_origin = self._origin_tuple(sel_url)
            sel_model = next((m for _, _, m in layers if m), p_model)
            p_origin = self._origin_tuple(p_url)

            # Never send a key to an origin other than its own layer's (a layer without its own url sits on the primary url)
            sel_key = ""
            if sel_origin is not None:
                for l_url, l_key, _ in layers:
                    if not l_key:
                        continue
                    if self._origin_tuple(l_url or p_url) == sel_origin:
                        sel_key = l_key
                        break

            eps: List[Tuple[ModelEndpoint, bool]] = []
            if sel_key:
                eps.append((ModelEndpoint(base_url=sel_url, api_key=sel_key, model=sel_model), False))

            # A fallback without its own address is only valid for the primary address, never the resident/task url.
            same_as_primary = sel_origin is not None and sel_origin == p_origin
            fb = self._fallback_tuple(p_url, sel_model if same_as_primary else p_model)
            if fb is not None and self._origin_tuple(fb[0]) is not None:
                fb_ep = ModelEndpoint(base_url=fb[0], api_key=fb[1], model=fb[2])
                if not eps or fb_ep != eps[0][0]:
                    eps.append((fb_ep, True))

            issue: Optional[Dict[str, Any]] = None
            if not sel_key:
                reason = ""
                code = "CONFIG_NO_KEY"
                if sel_origin is None:
                    code = "CONFIG_BAD_URL"
                    reason = "base_url 无效（例如端口不是数字），该地址不会被调用"
                elif sel_origin != p_origin:
                    reason = "已配置自定义 base_url 但没有为该地址配置 api_key；主 API Key 不会发往其它地址，请补上该层的 api_key"
                if reason:
                    issue = {
                        "purpose": purpose,
                        "actor": str(actor or ""),
                        "base_url_host": _host_label(sel_url),
                        "reason": reason,
                        "code": code,
                    }
            return eps, issue

    def resolve_endpoints(self, *, purpose: str = "chat", actor: str = "") -> List[Tuple[ModelEndpoint, bool]]:
        """Return ordered `[(ModelEndpoint, is_fallback), ...]` for the given task purpose and roommate."""
        eps, issue = self._resolve(purpose, actor)
        if issue is not None:
            self._warn_issue_once(issue)
        return eps

    def _warn_issue_once(self, issue: Mapping[str, Any]) -> None:
        sig = (str(issue.get("purpose")), str(issue.get("actor")), str(issue.get("base_url_host")))
        with self._lock:
            if sig in self._issue_logged:
                return
            self._issue_logged.add(sig)
        who = f"住客 {issue['actor']}" if issue.get("actor") else f"任务 {issue['purpose']}"
        _LOG.warning(
            "LLM 配置错误：%s -> %s 不可用（%s）。缺少 api_key 或地址无效，已拒绝调用且不会使用主 Key。",
            who,
            issue.get("base_url_host"),
            issue.get("reason"),
        )

    def config_issue(self, *, purpose: str = "chat", actor: str = "") -> Optional[Dict[str, Any]]:
        """The config problem that makes (purpose, actor) unusable, or None. Never contains key material."""
        return self._resolve(purpose, actor)[1]

    def config_issues(self) -> List[Dict[str, Any]]:
        """All override misconfigurations that would silently disable a task/resident (no key material)."""
        with self._lock:
            actors: List[str] = [""]
            actors += [str(a) for a in (self._overrides.get("resident_models") or {})]
            for k in self.env:
                for pref in ("AI_HOUSE_ACTOR_BASE_URL_", "AI_HOUSE_ACTOR_API_KEY_"):
                    if k.startswith(pref) and k[len(pref):]:
                        actors.append(k[len(pref):].lower())  # env labels are upper-cased; actor ids are lower-case
            grouped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            for actor in dict.fromkeys(actors):
                for purpose in VALID_PURPOSES:
                    issue = self._resolve(purpose, actor)[1]
                    if issue is None:
                        continue
                    gk = (issue["actor"], issue["base_url_host"], issue["code"])
                    if issue["actor"] and ("", issue["base_url_host"], issue["code"]) in grouped:
                        continue  # same problem already reported for the task layer
                    ent = grouped.setdefault(gk, {**issue, "purposes": []})
                    ent["purposes"].append(purpose)
            return list(grouped.values())

    def has_live_endpoint(self, *, purpose: str = "chat", actor: str = "") -> bool:
        return bool(self.resolve_endpoints(purpose=purpose, actor=actor))

    def _record_usage(
        self,
        *,
        purpose: str,
        actor: str,
        endpoint: ModelEndpoint,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        latency_ms: int,
        ok: bool,
        fallback_used: bool,
        error_code: str = "",
    ) -> None:
        if self._usage_db is None:
            return
        ts = self._now()
        day_jst = dt.datetime.fromtimestamp(ts, JST).date().isoformat()
        host = _host_label(endpoint.base_url)
        with self._lock, contextlib.closing(sqlite3.connect(self._usage_db, timeout=5)) as c:
            with c:
                c.execute(
                    "INSERT INTO llm_usage(ts, day_jst, purpose, actor, model, base_url_host, "
                    "prompt_tokens, completion_tokens, total_tokens, latency_ms, ok, fallback_used, error_code) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ts,
                        day_jst,
                        str(purpose or "chat"),
                        str(actor or ""),
                        endpoint.model,
                        host,
                        int(prompt_tokens or 0),
                        int(completion_tokens or 0),
                        int(total_tokens or 0),
                        int(latency_ms or 0),
                        1 if ok else 0,
                        1 if fallback_used else 0,
                        str(error_code or "")[:64],
                    ),
                )

    def get_usage_summary(
        self,
        *,
        days: int = 7,
        period: Optional[str] = None,
        recent_limit: int = 30,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Aggregate LLM calls and tokens from `usage.db` across a trailing window or named period."""
        ref_ts = float(now) if now is not None else self._now()
        clean_period = str(period or "").strip().lower() or None
        clean_recent = max(1, min(int(recent_limit or 30), 200))
        if clean_period == "today":
            today_jst = dt.datetime.fromtimestamp(ref_ts, JST).date().isoformat()
            where_sql = "day_jst=?"
            where_arg: Any = today_jst
            clean_days = 1
        elif clean_period == "all":
            where_sql = "ts>=?"
            where_arg = 0.0
            clean_days = 90
        elif clean_period == "30d":
            where_sql = "ts>=?"
            where_arg = ref_ts - 30 * 86400.0
            clean_days = 30
        elif clean_period == "7d":
            where_sql = "ts>=?"
            where_arg = ref_ts - 7 * 86400.0
            clean_days = 7
        else:
            clean_days = max(1, min(int(days or 7), 90))
            where_sql = "ts>=?"
            where_arg = ref_ts - clean_days * 86400.0
            clean_period = f"{clean_days}d"

        empty = {
            "ok": True,
            "days": clean_days,
            "period": clean_period,
            "totals": {"calls": 0, "ok_calls": 0, "failed_calls": 0, "fallback_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "by_day": [],
            "by_purpose": [],
            "by_actor": [],
            "by_model": [],
            "recent": [],
            "config_issues": self.config_issues(),
        }
        if self._usage_db is None or not self._usage_db.is_file():
            return empty

        with self._lock, contextlib.closing(sqlite3.connect(self._usage_db, timeout=5)) as c:
            c.row_factory = sqlite3.Row
            tot = c.execute(
                "SELECT COUNT(*) AS calls, SUM(ok) AS ok_calls, SUM(1 - ok) AS failed_calls, "
                "SUM(fallback_used) AS fallback_calls, COALESCE(SUM(prompt_tokens), 0) AS pt, "
                "COALESCE(SUM(completion_tokens), 0) AS ct, COALESCE(SUM(total_tokens), 0) AS tt "
                f"FROM llm_usage WHERE {where_sql}",
                (where_arg,),
            ).fetchone()
            by_day = [
                dict(r)
                for r in c.execute(
                    "SELECT day_jst AS day, COUNT(*) AS calls, COALESCE(SUM(1 - ok), 0) AS failed_calls, "
                    "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
                    "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
                    "COALESCE(SUM(total_tokens), 0) AS total_tokens "
                    f"FROM llm_usage WHERE {where_sql} GROUP BY day_jst ORDER BY day_jst DESC",
                    (where_arg,),
                ).fetchall()
            ]
            by_purpose = [
                dict(r)
                for r in c.execute(
                    "SELECT purpose, COUNT(*) AS calls, COALESCE(SUM(1 - ok), 0) AS failed_calls, "
                    "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
                    "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
                    "COALESCE(SUM(total_tokens), 0) AS total_tokens "
                    f"FROM llm_usage WHERE {where_sql} GROUP BY purpose ORDER BY total_tokens DESC, purpose ASC",
                    (where_arg,),
                ).fetchall()
            ]
            by_actor = [
                dict(r)
                for r in c.execute(
                    "SELECT actor, COUNT(*) AS calls, COALESCE(SUM(1 - ok), 0) AS failed_calls, "
                    "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
                    "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
                    "COALESCE(SUM(total_tokens), 0) AS total_tokens "
                    f"FROM llm_usage WHERE {where_sql} AND actor!='' GROUP BY actor ORDER BY total_tokens DESC, actor ASC",
                    (where_arg,),
                ).fetchall()
            ]
            by_model = [
                dict(r)
                for r in c.execute(
                    "SELECT model, MAX(base_url_host) AS base_url_host, COUNT(*) AS calls, "
                    "COALESCE(SUM(1 - ok), 0) AS failed_calls, "
                    "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
                    "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
                    "COALESCE(SUM(total_tokens), 0) AS total_tokens "
                    f"FROM llm_usage WHERE {where_sql} GROUP BY model ORDER BY total_tokens DESC, model ASC",
                    (where_arg,),
                ).fetchall()
            ]
            recent = [
                dict(r)
                for r in c.execute(
                    "SELECT ts, day_jst, purpose, actor, model, base_url_host, "
                    "prompt_tokens, completion_tokens, total_tokens, latency_ms, ok, fallback_used, error_code "
                    f"FROM llm_usage WHERE {where_sql} ORDER BY ts DESC, id DESC LIMIT ?",
                    (where_arg, clean_recent),
                ).fetchall()
            ]

        return {
            "ok": True,
            "days": clean_days,
            "period": clean_period,
            "totals": {
                "calls": int(tot["calls"] or 0),
                "ok_calls": int(tot["ok_calls"] or 0),
                "failed_calls": int(tot["failed_calls"] or 0),
                "fallback_calls": int(tot["fallback_calls"] or 0),
                "prompt_tokens": int(tot["pt"] or 0),
                "completion_tokens": int(tot["ct"] or 0),
                "total_tokens": int(tot["tt"] or 0),
            },
            "by_day": by_day,
            "by_purpose": by_purpose,
            "by_actor": by_actor,
            "by_model": by_model,
            "recent": recent,
            "config_issues": self.config_issues(),
        }

    # ---- paid-plugin quota: atomic reserve -> settle -------------------------------------------------------------
    # `check_plugin_quota` *reserves* one slot (insert a `held` row inside one IMMEDIATE transaction, under the
    # router lock), `record_plugin_call` settles it: ok -> `ok` (counts toward the daily limit), failure ->
    # `failed` (slot refunded for the daily limit, but still an attempt for the visitor hourly limit).
    # A reservation that is never settled expires after PLUGIN_QUOTA_HOLD_TTL_S and is then refunded as well.
    PLUGIN_QUOTA_HOLD_TTL_S = 300.0
    PLUGIN_OWNER_RESERVE_DEFAULT = 0.3
    PLUGIN_QUOTA_SCHEMA = """
    CREATE TABLE IF NOT EXISTS plugin_quota (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        day_jst TEXT NOT NULL,
        plugin_id TEXT NOT NULL,
        visitor_key TEXT NOT NULL,
        is_owner INTEGER NOT NULL,
        state TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plugin_quota_day ON plugin_quota(plugin_id, day_jst, state);
    CREATE INDEX IF NOT EXISTS idx_plugin_quota_visitor ON plugin_quota(plugin_id, visitor_key, ts);
    """

    @contextlib.contextmanager
    def _quota_conn(self):
        """Autocommit connection holding `plugin_quota` (+ `llm_usage`); in-memory when the router has no runtime dir."""
        if self._usage_db is None:
            mem = getattr(self, "_quota_mem", None)
            if mem is None:
                mem = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
                mem.executescript(USAGE_SCHEMA + self.PLUGIN_QUOTA_SCHEMA)
                self._quota_mem = mem
            yield mem
            return
        self._usage_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._usage_db, timeout=5, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(USAGE_SCHEMA + self.PLUGIN_QUOTA_SCHEMA)
            yield conn
        finally:
            conn.close()

    def _owner_reserve_ratio(self, override: Optional[float]) -> float:
        raw: Any = override if override is not None else self.env.get("AI_HOUSE_PLUGIN_OWNER_RESERVE")
        try:
            ratio = float(raw) if raw not in (None, "") else self.PLUGIN_OWNER_RESERVE_DEFAULT
        except (TypeError, ValueError):
            ratio = self.PLUGIN_OWNER_RESERVE_DEFAULT
        if ratio != ratio:  # NaN
            ratio = self.PLUGIN_OWNER_RESERVE_DEFAULT
        return min(1.0, max(0.0, ratio))

    @staticmethod
    def _quota_key(speaker_id: str) -> str:
        """Bucket key for a speaker; callers pass a stable identity (see `plugin_api.quota_speaker_key`)."""
        return str(speaker_id or "").strip()[:128] or "anonymous"

    def check_plugin_quota(
        self,
        plugin_id: str,
        *,
        speaker_id: str = "",
        is_owner: bool = True,
        daily_limit: int = 60,
        visitor_hourly_limit: int = 20,
        owner_reserve_ratio: Optional[float] = None,
    ) -> Optional[str]:
        """Atomically check AND reserve one call slot; returns the refusal reason, or None when a slot is now held.

        Every `None` result must be followed by exactly one `record_plugin_call` (ok or failed) for the same
        plugin / speaker / owner flag. Limits: `daily_limit` for everybody together; visitors (non-owners) together
        may use at most `daily_limit - ceil(daily_limit * owner_reserve_ratio)` of it (default ratio 0.3, env
        `AI_HOUSE_PLUGIN_OWNER_RESERVE`), so the owner always keeps a floor; each visitor key gets
        `visitor_hourly_limit` attempts (successful or not) per rolling hour.
        """
        pid = str(plugin_id or "").strip() or "unknown"
        eff_daily = max(1, min(int(daily_limit or 60), 5000))
        eff_hourly = max(1, int(visitor_hourly_limit or 20))
        ratio = self._owner_reserve_ratio(owner_reserve_ratio)
        now_ts = self._now()
        day_jst = dt.datetime.fromtimestamp(now_ts, JST).date().isoformat()
        vkey = self._quota_key(speaker_id)
        purpose = f"plugin:{pid}"
        with self._lock, self._quota_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("DELETE FROM plugin_quota WHERE ts < ?", (now_ts - 3 * 86400.0,))
                c.execute(
                    "UPDATE plugin_quota SET state='expired' WHERE state='held' AND ts < ?",
                    (now_ts - self.PLUGIN_QUOTA_HOLD_TTL_S,),
                )
                ledger_ok = int(c.execute(
                    "SELECT COUNT(*) FROM llm_usage WHERE day_jst=? AND purpose=? AND ok=1", (day_jst, purpose)
                ).fetchone()[0])
                q_ok, q_held = c.execute(
                    "SELECT COALESCE(SUM(state='ok'), 0), COALESCE(SUM(state='held'), 0) "
                    "FROM plugin_quota WHERE plugin_id=? AND day_jst=?",
                    (pid, day_jst),
                ).fetchone()
                used_today = max(ledger_ok, int(q_ok)) + int(q_held)
                if used_today >= eff_daily:
                    c.execute("ROLLBACK")
                    return f"插件 {pid} 已达到今日调用上限 ({used_today}/{eff_daily})"
                if not is_owner:
                    reserve = int(eff_daily * ratio)
                    if reserve < eff_daily * ratio - 1e-9:
                        reserve += 1
                    visitor_cap = max(0, eff_daily - reserve)
                    visitor_used = int(c.execute(
                        "SELECT COUNT(*) FROM plugin_quota WHERE plugin_id=? AND day_jst=? AND is_owner=0 "
                        "AND state IN ('ok', 'held')",
                        (pid, day_jst),
                    ).fetchone()[0])
                    if visitor_used >= visitor_cap:
                        c.execute("ROLLBACK")
                        return f"插件 {pid} 访客今日合计调用已达上限 ({visitor_used}/{visitor_cap})，其余额度留给房主"
                    attempts = int(c.execute(
                        "SELECT COUNT(*) FROM plugin_quota WHERE plugin_id=? AND visitor_key=? AND is_owner=0 AND ts>=?",
                        (pid, vkey, now_ts - 3600.0),
                    ).fetchone()[0])
                    if attempts >= eff_hourly:
                        c.execute("ROLLBACK")
                        return f"插件 {pid} 非房主每小时调用已达上限 ({attempts}/{eff_hourly})"
                c.execute(
                    "INSERT INTO plugin_quota(ts, day_jst, plugin_id, visitor_key, is_owner, state) "
                    "VALUES(?, ?, ?, ?, ?, 'held')",
                    (now_ts, day_jst, pid, vkey, 1 if is_owner else 0),
                )
                c.execute("COMMIT")
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
        return None

    def record_plugin_call(
        self,
        plugin_id: str,
        *,
        actor: str = "",
        model: str = "",
        base_url: str = "",
        speaker_id: str = "",
        is_owner: bool = True,
        latency_ms: int = 0,
        ok: bool = True,
        error_code: str = "",
        total_tokens: int = 0,
    ) -> None:
        """Settle the slot reserved by `check_plugin_quota` and record the call in `usage.db` under `plugin:<id>`."""
        pid = str(plugin_id or "").strip() or "unknown"
        now_ts = self._now()
        day_jst = dt.datetime.fromtimestamp(now_ts, JST).date().isoformat()
        vkey = self._quota_key(speaker_id)
        owner_flag = 1 if is_owner else 0
        new_state = "ok" if ok else "failed"
        with self._lock, self._quota_conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute(
                    "SELECT id FROM plugin_quota WHERE plugin_id=? AND visitor_key=? AND is_owner=? AND state='held' "
                    "AND ts>=? ORDER BY id LIMIT 1",
                    (pid, vkey, owner_flag, now_ts - self.PLUGIN_QUOTA_HOLD_TTL_S),
                ).fetchone()
                if row is not None:
                    c.execute("UPDATE plugin_quota SET state=? WHERE id=?", (new_state, int(row[0])))
                else:  # recorded without a prior reservation (legacy caller): still account for it
                    c.execute(
                        "INSERT INTO plugin_quota(ts, day_jst, plugin_id, visitor_key, is_owner, state) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (now_ts, day_jst, pid, vkey, owner_flag, new_state),
                    )
                c.execute("COMMIT")
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
        ep = ModelEndpoint(
            base_url=str(base_url or "plugin://local"),
            api_key="",
            model=str(model or pid),
        )
        self._record_usage(
            purpose=f"plugin:{pid}",
            actor=actor,
            endpoint=ep,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=int(total_tokens or 0),
            latency_ms=int(latency_ms or 0),
            ok=ok,
            fallback_used=False,
            error_code=error_code,
        )

    def resolve_total_budget(self, purpose: str = "chat") -> float:
        """Total wall-clock seconds one logical call may take, across retries, fallbacks and tool rounds."""
        p = purpose if purpose in DEFAULT_TOTAL_BUDGET_S else "chat"
        with self._lock:
            raw = self.env.get(f"AI_HOUSE_{p.upper()}_TOTAL_BUDGET_S") or self.env.get("AI_HOUSE_LLM_TOTAL_BUDGET_S")
        return max(_MIN_BUDGET_S, min(_safe_float(raw, DEFAULT_TOTAL_BUDGET_S[p]), _MAX_BUDGET_S))

    # ------------------------------------------------------------------ circuit breaker
    def _breaker_acquire(self, cap_key: Tuple[str, str]) -> str:
        """'allow' (closed), 'probe' (half-open, this caller is THE probe) or 'block' (open / probe in flight)."""
        with self._lock:
            st = self._breaker_state.get(cap_key)
            if not st:
                return "allow"
            now = self._now()
            if st["open_until"] > now:
                return "block"
            if st["open_until"] > 0:
                if st["probing"] and now - st["probe_ts"] < BREAKER_PROBE_STALE_S:
                    return "block"
                st["probing"] = True
                st["probe_ts"] = now
                return "probe"
            return "allow"

    def _breaker_release_probe(self, cap_key: Tuple[str, str]) -> None:
        with self._lock:
            st = self._breaker_state.get(cap_key)
            if st:
                st["probing"] = False

    def _breaker_is_open(self, cap_key: Tuple[str, str]) -> bool:
        with self._lock:
            st = self._breaker_state.get(cap_key)
            return bool(st and st["open_until"] > self._now())

    def _record_breaker_success(self, cap_key: Tuple[str, str]) -> None:
        with self._lock:
            self._breaker_state.pop(cap_key, None)

    def _record_breaker_failure(self, cap_key: Tuple[str, str]) -> bool:
        """Count a failure inside the time window; True when the breaker is (re)opened by it."""
        with self._lock:
            now = self._now()
            st = self._breaker_state.setdefault(cap_key, {"fails": [], "open_until": 0.0, "probing": False, "probe_ts": 0.0})
            if st["probing"]:
                st.update(probing=False, open_until=now + BREAKER_OPEN_S, fails=[now])
                return True
            fails = [t for t in st["fails"] if 0.0 <= now - t <= BREAKER_WINDOW_S]
            fails.append(now)
            st["fails"] = fails
            if len(fails) >= BREAKER_THRESHOLD:
                st.update(open_until=now + BREAKER_OPEN_S, fails=[])
                return True
            return False

    def _call_single_endpoint(
        self,
        ep: ModelEndpoint,
        base_payload: Mapping[str, Any],
        timeout_s: float,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        url = _chat_completions_url(ep.base_url)
        cap_key = (ep.base_url, ep.model)
        with self._lock:
            is_reasoning = cap_key in self._reasoning_caps
        payload = _adapt_reasoning_payload(base_payload) if is_reasoning else dict(base_payload)
        try:
            return _send_with_length_retry(url, ep.api_key, payload, timeout_s, deadline, retry_empty=True)
        except urllib.error.HTTPError as exc:
            detail = _extract_http_error_detail(exc, api_key=ep.api_key)
            if exc.code == 400 and not is_reasoning:
                retry_payload = _compat_retry_payload(base_payload, detail)
                if retry_payload is not None:
                    try:
                        doc = _send_with_length_retry(url, ep.api_key, retry_payload, timeout_s, deadline, retry_empty=True)
                    except urllib.error.HTTPError as retry_exc:
                        r_detail = _extract_http_error_detail(retry_exc, api_key=ep.api_key)
                        raise LLMAPIError(retry_exc.code, f"HTTP {retry_exc.code}: {r_detail}", detail=r_detail) from retry_exc
                    # remember the adapted parameter shape only once the adapted request has actually succeeded
                    with self._lock:
                        self._reasoning_caps.add(cap_key)
                    return doc
            raise LLMAPIError(exc.code, f"HTTP {exc.code}: {detail}", detail=detail) from exc

    def _attempt_once(
        self,
        ep: ModelEndpoint,
        payload: Mapping[str, Any],
        timeout_s: float,
        deadline: float,
        *,
        tools: Optional[Sequence[Dict[str, Any]]],
        retry_without_tools_on_400: bool,
    ) -> Dict[str, Any]:
        try:
            return self._call_single_endpoint(ep, payload, timeout_s, deadline)
        except LLMAPIError as exc:
            if exc.status_code == 400 and tools and retry_without_tools_on_400 and not isinstance(exc, LLMDeadlineError):
                plain_payload = {k: v for k, v in payload.items() if k not in ("tools", "tool_choice")}
                return self._call_single_endpoint(ep, plain_payload, timeout_s, deadline)
            raise

    def complete_messages(
        self,
        messages: Sequence[Dict[str, Any]],
        *,
        purpose: str = "chat",
        actor: str = "",
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        retry_without_tools_on_400: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout_s: Optional[float] = None,
        total_budget_s: Optional[float] = None,
    ) -> LLMCallResult:
        """Execute a chat completion across resolved endpoints with retries, failover, and usage logging.

        One total deadline (`total_budget_s`, default per purpose, env-configurable, and never later than an
        enclosing `deadline_scope`) covers every retry and fallback hop; each HTTP request gets
        `min(per-request timeout, remaining budget)`. When the budget is gone the call stops with `LLMDeadlineError`.
        """
        eps, issue = self._resolve(purpose, actor)
        if not eps:
            if issue is not None:
                self._warn_issue_once(issue)
                self._record_usage(
                    purpose=purpose,
                    actor=actor,
                    endpoint=ModelEndpoint(base_url=str(issue["base_url_host"]), api_key="", model=""),
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    latency_ms=0,
                    ok=False,
                    fallback_used=False,
                    error_code=str(issue["code"]),
                )
                raise RuntimeError(f"no usable endpoint: {issue['reason']}")
            raise RuntimeError("no API key configured")
        prof = self.resolve_profile(purpose)
        eff_temp = prof.temperature if temperature is None else float(temperature)
        eff_max = prof.max_tokens if max_tokens is None else int(max_tokens)
        eff_timeout = prof.timeout_s if timeout_s is None else float(timeout_s)
        started = time.monotonic()
        budget = self.resolve_total_budget(purpose) if total_budget_s is None else max(_MIN_BUDGET_S, float(total_budget_s))
        deadline = _effective_deadline(started + budget) or (started + budget)
        use_breaker = purpose != "ping"  # connectivity tests neither feed nor consult the traffic breaker

        def out_of_budget() -> bool:
            return deadline - time.monotonic() <= _DEADLINE_EPSILON_S

        def give_up(ep: ModelEndpoint, is_fallback: bool, cause: Optional[BaseException]) -> LLMDeadlineError:
            self._record_usage(
                purpose=purpose,
                actor=actor,
                endpoint=ep,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                latency_ms=int((time.monotonic() - started) * 1000),
                ok=False,
                fallback_used=is_fallback,
                error_code="DEADLINE",
            )
            if deadline < started + budget - 0.01:
                err = LLMDeadlineError("LLM 总时限已耗尽（外层调用共享的预算已用完），已停止重试与回退")
            else:
                err = LLMDeadlineError(f"LLM 总时限 {budget:.1f}s 已耗尽，已停止重试与回退")
            err.__cause__ = cause
            return err

        last_exc: Optional[BaseException] = None
        last_ep = eps[0]
        for ep, is_fallback in eps:
            last_ep = (ep, is_fallback)
            if out_of_budget():
                raise give_up(ep, is_fallback, last_exc)
            cap_key = (ep.base_url, ep.model)
            mode = self._breaker_acquire(cap_key) if use_breaker else "allow"
            if mode == "block":
                self._record_usage(
                    purpose=purpose,
                    actor=actor,
                    endpoint=ep,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    latency_ms=0,
                    ok=False,
                    fallback_used=is_fallback,
                    error_code="CIRCUIT_OPEN",
                )
                last_exc = LLMAPIError(503, "Endpoint circuit breaker open", detail="circuit_open")
                continue

            payload: Dict[str, Any] = {
                "model": ep.model,
                "messages": list(messages),
                "temperature": eff_temp,
                "max_tokens": eff_max,
            }
            if tools:
                payload["tools"] = [{"type": "function", "function": t} for t in tools]
                payload["tool_choice"] = "auto"

            try:
                for attempt in range(prof.max_retries + 1):
                    if attempt > 0 and out_of_budget():
                        raise give_up(ep, is_fallback, last_exc)
                    t0 = time.monotonic()
                    err_code = ""
                    err_tokens = 0
                    retryable = False
                    try:
                        doc = self._attempt_once(
                            ep, payload, eff_timeout, deadline,
                            tools=tools, retry_without_tools_on_400=retry_without_tools_on_400,
                        )
                        if use_breaker:
                            self._record_breaker_success(cap_key)
                        return self._finish_usage_success(doc, ep, purpose, actor, is_fallback, t0)
                    except LLMDeadlineError as exc:
                        raise give_up(ep, is_fallback, last_exc) from exc
                    except LLMAPIError as exc:
                        last_exc = exc
                        err_code = "EMPTY_REPLY" if exc.detail == "empty_reply" else f"HTTP_{exc.status_code}"
                        err_tokens = int(getattr(exc, "total_tokens", 0) or 0)
                        if exc.status_code in _TRANSIENT_HTTP_CODES:
                            opened = use_breaker and self._record_breaker_failure(cap_key)
                            retryable = (not opened) and exc.detail not in ("empty_reasoning_length", "empty_reply")
                    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                        last_exc = exc
                        err_code = type(exc).__name__
                        opened = use_breaker and self._record_breaker_failure(cap_key)
                        retryable = not opened
                    except Exception as exc:  # noqa: BLE001
                        last_exc = exc
                        err_code = type(exc).__name__
                    self._record_usage(
                        purpose=purpose,
                        actor=actor,
                        endpoint=ep,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=err_tokens,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        ok=False,
                        fallback_used=is_fallback,
                        error_code=err_code,
                    )
                    if retryable and attempt < prof.max_retries:
                        time.sleep(min(self.retry_delay_s * (attempt + 1), max(0.0, deadline - time.monotonic())))
                        continue
                    break
            finally:
                if use_breaker and mode == "probe":
                    self._breaker_release_probe(cap_key)

        assert last_exc is not None
        if isinstance(last_exc, OSError) and out_of_budget():
            raise give_up(last_ep[0], last_ep[1], last_exc)
        raise last_exc

    def _finish_usage_success(
        self,
        doc: Mapping[str, Any],
        ep: ModelEndpoint,
        purpose: str,
        actor: str,
        is_fallback: bool,
        t0: float,
    ) -> LLMCallResult:
        latency_ms = int((time.monotonic() - t0) * 1000)
        choice = (doc.get("choices") or [{}])[0]
        message = dict(choice.get("message") or {})
        content = str(message.get("content") or "").strip()
        usage = doc.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        tt = int(usage.get("total_tokens") or (pt + ct))
        self._record_usage(
            purpose=purpose,
            actor=actor,
            endpoint=ep,
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt,
            latency_ms=latency_ms,
            ok=True,
            fallback_used=is_fallback,
        )
        return LLMCallResult(
            message=message,
            content=content,
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt,
            model=ep.model,
            base_url=ep.base_url,
            fallback_used=is_fallback,
            latency_ms=latency_ms,
        )

    def complete_text(
        self,
        system: str,
        user: str,
        *,
        purpose: str = "chat",
        actor: str = "",
        timeout_s: Optional[float] = None,
    ) -> Tuple[str, int]:
        res = self.complete_messages(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            purpose=purpose,
            actor=actor,
            timeout_s=timeout_s,
        )
        return res.content, res.total_tokens

    def chat_fn(self, *, purpose: str = "chat") -> Callable[..., Tuple[str, int]]:
        def _call(system: str, user: str, *, actor: str = "") -> Tuple[str, int]:
            return self.complete_text(system, user, purpose=purpose, actor=actor)

        return _call
