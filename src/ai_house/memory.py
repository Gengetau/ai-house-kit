"""The house's long-term memory: the names other modules import, and the small parts every store shares.

The store itself is `ai_house.memory_common.CommonMemoryStore` (database `memory-common.db`), which runs the memory
code shared with the private deployment (`ai_house/common/memory.py`). `MemoryStore` here is that same class, kept
under the name callers have always used. The earlier self-contained implementation (database `memory.db`) is gone;
a house that still has a `memory.db` gets its memories carried over at start (`ai_house.memory_migrate`).

What lives in this file: the vector-service client, the text cleaners used before anything reaches the shared code,
the two maintainer switches' names and the parser for the leak-free group list, and the narrow facade the daemon
hands to conversation code.
"""
from __future__ import annotations

import array
import json
import logging
import math
import re
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple, Union

if TYPE_CHECKING:  # the store imports this module; the name is resolved lazily at the bottom
    from .memory_common import CommonMemoryStore as MemoryStore

log = logging.getLogger("ai_house.memory")

SCOPES = ("private", "group", "world")
KINDS = ("person", "preference", "event", "promise", "joke", "fact", "claim", "story", "rule", "diary")

MAX_LINE_CHARS = 800
MAX_TEXT_CHARS = 140
DEFAULT_RECALL_MAX_CHARS = 700

_ACTOR_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_RESERVED_OWNERS = {"shared", "public", "system", "owner", "all"}
_CHANNEL_BAD_RE = re.compile(r"[\x00-\x1f\x7f\s]")
LEAK_FREE_GROUPS_ENV = "AI_HOUSE_MEMORY_LEAK_FREE_GROUPS"
PRIVATE_IN_GROUPS_ENV = "AI_HOUSE_MEMORY_PRIVATE_IN_GROUPS"
_GROUPS_SPLIT_RE = re.compile(r"[,，;；\s]+")
_QQ_GROUP_ITEM_RE = re.compile(r"qq-group:[0-9]{1,20}")


def parse_leak_free_groups(raw: object) -> Tuple[List[str], List[str]]:
    """Parse `AI_HOUSE_MEMORY_LEAK_FREE_GROUPS` into `(valid channel ids, rejected items)`.

    A valid item is exactly a group channel id as the house spells it: `group` (the web group) or
    `qq-group:<digits>`. Blanks around items and around the colon are tolerated, duplicates are dropped, and
    anything else (including every `private...` spelling) is rejected: callers either refuse to start
    (`ai-house up`) or ignore the item (the store), never treat it as open.
    """
    text = re.sub(r"\s*[:：]\s*", ":", str(raw or ""))
    valid: List[str] = []
    bad: List[str] = []
    for part in _GROUPS_SPLIT_RE.split(text):
        if not part:
            continue
        low = part.lower()
        item = "group" if low == "group" else (low if _QQ_GROUP_ITEM_RE.fullmatch(low) else "")
        if not item:
            bad.append(part)
        elif item not in valid:
            valid.append(item)
    return valid, bad


_NAME_BAD_RE = re.compile(r"[\[\]|<>{}`【】［］｜\r\n\t]")
_ROLE_CLAIM_RE = re.compile(r"管理员|房主|室友|系统|已验证|\bowner\b|\badmin\b|\bbot\b|\bsystem\b", re.I)


class MemoryRefusedError(ValueError):
    """Raised when an upstream extractor model refuses a batch outright (0 completion tokens)."""


class VectorEmbedder:
    """Optional vector search plugin interface (`embed(texts) -> list[list[float]] | None`)."""

    def embed(self, texts: Sequence[str]) -> Optional[List[array.array]]:
        raise NotImplementedError


class HttpOpenAIEmbedder(VectorEmbedder):
    """Stdlib HTTP client for any OpenAI-compatible `/v1/embeddings` endpoint (opt-in only)."""

    def __init__(
        self,
        embed_url: str,
        *,
        api_key: str = "",
        model: str = "",
        timeout_s: float = 5.0,
    ):
        url = embed_url.strip().rstrip("/")
        if url and not url.endswith("/embeddings"):
            url = (url + "/embeddings") if url.endswith("/v1") else (url + "/v1/embeddings")
        self.url = url
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout_s = float(timeout_s)

    def ask(self, texts: Sequence[str]) -> List[array.array]:
        """One request to the service; raises whatever went wrong (`embed` is the quiet form the store uses)."""
        payload: Dict[str, Any] = {"input": [str(t or "")[:1000] or "-" for t in texts]}
        if self.model:
            payload["model"] = self.model
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ai-house-kit/0.2 (+https://github.com/Gengetau/ai-house-kit)",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            doc = json.loads(resp.read().decode("utf-8", errors="replace"))
        items = sorted(doc.get("data") or [], key=lambda d: int(d.get("index", 0)))
        if len(items) != len(texts):
            raise ValueError("the service answered with a different number of vectors than texts")
        return [_normalize_vec(item.get("embedding") or ()) for item in items]

    def embed(self, texts: Sequence[str]) -> Optional[List[array.array]]:
        if not self.url or not texts:
            return [] if not texts else None
        try:
            return self.ask(texts)
        except Exception as exc:  # noqa: BLE001
            log.debug("vector embedding unavailable (%s)", type(exc).__name__)
            return None


def _normalize_vec(values: Sequence[float]) -> array.array:
    vals = [float(x) for x in values]
    if not vals:
        raise ValueError("empty embedding vector")
    norm = math.sqrt(sum(x * x for x in vals)) or 1.0
    return array.array("f", (x / norm for x in vals))


def _clean_text(text: object, limit: int = MAX_TEXT_CHARS) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))).strip()[:limit]


