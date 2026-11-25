"""Agent-backed residents: the behaviours every engine gets from the runtime, driven through the Claude Code
engine with a scripted client (no SDK, no model). One test per row of the table in docs/PORTING-PLAN.md 12.6."""
import itertools
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from agent_fakes import FakeHub

from ai_house.agent import AgentRuntime, EngineError, EngineUnavailable, TurnRequest, brain_for, build_engines
from ai_house.agent import prompts
from ai_house.agent.claude_engine import ClaudeCodeEngine, decide_tool
from ai_house.llm import LLMDeadlineError

ENV = {"AI_HOUSE_AGENT_API_KEY": "k" * 24}
TOOLS = [
    {"name": "inspect_house", "description": "看屋里", "parameters": {"type": "object", "properties": {}}},
    {"name": "check_fridge", "description": "看冰箱", "parameters": {"type": "object", "properties": {"item": {"type": "string"}}}},
]


_RIG_IDS = itertools.count(1)


class Rig:
    def __init__(self, tc: unittest.TestCase, state_dir: str = "", env=None, *, engine_clock: bool = False):
        if not state_dir:
            tmp = tempfile.TemporaryDirectory()
            tc.addCleanup(tmp.cleanup)
            state_dir = tmp.name
        self.state_dir = state_dir
        self.now = [1000.0]
        self.hub = FakeHub()
        self.hub._n = next(_RIG_IDS) * 1000  # a restarted house must not hand out ids an earlier one already used
        self.logs = []
        extra = {"monotonic": lambda: self.now[0]} if engine_clock else {}
        self.engine = ClaudeCodeEngine(env or ENV, state_dir, client_factory=self.hub.factory, read_timeout_s=5.0, **extra)
        self.hub.engine = self.engine
        self.rt = AgentRuntime(
            state_dir, env=env or ENV, engines={"claude": self.engine}, log=self.logs.append, monotonic=lambda: self.now[0]
        )
        tc.addCleanup(self.rt.close)
        self.tool_threads = []
        self.tool_calls = []

    def runner(self, name, args):
        self.tool_threads.append(threading.get_ident())
        self.tool_calls.append((name, args))
        if name == "check_fridge":
            return {"item": args.get("item"), "left": 1}
        return {"weather": "晴"}

    def session(self, actor="nell", channel=""):
        return self.rt._sessions[AgentRuntime.session_key(actor, channel or f"private:{actor}")]

    def stored(self, actor="nell", channel=""):
        doc = json.loads((Path(self.state_dir) / "sessions.json").read_text(encoding="utf-8"))
        return doc["sessions"][AgentRuntime.session_key(actor, channel or f"private:{actor}")]

    def in_thread(self, fn):
        box = {}

        def _run():
            try:
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t, box

    def turn(self, said="房主刚刚说：在吗", *, actor="nell", where="private", channel="", history=(), tools=TOOLS):
        return self.rt.run_turn(
            TurnRequest(
                engine="claude",
                actor=actor,
                channel=channel or (f"private:{actor}" if where == "private" else "group"),
                where=where,
                system=prompts.system_prompt("你是奈尔。", where=where, allow_quiet=where == "group"),
                state_lines=["- 所在位置：客厅"],
                said_line=said,
                history=history,
                tool_specs=tools,
                tool_runner=self.runner,
                allow_quiet=where == "group",
            )
        )


