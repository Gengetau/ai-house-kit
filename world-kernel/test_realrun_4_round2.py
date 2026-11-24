"""Real-run #4, second review round (gpt-6.1-terra, 7 residents, about 36 virtual hours): defects the reviewer found.

Every sentence, event and line below is taken from that run's evidence (life1.db / world.db / terra.jsonl).

1. 「米娜，明天上午陪我去逛街买衣服，可以吗？」 was agreed to, but no 约定 was recorded: 「逛街」「陪我」 were not words
   of a plan, and the sentence is neither a purchase (「买衣服」) nor a question about the past;
2. 「婉妲，下午要不要一起喝下午茶呀？」 (Saturday 14:00) had no due window at all, so it got the default 24 hours and was
   closed by the tea party of Sunday 11:40, 21 hours later; a time word in the sentence now sets the window;
3. the fact list wrote 「米娜和阿尔玛、艾莎、奈尔、婉妲、由纪一起吃了牛丼」 for a table where only 奈尔 and 米娜 ate the
   牛丼 (the others ate 豚汁), and a walk line 「奈尔从超市去超市」 whose start and end are the same place.
No test calls a model.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

import test_daylog  # noqa: E402
from test_realrun_1_promises import H, JST, T0, _StoreCase  # noqa: E402
from worldkernel import daylog, legacy  # noqa: E402
from worldkernel.intents_lite import owner_proposes_plan, parse_due_window  # noqa: E402

M = test_daylog.M  # 2026-11-07 00:00 JST
NAMES = {"nell": "奈尔", "elsa": "艾莎", "alma": "阿尔玛", "mina": "米娜", "yuki": "由纪", "wanda": "婉妲", "kai": "凯"}
SHOP = "place:supermarket"
SAT_1730 = dt.datetime(2026, 11, 21, 17, 30, tzinfo=JST).timestamp()


def _at(day: int, hh: int, mm: int = 0) -> float:
    return dt.datetime(2026, 11, day, hh, mm, tzinfo=JST).timestamp()


# ---------------------------------------------------------------------------------------------------------------
# 1. an invitation to go out together is a promise
# ---------------------------------------------------------------------------------------------------------------
class GoingOutTogetherIsRecorded(_StoreCase):
    SAID = "米娜，明天上午陪我去逛街买衣服，可以吗？"
    REPLY = "明天上午啊，有点考验我这个夜猫子！不过陪你逛街没问题，几点出发呀？"

    def test_the_real_sentence_is_recorded_as_an_outing_for_tomorrow_morning(self) -> None:
        self.say("mina", self.SAID, self.REPLY, now=SAT_1730)
        rows = self.rows("mina")
        self.assertEqual([(r["kind"], r["state"], r["item"], r["raw_item"]) for r in rows],
                         [("outing", "open", None, "")], rows)
        self.assertEqual(dt.datetime.fromtimestamp(rows[0]["due_from"], JST).strftime("%d %H:%M"), "22 07:00")
        self.assertGreaterEqual(rows[0]["expires_at"], _at(22, 12))
        self.assertLess(rows[0]["expires_at"], _at(22, 15))

    def test_invitations_in_other_words(self) -> None:
        for said in ("陪我去书店转转吧", "周末陪我去趟医院好吗", "我们去逛逛街吧", "明天一起逛逛商店街怎么样？",
                     "下午陪我出去走走吧"):
            with self.subTest(said=said):
                self.setUp()
                self.say("mina", said, "好呀，没问题")
                self.assertEqual(len(self.rows("mina")), 1, said)
                self.assertEqual(self.rows("mina")[0]["kind"], "outing", said)

    def test_looking_back_or_small_talk_is_not_a_plan(self) -> None:
        for said in ("昨天说好的逛街，后来去了吗？", "上次陪我逛街的时候你买了什么？", "你平时喜欢逛街吗？",
                     "你昨天陪我去逛街，累不累？", "你陪我聊天的时候真开心"):
            with self.subTest(said=said):
                self.assertFalse(owner_proposes_plan(said), said)
                self.setUp()
                self.say("mina", said, "去了呀，挺开心的")
                self.assertEqual(self.rows("mina"), [], said)

    def test_buying_clothes_is_not_a_supermarket_errand(self) -> None:
        self.say("mina", self.SAID, self.REPLY, now=SAT_1730)
        self.say("mina", "陪我去商店街买衣服好不好？", "好呀", now=SAT_1730 + 600)
        for r in self.rows("mina"):
            self.assertNotEqual(r["kind"], "errand", r)
            self.assertFalse(r["item"] or r["raw_item"], r)

    def test_an_agreed_outing_closes_on_arrival_and_expires_otherwise(self) -> None:
        self.say("mina", self.SAID, self.REPLY, now=SAT_1730)
        self.evidence({"kind": "arrived", "actor": "mina", "place": "place:shotengai", "at": _at(22, 10)})
        self.store.sweep(_at(22, 11))
        self.assertEqual(self.rows("mina")[0]["state"], "completed")
        self.setUp()
        self.say("mina", self.SAID, self.REPLY, now=SAT_1730)
        self.store.sweep(_at(23, 8))
        self.assertEqual(self.rows("mina")[0]["state"], "expired")

    def test_a_reply_that_declines_still_records_nothing(self) -> None:
        self.say("mina", self.SAID, "明天上午不行，我要睡觉，改天吧", now=SAT_1730)
        self.assertEqual(self.rows("mina"), [])


# ---------------------------------------------------------------------------------------------------------------
# 2. a time word in the sentence sets the window of the promise
# ---------------------------------------------------------------------------------------------------------------
class TimeWordsSetTheWindow(_StoreCase):
    TEA = "婉妲，下午要不要一起喝下午茶呀？"
    TEA_PARTY = {"kind": "plan_finished", "actor": "wanda", "plan_id": "plan-2ee60ec729a9", "place": "living",
                 "outcome": "done", "members": ["wanda", "yuki"]}

    def test_this_afternoon_tea_is_not_closed_by_tomorrows_tea_party(self) -> None:
        # the real run: said Saturday 14:00, closed by the 11:40 tea party of Sunday (event 1133)
        self.say("wanda", self.TEA, "当然好呀", now=T0)
        row = self.rows("wanda")[0]
        self.assertEqual(row["kind"], "together")
        self.assertLessEqual(row["expires_at"], _at(21, 19))
        self.assertGreater(row["expires_at"], _at(21, 18))
        self.evidence({**self.TEA_PARTY, "activity": "tea_party", "at": _at(22, 11, 40)})
        self.store.sweep(_at(22, 12))
        self.assertEqual(self.rows("wanda")[0]["state"], "expired")

    def test_the_same_afternoon_tea_party_still_closes_it(self) -> None:
        self.say("wanda", self.TEA, "当然好呀", now=T0)
        self.evidence({**self.TEA_PARTY, "activity": "tea_party", "at": _at(21, 16, 30)})
        self.store.sweep(_at(21, 17))
        row = self.rows("wanda")[0]
        self.assertEqual((row["state"], row["evidence_key"]), ("completed", "k1"))

    def test_without_a_time_word_the_window_is_the_old_24_hours(self) -> None:
        self.say("wanda", "婉妲，要不要一起喝茶呀？", "好呀", now=T0)
        row = self.rows("wanda")[0]
        self.assertEqual((row["due_from"], row["due_to"]), (None, None))
        self.assertEqual(row["expires_at"], T0 + 24 * H)

    def test_parse_due_window_periods(self) -> None:
        def win(text: str, now: float) -> tuple:
            a, b = parse_due_window(text, now)
            f = lambda x: None if x is None else dt.datetime.fromtimestamp(x, JST).strftime("%d %H:%M")  # noqa: E731
            return f(a), f(b)

        self.assertEqual(win("下午要不要一起喝下午茶呀", T0), ("21 14:00", "21 18:30"))
        self.assertEqual(win("今天下午一起去逛逛", _at(21, 10)), ("21 12:00", "21 18:30"))
        self.assertEqual(win("今天上午去图书馆", _at(21, 8)), ("21 08:00", "21 12:30"))
        self.assertEqual(win("中午一起吃饭", _at(21, 9)), ("21 11:00", "21 14:00"))
        self.assertEqual(win("晚上一起看电影", _at(21, 14)), ("21 18:00", "21 23:59"))
        self.assertEqual(win("明天上午陪我去逛街", SAT_1730), ("22 07:00", "22 12:30"))
        self.assertEqual(win("明天下午去公园", SAT_1730), ("22 12:00", "22 18:30"))
        self.assertEqual(win("明天中午吃拉面", SAT_1730), ("22 11:00", "22 14:00"))
        self.assertEqual(win("今天一起大扫除", _at(21, 9)), ("21 09:00", "21 23:59"))
        # said after the period is over: it means tomorrow's
        self.assertEqual(win("下午一起喝茶", _at(21, 20)), ("22 12:00", "22 18:30"))
        # 「下午茶」 is a thing to drink, not a time
        self.assertEqual(win("要不要一起喝下午茶", T0), (None, None))
        # the words that were there before keep their windows
        self.assertEqual(win("明早去跑步", SAT_1730), ("22 07:00", "22 11:30"))
        self.assertEqual(win("明天去跑步", SAT_1730), ("22 08:00", "22 23:59"))
        self.assertEqual(win("今晚做咖喱", T0), ("21 18:00", "21 23:59"))


# ---------------------------------------------------------------------------------------------------------------
# 3. the fact list: who ate which dish, and walks that go nowhere
# ---------------------------------------------------------------------------------------------------------------
class _FakeStore:
    world_id = "ai-house/life1-test"
    synthetic = True

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def meta(self, key, default=None):
        return default

    def read(self):
        import contextlib
        return contextlib.nullcontext(self.db)


class SharedMealIsPerDish(unittest.TestCase):
    def meal(self, eats):
        """eats: [(actor, item, at_minutes_after_13:00)]; every dish was cooked by nell in the kitchen."""
        day = test_daylog.Day()
        self.addCleanup(day.db.close)
        cooks = {}
        for item in sorted({e[1] for e in eats}):
            cooks[item] = day.act("nell", "cook", {"recipe": item}, M + 12 * H, M + 12.5 * H, "kitchen")
        for actor, item, minute in eats:
            at = M + 13 * H + minute * 60
            day.act(actor, "eat", {"item": item, "resource_id": f"{item}@{cooks[item]}"}, at - 600, at, "kitchen")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wpath = str(Path(tmp.name) / "world.db")
        legacy.offline_world(wpath)
        life = SimpleNamespace(names=NAMES, clock=SimpleNamespace(now=lambda: M + 24 * H),
                               k=SimpleNamespace(s=_FakeStore(day.db)))
        proj = legacy.Projector(life, wpath)
        c = sqlite3.connect(wpath)
        c.row_factory = sqlite3.Row
        self.addCleanup(c.close)
        proj.facts(c)
        c.commit()
        return [(r["ts"], r["text"], json.loads(r["actors"]), json.loads(r["knowers"]))
                for r in c.execute("select * from script where source='kernel' and text like '%吃了%' order by ts, id")]

    def test_the_real_noon_table(self) -> None:
        # 豚汁: wanda, elsa, alma, yuki at 13:02; 牛丼: nell 13:12, mina 13:13
        rows = self.meal([("wanda", "tonjiru", 2), ("elsa", "tonjiru", 2), ("alma", "tonjiru", 2), ("yuki", "tonjiru", 2),
                          ("nell", "gyudon", 12), ("mina", "gyudon", 13)])
        texts = [r[1] for r in rows]
        gyudon = [r for r in rows if "牛丼" in r[1]]
        tonjiru = [r for r in rows if "豚汁" in r[1]]
        self.assertEqual(len(gyudon), 1, texts)
        self.assertEqual(len(tonjiru), 1, texts)
        self.assertEqual(sorted(gyudon[0][2]), ["mina", "nell"])
        self.assertEqual(sorted(tonjiru[0][2]), ["alma", "elsa", "wanda", "yuki"])
        for name in ("阿尔玛", "艾莎", "婉妲", "由纪"):
            self.assertNotIn(name, gyudon[0][1])
        for name in ("米娜", "奈尔"):
            self.assertNotIn(name, tonjiru[0][1].split("（")[0])  # 奈尔 cooked it, she did not eat it
        self.assertEqual(sorted(gyudon[0][3]), ["mina", "nell"])  # who knows it: the two who ate it

    def test_a_lone_eater_of_the_other_dish_has_her_own_line(self) -> None:
        rows = self.meal([("wanda", "tonjiru", 2), ("elsa", "tonjiru", 2), ("mina", "gyudon", 13)])
        texts = sorted(r[1] for r in rows)
        self.assertEqual(len(texts), 2, texts)
        self.assertIn("米娜吃了牛丼（奈尔做的）", texts)
        self.assertTrue(any("豚汁" in t and "米娜" not in t and "婉妲" in t and "艾莎" in t for t in texts), texts)

    def test_the_same_dish_is_still_one_line_for_the_table(self) -> None:
        rows = self.meal([("yuki", "curry_rice", 0), ("elsa", "curry_rice", 1), ("alma", "curry_rice", 2)])
        self.assertEqual(len(rows), 1, [r[1] for r in rows])
        self.assertEqual(sorted(rows[0][2]), ["alma", "elsa", "yuki"])

    def test_two_dishes_at_one_table_do_not_hide_each_others_line(self) -> None:
        # 艾莎 and 米娜 sit together, 艾莎 eats a different dish than the pair next to her: three lines, none shared
        rows = self.meal([("elsa", "tonjiru", 1), ("yuki", "gyudon", 2), ("mina", "gyudon", 3)])
        texts = [r[1] for r in rows]
        self.assertEqual(len(rows), 2, texts)
        self.assertTrue(any(t.startswith("艾莎吃了豚汁") for t in texts), texts)
        self.assertTrue(any("牛丼" in t and "艾莎" not in t for t in texts), texts)


class WalksThatGoNowhereAreNotLines(unittest.TestCase):
    def day(self):
        d = test_daylog.Day()
        self.addCleanup(d.db.close)
        t = M + 15.35 * H
        d.move("nell", "room_nell", "away", t - 600)
        d.move("nell", "away", SHOP, t - 500, secs=600)
        d.buy("nell", "pudding_cup", 1, 160, SHOP, t + 100)
        # left the shop, was home for a minute, and out again to the same shop: two trips, not one walk
        d.move("nell", SHOP, "away", t + 200, secs=600)
        d.move("nell", "away", "living", t + 800)
        d.move("nell", "living", "room_nell", t + 860)
        d.move("nell", "room_nell", "away", t + 1000, secs=75)
        d.move("nell", "away", SHOP, t + 1100, secs=600)
        d.buy("nell", "black_tea", 1, 420, SHOP, t + 1750)
        d.move("nell", SHOP, "away", t + 1800, secs=600)
        d.move("nell", "away", "living", t + 2400)
        return d

    def test_no_line_goes_from_a_place_to_the_same_place(self) -> None:
        texts = [x["text"] for x in daylog.entries(self.day().db, M, M + 24 * H, M + 24 * H, names=NAMES)]
        for t in texts:
            self.assertNotRegex(t, r"从(.{1,8})去\1")
        self.assertTrue(any(t.startswith("奈尔出门去超市，买了红茶") for t in texts), texts)
        self.assertTrue(any(t.startswith("奈尔出门去超市，买了布丁杯") for t in texts), texts)
        self.assertTrue(any("从超市回到家" in t for t in texts), texts)

    def test_a_walk_with_the_same_start_and_end_outside_is_dropped(self) -> None:
        d = test_daylog.Day()
        self.addCleanup(d.db.close)
        d.move("nell", SHOP, "away", M + 10 * H, secs=600)
        d.move("nell", "away", SHOP, M + 10 * H + 700, secs=600)
        got = daylog.entries(d.db, M, M + 24 * H, M + 24 * H, names=NAMES)
        self.assertEqual([x["text"] for x in got], [])

    def test_a_round_trip_with_nothing_bought_is_still_one_line(self) -> None:
        d = test_daylog.Day()
        self.addCleanup(d.db.close)
        d.move("nell", "kitchen", "away", M + 10 * H)
        d.move("nell", "away", SHOP, M + 10 * H + 60, secs=600)
        d.move("nell", SHOP, "away", M + 10 * H + 700, secs=600)
        d.move("nell", "away", "living", M + 10 * H + 1300)
        self.assertEqual([x["text"] for x in daylog.entries(d.db, M, M + 24 * H, M + 24 * H, names=NAMES)],
                         ["奈尔从超市回到家，到了一楼客厅"])


if __name__ == "__main__":
    unittest.main()
