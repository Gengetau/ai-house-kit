"""#191 R191-E1..E4 regressions via real kernel actions and Authority.
Initial reproductions: gemini-ext's independent probe_191_r2.py; assertions here require repaired behavior.
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "world-kernel"))
from worldkernel import legacy, promises as PR
from worldkernel.store import Store, VirtualClock
from worldkernel.life import Life
from worldkernel.life_model import FakeLifeModel
from worldkernel.outings import Outings
from worldkernel.wallet import JST
import authority as AUTH
import intents as I
import world as W
from test_intents import IntentBase, G
from test_life_outings import prices


class InvitationSeams(IntentBase):
    def setUp(self):
        super().setUp()
        self.now = datetime.fromisoformat("2026-11-02T10:00").replace(tzinfo=JST).timestamp()
        self.env = patch.dict(
            os.environ,
            {
                "INTENTS_KERNEL_MATCH": "on",
                "INTENTS_EXPIRY_V2": "1",
                "INTENTS_EXTRACT_V2": "1",
                "INTENTS_DECIDE_CTX": "1",
                "WORLD_KERNEL_ERRANDS_FROM_INTENTS": "1",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        I.init()
        self.kdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.kdir.cleanup)
        self.clock = VirtualClock(self.now)
        self.store = Store(os.path.join(self.kdir.name, "life1.db"), self.clock, world_id="test")
        self.store.acquire()
        self.addCleanup(self.store.close)
        self.life = Life(
            self.store,
            model=None,
            weather=lambda _: {"rain": False, "temp_c": 18},
            extensions=[lambda l: Outings(l, places=prices(), targets={})],
        )
        legacy.offline_world(W.DB_PATH)
        I.init()
        self.proj = legacy.Projector(self.life, W.DB_PATH)
        self.proj.run()
        self.promises = PR.Promises(self.life, W.DB_PATH, self.proj, I)
        self.promises.life.put("promises:cursor", 0)

    def add_intent(self, text, party, **kw):
        now = self.clock.now()
        args = dict(
            actor=party[0],
            party=json.dumps(party),
            agreed=json.dumps(party if kw.get("status", "accepted") == "accepted" else [party[0]]),
            kind="joint",
            text=text,
            status="proposed",
            source="chat",
            refs=json.dumps([{"chat": G, "at": now, "said_at": now}]),
            visibility="group",
            confidence=0.5,
            created=now,
            updated=now,
            last_action=now,
            scope="world",
            beneficiary="[]",
        )
        args.update(kw)
        with W.db() as c:
            return c.execute(
                "insert into intents(" + ",".join(args) + ") values(" + ",".join("?" * len(args)) + ")",
                tuple(args.values()),
            ).lastrowid

    def get_row(self, iid):
        with W.db() as c:
            return dict(c.execute("select * from intents where id=?", (iid,)).fetchone())

    def cmd(self, actor, action_type, args, key):
        return self.life.k.submit(
            {
                "idempotency_key": key,
                "principal": f"channel:{actor}",
                "actor_id": actor,
                "origin": "self",
                "action_type": action_type,
                "args": args,
            }
        )

    def test_optional_end_real_invite_binds_and_completes(self):
        # Chat proposed promise: "婉妲和由纪一起看电影"
        iid = self.add_intent("婉妲和由纪一起看电影", ["wanda", "yuki"], status="proposed")
        # Real kernel invite WITHOUT optional 'end_at' (exact shape emitted by life_model.py:178, 339!)
        r = self.cmd(
            "wanda",
            "invite",
            {"to": ["yuki"], "kind": "activity", "what": "movie_night", "location": "living", "start_at": self.clock.now() + 600},
            "inv-movie-1",
        )
        self.assertEqual(r["status"], "succeeded", r)
        pid = r["plan_id"]
        inv_id = self.life.plans.pending_for("yuki")[0]["inv_id"]
        self.clock.advance(60)
        r2 = self.cmd("yuki", "respond_invite", {"inv_id": inv_id, "answer": "yes", "reason": "好呀"}, "ans-movie-1")
        self.assertEqual(r2["status"], "succeeded", r2)

        self.proj.run()
        self.promises.run()
        after_accept = self.get_row(iid)

        # Move both to living and run movie_night together for 6500s
        self.clock.advance(600)
        for a in ("wanda", "yuki"):
            if self.life.k.actor(a)["location"] != "living":
                self.cmd(a, "move", {"to": "living"}, f"mv-{a}")
        self.clock.advance(120)
        self.life.k.tick()
        for a in ("wanda", "yuki"):
            ra = self.cmd(a, "begin_activity", {"activity": "movie_night", "seconds": 6500, "party": pid}, f"act-{a}")
            self.assertIn(ra["status"], ("running", "succeeded"), ra)
        self.clock.advance(6600)
        self.life.k.tick()
        self.proj.run()
        self.promises.run()
        after_done = self.get_row(iid)
        self.assertEqual((after_accept["status"], after_accept["plan_ref"]), ("accepted", pid))
        self.assertEqual((after_done["status"], after_done["plan_ref"]), ("completed", pid))

    def test_three_person_invite_waits_for_all_answers(self):
        iid_2p = self.add_intent("婉妲和由纪一起看电影", ["wanda", "yuki"], status="accepted", confidence=1.0)
        iid_3p = self.add_intent("婉妲、由纪和奈尔一起看电影", ["nell", "wanda", "yuki"], status="proposed")
        # Wanda invites BOTH yuki and nell (with explicit end_at)
        r = self.cmd(
            "wanda",
            "invite",
            {
                "to": ["yuki", "nell"],
                "kind": "activity",
                "what": "movie_night",
                "location": "living",
                "start_at": self.clock.now() + 600,
                "end_at": self.clock.now() + 7800,
            },
            "inv-3p",
        )
        pid = r["plan_id"]
        inv_yuki = self.life.plans.pending_for("yuki")[0]["inv_id"]
        inv_nell = self.life.plans.pending_for("nell")[0]["inv_id"]

        # Minute 1: Yuki answers yes first while Nell is still pending
        self.clock.advance(60)
        self.cmd("yuki", "respond_invite", {"inv_id": inv_yuki, "answer": "yes"}, "ans-yuki")
        self.proj.run()
        self.promises.run()

        self.assertIsNone(self.get_row(iid_2p)["plan_ref"])
        self.assertIsNone(self.get_row(iid_3p)["plan_ref"])

        # Minute 2: Nell also answers yes
        self.clock.advance(60)
        self.cmd("nell", "respond_invite", {"inv_id": inv_nell, "answer": "yes"}, "ans-nell")
        self.proj.run()
        self.promises.run()
        self.assertEqual((self.get_row(iid_2p)["status"], self.get_row(iid_2p)["plan_ref"]), ("accepted", None))
        self.assertEqual((self.get_row(iid_3p)["status"], self.get_row(iid_3p)["plan_ref"]), ("accepted", pid))
        self.perform_movie(pid, ("wanda", "yuki", "nell"))
        self.assertEqual(self.get_row(iid_3p)["status"], "completed")
        self.assertEqual(self.get_row(iid_2p)["status"], "accepted")

    def retry_finished_plan(self, outcome, backlog=False):
        iid = self.add_intent(
            "婉妲和由纪一起去公园",
            ["wanda", "yuki"],
            status="accepted",
            confidence=1.0,
            when_kind="day",
            due_from=self.clock.now(),
            due_to=self.clock.now() + 86400,
        )
        # Morning plan accepted, then lapses without happening
        r1 = self.cmd(
            "wanda",
            "invite",
            {
                "to": ["yuki"],
                "kind": "outing",
                "what": "stroll",
                "location": "place:park",
                "start_at": self.clock.now() + 600,
                "end_at": self.clock.now() + 600 + 4 * 3600,
            },
            "inv-am",
        )
        self.assertEqual(r1["status"], "succeeded", r1)
        pid_am = r1["plan_id"]
        inv_am = self.life.plans.pending_for("yuki")[0]["inv_id"]
        self.clock.advance(60)
        self.cmd("yuki", "respond_invite", {"inv_id": inv_am, "answer": "yes"}, "ans-am")
        if not backlog:
            self.proj.run()
            self.promises.run()
        self.life.plans.finish(pid_am, outcome)
        if not backlog:
            self.proj.run()
            self.promises.run()

        self.assertIsNone(self.get_row(iid)["plan_ref"])
        I.init()  # released plan stays retired across schema/init and exporter restart
        self.promises = PR.Promises(self.life, W.DB_PATH, self.proj, I)

        # Afternoon plan: they re-invite and actually go to park together!
        self.clock.advance(7200)
        r2 = self.cmd(
            "wanda",
            "invite",
            {
                "to": ["yuki"],
                "kind": "outing",
                "what": "stroll",
                "location": "place:park",
                "start_at": self.clock.now() + 600,
                "end_at": self.clock.now() + 600 + 4 * 3600,
            },
            "inv-pm",
        )
        self.assertEqual(r2["status"], "succeeded", r2)
        pid_pm = r2["plan_id"]
        inv_pm = self.life.plans.pending_for("yuki")[0]["inv_id"]
        self.clock.advance(60)
        self.cmd("yuki", "respond_invite", {"inv_id": inv_pm, "answer": "yes"}, "ans-pm")
        for _ in range(180):
            self.life.k.tick()
            self.life.extensions[0].step()
            if self.life.plans.plan(pid_pm)["state"] == "done":
                break
            self.clock.advance(60)
        self.proj.run()
        self.promises.run()
        self.assertEqual(self.life.plans.plan(pid_pm)["state"], "done")
        self.assertEqual((self.get_row(iid)["status"], self.get_row(iid)["plan_ref"]), ("completed", pid_pm))
        later = self.add_intent("婉妲和由纪一起去公园", ["wanda", "yuki"])
        self.promises.run()
        self.assertEqual(self.get_row(later)["status"], "proposed")
        with W.db() as db:
            old = db.execute("select intent,reason from intent_plan_claim where plan=?", (pid_am,)).fetchone()
            self.assertEqual(tuple(old) if old else None, None if backlog else (iid, "released"))

    def test_lapsed_plan_allows_later_retry(self):
        self.retry_finished_plan("lapsed")

    def test_cancelled_plan_allows_later_retry(self):
        self.retry_finished_plan("cancelled")

    def test_missed_plan_allows_later_retry(self):
        self.retry_finished_plan("missed")

    def test_done_without_fulfillment_allows_later_retry(self):
        self.retry_finished_plan("done")

    def test_early_home_without_fulfillment_allows_later_retry(self):
        self.retry_finished_plan("early_home")

    def test_failed_attempt_and_retry_in_one_backlog(self):
        self.retry_finished_plan("lapsed", backlog=True)

    def test_reopen_queue_drains_through_authority_with_phase1_off(self):
        with patch.dict(os.environ, {"INTENTS": "1", "INTENTS_KERNEL_MATCH": "off",
                                   "INTENTS_EXPIRY_V2": "0", "INTENTS_EXTRACT_V2": "0",
                                   "INTENTS_DECIDE_CTX": "0", "WORLD_KERNEL_ERRANDS_FROM_INTENTS": "0"}), \
                patch.object(W, "DB_PATH", os.path.join(self.kdir.name, "authority-world.db")):
            W.init()
            legacy.offline_world(W.DB_PATH)
            I.init()
            iid = self.add_intent("婉妲去公园", ["wanda"], status="completed", confidence=1.0)
            row = self.get_row(iid)
            I.request_reopen(iid, row["updated"], "op_rollback_1", now=self.clock.now())
            runner = AUTH.Authority(os.path.join(self.kdir.name, "authority.db"), W.DB_PATH, None, {},
                                    clock=self.clock, model=FakeLifeModel(), migrate_items=False, day_note=False)
            try:
                self.assertIsNotNone(runner.promises)
                runner.step()
                self.assertEqual(self.get_row(iid)["status"], "accepted")
                with W.db() as db:
                    self.assertEqual(db.execute("select state from intent_ops_queue where key='op_rollback_1'").fetchone()[0], "done")
                    self.assertEqual(db.execute("select count(*) from intent_evidence").fetchone()[0], 0)
            finally:
                runner.store.close()

    def perform_movie(self, pid, actors):
        self.clock.advance(600)
        for actor in actors:
            if self.life.k.actor(actor)["location"] != "living":
                self.cmd(actor, "move", {"to": "living"}, "move-" + actor)
        self.clock.advance(120)
        self.life.k.tick()
        for actor in actors:
            result = self.cmd(actor, "begin_activity",
                              {"activity": "movie_night", "seconds": 6500, "party": pid}, "movie-" + actor)
            self.assertIn(result["status"], ("running", "succeeded"), result)
        self.clock.advance(6600)
        self.life.k.tick()
        self.proj.run()
        self.promises.run()

    def settled_after_last_response(self, answer, backlog=False):
        iid = self.add_intent("婉妲和由纪一起看电影", ["wanda", "yuki"])
        r = self.cmd("wanda", "invite", {"to": ["yuki", "nell"], "kind": "activity", "what": "movie_night",
                                      "location": "living", "start_at": self.clock.now() + 600}, "invite")
        pid = r["plan_id"]
        first = self.life.plans.pending_for("yuki")[0]["inv_id"]
        last = self.life.plans.pending_for("nell")[0]["inv_id"]
        self.clock.advance(60)
        self.cmd("yuki", "respond_invite", {"inv_id": first, "answer": "yes"}, "first")
        if not backlog:
            self.proj.run()
            self.promises.run()
            self.assertIsNone(self.get_row(iid)["plan_ref"])
        if answer == "expired":
            self.clock.advance(1600)
            self.life.plans.sync()
        else:
            self.clock.advance(60)
            self.cmd("nell", "respond_invite", {"inv_id": last, "answer": answer}, "last")
        self.proj.run()
        self.promises.run()
        self.assertEqual((self.get_row(iid)["status"], self.get_row(iid)["plan_ref"]), ("accepted", pid))
        with W.db() as db:
            events = [json.loads(r[0]) for r in db.execute("select body from intent_evidence")]
        settled = [e for e in events if e["kind"] == "invite_settled"]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["members"], ["wanda", "yuki"])

    def test_last_decline_settles_accepted_members(self):
        self.settled_after_last_response("no")

    def test_last_expiry_settles_accepted_members(self):
        self.settled_after_last_response("expired")

    def test_backlog_does_not_recreate_partial_membership(self):
        self.settled_after_last_response("no", backlog=True)
