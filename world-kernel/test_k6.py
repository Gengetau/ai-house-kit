"""K6: scoped materials, phone mailbox, owner input, admin correction, live fence, summaries, outage."""
import json
import os
import tempfile
import unittest

from worldkernel import admin, materials, summary, views
from worldkernel.cognition import Cognition, FakeModel
from worldkernel.budget import Budget
from worldkernel.kernel import Kernel
from worldkernel.store import Store, VirtualClock

from test_kernel import FIXTURE, ROSTER, T0


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock(T0)
        self.path = os.path.join(tempfile.mkdtemp(), "w.db")
        self.store = Store(self.path, self.clock, holder="svc")
        self.store.acquire()
        self.k = Kernel(self.store, ROSTER)
        self.k.seed(FIXTURE)
        self.c = Cognition(self.k, Budget(self.store), FakeModel(lambda ctx: {"steps": []}))
        self.addCleanup(lambda: self.store.close())

    def own(self, actor, action, key, **kw):
        return self.k.submit({"idempotency_key": key, "principal": f"channel:{actor}", "actor_id": actor,
                              "origin": "self", "action_type": action, "args": kw.pop("args", {}), **kw})


class MaterialTests(Base):
    def test_scope_rules(self):
        self.assertTrue(materials.allowed("public", "room:kitchen"))
        self.assertFalse(materials.allowed("room:kitchen", "public"))  # room is not public
        self.assertFalse(materials.allowed("dm:nell,wanda", "owner_private"))
        self.assertTrue(materials.allowed("table:g1:seat1", "self:nell"))
        with self.assertRaises(ValueError):
            materials.observe(self.k, "nell", "x", "everyone", "t")

    def test_knowing_is_not_disclosing(self):
        materials.observe(self.k, "nell", "src1", "owner_private", "CANARY-OP")
        self.assertIn("CANARY-OP", json.dumps(self.c.context("nell", "owner_private"), ensure_ascii=False))
        self.assertNotIn("CANARY-OP", json.dumps(self.c.context("nell", "room:kitchen"), ensure_ascii=False))
        self.assertNotIn("CANARY-OP", json.dumps(self.c.context("wanda", "owner_private"), ensure_ascii=False))

    def test_phone_body_waits_for_her_own_reading_at_a_safe_point(self):
        self.own("nell", "cook", "c", args={"recipe": "omelette"})  # at_end: hands busy
        materials.phone_receive(self.k, "nell", "m1", "owner_private", "CANARY-PH")
        self.assertNotIn("CANARY-PH", json.dumps(self.c.context("nell"), ensure_ascii=False))
        self.assertEqual(materials.read_message(self.k, "nell", "m1", "channel:nell")["reason"], "busy_until_safe_point")
        self.assertEqual(materials.read_message(self.k, "nell", "m1", "channel:wanda")["reason"],
                         "principal_actor_mismatch")
        self.clock.advance(600)
        self.k.tick()
        self.assertEqual(materials.read_message(self.k, "nell", "m1", "channel:nell")["status"], "succeeded")
        self.assertIn("CANARY-PH", json.dumps(self.c.context("nell"), ensure_ascii=False))
        self.assertEqual(self.k.actor("nell")["location"], "kitchen")
        self.assertNotIn("CANARY-PH", json.dumps(views.events(self.store, self.k.policy, 0), ensure_ascii=False))


