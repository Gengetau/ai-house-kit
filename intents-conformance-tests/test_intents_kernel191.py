"""#191: synthetic evidence, SQLite transactions, expiry/extraction and privacy contracts."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "world-kernel"))
from worldkernel import promise_rules as P, vocab_life1 as V
import intents as I
import world as W
from test_intents import IntentBase, NELL, WANDA, G


class Rules(IntentBase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {"INTENTS_KERNEL_MATCH": "on", "INTENTS_EXPIRY_V2": "1",
                                           "INTENTS_EXTRACT_V2": "1", "INTENTS_DECIDE_CTX": "1",
                                           "WORLD_KERNEL_ERRANDS_FROM_INTENTS": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        with W.db() as c:
            c.execute("create table if not exists kernel_bind(k text primary key,v text)")
            c.execute("insert or replace into kernel_bind values('world','test#1')")

    def intent(self, text="婉妲买两袋薯片带回来", party=None, **kw):
        now = self.now
        args = dict(actor="wanda", party=json.dumps(party or ["wanda"]), agreed=json.dumps(party or ["wanda"]),
                    kind="plan", text=text, status="accepted", source="chat",
                    refs=json.dumps([{"chat": G, "at": now, "said_at": now}]),
                    visibility="group", confidence=1, created=now, updated=now, last_action=now,
                    scope="world", beneficiary="[]")
        args.update(kw)
        with W.db() as c:
            iid = c.execute("insert into intents(" + ",".join(args) + ") values(" + ",".join("?"*len(args)) + ")",
                            tuple(args.values())).lastrowid
        return iid

    def ev(self, kind, iid=None, key=None, actor="wanda", plan="p1", **kw):
        n = len(self.evidence()) + 1
        body = dict(key=key or f"test#1#{n}", kind=kind, actor=actor, plan_id=plan, at=self.now+100,
                    item="chips", qty=1, account=actor, action_id=f"a{n}", resource_id=f"r{n}", intent_ref=iid,
                    beneficiary=[], place="living")
        body.update(kw)
        if iid:
            with W.db() as c:
                row = c.execute("select * from intents where id=?", (iid,)).fetchone()
                body["fingerprint"] = I._fingerprint(I._structured(row, self.now))
        with W.db() as c:
            c.execute("insert or ignore into intent_evidence values(?,?,?,?,?, 'queued')",
                      (body["key"], "test#1", n, body["at"], json.dumps(body)))
        return body

    def evidence(self):
        with W.db() as c:
            return [json.loads(r[0]) for r in c.execute("select body from intent_evidence")]

    def status(self, iid):
        with W.db() as c:
            return c.execute("select status from intents where id=?", (iid,)).fetchone()[0]

    def test_closed_parser_keeps_all_terms(self):
        terms = P.compile_terms("婉妲去超市买两袋薯片带回来", V)
        self.assertEqual([t["kind"] for t in terms], ["arrived", "purchased", "brought_home"])
        self.assertEqual(terms[1]["qty"], 2)
        self.assertEqual(P.compile_terms("婉妲买牛排", V), [{"kind":"purchased","item":"ready_steak","qty":1}])
        self.assertEqual(P.compile_terms("婉妲买薯片并修好电脑", V), [{"kind":"unsupported"}])
        self.assertIn({"kind":"handoff"}, P.compile_terms("婉妲买薯片交给由纪", V))
        self.assertEqual(P.compile_terms("婉妲和由纪一起看电影", V)[0]["activity"], "movie_night")

    def test_invitation_window_and_plan_claim_survive_terminal_rows(self):
        text="婉妲和由纪一起去公园"
        iid=self.intent(text,party=['wanda','yuki'],status='proposed',when_kind='day',
                        due_from=self.now+5*86400,due_to=self.now+6*86400)
        self.ev('invite_answered',actor='yuki',plan='monday',answer='yes',members=['wanda','yuki'],
                place='place:park',activity='walk_park',plan_start=self.now+5400,plan_end=self.now+7200)
        I.sweep(self.now+200)
        self.assertEqual(self.status(iid),'proposed')
        I.sweep(self.now+86400)
        self.assertEqual(self.status(iid),'proposed')
        # The actual future invitation may bind, but its early finish must not
        # shorten the explicit Saturday deadline to Tuesday.
        self.ev('invite_answered',actor='yuki',plan='saturday',answer='yes',members=['wanda','yuki'],
                place='place:park',activity='walk_park',plan_start=self.now+5*86400,plan_end=self.now+5*86400+7200)
        I.sweep(self.now+201)
        with W.db() as c:
            self.assertEqual(c.execute('select plan_ref from intents where id=?',(iid,)).fetchone()[0],'saturday')
        self.ev('plan_finished',plan='saturday',at=self.now+7200)
        I.sweep(self.now+86400)
        self.assertEqual(self.status(iid),'accepted')
        with W.db() as c:
            c.execute("update intents set status='cancelled' where id=?",(iid,))
        second=self.intent(text,party=['wanda','yuki'],status='proposed',
                           due_from=self.now+5*86400,due_to=self.now+6*86400)
        I.init()  # real schema/init boundary, claims persist across restarts
        I.sweep(self.now+86401)
        self.assertEqual(self.status(second),'proposed')

    def test_expiry_ignores_plan_finish_from_restored_old_stream(self):
        iid=self.intent('婉妲去公园',plan_ref='p1')
        body=dict(key='old#1',kind='plan_finished',plan_id='p1',at=self.now+100)
        with W.db() as c:
            c.execute("insert into intent_evidence values(?,?,?,?,?,'queued')",
                      ('old#1','old-stream',1,self.now+100,json.dumps(body)))
        I.sweep(self.now+I.LATE_S+200)
        self.assertEqual(self.status(iid),'accepted')
        self.ev('plan_finished',at=self.now+300)
        I.sweep(self.now+I.LATE_S+301)
        self.assertEqual(self.status(iid),'missed')

    def test_invitation_missing_window_refuses_dated_binding(self):
        iid=self.intent('婉妲去公园',status='proposed',due_from=self.now,due_to=self.now+7200)
        self.ev('invite_answered',answer='yes',members=['wanda'],place='place:park',activity='walk_park')
        I.sweep(self.now+200)
        self.assertEqual(self.status(iid),'proposed')

    def test_ambiguous_trip_never_reused_after_one_agreement_leaves_open(self):
        first=self.intent('婉妲去公园',due_from=self.now,due_to=self.now+3600,when_kind='day')
        second=self.intent('婉妲去公园',when_kind='vague')
        self.ev('arrived',place='place:park')
        self.assertEqual(I.sweep(self.now+200),0)
        I.sweep(self.now+3600+86401)
        self.assertEqual(self.status(first),'missed')
        I.init()
        self.assertEqual(I.sweep(self.now+3600+86402),0)
        self.assertEqual(self.status(second),'accepted')

    def test_completed_trip_never_reused_by_second_agreement(self):
        first=self.intent('婉妲去公园')
        self.ev('arrived',place='place:park')
        self.assertEqual(I.sweep(self.now+200),1)
        second=self.intent('婉妲去公园')
        self.assertEqual(I.sweep(self.now+201),0)
        self.assertEqual([self.status(first),self.status(second)],['completed','accepted'])

    def test_short_place_names_have_positive_and_wrong_place_controls(self):
        for name,pid in [('公园','park'),('书店','bookshop'),('咖啡店','cafe'),('神社','shrine'),
                         ('图书馆','library'),('超市','supermarket')]:
            with self.subTest(name=name):
                iid=self.intent('婉妲去'+name)
                plan='short-'+pid
                self.ev('arrived',plan=plan,place='place:wrong')
                self.assertEqual(I.sweep(self.now+200),0)
                self.ev('arrived',plan=plan,place='place:'+pid)
                self.assertEqual(I.sweep(self.now+200),1)
                self.assertEqual(self.status(iid),'completed')

    def test_home_rooms_and_activity_variants_have_positive_controls(self):
        cases=[('婉妲去院子观星',['wanda'],'stargazing','yard'),
               ('婉妲在客厅看书',['wanda'],'reading','living'),
               ('婉妲和由纪观星',['wanda','yuki'],'stargazing_together','yard'),
               ('婉妲和由纪看电影',['wanda','yuki'],'movie_night','living'),
               ('婉妲看电影',['wanda'],'movie_night','living')]
        for index,(text,party,aid,place) in enumerate(cases):
            with self.subTest(text=text):
                iid=self.intent(text,party=party)
                terms=P.compile_terms(text,V,party)
                self.assertEqual(terms[0]['activity'],aid)
                for actor in party:
                    self.ev('activity',actor=actor,plan='home-'+str(index),activity=aid,place='place:wrong',
                            started_at=self.now+1,ended_at=self.now+1200,outcome='succeeded')
                if '在客厅' in text or '去院子' in text:
                    self.assertEqual(I.sweep(self.now+1300),0)
                for actor in party:
                    self.ev('activity',actor=actor,plan='home-'+str(index),activity=aid,place=place,
                            started_at=self.now+1,ended_at=self.now+1200,outcome='succeeded')
                self.assertEqual(I.sweep(self.now+1300),1)
                self.assertEqual(self.status(iid),'completed')

    def test_qingkong_park_requires_arrival_and_actual_joint_activity(self):
        # 11-20 晴空都 hotfix: the real-city places (吉祥寺 cinema) left life1; same rule on a 晴空都 place by its short name
        text='婉妲和由纪一起去南琉璃公园晒太阳'
        iid=self.intent(text,party=['wanda','yuki'])
        self.assertEqual(P.compile_terms(text,V,['wanda','yuki'])[0]['place'],'place:minami_park')
        self.assertEqual(P.compile_terms(text,V,['wanda','yuki'])[1]['activity'],'sunbathing')
        for actor in ['wanda','yuki']:
            self.ev('arrived',actor=actor,place='place:minami_park')
            self.ev('departed',actor=actor,at=self.now+2000,place='place:minami_park')
            self.ev('activity',actor=actor,place='place:minami_park',activity='stroll',
                    started_at=self.now+101,ended_at=self.now+1300,outcome='succeeded')
        self.assertEqual(I.sweep(self.now+2100),0)
        self.ev('activity',place='place:minami_park',activity='sunbathing',
                started_at=self.now+101,ended_at=self.now+1300,outcome='succeeded')
        self.assertEqual(I.sweep(self.now+2100),0)
        self.ev('activity',actor='yuki',place='place:minami_park',activity='sunbathing',
                started_at=self.now+101,ended_at=self.now+1300,outcome='succeeded')
        self.assertEqual(I.sweep(self.now+2100),1)
        self.assertEqual(self.status(iid),'completed')

    def test_partial_delivery_and_duplicates(self):
        iid = self.intent()
        a = self.ev("purchased", iid)
        self.ev("plan_finished", iid)
        self.assertEqual(I.sweep(self.now+200), 0)
        self.ev("purchased", iid, action_id=a["action_id"], resource_id=a["resource_id"])
        self.assertEqual(I.sweep(self.now+200), 0)
        b = self.ev("purchased", iid, plan="p2")
        self.ev("brought_home", iid, action_id=a["action_id"], resource_id=a["resource_id"])
        self.assertEqual(I.sweep(self.now+200), 0)
        self.ev("brought_home", iid, action_id=b["action_id"], resource_id=b["resource_id"], plan="p2")
        self.assertEqual(I.sweep(self.now+200), 1)
        self.assertEqual(I.sweep(self.now+200), 0)
        self.assertEqual(self.status(iid), "completed")

    def test_composite_purchase_requires_same_trip_conditions(self):
        iid = self.intent("婉妲去超市买两袋薯片带回来")
        self.ev("arrived", place="place:supermarket", plan="unrelated")
        for plan in ("p1", "p2"):
            a = self.ev("purchased", iid, plan=plan)
            self.ev("brought_home", iid, plan=plan, action_id=a["action_id"], resource_id=a["resource_id"])
        self.assertEqual(I.sweep(self.now+200), 0)
        self.ev("arrived", place="place:supermarket", plan="p1")
        self.assertEqual(I.sweep(self.now+200), 0)
        self.ev("arrived", place="place:supermarket", plan="p2")
        self.assertEqual(I.sweep(self.now+200), 1)

    def test_bound_plan_and_preexisting_activity_do_not_fulfill(self):
        iid = self.intent("婉妲买薯片", plan_ref="p1")
        self.ev("purchased", iid, plan="p2")
        self.assertEqual(I.sweep(self.now+200), 0)
        self.ev("purchased", iid, plan="p1")
        self.assertEqual(I.sweep(self.now+200), 1)
        iid = self.intent("婉妲看电影")
        self.ev("activity", activity="movie_night", started_at=self.now-1000,
                ended_at=self.now+100, outcome="succeeded")
        self.assertEqual(I.sweep(self.now+200), 0)

    def test_handoff_and_owner_never_automatically_complete(self):
        a = self.intent("婉妲买薯片交给由纪")
        b = self.intent("婉妲去公园", party=["wanda","owner"])
        self.ev("purchased", a)
        self.ev("arrived", b, place="place:park")
        I.sweep(self.now+200)
        self.assertEqual([self.status(a), self.status(b)], ["accepted","accepted"])

    def test_purchase_before_promise_and_wrong_account_ignored(self):
        iid = self.intent("婉妲买薯片")
        self.ev("purchased", iid, at=self.now-1)
        self.ev("purchased", iid, account="house")
        self.ev("owner_gave", iid)
        self.assertEqual(I.sweep(self.now+200), 0)

    def test_shadow_and_revalidation(self):
        iid = self.intent("婉妲买薯片")
        self.ev("purchased", iid)
        with patch.dict(os.environ, {"INTENTS_KERNEL_MATCH":"shadow"}):
            self.assertEqual(I.sweep(self.now+200),0)
        self.assertEqual(self.status(iid),"accepted")
        with W.db() as c:
            c.execute("update intents set status='cancelled' where id=?", (iid,))
        self.assertEqual(I.sweep(self.now+200),0)
        self.assertEqual(self.status(iid),"cancelled")

    def test_shadow_can_promote_after_same_judge(self):
        iid = self.intent("婉妲买薯片")
        self.ev("purchased", iid)
        with patch.dict(os.environ, {"INTENTS_KERNEL_MATCH":"shadow"}):
            I.sweep(self.now+200)
        self.assertEqual(I.sweep(self.now+201),1)

    def test_unrelated_people_and_stale_version_ignored(self):
        iid = self.intent("婉妲买薯片")
        self.ev("purchased", iid, actor="yuki")
        self.ev("purchased", iid)
        with W.db() as c:
            c.execute("update intents set due_to=? where id=?", (self.now+10000,iid))
        self.assertEqual(I.sweep(self.now+200),0)

    def test_ambiguity_and_plan_finished(self):
        a = self.intent("婉妲去公园")
        b = self.intent("婉妲去公园")
        self.ev("arrived", place="place:park")
        self.ev("plan_finished")
        self.assertEqual(I.sweep(self.now+200),0)
        self.assertEqual([self.status(a),self.status(b)],["accepted","accepted"])

    def test_nonexact_two_trips_is_ambiguous(self):
        iid = self.intent("婉妲去公园")
        self.ev("arrived", place="place:park")
        self.ev("arrived", plan="p2", place="place:park")
        self.assertEqual(I.sweep(self.now+200),0)

    def test_activity_needs_real_overlapping_participants(self):
        iid = self.intent("婉妲和由纪一起看电影", party=["wanda","yuki"])
        self.ev("activity", activity="movie_night", started_at=self.now+1, ended_at=self.now+1200, outcome="succeeded")
        self.assertEqual(I.sweep(self.now+1300),0)
        self.ev("activity", actor="yuki", activity="movie_night", started_at=self.now+1000,
                ended_at=self.now+2000, outcome="cancelled")
        self.assertEqual(I.sweep(self.now+2100),0)
        self.ev("activity", actor="yuki", activity="movie_night", started_at=self.now+500,
                ended_at=self.now+1800, outcome="cancelled")
        self.assertEqual(I.sweep(self.now+2100),1)

    def test_joint_arrivals_need_actual_stay(self):
        iid = self.intent("婉妲和由纪一起去公园", party=["wanda","yuki"])
        self.ev("arrived", place="place:park")
        self.ev("arrived", actor="yuki", place="place:park")
        self.ev("departed", at=self.now+400)
        self.ev("departed", actor="yuki", at=self.now+1200)
        self.assertEqual(I.sweep(self.now+1300),0)

    def test_listing_is_readonly_and_kernel_ref_is_safe(self):
        iid = self.intent("婉妲去公园", due_to=self.now-86401)
        self.assertEqual(len(I.listing(now=self.now)),1)
        self.assertEqual(self.status(iid),"accepted")
        with W.db() as c:
            c.execute("update intents set refs=? where id=?",(json.dumps([
                {"chat":G,"at":self.now},{"kernel":"test#1#1","at":self.now}]),iid))
            row=c.execute("select * from intents where id=?",(iid,)).fetchone()
            self.assertTrue(I._same_audience(row,"wanda",G,False))

    def test_expiry_table(self):
        for kind,days in (("",14),("vague",30),("after",30)):
            iid=self.intent(when_kind=kind)
            I.sweep(self.now+days*86400-1)
            self.assertEqual(self.status(iid),"accepted")
            I.sweep(self.now+days*86400+1)
            self.assertEqual(self.status(iid),"expired")
        iid=self.intent(due_to=self.now+1,when_kind="day")
        I.sweep(self.now+86400)
        self.assertEqual(self.status(iid),"accepted")
        I.sweep(self.now+86402)
        self.assertEqual(self.status(iid),"missed")

    def test_beneficiary_request_requires_her_affirmative(self):
        op={"op":"intent","text":"奈尔帮婉妲买薯片","kind":"promise","for":["婉妲"],"scope":"world",
            "quote":"帮我买薯片","agreed":["奈尔"]}
        lines=[f"{WANDA} 帮我买薯片",f"{NELL} 好呀"]
        got=I.apply_chat("nell",G,False,[op],lines,now=self.now,times=[self.now-100,self.now-90])
        self.assertEqual(got["intents"],1,got)
        r=self.rows()[0]
        self.assertEqual(r["party"],["nell"])
        self.assertEqual(json.loads(r["beneficiary"]),["wanda"])
        self.assertEqual(r["refs"][0]["said_at"],self.now-100)

    def test_context_and_errands_scope(self):
        good=self.intent("婉妲买薯片",beneficiary='["yuki"]')
        self.intent("婉妲买汽水",visibility="private",refs=json.dumps([{"chat":"private","at":self.now}]))
        self.intent("婉妲买糖果",refs=json.dumps([{"chat":"group:999","at":self.now}]))
        self.assertEqual([r["id"] for r in I.kernel_context("wanda",self.now)],[good])
        rows=I.errands("wanda",self.now)
        self.assertEqual(rows[0]["beneficiary"],["yuki"])
        self.assertEqual(rows[0]["account"],"wanda")
        self.ev("purchased",good,beneficiary=["yuki"])
        self.assertNotIn(good,[r["intent"] for r in I.errands("wanda",self.now+200)])

    def test_reopen_idempotence_and_no_recompletion(self):
        iid=self.intent("婉妲买薯片")
        self.ev("purchased",iid)
        I.sweep(self.now+200)
        I.request_reopen(iid,self.now+200,"synthetic-op",self.now+201)
        I.request_reopen(iid,self.now+200,"synthetic-op",self.now+201)
        I.sweep(self.now+202)
        self.assertEqual(self.status(iid),"accepted")
        self.assertEqual(I.sweep(self.now+203),0)

    def test_reopen_rejects_expired_and_other_noncompleted_states(self):
        for status in ("expired", "missed", "declined", "cancelled", "proposed", "accepted"):
            with self.subTest(status=status):
                iid=self.intent(status=status,due_to=self.now-2*86400)
                with self.assertRaises(ValueError):
                    I.request_reopen(iid,self.now,"reject-"+status,self.now+1)
                self.assertFalse(next(r for r in I.listing(now=self.now+1) if r['id']==iid)['can_reopen'])
                with W.db() as c:
                    self.assertEqual(c.execute("select count(*) from intent_ops_queue where intent=?",(iid,)).fetchone()[0],0)
                I.sweep(self.now+2)
                if status in I.ENDED:
                    self.assertEqual(self.status(iid),status)
        with W.db() as c:
            self.assertEqual(c.execute("select count(*) from events where kind='intent_reopened'").fetchone()[0],0)

    def test_reopen_cannot_manufacture_joint_consent(self):
        for status in ('declined','completed'):
            with self.subTest(status=status):
                iid=self.intent(party=['wanda','yuki'],agreed='["wanda"]',status=status)
                with self.assertRaises(ValueError):
                    I.request_reopen(iid,self.now,'consent-'+status,self.now+1)
                # Also reject receipts already queued by the earlier implementation.
                with W.db() as c:
                    c.execute("insert into intent_ops_queue values(?,?,?,?, 'queued')",
                              ('legacy-'+status,iid,self.now,self.now+1))
                I.sweep(self.now+2)
                self.assertEqual(self.status(iid),status)
                with W.db() as c:
                    self.assertEqual(c.execute("select agreed from intents where id=?",(iid,)).fetchone()[0],'["wanda"]')
                    self.assertEqual(c.execute("select state from intent_ops_queue where key=?",('legacy-'+status,)).fetchone()[0],'stale')

    def test_reopen_preserves_dated_and_finished_plan_deadlines(self):
        dated=self.intent(status='completed',due_from=self.now-3*86400,due_to=self.now-2*86400)
        finished=self.intent(status='completed',plan_ref='finished')
        self.ev('plan_finished',plan='finished',at=self.now-2*86400)
        for iid in (dated,finished):
            with self.assertRaises(ValueError):
                I.request_reopen(iid,self.now,'old-window-'+str(iid),self.now+1)
            self.assertFalse(next(r for r in I.listing(now=self.now+1) if r['id']==iid)['can_reopen'])
        I.sweep(self.now+2)
        self.assertEqual(self.status(dated),'completed')
        self.assertEqual(self.status(finished),'completed')

    def test_reopen_rechecks_window_when_queue_is_consumed(self):
        iid=self.intent(status='completed',due_to=self.now,when_kind='exact')
        self.assertTrue(next(r for r in I.listing(now=self.now+1) if r['id']==iid)['can_reopen'])
        I.request_reopen(iid,self.now,'window-race',self.now+1)
        I.sweep(self.now+I.LATE_S+1)
        self.assertEqual(self.status(iid),'completed')
        with W.db() as c:
            self.assertEqual(c.execute("select state from intent_ops_queue where key='window-race'").fetchone()[0],'stale')
            self.assertEqual(c.execute("select count(*) from events where kind='intent_reopened'").fetchone()[0],0)


    def test_invitation_links_without_claiming_attendance(self):
        iid=self.intent("婉妲和由纪一起看电影",party=["wanda","yuki"],status="proposed")
        self.ev("invite_answered",actor="yuki",members=["wanda","yuki"],answer="yes",
                activity="movie_night",place="living",plan_start=self.now+200,plan_end=self.now+7200)
        self.assertEqual(I.sweep(self.now+200),0)
        with W.db() as c:
            row=c.execute("select * from intents where id=?",(iid,)).fetchone()
        self.assertEqual((row["status"],row["plan_ref"]),("accepted","p1"))
        self.ev("plan_finished",at=self.now+300)
        I.sweep(self.now+300+I.LATE_S+1)
        self.assertEqual(self.status(iid),"missed")

    def test_shadow_does_not_accept_kernel_invitation(self):
        iid=self.intent("婉妲和由纪一起看电影",party=["wanda","yuki"],status="proposed")
        self.ev("invite_answered",actor="yuki",members=["wanda","yuki"],answer="yes",
                activity="movie_night",place="living")
        with patch.dict(os.environ,{"INTENTS_KERNEL_MATCH":"shadow"}):
            I.sweep(self.now+200)
        self.assertEqual(self.status(iid),"proposed")

    def test_seven_days_twenty_promises_two_restarts(self):
        from worldkernel.store import Store, VirtualClock
        from worldkernel.life import Life
        from worldkernel.outings import Outings
        from worldkernel.promises import Promises
        from worldkernel.legacy import Projector
        from test_life_outings import prices
        with W.db() as c:
            c.execute("insert or replace into kernel_bind values('mode','offline')")
        positives=[self.intent("婉妲买薯片") for _ in range(10)]
        negatives=[self.intent("婉妲买薯片交给由纪") for _ in range(5)]
        negatives += [self.intent("婉妲和房主去公园",party=["wanda","owner"]) for _ in range(5)]
        clock=VirtualClock(self.now)
        path=str(Path(self.temp.name)/"promises-week.db") if hasattr(self,"temp") else str(Path(W.DB_PATH).with_name("promises-week.db"))
        def build():
            store=Store(path,clock,world_id="test")
            store.acquire()
            life=Life(store,model=None,weather=lambda _:{"rain":False,"temp_c":15},
                      extensions=[lambda l:Outings(l,places=prices(),targets={})])
            p=Promises(life,W.DB_PATH,Projector(life,W.DB_PATH),I)
            life.promise_source=p
            life.extensions.append(p)
            if life.get("promises:cursor") is None:
                life.put("promises:cursor",0)
            return life,p
        life,p=build()
        errors=[]
        try:
            for tick in range(7*24*4):
                if tick in (2*24*4,4*24*4):
                    life.s.release(); life.s.close()
                    life,p=build()
                    life.k.resume(clock.now()-900)
                try:
                    life.step()
                    p.run()
                except Exception as e:
                    errors.append(type(e).__name__)
                    break
                clock.advance(900)
            states={iid:self.status(iid) for iid in positives+negatives}
            self.assertEqual(errors,[])
            self.assertTrue(all(states[i]=="completed" for i in positives),states)
            self.assertTrue(all(states[i]!="completed" for i in negatives),states)
            print("7d/20 promises/2 restarts: completed=10 false_completed=0 kernel_errors=0")
        finally:
            life.s.close()


    def test_future_and_conditional_errands_wait(self):
        self.intent("婉妲买薯片",due_from=self.now+3*86400,due_to=self.now+4*86400)
        self.intent("婉妲买汽水",when_kind="after")
        self.assertEqual(I.errands("wanda",self.now),[])
        self.assertEqual(len(I.errands("wanda",self.now+3*86400)),1)

    def test_reopened_purchases_do_not_rebuy(self):
        iid=self.intent("婉妲买薯片")
        self.ev("purchased",iid)
        I.sweep(self.now+200)
        I.request_reopen(iid,self.now+200,"no-rebuy",self.now+201)
        I.sweep(self.now+202)
        self.assertNotIn(iid,[r["intent"] for r in I.errands("wanda",self.now+203)])

    def test_sqlite_failure_rolls_back_receipt_consumption(self):
        iid=self.intent("婉妲买薯片")
        self.ev("purchased",iid)
        with W.db() as c:
            c.execute("create trigger synthetic_abort before update of status on intents "
                      "when NEW.status='completed' begin select raise(ABORT,'synthetic crash'); end")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            I.sweep(self.now+200)
        self.assertEqual(self.status(iid),"accepted")
        with W.db() as c:
            self.assertEqual(c.execute("select state from intent_evidence").fetchone()[0],"queued")
            c.execute("drop trigger synthetic_abort")
        self.assertEqual(I.sweep(self.now+201), 1)
