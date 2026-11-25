"""Unified outbound SendGate enforcing leak guards, identity checks, JST quiet hours & rate caps (`0.3.0`)."""
from __future__ import annotations

import contextlib
import datetime as dt
import re
import sqlite3
import threading
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from worldkernel.outbox import BlockedDelivery, NotSent

from .media_provenance import MediaProvenance, media_b64, media_kind, sha256_of_b64

JST = dt.timezone(dt.timedelta(hours=9))
DEFAULT_QUIET_HOURS = "23:30-07:00"
DEFAULT_HOURLY_CAP = 10
DEFAULT_DAILY_CAP = 60
DEFAULT_DIRECT_HOURLY_CAP = 60
DEFAULT_DIRECT_DAILY_CAP = 300
KIND_OUTBOX = "outbox"  # outbox deliveries: proactive talk, reminders, notices
KIND_DIRECT = "direct"  # direct channel replies (OneBot answering a message); counted separately
SHADOW_SUFFIX = "_shadow"  # listen-only mode keeps its own ledger rows: kind and recipient key never mix with live ones
SHADOW_PREFIX = "shadow:"
LOG_RETENTION_S = 3 * 86400.0  # send_gate_log rows older than this are deleted (only today's rows are ever counted)
_LOG_PRUNE_EVERY_S = 600.0
DAILY_COUNT_SQL = "SELECT COUNT(*) FROM send_gate_log WHERE kind=? AND day_jst=?"
HOURLY_COUNT_SQL = "SELECT COUNT(*) FROM send_gate_log WHERE recip_key=? AND sent_at>?"

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


def group_channel_of(channel_id: str, eff_target: str, *, is_webchat: bool) -> str:
    """The group channel id a gated group message is going to, spelled the way chat and memory spell it
    (`group`, `qq-group:<id>`), so the outbound checks ask the same question recall answered.

    Another channel plugin has no such id: it keeps its plugin id, which no `AI_HOUSE_MEMORY_LEAK_FREE_GROUPS` entry matches."""
    if is_webchat:
        return eff_target or "group"
    if channel_id in ("onebot_v11", "onebot"):
        return f"qq-group:{eff_target}"
    return channel_id


def _safe_int(raw: Any, default: int) -> int:
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default


def is_listen_only(plugin: Any) -> bool:
    """True only for a channel that says, with a real boolean, that it is in listen-only mode."""
    return getattr(plugin, "listen_only", False) is True


def _mode_kind(kind: str, shadow: bool) -> str:
    return f"{kind}{SHADOW_SUFFIX}" if shadow else kind


