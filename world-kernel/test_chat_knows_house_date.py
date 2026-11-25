"""Every chat turn, on both brains, private and group, starts its context with the house's own date.

Defect (live run, --virtual-clock, house date 2026-11-21): she answered "大后天（11月28日）" because the model took
today's date from its own machine (the real date + 3). The date now comes from the daemon's clock for the turn.
No network, no real model. Every test says what would make it fail.
"""
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_fakes import FakeHub  # noqa: E402

from ai_house.agent.claude_engine import ClaudeCodeEngine  # noqa: E402
from ai_house.agent.reserved import AntigravityEngine, CodexEngine  # noqa: E402
from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, JST  # noqa: E402

KEY = "sk-ant-" + "a" * 40
SAT = dt.datetime(2031, 4, 12, 9, 45, tzinfo=JST).timestamp()   # a Saturday, years from the real today
MON = dt.datetime(2031, 4, 14, 23, 5, tzinfo=JST).timestamp()   # the Monday after
LINE_SAT = "小屋现在的时间：2031年4月12日 星期六 09:45（日本时间）。说“今天、明天、后天”都按这个日期算，不要用别处看到的日期。"
LINE_MON = "小屋现在的时间：2031年4月14日 星期一 23:05（日本时间）。说“今天、明天、后天”都按这个日期算，不要用别处看到的日期。"
HEAD = "小屋现在的时间："


class Base(unittest.TestCase):
    def house(self, start, env=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = HouseDaemon(tmp.name, env=env or {}, start_ts=start, virtual_clock=True, allow_onboarding=False)
        self.addCleanup(d.close)
        return d

    def ctx(self, d, channel):
        """What the direct brain's reply_as receives as memory_context."""
        seen, real = [], d.chat_agent.reply_as

        def spy(*a, **kw):
            seen.append(kw.get("memory_context") or "")
            return real(*a, **kw)

        d.chat_agent.reply_as = spy
        d.converse(channel, "后天有空吗？", owner_speaker())
        self.assertTrue(seen)
        return seen[-1]


class HouseDateTests(Base):
    def test_private_turn_starts_with_the_house_date_not_the_real_one(self):
        """Fails if the line is missing, is not the first line of the context, or shows time.time() instead of the
        daemon's (virtual) clock: the house starts in 2031, so the real year can not be in it."""
        d = self.house(SAT)
        ctx = self.ctx(d, "private:nell")
        self.assertEqual(ctx.split("\n")[0], LINE_SAT)
        self.assertEqual(ctx.count(HEAD), 1)
        real = dt.datetime.now(JST)
        self.assertNotIn(f"{real.year}年{real.month}月{real.day}日", ctx)

    def test_the_weekday_follows_the_date(self):
        """Fails if the weekday is fixed or wrong: Saturday 2031-04-12 and Monday 2031-04-14 must each get their own."""
        for start, line in ((SAT, LINE_SAT), (MON, LINE_MON)):
            with self.subTest(line):
                self.assertEqual(self.ctx(self.house(start), "private:nell").split("\n")[0], line)

    def test_the_line_moves_with_the_clock(self):
        """Fails if the line is built once (at start-up) instead of from the clock of each turn."""
        d = self.house(SAT)
        self.assertEqual(self.ctx(d, "private:nell").split("\n")[0], LINE_SAT)
        d.clock.advance((MON - SAT))
        self.assertEqual(self.ctx(d, "private:nell").split("\n")[0], LINE_MON)

    def test_group_turn_has_it_too(self):
        """Fails if a group turn gets no date line."""
        d = self.house(SAT)
        self.assertEqual(self.ctx(d, "group").split("\n")[0], LINE_SAT)

    def test_direct_brain_system_prompt_carries_it_once(self):
        """Fails if the direct path's system lines lack the date, or state it twice."""
        d = self.house(SAT)
        got = []
        d.chat_agent._llm_fn = lambda system, user: (got.append(system), "好的")[1]
        d.converse("private:nell", "后天有空吗？", owner_speaker())
        self.assertTrue(got)
        self.assertEqual(got[-1].count(HEAD), 1)
        self.assertIn(LINE_SAT, got[-1])

    def test_agent_brain_turn_prompt_carries_it_in_private_and_group(self):
        """Fails if the agent brain's turn prompt lacks the date line, in a private or a group turn."""
        d = self.house(SAT, env={"AI_HOUSE_BRAIN": "claude", "AI_HOUSE_AGENT_API_KEY": KEY})
        h = FakeHub()
        eng = ClaudeCodeEngine(d.env, d.runtime_dir / "agent", client_factory=h.factory, read_timeout_s=5.0)
        h.engine = eng
        d.agent_runtime.set_engines({"claude": eng, "codex": CodexEngine(), "agy": AntigravityEngine()})
        for channel in ("private:nell", "group"):
            with self.subTest(channel):
                h.script = [h.say("好的。")]
                before = len(h.prompts)
                d.converse(channel, "奈尔，后天有空吗？", owner_speaker())
                self.assertEqual(len(h.prompts), before + 1)
                self.assertIn(LINE_SAT, h.prompts[-1])
                self.assertEqual(h.prompts[-1].count(HEAD), 1)


if __name__ == "__main__":
    unittest.main()
