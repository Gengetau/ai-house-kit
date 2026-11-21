"""The live shadow runner (shadow.py), on a virtual clock with a fake chat and a fake world.db."""
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest

import shadow as SH
from worldkernel.store import VirtualClock

T0 = 1792875600.0  # 2026-10-25 06:00 JST
ROUTINE = {
    "nell": [("06:00", "23:00", "kitchen", "cooking", "做饭", "limited"), ("23:00", "06:00", "room", "sleeping", "睡觉", "sleeping")],
    "elsa": [("07:00", "23:30", "study", "reading", "看书", "free"), ("23:30", "07:00", "room", "sleeping", "睡觉", "sleeping")],
    "alma": [("02:30", "10:30", "room", "sleeping", "睡觉", "sleeping"), ("10:30", "02:30", "room", "hobby", "收藏", "free")],
    "mina": [("03:30", "11:30", "room", "sleeping", "睡觉", "sleeping"), ("11:30", "03:30", "living", "gaming", "游戏", "free")],
    "yuki": [("00:00", "08:30", "room", "sleeping", "睡觉", "sleeping"), ("08:30", "00:00", "yard", "resting", "晒太阳", "free")],
    "wanda": [("01:00", "09:00", "room", "sleeping", "睡觉", "sleeping"), ("09:00", "01:00", "living", "phone", "刷手机", "free")],
}
SELVES = {a: f"{a}：测试室友" for a in ROUTINE}
REALITY = {"nell": ("kitchen", "cooking", 40, 80), "elsa": ("study", "reading", 30, 90), "alma": ("room", "sleeping", 20, 60),
           "mina": ("room", "sleeping", 50, 70), "yuki": ("room", "sleeping", 10, 95), "wanda": ("living", "phone", 65, 85)}