def _clean_line(text: object, *, single_line: bool = False) -> str:
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", " ", raw)
    if single_line:
        raw = re.sub(r"\s+", " ", raw)
    return raw.strip()[:MAX_LINE_CHARS]


def _clean_speaker(name: object, role: str) -> str:
    if role == "owner":
        n = _NAME_BAD_RE.sub(" ", str(name or "")).strip()[:24]
        return n or "房主"
    if role == "self":
        n = _NAME_BAD_RE.sub(" ", str(name or "")).strip()[:24]
        return n or "自己"
    if role == "world":
        return "世界记录"
    n = _NAME_BAD_RE.sub(" ", str(name or ""))
    n = _ROLE_CLAIM_RE.sub("·", n)
    n = re.sub(r"\d{5,}", "#", n)
    n = re.sub(r"\s+", " ", n).strip()[:24]
    return n or "群友"


def _openai_json_extractor(
    system: str,
    user: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout_s: float = 45.0,
) -> Dict[str, Any]:
    from .chat_agent import post_openai_chat_json

    doc = post_openai_chat_json(
        base_url=base_url,
        api_key=api_key,
        payload={
            "model": model,
            "temperature": 0.2,
            "max_tokens": 2000,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout_s=timeout_s,
    )
    content = str((((doc.get("choices") or [{}])[0]).get("message") or {}).get("content") or "")
    if not content.strip() and not ((doc.get("usage") or {}).get("completion_tokens") or 0):
        raise MemoryRefusedError("memory extractor model returned empty refusal")
    return _parse_ops_json(content)


def _parse_ops_json(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, tuple) and raw:
        raw = raw[0]
    text = str(raw or "").strip()
    if not text:
        raise MemoryRefusedError("empty extractor output")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in memory extractor response")
    return json.loads(m.group(0))


@dataclass(frozen=True)
class NarrowMemoryAPI:
    """Narrow conversation-entry facade (`observe`, `recall`, `extract`) for `HouseDaemon`."""

    store: "MemoryStore"

    def observe(
        self,
        actor: str,
        scope: str,
        text: str,
        *,
        channel: str = "",
        speaker: str = "",
        speaker_role: Optional[str] = None,
        speaker_id: str = "",
        reply: str = "",
        now: Optional[float] = None,
    ) -> int:
        return self.store.observe(
            actor,
            scope,
            text,
            channel=channel,
            speaker=speaker,
            speaker_role=speaker_role,
            speaker_id=speaker_id,
            reply=reply,
            now=now,
        )

    def recall(
        self,
        actor: str,
        scope: str,
        text: str,
        *,
        channel: str = "",
        about: Sequence[str] = (),
        k: Optional[int] = None,
        now: Optional[float] = None,
    ) -> str:
        return self.store.recall(actor, scope, text, channel=channel, about=about, k=k, now=now)

    def extract(
        self,
        actor: Optional[str] = None,
        *,
        force: bool = False,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self.store.extract(actor=actor, force=force, now=now)


_default_store: Optional["MemoryStore"] = None
_default_lock = threading.Lock()


def configure(data_dir: Union[str, Path], **kwargs: Any) -> "MemoryStore":
    """Configure and return the module-level store for functional callers."""
    global _default_store
    from .memory_common import CommonMemoryStore

    with _default_lock:
        if _default_store is not None:
            _default_store.close()
        _default_store = CommonMemoryStore(data_dir, **kwargs)
        return _default_store


def reset_default_store() -> None:
    """Close and clear the module-level store."""
    global _default_store
    with _default_lock:
        if _default_store is not None:
            _default_store.close()
            _default_store = None


def _require_store() -> "MemoryStore":
    if _default_store is None:
        raise RuntimeError("MemoryStore 尚未初始化：请先实例化 MemoryStore(runtime_dir) 或调用 memory.configure(runtime_dir)")
    return _default_store


def observe(actor: str, scope: str, text: str, **kwargs: Any) -> int:
    return _require_store().observe(actor, scope, text, **kwargs)


def recall(actor: str, scope: str, text: str, **kwargs: Any) -> str:
    return _require_store().recall(actor, scope, text, **kwargs)


def extract(actor: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
    return _require_store().extract(actor=actor, **kwargs)


def __getattr__(name: str) -> Any:
    if name == "MemoryStore":  # the store imports this module, so the class is looked up on first use
        from .memory_common import CommonMemoryStore

        return CommonMemoryStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "HttpOpenAIEmbedder",
    "KINDS",
    "MemoryRefusedError",
    "MemoryStore",
    "NarrowMemoryAPI",
    "SCOPES",
    "VectorEmbedder",
    "configure",
    "extract",
    "observe",
    "recall",
    "reset_default_store",
]
