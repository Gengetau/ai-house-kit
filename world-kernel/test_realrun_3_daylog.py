"""Real-run #3 (7 residents, 38.5 virtual hours): the fact list a diary is written from.

Evidence of the run: 凯 (a custom resident who moved in later) bought 6 things and was busy all day, but her diary fact
list only had five 「到了超市」 lines, so the model invented 「买了咖啡豆」. Three defects:
  1. daylog.entries() only knew the six built-in residents (legacy.NAMES): a custom resident was left out whole.
  2. collect_actor_facts() cut the list with deduped[:40]: on a busy day everything after the morning was lost.
  3. the diary's max_tokens (500) is tight for 300 Chinese characters.
No test calls a model.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

import test_daylog  # noqa: E402
from ai_house import diary as diary_mod  # noqa: E402
from ai_house import llm as llm_mod  # noqa: E402
from ai_house.diary import collect_actor_facts  # noqa: E402
from worldkernel import daylog, daynote, legacy  # noqa: E402

M, H = test_daylog.M, test_daylog.H  # 2026-11-07 00:00 JST
DAY = dt.date(2026, 11, 7)
NAMES = {"nell": "奈尔", "elsa": "艾莎", "alma": "阿尔玛", "mina": "米娜", "yuki": "由纪", "wanda": "婉妲", "kai": "凯"}
SHOP = "place:supermarket"


def _hm(h: float) -> str:
    return dt.datetime.fromtimestamp(M + h * H, test_daylog.JST).strftime("%H:%M")


class _Files:
    """A Day (in memory) written out to a life1.db file, plus a world.db with a script table."""

    def __init__(self, test: unittest.TestCase, day: "test_daylog.Day") -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.life = str(Path(tmp.name) / "life1.db")
        self.world = str(Path(tmp.name) / "world.db")
        day.db.commit()
        out = sqlite3.connect(self.life)
        day.db.backup(out)
        out.close()
        test.addCleanup(day.db.close)
        legacy.offline_world(self.world)
        self.day = day

    def script(self, at_h: float, actors: list, text: str) -> None:
        import json
        c = sqlite3.connect(self.world)
        with c:
            c.execute("INSERT INTO script(ts,actors,scene,text,source,created) VALUES(?,?,?,?,?,?)",
                      (M + at_h * H, json.dumps(actors), None, text, "kernel", M + at_h * H))
        c.close()

    def facts(self, actor: str, now_h: float = 24) -> list:
        return collect_actor_facts(self.life, self.world, actor, DAY, M + now_h * H, NAMES)


def _kai_shopping_day() -> "test_daylog.Day":
    d = test_daylog.Day()
    t = M + 14 * H
    d.move("kai", "kitchen", "away", t)
    d.move("kai", "away", SHOP, t + 60, secs=600)
    for i, (item, qty, yen) in enumerate((("cabbage", 1, 120), ("carrot", 2, 140), ("rice", 1, 480),
                                          ("egg", 1, 230), ("pork", 1, 390), ("chicken", 1, 350))):
        d.buy("kai", item, qty, yen, SHOP, t + 700 + i * 20, account="kai")
    d.move("kai", SHOP, "away", t + 900, secs=600)
    d.move("kai", "away", "living", t + 1500)
    d.do("kai", "reading", "living", M + 10 * H, 60)
    d.do("kai", "tea_time", "living", M + 16 * H, 30)
    return d


# ---------------------------------------------------------------------------------------------------------------
# 1. a custom resident is part of the world's roster
# ---------------------------------------------------------------------------------------------------------------
class CustomResidentInDayLog(unittest.TestCase):
    def test_diary_facts_of_a_custom_resident_carry_what_she_bought(self) -> None:
        f = _Files(self, _kai_shopping_day())
        f.script(14.2, ["kai"], "凯到了超市")
        got = f.facts("kai")
        text = "\n".join(got)
        for label in ("卷心菜", "胡萝卜", "米", "鸡蛋", "猪肉", "鸡肉"):
            self.assertIn(label, text)
        self.assertIn("凯出门去超市，买了", text)
        self.assertIn("自己的钱", text)  # her own wallet, not 公账
        self.assertIn("凯在一楼客厅看书 1 小时", text)  # the all-day activity is there as well
        self.assertIn("凯在一楼客厅喝茶 30 分钟", text)

    def test_entries_with_the_roster_show_her_room_and_name(self) -> None:
        d = test_daylog.Day()
        self.addCleanup(d.db.close)
        d.move("kai", "living", "room_kai", M + 21 * H)
        d.do("kai", "reading", "room_kai", M + 21 * H + 60, 30)
        d.do("nell", "reading", "room_nell", M + 22 * H, 30)
        d.move("nell", "room_nell", "room_kai", M + 23 * H)
        got = daylog.entries(d.db, M, M + 24 * H, M + 24 * H, names=NAMES)
        texts = [x["text"] for x in got]
        self.assertTrue(any(t.startswith("凯去自己房间") for t in texts), texts)
        self.assertIn("奈尔从自己房间去了凯的房间", texts)

    def test_default_roster_is_unchanged(self) -> None:
        d = test_daylog.Day()
        self.addCleanup(d.db.close)
        d.do("kai", "reading", "living", M + 10 * H, 30)
        d.do("nell", "reading", "living", M + 11 * H, 30)
        got = daylog.entries(d.db, M, M + 24 * H, M + 24 * H)
        self.assertEqual({x["actor"] for x in got}, {"nell"})

    def test_day_note_facts_know_the_roster_too(self) -> None:
        f = _Files(self, _kai_shopping_day())
        f.script(14.2, ["kai"], "凯到了超市")
        db = sqlite3.connect(f.life)
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        lines = daynote.facts(db, f.world, DAY, M + 24 * H, names=NAMES)
        texts = [x["text"] for x in lines]
        self.assertTrue(any("凯出门去超市，买了" in t for t in texts), texts)
        # the script's 「凯到了超市」 is the same thing the detail line says: told once
        self.assertFalse(any(t == "凯到了超市" for t in texts), texts)


# ---------------------------------------------------------------------------------------------------------------
# 2. a busy day still covers the whole day
# ---------------------------------------------------------------------------------------------------------------
def _busy_day() -> "test_daylog.Day":
    """06:00-23:00: a walk every 12 minutes (about 85 pure move lines) plus a handful of real things, mostly late."""
    d = test_daylog.Day()
    rooms = ("living", "kitchen", "room_nell", "bath")
    at, i = M + 6 * H, 0
    while at < M + 23 * H:
        d.move("nell", rooms[i % 4], rooms[(i + 1) % 4], at, secs=30)
        at += 12 * 60
        i += 1
    d.do("nell", "reading", "living", M + 9 * H + 120, 40)
    d.do("nell", "tea_time", "living", M + 15 * H + 120, 30)
    d.do("nell", "housework", "living", M + 18 * H + 120, 30)
    d.buy("nell", "cabbage", 1, 120, SHOP, M + 19 * H + 130)
    d.do("nell", "laundry", "living", M + 21 * H + 120, 30)
    return d


class BusyDayStaysWholeDay(unittest.TestCase):
    def setUp(self) -> None:
        self.f = _Files(self, _busy_day())
        self.f.script(7, ["nell"], "奈尔做了早饭")
        self.f.script(12, ["nell"], "奈尔做了午饭")
        self.f.script(19, ["nell"], "奈尔做了晚饭：咖喱饭")
        self.f.script(20, ["nell", "elsa"], "奈尔和艾莎一起看电影")
        self.f.script(22, ["nell"], "奈尔泡了汤")
        self.got = self.f.facts("nell")

    def test_the_list_is_big_enough_to_need_a_cut(self) -> None:
        all_lines = [x for x in daylog.entries(self.f.day.db, M, M + 24 * H, M + 24 * H) if x["actor"] == "nell"]
        self.assertGreater(len(all_lines), diary_mod.MAX_FACTS * 2)

    def test_stays_within_the_limit_and_in_time_order(self) -> None:
        self.assertLessEqual(len(self.got), diary_mod.MAX_FACTS)
        clocks = [x[:5] for x in self.got]
        self.assertEqual(clocks, sorted(clocks))

    def test_the_evening_and_night_are_still_there(self) -> None:
        last = self.got[-1][:5]
        self.assertGreaterEqual(last, "21:00", self.got[-5:])

    def test_things_beat_walks(self) -> None:
        text = "\n".join(self.got)
        for must in ("奈尔做了早饭", "奈尔做了午饭", "奈尔做了晚饭：咖喱饭", "奈尔和艾莎一起看电影", "奈尔泡了汤",
                     "买了卷心菜", "看书 40 分钟", "喝茶 30 分钟", "做家务 30 分钟", "洗衣服 30 分钟"):
            self.assertIn(must, text)

    def test_walks_are_thinned_evenly_across_the_day(self) -> None:
        walks = [x for x in self.got if "去了" in x]
        self.assertGreater(len(walks), 5)
        hours = {int(x[:2]) for x in walks}
        self.assertTrue(any(h < 10 for h in hours) and any(h >= 20 for h in hours), sorted(hours))

    def test_a_short_day_is_not_touched(self) -> None:
        d = test_daylog.Day()
        d.do("nell", "reading", "living", M + 9 * H, 30)
        d.do("nell", "tea_time", "living", M + 15 * H, 30)
        f = _Files(self, d)
        self.assertEqual(len(f.facts("nell")), 2)


# ---------------------------------------------------------------------------------------------------------------
# 3. the diary's token budget
# ---------------------------------------------------------------------------------------------------------------
class DiaryTokenBudget(unittest.TestCase):
    def test_diary_max_tokens_fits_300_chinese_characters(self) -> None:
        temperature, max_tokens, timeout, retries = llm_mod.DEFAULT_TASK_PROFILES["diary"]
        self.assertEqual(max_tokens, 800)
        self.assertEqual((temperature, timeout, retries), (0.7, 25.0, 1))  # nothing else moved


if __name__ == "__main__":
    unittest.main()
