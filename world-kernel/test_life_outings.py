"""Full-life extension contracts on f3a7f1b; all prices/weather/model inputs are synthetic."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from worldkernel.life import Life, ROSTER
from worldkernel import vocab_life1 as V
from worldkernel.outings import Outings,is_open
from worldkernel.kernel import Refused
from worldkernel.store import Store,VirtualClock
from worldkernel.cognition import Cognition,FakeModel
from worldkernel.budget import Budget
from worldkernel.wallet import JST, HOUSE_ALLOWANCE


def at(day='2026-11-02',clock='10:00'):
    return datetime.fromisoformat(day+'T'+clock).replace(tzinfo=JST).timestamp()


def prices():
    out=copy.deepcopy(V.PLACES)
    for p in out.values():
        p['travel']['price_yen']=0 if p['travel']['mode']=='walk' else 500
        p['activity_costs_yen']={a:0 for a in p['activities']}
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=os.path.join(self.temp.name,'w.db'); self.clock=VirtualClock(at())
        self.rain=False
        self.make()
        self.addCleanup(lambda:self.s.close())
        self.n=0

    def make(self):
        self.s=Store(self.path,self.clock,world_id='test/life1')
        self.s.acquire()
        self.l=Life(self.s,model=None,weather=lambda _:dict(rain=self.rain,temp_c=15),
                    extensions=[lambda l:Outings(l,places=prices(),targets={})])
        self.o=self.l.extensions[0]

    def restart(self):
        self.s.release(); self.s.close(); self.make(); self.l.k.resume(self.clock.now()-60)

    def command(self,actor,action,args,key=None):
        self.n+=1
        return self.l.k.submit(dict(idempotency_key=key or f'test:{self.n}',principal=f'channel:{actor}',
                                    actor_id=actor,origin='self',action_type=action,args=args))

    def plan(self,place='park',start=None,end=None,kind='outing',host='nell',guest='wanda',what='stroll'):
        start=start or self.clock.now()
        r=self.command(host,'invite',dict(to=[guest],kind=kind,what=what,location='place:'+place,
                                         start_at=start,end_at=end or start+4*3600))
        self.assertEqual(r['status'],'succeeded',r)
        inv=self.l.plans.pending_for(guest)[-1]['inv_id']
        answer=self.command(guest,'respond_invite',dict(inv_id=inv,answer='yes'))
        self.assertEqual(answer['status'],'succeeded',answer)
        return r['plan_id']

    def finish(self,plan_id,restarts=()):
        for i in range(5000):
            if i in restarts:
                self.restart()
            self.l.k.tick(); self.o.step()
            if self.l.plans.plan(plan_id)['state']=='done':
                return
            self.clock.advance(60)
        self.fail('plan failed to finish: '+str([self.o.job(a) for a in ('nell','wanda')]))


class Mechanisms(Base):
    def test_route_activity_return_shared_plan_and_two_restarts(self):
        p=self.plan()
        self.finish(p,restarts=(3,15))
        self.assertEqual(self.l.k.actor('nell')['location'],'living')
        self.assertEqual(self.l.k.actor('wanda')['location'],'living')
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM events WHERE kind='travel_leg_completed'").fetchone()[0],4)
        count=self.s.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0]
        self.o.step(); self.l.k.tick()
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0],count)

    def test_owner_and_forged_response_cannot_join(self):
        r=self.command('nell','invite',dict(to=['owner'],kind='outing',what='stroll',location='place:park',start_at=self.clock.now()))
        self.assertEqual(r['status'],'failed')
        r=self.command('nell','invite',dict(to=['wanda'],kind='outing',what='stroll',location='place:park',start_at=self.clock.now()))
        inv=self.l.plans.pending_for('wanda')[0]['inv_id']
        self.assertEqual(self.command('nell','respond_invite',dict(inv_id=inv,answer='yes'))['reason'],'not_her_invitation')

    def test_monthly_grants_are_fenced_and_have_facts(self):
        self.assertEqual(self.o.wallet.available('nell'),40000)
        self.assertEqual(self.o.wallet.available('owner'),0)
        self.clock.advance(at('2026-12-01','00:00')-self.clock.now()); self.o.wallet.sync(); self.restart()
        self.assertEqual(self.o.wallet.available('nell'),80000)
        self.assertEqual(self.o.wallet.available('house'),2*HOUSE_ALLOWANCE)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM wallet_entries').fetchone()[0],14)

    def test_no_raw_outside_move_bypass(self):
        r=self.command('nell','move',{'to':'place:park'})
        self.assertEqual(r['status'],'failed')
        self.assertIn('outing_plan_required',r['reason'])

    def test_exact_fare_required_and_insufficient_funds(self):
        self.o.places['library']['travel'].pop('price_yen')
        r=self.command('nell','invite',dict(to=['wanda'],kind='outing',location='place:library',start_at=self.clock.now()))
        self.assertEqual(r['reason'],'exact_price_required')
        self.o.places['park']['activity_costs_yen']['stroll']=40001
        r=self.command('nell','invite',dict(to=['wanda'],kind='outing',what='stroll',location='place:park',start_at=self.clock.now()))
        self.assertEqual(r['reason'],'insufficient_funds')

    def test_overnight_notice_monthly_limit_luggage_lodging_and_return(self):
        early=self.command('nell','invite',dict(to=['wanda'],kind='trip',what='sightseeing',location='place:hakone',
                                              start_at=at('2026-11-03'),end_at=at('2026-11-04','17:00')))
        self.assertEqual(early['reason'],'insufficient_trip_notice')
        p=self.plan('hakone',at('2026-11-05'),at('2026-11-06','17:00'),'trip',what='sightseeing')
        r=self.command('nell','invite',dict(to=['elsa'],kind='trip',what='sightseeing',location='place:hakone',
                                          start_at=at('2026-11-12'),end_at=at('2026-11-13','17:00')))
        self.assertEqual(r['reason'],'monthly_overnight_limit')
        self.clock.advance(at('2026-11-05')-self.clock.now()); self.finish(p,restarts=(20,1000))
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM events WHERE kind='luggage_packed'").fetchone()[0],2)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM events WHERE kind='purchased' AND json_extract(payload,'$.kind')='lodging'").fetchone()[0],2)
        self.assertGreater(self.o.wallet.available('nell'),0)
        self.assertLess(self.o.wallet.available('nell'),40000)
        self.assertEqual(self.l.k.actor('nell')['location'],'living')

    def test_respond_checks_invitees_monthly_slot(self):
        self.plan('hakone',at('2026-11-05'),at('2026-11-06','17:00'),'trip',host='elsa',guest='wanda',what='sightseeing')
        r=self.command('nell','invite',dict(to=['wanda'],kind='trip',what='sightseeing',location='place:hakone',
                                         start_at=at('2026-11-12'),end_at=at('2026-11-13','17:00')))
        self.assertEqual(r['status'],'succeeded')
        inv=self.l.plans.pending_for('wanda')[-1]['inv_id']
        r=self.command('wanda','respond_invite',dict(inv_id=inv,answer='yes'))
        self.assertEqual(r['reason'],'monthly_overnight_limit')

    def test_rain_requires_owned_umbrella_no_fabrication(self):
        # v2 gives nell a folding umbrella (nell_umbrella); this test is about someone who owns none
        with self.s.tx() as db:
            db.execute("DELETE FROM garments WHERE actor='nell' AND garment_id LIKE '%umbrella%'")
        p=self.plan('cafe',what='reading')
        self.rain=True; self.finish(p)
        self.assertEqual(self.l.k.actor('nell')['location'],'room_nell')
        self.assertEqual(self.o.job('nell')['failure'],'rain_requires_owned_umbrella')
        self.assertEqual(self.o.wallet.available('nell'),40000)

    def test_rain_with_the_owned_umbrella_from_the_catalog_still_goes_out(self):
        p=self.plan('cafe',what='reading')
        self.rain=True; self.finish(p)
        self.assertNotEqual(self.o.job('nell').get('failure'),'rain_requires_owned_umbrella')
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM actions WHERE actor_id='nell' AND action_type='change_clothes'"
                                           " AND args LIKE '%nell_umbrella%'").fetchone()[0],1)

    def test_a_parasol_is_not_an_umbrella(self):
        # elsa's only 伞 is the lace parasol (elsa_parasol 蕾丝阳伞): rain still stops her
        p=self.plan('cafe',host='elsa',what='reading')
        self.rain=True; self.finish(p)
        self.assertEqual(self.o.job('elsa')['failure'],'rain_requires_owned_umbrella')
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM actions WHERE args LIKE '%elsa_parasol%'").fetchone()[0],0)

    def test_open_interval_midnight_and_contiguous_24h(self):
        self.assertTrue(is_open({'open':'22:00-02:00'},at(clock='23:00'),at('2026-11-03','01:00')))
        self.assertTrue(is_open({'open':'00:00-24:00'},at(clock='23:00'),at('2026-11-03','01:00')))
        self.assertFalse(is_open({'open':'09:00-17:00'},at(clock='16:00'),at(clock='18:00')))
        self.assertFalse(is_open({'open':'09:00-17:00'},at(clock='18:00'),at(clock='18:00')))
        self.assertFalse(is_open({'open':'09:00-17:00'},at(clock='17:00'),at(clock='17:00')))
        self.assertTrue(is_open({'open':'09:00-17:00'},at(clock='09:00'),at(clock='09:00')))

    def test_closed_activity_returns_without_free_effects(self):
        p=self.plan()
        self.o.places['park']['open']='10:00-10:05'
        self.finish(p)
        self.assertEqual(self.l.k.actor('nell')['location'],'living')
        self.assertIn('place_closed',self.o.job('nell')['failure'])

    def test_shopping_carries_then_unpacks_and_never_refills_old_row(self):
        self.o.shopping.targets={'egg':14}
        self.o.step()
        p=self.l.get('shopping:current')['plan_id']
        self.assertEqual(self.l.k.resource('egg')['qty'],12)
        self.finish(p,restarts=(8,))
        self.assertEqual(self.l.k.resource('egg')['qty'],12)
        bought=self.s.db.execute("SELECT resource_id,qty,source FROM resources WHERE item='egg' AND resource_id!='egg'").fetchone()
        self.assertEqual(bought['qty'],2); self.assertTrue(bought['source'].startswith('event:'))
        self.assertEqual(self.l.k.resource(bought['resource_id'])['location'],'kitchen')
        self.assertEqual(self.o.wallet.available('house'),HOUSE_ALLOWANCE-2*V.INGREDIENTS['egg']['price_yen'])


class CognitionPersistence(Base):
    def cognition(self,script):
        return Cognition(self.l.k,Budget(self.s,limits={'total':1000}),FakeModel(script))

    def test_saved_answer_and_two_step_plan_resume(self):
        c=self.cognition(lambda _:{'latency':10,'steps':[{'action_type':'wait','args':{'seconds':60}},
                                                        {'action_type':'wait','args':{'seconds':60}}]})
        c.begin('nell','test'); self.restart()
        c=self.cognition(lambda _:self.fail('must not call again'))
        self.clock.advance(10); c.run_due(); self.restart(); c=self.cognition(lambda _:self.fail('unexpected call'))
        self.clock.advance(60); self.l.k.tick(); c.advance_plans()
        self.clock.advance(60); self.l.k.tick(); c.advance_plans()
        self.assertFalse(c.plans)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM actions WHERE action_type='wait'").fetchone()[0],2)

    def test_interrupted_call_counts_and_is_not_replayed(self):
        def crash(_): raise SystemExit('synthetic')
        c=self.cognition(crash)
        with self.assertRaises(SystemExit): c.begin('nell','test')
        self.restart(); c=self.cognition(lambda _:self.fail('unknown replayed'))
        self.assertEqual(c.begin('nell','test'),'degraded'); self.assertEqual(c.budget.used(),1)

    def test_submit_and_cursor_commit_together(self):
        c=self.cognition(lambda _:{'steps':[{'action_type':'wait','args':{'seconds':60}}]})
        c.begin('nell','test')
        self.s.faults['command_acceptance:after_commit']=lambda:(_ for _ in ()).throw(SystemExit())
        with self.assertRaises(SystemExit): c.run_due()
        self.restart(); c=self.cognition(lambda _:self.fail('unexpected call')); c.advance_plans()
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0],1)

    def test_real_process_death_at_five_persistence_boundaries(self):
        for phase in ('reserved','calling','answer_saved','submitted_before_attach','completed_before_advance'):
            with self.subTest(phase=phase):
                path=os.path.join(self.temp.name,phase+'.db')
                source='''
import os,sys
from worldkernel.store import Store,VirtualClock
from worldkernel.life import Life
from worldkernel.cognition import FakeModel
clock=VirtualClock(float(sys.argv[2]))
s=Store(sys.argv[1],clock,world_id='test/crash',holder='child'); s.acquire()
phase=sys.argv[3]
def answer(_):
    if phase=='calling': os._exit(71)
    return {'steps':[{'action_type':'wait','args':{'seconds':60}},{'action_type':'wait','args':{'seconds':60}}]}
l=Life(s,model=FakeModel(answer)); c=l.cog
s.faults['cognition:'+phase]=lambda:os._exit(71)
c.begin('nell','test'); c.run_due()
clock.advance(60); l.k.tick(); c.advance_plans()
raise RuntimeError('fault was not reached')
'''
                result=subprocess.run([sys.executable,'-c',source,path,str(at()),phase],capture_output=True,text=True)
                self.assertEqual(result.returncode,71,result.stderr)
                clock=VirtualClock(at()+121)
                s=Store(path,clock,world_id='test/crash'); s.acquire()
                try:
                    l=Life(s,model=lambda _:self.fail('external call replayed'))
                    l.k.resume(at()); l.cog.run_due(); l.cog.advance_plans()
                    for _ in range(4):
                        clock.advance(60); l.k.tick(); l.cog.advance_plans()
                    expected=0 if phase in ('reserved','calling') else 2
                    self.assertEqual(s.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0],expected)
                    self.assertEqual(l.budget.used(),1)
                    self.assertFalse(l.cog.plans)
                    self.assertEqual(s.db.execute('PRAGMA quick_check').fetchone()[0],'ok')
                finally:
                    s.close()

    def test_old_completed_plan_is_not_resurrected_after_own_new_action(self):
        c=self.cognition(lambda _:{'steps':[{'action_type':'wait','args':{'seconds':60}},
                                          {'action_type':'wait','args':{'seconds':120}}]})
        c.begin('nell','test'); c.run_due()
        self.clock.advance(60); self.l.k.tick()
        self.command('nell','wait',{'seconds':180})
        self.restart(); c=self.cognition(lambda _:self.fail('unexpected model'))
        c.advance_plans()
        self.assertFalse(c.plans)
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0],2)

    def test_expired_saved_answer_does_not_start_an_action(self):
        c=self.cognition(lambda _:{'latency':90000,'steps':[{'action_type':'wait','args':{'seconds':60}}]})
        c.begin('nell','test'); self.clock.advance(90001); self.restart()
        self.l.cog.run_due()
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM actions').fetchone()[0],0)


class StockLots(Base):
    def test_cook_and_l0_use_new_lot_when_old_lot_is_empty_or_reserved(self):
        self.command('nell','move',{'to':'kitchen'})
        self.clock.advance(60); self.l.k.tick()
        recipe=V.RECIPES['omelette_rice']  # v2 replaced the starter omelette
        with self.s.tx() as db:
            for item,qty in recipe['consume'].items():
                db.execute('UPDATE resources SET qty=0 WHERE item=?',(item,))
                db.execute("INSERT INTO resources(resource_id,kind,item,location,qty,source) VALUES(?,'food',?,'kitchen',?,'fixture')",
                           ('z:new:'+item,item,qty))
        self.assertTrue(self.l.can_cook('omelette_rice'))
        r=self.command('nell','cook',{'recipe':'omelette_rice'})
        self.assertEqual(r['status'],'running',r)
        self.assertFalse(self.l.can_cook('omelette_rice'))
        self.clock.advance(recipe['seconds']); self.l.k.tick()
        self.assertTrue(all(self.l.k.resource('z:new:'+i)['qty']==0 for i in recipe['consume']))

    def test_eat_chooses_available_lot_without_consuming_someone_elses_hold(self):
        self.command('nell','move',{'to':'kitchen'})
        self.clock.advance(60); self.l.k.tick()
        with self.s.tx() as db:
            db.execute("INSERT INTO resources(resource_id,kind,item,location,qty,source) VALUES('z:bread','food','bread','kitchen',1,'fixture')")
            db.execute("INSERT INTO reservations VALUES('other','bread','other-action',8,1,'held',?)",(self.clock.now(),))
        r=self.command('nell','eat',{'item':'bread'})
        self.assertEqual(r['status'],'running',r)
        self.clock.advance(600); self.l.k.tick()
        self.assertEqual(self.l.k.resource('bread')['qty'],8)
        self.assertEqual(self.l.k.resource('z:bread')['qty'],0)


if __name__=='__main__': unittest.main()
