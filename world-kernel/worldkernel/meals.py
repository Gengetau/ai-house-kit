"""Three shared meal opportunities, truthful per-person attendance, separate snack facts.

No food or cooking is invented. Default participation is a low-level habit, never an
interruption: sleeping/away/busy actors can miss a window, or explicitly opt out.
Only a completed main-food eat action can mark a meal eaten.
"""
import datetime as dt
import json
from .store import dumps
from .kernel import Refused

JST = dt.timezone(dt.timedelta(hours=9))
WINDOWS = (("breakfast", 7 * 60, 9 * 60 + 30), ("lunch", 12 * 60, 13 * 60 + 30),
           ("dinner", 18 * 60, 20 * 60 + 30))
MAIN = frozenset(("cooked_rice", "omelette", "miso_soup"))
SNACK = frozenset(("bread", "onigiri", "fruit"))
SKIP_REASONS = frozenset(("not_hungry", "unwell", "busy", "away", "personal_choice"))
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meal_runtime (key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS meal_attendance (day TEXT,slot TEXT,actor TEXT,starts REAL,ends REAL,"
    "status TEXT NOT NULL,reason TEXT,action_id TEXT,last_seen REAL,PRIMARY KEY(day,slot,actor))",
    "CREATE TABLE IF NOT EXISTS meal_consumption (action_id TEXT PRIMARY KEY,actor TEXT,at REAL,item TEXT,"
    "kind TEXT,day TEXT,slot TEXT)",
    "CREATE TABLE IF NOT EXISTS meal_decisions (day TEXT,slot TEXT,actor TEXT,at REAL,"
    "PRIMARY KEY(day,slot,actor))",
    "CREATE TABLE IF NOT EXISTS meal_preparations (day TEXT,slot TEXT,actor TEXT,command TEXT NOT NULL,"
    "state TEXT NOT NULL,PRIMARY KEY(day,slot,actor))",
)

class Meals:
    def __init__(self, kernel, roster, windows=WINDOWS, routine=None, late_grace_s=0):
        self.routine = routine
        self.k, self.s, self.roster, self.windows = kernel, kernel.s, tuple(roster), tuple(windows)
        # life1: main food and snacks come from the vocabulary; a main she started cooking inside the window
        # still counts when she eats it within late_grace_s after the window (her breakfast finished at 09:32).
        self.main = getattr(kernel.V, "MAIN_FOODS", MAIN)
        self.snack = getattr(kernel.V, "SNACKS", SNACK)
        self.late_grace_s = late_grace_s
        if len(self.windows) != 3 or {w[0] for w in self.windows} != {'breakfast','lunch','dinner'}:
            raise ValueError('exactly three named meals required')
        ordered = sorted(self.windows, key=lambda w:w[1])
        if any(not 0 <= lo < hi <= 1440 for _,lo,hi in ordered) or any(a[2] > b[1] for a,b in zip(ordered,ordered[1:])):
            raise ValueError('meal windows must be bounded and disjoint')
        with self.s.tx() as c:
            for stmt in SCHEMA: c.execute(stmt)
            c.execute("INSERT OR IGNORE INTO meal_runtime VALUES('activated',?)",(str(self.s.clock.now()),))
            self.activated = float(c.execute("SELECT value FROM meal_runtime WHERE key='activated'").fetchone()[0])

    def _day(self, at):
        return dt.datetime.fromtimestamp(at,JST).date().isoformat()

    def _late_row(self, c, actor, at, resource_id=None):
        """Her meal whose window closed less than late_grace_s ago, while she was preparing it (or, with a
        resource, when that dish came from her own cook started inside the window)."""
        if not self.late_grace_s:
            return None
        row = c.execute("SELECT * FROM meal_attendance WHERE actor=? AND ends<=? AND ends+?>? AND status!='eaten'"
                        " ORDER BY ends DESC LIMIT 1", (actor, at, self.late_grace_s, at)).fetchone()
        if not row:
            return None
        if resource_id is None:
            return row if row['reason'] == 'preparing' else None
        _, _, act = str(resource_id).partition('@')
        cook = c.execute("SELECT created_at FROM actions WHERE action_id=? AND actor_id=? AND action_type='cook'",
                         (act, actor)).fetchone() if act else None
        return row if cook and row['starts'] <= cook[0] < row['ends'] else None

    def sync(self):
        now = self.s.clock.now(); day = self._day(now)
        midnight = dt.datetime.fromisoformat(day).replace(tzinfo=JST).timestamp()
        # Initialize all three opportunities, not three successful meals. No historical backfill.
        with self.s.tx() as c:
            for slot,lo,hi in self.windows:
                for actor in self.roster:
                    c.execute("INSERT OR IGNORE INTO meal_attendance VALUES(?,?,?,?,?,'pending',NULL,NULL,NULL)",
                              (day,slot,actor,midnight+lo*60,midnight+hi*60))
            rows=c.execute("SELECT action_id,actor_id,args,created_at,finished_at FROM actions WHERE action_type='eat' "
                           "AND state='succeeded' AND created_at>=? AND action_id NOT IN "
                           "(SELECT action_id FROM meal_consumption) ORDER BY finished_at,action_id",(self.activated,)).fetchall()
            # an eating activity that had its snack is a snack eaten, the item read from the resource
            # it took (the reservation keeps which one, and the resource its source: the owner's gift or the house's)
            snacks=c.execute("SELECT x.action_id,x.actor_id,x.args,x.created_at,x.finished_at,r.item FROM actions x"
                             " JOIN reservations v ON v.action_id=x.action_id AND v.consume=1 AND v.state='used'"
                             " JOIN resources r ON r.resource_id=v.resource_id WHERE x.action_type='begin_activity'"
                             " AND x.state='succeeded' AND x.created_at>=? AND json_extract(x.args,'$.snack') IS NOT NULL"
                             " AND x.action_id NOT IN (SELECT action_id FROM meal_consumption)"
                             " ORDER BY x.finished_at,x.action_id",(self.activated,)).fetchall()
            for a in snacks:
                if a['actor_id'] in self.roster:
                    c.execute("INSERT OR IGNORE INTO meal_consumption VALUES(?,?,?,?,?,?,?)",
                              (a['action_id'],a['actor_id'],a['finished_at'],a['item'],'snack',self._day(a['finished_at']),None))
            for a in rows:
                if a['actor_id'] not in self.roster: continue
                args=json.loads(a['args']); item=args.get('item')
                kind='main' if item in self.main else 'snack' if item in self.snack else 'unclassified'
                meal=c.execute("SELECT * FROM meal_attendance WHERE actor=? AND starts<=? AND ends>?",
                               (a['actor_id'],a['created_at'],a['created_at'])).fetchone()
                late = meal is None and kind == 'main' and self._late_row(c, a['actor_id'], a['created_at'],
                                                                          args.get('resource_id'))
                if late:
                    meal = late
                # An action started before the window closes may finish its normal ten-minute eat.
                # Only the first completion fills this person's meal; extra main food
                # remains a consumption fact with no slot, including within one sync.
                qualifies=(kind=='main' and meal is not None and meal['status']!='eaten'
                           and a['finished_at'] is not None
                           and a['finished_at']<=meal['ends']+(self.late_grace_s+600 if late else 600))
                c.execute("INSERT OR IGNORE INTO meal_consumption VALUES(?,?,?,?,?,?,?)",
                          (a['action_id'],a['actor_id'],a['finished_at'],item,kind,self._day(a['finished_at']),meal['slot'] if qualifies else None))
                if qualifies:
                    c.execute("UPDATE meal_attendance SET status='eaten',reason=NULL,action_id=? WHERE day=? AND slot=? AND actor=? "
                              "AND status!='eaten'",(a['action_id'],meal['day'],meal['slot'],a['actor_id']))
            active=c.execute("SELECT * FROM meal_attendance WHERE status='pending'").fetchall()
            for row in active:
                if row['starts']<=now<row['ends']:
                    a=c.execute('SELECT * FROM actors WHERE actor_id=?',(row['actor'],)).fetchone()
                    reason='sleeping' if a['activity']=='sleeping' else 'away' if a['location']=='away' else 'busy' if a['action_id'] else 'not_eaten'
                    if a['action_id']:
                        action=c.execute('SELECT action_type FROM actions WHERE action_id=?',(a['action_id'],)).fetchone()
                        if action and action[0]=='cook': reason='preparing'
                    elif a['location']=='kitchen':
                        foods=c.execute("SELECT resource_id,item FROM resources WHERE kind='food' AND location='kitchen' AND holder IS NULL AND qty>0").fetchall()
                        if not any(r['item'] in self.main and (self.k._free_qty(c,r['resource_id']) or 0)>0 for r in foods): reason='no_main_food'
                    c.execute("UPDATE meal_attendance SET reason=?,last_seen=? WHERE day=? AND slot=? AND actor=?",
                              (reason,now,row['day'],row['slot'],row['actor']))
                elif now>=row['ends']+600 + (self.late_grace_s if row['reason']=='preparing' else 0):
                    reason=row['reason'] if row['last_seen'] and row['ends']-row['last_seen']<=120 else 'not_observed'
                    c.execute("UPDATE meal_attendance SET status='missed',reason=? WHERE day=? AND slot=? AND actor=?",
                              (reason,row['day'],row['slot'],row['actor']))

    def current(self, actor):
        now=self.s.clock.now()
        with self.s.read() as c:
            row=c.execute("SELECT * FROM meal_attendance WHERE actor=? AND starts<=? AND ends>?",(actor,now,now)).fetchone()
        return dict(row) if row else None

    def respond(self, actor, choice, reason='personal_choice'):
        row=self.current(actor)
        if row and choice=='skip' and row['status']=='pending':
            with self.s.tx() as c:
                c.execute("UPDATE meal_attendance SET status='declined',reason=? WHERE day=? AND slot=? AND actor=? AND status='pending'",
                          (reason if reason in SKIP_REASONS else 'personal_choice',row['day'],row['slot'],actor))

    def release_decision(self, actor):
        """Give back the claim of `claim_decision` when the model was never reached (nothing was asked)."""
        row = self.current(actor)
        if row:
            with self.s.tx() as c:
                c.execute("DELETE FROM meal_decisions WHERE day=? AND slot=? AND actor=?",
                          (row['day'], row['slot'], actor))

    def claim_decision(self, actor):
        """Reserve at most one preparation decision per person/meal, including unknown failures.

        Commit before calling the model. Restarting or an empty/failed reply must not
        turn a single meal into repeated requests; L0 can still eat food made later.
        """
        row = self.current(actor)
        if not row or row['status'] != 'pending':
            return False
        with self.s.tx() as c:
            return c.execute("INSERT OR IGNORE INTO meal_decisions VALUES(?,?,?,?)",
                             (row['day'], row['slot'], actor, self.s.clock.now())).rowcount == 1

    def context(self, actor):
        row=self.current(actor)
        return ({'slot':row['slot'],'status':row['status'],'location':'kitchen',
                 'main_food':sorted(self.main),'snacks_do_not_count':sorted(self.snack),
                 'instruction':'统一正餐时间；可去厨房吃现成正餐或自己做饭。可以因事不参加，用meal_response说明；不要替别人决定。'} if row else None)

    def defer_cook(self, cmd, result, kind):
        """Keep only her chosen, never-started meal cook after a resource collision.

        No new model call or invented recipe. The rest of the failed plan is discarded.
        An intervening action invalidates this intent; one guarded retry is allowed.
        """
        if (not kind.startswith('scheduled_meal_') or cmd.get('action_type') != 'cook'
                or result.get('reason') != 'resource_unavailable'):
            return False
        actor = cmd['actor_id']
        if not self.k.city_visits.home_allowed(actor):
            return False
        row = self.current(actor); a = self.k.actor(actor)
        if not row or row['status'] != 'pending' or a['action_id'] or a['location'] != 'kitchen':
            return False
        retry = dict(cmd, idempotency_key=cmd['idempotency_key'] + ':meal-resource-retry',
                     generation=a['generation'], expires_at=row['ends'])
        # A released reservation changes ingredient versions. Recheck current stock at
        # retry, while retaining the actor generation, recipe, principal and vocabulary.
        retry.pop('relevant_versions', None)
        retry.pop('interrupt', None)
        with self.s.tx() as c:
            return c.execute("INSERT OR IGNORE INTO meal_preparations VALUES(?,?,?,?,'waiting')",
                             (row['day'], row['slot'], actor, dumps(retry))).rowcount == 1

    def _retry_cook(self, row, actor):
        with self.s.read() as c:
            pending = c.execute("SELECT * FROM meal_preparations WHERE day=? AND slot=? AND actor=? AND state='waiting'",
                                (row['day'], row['slot'], actor['actor_id'])).fetchone()
        if not pending:
            return {}
        cmd = json.loads(pending['command'])
        def finish(state):
            with self.s.tx() as c:
                c.execute('UPDATE meal_preparations SET state=? WHERE day=? AND slot=? AND actor=?',
                          (state, row['day'], row['slot'], actor['actor_id']))
        # Recover a crash between submit and finish without replaying a new action.
        with self.s.read() as c:
            old = c.execute('SELECT command_id FROM commands WHERE idempotency_key=?',
                            (cmd['idempotency_key'],)).fetchone()
        if old:
            finish('submitted')
            return {}
        rec = self.k.V.RECIPES.get(cmd['args'].get('recipe'))
        if (actor['generation'] != cmd['generation'] or actor['location'] != 'kitchen'
                or not rec or self.s.clock.now() + rec['seconds'] + 30 >= row['ends']):
            finish('expired')
            return {}
        try:
            with self.s.read() as c:
                self.k._guard(c, 'cook', cmd['args'], actor)
                need = self.k._requirements(c, 'cook', cmd['args'], actor)
                if any((self.k._free_qty(c, rid) or 0) < qty for rid, qty, _ in need):
                    return {'would_call': 'scheduled_meal_preparation_waiting'}
        except Refused:
            finish('unavailable')
            return {}
        result = self.k.submit(cmd)
        finish('submitted')
        return {'action': 'cook', 'result': result}

    def act(self, actor):
        if not self.k.city_visits.home_allowed(actor):
            return {}
        row=self.current(actor)
        if not row and self.late_grace_s:
            # her own dish, finished just after the window: she still sits down and eats it
            with self.s.read() as c:
                late=self._late_row(c, actor, self.s.clock.now())
                a=self.k._actor(c, actor)
                mine=None
                if late and not a['action_id'] and a['location']=='kitchen':
                    for r in c.execute("SELECT resource_id,item FROM resources WHERE kind='food' AND location='kitchen'"
                                       " AND holder IS NULL AND qty>0 ORDER BY resource_id").fetchall():
                        if (r['item'] in self.main and (self.k._free_qty(c,r['resource_id']) or 0)>0
                                and self._late_row(c, actor, self.s.clock.now(), r['resource_id'])):
                            mine=r; break
            if mine:
                key=f"meal:{late['day']}:{late['slot']}:{actor}:late-eat"
                result=self.k.submit({'idempotency_key':key,'principal':'routine','actor_id':actor,'origin':'routine',
                                      'action_type':'eat','args':{'item':mine['item'],'resource_id':mine['resource_id']},
                                      'context_refs':[f"meal:{late['day']}:{late['slot']}"]})
                return {'action':'eat','result':result}
            return {}
        if not row or row['status']!='pending': return {}
        a=self.k.actor(actor)
        if a['action_id'] or a['activity']=='sleeping' or a['location']=='away': return {}
        if self.routine:
            from .routine import in_window
            from .guide import jst_minute
            h=self.routine.habit(actor)
            if h['enabled']['sleep'] and in_window(jst_minute(self.s.clock.now()), h['sleep_jst']): return {}
            if not h['enabled']['eat_ready_food']: return {'would_call':'scheduled_meal_own_choice'}
        args=None
        if a['location']!='kitchen':
            hop=self.k.V.next_hop(a['location'],'kitchen')
            if hop: action,args='move',{'to':hop}
        else:
            with self.s.read() as c:
                foods=c.execute("SELECT resource_id,item FROM resources WHERE kind='food' AND location='kitchen' "
                                "AND holder IS NULL AND qty>0 ORDER BY resource_id").fetchall()
                food=next((r for r in foods if r['item'] in self.main and (self.k._free_qty(c,r['resource_id']) or 0)>0),None)
            if food: action,args='eat',{'item':food['item'],'resource_id':food['resource_id']}
        if args is None:
            retry = self._retry_cook(row, a)
            return retry or {'would_call':'scheduled_meal_needs_preparation'}
        key=f"meal:{row['day']}:{row['slot']}:{actor}:{action}:{int(self.s.clock.now()//60)}"
        result=self.k.submit({'idempotency_key':key,'principal':'routine','actor_id':actor,'origin':'routine',
                              'action_type':action,'args':args,'context_refs':[f"meal:{row['day']}:{row['slot']}"]})
        return {'action':action,'result':result}
