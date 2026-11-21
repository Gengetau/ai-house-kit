"""K4: Guide scene offers and the L0 routine. Synthetic data only."""
import json
import os
import tempfile
import unittest

from worldkernel import vocab as V
from worldkernel.guide import Guide
from worldkernel.habits import HabitBook, HabitError
from worldkernel.kernel import Kernel
from worldkernel.routine import Routine
from worldkernel.store import Store, VirtualClock

from test_kernel import FIXTURE, ROSTER

DINNER = 1792832400.0 + 300  # 2026-10-24 18:05 JST
NIGHT = 1792832400.0 + 5 * 3600 + 1800  # 23:30 JST


class Base(unittest.TestCase):
    start = DINNER

    def setUp(self):
        self.clock = VirtualClock(self.start)
        self.store = Store(os.path.join(tempfile.mkdtemp(), "w.db"), self.clock)
        self.store.acquire()
        self.k = Kernel(self.store, ROSTER)
        self.k.seed(FIXTURE)
        self.g = Guide(self.store)
        self.r = Routine(self.k)
        self.addCleanup(self.store.close)

    def own(self, actor, action, key, **kw):
        return self.k.submit({"idempotency_key": key, "principal": f"channel:{actor}", "actor_id": actor,
                              "origin": "self", "action_type": action, "args": kw.pop("args", {}), **kw})

    def offers(self):
        return self.store.db.execute("SELECT * FROM scene_offers ORDER BY created_at").fetchall()


class GuideTests(Base):
    def test_one_offer_per_scene_per_slot(self):
        made = self.g.run()
        self.assertEqual(len(made), 1)
        o = self.offers()[0]
        self.assertEqual((o["location"], o["state"], o["rule_version"]), ("kitchen", "offered", "guide/p1a-1"))
        self.assertEqual(o["expires_at"] - o["window_start"], 1800)
        self.clock.advance(600)
        self.assertEqual(self.g.run(), [])  # still live: no repeat
        self.clock.advance(1200)  # 18:35, next slot
        self.assertEqual(len(self.g.run()), 1)
        self.assertEqual([r["state"] for r in self.offers()], ["expired", "offered"])

    def test_offer_names_nobody_and_needs_her_own_look(self):
        self.g.run()
        o = self.g.offers("nell")[0]
        self.assertEqual(o["constraints"], {"check_yourself": ["fridge"], "invite_separately": True})
        text = json.dumps(o, ensure_ascii=False)
        for name in ROSTER + ("owner",):
            self.assertNotIn(name, text)
        self.assertEqual(self.g.offers("elsa"), [])  # not simulated

    def test_taking_marks_partial_then_used(self):
        gid = self.g.run()[0]
        r = self.own("nell", "cook", "c1", args={"recipe": "omelette"}, guide_id=gid)
        self.assertEqual(r["status"], "running")
        o = self.offers()[0]
        self.assertEqual((o["state"], json.loads(o["takers"])), ("partially_used", ["nell"]))
        # wanda does not take it; nobody is added for her
        self.clock.advance(1800)
        self.g.run()
        self.assertEqual(self.offers()[0]["state"], "used")
        late = self.own("nell", "wait", "w1", args={"seconds": 5}, guide_id=gid)
        self.assertEqual(late["reason"], "guide_not_live")

    def test_failed_action_does_not_count_as_taking(self):
        gid = self.g.run()[0]
        r = self.own("wanda", "cook", "c2", args={"recipe": "rice"}, guide_id=gid)  # she is in the living room
        self.assertEqual(r["reason"], "wrong_location")
        self.assertEqual(self.offers()[0]["state"], "offered")

    def test_owner_entering_refreshes_only_after_quiet_gap(self):
        self.g.run()
        self.g.withdraw(self.offers()[0]["guide_id"], "test")
        self.assertEqual(self.g.run([("owner_entered", "kitchen", "ev-1")]), [])  # within 10 min of the last
        self.clock.advance(600)
        made = self.g.run([("owner_entered", "kitchen", "ev-2")])
        self.assertEqual(len(made), 1)
        self.assertEqual(json.loads(self.offers()[-1]["source_refs"]), [{"kind": "owner_entered", "ref": "ev-2"}])

    def test_outside_the_window_nothing(self):
        self.clock.advance(5 * 3600)  # 23:05
        self.assertEqual(self.g.run(), [])


