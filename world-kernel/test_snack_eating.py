"""eating_snack eats one snack from the stock (the owner's gifts as well as the house's), and
the hungry snack is not always the house bread."""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from worldkernel.store import Store, VirtualClock  # noqa: E402
from worldkernel.life import Life, JST, day_of  # noqa: E402
from worldkernel import inventory, vocab_life1 as V  # noqa: E402

# the shape inventory.legacy_items gives: two of the owner's gifts (a snack, a drink) and elsa's own cookies
SEED = [
    {"id": 24, "name": "湖池屋海苔盐薯片", "place": "snacks", "qty": 6, "unit": "袋", "kind": "food", "owner": "",
     "holder": "", "note": "", "by": "owner", "owner_events": [4340], "owner_qty": 6},
    {"id": 27, "name": "午後の紅茶", "place": "fridge", "qty": 1, "unit": "瓶", "kind": "food", "owner": "",
     "holder": "", "note": "", "by": "owner", "owner_events": [4342], "owner_qty": 1},
    {"id": 30, "name": "曲奇", "place": "room", "qty": 2, "unit": "盒", "kind": "food", "owner": "elsa",
     "holder": "", "note": "", "by": "elsa", "owner_events": [], "owner_qty": 0},
]
CHIPS, TEA, CLAIRES = "chips@legacy:24", "bottled_tea@legacy:27", "cookie@legacy:30"
T0 = dt.datetime(2026, 11, 7, 15, 0, tzinfo=JST).timestamp()  # Saturday, between lunch and dinner


