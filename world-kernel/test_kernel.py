"""K1–K3: store/lock/clock, command pipeline, observers and the game view. Synthetic data only."""
import json
import os
import tempfile
import unittest

from worldkernel import vocab as V
from worldkernel import views
from worldkernel.kernel import Kernel
from worldkernel.store import LockHeld, Store, VirtualClock

ROSTER = ("nell", "elsa", "alma", "mina", "yuki", "wanda")
T0 = 1792800000.0  # a fixed synthetic evening

FIXTURE = {
    "actors": {"nell": {"location": "kitchen", "body": {"energy": 60, "hunger": 70}},
               "wanda": {"location": "living", "body": {"energy": 40, "hunger": 80}}},
    "resources": [
        {"id": "egg", "kind": "food", "location": "kitchen", "qty": 2},
        {"id": "tofu", "kind": "food", "location": "kitchen", "qty": 1},
        {"id": "miso", "kind": "food", "location": "kitchen", "qty": 3},
        {"id": "rice", "kind": "food", "location": "kitchen", "qty": 2},
        {"id": "stove", "kind": "anchor", "location": "kitchen", "qty": 1},
        {"id": "pan", "kind": "anchor", "location": "kitchen", "qty": 1},
        {"id": "pot", "kind": "anchor", "location": "kitchen", "qty": 1},
        {"id": "rice_cooker", "kind": "anchor", "location": "kitchen", "qty": 1},
        {"id": "cake", "kind": "food", "location": "living", "qty": 1},
    ],
}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "world.db")
        self.clock = VirtualClock(T0)
        self.store = Store(self.path, self.clock)
        self.store.acquire()
        self.k = Kernel(self.store, ROSTER)
        self.k.seed(FIXTURE)
        self.n = 0
        self.addCleanup(self.store.close)

    def cmd(self, actor, action, key=None, principal=None, origin="self", **kw):
        self.n += 1
        c = {"idempotency_key": key or f"k{self.n}", "principal": principal or f"channel:{actor}",
             "actor_id": actor, "origin": origin, "action_type": action, "args": kw.pop("args", {})}
        c.update(kw)
        return self.k.submit(c)

    def run_for(self, seconds):
        self.clock.advance(seconds)
        return self.k.tick()

    def view(self, after=None):
        return views.events(self.store, self.k.policy, after)["events"]


class StoreTests(Base):
    def test_second_scheduler_is_refused_and_epoch_moves(self):
        other = Store(self.path, self.clock, holder="other")
        self.addCleanup(other.close)
        with self.assertRaises(LockHeld):
            other.acquire()
        self.store.release()
        self.assertEqual(other.acquire(), 2)

    def test_lock_lease_expires(self):
        other = Store(self.path, self.clock, holder="other")
        self.addCleanup(other.close)
        self.clock.advance(Store.LOCK_LEASE + 1)
        self.assertEqual(other.acquire(), 2)
        with self.assertRaises(LockHeld):
            self.store.renew()

    def test_trial_db_cannot_open_as_live(self):
        with self.assertRaises(RuntimeError):
            Store(self.path, self.clock, synthetic=False)
        with self.assertRaises(RuntimeError):
            Store(self.path, self.clock, world_id="ai-house/live")

    def test_writes_need_the_lock(self):
        s = Store(self.path, self.clock, holder="reader")
        self.addCleanup(s.close)
        with self.assertRaises(LockHeld):
            Kernel(s, ROSTER).submit({"idempotency_key": "x"})

    def test_vocab_is_valid(self):
        self.assertEqual(V.validate(), [])
        self.assertEqual(V.VOCAB_VERSION, "world-catalog/1.0.0-p1a")