class SpeakingTests(unittest.TestCase):
    def test_reply_is_what_she_sent_and_the_turn_stops_there(self):
        r = Rig(self)
        r.hub.script = [[("tool", "send_message", {"text": "在呢，刚泡好茶。"}), ("text", "这句不该再生成")]]
        res = r.turn()
        self.assertEqual(res.messages, ["在呢，刚泡好茶。"])
        self.assertFalse(res.quiet)
        self.assertTrue(res.fresh_session)
        client = r.hub.clients[0]
        self.assertEqual(client.stopped_by, "hook", "the PostToolUse hook ends the turn once she has spoken")
        self.assertEqual(client.interrupts, 1, "and the engine's own interrupt is issued as well (harmless after the hook)")
        self.assertNotIn(("text", "这句不该再生成"), client.executed)
        self.assertEqual(res.engine, "claude")
        self.assertGreater(res.usage.prompt_tokens, 0)

    def test_when_the_stop_hook_does_not_take_effect_the_interrupt_ends_the_turn(self):
        r = Rig(self)
        r.hub.honor_stop = False
        r.hub.script = [[("tool", "send_message", {"text": "在呢，刚泡好茶。"}), ("text", "这句不该再生成")]]
        res = r.turn()
        self.assertEqual(res.messages, ["在呢，刚泡好茶。"])
        client = r.hub.clients[0]
        self.assertEqual(client.stopped_by, "interrupt", "the fake saw the engine's interrupt, not a timeout")
        self.assertEqual(client.interrupts, 1)
        self.assertNotIn(("text", "这句不该再生成"), client.executed)
        # the interrupted shell's error result is not mistaken for a failed turn
        self.assertEqual(r.rt.status()["live_clients"], 1)

    def test_two_messages_when_she_says_more_is_coming(self):
        r = Rig(self)
        r.hub.script = [[
            ("tool", "send_message", {"text": "等一下。", "end_turn": False}),
            ("tool", "send_message", {"text": "好了，我来了。"}),
        ]]
        res = r.turn()
        self.assertEqual(res.messages, ["等一下。", "好了，我来了。"])

    def test_house_tools_run_on_the_callers_thread_and_their_results_are_collected(self):
        """Fails if a house tool runs on another thread than the caller's, a call is dropped or run twice, the tool
        runner's results do not reach the turn (count and what she sees), or the reply after the tools is lost.
        (The `record_promise` tool and `recorded_intents` are gone; the thread and counting guarantees stay.)"""
        r = Rig(self)
        r.hub.script = [[
            ("tool", "inspect_house", {}),
            ("tool", "check_fridge", {"item": "milk"}),
            ("tool", "send_message", {"text": "冰箱里还有一盒牛奶。"}),
        ]]
        res = r.turn()
        self.assertEqual([c[0] for c in r.tool_calls], ["inspect_house", "check_fridge"])
        self.assertEqual(r.tool_calls[1][1], {"item": "milk"})
        self.assertEqual(set(r.tool_threads), {threading.get_ident()})
        self.assertEqual(r.hub.tool_results[1][1], {"item": "milk", "left": 1})
        self.assertEqual(res.tool_calls, 2)
        self.assertEqual(res.messages, ["冰箱里还有一盒牛奶。"])
        self.assertFalse(hasattr(res, "recorded_intents"))

    def test_stay_quiet_in_a_group(self):
        r = Rig(self)
        r.hub.script = [[("tool", "stay_quiet", {"reason": "不是在问我"})]]
        res = r.turn(where="group")
        self.assertTrue(res.quiet)
        self.assertEqual(res.messages, [])
        self.assertEqual(res.quiet_reason, "不是在问我")

    def test_stay_quiet_is_not_offered_or_honoured_in_a_private_chat(self):
        r = Rig(self)
        r.hub.script = [[("tool", "stay_quiet", {}), ("tool", "send_message", {"text": "嗯，我在。"})]]
        res = r.turn(where="private")
        self.assertEqual(res.messages, ["嗯，我在。"])
        self.assertFalse(res.quiet)
        self.assertNotIn("stay_quiet", r.hub.clients[0].tool_names)
        self.assertIn("denied", r.hub.tool_results[0][1])

    def test_a_tool_that_raises_is_a_result_she_can_read_not_a_crash(self):
        r = Rig(self)
        r.runner = lambda name, args: (_ for _ in ()).throw(RuntimeError("boom"))
        r.hub.script = [[("tool", "inspect_house", {}), ("tool", "send_message", {"text": "看不了，晚点再说。"})]]
        res = r.rt.run_turn(TurnRequest(
            engine="claude", actor="nell", channel="private:nell", where="private", system="s", state_lines=["x"],
            said_line="房主刚刚说：看看家里", tool_specs=TOOLS, tool_runner=r.runner,
        ))
        self.assertEqual(res.messages, ["看不了，晚点再说。"])
        self.assertIn("RuntimeError", r.hub.tool_results[0][1]["error"])


class ForgotToSendTests(unittest.TestCase):
    def test_reminded_once_in_the_same_turn(self):
        r = Rig(self)
        r.hub.script = [[("text", "在呢，怎么啦？")], r.hub.say("在呢，怎么啦？")]
        res = r.turn()
        self.assertTrue(res.nudged)
        self.assertFalse(res.autosent)
        self.assertEqual(res.messages, ["在呢，怎么啦？"])
        self.assertEqual(r.hub.prompts[1], prompts.TEXT_ONLY_NUDGE)

    def test_private_chat_sends_her_text_for_her_and_reminds_next_turn(self):
        r = Rig(self)
        r.hub.script = [[("text", "第一稿")], [("text", "我在的。")], r.hub.say("好。")]
        res = r.turn()
        self.assertTrue(res.nudged and res.autosent)
        self.assertEqual(res.messages, ["我在的。"])
        r.turn("房主刚刚说：那就好")
        self.assertIn(prompts.UNSENT_REMINDER, r.hub.prompts[2])
        r.hub.script = [r.hub.say("嗯。")]
        r.turn("房主刚刚说：晚安")
        self.assertNotIn(prompts.UNSENT_REMINDER, r.hub.prompts[3], "the reminder is given once, not forever")

    def test_group_chat_stays_silent_instead_of_sending_unsent_text(self):
        r = Rig(self)
        r.hub.script = [[("text", "我觉得可以")], [("text", "嗯嗯")]]
        res = r.turn(where="group")
        self.assertEqual(res.messages, [])
        self.assertTrue(res.quiet)
        self.assertFalse(res.autosent)
        self.assertEqual(r.hub.prompts[1], prompts.TEXT_ONLY_NUDGE_GROUP)

    def test_three_turns_in_a_row_gets_a_fresh_session_with_a_note(self):
        r = Rig(self)
        r.hub.script = [[("text", "a")], [("text", "a2")], [("text", "b")], [("text", "b2")], [("text", "c")], [("text", "c2")], r.hub.say("这次发出来了")]
        r.turn()
        r.turn()
        third = r.turn()
        self.assertEqual(third.rotated, "unsent")
        self.assertTrue(r.hub.clients[0].disconnected)
        res = r.turn("房主刚刚说：还在吗")
        self.assertTrue(res.fresh_session)
        self.assertEqual(len(r.hub.clients), 2)
        self.assertNotIn("resume", r.hub.clients[1].kwargs)
        self.assertIn(prompts.RESET_NOTE, r.hub.prompts[-1])
        self.assertIn(prompts.FRESH_SESSION_NOTE, r.hub.prompts[-1])
        self.assertEqual(res.messages, ["这次发出来了"])

    def test_private_chat_with_no_output_at_all_is_an_error_not_an_empty_reply(self):
        r = Rig(self)
        r.hub.script = [[]]
        with self.assertRaises(EngineError):
            r.turn()


