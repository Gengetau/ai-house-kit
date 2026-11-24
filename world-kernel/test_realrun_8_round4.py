"""Real-run round 4 (7 residents, about 36 virtual hours, a real model): two small defects found when the run was audited.

Every sentence below is taken from that run's evidence.

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
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.diary import DIARY_MAX_CHARS, finish_diary_text  # noqa: E402
from ai_house.chat_identity import owner_speaker  # noqa: E402
from test_realrun_1_promises import H, JST, PACK, T0, _CapturingModel, _StoreCase  # noqa: E402
from ai_house.daemon import HouseDaemon  # noqa: E402


def _at(day: int, hh: int, mm: int = 0) -> float:
    return dt.datetime(2026, 11, day, hh, mm, tzinfo=JST).timestamp()


SAT_1730 = _at(21, 17, 30)
SUN_1730 = _at(22, 17, 30)
SUN_1300 = _at(22, 13, 0)
OUTING_SAID = "米娜，明天上午陪我去逛街买衣服，可以吗？"
ASK_BACK = "米娜，昨天说好的逛街，后来去了吗？"


def _non_errand(hints):
    return [h for h in hints if "买" not in h]


# ---------------------------------------------------------------------------------------------------------------
# 1. she can answer about her own non-purchase promises
# ---------------------------------------------------------------------------------------------------------------
class PromiseHintsForOutingsAndTogether(_StoreCase):
    def promise_outing(self) -> None:
        self.say("mina", OUTING_SAID, "好呀，陪你逛街没问题", now=SAT_1730)

    def test_expired_outing_is_told_with_absolute_date_and_part_of_day(self) -> None:
        self.promise_outing()
        self.store.sweep(SUN_1300)
        self.assertEqual([(r["kind"], r["state"]) for r in self.rows("mina")], [("outing", "expired")])
        hints = self.store.reply_hints("mina", ASK_BACK, SUN_1730)
        self.assertEqual(len(hints), 1, hints)
        h = hints[0]
        for must in ("逛街", "答应过", "11月22日（周日）上午，也就是今天上午", "已经过了", "没有成行", "没去成", "不要说不记得", "不要编"):
            self.assertIn(must, h)
        self.assertNotIn("和房主聊到", h)

    def test_the_next_day_the_same_promise_is_yesterday(self) -> None:
        self.promise_outing()
        self.store.sweep(SUN_1300)
        h = self.store.reply_hints("mina", ASK_BACK, _at(23, 9))[0]
        self.assertIn("11月22日（周日）上午，也就是昨天上午", h)

    def test_a_relative_word_inside_the_old_summary_is_not_left_to_mislead(self) -> None:
        self.promise_outing()
        self.store.sweep(SUN_1300)
        h = self.store.reply_hints("mina", ASK_BACK, SUN_1730)[0]
        # the stored sentence still says 「明天上午」, which was Saturday's tomorrow
        self.assertIn("以这里写的日期为准", h)

    def test_asked_with_other_words_for_the_same_promise(self) -> None:
        self.promise_outing()
        self.store.sweep(SUN_1300)
        for said in ("米娜，逛街好玩吗？", "我们昨天有去逛街吗", "逛街的事你还记得吗", "买衣服逛街那次怎么样"):
            with self.subTest(said=said):
                hints = self.store.reply_hints("mina", said, SUN_1730)
                self.assertEqual(len(hints), 1, hints)
                self.assertIn("11月22日（周日）", hints[0])

    def test_other_kinds_park_tea_and_curry(self) -> None:
        cases = [
            ("明天下午我们一起去公园玩好不好？", "好呀", "公园吗，去了没有", "公园"),
            ("婉妲，下午要不要一起喝下午茶呀？", "当然好呀", "下午茶后来喝了吗", "下午茶"),
            ("奈尔，今晚的晚饭能做咖喱饭吗？", "好的，我会做咖喱饭的", "咖喱饭你做了吗", "咖喱"),
        ]
        for said, reply, ask, word in cases:
            with self.subTest(ask=ask):
                self.setUp()
                self.say("mina", said, reply, now=SAT_1730)
                self.store.sweep(_at(24, 10))
                hints = self.store.reply_hints("mina", ask, _at(24, 10), window_s=5 * 86400.0)
                self.assertEqual(len(hints), 1, hints)
                self.assertIn(word, hints[0])
                self.assertIn("已经过了", hints[0])

    def test_completed_promise_says_it_was_done(self) -> None:
        self.promise_outing()
        self.evidence({"kind": "arrived", "actor": "mina", "place": "place:shotengai", "at": _at(22, 10)})
        self.store.sweep(_at(22, 11))
        self.assertEqual(self.rows("mina")[0]["state"], "completed")
        hints = self.store.reply_hints("mina", ASK_BACK, SUN_1730)
        self.assertEqual(len(hints), 1, hints)
        self.assertIn("去过了", hints[0])
        self.assertIn("11月22日（周日）上午", hints[0])
        self.assertNotIn("没去成", hints[0])
        self.assertNotIn("已经过了", hints[0])

    def test_not_yet_due_promise_says_it_is_still_to_come(self) -> None:
        self.promise_outing()
        hints = self.store.reply_hints("mina", "逛街的事你没忘吧？", SAT_1730 + 1800)
        self.assertEqual(len(hints), 1, hints)
        self.assertIn("还没到时间", hints[0])
        self.assertIn("11月22日（周日）上午", hints[0])
        self.assertIn("明天", hints[0])
        self.assertNotIn("没去成", hints[0])

    def test_unrelated_talk_brings_out_no_promise(self) -> None:
        self.promise_outing()
        self.say("mina", "婉妲，下午要不要一起喝下午茶呀？", "好呀", now=SAT_1730 + 60)
        self.store.sweep(SUN_1300)
        for said in ("米娜，今天天气怎么样？", "你最近在看什么书？", "晚饭想吃什么", "你喜欢吃什么水果",
                     "早上好，睡得好吗", "昨天说好的那本书，后来看了吗？", "米娜，你累不累", "我今天有点难过"):
            with self.subTest(said=said):
                self.assertEqual(self.store.reply_hints("mina", said, SUN_1730), [], said)

    def test_the_promise_of_another_resident_is_not_given(self) -> None:
        self.promise_outing()
        self.store.sweep(SUN_1300)
        self.assertEqual(self.store.reply_hints("nell", ASK_BACK, SUN_1730), [])

    def test_a_promise_older_than_the_window_is_not_brought_up(self) -> None:
        self.promise_outing()
        self.store.sweep(SUN_1300)
        ends = _at(22, 12, 30)  # the due window of the outing ended Sunday 12:30
        self.assertEqual(len(self.store.reply_hints("mina", ASK_BACK, ends + 3 * 86400.0 - 60)), 1)
        self.assertEqual(self.store.reply_hints("mina", ASK_BACK, ends + 3 * 86400.0 + 60), [])

    def test_a_note_to_remember_or_a_cancelled_promise_is_not_a_promise_to_report(self) -> None:
        self.say("mina", "米娜，我对花生过敏，你记得哦", "记住了", now=SAT_1730)
        self.say("mina", "明天一起去公园玩吧", "好呀", now=SAT_1730 + 60)
        for r in self.rows("mina"):
            if r["kind"] == "outing":
                self.store.cancel(r["intent_id"], now=SAT_1730 + 120)
        self.store.sweep(SUN_1300)
        self.assertEqual(self.store.reply_hints("mina", "花生过敏的事你记得吗", SUN_1730), [])
        self.assertEqual(self.store.reply_hints("mina", "昨天说好的公园，去了吗", SUN_1730), [])

    def test_a_plain_chat_about_the_same_words_without_a_promise_gets_nothing(self) -> None:
        self.assertEqual(self.store.reply_hints("mina", ASK_BACK, SUN_1730), [])


class VagueFollowUpBringsBackTheLatestTwo(_StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.say("mina", "明天上午陪我去逛街买衣服，可以吗？", "好呀", now=SAT_1730 - 7200)
        self.say("mina", "明天一起去公园玩吧", "好呀", now=SAT_1730 - 3600)
        self.say("mina", "婉妲，下午要不要一起喝下午茶呀？", "好呀", now=SAT_1730)
        self.store.sweep(_at(23, 3))

    def test_vague_question_gives_at_most_the_two_latest(self) -> None:
        hints = self.store.reply_hints("mina", "昨天说好的事，后来怎么样了？", SUN_1730)
        self.assertEqual(len(hints), 2, hints)
        self.assertIn("下午茶", hints[0])
        self.assertIn("公园", hints[1])

    def test_vague_question_without_any_word_of_a_promise_gives_nothing(self) -> None:
        for said in ("后来怎么样了？", "你今天过得怎么样？", "昨天怎么样"):
            with self.subTest(said=said):
                self.assertEqual(self.store.reply_hints("mina", said, SUN_1730), [], said)

    def test_a_specific_word_wins_over_the_vague_reading(self) -> None:
        hints = self.store.reply_hints("mina", "昨天说好的公园，后来去了吗", SUN_1730)
        self.assertEqual(len(hints), 1, hints)
        self.assertIn("公园", hints[0])


class ErrandHintsAreUnchanged(_StoreCase):
    def test_expired_catalog_errand_text_is_as_before(self) -> None:
        self.say("mina", "帮我买两个鸡蛋回来", "好呀", now=T0)
        self.store.sweep(T0 + 25 * H)
        hints = self.store.reply_hints("mina", "鸡蛋买了吗？", T0 + 26 * H)
        self.assertEqual(hints, ["你之前答应过帮房主买鸡蛋，但一直没买成，约定已经过期了。"
                                 "她问起时请如实说没买成、已经过期，不要当成还在办；她还要的话请她再说一声。"])

    def test_unbuyable_noun_text_is_as_before(self) -> None:
        hints = self.store.reply_hints("mina", "能帮我买个榴莲回来吗？", T0)
        self.assertEqual(hints, ["房主这句话想让你买「榴莲」，但这个你买不了（你只能去超市采购常见的食材、零食和饮品，「榴莲」不在其中）。"
                                 "请如实告诉她你买不了，不要答应去买；最多说可以先记个提醒。"])

    def test_errand_and_outing_hints_together(self) -> None:
        self.say("mina", "帮我买两个鸡蛋回来", "好呀", now=SAT_1730 - 600)
        self.say("mina", OUTING_SAID, "好呀", now=SAT_1730)
        self.store.sweep(_at(23, 1))
        hints = self.store.reply_hints("mina", "鸡蛋买了吗？逛街去了吗？", _at(23, 2))
        self.assertEqual(len(hints), 2, hints)
        self.assertIn("买鸡蛋", hints[0])
        self.assertIn("逛街", hints[1])

    def test_an_errand_is_not_repeated_as_a_promise(self) -> None:
        self.say("mina", "帮我买两个鸡蛋回来", "好呀", now=T0)
        self.store.sweep(T0 + 25 * H)
        self.assertEqual(len(self.store.reply_hints("mina", "说好的鸡蛋，后来买了吗", T0 + 26 * H)), 1)


# ---------------------------------------------------------------------------------------------------------------
# 1b. the same, through the daemon: the owner asks, the model's system prompt carries the fact
# ---------------------------------------------------------------------------------------------------------------
class DaemonGivesTheModelTheFact(unittest.TestCase):
    def test_follow_up_the_next_evening_reaches_the_prompt(self) -> None:
        srv = _CapturingModel("好呀，陪你逛街没问题")
        self.addCleanup(srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = HouseDaemon(
            tmp.name,
            pack_dir=PACK,
            env={"AI_HOUSE_API_KEY": "mock-key", "AI_HOUSE_BASE_URL": srv.base_url, "AI_HOUSE_MODEL": "mock-model"},
            start_ts=SAT_1730,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        def talk(text: str) -> None:
            d.converse("private:sora", text, owner_speaker("房主"))

        talk("明天上午陪我去逛街买衣服，可以吗？")
        rows = [r for r in d.intents.list_intents(actor="sora") if r["kind"] == "outing"]
        self.assertEqual(len(rows), 1, d.intents.list_intents(actor="sora"))

        d.clock.advance(SUN_1730 - d.clock.now())
        d.intents.sweep(d.clock.now())
        self.assertEqual([r["state"] for r in d.intents.list_intents(actor="sora") if r["kind"] == "outing"], ["expired"])

        talk("昨天说好的逛街，后来去了吗？")
        prompt = srv.systems[-1]
        self.assertIn("关于你和房主的约定（据实回答）", prompt)
        self.assertIn("11月22日（周日）上午", prompt)
        self.assertIn("没去成", prompt)
        self.assertIn("不要说不记得", prompt)

        talk("今天天气怎么样？")
        self.assertNotIn("关于你和房主的约定", srv.systems[-1])


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
