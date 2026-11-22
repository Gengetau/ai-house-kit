"""real kernel actions/receipts, retries and extension isolation (all offline)."""
import contextlib
import json
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from test_life_outings import Base
from worldkernel import legacy, promises
from worldkernel.store import LockHeld


class ExportTests(Base):
    def setUp(self):
        super().setUp()
        self.world = os.path.join(self.temp.name, "world.db")
        legacy.offline_world(self.world)
        with contextlib.closing(sqlite3.connect(self.world)) as c:
            with c:
                c.execute("create table intent_evidence(key text primary key,stream text,event_id int,at real,body text,state text default 'queued')")
        self.projector = legacy.Projector(self.l, self.world)
        self.p = promises.Promises(self.l, self.world, self.projector,
                                  SimpleNamespace(sweep=lambda _:None,kernel_context=lambda *_:[],errands=lambda *_:[]))
        self.mode = patch.dict(os.environ, {"INTENTS_KERNEL_MATCH":"shadow"})
        self.mode.start()
        self.addCleanup(self.mode.stop)
        self.l.put("promises:cursor", 0)

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.world)) as c:
            return [json.loads(r[0]) for r in c.execute("select body from intent_evidence order by event_id")]

    def test_outing_actual_arrival_stay_activity_and_lifecycle(self):
        pid=self.plan()
        self.finish(pid,restarts=())
        self.p.run()
        rows=self.rows()
        for actor in ("nell","wanda"):
            self.assertEqual(len([e for e in rows if e["actor"]==actor and e["kind"]=="arrived"]),1)
            activity=[e for e in rows if e["actor"]==actor and e["kind"]=="activity"]
            self.assertTrue(activity)
            self.assertTrue(all(e["ended_at"]>e["started_at"] and e["plan_id"]==pid for e in activity))
            self.assertEqual(len([e for e in rows if e["actor"]==actor and e["kind"]=="departed"]),1)
        self.assertEqual(len([e for e in rows if e["kind"]=="plan_finished"]),1)
        before=len(rows)
        self.p.run()
        self.assertEqual(len(self.rows()),before)

    def test_crash_after_commit_redelivers_without_duplicates(self):
        self.plan()
        self.s.faults["promises:after_world_commit"]=lambda:(_ for _ in ()).throw(RuntimeError("crash"))
        with self.assertRaises(RuntimeError):
            self.p.run()
        self.assertEqual(self.l.get("promises:cursor"),0)
        del self.s.faults["promises:after_world_commit"]
        self.p.run()
        self.assertGreater(self.l.get("promises:cursor"),0)
        self.assertEqual(len({e["key"] for e in self.rows()}),len(self.rows()))

    def test_stream_mismatch_refuses_before_cursor_write(self):
        with contextlib.closing(sqlite3.connect(self.world)) as c:
            with c:
                c.execute("insert or replace into kernel_bind values('world','other#2')")
        with self.assertRaises(RuntimeError):
            self.p.run()
        self.assertEqual(self.l.get("promises:cursor"),0)

    def test_plan_finished_is_idempotent_and_first_outcome_wins(self):
        pid=self.plan()
        self.assertTrue(self.l.plans.finish(pid,"cancelled"))
        self.assertFalse(self.l.plans.finish(pid,"done"))
        events=self.s.db.execute("select payload from events where kind='plan_finished'").fetchall()
        self.assertEqual(len(events),1)
        self.assertEqual(json.loads(events[0][0])["outcome"],"cancelled")

    def test_express_strips_decision_only_context(self):
        got=[]
        self.l.model=lambda ctx:got.append(ctx) or {}
        self.l.life_context=lambda *_:{"names":{"nell":"奈尔"},"qq_chat":[{"secret":"synthetic"}],
                                       "promises":[{"text":"synthetic private"}]}
        self.l._ask({"actor":"nell","purpose":"express"})
        self.assertEqual(got[-1]["life"],{"names":{"nell":"奈尔"}})
        self.l._ask({"actor":"nell","purpose":"decide"})
        self.assertIn("promises",got[-1]["life"])

    def test_no_extra_step_export_or_model_call(self):
        self.l.extensions.append(self.p)
        self.p.run=lambda: (_ for _ in ()).throw(AssertionError("export inside life.step"))
        self.l.step()

    def test_private_errand_and_public_stock_separate_accounts(self):
        row={"intent":1,"item":"chips","qty":2,"account":"nell","beneficiary":["wanda"],"fingerprint":"v1"}
        self.l.promise_source=SimpleNamespace(errands=lambda a:[row] if a=="nell" else [])
        self.o.shopping.targets={"chips":1}
        self.o.shopping.step()
        current=self.l.get("shopping:current")
        self.assertEqual(current["actor"],"nell")
        self.assertEqual(current["items_from"],[row])
        pid=current["plan_id"]
        before_house=self.o.wallet.available("house")
        before_nell=self.o.wallet.available("nell")
        self.finish(pid)
        self.p.run()
        purchases=[e for e in self.rows() if e["kind"]=="purchased"]
        private=[e for e in purchases if e.get("intent_ref")==1]
        public=[e for e in purchases if not e.get("intent_ref")]
        self.assertEqual([(e["qty"],e["account"]) for e in private],[(2,"nell")])
        self.assertEqual([(e["qty"],e["account"]) for e in public],[(1,"house")])
        self.assertEqual(before_house-self.o.wallet.available("house"),170)
        self.assertEqual(before_nell-self.o.wallet.available("nell"),340)
        brought=[e for e in self.rows() if e["kind"]=="brought_home" and e.get("intent_ref")==1]
        self.assertTrue(brought)
        self.assertEqual(brought[0]["resource_id"],private[0]["resource_id"])
        self.assertEqual(brought[0]["action_id"],private[0]["action_id"])

    def test_ready_steak_uses_owner_price_and_private_wallet(self):
        row={"intent":1,"item":"ready_steak","qty":1,"account":"nell","beneficiary":["wanda"],"fingerprint":"v1"}
        self.l.promise_source=SimpleNamespace(errands=lambda a:[row] if a=="nell" else [])
        self.o.shopping.targets={}
        self.o.shopping.step()
        pid=self.l.get("shopping:current")["plan_id"]
        before_nell=self.o.wallet.available("nell")
        before_house=self.o.wallet.available("house")
        self.finish(pid)
        self.p.run()
        purchased=[e for e in self.rows() if e["kind"]=="purchased" and e.get("intent_ref")==1]
        self.assertEqual([(e["item"],e["qty"],e["account"]) for e in purchased],[("ready_steak",1,"nell")])
        self.assertEqual(before_nell-self.o.wallet.available("nell"),1500)
        self.assertEqual(before_house,self.o.wallet.available("house"))

    def test_changed_errand_is_not_bought(self):
        row={"intent":1,"item":"chips","qty":1,"account":"nell","beneficiary":[],"fingerprint":"v1"}
        self.l.promise_source=SimpleNamespace(errands=lambda a:[row] if a=="nell" else [])
        self.o.shopping.targets={}
        self.o.shopping.step()
        pid=self.l.get("shopping:current")["plan_id"]
        self.l.promise_source.errands=lambda _: []
        # Pure withdrawn private lines cancel without freezing public restocks.
        for _ in range(180):
            self.o.step()
            if self.l.plans.plan(pid)['state'] == 'cancelled':
                break
            self.clock.advance(60)
            self.l.k.tick()
        self.assertEqual(self.l.plans.plan(pid)['state'], 'cancelled')
        self.assertNotIn('retry_after', self.l.get('shopping:current'))
        self.assertEqual(self.s.db.execute("select count(*) from events where kind='purchased'").fetchone()[0],0)
        event=self.s.db.execute("select payload from events where kind='plan_finished'").fetchone()
        self.assertEqual(json.loads(event[0])["outcome"],"cancelled")

    def test_withdrawn_private_line_does_not_cancel_public_or_other_private_lines(self):
        stale={"intent":1,"item":"soda","qty":1,"account":"nell","beneficiary":[],"fingerprint":"v1"}
        valid=dict(stale,intent=2,item="candy",fingerprint="v2")
        self.l.promise_source=SimpleNamespace(errands=lambda a:[stale,valid] if a=="nell" else [])
        self.o.shopping.targets={"chips":1}
        self.o.shopping.step()
        pid=self.l.get('shopping:current')['plan_id']
        self.l.promise_source.errands=lambda a:[valid] if a=='nell' else []
        self.finish(pid)
        self.p.run()
        purchases=[e for e in self.rows() if e['kind']=='purchased']
        self.assertEqual({(e['item'],e['account']) for e in purchases},{('chips','house'),('candy','nell')})
        self.assertNotIn('retry_after',self.l.get('shopping:current'))

    def test_affordable_candidate_and_three_per_line_six_per_trip(self):
        with self.s.tx() as db:
            db.execute("update wallets set balance=50 where account='nell'")
        row={"intent":1,"item":"chips","qty":5,"account":"nell","beneficiary":[],"fingerprint":"v1"}
        wanda=[dict(row,intent=2,account='wanda'),dict(row,intent=3,account='wanda',item='soda')]
        self.l.promise_source=SimpleNamespace(errands=lambda a:[row] if a=='nell' else wanda if a=='wanda' else [])
        self.o.shopping.targets={}
        self.o.shopping.step()
        cur=self.l.get('shopping:current')
        self.assertEqual(cur['actor'],'wanda')
        self.assertEqual([r['qty'] for r in cur['items_from']],[3,3])

    def test_empty_rainy_day_does_not_consume_rain_audit(self):
        self.o.shopping.targets={}
        self.l.promise_source=SimpleNamespace(errands=lambda _:[])
        self.l.weather=lambda _:{'rain':True,'temp_c':15}
        self.o.rain_ready=lambda _:False
        self.o.shopping.step()
        self.assertEqual(self.s.db.execute("select count(*) from audits where kind='errand_held'").fetchone()[0],0)
