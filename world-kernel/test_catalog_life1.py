"""the life1 catalog converted from v2 loads, every id reference resolves, and the kernel can live in it.

All data is the committed catalog; worlds are temporary synthetic stores (no live db, no model)."""
import copy
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from worldkernel import vocab_life1 as V
from worldkernel import legacy
from worldkernel.life import Life, ROSTER, STOCK, PASS_TIME
from worldkernel.store import Store, VirtualClock
from worldkernel.wallet import JST
from worldcatalog import kernel_vocab_life1 as KV

LEGACY_ACTIVITIES = set(json.load(open(os.path.join(os.path.dirname(KV.__file__), "..", "world-vocab.v1.json"),
                                        encoding="utf-8"))["activities"])


class Loads(unittest.TestCase):
    def test_validate_is_clean(self):
        self.assertEqual(V.validate(), [])

    def test_counts_match_106_v2(self):
        # 34 from v2 plus the snack and drink kinds added in life1/snacks.json
        self.assertEqual(len(V.INGREDIENTS) - len(KV.EXTRA_SNACKS), 34)
        self.assertEqual(len(KV.EXTRA_SNACKS), 10)  # ready_steak is distinct from the steak recipe
        self.assertEqual(len(KV.RECIPE_DATA), 33)
        self.assertEqual(len(KV.ACTIVITY_DATA), 36)  # 39 rows minus cooking/sleeping (recipes and sleep decide those); 11-20 白帆市 hotfix dropped movie_theater (no cinema in the city)
        self.assertEqual(len(V.PLACES), 25)
        self.assertEqual(len(V.MENU), 14)  # 11-20: five real-city menus out, three 白帆市 ones in
        self.assertEqual(sorted(V.PEOPLE), sorted(ROSTER))
        for a, p in V.PEOPLE.items():
            self.assertEqual(len(p["wardrobe"]), 17, a)

    def test_every_entry_is_marked_existing_or_new(self):
        tables = [V.INGREDIENTS, KV.RECIPE_DATA, KV.ACTIVITY_DATA, V.PLACES]
        for t in tables:
            for k, v in t.items():
                self.assertIn(v.get("source"), ("已有", "新设"), k)
        for m in V.MENU.values():
            self.assertEqual(m["source"], "新设")
        for a, p in V.PEOPLE.items():
            self.assertNotIn("status", p)
            for w in p["wardrobe"]:
                self.assertIn(w["source"], ("已有", "新设"), w["id"])
                self.assertNotIn("placeholder", w)
        self.assertIn("example world", KV.MANIFEST["status"])

    def test_house_stock_and_kernel_ids_are_catalog_ids(self):
        self.assertEqual(set(STOCK) - set(V.INGREDIENTS), set())
        self.assertIn(PASS_TIME, V.ACTIVITIES)
        self.assertTrue(set(legacy.ACT_LEGACY.values()) <= LEGACY_ACTIVITIES)
        for k in V.ACTIVITIES:
            self.assertIn(legacy.ACT_LEGACY.get(k, "hobby"), LEGACY_ACTIVITIES, k)
        for pid in legacy.PLACE_LEGACY:
            self.assertIn(pid, V.PLACES)

    def test_every_meal_slot_has_a_main_dish_from_the_starting_stock(self):
        for slot in ("breakfast", "lunch", "dinner"):
            ok = [r for r, rec in V.RECIPES.items() if rec["main"] and slot in rec["meal"]
                  and all(STOCK.get(i, 0) >= q for i, q in rec["consume"].items())]
            self.assertTrue(ok, slot)
            self.assertTrue(set(ok) & set(V.PEOPLE["nell"]["cooking"]["good_at"]), slot)  # the head cook has one

    def test_travel_and_prices_are_exact(self):
        for k, p in V.PLACES.items():
            t = p["travel"]
            self.assertIsInstance(t["price_yen"], int, k)
            if t["mode"] in ("train", "bus"):  # walking and cycling are free
                self.assertGreater(t["price_yen"], 0, k)
            for act in p["activities"]:
                self.assertIsInstance(p["activity_costs_yen"][act], int, (k, act))
        self.assertEqual(V.VOCAB_VERSION, "life1-0.5")
        self.assertEqual(KV.MANIFEST["location_origin"], "樱丘一带（樱丘站与星川之间的虚构旧洋馆）")
        self.assertEqual(V.PLACES["kissa_tomoshibi"]["travel"]["segments"][1], {"mode": "train", "minutes": 13, "price_yen": 200})
        self.assertEqual(V.PLACES["sento"]["weekdays"], [0, 1, 2, 4, 5, 6])  # 周五至周三: closed Thursday
        self.assertEqual(V.ROUTES[("away", "place:nikko")], 160 * 60)
        # 11-20 hotfix: the "city" tier is 白帆市 (engine route from the house: walk 12 to 樱丘站, train, walk)
        self.assertEqual(V.PLACES["muginohi"]["travel"],
                         {"mode": "train", "minutes": 36, "price_yen": 260,
                          "segments": [{"mode": "walk", "minutes": 12, "price_yen": 0},
                                       {"mode": "train", "minutes": 19, "price_yen": 260},
                                       {"mode": "walk", "minutes": 5, "price_yen": 0}]})
        self.assertEqual(V.PLACES["shiorido"]["travel"]["minutes"], 30)
        self.assertEqual(V.PLACES["kamitsuki"]["travel"]["price_yen"], 260)
        self.assertEqual(V.PLACES["yokohama"]["travel"], {"mode": "train", "minutes": 55, "price_yen": 810})
        expected_fares = {
            "kissa_tomoshibi": 200, "shiorido": 200, "minami_park": 200, "muginohi": 260,
            "southern_terrace": 260, "kamitsuki": 260, "takaosan": 690, "kamakura": 1230,
            "enoshima": 1400, "yokohama": 810, "hakone": 2150, "atami": 2090, "nikko": 2750,
        }
        for pid, yen in expected_fares.items():
            self.assertEqual(V.PLACES[pid]["travel"]["price_yen"], yen, pid)
        self.assertEqual(V.PLACES["riverside"]["label"], "星川河岸")
        for k, p in V.PLACES.items():
            if p["tier"] in ("near", "town"):
                desc = p.get("description", "")
                label = p.get("label", "")
                self.assertTrue(desc, k)
                self.assertFalse(any(bad in desc for bad in ("丁目", "番地", "号")), k)
                self.assertFalse(any(bad in (label + desc) for bad in ("旧町", "旧川", "某区")), k)
        expected_distant_labels = {
            "kissa_tomoshibi": "琉璃街·喫茶「帆」", "shiorido": "琉璃街·书店「书签堂」",
            "minami_park": "琉璃街·南琉璃公园", "muginohi": "风港·面包店「麦穗」",
            "southern_terrace": "风港·南口露台步道", "kamitsuki": "风港·文具店「纸鸢」", "takaosan": "高尾山", "kamakura": "镰仓",
            "enoshima": "江之岛", "yokohama": "横滨", "hakone": "箱根", "atami": "热海", "nikko": "日光",
        }
        for pid, expected_label in expected_distant_labels.items():
            self.assertEqual(V.PLACES[pid]["label"], expected_label, pid)
        self.assertTrue(V.LOCATIONS["place:hakone"]["lodging"])
        self.assertFalse(V.LOCATIONS["place:kamakura"]["lodging"])

    def test_activity_where_includes_every_place_that_offers_it(self):
        for pid, p in V.PLACES.items():
            for act in p["activities"]:
                self.assertTrue(V.activity_allowed_at(act, "nell", "place:" + pid), (pid, act))
        self.assertTrue(V.activity_allowed_at("reading", "elsa", "room_elsa"))
        self.assertFalse(V.activity_allowed_at("gaming", "mina", "room_mina"))  # D1: the console is in the living room


