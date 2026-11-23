"""OneBot v11 channel: lets one roommate talk on QQ through a OneBot implementation you run yourself.

Forward WebSocket only: this process connects *out* to your OneBot server (NapCat, Lagrange, ...).
You need your own QQ account and OneBot implementation; running a bot on QQ may break the platform's
terms and can get an account restricted or banned. See docs/CHAT-AND-QQ.md.

Rules enforced here:
  * one QQ account = one roommate (`ONEBOT_ACTOR`); in groups only that roommate answers;
  * private chats: only the bound owner QQ number(s) (`ONEBOT_OWNER_QQ`); everyone else is ignored;
  * groups: only whitelisted ones (`ONEBOT_GROUPS`, empty = none) and only when the bot is @-mentioned;
  * the owner/member role comes from the QQ number in the event, never from a nickname or message text;
  * per-chat rate limit (minimum gap + hourly cap + one total cap); over-limit messages are dropped before the model
    is called. The owner's private chat is exempt from the minimum gap and the total cap (only its own hourly cap
    applies), and has its own queue that the worker serves first, so group chatter can never crowd it out;
  * a stand-in "demo" reply (no model configured, or the model call failed) is never sent to QQ;
  * messages go out as OneBot segment arrays (no CQ-code parsing); the daemon's reply guard runs first;
  * a send that fails (non-zero retcode, timeout, closed socket) is logged by error code and dropped, not retried;
  * the socket reconnects with exponential backoff; the access token is only sent as a header and never logged.

Threads: one reader (only receives, resolves echo futures, queues events), one worker (talks to the model and
sends). `start()` returns immediately.
"""
from __future__ import annotations

import concurrent.futures
import inspect
import json
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from ..channel_spool import ChannelSpool, split_bubbles
from ..chat_identity import Speaker, member_speaker, owner_speaker
from worldkernel.outbox import BlockedDelivery, NotSent
from ..plugin_api import ChannelPlugin, ConfigField, PluginDefinition, PluginManifest


class ChannelConfigError(ValueError):
    """The OneBot settings are present but unusable; the daemon refuses to start rather than run half-configured."""


def _id(value: Any) -> str:
    s = str(value if value is not None else "").strip()
    return s if s.isdigit() else ""


def _id_set(raw: str, what: str) -> frozenset:
    out = set()
    for part in re.split(r"[,\s]+", str(raw or "").strip()):
        if not part:
            continue
        if not part.isdigit():
            raise ChannelConfigError(f"{what} 只能写数字 QQ/群号，用逗号分隔")
        out.add(part)
    return frozenset(out)