class SessionTests(unittest.TestCase):
    def test_session_id_survives_a_restart_and_is_resumed(self):
        with tempfile.TemporaryDirectory() as d:
            a = Rig(self, d)
            a.hub.script = [a.hub.say("第一次")]
            a.turn()
            sid = a.hub.clients[0].session_id
            a.rt.close()
            b = Rig(self, d)
            b.hub.script = [b.hub.say("第二次")]
            res = b.turn("房主刚刚说：还记得吗", history=[("房主: 在吗", False), ("奈尔: 第一次", True), ("房主: 群里有人说话了", False)])
            self.assertFalse(res.fresh_session)
            self.assertEqual(b.hub.clients[0].kwargs.get("resume"), sid)
            prompt = b.hub.prompts[0]
            self.assertNotIn(prompts.FRESH_SESSION_NOTE, prompt)
            self.assertIn("房主: 群里有人说话了", prompt)
            self.assertNotIn("奈尔: 第一次", prompt, "what she already knows from the session is not repeated")

    def test_fresh_session_says_so_and_lists_recent_lines(self):
        r = Rig(self)
        r.hub.script = [r.hub.say("嗯")]
        r.turn(history=[("房主: 早", False), ("奈尔: 早呀", True)])
        self.assertIn(prompts.FRESH_SESSION_NOTE, r.hub.prompts[0])
        self.assertIn("奈尔: 早呀", r.hub.prompts[0])

    def test_unresumable_session_is_replaced_by_a_fresh_one(self):
        with tempfile.TemporaryDirectory() as d:
            a = Rig(self, d)
            a.hub.script = [a.hub.say("第一次")]
            a.turn()
            sid = a.hub.clients[0].session_id
            a.rt.close()
            b = Rig(self, d)
            b.hub.unresumable.add(sid)
            b.hub.script = [b.hub.say("重新开始")]
            res = b.turn()
            self.assertTrue(res.fresh_session)
            self.assertEqual(res.messages, ["重新开始"])
            self.assertEqual(b.hub.clients[0].kwargs.get("resume"), sid)
            self.assertNotIn("resume", b.hub.clients[1].kwargs)
            self.assertIn(prompts.FRESH_SESSION_NOTE, b.hub.prompts[-1])
            self.assertEqual(b.stored()["session_id"], b.hub.clients[1].session_id)
            self.assertEqual(b.stored()["resume_failures"], 0)

    def _stored_session(self, d):
        """A house that has had one turn and is then restarted: (state dir is `d`, the stored session id)."""
        a = Rig(self, d)
        a.hub.script = [a.hub.say("第一次")]
        a.turn()
        sid = a.hub.clients[0].session_id
        a.rt.close()
        return sid

    def test_process_dying_on_the_first_resumed_turn_keeps_the_session_and_resumes_it_next_time(self):
        # (was: "...also_falls_back_to_fresh": one dead process is not proof that the conversation is lost)
        with tempfile.TemporaryDirectory() as d:
            sid = self._stored_session(d)
            b = Rig(self, d)
            b.hub.script = [[("die",)], b.hub.say("回来了")]
            with self.assertRaises(EngineError) as cm:
                b.turn()
            self.assertNotIsInstance(cm.exception, EngineUnavailable)
            self.assertEqual(b.stored()["session_id"], sid, "the session id is not wiped by one failure")
            self.assertEqual(b.stored()["resume_failures"], 1)
            res = b.turn()
            self.assertFalse(res.fresh_session)
            self.assertEqual(b.hub.clients[-1].kwargs.get("resume"), sid)
            self.assertEqual(res.messages, ["回来了"])
            self.assertEqual(b.stored()["resume_failures"], 0, "a good turn clears the count")

    def test_a_third_turn_after_two_failures_in_a_row_starts_a_fresh_session(self):
        with tempfile.TemporaryDirectory() as d:
            sid = self._stored_session(d)
            b = Rig(self, d)
            b.hub.script = [[("die",)], [("die",)], b.hub.say("新的开始")]
            for _ in range(2):
                with self.assertRaises(EngineError):
                    b.turn()
            self.assertEqual(b.stored()["session_id"], sid)
            self.assertEqual(b.stored()["resume_failures"], 2)
            res = b.turn()
            self.assertTrue(res.fresh_session)
            self.assertEqual(res.messages, ["新的开始"])
            self.assertNotIn("resume", b.hub.clients[-1].kwargs)
            self.assertIn(prompts.FRESH_SESSION_NOTE, b.hub.prompts[-1])
            self.assertNotEqual(b.stored()["session_id"], sid)
            self.assertEqual(b.stored()["session_id"], b.hub.clients[-1].session_id)
            self.assertEqual(b.stored()["resume_failures"], 0)

    def test_the_failure_count_is_stored_so_a_restart_does_not_forget_it(self):
        with tempfile.TemporaryDirectory() as d:
            sid = self._stored_session(d)
            b = Rig(self, d)
            b.hub.script = [[("die",)], [("die",)]]
            for _ in range(2):
                with self.assertRaises(EngineError):
                    b.turn()
            b.rt.close()
            c = Rig(self, d)
            c.hub.script = [c.hub.say("换新的了")]
            res = c.turn()
            self.assertTrue(res.fresh_session)
            self.assertNotIn("resume", c.hub.clients[0].kwargs)
            self.assertNotEqual(c.stored()["session_id"], sid)

    def test_a_good_turn_in_between_resets_the_count(self):
        with tempfile.TemporaryDirectory() as d:
            sid = self._stored_session(d)
            b = Rig(self, d)
            b.hub.script = [[("die",)], b.hub.say("一"), [("die",)], b.hub.say("二")]
            for expect_ok in (False, True, False, True):
                if expect_ok:
                    self.assertFalse(b.turn().fresh_session)
                else:
                    with self.assertRaises(EngineError):
                        b.turn()
            self.assertEqual(b.stored()["session_id"], sid)
            self.assertEqual(b.hub.clients[-1].kwargs.get("resume"), sid)

    def test_a_connection_that_fails_for_another_reason_keeps_the_session(self):
        with tempfile.TemporaryDirectory() as d:
            sid = self._stored_session(d)
            b = Rig(self, d)
            b.hub.connect_errors.append(TimeoutError("connect timed out"))
            b.hub.script = [b.hub.say("网络好了")]
            with self.assertRaises(EngineError) as cm:
                b.turn()
            self.assertNotIsInstance(cm.exception, EngineUnavailable)
            self.assertEqual(b.stored()["session_id"], sid)
            res = b.turn()
            self.assertFalse(res.fresh_session)
            self.assertEqual(b.hub.clients[-1].kwargs.get("resume"), sid)

    def test_an_error_report_from_the_shell_is_not_counted_against_the_session(self):
        with tempfile.TemporaryDirectory() as d:
            sid = self._stored_session(d)
            b = Rig(self, d)
            b.hub.script = [[("error", "Invalid API key · Please run /login")]] * 3 + [b.hub.say("好了")]
            for _ in range(3):
                with self.assertRaises(EngineError):
                    b.turn()
            self.assertEqual(b.stored()["session_id"], sid)
            self.assertEqual(b.stored()["resume_failures"], 0, "a bad key says nothing about whether the session is usable")
            res = b.turn()
            self.assertFalse(res.fresh_session)

    def test_an_empty_session_id_is_not_stored_before_the_new_session_works(self):
        with tempfile.TemporaryDirectory() as d:
            sid = self._stored_session(d)
            b = Rig(self, d)
            b.hub.unresumable.add(sid)
            # the second connect (the fresh session's) fails too
            b.hub.on_connect = lambda: b.hub.connect_errors.append(RuntimeError("boom")) if b.hub.connects_started == 2 else None
            b.hub.script = [b.hub.say("总算好了")]
            with self.assertRaises(EngineError):
                b.turn()
            self.assertEqual(b.stored()["session_id"], sid, "the old id stays on disk until a replacement has worked")
            res = b.turn()
            self.assertTrue(res.fresh_session)
            self.assertEqual(res.messages, ["总算好了"])
            self.assertEqual(b.stored()["session_id"], b.hub.clients[-1].session_id)
            self.assertNotEqual(b.stored()["session_id"], sid)

    def test_private_and_group_are_separate_sessions(self):
        r = Rig(self)
        r.hub.script = [r.hub.say("私聊回复"), r.hub.say("群里回复")]
        r.turn(where="private")
        r.turn(where="group")
        self.assertEqual(len(r.hub.clients), 2)
        self.assertNotEqual(r.hub.clients[0].session_id, r.hub.clients[1].session_id)
        self.assertIn("私聊", r.hub.clients[0].kwargs["system_prompt"])
        self.assertIn("群聊", r.hub.clients[1].kwargs["system_prompt"])

    def test_changed_tools_rebuild_the_client_but_keep_the_session(self):
        r = Rig(self)
        r.hub.script = [r.hub.say("一"), r.hub.say("二")]
        r.turn()
        sid = r.hub.clients[0].session_id
        r.turn(tools=[*TOOLS, {"name": "send_sticker", "description": "贴纸", "parameters": {"type": "object", "properties": {}}}])
        self.assertEqual(len(r.hub.clients), 2)
        self.assertTrue(r.hub.clients[0].disconnected)
        self.assertEqual(r.hub.clients[1].kwargs.get("resume"), sid)
        self.assertIn("send_sticker", r.hub.clients[1].tool_names)

    def test_too_long_a_conversation_is_rotated_with_a_note(self):
        r = Rig(self)
        r.rt.session_max_tokens = 3000
        r.hub.context_tokens = 5000
        r.hub.script = [r.hub.say("长了"), r.hub.say("新的")]
        first = r.turn()
        self.assertEqual(first.rotated, "too_long")
        self.assertEqual(first.messages, ["长了"], "the reply that crossed the cap is still delivered")
        r.hub.context_tokens = 100
        res = r.turn("房主刚刚说：继续")
        self.assertTrue(res.fresh_session)
        self.assertIn(prompts.ROTATED_NOTE, r.hub.prompts[-1])
        r.hub.script = [r.hub.say("第三轮")]
        r.turn("房主刚刚说：再来")
        self.assertNotIn(prompts.ROTATED_NOTE, r.hub.prompts[-1])

    def test_rotating_for_length_keeps_the_reminder_set_in_the_same_turn(self):
        r = Rig(self)
        r.rt.session_max_tokens = 3000
        r.hub.context_tokens = 5000
        r.hub.script = [[("text", "我觉得可以")], [("text", "嗯嗯")], r.hub.say("这回发出来了")]
        first = r.turn(where="group")  # wrote text twice, did not send: a reminder is due
        self.assertEqual(first.rotated, "too_long")
        self.assertTrue(r.session("nell", "group").pending_reminder, "the reminder survives the rotation")
        self.assertTrue(r.stored("nell", "group")["pending_reminder"])
        r.hub.context_tokens = 100
        res = r.turn("房主刚刚说：在吗", where="group")
        self.assertTrue(res.fresh_session)
        self.assertIn(prompts.UNSENT_REMINDER, r.hub.prompts[-1])
        self.assertIn(prompts.ROTATED_NOTE, r.hub.prompts[-1])

    def test_held_back_reply_is_mentioned_at_the_next_turn(self):
        r = Rig(self)
        r.hub.script = [r.hub.say("（含密钥的话）"), r.hub.say("好的")]
        r.turn()
        r.rt.note_blocked("nell", "private:nell")
        r.turn("房主刚刚说：嗯？")
        self.assertIn(prompts.BLOCKED_NOTE, r.hub.prompts[1])

    def test_forget_all_drops_clients_and_ids(self):
        r = Rig(self)
        r.hub.script = [r.hub.say("一"), r.hub.say("二")]
        r.turn()
        r.rt.forget_all()
        self.assertTrue(r.hub.clients[0].disconnected)
        self.assertEqual(r.rt.status()["sessions"], 0)
        res = r.turn()
        self.assertTrue(res.fresh_session)
        self.assertNotIn("resume", r.hub.clients[1].kwargs)


    def test_forget_all_deletes_the_conversation_files_after_the_clients_are_closed(self):
        r = Rig(self)
        r.hub.script = [r.hub.say("私聊里说的事")]
        r.turn()
        state = Path(r.state_dir)
        projects = state / "claude-config" / "projects" / "-some-cwd"
        projects.mkdir(parents=True)
        (projects / "abc.jsonl").write_text('{"text": "房主私聊"}', encoding="utf-8")
        keep = state / "claude-config" / "settings.json"
        keep.write_text("{}", encoding="utf-8")
        (state / "work" / "nell").mkdir(parents=True, exist_ok=True)
        (state / "work" / "nell" / "scratch.txt").write_text("x", encoding="utf-8")
        seen = []
        client = r.hub.clients[0]
        orig = client.disconnect

        async def watched():
            seen.append((state / "claude-config" / "projects").exists())
            await orig()

        client.disconnect = watched
        r.rt.forget_all()
        self.assertEqual(seen, [True], "the files are still there when the client is closed, and removed after")
        self.assertFalse((state / "claude-config" / "projects").exists())
        self.assertFalse((state / "work").exists())
        self.assertTrue(keep.exists(), "only the conversations go, not the shell's other settings")

    def test_forget_all_logs_and_goes_on_when_a_file_cannot_be_deleted(self):
        r = Rig(self)
        r.hub.script = [r.hub.say("嗯")]
        r.turn()
        (Path(r.state_dir) / "work" / "nell").mkdir(parents=True, exist_ok=True)
        real = shutil.rmtree

        def boom(path, *a, **kw):
            raise PermissionError("denied")

        shutil.rmtree = boom
        try:
            r.rt.forget_all()  # must not raise
        finally:
            shutil.rmtree = real
        self.assertEqual(r.rt.status()["sessions"], 0)
        self.assertTrue(any("not deleted" in ln for ln in r.logs), r.logs)

    def test_forget_all_with_nothing_stored_writes_nothing(self):
        r = Rig(self)
        r.rt.forget_all()
        self.assertFalse((Path(r.state_dir) / "sessions.json").exists())

    def test_a_client_that_finishes_connecting_after_a_reset_is_thrown_away_and_the_turn_fails(self):
        import threading as th

        r = Rig(self)
        r.hub.connect_gate = th.Event()
        r.hub.script = [r.hub.say("这句不该发出去")]
        t, box = r.in_thread(r.turn)
        self.assertTrue(r.hub.connecting.wait(5), "the turn is stuck in connect()")
        r.rt.forget_all()
        r.hub.connect_gate.set()
        t.join(10)
        self.assertIsInstance(box.get("error"), EngineError, box)
        client = r.hub.clients[0]
        for _ in range(100):
            if client.disconnected:
                break
            time.sleep(0.02)
        self.assertTrue(client.disconnected, "nobody is left to close it later, so it is closed at once")
        self.assertEqual(client.prompts, [], "the old conversation's context was never sent to the model")
        self.assertEqual(r.rt.status()["live_clients"], 0)
        self.assertEqual(r.rt.status()["sessions"], 0)
        self.assertEqual(r.rt._stored, {}, "the forgotten session is not brought back")
        res = r.turn()
        self.assertTrue(res.fresh_session)
        self.assertEqual(len(r.hub.clients), 2)


