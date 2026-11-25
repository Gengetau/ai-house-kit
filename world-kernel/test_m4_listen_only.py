"""M4 part B: `ONEBOT_LISTEN_ONLY=1` (listen-only / shadow-parallel mode).

The channel reads everything and writes nothing to QQ. Everything here uses the real `OneBotChannel` against a real local
OneBot WebSocket server that records every frame it receives, and the real `HouseDaemon` with its `SendGate` and
SQLite files. The assertion that matters most is always "the server saw zero frames", and each such test has a positive
control (a live channel on the same server does write) so that "zero" means "refused" and not "the server saw nothing".
Where a scripted model reply is needed, `reply_as` / the host's `converse` is replaced; nothing else is.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from test_tail_1_sendgate_channels import (  # noqa: E402
    AUD_B64,
    IMG_B64,
    Base,
    _FakeOneBotServer,
    _Host,
    _wait_until,
)

from ai_house.builtin_plugins.onebot_ws import (  # noqa: E402
    ChannelConfigError,
    OneBotChannel,
    parse_listen_only,
)
from ai_house.chat_agent import RoommateChatAgent  # noqa: E402
from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import make_http_server  # noqa: E402
from ai_house.media_provenance import sha256_of_b64  # noqa: E402
from worldkernel.outbox import BlockedDelivery, NotSent  # noqa: E402

CHAT = "qq-group:9001"
OWNER_TOKEN = "tail1-owner-token"  # the token `Base.mk_daemon` configures (a test value, not a real credential)
IMG = {"kind": "image", "image_base64": IMG_B64}


def _dm_event(mid: str, text: str = "在吗") -> Dict[str, Any]:
    return {"post_type": "message", "message_type": "private", "self_id": "7001", "user_id": "8001", "message_id": mid,
            "time": int(time.time()), "sender": {"nickname": "boss"}, "message": [{"type": "text", "data": {"text": text}}]}


def _group_event(mid: str, text: str = "你好呀") -> Dict[str, Any]:
    return {"post_type": "message", "message_type": "group", "self_id": "7001", "user_id": "8001", "group_id": "9001",
            "message_id": mid, "time": int(time.time()), "sender": {"nickname": "boss"},
            "message": [{"type": "at", "data": {"qq": "7001"}}, {"type": "text", "data": {"text": " " + text}}]}


class _ListenBase(Base):
    def setUp(self) -> None:
        self.d = self.mk_daemon()
        self.srv = _FakeOneBotServer()
        self.addCleanup(self.srv.close)

    def listening(self, replies: Optional[List[Dict[str, Any]]] = None, **cfg: Any) -> OneBotChannel:
        cfg.setdefault("groups", frozenset({"9001"}))
        host = _Host(self.d, replies) if replies is not None else None
        ch = self.mk_channel(self.d, self.srv, host=host, listen_only=True, **cfg)
        self.assertTrue(ch.listen_only)
        return ch

    def rows(self, ch: OneBotChannel) -> List[Dict[str, Any]]:
        return ch.listen_only_rows(limit=200)

    def assert_silent(self, why: str = "") -> None:
        time.sleep(0.15)  # a frame written on a worker thread would have arrived by now
        self.assertEqual(self.srv.frames, [], why or "a frame reached the OneBot server")

    def live_control(self) -> None:
        """A live channel on the same server does write, so the silence above was a refusal."""
        before = len(self.srv.frames)
        live = self.mk_channel(self.d, self.srv)
        self.assertTrue(live._send_bubbles("group", "9001", ["control"]))
        self.assertTrue(_wait_until(lambda: len(self.srv.frames) == before + 1))


# ======================================================================================================================
class SwitchTests(unittest.TestCase):
    ENV = {"ONEBOT_WS_URL": "ws://127.0.0.1:9", "ONEBOT_OWNER_QQ": "8001"}

    def test_1_exactly_one_zero_or_empty_anything_else_refuses_to_start(self) -> None:
        self.assertIs(parse_listen_only("1"), True)
        self.assertIs(parse_listen_only(" 1 "), True)
        for off in ("0", "", "  ", None):
            self.assertIs(parse_listen_only(off), False, off)
        for bad in ("true", "yes", "on", "2", "01", "TRUE", "1.0", "-1"):
            with self.assertRaises(ChannelConfigError, msg=bad):
                parse_listen_only(bad)
            with self.assertRaises(ChannelConfigError, msg=bad):
                OneBotChannel.from_env({**self.ENV, "ONEBOT_LISTEN_ONLY": bad})

    def test_2_default_is_off_and_only_the_environment_turns_it_on(self) -> None:
        self.assertFalse(OneBotChannel.from_env(dict(self.ENV)).listen_only)
        self.assertFalse(OneBotChannel.from_env({**self.ENV, "ONEBOT_LISTEN_ONLY": "0"}).listen_only)
        self.assertTrue(OneBotChannel.from_env({**self.ENV, "ONEBOT_LISTEN_ONLY": "1"}).listen_only)
        # a stored plugin setting cannot switch it on or off
        stored = {"ws_url": "ws://127.0.0.1:9", "owner_qq": "8001", "listen_only": True}
        self.assertFalse(OneBotChannel.from_config(stored, {}).listen_only)
        self.assertTrue(OneBotChannel.from_config({**stored, "listen_only": False}, {"ONEBOT_LISTEN_ONLY": "1"}).listen_only)

    def test_3_the_daemon_refuses_to_start_on_a_bad_value(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from test_tail_1_sendgate_channels import PACK, T0
        from ai_house.daemon import HouseDaemon

        env = {**self.ENV, "ONEBOT_LISTEN_ONLY": "true", "AI_HOUSE_OWNER_TOKEN": OWNER_TOKEN}
        with self.assertRaises(ChannelConfigError):
            d = HouseDaemon(tmp.name, pack_dir=PACK, env=env, start_ts=T0, virtual_clock=True, allow_onboarding=False)
            d.close()

    def test_4_ws_send_is_called_in_exactly_one_place(self) -> None:
        src = (ROOT / "src" / "ai_house" / "builtin_plugins" / "onebot_ws.py").read_text(encoding="utf-8")
        calls = re.findall(r"\bws\.send\(", src)
        self.assertEqual(len(calls), 1, "a second place writes to the socket: it needs the listen-only decision too")
        self.assertEqual(len(re.findall(r"\.send_text\(|\.sendall\(", src)), 0)


# ======================================================================================================================
class WriterGuaranteeTests(_ListenBase):
    def test_5_every_way_to_write_to_the_socket_is_refused_and_nothing_is_sent(self) -> None:
        ch = self.listening()
        seg = [{"type": "text", "data": {"text": "x"}}]
        for action, params in (
            ("send_group_msg", {"group_id": 9001, "message": seg}),
            ("send_private_msg", {"user_id": 8001, "message": seg}),
            ("get_login_info", {}),  # even a harmless action: listening needs no call at all
            ("set_group_kick", {"group_id": 9001, "user_id": 1}),
        ):
            self.assertEqual(ch._call_detailed(action, params), (False, "refused_listen_only", "", False), action)
        self.assertEqual(ch._call("send_group_msg", {"group_id": 9001, "message": seg})[:2], (False, "refused_listen_only"))
        with self.assertRaises(PermissionError):
            ch._ws.send(json.dumps({"action": "send_group_msg", "params": {"group_id": 9001, "message": seg}}))
        with self.assertRaises((BlockedDelivery, NotSent)):
            ch.deliver("dlv-direct-raw", {"kind": "group", "group_id": "9001", "text": "hi"})
        self.assert_silent()
        self.live_control()

    def test_6_the_gate_will_not_authorize_a_listen_only_channel_to_write(self) -> None:
        ch = self.listening()
        with self.assertRaises(BlockedDelivery) as cm:  # what a plugin calling `authorize_send` would get
            self.d.send_gate.authorize_direct_send("onebot_v11", ch, kind="group", target="9001", actor="sora", text="hi")
        self.assertEqual(cm.exception.reason, "listen_only")
        self.assertEqual(self.log_rows(self.d), 0)
        self.assert_silent()

    def test_7_a_channel_with_a_host_that_cannot_converse_in_listen_only_is_not_used(self) -> None:
        calls: List[str] = []

        class OldHost:
            def __init__(self, d: Any) -> None:
                self._d = d

            def __getattr__(self, name: str) -> Any:
                return getattr(self._d, name)

            def converse(self, channel: str, text: str, speaker: Any) -> Dict[str, Any]:  # no `listen_only`, no **kw
                calls.append(text)
                return {"replies": [{"mode": "live", "text": "hi", "media": []}]}

        ch = self.mk_channel(self.d, self.srv, host=OldHost(self.d), listen_only=True)
        self.assertEqual(ch.handle_event(_dm_event("m-old")), "ignored:listen_only_unsupported_host")
        self.assertEqual(calls, [])
        self.assert_silent()


# ======================================================================================================================
class DraftRecordTests(_ListenBase):
    def test_8_a_direct_reply_is_judged_recorded_charged_in_the_shadow_ledger_and_not_written(self) -> None:
        ch = self.listening([{"mode": "live", "text": "晚上好呀", "media": []}])
        self.assertEqual(ch.handle_event(_dm_event("m-direct")), "listen_only")
        self.assert_silent()
        rows = self.rows(ch)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["path"], r["verdict"], r["kind"], r["target"], r["actor"]),
                         ("direct", "allowed", "private", "8001", "sora"))
        self.assertEqual(r["bubbles"], ["晚上好呀"])
        self.assertEqual(r["inbound_ids"], ["m-direct"])  # the join key against the live system's own records
        self.assertEqual(ch.stats["listen_only"], 1)
        self.assertEqual(ch.stats["replied"], 0)
        # a reply that has a saved inbound row: the row is closed, with the reason, and no plan is saved for a resume
        sid = self.new_sid(ch, "m-direct-2", kind="private", target="8001")
        self.assertFalse(ch._send_bubbles("private", "8001", ["第二句"], spool_ids=[sid]))
        spool = self.spool_row(ch, sid)
        self.assertEqual((spool["state"], spool["reason"]), ("skipped", "listen_only"))
        # ledger: marked, and it is not one of the live kinds
        self.assertEqual(self.log_rows(self.d), 2)
        self.assertEqual(self.log_rows(self.d, "shadow=1 AND kind='direct_shadow' AND recip_key LIKE 'shadow:%'"), 2)
        self.assertEqual(self.log_rows(self.d, "kind='direct' OR shadow=0"), 0)
        self.live_control()

    def test_9_the_caps_are_simulated_in_the_shadow_ledger_and_live_starts_clean(self) -> None:
        d = self.mk_daemon({"AI_HOUSE_DIRECT_DAILY_CAP": "1"})
        ch = self.mk_channel(d, self.srv, host=_Host(d, [{"mode": "live", "text": "一", "media": []}]), listen_only=True)
        self.assertEqual(ch.handle_event(_dm_event("m-cap-1")), "listen_only")
        ch._host = _Host(d, [{"mode": "live", "text": "二", "media": []}])
        self.assertEqual(ch.handle_event(_dm_event("m-cap-2")), "no_reply")  # the daily cap said no, as it would live
        verdicts = [(r["verdict"]) for r in self.rows(ch)]
        self.assertIn("allowed", verdicts)
        self.assertIn("limited:daily_cap", verdicts)
        limited = next(r for r in self.rows(ch) if r["verdict"] == "limited:daily_cap")
        self.assertEqual(limited["bubbles"], [])
        self.assertGreater(limited["text_len"], 0)
        # "switch off": a live channel on the same data starts with an empty live count and may send today
        self.assertEqual(self.log_rows(d, "kind='direct'"), 0)
        live = self.mk_channel(d, self.srv)
        self.assertTrue(live._send_bubbles("private", "8001", ["live now"]))
        self.assertEqual(self.log_rows(d, "kind='direct'"), 1)

    def test_10_a_draft_the_privacy_check_stops_is_recorded_with_reason_and_length_not_text(self) -> None:
        secret_line = "只有私聊里才说过的那件事"
        ch = self.listening([{"mode": "live", "text": secret_line, "media": []}])
        with patch.object(self.d.memory, "check_outbound_leak", return_value=[1]):
            self.assertEqual(ch.handle_event(_group_event("m-leak")), "no_reply")
        r = self.rows(ch)[0]
        self.assertEqual(r["verdict"], "blocked:memory_leak:private_to_public")
        self.assertEqual(r["bubbles"], [])
        self.assertEqual(r["text_len"], len(secret_line))
        self.assertNotIn(secret_line, json.dumps(self.rows(ch), ensure_ascii=False))
        self.assertEqual(self.log_rows(self.d), 0)
        self.assert_silent()

    def test_11_media_is_kept_as_kind_hash_and_size_never_as_bytes(self) -> None:
        reg = self.d.media_provenance
        reg.register(IMG_B64, chat=CHAT, source_texts=["a small test picture"], kind="image")
        ch = self.listening([{"mode": "live", "text": "", "media": [IMG]}])
        self.assertEqual(ch.handle_event(_group_event("m-img")), "listen_only")
        r = self.rows(ch)[0]
        self.assertEqual(r["verdict"], "allowed")
        self.assertEqual(len(r["media"]), 1)
        self.assertEqual(r["media"][0]["kind"], "image")
        self.assertEqual(r["media"][0]["sha256"], sha256_of_b64(IMG_B64))
        self.assertGreater(r["media"][0]["size"], 0)
        blob = json.dumps(self.rows(ch))
        self.assertNotIn(IMG_B64, blob)
        for db in Path(self.tmp).rglob("*.db"):  # nor does any database file hold the bytes
            self.assertNotIn(IMG_B64.encode(), db.read_bytes(), str(db))
        # a media item that was never recorded is blocked in the shadow too, and it is still only hash/size
        ch._host = _Host(self.d, [{"mode": "live", "text": "", "media": [{"kind": "audio", "audio_base64": AUD_B64}]}])
        self.assertEqual(ch.handle_event(_group_event("m-aud")), "no_reply")
        r2 = self.rows(ch)[0]
        self.assertEqual(r2["verdict"], "blocked:media_provenance:missing")
        self.assertNotIn(AUD_B64, json.dumps(self.rows(ch)))
        self.assert_silent()


# ======================================================================================================================
class OutboxTests(_ListenBase):
    def flush(self, did: str, payload: Dict[str, Any]) -> str:
        self.d.enqueue_outbound(channel="onebot_v11", payload=payload, delivery_id=did)
        return str(self.d.flush_outbox(reconcile=False)["dispatched"].get(did))

    def test_12_reminders_and_proactive_leave_the_outbox_with_a_shadow_ref_and_are_not_replayed(self) -> None:
        ch = self.listening()
        self.d.channels = [ch]
        base = {"actor": "sora", "kind": "group", "group_id": "9001"}
        now = self.d.clock.now()
        with patch.object(self.d.memory, "observe", wraps=self.d.memory.observe) as observe:
            self.assertEqual(self.flush("rem-lo-1", {**base, "text": "⏰ 到点提醒：喝水", "urgent": True, "source": "reminder"}), "sent")
            self.assertEqual(self.flush("pro-lo-1", {**base, "text": "想起一件事", "source": "proactive", "created_ts": now}), "sent")
            self.assertEqual(observe.call_count, 0, "a draft that was never sent was remembered as said")
        self.assert_silent()
        self.assertEqual(self.d.send_gate.lookup_delivery("onebot_v11", ch, "rem-lo-1", is_webchat=False) or "", "")
        paths = [(r["path"], r["source"], r["verdict"], r["delivery_id"]) for r in self.rows(ch)]
        self.assertIn(("outbox", "reminder", "allowed", "rem-lo-1"), paths)
        self.assertIn(("outbox", "proactive", "allowed", "pro-lo-1"), paths)
        self.assertEqual(self.log_rows(self.d, "kind='outbox_shadow' AND shadow=1"), 2)
        self.assertEqual(self.log_rows(self.d, "kind='outbox' OR shadow=0"), 0)
        # the switch is turned off: the same rows are not sent now
        ch.stop()
        live = self.mk_channel(self.d, self.srv)
        self.d.channels = [live]
        res = self.d.flush_outbox(reconcile=False)
        self.assertNotIn("rem-lo-1", res["dispatched"])
        self.assertNotIn("pro-lo-1", res["dispatched"])
        self.assert_silent()

    def test_13_a_blocked_outbox_draft_keeps_reason_and_length_only(self) -> None:
        ch = self.listening()
        self.d.channels = [ch]
        text = "看一下 /etc/passwd 里有什么"
        with self.assertRaises(BlockedDelivery):
            self.d.send_gate.deliver_gated("onebot_v11", ch, "pro-lo-bad",
                                           {"actor": "sora", "kind": "group", "group_id": "9001", "text": text,
                                            "source": "proactive", "created_ts": self.d.clock.now()}, is_webchat=False)
        r = self.rows(ch)[0]
        self.assertTrue(r["verdict"].startswith("blocked:guard:"), r["verdict"])
        self.assertEqual((r["bubbles"], r["text_len"]), ([], len(text)))
        self.assertNotIn("passwd", json.dumps(self.rows(ch)))
        self.assertEqual(self.log_rows(self.d), 0)

    def test_14b_a_held_back_reminder_is_recorded_once_per_reason_not_once_per_flush(self) -> None:
        """Fails if every flush of a reminder that waits on a mute writes another row: the outbox keeps it pending and
        the daemon flushes on every tick, so the record (capped) would fill up with copies of one waiting reminder."""
        ch = self.listening()
        self.d.channels = [ch]
        payload = {"actor": "sora", "kind": "group", "group_id": "9001", "text": "⏰ 到点提醒：喝水",
                   "urgent": True, "source": "reminder"}
        did = self.d.enqueue_outbound(channel="onebot_v11", payload=payload, delivery_id="rem-lo-wait")
        self.d.group_behavior.mute(CHAT, minutes=30)
        for _ in range(3):
            self.assertNotEqual(self.d.flush_outbox(reconcile=False)["dispatched"].get(did), "sent")
        mine = [r for r in self.rows(ch) if r["delivery_id"] == did]
        self.assertEqual([r["verdict"] for r in mine], ["limited:group_muted"], mine)
        self.d.group_behavior.unmute(CHAT)
        self.assertEqual(self.d.flush_outbox(reconcile=False)["dispatched"].get(did), "sent")
        mine = [r for r in self.rows(ch) if r["delivery_id"] == did]
        self.assertEqual(sorted(r["verdict"] for r in mine), ["allowed", "limited:group_muted"], mine)
        self.assert_silent()

    def test_14_reply_ready_rows_saved_before_the_switch_are_closed_at_start(self) -> None:
        live = self.mk_channel(self.d, self.srv, groups=frozenset({"9001"}))
        sid = self.new_sid(live, "m-old-ready")
        live._spool.save_reply_plan([sid], ["old a", "old b"], target="9001", kind="group", at="")
        self.assertEqual(self.spool_row(live, sid)["state"], "reply_ready")
        listen = self.mk_channel(self.d, self.srv, listen_only=True)
        row = self.spool_row(listen, sid)
        self.assertEqual((row["state"], row["reason"]), ("skipped", "listen_only"))
        self.assert_silent()
        listen.stop()
        again = self.mk_channel(self.d, self.srv)  # switched off: nothing resumes
        self.assertEqual(self.spool_row(again, sid)["state"], "skipped")
        self.assert_silent()


# ======================================================================================================================
class MemoryTests(_ListenBase):
    def setUp(self) -> None:
        super().setUp()
        self.reply = "这是一条没有真的发出去的话"
        self.seen_recent: List[Any] = []

        def reply_as(aid, text, card, snap, recent, where="private", speaker=None, **kw):
            self.seen_recent.append(list(recent))
            return {"actor": aid, "name": "Sora", "text": self.reply, "context_badge": "", "mode": "live_api"}

        patcher = patch.object(self.d.chat_agent, "reply_as", side_effect=reply_as)
        patcher.start()
        self.addCleanup(patcher.stop)
        obs = patch.object(self.d.memory, "observe", wraps=self.d.memory.observe)
        self.observe = obs.start()
        self.addCleanup(obs.stop)

    def test_15_inbound_is_stored_the_draft_is_a_shadow_row_and_memory_gets_an_empty_reply(self) -> None:
        ch = self.mk_channel(self.d, self.srv, listen_only=True)
        self.assertEqual(ch.handle_event(_dm_event("m-mem", "明天想吃什么")), "listen_only")
        self.assert_silent()
        rows = self.d.list_chat("private:sora")
        inbound = [r for r in rows if r["text"] == "明天想吃什么"]
        draft = [r for r in rows if r["text"] == self.reply]
        self.assertEqual(len(inbound), 1)
        self.assertEqual([r["mode"] for r in draft], ["shadow"])
        calls = self.observe.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs.get("reply"), "")  # extraction never reads the draft as her words
        self.assertEqual(calls[0].args[2] if len(calls[0].args) > 2 else calls[0].kwargs.get("text"), "明天想吃什么")

    def test_16_web_chat_is_unaffected_by_a_listening_channel(self) -> None:
        ch = self.mk_channel(self.d, self.srv, listen_only=True)
        self.assertTrue(ch.listen_only)
        res = self.d.converse("private:sora", "网页上说句话", owner_speaker("房主"))
        self.assertEqual(res["replies"][0]["text"], self.reply)
        rows = self.d.list_chat("private:sora")
        self.assertEqual([r["mode"] for r in rows if r["text"] == self.reply], ["live_api"])
        self.assertEqual(self.observe.call_args_list[-1].kwargs.get("reply"), self.reply)
        self.assert_silent()

    def test_17_a_shadow_row_is_not_handed_back_to_her_as_something_she_said(self) -> None:
        agent = RoommateChatAgent(SimpleNamespace(ROSTER=("sora",), NAMES={"sora": "Sora"}, SELVES={}), env={},
                                  router=SimpleNamespace(record_external_usage=lambda **kw: None,
                                                         collect_secrets=lambda: []))  # type: ignore[arg-type]
        ctx = {"place_label": "客厅", "doing": "发呆", "roommates_here": [], "wearing": "家居服", "energy": 50,
               "hunger": 10, "mood": "平静", "meals_today": [], "availability": "awake", "attachment_notes": []}
        captured: List[Any] = []

        def via_agent(*a: Any, **kw: Any) -> Dict[str, Any]:
            captured.append(kw["history"])
            return {"quiet": False, "text": "好", "engine": "claude", "quiet_reason": "", "model": "m"}

        history = [
            {"sender": "owner", "sender_name": "房主", "text": "今晚吃什么", "mode": "owner"},
            {"sender": "sora", "sender_name": "Sora", "text": "没发出去的草稿", "mode": "shadow"},
            {"sender": "sora", "sender_name": "Sora", "text": "真的说过的话", "mode": "live_api"},
            {"sender": "owner", "sender_name": "房主", "text": "那就这样", "mode": "owner"},
        ]
        with patch.object(agent, "build_actor_context", return_value=ctx), \
                patch.object(agent, "brain_for", return_value="claude"), \
                patch.object(agent, "agent_engine_for", return_value="claude"), \
                patch.object(agent, "_reply_via_agent", side_effect=via_agent):
            agent.reply_as("sora", "那就这样", {}, {}, history, "private", owner_speaker("房主"), channel="private:sora")
        shown = "\n".join(line for line, _is_self in captured[0])
        self.assertIn("真的说过的话", shown)
        self.assertIn("今晚吃什么", shown)
        self.assertNotIn("没发出去的草稿", shown)


# ======================================================================================================================
class VisibilityTests(_ListenBase):
    def test_18_doctor_the_ops_state_and_the_owner_only_endpoint_show_the_mode(self) -> None:
        from ai_house import cli

        def doctor(settings: Dict[str, str]) -> Dict[str, Any]:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            env_file = Path(tmp.name) / ".env"
            env_file.write_text("".join(f"{k}={v}\n" for k, v in settings.items()), encoding="utf-8")
            out = io.StringIO()
            with patch.dict("os.environ", {}, clear=False), contextlib.redirect_stdout(out):
                import os

                os.environ.pop("ONEBOT_LISTEN_ONLY", None)
                cli.main(["doctor", "--env-file", str(env_file), "--data-dir", str(Path(tmp.name) / "rt"), "--json"])
            return {c["name"]: c for c in json.loads(out.getvalue())["checks"]}["onebot_listen_only"]

        qq = {"ONEBOT_WS_URL": "ws://127.0.0.1:9", "ONEBOT_OWNER_QQ": "8001"}  # the line belongs to a configured QQ channel
        on, off, bad = (doctor({**qq, "ONEBOT_LISTEN_ONLY": "1"}), doctor(qq), doctor({**qq, "ONEBOT_LISTEN_ONLY": "true"}))
        self.assertTrue(on["ok"])
        self.assertIn("只听不发已开启", on["detail"])
        self.assertTrue(off["ok"])
        self.assertIn("未开启", off["detail"])
        self.assertFalse(bad["ok"])

        ch = self.listening([{"mode": "live", "text": "给你看的草稿", "media": []}])
        self.d.channels = [ch]
        self.assertEqual(self.d._plugins_state_view()["listen_only_channels"], ["onebot_v11"])
        self.assertEqual(ch.handle_event(_dm_event("m-vis")), "listen_only")
        srv = make_http_server(self.d, host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_port}/api/listen-only"

        def get(token: str) -> Any:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as err:
                return err.code, None

        self.assertIn(get("")[0], (401, 403))
        self.assertIn(get("not-the-owner-token")[0], (401, 403))
        status, body = get(OWNER_TOKEN)
        self.assertEqual(status, 200)
        self.assertTrue(body["listen_only"])
        self.assertEqual(body["items"][0]["bubbles"], ["给你看的草稿"])
        # off: the same endpoint says so and lists nothing
        self.d.channels = []
        self.assertEqual(self.d._plugins_state_view()["listen_only_channels"], [])
        self.assertFalse(get(OWNER_TOKEN)[1]["listen_only"])

    def test_19_unset_switch_means_the_channel_behaves_as_before(self) -> None:
        ch = self.mk_channel(self.d, self.srv, host=_Host(self.d, [{"mode": "live", "text": "照常发", "media": []}]))
        self.assertFalse(ch.listen_only)
        self.assertEqual(ch.handle_event(_dm_event("m-live")), "replied")
        self.assertTrue(_wait_until(lambda: len(self.srv.frames) == 1))
        self.assertEqual(self.log_rows(self.d, "kind='direct' AND shadow=0"), 1)
        self.assertEqual(ch.listen_only_rows(), [])
        self.assertEqual(ch.stats["listen_only"], 0)


if __name__ == "__main__":
    unittest.main()
