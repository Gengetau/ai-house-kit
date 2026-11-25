"""S1-Kernel weather compatibility, half-open resolution, and paired rollback gate tests."""
import datetime as dt
import json
import os
import tempfile
import unittest

from worldkernel import outings
from worldkernel.city_bridge import CityBridgeAdapter, DeterministicCityModel, install_from_env
from worldkernel.city_client import (
    HOUR_MS,
    CityBridgeError,
    CityBridgeHttpClient,
    CityWeatherProvider,
    city_fallback_weather,
    resolve_weather_at,
)
from worldkernel.kernel import Refused
from worldkernel.legacy import offline_world
from worldkernel.life import JST, ROSTER, Life
from worldkernel.store import Store, VirtualClock, dumps
from test_city_m1a import MockCityBridgeEngine


def _ms(year, month, day, hour, minute=0, second=0):
    return int(dt.datetime(year, month, day, hour, minute, second, tzinfo=JST).timestamp() * 1000)


class CityWeatherS1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "life1_s1.db")
        self.world_db = os.path.join(self.tmp.name, "world_s1.db")
        offline_world(self.world_db)
        self.t_1020_ms = _ms(2026, 11, 17, 10, 20, 0)
        self.clock = VirtualClock(self.t_1020_ms / 1000.0)
        self.engine = MockCityBridgeEngine(self.clock)
        self._seq = 0
        self._boot()
        with self.s.tx() as db:
            for a in ROSTER:
                self.k._set_actor(db, a, location="living", activity="idle", action_id=None)

    def tearDown(self):
        self.s.release()
        self.s.close()
        self.tmp.cleanup()

    def _boot(self, *, env=None):
        self._seq += 1
        self.s = Store(self.db_path, self.clock, synthetic=True, seed=31300 + self._seq)
        self.s.acquire()
        self.client = CityBridgeHttpClient(
            "http://127.0.0.1:17700/city/bridge/v1",
            self.engine.bridge_key,
            sleep_fn=lambda _: None,
            transport=self.engine.transport,
        )
        self.life = Life(self.s, extensions=[outings.install])
        self.k = self.life.k
        self.outings_ext = next(e for e in self.life.extensions if hasattr(e, "rain_ready"))
        self.adapter = None
        if env is not None:
            # Reuse deterministic transport on installed adapter via patched client or direct CityBridgeAdapter
            self.adapter = CityBridgeAdapter(
                self.life,
                self.client,
                city_model=DeterministicCityModel(),
                allowed_actors=("nell", "elsa"),
                intercept_plans=(env.get("WORLD_KERNEL_CITY_PLANS", "0") == "1"),
                weather_source=env.get("WORLD_KERNEL_WEATHER_SOURCE", "open-meteo"),
            )
            self.life.extensions.append(self.adapter)

    def _restart(self, *, advance_s=0, env=None):
        self.s.release()
        self.s.close()
        if advance_s:
            self.clock.advance(advance_s)
        self._boot(env=env)

    def _make_25h_payload(self, base_hour_ms, *, override=None, rain_hours=()):
        hourly = []
        for k in range(25):
            at_ms = base_hour_ms + k * HOUR_MS
            is_rain = k in rain_hours
            nat = {
                "code": 61 if is_rain else 0,
                "temp_c": 16.0 - (k % 4) * 0.5,
                "precip_mm": 1.8 if is_rain else 0.0,
                "rain": is_rain,
            }
            ov_covers = (
                override is not None
                and override["starts_at_ms"] < at_ms + HOUR_MS
                and max(self.t_1020_ms, at_ms) < override["expires_at_ms"]
            )
            pt = {
                "at_ms": at_ms,
                "code": override["code"] if ov_covers else nat["code"],
                "temp_c": override["temp_c"] if ov_covers else nat["temp_c"],
                "precip_mm": override["precip_mm"] if ov_covers else nat["precip_mm"],
                "rain": override["rain"] if ov_covers else nat["rain"],
                "natural": nat,
            }
            hourly.append(pt)
        cur = override if override is not None else hourly[0]
        snap = {
            "area": "sakuraoka",
            "observed_at_ms": self.t_1020_ms,
            "fetched_at_ms": self.t_1020_ms,
            "code": cur["code"],
            "temp_c": cur["temp_c"],
            "precip_mm": cur["precip_mm"],
            "rain": cur["rain"],
            "source": "qingkong",
            "status": "ok",
            "text": "下雨" if cur["rain"] else "晴",
            "hourly": hourly,
            "override": override,
        }
        return {"at_ms": self.t_1020_ms, "attribution": "Qingkong City Climate Engine", "areas": [snap]}

    def test_code_zero_preserved_never_coerced_to_one(self):
        h10 = _ms(2026, 11, 17, 10, 0, 0)
        payload = self._make_25h_payload(h10)

        def transport(method, path, query, body, headers):
            if path == "/weather":
                return 200, {}, json.dumps(payload)
            return self.engine.transport(method, path, query, body, headers)

        self.client._transport = transport
        provider = CityWeatherProvider(self.s, self.client, weather_source="qingkong")
        snap = provider.area_snapshot("sakuraoka", sync=True)
        self.assertEqual(0, snap["code"])
        self.assertEqual("晴", snap["text"])
        with self.s.read() as db:
            row = db.execute("SELECT * FROM city_weather_cache WHERE area='sakuraoka'").fetchone()
        self.assertEqual(0, row["code"])
        self.assertEqual("晴", row["text"])
        # Re-read offline via resolve_weather_at (code=0 survives DB roundtrip)
        res_direct = resolve_weather_at(row, self.t_1020_ms + 5 * 60_000, clock_ms=self.t_1020_ms + 5 * 60_000)
        self.assertEqual(0, res_direct["code"])
        self.assertEqual("晴", res_direct["text"])
        res_snap = provider.area_snapshot("sakuraoka", now_ms=self.t_1020_ms + 5 * 60_000, sync=False)
        self.assertEqual(0, res_snap["code"])
        self.assertEqual("晴", res_snap["text"])

    def test_half_open_hourly_and_outings_start_at_validation(self):
        h10 = _ms(2026, 11, 17, 10, 0, 0)
        # k=0 -> 10:00 clear, k=1 -> 11:00 clear, k=2 -> 12:00 rain
        payload = self._make_25h_payload(h10, rain_hours=(2,))

        def transport(method, path, query, body, headers):
            if path == "/weather":
                return 200, {}, json.dumps(payload)
            return self.engine.transport(method, path, query, body, headers)

        self.client._transport = transport
        provider = CityWeatherProvider(self.s, self.client, weather_source="qingkong")
        self.life.weather = provider

        # Point Life outings place lookup to a park with rain='avoid'
        avoid_pid = next((pid for pid, p in self.outings_ext.places.items() if p.get("rain") in ("avoid", "closed")), None)
        self.assertIsNotNone(avoid_pid)
        alias_loc = next((loc for loc in self.k.V.LOCATIONS if self.k.V.place_id(loc) == avoid_pid), None)
        self.assertIsNotNone(alias_loc)

        t_1150 = _ms(2026, 11, 17, 11, 50, 0) / 1000.0
        t_1200 = _ms(2026, 11, 17, 12, 0, 0) / 1000.0
        w_1150 = provider(t_1150)
        self.assertFalse(w_1150["rain"])
        self.assertEqual(0, w_1150["code"])
        # Future target_ms must NOT pollute _last_fetch_attempt_ms (stays at clock 10:20)
        self.assertEqual(self.t_1020_ms, provider._last_fetch_attempt_ms)

        # 11:50 passes outings validation
        act_name = next((a for a in self.outings_ext.places[avoid_pid].get("activities", ()) if a not in ("eat_out", "shopping")), "walk")
        with self.s.tx() as db:
            actor = self.k.actor("nell")
            self.outings_ext.validate(
                self.k,
                db,
                actor,
                {"location": alias_loc, "kind": "outing", "what": act_name, "start_at": t_1150, "end_at": t_1150 + 4 * 3600, "members": ["nell"]},
            )

        # 12:00 hits half-open rainy [12:00, 13:00) slot -> Refused('avoid_place_in_rain')
        w_1200 = provider(t_1200)
        self.assertTrue(w_1200["rain"])
        self.assertEqual(61, w_1200["code"])
        with self.s.tx() as db:
            actor = self.k.actor("nell")
            with self.assertRaises(Refused) as cm:
                self.outings_ext.validate(
                    self.k,
                    db,
                    actor,
                    {"location": alias_loc, "kind": "outing", "what": act_name, "start_at": t_1200, "end_at": t_1200 + 4 * 3600, "members": ["nell"]},
                )
            self.assertEqual("avoid_place_in_rain", cm.exception.reason)

        # Read tx must remain ReadOnlyTx-safe when weather() is queried
        with self.s.read():
            self.assertFalse(provider(t_1150)["rain"])
            self.assertTrue(provider(t_1200)["rain"])

        # Beyond 25h forecast window -> isomorphic fallback (rain=False)
        t_beyond_ms = h10 + 26 * HOUR_MS
        beyond = provider.area_snapshot("sakuraoka", now_ms=self.t_1020_ms, target_ms=t_beyond_ms, sync=False)
        self.assertEqual(city_fallback_weather(t_beyond_ms, "sakuraoka")["temp_c"], beyond["temp_c"])
        self.assertFalse(beyond["rain"])

    def test_non_hour_override_expiry_boundary_1105(self):
        h10 = _ms(2026, 11, 17, 10, 0, 0)
        exp_1105 = _ms(2026, 11, 17, 11, 5, 0)
        ov = {
            "preset": "rain",
            "code": 61,
            "temp_c": 12.5,
            "precip_mm": 2.4,
            "rain": True,
            "starts_at_ms": self.t_1020_ms,
            "expires_at_ms": exp_1105,
        }
        payload = self._make_25h_payload(h10, override=ov)

        def transport(method, path, query, body, headers):
            if path == "/weather":
                return 200, {}, json.dumps(payload)
            return self.engine.transport(method, path, query, body, headers)

        self.client._transport = transport
        provider = CityWeatherProvider(self.s, self.client, weather_source="qingkong")
        self.assertTrue(provider.sync(self.t_1020_ms, force=True))

        # 11:04 -> still covered by override (rain=True)
        r_1104 = provider.area_snapshot("sakuraoka", now_ms=self.t_1020_ms, target_ms=_ms(2026, 11, 17, 11, 4), sync=False)
        self.assertTrue(r_1104["rain"])
        self.assertEqual(61, r_1104["code"])

        # 11:05 and 11:10 -> past expiry, resolves hourly[1]['natural'] (code=0, rain=False)
        for minute in (5, 10):
            r_after = provider.area_snapshot("sakuraoka", now_ms=self.t_1020_ms, target_ms=_ms(2026, 11, 17, 11, minute), sync=False)
            self.assertFalse(r_after["rain"])
            self.assertEqual(0, r_after["code"])

    def test_r4_revert_cache_poisoning_defense_preserves_synthetic(self):
        now_ms = self.t_1020_ms
        with self.s.tx() as db:
            # Leftover self-authored rain cached under qingkong + legitimate synthetic row
            db.execute(
                "INSERT INTO city_weather_cache(area,observed_at_ms,fetched_at_ms,last_ok_at_ms,code,temp_c,precip_mm,rain,source,status,text,hourly_json)"
                " VALUES('sakuraoka',?,?,?,?,?,?,?,?,?,?,?)",
                (now_ms, now_ms, now_ms, 61, 14.0, 2.4, 1, "qingkong", "ok", "下雨", "[]"),
            )
            db.execute(
                "INSERT INTO city_weather_cache(area,observed_at_ms,fetched_at_ms,last_ok_at_ms,code,temp_c,precip_mm,rain,source,status,text,hourly_json)"
                " VALUES('rurigai',?,?,?,?,?,?,?,?,?,?,?)",
                (now_ms, now_ms, None, 2, 15.0, 0.0, 0, "synthetic", "fallback", "多云间晴", "[]"),
            )

        # Bridge /weather fails with CityBridgeError within 3h WEATHER_STALE_MS window
        def fail_weather_transport(method, path, query, body, headers):
            if path == "/weather":
                raise CityBridgeError("INTERNAL", "bridge temporarily unavailable")
            return self.engine.transport(method, path, query, body, headers)

        self.client._transport = fail_weather_transport
        provider = CityWeatherProvider(self.s, self.client, weather_source="open-meteo")
        self.clock.advance(1800)  # 30m later, still well within 3h stale window
        snap = provider.area_snapshot("sakuraoka", sync=True)
        self.assertFalse(snap["rain"])
        self.assertEqual("synthetic", snap["source"])
        self.assertEqual("fallback", snap["status"])

        # Verify leftover 'qingkong' row was purged while 'synthetic' row on rurigai remained intact
        with self.s.read() as db:
            qk_cnt = db.execute("SELECT COUNT(*) FROM city_weather_cache WHERE source='qingkong'").fetchone()[0]
            syn_cnt = db.execute("SELECT COUNT(*) FROM city_weather_cache WHERE area='rurigai' AND source='synthetic'").fetchone()[0]
        self.assertEqual(0, qk_cnt)
        self.assertEqual(1, syn_cnt)

        # Direct resolve_weather_at also refuses wrong-source row even before DB delete
        poison_row = {
            "area": "sakuraoka",
            "observed_at_ms": now_ms,
            "fetched_at_ms": now_ms,
            "last_ok_at_ms": now_ms,
            "code": 61,
            "temp_c": 14.0,
            "precip_mm": 2.4,
            "rain": 1,
            "source": "qingkong",
            "status": "ok",
            "text": "下雨",
            "hourly_json": "[]",
        }
        direct_res = resolve_weather_at(poison_row, now_ms + 600_000, clock_ms=now_ms + 600_000, allowed_sources=frozenset(("open-meteo", "synthetic")))
        self.assertFalse(direct_res["rain"])
        self.assertEqual("synthetic", direct_res["source"])

    def test_r5_two_step_rollback_gap_no_handover_and_pre_city_plans_restore(self):
        # Pre-check install_from_env wires WORLD_KERNEL_WEATHER_SOURCE & WORLD_KERNEL_CITY_PLANS
        env_off = {
            "WORLD_KERNEL_CITY_BRIDGE": "1",
            "CITY_BRIDGE_KEY": "weather-test-bridge-key-not-a-secret",  # the kit wants 24 characters or more here
            "WORLD_KERNEL_CITY_PLANS": "0",
            "WORLD_KERNEL_WEATHER_SOURCE": "qingkong",
            "WORLD_KERNEL_CITY_ACTORS": "nell,elsa",
        }
        installed_off = install_from_env(self.life, env=env_off)
        self.assertIsNotNone(installed_off)
        self.assertFalse(installed_off.intercept_plans)
        self.assertEqual("qingkong", installed_off.weather.weather_source)

        def _confirm_plan(life_k, life_obj, tag, start_offset_s=300):
            t0 = life_obj.s.clock.now()
            r1 = life_k.submit(
                {
                    "idempotency_key": f"r5:{tag}:inv",
                    "principal": "channel:nell",
                    "actor_id": "nell",
                    "origin": "self",
                    "action_type": "invite",
                    "args": {
                        "to": ["elsa"],
                        "kind": "outing",
                        "what": "cafe_time",
                        "location": "place:rurigai",
                        "start_at": t0 + start_offset_s,
                        "end_at": t0 + 7200,
                    },
                }
            )
            self.assertEqual("succeeded", r1["status"], r1)
            pid = r1["plan_id"]
            iid = next(p["inv_id"] for p in life_obj.plans.pending_for("elsa") if p["plan_id"] == pid)
            r2 = life_k.submit(
                {
                    "idempotency_key": f"r5:{tag}:resp",
                    "principal": "channel:elsa",
                    "actor_id": "elsa",
                    "origin": "self",
                    "action_type": "respond_invite",
                    "args": {"inv_id": iid, "answer": "yes"},
                }
            )
            self.assertEqual("succeeded", r2["status"], r2)
            self.assertEqual("confirmed", life_obj.plans.plan(pid)["state"])
            return pid

        for pre_val in ("0", "1"):
            with tempfile.TemporaryDirectory() as sub:
                dbp = os.path.join(sub, f"life1_r5_{pre_val}.db")
                clk = VirtualClock(self.t_1020_ms / 1000.0)
                eng = MockCityBridgeEngine(clk)
                cli = CityBridgeHttpClient(
                    "http://127.0.0.1:17700/city/bridge/v1",
                    eng.bridge_key,
                    sleep_fn=lambda _: None,
                    transport=eng.transport,
                )
                s0 = Store(dbp, clk, synthetic=True, seed=31400 + int(pre_val))
                s0.acquire()
                l0 = Life(s0, extensions=[outings.install])
                with s0.tx() as db:
                    for a in ROSTER:
                        l0.k._set_actor(db, a, location="living", activity="idle", action_id=None)
                a0 = CityBridgeAdapter(
                    l0,
                    cli,
                    city_model=DeterministicCityModel(),
                    allowed_actors=("nell", "elsa"),
                    intercept_plans=(pre_val == "1"),
                    weather_source="open-meteo",
                )
                l0.extensions.append(a0)
                plan_gap = None
                if pre_val == "1":
                    plan_gap = _confirm_plan(l0.k, l0, f"gap_{pre_val}", start_offset_s=300)
                else:
                    # Even if a confirmed plan record exists in DB while PRE_CITY_PLANS='0', bridge must ignore it
                    t0 = clk.now()
                    with s0.tx() as db:
                        db.execute(
                            "INSERT INTO plans(plan_id,host,kind,what,location,start_at,end_at,members,state,created_at,updated_at)"
                            " VALUES('plan_gap_0','nell','outing','cafe_time','place:rurigai',?,?,?,'confirmed',?,?)",
                            (t0 + 300, t0 + 7200, json.dumps(["nell", "elsa"]), t0, t0),
                        )
                    plan_gap = "plan_gap_0"
                s0.release()
                s0.close()

                # Step 1 -> Step 2 gap: kernel restarts with WORLD_KERNEL_CITY_PLANS=0, clock crosses start_at
                clk.advance(360)
                s1 = Store(dbp, clk, synthetic=True, seed=31410 + int(pre_val))
                s1.acquire()
                l1 = Life(s1, extensions=[outings.install])
                a1 = CityBridgeAdapter(
                    l1,
                    cli,
                    city_model=DeterministicCityModel(),
                    allowed_actors=("nell", "elsa"),
                    intercept_plans=False,
                    weather_source="open-meteo",
                )
                l1.extensions.append(a1)
                a1._activate_due_city_plans()
                with s1.read() as db:
                    self.assertEqual(0, db.execute("SELECT COUNT(*) FROM city_visits").fetchone()[0])
                self.assertEqual("confirmed", l1.plans.plan(plan_gap)["state"])
                s1.release()
                s1.close()

                # Step 3 restore: restore WORLD_KERNEL_CITY_PLANS=PRE_CITY_PLANS and step
                s2 = Store(dbp, clk, synthetic=True, seed=31420 + int(pre_val))
                s2.acquire()
                l2 = Life(s2, extensions=[outings.install])
                a2 = CityBridgeAdapter(
                    l2,
                    cli,
                    city_model=DeterministicCityModel(),
                    allowed_actors=("nell", "elsa"),
                    intercept_plans=(pre_val == "1"),
                    weather_source="open-meteo",
                )
                l2.extensions.append(a2)
                if pre_val == "0":
                    # Restoring PRE_CITY_PLANS='0' keeps city bridge gate closed: due DB row ignored & new invite rejected
                    a2._activate_due_city_plans()
                    l2.step()
                    with s2.read() as db:
                        self.assertEqual(0, db.execute("SELECT COUNT(*) FROM city_visits").fetchone()[0])
                    self.assertFalse(a2.intercept_plans)
                    self.assertIsNone(l2.k.city_visits.current("nell"))
                    t2 = clk.now()
                    rej = l2.k.submit(
                        {
                            "idempotency_key": "r5:after_0:inv",
                            "principal": "channel:nell",
                            "actor_id": "nell",
                            "origin": "self",
                            "action_type": "invite",
                            "args": {
                                "to": ["elsa"],
                                "kind": "outing",
                                "what": "cafe_time",
                                "location": "place:rurigai",
                                "start_at": t2 + 60,
                                "end_at": t2 + 5400,
                            },
                        }
                    )
                    self.assertEqual("failed", rej["status"])
                else:
                    l2.step()
                    self.assertTrue(a2.intercept_plans)
                    self.assertIsNotNone(l2.k.city_visits.current("nell"))
                    self.assertIsNotNone(l2.k.city_visits.current("elsa"))
                s2.release()
                s2.close()

    def test_open_meteo_current_rain_overrides_dry_hourly_and_midhour_expiry(self):
        h10 = _ms(2026, 11, 17, 10, 0, 0)
        h11 = h10 + HOUR_MS
        om_payload = {
            "at_ms": self.t_1020_ms,
            "attribution": "Weather data by Open-Meteo.com (CC BY 4.0)",
            "areas": [
                {
                    "area": "sakuraoka",
                    "observed_at_ms": self.t_1020_ms,
                    "fetched_at_ms": self.t_1020_ms,
                    "code": 61,
                    "temp_c": 14.2,
                    "precip_mm": 2.1,
                    "rain": True,
                    "source": "open-meteo",
                    "status": "ok",
                    "text": "下雨",
                    "hourly": [
                        {"at_ms": h10, "code": 3, "temp_c": 15.0, "precip_mm": 0.0},
                        {"at_ms": h11, "code": 3, "temp_c": 15.5, "precip_mm": 0.0},
                    ],
                }
            ],
        }

        def om_transport(method, path, query, body, headers):
            if path == "/weather":
                return 200, {}, json.dumps(om_payload)
            return self.engine.transport(method, path, query, body, headers)

        self.client._transport = om_transport
        provider = CityWeatherProvider(self.s, self.client, weather_source="open-meteo")
        adapter = CityBridgeAdapter(
            self.life,
            self.client,
            city_model=DeterministicCityModel(),
            allowed_actors=("nell",),
            weather_provider=provider,
            weather_source="open-meteo",
        )

        # Current [10:00, 11:00) window: must read live current snapshot (61, rain=True), never hourly[0] (3, False)
        live_house = provider()
        self.assertEqual(61, live_house["code"])
        self.assertTrue(live_house["rain"])
        self.assertEqual("下雨", live_house["text"])

        live_now_arg = provider(self.t_1020_ms / 1000.0)
        self.assertEqual(61, live_now_arg["code"])
        self.assertTrue(live_now_arg["rain"])

        with self.s.tx() as db:
            db.execute("DELETE FROM garments WHERE actor='nell' AND (garment_id LIKE '%umbrella%' OR label LIKE '%伞%')")
        with self.assertRaises(Refused) as cm:
            adapter.depart("nell", "rurigai:kissa-tomoshibi", carry_yen=10000, visit_id="vis_p1_om_rain")
        self.assertEqual("rain_requires_owned_umbrella", cm.exception.reason)

        # Next hour [11:00, 12:00): reads hourly[1] forecast (3, rain=False)
        next_hr = provider(h11 / 1000.0)
        self.assertEqual(3, next_hr["code"])
        self.assertFalse(next_hr["rain"])
        self.assertEqual("阴", next_hr["text"])

        # Mid-hour override expiry inside [10:00, 11:00): [10:20, 10:35) queried at 10:40 must skip expired override
        ov_short = {
            "preset": "rain",
            "code": 61,
            "temp_c": 12.0,
            "precip_mm": 2.0,
            "rain": True,
            "starts_at_ms": self.t_1020_ms,
            "expires_at_ms": _ms(2026, 11, 17, 10, 35, 0),
        }
        qk_payload = self._make_25h_payload(h10, override=ov_short)

        def qk_transport(method, path, query, body, headers):
            if path == "/weather":
                return 200, {}, json.dumps(qk_payload)
            return self.engine.transport(method, path, query, body, headers)

        self.client._transport = qk_transport
        qk_provider = CityWeatherProvider(self.s, self.client, weather_source="qingkong")
        self.assertTrue(qk_provider.sync(self.t_1020_ms, force=True))
        during_ov = qk_provider.area_snapshot("sakuraoka", now_ms=self.t_1020_ms, target_ms=_ms(2026, 11, 17, 10, 30), sync=False)
        self.assertTrue(during_ov["rain"])
        after_ov = qk_provider.area_snapshot("sakuraoka", now_ms=self.t_1020_ms, target_ms=_ms(2026, 11, 17, 10, 40), sync=False)
        self.assertFalse(after_ov["rain"])
        self.assertEqual(0, after_ov["code"])