class ResourceTests(unittest.TestCase):
    def test_idle_client_is_dropped_and_the_next_turn_resumes(self):
        r = Rig(self)
        r.hub.script = [r.hub.say("一"), r.hub.say("二")]
        r.turn()
        sid = r.hub.clients[0].session_id
        import asyncio

        self.assertEqual(asyncio.run_coroutine_threadsafe(r.rt.reap_idle(), r.rt._loop).result(5), 0)
        r.now[0] += r.rt.idle_drop_s + 1
        self.assertEqual(asyncio.run_coroutine_threadsafe(r.rt.reap_idle(), r.rt._loop).result(5), 1)
        self.assertTrue(r.hub.clients[0].disconnected)
        self.assertEqual(r.rt.status()["live_clients"], 0)
        res = r.turn()
        self.assertFalse(res.fresh_session)
        self.assertEqual(r.hub.clients[1].kwargs.get("resume"), sid)

    def test_live_clients_are_capped(self):
        r = Rig(self)
        r.rt.max_clients = 1
        r.hub.script = [r.hub.say("a"), r.hub.say("b")]
        r.turn(actor="nell")
        r.now[0] += 5
        r.turn(actor="sora")
        self.assertTrue(r.hub.clients[0].disconnected)
        self.assertFalse(r.hub.clients[1].disconnected)
        self.assertEqual(r.rt.status()["live_clients"], 1)

    def test_a_turn_that_never_ends_hits_the_deadline_and_its_client_is_dropped(self):
        r = Rig(self)
        r.rt.turn_budget_s = 0.3
        r.hub.script = [[("hang",)], r.hub.say("恢复了")]
        t0 = time.monotonic()
        with self.assertRaises(LLMDeadlineError):
            r.turn()
        self.assertLess(time.monotonic() - t0, 3.0)
        for _ in range(100):
            if r.hub.clients[0].disconnected:
                break
            time.sleep(0.02)
        self.assertTrue(r.hub.clients[0].disconnected)
        self.assertEqual(r.hub.clients[0].interrupts, 1, "a shell dropped mid-turn is told to stop first, so it closes at once")
        r.rt.turn_budget_s = 10
        self.assertEqual(r.turn().messages, ["恢复了"])
        before = r.hub.clients[1].interrupts
        r.rt.close()
        self.assertTrue(r.hub.clients[1].disconnected)
        self.assertEqual(r.hub.clients[1].interrupts, before, "an idle shell is simply closed")

    def test_deadline_after_she_spoke_keeps_the_reply(self):
        r = Rig(self)
        r.rt.turn_budget_s = 0.3
        r.hub.script = [[("tool", "send_message", {"text": "先回你一句。", "end_turn": False}), ("hang",)]]
        res = r.turn()
        self.assertEqual(res.messages, ["先回你一句。"])
        self.assertTrue(res.partial)

    def test_engine_failure_after_she_spoke_keeps_the_reply_and_the_session(self):
        r = Rig(self)
        r.hub.script = [
            r.hub.say("一"),
            [("tool", "send_message", {"text": "先回你一句。", "end_turn": False}), ("die",)],
            r.hub.say("接着说"),
        ]
        r.turn()
        sid = r.session().session_id
        res = r.turn("房主刚刚说：再来")
        self.assertEqual(res.messages, ["先回你一句。"])
        self.assertTrue(res.partial)
        self.assertTrue(r.hub.clients[0].disconnected, "the client that died is dropped")
        self.assertEqual(r.stored()["session_id"], sid, "the session id is kept")
        res = r.turn("房主刚刚说：然后呢")
        self.assertEqual(res.messages, ["接着说"])
        self.assertFalse(res.fresh_session)
        self.assertEqual(r.hub.clients[1].kwargs.get("resume"), sid)

    def test_engine_failure_before_she_spoke_is_still_an_error(self):
        r = Rig(self)
        r.hub.script = [[("die",)]]
        with self.assertRaises(EngineError):
            r.turn()

    def test_a_turn_queued_behind_one_that_timed_out_gets_a_new_client_not_the_half_dead_one(self):
        r = Rig(self)
        r.rt.turn_budget_s = 0.4
        r.hub.script = [[("hang",)], r.hub.say("B 自己的回复")]
        ta, box_a = r.in_thread(r.turn)
        for _ in range(250):
            if r.hub.prompts:
                break
            time.sleep(0.02)
        self.assertEqual(len(r.hub.prompts), 1, "A is running (and hanging)")
        r.rt.turn_budget_s = 10.0
        tb, box_b = r.in_thread(lambda: r.turn("房主刚刚说：B 来了"))  # same conversation, queued on its lock
        ta.join(10)
        tb.join(10)
        self.assertIsInstance(box_a.get("error"), LLMDeadlineError, box_a)
        self.assertEqual(box_b.get("error"), None, box_b)
        self.assertEqual(box_b["result"].messages, ["B 自己的回复"])
        self.assertEqual(len(r.hub.clients), 2, "B did not reuse the client A gave up on")
        self.assertTrue(r.hub.clients[0].disconnected)
        self.assertEqual(r.hub.clients[0].prompts, [r.hub.prompts[0]], "the old client saw only A's prompt")
        self.assertIn("B 来了", r.hub.clients[1].prompts[0])
        time.sleep(0.3)  # a late clean-up of A's turn must not close the client B is now using
        self.assertFalse(r.hub.clients[1].disconnected)

    def test_enclosing_deadline_is_respected(self):
        from ai_house.llm import deadline_scope

        r = Rig(self)
        r.hub.script = [[("hang",)]]
        t0 = time.monotonic()
        with deadline_scope(0.3), self.assertRaises(LLMDeadlineError):
            r.turn()
        self.assertLess(time.monotonic() - t0, 3.0)


