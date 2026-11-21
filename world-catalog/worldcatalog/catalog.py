"""Catalogue validation and pure request guards. No I/O except loading the JSON file."""
import copy
import json
import math
from pathlib import Path

REQUIRED_ACTIVITIES = {"idle", "resting", "sleeping", "eating", "cooking", "chores", "reading", "working",
                       "gaming", "hobby", "phone", "bathing", "socializing", "outing", "walking", "browsing"}
REQUIRED_LOCATIONS = {"living", "kitchen", "study", "yard", "bath", "room", "street", "station", "river", "shop", "cafe", "away"}
REQUIRED_AVAIL = {"free", "limited", "occupied", "sleeping"}
REQUIRED_P1A = {"move", "wait", "rest", "check", "cook", "eat", "say", "sleep"}
RULES = {"fixed", "arg", "instant", "route", "recipe", "activity", "external"}
PREDICATES = {"route_exists", "at_resource", "holds_or_at_resource", "private_rest_location", "edible_ready_food",
              "recipe_and_anchors_available", "activity_location_allowed", "reading_material_accessible",
              "work_resource_accessible", "invitation_accepted", "party_engine_receipt", "hobby_resource_accessible",
              "message_in_actor_mailbox", "reading_permitted", "bath_exclusive", "participants_present",
              "at_outdoor_location", "phone_executor_claim", "tool_policy_allows", "holder_empty",
              "resource_transfer_allowed", "held_by_actor", "destination_reachable", "recipient_exists",
              "scope_allows_invitation", "actor_is_recipient", "invitation_pending", "not_expired",
              "simulated_stock_and_budget", "approved_simulation_only", "verified_gift_source", "recipient_is_actor",
              "principal_admin_world", "correction_reason", "perception_policy", "activity_phase_enabled",
              "activity_prerequisites", "engine42_receipt", "seat_identity_scope", "verified_claim"}
PREDICATES.update({"game_mode_selected", "game_resource_accessible", "participant_self_opt_in"})


def number(value, positive=False):
    try:
        return type(value) in (int, float) and math.isfinite(value) and (not positive or value > 0)
    except (OverflowError, TypeError):
        return False


def load(path=None):
    with Path(path or Path(__file__).with_name("catalog.json")).open(encoding="utf-8") as stream:
        data = json.load(stream)
    errors = validate(data)
    if errors:
        raise ValueError("invalid catalogue: " + "; ".join(errors))
    return data


def _effects(errors, label, effects, body_keys):
    if not isinstance(effects, dict) or set(effects) - {"rate", "once"}:
        errors.append(label + ": invalid effects"); return
    for mode, values in effects.items():
        if not isinstance(values, dict) or set(values) - body_keys:
            errors.append(label + ": unknown body field"); continue
        if any(not number(v) for v in values.values()):
            errors.append(label + ": effect must be finite number")
    if effects.get("rate", {}).get("hunger", 0) < 0 and effects.get("once", {}).get("hunger", 0) < 0:
        errors.append(label + ": satiety cannot be settled by both rate and once")


def _predicates(errors, label, values, locations):
    if not isinstance(values, list) or any(not isinstance(x, str) for x in values):
        errors.append(label + ": invalid prerequisites"); return
    for value in values:
        if value.startswith("at_location:"):
            if value.split(":", 1)[1] not in locations:
                errors.append(label + ": unknown prerequisite location")
        elif value not in PREDICATES:
            errors.append(label + ": unknown predicate " + value)


