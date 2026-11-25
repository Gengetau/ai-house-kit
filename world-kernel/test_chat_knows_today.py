"""A chat turn knows what she already did today and which promises the kernel already closed.

Defect (live run): she bought the tea at 10:25, the promise closed, and at 11:00 she told the owner "not bought yet" and
registered the same promise a second time. No network, no real model. Every test says what would make it fail.
"""
import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_fakes import FakeHub  # noqa: E402
from test_m2_intents_adapter import Dir, OWNER_LINE, T0, egg_ops, fake_model, rows  # noqa: E402

from ai_house.agent.claude_engine import ClaudeCodeEngine  # noqa: E402
from ai_house.agent.reserved import AntigravityEngine, CodexEngine  # noqa: E402
from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, JST  # noqa: E402

BLOCK = "你今天已经做过的事"
DONE = "已经办完了"
PRIVATE_TEXT = "奈尔买2个鸡蛋"
RULE = "只看“你今天已经做过的事”"
KEY = "sk-ant-" + "a" * 40


def restate(d, her_reply, owner_line="我早上托你带的东西，后来怎么样了？", quote=None, now=None, text=PRIVATE_TEXT):
    """The owner asks again in private, she answers `her_reply`, and one forced memory extraction runs with a fake model
    whose only op is the promise `text` quoting `quote` (default: her own line)."""
    now = now or d.clock.now()
    d.memory.observe("nell", "private", owner_line, speaker_role="owner", speaker="房主", reply=her_reply, now=now)
    op = {"op": "intent", "text": text, "kind": "promise", "for": ["房主"], "agreed": ["奈尔"], "scope": "world",
          "when": "vague", "quote": quote or her_reply}
    d.memory.extract(force=True, now=now + 2, llm_fn=lambda system, user: {"ops": [op]})


class Base(Dir):
    def house(self, env=None, errand=True):
        d = self.daemon(env={"INTENTS": "1", **(env or {})})
        if errand:
            d.memory.observe("nell", "private", OWNER_LINE, speaker_role="owner", speaker="房主", reply="好的", now=T0)
            d.memory.extract(force=True, now=T0 + 5, llm_fn=fake_model(egg_ops("奈尔")))
        return d

    def finish(self, d):
        for _ in range(12):
            d.fast_forward(240)
            if d.intents.list_intents(actor="nell")[0]["status"] == "completed":
                return
        self.fail("the errand never completed")

    def ctx(self, d, channel, actor="nell"):
        seen, real = [], d.chat_agent.reply_as

        def spy(*a, **kw):
            seen.append(kw.get("memory_context") or "")
            return real(*a, **kw)

        d.chat_agent.reply_as = spy
        d.converse(channel, "我早上托你带的东西，后来怎么样了？", owner_speaker(), actor=actor)
        self.assertTrue(seen)
        return seen[-1]