class EngineTests(unittest.TestCase):
    def test_late_result_of_an_earlier_turn_is_skipped(self):
        r = Rig(self)
        r.hub.script = [[("stale_result",), ("tool", "send_message", {"text": "这才是这一轮的"})]]
        res = r.turn()
        self.assertEqual(res.messages, ["这才是这一轮的"])
        sess = r.rt._sessions["nell|private:nell"]
        self.assertEqual(sess.session_id, r.hub.clients[0].session_id, "the stale result's session id is not adopted")

    def test_stale_results_are_skipped_only_up_to_the_cap(self):
        from ai_house.agent.claude_engine import MAX_STALE_RESULTS

        r = Rig(self)
        r.hub.script = [[("stale_result",)] * MAX_STALE_RESULTS + [("tool", "send_message", {"text": "这才是这一轮的"})]]
        self.assertEqual(r.turn().messages, ["这才是这一轮的"], "up to the cap every late result is skipped")
        self.assertEqual(r.session().session_id, r.hub.clients[0].session_id)
        # one more than the cap: the next one is taken as this turn's result, so the loop cannot wait for ever
        r2 = Rig(self)
        r2.hub.script = [[("stale_result",)] * (MAX_STALE_RESULTS + 1)]
        res = r2.turn(where="group")
        self.assertEqual(res.messages, [])
        self.assertEqual(r2.session("nell", "group").session_id, "stale-session")

    def test_a_result_just_inside_the_slack_is_this_turns_and_just_outside_is_a_late_one(self):
        from ai_house.agent.claude_engine import STALE_SLACK_S

        ms = int(STALE_SLACK_S * 1000)
        inside = Rig(self, engine_clock=True)
        inside.hub.script = [[("result_ms", ms)]]
        inside.turn(where="group")
        self.assertEqual(inside.session("nell", "group").session_id, "late-session", "exactly at the slack: accepted")
        outside = Rig(self, engine_clock=True)
        outside.hub.script = [[("result_ms", ms + 1), ("tool", "stay_quiet", {"reason": "x"})]]
        res = outside.turn(where="group")
        self.assertTrue(res.quiet)
        self.assertEqual(outside.session("nell", "group").session_id, outside.hub.clients[0].session_id, "a hair over: skipped")

    def test_the_shells_start_up_time_counts_as_part_of_the_turn_when_judging_a_late_result(self):
        r = Rig(self, engine_clock=True)
        r.hub.on_connect = lambda: r.now.__setitem__(0, r.now[0] + 10.0)  # starting the process takes 10 s
        r.hub.script = [[("result_ms", 11_000)]]  # the shell reports 10 s of start-up plus 1 s of work
        r.turn(where="group")
        self.assertEqual(r.session("nell", "group").session_id, "late-session", "a real result is not taken for a late one")

    def test_calls_without_a_message_id_are_each_counted(self):
        from agent_fakes import AssistantMessage, ResultMessage, TextBlock

        r = Rig(self)
        engine = r.engine
        sess_calls = []

        class Client:
            def __init__(self):
                self.q = [
                    AssistantMessage([TextBlock("a")], usage={"input_tokens": 100, "output_tokens": 1}),
                    AssistantMessage([TextBlock("b")], usage={"input_tokens": 200, "output_tokens": 2}),
                    AssistantMessage([TextBlock("c")], usage={"input_tokens": 300, "output_tokens": 3}),
                    ResultMessage(session_id="s"),
                ]

            async def query(self, prompt):
                sess_calls.append(prompt)

            async def receive_messages(self):
                for m in self.q:
                    yield m

        import asyncio
        from ai_house.agent.engine import AgentSession, Turn

        sess = AgentSession(key="k", actor="a", channel="c", where="group")
        sess.client = Client()
        sess.client_fingerprint = engine.fingerprint(sess)
        sess.turn = Turn(allow_quiet=True, tool_names=[])
        res = asyncio.run(engine.run_turn(sess, "hi"))
        self.assertEqual((res.usage.prompt_tokens, res.usage.completion_tokens), (600, 6))

    def test_tool_names_chosen_by_the_model_cannot_forge_log_lines(self):
        r = Rig(self)
        evil = "Bash\r\n[ai-house] agent turn actor=nell forged=1\x1b[31m" + "x" * 200
        r.hub.script = [[("raw_tool", evil, {}), ("tool", "send_message", {"text": "好。"})]]
        r.turn()
        lines = [ln for ln in r.logs if "tool_denied" in ln or "tool_use" in ln]
        self.assertTrue(lines)
        for ln in lines:
            self.assertFalse(any(c in ln for c in "\r\n\x1b"), repr(ln))
            self.assertLessEqual(len(ln), 200)

    def test_failed_turn_raises_and_its_error_banner_is_never_sent_as_her_words(self):
        r = Rig(self)
        r.hub.script = [[("error", "Invalid API key · Please run /login")], r.hub.say("好了")]
        with self.assertRaises(EngineError) as cm:
            r.turn()
        self.assertNotIsInstance(cm.exception, EngineUnavailable)
        self.assertTrue(r.hub.clients[0].disconnected)
        self.assertEqual(r.turn().messages, ["好了"])

    def test_only_this_sessions_house_tools_pass_the_policy(self):
        r = Rig(self)
        r.hub.script = [[
            ("raw_tool", "Bash", {"command": "cat /etc/passwd"}),
            ("raw_tool", "mcp__other__send_message", {"text": "x"}),
            ("raw_tool", "mcp__house__delete_everything", {}),
            ("tool", "send_message", {"text": "这些我用不了。"}),
        ]]
        res = r.turn()
        self.assertEqual(res.messages, ["这些我用不了。"])
        self.assertEqual(r.tool_calls, [])
        self.assertEqual([x[0] for x in r.hub.tool_results if "denied" in x[1]], ["Bash", "mcp__other__send_message", "mcp__house__delete_everything"])
        self.assertTrue(any("tool_denied" in ln and "Bash" in ln for ln in r.logs))

    def test_policy_denies_everything_when_no_turn_is_running(self):
        r = Rig(self)
        r.hub.script = [r.hub.say("嗯")]
        r.turn()
        sess = r.rt._sessions["nell|private:nell"]
        self.assertEqual(decide_tool(sess, "mcp__house__send_message")[0], False)

    def test_shell_options_are_locked_down(self):
        r = Rig(self)
        r.hub.script = [r.hub.say("嗯")]
        r.turn()
        kw = r.hub.clients[0].kwargs
        self.assertEqual(kw["tools"], [])
        self.assertIs(kw["strict_mcp_config"], True)
        self.assertEqual(kw["setting_sources"], [])
        self.assertEqual(kw["permission_mode"], "default")
        self.assertNotIn("allowed_tools", kw, "tools are admitted one call at a time by the policy, not pre-approved")
        env = kw["env"]
        self.assertTrue(env["CLAUDE_CONFIG_DIR"].startswith(r.state_dir))
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")
        self.assertEqual(env["DISABLE_TELEMETRY"], "1")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k" * 24)
        self.assertTrue(kw["cwd"].startswith(r.state_dir))
        self.assertTrue(kw["system_prompt"].startswith("你是奈尔。"))

    def test_too_many_tool_calls_or_messages_are_refused(self):
        r = Rig(self)
        r.hub.script = [[("tool", "inspect_house", {})] * 9 + [
            ("tool", "send_message", {"text": "1", "end_turn": False}),
            ("tool", "send_message", {"text": "2", "end_turn": False}),
            ("tool", "send_message", {"text": "3", "end_turn": False}),
            ("tool", "send_message", {"text": "4", "end_turn": False}),
        ]]
        res = r.turn()
        self.assertEqual(len(r.tool_calls), 8)
        self.assertEqual(res.messages, ["1", "2", "3"])

    def test_secrets_never_reach_the_log(self):
        r = Rig(self)
        r.hub.connect_errors.append(RuntimeError("bad key " + "k" * 24))
        with self.assertRaises(EngineError) as cm:
            r.turn()
        self.assertNotIn("k" * 24, str(cm.exception))


