"""Regressions for 奈尔's mechanism review."""
import json
import os
import tempfile
import unittest

from worldkernel import materials, views
from worldkernel.budget import Budget
from worldkernel.cognition import Cognition, FakeModel
from worldkernel.guide import Guide
from worldkernel.habits import HabitBook
from worldkernel.kernel import Kernel
from worldkernel.routine import Routine
import sqlite3

from worldkernel.store import LockHeld, ReadOnlyTx, Store, VirtualClock

from test_kernel import FIXTURE, ROSTER, Base

DINNER = 1792832400.0 + 300  # 18:05 JST


def count(store, table):
    return store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class FencingTests(Base):
    """once a successor took the world, the old scheduler can write nothing at all."""

    def takeover(self):
        other = Store(self.path, self.clock, holder="successor")
        self.addCleanup(other.close)
        self.clock.advance(Store.LOCK_LEASE + 1)
        other.acquire()
        return other

    def test_every_write_path_of_the_stale_scheduler_is_refused(self):
        moving = self.cmd("nell", "move", args={"to": "living"})["action_id"]
        self.takeover()
        events, effects = count(self.store, "events"), count(self.store, "effects")
        attempts = [lambda: self.cmd("nell", "say", args={"text": "stale leader wrote"}),
                    lambda: self.k.tick(),
                    lambda: self.k.cancel("nell", moving, "channel:nell"),
                    lambda: materials.observe(self.k, "nell", "x", "public", "t"),
                    lambda: Guide(self.store, kernel=self.k).offer_now("dinner"),
                    lambda: Budget(self.store).reserve("nell", "x"),
                    lambda: self.k.audit("x")]
        for attempt in attempts:
            with self.assertRaises(LockHeld):
                attempt()
        self.assertEqual((count(self.store, "events"), count(self.store, "effects")), (events, effects))
        self.assertEqual(self.k.actor("nell")["location"], "kitchen")  # reads keep working
        with self.assertRaises(LockHeld):  # and it stays out even if it tries a write later
            self.cmd("wanda", "say", args={"text": "again"})

    def test_lapsed_lease_without_successor_is_still_ours_and_writes_renew_it(self):
        self.clock.advance(Store.LOCK_LEASE * 10)
        self.assertEqual(self.cmd("nell", "say", args={"text": "still here"})["status"], "succeeded")
        other = Store(self.path, self.clock, holder="successor")
        self.addCleanup(other.close)
        with self.assertRaises(LockHeld):  # the write just renewed the lease
            other.acquire()

    def test_release_then_successor(self):
        self.store.release()
        other = Store(self.path, self.clock, holder="successor")
        self.addCleanup(other.close)
        other.acquire()
        with self.assertRaises(LockHeld):
            self.k.tick()


class ReadBoundaryTests(Base):
    """a read() boundary is never a fenced writer, whoever holds the lock."""

    takeover = FencingTests.takeover

    def writes(self, moving):
        return [lambda: self.cmd("nell", "say", args={"text": "written under read"}),
                lambda: self.k.tick(),
                lambda: self.k.cancel("nell", moving, "channel:nell"),
                lambda: materials.observe(self.k, "nell", "x", "public", "t"),
                lambda: Guide(self.store, kernel=self.k).offer_now("dinner"),
                lambda: Budget(self.store).reserve("nell", "x"),
                lambda: self.k.audit("x")]

    def tables(self):
        return [count(self.store, t) for t in ("events", "effects", "materials", "commands", "audits")]

    def check_refused_inside_read(self, moving):
        before = self.tables()
        for attempt in self.writes(moving):
            with self.assertRaises(ReadOnlyTx):
                with self.store.read():
                    self.k.actor("nell")
                    attempt()
        self.assertEqual(self.tables(), before)

    def test_stale_scheduler_cannot_write_through_a_read(self):
        moving = self.cmd("nell", "move", args={"to": "living"})["action_id"]
        self.takeover()
        self.check_refused_inside_read(moving)
        with self.store.read():  # plain reads still work
            self.assertEqual(self.k.actor("nell")["location"], "kitchen")

    def test_even_the_live_scheduler_does_not_upgrade_a_read(self):
        moving = self.cmd("nell", "move", args={"to": "living"})["action_id"]
        self.check_refused_inside_read(moving)
        self.assertEqual(self.cmd("nell", "say", args={"text": "own tx"})["status"], "succeeded")  # not stuck

    def test_raw_write_under_read_is_refused_by_sqlite(self):
        before = self.tables()
        with self.assertRaises(sqlite3.OperationalError):
            with self.store.read() as db:
                db.execute("INSERT INTO audits(kind, detail, at, runtime_epoch) VALUES('raw', '{}', 0, 1)")
        self.assertEqual(self.tables(), before)
        self.store.db.execute("SELECT 1")
        self.assertEqual(self.store.db.execute("PRAGMA query_only").fetchone()[0], 0)

    def test_reads_inside_a_fenced_write_still_join_it(self):
        with self.store.tx():
            with self.store.read():
                self.k.actor("nell")
            materials.observe(self.k, "nell", "joined", "public", "t")
        self.assertTrue(self.store.db.execute("SELECT 1 FROM materials WHERE source_ref='joined'").fetchone())


