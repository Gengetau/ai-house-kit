"""K5: fake cognition (context privacy, late plans, retry), virtual budget, outbox and receipts."""
import json
import os
import tempfile
import unittest

from worldkernel import outbox
from worldkernel.budget import Budget
from worldkernel.cognition import Cognition, FakeModel
from worldkernel.guide import Guide
from worldkernel.kernel import Kernel
from worldkernel.routine import Routine
from worldkernel.store import Store, VirtualClock

from test_kernel import FIXTURE, ROSTER
from test_guide_routine import DINNER

THREE = dict(FIXTURE, actors=dict(FIXTURE["actors"], alma={"location": "kitchen", "body": {"energy": 50, "hunger": 30}}))


class Base(unittest.TestCase):
    fixture = THREE
    limits = None

    def setUp(self):
        self.clock = VirtualClock(DINNER)
        self.store = Store(os.path.join(tempfile.mkdtemp(), "w.db"), self.clock)
        self.store.acquire()
        self.k = Kernel(self.store, ROSTER)
        self.k.seed(self.fixture)
        self.budget = Budget(self.store, limits=self.limits)
        self.answers = {}
        self.model = FakeModel(lambda ctx: self.answers[ctx["actor"]].pop(0))
        self.c = Cognition(self.k, self.budget, self.model, Guide(self.store), Routine(self.k))
        self.n = 0
        self.addCleanup(self.store.close)

    def own(self, actor, action, **kw):
        self.n += 1
        return self.k.submit({"idempotency_key": f"own{self.n}", "principal": f"channel:{actor}", "actor_id": actor,
                              "origin": "self", "action_type": action, "args": kw.pop("args", {}), **kw})

    def step(self, seconds):
        self.clock.advance(seconds)
        self.k.tick()
        self.c.run_due()
        self.c.advance_plans()


class ContextTests(Base):
    def test_context_holds_only_what_she_perceived(self):
        self.own("wanda", "say", args={"text": "客厅里没人听见这句"})  # wanda alone in the living room
        self.own("wanda", "move", args={"to": "kitchen"})
        self.step(8)
        self.own("wanda", "say", args={"text": "PRIVATE-CANARY-7", "to": "nell"})
        self.own("nell", "say", args={"text": "大家晚上好"})
        ali = json.dumps(self.c.context("alma"), ensure_ascii=False)
        self.assertNotIn("PRIVATE-CANARY-7", ali)
        self.assertNotIn("客厅里没人听见这句", ali)
        self.assertIn("talked_quietly", ali)
        self.assertIn("大家晚上好", ali)
        nell = json.dumps(self.c.context("nell"), ensure_ascii=False)
        self.assertIn("PRIVATE-CANARY-7", nell)
        ctx = self.c.context("alma")
        self.assertEqual(sorted(ctx["here"]), ["nell", "wanda"])
        self.assertEqual(ctx["audience"], "self:alma")
        self.assertNotIn("cake", [i["item"] for i in ctx["items_here"]])  # the cake is in the living room
        self.assertEqual(set(ctx["body"]), {"energy", "hunger"})  # her own body only


class DecisionTests(Base):
    def test_late_plan_finds_the_eggs_gone_and_retries_once(self):
        self.answers["nell"] = [
            {"steps": [{"action_type": "cook", "args": {"recipe": "omelette"}, "uses": ["egg"]}], "latency": 120},
            {"steps": [{"action_type": "cook", "args": {"recipe": "omelette"}, "uses": ["egg"]}], "latency": 60}]
        self.assertEqual(self.c.begin("nell", "dinner"), "asked")
        self.step(30)
        self.assertEqual(self.own("alma", "cook", args={"recipe": "omelette"})["status"], "running")  # gets there first
        self.step(100)  # nell's answer lands: eggs are reserved by alma
        self.step(70)  # the one corrective retry also fails
        pts = [tuple(r) for r in self.store.db.execute(
            "SELECT outcome FROM decision_points WHERE actor_id='nell' ORDER BY point_id")]
        self.assertIn(("retry",), pts)
        self.assertEqual(pts[-1], ("replan_needed",))
        self.assertEqual(len(self.model.calls), 2)
        self.step(600)
        self.assertEqual(self.k.resource("egg")["qty"], 0)
        made = self.store.db.execute("SELECT COUNT(*) FROM resources WHERE item='omelette'").fetchone()[0]
        self.assertEqual(made, 1)
        self.assertEqual(self.budget.used(), 2)

    def test_stale_generation_when_she_moved_meanwhile(self):
        self.answers["wanda"] = [{"steps": [{"action_type": "eat", "args": {"item": "cake"}, "uses": ["cake"]}],
                                  "latency": 60}, {"steps": [], "latency": 5}]
        self.c.begin("wanda", "hunger")
        self.own("wanda", "move", args={"to": "kitchen"})  # her own move while the model thinks
        self.step(60)
        reasons = [r[0] for r in self.store.db.execute("SELECT reason FROM decision_points WHERE actor_id='wanda'")]
        self.assertTrue(any("stale_generation" in r or "busy" in r for r in reasons))
        self.assertEqual(self.k.resource("cake")["qty"], 1)

    def test_plan_runs_step_by_step(self):
        self.answers["wanda"] = [{"steps": [{"action_type": "move", "args": {"to": "kitchen"}},
                                            {"action_type": "cook", "args": {"recipe": "rice"}}], "latency": 10}]
        self.c.begin("wanda", "dinner")
        self.step(10)
        self.assertEqual(self.k.actor("wanda")["activity"], "walking")
        self.step(8)
        self.assertEqual(self.k.actor("wanda")["activity"], "cooking")
        origins = [r[0] for r in self.store.db.execute("SELECT origin FROM actions WHERE actor_id='wanda'")]
        self.assertEqual(origins, ["model", "model"])

    def test_model_error_counts_as_unknown_cost(self):
        def boom(ctx):
            raise TimeoutError("fake")
        self.c.model = FakeModel(boom)
        self.assertEqual(self.c.begin("nell", "x"), "failed")
        self.assertEqual(self.store.db.execute("SELECT state FROM budget_reservations").fetchone()[0], "unknown")
        self.assertEqual(self.budget.used(), 1)


