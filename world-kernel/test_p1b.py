"""P1b shadow catalog: the bigger house, begin_activity, own bedrooms, ready food, and the
shadow runner on it. The P1a vocabulary and every P1a test stay as they were."""
import hashlib
import json
import os
import tempfile
import unittest

import shadow as SH
from test_shadow import REALITY, ROUTINE, SELVES, T0, fake_world
from worldkernel import vocab as V1
from worldkernel import vocab_p1b as V
from worldkernel.budget import Budget
from worldkernel.cognition import Cognition
from worldkernel.kernel import Kernel
from worldkernel.routine import Routine
from worldkernel.store import Store, VirtualClock

ROSTER = SH.ROSTER
FIXTURE = {
    "actors": {"nell": {"location": "living", "body": {"energy": 60, "hunger": 30}},
               "elsa": {"location": "living", "body": {"energy": 60, "hunger": 30}},
               "wanda": {"location": "kitchen", "body": {"energy": 60, "hunger": 90}}},
    "resources": [
        {"id": "bread", "kind": "food", "location": "kitchen", "qty": 2},
        {"id": "bathtub", "kind": "anchor", "location": "bath", "qty": 1},
    ],
}


class Fixture(unittest.TestCase):
    """Helpers only (no tests), so other fixtures can reuse them without rerunning P1bKernel."""
    fixture = FIXTURE

    def setUp(self):
        d = tempfile.mkdtemp()
        self.clock = VirtualClock(1792800000.0)
        self.store = Store(os.path.join(d, "w.db"), self.clock)
        self.store.acquire()
        self.addCleanup(self.store.close)
        self.k = Kernel(self.store, ROSTER, vocab=V)
        self.k.seed(self.fixture)
        self.n = 0

    def cmd(self, actor, action, **args):
        self.n += 1
        return self.k.submit({"idempotency_key": f"k{self.n}", "principal": f"channel:{actor}", "actor_id": actor,
                              "origin": "self", "action_type": action, "args": args})

    def go(self, actor, to):
        r = self.cmd(actor, "move", to=to)
        self.assertEqual(r["status"], "running", r)
        self.run_for(120)
        self.assertEqual(self.k.actor(actor)["location"], to)

    def run_for(self, seconds):
        self.clock.advance(seconds)
        return self.k.tick()


