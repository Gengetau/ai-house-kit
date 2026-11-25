"""Real-run #4, second review round (gpt-6.1-terra, 7 residents, about 36 virtual hours): defects the reviewer found.

Every sentence, event and line below is taken from that run's evidence (life1.db / world.db / terra.jsonl).

1. 「米娜，明天上午陪我去逛街买衣服，可以吗？」 was agreed to; 「陪我去…买衣服」 is company for the owner's own shopping, never
   an errand for the resident (what is still checked: `buy_words`; an agreed outing closes on arrival at its place and
   otherwise ends quietly). Recording such a sentence by rule is gone with the kit's own promise code;
2. 「婉妲，下午要不要一起喝下午茶呀？」 (Saturday 14:00) was closed by the tea party of Sunday 11:40, 21 hours later; a time
   given for the promise (now the extraction model's `due`) bounds what can close it;
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
from typing import List
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

import test_daylog  # noqa: E402
from test_realrun_1_promises import H, JST, T0, _StoreCase, plan_ops  # noqa: E402
from worldkernel import buy_words, daylog, legacy, vocab_life1  # noqa: E402

M = test_daylog.M  # 2026-11-07 00:00 JST
NAMES = {"nell": "奈尔", "elsa": "艾莎", "alma": "阿尔玛", "mina": "米娜", "yuki": "由纪", "wanda": "婉妲", "kai": "凯"}
SHOP = "place:supermarket"
SAT_1730 = dt.datetime(2026, 11, 21, 17, 30, tzinfo=JST).timestamp()


def _at(day: int, hh: int, mm: int = 0) -> float:
    return dt.datetime(2026, 11, day, hh, mm, tzinfo=JST).timestamp()


# ---------------------------------------------------------------------------------------------------------------
# 1. an invitation to go out together
# ---------------------------------------------------------------------------------------------------------------
class CompanionShoppingIsNotAnErrand(unittest.TestCase):
    SAID = "米娜，明天上午陪我去逛街买衣服，可以吗？"

    def test_buying_clothes_is_not_a_supermarket_errand(self) -> None:
        """Fails if the owner asking for company while she buys clothes ("陪我去商店街买衣服") is read as an errand for
        the resident (an item, a request noun, or the "买不了" tail); the control is a real request for eggs."""
        for said in (self.SAID, "陪我去商店街买衣服好不好？"):
            with self.subTest(said=said):
                self.assertIsNone(buy_words.detect_errand_item(vocab_life1, said)[0])
                self.assertEqual(buy_words.extract_buy_request_noun(said, "好呀，没问题"), "")
                self.assertEqual(buy_words.unbuyable_noun(vocab_life1, said), "")
        self.assertEqual(buy_words.detect_errand_item(vocab_life1, "米娜，帮我买两个鸡蛋回来")[0], "egg")
        self.assertEqual(buy_words.unbuyable_noun(vocab_life1, "米娜，帮我买个榴莲回来"), "榴莲")


class AnAgreedOutingClosesOnArrivalAndExpiresOtherwise(_StoreCase):
    SAID = "我明天上午去商店街"

    def agree(self) -> None:
        ops = plan_ops("米娜明天上午去商店街", self.SAID, due="2026-11-22 09:00", when="exact")
        self.assertEqual(self.chat("mina", [("mina", self.SAID)], ops, now=SAT_1730)["intents"], 1)
        row = self.rows("mina")[0]
        self.assertEqual((row["state"], dt.datetime.fromtimestamp(row["due_from"], JST).strftime("%d %H:%M"),
                          dt.datetime.fromtimestamp(row["due_to"], JST).strftime("%d %H:%M")), ("open", "22 08:00", "22 11:00"))

    def test_an_agreed_outing_closes_on_arrival_and_expires_otherwise(self) -> None:
        """Fails if arriving at the shopping street inside the window does not close it (and name the evidence), or if
        the same promise with no evidence is still open the evening after its window (quiet expiry, no notice)."""
        self.agree()
        key = self.evidence({"kind": "arrived", "actor": "mina", "place": "place:shotengai", "at": _at(22, 10)})
        self.sweep(_at(22, 11))
        row = self.rows("mina")[0]
        self.assertEqual((row["state"], row["evidence_key"]), ("completed", key))
        self.setUp()
        self.agree()
        self.sweep(_at(22, 20))
        self.assertEqual(self.rows("mina")[0]["state"], "open")  # still inside the grace after the window
        self.sweep(_at(23, 8))
        self.assertEqual(self.rows("mina")[0]["state"], "expired")

    def test_an_arrival_the_day_before_does_not_close_it(self) -> None:
        """Fails if an arrival at the same street before the promise was made (Saturday evening) closes it; the
        arrival inside the window (the control) does."""
        self.agree()
        self.evidence({"kind": "arrived", "actor": "mina", "place": "place:shotengai", "at": SAT_1730 - 3 * H})
        self.sweep(_at(22, 11))
        self.assertEqual(self.rows("mina")[0]["state"], "open")
        self.evidence({"kind": "arrived", "actor": "mina", "place": "place:shotengai", "at": _at(22, 10)})
        self.sweep(_at(22, 11))
        self.assertEqual(self.rows("mina")[0]["state"], "completed")


# ---------------------------------------------------------------------------------------------------------------
# 2. a time given for the promise bounds what can close it
# ---------------------------------------------------------------------------------------------------------------
class ATimeGivenBoundsThePromise(_StoreCase):
    FILM = "我们下午一起看电影吧"
    FILM_NIGHT = {"kind": "activity", "activity": "movie_night", "place": "living", "outcome": "succeeded"}

    def agree(self) -> None:
        ops = [{"op": "intent", "text": "婉妲和由纪一起看电影", "kind": "joint", "with": ["由纪"],
                "agreed": ["由纪", "婉妲"], "quote": self.FILM, "due": "2026-11-21 16:00", "when": "exact", "scope": "world"}]
        self.assertEqual(self.chat("wanda", [("wanda", self.FILM), ("yuki", "好呀")], ops, now=T0)["intents"], 1)
        row = self.rows("wanda")[0]
        self.assertEqual((row["status"], dt.datetime.fromtimestamp(row["due_to"], JST).strftime("%d %H:%M")),
                         ("accepted", "21 18:00"))

    def film_night(self, start: float) -> List[str]:
        return [self.evidence({**self.FILM_NIGHT, "actor": who, "plan_id": f"film-{int(start)}", "started_at": start,
                               "ended_at": start + 1800, "at": start + 1800}) for who in ("wanda", "yuki")]

    def test_this_afternoon_film_is_not_closed_by_tomorrows_film_night(self) -> None:
        """Fails if a film night of both the next day (11:40, long after the window of 15:00-18:00) closes the promise,
        or if it is still open after its window and grace; the same-afternoon film night (the control, a second house)
        closes it."""
        self.agree()
        self.film_night(_at(22, 11, 40))
        self.sweep(_at(22, 12))
        row = self.rows("wanda")[0]
        self.assertEqual((row["state"], row["status"]), ("expired", "missed"))
        self.setUp()
        self.agree()
        keys = self.film_night(_at(21, 16, 30))
        self.sweep(_at(21, 17))
        row = self.rows("wanda")[0]
        self.assertEqual(row["state"], "completed")
        self.assertIn(row["evidence_key"], keys)


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
