"""life1 extension: shared Plans -> paid route legs, venue blocks, meals and return.

Uses only instant['buy'], on_complete and the shared vocabulary/runner contracts.
No second plan ledger, invitations, model, scheduler or production configuration.
"""
import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from .kernel import Refused
from .wallet import Wallet, local, JST
from .trips import Trips
from .shopping import Shopping
from . import inventory


def yen(value):
    if type(value) is not int or value < 0:
        raise Refused('exact_price_required')
    return value


def is_open(place, start, end):
    intervals = []
    day = local(start).replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=1)
    last = local(end).replace(hour=0,minute=0,second=0,microsecond=0)
    while day <= last:
        if day.weekday() in place.get('weekdays',range(7)):
            for span in place['open'].split(','):
                lo,hi = span.split('-')
                def minute(t):
                    h,m=map(int,t.split(':'))
                    return h*60+m
                a,b = day.timestamp()+minute(lo)*60,day.timestamp()+minute(hi)*60
                intervals.append((a,b if b>a else b+86400))
        day += timedelta(days=1)
    covered = start
    if start==end:
        return any(a<=start<b for a,b in intervals)
    for a,b in sorted(intervals):
        if a <= covered:
            covered=max(covered,b)
    return end <= covered


class Outings:
    shops = True

    def __init__(self, life, places=None, targets=None):
        self.l,self.s,self.k = life,life.s,life.k
        self.v = self.k.V
        self.places = copy.deepcopy(self.v.PLACES if places is None else places)
        self.wallet = Wallet(self.k)
        self.wallet.sync()
        self.trips = Trips(life,self.places)
        self.shopping = Shopping(self,targets)
        self.k.instant['buy'] = self.buy
        self.k.on_complete.append(self.completed)
        self.l.plans.validators.update(outing=self.validate,trip=self.validate)
        respond = self.k.instant['respond_invite']
        def checked_response(k,db,aid,actor,args,cmd):
            inv = db.execute('SELECT * FROM invitations WHERE inv_id=?',(args.get('inv_id'),)).fetchone()
            if inv and inv['invitee']==actor['actor_id'] and args.get('answer')=='yes':
                plan = self.l.plans.plan(inv['plan_id'])
                if plan['kind'] in ('outing','trip'):
                    validator = self.l.plans.validators.get(plan['kind'], self.validate)
                    validator(k,db,actor,dict(plan,_proposed_at=plan['created_at'],_plan_id=plan['plan_id']))
            return respond(k,db,aid,actor,args,cmd)
        self.k.instant['respond_invite'] = checked_response
        # Per-instance view of the catalog: shared modules and the published vocabulary are untouched.
        proxy = SimpleNamespace(**{k:v for k,v in vars(self.v).items() if not k.startswith('__')})
        proxy.guard = self.guard
        proxy.route_seconds = self.route_seconds
        self.k.V = self.s.vocab = proxy

    def job(self,actor):
        return self.l.get(f'outing:active:{actor}')

    def save(self,actor,job):
        self.l.put(f'outing:active:{actor}',job)

    def owns(self,actor):
        j = self.job(actor)
        return bool(j and j['state'] in ('running','returning','paused'))

    def context(self,actor,kind):
        return {'wallet_yen':self.wallet.available(actor), 'outing':self.job(actor)}

    def route(self,place):
        travel=place['travel']
        legs=travel.get('segments',[travel])
        out=[]
        for leg in legs:
            minutes=leg.get('minutes')
            if type(minutes) not in (int,float) or not 0<minutes<=240:
                raise Refused('invalid_route_duration')
            out.append(dict(mode=leg['mode'],seconds=round(minutes*60),
                            yen=yen(leg.get('price_yen',0 if leg['mode']=='walk' else None))))
        if not out:
            raise Refused('missing_route')
        return out

    def route_seconds(self,source,destination):
        pid = self.v.place_id(destination) if source=='away' else self.v.place_id(source) if destination=='away' else None
        if pid and pid in self.places:
            legs=self.route(self.places[pid])
            leg=legs[-1] if source=='away' else legs[0]
            return leg['seconds']*(1.25 if leg['mode']=='walk' and self.l.weather(self.s.clock.now())['rain'] else 1)
        return self.v.route_seconds(source,destination)

    def _menu(self,pid):
        menus=[m for m in self.places[pid].get('menu',[]) if m.get('main') and m['price_yen']>0]
        return min(menus,key=lambda m:m['price_yen']) if menus else None

    def itinerary(self,actor,plan):
        pid=self.v.place_id(plan['location'])
        p=self.places[pid]
        route=self.route(p)
        steps=[]
        cursor=plan['start_at']
        def add(action,args,seconds=0,cost=0,account=actor,**details):
            nonlocal cursor
            maximum=self.v.ACTIONS[action]['duration_rule'].get('max')
            if maximum and seconds>maximum:
                # Keep the catalog's action limit, including on long transit legs and hotel nights.
                left=seconds
                while left>0:
                    part=min(left,maximum)
                    extra=details if left<=maximum else {}
                    add(action,dict(args,seconds=part),part,cost,account,**extra)
                    cost=0
                    left-=part
                return
            steps.append(dict(action=action,args=args,seconds=seconds,yen=cost,account=account,**details))
            cursor+=seconds
        def wait_until(target):
            while target>cursor:
                seconds=min(4*3600,round(target-cursor))
                add('wait',{'seconds':seconds},seconds)
        def meal():
            m=self._menu(pid)
            if m:
                add('buy',{'kind':'menu','item':m['id'],'qty':1},cost=yen(m['price_yen']))
                add('eat',{'item':m['id']},600)
        nights=(local(plan['end_at']).date()-local(plan['start_at']).date()).days if plan.get('end_at') else 0
        if nights:
            clothes=[g['garment_id'] for g in self.l.wardrobe.owned(actor) if g['state']!='dirty'
                     and g['category'] in ('top','bottom','dress','sleepwear','loungewear')]
            if not clothes:
                raise Refused('packing_requires_owned_clothes')
            add('wait',{'seconds':600},600,packing=clothes)
        add('move',{'to':'away'},self.v.route_seconds(self.k.actor(actor)['location'],'away') or 60)
        for i,leg in enumerate(route):
            seconds=leg['seconds']*(1.25 if leg['mode']=='walk' and self.l.weather(cursor)['rain'] else 1)
            if i==len(route)-1:
                add('move',{'to':plan['location']},seconds,cost=leg['yen'],leg=leg)
            else:
                add('wait',{'seconds':seconds},seconds,cost=leg['yen'],leg=leg)
        shopping=self.shopping.current(plan['plan_id'])
        if shopping:
            for item,qty in sorted(shopping['items'].items()):
                add('buy',{'kind':'ingredient','item':item,'qty':qty},cost=yen(self.v.INGREDIENTS[item]['price_yen'])*qty,
                    account='house')
            for row in shopping.get('items_from', []):
                add('buy', {'kind':'ingredient','item':row['item'],'qty':row['qty']},
                    cost=yen(self.v.INGREDIENTS[row['item']]['price_yen'])*row['qty'],
                    account=actor, promise=row)
        else:
            activity=plan['what'] if plan['what'] in p['activities'] else next((a for a in p['activities'] if a not in ('eat_out','shopping')),None)
            for day in range(nights+1):
                if day:
                    meal()
                if local(cursor).hour>=11:
                    meal()
                if activity:
                    spec=self.v.ACTIVITIES[activity]
                    seconds=max(spec['min'],min(spec['default'],2*3600))
                    cost=yen(p.get('activity_costs_yen',{}).get(activity,0 if activity in ('stroll','reading','working','shopping') else None))
                    add('begin_activity',{'activity':activity,'seconds':seconds},seconds,cost=cost)
                if day<nights:
                    evening=local(plan['start_at']).replace(hour=18,minute=0,second=0,microsecond=0)+timedelta(days=day)
                    wait_until(evening.timestamp())
                    add('buy',{'kind':'lodging','item':pid,'qty':1},cost=yen(p['lodging_yen']))
                    meal()
                    wake=(evening+timedelta(days=1)).replace(hour=8).timestamp()
                    sleep=round(wake-cursor)
                    if sleep>0:
                        add('sleep',{'seconds':sleep},sleep)
        for i,leg in enumerate(reversed(route)):
            seconds=leg['seconds']*(1.25 if leg['mode']=='walk' and self.l.weather(cursor)['rain'] else 1)
            if i==0:
                add('move',{'to':'away'},seconds,cost=leg['yen'],leg=leg)
            else:
                add('wait',{'seconds':seconds},seconds,cost=leg['yen'],leg=leg)
        add('move',{'to':'living'},self.v.route_seconds('away','living'))
        if shopping:
            add('wait',{'seconds':300},300,unpack=True)
        if plan.get('end_at') and cursor>plan['end_at']:
            raise Refused('itinerary_exceeds_plan_window')
        return steps

    def validate(self,k,db,actor,args):
        for ext in getattr(self.l, 'extensions', ()):
            if ext is not self and getattr(ext, 'handles_plan_validation', None) and ext.handles_plan_validation(args, kind=args.get('kind'), actor=actor):
                ext.validate_plan(k, db, actor, args, kind=args.get('kind'))
                return
        self.trips.validate(actor,args)
        pid=self.v.place_id(args.get('location'))
        place=self.places[pid]
        if self.l.weather(args['start_at'])['rain'] and place.get('rain') in ('avoid','closed'):
            raise Refused('avoid_place_in_rain')
        plan=dict(args,plan_id=args.get('_plan_id','proposal'))
        steps=self.itinerary(actor['actor_id'],plan)
        for account in {s['account'] for s in steps}:
            needed=sum(s['yen'] for s in steps if s['account']==account)
            if self.wallet.available(account)<needed:
                raise Refused('insufficient_funds')

    def guard(self,action,args,actor,resources=(),**kwargs):
        why=self.v.guard(action,args,actor,resources,**kwargs)
        if why:
            return why
        here=actor['location']
        outside=here=='away' or bool(self.v.place_id(here))
        outbound=action=='move' and (args.get('to')=='away' or self.v.place_id(args.get('to')))
        if action=='move' and (outside or outbound):
            if self.k.city_visits.is_door_move(actor['actor_id'], here, args):
                return []
            for ext in getattr(self.l, 'extensions', ()):
                is_door = getattr(ext, 'is_door_move', None)
                if is_door and is_door(actor['actor_id'], here, args):
                    return []
            j=self.job(actor['actor_id'])
            if not j or j['state'] not in ('running','returning'):
                return ['outing_plan_required']
            step=j['steps'][j['index']]
            if step['action']!='move' or args!=step['args']:
                return ['not_the_confirmed_route_step']
            candidates=self.s.db.execute("SELECT a.action_id,c.idempotency_key FROM actions a JOIN commands c"
                                         " ON c.command_id=a.command_id WHERE a.actor_id=? AND a.state IN ('running','accepted')",
                                         (actor['actor_id'],)).fetchall()
            if not any(r['idempotency_key']==j.get('command_key') for r in candidates):
                return ['route_step_not_issued_by_outing']
        pid=self.v.place_id(here)
        if pid and action=='sleep':
            current=self.s.db.execute('SELECT started_at FROM actions WHERE action_id=(SELECT action_id FROM actors WHERE actor_id=?)',
                                      (actor['actor_id'],)).fetchone()
            when=current[0] if current and current[0] is not None else self.s.clock.now()
            if not self.l.get(f'lodging:{actor["actor_id"]}:{pid}:{local(when).date()}'):
                # A night's receipt remains valid through the following morning.
                previous=local(when).date()-timedelta(days=1)
                if not (local(when).hour<12 and self.l.get(f'lodging:{actor["actor_id"]}:{pid}:{previous}')):
                    return ['sleep_requires_paid_lodging']
        if pid and action=='begin_activity':
            j=self.job(actor['actor_id'])
            if not j or j['state']!='running' or j['steps'][j['index']]['args']!=args:
                return ['activity_requires_confirmed_itinerary']
            issued=self.s.db.execute("SELECT 1 FROM actions a JOIN commands c ON c.command_id=a.command_id"
                                     " WHERE a.actor_id=? AND a.state IN ('accepted','running') AND c.idempotency_key=?",
                                     (actor['actor_id'],j.get('command_key'))).fetchone()
            if not issued:
                return ['activity_not_issued_by_outing']
        if pid and action in ('begin_activity','eat'):
            dur=args.get('seconds',self.v.ACTIVITIES[args['activity']]['default']) if action=='begin_activity' else 600
            now=self.s.clock.now()
            # Completion rechecks the interval of the running action, not the later wall clock.
            a=self.s.db.execute('SELECT started_at,due_at FROM actions WHERE action_id=(SELECT action_id FROM actors WHERE actor_id=?)',
                               (actor['actor_id'],)).fetchone()
            start,end=(a[0],a[1]) if a and a[0] is not None else (now,now+dur)
            if not is_open(self.places[pid],start,end):
                return ['place_closed']
            if self.places[pid].get('rain')=='closed' and self.l.weather(now)['rain']:
                return ['place_closed_by_rain']
        return []

    def _account(self,actor,j,kind):
        shopping=self.shopping.current(j['plan_id']) if j else None
        if j and j['state'] in ('running', 'returning'):
            return j['steps'][j['index']]['account']
        return 'house' if kind=='ingredient' and shopping and shopping['actor']==actor else actor

    def buy(self,k,db,aid,actor,args,cmd):
        me=actor['actor_id']; pid=self.v.place_id(actor['location'])
        if not pid or pid not in self.places or actor['action_id']:
            raise Refused('purchase_requires_idle_customer_at_place')
        qty=args.get('qty'); item=args.get('item'); kind=args.get('kind')
        if type(qty) is not int or not 1<=qty<=100 or not isinstance(item,str):
            raise Refused('bad_purchase')
        now=self.s.clock.now(); place=self.places[pid]
        if not is_open(place,now,now):
            raise Refused('place_closed')
        garment=None
        original_name = item
        if kind=='ingredient':
            if item not in self.v.INGREDIENTS:
                item, matched = inventory.category(item)
                if not matched:
                    raise Refused('item_not_sold_here')
            spec=self.v.INGREDIENTS.get(item)
            sells=place.get('sells',[])
            if item == inventory.OTHER or not spec or not ('ingredients' in sells or item in sells or pid in spec.get('where_to_buy',[])):
                raise Refused('item_not_sold_here')
            cost=yen(spec['price_yen'])*qty
        elif kind=='menu':
            spec=next((m for m in place.get('menu',[]) if m['id']==item),None)
            if not spec or qty!=1:
                raise Refused('menu_not_sold_here')
            cost=yen(spec['price_yen'])
            if cost==0 and place.get('lodging_yen') and not self.l.get(f'lodging:{me}:{pid}:{local(now).date()}'):
                raise Refused('included_meal_requires_paid_lodging')
        elif kind=='lodging':
            if item!=pid or not self.v.LOCATIONS[actor['location']].get('lodging') or qty!=1:
                raise Refused('not_lodging')
            cost=yen(place['lodging_yen'])
        elif kind=='clothing':
            spec=place.get('clothing_stock',{}).get(item)
            if not spec or qty!=1:
                raise Refused('clothing_not_sold_here')
            garment=copy.deepcopy(spec['garment']); cost=yen(spec['price_yen'])
            garment['id']=f'{me}_bought_{aid}'
        else:
            raise Refused('unknown_purchase_kind')
        j=self.job(me); account=self._account(me,j,kind)
        if j and j['state'] in ('running','returning'):
            step=j['steps'][j['index']]
            if (step['action']!='buy' or step['args']!=args or step['yen']!=cost
                    or cmd.get('idempotency_key')!=j.get('command_key')):
                raise Refused('purchase_not_in_confirmed_itinerary')
            self.wallet.transfer(f"{j['key']}:{account}",aid,account,cost)
        else:
            self.wallet.hold(aid,account,cost)
        receipt = j['steps'][j['index']].get('promise', {}) if j and j['state'] in ('running','returning') else {}
        eid=k._emit('purchased',me,aid,actor['location'],{'kind':kind,'item':item,'name':original_name,'qty':qty,'yen':cost,'account':account,
                    'plan_id':j['plan_id'] if j else None, 'resource_id':f'bought:{aid}',
                    'intent_ref':receipt.get('intent'), 'beneficiary':receipt.get('beneficiary', []),
                    'fingerprint':receipt.get('fingerprint')},
                    scope=f'self:{me}',occurred=now,observers=())
        if kind in ('ingredient','menu'):
            rid=f'bought:{aid}'
            db.execute('INSERT INTO resources(resource_id,kind,item,holder,qty,source) VALUES(?,?,?,?,?,?)',
                       (rid,'food',item,me,qty,f'event:{eid}'))
            if j:
                j.setdefault('purchases',[]).append(rid)
                self.save(me,j)
        elif kind=='lodging':
            self.l.put(f'lodging:{me}:{pid}:{local(now).date()}',eid)
        else:
            self.l.wardrobe.add(db,me,garment,f'event:{eid}')
        k._finish(db,aid,'succeeded',None,now)
        self.wallet.settle(aid,eid,now)
        return dict(status='succeeded',action_id=aid,event_id=eid)

    def completed(self,k,db,action,actor,args,eid,location):
        me=actor['actor_id']; j=self.job(me)
        if not j or j.get('action_id')!=action['action_id']:
            return
        step=j['steps'][j['index']]
        self.wallet.settle(action['action_id'],eid,self.s.clock.now())
        if step.get('leg'):
            k._emit('travel_leg_completed',me,action['action_id'],location,dict(step['leg'],plan_id=j['plan_id']),
                    scope=f'self:{me}',occurred=action['due_at'],observers=(),source_ref=f'event:{eid}')
        if step.get('packing'):
            k._emit('luggage_packed',me,action['action_id'],location,{'garments':step['packing'],'plan_id':j['plan_id']},
                    scope=f'self:{me}',occurred=action['due_at'],observers=(),source_ref=f'event:{eid}')
        if step.get('unpack') or (action['action_type'] == 'move' and location == 'living'):
            for rid in j.get('purchases', []):
                resource = k.resource(rid)
                if not resource or resource['holder'] != me or resource['qty'] <= 0:
                    continue
                purchase = db.execute("SELECT payload,action_id FROM events WHERE kind='purchased' AND event_id=?",
                                      (int(resource['source'].split(':')[-1]),)).fetchone()
                if purchase:
                    body = json.loads(purchase['payload'])
                    body.update(qty=min(body['qty'],resource['qty']), action_id=purchase['action_id'], plan_id=j['plan_id'])
                    k._emit('brought_home', me, action['action_id'], location, body, scope=f'self:{me}',
                            occurred=action['due_at'], observers=())
        if step.get('unpack'):
            for rid in j.get('purchases',[]):
                db.execute("UPDATE resources SET holder=NULL,location='kitchen',version=version+1 WHERE resource_id=? AND holder=?",
                           (rid,me))
        j['index']+=1; j['action_id']=None
        self.save(me,j)

    def _activate(self,plan,actor):
        if self.l.busy(actor) or self.owns(actor):
            return
        key=f"journey:{plan['plan_id']}:{actor}"
        if self.l.get(key):
            return
        with self.s.tx():
            steps=self.itinerary(actor,plan)
            for account in {s['account'] for s in steps}:
                amount=sum(s['yen'] for s in steps if s['account']==account)
                if self.wallet.available(account)<amount:
                    raise Refused('insufficient_funds')
            for account in {s['account'] for s in steps}:
                self.wallet.hold(f'{key}:{account}',account,sum(s['yen'] for s in steps if s['account']==account))
            self.l.put(key,'running')
            self.save(actor,dict(key=key,plan_id=plan['plan_id'],place=plan['location'],steps=steps,index=0,
                                 state='running',action_id=None,purchases=[],accounts=sorted({s['account'] for s in steps}),revision=0))
            self.l.plans.set_state(plan['plan_id'],'running')

    def _finish(self,actor,j):
        for account in j['accounts']:
            self.wallet.release(f"{j['key']}:{account}")
        j['state']='done'; self.save(actor,j); self.l.put(j['key'],'done')
        plan=self.l.plans.plan(j['plan_id'])
        withdrawn = bool(self.shopping.current(j['plan_id']) and j.get('skipped_promises')
                         and not j.get('purchases') and not j.get('failure'))
        if plan['state'] not in ('cancelled','lapsed') and all(self.l.get(f"journey:{j['plan_id']}:{a}")=='done' for a in plan['members']):
            early = any((self.job(a) or {}).get('early_return') or (self.job(a) or {}).get('failure')
                        for a in plan['members'])
            # Failed procurement retains its existing lapsed state; never finish it
            # as done before Shopping.finish gets to record the failure.
            failed_procurement = self.shopping.current(j['plan_id']) and j.get('failure')
            self.l.plans.finish(j['plan_id'], 'cancelled' if withdrawn else 'lapsed' if failed_procurement else 'early_home' if early else 'done')
        self.shopping.finish(j['plan_id'],not j.get('failure'),withdrawn=withdrawn)

    def _advance(self,actor,j):
        with self.s.tx():
            if self.k.actor(actor)['action_id']:
                return
            if j.get('action_id'):
                action=self.s.db.execute('SELECT state FROM actions WHERE action_id=?',(j['action_id'],)).fetchone()
                if action and action[0] in ('accepted','running'):
                    return
                step=j['steps'][j['index']]
                self.wallet.restore(f"{j['key']}:{step['account']}",j['action_id'])
                j['state']='paused'; j['failure']='step_'+(action[0] if action else 'missing')
                self.save(actor,j)
                return
            if j['index']>=len(j['steps']):
                self._finish(actor,j)
                return
            if j['index']==0:
                weather=self.l.weather(self.s.clock.now())
                self.l.wardrobe.dress(actor,'out',weather['temp_c'],weather['rain'])
                if weather['rain'] and not self._umbrella(actor):
                    umbrella=self._own_umbrella(actor)
                    outfit=self.l.wardrobe.current(actor)
                    if umbrella and outfit:
                        self.k.submit(dict(idempotency_key=f"{j['key']}:umbrella",principal='routine',actor_id=actor,origin='routine',
                                           action_type='change_clothes',args={'items':outfit['items']+[umbrella['garment_id']],'occasion':'out'}))
                    else:
                        j['state']='paused'; j['failure']='rain_requires_owned_umbrella'; self.save(actor,j)
                        return
            step=j['steps'][j['index']]
            receipt = step.get('promise')
            if receipt:
                source = getattr(self.l, 'promise_source', None)
                current = source.errands(actor) if source else []
                if not any(x['intent'] == receipt['intent'] and x['item'] == receipt['item']
                           and x['fingerprint'] == receipt['fingerprint'] for x in current):
                    # A withdrawn private line cannot cancel the household's
                    # remaining purchases or impose a global restock cooldown.
                    j['index'] += 1
                    j['skipped_promises'] = True
                    self.save(actor,j)
                    return
            weather=self.l.weather(self.s.clock.now())
            if step['action']=='sleep':
                self.l.wardrobe.dress(actor,'sleep',weather['temp_c'])
            elif (self.l.wardrobe.current(actor) or {}).get('occasion')=='sleep':
                self.l.wardrobe.dress(actor,'out',weather['temp_c'],weather['rain'])
            if step['action']=='eat':
                rid=next((r for r in reversed(j['purchases']) if self.k.resource(r)['item']==step['args']['item']),None)
                if rid:
                    step['args']=dict(step['args'],resource_id=rid)
            j['command_key']=f"{j['key']}:revision:{j['revision']}:step:{j['index']}"
            self.save(actor,j)
            result=self.k.submit(dict(idempotency_key=j['command_key'],principal='routine',actor_id=actor,origin='routine',
                                      action_type=step['action'],args=step['args']))
            if result['status']=='succeeded':
                # Instant buys saved their newly acquired resource ids in this same transaction.
                j=self.job(actor); j['index']+=1
            elif result['status'] in ('running','accepted'):
                self.wallet.transfer(f"{j['key']}:{step['account']}",result['action_id'],step['account'],step['yen'])
                j['action_id']=result['action_id']
            else:
                j['state']='paused'; j['failure']=result.get('reason','failed')
            self.save(actor,j)

    def _umbrella(self,actor):
        outfit=self.l.wardrobe.current(actor)
        if outfit and any('umbrella' in i for i in outfit['items']):
            return True
        return self.s.db.execute("SELECT 1 FROM resources WHERE item='umbrella' AND holder=? AND qty>0",(actor,)).fetchone() is not None

    def _own_umbrella(self,actor):
        """An umbrella of her own to take; a parasol (阳伞, elsa_parasol) is no rain gear."""
        return next((g for g in self.l.wardrobe.owned(actor) if 'umbrella' in g['garment_id']
                     or ('伞' in g['label'] and '阳伞' not in g['label'])),None)

    def rain_ready(self,actor):
        """Whether she can go out in the rain: an umbrella on her or one of her own to take."""
        return self._umbrella(actor) or self._own_umbrella(actor) is not None

    def return_home(self,actor):
        j=self.job(actor)
        if not j or self.k.actor(actor)['action_id']:
            return
        pid=self.v.place_id(self.k.actor(actor)['location'])
        steps=[]
        if pid:
            for i,leg in enumerate(reversed(self.route(self.places[pid]))):
                steps.append(dict(action='move' if i==0 else 'wait',args={'to':'away'} if i==0 else {'seconds':leg['seconds']},
                                  seconds=leg['seconds'],yen=leg['yen'],account=actor,leg=leg))
        if pid or self.k.actor(actor)['location']=='away':
            steps.append(dict(action='move',args={'to':'living'},seconds=60,yen=0,account=actor))
        if self.shopping.current(j['plan_id']) and j.get('purchases'):
            steps.append(dict(action='wait',args={'seconds':300},seconds=300,yen=0,account=actor,unpack=True))
        j.update(steps=steps,index=0,action_id=None,state='returning',early_return=True,revision=j['revision']+1)
        self.save(actor,j)
        self.k._emit('outing_returned_early',actor,None,self.k.actor(actor)['location'],
                     {'plan_id':j['plan_id'],'reason':j.get('failure')},scope=f'self:{actor}',occurred=self.s.clock.now(),observers=())

    def _city_routed_plan(self, plan):
        with self.s.read() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='city_visit_carry'").fetchone():
                if db.execute("SELECT 1 FROM city_visit_carry WHERE plan_id=? LIMIT 1", (plan['plan_id'],)).fetchone():
                    return True
        for ext in self.l.extensions:
            if ext is not self and getattr(ext, 'claims_plan', None) and ext.claims_plan(plan):
                return True
        pid = self.v.place_id(plan.get('location'))
        return bool(plan.get('location') and (not pid or pid not in self.places))

    def step(self):
        self.wallet.sync()
        self.shopping.step()
        now=self.s.clock.now()
        with self.s.read() as db:
            rows=[dict(r,members=json.loads(r['members'])) for r in db.execute(
                "SELECT * FROM plans WHERE kind IN ('outing','trip') AND state IN ('confirmed','running') AND start_at<=?",(now,))]
        for plan in rows:
            if self._city_routed_plan(plan):
                continue
            for actor in plan['members']:
                try:
                    self._activate(plan,actor)
                except Refused as e:
                    if self.l.once(f"outing:refused:{plan['plan_id']}:{actor}"):
                        self.k.audit('outing_not_started',actor=actor,plan_id=plan['plan_id'],reason=e.reason)
                    self.l.plans.set_state(plan['plan_id'],'cancelled',e.reason)
                    self.shopping.finish(plan['plan_id'],False)
                    break
        for actor in self.k.roster:
            j=self.job(actor)
            if j and j['state']=='running' and self.l.plans.plan(j['plan_id'])['state']=='cancelled':
                j['state']='paused'; j['failure']='plan_cancelled'; self.save(actor,j)
            if j and j['state']=='paused':
                with self.s.tx():
                    self.return_home(actor)
                j=self.job(actor)
            if j and j['state'] in ('running','returning'):
                self._advance(actor,j)


def install(life):
    """Factory for Life(extensions=[install]); classes with step are treated as instances by f3a7f1b."""
    return Outings(life)
