"""A chat turn gets her open promises with the date already worked out by the house.

Defect (live run): asked "which day was the milk for?" (promise due 11-24) she answered "大后天，11月28号" twice, because
in a private chat she saw no open promises and did the date arithmetic herself. No network, no real model. Every test
says what would make it fail."""
import datetime as dt
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_m2_intents_adapter import Dir, T0  # noqa: E402

from ai_house.agent import prompts as agent_prompts  # noqa: E402
from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import JST  # noqa: E402

HEAD = "你答应了、还没办的事（屋里的记录）："
MILK = "奈尔答应房主，11月25日帮他带两盒牛奶回来"
RULE = "不要自己换算"
DAY = 86400.0
MIDNIGHT = dt.datetime(2026, 11, 22, 0, 0, tzinfo=JST).timestamp()


def add(d, text=MILK, due_from=None, due_to=None, status="accepted", visibility="private", beneficiary='["owner"]'):
    with sqlite3.connect(d.world_db) as c:
        c.execute("insert into intents(actor,party,agreed,kind,text,due_from,due_to,status,source,refs,visibility,"
                  "confidence,created,updated,last_action,beneficiary,scope,when_kind) values('nell','[\"nell\"]',"
                  "'[\"nell\"]','promise',?,?,?,?,'chat','[]',?,0.7,?,?,?,?,'world','day')",
                  (text, due_from, due_to, status, visibility, T0, T0, T0, beneficiary))
    c.close()


class Base(Dir):
    def house(self, env=None):
        return self.daemon(env={"INTENTS": "1", **(env or {})})

    def ctx(self, d, channel="private:nell"):
        seen, real = [], d.chat_agent.reply_as

        def spy(*a, **kw):
            seen.append(kw.get("memory_context") or "")
            return real(*a, **kw)

        d.chat_agent.reply_as = spy
        d.converse(channel, "牛奶是哪天去买来着？", owner_speaker(), actor="nell")
        self.assertTrue(seen)
        return seen[-1]


class ContextTests(Base):
    def test_private_turn_has_the_heading_and_the_worked_out_day(self):
        """Fails if the block is missing, the absolute date is missing, or the relative word is not the house's."""
        d = self.house()
        add(d, due_from=MIDNIGHT + 3 * DAY, due_to=MIDNIGHT + 4 * DAY)  # 11-25 seen from 11-22
        ctx = self.ctx(d)
        self.assertIn(HEAD, ctx)
        self.assertIn(f"答应房主的「{MILK}」还没办，约在11月25日（大后天）", ctx)

    def test_a_day_later_it_is_a_day_nearer(self):
        """Fails if the word is frozen at the moment the promise was made instead of worked out each turn."""
        d = self.house()
        add(d, due_from=MIDNIGHT + 3 * DAY, due_to=MIDNIGHT + 4 * DAY)
        for _ in range(6):  # 6 x 240 minutes: one house day
            d.fast_forward(240)
        self.assertIn("约在11月25日（后天）", self.ctx(d))

    def test_group_turn_never_has_a_private_promise(self):
        """Fails if the text of a private promise reaches a group prompt."""
        d = self.house()
        add(d, due_from=MIDNIGHT + 3 * DAY, due_to=MIDNIGHT + 4 * DAY)
        ctx = self.ctx(d, "group")
        self.assertNotIn(MILK, ctx)
        self.assertNotIn(HEAD, ctx)

    def test_completed_and_cancelled_are_not_listed(self):
        """Fails if a closed promise, or one still only proposed, is told as open."""
        d = self.house()
        for st in ("completed", "cancelled", "proposed"):
            add(d, f"奈尔答应房主，{st}的事", MIDNIGHT + 3 * DAY, MIDNIGHT + 4 * DAY, status=st)
        ctx = self.ctx(d)
        self.assertNotIn(HEAD, ctx)

    def test_undated_promise_says_no_day(self):
        """Fails if an undated promise gets an invented day."""
        d = self.house()
        add(d, "奈尔答应房主，帮他带一盒红茶回来")
        self.assertIn("答应房主的「奈尔答应房主，帮他带一盒红茶回来」还没办，没有约具体哪天", self.ctx(d))

    def test_intents_off_gives_no_block(self):
        """Fails if INTENTS=0 still tells open promises."""
        d = self.house({"INTENTS": "0"})
        add(d, due_from=MIDNIGHT + 3 * DAY, due_to=MIDNIGHT + 4 * DAY)
        self.assertNotIn(HEAD, self.ctx(d))

    def test_direct_brain_system_prompt_has_block_and_rule(self):
        """Fails if the direct path drops the block or lacks the rule sentence."""
        d = self.house()
        add(d, due_from=MIDNIGHT + 3 * DAY, due_to=MIDNIGHT + 4 * DAY)
        got = []
        d.chat_agent._llm_fn = lambda system, user: (got.append(system), "大后天")[1]
        d.converse("private:nell", "牛奶是哪天去买来着？", owner_speaker())
        self.assertIn(HEAD, got[-1])
        self.assertIn("（大后天）", got[-1])
        self.assertIn(agent_prompts.OPEN_PROMISES_RULE, got[-1])


