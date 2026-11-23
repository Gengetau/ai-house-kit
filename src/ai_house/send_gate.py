"""Unified outbound SendGate enforcing leak guards, identity checks, JST quiet hours & rate caps (`0.3.0`)."""
from __future__ import annotations

import contextlib
import datetime as dt
import re
import sqlite3
import threading
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Mapping, Optional, Tuple

from worldkernel.outbox import BlockedDelivery, NotSent

JST = dt.timezone(dt.timedelta(hours=9))
DEFAULT_QUIET_HOURS = "23:30-07:00"
DEFAULT_HOURLY_CAP = 10
DEFAULT_DAILY_CAP = 60

_QUIET_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def parse_quiet_hours(raw: str) -> Optional[Tuple[int, int]]:
    """Parse `'HH:MM-HH:MM'` into `(start_minute, end_minute)` in `[0, 1440)`, or `None` if disabled."""
    spec = str(raw or "").strip()
    if not spec or spec.lower() in ("off", "none", "disabled", "0"):
        return None
    m = _QUIET_RE.match(spec)
    if not m:
        return 23 * 60 + 30, 7 * 60
    sh, sm, eh, em = (int(x) for x in m.groups())
    if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
        return 23 * 60 + 30, 7 * 60
    return sh * 60 + sm, eh * 60 + em


def in_quiet_hours(now: float, window_spec: str) -> bool:
    parsed = parse_quiet_hours(window_spec)
    if parsed is None:
        return False
    start_m, end_m = parsed
    if start_m == end_m:
        return False
    d = dt.datetime.fromtimestamp(float(now), JST)
    cur_m = d.hour * 60 + d.minute
    if start_m < end_m:
        return start_m <= cur_m < end_m
    return cur_m >= start_m or cur_m < end_m


def _safe_int(raw: Any, default: int) -> int:
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default


class _GatedChannelAdapter:
    """`worldkernel.outbox.Dispatcher` adapter backed by `SendGate` + `ChannelPlugin`."""

    def __init__(self, gate: "SendGate", channel_id: str, plugin: Any = None, *, is_webchat: bool = False) -> None:
        self._gate = gate
        self.channel_id = channel_id
        self._plugin = plugin
        self._is_webchat = is_webchat
        self.idempotent = bool(is_webchat or getattr(plugin, "idempotent_delivery", False))

    def send(self, delivery_id: str, payload: Dict[str, Any]) -> Optional[str]:
        return self._gate.deliver_gated(
            self.channel_id,
            self._plugin,
            delivery_id,
            dict(payload or {}),
            is_webchat=self._is_webchat,
        )

    def lookup(self, delivery_id: str) -> Optional[str]:
        return self._gate.lookup_delivery(self.channel_id, self._plugin, delivery_id, is_webchat=self._is_webchat)


