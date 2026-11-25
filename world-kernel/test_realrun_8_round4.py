"""Real-run round 4 (7 residents, about 36 virtual hours, a real model): two small defects found when the run was audited.

Every sentence below is taken from that run's evidence.

(M2: the promise code is now the shared module. The reply hints of defect 1 (D6) are retired; what is left of this file
is the guarantee that the grocery run does not close an outing promise, on the new path. See docs/_m2-cases-g2.md.)

1. Saturday 17:30 the owner asked 米娜 「明天上午陪我去逛街买衣服，可以吗？」 and she agreed; the promise was recorded
   (kind outing, due Sunday 07:00-12:30) and expired on Sunday. Sunday 17:30 the owner asked 「米娜，昨天说好的逛街，后来
   去了吗？」 and she answered 「这个我真翻不到逛街的记录……不敢瞎说去没去」: `IntentStore.reply_hints` only spoke about
   errands (purchases), so an expired outing / together / cooking promise gave her no fact to answer with.
2. A model wrote its diary as 「**奈尔的睡前日记** 今天早上……」 and the stored text became
   「2026-11-21 日记：**奈尔的睡前日记** 今天早上……」: `finish_diary_text` kept a Markdown title and bold marks.
No test calls a model.
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.diary import DIARY_MAX_CHARS, finish_diary_text  # noqa: E402
from test_realrun_7_round3 import JST, _PromiseCase  # noqa: E402
from worldkernel import buy_words, vocab_life1  # noqa: E402


def _at(day: int, hh: int, mm: int = 0) -> float:
    return dt.datetime(2026, 11, day, hh, mm, tzinfo=JST).timestamp()


SAT_1730 = _at(21, 17, 30)
SUN_1000 = _at(22, 10, 0)
SUN_1030 = _at(22, 10, 30)
TUE_0001 = _at(24, 0, 1)
OUTING_SAID = "米娜，明天上午陪我去逛街买衣服，可以吗？"
OUTING_TEXT = "米娜明天上午陪房主去逛街买衣服"  # what the extraction writes; the rules cannot read it as a place
SUNDAY = "2026-11-22"


# ---------------------------------------------------------------------------------------------------------------
# 0. the rerun with the fixes: the outing promise was closed by her own grocery run that morning
# ---------------------------------------------------------------------------------------------------------------
class GroceryRunDoesNotCloseAnOutingPromise(_PromiseCase):
    """The kernel's evidence closes a promise only through the places and things the promise text names.

    The promise-code module reads a promise's text into terms (`promise_rules.compile_terms`); a text it cannot read
    ("陪房主去逛街买衣服") has no term the evidence could satisfy, and a text naming a place ("去商店街") is closed only
    by arriving there. Every case holds a positive control: the same evidence closes a promise to go to the supermarket.
    """

    # the event that closed 「明天上午陪房主去逛街买衣服」 in the field, 09:43 on Sunday
    ARRIVED = {"kind": "arrived", "actor": "mina", "plan_id": "shopping-plan-ddcda32dc430",
               "action_id": "act-94fb639165a6", "place": "place:supermarket"}

    def promise(self, actor: str, owner_said: str, text: str, reply: str = "好呀，陪你逛街没问题") -> None:
        self.say(owner_said, {actor: reply}, text=text, due=SUNDAY, when="day", now=SAT_1730)

    def states(self, actor: str = "mina"):
        return [(r["text"], r["status"]) for r in self.rows(actor)]

    def finish(self, actor: str, plan: str, place: str, at: float) -> None:
        self.evidence({"key": f"{plan}#done", "kind": "plan_finished", "actor": actor, "plan_id": plan, "place": place,
                       "outcome": "done", "members": [actor], "at": at})

    def test_the_real_case_stays_open_then_ends_as_missed(self) -> None:
        """Fails if the grocery run (arrival at the supermarket, then the plan finished) closes 「陪房主去逛街买衣服」,
        if it is not still open after the sweep, if it never ends once its day has passed, or (control) if the same
        evidence does not close a second promise of hers to go to the supermarket."""
        self.promise("mina", OUTING_SAID, OUTING_TEXT)
        self.promise("mina", "米娜，明天上午去超市看看，可以吗？", "米娜去超市", reply="好呀，我去超市看看")
        self.evidence(dict(self.ARRIVED, key="e1", at=_at(22, 9, 43)))
        self.finish("mina", self.ARRIVED["plan_id"], "place:supermarket", _at(22, 10, 8))
        self.ci.sweep(SUN_1030)
        self.assertEqual(self.states(), [(OUTING_TEXT, "accepted"), ("米娜去超市", "completed")])
        self.ci.sweep(TUE_0001)
        self.assertEqual(self.states(), [(OUTING_TEXT, "missed"), ("米娜去超市", "completed")])
        self.assertEqual([r["state"] for r in self.ci.list_intents(actor="mina")].count("expired"), 1)

    def test_a_named_place_closes_it_and_other_places_do_not(self) -> None:
        """Fails if arriving at the park, the library or the convenience store closes a promise to go to the shopping
        street, or (control) if arriving at the shopping street does not."""
        for place, closes in (("place:shotengai", True), ("place:park", False), ("place:library", False),
                              ("place:konbini", False)):
            with self.subTest(place=place):
                self.setUp()
                self.promise("mina", "米娜，明天上午陪我去商店街逛逛，可以吗？", "米娜去商店街", reply="好呀，陪你逛逛没问题")
                self.assertEqual(self.states(), [("米娜去商店街", "accepted")])
                self.evidence({"key": "e1", "kind": "arrived", "actor": "mina", "plan_id": "outing-1", "place": place,
                               "at": _at(22, 9, 43)})
                self.ci.sweep(SUN_1030)
                self.assertEqual(self.states(), [("米娜去商店街", "completed" if closes else "accepted")])

    def test_an_outing_with_no_place_named_is_not_closed_by_the_grocery_run(self) -> None:
        """Fails if a grocery run (supermarket arrival, or a finished shopping plan) closes 「陪房主出去走走」, or
        (control) if the same events do not close 「去超市」. Kept from the old case: the grocery run does not close
        it. Removed: that an arrival at the park / riverside DOES close it (D6, kind-based closing: the text names no
        place, so no evidence closes it now)."""
        said, reply = "婉妲，明天上午陪我出去走走好吗？", "好呀，陪你出去走走！"
        for ev in (
            {"kind": "arrived", "actor": "wanda", "plan_id": "shopping-plan-9", "place": "place:supermarket"},
            {"kind": "arrived", "actor": "wanda", "plan_id": "shopping-plan-9", "place": "place:shotengai"},
            {"kind": "plan_finished", "actor": "wanda", "plan_id": "shopping-plan-9", "place": "place:supermarket",
             "outcome": "done", "members": ["wanda"]},
        ):
            with self.subTest(ev=ev):
                self.setUp()
                self.promise("wanda", said, "婉妲陪房主出去走走", reply=reply)
                self.promise("wanda", "婉妲，明天上午去超市看看好吗？", "婉妲去超市", reply="好呀，我去超市看看")
                self.assertEqual(self.states("wanda"), [("婉妲陪房主出去走走", "accepted"), ("婉妲去超市", "accepted")])
                self.evidence(dict(ev, key="e1", at=_at(22, 9, 43)))
                self.ci.sweep(SUN_1030)
                want = "completed" if ev["place"] == "place:supermarket" and ev["kind"] == "arrived" else "accepted"
                self.assertEqual(self.states("wanda"), [("婉妲陪房主出去走走", "accepted"), ("婉妲去超市", want)])

    def test_a_promise_to_go_to_the_supermarket_is_still_closed_by_going_there(self) -> None:
        """Fails if the arrival at the supermarket does not close 「去超市」, or closes it before the evidence is there
        (it must be open right after it was made)."""
        self.promise("wanda", "婉妲，明天上午陪我去超市逛逛好吗？", "婉妲去超市", reply="好呀，一起去超市！")
        self.assertEqual(self.states("wanda"), [("婉妲去超市", "accepted")])
        self.ci.sweep(SUN_1000)
        self.assertEqual(self.states("wanda"), [("婉妲去超市", "accepted")])
        self.evidence({"kind": "arrived", "actor": "wanda", "plan_id": "shopping-plan-9", "place": "place:supermarket",
                       "key": "e1", "at": _at(22, 9, 43)})
        self.ci.sweep(SUN_1030)
        self.assertEqual(self.states("wanda"), [("婉妲去超市", "completed")])


# ---------------------------------------------------------------------------------------------------------------
# 1. what is left of the old reply hints: the "cannot be bought" noun, and an errand whose day has passed
#    (the hints for outing / together / cooking promises and the daemon prompt block are retired: D6)
# ---------------------------------------------------------------------------------------------------------------
class UnbuyableNounAndExpiredErrand(_PromiseCase):
    def test_the_noun_behind_the_cannot_buy_tail_is_found_only_for_what_the_catalogue_lacks(self) -> None:
        """Fails if the durian (not sold) is not named, or a catalogue item (eggs, milk), small talk with 买 in it, or
        a person's name is taken for an unbuyable thing."""
        vocab = vocab_life1
        self.assertEqual(buy_words.unbuyable_noun(vocab, "能帮我买个榴莲回来吗？"), "榴莲")
        for said in ("帮我买两个鸡蛋回来", "能帮我买两盒牛奶回来吗？", "米娜帮我买点东西", "今天过得怎么样？"):
            with self.subTest(said=said):
                self.assertEqual(buy_words.unbuyable_noun(vocab, said), "")
        self.assertEqual(buy_words.detect_errand_item(vocab, "帮我买两个鸡蛋回来"), ("egg", 2))

    def test_an_errand_whose_day_has_passed_ends_quietly_and_stops_being_an_errand(self) -> None:
        """Fails if the errand is not offered to the kernel on its day (and not before), is still offered after its day has passed, is
        not ended as missed, or if no purchase evidence was needed to end it (the control is the open state before)."""
        self.say("帮我买两个鸡蛋回来", {"mina": "好呀"}, text="米娜买2个鸡蛋", due=SUNDAY, when="day", now=SAT_1730)
        self.assertEqual(self.errands("mina", SAT_1730 + 60), [])  # not before the day (two hours earlier)
        self.assertEqual([(e["item"], e["qty"]) for e in self.errands("mina", SUN_1000)], [("egg", 2)])
        self.ci.sweep(_at(22, 12))
        self.assertEqual([r["status"] for r in self.rows("mina")], ["accepted"])
        self.ci.sweep(TUE_0001)
        self.assertEqual([r["status"] for r in self.rows("mina")], ["missed"])
        self.assertEqual(self.errands("mina", TUE_0001 + 60), [])
        self.assertEqual([r["state"] for r in self.ci.list_intents(actor="mina")], ["expired"])