class SelectionTests(unittest.TestCase):
    def test_default_is_direct_and_names_are_normalised(self):
        self.assertEqual(brain_for({}), "direct")
        self.assertEqual(brain_for({"AI_HOUSE_BRAIN": "Claude-Code"}), "claude")
        self.assertEqual(brain_for({"AI_HOUSE_BRAIN": "antigravity"}), "agy")
        self.assertEqual(brain_for({"AI_HOUSE_BRAIN": "claude", "AI_HOUSE_ACTOR_BRAIN_NELL": "direct"}, "nell"), "direct")
        self.assertEqual(brain_for({"AI_HOUSE_ACTOR_BRAIN_NELL": "claude"}, "sora"), "direct")
        self.assertEqual(brain_for({"AI_HOUSE_BRAIN": "gpt"}), "gpt", "an unknown name is kept so that it fails visibly")

    def test_reserved_engines_say_they_are_not_implemented(self):
        with tempfile.TemporaryDirectory() as d:
            engines = build_engines(ENV, d)
            self.assertEqual(set(engines), {"claude", "codex", "agy"})
            for name in ("codex", "agy"):
                self.assertIn("还没有实现", engines[name].unavailable_reason())
            rt = AgentRuntime(d, env=ENV)
            self.addCleanup(rt.close)
            with self.assertRaises(EngineUnavailable):
                rt.run_turn(TurnRequest(engine="agy", actor="a", channel="c", where="private", system="s", state_lines=[], said_line="x"))
            with self.assertRaises(EngineUnavailable):
                rt.run_turn(TurnRequest(engine="nope", actor="a", channel="c", where="private", system="s", state_lines=[], said_line="x"))
            self.assertIsNone(rt._loop, "nothing is started for an engine that cannot run")

    def test_claude_engine_needs_a_credential(self):
        with tempfile.TemporaryDirectory() as d:
            eng = ClaudeCodeEngine({}, d, client_factory=lambda k, s: None)
            self.assertIn("密钥", eng.unavailable_reason())
            eng = ClaudeCodeEngine({"ANTHROPIC_API_KEY": "x" * 20}, d, client_factory=lambda k, s: None)
            self.assertEqual(eng.unavailable_reason(), "")


if __name__ == "__main__":
    unittest.main()
