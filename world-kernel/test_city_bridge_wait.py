"""A confirmed city outing waits out a short bridge outage instead of being cancelled.

Offline: reuses the in-memory mock bridge from test_city_m1a (no network, no paid calls).
"""
import json
import unittest
from unittest.mock import patch

import test_city_m1a as m1a


class BridgeWaitTests(unittest.TestCase):
    setUp = m1a.CityBridgeM1ATests.setUp
    _boot = m1a.CityBridgeM1ATests._boot
    _restart_kernel = m1a.CityBridgeM1ATests._restart_kernel
    tearDown = m1a.CityBridgeM1ATests.tearDown

    def _plan(self, pid, location, members=("nell",), *, end_after_s=None, advance_quiet=True):
        # Quiet mid-afternoon: nobody is cooking or eating, so a due plan is not held back by a meal.
        if advance_quiet:
            self.clock.advance(2.5 * 3600)
        with self.s.tx() as db:
            for a in m1a.ROSTER:
                self.k._set_actor(db, a, location="living", activity="idle", action_id=None)
        now = self.clock.now()
        end_at = now + end_after_s if end_after_s is not None else None
        with self.s.tx() as db:
            db.execute(
                "INSERT INTO plans(plan_id,host,kind,what,location,start_at,end_at,members,state,created_at,updated_at)"
                " VALUES(?,?,'outing','cafe',?,?,?,?,'confirmed',?,?)",
                (pid, members[0], location, now, end_at, json.dumps(list(members)), now, now),
            )

    def _visits(self, pid):
        with self.s.read() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT actor_id, visit_id, state FROM city_visits JOIN city_visit_carry USING(visit_id,actor_id)"
                    " WHERE plan_id=? ORDER BY actor_id",
                    (pid,),
                )
            ]

    def _wrap_transport(self, override):
        original = self.client._transport
        self.client._transport = lambda method, url, query, body, headers: override(
            original, method, url, query, body, headers
        )
        return original

    def _carry(self, pid):
        with self.s.read() as db:
            return [dict(r) for r in db.execute("SELECT * FROM city_visit_carry WHERE plan_id=?", (pid,))]

    def _audits(self, kind):
        with self.s.read() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT detail FROM audits WHERE kind=?", (kind,))]

    def _state(self, pid):
        return self.life.plans.plan(pid)["state"]

    def _run(self, minutes, step_s=60):
        for _ in range(int(minutes * 60 / step_s)):
            self.clock.advance(step_s)
            self.life.step()

    def test_bridge_returns_in_window_goes_to_city_once(self):
        self._plan("pbwa", "place:muginohi")
        self.engine.catalog_offline = True
        self._run(8)
        self.assertEqual("confirmed", self._state("pbwa"), "outage must not cancel a confirmed outing")
        self.assertEqual([], self._carry("pbwa"))
        self.assertTrue(self._audits("city_plan_wait"), "waiting must leave an audit")
        self.engine.catalog_offline = False
        self._run(8)
        self.assertEqual(1, len(self._carry("pbwa")))
        self.assertEqual(1, len(self.engine.visits))
        self.assertNotEqual("cancelled", self._state("pbwa"))
        self._run(10)
        self.assertEqual(1, len(self.engine.visits), "exactly one city visit")

    def test_window_expires_degrades_to_one_local_outing(self):
        self._plan("pbwb", "place:muginohi")
        self.engine.catalog_offline = True
        self._run(45)
        self.assertEqual({}, self.engine.visits)
        self.assertEqual([], self._carry("pbwb"))
        self.assertNotEqual("cancelled", self._state("pbwb"))
        self.assertEqual(1, len(self._audits("city_plan_degraded_local")))
        job = None
        for _ in range(150):  # she may be mid-pastime when the window closes; the local outing starts once she is free
            self._run(1)
            job = self.adapter._outings.job("nell")
            if job:
                break
        self.assertIsNotNone(job, "local outing must have started")
        self.assertEqual("pbwb", job["plan_id"])
        self.assertEqual("running", self._state("pbwb"))
        self._run(10)
        self.assertEqual({}, self.engine.visits)
        with self.s.read() as db:
            n = db.execute("SELECT COUNT(*) FROM plans WHERE kind IN ('outing','trip')").fetchone()[0]
        self.assertEqual(1, n, "same plan, no second plan")

    def test_lost_prepare_receipt_never_reopens_or_cancels(self):
        self.engine.catalog_offline = False
        self._plan("pbwc", "place:muginohi")
        self.engine.drop_next_prepare_response = True
        self._run(45)
        self.assertEqual(1, len(self.engine.visits), "one engine visit only")
        self.assertEqual(1, len(self._carry("pbwc")))
        self.assertNotEqual("cancelled", self._state("pbwc"))
        self.assertEqual([], self._audits("city_plan_degraded_local"))

    def test_alias_without_local_route_cancels_after_window_with_reason(self):
        self._plan("pbwd", "place:rurigai")
        self.engine.catalog_offline = True
        self._run(10)
        self.assertEqual("confirmed", self._state("pbwd"))
        self._run(30)
        self.assertEqual("cancelled", self._state("pbwd"))
        self.assertEqual("bridge_unavailable_no_local_route", self.life.plans.plan("pbwd")["note"])
        self.assertEqual({}, self.engine.visits)
        self.assertTrue(self._audits("city_plan_cancelled_bridge"))

    def test_hold_follows_wait_and_does_not_outlive_departure(self):
        self._plan("pbwh", "place:muginohi")
        self.engine.catalog_offline = True
        self._run(3)
        held = self.adapter.hold_until("nell", self.clock.now())
        self.assertIsNotNone(held)
        self.assertGreater(held, self.clock.now() - 1, "hold is a future retry time")
        self.engine.catalog_offline = False
        self._run(8)
        self.assertEqual(1, len(self._carry("pbwh")))
        stale = self.adapter.hold_until("nell", self.clock.now())
        self.assertIsNone(stale, "after she left, a leftover wait record must not hold her")

    def test_wait_survives_kernel_restart(self):
        self._plan("pbwr", "place:muginohi")
        self.engine.catalog_offline = True
        self._run(4)
        self.assertIsNotNone(self.life.get("cityplan:wait:pbwr"))
        self._restart_kernel(downtime_s=30)
        self.assertEqual("confirmed", self._state("pbwr"))
        self.engine.catalog_offline = False
        self._run(8)
        self.assertEqual(1, len(self.engine.visits))
        self.assertEqual(1, len(self._carry("pbwr")))

    def test_version_mismatch_still_cancels_at_once(self):
        self._plan("pbwe", "place:muginohi")
        self.engine.mappings_version = 99
        self._run(3)
        self.assertEqual("cancelled", self._state("pbwe"))
        self.assertEqual("VERSION_MISMATCH", self.life.plans.plan("pbwe")["note"])

    def test_lost_prepare_receipt_with_precheck_and_lookup_outage_past_deadline(self):
        self.adapter.enable_all_roommates()
        self._plan("pbwp", "place:muginohi", members=("nell", "elsa"))
        self.k.submit(
            dict(
                idempotency_key="elsa-busy-p",
                principal="channel:elsa",
                actor_id="elsa",
                origin="model",
                action_type="wait",
                args={"seconds": 60},
            )
        )
        self.adapter._activate_due_city_plans()
        first, = self._visits("pbwp")
        self.assertEqual(("nell", "preparing"), (first["actor_id"], first["state"]))
        vid1 = first["visit_id"]
        self.clock.advance(90)
        self.k.tick()

        outage = [True]
        prepared_once = [False]

        def bridge(original, method, url, query, body, headers):
            if outage[0] and url.endswith("/catalog"):
                return 401, {}, json.dumps({"error": "UNAUTHENTICATED", "message": "outage"})
            if outage[0] and url.endswith("/visits/" + vid1):
                raise m1a.CityTransportError("cannot read original receipt")
            if outage[0] and "/visits/prepare" in url and prepared_once[0]:
                raise m1a.CityTransportError("cannot replay prepare during outage")
            try:
                return original(method, url, query, body, headers)
            except m1a.CityTransportError:
                if "/visits/prepare" in url:
                    prepared_once[0] = True
                raise

        self._wrap_transport(bridge)
        self.engine.drop_next_prepare_response = True
        self.adapter.step()
        self.assertEqual("prepared", self.engine.visits[vid1]["state"])
        self.assertEqual([dict(first, state="preparing")], self._visits("pbwp"))
        self.assertEqual("UNAUTHENTICATED", self._audits("city_plan_wait")[-1]["code"])

        # Cross the 30-min wait deadline while both precheck and receipt lookup are unreachable.
        self.clock.advance(1800)
        self.adapter.step()
        self.assertEqual([dict(first, state="preparing")], self._visits("pbwp"))
        self.assertEqual("running", self._state("pbwp"))
        self.assertEqual([], self._audits("city_plan_degraded_local"))
        self.assertIsNone(self.life.get("cityplan:local:pbwp"))
        self.assertIsNone(self.life.get("cityplan:skipped:pbwp:elsa"))
        self.assertEqual([], self._audits("city_plan_member_skipped"))
        hold_audit = self._audits("city_plan_wait")[-1]
        self.assertEqual("hold_carry_rows", hold_audit["phase"])
        self.assertEqual("UNAUTHENTICATED", hold_audit["code"])

        # Bridge recovers before plan end: Nell finishes her original visit handover, Elsa departs on the same plan.
        outage[0] = False
        self.clock.advance(300)
        self.adapter.step()
        self.clock.advance(90)
        self.k.tick()
        self.adapter.step()
        visits = self._visits("pbwp")
        self.assertEqual([("elsa", "active"), ("nell", "active")], [(v["actor_id"], v["state"]) for v in visits])
        self.assertEqual(vid1, next(v["visit_id"] for v in visits if v["actor_id"] == "nell"))
        self.assertEqual(2, len(self.engine.visits))
        self.assertIsNone(self.life.get("cityplan:wait:pbwp"))

        # First member returning must not mark the plan done before Elsa returns.
        vid2 = next(v["visit_id"] for v in visits if v["actor_id"] == "elsa")
        self.engine.simulate_return_and_settle(vid1)
        self.adapter.step()
        self.assertEqual("running", self._state("pbwp"))
        self.engine.simulate_return_and_settle(vid2)
        self.adapter.step()
        self.assertEqual("done", self._state("pbwp"))
        self.assertEqual(2, len(self.engine.visits))

    def test_active_first_member_holds_plan_until_bridge_recovers_for_rest(self):
        self.adapter.enable_all_roommates()
        self._plan("pbwact", "place:muginohi", members=("nell", "elsa"))
        self.k.submit(
            dict(
                idempotency_key="elsa-busy-act",
                principal="channel:elsa",
                actor_id="elsa",
                origin="model",
                action_type="wait",
                args={"seconds": 60},
            )
        )
        self.adapter._activate_due_city_plans()
        first, = self._visits("pbwact")
        vid1 = first["visit_id"]
        self.clock.advance(90)
        self.k.tick()
        active = self.adapter.advance_handover(vid1)
        self.assertEqual("active", active["state"])

        outage = [True]

        def bridge(original, method, url, query, body, headers):
            if outage[0] and url.endswith("/catalog"):
                return 400, {}, json.dumps({"error": "PROTOCOL_MISMATCH", "message": "outage"})
            return original(method, url, query, body, headers)

        self._wrap_transport(bridge)
        self.adapter._activate_due_city_plans()
        self.assertEqual("PROTOCOL_MISMATCH", self._audits("city_plan_wait")[-1]["code"])

        self.clock.advance(1800)
        self.adapter._activate_due_city_plans()
        self.assertEqual("running", self._state("pbwact"))
        self.assertIsNone(self.life.get("cityplan:skipped:pbwact:elsa"))
        self.assertEqual([], self._audits("city_plan_member_skipped"))
        hold_audit = self._audits("city_plan_wait")[-1]
        self.assertEqual("hold_carry_rows", hold_audit["phase"])
        self.assertEqual("PROTOCOL_MISMATCH", hold_audit["code"])

        outage[0] = False
        self.clock.advance(300)
        self.adapter._activate_due_city_plans()
        self.clock.advance(90)
        self.k.tick()
        self.adapter.step()
        visits = self._visits("pbwact")
        self.assertEqual([("elsa", "active"), ("nell", "active")], [(v["actor_id"], v["state"]) for v in visits])
        self.assertEqual(vid1, next(v["visit_id"] for v in visits if v["actor_id"] == "nell"))
        self.assertEqual(2, len(self.engine.visits))
        self.assertIsNone(self.life.get("cityplan:wait:pbwact"))

        vid2 = next(v["visit_id"] for v in visits if v["actor_id"] == "elsa")
        self.engine.simulate_return_and_settle(vid1)
        self.adapter.step()
        self.assertEqual("running", self._state("pbwact"))
        self.engine.simulate_return_and_settle(vid2)
        self.adapter.step()
        self.assertEqual("done", self._state("pbwact"))

    def test_all_aborted_carries_hold_original_plan_and_depart_only_uncarried(self):
        self.adapter.enable_all_roommates()
        self._plan("pbwabt", "place:muginohi", members=("nell", "elsa"))
        vis = self.adapter.depart("nell", "kazeminato:muginohi", plan_id="pbwabt")
        carry = self.adapter._carry_row(vis["visit_id"])
        req_id = carry["abort_request_id"]
        receipt = self.client.abort_visit(vis["visit_id"], request_id=req_id, reason="probe")
        self.k.city_visits.transition(
            vis["visit_id"],
            "aborted",
            req_id,
            vis["domain_epoch"],
            receipt=dict(receipt, request_id=req_id),
        )

        outage = [True]

        def bridge(original, method, url, query, body, headers):
            if outage[0] and url.endswith("/catalog"):
                return 401, {}, json.dumps({"error": "UNAUTHENTICATED", "message": "outage"})
            return original(method, url, query, body, headers)

        self._wrap_transport(bridge)
        self.adapter._activate_due_city_plans()
        self.clock.advance(1800)
        self.adapter._activate_due_city_plans()
        self.assertEqual("running", self._state("pbwabt"))
        self.assertEqual([("nell", "aborted")], [(v["actor_id"], v["state"]) for v in self._visits("pbwabt")])
        self.assertIsNone(self.life.get("cityplan:local:pbwabt"))
        self.assertIsNone(self.life.get("cityplan:skipped:pbwabt:elsa"))
        held = self.life.get("cityplan:wait:pbwabt")
        self.assertEqual(self.clock.now() + 300, held["next_try"])
        self.assertEqual("UNAUTHENTICATED", held["code"])
        hold_audit = self._audits("city_plan_wait")[-1]
        self.assertEqual("hold_carry_rows", hold_audit["phase"])
        self.assertEqual("UNAUTHENTICATED", hold_audit["code"])

        outage[0] = False
        self.clock.advance(300)
        self.adapter._activate_due_city_plans()
        self.assertEqual(
            [("elsa", "preparing"), ("nell", "aborted")],
            [(v["actor_id"], v["state"]) for v in self._visits("pbwabt")],
        )
        self.assertEqual(2, len(self._carry("pbwabt")))
        self.assertIsNone(self.life.get("cityplan:wait:pbwabt"))

    def test_wait_expired_audit_preserves_bridge_error_code(self):
        self._plan("pbwexp", "place:muginohi", members=("nell",), end_after_s=45 * 60)

        def bridge(original, method, url, query, body, headers):
            if url.endswith("/catalog"):
                return 401, {}, json.dumps({"error": "UNAUTHENTICATED", "message": "probe"})
            return original(method, url, query, body, headers)

        self._wrap_transport(bridge)
        self.adapter.step()
        self.assertEqual("UNAUTHENTICATED", self._audits("city_plan_wait")[-1]["code"])
        self.clock.advance(45 * 60)
        self.life.step()
        self.assertEqual("lapsed", self._state("pbwexp"))
        expired, = self._audits("city_plan_wait_expired")
        self.assertEqual("pbwexp", expired["plan_id"])
        self.assertEqual("plan_window_passed", expired["reason"])
        self.assertEqual("UNAUTHENTICATED", expired.get("code"))
        self.assertIsNone(self.life.get("cityplan:wait:pbwexp"))

    def test_wait_expired_past_end_and_held_carry_keep_bridge_code(self):
        self.adapter.enable_all_roommates()
        # 1) Advancing strictly past end_at (plans.sync runs before adapter.step) still audits wait expiry with code.
        self._plan("pbwexp2", "place:muginohi", members=("nell",), end_after_s=45 * 60)
        orig = self._wrap_transport(
            lambda original, method, url, query, body, headers: (
                (400, {}, json.dumps({"error": "PROTOCOL_MISMATCH", "message": "probe"}))
                if url.endswith("/catalog")
                else original(method, url, query, body, headers)
            )
        )
        self.adapter.step()
        self.clock.advance(45 * 60 + 30)
        self.life.step()
        self.assertEqual("lapsed", self._state("pbwexp2"))
        exp2 = [a for a in self._audits("city_plan_wait_expired") if a["plan_id"] == "pbwexp2"]
        self.assertEqual(1, len(exp2))
        self.assertEqual("PROTOCOL_MISMATCH", exp2[0]["code"])
        self.assertIsNone(self.life.get("cityplan:wait:pbwexp2"))

        # 2) Held carry plan whose bridge never recovers before end_at also audits wait expiry with code once.
        self.client._transport = orig
        self._plan("pbwexp3", "place:muginohi", members=("nell", "elsa"), end_after_s=45 * 60, advance_quiet=False)
        vis = self.adapter.depart("nell", "kazeminato:muginohi", plan_id="pbwexp3")
        self.clock.advance(90)
        self.k.tick()
        self.adapter.advance_handover(vis["visit_id"])
        self._wrap_transport(
            lambda original, method, url, query, body, headers: (
                (401, {}, json.dumps({"error": "UNAUTHENTICATED", "message": "probe"}))
                if url.endswith("/catalog")
                else original(method, url, query, body, headers)
            )
        )
        self.adapter._activate_due_city_plans()
        self.clock.advance(1800)
        self.adapter._activate_due_city_plans()
        self.assertEqual("hold_carry_rows", self._audits("city_plan_wait")[-1]["phase"])
        self.clock.advance(45 * 60)
        self.adapter._activate_due_city_plans()
        self.adapter._activate_due_city_plans()  # idempotent: only one expiry audit
        exp3 = [a for a in self._audits("city_plan_wait_expired") if a["plan_id"] == "pbwexp3"]
        self.assertEqual(1, len(exp3))
        self.assertEqual("plan_window_passed", exp3[0]["reason"])
        self.assertEqual("UNAUTHENTICATED", exp3[0]["code"])
        self.assertIsNone(self.life.get("cityplan:wait:pbwexp3"))

    def test_legacy_bridge_unavailable_skip_marker_is_reprocessed_on_departable_plan(self):
        self.adapter.enable_all_roommates()
        self._plan("pbwleg", "place:muginohi", members=("nell", "elsa"), end_after_s=45 * 60)
        self.k.submit(
            dict(
                idempotency_key="elsa-busy-leg",
                principal="channel:elsa",
                actor_id="elsa",
                origin="model",
                action_type="wait",
                args={"seconds": 60},
            )
        )
        self.adapter._activate_due_city_plans()
        first, = self._visits("pbwleg")
        vid1 = first["visit_id"]
        self.clock.advance(90)
        self.k.tick()
        self.assertEqual("active", self.adapter.advance_handover(vid1)["state"])
        self.assertEqual("running", self._state("pbwleg"))

        # Legacy db8296f write: precheck failure after another member started wrote city_bridge_unavailable skip.
        self.life.put("cityplan:skipped:pbwleg:elsa", "city_bridge_unavailable")
        self._restart_kernel(allowed=m1a.ROSTER)

        # Bridge is healthy and plan is still inside its window: Elsa departs once on the original plan.
        self.adapter.step()
        self.assertIsNone(self.life.get("cityplan:skipped:pbwleg:elsa"))
        reprocessed = [a for a in self._audits("city_plan_legacy_skip_reprocessed") if a["plan_id"] == "pbwleg"]
        self.assertEqual(
            [{"plan_id": "pbwleg", "actor": "elsa", "reason": "city_bridge_unavailable"}],
            reprocessed,
        )
        visits = self._visits("pbwleg")
        self.assertEqual([("elsa", "preparing"), ("nell", "active")], [(v["actor_id"], v["state"]) for v in visits])
        vid2 = next(v["visit_id"] for v in visits if v["actor_id"] == "elsa")

        # First member returning must keep the plan running; repeated steps/restarts never double-depart or double-audit.
        self.engine.simulate_return_and_settle(vid1)
        self.adapter.step()
        self.assertEqual("running", self._state("pbwleg"))
        self.clock.advance(90)
        self.k.tick()
        self.adapter.step()
        self._restart_kernel(allowed=m1a.ROSTER)
        self.adapter.step()
        visits = self._visits("pbwleg")
        self.assertEqual([("elsa", "active"), ("nell", "closed")], [(v["actor_id"], v["state"]) for v in visits])
        self.assertEqual(2, len(self.engine.visits))
        self.assertEqual(2, len(self._carry("pbwleg")))
        self.assertEqual(
            1,
            len([a for a in self._audits("city_plan_legacy_skip_reprocessed") if a["plan_id"] == "pbwleg"]),
        )

        # Once both return, the plan finishes as done.
        self.engine.simulate_return_and_settle(vid2)
        self.adapter.step()
        self.assertEqual("done", self._state("pbwleg"))
        self.assertEqual(2, len(self.engine.visits))
        self.assertEqual(
            1,
            len([a for a in self._audits("city_plan_legacy_skip_reprocessed") if a["plan_id"] == "pbwleg"]),
        )

    def test_legacy_bridge_skip_settlement_before_scheduler_and_expired_plan(self):
        self.adapter.enable_all_roommates()

        # 1) Even if Nell settles BEFORE _activate_due_city_plans clears the legacy marker, settlement keeps plan running.
        self._plan("pbwleg2", "place:muginohi", members=("nell", "elsa"), end_after_s=45 * 60)
        vis1 = self.adapter.depart("nell", "kazeminato:muginohi", plan_id="pbwleg2")
        vid1 = vis1["visit_id"]
        self.clock.advance(90)
        self.k.tick()
        self.assertEqual("active", self.adapter.advance_handover(vid1)["state"])
        self.life.put("cityplan:skipped:pbwleg2:elsa", "city_bridge_unavailable")

        self.engine.simulate_return_and_settle(vid1)
        self.adapter.poll_outbox_once(wait_ms=0)
        self.assertEqual([("nell", "closed")], [(v["actor_id"], v["state"]) for v in self._visits("pbwleg2")])
        self.assertEqual("running", self._state("pbwleg2"))
        self.assertEqual("city_bridge_unavailable", self.life.get("cityplan:skipped:pbwleg2:elsa"))

        # Next scheduling step reprocesses Elsa once without re-departing Nell (member dedup intact).
        self.adapter.step()
        self.assertIsNone(self.life.get("cityplan:skipped:pbwleg2:elsa"))
        self.assertEqual(
            1,
            len([a for a in self._audits("city_plan_legacy_skip_reprocessed") if a["plan_id"] == "pbwleg2"]),
        )
        visits = self._visits("pbwleg2")
        self.assertEqual([("elsa", "preparing"), ("nell", "closed")], [(v["actor_id"], v["state"]) for v in visits])
        vid2 = next(v["visit_id"] for v in visits if v["actor_id"] == "elsa")
        self.clock.advance(90)
        self.k.tick()
        self.adapter.step()
        self.engine.simulate_return_and_settle(vid2)
        self.adapter.step()
        self.assertEqual("done", self._state("pbwleg2"))
        self.assertEqual(2, len(self._carry("pbwleg2")))

        # Complete Elsa's return door-walk before starting the next plan.
        self.clock.advance(90)
        self.k.tick()

        # 2) Past-window plan with legacy city_bridge_unavailable skip is not reprocessed and settles to done.
        self._plan("pbwold", "place:muginohi", members=("nell", "elsa"), end_after_s=45 * 60, advance_quiet=False)
        vis_old = self.adapter.depart("nell", "kazeminato:muginohi", plan_id="pbwold")
        vid_old = vis_old["visit_id"]
        self.clock.advance(90)
        self.k.tick()
        self.assertEqual("active", self.adapter.advance_handover(vid_old)["state"])
        self.life.put("cityplan:skipped:pbwold:elsa", "city_bridge_unavailable")

        self.clock.advance(45 * 60)
        self.adapter.step()
        self.assertEqual([("nell", "active")], [(v["actor_id"], v["state"]) for v in self._visits("pbwold")])
        self.assertEqual("running", self._state("pbwold"))
        self.assertEqual([], [a for a in self._audits("city_plan_legacy_skip_reprocessed") if a["plan_id"] == "pbwold"])

        self.engine.simulate_return_and_settle(vid_old)
        self.adapter.poll_outbox_once(wait_ms=0)
        self.assertEqual("done", self._state("pbwold"))
        self.assertEqual(1, len(self._carry("pbwold")))

    def test_legacy_bridge_skip_keeps_skip_when_shrunk_roster_cannot_take_plan_over(self):
        # The access list shrinks across the upgrade restart, so the scheduler no longer
        # claims the plan (claims_plan needs every member on the list). The legacy marker must keep
        # its skip meaning in settlement and hold_until(), and the returned plan must finish.
        self.adapter.enable_all_roommates()
        self._plan("pbwshr", "place:muginohi", members=("nell", "elsa"), end_after_s=45 * 60)
        vis = self.adapter.depart("nell", "kazeminato:muginohi", plan_id="pbwshr")
        vid = vis["visit_id"]
        self.clock.advance(90)
        self.k.tick()
        self.assertEqual("active", self.adapter.advance_handover(vid)["state"])
        self.life.put("cityplan:skipped:pbwshr:elsa", "city_bridge_unavailable")
        now = self.clock.now()
        self.life.put("cityplan:wait:pbwshr", {"next_try": now + 600, "deadline": now + 1200})

        self._restart_kernel(allowed=("elsa",))
        plan = self.life.plans.plan("pbwshr")
        self.assertLess(self.clock.now(), plan["end_at"])
        self.assertFalse(self.adapter.claims_plan(plan))
        # hold_until() reads the plan as a raw row: the marker still counts as a skip there.
        self.assertIsNone(self.adapter.hold_until("elsa", self.clock.now()))

        self.adapter.step()
        self.assertEqual("city_bridge_unavailable", self.life.get("cityplan:skipped:pbwshr:elsa"))
        self.assertEqual([], [a for a in self._audits("city_plan_legacy_skip_reprocessed") if a["plan_id"] == "pbwshr"])

        self.engine.simulate_return_and_settle(vid)
        self.adapter.poll_outbox_once(wait_ms=0)
        self.assertEqual([("nell", "closed")], [(v["actor_id"], v["state"]) for v in self._visits("pbwshr")])
        self.assertEqual("done", self._state("pbwshr"))
        self.assertEqual(1, len(self.engine.visits))

    def test_non_bridge_skip_reason_still_skips_in_scheduler_and_settlement(self):
        self.adapter.enable_all_roommates()
        self._plan("pbwoth", "place:muginohi", members=("nell", "elsa"), end_after_s=45 * 60)
        vis_o = self.adapter.depart("nell", "kazeminato:muginohi", plan_id="pbwoth")
        vid_o = vis_o["visit_id"]
        self.clock.advance(90)
        self.k.tick()
        self.assertEqual("active", self.adapter.advance_handover(vid_o)["state"])
        self.life.put("cityplan:skipped:pbwoth:elsa", "insufficient_funds")

        # Still inside the plan window with healthy bridge: non-bridge skip must still skip Elsa.
        self.adapter.step()
        self.assertEqual([("nell", "active")], [(v["actor_id"], v["state"]) for v in self._visits("pbwoth")])
        self.assertEqual("insufficient_funds", self.life.get("cityplan:skipped:pbwoth:elsa"))
        self.assertEqual([], [a for a in self._audits("city_plan_legacy_skip_reprocessed") if a["plan_id"] == "pbwoth"])

        # Nell returning must finish the plan as done because Elsa was skipped for insufficient_funds.
        self.engine.simulate_return_and_settle(vid_o)
        self.adapter.step()
        self.assertEqual("done", self._state("pbwoth"))
        self.assertEqual(1, len(self._carry("pbwoth")))

    def test_legacy_bridge_skip_reprocess_is_atomic_and_waits_if_bridge_still_offline(self):
        self.adapter.enable_all_roommates()
        self._plan("pbwlegoff", "place:muginohi", members=("nell", "elsa"), end_after_s=45 * 60)
        vis1 = self.adapter.depart("nell", "kazeminato:muginohi", plan_id="pbwlegoff")
        vid1 = vis1["visit_id"]
        self.clock.advance(90)
        self.k.tick()
        self.assertEqual("active", self.adapter.advance_handover(vid1)["state"])
        self.life.put("cityplan:skipped:pbwlegoff:elsa", "city_bridge_unavailable")
        self.engine.catalog_offline = True

        # Fault after audit insert must roll back both skip-marker deletion and audit insert together.
        real_audit = self.k.audit

        def fail_after_reprocess_audit(kind, **payload):
            real_audit(kind, **payload)
            if kind == "city_plan_legacy_skip_reprocessed":
                raise RuntimeError("injected after legacy skip audit")

        with patch.object(self.k, "audit", fail_after_reprocess_audit):
            with self.assertRaisesRegex(RuntimeError, "injected after legacy skip audit"):
                self.adapter._activate_due_city_plans()
        self.assertEqual("city_bridge_unavailable", self.life.get("cityplan:skipped:pbwlegoff:elsa"))
        self.assertEqual([], [a for a in self._audits("city_plan_legacy_skip_reprocessed") if a["plan_id"] == "pbwlegoff"])

        # Retry while bridge is still offline: clears legacy marker once, audits once, enters wait & holds Elsa.
        self.adapter._activate_due_city_plans()
        self.assertIsNone(self.life.get("cityplan:skipped:pbwlegoff:elsa"))
        self.assertEqual(
            1,
            len([a for a in self._audits("city_plan_legacy_skip_reprocessed") if a["plan_id"] == "pbwlegoff"]),
        )
        wait = self.life.get("cityplan:wait:pbwlegoff")
        self.assertIsNotNone(wait)
        self.assertEqual(wait["next_try"], self.adapter.hold_until("elsa", self.clock.now()))

        # Recovery before end_at departs Elsa once and keeps audit idempotent.
        self.engine.catalog_offline = False
        self.clock.advance(max(1.0, float(wait["next_try"]) - self.clock.now()))
        self.adapter._activate_due_city_plans()
        self.assertEqual(
            [("elsa", "preparing"), ("nell", "active")],
            [(v["actor_id"], v["state"]) for v in self._visits("pbwlegoff")],
        )
        self.assertIsNone(self.life.get("cityplan:wait:pbwlegoff"))
        self.assertEqual(
            1,
            len([a for a in self._audits("city_plan_legacy_skip_reprocessed") if a["plan_id"] == "pbwlegoff"]),
        )


if __name__ == "__main__":
    unittest.main()