# ---------------------------------------------------------------------------------------------------------------
# 2. no Markdown title or bold marks in the diary text
# ---------------------------------------------------------------------------------------------------------------
BODY = "今天早上我在厨房帮艾莎切了洋葱，中午大家一起吃了咖喱饭，下午在院子里看了一会儿书，晚上早早洗了澡，准备睡觉了。"


class DiaryTextHasNoTitle(unittest.TestCase):
    def test_the_real_bold_title_is_removed(self) -> None:
        raw = "**奈尔的睡前日记** 今天早上我在厨房帮艾莎切了洋葱。中午大家一起吃了咖喱饭。"
        self.assertEqual(finish_diary_text(raw), "今天早上我在厨房帮艾莎切了洋葱。中午大家一起吃了咖喱饭。")

    def test_other_title_forms(self) -> None:
        forms = [
            "**奈尔的睡前日记**\n" + BODY,
            "**奈尔的睡前日记**：" + BODY,
            "# 奈尔的日记\n\n" + BODY,
            "## 11月21日 日记\n" + BODY,
            "【奈尔的睡前日记】" + BODY,
            "【奈尔的睡前日记】\n" + BODY,
            "11月21日 日记\n" + BODY,
            "11月21日（周六）日记：\n" + BODY,
            "奈尔的睡前日记\n" + BODY,
            "日记\n" + BODY,
            "# 奈尔的日记 " + BODY,
        ]
        for raw in forms:
            with self.subTest(raw=raw[:24]):
                self.assertEqual(finish_diary_text(raw), BODY)

    def test_paired_bold_marks_in_the_body_are_removed_but_not_the_words(self) -> None:
        raw = "今天**特别**开心，中午吃了**咖喱饭**，下午和艾莎一起整理了院子里的花，晚上洗了澡，准备睡觉了。"
        self.assertEqual(finish_diary_text(raw), raw.replace("**", ""))

    def test_title_and_bold_together(self) -> None:
        raw = "**奈尔的睡前日记**\n" + "今天**特别**开心，" + BODY[2:]
        self.assertEqual(finish_diary_text(raw), "今天特别开心，" + BODY[2:])

    def test_normal_diaries_are_not_touched(self) -> None:
        for raw in (
            BODY,
            "「今天真好」，我对自己说。" + BODY,
            "《夜航船》我今天读完了，很喜欢结尾。" + BODY,
            "“日记”这个词我今天想了很久，" + BODY,
            "《日记》是我今天读的一本小书，" + BODY,
            "#今天的天气真好，" + BODY,
            "今天写日记的时候，窗外下起了雨。" + BODY,
            "日记本用完了，明天要去买新的。" + BODY,
            "今天是11月21日，" + BODY,
            "11月21日，晴。" + BODY,
            "2026-11-21 日记：" + BODY,  # the stored form (the template diary carries it itself) is left as it is
        ):
            with self.subTest(raw=raw[:20]):
                self.assertEqual(finish_diary_text(raw), raw)

    def test_a_lone_bold_word_that_is_not_a_title_only_loses_its_marks(self) -> None:
        raw = "**今天**真的很开心，" + BODY
        self.assertEqual(finish_diary_text(raw), "今天真的很开心，" + BODY)

    def test_title_goes_before_the_length_limit_and_the_sentence_cut(self) -> None:
        long = "**奈尔的睡前日记** " + "今天我在院子里看书，看了很久很久。" * 30
        out = finish_diary_text(long)
        self.assertLessEqual(len(out), DIARY_MAX_CHARS)
        self.assertTrue(out.startswith("今天我在院子里看书"))
        self.assertTrue(out.endswith("。"))
        self.assertNotIn("*", out)
        # and a short title-only head does not eat the budget of the body
        body = "今天我在院子里看书，看了很久很久。" * 17  # 289 chars: fits once the 14-char title is gone
        self.assertEqual(finish_diary_text("**奈尔的睡前日记** " + body), body)

    def test_a_dateline_of_its_own_above_the_text_goes(self) -> None:
        # what the rerun with the fixes showed: 「11月21日，夜\n\n早上换好…」 under the stored 「2026-11-21 日记：」 prefix
        body = "早上换好苏格兰格纹连衣裙，别上珍珠发夹。我和由纪吃了奈尔做的盐烤鱼。"
        for head in ("11月21日，夜\n\n", "11月21日，晚上\n\n", "2026年11月22日，夜。\n\n", "2026年11月22日，夜\n",
                     "11月21日（周六） 晴\n", "2026-11-21 周六，夜\n\n", "11月21日\n"):
            with self.subTest(head=head):
                self.assertEqual(finish_diary_text(head + body), body)

    def test_a_date_that_opens_a_sentence_stays(self) -> None:
        for text in ("11月21日，夜里我睡不着，就去厨房热了牛奶。", "11月21日，晚上和大家吃了咖喱饭。后来早早睡了。",
                     "2026-11-22 日记：早上我做了吐司；中午我去了超市。", "11月21日，夜"):
            with self.subTest(text=text):
                self.assertEqual(finish_diary_text(text), text)

    def test_a_reply_that_is_only_a_title_is_not_emptied(self) -> None:
        self.assertTrue(finish_diary_text("**奈尔的睡前日记**").strip("*"))


if __name__ == "__main__":
    unittest.main()