class P1bKernel(Fixture):
    def test_profile_is_valid_and_p1a_is_untouched(self):
        self.assertEqual(V.validate(), [])
        self.assertEqual(V1.VOCAB_VERSION, "world-catalog/1.0.0-p1a")
        self.assertEqual(set(V1.LOCATIONS), {"kitchen", "living", "exit_rooms"})
        self.assertNotIn("begin_activity", V1.ACTIONS)
        self.assertEqual(V.public_location("room_nell"), "room")
        self.assertEqual(V.public_location("away"), "away")
        self.assertIn("bread", V.EDIBLE)
        self.assertNotIn("bread", V1.EDIBLE)

    def test_she_sleeps_in_her_own_bedroom_and_nobody_walks_into_another(self):
        r = self.cmd("nell", "sleep", seconds=600)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(self.cmd("nell", "move", to="room_elsa")["reason"], "guard:someone_elses_bedroom")
        self.go("nell", "room_nell")
        self.assertEqual(self.cmd("nell", "sleep", seconds=600)["status"], "running")
        self.assertEqual(self.k.actor("nell")["activity"], "sleeping")

    def test_an_activity_runs_only_where_it_may_and_names_itself(self):
        r = self.cmd("nell", "begin_activity", activity="reading", seconds=600)  # living is fine for reading
        self.assertEqual(r["status"], "running", r)
        self.assertEqual(self.k.actor("nell")["activity"], "reading")
        self.run_for(601)
        self.assertEqual(self.k.actor("nell")["activity"], "idle")
        self.assertEqual(self.cmd("nell", "begin_activity", activity="working", seconds=600)["reason"],
                         "guard:activity_not_allowed_here")
        self.assertEqual(self.cmd("nell", "begin_activity", activity="browsing")["reason"], "activity_not_enabled")
        self.assertEqual(self.cmd("nell", "begin_activity", activity="reading", seconds=99999)["status"], "failed")
        self.go("nell", "study")
        self.assertEqual(self.cmd("nell", "begin_activity", activity="working")["status"], "running")

    def test_activity_effects_and_interrupt_policy_come_from_the_catalog(self):
        self.go("nell", "yard")
        e0 = self.k.actor("nell")["body"]["energy"]
        r = self.cmd("nell", "begin_activity", activity="chores", seconds=1200)
        self.assertEqual(r["status"], "running")
        self.assertEqual(self.store.db.execute("SELECT interrupt_policy FROM actions WHERE action_id=?",
                                               (r["action_id"],)).fetchone()[0], "at_end")
        self.run_for(1200)
        drop = e0 - self.k.actor("nell")["body"]["energy"]
        self.assertAlmostEqual(drop, (0.05 + 0.1) * 20, places=3)  # base drift + the chores rate, 20 minutes

    def test_the_bath_takes_one_at_a_time(self):
        self.go("nell", "bath")
        self.go("elsa", "bath")
        self.assertEqual(self.cmd("nell", "begin_activity", activity="bathing", seconds=600)["status"], "running")
        r = self.cmd("elsa", "begin_activity", activity="bathing", seconds=600)
        self.assertEqual((r["status"], r["reason"]), ("failed", "resource_unavailable"))
        self.run_for(601)
        self.assertEqual(self.cmd("elsa", "begin_activity", activity="bathing", seconds=600)["status"], "running")

    def test_ready_food_needs_no_cook_and_l0_eats_it(self):
        got = Routine(self.k).check("wanda")
        self.assertEqual(got.get("action"), "eat")
        self.run_for(601)
        self.assertLess(self.k.actor("wanda")["body"]["hunger"], 40)
        self.assertEqual(self.k.resource("bread")["qty"], 1)

    def test_routine_walks_her_to_her_own_bed(self):
        r = Routine(self.k, habits={"nell": {"sleep_jst": (0, 1439)}})
        self.assertEqual(r.check("nell")["result"]["status"], "running")
        self.run_for(30)
        self.assertEqual(self.k.actor("nell")["location"], "room_nell")
        self.assertEqual(r.check("nell")["action"], "sleep")

    def test_malformed_activity_from_the_model_is_refused_and_she_can_decide_again(self):
        # nell's review: a list as activity raised TypeError and left her plan stranded (busy forever)
        answers = ['{"steps":[{"action_type":"begin_activity","args":{"activity":["reading"]}}]}',
                   '{"steps":[{"action_type":"begin_activity","args":{"activity":{"k":"reading"}}}]}',
                   '{"steps":[{"action_type":"begin_activity","args":["reading"]}]}',
                   '{"steps":[{"action_type":"begin_activity","args":{"activity":"reading","seconds":600}}]}']
        c = Cognition(self.k, Budget(self.store), SH.LiveModel(lambda s, u: (answers.pop(0), 1), {}, profile="p1b"))
        for want in ("bad_activity", "bad_activity", "bad_args"):
            c.begin("nell", "probe")
            r = c.run_due()
            self.assertEqual((r[0]["status"], r[0]["reason"]), ("failed", want))
            self.assertNotIn("nell", c.plans)
        self.assertEqual(V.guard("begin_activity", {"activity": ["reading"]}, {"actor_id": "nell", "location": "living"}),
                         ["invalid_argument_type:activity"])
        c.begin("nell", "probe")
        self.assertEqual(c.run_due()[0]["status"], "running")
        self.assertEqual(self.k.actor("nell")["activity"], "reading")

    def test_solo_gaming_needs_the_console(self):
        # the catalog's solo variant requires game_console; this fixture has none
        self.assertEqual(V.ACTIVITIES["gaming"]["hold"], ("game_console",))
        r = self.cmd("nell", "begin_activity", activity="gaming", seconds=600)
        self.assertEqual(r["status"], "failed")


class P1bConsole(Fixture):
    fixture = dict(FIXTURE, resources=FIXTURE["resources"] + [
        {"id": "game_console", "kind": "anchor", "location": "living", "qty": 1}])

    def test_solo_gaming_holds_the_console_one_player_at_a_time(self):
        self.assertEqual(self.cmd("nell", "begin_activity", activity="gaming", seconds=600)["status"], "running")
        r = self.cmd("elsa", "begin_activity", activity="gaming", seconds=600)
        self.assertEqual((r["status"], r["reason"]), ("failed", "resource_unavailable"))
        self.run_for(601)
        self.assertEqual(self.cmd("elsa", "begin_activity", activity="gaming", seconds=600)["status"], "running")
        self.go("nell", "room_nell")
        self.assertEqual(self.cmd("nell", "begin_activity", activity="gaming", seconds=600)["status"], "failed")


