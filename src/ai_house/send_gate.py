"""Unified outbound SendGate enforcing leak guards, identity checks, JST quiet hours & rate caps (`0.3.0`)."""
from __future__ import annotations

import datetime as dt
import re
import threading
from collections import deque
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

    def reset(self) -> None:
        with self._lock:
            self._sent_hourly.clear()
            self._sent_daily.clear()
            self._webchat_refs.clear()
            for k in self._stats:
                self._stats[k] = 0

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
                return self._webchat_refs.get(delivery_id)
        if plugin is not None and hasattr(plugin, "lookup_delivery"):
            return plugin.lookup_delivery(delivery_id)
        return None

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

        actor = str(payload.get("actor") or "").strip()
        kind = str(payload.get("kind") or ("group" if channel_id == "webchat" and not actor else "private")).strip().lower()
        scope = str(payload.get("scope") or ("private" if kind == "private" else "group")).strip().lower()
        to_identity = str(payload.get("to_identity") or "").strip()
        out_payload = dict(payload)

        with self._lock:
            now = float(d.clock.now())
            check_ch = f"private:{actor}" if scope == "private" else channel_id
            if getattr(d, "guard", None) is not None:
                why = d.guard.check(check_ch, text)
                if why:
                    self._stats["blocked"] += 1
                    raise BlockedDelivery(f"guard:{why}")
            if scope != "private" and actor and actor in d.vocab.ROSTER and getattr(d, "memory", None) is not None:
                if d.memory.check_outbound_leak(actor, "group", text, now=now):
                    self._stats["blocked"] += 1
                    raise BlockedDelivery("memory_leak:private_to_public")

            id_store = getattr(d, "identity_store", None)
            if to_identity and id_store is not None:
                ident = id_store.get_identity(to_identity)
                if ident is None:
                    raise NotSent("unknown_identity")
                if ident["kind"] == "bot":
                    self._stats["blocked"] += 1
                    raise BlockedDelivery("bot_recipient")
                if not is_webchat and kind != "group" and not str(out_payload.get("target") or "").strip():
                    plat = "onebot" if channel_id in ("onebot_v11", "onebot") else channel_id
                    targets = id_store.external_targets_for(to_identity, platform=plat)
                    if not targets:
                        raise NotSent("unmapped_identity")
                    out_payload["target"] = targets[0]["external_id"]

            if not bool(payload.get("urgent")):
                quiet_spec = str(d.env.get("AI_HOUSE_QUIET_HOURS", DEFAULT_QUIET_HOURS))
                if in_quiet_hours(now, quiet_spec):
                    self._stats["deferred_quiet_hours"] += 1
                    raise NotSent("quiet_hours")

            hourly_cap = _safe_int(d.env.get("AI_HOUSE_OUTBOX_HOURLY_CAP"), DEFAULT_HOURLY_CAP)
            daily_cap = _safe_int(d.env.get("AI_HOUSE_OUTBOX_DAILY_CAP"), DEFAULT_DAILY_CAP)
            recip_key = to_identity or f"{channel_id}:{out_payload.get('target') or scope}"

            q_hour = self._sent_hourly.setdefault(recip_key, deque())
            while q_hour and (now - q_hour[0]) >= 3600.0:
                q_hour.popleft()
            if len(q_hour) >= hourly_cap:
                self._stats["deferred_rate_limit"] += 1
                raise NotSent("hourly_cap")

            today_jst = dt.datetime.fromtimestamp(now, JST).date().isoformat()
            while self._sent_daily and self._sent_daily[0][0] != today_jst:
                self._sent_daily.popleft()
            if len(self._sent_daily) >= daily_cap:
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
            self._sent_hourly.setdefault(recip_key, deque()).append(done_now)
            self._sent_daily.append((done_day, done_now))
            self._stats["sent"] += 1
        return ref
