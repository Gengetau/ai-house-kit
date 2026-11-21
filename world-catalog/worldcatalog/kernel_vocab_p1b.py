"""P1b shadow profile of the catalog: the same vocabulary object shape as kernel_vocab, on a bigger house.

catalog.json stays the single source of actions, activities (durations, effects, interrupt policies) and
recipes; p1b.json only says which of them are switched on and where: the five shared rooms, a bedroom of her
own for each housemate (room_<id>, shown to the game as "room"), "away" as the leave point for going out,
begin_activity for eight everyday activities, and three ready-to-eat foods. kernel_vocab (p1a) is untouched,
so the P1a acceptance tests, recordings and cases run exactly as before.

An activity that names a catalog variant (gaming: solo) also takes that variant's resource_requirements as
anchors to hold (solo gaming holds the game_console), so the variant's resource rule is enforced, not metadata.

Use it by handing the module to the kernel: Kernel(store, roster, vocab=worldkernel.vocab_p1b).
"""
import copy
import heapq
import json
from pathlib import Path

from .catalog import guard_request, load

BASE = load()
PROFILE = json.loads(Path(__file__).with_name("p1b.json").read_text(encoding="utf-8"))

VOCAB_VERSION = PROFILE["vocab_version"]
BEDROOM = "room_"


def bed(actor_id):
    """Where she sleeps: her own bedroom."""
    return BEDROOM + actor_id


def owner_of(location):
    """The housemate whose bedroom this is, or None for every other place."""
    return location[len(BEDROOM):] if isinstance(location, str) and location.startswith(BEDROOM) else None


def _locations():
    out = {k: {"kind": "room", "public_location": k} for k in PROFILE["rooms"]}
    for a in PROFILE["bedrooms"]:
        out[bed(a)] = {"kind": "room", "public_location": "room", "owner": a}
    for k, v in PROFILE["leave_points"].items():
        out[k] = dict(v, kind="leave_point")
    return out


def _routes():
    out = {}
    for e in PROFILE["routes"]:
        ends = [bed(a) for a in PROFILE["bedrooms"]] if e["to"] == "bedrooms" else [e["to"]]
        for b in ends:
            out[(e["from"], b)] = e["seconds"]
    return out


def _actions():
    out = {}
    for k in PROFILE["action_types"]:
        spec = copy.deepcopy(BASE["actions"][k])
        spec.update(copy.deepcopy(PROFILE["action_overrides"].get(k, {})))
        out[k] = spec
    return out


def _activities():
    out = {}
    for k, p in PROFILE["activities"].items():
        a = BASE["activities"][k]
        d = a["duration_rule"]
        hold = list(p.get("hold", ()))
        if p.get("variant"):
            hold += [r for r in a["variants"][p["variant"]]["resource_requirements"] if r not in hold]
        out[k] = {"label": a["label"], "min": d["min_seconds"], "default": d["default_seconds"],
                  "max": d["max_seconds"], "effects": copy.deepcopy(a["effects"]),
                  "interrupt_policy": a["interrupt_policy"], "availability": a["availability"],
                  "where": tuple(p["where"]), "hold": tuple(hold), "variant": p.get("variant")}
    return out


CATALOG = copy.deepcopy(BASE)
CATALOG["vocab_version"] = VOCAB_VERSION
CATALOG["items"].update(copy.deepcopy(PROFILE["items"]))
LOCATIONS = _locations()
ROUTES = _routes()
ACTIONS = _actions()
ACTIVITIES = _activities()
RECIPES = copy.deepcopy(BASE["recipes"])
BODY_KEYS = tuple(BASE["body"]["keys"])
BODY_RANGE = tuple(BASE["body"]["range"])
BASE_DRIFT = dict(BASE["body"]["base_drift_per_minute"])

# the shape guard_request reads, with this profile in the place of p1a
_VIEW = dict(CATALOG, actions=ACTIONS, p1a={"action_types": list(ACTIONS), "locations": LOCATIONS})


def public_location(location):
    """The public (game/legacy) key of a place: room_<id> -> room, away -> away."""
    return LOCATIONS.get(location, {}).get("public_location")


def activity_allowed_at(activity, actor_id, location):
    where = ACTIVITIES[activity]["where"]
    return location in where or ("own_room" in where and location == bed(actor_id))


