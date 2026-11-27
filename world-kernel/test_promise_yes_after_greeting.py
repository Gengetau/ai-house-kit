"""A plain yes after a greeting, or before an unrelated question, still counts as a yes to a proposal.

Defect (seen twice with a real model): to "奈尔，早上好。今天出门的时候帮我带一盒红茶回来，好吗？" she said
"早上好~好的,红茶我记下了..." and "好呀，...我正在厨房做法式吐司，要不要顺便给你留一份？"; both were judged "not a yes", so the
errand was never registered. No network, no real model. Every test says what would make it fail.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_m2_intents_adapter import Dir, T0, fake_model  # noqa: E402

import re  # noqa: E402

from ai_house.common import intents as I  # noqa: E402
from ai_house.common.intents import GREETING, NO, NO_EVIDENCE, SENTENCE, YES_START, _norm, _same_topic, _unreal  # noqa: E402,F401

P = "奈尔，早上好。今天出门的时候帮我带一盒红茶回来，好吗？"
R1 = "早上好~好的,红茶我记下了,出门的时候帮你带一盒回来。"
R2 = "好呀，出门的时候我记着帮你带一盒红茶回来。我正在厨房做法式吐司，要不要顺便给你留一份？"


# The function as it was before the change, copied verbatim.
def _old_bound_yes(who, quote_at, spoken):
    proposer, proposal = spoken[quote_at]
    if who == proposer:
        return True
    for i, (speaker, text) in enumerate(spoken):
        if speaker != who or i <= quote_at or _unreal(text) or NO.search(text) or NO_EVIDENCE.search(text):
            continue
        # A later, explicit quote of this particular proposal can identify the reply without time metadata.
        cited = re.findall(r'[「“\"]([^」”\"]+)[」”\"]', text)
        cites_proposal = any(_norm(c) == _norm(proposal) for c in cited)
        reply = text
        if cites_proposal:
            for c in cited:
                reply = reply.replace(c, '')
            reply = reply.strip('「」“”\" ：:，,')
        elif i != quote_at + 1:
            continue
        if re.search(r'[?？]', reply):
            continue
        yes = YES_START.match(reply.strip())
        if not yes:
            continue
        tail = reply.strip()[yes.end():].strip(' ，。！、,.!～~')
        if not tail or _same_topic(proposal, tail):
            return True
        # Object-free scheduling / participation continuation, not an unrelated affirmative sentence.
        tail = re.sub(r'^(?:我们|我|那|就)', '', tail)
        if re.fullmatch(r'(?:明天|后天|今天|周[一二三四五六日天])?[一二三四五六七八九十0-9]+点', tail):
            return True
        if re.fullmatch(r'(?:(?:明天|后天|今天|周[一二三四五六日天])?[一二三四五六七八九十0-9]*点?)?'
                        r'(?:出门|出发|见|见面|集合|去|参加|一起去)', tail):
            return True
    return False


def yes(reply, proposal=P):
    return I._bound_yes("nell", 0, [("owner", proposal), ("nell", reply)])


class Yes(unittest.TestCase):
    def test_plain_agreements_are_a_yes(self):
        """Fails if a greeting before the yes, or an unrelated later question, makes a plain agreement "not a yes"."""
        for r in (R1, R2, "好，明天出门时顺便帮你买六个鸡蛋，我记下了。", "早。好的，红茶我去买。",
                  "好呀。我正在做吐司，要不要给你留一份？"):
            with self.subTest(r):
                self.assertTrue(yes(r))

    def test_everything_else_stays_not_a_yes(self):
        """Fails if a question about the deal, a lone question, a yes inside a question or a greeting alone passes."""
        for r in ("好的。不过你确定要红茶吗？", "好的。红茶要哪一种？", "好吗？", "好的吧？", "早上好~",
                  "早餐吃了吗？好的", "早上好，我今天不出门。", "好的。真的要我去吗？",
                  "好的。不过我不能明天去，行吗？", "好呀。也许吧，你觉得呢？", "好的。不买行吗？"):
            with self.subTest(r):
                self.assertFalse(yes(r))

    def test_yes_must_be_the_line_right_after_unless_it_cites_the_proposal(self):
        """Fails if a yes two lines later counts without citing the proposal, or a citing one stops counting."""
        spoken = [("owner", P), ("nell", "我先去看看窗外。"), ("nell", R1)]
        self.assertFalse(I._bound_yes("nell", 0, spoken))
        spoken[2] = ("nell", "「" + P + "」好的，红茶我记下了。")
        self.assertTrue(I._bound_yes("nell", 0, spoken))



OLD_CASES = [
    "好的", "好的。", "好呀，红茶我记着。", "好，明天出门时顺便帮你买六个鸡蛋，我记下了。", "好的。我下午去超市买。",
    "好，明天3点", "行，出发", "可以，三点见", "好的，我们一起去", "好啊，帮你带红茶回来", "行吧", "没问题，红茶交给我",
    "当然，出门就带", "好的，不过我可能忘", "好的，了吧", "不行，我不去", "好的，不过你确定吗？", "好的吗？",
    "嗯嗯，我去买红茶。晚上给你。", "好的。今天天气真好。", "ok，红茶", "好的，我不能保证", "我也去", "算了，好吧",
    "好的，如果有空的话", "好的。红茶。", "好呀，我正在做饭", "行，明天出门", "一起，红茶", "好~红茶我记下了",
]


class NeverStricter(unittest.TestCase):
    def test_nothing_that_was_a_yes_stops_being_one(self):
        """Fails if any reply the old function accepted is refused by the new one."""
        passed = 0
        for r in OLD_CASES + [R1, R2, "好呀。我正在做吐司，要不要给你留一份？"]:
            for prop in (P, "明天3点一起去公园吗", "一起出发吗"):
                spoken = [("owner", prop), ("nell", r)]
                old = _old_bound_yes("nell", 0, spoken)
                if old:
                    passed += 1
                    with self.subTest(reply=r, proposal=prop):
                        self.assertTrue(I._bound_yes("nell", 0, spoken))
        self.assertGreater(passed, 15)  # the check really covers accepted replies


class EndToEnd(Dir):
    def stored(self, reply, quote="今天出门的时候帮我带一盒红茶回来"):
        d = self.daemon(env={"INTENTS": "1"})
        op = {"op": "intent", "text": "奈尔答应房主：帮房主带一盒红茶回来", "kind": "promise", "for": ["房主"],
              "agreed": ["奈尔"], "scope": "world", "when": "vague", "quote": quote}
        d.memory.observe("nell", "private", P, speaker_role="owner", speaker="房主", reply=reply, now=T0)
        d.memory.extract(force=True, now=T0 + 5, llm_fn=fake_model([op]))
        with d.intents._conn() as c:
            return [r["status"] for r in c.execute("select status from intents")]

    def test_reply_after_a_greeting_is_accepted(self):
        """Fails if the promise is stored as anything but accepted (it was rejected as "not a yes")."""
        self.assertEqual(self.stored(R1), ["accepted"])

    def test_reply_with_an_unrelated_later_question_is_accepted(self):
        """Fails if the promise is stored as anything but accepted."""
        self.assertEqual(self.stored(R2), ["accepted"])

    def test_quoting_her_line_with_an_offer_in_it_is_accepted(self):
        """Fails if a quote of her line is rejected as "turns it down" because the line also says 要不要 (an offer)."""
        reply = "好呀，我记下了，出门时顺路给你带一盒红茶回来。我这边法式吐司快好了，你要不要先来一块？"
        self.assertEqual(self.stored(reply, quote="出门时顺路给你带一盒红茶回来"), ["accepted"])

    def test_a_real_refusal_in_her_line_still_rejects(self):
        """Fails if a line with 不要 (not part of 要不要) or 算了 stops turning the promise down."""
        self.assertEqual(self.stored("不要，我今天不出门。", quote="不要，我今天不出门"), [])
        self.assertEqual(self.stored("好呀，不过算了吧，要不要改天？", quote="好呀，不过算了吧，要不要改天"), [])


class OfferIsNotARefusal(unittest.TestCase):
    def test_a_statement_with_an_offer_is_still_a_yes(self):
        """Fails if 要不要 inside a statement (no question mark) makes a plain yes "not a yes"."""
        for r in ("好呀，红茶我记下了，要不要我顺便带点牛奶。", "好呀，红茶我这就去买，要不要牛奶我也带。",
                  "好呀，红茶这就去买。你要不要也来一杯？"):
            with self.subTest(r):
                self.assertTrue(yes(r))
        self.assertTrue(I._agrees("好呀，红茶我记下了，要不要我顺便带点牛奶。"))

    def test_real_refusals_beside_an_offer_still_count(self):
        """Fails if removing 要不要 also hides a real 不要 / 算了 in the same reply."""
        for r in ("好呀，不过算了，要不要改天？", "好呀，红茶不要了，要不要牛奶我也带。"):
            with self.subTest(r):
                self.assertFalse(yes(r))
                self.assertFalse(I._agrees(r))


if __name__ == "__main__":
    unittest.main()
