"""Low stock creates one household errand in the shared plan ledger; no timed refill."""
import json

from .store import dumps
from .life import STOCK, day_of

# an errand is not given to someone whose meal is on or starts within this (a round trip to the supermarket is
# 25-40 minutes): in sim7-r2 nell, the one with the umbrella, left for it right after cooking lunch, and missed it
ERRAND_MEAL_GAP_S = 45 * 60


class Shopping:
    def __init__(self, outings, targets=None):
        self.o, self.l, self.s = outings, outings.l, outings.s
        self.targets = dict(STOCK if targets is None else targets)

    def step(self):
        minute = self.l.minute()
        if not 9*60+30 <= minute < 16*60:
            return
        current = self.l.get('shopping:current')
        if current and current['state'] not in ('done','cancelled'):
            plan = self.l.plans.plan(current['plan_id'])
            if plan and plan['state'] in ('cancelled','lapsed'):
                self.finish(current['plan_id'],False)
                current = self.l.get('shopping:current')
            else:
                return
        if current and self.s.clock.now() < current.get('retry_after',0):
            return
        with self.s.tx() as db:
            needed = {}
            for item,target in self.targets.items():
                free = sum(self.l.k._free_qty(db,r[0]) or 0 for r in db.execute(
                    "SELECT resource_id FROM resources WHERE item=? AND location='kitchen' AND holder IS NULL",(item,)))
                if free < target:
                    needed[item] = target-free
            actors = [a for a in self.l.k.roster if not self.l.busy(a) and not self.o.owns(a)
                      and self.l.k.actor(a)['activity'] != 'sleeping'
                      and self.l.k.actor(a)['location'] in self.l.k.V.LOCATIONS
                      and self.l.k.V.LOCATIONS[self.l.k.actor(a)['location']]['kind']=='room']
            if not actors:
                return
            now = self.s.clock.now()
            actors = [a for a in actors if not self._meal_near(a, now) and not self._outing_near(a, now)]
            if not actors:
                return
            source = getattr(self.l, 'promise_source', None)
            errands = {}
            if source:
                try:
                    errands = {a: source.errands(a) for a in actors}
                except Exception as e:
                    print('[promises] errands unavailable: ' + type(e).__name__, flush=True)
            if not needed and not any(errands.values()):
                return
            if self.l.weather(self.s.clock.now()).get('rain'):
                # in the rain the errand goes with someone who has an umbrella (without one the trip turns
                # back at the door). In the fast-forward week of 11-05 (sim7-r1) it rained on 11-06 and 11-07, eleven
                # errands in a row went to roommates without one and turned back, the fridge ran out, and on 11-08 no
                # one could cook breakfast or lunch. Nobody ready: no errand now, the next tick looks again.
                actors = [a for a in actors if self.o.rain_ready(a)]
                if not actors:
                    # review P3: the fridge may stay short while it rains (with Outings there is no timed refill);
                    # leave a mark of why no errand went, once a day
                    if self.l.once(f"shopping:held_for_rain:{day_of(now)}"):
                        self.l.k.audit("errand_held", reason="rain_no_umbrella", items=needed)
                    return
            affordable = {}
            for candidate in actors:
                personal, count, budget = [], 0, self.o.wallet.available(candidate)
                per_intent = {}
                for row in errands.get(candidate, []):
                    qty = min(row['qty'], 3-per_intent.get(row['intent'],0), 6-count)
                    cost = self.l.k.V.INGREDIENTS[row['item']]['price_yen'] * qty
                    if qty > 0 and cost <= budget:
                        personal.append(dict(row, qty=qty))
                        count += qty
                        per_intent[row['intent']] = per_intent.get(row['intent'],0) + qty
                        budget -= cost
                if personal:
                    affordable[candidate] = personal
            actor = next(iter(affordable), actors[int(self.l.minute()/60)%len(actors)])
            personal = affordable.get(actor, [])
            if not needed and not personal:
                return
            pid = self.s.new_id('shopping-plan')
            eid = self.l.k._emit('shopping_list_created',actor,None,None,{'items':needed,'plan_id':pid},
                                scope=f'self:{actor}',occurred=now,observers=())
            db.execute("INSERT INTO plans(plan_id,host,kind,what,location,start_at,end_at,members,state,note,created_at,updated_at,event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pid,actor,'outing','shopping','place:supermarket',now,now+3*3600,dumps([actor]),
                        'confirmed','household procurement',now,now,eid))
            self.l.put('shopping:current',dict(plan_id=pid,actor=actor,items=needed,items_from=personal,state='planned',source=eid))

    def current(self, plan_id):
        row = self.l.get('shopping:current')
        return row if row and row['plan_id']==plan_id else None

    def _meal_near(self, actor, now):
        """Her meal is on (not had yet) or starts within ERRAND_MEAL_GAP_S; or it closed while she was still making
        it, and what she makes may yet be eaten as that meal (the late grace, review P3). That row stays pending
        (reason preparing) through the grace and is marked missed only after it, so the grace is read the way the
        meal itself is counted (Meals._late_row), not by status='missed' (review r3 P2: that never matched)."""
        row = self.l.next_meal_row(actor, now)
        if row and row[1] < now + ERRAND_MEAL_GAP_S:
            return True
        with self.s.read() as c:
            return self.l.meals._late_row(c, actor, now) is not None

    def _outing_near(self, actor, now):
        """Do not assign a 3h household supermarket errand over an already-confirmed outing/trip window."""
        with self.s.read() as c:
            has_city = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='city_visit_carry'").fetchone() is not None
            rows = c.execute(
                "SELECT plan_id, kind, location, start_at, end_at, members FROM plans"
                " WHERE kind IN ('outing','trip') AND state IN ('confirmed','running')"
            ).fetchall()
            for r in rows:
                if actor not in json.loads(r['members']):
                    continue
                if self.l.get(f"journey:{r['plan_id']}:{actor}") == 'done':
                    continue
                if has_city and c.execute(
                    "SELECT 1 FROM city_visit_carry cv JOIN city_visits v USING(visit_id)"
                    " WHERE cv.plan_id=? AND cv.actor_id=? AND v.state IN ('closed','reconcile') LIMIT 1",
                    (r['plan_id'], actor),
                ).fetchone():
                    continue
                end_s = self.l.plans.effective_end(r) if hasattr(self.l, 'plans') else (r['end_at'] if r['end_at'] is not None else (r['start_at'] + 4 * 3600))
                if now < end_s and r['start_at'] < now + 3 * 3600:
                    return True
        return False

    def finish(self, plan_id, success, withdrawn=False):
        with self.s.tx():
            row = self.current(plan_id)
            if row:
                row['state'] = 'done' if success and not withdrawn else 'cancelled'
                if not success and not withdrawn:
                    # Failed/never-started errands must not become completed shopping trips.
                    # Persist the retry interval together with the plan state across restarts.
                    row['retry_after'] = self.s.clock.now()+3600
                    plan = self.l.plans.plan(plan_id)
                    if plan and plan['state'] != 'cancelled':
                        self.l.plans.set_state(plan_id,'lapsed','procurement incomplete')
                self.l.put('shopping:current',row)