class OpenLinesTests(Base):
    def lines(self, d, now=T0, private=True, n=5):
        return d.intents.open_lines("nell", now, private, n)

    def test_dated_by_start_then_undated_and_the_cap(self):
        """Fails if undated rows come first, dated ones are not by start, or `n` is not a cap."""
        d = self.house()
        add(d, "没日期的事")
        add(d, "晚的事", MIDNIGHT + 5 * DAY, MIDNIGHT + 6 * DAY)
        add(d, "早的事", MIDNIGHT + 1 * DAY, MIDNIGHT + 2 * DAY)
        out = self.lines(d)
        self.assertEqual([("早的事" in x, "晚的事" in x, "没日期的事" in x) for x in out],
                         [(True, False, False), (False, True, False), (False, False, True)])
        self.assertIn("（明天）", out[0])
        self.assertIn("（5天后）", out[1])
        self.assertEqual(len(self.lines(d, n=2)), 2)
        self.assertEqual(self.lines(d, n=0), [])

    def test_window_rules(self):
        """Fails if yesterday's is dropped or a long-past one is kept, or an exact time lacks its clock."""
        d = self.house()
        add(d, "昨天的事", MIDNIGHT - DAY, MIDNIGHT)
        add(d, "上周的事", MIDNIGHT - 8 * DAY, MIDNIGHT - 7 * DAY)
        add(d, "约好的钟点", MIDNIGHT + 2 * DAY + 14 * 3600, MIDNIGHT + 2 * DAY + 17 * 3600)
        out = self.lines(d, MIDNIGHT + 10 * 3600)
        self.assertEqual(len(out), 2)
        self.assertTrue(any("昨天的事" in x and "（昨天，已经过了）" in x for x in out))
        self.assertTrue(any("约在11月24日 15:00（后天）" in x for x in out))

    def test_visibility_and_beneficiary(self):
        """Fails if a private row leaks outside her private chat, or a promise not for the owner says 答应房主的."""
        d = self.house()
        add(d, "私下的事", MIDNIGHT + DAY, MIDNIGHT + 2 * DAY)
        add(d, "群里的事", MIDNIGHT + DAY, MIDNIGHT + 2 * DAY, visibility="group", beneficiary="[]")
        self.assertEqual(len(self.lines(d, private=True)), 2)
        out = self.lines(d, private=False)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("约好的「群里的事」"))

    def test_read_error_gives_nothing(self):
        """Fails if a missing table raises into the chat turn."""
        d = self.house()
        with sqlite3.connect(d.world_db) as c:
            c.execute("drop table intents")
        c.close()
        self.assertEqual(self.lines(d), [])


class RuleTests(unittest.TestCase):
    def test_rule_is_in_the_agent_system_prompt(self):
        """Fails if the agent path's rules lack the sentence (the direct path is checked in ContextTests)."""
        for where in ("private", "group"):
            text = agent_prompts.system_prompt("奈尔", where=where, allow_quiet=where == "group")
            self.assertIn(agent_prompts.OPEN_PROMISES_RULE, text)
        self.assertIn(RULE, agent_prompts.OPEN_PROMISES_RULE)
        self.assertIn("“你答应了、还没办的事”", agent_prompts.OPEN_PROMISES_RULE)


if __name__ == "__main__":
    unittest.main()
