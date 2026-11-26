"""`ONEBOT_LISTEN_ONLY_OBSERVE=1`: a listen-only channel reads back what the account really sent and uses it as her reply.

Same harness as `test_m4_listen_only`: the real `OneBotChannel` against a local OneBot WebSocket server that records
every frame, and the real `HouseDaemon`. The server here also answers the two history actions from a script. The
assertion that matters most: the only frames the server ever sees are history reads, never a send.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from test_m2_intents_adapter import T0 as T0_DEFAULT, fake_model  # noqa: E402
from test_tail_1_sendgate_channels import Base, _FakeOneBotServer, _wait_until  # noqa: E402

from ai_house.builtin_plugins.onebot_ws import (  # noqa: E402
    LISTEN_ONLY_READ_ACTIONS,
    ChannelConfigError,
    OneBotChannel,
    OneBotConfig,
    parse_listen_only_observe,
)
from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon  # noqa: E402

HIST = ("get_friend_msg_history", "get_group_msg_history")
SELF = "7001"
OWNER_ASK = "奈尔，明天帮我买一盒红茶回来，好吗？"
OWNER_QUOTE = "明天帮我买一盒红茶回来"
HER_REAL = "好呀，明天帮你买一盒红茶回来。"
DRAFT = "这是一条没有真的发出去的草稿"


def _seg(text: str) -> List[Dict[str, Any]]:
    return [{"type": "text", "data": {"text": text}}]


def _msg(mid: str, who: str, t: float, message: Any) -> Dict[str, Any]:
    return {"message_id": mid, "user_id": int(who), "time": int(t), "message": message, "sender": {"user_id": int(who)}}


def _dm(mid: str, t: float, text: str = "在吗") -> Dict[str, Any]:
    return {"post_type": "message", "message_type": "private", "self_id": SELF, "user_id": "8001", "message_id": mid,
            "time": int(t), "sender": {"nickname": "boss"}, "message": _seg(text)}


def _grp(mid: str, t: float, text: str = "你好呀") -> Dict[str, Any]:
    return {"post_type": "message", "message_type": "group", "self_id": SELF, "user_id": "8001", "group_id": "9001",
            "message_id": mid, "time": int(t), "sender": {"nickname": "boss"},
            "message": [{"type": "at", "data": {"qq": SELF}}, {"type": "text", "data": {"text": " " + text}}]}


class _HistoryServer(_FakeOneBotServer):
    """Records every frame; answers the history actions from `script[action](params, call_no)`.
    A script returning None makes the action fail with a retcode (an endpoint without the extension)."""

    def __init__(self) -> None:
        self.script: Dict[str, Callable[[Dict[str, Any], int], Any]] = {}
        self.calls: Dict[str, int] = {}
        super().__init__()

    def _handler(self, ws: Any) -> None:
        for raw in ws:
            frame = json.loads(raw)
            self.frames.append(frame)
            act = frame.get("action")
            if act in HIST:
                self.calls[act] = self.calls.get(act, 0) + 1
                fn = self.script.get(act)
                data = fn(frame.get("params") or {}, self.calls[act]) if fn else None
                if data is None:
                    ws.send(json.dumps({"status": "failed", "retcode": 1404, "echo": frame.get("echo")}))
                else:
                    ws.send(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": frame.get("echo")}))
                continue
            ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 1}, "echo": frame.get("echo")}))

    def sends(self) -> List[Dict[str, Any]]:
        return [f for f in self.frames if f.get("action") not in HIST]

    def hist_frames(self) -> List[Dict[str, Any]]:
        return [f for f in self.frames if f.get("action") in HIST]


class _ObserveBase(Base):
    OBS = dict(observe_window_s=6.0, observe_poll_s=0.1)  # replies end the wait early; "none" tests shorten it

    def setUp(self) -> None:
        self.d = self.mk_daemon()
        self.srv = _HistoryServer()
        self.addCleanup(self.srv.close)
        self.reply_as = patch.object(self.d.chat_agent, "reply_as", side_effect=self._reply_as)
        self.reply_as.start()
        self.addCleanup(self.reply_as.stop)
        obs = patch.object(self.d.memory, "observe", wraps=self.d.memory.observe)
        self.observe = obs.start()
        self.addCleanup(obs.stop)

    def _reply_as(self, aid: str, text: str, *a: Any, **kw: Any) -> Dict[str, Any]:
        return {"actor": aid, "name": "Sora", "text": DRAFT, "context_badge": "", "mode": "live_api"}

    def channel(self, observe: bool = True, **cfg: Any) -> OneBotChannel:
        kw = dict(self.OBS) if observe else {}
        kw.update(cfg)
        ch = self.mk_channel(self.d, self.srv, listen_only=True, listen_only_observe=observe, **kw)
        self.logs: List[str] = []
        ch._log = self.logs.append  # type: ignore[method-assign]
        return ch

    def observe_rows(self, ch: OneBotChannel) -> List[Dict[str, Any]]:
        return [r for r in ch.listen_only_rows(limit=200) if r["path"] == "observe"]

    def wait_record(self, ch: OneBotChannel) -> Dict[str, Any]:
        self.assertTrue(_wait_until(lambda: bool(self.observe_rows(ch)), 8.0), "the observer never finished")
        return self.observe_rows(ch)[0]

    def chat(self, channel: str) -> List[Dict[str, Any]]:
        return self.d.list_chat(channel)

    def hist_for(self, kind: str, msgs: Any, wrap: bool = False) -> None:
        act = "get_friend_msg_history" if kind == "private" else "get_group_msg_history"
        self.srv.script[act] = lambda params, n: ({"messages": msgs} if wrap else msgs) if not callable(msgs) else msgs(n)


# ======================================================================================================================
class SwitchTests(unittest.TestCase):
    ENV = {"ONEBOT_WS_URL": "ws://127.0.0.1:9", "ONEBOT_OWNER_QQ": "8001"}

    def test_1_exactly_one_zero_or_empty(self) -> None:
        self.assertIs(parse_listen_only_observe("1", True), True)
        self.assertIs(parse_listen_only_observe(" 1 ", True), True)
        for off in ("0", "", " ", None):
            self.assertIs(parse_listen_only_observe(off, True), False, off)
            self.assertIs(parse_listen_only_observe(off, False), False, off)
        for bad in ("true", "yes", "on", "2", "01", "TRUE"):
            with self.assertRaises(ChannelConfigError, msg=bad):
                parse_listen_only_observe(bad, True)

    def test_2b_the_wait_is_long_enough_for_a_slow_bot_and_can_be_set(self) -> None:
        """Seen on a real account: the other program answered 176 s after the owner's line, past a 120 s wait.
        Fails if the default wait is under 10 minutes, or the two settings are not read or not range-checked."""
        base = {**self.ENV, "ONEBOT_LISTEN_ONLY": "1", "ONEBOT_LISTEN_ONLY_OBSERVE": "1"}
        on = OneBotChannel.from_env(base)
        self.assertGreaterEqual(on.cfg.observe_window_s, 600.0)
        self.assertEqual(on.cfg.observe_poll_s, 10.0)
        set_ = OneBotChannel.from_env({**base, "ONEBOT_LISTEN_ONLY_OBSERVE_WINDOW_S": "900",
                                       "ONEBOT_LISTEN_ONLY_OBSERVE_POLL_S": "20"})
        self.assertEqual((set_.cfg.observe_window_s, set_.cfg.observe_poll_s), (900.0, 20.0))
        for key, bad in (("ONEBOT_LISTEN_ONLY_OBSERVE_WINDOW_S", "10"), ("ONEBOT_LISTEN_ONLY_OBSERVE_WINDOW_S", "abc"),
                         ("ONEBOT_LISTEN_ONLY_OBSERVE_POLL_S", "0.5"), ("ONEBOT_LISTEN_ONLY_OBSERVE_POLL_S", "999")):
            with self.assertRaises(ChannelConfigError, msg=(key, bad)):
                OneBotChannel.from_env({**base, key: bad})

    def test_2_observe_without_listen_only_fails_and_the_default_is_off(self) -> None:
        for lo in ("", "0"):
            with self.assertRaises(ChannelConfigError) as cm:
                OneBotChannel.from_env({**self.ENV, "ONEBOT_LISTEN_ONLY": lo, "ONEBOT_LISTEN_ONLY_OBSERVE": "1"})
            self.assertIn("ONEBOT_LISTEN_ONLY=1", str(cm.exception))
        with self.assertRaises(ChannelConfigError):
            OneBotChannel.from_env({**self.ENV, "ONEBOT_LISTEN_ONLY": "1", "ONEBOT_LISTEN_ONLY_OBSERVE": "true"})
        on = OneBotChannel.from_env({**self.ENV, "ONEBOT_LISTEN_ONLY": "1", "ONEBOT_LISTEN_ONLY_OBSERVE": "1"})
        self.assertTrue(on.observing)
        only = OneBotChannel.from_env({**self.ENV, "ONEBOT_LISTEN_ONLY": "1"})
        self.assertTrue(only.listen_only and not only.observing)
        self.assertFalse(OneBotChannel.from_env(dict(self.ENV)).observing)
        # a stored plugin setting cannot switch it on
        stored = {"ws_url": "ws://127.0.0.1:9", "owner_qq": "8001", "listen_only_observe": True}
        self.assertFalse(OneBotChannel.from_config(stored, {"ONEBOT_LISTEN_ONLY": "1"}).observing)

    def test_3_the_daemon_refuses_to_start(self) -> None:
        from test_tail_1_sendgate_channels import PACK, T0

        for extra in ({"ONEBOT_LISTEN_ONLY_OBSERVE": "1"}, {"ONEBOT_LISTEN_ONLY": "1", "ONEBOT_LISTEN_ONLY_OBSERVE": "yes"}):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            env = {**self.ENV, **extra, "AI_HOUSE_OWNER_TOKEN": "tail1-owner-token"}
            with self.assertRaises(ChannelConfigError):
                HouseDaemon(tmp.name, pack_dir=PACK, env=env, start_ts=T0, virtual_clock=True, allow_onboarding=False).close()


# ======================================================================================================================
class GuardTests(_ObserveBase):
    SEG = _seg("x")

    def test_4_only_the_two_history_reads_pass_and_only_with_observe_on(self) -> None:
        self.assertEqual(LISTEN_ONLY_READ_ACTIONS, frozenset(HIST))
        ch = self.channel()
        self.srv.script.update({a: (lambda params, n: []) for a in HIST})
        refused = (False, "refused_listen_only", "", False)
        for action, params in (
            ("send_group_msg", {"group_id": 9001, "message": self.SEG}),
            ("send_private_msg", {"user_id": 8001, "message": self.SEG}),
            ("send_msg", {"user_id": 8001, "message": self.SEG}),
            ("delete_msg", {"message_id": 1}),
            ("set_group_kick", {"group_id": 9001, "user_id": 1}),
            ("get_login_info", {}),  # a harmless read that is not on the list
            ("get_group_msg_history_x", {"group_id": 9001}),
            ("totally_unknown_action", {}),
        ):
            self.assertEqual(ch._call_detailed(action, params), refused, action)
            self.assertEqual(ch._may_write(action, params), "refused_listen_only", action)
        for action, params in (("get_friend_msg_history", {"user_id": 8001, "count": 5}),
                               ("get_group_msg_history", {"group_id": 9001, "count": 5})):
            self.assertEqual(ch._may_write(action, params), "", action)
            ok, code, _, _ = ch._call_detailed(action, params)
            self.assertEqual((ok, code), (True, "ok"), action)
        # the wrapper around the socket says the same on its own, whoever calls it
        with self.assertRaises(PermissionError):
            ch._ws.send(json.dumps({"action": "send_group_msg", "params": {"group_id": 9001, "message": self.SEG}}))
        with self.assertRaises(PermissionError):
            ch._ws.send(json.dumps({"action": "unknown_thing", "params": {}}))
        with self.assertRaises(PermissionError):
            ch._ws.send("not json")
        self.assertEqual(self.srv.sends(), [])
        self.assertEqual(len(self.srv.hist_frames()), 2)

    def test_5_with_observe_off_even_the_history_reads_are_refused(self) -> None:
        ch = self.channel(observe=False)
        for action in HIST:
            self.assertEqual(ch._call_detailed(action, {"user_id": 8001, "count": 5}), (False, "refused_listen_only", "", False))
            with self.assertRaises(PermissionError):
                ch._ws.send(json.dumps({"action": action, "params": {}}))
        time.sleep(0.15)
        self.assertEqual(self.srv.frames, [])

    def test_6_a_live_channel_is_not_affected(self) -> None:
        live = self.mk_channel(self.d, self.srv)
        self.assertEqual(live._may_write("get_friend_msg_history", {"user_id": 8001}), "refused_action")
        self.assertEqual(live._may_write("send_private_msg", {"user_id": 8001, "message": self.SEG}), "")


# ======================================================================================================================
class ObserveOffTests(_ObserveBase):
    def test_7_nothing_is_read_and_nothing_changes(self) -> None:
        ch = self.channel(observe=False)
        self.srv.script.update({a: (lambda params, n: [_msg("s1", SELF, time.time() + 1, _seg(HER_REAL))]) for a in HIST})
        self.assertEqual(ch.handle_event(_dm("m-off", time.time(), "明天想吃什么")), "listen_only")
        time.sleep(0.4)
        self.assertEqual(self.srv.frames, [])  # no history call, nothing at all
        self.assertEqual(self.srv.calls, {})
        self.assertEqual([r["mode"] for r in self.chat("private:sora") if r["text"] == DRAFT], ["shadow"])
        self.assertEqual(self.observe.call_args_list[0].kwargs.get("reply"), "")
        self.assertEqual(len(self.observe.call_args_list), 1)
        self.assertEqual(self.observe_rows(ch), [])
        self.assertNotIn("observed", ch.stats)  # the counters of the new path are not even there


# ======================================================================================================================
class ObserveTests(_ObserveBase):
    def test_8_private_turn_the_real_reply_becomes_her_chat_row_and_her_memory_line(self) -> None:
        t = time.time()
        ch = self.channel()
        old_self = _msg("s0", SELF, t - 50, _seg("很久以前我自己发的"))  # before the incoming message: not a reply
        mine = _msg("s1", SELF, t + 2, _seg(HER_REAL))
        self.hist_for("private", [old_self, _msg("o1", "8001", t, _seg("x")), mine])
        self.assertEqual(ch.handle_event(_dm("m-priv", t, "明天想吃什么")), "listen_only")
        rec = self.wait_record(ch)
        rows = self.chat("private:sora")
        self.assertEqual([r["mode"] for r in rows if r["text"] == DRAFT], ["shadow"])  # the draft row is unchanged
        observed = [r for r in rows if r["mode"] == "observed"]
        self.assertEqual([(r["text"], r["sender"]) for r in observed], [(HER_REAL, "sora")])
        calls = self.observe.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual((calls[0].args[2], calls[0].kwargs["reply"]), ("明天想吃什么", ""))  # as before
        self.assertEqual((calls[1].args[2], calls[1].kwargs["reply"]), ("", HER_REAL))  # her real line, once
        # the record: that it was observed, how long, how many bubbles; not the text
        self.assertEqual(rec["verdict"], "observed:1")
        self.assertEqual((rec["kind"], rec["target"], rec["text_len"], rec["bubbles"]), ("private", "8001", len(HER_REAL), []))
        self.assertEqual(rec["inbound_ids"], ["m-priv"])
        self.assertNotIn(HER_REAL, json.dumps(ch.listen_only_rows(limit=200), ensure_ascii=False))
        self.assertEqual(ch.stats["observed"], 1)
        # only history reads left the process; one at the first poll and one more for following bubbles
        self.assertEqual(self.srv.sends(), [])
        h = self.srv.hist_frames()
        self.assertEqual({f["action"] for f in h}, {"get_friend_msg_history"})
        self.assertEqual(h[0]["params"]["user_id"], 8001)
        self.assertIn("count", h[0]["params"])
        self.assertEqual(len(h), 2)
        # the live-only counters stay at nothing written
        self.assertEqual(ch.stats["replied"], 0)

    def test_9_group_turn_and_the_messages_shape(self) -> None:
        t = time.time()
        ch = self.channel()
        self.hist_for("group", [_msg("g0", "8002", t + 1, _seg("别人插话")), _msg("g1", SELF, t + 3, _seg("在的在的"))], wrap=True)
        self.assertEqual(ch.handle_event(_grp("m-grp", t)), "listen_only")
        rec = self.wait_record(ch)
        self.assertEqual(rec["verdict"], "observed:1")
        self.assertEqual(rec["kind"], "group")
        rows = self.chat("qq-group:9001")
        self.assertEqual([r["text"] for r in rows if r["mode"] == "observed"], ["在的在的"])
        self.assertEqual([r["mode"] for r in rows if r["text"] == DRAFT], ["shadow"])
        self.assertEqual(self.srv.sends(), [])
        h = self.srv.hist_frames()
        self.assertEqual({f["action"] for f in h}, {"get_group_msg_history"})
        self.assertEqual(h[0]["params"]["group_id"], 9001)

    def test_10_two_bubbles_are_joined_and_media_are_placeholders(self) -> None:
        t = time.time()
        ch = self.channel()
        first = _msg("s1", SELF, t + 1, _seg("好呀"))
        second = _msg("s2", SELF, t + 2, [{"type": "text", "data": {"text": "看这个"}},
                                          {"type": "image", "data": {"file": "x.png", "url": "http://example.invalid/x"}}])
        third = _msg("s3", SELF, t + 3, _seg("第三条"))
        # the first poll sees one bubble; the one more poll sees the rest
        self.hist_for("private", lambda n: [first] if n == 1 else [first, second, third])
        ch.handle_event(_dm("m-two", t))
        rec = self.wait_record(ch)
        self.assertEqual(rec["verdict"], "observed:3")
        text = "好呀\n看这个[图片]\n第三条"
        self.assertEqual([r["text"] for r in self.chat("private:sora") if r["mode"] == "observed"], [text])
        self.assertEqual(rec["text_len"], len(text))
        self.assertEqual(self.observe.call_args_list[-1].kwargs["reply"], text)

    def test_11_the_cap_and_someone_else_speaking_stop_the_collection(self) -> None:
        t = time.time()
        ch = self.channel(observe_max_msgs=2)
        many = [_msg(f"s{i}", SELF, t + i, _seg(f"第{i}条")) for i in range(1, 6)]
        self.hist_for("private", many)
        ch.handle_event(_dm("m-cap", t))
        self.wait_record(ch)
        self.assertEqual(self.observe.call_args_list[-1].kwargs["reply"], "第1条\n第2条")
        # a newer message from someone else ends it: what the account sends after that is another turn
        picked, closed = ch._pick_own_replies(
            [_msg("a", SELF, t + 1, _seg("答")), _msg("b", "8001", t + 2, _seg("又问")), _msg("c", SELF, t + 3, _seg("后一句"))],
            t, SELF)
        self.assertEqual(([x[1] for x in picked], closed), (["答"], True))
        # window edges: before the incoming message and after the window are not hers for this turn
        picked, _ = ch._pick_own_replies(
            [_msg("a", SELF, t - 1, _seg("早")), _msg("b", SELF, t + 500, _seg("晚"))], t, SELF)
        self.assertEqual(picked, [])

    def test_12_a_message_is_used_for_one_turn_only(self) -> None:
        t = time.time()
        ch = self.channel()
        self.hist_for("private", [_msg("s1", SELF, t + 1, _seg("只有这一条"))])
        ch.handle_event(_dm("m-a", t))
        self.wait_record(ch)
        ch.handle_event(_dm("m-b", t))
        self.assertTrue(_wait_until(lambda: len(self.observe_rows(ch)) == 2, 8.0))
        self.assertEqual(sorted(r["verdict"] for r in self.observe_rows(ch)), ["observe_none", "observed:1"])
        self.assertEqual(len([r for r in self.chat("private:sora") if r["mode"] == "observed"]), 1)

    def test_13_no_reply_in_the_window_is_todays_behaviour(self) -> None:
        t = time.time()
        ch = self.channel(observe_window_s=0.5)
        self.hist_for("private", [_msg("o1", "8001", t, _seg("x"))])
        self.assertEqual(ch.handle_event(_dm("m-none", t, "明天想吃什么")), "listen_only")
        rec = self.wait_record(ch)
        self.assertEqual((rec["verdict"], rec["text_len"]), ("observe_none", 0))
        self.assertEqual([r for r in self.chat("private:sora") if r["mode"] == "observed"], [])
        self.assertEqual(len(self.observe.call_args_list), 1)
        self.assertEqual(self.observe.call_args_list[0].kwargs["reply"], "")
        self.assertEqual(ch.stats["observe_miss"], 1)
        self.assertEqual(self.srv.sends(), [])
        self.assertGreaterEqual(len(self.srv.hist_frames()), 2)  # it did poll, several times, and then gave up

    def test_14_a_failing_history_action_falls_back_with_one_log_line_and_no_retry(self) -> None:
        t = time.time()
        ch = self.channel()  # the server has no script: every history call answers retcode 1404
        self.assertEqual(ch.handle_event(_dm("m-fail", t, "明天想吃什么")), "listen_only")
        rec = self.wait_record(ch)
        self.assertEqual(rec["verdict"], "observe_error:retcode_1404")
        self.assertEqual(len(self.srv.hist_frames()), 1)  # asked once, never again
        time.sleep(0.4)
        self.assertEqual(len(self.srv.hist_frames()), 1)
        lines = [x for x in self.logs if "observe" in x]
        self.assertEqual(len(lines), 1, self.logs)
        self.assertIn("retcode_1404", lines[0])
        self.assertNotIn("明天想吃什么", lines[0])
        self.assertEqual([r for r in self.chat("private:sora") if r["mode"] == "observed"], [])
        self.assertEqual(self.observe.call_args_list[0].kwargs["reply"], "")
        self.assertEqual(ch.stats["observe_failed"], 1)
        self.assertEqual(self.srv.sends(), [])

    def test_15_an_unusable_response_shape_is_an_error_not_a_reply(self) -> None:
        t = time.time()
        ch = self.channel()
        self.srv.script["get_friend_msg_history"] = lambda params, n: {"unexpected": True}
        ch.handle_event(_dm("m-shape", t))
        self.assertEqual(self.wait_record(ch)["verdict"], "observe_error:bad_shape")

    def test_16_handling_the_event_does_not_wait_for_the_observer(self) -> None:
        t = time.time()
        ch = self.channel(observe_window_s=3.0, observe_poll_s=1.0)
        self.hist_for("private", [])
        start = time.monotonic()
        ch.handle_event(_dm("m-fast", t))
        self.assertLess(time.monotonic() - start, 0.9)  # returned long before the first poll
        self.assertEqual(self.observe_rows(ch), [])
        ch.stop()  # and a stop ends the waiting thread quickly

    def test_17_the_observed_row_is_part_of_what_she_is_shown_as_said(self) -> None:
        src = (ROOT / "src" / "ai_house" / "chat_agent.py").read_text(encoding="utf-8")
        self.assertIn('not in ("blocked", "shadow")', src)  # `observed` is not in that list: it is shown
        self.d.record_observed_reply("private:sora", HER_REAL, owner_speaker("房主"))
        self.assertEqual([r["mode"] for r in self.chat("private:sora")], ["observed"])

    def test_17b_the_inbound_line_waits_for_the_real_reply_and_keeps_its_own_time(self) -> None:
        t = time.time()
        ch = self.channel()
        came = threading.Event()
        self.hist_for("private", lambda n: [_msg("s1", SELF, t + 2, _seg(HER_REAL))] if came.is_set() else [])
        self.assertEqual(ch.handle_event(_dm("m-held", t, "明天想吃什么")), "listen_only")
        time.sleep(0.3)  # several polls: still waiting
        self.assertEqual(self.observe.call_args_list, [])  # memory has not got the line yet
        came.set()
        self.assertEqual(self.wait_record(ch)["verdict"], "observed:1")
        calls = self.observe.call_args_list
        self.assertEqual([(c.args[2], c.kwargs["reply"]) for c in calls], [("明天想吃什么", ""), ("", HER_REAL)])
        self.assertLessEqual(calls[0].kwargs["now"], calls[1].kwargs["now"])  # the line keeps the time it was said

    def test_17c_held_lines_are_let_go_when_no_reply_will_come(self) -> None:
        sp = owner_speaker("房主")
        self.d.converse("private:sora", "第一句", sp, listen_only=True, observe_pending=True)
        self.d.converse("private:sora", "第二句", sp, listen_only=True, observe_pending=True)
        self.assertEqual(self.observe.call_args_list, [])
        self.assertEqual(self.d.release_observed_turn("private:sora"), 2)
        self.assertEqual([(c.args[2], c.kwargs["reply"]) for c in self.observe.call_args_list], [("第一句", ""), ("第二句", "")])
        self.assertEqual(self.d.release_observed_turn("private:sora"), 0)  # nothing twice
        # without observe_pending (observe off, or a live channel) the line goes in at once, as before
        self.d.converse("private:sora", "第三句", sp, listen_only=True)
        self.assertEqual(self.observe.call_args_list[-1].args[2], "第三句")

    def test_17d_a_line_held_too_long_goes_in_alone_and_a_reset_drops_held_lines(self) -> None:
        import ai_house.daemon as dmod
        sp = owner_speaker("房主")
        self.d.converse("private:sora", "很久以前", sp, listen_only=True, observe_pending=True)
        with patch.object(dmod, "OBSERVE_HOLD_MAX_S", -1.0):  # every held line is now too old
            self.d.converse("group", "别处的一句", sp, listen_only=True, observe_pending=True)
        self.assertIn("很久以前", [c.args[2] for c in self.observe.call_args_list])
        self.d._observe_held["private:sora"] = [{"actor": "sora", "scope": "private", "text": "x", "channel": "",
                                                 "speaker": "房主", "speaker_role": "owner", "speaker_id": "", "now": 0.0}]
        self.d.reset_house()
        self.assertEqual(self.d._observe_held, {})


# ======================================================================================================================
class PromiseEndToEnd(unittest.TestCase):
    """The reason for all this: an owner request and her real agreement register an accepted promise."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = HouseDaemon(tmp.name, pack_dir=None, env={"INTENTS": "1", "AI_HOUSE_OWNER_TOKEN": "tail1-owner-token"},
                             start_ts=T0_DEFAULT, virtual_clock=True, allow_onboarding=False)
        self.addCleanup(self.d.close)
        self.srv = _HistoryServer()
        self.addCleanup(self.srv.close)
        self.aid = self.d.vocab.ROSTER[0]
        self.assertEqual(self.aid, "nell")
        reply = patch.object(self.d.chat_agent, "reply_as", side_effect=lambda aid, text, *a, **kw: {
            "actor": aid, "name": "奈尔", "text": DRAFT, "context_badge": "", "mode": "live_api"})
        reply.start()
        self.addCleanup(reply.stop)

    def run_turn(self, history: Any, *, wait: bool = True, window_s: float = 1.0) -> OneBotChannel:
        t = time.time()
        self.srv.script["get_friend_msg_history"] = lambda params, n: history(t)
        cfg = OneBotConfig(url=self.srv.url, owners=frozenset({"8001"}), groups=frozenset(), actor="nell", min_interval_s=0.0,
                           send_retries=0, send_timeout_s=3.0, listen_only=True, listen_only_observe=True,
                           observe_window_s=window_s, observe_poll_s=0.1)
        ch = OneBotChannel(cfg)
        ch.start(self.d)
        self.addCleanup(ch.stop)
        self.assertTrue(_wait_until(lambda: ch._ws is not None, 5.0))
        self.assertEqual(ch.handle_event(_dm("m-promise", t, OWNER_ASK)), "listen_only")
        if wait:
            self.wait_observed(ch)
        return ch

    def wait_observed(self, ch: OneBotChannel) -> None:
        self.assertTrue(_wait_until(lambda: any(r["path"] == "observe" for r in ch.listen_only_rows(200)), 8.0))
        self.assertEqual(self.srv.sends(), [])  # QQ received zero send actions

    def statuses(self) -> List[str]:
        op = {"op": "intent", "text": "奈尔答应房主：明天帮房主买一盒红茶回来", "kind": "promise", "for": ["房主"],
              "agreed": ["奈尔"], "scope": "world", "when": "vague", "quote": OWNER_QUOTE}
        self.d.memory.extract(force=True, now=T0_DEFAULT + 5, llm_fn=fake_model([op]))
        with contextlib.closing(sqlite3.connect(self.d.world_db)) as c:
            return [r[0] for r in c.execute("select status from intents")]

    def test_18_her_real_agreement_registers_an_accepted_promise(self) -> None:
        ch = self.run_turn(lambda t: [_msg("s1", SELF, t + 1, _seg(HER_REAL))])
        rows = self.d.list_chat("private:nell")
        self.assertEqual([r["mode"] for r in rows if r["text"] == DRAFT], ["shadow"])
        self.assertEqual([r["text"] for r in rows if r["mode"] == "observed"], [HER_REAL])
        self.assertEqual(self.statuses(), ["accepted"])
        self.assertEqual(ch.stats["observed"], 1)

    def test_19_without_an_observed_reply_nothing_is_accepted_as_before(self) -> None:
        self.run_turn(lambda t: [])
        self.assertNotIn("accepted", self.statuses())

    def test_20_an_extraction_while_the_real_reply_is_still_coming_does_not_lose_the_promise(self) -> None:
        # 2026-11-26 parallel run: the other program answered about two minutes after the owner; memory had already
        # extracted the owner's request alone (owner_quiet, 90 s) and then her answer alone: no promise. The request now
        # waits for the answer, so whatever extraction runs in between, the two are read together.
        came = threading.Event()
        ch = self.run_turn(lambda t: [_msg("s1", SELF, t + 1, _seg(HER_REAL))] if came.is_set() else [],
                           wait=False, window_s=8.0)
        self.assertEqual(self.statuses(), [])  # an extraction during the wait: nothing to read yet, nothing lost
        came.set()
        self.wait_observed(ch)
        self.assertEqual(self.statuses(), ["accepted"])
        self.assertEqual(ch.stats["observed"], 1)


if __name__ == "__main__":
    unittest.main()