class Snack163(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = VirtualClock(T0)
        self.s = Store(os.path.join(self.tmp.name, "kernel.db"), self.clock, world_id="snack-163", synthetic=True,
                       holder="test")
        self.s.acquire()
        self.l = Life(self.s, seed_items=SEED)

    def tearDown(self):
        self.s.release()
        self.s.close()
        self.tmp.cleanup()

    def walk(self, a, to):
        for _ in range(30):
            if self.l.k.actor(a)["location"] == to:
                return
            if not self.l.k.actor(a)["action_id"]:
                self.l.submit(a, "move", {"to": to}, "walk")
            self.clock.advance(60)
            self.l.k.tick()
        self.fail(f"{a} did not get to {to}")

    def begin(self, a, args, key):
        return self.l.k.submit({"idempotency_key": key, "principal": "routine", "actor_id": a, "origin": "routine",
                                "action_type": "begin_activity", "args": args})

    def qty(self, rid):
        with self.s.read() as c:
            return c.execute("SELECT qty FROM resources WHERE resource_id=?", (rid,)).fetchone()[0]

    def finish(self, seconds):
        self.clock.advance(seconds + 1)
        self.l.k.tick()
        self.l.meals.sync()

    def test_eating_snack_takes_one_from_the_stock_with_its_source(self):
        self.walk("wanda", "living")
        r = self.begin("wanda", {"activity": "eating_snack", "seconds": 600}, "t1")
        self.assertEqual((r["status"], r["reason"]), ("failed", "guard:snack_required"))
        r = self.begin("wanda", {"activity": "eating_snack", "seconds": 600, "snack": CHIPS}, "t2")
        self.assertEqual(r["status"], "running", r)
        self.assertEqual(self.qty(CHIPS), 6)  # held while she eats
        self.finish(600)
        self.assertEqual(self.qty(CHIPS), 5)
        with self.s.read() as c:
            row = c.execute("SELECT actor, item, kind, slot FROM meal_consumption WHERE action_id=?",
                            (r["action_id"],)).fetchone()
            src = c.execute("SELECT r.resource_id, r.source FROM reservations v JOIN resources r"
                            " ON r.resource_id=v.resource_id WHERE v.action_id=? AND v.consume=1",
                            (r["action_id"],)).fetchone()
        self.assertEqual(tuple(row), ("wanda", "chips", "snack", None))
        self.assertEqual(src[0], CHIPS)
        self.assertEqual(inventory.source_of({"source": src[1]})["owner_events"], [4340])

    def test_the_last_piece_is_one_persons(self):
        self.walk("wanda", "living")
        self.walk("mina", "living")
        self.assertEqual(self.begin("wanda", {"activity": "eating_snack", "seconds": 600, "snack": TEA}, "w")["status"],
                         "running")
        r = self.begin("mina", {"activity": "eating_snack", "seconds": 600, "snack": TEA}, "g")
        self.assertEqual((r["status"], r["reason"]), ("failed", "resource_unavailable"))
        self.finish(600)
        self.assertEqual(self.qty(TEA), 0)

    def test_what_she_may_have(self):
        self.walk("wanda", "living")
        bad = {CLAIRES: "guard:snack_missing_mismatched_or_out_of_reach",  # elsa's own
               "egg": "guard:snack_missing_mismatched_or_out_of_reach",  # an ingredient, not a snack
               "nothing@here": "guard:snack_missing_mismatched_or_out_of_reach"}
        for i, (rid, why) in enumerate(bad.items()):
            r = self.begin("wanda", {"activity": "eating_snack", "seconds": 600, "snack": rid}, f"b{i}")
            self.assertEqual((r["status"], r["reason"]), ("failed", why), rid)
        r = self.begin("wanda", {"activity": "phone_browsing", "seconds": 900, "snack": CHIPS}, "p")
        self.assertEqual((r["status"], r["reason"]), ("failed", "guard:snack_not_eaten_in_this_activity"))
        self.walk("elsa", "living")  # her own cookies from her room, on the sofa
        self.assertEqual(self.begin("elsa", {"activity": "eating_snack", "seconds": 600, "snack": CLAIRES},
                                    "c")["status"], "running")

    def test_her_habit_picks_a_snack_for_it(self):
        self.walk("wanda", "living")
        self.l.pastime_options = lambda a, now: [("eating_snack", 5, ["living", "kitchen"])]
        self.l._pastime("wanda", day_of(self.clock.now()), self.clock.now())
        with self.s.read() as c:
            args = json.loads(c.execute("SELECT args FROM actions WHERE action_id=?",
                                        (self.l.k.actor("wanda")["action_id"],)).fetchone()[0])
        self.assertEqual(args["activity"], "eating_snack")
        self.assertIn(args["snack"], {CHIPS, TEA, "bread", "fruit", "pudding_cup", "black_tea"})

    def test_no_snack_left_no_eating_snack(self):
        self.assertIn("eating_snack", [k for k, _, _ in self.l.pastime_options("wanda", self.clock.now())])
        with self.s.tx() as c:
            c.execute("UPDATE resources SET qty=0 WHERE kind='food' AND item IN (%s)" % ",".join("?" * len(V.SNACKS)),
                      sorted(V.SNACKS))
        self.assertNotIn("eating_snack", [k for k, _, _ in self.l.pastime_options("wanda", self.clock.now())])
        self.assertIsNone(self.l._before_step("wanda", {"action_type": "begin_activity",
                                                        "args": {"activity": "eating_snack", "seconds": 900}}))

    def test_her_own_decision_gets_a_snack_too(self):
        self.walk("yuki", "living")
        args = self.l._before_step("yuki", {"action_type": "begin_activity",
                                              "args": {"activity": "eating_snack", "seconds": 900}})
        self.assertEqual(args["seconds"], 900)
        with self.s.read() as c:
            row = dict(c.execute("SELECT resource_id, kind, item, location, holder, qty FROM resources"
                                 " WHERE resource_id=?", (args["snack"],)).fetchone())
        self.assertTrue(V.snack_ok(row, "eating_snack", "yuki", "living"))
        # a step that is not an eating activity is left alone
        self.assertIsNone(self.l._before_step("yuki", {"action_type": "begin_activity",
                                                         "args": {"activity": "phone_browsing", "seconds": 900}}))

    def test_hungry_snack_is_not_always_the_house_bread(self):
        picks = []
        for _ in range(120):
            picks.append(self.l.snack_for("mina", classes=("snack", "dish")))
            self.clock.advance(60)
        items = {p[1] for p in picks}
        self.assertIn("chips", items)  # the owner's, and mina likes chips
        self.assertIn("bread", items)  # the house's still come up
        self.assertFalse(items & {"bottled_tea", "black_tea"})  # no drink when she is hungry
        self.assertNotIn(CLAIRES, {p[0] for p in picks})  # never someone else's own

    def test_hungry_snack_is_eaten_and_counted(self):
        before = {r: self.qty(r) for r in (CHIPS, "bread", "fruit", "pudding_cup")}
        for _ in range(30):
            if self.l.k.actor("mina")["action_id"] is None:
                self.l._snack("mina")
            self.clock.advance(60)
            self.l.k.tick()
            self.l.meals.sync()
            with self.s.read() as c:
                got = c.execute("SELECT item, kind FROM meal_consumption WHERE actor='mina'").fetchall()
            if got:
                break
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1], "snack")
        after = {r: self.qty(r) for r in before}
        self.assertEqual(sum(before.values()) - sum(after.values()), 1)

    def test_interrupted_she_keeps_it(self):
        self.walk("wanda", "living")
        r = self.begin("wanda", {"activity": "eating_snack", "seconds": 1200, "snack": CHIPS}, "i1")
        self.assertEqual(r["status"], "running")
        self.clock.advance(120)
        got = self.l.k.submit({"idempotency_key": "i2", "principal": "routine", "actor_id": "wanda", "origin": "routine",
                               "action_type": "move", "args": {"to": "kitchen"}, "interrupt": True})
        self.assertIn(got["status"], ("running", "accepted"))
        self.finish(1200)
        self.assertEqual(self.qty(CHIPS), 6)
        with self.s.read() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM meal_consumption WHERE action_id=?",
                                        (r["action_id"],)).fetchone())

    def test_a_snack_together_each_has_one(self):
        """review P2: an invitation to eating_snack; each member who joins has her own snack (before, all refused)."""
        start = self.clock.now() + 1200
        plan = {"plan_id": "plan-1", "kind": "activity", "state": "confirmed", "location": "living",
                "start_at": start, "end_at": start + 900, "what": "eating_snack"}
        self.l.plans.upcoming = lambda a, within: [plan]
        self.l.plans.set_state = lambda pid, state: plan.update(state=state)
        for _ in range(40):
            for a in ("wanda", "mina"):
                if self.l.k.actor(a)["action_id"] is None:
                    self.l._join_plans(a, self.clock.now())
            self.clock.advance(60)
            self.l.k.tick()
        with self.s.read() as c:
            rows = c.execute("SELECT actor_id, state, json_extract(args,'$.snack') FROM actions"
                             " WHERE json_extract(args,'$.party')='plan-1'").fetchall()
        self.assertEqual(sorted(r[0] for r in rows), ["mina", "wanda"])
        self.assertTrue(all(r[1] == "succeeded" and r[2] for r in rows), [tuple(r) for r in rows])
        self.l.meals.sync()
        with self.s.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM meal_consumption WHERE kind='snack'").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