def fake_world(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE reality (actor TEXT PRIMARY KEY, location TEXT, activity TEXT, availability TEXT,"
              " hunger REAL, energy REAL)")
    for a, (loc, act, h, e) in REALITY.items():
        c.execute("INSERT INTO reality VALUES(?,?,?,?,?,?)", (a, loc, act, "free", h, e))
    c.commit()
    c.close()


class Chat:
    """Answers like a sensible housemate would, from the prompt it is given only."""

    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, []

    def __call__(self, system, user):
        self.calls.append((system, user))
        if self.mode == "boom":
            raise TimeoutError("model timeout")
        if self.mode == "junk":
            return "sorry, no json here", 7
        if "你决定对" in user:
            return '{"text": "要一起吃点吗？"}', 20
        seen = json.loads(user.split("\n", 1)[1].split("\n让你想一想", 1)[0])
        food = {i["item"]: i["resource_id"] for i in seen["items_here"]}
        if "omelette" in food:
            return json.dumps({"steps": [{"action_type": "eat", "args": {"item": "omelette", "resource_id": food["omelette"]}}]}), 30
        if seen["location"] == "kitchen" and "egg" in food:
            return json.dumps({"steps": [{"action_type": "cook", "args": {"recipe": "omelette"}, "uses": ["egg"]}]}), 30
        if seen["location"] != "kitchen":
            return json.dumps({"steps": [{"action_type": "move", "args": {"to": "kitchen"}}]}), 30
        return '{"steps": []}', 10


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.world = os.path.join(self.dir, "world.db")
        fake_world(self.world)
        self.clock = VirtualClock(T0)

    def make(self, chat):
        return SH.Shadow(os.path.join(self.dir, "kernel.db"), os.path.join(self.dir, "samples.db"), self.world,
                         ROUTINE, SELVES, chat, clock=self.clock)

    def run_for(self, sh, hours, step=60):
        for _ in range(int(hours * 3600 // step)):
            sh.step()
            self.clock.advance(step)

    def digest(self):
        with open(self.world, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def test_genesis_maps_the_live_house_onto_the_catalog(self):
        sh = self.make(Chat())
        self.addCleanup(sh.store.close)
        self.assertEqual(sh.store.meta("seeded"), "legacy_genesis")
        where = {a: sh.k.actor(a)["location"] for a in SH.ROSTER}
        self.assertEqual(where, {"nell": "kitchen", "elsa": "exit_rooms", "alma": "exit_rooms", "mina": "exit_rooms",
                                 "yuki": "exit_rooms", "wanda": "living"})
        self.assertEqual(sh.k.actor("wanda")["body"]["hunger"], 65)

    def test_genesis_preserves_zero_and_defaults_only_missing_metrics(self):
        zero = SH.genesis({"nell": {"location": "kitchen", "hunger": 0, "energy": 0.0}})
        self.assertEqual(zero["actors"]["nell"]["body"], {"hunger": 0.0, "energy": 0.0})
        absent = SH.genesis({"nell": {"location": "kitchen"}})
        self.assertEqual(absent["actors"]["nell"]["body"], {"hunger": 30.0, "energy": 75.0})
        nulls = SH.genesis({"nell": {"location": "kitchen", "hunger": None, "energy": None}})
        self.assertEqual(nulls["actors"]["nell"]["body"], {"hunger": 30.0, "energy": 75.0})

    def test_habits_come_from_the_routine_sleep_slot(self):
        p = SH.profiles(ROUTINE)
        self.assertEqual(p["alma"]["sleep"], "02:30-10:30")
        self.assertEqual(p["nell"]["profile_ref"], "world.py:ROUTINE:nell")
        sh = self.make(Chat())
        self.addCleanup(sh.store.close)
        self.assertEqual(sh.routine.habit("mina")["sleep_jst"], (210, 690))

    def test_a_day_stays_in_budget_and_never_touches_world_db(self):
        before = self.digest()
        chat = Chat()
        sh = self.make(chat)
        self.addCleanup(sh.store.close)
        self.run_for(sh, 24)
        self.assertEqual(self.digest(), before)
        db = sh.store.db
        counted = db.execute("SELECT COUNT(*) FROM budget_reservations WHERE state IN ('reserved','settled','unknown')").fetchone()[0]
        self.assertEqual(counted, len(chat.calls))  # every call reserved first
        self.assertGreater(counted, 0)
        self.assertLessEqual(counted, SH.LIMITS["per_day"])
        per_hour = db.execute("SELECT MAX(n) FROM (SELECT COUNT(*) n FROM budget_reservations GROUP BY CAST(created_at/3600 AS INT))").fetchone()[0]
        self.assertLessEqual(per_hour, SH.LIMITS["per_hour"])
        slept = {r[0] for r in db.execute("SELECT DISTINCT actor_id FROM actions WHERE action_type='sleep'")}
        self.assertEqual(slept, set(SH.ROSTER))
        self.assertTrue(db.execute("SELECT 1 FROM actions WHERE action_type='eat' AND state='succeeded'").fetchone())
        n = sh.samples.execute("SELECT COUNT(DISTINCT at) FROM samples").fetchone()[0]
        self.assertEqual(n, 24 * 6)

    def test_the_prompt_holds_only_what_she_perceived(self):
        chat = Chat()
        sh = self.make(chat)
        self.addCleanup(sh.store.close)
        self.run_for(sh, 14)
        self.assertTrue(chat.calls)
        for system, user in chat.calls:
            self.assertNotIn("api-keys", user)
            if "你决定对" not in user:
                seen = json.loads(user.split("\n", 1)[1].split("\n让你想一想", 1)[0])
                self.assertEqual(set(seen), {"location", "activity", "body", "here", "items_here", "recent", "offers",
                                             "materials"})

    def test_restock_happens_once_a_day_after_the_shopping_trip(self):
        sh = self.make(Chat("junk"))
        self.addCleanup(sh.store.close)
        sh.k.submit  # noqa: B018
        from worldkernel import admin
        admin.correction(sh.k, "admin:world", "egg", 0, "test")
        self.run_for(sh, 11, step=600)  # to 17:00
        self.assertEqual(sh.k.resource("egg")["qty"], 0)
        self.run_for(sh, 1, step=600)
        self.assertEqual(sh.k.resource("egg")["qty"], SH.STOCK["egg"])
        corrections = sh.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='owner_correction'").fetchone()[0]
        self.run_for(sh, 2, step=600)
        self.assertEqual(sh.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='owner_correction'").fetchone()[0],
                         corrections)

    def test_model_failures_count_and_never_stop_the_world(self):
        for mode in ("junk", "boom"):
            with self.subTest(mode=mode):
                self.setUp()
                chat = Chat(mode)
                sh = self.make(chat)
                self.addCleanup(sh.store.close)
                self.run_for(sh, 14)
                self.assertTrue(chat.calls)
                unknown = sh.store.db.execute("SELECT COUNT(*) FROM budget_reservations WHERE state='unknown'").fetchone()[0]
                self.assertEqual(unknown, len(chat.calls))
                self.assertTrue(sh.store.db.execute("SELECT 1 FROM actions WHERE action_type='sleep'").fetchone())

    def test_restart_resumes_without_reseeding(self):
        sh = self.make(Chat())
        self.run_for(sh, 2)
        sh.store.close()
        self.clock.advance(3600)
        again = self.make(Chat())
        self.addCleanup(again.store.close)
        self.assertEqual(again.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='world_seeded'").fetchone()[0], 1)
        self.assertEqual(again.store.epoch, 2)
        self.run_for(again, 1)

    def test_report_renders_a_day(self):
        sh = self.make(Chat())
        self.addCleanup(sh.store.close)
        self.run_for(sh, 24, step=120)
        text = SH.report(self.dir, "2026-10-25")
        self.assertIn("| 奈尔 |", text)
        self.assertIn("模型调用", text)
        self.assertNotIn("| 奈尔 | 0 |", text)

    def test_api_key_from_env(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"WORLD_KERNEL_API_KEY": "k-123"}):
            self.assertEqual(SH.api_key(), "k-123")
        with mock.patch.dict(os.environ, {"WORLD_KERNEL_API_KEY": ""}):
            with self.assertRaises(RuntimeError):
                SH.api_key()


if __name__ == "__main__":
    unittest.main()