def validate(data):
    """Return all schema/reference errors; unsupported schema versions fail closed."""
    errors = []
    if not isinstance(data, dict):
        return ["catalogue must be an object"]
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        errors.append("unsupported schema_version")
    if not isinstance(data.get("vocab_version"), str) or not data.get("vocab_version"):
        errors.append("vocab_version required")
    if data.get("units") != {"duration": "seconds", "rate": "points_per_minute_offset_to_base", "once": "points_once_on_success"}:
        errors.append("duration/effect units must be explicit")
    try:
        vocab, activities, actions, p1a, body = (data[k] for k in ("vocabulary", "activities", "actions", "p1a", "body"))
        if not all(isinstance(x, dict) for x in (vocab, activities, actions, p1a, body)):
            return errors + ["catalogue sections must be objects"]
        if set(vocab["locations"]) != REQUIRED_LOCATIONS:
            errors.append("public locations must preserve all 12 legacy keys")
        if set(vocab["activities"]) != REQUIRED_ACTIVITIES or set(activities) != REQUIRED_ACTIVITIES:
            errors.append("activities must cover all 16 legacy keys")
        if set(vocab["availability"]) != REQUIRED_AVAIL:
            errors.append("availability must preserve 4 legacy keys")
        body_keys = set(body["keys"])
        if body_keys != {"energy", "hunger"} or body["range"] != [0.0, 100.0] or not all(number(x) for x in body["range"]):
            errors.append("P1a body contract differs")
        if set(body["base_drift_per_minute"]) != body_keys or any(not number(v) for v in body["base_drift_per_minute"].values()):
            errors.append("invalid base drift")
        all_locations = REQUIRED_LOCATIONS | set(p1a["locations"])
        for key, activity in activities.items():
            prefix = "activity " + key
            if activity["label"] != vocab["activities"][key] or activity["availability"] not in REQUIRED_AVAIL:
                errors.append(prefix + ": label/availability mismatch")
            duration = activity["duration_rule"]
            vals = [duration[k] for k in ("min_seconds", "default_seconds", "max_seconds")]
            if not all(number(x, positive=True) for x in vals) or not vals[0] <= vals[1] <= vals[2]:
                errors.append(prefix + ": invalid duration bounds")
            if activity["interrupt_policy"] not in {"now", "at_end"} or activity["phase"] not in {"P1a", "P2"}:
                errors.append(prefix + ": invalid interrupt/phase")
            if not activity["action_types"] or any(x not in actions for x in activity["action_types"]):
                errors.append(prefix + ": unknown action reference")
            if activity["phase"] == "P1a":
                for action_name in activity["action_types"]:
                    linked = actions.get(action_name, {})
                    if linked.get("phase") != "P1a" or linked.get("activity") != key or linked.get("effects") != activity["effects"]:
                        errors.append(prefix + ": executable action/effect differs")
            for variant in activity.get("variants", {}).values():
                if variant["action_type"] not in actions:
                    errors.append(prefix + ": unknown variant action")
                _predicates(errors, prefix, variant["prerequisites"], all_locations)
            _effects(errors, prefix, activity["effects"], body_keys)
            _predicates(errors, prefix, activity["prerequisites"], all_locations)
        for name, action in actions.items():
            prefix = "action " + name
            for field in ("activity", "duration_rule", "prerequisites", "effects", "interrupt_policy", "resource_requirements", "body", "phase", "execution", "argument_schema"):
                if field not in action:
                    errors.append(prefix + ": missing " + field)
            rule = action["duration_rule"]
            kinds = set(rule) & RULES
            if len(kinds) != 1:
                errors.append(prefix + ": duration needs one resolver")
            if "fixed" in rule and not number(rule["fixed"], positive=True):
                errors.append(prefix + ": fixed duration must be positive")
            if "arg" in rule and (not isinstance(rule["arg"], str) or not number(rule.get("max"), positive=True)):
                errors.append(prefix + ": bad bounded duration")
            for flag in ("instant", "route", "recipe", "activity", "external"):
                if flag in rule and rule[flag] is not True:
                    errors.append(prefix + ": duration flag must be true")
            if type(action["body"]) is not bool or action["interrupt_policy"] not in {"now", "at_end"}:
                errors.append(prefix + ": invalid body/interrupt")
            if action["activity"] is not None and action["activity"] not in REQUIRED_ACTIVITIES:
                errors.append(prefix + ": unknown activity")
            if action["phase"] not in {"P1a", "P2"} or not isinstance(action["resource_requirements"], list):
                errors.append(prefix + ": invalid phase/resources")
            if any(not isinstance(x, str) or not x for x in action["resource_requirements"]):
                errors.append(prefix + ": resource expressions must be nonempty strings")
            if action["execution"] not in {"kernel", "owner_correction", "engine42", "browse_executor"}:
                errors.append(prefix + ": unknown execution authority")
            _effects(errors, prefix, action["effects"], body_keys)
            _predicates(errors, prefix, action["prerequisites"], all_locations)
        enabled = set(p1a["action_types"])
        if enabled != REQUIRED_P1A or {k for k, v in actions.items() if v["phase"] == "P1a"} != enabled:
            errors.append("P1a executable set must be exactly the 8 agreed actions")
        if p1a["internal_locations"].get("exit_rooms", {}).get("public_location", "missing") is not None:
            errors.append("leave point must not assert arrival in a real bedroom")
        edges = set()
        if set(p1a["locations"]) != {"kitchen", "living", "exit_rooms"}:
            errors.append("P1a map must preserve its two rooms and leave point")
        for route in p1a["routes"]:
            a, b = route["from"], route["to"]
            if a not in p1a["locations"] or b not in p1a["locations"] or a == b:
                errors.append("route has invalid endpoints")
            edge = tuple(sorted((a, b)))
            if edge in edges:
                errors.append("duplicate route")
            edges.add(edge)
            if not number(route["seconds"], positive=True) or route.get("bidirectional") is not True:
                errors.append("route needs positive seconds and explicit bidirectionality")
        reached = {"kitchen"}
        for _ in p1a["locations"]:
            for a, b in edges:
                if a in reached or b in reached:
                    reached.update((a, b))
        if reached != set(p1a["locations"]):
            errors.append("P1a map must be connected")
        for name, recipe in data["recipes"].items():
            if not number(recipe["seconds"], positive=True):
                errors.append("recipe " + name + ": invalid seconds")
            for field in ("consume", "makes"):
                values = recipe[field]
                if not values or any(k not in data["items"] or not number(q, positive=True) for k, q in values.items()):
                    errors.append("recipe " + name + ": invalid ingredients/yields")
            if any(data["items"].get(k, {}).get("kind") != "anchor" for k in recipe["hold"]):
                errors.append("recipe " + name + ": hold must reference anchors")
            if len(recipe["hold"]) != len(set(recipe["hold"])) or any(data["items"].get(k, {}).get("kind") != "food" for k in recipe["consume"]):
                errors.append("recipe " + name + ": duplicate anchors or nonfood ingredient")
            if any(not data["items"].get(k, {}).get("edible") for k in recipe["makes"]):
                errors.append("recipe " + name + ": output must be edible")
        for alias, target in data["aliases"].items():
            if alias in actions or target not in actions:
                errors.append("ambiguous/unknown alias")
        for actor, rows in vocab["routine"].items():
            for row in rows:
                if row["activity"] not in activities or row["location"] not in REQUIRED_LOCATIONS or row["availability"] not in REQUIRED_AVAIL:
                    errors.append("legacy routine has unknown enum: " + actor)
    except (KeyError, TypeError, ValueError, AttributeError):
        errors.append("missing or malformed nested catalogue structure")
    return errors