class DisclosureTests(Base):
    """knowing is not saying; a model's line is written for its audience from what it may hear."""

    SECRET = "CANARY-R51-02-OWNER-PRIVATE"  # gitleaks:allow (test canary, not a secret)

    def setUp(self):
        super().setUp()
        self.cmd("wanda", "move", args={"to": "kitchen"})
        self.run_for(8)
        materials.observe(self.k, "nell", "op1", "owner_private", self.SECRET)
        materials.observe(self.k, "nell", "pub1", "public", "冰箱门坏了")
        self.k.owner_presence("avatar", "enter", "kitchen")
        self.express_ctx = []

    def cog(self, steps, line=None):
        def model(ctx):
            if ctx.get("purpose") == "express":
                self.express_ctx.append(ctx)
                return {"text": line(ctx) if line else "晚上好", "tokens": 0}
            return {"steps": steps(ctx), "latency": 0, "tokens": 0}
        c = Cognition(self.k, Budget(self.store), FakeModel(model))
        c.begin("nell", "reply")
        return c, c.run_due()

    def leaked(self, c):
        stream = json.dumps(views.events(self.store, self.k.policy, 0)["events"], ensure_ascii=False)
        heard = json.dumps(c.context("wanda"), ensure_ascii=False)
        return self.SECRET in stream or self.SECRET in heard

    def test_words_from_her_private_deliberation_are_refused(self):
        c, out = self.cog(lambda ctx: [{"action_type": "say", "args": {"text": ctx["materials"][0]["text"]}}])
        self.assertEqual(out[0]["reason"], "say_text_from_deliberation")
        self.assertFalse(self.leaked(c))

    def test_the_expression_call_never_sees_what_the_room_may_not_hear(self):
        c, out = self.cog(lambda ctx: [{"action_type": "say", "args": {}}],
                          line=lambda ctx: " ".join(m["text"] for m in ctx["materials"]) or "嗯")
        self.assertEqual(out[0]["status"], "succeeded")
        self.assertEqual(self.express_ctx[0]["audience"], "room:kitchen")
        self.assertNotIn(self.SECRET, json.dumps(self.express_ctx[0], ensure_ascii=False))
        self.assertFalse(self.leaked(c))
        self.assertIn("冰箱门坏了", json.dumps(c.context("wanda"), ensure_ascii=False))  # public may be said

    def test_about_refs_must_be_disclosable_to_that_audience(self):
        c, out = self.cog(lambda ctx: [{"action_type": "say", "args": {}, "about": ["material:op1"]}])
        self.assertEqual(out[0]["reason"], "about_not_disclosable_here")
        c, out = self.cog(lambda ctx: [{"action_type": "say", "args": {}, "about": ["material:pub1"]}])
        self.assertEqual(out[0]["status"], "succeeded")

    def test_private_word_uses_the_dm_audience(self):
        c, out = self.cog(lambda ctx: [{"action_type": "say", "args": {"to": "wanda"}}])
        self.assertEqual(self.express_ctx[0]["audience"], "dm:nell,wanda")
        ev = views.events(self.store, self.k.policy, 0)["events"]
        self.assertEqual([e["kind"] for e in ev if e["kind"] in ("said", "talked_quietly")], ["talked_quietly"])

    def test_kernel_refuses_a_model_line_without_matching_audience(self):
        base = {"principal": "channel:nell", "actor_id": "nell", "origin": "model", "action_type": "say",
                "args": {"text": "hi"}}
        self.assertEqual(self.k.submit(dict(base, idempotency_key="a"))["reason"], "not_expressed_for_this_audience")
        self.assertEqual(self.k.submit(dict(base, idempotency_key="b", expressed_for="self:nell"))["reason"],
                         "not_expressed_for_this_audience")
        self.assertEqual(self.k.submit(dict(base, idempotency_key="c", expressed_for="room:kitchen"))["status"],
                         "succeeded")


class SafePointTests(Base):
    """the catalog's interrupt policy is not negotiable."""

    def test_model_cannot_loosen_cook(self):
        r = self.cmd("nell", "cook", origin="model", args={"recipe": "rice"}, interrupt_policy="now")
        self.assertEqual(r["reason"], "interrupt_policy_fixed_by_catalog")
        self.assertIsNone(self.k.actor("nell")["action_id"])
        self.assertEqual(self.cmd("nell", "cook", origin="model", args={"recipe": "rice"},
                                  interrupt_policy="at_end")["status"], "running")  # the catalog's own value is fine

    def test_interrupt_during_cooking_waits_for_the_end(self):
        self.cmd("nell", "cook", origin="model", args={"recipe": "rice"})
        r = self.cmd("nell", "move", origin="model", args={"to": "living"}, interrupt=True)
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(self.k.actor("nell")["activity"], "cooking")
        self.run_for(1800)
        self.assertEqual(self.k.actor("nell")["activity"], "walking")


