"""Event-driven proactive talk engine with active hours, quotas, sleep guards & outbox routing (`0.3.0`)."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .chat_agent import LLMAPIError

JST = dt.timezone(dt.timedelta(hours=9))
DEFAULT_WINDOW = "09:00-22:30"
_WINDOW_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def parse_active_window(raw: str) -> Optional[Tuple[int, int]]:
    """Parse `'HH:MM-HH:MM'` into `(start_minute, end_minute)` in `[0, 1440)`, or `None` if always closed."""
    spec = str(raw or "").strip()
    if not spec:
        return 9 * 60, 22 * 60 + 30
    if spec.lower() in ("off", "none", "disabled", "0"):
        return None
    if spec.lower() in ("always", "all", "24h"):
        return 0, 0
    m = _WINDOW_RE.match(spec)
    if not m:
        return 9 * 60, 22 * 60 + 30
    sh, sm, eh, em = (int(x) for x in m.groups())
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        return 9 * 60, 22 * 60 + 30
    return sh * 60 + sm, eh * 60 + em


def in_active_window(now: float, window_spec: str) -> bool:
    """Return `True` when `now` falls inside the configured JST active hours window."""
    parsed = parse_active_window(window_spec)
    if parsed is None:
        return False
    start_m, end_m = parsed
    if start_m == end_m:
        return True
    cur = dt.datetime.fromtimestamp(float(now), JST)
    cur_m = cur.hour * 60 + cur.minute
    if start_m < end_m:
        return start_m <= cur_m < end_m
    return cur_m >= start_m or cur_m < end_m


def _safe_int(raw: Any, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ProactiveCandidate:
    """One newly observed world event eligible to spark proactive chatter."""

    source: str
    event_id: int
    ts: float
    actor: str
    text: str


class ProactiveEngine:
    """Converts fresh life/evidence events into quota-bounded proactive messages via `SendGate`."""

    def __init__(self, runtime_dir: Path | str, *, clock: Any = None) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.state_file = self.runtime_dir / "proactive_state.json"
        self.clock = clock
        self._lock = threading.RLock()

    def _now(self, now: Optional[float] = None) -> float:
        if now is not None:
            return float(now)
        if self.clock is not None:
            return float(self.clock.now())
        return dt.datetime.now(JST).timestamp()

    @staticmethod
    def _default_state(now: float) -> Dict[str, Any]:
        return {
            "initialized": False,
            "last_script_id": 0,
            "last_evidence_id": 0,
            "day_jst": dt.datetime.fromtimestamp(float(now), JST).date().isoformat(),
            "house_count": 0,
            "actor_counts": {},
            "last_sent_at": 0.0,
            "actor_last_sent": {},
        }

    def _load_locked(self, now: float) -> Dict[str, Any]:
        base = self._default_state(now)
        if not self.state_file.exists():
            return base
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return base
        if not isinstance(raw, dict):
            return base
        base["initialized"] = bool(raw.get("initialized", False))
        base["last_script_id"] = max(0, int(raw.get("last_script_id") or 0))
        base["last_evidence_id"] = max(0, int(raw.get("last_evidence_id") or 0))
        base["day_jst"] = str(raw.get("day_jst") or base["day_jst"])
        base["house_count"] = max(0, int(raw.get("house_count") or 0))
        base["last_sent_at"] = max(0.0, float(raw.get("last_sent_at") or 0.0))
        if isinstance(raw.get("actor_counts"), dict):
            base["actor_counts"] = {
                str(k): max(0, int(v or 0)) for k, v in raw["actor_counts"].items() if str(k).strip()
            }
        if isinstance(raw.get("actor_last_sent"), dict):
            base["actor_last_sent"] = {
                str(k): max(0.0, float(v or 0.0)) for k, v in raw["actor_last_sent"].items() if str(k).strip()
            }
        self._rollover_day_locked(base, now)
        return base

    def _save_locked(self, data: Mapping[str, Any]) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    @staticmethod
    def _rollover_day_locked(data: Dict[str, Any], now: float) -> bool:
        today = dt.datetime.fromtimestamp(float(now), JST).date().isoformat()
        if data.get("day_jst") == today:
            return False
        data["day_jst"] = today
        data["house_count"] = 0
        data["actor_counts"] = {}
        return True

    def is_initialized(self) -> bool:
        with self._lock:
            if not self.state_file.exists():
                return False
            st = self._load_locked(self._now())
            return bool(st.get("initialized"))

    def reset(self) -> None:
        with self._lock:
            if self.state_file.exists():
                self.state_file.unlink()

    @staticmethod
    def _read_max_cursors(world_db: Path | str) -> Tuple[int, int]:
        p = Path(world_db)
        if not p.exists():
            return 0, 0
        uri = p.resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as c:
            max_script = 0
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='script'").fetchone():
                max_script = int(c.execute("SELECT COALESCE(MAX(id), 0) FROM script").fetchone()[0] or 0)
            max_ev = 0
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='intent_evidence'").fetchone():
                max_ev = int(c.execute("SELECT COALESCE(MAX(rowid), 0) FROM intent_evidence").fetchone()[0] or 0)
            return max_script, max_ev

    def anchor(self, world_db: Path | str, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Anchor cursors at the current DB tail so enabling proactive talk never replays old history."""
        ts = self._now(now)
        max_script, max_ev = self._read_max_cursors(world_db)
        with self._lock:
            st = self._load_locked(ts)
            st["initialized"] = True
            st["last_script_id"] = max(int(st.get("last_script_id") or 0), max_script)
            st["last_evidence_id"] = max(int(st.get("last_evidence_id") or 0), max_ev)
            self._save_locked(st)
            return dict(st)

    def status_view(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        ts = self._now(now)
        with self._lock:
            return self._load_locked(ts)

    @staticmethod
    def _format_evidence(body_raw: str, roster: Sequence[str], names: Mapping[str, str]) -> Optional[Tuple[str, str]]:
        try:
            body = json.loads(body_raw or "{}")
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        actor = str(body.get("actor") or body.get("who") or "").strip()
        if actor not in roster:
            return None
        kind = str(body.get("kind") or "").strip()
        item = str(body.get("item") or body.get("what") or "").strip()
        place = str(body.get("place") or "").strip()
        who_name = names.get(actor, actor)
        if kind == "purchased" and item:
            return actor, f"{who_name}刚刚买到了{item}"
        if kind == "brought_home" and item:
            return actor, f"{who_name}把{item}带回了合租屋"
        if kind == "arrived" and place:
            return actor, f"{who_name}刚到达{place}"
        if kind == "departed" and place:
            return actor, f"{who_name}正准备出门去{place}"
        if kind == "invite_settled":
            return actor, f"{who_name}和大家一起定好了出门计划"
        if kind == "plan_finished":
            return actor, f"{who_name}刚完成了一项生活安排"
        if kind == "activity":
            act = str(body.get("activity") or "").strip()
            if act:
                return actor, f"{who_name}刚刚做完了 {act}"
        return None

    def _collect_new_events(
        self,
        world_db: Path | str,
        *,
        last_script_id: int,
        last_evidence_id: int,
        roster: Sequence[str],
        names: Mapping[str, str],
    ) -> Tuple[List[ProactiveCandidate], int, int]:
        p = Path(world_db)
        if not p.exists():
            return [], last_script_id, last_evidence_id
        cands: List[ProactiveCandidate] = []
        new_script = last_script_id
        new_ev = last_evidence_id
        uri = p.resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as c:
            c.row_factory = sqlite3.Row
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='script'").fetchone():
                s_rows = c.execute(
                    "SELECT id, ts, actors, text FROM script WHERE id>? ORDER BY id ASC LIMIT 50",
                    (last_script_id,),
                ).fetchall()
                for r in s_rows:
                    sid = int(r["id"])
                    if sid > new_script:
                        new_script = sid
                    txt = str(r["text"] or "").strip()
                    if not txt:
                        continue
                    try:
                        actors = json.loads(r["actors"] or "[]")
                    except ValueError:
                        actors = []
                    if not isinstance(actors, list):
                        continue
                    valid = [str(a).strip() for a in actors if str(a).strip() in roster]
                    if not valid:
                        continue
                    cands.append(
                        ProactiveCandidate(
                            source="script",
                            event_id=sid,
                            ts=float(r["ts"] or 0.0),
                            actor=valid[0],
                            text=txt,
                        )
                    )
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='intent_evidence'").fetchone():
                e_rows = c.execute(
                    "SELECT rowid AS eid, at, body FROM intent_evidence WHERE rowid>? ORDER BY rowid ASC LIMIT 50",
                    (last_evidence_id,),
                ).fetchall()
                for r in e_rows:
                    eid = int(r["eid"])
                    if eid > new_ev:
                        new_ev = eid
                    formatted = self._format_evidence(str(r["body"] or ""), roster, names)
                    if formatted is None:
                        continue
                    cands.append(
                        ProactiveCandidate(
                            source="intent_evidence",
                            event_id=eid,
                            ts=float(r["at"] or 0.0),
                            actor=formatted[0],
                            text=formatted[1],
                        )
                    )
        return cands, new_script, new_ev

    @staticmethod
    def _resident_sleeping(resident: Mapping[str, Any]) -> bool:
        reality = resident.get("reality") or {}
        kernel = resident.get("kernel") or {}
        return reality.get("availability") == "sleeping" or kernel.get("availability") == "sleeping"

    @staticmethod
    def _generate_message(
        daemon: Any,
        cand: ProactiveCandidate,
        target_scope: str,
        target_channel: str,
    ) -> Tuple[Optional[str], str]:
        name = daemon.vocab.NAMES.get(cand.actor, cand.actor)
        persona = daemon.vocab.SELVES.get(cand.actor, f"你是合租屋里的{name}。")
        scope_desc = "房主私聊" if target_scope == "private" else "合租屋群聊"
        sys_p = (
            f"你是合租屋住客{name}。人设：{persona}\n"
            f"请你只基于刚刚发生的生活状态事件，向{scope_desc}自然发一句 15–55 字的简短近况分享，"
            "不要编造事件里没有的事实，不要泄露私聊内容或系统信息。"
        )
        user_p = f"刚刚发生的生活事件：{cand.text}\n请用{name}第一人称说一句简短自然的话。"
        router = getattr(daemon, "model_router", None)
        if router is not None and router.has_live_endpoint(purpose="proactive", actor=cand.actor):
            try:
                res = router.complete_messages(
                    [{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
                    purpose="proactive",
                    actor=cand.actor,
                    max_tokens=160,
                )
                txt = str(res.content or "").strip()
                if txt:
                    return txt[:240], "live_api"
            except (LLMAPIError, RuntimeError):
                return None, "llm_error"
        llm_fn = getattr(getattr(daemon, "chat_agent", None), "_llm_fn", None)
        if callable(llm_fn):
            try:
                got = llm_fn(sys_p, user_p)
                if isinstance(got, (tuple, list)):  # (text, tokens) convention of the chat agent
                    got = got[0] if got else ""
                txt = str(got or "").strip()
                if txt:
                    return txt[:240], "injected_llm"
            except Exception:  # noqa: BLE001
                return None, "llm_error"
        if target_channel != "webchat":
            return None, "no_live_model"
        return f"我这边刚{cand.text}～", "demo"

    def scan(
        self,
        daemon: Any,
        *,
        cfg: Optional[Mapping[str, Any]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Inspect newly appended world events, enforce window/caps before LLM, and enqueue 1 message at most."""
        if getattr(daemon, "needs_onboarding", True) or getattr(daemon, "authority", None) is None:
            return {"ok": True, "triggered": False, "reason": "not_ready"}

        ts = self._now(now)
        eff_cfg = dict(cfg or {})
        window_spec = str(eff_cfg.get("window") or DEFAULT_WINDOW).strip()
        actor_cap = _safe_int(eff_cfg.get("actor_daily_cap"), 2, minimum=0)
        house_cap = _safe_int(eff_cfg.get("house_daily_cap"), 6, minimum=0)
        min_iv_s = float(_safe_int(eff_cfg.get("min_interval_min"), 45, minimum=1)) * 60.0
        target_channel = str(eff_cfg.get("target_channel") or "webchat").strip() or "webchat"
        target_scope = str(eff_cfg.get("target_scope") or "group").strip().lower()
        if target_scope not in ("group", "private"):
            target_scope = "group"

        target_group_id = str(
            eff_cfg.get("target_group_id")
            or (getattr(daemon, "env", None) or {}).get("AI_HOUSE_PROACTIVE_GROUP_ID")
            or ""
        ).strip()
        if target_scope == "group" and target_channel in ("onebot_v11", "onebot"):
            whitelisted_groups: List[str] = []
            for ch_obj in getattr(daemon, "channels", ()):
                if str(getattr(ch_obj, "channel_id", "") or "") == "onebot_v11":
                    cfg_obj = getattr(ch_obj, "cfg", None)
                    if cfg_obj is not None and getattr(cfg_obj, "groups", None):
                        whitelisted_groups = sorted(str(g) for g in cfg_obj.groups if str(g).strip())
                    break
            if not whitelisted_groups:
                raw_env_g = str((getattr(daemon, "env", None) or {}).get("ONEBOT_GROUPS") or "").strip()
                if raw_env_g:
                    whitelisted_groups = [x.strip() for x in re.split(r"[,;\s]+", raw_env_g) if x.strip()]
            if not target_group_id:
                if len(whitelisted_groups) == 1:
                    target_group_id = whitelisted_groups[0]
                else:
                    return {"ok": True, "triggered": False, "reason": "missing_target_group"}
            if whitelisted_groups and target_group_id not in whitelisted_groups:
                return {"ok": True, "triggered": False, "reason": "group_not_whitelisted"}
            eff_group_channel = f"qq-group:{target_group_id}"
        else:
            eff_group_channel = "group"

        if target_scope == "group" and getattr(daemon, "group_behavior", None) is not None:
            gb = daemon.group_behavior
            if gb.is_muted(eff_group_channel, now=ts) or (
                eff_group_channel != "group" and gb.is_muted("group", now=ts)
            ):
                return {"ok": True, "triggered": False, "reason": "group_muted"}

        if target_channel != "webchat":
            # Nothing can deliver to a channel that is not loaded: do not consume events, spend an LLM call, enqueue
            # an outbox row that would sit pending forever, or charge the daily quota.
            wanted = "onebot_v11" if target_channel == "onebot" else target_channel
            loaded = {str(getattr(c, "channel_id", "") or "") for c in getattr(daemon, "channels", ())}
            if wanted not in loaded:
                return {"ok": True, "triggered": False, "reason": "channel_unavailable"}

        with self._lock:
            st = self._load_locked(ts)
            if not st["initialized"]:
                anchored = self.anchor(daemon.world_db, now=ts)
                return {
                    "ok": True,
                    "triggered": False,
                    "reason": "cursor_anchored",
                    "state": anchored,
                }
            last_sid = int(st["last_script_id"])
            last_eid = int(st["last_evidence_id"])

        cands, new_sid, new_eid = self._collect_new_events(
            daemon.world_db,
            last_script_id=last_sid,
            last_evidence_id=last_eid,
            roster=daemon.vocab.ROSTER,
            names=daemon.vocab.NAMES,
        )

        with self._lock:
            st = self._load_locked(ts)
            st["last_script_id"] = max(int(st["last_script_id"]), new_sid)
            st["last_evidence_id"] = max(int(st["last_evidence_id"]), new_eid)
            self._save_locked(st)

        if not cands:
            return {"ok": True, "triggered": False, "reason": "no_new_events"}
        if not in_active_window(ts, window_spec):
            return {"ok": True, "triggered": False, "reason": "outside_window"}

        with self._lock:
            st = self._load_locked(ts)
            if house_cap <= 0 or int(st["house_count"]) >= house_cap:
                return {"ok": True, "triggered": False, "reason": "house_daily_cap"}
            if st["last_sent_at"] > 0 and (ts - float(st["last_sent_at"])) < min_iv_s:
                return {"ok": True, "triggered": False, "reason": "min_interval"}
            actor_counts = dict(st["actor_counts"])

        state_snap = daemon.get_state()
        residents = state_snap.get("residents") or {}

        chosen: Optional[ProactiveCandidate] = None
        saw_awake = False
        for cand in reversed(cands):
            res_info = residents.get(cand.actor) or {}
            if self._resident_sleeping(res_info):
                continue
            saw_awake = True
            if actor_cap <= 0 or int(actor_counts.get(cand.actor, 0)) >= actor_cap:
                continue
            chosen = cand
            break

        if chosen is None:
            reason = "actor_daily_cap" if saw_awake else "all_sleeping"
            return {"ok": True, "triggered": False, "reason": reason}

        text, gen_mode = self._generate_message(daemon, chosen, target_scope, target_channel)
        if not text:
            return {"ok": True, "triggered": False, "reason": gen_mode}

        # the real group channel the message goes to (web `group`, or `qq-group:<id>`), the same id recall uses
        out_ch = eff_group_channel if target_channel in ("webchat", "onebot_v11", "onebot") else target_channel
        check_ch = f"private:{chosen.actor}" if target_scope == "private" else out_ch
        if getattr(daemon, "guard", None) is not None:
            why = daemon.guard.check(check_ch, text, actor=chosen.actor)
            if why:
                return {"ok": True, "triggered": False, "reason": f"blocked:{why}"}
        if target_scope != "private" and getattr(daemon, "memory", None) is not None:
            if daemon.memory.check_outbound_leak(chosen.actor, "group", text, channel=out_ch, now=ts):
                return {"ok": True, "triggered": False, "reason": "blocked:memory_leak"}

        with self._lock:
            st = self._load_locked(ts)
            if house_cap <= 0 or int(st["house_count"]) >= house_cap:
                return {"ok": True, "triggered": False, "reason": "house_daily_cap"}
            cur_actor_cnt = int(st["actor_counts"].get(chosen.actor, 0))
            if actor_cap <= 0 or cur_actor_cnt >= actor_cap:
                return {"ok": True, "triggered": False, "reason": "actor_daily_cap"}

        payload: Dict[str, Any] = {
            "actor": chosen.actor,
            "kind": target_scope,
            "scope": target_scope,
            "text": text,
            "source": "proactive",
            "mode": gen_mode,
            "event_id": chosen.event_id,
            "created_ts": ts,
        }
        if target_channel == "webchat":
            payload["target_channel"] = f"private:{chosen.actor}" if target_scope == "private" else "group"
        elif target_channel in ("onebot_v11", "onebot") and target_scope == "group" and target_group_id:
            payload["group_id"] = target_group_id
            payload["target"] = target_group_id
        if target_scope == "private":
            payload["to_identity"] = "owner"

        delivery_id = daemon.enqueue_outbound(
            channel=target_channel,
            payload=payload,
            event_id=chosen.event_id,
        )
        flush_res = daemon.flush_outbox(reconcile=False)
        disp_state = str((flush_res.get("dispatched") or {}).get(delivery_id) or "")
        if disp_state == "blocked":
            return {
                "ok": True,
                "triggered": False,
                "reason": "outbox_blocked",
                "delivery_id": delivery_id,
                "flush": flush_res,
            }

        with self._lock:
            st = self._load_locked(ts)
            cur_actor_cnt = int(st["actor_counts"].get(chosen.actor, 0))
            st["house_count"] = int(st["house_count"]) + 1
            st["actor_counts"][chosen.actor] = cur_actor_cnt + 1
            st["last_sent_at"] = ts
            st["actor_last_sent"][chosen.actor] = ts
            self._save_locked(st)

        return {
            "ok": True,
            "triggered": True,
            "actor": chosen.actor,
            "source": chosen.source,
            "delivery_id": delivery_id,
            "text": text,
            "mode": gen_mode,
            "flush": flush_res,
        }


__all__ = [
    "DEFAULT_WINDOW",
    "ProactiveCandidate",
    "ProactiveEngine",
    "in_active_window",
    "parse_active_window",
]
