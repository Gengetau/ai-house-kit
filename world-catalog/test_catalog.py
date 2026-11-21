"""W5 catalogue and guard contract checks, including bad-data counterexamples."""
import copy
import json
import unittest
from pathlib import Path
from worldcatalog.catalog import load, validate, public_vocabulary, guard_request
from worldcatalog import kernel_vocab as V


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.data = load()
        self.actor = {"actor_id": "nell", "location": "kitchen"}
        self.food = {"resource_id": "cake1", "item": "cake", "kind": "food", "location": "kitchen", "holder": None, "qty": 1}

    def invalid(self, change):
        change(self.data)
        self.assertTrue(validate(self.data))

    def test_complete_legacy_enums_and_routine_references(self):
        self.assertEqual(validate(self.data), [])
        self.assertEqual(len(self.data["activities"]), 16)
        self.assertEqual(len(self.data["vocabulary"]["locations"]), 12)
        self.assertEqual(len(self.data["vocabulary"]["routine"]), 6)
        for rows in self.data["vocabulary"]["routine"].values():
            for row in rows:
                self.assertIn(row["activity"], self.data["activities"])

    def test_generated_projection_is_current_and_excludes_internal_node(self):
        projection = json.loads(Path(__file__).with_name("world-vocab.v1.json").read_text())
        self.assertEqual(projection, public_vocabulary(self.data))
        self.assertNotIn("exit_rooms", projection["locations"])
        self.assertEqual(projection["version"], V.VOCAB_VERSION)

    def test_public_projection_returns_a_copy(self):
        projection = public_vocabulary(self.data)
        projection["activities"].clear()
        self.assertEqual(len(self.data["vocabulary"]["activities"]), 16)

    def test_unknown_schema_units_and_boolean_numbers_fail(self):
        variants = [lambda d: d.update(schema_version=2), lambda d: d.update(schema_version=True),
                    lambda d: d["units"].update(rate="per_hour"),
                    lambda d: d["recipes"]["omelette"].update(seconds=True),
                    lambda d: d["body"].update(range=[False, 100])]
        for change in variants:
            d = copy.deepcopy(self.data);change(d)
            self.assertTrue(validate(d))

    def test_invalid_duration_or_missing_effect_contract_fails(self):
        self.invalid(lambda d: d["activities"]["reading"]["duration_rule"].update(min_seconds=9999))
        self.data = load()
        self.invalid(lambda d: d["actions"]["rest"].pop("effects"))

    def test_unknown_location_and_duplicate_routes_fail(self):
        self.invalid(lambda d: d["p1a"]["routes"][0].update(to="imaginary"))
        self.data = load()
        self.invalid(lambda d: d["p1a"]["routes"].append(copy.deepcopy(d["p1a"]["routes"][0])))

    def test_unknown_predicate_and_public_key_removal_fail(self):
        self.invalid(lambda d: d["actions"]["wait"]["prerequisites"].append("typo_predicate"))
        self.data = load()
        self.invalid(lambda d: d["vocabulary"]["activities"].pop("browsing"))

    def test_double_satiety_and_nan_effects_fail(self):
        self.invalid(lambda d: d["actions"]["eat"]["effects"].update(rate={"hunger": -1}))
        self.data = load()
        self.invalid(lambda d: d["actions"]["rest"]["effects"]["rate"].update(energy=float("nan")))

    def test_recipe_references_and_quantity_fail_closed(self):
        self.invalid(lambda d: d["recipes"]["omelette"]["consume"].update(ghost=1))
        self.data = load()
        self.invalid(lambda d: d["recipes"]["rice"]["makes"].update(cooked_rice=-1))

    def test_only_eight_p1a_actions_are_exported(self):
        self.assertEqual(len(V.ACTIONS), 8)
        self.assertNotIn("buy", V.ACTIONS)
        self.assertNotIn("join_party", V.ACTIONS)
        self.assertEqual(V.validate(), [])
        self.assertEqual(V.BASE_DRIFT, {"energy": -0.05, "hunger": 0.1})

    def test_paths_and_unknown_nodes(self):
        self.assertEqual(V.route_seconds("kitchen", "exit_rooms"), 20)
        self.assertEqual(V.route_seconds("exit_rooms", "kitchen"), 20)
        self.assertEqual(V.next_hop("kitchen", "exit_rooms"), "living")
        self.assertIsNone(V.route_seconds("ghost", "ghost"))
        self.assertIsNone(V.next_hop("living", "living"))

    def test_disconnected_map_and_unknown_authority_fail(self):
        self.invalid(lambda d: d["p1a"].update(routes=[]))
        self.data = load()
        self.invalid(lambda d: d["actions"]["wait"].update(execution="model_direct_sql"))

    def test_solo_gaming_does_not_require_a_party_engine(self):
        gaming = self.data["activities"]["gaming"]
        self.assertEqual(gaming["variants"]["solo"]["execution"], "kernel")
        self.assertNotIn("invitation_accepted", gaming["variants"]["solo"]["prerequisites"])
        self.assertIn("invitation_accepted", gaming["variants"]["party"]["prerequisites"])
        self.assertEqual(gaming["variants"]["party"]["execution"], "engine42")

    def test_guard_rejects_deferred_and_bad_typed_arguments(self):
        for action, args in [("buy", {}), ("wait", {"seconds": True}), ("wait", {"seconds": float("inf")}),
                             ("wait", {"seconds": 10 ** 1000}),
                             ("wait", {"seconds": -1}), ("rest", {"seconds": 4000}), ("say", {"text": " "}),
                             ("eat", {"item": []}), ("cook", {"recipe": []}), ("move", {"to": "ghost"})]:
            with self.subTest(action=action, args=args):
                self.assertTrue(guard_request(self.data, action, args, self.actor))
        self.assertTrue(guard_request(self.data, [], {}, self.actor))

    def test_check_must_be_reachable_even_if_resource_id_is_valid(self):
        resource = dict(self.food, location="living")
        self.assertTrue(guard_request(self.data, "check", {"resource_id": "cake1"}, self.actor, [resource]))
        self.assertEqual(guard_request(self.data, "inspect", {"resource_id": "cake1"}, self.actor, [self.food]), [])

    def test_eat_rejects_nonfood_raw_food_mismatched_id_and_empty_stock(self):
        variants = [({"item": "egg"}, [dict(self.food, item="egg")]),
                    ({"item": "cake", "resource_id": "stove1"}, [dict(self.food, resource_id="stove1", kind="anchor", item="stove")]),
                    ({"item": "cake"}, [dict(self.food, qty=0)]),
                    ({"item": "cake"}, [dict(self.food, holder="wanda")])]
        for args, rows in variants:
            self.assertTrue(guard_request(self.data, "eat", args, self.actor, rows))
        self.assertEqual(guard_request(self.data, "eat", {"item": "cake"}, self.actor, [self.food]), [])

    def test_cook_needs_actual_ingredients_anchors_and_location(self):
        rows = [dict(self.food, resource_id="eggs", item="egg", qty=2)]
        self.assertTrue(guard_request(self.data, "cook", {"recipe": "omelette"}, self.actor, rows))
        rows += [dict(self.food, resource_id=k, item=k, kind="anchor") for k in ("stove", "pan")]
        self.assertEqual(guard_request(self.data, "cook", {"recipe": "omelette"}, self.actor, rows), [])
        self.assertTrue(guard_request(self.data, "cook", {"recipe": "omelette"}, dict(self.actor, location="living"), rows))

    def test_sleep_and_unknown_argument_cannot_bypass_guard(self):
        self.assertTrue(guard_request(self.data, "sleep", {"seconds": 60}, self.actor))
        self.assertEqual(guard_request(self.data, "sleep", {"seconds": 60}, dict(self.actor, location="exit_rooms")), [])
        self.assertTrue(guard_request(self.data, "say", {"text": "好", "owner": True}, self.actor))


if __name__ == "__main__":
    unittest.main()