def route_seconds(a, b):
    if a not in LOCATIONS or b not in LOCATIONS:
        return None
    best, queue = {a: 0}, [(0, a)]
    while queue:
        cost, node = heapq.heappop(queue)
        if cost != best[node]:
            continue
        if node == b:
            return cost
        for (x, y), seconds in ROUTES.items():
            nxt = y if node == x else x if node == y else None
            if nxt is not None and cost + seconds < best.get(nxt, float("inf")):
                best[nxt] = cost + seconds
                heapq.heappush(queue, (cost + seconds, nxt))
    return None


def next_hop(a, b):
    if a == b or route_seconds(a, b) is None:
        return None
    choices = []
    for (x, y), seconds in ROUTES.items():
        nxt = y if a == x else x if a == y else None
        rest = route_seconds(nxt, b) if nxt else None
        if rest is not None:
            choices.append((seconds + rest, nxt))
    return min(choices)[1] if choices else None


def validate():
    """Profile errors (the base catalogue is validated by load())."""
    errors = []
    if not set(BASE["p1a"]["action_types"]) <= set(PROFILE["action_types"]):
        errors.append("p1b must keep every P1a action")
    for k in PROFILE["action_types"]:
        if k not in BASE["actions"]:
            errors.append("unknown action " + k)
    for k, p in PROFILE["activities"].items():
        if k not in BASE["activities"]:
            errors.append("unknown activity " + k)
            continue
        if p.get("variant"):
            v = BASE["activities"][k].get("variants", {}).get(p["variant"])
            if not v or v["action_type"] != "begin_activity" or v.get("execution") != "kernel":
                errors.append(f"activity {k}: variant {p['variant']} is not a kernel begin_activity variant")
        for w in p["where"]:
            if w != "own_room" and w not in LOCATIONS:
                errors.append(f"activity {k}: unknown place {w}")
    for k, a in ACTIVITIES.items():
        for h in a["hold"]:
            if CATALOG["items"].get(h, {}).get("kind") != "anchor":
                errors.append(f"activity {k}: hold must be an anchor")
    for k in PROFILE["items"]:
        if k in BASE["items"]:
            errors.append("item already in the base catalogue: " + k)
    public = set(BASE["vocabulary"]["locations"])
    for k, v in LOCATIONS.items():
        if v.get("public_location") not in public:
            errors.append(f"place {k} has no public location")
    for (a, b), s in ROUTES.items():
        if a not in LOCATIONS or b not in LOCATIONS or not s > 0:
            errors.append(f"bad route {a}-{b}")
    if any(route_seconds("living", x) is None for x in LOCATIONS):
        errors.append("p1b map must be connected")
    return errors


def guard(action_type, args, actor, resources=()):
    """The catalogue guard on this profile, plus the three rules the bigger house adds: nobody walks into
    another housemate's bedroom, she sleeps in her own, and an activity runs only where it may."""
    action_type = CATALOG["aliases"].get(action_type, action_type)
    if action_type == "begin_activity" and (not isinstance(args, dict) or not isinstance(args.get("activity"), str)):
        return ["invalid_argument_type:activity"]  # a list or an object from a model is a refusal, not a crash
    reasons = [r for r in guard_request(_VIEW, action_type, args, actor, resources)
               if r != "sleep_requires_private_leave_point"]
    if reasons and reasons[0] in ("invalid_action_type", "action_not_enabled_in_P1a", "invalid_arguments_or_actor",
                                  "missing_or_unknown_argument", "unknown_actor_or_location"):
        return reasons
    here, me = actor["location"], actor["actor_id"]
    if action_type == "move" and owner_of(args.get("to")) not in (None, me):
        reasons.append("someone_elses_bedroom")
    if action_type == "sleep" and here != bed(me):
        reasons.append("sleep_requires_own_bedroom")
    if action_type == "begin_activity":
        act = args.get("activity")
        if act not in ACTIVITIES:
            return reasons + ["activity_not_enabled_in_p1b"]
        spec = ACTIVITIES[act]
        if not activity_allowed_at(act, me, here):
            reasons.append("activity_not_allowed_here")
        sec = args.get("seconds", spec["default"])
        if type(sec) not in (int, float) or not spec["min"] <= sec <= spec["max"]:
            reasons.append("invalid_duration_seconds")
        rows = [dict(r) for r in resources]
        for anchor in spec["hold"]:
            if not any(r.get("item") == anchor and r.get("kind") == "anchor" and r.get("holder") is None
                       and r.get("location") == here for r in rows):
                reasons.append("missing_anchor:" + anchor)
    return reasons