class CommandTests(Base):
    def test_idempotent_replay_and_conflict(self):
        a = self.cmd("nell", "wait", key="same", args={"seconds": 60})
        b = self.k.submit({"idempotency_key": "same", "principal": "channel:nell", "actor_id": "nell", "origin": "self",
                           "action_type": "wait", "args": {"seconds": 60}})
        self.assertEqual(a["command_id"], b["command_id"])
        self.assertTrue(b["replayed"])
        c = self.k.submit({"idempotency_key": "same", "principal": "channel:nell", "actor_id": "nell", "origin": "self",
                           "action_type": "wait", "args": {"seconds": 90}})
        self.assertEqual(c["status"], "conflict")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 1)

    def test_replay_after_lost_response_shows_current_state(self):
        a = self.cmd("nell", "wait", key="w", args={"seconds": 30})
        self.run_for(31)
        b = self.k.submit({"idempotency_key": "w", "principal": "channel:nell", "actor_id": "nell", "origin": "self",
                           "action_type": "wait", "args": {"seconds": 30}})
        self.assertEqual((b["command_id"], b["status"]), (a["command_id"], "succeeded"))

    def test_principals(self):
        self.assertEqual(self.cmd("wanda", "wait", principal="channel:nell", args={"seconds": 5})["reason"],
                         "principal_actor_mismatch")
        self.assertEqual(self.cmd("nell", "wait", principal="cmd:owner", args={"seconds": 5})["reason"],
                         "principal_cannot_act_for_housemate")
        self.assertEqual(self.cmd("nell", "wait", principal="routine", origin="self", args={"seconds": 5})["reason"],
                         "origin_not_allowed")
        self.assertEqual(self.cmd("elsa", "wait", args={"seconds": 5})["reason"], "actor_not_simulated")

    def test_expiry_vocab_generation_and_versions(self):
        self.assertEqual(self.cmd("nell", "wait", args={"seconds": 5}, expires_at=T0 - 1)["reason"], "expired")
        self.assertEqual(self.cmd("nell", "fly", args={})["reason"], "unknown_action")
        self.assertEqual(self.cmd("nell", "wait", args={"seconds": 5}, vocab_version="old")["reason"],
                         "vocab_version_mismatch")
        g = self.k.actor("nell")["generation"]
        self.cmd("nell", "wait", args={"seconds": 5})
        self.run_for(6)
        self.assertEqual(self.cmd("nell", "wait", args={"seconds": 5}, generation=g)["reason"], "stale_generation")
        egg_v = self.k.resource("egg")["version"]
        self.store.db.execute("UPDATE resources SET version=version+1 WHERE resource_id='egg'")
        r = self.cmd("nell", "cook", args={"recipe": "omelette"}, relevant_versions={"resource:egg": egg_v})
        self.assertEqual(r["reason"], "stale_version")

    def test_last_eggs_go_to_one_cook_only(self):
        self.cmd("wanda", "move", args={"to": "kitchen"})
        self.run_for(8)
        g = self.k.actor("wanda")["generation"]
        a = self.cmd("nell", "cook", args={"recipe": "omelette"})
        self.assertEqual(a["status"], "running")
        # wanda's model planned with her own unchanged generation, but the eggs are already reserved
        self.run_for(1)
        b = self.cmd("wanda", "cook", args={"recipe": "omelette"}, generation=g, interrupt=False)
        self.assertEqual((b["status"], b["reason"]), ("failed", "resource_unavailable"))
        self.run_for(600)
        self.assertEqual(self.k.resource("egg")["qty"], 0)
        made = self.store.db.execute("SELECT qty FROM resources WHERE item='omelette'").fetchone()[0]
        self.assertEqual(made, 1)
        # settling the same action again changes nothing
        with self.store.tx() as db:
            self.k._complete(db, a["action_id"], self.clock.now())
        self.assertEqual(self.k.resource("egg")["qty"], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resources WHERE item='omelette'").fetchone()[0], 1)
        c = self.cmd("wanda", "cook", args={"recipe": "omelette"})
        self.assertEqual(c["reason"], "guard:missing_ingredient:egg")  # stock is gone, the catalog guard says so first

    def test_cancel_releases_unused_stock(self):
        a = self.cmd("nell", "cook", args={"recipe": "miso_soup"})
        self.run_for(300)
        self.assertEqual(self.k.cancel("nell", a["action_id"], "channel:nell")["status"], "cancelled")
        self.assertEqual(self.k.resource("tofu")["qty"], 1)
        self.assertEqual(self.k.actor("nell")["activity"], "idle")
        self.assertEqual(self.k.command(a["command_id"])["status"], "cancelled")
        self.assertEqual(self.k.cancel("nell", a["action_id"], "channel:wanda")["reason"], "principal_actor_mismatch")
        b = self.cmd("nell", "cook", args={"recipe": "miso_soup"})
        self.assertEqual(b["status"], "running")

    def test_stock_removed_behind_a_reservation_fails_the_action(self):
        a = self.cmd("nell", "cook", args={"recipe": "omelette"})
        self.store.db.execute("UPDATE resources SET qty=0 WHERE resource_id='egg'")  # e.g. an admin correction
        self.run_for(600)
        self.assertEqual(self.k.command(a["command_id"])["status"], "failed")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM resources WHERE item='omelette'").fetchone()[0], 0)

    def test_wrong_room_and_out_of_reach(self):
        self.assertEqual(self.cmd("wanda", "cook", args={"recipe": "rice"})["reason"], "wrong_location")
        self.assertEqual(self.cmd("nell", "eat", args={"item": "cake"})["reason"], "resource_not_found")
        self.assertEqual(self.cmd("nell", "eat", args={"resource_id": "cake", "item": "cake"})["reason"],
                         "resource_out_of_reach")
        self.assertEqual(self.cmd("nell", "move", args={"to": "garden"})["reason"], "no_route")
        self.assertEqual(self.cmd("nell", "cook", args={"recipe": "cake"})["reason"], "unknown_recipe")
        self.assertEqual(self.cmd("nell", "cook", args={})["reason"], "unknown_recipe")

    def test_busy_supersede_and_at_end_queue(self):
        r = self.cmd("wanda", "rest", principal="routine", origin="routine", args={"seconds": 1200})
        self.assertEqual(r["status"], "running")
        own = self.cmd("wanda", "move", args={"to": "kitchen"})
        self.assertEqual(own["status"], "running")  # her own choice replaces the routine one
        self.assertEqual(self.k.command(r["command_id"])["status"], "cancelled")
        self.assertEqual(self.cmd("wanda", "wait", args={"seconds": 5})["reason"], "busy")
        q = self.cmd("wanda", "wait", args={"seconds": 5}, interrupt=True)  # move is at_end: queued
        self.assertEqual(q["status"], "accepted")
        self.run_for(8)
        self.assertEqual(self.k.actor("wanda")["location"], "kitchen")
        self.assertEqual(self.k.command(q["command_id"])["status"], "running")
        self.run_for(5)
        self.assertEqual(self.k.command(q["command_id"])["status"], "succeeded")

    def test_body_integrates_once_and_eat_counts_once(self):
        self.cmd("nell", "rest", args={"seconds": 600})
        self.run_for(600)
        b = self.k.actor("nell")["body"]
        self.assertAlmostEqual(b["energy"], 60 + (0.5 - 0.05) * 10, places=5)
        self.cmd("wanda", "eat", args={"item": "cake"})
        self.run_for(600)
        w = self.k.actor("wanda")["body"]  # 20 idle+eating minutes of drift since seeding
        self.assertAlmostEqual(w["hunger"], 80 + 0.1 * 20 - 60, places=5)
        self.assertEqual(self.k.resource("cake")["qty"], 0)


class CatalogGuardTests(Base):
    """mandatory guards, run on authoritative rows (README §内核接入)."""

    def test_raw_ingredient_is_not_a_meal(self):
        self.assertEqual(self.cmd("nell", "eat", args={"item": "egg"})["reason"], "guard:item_not_ready_food")

    def test_resource_id_must_match_the_item(self):
        r = self.cmd("nell", "eat", args={"item": "cake", "resource_id": "stove"})
        self.assertEqual(r["reason"], "guard:food_missing_mismatched_or_out_of_reach")

    def test_check_needs_a_reachable_resource(self):
        self.assertEqual(self.cmd("nell", "check", args={"resource_id": "cake"})["reason"],
                         "guard:inspection_resource_not_reachable")
        self.assertEqual(self.cmd("nell", "check", args={"resource_id": "stove"})["status"], "running")

    def test_arguments_follow_the_schema(self):
        self.assertEqual(self.cmd("nell", "say", args={"text": "  "})["reason"], "guard:invalid_say_text")
        self.assertEqual(self.cmd("nell", "wait", args={"seconds": 5, "why": "x"})["reason"],
                         "guard:missing_or_unknown_argument")

    def test_settling_rechecks_the_guard(self):
        a = self.cmd("wanda", "eat", args={"item": "cake"})
        self.store.db.execute("UPDATE resources SET location='kitchen' WHERE resource_id='cake'")  # moved away
        self.run_for(600)
        self.assertEqual(self.k.command(a["command_id"])["status"], "failed")
        self.assertEqual(self.k.resource("cake")["qty"], 1)


class PerceptionTests(Base):
    def test_observers_are_fixed_when_it_happens(self):
        self.cmd("wanda", "move", args={"to": "kitchen"})
        self.run_for(8)
        dep = self.store.db.execute("SELECT event_id FROM events WHERE kind='departed'").fetchone()[0]
        arr = self.store.db.execute("SELECT event_id FROM events WHERE kind='arrived'").fetchone()[0]
        obs = lambda e: sorted(r[0] for r in self.store.db.execute(
            "SELECT observer_id FROM observations WHERE event_id=?", (e,)))
        self.assertEqual(obs(dep), ["wanda"])  # nobody else was in the living room
        self.assertEqual(obs(arr), ["nell", "wanda"])
        self.cmd("nell", "move", args={"to": "living"})
        self.run_for(8)
        self.assertEqual(obs(arr), ["nell", "wanda"])  # later moves never rewrite who saw it

    def test_private_words_and_the_owner_view(self):
        self.assertEqual(self.k.owner_presence("s1", "enter", "kitchen", principal="channel:nell")["reason"],
                         "owner_only")
        self.cmd("nell", "say", args={"text": "冰箱里只剩两个蛋了"})
        self.assertEqual([e for e in self.view() if e["kind"] == "said"], [])  # owner not there: nothing shown
        self.k.owner_presence("s1", "enter", "kitchen")
        self.cmd("wanda", "move", args={"to": "kitchen"})
        self.run_for(8)
        self.cmd("nell", "say", args={"text": "晚饭吃什么"})
        self.cmd("nell", "say", args={"text": "SECRET-CANARY-1", "to": "wanda"})
        said = [e for e in self.view() if e["kind"] in ("said", "talked_quietly")]
        self.assertEqual([(e["kind"], e.get("text")) for e in said], [("said", "晚饭吃什么"), ("talked_quietly", None)])
        self.assertNotIn("SECRET-CANARY-1", json.dumps(self.view(), ensure_ascii=False))
        ev = self.store.db.execute("SELECT knowers, disclosure_scope FROM events WHERE kind='said' ORDER BY event_id DESC"
                                   " LIMIT 1").fetchone()
        self.assertEqual((json.loads(ev[0]), ev[1]), (["nell", "wanda"], "dm:nell,wanda"))
        self.assertEqual(self.cmd("nell", "say", args={"text": "hi", "to": "elsa"})["reason"], "addressee_not_present")

    def test_view_never_shows_stock_or_reasons(self):
        self.cmd("nell", "cook", args={"recipe": "omelette"})
        self.run_for(600)
        dump = json.dumps(self.view(), ensure_ascii=False)
        self.assertNotIn("egg", dump)
        self.assertNotIn("qty", dump)

    def test_presence_lease_runs_out_as_unknown(self):
        self.k.owner_presence("s1", "enter", "living", lease=30)
        self.clock.advance(31)
        self.k.expire_presence()
        snap = views.snapshot(self.store, ROSTER, self.k.policy)
        self.assertEqual(snap["owner"]["state"], "unknown")
        kinds = [e["kind"] for e in self.view()]
        self.assertNotIn("owner_exited_avatar", kinds)


class ViewTests(Base):
    def test_snapshot_and_events_continue_from_one_boundary(self):
        self.cmd("wanda", "move", args={"to": "kitchen"})
        snap = views.snapshot(self.store, ROSTER, self.k.policy)
        self.assertEqual(set(snap["actors"]), set(ROSTER))
        self.assertIsNone(snap["actors"]["elsa"])
        self.assertIn("elsa", snap["not_simulated"])
        self.assertEqual(snap["actors"]["wanda"]["activity"], "walking")
        self.run_for(8)
        got = views.events(self.store, self.k.policy, snap["cursor"])
        self.assertEqual([e["kind"] for e in got["events"]], ["departed", "arrived"])
        self.assertEqual(got["events"][0]["seq"], snap["view_revision"] + 1)
        again = views.events(self.store, self.k.policy, got["cursor"])
        self.assertEqual(again["events"], [])

    def test_cursor_from_another_world_or_epoch(self):
        snap = views.snapshot(self.store, ROSTER, self.k.policy)
        with self.assertRaises(views.CursorMismatch):
            views.events(self.store, self.k.policy, snap["cursor"].replace("ai-house/p1a", "ai-house/live"))
        self.store.set_meta("stream_epoch", 2)
        with self.assertRaises(views.Gone):  # an older epoch (e.g. after a restore) means: snapshot again
            views.events(self.store, self.k.policy, snap["cursor"])

    def test_expired_cursor_is_gone_and_seq_never_reused(self):
        self.cmd("nell", "wait", args={"seconds": 5})
        self.run_for(5)
        old = views.snapshot(self.store, ROSTER, self.k.policy)["view_revision"]
        self.clock.advance(7 * 86400)
        views.purge(self.store, "game", self.clock.now())
        with self.assertRaises(views.Gone):
            views.events(self.store, self.k.policy, 0)
        self.cmd("nell", "wait", args={"seconds": 5})
        new = views.events(self.store, self.k.policy, old)["events"]
        self.assertEqual(new[0]["seq"], old + 1)

    def test_limit_is_capped(self):
        for i in range(30):
            self.cmd("nell", "wait", args={"seconds": 1})
            self.run_for(1)
        self.assertEqual(len(views.events(self.store, self.k.policy, 0, limit=500)["events"]), 50)


if __name__ == "__main__":
    unittest.main()