class RoutineTests(Base):
    def test_hungry_with_ready_food_eats_and_her_choice_waits_for_the_safe_point(self):
        r = self.r.check("wanda")  # hunger 80, cake in the living room
        self.assertEqual(r["action"], "eat")
        act = self.k.actor("wanda")["action_id"]
        own = self.own("wanda", "move", "m1", args={"to": "kitchen"})
        self.assertEqual(own["status"], "accepted")  # eating is at_end: her choice comes right after the meal
        self.clock.advance(own["starts_at"] - self.clock.now())
        self.k.tick()
        row = self.store.db.execute("SELECT state, origin FROM actions WHERE action_id=?", (act,)).fetchone()
        self.assertEqual(tuple(row), ("succeeded", "routine"))
        self.assertEqual(self.k.actor("wanda")["activity"], "walking")

    def test_her_choice_replaces_an_interruptible_routine_step_at_once(self):
        self.store.db.execute("UPDATE actors SET body=json_set(body,'$.energy',10,'$.hunger',10) WHERE actor_id='nell'")
        self.assertEqual(self.r.check("nell")["action"], "rest")  # rest is interrupt_policy=now
        act = self.k.actor("nell")["action_id"]
        self.assertEqual(self.own("nell", "move", "m2", args={"to": "living"})["status"], "running")
        row = self.store.db.execute("SELECT state, reason FROM actions WHERE action_id=?", (act,)).fetchone()
        self.assertEqual(tuple(row), ("cancelled", "superseded"))

    def test_hungry_without_ready_food_is_a_would_call(self):
        self.store.db.execute("UPDATE actors SET body=json_set(body,'$.hunger',90) WHERE actor_id='nell'")
        self.assertEqual(self.r.check("nell"), {"would_call": "hungry_no_ready_food"})  # eggs are not a meal
        p = self.store.db.execute("SELECT kind, outcome FROM decision_points WHERE actor_id='nell'").fetchone()
        self.assertEqual(tuple(p), ("hunger", "would_call"))
        self.assertIsNone(self.k.actor("nell")["action_id"])  # routine never cooks for her

    def test_busy_or_fine_means_nothing(self):
        self.own("wanda", "wait", "w", args={"seconds": 60})
        self.assertEqual(self.r.check("wanda"), {})
        self.assertEqual(self.r.check("nell"), {})  # hunger 70 < 75, energy 60
        self.assertEqual(self.r.check("elsa"), {})

    def test_same_minute_check_is_idempotent(self):
        self.r.check("wanda")
        self.k.cancel("wanda", self.k.actor("wanda")["action_id"], "channel:wanda")
        again = self.r.check("wanda")
        self.assertTrue(again["result"]["replayed"])  # same routine key within the minute: no second meal

    def test_tired_rests(self):
        self.store.db.execute("UPDATE actors SET body=json_set(body,'$.energy',10,'$.hunger',10)"
                              " WHERE actor_id='nell'")
        self.assertEqual(self.r.check("nell")["action"], "rest")


NELL = {"actor_id": "nell", "profile_ref": "persona:nell", "profile_version": "v2-20261020", "sleep": "23:30-07:30",
       "hunger_eat_at": 65, "rest_at_energy": 25}


