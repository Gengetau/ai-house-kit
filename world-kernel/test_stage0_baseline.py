"""Regression tests for Stage 0 baseline fixes (PORTING-PLAN.md items 0.1-0.4)."""
from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.chat_agent import RoommateChatAgent
from ai_house.daemon import HouseDaemon, OffsetWallClock
from worldkernel.store import VirtualClock

JST = dt.timezone(dt.timedelta(hours=9))


class _FixedClock:
    def __init__(self, ts: float):
        self._ts = float(ts)

    def now(self) -> float:
        return self._ts


class Stage0ClockTests(unittest.TestCase):
    def test_real_clock_fast_forward_overnight_sleeps_changes_outfit_and_cooks(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        t0 = dt.datetime(2026, 11, 22, 18, 0, tzinfo=JST).timestamp()
        clock = OffsetWallClock(rt / "clock_offset.json", base_clock=VirtualClock(t0))
        daemon = HouseDaemon(runtime_dir=rt, virtual_clock=False, clock=clock, env={})
        self.addCleanup(daemon.close)

        with contextlib.closing(sqlite3.connect(daemon.life_db)) as lc:
            init_outfits = lc.execute("SELECT count(*) FROM outfit_log").fetchone()[0]

        for _ in range(6):
            daemon.fast_forward(180)  # 18:00 -> next day 12:00 JST

        self.assertAlmostEqual(daemon.memory._now(), daemon.clock.now(), places=3)
        self.assertAlmostEqual(daemon.authority.life.clock.now(), daemon.clock.now(), places=3)

        with contextlib.closing(sqlite3.connect(daemon.life_db)) as lc:
            sleeps = lc.execute("SELECT count(*) FROM actions WHERE action_type='sleep'").fetchone()[0]
            outfits = lc.execute("SELECT count(*) FROM outfit_log").fetchone()[0]
            cooks = lc.execute("SELECT count(*) FROM actions WHERE action_type='cook'").fetchone()[0]

        self.assertGreaterEqual(sleeps, 6, f"expected >=6 sleep actions overnight, got {sleeps}")
        self.assertGreater(outfits, init_outfits, "expected wardrobe changes after overnight fast-forward")
        self.assertGreaterEqual(cooks, 1, "expected at least one cooked meal overnight/morning")

    def test_fast_forward_on_non_advance_clock_updates_life_and_memory_clocks(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        t0 = dt.datetime(2026, 11, 22, 18, 0, tzinfo=JST).timestamp()
        daemon = HouseDaemon(runtime_dir=rt, virtual_clock=False, clock=_FixedClock(t0), env={})
        self.addCleanup(daemon.close)

        for _ in range(5):
            daemon.fast_forward(180)  # 18:00 -> next day 09:00 JST

        self.assertIs(daemon.authority.life.clock, daemon.clock)
        self.assertIs(daemon.authority.store.clock, daemon.clock)
        self.assertIs(daemon.memory._clock, daemon.clock)
        with contextlib.closing(sqlite3.connect(daemon.life_db)) as lc:
            sleeps = lc.execute("SELECT count(*) FROM actions WHERE action_type='sleep'").fetchone()[0]
        self.assertGreaterEqual(sleeps, 6)

    def test_fast_forward_offset_persists_across_restart_and_clears_on_reset(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        t0 = dt.datetime(2026, 11, 22, 9, 0, tzinfo=JST).timestamp()

        d1 = HouseDaemon(
            runtime_dir=rt,
            virtual_clock=False,
            clock=OffsetWallClock(rt / "clock_offset.json", base_clock=VirtualClock(t0)),
            env={},
        )
        for _ in range(6):
            d1.fast_forward(240)  # +24h
        d1.close()

        self.assertTrue((rt / "clock_offset.json").is_file())
        d2 = HouseDaemon(
            runtime_dir=rt,
            virtual_clock=False,
            clock=OffsetWallClock(rt / "clock_offset.json", base_clock=VirtualClock(t0)),
            env={},
        )
        self.addCleanup(d2.close)
        self.assertGreaterEqual(d2.get_state()["now"], t0 + 86400)

        d2.reset_house()
        self.assertFalse((rt / "clock_offset.json").exists())
        self.assertEqual(d2.clock.offset_s, 0.0)


class Stage0ErrandIntentTests(unittest.TestCase):
    def test_tool_promise_with_empty_or_chinese_item_falls_back_and_completes_shopping(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        start = dt.datetime(2026, 11, 21, 13, 30, tzinfo=JST).timestamp()
        d = HouseDaemon(runtime_dir=rt, virtual_clock=True, start_ts=start, env={})
        self.addCleanup(d.close)

        # Case 1: Model returned errand intent without `item`
        d.intents.enqueue_chat(
            "nell",
            "帮我买 3 个洋葱",
            "好呀，我待会儿顺路买",
            where="group",
            intents=[{"text": "帮房主买 3 个洋葱", "kind": "errand", "qty": 3, "答应了的": ["nell"]}],
            now=d.clock.now(),
        )
        # Case 2: Model returned errand intent with Chinese label `鸡蛋` instead of canonical `egg`
        d.intents.enqueue_chat(
            "nell",
            "再带两个鸡蛋回来",
            "没问题，记下啦",
            where="group",
            intents=[{"text": "买鸡蛋", "kind": "errand", "item": "鸡蛋", "qty": 2, "答应了的": ["nell"]}],
            now=d.clock.now(),
        )

        rows = {r["item"]: r for r in d.intents.list_intents(actor="nell")}
        self.assertIn("onion", rows)
        self.assertEqual(rows["onion"]["qty"], 3)
        self.assertEqual(rows["onion"]["state"], "open")
        self.assertIn("egg", rows)
        self.assertEqual(rows["egg"]["qty"], 2)
        self.assertEqual(rows["egg"]["state"], "open")

        errands = d.intents.errands("nell", d.clock.now())
        self.assertEqual({e["item"] for e in errands}, {"onion", "egg"})

        d.fast_forward(60)  # Daytime supermarket errand runs inside 09:30-16:00 JST

        after = {r["item"]: r for r in d.intents.list_intents(actor="nell")}
        self.assertEqual(after["onion"]["state"], "completed")
        self.assertEqual(after["egg"]["state"], "completed")
        self.assertTrue(str(after["onion"]["evidence_key"]).startswith("ai-house/"))
        self.assertTrue(str(after["egg"]["evidence_key"]).startswith("ai-house/"))

    def test_record_promise_tool_schema_and_allergy_note_not_completed_by_cooking(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        start = dt.datetime(2026, 11, 21, 17, 45, tzinfo=JST).timestamp()
        d = HouseDaemon(runtime_dir=rt, virtual_clock=True, start_ts=start, env={})
        self.addCleanup(d.close)

        specs = RoommateChatAgent(d.vocab)._builtin_tool_specs()
        rec_spec = next(s for s in specs if s["name"] == "record_promise")
        desc = rec_spec["parameters"]["properties"]["item"]["description"]
        self.assertIn("onion=洋葱", desc)
        self.assertIn("egg=鸡蛋", desc)

        d.intents.enqueue_chat(
            "nell",
            "记住我对花生过敏",
            "我记牢啦，绝不让你误食",
            where="private",
            intents=[{"text": "记得房主对花生过敏", "kind": "cook", "答应了的": ["nell"]}],
            now=d.clock.now(),
        )
        d.fast_forward(120)  # Crosses dinner cooking window 17:45 -> 19:45 JST
        allergy_rows = [r for r in d.intents.list_intents(actor="nell") if "花生过敏" in r["summary"]]
        self.assertTrue(allergy_rows)
        self.assertNotEqual(allergy_rows[0]["state"], "completed")


if __name__ == "__main__":
    unittest.main()
