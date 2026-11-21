"""Temp DB and fake clock: meal attendance requires completed main-food facts."""
import datetime as dt
import os,tempfile,unittest
from worldkernel.store import Store,VirtualClock
from worldkernel.kernel import Kernel
from worldkernel import vocab_p1b as V
from worldkernel.meals import Meals,JST
from worldkernel.routine import Routine

class MealTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.clock=VirtualClock(dt.datetime(2026,10,28,12,tzinfo=JST).timestamp())
  self.s=Store(os.path.join(self.tmp.name,'k.db'),self.clock,world_id='meals',synthetic=True);self.s.acquire();self.addCleanup(self.s.close)
  self.roster=('nell','yuki','elsa');self.k=Kernel(self.s,self.roster,vocab=V)
  self.k.seed({'actors':{a:{'location':'kitchen','body':{'hunger':20,'energy':90}} for a in self.roster},'resources':[{'id':x,'kind':'food','location':'kitchen','qty':9} for x in ('cooked_rice','bread')]})
  self.meals=Meals(self.k,self.roster);self.meals.sync()
 def row(self,actor='nell',slot='lunch'):
  with self.s.read() as c:return dict(c.execute('select * from meal_attendance where actor=? and slot=?',(actor,slot)).fetchone())
 def eat(self,item='cooked_rice',key='meal',finish=True):
  r=self.k.submit({'idempotency_key':key,'principal':'channel:nell','actor_id':'nell','origin':'model','action_type':'eat','args':{'item':item,'resource_id':item}})
  self.assertIn(r['status'],('running','accepted'),r)
  if finish:self.clock.advance(600);self.k.tick();self.meals.sync()
  return r
 def test_three_opportunities_not_claimed_meals(self):
  with self.s.read() as c:
   self.assertEqual(c.execute('select count(*) from meal_attendance').fetchone()[0],9)
   self.assertEqual(c.execute("select count(*) from meal_attendance where status='eaten'").fetchone()[0],0)
 def test_snack_never_completes_lunch(self):
  self.eat(item='bread');self.assertEqual(self.row()['status'],'pending')
  with self.s.read() as c:self.assertEqual(c.execute('select kind from meal_consumption').fetchone()[0],'snack')
 def test_main_counts_once_across_ticks_and_second_serving(self):
  self.eat();first=self.row()['action_id'];self.eat(key='second')
  for _ in range(3):self.meals.sync()
  self.assertEqual(self.row()['status'],'eaten');self.assertEqual(self.row()['action_id'],first)
  with self.s.read() as c:
   self.assertEqual([tuple(r) for r in c.execute('select kind,slot from meal_consumption order by at')], [('main','lunch'),('main',None)])
   self.assertEqual(c.execute("select count(*) from meal_consumption where kind='main' and slot is null").fetchone()[0],1)
 def test_second_dinner_in_same_sync_is_extra_main(self):
  # Both real completions can be discovered in one sync after a scheduler delay.
  self.clock.advance((6*60+26)*60)
  first=self.eat(key='dinner-first',finish=False)
  self.clock.advance(600);self.k.tick()
  self.eat(key='dinner-second',finish=False)
  self.clock.advance(600);self.k.tick()
  self.meals.sync();self.meals.sync()
  self.assertEqual(self.row(slot='dinner')['action_id'],first['action_id'])
  with self.s.read() as c:
   self.assertEqual([tuple(r) for r in c.execute('select kind,slot from meal_consumption order by at')], [('main','dinner'),('main',None)])
   self.assertEqual(c.execute("select count(*) from meal_attendance where actor='nell' and status='eaten'").fetchone()[0],1)
   self.assertEqual(c.execute("select qty from resources where resource_id='cooked_rice'").fetchone()[0],7)
 def test_extra_main_after_restart_does_not_claim_other_meals_or_people(self):
  self.eat();self.meals=Meals(self.k,self.roster);self.meals.sync()
  self.eat(key='second-after-restart')
  other=self.k.submit({'idempotency_key':'yuki-lunch','principal':'channel:yuki','actor_id':'yuki','origin':'model','action_type':'eat','args':{'item':'cooked_rice','resource_id':'cooked_rice'}})
  self.assertIn(other['status'],('running','accepted'),other)
  self.clock.advance(600);self.k.tick();self.meals.sync()
  self.assertEqual(self.row('yuki')['status'],'eaten')
  self.clock.advance((5*60+30)*60);self.meals.sync();self.eat(key='next-dinner')
  self.clock.advance((17*60+50)*60);self.meals.sync();self.eat(key='next-day-lunch')
  with self.s.read() as c:
   self.assertEqual([tuple(r) for r in c.execute("select day,slot from meal_consumption where actor='nell' order by at")], [('2026-10-28','lunch'),('2026-10-28',None),('2026-10-28','dinner'),('2026-10-29','lunch')])
   self.assertEqual(c.execute("select count(*) from meal_attendance where actor='nell' and status='eaten'").fetchone()[0],3)
 def test_cancelled_eat_does_not_count(self):
  r=self.eat(finish=False);self.meals.sync();self.assertEqual(self.row()['status'],'pending')
  self.k.cancel('nell',r['action_id'],'channel:nell');self.meals.sync();self.assertEqual(self.row()['status'],'pending')
 def test_main_without_hunger_threshold(self):
  self.assertEqual(self.meals.act('nell')['action'],'eat');self.clock.advance(600);self.k.tick();self.meals.sync();self.assertEqual(self.row()['status'],'eaten')
 def test_own_decline_does_not_decide_for_others(self):
  self.meals.respond('nell','skip','not_hungry');self.assertEqual(self.row()['status'],'declined');self.assertEqual(self.meals.act('nell'),{});self.assertEqual(self.row('yuki')['status'],'pending')
 def test_missing_main_requests_preparation_without_fake_food(self):
  with self.s.tx() as c:c.execute("update resources set qty=0 where resource_id='cooked_rice'")
  self.assertEqual(self.meals.act('nell'),{'would_call':'scheduled_meal_needs_preparation'});self.assertEqual(self.row()['status'],'pending')
 def test_sleep_schedule_respected(self):
  self.meals.routine=Routine(self.k,habits={'nell':{'sleep_jst':(660,840)}});self.assertEqual(self.meals.act('nell'),{})
 def test_busy_not_interrupted(self):
  self.k.submit({'idempotency_key':'busy','principal':'channel:nell','actor_id':'nell','origin':'model','action_type':'wait','args':{'seconds':1200}})
  action=self.k.actor('nell')['action_id'];self.assertEqual(self.meals.act('nell'),{});self.assertEqual(self.k.actor('nell')['action_id'],action)
 def test_window_close_keeps_unknown_history_honest(self):
  self.clock.advance(100*60);self.meals.sync();self.assertEqual(self.row()['status'],'missed');self.assertEqual(self.row()['reason'],'not_observed');self.assertEqual(self.row(slot='dinner')['status'],'pending')
 def test_completion_grace(self):
  self.clock.advance(89*60);self.eat();self.assertEqual(self.row()['status'],'eaten')
 def test_outside_window_not_backdated(self):
  self.clock.advance(120*60);self.eat();self.assertEqual(self.row()['status'],'missed')
  with self.s.read() as c:self.assertIsNone(c.execute('select slot from meal_consumption').fetchone()[0])
 def test_restart_keeps_attendance_and_decline(self):
  self.eat();self.meals.respond('elsa','skip','personal_choice');Meals(self.k,self.roster).sync();self.assertEqual(self.row()['status'],'eaten');self.assertEqual(self.row('elsa')['status'],'declined')
  with self.s.read() as c:self.assertEqual(c.execute('select count(*) from meal_consumption').fetchone()[0],1)
 def test_overlapping_windows_rejected(self):
  with self.assertRaises(ValueError):Meals(self.k,self.roster,windows=(('breakfast',420,800),('lunch',720,810),('dinner',1080,1200)))
 def test_away_is_recorded_as_reason_without_forced_return(self):
  with self.s.tx() as c:c.execute("update actors set location='away' where actor_id='nell'")
  self.assertEqual(self.meals.act('nell'),{})
  self.clock.advance(89*60);self.meals.sync();self.clock.advance(11*60);self.meals.sync()
  self.assertEqual(self.row()['status'],'missed');self.assertEqual(self.row()['reason'],'away')
 def test_snack_after_main_does_not_add_or_erase_meal(self):
  self.eat();self.eat(item='bread',key='snack-after-main')
  self.assertEqual(self.row()['status'],'eaten')
  with self.s.read() as c:
   self.assertEqual(c.execute("select count(*) from meal_attendance where actor='nell' and status='eaten'").fetchone()[0],1)
   self.assertEqual(c.execute("select count(*) from meal_consumption where kind='snack'").fetchone()[0],1)