class SendGate:
    """Shared outbound gate wired between `worldkernel.outbox.Dispatcher` and chat channels."""

    def __init__(self, daemon: Any) -> None:
        self.daemon = daemon
        self._lock = threading.RLock()
        self._sent_hourly: Dict[str, Deque[float]] = {}
        self._sent_daily: Deque[Tuple[str, float]] = deque()
        self._webchat_refs: Dict[str, str] = {}
        self._stats: Dict[str, int] = {
            "sent": 0,
            "blocked": 0,
            "deferred_quiet_hours": 0,
            "deferred_rate_limit": 0,
            "unknown": 0,
        }

    def _world_conn(self) -> Optional[sqlite3.Connection]:
        wdb = getattr(self.daemon, "world_db", None)
        if not wdb or not Path(wdb).is_file():
            return None
        c = sqlite3.connect(str(wdb), timeout=5)
        c.execute(
            "CREATE TABLE IF NOT EXISTS webchat_deliveries ("
            "delivery_id TEXT PRIMARY KEY, channel TEXT NOT NULL, msg_id INTEGER NOT NULL, "
            "ref TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS send_gate_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT UNIQUE, "
            "recip_key TEXT NOT NULL, day_jst TEXT NOT NULL, sent_at REAL NOT NULL)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_send_gate_log_ts ON send_gate_log(sent_at, day_jst)")
        return c

    def reset(self) -> None:
        with self._lock:
            self._sent_hourly.clear()
            self._sent_daily.clear()
            self._webchat_refs.clear()
            for k in self._stats:
                self._stats[k] = 0
            c = self._world_conn()
            if c is not None:
                with contextlib.closing(c):
                    with c:
                        c.execute("DELETE FROM webchat_deliveries")
                        c.execute("DELETE FROM send_gate_log")

    def stats_view(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def channel_map(self) -> Dict[str, _GatedChannelAdapter]:
        mapping: Dict[str, _GatedChannelAdapter] = {
            "webchat": _GatedChannelAdapter(self, "webchat", None, is_webchat=True),
        }
        for ch in getattr(self.daemon, "channels", ()):
            cid = str(getattr(ch, "channel_id", "") or "").strip()
            if not cid:
                continue
            adapter = _GatedChannelAdapter(self, cid, ch, is_webchat=False)
            mapping[cid] = adapter
            if cid == "onebot_v11" and "onebot" not in mapping:
                mapping["onebot"] = adapter
        return mapping

    def lookup_delivery(self, channel_id: str, plugin: Any, delivery_id: str, *, is_webchat: bool) -> Optional[str]:
        if is_webchat:
            with self._lock:
                cached = self._webchat_refs.get(delivery_id)
                if cached:
                    return cached
                c = self._world_conn()
                if c is not None:
                    with contextlib.closing(c):
                        row = c.execute(
                            "SELECT ref FROM webchat_deliveries WHERE delivery_id=?",
                            (str(delivery_id),),
                        ).fetchone()
                    if row is not None and row[0]:
                        ref = str(row[0])
                        self._webchat_refs[delivery_id] = ref
                        return ref
                return None
        if plugin is not None and hasattr(plugin, "lookup_delivery"):
            return plugin.lookup_delivery(delivery_id)
        return None

    def resolve_destination(
        self,
        channel_id: str,
        plugin: Any,
        payload: Mapping[str, Any],
        *,
        is_webchat: bool,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Resolve a single canonical `(eff_scope, eff_target, normalized_payload)` before policy checks."""
        d = self.daemon
        out = dict(payload or {})
        raw_actor = str(out.get("actor") or "").strip()
        raw_kind = str(out.get("kind") or "").strip().lower()
        raw_scope = str(out.get("scope") or "").strip().lower()
        raw_tch = str(out.get("target_channel") or "").strip()
        raw_target = str(out.get("target") or "").strip()
        raw_gid = str(out.get("group_id") or "").strip()
        raw_uid = str(out.get("user_id") or "").strip()
        to_identity = str(out.get("to_identity") or "").strip()

        group_signals = any([
            raw_kind == "group",
            raw_scope in ("group", "world", "public"),
            raw_tch == "group",
            raw_tch.startswith("qq-group:"),
            raw_target == "group",
            raw_target.startswith("qq-group:"),
            bool(raw_gid),
        ])
        private_signals = any([
            raw_kind == "private",
            raw_scope == "private",
            raw_tch.startswith("private:"),
            raw_target.startswith("private:"),
            bool(raw_uid),
        ])
        if group_signals and private_signals:
            raise BlockedDelivery("inconsistent_scope")

        if raw_kind in ("group", "private"):
            eff_kind = raw_kind
        elif raw_scope == "private":
            eff_kind = "private"
        elif raw_scope in ("group", "world", "public"):
            eff_kind = "group"
        elif group_signals:
            eff_kind = "group"
        elif private_signals:
            eff_kind = "private"
        elif is_webchat:
            eff_kind = "group" if not raw_actor else "private"
        else:
            eff_kind = "private"
        eff_scope = "private" if eff_kind == "private" else "group"

        id_store = getattr(d, "identity_store", None)
        ident = None
        if to_identity and id_store is not None:
            ident = id_store.get_identity(to_identity)
            if ident is None:
                raise NotSent("unknown_identity")
            if ident["kind"] == "bot":
                raise BlockedDelivery("bot_recipient")

        if is_webchat:
            if eff_kind == "group":
                if raw_tch and raw_target and raw_tch != raw_target:
                    raise BlockedDelivery("inconsistent_target")
                chosen_grp = raw_tch or raw_target or "group"
                if (
                    chosen_grp != "group"
                    and not chosen_grp.startswith("qq-group:")
                    and not chosen_grp.startswith("group:")
                ) or any(c.isspace() for c in chosen_grp):
                    raise BlockedDelivery("unsupported_target_channel")
                eff_target = chosen_grp
                out["kind"] = "group"
                out["scope"] = "group"
                out["target"] = eff_target
                out["target_channel"] = eff_target
                out["actor"] = raw_actor
                return "group", eff_target, out

            tch_actor = raw_tch.split(":", 1)[1].strip() if raw_tch.startswith("private:") else ""
            tgt_actor = raw_target.split(":", 1)[1].strip() if raw_target.startswith("private:") else ""
            chosen = {x for x in (raw_actor, tch_actor, tgt_actor) if x}
            if len(chosen) > 1:
                raise BlockedDelivery("inconsistent_target")
            eff_actor = next(iter(chosen), "")
            if not eff_actor or eff_actor not in d.vocab.ROSTER:
                raise BlockedDelivery("unknown_actor")
            if ident is not None and ident["kind"] != "owner":
                raise BlockedDelivery("private_not_owner")
            eff_ch = f"private:{eff_actor}"
            out["kind"] = "private"
            out["scope"] = "private"
            out["actor"] = eff_actor
            out["target"] = eff_ch
            out["target_channel"] = eff_ch
            return "private", eff_ch, out

        # External channel plugin (`onebot_v11` / generic `ChannelPlugin`)
        is_onebot = channel_id in ("onebot_v11", "onebot")
        if eff_kind == "group":
            cand = raw_gid or raw_target or raw_tch
            if cand.startswith("qq-group:"):
                cand = cand.split(":", 1)[1].strip()
            eff_target = cand
        else:
            cand = raw_uid or raw_target
            if not cand and to_identity and id_store is not None:
                plat = "onebot" if is_onebot else channel_id
                targets = id_store.external_targets_for(to_identity, platform=plat)
                if not targets:
                    raise NotSent("unmapped_identity")
                cand = str(targets[0]["external_id"]).strip()
            eff_target = cand

        if is_onebot:
            if not eff_target or not eff_target.isdigit():
                raise NotSent("invalid_target")
            cfg = getattr(plugin, "cfg", None)
            if cfg is not None:
                if eff_kind == "group" and eff_target not in cfg.groups:
                    raise BlockedDelivery("group_not_whitelisted")
                if eff_kind == "private":
                    in_owners = eff_target in cfg.owners
                    mapped_owner = bool(id_store is not None and id_store.is_owner_external("onebot", eff_target))
                    bound_ident = False
                    if ident is not None and id_store is not None:
                        bound_ident = any(
                            str(t.get("external_id")) == eff_target
                            for t in id_store.external_targets_for(to_identity, platform="onebot")
                        )
                    if not (in_owners or mapped_owner or bound_ident):
                        raise BlockedDelivery("recipient_not_allowed")

        out["kind"] = eff_kind
        out["scope"] = eff_scope
        out["actor"] = raw_actor
        out["target"] = eff_target
        if eff_kind == "group":
            out.pop("user_id", None)
            if is_onebot and eff_target:
                out["group_id"] = eff_target
        else:
            out.pop("group_id", None)
            if is_onebot and eff_target:
                out["user_id"] = eff_target
        return eff_scope, eff_target, out

    def _rate_counts(self, recip_key: str, now: float, today_jst: str) -> Tuple[int, int]:
        c = self._world_conn()
        if c is not None:
            with contextlib.closing(c):
                h_row = c.execute(
                    "SELECT COUNT(*) FROM send_gate_log WHERE recip_key=? AND sent_at>?",
                    (recip_key, float(now) - 3600.0),
                ).fetchone()
                d_row = c.execute(
                    "SELECT COUNT(*) FROM send_gate_log WHERE day_jst=?",
                    (today_jst,),
                ).fetchone()
                return int(h_row[0] or 0), int(d_row[0] or 0)

        q_hour = self._sent_hourly.setdefault(recip_key, deque())
        while q_hour and (now - q_hour[0]) >= 3600.0:
            q_hour.popleft()
        while self._sent_daily and self._sent_daily[0][0] != today_jst:
            self._sent_daily.popleft()
        return len(q_hour), len(self._sent_daily)

    def _record_sent(self, delivery_id: str, recip_key: str, done_day: str, done_now: float) -> None:
        self._sent_hourly.setdefault(recip_key, deque()).append(done_now)
        self._sent_daily.append((done_day, done_now))
        self._stats["sent"] += 1
        c = self._world_conn()
        if c is not None:
            with contextlib.closing(c):
                with c:
                    c.execute(
                        "INSERT OR IGNORE INTO send_gate_log(delivery_id, recip_key, day_jst, sent_at) VALUES(?, ?, ?, ?)",
                        (str(delivery_id), recip_key, done_day, float(done_now)),
                    )

    def deliver_gated(
        self,
        channel_id: str,
        plugin: Any,
        delivery_id: str,
        payload: Dict[str, Any],
        *,
        is_webchat: bool,
    ) -> Optional[str]:
        d = self.daemon
        text = str(payload.get("text") or "").strip()
        if not text:
            with self._lock:
                self._stats["blocked"] += 1
            raise BlockedDelivery("empty_text")

        with self._lock:
            try:
                eff_scope, eff_target, out_payload = self.resolve_destination(
                    channel_id, plugin, payload, is_webchat=is_webchat
                )
            except BlockedDelivery:
                self._stats["blocked"] += 1
                raise

            actor = str(out_payload.get("actor") or "").strip()
            to_identity = str(out_payload.get("to_identity") or "").strip()
            now = float(d.clock.now())
            if str(out_payload.get("source") or "") == "proactive":
                created_ts = float(out_payload.get("created_ts") or 0.0)
                if created_ts > 0 and (now - created_ts) > 1800.0:
                    self._stats["blocked"] += 1
                    raise BlockedDelivery("expired_ttl")
            check_ch = f"private:{actor}" if eff_scope == "private" else channel_id
            if getattr(d, "guard", None) is not None:
                why = d.guard.check(check_ch, text)
                if why:
                    self._stats["blocked"] += 1
                    raise BlockedDelivery(f"guard:{why}")
            if eff_scope != "private" and actor and actor in d.vocab.ROSTER and getattr(d, "memory", None) is not None:
                if d.memory.check_outbound_leak(actor, "group", text, now=now):
                    self._stats["blocked"] += 1
                    raise BlockedDelivery("memory_leak:private_to_public")

            if not bool(out_payload.get("urgent")):
                quiet_spec = str(d.env.get("AI_HOUSE_QUIET_HOURS", DEFAULT_QUIET_HOURS))
                if in_quiet_hours(now, quiet_spec):
                    self._stats["deferred_quiet_hours"] += 1
                    raise NotSent("quiet_hours")

            hourly_cap = _safe_int(d.env.get("AI_HOUSE_OUTBOX_HOURLY_CAP"), DEFAULT_HOURLY_CAP)
            daily_cap = _safe_int(d.env.get("AI_HOUSE_OUTBOX_DAILY_CAP"), DEFAULT_DAILY_CAP)
            recip_key = to_identity or f"{channel_id}:{eff_target or eff_scope}"
            today_jst = dt.datetime.fromtimestamp(now, JST).date().isoformat()
            h_count, d_count = self._rate_counts(recip_key, now, today_jst)
            if h_count >= hourly_cap:
                self._stats["deferred_rate_limit"] += 1
                raise NotSent("hourly_cap")
            if d_count >= daily_cap:
                self._stats["deferred_rate_limit"] += 1
                raise NotSent("daily_cap")

        try:
            if is_webchat:
                ref = d._deliver_webchat_outbox(delivery_id, out_payload)
                with self._lock:
                    self._webchat_refs[delivery_id] = ref
            else:
                if plugin is None:
                    raise NotSent("channel_unavailable")
                ref = plugin.deliver(delivery_id, out_payload) or f"{channel_id}-{delivery_id}"
        except BlockedDelivery:
            with self._lock:
                self._stats["blocked"] += 1
            raise
        except TimeoutError:
            with self._lock:
                self._stats["unknown"] += 1
            raise
        except NotSent:
            raise

        with self._lock:
            done_now = float(d.clock.now())
            done_day = dt.datetime.fromtimestamp(done_now, JST).date().isoformat()
            self._record_sent(delivery_id, recip_key, done_day, done_now)
        return ref

    def authorize_direct_send(
        self,
        channel_id: str,
        plugin: Any,
        *,
        kind: str,
        target: str,
        actor: str,
        text: str,
        delivery_id: str = "",
        urgent: bool = True,
        enforce_rate_cap: bool = True,
    ) -> str:
        """Gate direct channel replies (e.g. OneBot WS) through destination, guard, leak & cap checks."""
        d = self.daemon
        body = str(text or "").strip()
        if not body:
            with self._lock:
                self._stats["blocked"] += 1
            raise BlockedDelivery("empty_text")
        with self._lock:
            try:
                eff_scope, eff_target, _ = self.resolve_destination(
                    channel_id,
                    plugin,
                    {"kind": kind, "target": target, "actor": actor, "text": body, "urgent": urgent},
                    is_webchat=False,
                )
            except BlockedDelivery:
                self._stats["blocked"] += 1
                raise

            now = float(d.clock.now())
            check_ch = f"private:{actor}" if eff_scope == "private" else f"qq-group:{eff_target}" if channel_id == "onebot_v11" and eff_scope == "group" else channel_id
            if getattr(d, "guard", None) is not None:
                why = d.guard.check(check_ch, body)
                if why:
                    self._stats["blocked"] += 1
                    raise BlockedDelivery(f"guard:{why}")
            if eff_scope != "private" and actor and actor in d.vocab.ROSTER and getattr(d, "memory", None) is not None:
                if d.memory.check_outbound_leak(actor, "group", body, now=now):
                    self._stats["blocked"] += 1
                    raise BlockedDelivery("memory_leak:private_to_public")

            recip_key = f"direct:{channel_id}:{eff_target or eff_scope}"
            today_jst = dt.datetime.fromtimestamp(now, JST).date().isoformat()
            if enforce_rate_cap:
                hourly_cap = _safe_int(d.env.get("AI_HOUSE_DIRECT_HOURLY_CAP") or d.env.get("AI_HOUSE_OUTBOX_HOURLY_CAP"), 60)
                daily_cap = _safe_int(d.env.get("AI_HOUSE_DIRECT_DAILY_CAP") or d.env.get("AI_HOUSE_OUTBOX_DAILY_CAP"), 300)
                h_count, d_count = self._rate_counts(recip_key, now, today_jst)
                if h_count >= hourly_cap:
                    self._stats["deferred_rate_limit"] += 1
                    raise NotSent("hourly_cap")
                if d_count >= daily_cap:
                    self._stats["deferred_rate_limit"] += 1
                    raise NotSent("daily_cap")

            did = delivery_id or f"direct-{channel_id}-{now:.6f}-{len(self._sent_daily)}"
            self._record_sent(did, recip_key, today_jst, now)
            return recip_key