def _num(env: Mapping[str, str], key: str, default: float, lo: float, hi: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        raise ChannelConfigError(f"{key} 必须是数字") from None
    if not lo <= v <= hi:
        raise ChannelConfigError(f"{key} 必须在 {lo:g}~{hi:g} 之间")
    return v


@dataclass
class OneBotConfig:
    url: str
    owners: frozenset
    token: str = ""
    groups: frozenset = frozenset()
    actor: str = ""
    min_interval_s: float = 3.0
    max_per_hour: int = 30
    max_total_per_hour: int = 120
    stale_s: float = 120.0
    send_timeout_s: float = 10.0
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 30.0
    max_part_chars: int = 400
    max_parts: int = 3
    debounce_ms: int = 0
    send_retries: int = 2
    retry_backoff_s: float = 0.15

    @property
    def where(self) -> str:
        """host:port only; the URL itself may carry credentials and is never logged."""
        p = urlparse(self.url)
        return f"{p.hostname}:{p.port}" if p.port else str(p.hostname)


def parse_message(message: Any, self_id: str) -> Tuple[str, bool]:
    """(plain text, whether the bot itself was @-mentioned) from an OneBot `message` (segment array or CQ string)."""
    mentioned = False
    out: List[str] = []
    if isinstance(message, str):
        for m in re.finditer(r"\[CQ:at,qq=(\d+|all)[^\]]*\]", message):
            if m.group(1) == self_id:
                mentioned = True
        return re.sub(r"\[CQ:[^\]]*\]", "", message).strip(), mentioned
    for seg in message if isinstance(message, list) else []:
        if not isinstance(seg, dict):
            continue
        kind = re.sub(r"[^a-z_]", "", str(seg.get("type") or "").lower())[:16]
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if kind == "text":
            out.append(str(data.get("text") or ""))
        elif kind == "at":
            qq = str(data.get("qq") or "")
            if qq == self_id:
                mentioned = True
            elif qq != "all":
                out.append("@某人 ")
        elif kind in ("image", "mface"):
            out.append("[图片]")
        elif kind == "face":
            out.append("[表情]")
        elif kind == "record":
            out.append("[语音]")
        elif kind == "reply":
            continue
        elif kind:
            out.append(f"[{kind}]")
    return "".join(out).strip(), mentioned


def extract_media_segments(message: Any) -> Dict[str, List[str]]:
    """Extract image and audio refs (`url` / `file` / `data:`) from OneBot segment arrays."""
    images: List[str] = []
    records: List[str] = []
    if not isinstance(message, list):
        return {"images": images, "records": records}
    for seg in message:
        if not isinstance(seg, dict):
            continue
        kind = re.sub(r"[^a-z_]", "", str(seg.get("type") or "").lower())[:16]
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        ref = str(data.get("url") or data.get("file") or "").strip()
        if not ref:
            continue
        if kind in ("image", "mface") and len(images) < 4:
            images.append(ref)
        elif kind == "record" and len(records) < 2:
            records.append(ref)
    return {"images": images, "records": records}


class OneBotChannel(ChannelPlugin):
    channel_id = "onebot_v11"

    def __init__(self, cfg: OneBotConfig, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self._clock = clock
        self._host: Any = None
        self.actor = cfg.actor
        self.self_id = ""
        self._stop = threading.Event()
        self._ws: Any = None
        self._ws_lock = threading.Lock()
        self._pending: Dict[str, "concurrent.futures.Future[Dict[str, Any]]"] = {}
        self._pending_lock = threading.Lock()
        self._echo = 0
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=200)  # group @ messages
        self._owner_events: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=50)  # owner private chat, served first
        self._owner_backlog: Deque[Dict[str, Any]] = deque()
        self._group_backlog: Deque[Dict[str, Any]] = deque()
        self._spool: Optional[ChannelSpool] = None
        self._inflight_spool_ids: set[int] = set()
        self._mem_dedup: Dict[str, float] = {}
        self._dedup_lock = threading.Lock()
        self._last_reply: Dict[str, float] = {}
        self._window: Dict[str, List[float]] = {}
        self._rate_lock = threading.Lock()
        self._threads: List[threading.Thread] = []
        self._delivered_refs: Dict[str, str] = {}
        self._ref_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self.stats: Dict[str, int] = {
            "connects": 0, "received": 0, "replied": 0, "ignored": 0, "rate_limited": 0,
            "send_failed": 0, "blocked": 0, "dropped": 0, "demo_skipped": 0, "duplicates": 0,
        }

    # ---- configuration -------------------------------------------------------------------------------------------
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> Optional["OneBotChannel"]:
        import os

        eff = os.environ if env is None else env
        url = (eff.get("ONEBOT_WS_URL") or "").strip()
        if not url:
            if (eff.get("ONEBOT_HTTP_URL") or "").strip():
                raise ChannelConfigError("ONEBOT_HTTP_URL 已不再支持：请改用 ONEBOT_WS_URL（OneBot 的正向 WebSocket 地址）")
            return None
        p = urlparse(url)
        if p.scheme not in ("ws", "wss") or not p.hostname:
            raise ChannelConfigError("ONEBOT_WS_URL 必须是 ws://主机:端口 或 wss://主机:端口")
        if p.query or p.username or p.password:
            raise ChannelConfigError("ONEBOT_WS_URL 里不要带 ?access_token= 或账号密码，令牌请放进 ONEBOT_ACCESS_TOKEN")
        owners = _id_set(eff.get("ONEBOT_OWNER_QQ", ""), "ONEBOT_OWNER_QQ")
        if not owners:
            raise ChannelConfigError("接 QQ 必须设置 ONEBOT_OWNER_QQ（房主的 QQ 号）：只有房主能私聊下指令，没设置就不启用")
        try:
            import websockets.sync.client  # noqa: F401
        except ImportError:
            raise ChannelConfigError('接 QQ 需要 websockets：pip install "ai-house-kit[onebot]"') from None
        cfg = OneBotConfig(
            url=url,
            owners=owners,
            token=(eff.get("ONEBOT_ACCESS_TOKEN") or "").strip(),
            groups=_id_set(eff.get("ONEBOT_GROUPS", ""), "ONEBOT_GROUPS"),
            actor=(eff.get("ONEBOT_ACTOR") or "").strip(),
            min_interval_s=_num(eff, "ONEBOT_MIN_INTERVAL_S", 3.0, 0.0, 3600.0),
            max_per_hour=int(_num(eff, "ONEBOT_MAX_PER_HOUR", 30, 1, 3600)),
            max_total_per_hour=int(_num(eff, "ONEBOT_MAX_TOTAL_PER_HOUR", 120, 1, 36000)),
            stale_s=_num(eff, "ONEBOT_STALE_S", 120.0, 0.0, 86400.0),
            send_timeout_s=_num(eff, "ONEBOT_SEND_TIMEOUT_S", 10.0, 0.05, 120.0),
            reconnect_min_s=_num(eff, "ONEBOT_RECONNECT_MIN_S", 1.0, 0.01, 600.0),
            reconnect_max_s=_num(eff, "ONEBOT_RECONNECT_MAX_S", 30.0, 0.01, 3600.0),
            max_part_chars=int(_num(eff, "ONEBOT_MAX_PART_CHARS", 400, 40, 2000)),
            max_parts=int(_num(eff, "ONEBOT_MAX_PARTS", 3, 1, 8)),
            debounce_ms=int(_num(eff, "ONEBOT_DEBOUNCE_MS", 0, 0, 5000)),
            send_retries=int(_num(eff, "ONEBOT_SEND_RETRIES", 2, 0, 5)),
            retry_backoff_s=_num(eff, "ONEBOT_RETRY_BACKOFF_S", 0.15, 0.01, 10.0),
        )
        return cls(cfg)

    # ---- lifecycle -----------------------------------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        line = f"[onebot_v11] {msg}"
        if self._host is not None and hasattr(self._host, "log"):
            self._host.log(line)
        else:
            print(line, flush=True)

    def start(self, host: Any) -> None:
        self._host = host
        if getattr(host, "identity_store", None) is not None:
            host.identity_store.sync_onebot_owners(self.cfg.owners)
        rt_dir = getattr(host, "runtime_dir", None)
        if rt_dir is not None:
            self._spool = ChannelSpool(rt_dir)
            self._enqueue_recovered(only_ready=False)
        roster = list(host.vocab.ROSTER)
        if not self.actor:
            self.actor = roster[0]
        if self.actor not in roster:
            raise ChannelConfigError(f"ONEBOT_ACTOR={self.actor!r} 不是这个世界里的室友（可选：{', '.join(roster)}）")
        self._log(
            f"enabled: {self.cfg.where}, roommate={self.actor}, owners={len(self.cfg.owners)}, groups={len(self.cfg.groups)}"
        )
        for name, fn in (("reader", self._run), ("worker", self._work)):
            t = threading.Thread(target=fn, name=f"onebot-{name}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        self._fail_pending()
        for t in self._threads:
            t.join(timeout=3.0)
        if self._spool is not None:
            self._spool.close()

    def _bump(self, key: str) -> None:
        with self._stats_lock:
            self.stats[key] += 1

    def _enqueue_recovered(self, *, only_ready: bool) -> None:
        if self._spool is None:
            return
        rows = self._spool.recover_unfinished("onebot_v11", stale_s=self.cfg.stale_s, only_ready=only_ready)
        for row in rows:
            sid = int(row["spool_id"])
            with self._dedup_lock:
                if sid in self._inflight_spool_ids:
                    continue
                self._inflight_spool_ids.add(sid)
            if row["state"] == "reply_ready":
                ev: Dict[str, Any] = {
                    "_resume_plan": row["plan"],
                    "_sent_parts": row["sent_parts"],
                    "_spool_ids": [sid],
                    "message_type": row["kind"],
                    "user_id": row["uid"],
                }
            else:
                ev = dict(row["raw_event"])
                ev["_spool_id"] = sid
            q = self._owner_events if row["kind"] == "private" else self._events
            try:
                q.put_nowait(ev)
            except queue.Full:
                with self._dedup_lock:
                    self._inflight_spool_ids.discard(sid)

    def _check_standalone_dedup(self, mid: str) -> bool:
        if not mid:
            return False
        now = time.monotonic()
        with self._dedup_lock:
            cutoff = now - 3600.0
            for k in [k for k, t in self._mem_dedup.items() if t < cutoff]:
                self._mem_dedup.pop(k, None)
            if mid in self._mem_dedup:
                return True
            if len(self._mem_dedup) > 512:
                self._mem_dedup.clear()
            self._mem_dedup[mid] = now
            return False

    # ---- connection (reader thread) ------------------------------------------------------------------------------
    def _run(self) -> None:
        from websockets.sync.client import connect

        delay = self.cfg.reconnect_min_s
        headers = {"Authorization": f"Bearer {self.cfg.token}"} if self.cfg.token else None
        while not self._stop.is_set():
            lived = 0.0
            try:
                with connect(
                    self.cfg.url, additional_headers=headers, open_timeout=10, max_size=4 * 1024 * 1024
                ) as ws:
                    with self._ws_lock:
                        self._ws = ws
                    started = time.monotonic()
                    self._bump("connects")
                    self._log("connected")
                    self._enqueue_recovered(only_ready=True)
                    try:
                        if not self._stop.is_set():
                            self._read(ws)
                    finally:
                        lived = time.monotonic() - started
            except Exception as exc:
                if not self._stop.is_set():
                    self._log(f"connection problem: {type(exc).__name__}")
            finally:
                with self._ws_lock:
                    self._ws = None
                self._fail_pending()
            if self._stop.wait(delay):
                break
            delay = self._next_delay(delay, lived)

    def _next_delay(self, delay: float, lived_s: float) -> float:
        """Back off exponentially; only a connection that stayed up for a while resets the delay."""
        if lived_s >= 10.0:
            return self.cfg.reconnect_min_s
        return min(delay * 2, self.cfg.reconnect_max_s)

    def _read(self, ws: Any) -> None:
        for raw in ws:
            if self._stop.is_set():
                return
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            echo = data.get("echo")
            if echo is not None:
                with self._pending_lock:
                    fut = self._pending.pop(str(echo), None)
                if fut is not None and not fut.done():
                    fut.set_result(data)
                continue
            sid = _id(data.get("self_id"))
            if sid:
                self.self_id = sid
            if data.get("post_type") == "message":
                # Cheap filters run here so unrelated chatter can never push the owner's message out of the queue.
                why, info = self._classify(data)
                if why:
                    self._bump("ignored")
                    continue
                mid = str(data.get("message_id") or "").strip()
                target_q = self._owner_events if data.get("message_type") == "private" else self._events
                if target_q.full():
                    self._bump("dropped")
                    continue
                ev_item = dict(data)
                if self._spool is not None:
                    kind = info["kind"]
                    tgt = info["gid"] if kind == "group" else info["uid"]
                    spool_id = self._spool.enqueue_event(
                        "onebot_v11",
                        data,
                        message_id=mid,
                        session_key=f"{kind}:{tgt}",
                        kind=kind,
                        target=tgt,
                        uid=info["uid"],
                        nick=info["nick"],
                        actor=self.actor,
                    )
                    if spool_id is None:
                        self._bump("duplicates")
                        continue
                    with self._dedup_lock:
                        self._inflight_spool_ids.add(spool_id)
                    ev_item["_spool_id"] = spool_id
                elif mid and self._check_standalone_dedup(mid):
                    self._bump("duplicates")
                    continue
                try:
                    target_q.put_nowait(ev_item)
                except queue.Full:
                    if self._spool is not None and ev_item.get("_spool_id"):
                        sp_id = int(ev_item["_spool_id"])
                        self._spool.ack([sp_id], state="dropped")
                        with self._dedup_lock:
                            self._inflight_spool_ids.discard(sp_id)
                    self._bump("dropped")

    def _fail_pending(self) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("disconnected"))

    # ---- handling (worker thread) --------------------------------------------------------------------------------
    def _pop_next_event(self) -> Optional[Tuple[Dict[str, Any], bool]]:
        if self._owner_backlog:
            return self._owner_backlog.popleft(), True
        try:
            return self._owner_events.get_nowait(), True
        except queue.Empty:
            pass
        if self._group_backlog:
            return self._group_backlog.popleft(), False
        try:
            return self._events.get(timeout=0.2), False
        except queue.Empty:
            return None

    def _resume_ready_event(self, ev: Mapping[str, Any]) -> None:
        spool_ids = [int(x) for x in (ev.get("_spool_ids") or ()) if x]
        plan = ev.get("_resume_plan") or {}
        bubbles = [str(x) for x in (plan.get("bubbles") or ()) if str(x).strip()]
        start_idx = max(0, int(ev.get("_sent_parts") or 0))
        kind = str(plan.get("kind") or ev.get("message_type") or "private")
        target = str(plan.get("target") or ev.get("user_id") or "")
        at_uid = str(plan.get("at") or "") or None
        try:
            if not bubbles or start_idx >= len(bubbles) or not target:
                if self._spool is not None and spool_ids:
                    self._spool.ack(spool_ids, state="done")
                return
            sent_any = self._send_bubbles(
                kind,
                target,
                bubbles,
                at=at_uid,
                start_idx=start_idx,
                spool_ids=spool_ids,
                save_plan=False,
            )
            if sent_any:
                self._bump("replied")
        finally:
            if spool_ids:
                with self._dedup_lock:
                    for sp_id in spool_ids:
                        self._inflight_spool_ids.discard(sp_id)

    def _collect_debounced_batch(
        self,
        first_ev: Dict[str, Any],
        is_owner_q: bool,
    ) -> List[Dict[str, Any]]:
        batch = [first_ev]
        if self.cfg.debounce_ms <= 0:
            return batch
        src_q = self._owner_events if is_owner_q else self._events
        dst_backlog = self._owner_backlog if is_owner_q else self._group_backlog
        session_sig = (first_ev.get("message_type"), _id(first_ev.get("user_id")), _id(first_ev.get("group_id")))
        deadline = time.monotonic() + (self.cfg.debounce_ms / 1000.0)
        while not self._stop.is_set() and len(batch) < 10:
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            try:
                nxt = src_q.get(timeout=min(0.05, rem))
            except queue.Empty:
                continue
            if "_resume_plan" in nxt:
                dst_backlog.append(nxt)
                continue
            nxt_sig = (nxt.get("message_type"), _id(nxt.get("user_id")), _id(nxt.get("group_id")))
            if nxt_sig == session_sig:
                batch.append(nxt)
            else:
                dst_backlog.append(nxt)
        return batch

    def _handle_batch(self, batch: Sequence[Dict[str, Any]]) -> str:
        if len(batch) == 1:
            return self.handle_event(batch[0])
        texts: List[str] = []
        spool_ids: List[int] = []
        merged_images: List[str] = []
        merged_records: List[str] = []
        last_ev = dict(batch[-1])
        for item in batch:
            if item.get("_spool_id"):
                spool_ids.append(int(item["_spool_id"]))
            why, info = self._classify(item)
            if not why and info.get("text"):
                texts.append(str(info["text"]))
            media = extract_media_segments(item.get("message", item.get("raw_message", "")))
            merged_images.extend(x for x in media["images"] if x not in merged_images)
            merged_records.extend(x for x in media["records"] if x not in merged_records)
        merged = dict(last_ev)
        merged["_spool_ids"] = spool_ids
        if merged_images or merged_records:
            merged["_merged_attachments"] = {"images": merged_images[:4], "records": merged_records[:2]}
        if texts:
            merged["message"] = [{"type": "text", "data": {"text": "\n".join(texts)}}]
            if merged.get("message_type") == "group":
                sid = _id(merged.get("self_id")) or self.self_id
                if sid:
                    merged["message"] = [{"type": "at", "data": {"qq": sid}}, {"type": "text", "data": {"text": " " + "\n".join(texts)}}]
        return self.handle_event(merged)

    def _work(self) -> None:
        while not self._stop.is_set():
            pulled = self._pop_next_event()
            if pulled is None:
                continue
            ev, is_owner_q = pulled
            try:
                if "_resume_plan" in ev:
                    self._resume_ready_event(ev)
                    continue
                batch = self._collect_debounced_batch(ev, is_owner_q)
                self._handle_batch(batch)
            except Exception as exc:  # never log message text
                self._log(f"event failed: {type(exc).__name__}")

    def _ignore(self, why: str) -> str:
        self._bump("ignored")
        return f"ignored:{why}"

    def _rate_ok(self, key: str, owner_dm: bool = False) -> Optional[str]:
        now = self._clock()
        with self._rate_lock:
            last = self._last_reply.get(key)
            if not owner_dm and last is not None and now - last < self.cfg.min_interval_s:
                return "min_interval"
            win = [t for t in self._window.get(key, []) if now - t < 3600.0]
            total = [t for t in self._window.get("*", []) if now - t < 3600.0]
            self._window[key], self._window["*"] = win, total
            if len(win) >= self.cfg.max_per_hour:
                return "hourly_cap"
            if not owner_dm and len(total) >= self.cfg.max_total_per_hour:
                return "total_cap"
            win.append(now)
            if not owner_dm:
                total.append(now)
            self._last_reply[key] = now
        return None

    def _classify(self, ev: Mapping[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
        """(reason to ignore, None-info) or (None, parsed fields). Pure: no side effects, no model, no sending."""
        if ev.get("post_type") != "message":
            return "not_message", {}
        uid = _id(ev.get("user_id"))
        self_id = _id(ev.get("self_id")) or self.self_id
        if not uid or uid == self_id:
            return "self_or_unknown", {}
        kind = ev.get("message_type")
        gid = ""
        id_store = getattr(self._host, "identity_store", None)
        is_mapped_owner = bool(id_store is not None and id_store.is_owner_external("onebot", uid))
        if kind == "private":
            if uid not in self.cfg.owners and not is_mapped_owner:
                return "private_not_owner", {}
        elif kind == "group":
            gid = _id(ev.get("group_id"))
            if not gid or gid not in self.cfg.groups:
                return "group_not_allowed", {}
        else:
            return "unknown_type", {}
        text, mentioned = parse_message(ev.get("message", ev.get("raw_message", "")), self_id)
        if kind == "group" and not mentioned:
            return "group_not_mentioned", {}
        sender = ev.get("sender") if isinstance(ev.get("sender"), dict) else {}
        nick = sender.get("card") or sender.get("nickname") or ""
        return None, {"uid": uid, "kind": kind, "gid": gid, "text": text, "nick": nick}

    def handle_event(self, ev: Mapping[str, Any]) -> str:
        """Decide what to do with one message event; returns a short outcome label (used by tests and stats)."""
        spool_ids: List[int] = [int(x) for x in (ev.get("_spool_ids") or ()) if x]
        if not spool_ids and ev.get("_spool_id"):
            spool_ids = [int(ev["_spool_id"])]
        try:
            why, info = self._classify(ev)
            if why:
                if self._spool is not None and spool_ids:
                    self._spool.ack(spool_ids, state="skipped")
                return self._ignore(why)
            if not spool_ids:
                mid = str(ev.get("message_id") or "").strip()
                if mid:
                    is_dup = (
                        self._spool.check_and_mark_dedup("onebot_v11", mid)
                        if self._spool is not None
                        else self._check_standalone_dedup(mid)
                    )
                    if is_dup:
                        self._bump("duplicates")
                        return self._ignore("duplicate")
            self._bump("received")
            stamp = ev.get("time")
            if self.cfg.stale_s > 0 and isinstance(stamp, (int, float)) and time.time() - stamp > self.cfg.stale_s:
                if self._spool is not None and spool_ids:
                    self._spool.ack(spool_ids, state="expired")
                return self._ignore("stale")  # backlog from before a reconnect or a slow model: do not answer late
            uid, kind, text, nick = info["uid"], info["kind"], info["text"], info["nick"]
            id_store = getattr(self._host, "identity_store", None)
            is_cfg_owner = uid in self.cfg.owners
            if id_store is not None:
                speaker = id_store.resolve_speaker(
                    "onebot",
                    uid,
                    fallback_name=nick or ("房主" if is_cfg_owner else ""),
                    default_owner=is_cfg_owner,
                )
            else:
                speaker = owner_speaker(nick or "房主", uid) if (kind == "private" or is_cfg_owner) else member_speaker(nick, uid)
            if speaker.is_bot:
                if self._spool is not None and spool_ids:
                    self._spool.ack(spool_ids, state="skipped")
                return self._ignore("bot_speaker")
            if kind == "private":
                channel, key, target, actor = f"private:{self.actor}", f"private:{uid}", uid, None
            else:
                gid = info["gid"]
                # the owner has their own budget in a group, so members cannot use it up
                channel, key, target, actor = f"qq-group:{gid}", f"group:{gid}:{'owner' if speaker.is_owner else 'member'}", gid, self.actor
                if not text:
                    text = "（只@了你一下，没说别的）"
            if not text:
                if self._spool is not None and spool_ids:
                    self._spool.ack(spool_ids, state="skipped")
                return self._ignore("empty")
            why = self._rate_ok(key, owner_dm=(kind == "private"))
            if why:
                self._bump("rate_limited")
                if self._spool is not None and spool_ids:
                    self._spool.ack(spool_ids, state="skipped")
                return f"ignored:{why}"
            if self._spool is not None and spool_ids:
                self._spool.mark_processing(spool_ids)
            attachments = ev.get("_merged_attachments")
            if not isinstance(attachments, dict):
                attachments = extract_media_segments(ev.get("message", ev.get("raw_message", "")))
            has_media = bool(attachments.get("images") or attachments.get("records"))
            converse_kw: Dict[str, Any] = {"actor": actor}
            if has_media:
                try:
                    sig = inspect.signature(self._host.converse)
                    if "attachments" in sig.parameters or any(
                        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                    ):
                        converse_kw["attachments"] = attachments
                except (TypeError, ValueError):
                    pass
            res = self._host.converse(channel, text, speaker, **converse_kw)
            sent = 0
            send_groups: List[List[str]] = []
            for rep in res.get("replies") or ():
                if rep.get("mode") == "blocked":
                    self._bump("blocked")
                    continue
                if rep.get("mode") == "demo":
                    self._bump("demo_skipped")  # a canned stand-in line (and maybe an exception name) is not for QQ
                    continue
                if rep.get("mode") == "sleeping" or not str(rep.get("text") or "").strip():
                    continue  # asleep: stay quiet on QQ
                parts = self._split(str(rep["text"]))
                if parts:
                    send_groups.append(parts)
            if not send_groups:
                if self._spool is not None and spool_ids:
                    self._spool.ack(spool_ids, state="skipped")
                return "no_reply"
            at_uid = uid if kind == "group" else None
            for idx, parts in enumerate(send_groups):
                attach_ids = spool_ids if idx == len(send_groups) - 1 else ()
                if self._send_bubbles(kind, target, parts, at=at_uid, spool_ids=attach_ids):
                    sent += 1
            if sent:
                self._bump("replied")
            return "replied" if sent else "no_reply"
        finally:
            if spool_ids:
                with self._dedup_lock:
                    for sp_id in spool_ids:
                        self._inflight_spool_ids.discard(sp_id)

    # ---- sending -------------------------------------------------------------------------------------------------
    def _split(self, text: str) -> List[str]:
        return split_bubbles(text, max_part_chars=self.cfg.max_part_chars, max_parts=self.cfg.max_parts)

    def _send_bubbles(
        self,
        kind: str,
        target: str,
        bubbles: Sequence[str],
        *,
        at: Optional[str] = None,
        start_idx: int = 0,
        spool_ids: Sequence[int] = (),
        save_plan: bool = True,
    ) -> bool:
        """Deliver bubble chunks, retrying only pre-wire `not_connected` failures cleanly."""
        parts = [str(p).strip() for p in bubbles if str(p).strip()]
        if not parts:
            if self._spool is not None and spool_ids:
                self._spool.ack(spool_ids, state="done")
            return False
        if save_plan and self._spool is not None and spool_ids:
            self._spool.save_reply_plan(spool_ids, parts, target=target, kind=kind, at=at or "")

        sent_any = start_idx > 0
        for i in range(start_idx, len(parts)):
            part = parts[i]
            segs: List[Dict[str, Any]] = [{"type": "text", "data": {"text": part}}]
            if at and i == 0:
                segs = [{"type": "at", "data": {"qq": at}}, {"type": "text", "data": {"text": " " + part}}]
            action = "send_group_msg" if kind == "group" else "send_private_msg"
            params = {("group_id" if kind == "group" else "user_id"): int(target), "message": segs}

            ok = False
            code = "not_connected"
            left_wire = False
            max_attempts = 1 + (self.cfg.send_retries if not self._stop.is_set() else 0)
            for attempt in range(max_attempts):
                ok, code, _, left_wire = self._call_detailed(action, params)
                if ok:
                    break
                # Retry ONLY when nothing touched the wire yet (`not_connected`); never retry post-wire/retcode errors.
                if left_wire or code != "not_connected" or attempt + 1 >= max_attempts or self._stop.is_set():
                    break
                self._stop.wait(self.cfg.retry_backoff_s * (attempt + 1))

            if not ok:
                if code == "not_connected" and self._spool is not None and spool_ids:
                    # Leave `reply_ready` pending in spool so `_enqueue_recovered` resumes once WS reconnects.
                    return sent_any
                self._bump("send_failed")
                self._log(f"send failed ({code}); dropped, not retried")
                if self._spool is not None and spool_ids:
                    self._spool.ack(spool_ids, state="failed_wire")
                return sent_any

            sent_any = True
            if self._spool is not None and spool_ids:
                self._spool.record_part_sent(spool_ids, i + 1)

        if self._spool is not None and spool_ids:
            self._spool.ack(spool_ids, state="done")
        return sent_any

    def _send(self, kind: str, target: str, text: str, at: Optional[str] = None) -> bool:
        """True if at least one part went out (a later part failing still leaves the first one delivered)."""
        return self._send_bubbles(kind, target, self._split(text), at=at)

    def _call_detailed(self, action: str, params: Dict[str, Any]) -> Tuple[bool, str, str, bool]:
        with self._ws_lock:
            ws = self._ws
        if ws is None:
            return False, "not_connected", "", False
        with self._pending_lock:
            self._echo += 1
            echo = f"ah{self._echo}"
            fut: "concurrent.futures.Future[Dict[str, Any]]" = concurrent.futures.Future()
            self._pending[echo] = fut
        left_wire = False
        try:
            ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            left_wire = True
            resp = fut.result(timeout=self.cfg.send_timeout_s)
        except concurrent.futures.TimeoutError:
            return False, "timeout", "", True
        except ConnectionError:
            return False, "disconnected", "", left_wire
        except Exception:
            return False, "send_error", "", left_wire
        finally:
            with self._pending_lock:
                self._pending.pop(echo, None)
        if resp.get("status") == "ok" or resp.get("retcode") == 0:
            data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
            msg_id = str(data.get("message_id") or "")
            return True, "ok", (msg_id or f"ob-{echo}"), True
        return False, f"retcode_{resp.get('retcode')}", "", False

    def _call(self, action: str, params: Dict[str, Any]) -> Tuple[bool, str]:
        ok, code, _, _ = self._call_detailed(action, params)
        return ok, code

    def lookup_delivery(self, delivery_id: str) -> Optional[str]:
        with self._ref_lock:
            return self._delivered_refs.get(delivery_id)

    def deliver(self, delivery_id: str, payload: Dict[str, Any]) -> Optional[str]:
        """Deliver an outbox message via OneBot v11 WebSocket with unknown-vs-not-sent semantics."""
        existing = self.lookup_delivery(delivery_id)
        if existing:
            return existing
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("empty outbox text")
        kind = str(payload.get("kind") or ("group" if payload.get("group_id") else "private")).strip().lower()
        target = str(payload.get("target") or payload.get("group_id") or payload.get("user_id") or "").strip()
        if target.startswith("qq-group:"):
            target = target.split(":", 1)[1].strip()
        if not target or not target.isdigit():
            raise NotSent("invalid_target")
        if kind == "group" and target not in self.cfg.groups:
            raise BlockedDelivery("group_not_whitelisted")
        if kind not in ("group", "private"):
            raise BlockedDelivery("unsupported_kind")
        at_qq = str(payload.get("at") or "").strip() or None
        last_ref = ""
        sent_any = False
        for i, part in enumerate(self._split(text)):
            segs: List[Dict[str, Any]] = [{"type": "text", "data": {"text": part}}]
            if at_qq and i == 0 and kind == "group":
                segs = [{"type": "at", "data": {"qq": at_qq}}, {"type": "text", "data": {"text": " " + part}}]
            action = "send_group_msg" if kind == "group" else "send_private_msg"
            param_key = "group_id" if kind == "group" else "user_id"
            ok, code, ext_ref, left_wire = self._call_detailed(action, {param_key: int(target), "message": segs})
            if not ok:
                self._bump("send_failed")
                if not sent_any and not left_wire:
                    raise NotSent(code)
                raise TimeoutError(code)
            sent_any = True
            last_ref = ext_ref
        audio_b64 = str(payload.get("audio_base64") or "").strip()
        if audio_b64:
            action = "send_group_msg" if kind == "group" else "send_private_msg"
            param_key = "group_id" if kind == "group" else "user_id"
            ok_a, code_a, ext_a, lw_a = self._call_detailed(
                action,
                {param_key: int(target), "message": [{"type": "record", "data": {"file": f"base64://{audio_b64}"}}]},
            )
            if ok_a:
                sent_any = True
                last_ref = ext_a
            elif not sent_any and not lw_a:
                raise NotSent(code_a)
            else:
                raise TimeoutError(code_a)
        image_b64 = str(payload.get("image_base64") or "").strip()
        if image_b64:
            action = "send_group_msg" if kind == "group" else "send_private_msg"
            param_key = "group_id" if kind == "group" else "user_id"
            ok_i, code_i, ext_i, lw_i = self._call_detailed(
                action,
                {param_key: int(target), "message": [{"type": "image", "data": {"file": f"base64://{image_b64}"}}]},
            )
            if ok_i:
                sent_any = True
                last_ref = ext_i
            elif not sent_any and not lw_i:
                raise NotSent(code_i)
            else:
                raise TimeoutError(code_i)
        with self._ref_lock:
            self._delivered_refs[delivery_id] = last_ref
        return last_ref

    @classmethod
    def from_config(
        cls,
        cfg_values: Mapping[str, Any],
        env: Optional[Mapping[str, str]] = None,
    ) -> Optional["OneBotChannel"]:
        merged = dict(env or {})
        mapping = {
            "ws_url": "ONEBOT_WS_URL",
            "access_token": "ONEBOT_ACCESS_TOKEN",
            "owner_qq": "ONEBOT_OWNER_QQ",
            "actor": "ONEBOT_ACTOR",
            "groups": "ONEBOT_GROUPS",
            "max_per_hour": "ONEBOT_MAX_PER_HOUR",
            "debounce_ms": "ONEBOT_DEBOUNCE_MS",
            "max_parts": "ONEBOT_MAX_PARTS",
        }
        for key, env_name in mapping.items():
            if key in cfg_values and cfg_values[key] not in (None, ""):
                val = cfg_values[key]
                merged[env_name] = ",".join(str(x) for x in val) if isinstance(val, (list, tuple)) else str(val)
        return cls.from_env(merged)


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id="onebot_v11",
        name="QQ / OneBot v11",
        kind="channel",
        version="0.3.0",
        description="通过外部 OneBot v11 正向 WebSocket 服务连接 QQ 私聊与白名单群聊。",
        default_enabled=False,
        requires_packages=("websockets",),
        config_fields=(
            ConfigField(
                key="ws_url",
                label="WebSocket 地址",
                field_type="string",
                default="",
                required=True,
                env_var="ONEBOT_WS_URL",
                placeholder="ws://127.0.0.1:3001",
                description="OneBot 正向 WebSocket 地址（不要在 URL 中携带 token 查询串）。",
            ),
            ConfigField(
                key="access_token",
                label="访问令牌 (Access Token)",
                field_type="string",
                default="",
                secret=True,
                env_var="ONEBOT_ACCESS_TOKEN",
                description="通过请求头鉴权的 OneBot Access Token，仅加密脱敏展示。",
            ),
            ConfigField(
                key="owner_qq",
                label="房主 QQ 号",
                field_type="string",
                default="",
                required=True,
                env_var="ONEBOT_OWNER_QQ",
                placeholder="多个用逗号分隔",
                description="唯一允许向室友下指令的房主 QQ 号，多个用逗号分隔。",
            ),
            ConfigField(
                key="actor",
                label="绑定室友代号",
                field_type="string",
                default="",
                env_var="ONEBOT_ACTOR",
                placeholder="留空默认首位住客",
                description="此 QQ 账号对应的住客 actor ID（如 nell / sora）。",
            ),
            ConfigField(
                key="groups",
                label="白名单 QQ 群",
                field_type="string",
                default="",
                env_var="ONEBOT_GROUPS",
                placeholder="多个群号用逗号分隔，留空不响应群聊",
                description="仅在这些群里被 @ 时回应。",
            ),
            ConfigField(
                key="max_per_hour",
                label="单会话每小时上限",
                field_type="int",
                default=30,
                min_val=1,
                max_val=3600,
                env_var="ONEBOT_MAX_PER_HOUR",
                description="每个私聊/群会话每小时最多回复条数（1–3600）。",
            ),
            ConfigField(
                key="debounce_ms",
                label="连发防抖窗口（毫秒）",
                field_type="int",
                default=0,
                min_val=0,
                max_val=5000,
                env_var="ONEBOT_DEBOUNCE_MS",
                description="短时间同一会话连续多条输入合并等待窗口（0 表示不合并）。",
            ),
            ConfigField(
                key="max_parts",
                label="单次最大分条气泡数",
                field_type="int",
                default=3,
                min_val=1,
                max_val=8,
                env_var="ONEBOT_MAX_PARTS",
                description="长回复按自然段落/句号拆分时的最大气泡数量（1–8）。",
            ),
        ),
    ),
    channel_factory=OneBotChannel.from_env,
)