class CatchesBrokenContent(unittest.TestCase):
    """validate() is not vacuous: each broken reference below is reported."""

    def broken(self, name, edit):
        data = copy.deepcopy(getattr(KV, name))
        edit(data)
        with mock.patch.object(KV, name, data):
            return KV.validate()

    def test_broken_references_are_reported(self):
        cases = {
            "taste": ("PEOPLE", lambda d: d["nell"]["taste"]["likes"].append("no_such_food")),
            "prefer": ("PEOPLE", lambda d: d["mina"]["activities"]["prefer"].update(no_such_activity=3)),
            "regular place": ("PEOPLE", lambda d: d["elsa"]["schedule"]["regular"][0].update(where="place:nowhere")),
            "regular time": ("PEOPLE", lambda d: d["elsa"]["schedule"]["regular"][0].update(at="9:00-12:00")),
            "occasion": ("PEOPLE", lambda d: d["wanda"]["wardrobe"][0]["occasions"].append("party")),
            "temp": ("PEOPLE", lambda d: d["wanda"]["wardrobe"][0].update(temp_c=None)),
            "cannot": ("PEOPLE", lambda d: d["alma"]["cooking"]["cannot"].append("no_such_recipe")),
            "weekday": ("PLACES", lambda d: d["bakery"].update(weekdays=[1, 7])),
            "open": ("PLACES", lambda d: d["bakery"].update(open="8:00-19:00")),
            "price": ("PLACES", lambda d: d["kissa_tomoshibi"]["menu"][0].update(price_yen="550")),
            "segments": ("PLACES", lambda d: d["kissa_tomoshibi"]["travel"].update(minutes=32)),
            "segments price": ("PLACES", lambda d: d["kissa_tomoshibi"]["travel"].update(price_yen=210)),
            "cost": ("PLACES", lambda d: d["sento"]["activity_costs_yen"].pop("onsen")),
            "sells": ("PLACES", lambda d: d["konbini"]["sells"].append("no_such_item")),
            "lodging": ("PLACES", lambda d: d["atami"].pop("lodging_yen")),
            "shelf": ("INGREDIENTS", lambda d: d["fish"].update(shelf_days=0)),
            "sold at": ("INGREDIENTS", lambda d: d["fish"]["where_to_buy"].append("nowhere")),
            "difficulty": ("RECIPE_DATA", lambda d: d["tempura"].update(difficulty=4)),
            "party": ("ACTIVITY_DATA", lambda d: d["tea_party"].update(party={"min": 5, "max": 4, "invite": True})),
            "hours order": ("ACTIVITY_DATA", lambda d: d["stargazing"].update(hours=["23:59", "19:00"])),
            "where not offered": ("PLACES", lambda d: d["supermarket"]["activities"].remove("shopping_together")
                                  or d["supermarket"]["activity_costs_yen"].pop("shopping_together")),
            "recipe is an ingredient": ("RECIPE_DATA", lambda d: d.update(fish=dict(d["tempura"]))),
        }
        for what, (name, edit) in cases.items():
            with self.subTest(what):
                self.assertNotEqual(self.broken(name, edit), [])

    def test_times_are_times_of_day(self):
        """24:01-24:59 and 24:00 as a start or a clock time are refused; 24:00 ends a span."""
        bad = {
            "open ends 24:59": ("PLACES", lambda d: d["bakery"].update(open="08:00-24:59")),
            "open ends 24:01": ("PLACES", lambda d: d["bakery"].update(open="08:00-24:01")),
            "open starts 24:00": ("PLACES", lambda d: d["bakery"].update(open="24:00-02:00")),
            "open 25:00": ("PLACES", lambda d: d["bakery"].update(open="08:00-25:00")),
            "open empty span": ("PLACES", lambda d: d["bakery"].update(open="08:00-08:00")),
            "activity hours 24:59": ("ACTIVITY_DATA", lambda d: d["tea_time"].update(hours=["14:00", "24:59"])),
            "activity starts 24:00": ("ACTIVITY_DATA", lambda d: d["tea_time"].update(hours=["24:00", "24:00"])),
            "activity hours not a list": ("ACTIVITY_DATA", lambda d: d["tea_time"].update(hours="14:00-21:30")),
            "wake 24:59": ("PEOPLE", lambda d: d["nell"]["schedule"]["weekday"].update(wake="24:59")),
            "wake 24:00": ("PEOPLE", lambda d: d["nell"]["schedule"]["weekday"].update(wake="24:00")),
            "bedtime 24:30": ("PEOPLE", lambda d: d["mina"]["schedule"]["weekend"].update(bedtime="24:30")),
            "regular 24:59": ("PEOPLE", lambda d: d["elsa"]["schedule"]["regular"][0].update(at="23:00-24:59")),
            "weekday 1.0": ("PLACES", lambda d: d["bakery"].update(weekdays=[1.0])),
            "weekday True": ("PLACES", lambda d: d["bakery"].update(weekdays=[True, 2])),
            "weekday twice": ("PLACES", lambda d: d["bakery"].update(weekdays=[1, 1])),
            "weekday empty": ("PLACES", lambda d: d["bakery"].update(weekdays=[])),
        }
        for what, (name, edit) in bad.items():
            with self.subTest(what):
                self.assertNotEqual(self.broken(name, edit), [])
        good = {
            "open to midnight": ("PLACES", lambda d: d["bakery"].update(open="08:00-24:00")),
            "open all day": ("PLACES", lambda d: d["bakery"].update(open="00:00-24:00")),
            "open two spans": ("PLACES", lambda d: d["bakery"].update(open="07:00-11:00,17:00-23:59")),
            "open past midnight": ("PLACES", lambda d: d["bakery"].update(open="18:00-02:00")),
            "regular past midnight": ("PEOPLE", lambda d: d["elsa"]["schedule"]["regular"][0].update(at="23:00-02:00")),
            "regular to midnight": ("PEOPLE", lambda d: d["elsa"]["schedule"]["regular"][0].update(at="23:30-00:00")),
            "activity to 24:00": ("ACTIVITY_DATA", lambda d: d["stargazing"].update(hours=["19:00", "24:00"])),
            "wake 00:00 bedtime 23:59": ("PEOPLE", lambda d: d["nell"]["schedule"]["weekday"].update(
                wake="00:00", bedtime="23:59")),
            "every weekday": ("PLACES", lambda d: d["bakery"].update(weekdays=[0, 1, 2, 3, 4, 5, 6])),
        }
        for what, (name, edit) in good.items():
            with self.subTest(what):
                self.assertEqual(self.broken(name, edit), [])

    def test_a_single_piece_is_not_a_whole_outfit(self):
        """a garment wardrobe.py may wear alone must not be trousers or a jacket by its own label."""
        cases = {
            "joggers as loungewear": ("PEOPLE", lambda d: d["mina"]["wardrobe"][10].update(category="loungewear")),
            "jacket as loungewear": ("PEOPLE", lambda d: d["yuki"]["wardrobe"][6].update(category="loungewear")),
            "trousers as sportswear": ("PEOPLE", lambda d: d["mina"]["wardrobe"][5].update(category="sportswear")),
            "unknown category": ("PEOPLE", lambda d: d["nell"]["wardrobe"][0].update(category="cape")),
        }
        self.assertEqual(KV.PEOPLE["mina"]["wardrobe"][10]["id"], "mina_joggers")
        self.assertEqual(KV.PEOPLE["yuki"]["wardrobe"][6]["id"], "yuki_fleece_lounge")
        self.assertEqual(KV.PEOPLE["mina"]["wardrobe"][5]["id"], "mina_cargo_pants")
        for what, (name, edit) in cases.items():
            with self.subTest(what):
                self.assertNotEqual(self.broken(name, edit), [])