class BudgetTests(Base):
    limits = {"per_hour": 2}

    def test_full_window_degrades_without_calling(self):
        self.answers["nell"] = [{"steps": [], "latency": 1}] * 3
        self.assertEqual(self.c.begin("nell", "a"), "asked")
        self.assertEqual(self.c.begin("nell", "b"), "asked")
        self.assertEqual(self.c.begin("nell", "c"), "degraded")
        self.assertEqual(len(self.model.calls), 2)
        self.clock.advance(3601)
        self.assertEqual(self.budget.remaining(), 2)


class OutboxTests(Base):
    def notice(self, to="nell"):
        with self.store.tx():
            eid = self.k._emit("timer", None, None, "kitchen", {}, scope="system", occurred=self.clock.now())
            return outbox.request_delivery(self.store, eid, "phone", "timer_done", to)

    def test_timeout_after_send_is_unknown_then_reconciled(self):
        ch = outbox.FakeChannel(["timeout_after_send"])
        d = outbox.Dispatcher(self.store, {"phone": ch})
        did = self.notice()
        self.assertEqual(d.run_once(), {did: "unknown"})
        self.assertEqual(d.run_once(), {})  # no blind resend
        self.assertEqual(len(ch.sent[did]), 1)
        self.assertEqual(d.reconcile(), {did: "sent"})
        self.assertEqual(len(ch.sent[did]), 1)

    def test_never_sent_stays_unknown_on_non_idempotent_channel(self):
        ch = outbox.FakeChannel(["timeout_before_send"])
        d = outbox.Dispatcher(self.store, {"phone": ch})
        did = self.notice()
        d.run_once()
        self.assertEqual(d.reconcile(), {did: "unknown"})
        ch.idempotent = True
        self.assertEqual(d.reconcile(), {did: "pending"})
        self.assertEqual(d.run_once(), {did: "sent"})

    def test_refusal_is_retried_and_receipts_are_kept_once(self):
        ch = outbox.FakeChannel(["refuse", "ok"])
        d = outbox.Dispatcher(self.store, {"phone": ch})
        did = self.notice()
        self.assertEqual(d.run_once(), {did: "pending"})
        self.assertEqual(d.run_once(), {did: "sent"})
        ref = self.store.db.execute("SELECT external_ref FROM outbox").fetchone()[0]
        self.assertTrue(d.receive(did, "delivered", ref, epoch=0))  # duplicate from an old epoch
        self.assertTrue(d.receive(did, "read", ref, epoch=0))
        rows = [tuple(r) for r in self.store.db.execute("SELECT status FROM receipts ORDER BY status")]
        self.assertEqual(rows, [("delivered",), ("read",)])
        self.assertFalse(d.receive("dlv-nope", "delivered", "x", 1))

    def test_crash_mid_send_is_reconciled_not_resent(self):
        ch = outbox.FakeChannel()
        d = outbox.Dispatcher(self.store, {"phone": ch})
        did = self.notice()
        self.store.db.execute("UPDATE outbox SET state='sending'")  # process died after marking
        ch.sent[did] = [{"template": "timer_done"}]
        self.assertEqual(d.run_once(), {})
        self.assertEqual(d.reconcile(), {did: "sent"})

    def test_only_templates(self):
        with self.assertRaises(ValueError):
            with self.store.tx():
                outbox.request_delivery(self.store, 1, "phone", "free text", "nell")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
