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
import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

from ..chat_identity import Speaker, member_speaker, owner_speaker
from ..plugin_api import ChannelPlugin


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
        self._last_reply: Dict[str, float] = {}
        self._window: Dict[str, List[float]] = {}
        self._rate_lock = threading.Lock()
        self._threads: List[threading.Thread] = []
        self._stats_lock = threading.Lock()
        self.stats: Dict[str, int] = {
            "connects": 0, "received": 0, "replied": 0, "ignored": 0, "rate_limited": 0,
            "send_failed": 0, "blocked": 0, "dropped": 0, "demo_skipped": 0,
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

    def _bump(self, key: str) -> None:
        with self._stats_lock:
            self.stats[key] += 1

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
                why, _ = self._classify(data)
                if why:
                    self._bump("ignored")
                    continue
                try:
                    (self._owner_events if data.get("message_type") == "private" else self._events).put_nowait(data)
                except queue.Full:
                    self._bump("dropped")

    def _fail_pending(self) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("disconnected"))

    # ---- handling (worker thread) --------------------------------------------------------------------------------
    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                ev = self._owner_events.get_nowait()
            except queue.Empty:
                try:
                    ev = self._events.get(timeout=0.2)
                except queue.Empty:
                    continue
            try:
                self.handle_event(ev)
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
        if kind == "private":
            if uid not in self.cfg.owners:
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
        why, info = self._classify(ev)
        if why:
            return self._ignore(why)
        self._bump("received")
        stamp = ev.get("time")
        if self.cfg.stale_s > 0 and isinstance(stamp, (int, float)) and time.time() - stamp > self.cfg.stale_s:
            return self._ignore("stale")  # backlog from before a reconnect or a slow model: do not answer late
        uid, kind, text, nick = info["uid"], info["kind"], info["text"], info["nick"]
        speaker: Speaker
        if kind == "private":
            speaker = owner_speaker(nick or "房主", uid)
            channel, key, target, actor = f"private:{self.actor}", f"private:{uid}", uid, None
        else:
            gid = info["gid"]
            is_owner = uid in self.cfg.owners
            speaker = owner_speaker(nick or "房主", uid) if is_owner else member_speaker(nick, uid)
            # the owner has their own budget in a group, so members cannot use it up
            channel, key, target, actor = f"qq-group:{gid}", f"group:{gid}:{'owner' if is_owner else 'member'}", gid, self.actor
            if not text:
                text = "（只@了你一下，没说别的）"
        if not text:
            return self._ignore("empty")
        why = self._rate_ok(key, owner_dm=(kind == "private"))
        if why:
            self._bump("rate_limited")
            return f"ignored:{why}"
        res = self._host.converse(channel, text, speaker, actor=actor)
        sent = 0
        for rep in res.get("replies") or ():
            if rep.get("mode") == "blocked":
                self._bump("blocked")
                continue
            if rep.get("mode") == "demo":
                self._bump("demo_skipped")  # a canned stand-in line (and maybe an exception name) is not for QQ
                continue
            if rep.get("mode") == "sleeping" or not str(rep.get("text") or "").strip():
                continue  # asleep: stay quiet on QQ
            if self._send(kind, target, str(rep["text"]), at=uid if kind == "group" else None):
                sent += 1
        if sent:
            self._bump("replied")
        return "replied" if sent else "no_reply"

    # ---- sending -------------------------------------------------------------------------------------------------
    def _split(self, text: str) -> List[str]:
        n = self.cfg.max_part_chars
        t = text.strip()
        return [t[i : i + n] for i in range(0, len(t), n)][: self.cfg.max_parts]

    def _send(self, kind: str, target: str, text: str, at: Optional[str] = None) -> bool:
        """True if at least one part went out (a later part failing still leaves the first one delivered)."""
        sent_any = False
        for i, part in enumerate(self._split(text)):
            segs: List[Dict[str, Any]] = [{"type": "text", "data": {"text": part}}]
            if at and i == 0:
                segs = [{"type": "at", "data": {"qq": at}}, {"type": "text", "data": {"text": " " + part}}]
            if kind == "group":
                ok, code = self._call("send_group_msg", {"group_id": int(target), "message": segs})
            else:
                ok, code = self._call("send_private_msg", {"user_id": int(target), "message": segs})
            if not ok:
                self._bump("send_failed")
                self._log(f"send failed ({code}); dropped, not retried")
                return sent_any
            sent_any = True
        return sent_any

    def _call(self, action: str, params: Dict[str, Any]) -> Tuple[bool, str]:
        with self._ws_lock:
            ws = self._ws
        if ws is None:
            return False, "not_connected"
        with self._pending_lock:
            self._echo += 1
            echo = f"ah{self._echo}"
            fut: "concurrent.futures.Future[Dict[str, Any]]" = concurrent.futures.Future()
            self._pending[echo] = fut
        try:
            ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            resp = fut.result(timeout=self.cfg.send_timeout_s)
        except concurrent.futures.TimeoutError:
            return False, "timeout"
        except ConnectionError:
            return False, "disconnected"
        except Exception:
            return False, "send_error"
        finally:
            with self._pending_lock:
                self._pending.pop(echo, None)
        if resp.get("status") == "ok" or resp.get("retcode") == 0:
            return True, "ok"
        return False, f"retcode_{resp.get('retcode')}"