def public_vocabulary(data, include_version=True):
    result = copy.deepcopy(data["vocabulary"])
    if include_version:
        result["version"] = data["vocab_version"]
    return result


def guard_request(data, action_type, args, actor, resources=()):
    """Pure mandatory guard for accept/start/pre-completion hooks.

    It returns reasons and never executes a side effect. The kernel still owns identities,
    reservations, versions, receipts, scope checks and exactly-once effect settlement.
    Resources are authoritative kernel rows, NOT a model's claims about inventory.
    """
    reasons = []
    if not isinstance(action_type, str):
        return ["invalid_action_type"]
    action_type = data["aliases"].get(action_type, action_type)
    if action_type not in data["p1a"]["action_types"]:
        return ["action_not_enabled_in_P1a"]
    if not isinstance(args, dict) or not isinstance(actor, dict):
        return ["invalid_arguments_or_actor"]
    spec = data["actions"][action_type]
    allowed = {k.rstrip("?") for k in spec["argument_schema"]}
    required = {k for k in spec["argument_schema"] if not k.endswith("?")}
    if set(args) - allowed or required - set(args):
        return ["missing_or_unknown_argument"]
    if not isinstance(actor.get("actor_id"), str) or not actor["actor_id"] or not isinstance(actor.get("location"), str) or actor["location"] not in data["p1a"]["locations"]:
        return ["unknown_actor_or_location"]
    for name, kind in spec["argument_schema"].items():
        key = name.rstrip("?")
        if key in args and kind in {"location", "resource_id", "recipe_id", "edible_item", "actor_id"}:
            if not isinstance(args[key], str) or not args[key]:
                return ["invalid_argument_type:" + key]
    rule = spec["duration_rule"]
    if "arg" in rule:
        seconds = args.get(rule["arg"])
        if not number(seconds, positive=True) or seconds > rule["max"]:
            reasons.append("invalid_duration_seconds")
    if action_type == "move":
        if args.get("to") not in data["p1a"]["locations"] or args["to"] == actor["location"]:
            reasons.append("invalid_move_destination")
    if action_type == "say":
        if not isinstance(args.get("text"), str) or not 1 <= len(args["text"].strip()) <= 500:
            reasons.append("invalid_say_text")
        if "to" in args and (not isinstance(args["to"], str) or not args["to"]):
            reasons.append("invalid_addressee")
    try:
        rows = [dict(row) for row in resources]
    except (TypeError, ValueError):
        return reasons + ["invalid_authoritative_resources"]
    def reachable(row):
        return row.get("holder") == actor["actor_id"] or (row.get("holder") is None and row.get("location") == actor["location"])
    def enough(row, quantity):
        return number(row.get("qty")) and row["qty"] >= quantity
    if action_type == "check":
        rows = [r for r in rows if r.get("resource_id") == args.get("resource_id")]
        if not rows or not reachable(rows[0]):
            reasons.append("inspection_resource_not_reachable")
    elif action_type == "eat":
        item = data["items"].get(args.get("item"), {})
        if not item.get("edible") or item.get("kind") != "food":
            reasons.append("item_not_ready_food")
        candidates = [r for r in rows if (r.get("resource_id") == args["resource_id"] if "resource_id" in args else r.get("item") == args["item"])]
        if not any(r.get("item") == args["item"] and r.get("kind") == "food" and reachable(r) and enough(r, 1) for r in candidates):
            reasons.append("food_missing_mismatched_or_out_of_reach")
    elif action_type == "cook":
        recipe = data["recipes"].get(args.get("recipe"))
        if not recipe:
            reasons.append("unknown_recipe")
        else:
            if actor["location"] != "kitchen":
                reasons.append("cooking_requires_kitchen")
            for item, quantity in recipe["consume"].items():
                if not any(r.get("item") == item and r.get("kind") == "food" and reachable(r) and enough(r, quantity) for r in rows):
                    reasons.append("missing_ingredient:" + item)
            for anchor in recipe["hold"]:
                if not any(r.get("item") == anchor and r.get("kind") == "anchor" and reachable(r) and enough(r, 1) for r in rows):
                    reasons.append("missing_anchor:" + anchor)
    elif action_type == "sleep" and actor["location"] != "exit_rooms":
        reasons.append("sleep_requires_private_leave_point")
    return reasons