class HabitTests(Base):
    """v1.1.1 F3: habits derived from the persona, versioned, visible, owner-overridable."""
    def setUp(self):
        super().setUp()
        self.book = HabitBook(self.k)
        self.r = Routine(self.k, book=self.book)

    def test_derived_habits_carry_their_sources_and_drive_l0(self):
        self.assertEqual({k: v[0] for k, v in self.book.install(NELL).items()},
                         {"sleep": "installed", "eat_ready_food": "installed", "rest": "installed"})
        rows = {r["kind"]: r for r in self.book.listing("nell")}
        self.assertEqual(rows["sleep"]["time_window"], [23 * 60 + 30, 7 * 60 + 30])
        for r in rows.values():
            self.assertEqual((r["profile_ref"], r["profile_version"], r["derivation_rule_version"], r["habit_version"]),
                             ("persona:nell", "v2-20261020", "habits-derive/p1a-1", 1))
            self.assertIsNone(r["override_source"])
        self.assertNotIn("persona text", json.dumps(rows))  # only the reference is kept
        self.store.db.execute("UPDATE actors SET body=json_set(body,'$.hunger',66) WHERE actor_id='wanda'")
        self.book.install(dict(NELL, actor_id="wanda", profile_ref="persona:wanda"))
        r = self.r.check("wanda")  # 66 >= her 65: the persona threshold, not the default 75
        self.assertEqual(r["action"], "eat")
        self.assertIn("wanda:eat_ready_food@1", self.r.habit("wanda")["refs"])

    def test_same_profile_is_unchanged_new_version_replaces(self):
        self.book.install(NELL)
        self.assertEqual(self.book.install(NELL)["sleep"], ("unchanged", 1))
        self.assertEqual(self.book.install(dict(NELL, profile_version="v3", sleep="00:00-08:00"))["sleep"],
                         ("installed", 2))
        self.assertEqual(len(self.book.history("nell:sleep")), 2)
        self.assertEqual(self.book.listing("nell")[2]["time_window"], [0, 480])

    def test_bad_profile_writes_nothing(self):
        for bad in ({"sleep": "25:00-07:00"}, {"hunger_eat_at": 150}, {"rest_seconds": 99999}, {"profile_version": ""}):
            with self.assertRaises(HabitError):
                self.book.install(dict(NELL, **bad))
        self.assertEqual(self.book.listing(), [])

    def test_owner_override_survives_persona_update_until_cleared(self):
        self.book.install(NELL)
        self.assertEqual(self.book.override("channel:nell", "nell", "rest", "x", enabled=False)["reason"], "owner_only")
        self.assertEqual(self.book.override("cmd:owner", "nell", "rest", "房主关掉", enabled=False)["habit_version"], 2)
        self.assertEqual(self.book.install(dict(NELL, profile_version="v3"))["rest"], ("kept_override", 2))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM audits WHERE kind='habit_override_kept'")
                         .fetchone()[0], 1)
        self.store.db.execute("UPDATE actors SET body=json_set(body,'$.energy',10,'$.hunger',10) WHERE actor_id='nell'")
        self.assertEqual(self.r.check("nell"), {})  # disabled: L0 does nothing for it
        self.book.clear_override("cmd:owner", dict(NELL, profile_version="v3"))
        rest = [r for r in self.book.listing("nell") if r["kind"] == "rest"][0]
        self.assertEqual((rest["habit_version"], rest["enabled"], rest["profile_version"]), (3, True, "v3"))
        self.assertEqual(self.r.check("nell")["action"], "rest")

    def test_override_cannot_add_actions_or_unknown_conditions(self):
        self.book.install(NELL)
        r = self.book.override("cmd:owner", "nell", "eat_ready_food", "x", preconditions={"cook_if_hungry": True})
        self.assertEqual(r["reason"], "unknown_precondition:cook_if_hungry")
        with self.assertRaises(HabitError):
            self.book.override("cmd:owner", "nell", "sleep", "x", time_window="7:00-7:00")


class NightTests(Base):
    start = NIGHT

    def test_sleep_route_goes_room_by_room_then_sleeps(self):
        self.assertEqual(self.r.check("nell")["result"]["status"], "running")  # kitchen -> living first
        self.clock.advance(8)
        self.k.tick()
        self.assertEqual(self.k.actor("nell")["location"], "living")
        self.r.check("nell")
        self.clock.advance(12)
        self.k.tick()
        self.assertEqual(self.k.actor("nell")["location"], "exit_rooms")
        s = self.r.check("nell")
        self.assertEqual(s["action"], "sleep")
        self.assertEqual(self.k.actor("nell")["activity"], "sleeping")
        hops = [json.loads(r[0]).get("to") for r in self.store.db.execute(
            "SELECT payload FROM events WHERE actor_id='nell' AND kind='departed' ORDER BY event_id")]
        self.assertEqual(hops, ["living", "exit_rooms"])

    def test_next_hop(self):
        self.assertEqual(V.next_hop("kitchen", "exit_rooms"), "living")
        self.assertIsNone(V.next_hop("kitchen", "kitchen"))


if __name__ == "__main__":
    unittest.main()
