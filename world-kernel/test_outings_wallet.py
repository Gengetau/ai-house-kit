"""R105 follow-ups: same-day cities and truthful, restart-safe procurement plans."""
from test_life_outings import Base, at
from worldkernel.wallet import HOUSE_ALLOWANCE


class CityNotice(Base):
    def test_same_day_city_invite_and_own_acceptance(self):
        for place in ('kissa_tomoshibi', 'muginohi', 'shiorido'):
            with self.subTest(place=place):
                host, guest = ('nell', 'wanda') if place == 'kissa_tomoshibi' else (
                    ('elsa', 'yuki') if place == 'muginohi' else ('alma', 'mina'))
                p = self.plan(place, at(clock='13:00'), at(clock='19:00'),
                              host=host, guest=guest, what='shopping')
                self.assertEqual(self.l.plans.plan(p)['state'], 'confirmed')

    def test_oldtown_city_routes_and_fare(self):
        # 11-20 hotfix: the city tier is 白帆市; without the city bridge these fall back to the catalog route
        for place, minutes, yen in (('shiorido', 30, 200), ('muginohi', 36, 260), ('kamitsuki', 37, 260)):
            with self.subTest(place=place):
                segs = self.o.route(self.o.places[place])
                self.assertEqual(segs[0], {'mode': 'walk', 'seconds': 12 * 60, 'yen': 0})
                self.assertEqual(sum(s['seconds'] for s in segs), minutes * 60)
                self.assertEqual(sum(s['yen'] for s in segs), yen)

    def test_daytrip_still_needs_next_jst_date(self):
        r = self.command('nell', 'invite', dict(to=['wanda'], kind='trip', what='sightseeing',
                         location='place:kamakura', start_at=at(clock='11:00'), end_at=at(clock='19:00')))
        self.assertEqual(r['reason'], 'insufficient_trip_notice')
        p = self.plan('kamakura', at('2026-11-03', '09:00'), at('2026-11-03', '18:00'),
                      kind='trip', what='sightseeing')
        self.assertEqual(self.l.plans.plan(p)['state'], 'confirmed')


class ProcurementPlans(Base):
    def failed_errand(self):
        # in the rain an errand only goes to someone with an umbrella, so this one is listed on a dry
        # minute and the rain starts before she leaves; nobody has an umbrella of her own (nell's from v2 neither)
        with self.s.tx() as db:
            db.execute("DELETE FROM garments WHERE garment_id LIKE '%umbrella%'")
        self.o.shopping.targets = {'egg': 14}
        self.o.shopping.step()
        self.rain = True
        self.o.step()
        p = self.l.get('shopping:current')['plan_id']
        for _ in range(10):
            self.l.k.tick(); self.o.step()
            if self.l.get('shopping:current')['state'] == 'cancelled':
                return p
            self.clock.advance(60)
        self.fail('failed errand did not settle')

    def test_no_departure_is_lapsed_never_done_and_retry_survives_restart(self):
        p = self.failed_errand()
        self.assertEqual(self.l.plans.plan(p)['state'], 'lapsed')
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM events WHERE kind='travel_leg_completed'").fetchone()[0], 0)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM events WHERE kind='purchased'").fetchone()[0], 0)
        self.assertEqual(self.o.wallet.available('house'), HOUSE_ALLOWANCE)
        self.restart(); self.o.shopping.targets = {'egg': 14}
        for _ in range(30):
            self.clock.advance(60); self.l.k.tick(); self.o.step()
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM plans WHERE what='shopping'").fetchone()[0], 1)
        # After the persisted retry interval, improved weather permits a real replacement errand.
        self.rain = False
        self.clock.advance(3600); self.o.step()
        replacement = self.l.get('shopping:current')['plan_id']
        self.assertNotEqual(replacement, p)
        self.finish(replacement)
        self.assertEqual(self.l.plans.plan(p)['state'], 'lapsed')
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM plans WHERE what='shopping' AND state='done'").fetchone()[0], 1)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM events WHERE kind='purchased'").fetchone()[0], 1)

    def test_success_is_one_plan_per_departure_even_with_restarts(self):
        self.o.shopping.targets = {'egg': 14}
        self.o.step(); p = self.l.get('shopping:current')['plan_id']
        self.finish(p, restarts=(3, 8))
        self.o.shopping.targets = {'egg': 14}
        for _ in range(10):
            self.o.step(); self.clock.advance(60); self.l.k.tick()
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM plans WHERE what='shopping'").fetchone()[0], 1)
        self.assertEqual(self.l.plans.plan(p)['state'], 'done')
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM events WHERE kind='travel_leg_completed'").fetchone()[0], 2)

    def test_expired_unstarted_plan_does_not_block_future_procurement(self):
        self.o.shopping.targets = {'egg': 14}
        self.o.shopping.step(); p = self.l.get('shopping:current')['plan_id']
        self.clock.advance(3*3600+60); self.l.plans.sync()
        self.assertEqual(self.l.plans.plan(p)['state'], 'lapsed')
        self.o.shopping.step()
        self.assertEqual(self.l.get('shopping:current')['state'], 'cancelled')
        self.clock.advance(3600); self.o.step()
        self.assertNotEqual(self.l.get('shopping:current')['plan_id'], p)

    def test_started_but_failed_procurement_is_not_done(self):
        self.o.shopping.targets = {'egg': 14}
        self.o.step(); p = self.l.get('shopping:current')['plan_id']
        self.o.places['supermarket']['open'] = '00:00-00:01'
        for _ in range(120):
            self.l.k.tick(); self.o.step()
            if self.l.get('shopping:current')['state'] == 'cancelled':
                break
            self.clock.advance(60)
        self.assertEqual(self.l.plans.plan(p)['state'], 'lapsed')
        self.assertGreater(self.s.db.execute("SELECT COUNT(*) FROM events WHERE kind='travel_leg_completed'").fetchone()[0], 0)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM events WHERE kind='purchased'").fetchone()[0], 0)