class Dresses(unittest.TestCase):
    """Every one of the six can be dressed by wardrobe.py for the three outfits, in any weather."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        clock = VirtualClock(datetime(2026, 11, 5, 9, 0, tzinfo=JST).timestamp())
        self.s = Store(os.path.join(self.temp.name, "k.db"), clock, world_id="test/catalog-life1", synthetic=True,
                       holder="test")
        self.s.acquire()
        self.addCleanup(self.s.close)
        self.life = Life(self.s, model=None, weather=lambda _: {"temp_c": 15, "rain": False})
        self.clock = clock

    def test_sleep_home_out_in_cold_mild_hot_and_rain(self):
        w = self.life.wardrobe
        mine = {a: {g["garment_id"]: g for g in w.owned(a)} for a in ROSTER}
        for a in ROSTER:
            for temp, rain in ((2, False), (15, True), (30, False)):
                for occasion in ("sleep", "home", "out"):
                    with self.subTest(actor=a, occasion=occasion, temp=temp):
                        items = w.pick(a, occasion, temp, rain)
                        self.assertTrue(items)
                        self.assertTrue(all(i in mine[a] for i in items))
                        cats = [mine[a][i]["category"] for i in items]
                        if occasion == "sleep":
                            self.assertIn("sleepwear", cats)
                        if occasion == "out":
                            self.assertIn("shoes", cats)
                        if occasion == "out" and (temp < 15 or rain):
                            self.assertIn("outer", cats)
                        self.clock.advance(60)
                        got = self.life.k.submit({"idempotency_key": f"t:{a}:{occasion}:{temp}", "principal": "routine",
                                                  "actor_id": a, "origin": "routine", "action_type": "change_clothes",
                                                  "args": {"items": items, "occasion": occasion}})
                        self.assertEqual(got["status"], "succeeded", got)
                        self.assertEqual(w.current(a)["items"], items)

    def test_sport_and_onsen_have_a_covering_outfit(self):
        for a in ROSTER:
            for occasion, temp in (("sport", 28), ("onsen", 25)):
                with self.subTest(actor=a, occasion=occasion):
                    self.assertTrue(self.life.wardrobe.pick(a, occasion, temp))

    def dirty(self, *ids):
        with self.s.tx() as db:
            db.executemany("UPDATE garments SET state='dirty' WHERE garment_id=?", [(i,) for i in ids])

    def change(self, actor, items, key):
        self.clock.advance(60)
        return self.life.k.submit({"idempotency_key": key, "principal": "routine", "actor_id": actor,
                                   "origin": "routine", "action_type": "change_clothes",
                                   "args": {"items": items, "occasion": "home"}})

    def test_mina_with_her_skirt_dirty_wears_a_top_with_the_joggers(self):
        """the joggers are trousers; without the skirt she wears them under a top, not alone."""
        self.dirty("mina_skirt_set")
        items = self.life.wardrobe.pick("mina", "home", 15)
        self.assertEqual(items[:2], ["mina_hoodie", "mina_joggers"])
        self.assertEqual(self.change("mina", items, "t:mina")["status"], "succeeded")
        got = self.change("mina", ["mina_joggers", "mina_headphones"], "t:mina:alone")
        self.assertEqual((got["status"], got["reason"]), ("failed", "not_dressed"))

    def test_yuki_with_her_dress_dirty_has_no_home_outfit_rather_than_a_jacket(self):
        """her fleece is a jacket; with the star dress dirty she has no full home outfit and says so."""
        self.dirty("yuki_dot_dress")
        self.assertIsNone(self.life.wardrobe.pick("yuki", "home", 15))
        got = self.change("yuki", ["yuki_fleece_lounge", "yuki_cherry_clip"], "t:yuki")
        self.assertEqual((got["status"], got["reason"]), ("failed", "not_dressed"))
        self.assertIsNone(self.life.wardrobe.current("yuki"))

    def test_dirtying_her_home_clothes_never_leaves_her_half_dressed(self):
        """As her chosen home clothes get dirty one outfit after another, every pick is a real outfit (accepted by
        change_clothes, whole-body pieces are whole-body by label) until she has none left (None)."""
        w = self.life.wardrobe
        for a in ROSTER:
            mine = {g["garment_id"]: g for g in w.owned(a)}
            for n in range(20):
                items = w.pick(a, "home", 15)
                if items is None:
                    break
                with self.subTest(actor=a, round=n, items=items):
                    for i in items:
                        self.assertFalse(KV.single_piece_as_whole_body(mine[i]), mine[i]["label"])
                    self.assertEqual(self.change(a, items, f"t:{a}:{n}")["status"], "succeeded")
                self.dirty(*[i for i in items if mine[i]["category"] != "accessory"])
            else:
                self.fail(f"{a}: never ran out of clean home clothes")


if __name__ == "__main__":
    unittest.main()