def _mode_key(key: str, shadow: bool) -> str:
    return f"{SHADOW_PREFIX}{key}" if shadow else key


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
        self._sent_daily: Dict[str, Deque[Tuple[str, float]]] = {KIND_OUTBOX: deque(), KIND_DIRECT: deque()}
        self._webchat_refs: Dict[str, str] = {}
        self._stats: Dict[str, int] = {
            "sent": 0,
            "blocked": 0,
            "deferred_quiet_hours": 0,
            "deferred_rate_limit": 0,
            "unknown": 0,
            "direct_limited": 0,
            "deferred_muted": 0,
            "leak_check_skipped": 0,
            "listen_only": 0,
        }
        self._mem_ids: Dict[str, float] = {}
        self._last_log_prune = 0.0

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
            "recip_key TEXT NOT NULL, day_jst TEXT NOT NULL, sent_at REAL NOT NULL, "
            f"kind TEXT NOT NULL DEFAULT '{KIND_OUTBOX}')"
        )
        cols = {r[1] for r in c.execute("PRAGMA table_info(send_gate_log)").fetchall()}
        if "kind" not in cols:  # table created before direct replies were counted separately
            try:
                with c:
                    c.execute(f"ALTER TABLE send_gate_log ADD COLUMN kind TEXT NOT NULL DEFAULT '{KIND_OUTBOX}'")
                    c.execute("UPDATE send_gate_log SET kind=? WHERE recip_key LIKE 'direct:%'", (KIND_DIRECT,))
            except sqlite3.OperationalError:
                pass  # another connection migrated it first
        if "shadow" not in cols:  # rows written in listen-only mode carry 1 (they are also kept apart by kind and key)
            try:
                with c:
                    c.execute("ALTER TABLE send_gate_log ADD COLUMN shadow INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_send_gate_log_ts ON send_gate_log(sent_at, day_jst)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_send_gate_log_kind_day ON send_gate_log(kind, day_jst)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_send_gate_log_recip ON send_gate_log(recip_key, sent_at)")
        return c

    def reset(self) -> None:
        with self._lock:
            self._sent_hourly.clear()
            for q in self._sent_daily.values():
                q.clear()
            self._mem_ids.clear()
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

    def _rate_counts(self, recip_key: str, now: float, today_jst: str, kind: str = KIND_OUTBOX) -> Tuple[int, int]:
        """(sends to `recip_key` in the last hour, sends of this `kind` today). Outbox and direct never mix."""
        c = self._world_conn()
        if c is not None:
            with contextlib.closing(c):
                h_row = c.execute(HOURLY_COUNT_SQL, (recip_key, float(now) - 3600.0)).fetchone()
                d_row = c.execute(DAILY_COUNT_SQL, (kind, today_jst)).fetchone()
                return int(h_row[0] or 0), int(d_row[0] or 0)

        q_hour = self._sent_hourly.setdefault(recip_key, deque())
        while q_hour and (now - q_hour[0]) >= 3600.0:
            q_hour.popleft()
        q_day = self._sent_daily.setdefault(kind, deque())
        while q_day and q_day[0][0] != today_jst:
            q_day.popleft()
        return len(q_hour), len(q_day)

    def _already_logged(self, delivery_id: str) -> bool:
        if not delivery_id:
            return False
        if str(delivery_id) in self._mem_ids:
            return True
        c = self._world_conn()
        if c is not None:
            with contextlib.closing(c):
                return c.execute("SELECT 1 FROM send_gate_log WHERE delivery_id=?", (str(delivery_id),)).fetchone() is not None
        return False

    def _record_sent(
        self,
        delivery_id: str,
        recip_key: str,
        done_day: str,
        done_now: float,
        kind: str = KIND_OUTBOX,
        shadow: bool = False,
    ) -> bool:
        """Charge one send. A `delivery_id` that is already in the log is not charged again (returns False)."""
        if self._already_logged(delivery_id):
            return False
        self._sent_hourly.setdefault(recip_key, deque()).append(done_now)
        self._sent_daily.setdefault(kind, deque()).append((done_day, done_now))
        self._mem_ids[str(delivery_id)] = done_now
        if len(self._mem_ids) > 4096:
            for k in sorted(self._mem_ids, key=self._mem_ids.get)[:1024]:  # type: ignore[arg-type]
                self._mem_ids.pop(k, None)
        self._stats["sent"] += 1
        c = self._world_conn()
        if c is not None:
            with contextlib.closing(c):
                with c:
                    c.execute(
                        "INSERT OR IGNORE INTO send_gate_log(delivery_id, recip_key, day_jst, sent_at, kind, shadow) "
                        "VALUES(?, ?, ?, ?, ?, ?)",
                        (str(delivery_id), recip_key, done_day, float(done_now), kind, 1 if shadow else 0),
                    )
                    if abs(done_now - self._last_log_prune) >= _LOG_PRUNE_EVERY_S:
                        self._last_log_prune = done_now
                        c.execute("DELETE FROM send_gate_log WHERE sent_at < ?", (float(done_now) - LOG_RETENTION_S,))
        return True

    def prune_log(self, now: Optional[float] = None) -> int:
        """Delete send-log rows older than the retention period; returns how many went."""
        ts = float(now) if now is not None else float(self.daemon.clock.now())
        with self._lock:
            c = self._world_conn()
            if c is None:
                return 0
            with contextlib.closing(c):
                with c:
                    cur = c.execute("DELETE FROM send_gate_log WHERE sent_at < ?", (ts - LOG_RETENTION_S,))
                    return int(cur.rowcount or 0)

    # ---- shared checks -------------------------------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        fn = getattr(self.daemon, "log", None)
        if callable(fn):
            try:
                fn(f"[send_gate] {msg}")
            except Exception:  # noqa: BLE001 - a log line never stops a send decision
                pass

    def _count(self, key: str) -> None:
        with self._lock:
            self._stats[key] = self._stats.get(key, 0) + 1

    def _is_muted(self, eff_scope: str, out_ch: str, now: float) -> bool:
        """A group the owner (or she herself) was told to be quiet in. Nothing goes there, whoever asks.
        A mute lookup that fails counts as muted: the safe direction."""
        if eff_scope == "private":
            return False
        fn = getattr(getattr(self.daemon, "group_behavior", None), "is_muted", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(out_ch, now=now))
        except Exception as exc:  # noqa: BLE001
            self._log(f"mute lookup failed ({type(exc).__name__}); treated as muted")
            return True

    def _media_texts(self, eff_scope: str, out_ch: str, items: Sequence[Mapping[str, Any]], declared: int) -> List[str]:
        """Group only: the text every media item was made from, or `BlockedDelivery("media_provenance:<why>")`.

        An image, a voice clip or a sticker cannot be read, so it goes into a group only when a built-in tool wrote a
        record for exactly these bytes and this chat (see `media_provenance`). The owner's private chat is exempt."""
        if eff_scope == "private" or declared <= 0:
            return []
        reg = getattr(self.daemon, "media_provenance", None)
        if not isinstance(reg, MediaProvenance):
            raise BlockedDelivery("media_provenance:unavailable")
        if declared > len(items):
            raise BlockedDelivery("media_provenance:unverifiable")  # media that is counted but not shown to the gate
        texts: List[str] = []
        for it in items:
            digest = sha256_of_b64(media_b64(it))
            if not digest or media_kind(it) not in ("image", "audio"):
                raise BlockedDelivery("media_provenance:unverifiable")
            rec, why = reg.lookup(digest, out_ch)
            if rec is None:
                raise BlockedDelivery(f"media_provenance:{why}")
            for t in rec["texts"]:
                if t not in texts:
                    texts.append(t)
        return texts

    def _leak_checks(self, actor: str, texts: Sequence[str], out_ch: str, eff_scope: str, now: float) -> None:
        """The privacy check for a group send. Raises `BlockedDelivery`; never holds the gate's lock.

        It cannot run when there is no memory store or the actor is unknown, and it can raise. In a group that is not
        leak-free that closes the send (`privacy_check_unavailable`, nothing charged); in a group listed as leak-free
        it does not block and a log line says the check was skipped. A fallback inside the shared `leak()` (no
        embedding service) is not a failure here: that code decides for itself."""
        if eff_scope == "private":
            return
        wanted = [t for t in texts if str(t or "").strip()]
        if not wanted:
            return
        d = self.daemon
        mem = getattr(d, "memory", None)
        free = False
        if mem is not None:
            try:
                free = bool(mem.leak_free_in(out_ch))
            except Exception:  # noqa: BLE001
                free = False
        roster = getattr(getattr(d, "vocab", None), "ROSTER", ())
        runnable = mem is not None and bool(actor) and actor in roster
        for t in wanted:
            failed = ""
            leaked = False
            if not runnable:
                failed = "no_memory_store" if mem is None else "no_known_actor"
            else:
                try:
                    leaked = bool(mem.check_outbound_leak(actor, "group", t, channel=out_ch, now=now))
                except Exception as exc:  # noqa: BLE001
                    failed = type(exc).__name__
            if failed:
                if free:
                    self._count("leak_check_skipped")
                    self._log(f"privacy check skipped in a leak-free chat ({failed}); chat={out_ch}")
                    return
                self._count("blocked")
                raise BlockedDelivery("privacy_check_unavailable")
            if leaked:
                self._count("blocked")
                raise BlockedDelivery("memory_leak:private_to_public")

    def _note_shadow(self, plugin: Any, **kw: Any) -> None:
        fn = getattr(plugin, "record_listen_only", None)
        if callable(fn):
            try:
                fn(**kw)
            except Exception as exc:  # noqa: BLE001 - the record never decides the send
                self._log(f"listen-only record failed ({type(exc).__name__})")

    @staticmethod
    def _shadow_seen(plugin: Any, delivery_id: str, verdict: str) -> bool:
        """True if the listen-only record (the channel's table, so it survives a restart) already holds a row for this
        delivery id and verdict. Any doubt counts as "not seen": a row too many is better than a missing one."""
        try:
            spool = getattr(plugin, "_spool", None)
            if spool is not None:
                return bool(spool.has_listen_only(delivery_id=delivery_id, verdict=verdict, path="outbox"))
            rows = plugin.listen_only_rows(limit=500)
            return any(r.get("delivery_id") == delivery_id[:80] and r.get("verdict") == verdict[:120] for r in rows)
        except Exception:  # noqa: BLE001
            return False

    def _observe_own_speech(self, actor: str, eff_scope: str, out_ch: str, text: str, now: float) -> None:
        """Her own proactive words that really went out are remembered as hers (a reminder or notice is not)."""
        mem = getattr(self.daemon, "memory", None)
        if mem is None or actor not in getattr(getattr(self.daemon, "vocab", None), "ROSTER", ()):
            return
        try:
            private = eff_scope == "private"
            mem.observe(actor, "private" if private else "group", "", channel="" if private else out_ch, reply=text, now=now)
        except Exception as exc:  # noqa: BLE001
            self._log(f"memory observe after send failed ({type(exc).__name__})")

    @staticmethod
    def _payload_media(payload: Mapping[str, Any]) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        if str(payload.get("audio_base64") or "").strip():
            items.append({"kind": "audio", "audio_base64": str(payload["audio_base64"]).strip()})
        if str(payload.get("image_base64") or "").strip():
            items.append({"kind": "image", "image_base64": str(payload["image_base64"]).strip()})
        return items

    # ---- outbox path ---------------------------------------------------------------------------------------------
    def _outbox_checks(
        self,
        channel_id: str,
        plugin: Any,
        payload: Dict[str, Any],
        text: str,
        items: Sequence[Mapping[str, Any]],
        *,
        is_webchat: bool,
        shadow: bool,
        note: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], str, str, str, str]:
        """Every check an outbox delivery passes before it may leave. Returns
        `(payload, recip_key, out_ch, eff_scope, actor)`; raises `BlockedDelivery` / `NotSent`."""
        d = self.daemon
        with self._lock:
            try:
                eff_scope, eff_target, out_payload = self.resolve_destination(
                    channel_id, plugin, payload, is_webchat=is_webchat
                )
            except BlockedDelivery:
                self._stats["blocked"] += 1
                raise
            note.update(kind=eff_scope, target=eff_target, actor=str(out_payload.get("actor") or "").strip())
            actor = str(out_payload.get("actor") or "").strip()
            to_identity = str(out_payload.get("to_identity") or "").strip()
            now = float(d.clock.now())
            out_ch = group_channel_of(channel_id, eff_target, is_webchat=is_webchat)
            if self._is_muted(eff_scope, out_ch, now):
                # nothing passes a hold, reminders included; the delivery waits and goes out once the mute is over
                self._stats["deferred_muted"] += 1
                raise NotSent("group_muted")
            if str(out_payload.get("source") or "") == "proactive":
                created_ts = float(out_payload.get("created_ts") or 0.0)
                if created_ts > 0 and (now - created_ts) > 1800.0:
                    self._stats["blocked"] += 1
                    raise BlockedDelivery("expired_ttl")
            check_ch = f"private:{actor}" if eff_scope == "private" else out_ch
            guard = getattr(d, "guard", None)
            if guard is not None:
                why = guard.check(check_ch, text, actor=actor)
                if why:
                    self._stats["blocked"] += 1
                    raise BlockedDelivery(f"guard:{why}")
            try:
                media_texts = self._media_texts(eff_scope, out_ch, items, len(items)) if not is_webchat else []
            except BlockedDelivery:
                self._stats["blocked"] += 1
                raise
            if guard is not None:
                for t in media_texts:
                    why = guard.check(check_ch, t, actor=actor)
                    if why:
                        self._stats["blocked"] += 1
                        raise BlockedDelivery(f"guard:{why}")

        # the privacy check can be slow (a vector service): it runs without the gate's lock. It only reads the
        # memory store and the caller's own text, and the quota below is decided after it, so nothing is lost by that.
        self._leak_checks(actor, [text, *media_texts], out_ch, eff_scope, now)

        with self._lock:
            if not bool(out_payload.get("urgent")):
                quiet_spec = str(d.env.get("AI_HOUSE_QUIET_HOURS", DEFAULT_QUIET_HOURS))
                if in_quiet_hours(now, quiet_spec):
                    self._stats["deferred_quiet_hours"] += 1
                    raise NotSent("quiet_hours")

            hourly_cap = _safe_int(d.env.get("AI_HOUSE_OUTBOX_HOURLY_CAP"), DEFAULT_HOURLY_CAP)
            daily_cap = _safe_int(d.env.get("AI_HOUSE_OUTBOX_DAILY_CAP"), DEFAULT_DAILY_CAP)
            recip_key = _mode_key(to_identity or f"{channel_id}:{eff_target or eff_scope}", shadow)
            today_jst = dt.datetime.fromtimestamp(now, JST).date().isoformat()
            h_count, d_count = self._rate_counts(recip_key, now, today_jst, _mode_kind(KIND_OUTBOX, shadow))
            if h_count >= hourly_cap:
                self._stats["deferred_rate_limit"] += 1
                raise NotSent("hourly_cap")
            if d_count >= daily_cap:
                self._stats["deferred_rate_limit"] += 1
                raise NotSent("daily_cap")
        return out_payload, recip_key, out_ch, eff_scope, actor

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
        shadow = (not is_webchat) and is_listen_only(plugin)
        items = [] if is_webchat else self._payload_media(payload)
        note: Dict[str, Any] = {"kind": "", "target": "", "actor": str(payload.get("actor") or "")}
        source = str(payload.get("source") or "")
        try:
            out_payload, recip_key, out_ch, eff_scope, actor = self._outbox_checks(
                channel_id, plugin, payload, text, items, is_webchat=is_webchat, shadow=shadow, note=note
            )
        except (BlockedDelivery, NotSent) as exc:
            if shadow:
                verdict = ("blocked:" if isinstance(exc, BlockedDelivery) else "limited:") + str(exc.reason)
                # a held-back delivery stays pending and is tried again on every flush: one row per (id, reason)
                if not (isinstance(exc, NotSent) and self._shadow_seen(plugin, str(delivery_id), verdict)):
                    self._note_shadow(
                        plugin, path="outbox", source=source, delivery_id=str(delivery_id), text_len=len(text),
                        media=items, verdict=verdict, **note,
                    )
            raise

        if shadow:
            # listen-only: the whole verdict was reached, nothing is written to the channel; the draft is kept in the
            # channel's own record and the quota is charged in the shadow ledger, so the limits are simulated
            ref = f"shadow:{delivery_id}"
            self._note_shadow(
                plugin, path="outbox", source=source, delivery_id=str(delivery_id), text=text, media=items,
                verdict="allowed", **note,
            )
            with self._lock:
                done_now = float(d.clock.now())
                done_day = dt.datetime.fromtimestamp(done_now, JST).date().isoformat()
                self._record_sent(SHADOW_PREFIX + str(delivery_id), recip_key, done_day, done_now,
                                  _mode_kind(KIND_OUTBOX, True), True)
                self._stats["listen_only"] += 1
            return ref

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
            self._record_sent(delivery_id, recip_key, done_day, done_now, KIND_OUTBOX)
        if not is_webchat and source == "proactive":
            # her own words that were really sent are remembered as hers; a reminder or other notice is not
            self._observe_own_speech(actor, eff_scope, out_ch, text, done_now)
        return ref

    def _direct_hourly_cap(self, plugin: Any) -> int:
        env = self.daemon.env
        raw = env.get("AI_HOUSE_DIRECT_HOURLY_CAP")
        if raw:
            return _safe_int(raw, DEFAULT_DIRECT_HOURLY_CAP)
        cfg = getattr(plugin, "cfg", None)
        per_hour = getattr(cfg, "max_per_hour", None) if cfg is not None else None
        if not per_hour:
            per_hour = env.get("ONEBOT_MAX_PER_HOUR")
        # The channel enforces ONEBOT_MAX_PER_HOUR per chat itself; the gate is the safety net above it, so a larger
        # configured value raises this cap instead of being clipped to the default.
        return max(DEFAULT_DIRECT_HOURLY_CAP, _safe_int(per_hour, DEFAULT_DIRECT_HOURLY_CAP))

    # ---- direct channel replies ------------------------------------------------------------------------------------
    def _direct_checks(
        self,
        channel_id: str,
        plugin: Any,
        *,
        kind: str,
        target: str,
        actor: str,
        body: str,
        items: Sequence[Mapping[str, Any]],
        declared_media: int,
        urgent: bool = True,
    ) -> Tuple[str, str, str, float]:
        """Destination, mute, guard, media provenance and privacy check for one direct reply; charges nothing.
        Returns `(eff_scope, eff_target, out_ch, now)`."""
        d = self.daemon
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
            out_ch = group_channel_of(channel_id, eff_target, is_webchat=False)
            if self._is_muted(eff_scope, out_ch, now):
                self._stats["blocked"] += 1
                raise BlockedDelivery("group_muted")
            check_ch = f"private:{actor}" if eff_scope == "private" else out_ch
            guard = getattr(d, "guard", None)
            if body and guard is not None:
                why = guard.check(check_ch, body, actor=actor)
                if why:
                    self._stats["blocked"] += 1
                    raise BlockedDelivery(f"guard:{why}")
            try:
                media_texts = self._media_texts(eff_scope, out_ch, items, declared_media)
            except BlockedDelivery:
                self._stats["blocked"] += 1
                raise
            if guard is not None:
                for t in media_texts:
                    why = guard.check(check_ch, t, actor=actor)
                    if why:
                        self._stats["blocked"] += 1
                        raise BlockedDelivery(f"guard:{why}")
        self._leak_checks(actor, [body, *media_texts], out_ch, eff_scope, now)  # no lock held, see `_outbox_checks`
        return eff_scope, eff_target, out_ch, now

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
        media_count: int = 0,
        media: Sequence[Mapping[str, Any]] = (),
        dry_run: bool = False,
    ) -> str:
        """Gate a direct channel reply (e.g. OneBot WS) through destination, mute, guard, media provenance, leak & cap checks.

        Nothing is charged here: the caller must call `record_direct_sent(delivery_id, <returned key>)` once the
        reply really went out, so a failed send costs no quota. A reply whose `delivery_id` is already in the log
        (resumed or authorised again) is not stopped by the caps and is not charged twice. Direct replies have their
        own hourly (per target) and daily counters, independent of outbox traffic.

        `media` are the reply's pictures / voice clips (`{"kind": "image", "image_base64": ...}`): in a group each
        one needs a provenance record (see `media_provenance`) and the text it was made from is checked again.
        `media_count` alone (no items) only lets a private reply of pure media through: in a group it is refused.

        A channel in listen-only mode (`listen_only is True`) is refused here with `listen_only` after every other
        check passed, unless the caller passes `dry_run=True` (the channel's own bookkeeping of what would have
        been sent); then the returned key belongs to the shadow ledger."""
        body = str(text or "").strip()
        items = [dict(m) for m in (media or ()) if isinstance(m, Mapping)]
        declared = max(int(media_count or 0), len(items))
        if not body and declared <= 0:
            with self._lock:
                self._stats["blocked"] += 1
            raise BlockedDelivery("empty_text")
        shadow = is_listen_only(plugin)
        eff_scope, eff_target, _out_ch, now = self._direct_checks(
            channel_id, plugin, kind=kind, target=target, actor=actor, body=body, items=items,
            declared_media=declared, urgent=urgent,
        )
        with self._lock:
            recip_key = _mode_key(f"direct:{channel_id}:{eff_target or eff_scope}", shadow)
            today_jst = dt.datetime.fromtimestamp(now, JST).date().isoformat()
            did = (SHADOW_PREFIX if shadow else "") + str(delivery_id)
            if enforce_rate_cap and not self._already_logged(did):
                hourly_cap = self._direct_hourly_cap(plugin)
                daily_cap = _safe_int(self.daemon.env.get("AI_HOUSE_DIRECT_DAILY_CAP"), DEFAULT_DIRECT_DAILY_CAP)
                h_count, d_count = self._rate_counts(recip_key, now, today_jst, _mode_kind(KIND_DIRECT, shadow))
                if h_count >= hourly_cap:
                    self._stats["deferred_rate_limit"] += 1
                    self._stats["direct_limited"] += 1
                    raise NotSent("hourly_cap")
                if d_count >= daily_cap:
                    self._stats["deferred_rate_limit"] += 1
                    self._stats["direct_limited"] += 1
                    raise NotSent("daily_cap")
            if shadow and not dry_run:
                self._stats["blocked"] += 1
                raise BlockedDelivery("listen_only")
            return recip_key

    def recheck_direct_send(
        self,
        channel_id: str,
        plugin: Any,
        *,
        kind: str,
        target: str,
        actor: str,
        media: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """The checks that cost nothing, asked again before the rest of a resumed reply goes out: the destination is
        still allowed, the group has not been muted since, and the pictures / voice clips still have their record.
        Raises `BlockedDelivery` / `NotSent`; charges and records nothing."""
        items = [dict(m) for m in (media or ()) if isinstance(m, Mapping)]
        self._direct_checks(
            channel_id, plugin, kind=kind, target=target, actor=actor, body="", items=items, declared_media=len(items)
        )

    def record_direct_sent(self, delivery_id: str, recip_key: str) -> bool:
        """Charge one direct reply after it has really been sent. Recording the same `delivery_id` twice is a no-op.
        A key from listen-only mode charges the shadow ledger (what would have been sent), never the live one."""
        shadow = str(recip_key).startswith(SHADOW_PREFIX)
        with self._lock:
            now = float(self.daemon.clock.now())
            day = dt.datetime.fromtimestamp(now, JST).date().isoformat()
            did = (SHADOW_PREFIX if shadow else "") + str(delivery_id)
            return self._record_sent(did, str(recip_key), day, now, _mode_kind(KIND_DIRECT, shadow), shadow)
