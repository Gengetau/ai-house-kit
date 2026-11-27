"""She is told before she replies that something the owner asks for cannot be bought.

Defect (live run): the owner asked for 茶叶 (not in the catalog), she said "好，我带一盒回来", and the house then appended
"「茶叶」我这边买不了" to the same bubble. No network, no real model. Every test says what would make it fail."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_m2_intents_adapter import Dir  # noqa: E402

from ai_house.chat_identity import owner_speaker  # noqa: E402

HINT = "附近的店里买不到（小屋的清单里没有）"
TAIL = "我这边买不了，没法替你带回来"
YES = "好，我带一盒回来"
NO = "茶叶我买不到，红茶可以吗？"


class Base(Dir):
    def turn(self, owner_line, reply, channel="private:nell"):
        """-> (memory context her model got, the reply text sent)."""
        d = self.daemon(env={"INTENTS": "1"})
        seen, real = [], d.chat_agent.reply_as

        def spy(*a, **kw):
            seen.append(kw.get("memory_context") or "")
            return real(*a, **kw)

        d.chat_agent.reply_as = spy
        d.chat_agent._llm_fn = lambda system, user: (reply, {})
        out = d.converse(channel, owner_line, owner_speaker(), actor="nell")
        self.assertTrue(seen)
        return seen[-1], " ".join(r["text"] for r in out["replies"])


class HintTests(Base):
    def test_unbuyable_noun_gives_the_hint(self):
        """Fails if the model is not told, before replying, that 茶叶 cannot be bought."""
        ctx, _ = self.turn("帮我买一盒茶叶回来吧。", YES)
        self.assertIn("「茶叶」", ctx)
        self.assertIn(HINT, ctx)
        self.assertIn("不要答应去买", ctx)

    def test_the_hint_reaches_a_group_turn_of_the_owner(self):
        """Fails if the owner's group turn lacks the hint (same scope as the tail)."""
        ctx, _ = self.turn("帮我买一盒茶叶回来吧。", YES, channel="group")
        self.assertIn(HINT, ctx)

    def test_catalog_item_gives_no_hint(self):
        """Fails if a buyable item (红茶) gets the hint."""
        ctx, _ = self.turn("帮我买一盒红茶回来", YES)
        self.assertNotIn(HINT, ctx)

    def test_a_line_that_buys_nothing_gives_no_hint(self):
        """Fails if an ordinary line gets the hint."""
        ctx, _ = self.turn("今天天气不错，你在做什么？", "在看书")
        self.assertNotIn(HINT, ctx)


class TailTests(Base):
    def test_a_reply_that_declines_gets_no_tail(self):
        """Fails if the tail is added to a reply that already says no."""
        _, text = self.turn("帮我买一盒茶叶回来吧。", NO)
        self.assertIn("买不到", text)
        self.assertNotIn(TAIL, text)

    def test_a_reply_that_says_yes_keeps_the_tail(self):
        """Fails if the fallback tail is gone when she still says yes."""
        _, text = self.turn("帮我买一盒茶叶回来吧。", YES)
        self.assertIn(YES, text)
        self.assertIn("「茶叶」" + TAIL, text)


if __name__ == "__main__":
    unittest.main()