class BodyTests(Base):
    """bodies move with world time through the one integrator, idle or not, never twice."""

    def test_idle_body_drifts_on_tick_and_not_twice(self):
        drift = self.k.V.BASE_DRIFT["hunger"]
        self.run_for(3600)
        self.assertAlmostEqual(self.k.actor("nell")["body"]["hunger"], 70 + drift * 60)
        self.k.tick()
        self.k.settle_bodies()
        self.assertAlmostEqual(self.k.actor("nell")["body"]["hunger"], 70 + drift * 60)

    def test_idle_need_reaches_l0_threshold(self):
        r = Routine(self.k)
        self.assertEqual(r.check("nell"), {})  # 70 < 75
        self.run_for(3600)  # +6
        self.assertEqual(r.check("nell"), {"would_call": "hungry_no_ready_food"})

    def test_rate_switches_at_the_action_boundary(self):
        rest = self.k.V.ACTIONS["rest"]["effects"]["rate"]["energy"]
        base = self.k.V.BASE_DRIFT["energy"]
        self.cmd("nell", "rest", args={"seconds": 600})
        self.run_for(1200)  # 10 minutes resting, then 10 idle
        self.assertAlmostEqual(self.k.actor("nell")["body"]["energy"], 60 + (base + rest) * 10 + base * 10)

    def test_outage_resume_settles_the_gap_once(self):
        stopped = self.clock.now()
        self.store.close()
        self.clock.advance(3600)
        self.store = Store(self.path, self.clock)
        self.store.acquire()
        self.k = Kernel(self.store, ROSTER)
        self.k.resume(stopped)
        self.k.resume(stopped)
        self.assertAlmostEqual(self.k.actor("nell")["body"]["hunger"], 70 + self.k.V.BASE_DRIFT["hunger"] * 60)


class ViewSyncTests(unittest.TestCase):
    """every public R1 field is followed by an R2 delta or declared snapshot-only."""

    def setUp(self):
        self.clock = VirtualClock(DINNER)
        self.store = Store(os.path.join(tempfile.mkdtemp(), "w.db"), self.clock)
        self.store.acquire()
        self.addCleanup(self.store.close)
        self.k = Kernel(self.store, ROSTER)
        self.k.seed(FIXTURE)
        self.g = Guide(self.store, kernel=self.k)

    def snap(self):
        return views.snapshot(self.store, ROSTER, self.k.policy)

    def after(self, snap):
        return views.events(self.store, self.k.policy, snap["cursor"])["events"]

    def test_owner_present_to_unknown_reaches_the_client(self):
        self.k.owner_presence("s", "enter", "kitchen", lease=30)
        mid = self.snap()
        self.clock.advance(31)
        self.k.expire_presence()
        ev = self.after(mid)
        self.assertEqual([e["kind"] for e in ev], ["owner_presence_unknown"])
        self.assertEqual(views.apply_events(mid, ev), views.public_state(self.snap()))
        self.assertEqual(self.snap()["owner"]["state"], "unknown")

    def test_scene_taken_and_expired_reach_the_client(self):
        first = self.snap()
        gid = self.g.run()[0]
        self.k.submit({"idempotency_key": "c", "principal": "channel:nell", "actor_id": "nell", "origin": "self",
                       "action_type": "cook", "args": {"recipe": "omelette"}, "guide_id": gid})
        mid = self.snap()
        self.assertEqual(mid["scenes"][0]["participants"], ["nell"])
        self.clock.advance(1800)
        self.g.run()
        final = self.snap()
        self.assertEqual(views.apply_events(first, self.after(first)), views.public_state(final))
        self.assertEqual(views.apply_events(mid, self.after(mid)), views.public_state(final))
        kinds = [e["kind"] for e in self.after(first)]
        self.assertIn("scene_updated", kinds)

    def test_the_reducer_notices_a_missing_delta(self):
        self.k.owner_presence("s", "enter", "kitchen", lease=30)
        first = self.snap()
        self.clock.advance(31)
        self.k.expire_presence()
        self.assertNotEqual(views.apply_events(first, []), views.public_state(self.snap()))


class HabitSourceTests(Base):
    """the source reference is part of a habit's version."""

    def test_new_profile_ref_is_a_new_version(self):
        book = HabitBook(self.k)
        p = {"actor_id": "nell", "profile_ref": "persona:nell-a", "profile_version": "v1"}
        book.install(p)
        self.assertEqual(book.install(dict(p, profile_ref="persona:nell-b"))["sleep"], ("installed", 2))
        self.assertTrue(all(h["profile_ref"] == "persona:nell-b" and h["habit_version"] == 2
                            for h in book.listing("nell")))
        self.assertEqual([h["profile_ref"] for h in book.history("nell:sleep")], ["persona:nell-a", "persona:nell-b"])

    def test_owner_override_still_wins_over_a_new_ref(self):
        book = HabitBook(self.k)
        p = {"actor_id": "nell", "profile_ref": "persona:nell-a", "profile_version": "v1"}
        book.install(p)
        book.override("cmd:owner", "nell", "rest", "房主改", enabled=False)
        self.assertEqual(book.install(dict(p, profile_ref="persona:nell-b"))["rest"][0], "kept_override")


if __name__ == "__main__":
    unittest.main()
