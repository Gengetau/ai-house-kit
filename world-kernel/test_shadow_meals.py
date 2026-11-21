"""Full shadow wiring, fake chat and legacy world DB; no real model or QQ."""
import datetime as dt
import hashlib,json,os,sys,tempfile,types,unittest
from unittest.mock import patch
import shadow as SH
from worldkernel.store import VirtualClock
from worldkernel.meals import JST
import test_shadow as fixture

class ShadowMealIntegration(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.world=os.path.join(self.tmp.name,'world.db');fixture.fake_world(self.world)
  self.clock=VirtualClock(dt.datetime(2026,10,28,12,tzinfo=JST).timestamp())
  self.before=self.digest()
 def digest(self):
  with open(self.world,'rb') as f:return hashlib.sha256(f.read()).hexdigest()
 def make(self,chat):
  sh=SH.Shadow(os.path.join(self.tmp.name,'kernel-p1b.db'),os.path.join(self.tmp.name,'samples-p1b.db'),self.world,fixture.ROUTINE,fixture.SELVES,chat,clock=self.clock,profile='p1b')
  self.addCleanup(sh.store.close);self.addCleanup(sh.samples.close);return sh
 def test_own_decline_is_wired_and_legacy_world_is_not_written(self):
  def chat(system,user):return json.dumps({'steps':[],'meal_response':{'choice':'skip','reason':'not_hungry'}}),12
  sh=self.make(chat);sh.step()
  self.assertEqual(sh.meals.current('nell')['status'],'declined')
  self.assertEqual(self.digest(),self.before)
  with sh.store.read() as c:self.assertEqual(c.execute("select count(*) from actions where actor_id='nell' and action_type='eat'").fetchone()[0],0)
 def test_snack_only_report_cannot_claim_lunch(self):
  sh=self.make(fixture.Chat());sh.meals.sync()
  r=sh.k.submit({'idempotency_key':'snack','principal':'channel:nell','actor_id':'nell','origin':'model','action_type':'eat','args':{'item':'bread','resource_id':'bread'}})
  self.assertIn(r['status'],('running','accepted'))
  self.clock.advance(600);sh.k.tick();sh.meals.sync();sh.sample(self.clock.now())
  report=SH.report(self.tmp.name,'2026-10-28','p1b')
  self.assertIn('每天固定早/午/晚三个机会',report)
  self.assertIn('零食次数',report)
  self.assertEqual(sh.meals.current('nell')['status'],'pending')
  self.assertNotIn('进食动作（含零食',report)
  self.assertIn('未观察（旧数据不补造）',SH.report(self.tmp.name,'2026-10-27','p1b'))
 def test_prepare_a_main_meal_instead_of_marking_initial_snacks_as_lunch(self):
  sh=self.make(fixture.Chat())
  # The initial p1b pantry has no completed main meal; the model must really cook.
  for _ in range(60):sh.step();self.clock.advance(30)
  with sh.store.read() as c:
   cooked=c.execute("select count(*) from actions where action_type='cook' and state='succeeded'").fetchone()[0]
   eaten=c.execute("select count(*) from meal_attendance where slot='lunch' and status='eaten'").fetchone()[0]
  self.assertGreater(cooked,0);self.assertGreater(eaten,0)
  self.assertEqual(self.digest(),self.before)

 def test_report_names_and_jst_extra_food_details(self):
  sh=self.make(fixture.Chat());sh.meals.sync();sh.sample(self.clock.now())
  def at(hour,minute):return dt.datetime(2026,10,28,hour,minute,tzinfo=JST).timestamp()
  with sh.store.tx() as c:
   for key,actor,when,item,kind,slot in (
    ('later','mina',at(15,10),'面包','main',None),
    ('unknown','mina',at(16,5),'未知物品','unclassified',None),
    ('earlier','mina',at(10,42),'饭团','main',None),
    ('meal','mina',at(12,10),'正餐不列','main','lunch'),
    ('snack','mina',at(11,0),'零食不列','snack',None),
    ('other','nell',at(10,0),'他人不列','main',None),
    ('tomorrow','mina',at(10,0)+86400,'明日不列','main',None)):
    c.execute('INSERT INTO meal_consumption VALUES(?,?,?,?,?,?,?)',(key,actor,when,item,kind,'2026-10-28',slot))
  with patch.dict(sys.modules,{'roster':None}):
   report=SH.report(self.tmp.name,'2026-10-28','p1b')
   self.assertEqual(SH._display('unknown_actor'),'unknown_actor')
  rows=[line for line in report.splitlines() if line.startswith('| 米娜 |')]
  self.assertEqual(len(rows),2)
  self.assertNotIn('| mina |',report)
  self.assertTrue(rows[1].endswith('| 1 | 2/1：10:42 饭团、15:10 面包；16:05 未知物品 |'))
  self.assertIn('| 艾莎 |',report)
  self.assertIn('| 0 | 0/0 |',report)
  for module in (types.SimpleNamespace(_BY={'mina':{'name':'现行名字'}}),
                 types.SimpleNamespace(ALIASES={'mina':('现行名字','旧名')})):
   with patch.dict(sys.modules,{'roster':module}):
    current=SH.report(self.tmp.name,'2026-10-28','p1b')
   self.assertEqual(sum(line.startswith('| 现行名字 |') for line in current.splitlines()),2)

 def test_report_extra_details_are_limited_to_five_total(self):
  sh=self.make(fixture.Chat());sh.meals.sync()
  with sh.store.tx() as c:
   for i in reversed(range(6)):
    when=dt.datetime(2026,10,28,10,i,tzinfo=JST).timestamp()
    c.execute('INSERT INTO meal_consumption VALUES(?,?,?,?,?,?,?)',
              (f'extra-{i}','mina',when,None if i==0 else f'条目{i}',
               'main' if i%2==0 else 'unclassified','2026-10-28',None))
  with patch.dict(sys.modules,{'roster':None}):
   report=SH.report(self.tmp.name,'2026-10-28','p1b')
  row=[line for line in report.splitlines() if line.startswith('| 米娜 |')][-1]
  self.assertTrue(row.endswith('| 3/3：10:00 未记名、10:02 条目2、10:04 条目4；10:01 条目1、10:03 条目3…等 6 条 |'))
  self.assertNotIn('10:05',row)
  self.assertNotIn('条目5',row)

 def test_one_lunch_decision_per_person_even_if_empty_or_failed(self):
  for fail in (False,True):
   with self.subTest(fail=fail):
    if fail:self.setUp()
    calls=[]
    def chat(system,user):
     calls.append(user)
     if fail:raise TimeoutError('synthetic')
     return json.dumps({'steps':[]}),10
    self.clock.advance(-600)  # same 11:50--13:50 window as the independent probe
    sh=self.make(chat)
    for _ in range(240):sh.step();self.clock.advance(30)
    self.assertEqual(len(calls),len(SH.ROSTER))
    with sh.store.read() as c:
     self.assertEqual(c.execute("select count(*) from decision_points where outcome='degraded'").fetchone()[0],0)
     self.assertEqual(c.execute("select count(*) from meal_decisions where slot='lunch'").fetchone()[0],len(SH.ROSTER))
     self.assertEqual(c.execute("select count(*) from meal_attendance where slot='lunch' and status='eaten'").fetchone()[0],0)

 def test_lunch_decision_is_retained_after_restart_and_dinner_is_separate(self):
  calls=[]
  def chat(system,user):calls.append(user);return json.dumps({'steps':[]}),10
  sh=self.make(chat)
  for _ in range(40):sh.step();self.clock.advance(30)
  self.assertEqual(len(calls),len(SH.ROSTER))
  sh.store.close();sh.samples.close()
  sh=self.make(chat)
  for _ in range(80):sh.step();self.clock.advance(30)
  self.assertEqual(len(calls),len(SH.ROSTER))
  self.clock.advance(5*3600)  # 18:00, another meal (outage does not backfill lunch)
  for _ in range(40):sh.step();self.clock.advance(30)
  with sh.store.read() as c:
   self.assertEqual(c.execute("select count(*) from meal_decisions where slot='dinner'").fetchone()[0],len(SH.ROSTER))
   self.assertEqual(c.execute("select count(*) from meal_decisions where slot='lunch'").fetchone()[0],len(SH.ROSTER))

 def test_meal_wait_and_rejected_action_do_not_hide_other_decisions(self):
  sh=self.make(fixture.Chat());sh.meals.sync()
  for meal in ({'would_call':'scheduled_meal_needs_preparation'},
               {'action':'eat','result':{'status':'failed','reason':'synthetic_rejection'}}):
   with self.subTest(meal=meal), patch.object(sh,'think',return_value='asked') as think, \
        patch.object(sh.meals,'act',return_value=meal), \
        patch.object(sh.cog,'context',return_value={'recent':[{'kind':'said','to':'nell','event_id':42,'actor':'elsa'}],
                                                  'offers':[{'guide_id':'guide-game-1'}]}):
    sh.heard.clear();sh.considered.clear();sh.decision_points()
    self.assertIn(unittest.mock.call('nell','addressed_by:elsa'),think.call_args_list)
    self.assertIn(unittest.mock.call('nell','guide:guide-game-1'),think.call_args_list)
