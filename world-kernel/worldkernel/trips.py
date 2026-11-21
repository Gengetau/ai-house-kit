"""Trip rules on the shared Plans ledger: no second invitation or consent system."""
import json
from .kernel import Refused
from .wallet import local


class Trips:
    def __init__(self, life, places):
        self.l, self.s, self.places = life, life.s, places

    def validate(self, actor, args):
        pid = str(args.get('location','')).removeprefix('place:')
        place = self.places.get(pid)
        if not place:
            raise Refused('unknown_outing_place')
        start, end = args['start_at'], args.get('end_at')
        tier = place['tier']
        nights = (local(end).date()-local(start).date()).days if end is not None else 0
        if tier == 'overnight' and nights not in place.get('nights',[1,2]):
            raise Refused('unsupported_trip_nights')
        if tier != 'overnight' and nights:
            raise Refused('outing_must_return_same_day')
        days = 3 if tier == 'overnight' else 1 if tier == 'daytrip' else 0
        proposed = args.get('_proposed_at',self.s.clock.now())
        if (local(start).date()-local(proposed).date()).days < days:
            raise Refused('insufficient_trip_notice')
        me = actor['actor_id']
        with self.s.read() as db:
            has_city_carry = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='city_visit_carry'").fetchone() is not None
            for row in db.execute("SELECT * FROM plans WHERE kind IN ('outing','trip') AND state NOT IN ('cancelled','lapsed','missed')").fetchall():
                if row['plan_id'] == args.get('_plan_id') or me not in json.loads(row['members']):
                    continue
                # an outing she is back from (done, or her own journey over while others stay on) is no
                # longer in the way of a new one; it still counts toward the month's overnight trip below
                back = row['state'] == 'done' or self.l.get(f"journey:{row['plan_id']}:{me}") == 'done'
                if not back and has_city_carry:
                    back = db.execute(
                        "SELECT 1 FROM city_visit_carry c JOIN city_visits v USING(visit_id)"
                        " WHERE c.plan_id=? AND c.actor_id=? AND v.state IN ('closed','reconcile') LIMIT 1",
                        (row['plan_id'], me),
                    ).fetchone() is not None
                other_end = self.l.plans.effective_end(row) if hasattr(self.l, 'plans') else (row['end_at'] if row['end_at'] is not None else row['start_at']+4*3600)
                if not back and start < other_end and row['start_at'] < (end if end is not None else start+4*3600):
                    raise Refused('outing_schedule_conflict')
                other = self.places.get(str(row['location']).removeprefix('place:'),{})
                if tier == 'overnight' and other.get('tier') == 'overnight' and local(start).strftime('%Y-%m') == local(row['start_at']).strftime('%Y-%m'):
                    raise Refused('monthly_overnight_limit')
        return nights