class Chat:
    """A sensible housemate on the P1b house: eat what is ready, else cook, else go to the kitchen."""

    def __init__(self):
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        if "你决定对" in user:
            return '{"text": "一起吃点吗？"}', 20
        seen = json.loads(user.split("\n", 1)[1].split("\n让你想一想", 1)[0])
        food = {i["item"]: i["resource_id"] for i in seen["items_here"]}
        for item in ("omelette", "bread", "onigiri", "fruit"):
            if item in food:
                return json.dumps({"steps": [{"action_type": "eat", "args": {"item": item, "resource_id": food[item]}},
                                             {"action_type": "say", "args": {}, "about": ["早饭"]}]}), 30
        if seen["location"] == "kitchen" and "egg" in food:
            return json.dumps({"steps": [{"action_type": "cook", "args": {"recipe": "omelette"}, "uses": ["egg"]}]}), 30
        if seen["location"] != "kitchen":
            return json.dumps({"steps": [{"action_type": "move", "args": {"to": "kitchen"}}]}), 30
        return '{"steps": []}', 10


class P1bShadow(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.world = os.path.join(self.dir, "world.db")
        fake_world(self.world)
        self.clock = VirtualClock(T0)

    def make(self, chat):
        k, s = SH.PROFILES["p1b"]["files"]
        sh = SH.Shadow(os.path.join(self.dir, k), os.path.join(self.dir, s), self.world, ROUTINE, SELVES, chat,
                       clock=self.clock, profile="p1b")
        self.addCleanup(sh.store.close)
        return sh

    def run_for(self, sh, hours, step=60):
        for _ in range(int(hours * 3600 // step)):
            sh.step()
            self.clock.advance(step)

    def test_genesis_keeps_the_real_places(self):
        g = SH.genesis({**{a: {"location": r[0]} for a, r in REALITY.items()}, "nell": {"location": "shop"}}, "p1b")
        where = {a: v["location"] for a, v in g["actors"].items()}
        self.assertEqual(where, {"nell": "away", "elsa": "study", "alma": "room_alma", "mina": "room_mina",
                                 "yuki": "room_yuki", "wanda": "living"})
        ids = {r["id"]: r["location"] for r in g["resources"]}
        self.assertEqual((ids["bread"], ids["bathtub"]), ("kitchen", "bath"))

    def test_a_day_on_the_bigger_house(self):
        with open(self.world, "rb") as f:
            before = hashlib.sha256(f.read()).hexdigest()
        chat = Chat()
        sh = self.make(chat)
        self.run_for(sh, 24, step=120)
        with open(self.world, "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), before)
        db = sh.store.db
        self.assertEqual(sh.store.world_id, "ai-house-shadow-2")
        rules = {r[0] for r in db.execute("SELECT rule_key FROM scene_offers")}
        self.assertTrue({"breakfast", "lunch", "dinner"} <= rules, rules)
        beds = {r[0]: r[1] for r in db.execute("SELECT actor_id, json_extract(args,'$.seconds') FROM actions"
                                               " WHERE action_type='sleep'")}
        self.assertEqual(set(beds), set(ROSTER))
        slept_in = {r[0] for r in db.execute("SELECT DISTINCT e.location FROM events e JOIN actions a ON"
                                             " a.action_id=e.action_id WHERE a.action_type='sleep'")}
        self.assertTrue(slept_in <= {f"room_{a}" for a in ROSTER}, slept_in)
        ate = {r[0] for r in db.execute("SELECT DISTINCT json_extract(args,'$.item') FROM actions WHERE"
                                        " action_type='eat' AND state='succeeded'")}
        self.assertTrue(ate & {"bread", "onigiri", "fruit"}, ate)
        refused = db.execute("SELECT COUNT(*) FROM decision_points WHERE reason LIKE 'say_refused:about%'").fetchone()[0]
        self.assertEqual(refused, 0)  # a free-word topic is dropped, not a reason to lose the line
        counted = db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0]
        self.assertLessEqual(counted, SH.LIMITS["per_day"])
        text = SH.report(self.dir, "2026-10-25", "p1b")
        self.assertIn("目录 p1b", text)
        self.assertNotIn("| nell | 0 |", text)

    def test_the_prompt_names_her_own_room_only(self):
        fmt = SH.decide_format("p1b", "elsa")
        self.assertIn("room_elsa", fmt)
        self.assertNotIn("room_nell", fmt)
        self.assertIn("begin_activity", fmt)
        self.assertEqual(SH.decide_format("p1a", "elsa"), SH.DECIDE_FORMAT)

    def test_comparison_precision(self):
        self.assertEqual(SH.legacy_public("p1b", "station"), "away")
        self.assertEqual(SH.legacy_public("p1b", "room"), SH.public("p1b", "room_wanda"))
        self.assertEqual(SH.legacy_public("p1a", "study"), "exit_rooms")


if __name__ == "__main__":
    unittest.main()