class FixturePhoneTests(Base):
    """w7-fast.1 registers read_phone as a kernel action; the kernel, not the model, decides attention."""
    def setUp(self):
        from worldcatalog.fixtures import fixture_vocabulary
        self.clock = VirtualClock(T0)
        self.store = Store(os.path.join(tempfile.mkdtemp(), "w.db"), self.clock, holder="svc")
        self.store.acquire()
        self.addCleanup(lambda: self.store.close())
        self.V = fixture_vocabulary("w7-fast.1", world_id=self.store.world_id, synthetic=True, fake_channels=True,
                                    paid_calls=False)
        self.k = Kernel(self.store, ROSTER, vocab=self.V)
        self.k.seed(FIXTURE)
        self.c = Cognition(self.k, Budget(self.store), FakeModel(lambda ctx: {"steps": []}))

    def read(self, key, mid="m1", **kw):
        return self.own("nell", "read_phone", key, args={"message_id": mid}, **kw)

    def test_read_phone_waits_for_the_safe_point_and_needs_her_own_mailbox(self):
        self.assertEqual(views.snapshot(self.store, ROSTER, self.k.policy)["vocab_version"], self.V.VOCAB_VERSION)
        self.assertEqual(self.read("r0")["reason"], "guard:message_not_in_actor_fake_mailbox")
        materials.phone_receive(self.k, "nell", "m1", "owner_private", "CANARY-FX")
        self.own("nell", "cook", "c", args={"recipe": "omelette"})
        self.assertEqual(self.read("r1")["reason"], "busy")
        queued = self.read("r2", interrupt=True)
        self.assertEqual(queued["status"], "accepted")
        self.assertNotIn("CANARY-FX", json.dumps(self.c.context("nell"), ensure_ascii=False))
        self.clock.advance(queued["starts_at"] - self.clock.now())
        self.k.tick()
        self.assertEqual(self.k.actor("nell")["activity"], "phone")
        self.assertNotIn("CANARY-FX", json.dumps(self.c.context("nell"), ensure_ascii=False))
        self.clock.advance(1)
        self.k.tick()
        self.assertIn("CANARY-FX", json.dumps(self.c.context("nell"), ensure_ascii=False))
        n = self.store.db.execute("SELECT COUNT(*) FROM effects WHERE effect_key LIKE '%:read:m1'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_default_catalog_keeps_read_phone_out(self):
        k = Kernel(self.store, ROSTER)
        r = k.submit({"idempotency_key": "x", "principal": "channel:nell", "actor_id": "nell", "origin": "self",
                      "action_type": "read_phone", "args": {"message_id": "m1"}})
        self.assertEqual(r["reason"], "unknown_action")


class AdminTests(Base):
    def test_owner_input_is_owner_only_and_housemates_cannot_send_it(self):
        self.assertEqual(admin.owner_input(self.k, "talk", "hi", "channel:nell")["reason"], "owner_only")
        r = self.own("nell", "owner_talk", "x", args={"text": "房主说可以"})
        self.assertEqual(r["reason"], "reserved_for_owner_or_admin")
        self.assertEqual(admin.owner_input(self.k, "talk", "hi", "cmd:owner")["status"], "accepted")

    def test_unwitnessed_correction_is_real_but_unseen(self):
        self.assertEqual(admin.correction(self.k, "channel:nell", "egg", 5, "x")["reason"], "admin_only")
        r = admin.correction(self.k, "admin:world", "egg", 5, "restock fixture")
        self.assertEqual(r["status"], "succeeded")
        self.assertEqual(self.k.resource("egg")["qty"], 5)
        n = self.store.db.execute("SELECT COUNT(*) FROM observations WHERE event_id=?", (r["event_id"],)).fetchone()[0]
        self.assertEqual(n, 0)
        self.assertNotIn("owner_correction", json.dumps(self.c.context("nell"), ensure_ascii=False))

    def test_correction_cannot_pull_reserved_stock(self):
        self.own("nell", "cook", "c", args={"recipe": "omelette"})
        self.assertEqual(admin.correction(self.k, "admin:world", "egg", 1, "x")["reason"], "would_break_reservation")

    def test_synthetic_never_writes_live_memory(self):
        r = admin.export_memory(self.k, "live", "nell", "CANARY")
        self.assertEqual(r["status"], "failed")
        self.assertEqual(self.store.db.execute("SELECT kind FROM audits").fetchone()[0], "synthetic_live_rejected")


class SummaryTests(Base):
    def test_timer_and_scene_end_make_one_summary_and_growth_counts_once(self):
        self.own("wanda", "eat", "e", args={"item": "cake"})
        self.clock.advance(600)
        self.k.tick()
        ids = [r[0] for r in self.store.db.execute("SELECT event_id FROM events WHERE actor_id='wanda'")]
        a = summary.summarize(self.k, "dinner", "public", ids, "timer", growth_rule="one_per_consumption_event")
        b = summary.summarize(self.k, "dinner", "public", ids, "scene_end", growth_rule="one_per_consumption_event")
        self.assertEqual(a, b)
        rows = summary.summaries(self.k)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["triggers"], ["timer", "scene_end"])
        self.assertEqual(rows[0]["text"], "")  # living-room facts are room-scoped: none in a public summary
        summary.summarize(self.k, "dinner", "room:living", ids, "timer", growth_rule="one_per_consumption_event")
        growth = self.store.db.execute("SELECT COUNT(*) FROM effects WHERE effect_key LIKE 'growth:%'").fetchone()[0]
        self.assertEqual(growth, 1)
        self.assertIn("action_succeeded", summary.summaries(self.k, "room:living")[0]["text"])


class OutageTests(Base):
    def test_mechanical_step_settles_on_resume_without_new_decisions(self):
        self.own("nell", "move", "m", args={"to": "living"})
        self.clock.advance(2)
        stopped = self.clock.now()
        self.store.close()
        self.clock.advance(3600)
        self.store = Store(self.path, self.clock, holder="svc")
        self.store.acquire()
        self.k = Kernel(self.store, ROSTER)
        self.k.resume(stopped)
        self.assertEqual(self.k.actor("nell")["location"], "living")
        kinds = [r[0] for r in self.store.db.execute("SELECT kind FROM events ORDER BY event_id")]
        self.assertEqual(kinds.count("resumed_after_outage"), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0], 0)

    def test_crash_before_commit_leaves_nothing(self):
        def boom():
            self.store.faults.clear()
            raise RuntimeError("crash")
        self.store.faults["command_acceptance:before_commit"] = boom
        with self.assertRaises(RuntimeError):
            self.own("wanda", "eat", "k", args={"item": "cake"})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 0)
        self.assertEqual(self.own("wanda", "eat", "k", args={"item": "cake"})["status"], "running")


if __name__ == "__main__":
    unittest.main()