class ChatContextTests(Base):
    def test_private_turn_after_completion_has_the_purchase_and_the_done_promise(self):
        """Fails if a private turn after the kernel closed the promise lacks the purchase line or the 已经办完了 line,
        or if either shows up before it was completed."""
        d = self.house()
        before = self.ctx(d, "private:nell")
        self.assertNotIn("买了鸡蛋", before)
        self.assertNotIn(DONE, before)
        self.finish(d)
        after = self.ctx(d, "private:nell")
        self.assertIn(BLOCK, after)
        self.assertIn("买了鸡蛋 ×2", after)
        self.assertIn(f"答应房主的「{PRIVATE_TEXT}」{DONE}（", after)

    def test_group_turn_has_the_day_facts_but_never_the_private_promise_text(self):
        """Fails if a group prompt lacks her own day record, or leaks the text of a private promise."""
        d = self.house()
        self.finish(d)
        ctx = self.ctx(d, "group")
        self.assertIn(BLOCK, ctx)
        self.assertIn("买了鸡蛋 ×2", ctx)
        self.assertNotIn(PRIVATE_TEXT, ctx)
        self.assertNotIn(DONE, ctx)

    def test_intents_off_gives_no_promise_lines_but_still_the_day_facts(self):
        """Fails if INTENTS=0 still reports finished promises, or if the day facts depend on the promise switch."""
        d = self.daemon(env={"INTENTS": "0"})
        d.fast_forward(240 * 8)
        self.assertEqual(d.intents.done_today_lines("nell", d.clock.now(), True), [])
        ctx = self.ctx(d, "private:nell")
        self.assertNotIn(DONE, ctx)

    def test_fresh_house_has_no_block_and_no_error(self):
        """Fails if a house with no events yet raises, or gets an empty heading."""
        d = self.daemon(env={"INTENTS": "1"})
        ctx = self.ctx(d, "private:nell")
        self.assertNotIn(BLOCK, ctx)
        self.assertNotIn(DONE, ctx)

    def test_done_today_is_limited_to_three_and_to_today(self):
        """Fails if more than 3 lines come back, or a promise completed before the house day began is reported."""
        d = self.house()
        self.finish(d)
        now = d.clock.now()
        self.assertEqual(len(d.intents.done_today_lines("nell", now, True)), 1)
        self.assertEqual(d.intents.done_today_lines("nell", now, False), [])
        self.assertEqual(d.intents.done_today_lines("nell", now + 86400 * 2, True), [])
        import sqlite3
        c = sqlite3.connect(d.world_db)
        with c:
            for k in range(4):
                c.execute("insert into intents(actor,party,agreed,kind,text,status,source,refs,visibility,confidence,"
                          "created,updated,last_action,beneficiary,scope,when_kind) values('nell','[\"nell\"]','[\"nell\"]',"
                          "'promise',?, 'completed','chat','[]','group',0.7,?,?,?,'[\"owner\"]','world','vague')",
                          (f"奈尔买第{k}个苹果", now - 100, now - 50 + k, now - 50))
        c.close()
        self.assertEqual(len(d.intents.done_today_lines("nell", now, True)), 3)
        self.assertEqual(len(d.intents.done_today_lines("nell", now, False)), 3)  # the group ones, never the private

    def test_direct_brain_system_prompt_carries_the_block_and_the_rule(self):
        """Fails if the direct path's system lines drop the block or lack the rule sentence."""
        d = self.house()
        self.finish(d)
        got = []
        d.chat_agent._llm_fn = lambda system, user: (got.append(system), "还没呢")[1]
        d.converse("private:nell", "我早上托你带的东西，后来怎么样了？", owner_speaker())
        self.assertTrue(got)
        self.assertIn(BLOCK, got[-1])
        self.assertIn("买了鸡蛋 ×2", got[-1])
        self.assertIn(f"「{PRIVATE_TEXT}」{DONE}", got[-1])
        self.assertIn(RULE, got[-1])

    def test_agent_brain_prompt_carries_the_block(self):
        """Fails if the agent brain's turn prompt (or its system rules) misses the block or the rule sentence."""
        d = self.house(env={"AI_HOUSE_BRAIN": "claude", "AI_HOUSE_AGENT_API_KEY": KEY})
        h = FakeHub()
        eng = ClaudeCodeEngine(d.env, d.runtime_dir / "agent", client_factory=h.factory, read_timeout_s=5.0)
        h.engine = eng
        d.agent_runtime.set_engines({"claude": eng, "codex": CodexEngine(), "agy": AntigravityEngine()})
        self.finish(d)
        h.script = [h.say("买好了。")]
        d.converse("private:nell", "我早上托你带的东西，后来怎么样了？", owner_speaker())
        self.assertIn(BLOCK, h.prompts[0])
        self.assertIn("买了鸡蛋 ×2", h.prompts[0])
        self.assertIn(DONE, h.prompts[0])
        self.assertIn(RULE, h.clients[0].kwargs["system_prompt"])
        self.assertNotIn("记下答应房主的事", h.clients[0].kwargs["system_prompt"])


class NoDuplicateTests(Base):
    def n(self, d):
        return len(rows(d.world_db, "select id from intents"))

    def test_her_restated_line_after_completion_adds_no_second_promise(self):
        """Fails if the owner's follow-up question and her answer repeating the old errand register it again."""
        d = self.house()
        self.finish(d)
        self.assertEqual(self.n(d), 1)
        restate(d, "我答应买2个鸡蛋，还没去买呢")
        self.assertEqual(self.n(d), 1)

    def test_a_clearly_new_request_after_completion_is_a_second_errand(self):
        """Fails if a genuinely new request ('再买' the same thing) is swallowed by the completed one."""
        d = self.house()
        self.finish(d)
        line = "再帮我买2个鸡蛋吧"
        restate(d, "好的，我再去买2个鸡蛋", owner_line=line, quote=line)
        self.assertEqual(self.n(d), 2)

    def test_before_completion_the_open_promise_still_merges(self):
        """Fails if the existing merge of an open promise stopped working."""
        d = self.house()
        restate(d, "我答应买2个鸡蛋，还没去买呢")
        self.assertEqual(self.n(d), 1)


if __name__ == "__main__":
    unittest.main()
