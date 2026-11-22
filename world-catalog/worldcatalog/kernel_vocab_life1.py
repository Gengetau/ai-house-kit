"""life1: the full-life profile.

Same vocabulary object shape as kernel_vocab_p1b, built from worldcatalog/life1/*.json instead of p1b.json:
the house (five shared rooms, a bedroom each, the street outside the door as `away`), every place you can go
(`place:<id>`, reached from `away` with its real travel time), activities at home and at places, ~30 recipes
whose output is the dish itself, ingredients with prices and shelf life, menus of places, and per-person data
(wardrobe, cooking, taste, preferences, schedule) in PEOPLE.

The guard is self-contained (no P1a phase gate): life1 enables its own action set. change_clothes, buy, invite
and respond_invite are instant actions whose handlers live in world-kernel modules (kernel.instant).
"""
import copy
import os
import heapq
import json
import re
from pathlib import Path

from .catalog import load

BASE = load()
DIR = Path(__file__).with_name("life1")


def _read(name):
    return json.loads((DIR / f"{name}.json").read_text(encoding="utf-8"))


MANIFEST = _read("manifest")
INGREDIENTS = _read("ingredients")
# snack and drink kinds the owner's gifts and her own shopping need (chips/chocolate/cookie were already there);
# kept apart from the generated ingredients.json so a re-run of life1_from_106.py does not drop them
EXTRA_SNACKS = _read("snacks")
assert not set(EXTRA_SNACKS) & set(INGREDIENTS), sorted(set(EXTRA_SNACKS) & set(INGREDIENTS))
INGREDIENTS.update(EXTRA_SNACKS)
RECIPE_DATA = _read("recipes")
ACTIVITY_DATA = _read("activities")
# an activity that eats one thing from the stock, of these item classes. She names it (args
# "snack": its resource id); it is held while she eats and taken from the stock when the activity ends. Kept apart
# from the generated activities.json, like EXTRA_SNACKS, so a re-run of life1_from_106.py does not drop it.
EATS = {"eating_snack": ("snack", "drink", "dish")}
assert set(EATS) <= set(ACTIVITY_DATA), sorted(set(EATS) - set(ACTIVITY_DATA))
PLACES = _read("places")
CLOTHING = _read("clothing")
RENDER = _read("render")
GARMENT_ASSETS = _read("garment_assets") if (DIR / "garment_assets.json").exists() else {"shelf": "未分类", "assets": {}}
PEOPLE = {a: json.loads((DIR / "people" / f"{a}.json").read_text(encoding="utf-8")) for a in MANIFEST["people"]}
HOUSE = MANIFEST["house"]
MONEY = MANIFEST["money"]

VOCAB_VERSION = MANIFEST["vocab_version"]
BEDROOM = "room_"
PLACE = "place:"


def bed(actor_id):
    return BEDROOM + actor_id


def owner_of(location):
    return location[len(BEDROOM):] if isinstance(location, str) and location.startswith(BEDROOM) else None


def place_id(location):
    """'place:cafe' -> 'cafe'; anything else -> None."""
    return location[len(PLACE):] if isinstance(location, str) and location.startswith(PLACE) else None


def _locations():
    out = {k: {"kind": "room", "public_location": k} for k in HOUSE["rooms"]}
    for a in MANIFEST["people"]:
        out[bed(a)] = {"kind": "room", "public_location": "room", "owner": a}
    out["away"] = {"kind": "leave_point", "public_location": "away", "presentation": "street outside the house"}
    for k, p in PLACES.items():
        out[PLACE + k] = {"kind": "place", "public_location": "away", "label": p["label"], "tier": p["tier"],
                          "lodging": bool(p.get("lodging_yen") is not None and p.get("nights"))}
    return out


def _routes():
    out = {}
    for e in HOUSE["routes"]:
        ends = [bed(a) for a in MANIFEST["people"]] if e["to"] == "bedrooms" else [e["to"]]
        for b in ends:
            out[(e["from"], b)] = e["seconds"]
    for k, p in PLACES.items():
        out[("away", PLACE + k)] = int(p["travel"]["minutes"]) * 60
    return out


INSTANT = ("change_clothes", "buy", "invite", "respond_invite", "pick_shelf_garment")
_ARGS = {
    "move": {"to": "location"}, "wait": {"seconds": "seconds"}, "rest": {"seconds": "seconds"},
    "check": {"resource_id": "str"}, "cook": {"recipe": "str"}, "eat": {"item": "str", "resource_id?": "str"},
    "say": {"text": "str", "to?": "str"}, "sleep": {"seconds": "seconds"},
    "begin_activity": {"activity": "str", "seconds?": "seconds", "party?": "str", "snack?": "str"},
}


def _actions():
    out = {}
    for k in ("move", "wait", "rest", "check", "cook", "eat", "say", "sleep"):
        out[k] = copy.deepcopy(BASE["actions"][k])
    out["sleep"]["prerequisites"] = []  # own bedroom or a lodging place: the guard decides
    out["begin_activity"] = dict(copy.deepcopy(BASE["actions"]["begin_activity"]), duration_rule={"activity": True},
                                 prerequisites=[], phase="life1")
    for k in INSTANT:
        out[k] = {"activity": None, "duration_rule": {"instant": True}, "prerequisites": [], "effects": {},
                  "interrupt_policy": "now", "resource_requirements": [], "body": False, "phase": "life1",
                  "execution": "kernel", "argument_schema": {"handler_defined": "life1"}}
    for k, spec in out.items():
        spec["phase"] = "life1"
    return out


def _activities():
    out = {}
    for k, a in ACTIVITY_DATA.items():
        lo, hi = a["minutes"]
        out[k] = {"label": a["label"], "min": lo * 60, "default": round((lo + hi) / 2) * 60, "max": hi * 60,
                  "effects": {"rate": dict(a.get("effects", {}))}, "interrupt_policy": "now",
                  "availability": "busy" if k in ("bathing", "movie_theater", "onsen") else "free",
                  "where": tuple(a["where"]), "hold": tuple(a.get("hold", ())), "party": a.get("party"),
                  "hours": tuple(a["hours"]) if a.get("hours") else None, "eats": tuple(EATS.get(k, ()))}
    return out


def _recipes():
    return {k: {"label": r["label"], "seconds": int(r["minutes"]) * 60, "consume": dict(r["consume"]),
                "hold": list(r["hold"]), "makes": {k: int(r["makes"]["servings"])}, "main": bool(r["main"]),
                "difficulty": r.get("difficulty", 1), "meal": list(r.get("meal", ())), "tags": list(r.get("tags", ()))}
            for k, r in RECIPE_DATA.items()}


def _menu():
    out = {}
    for pid, p in PLACES.items():
        for m in p.get("menu", ()):
            out[m["id"]] = dict(m, place=pid)
    return out


LOCATIONS = _locations()
ROUTES = _routes()
ACTIONS = _actions()
ACTIVITIES = _activities()
RECIPES = _recipes()
MENU = _menu()

CATALOG = copy.deepcopy(BASE)
CATALOG["vocab_version"] = VOCAB_VERSION
CATALOG["items"] = {}
for k, v in INGREDIENTS.items():
    CATALOG["items"][k] = {"kind": "food", "edible": bool(v.get("edible")), "label": v["label"], "class": v["kind"],
                           "price_yen": v["price_yen"], "shelf_days": v.get("shelf_days")}
for k, r in RECIPES.items():
    CATALOG["items"][k] = {"kind": "food", "edible": True, "label": r["label"], "class": "dish", "main": r["main"]}
for k, m in MENU.items():
    CATALOG["items"][k] = {"kind": "food", "edible": True, "label": m["label"], "class": "menu", "main": m["main"],
                           "price_yen": m["price_yen"], "place": m["place"]}
for k, where in HOUSE["anchors"].items():
    CATALOG["items"][k] = {"kind": "anchor", "edible": False, "label": k}

MAIN_FOODS = frozenset(k for k, v in CATALOG["items"].items() if v.get("edible") and v.get("main"))
SNACKS = frozenset(k for k, v in CATALOG["items"].items() if v.get("edible") and not v.get("main"))
BODY_KEYS = tuple(BASE["body"]["keys"])
BODY_RANGE = tuple(BASE["body"]["range"])
BASE_DRIFT = dict(BASE["body"]["base_drift_per_minute"])


def label(location):
    v = LOCATIONS.get(location, {})
    if v.get("kind") == "place":
        return v["label"]
    names = BASE["vocabulary"]["locations"]
    return names.get(v.get("public_location"), {}).get("name", location)


def public_location(location):
    return LOCATIONS.get(location, {}).get("public_location")


def activity_allowed_at(activity, actor_id, location):
    where = ACTIVITIES[activity]["where"]
    return location in where or ("own_room" in where and location == bed(actor_id))


def _adjacent(node):
    for (x, y), seconds in ROUTES.items():
        if node == x:
            yield y, seconds
        elif node == y:
            yield x, seconds


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
        for nxt, seconds in _adjacent(node):
            if cost + seconds < best.get(nxt, float("inf")):
                best[nxt] = cost + seconds
                heapq.heappush(queue, (cost + seconds, nxt))
    return None


def next_hop(a, b):
    if a == b or route_seconds(a, b) is None:
        return None
    choices = [(s + route_seconds(n, b), n) for n, s in _adjacent(a) if route_seconds(n, b) is not None]
    return min(choices)[1] if choices else None


def validate():
    errors = []
    errors += _validate_content()
    for k, a in ACTIVITIES.items():
        for w in a["where"]:
            if w != "own_room" and w not in LOCATIONS:
                errors.append(f"activity {k}: unknown place {w}")
        for h in a["hold"]:
            if CATALOG["items"].get(h, {}).get("kind") != "anchor":
                errors.append(f"activity {k}: hold {h} is not an anchor")
    for k, r in RECIPES.items():
        for i in r["consume"]:
            if i not in INGREDIENTS:
                errors.append(f"recipe {k}: unknown ingredient {i}")
        for h in r["hold"]:
            if h not in HOUSE["anchors"]:
                errors.append(f"recipe {k}: unknown anchor {h}")
    for k, p in PLACES.items():
        for act in p.get("activities", ()):
            if act not in ACTIVITIES:
                errors.append(f"place {k}: unknown activity {act}")
    for a, p in PEOPLE.items():
        ids = [w["id"] for w in p["wardrobe"]]
        if len(ids) != len(set(ids)):
            errors.append(f"{a}: duplicate wardrobe ids")
        for w in p["wardrobe"]:
            if w["category"] not in CLOTHING["categories"]:
                errors.append(f"{a}: {w['id']} unknown category")
        for r in p["cooking"]["good_at"]:
            if r not in RECIPES:
                errors.append(f"{a}: good_at unknown recipe {r}")
    if len(set(MENU) & (set(RECIPES) | set(INGREDIENTS))):
        errors.append("menu ids collide with recipes or ingredients")
    if any(route_seconds("living", x) is None for x in LOCATIONS):
        errors.append("life1 map must be connected")
    return errors


HHMM = r"([01]\d|2[0-3]):[0-5]\d"  # a time of day, 00:00-23:59
HHMM_END = rf"(?:{HHMM}|24:00)"  # the end of a span may also be 24:00 (open to midnight); never 24:01-24:59
MEALS = ("breakfast", "lunch", "dinner", "snack", "teatime", "dessert")
TIERS = ("near", "town", "city", "daytrip", "overnight")
REGULAR_DAYS = ("mon-fri", "sat-sun", "daily")
REGULAR_NOT_ACTIVITY = ("cooking",)  # her cooking slot: the meal clock and recipes decide it, not begin_activity
RENDER_SLOTS = {"actor", "activity", "dish", "place", "people", "item", "items", "price_yen", "slot", "cook", "when",
                "what"}
# wardrobe.BODY_COVER lets one garment of these categories be her whole outfit, so its label must not
# name a single piece (trousers, a jacket ...) unless it says it is a set (套装). The converter files the two
# known rows as their piece (rule D7, PIECE_OF) and stops on any other; the same lists live there.
WHOLE_BODY = ("dress", "loungewear", "sleepwear", "sportswear", "swimwear", "yukata")
SINGLE_PIECE = r"裤|外套|夹克|开衫|上衣|T ?恤|衬衫|卫衣|背心|毛衣|马甲|半身裙"


def single_piece_as_whole_body(garment):
    import re
    label = garment.get("label") or ""
    return garment.get("category") in WHOLE_BODY and "套装" not in label and bool(re.search(SINGLE_PIECE, label))


def _int(v, lo=0):
    return type(v) is int and v >= lo


def _span(v, lo=None, hi=None):
    return (isinstance(v, list) and len(v) == 2 and all(type(x) is int for x in v) and v[0] <= v[1]
            and (lo is None or v[0] >= lo) and (hi is None or v[1] <= hi))


def _hours(text):
    """'HH:MM-HH:MM[,...]': start is a time of day, end a time of day or 24:00; end before start wraps past
    midnight (23:00-02:00); an empty span (08:00-08:00) is refused."""
    import re
    return isinstance(text, str) and bool(text) and all(
        re.fullmatch(f"{HHMM}-{HHMM_END}", part) and part[:5] != part[6:] for part in text.split(","))


def _clock(v):
    import re
    return isinstance(v, str) and bool(re.fullmatch(HHMM, v))


def _weekdays(v):
    """A non-empty list of distinct int weekdays 0 (Mon) .. 6; bools and floats (1.0) are not weekdays."""
    return (isinstance(v, list) and bool(v) and all(type(d) is int and 0 <= d <= 6 for d in v)
            and len(set(v)) == len(v))


def _validate_content():
    """every id reference resolves; prices, times, weekdays and ranges are well-formed."""
    import re
    errors = []
    for k, v in INGREDIENTS.items():
        if v.get("kind") not in ("ingredient", "snack", "drink"):
            errors.append(f"ingredient {k}: bad kind")
        if not _int(v.get("price_yen"), 1) or not _int(v.get("shelf_days"), 1):
            errors.append(f"ingredient {k}: price_yen/shelf_days must be positive integers")
        for p in v.get("where_to_buy", ()):
            if p not in PLACES:
                errors.append(f"ingredient {k}: sold at unknown place {p}")
    for k, r in RECIPE_DATA.items():
        if not _int(r.get("minutes"), 1) or r.get("difficulty") not in (1, 2, 3) or not _int(r["makes"]["servings"], 1):
            errors.append(f"recipe {k}: minutes/difficulty/servings out of range")
        if not all(_int(q, 1) for q in r["consume"].values()):
            errors.append(f"recipe {k}: consume quantities must be positive integers")
        if set(r.get("meal", ())) - set(MEALS):
            errors.append(f"recipe {k}: unknown meal slot")
    for k, a in ACTIVITY_DATA.items():
        if not _span(a.get("minutes"), 1):
            errors.append(f"activity {k}: minutes must be [lo, hi]")
        party = a.get("party")
        if party is not None and not (_int(party.get("min"), 1) and _int(party.get("max"), 1) and party["min"] <= party["max"]):
            errors.append(f"activity {k}: bad party")
        if a.get("hours") is not None and not (
                isinstance(a["hours"], list) and len(a["hours"]) == 2 and _clock(a["hours"][0])
                and isinstance(a["hours"][1], str) and re.fullmatch(HHMM_END, a["hours"][1])
                and a["hours"][0] < a["hours"][1]):
            errors.append(f"activity {k}: bad hours")
        if not any(w == "own_room" or w in HOUSE["rooms"] or w.startswith(PLACE) for w in a["where"]):
            errors.append(f"activity {k}: no place to do it")
        for w in a["where"]:
            if w.startswith(PLACE) and (w[len(PLACE):] not in PLACES or k not in PLACES[w[len(PLACE):]].get("activities", ())):
                errors.append(f"activity {k}: {w} does not offer it")
    menu_ids = []
    for k, p in PLACES.items():
        t = p.get("travel", {})
        if p.get("tier") not in TIERS:
            errors.append(f"place {k}: bad tier")
        if not _int(t.get("minutes"), 1) or not _int(t.get("price_yen")):
            errors.append(f"place {k}: travel minutes/price_yen missing")
        if t.get("mode") != "walk" and t.get("price_yen") is None:
            errors.append(f"place {k}: non-walk travel without a price")
        seg = t.get("segments")
        if seg and (sum(s["minutes"] for s in seg) != t["minutes"] or sum(s["price_yen"] for s in seg) != t["price_yen"]):
            errors.append(f"place {k}: segments do not add up to travel")
        if not _hours(p.get("open")):
            errors.append(f"place {k}: bad open hours")
        if "weekdays" in p and not _weekdays(p["weekdays"]):
            errors.append(f"place {k}: bad weekdays")
        if p.get("rain") not in ("ok", "avoid", "closed"):
            errors.append(f"place {k}: bad rain")
        costs = p.get("activity_costs_yen", {})
        for act in p.get("activities", ()):
            if act not in costs and act not in ("stroll", "reading", "working", "shopping"):
                errors.append(f"place {k}: no price for {act}")
            if act in ACTIVITY_DATA and PLACE + k not in ACTIVITY_DATA[act]["where"]:
                errors.append(f"place {k}: activity {act} does not list this place in where")
        for act, yen in costs.items():
            if act not in p.get("activities", ()) or not _int(yen):
                errors.append(f"place {k}: cost for unlisted activity or bad price {act}")
        for m in p.get("menu", ()):
            menu_ids.append(m["id"])
            if not _int(m.get("price_yen")) or not isinstance(m.get("main"), bool):
                errors.append(f"place {k}: menu {m.get('id')} bad price/main")
        for s in p.get("sells", ()):
            if s != "ingredients" and s not in INGREDIENTS:
                errors.append(f"place {k}: sells unknown item {s}")
        if ("nights" in p) != ("lodging_yen" in p) or ("nights" in p and not (_span(p["nights"], 1) and _int(p["lodging_yen"]))):
            errors.append(f"place {k}: nights and lodging_yen go together")
        if p.get("tier") == "overnight" and "nights" not in p:
            errors.append(f"place {k}: overnight without nights/lodging")
        if not _span(p.get("spend_yen"), 0):
            errors.append(f"place {k}: bad spend_yen")
    if len(menu_ids) != len(set(menu_ids)):
        errors.append("menu ids repeat across places")
    if set(RECIPE_DATA) & set(INGREDIENTS):
        errors.append("recipe ids collide with ingredient ids: " + ",".join(sorted(set(RECIPE_DATA) & set(INGREDIENTS))))
    foods = set(INGREDIENTS) | set(RECIPE_DATA) | set(menu_ids)
    for a in MANIFEST["people"]:
        p = PEOPLE[a]
        if p.get("actor") != a:
            errors.append(f"{a}: actor field mismatch")
        for w in p["wardrobe"]:
            if w.get("category") not in CLOTHING["categories"]:
                errors.append(f"{a}: {w['id']} unknown category")
            if single_piece_as_whole_body(w):
                errors.append(f"{a}: {w['id']} is a single piece filed as whole-body {w['category']}")
            if "category_106" in w and w.get("category") in WHOLE_BODY:
                errors.append(f"{a}: {w['id']} re-filed but still whole-body")
            if set(w["occasions"]) - set(CLOTHING["occasions"]) or not w["occasions"]:
                errors.append(f"{a}: {w['id']} unknown occasion")
            if not _span(w.get("temp_c")) or w.get("like") not in (1, 2, 3, 4, 5):
                errors.append(f"{a}: {w['id']} bad temp_c/like")
            if not w["id"].startswith(a + "_"):
                errors.append(f"{a}: {w['id']} is not named after her")
        c = p["cooking"]
        if c.get("skill") not in (1, 2, 3):
            errors.append(f"{a}: cooking skill out of range")
        for r in c.get("cannot", ()):
            if r not in RECIPE_DATA:
                errors.append(f"{a}: cannot unknown recipe {r}")
        if set(c["good_at"]) & set(c.get("cannot", ())):
            errors.append(f"{a}: good_at and cannot overlap")
        for key in ("likes", "dislikes", "never"):
            for x in p["taste"].get(key, ()):
                if x not in foods:
                    errors.append(f"{a}: taste.{key} unknown food {x}")
        for k, v in p["activities"].get("prefer", {}).items():
            if k not in ACTIVITY_DATA or v not in (1, 2, 3, 4, 5):
                errors.append(f"{a}: prefer unknown activity or bad weight {k}")
        for k in p["activities"].get("avoid", ()):
            if k not in ACTIVITY_DATA:
                errors.append(f"{a}: avoid unknown activity {k}")
        s = p["schedule"]
        for part in ("weekday", "weekend"):
            if not all(_clock(s[part].get(x)) for x in ("wake", "bedtime")):
                errors.append(f"{a}: bad {part} wake/bedtime")
        for r in s.get("regular", ()):
            where = r["where"]
            if r["days"] not in REGULAR_DAYS or not _hours(r["at"]):
                errors.append(f"{a}: regular bad days/at {r}")
            if r["activity"] not in ACTIVITY_DATA and r["activity"] not in REGULAR_NOT_ACTIVITY:
                errors.append(f"{a}: regular unknown activity {r['activity']}")
            if where != "own_room" and where not in HOUSE["rooms"] and where[len(PLACE):] not in PLACES:
                errors.append(f"{a}: regular unknown place {where}")
            if r["activity"] in ACTIVITY_DATA and where not in ACTIVITY_DATA[r["activity"]]["where"]:
                errors.append(f"{a}: regular {r['activity']} is not done at {where}")
        if not _int(p.get("wallet_start_yen")):
            errors.append(f"{a}: bad wallet_start_yen")
    for key, lines in RENDER.get("facts", {}).items():
        for line in lines:
            if set(re.findall(r"\{(\w+)\}", line)) - RENDER_SLOTS:
                errors.append(f"render {key}: unknown slot")
    errors.extend(validate_garment_assets(GARMENT_ASSETS))
    return errors


ASSET_KINDS = ("single", "onepiece_set", "bundle")
ASSET_STATUSES = ("active", "draft", "archived")
GARMENT_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")


def validate_garment_assets(doc):
    """Validate a public-shelf garment_assets structure. Returns a list of error strings."""
    errors = []
    if not isinstance(doc, dict):
        return ["garment_assets must be an object"]
    assets = doc.get("assets")
    if not isinstance(assets, dict):
        return ["garment_assets.assets must be an object"]
    seen_ids = set()
    for aid, spec in assets.items():
        if not isinstance(aid, str) or not GARMENT_ID_PATTERN.match(aid):
            errors.append(f"garment_asset {aid!r}: bad asset_id")
            continue
        if not isinstance(spec, dict):
            errors.append(f"garment_asset {aid}: spec must be an object")
            continue
        if spec.get("kind") not in ASSET_KINDS:
            errors.append(f"garment_asset {aid}: bad kind {spec.get('kind')!r}")
        if spec.get("status", "active") not in ASSET_STATUSES:
            errors.append(f"garment_asset {aid}: bad status {spec.get('status')!r}")
        owners = spec.get("owners", ["*"])
        if not isinstance(owners, list) or not owners or any(
                not isinstance(o, str) or (o != "*" and o not in PEOPLE) for o in owners):
            errors.append(f"garment_asset {aid}: bad owners")
        if not isinstance(spec.get("label"), str) or not spec["label"].strip():
            errors.append(f"garment_asset {aid}: missing label")
        grants = spec.get("grants")
        if not isinstance(grants, list) or not grants:
            errors.append(f"garment_asset {aid}: grants must be a non-empty list")
            continue
        if spec.get("kind") == "single" and len(grants) != 1:
            errors.append(f"garment_asset {aid}: single expects exactly 1 grant")
        for g in grants:
            if not isinstance(g, dict):
                errors.append(f"garment_asset {aid}: grant entry must be an object")
                continue
            gid = g.get("id")
            if not isinstance(gid, str) or not GARMENT_ID_PATTERN.match(gid):
                errors.append(f"garment_asset {aid}: bad garment id {gid!r}")
            elif gid in seen_ids:
                errors.append(f"garment_asset {aid}: duplicate grant garment id {gid}")
            else:
                seen_ids.add(gid)
            if g.get("category") not in CLOTHING["categories"]:
                errors.append(f"garment_asset {aid}/{gid}: unknown category")
            occs = g.get("occasions") or []
            if not isinstance(occs, list) or not occs or set(occs) - set(CLOTHING["occasions"]):
                errors.append(f"garment_asset {aid}/{gid}: unknown occasion")
            if not _span(g.get("temp_c")) or g.get("like", 3) not in (1, 2, 3, 4, 5):
                errors.append(f"garment_asset {aid}/{gid}: bad temp_c/like")
    return errors


def _typed(kind, v):
    if kind == "str":
        return isinstance(v, str) and bool(v)
    if kind == "location":
        return isinstance(v, str) and v in LOCATIONS
    if kind == "seconds":
        return type(v) in (int, float) and v > 0
    return True


def guard(action_type, args, actor, resources=(), **_):
    """Pure check on authoritative rows. Instant actions are checked by their handlers."""
    if not isinstance(action_type, str) or action_type not in ACTIONS:
        return ["action_not_enabled_in_life1"]
    if not isinstance(args, dict) or not isinstance(actor, dict) or actor.get("location") not in LOCATIONS:
        return ["invalid_arguments_or_actor"]
    if action_type in INSTANT:
        return []
    schema = _ARGS[action_type]
    allowed = {k.rstrip("?") for k in schema}
    required = {k for k in schema if not k.endswith("?")}
    if set(args) - allowed or required - set(args):
        return ["missing_or_unknown_argument"]
    for name, kind in schema.items():
        key = name.rstrip("?")
        if key in args and not _typed(kind, args[key]):
            return ["invalid_argument_type:" + key]
    here, me = actor["location"], actor["actor_id"]
    rows = [dict(r) for r in resources]

    def reachable(r):
        return r.get("holder") == me or (r.get("holder") is None and r.get("location") == here)

    reasons = []
    if action_type == "move":
        if args["to"] == here:
            reasons.append("invalid_move_destination")
        if owner_of(args["to"]) not in (None, me):
            reasons.append("someone_elses_bedroom")
    elif action_type == "say":
        if not 1 <= len(args["text"].strip()) <= 500:
            reasons.append("invalid_say_text")
    elif action_type == "sleep":
        if here != bed(me) and not LOCATIONS[here].get("lodging"):
            reasons.append("sleep_requires_own_bedroom")
        if args["seconds"] > 14 * 3600:
            reasons.append("invalid_duration_seconds")
    elif action_type in ("wait", "rest"):
        if args["seconds"] > 4 * 3600:
            reasons.append("invalid_duration_seconds")
    elif action_type == "check":
        if not any(r.get("resource_id") == args["resource_id"] and reachable(r) for r in rows):
            reasons.append("inspection_resource_not_reachable")
    elif action_type == "eat":
        item = CATALOG["items"].get(args["item"], {})
        if not item.get("edible") or item.get("kind") != "food":
            reasons.append("item_not_ready_food")
        ok = [r for r in rows if r.get("item") == args["item"] and r.get("kind") == "food" and reachable(r)
              and (r.get("qty") or 0) >= 1 and ("resource_id" not in args or r.get("resource_id") == args["resource_id"])]
        if not ok:
            reasons.append("food_missing_mismatched_or_out_of_reach")
    elif action_type == "cook":
        rec = RECIPES.get(args["recipe"])
        if not rec:
            return ["unknown_recipe"]
        if here != "kitchen":
            reasons.append("cooking_requires_kitchen")
        for item, q in rec["consume"].items():
            if not any(r.get("item") == item and r.get("kind") == "food" and reachable(r) and (r.get("qty") or 0) >= q
                       for r in rows):
                reasons.append("missing_ingredient:" + item)
        for anchor in rec["hold"]:
            if not any(r.get("item") == anchor and r.get("kind") == "anchor" and reachable(r) for r in rows):
                reasons.append("missing_anchor:" + anchor)
    elif action_type == "begin_activity":
        act = args["activity"]
        if act not in ACTIVITIES:
            return ["activity_not_enabled_in_life1"]
        spec = ACTIVITIES[act]
        if not activity_allowed_at(act, me, here):
            reasons.append("activity_not_allowed_here")
        sec = args.get("seconds", spec["default"])
        if not spec["min"] <= sec <= spec["max"]:
            reasons.append("invalid_duration_seconds")
        for anchor in spec["hold"]:
            if not any(r.get("item") == anchor and r.get("kind") == "anchor" and r.get("location") == here
                       for r in rows):
                reasons.append("missing_anchor:" + anchor)
        if spec.get("eats"):
            if "snack" not in args:
                reasons.append("snack_required")
            elif not any(r.get("resource_id") == args["snack"] and snack_ok(r, act, me, here) for r in rows):
                reasons.append("snack_missing_mismatched_or_out_of_reach")
        elif "snack" in args:
            reasons.append("snack_not_eaten_in_this_activity")
    return reasons


def snack_ok(r, activity, me, here):
    """a resource row she can have in this eating activity: food of one of its classes, not a main dish, some
    left; hers anywhere, or the house's where she is or in the kitchen while she is in the house (the snack shelf and
    the fridge are in the kitchen, and she has them on the sofa)."""
    item = CATALOG["items"].get(r.get("item"), {})
    if r.get("kind") != "food" or not item.get("edible") or item.get("main") or \
            item.get("class") not in ACTIVITIES[activity]["eats"] or (r.get("qty") or 0) < 1:
        return False
    if r.get("holder") is not None:
        return r.get("holder") == me
    return r.get("location") == here or (r.get("location") == "kitchen" and here in HOUSE["rooms"])


def _derive_roster_meta(manifest, people):
    roster = tuple(manifest["people"])
    names = {a: str(people[a].get("name") or a) for a in roster}
    selves = {
        a: str(people[a].get("persona") or f"{names[a]}（{people[a].get('bio') or '合租室友'}）")
        for a in roster
    }
    cook_order = tuple(
        sorted(
            roster,
            key=lambda a: (
                int(people[a].get("cook_priority", 99)),
                -int(people[a].get("cooking", {}).get("skill", 1)),
                roster.index(a),
            ),
        )
    )
    night_owls = {a: float(people[a].get("night_owl", 0.35)) for a in roster}
    return roster, names, selves, cook_order, night_owls


ROSTER, NAMES, SELVES, COOK_ORDER, NIGHT_OWLS = _derive_roster_meta(MANIFEST, PEOPLE)


def load_pack(pack_dir=None):
    """Load any life1-compatible WorldPack directory into a self-contained vocabulary namespace."""
    from types import SimpleNamespace
    raw = str(pack_dir).strip() if pack_dir else os.environ.get("AI_HOUSE_PACK_DIR", "").strip()
    if not raw:
        pdir = DIR
    else:
        pdir = Path(raw)
        if not pdir.exists():
            candidates = [
                Path.cwd() / "packs" / raw,
                Path(__file__).resolve().parents[2] / "src" / "ai_house" / "bundled_packs" / raw,
            ]
            try:
                import importlib.resources as _res
                candidates.append(Path(str(_res.files("ai_house") / "bundled_packs" / raw)))
            except Exception:
                pass
            found = next((c for c in candidates if c.is_dir()), None)
            if found is not None:
                pdir = found
    if not pdir.exists():
        raise FileNotFoundError(f"WorldPack directory not found: {pdir}")

    def read_json(stem, default=None):
        fp = pdir / f"{stem}.json"
        if not fp.exists():
            if default is not None:
                return copy.deepcopy(default)
            return json.loads((DIR / f"{stem}.json").read_text(encoding="utf-8"))
        return json.loads(fp.read_text(encoding="utf-8"))

    manifest = read_json("manifest")
    ingredients = read_json("ingredients")
    extra_snacks = read_json("snacks", default={})
    for k, v in extra_snacks.items():
        if k not in ingredients:
            ingredients[k] = v
    recipe_data = read_json("recipes")
    activity_data = read_json("activities")
    places = read_json("places")
    clothing = read_json("clothing")
    render = read_json("render")
    garment_assets = read_json("garment_assets", default={"shelf": "未分类", "assets": {}})
    people = {}
    for a in manifest["people"]:
        pf = pdir / "people" / f"{a}.json"
        people[a] = json.loads(pf.read_text(encoding="utf-8"))
    house = manifest["house"]
    money = manifest["money"]
    roster, names, selves, cook_order, night_owls = _derive_roster_meta(manifest, people)

    locations = {k: {"kind": "room", "public_location": k} for k in house["rooms"]}
    for a in roster:
        locations[bed(a)] = {"kind": "room", "public_location": "room", "owner": a}
    locations["away"] = {"kind": "leave_point", "public_location": "away", "presentation": "street outside the house"}
    for k, p in places.items():
        locations[PLACE + k] = {
            "kind": "place", "public_location": "away", "label": p["label"], "tier": p["tier"],
            "lodging": bool(p.get("lodging_yen") is not None and p.get("nights")),
        }

    routes = {}
    for e in house["routes"]:
        ends = [bed(a) for a in roster] if e["to"] == "bedrooms" else [e["to"]]
        for b in ends:
            routes[(e["from"], b)] = e["seconds"]
    for k, p in places.items():
        routes[("away", PLACE + k)] = int(p["travel"]["minutes"]) * 60

    actions = _actions()
    activities = {}
    for k, a in activity_data.items():
        lo, hi = a["minutes"]
        activities[k] = {
            "label": a["label"], "min": lo * 60, "default": round((lo + hi) / 2) * 60, "max": hi * 60,
            "effects": {"rate": dict(a.get("effects", {}))}, "interrupt_policy": "now",
            "availability": "busy" if k in ("bathing", "movie_theater", "onsen") else "free",
            "where": tuple(a["where"]), "hold": tuple(a.get("hold", ())), "party": a.get("party"),
            "hours": tuple(a["hours"]) if a.get("hours") else None, "eats": tuple(EATS.get(k, ())),
        }
    recipes = {
        k: {
            "label": r["label"], "seconds": int(r["minutes"]) * 60, "consume": dict(r["consume"]),
            "hold": list(r["hold"]), "makes": {k: int(r["makes"]["servings"])}, "main": bool(r["main"]),
            "difficulty": r.get("difficulty", 1), "meal": list(r.get("meal", ())), "tags": list(r.get("tags", ())),
        }
        for k, r in recipe_data.items()
    }
    menu = {}
    for pid, p in places.items():
        for m in p.get("menu", ()):
            menu[m["id"]] = dict(m, place=pid)

    catalog = copy.deepcopy(BASE)
    catalog["vocab_version"] = manifest["vocab_version"]
    catalog["items"] = {}
    for k, v in ingredients.items():
        catalog["items"][k] = {
            "kind": "food", "edible": bool(v.get("edible")), "label": v["label"], "class": v["kind"],
            "price_yen": v["price_yen"], "shelf_days": v.get("shelf_days"),
        }
    for k, r in recipes.items():
        catalog["items"][k] = {"kind": "food", "edible": True, "label": r["label"], "class": "dish", "main": r["main"]}
    for k, m in menu.items():
        catalog["items"][k] = {
            "kind": "food", "edible": True, "label": m["label"], "class": "menu", "main": m["main"],
            "price_yen": m["price_yen"], "place": m["place"],
        }
    for k in house["anchors"]:
        catalog["items"][k] = {"kind": "anchor", "edible": False, "label": k}

    main_foods = frozenset(k for k, v in catalog["items"].items() if v.get("edible") and v.get("main"))
    snacks = frozenset(k for k, v in catalog["items"].items() if v.get("edible") and not v.get("main"))
    edible_set = frozenset(k for k, v in catalog["items"].items() if v.get("kind") == "food" and v.get("edible"))

    def pack_label(loc):
        v = locations.get(loc, {})
        if v.get("kind") == "place":
            return v["label"]
        loc_names = BASE["vocabulary"]["locations"]
        return loc_names.get(v.get("public_location"), {}).get("name", loc)

    def pack_public_location(loc):
        return locations.get(loc, {}).get("public_location")

    def pack_activity_allowed_at(act, actor_id, loc):
        where = activities[act]["where"]
        return loc in where or ("own_room" in where and loc == bed(actor_id))

    def pack_adjacent(node):
        for (x, y), seconds in routes.items():
            if node == x:
                yield y, seconds
            elif node == y:
                yield x, seconds

    def pack_route_seconds(a, b):
        if a not in locations or b not in locations:
            return None
        best, queue = {a: 0}, [(0, a)]
        while queue:
            cost, node = heapq.heappop(queue)
            if cost != best[node]:
                continue
            if node == b:
                return cost
            for nxt, seconds in pack_adjacent(node):
                if cost + seconds < best.get(nxt, float("inf")):
                    best[nxt] = cost + seconds
                    heapq.heappush(queue, (cost + seconds, nxt))
        return None

    def pack_next_hop(a, b):
        if a == b or pack_route_seconds(a, b) is None:
            return None
        choices = [(s + pack_route_seconds(n, b), n) for n, s in pack_adjacent(a) if pack_route_seconds(n, b) is not None]
        return min(choices)[1] if choices else None

    def pack_snack_ok(r, activity, me, here):
        item = catalog["items"].get(r.get("item"), {})
        if (
            r.get("kind") != "food"
            or not item.get("edible")
            or item.get("main")
            or item.get("class") not in activities[activity]["eats"]
            or (r.get("qty") or 0) < 1
        ):
            return False
        if r.get("holder") is not None:
            return r.get("holder") == me
        return r.get("location") == here or (r.get("location") == "kitchen" and here in house["rooms"])

    def pack_guard(action_type, args, actor, resources=(), **_):
        if not isinstance(action_type, str) or action_type not in actions:
            return ["action_not_enabled_in_life1"]
        if not isinstance(args, dict) or not isinstance(actor, dict) or actor.get("location") not in locations:
            return ["invalid_arguments_or_actor"]
        if action_type in INSTANT:
            return []
        schema = _ARGS[action_type]
        allowed = {k.rstrip("?") for k in schema}
        required = {k for k in schema if not k.endswith("?")}
        if set(args) - allowed or required - set(args):
            return ["missing_or_unknown_argument"]
        for name, kind in schema.items():
            key = name.rstrip("?")
            if key in args:
                val = args[key]
                ok_type = (
                    (isinstance(val, str) and bool(val)) if kind == "str"
                    else (isinstance(val, str) and val in locations) if kind == "location"
                    else (type(val) in (int, float) and val > 0) if kind == "seconds"
                    else True
                )
                if not ok_type:
                    return ["invalid_argument_type:" + key]
        here, me = actor["location"], actor["actor_id"]
        rows = [dict(r) for r in resources]

        def reachable(r):
            return r.get("holder") == me or (r.get("holder") is None and r.get("location") == here)

        reasons = []
        if action_type == "move":
            if args["to"] == here:
                reasons.append("invalid_move_destination")
            if owner_of(args["to"]) not in (None, me):
                reasons.append("someone_elses_bedroom")
        elif action_type == "say":
            if not 1 <= len(args["text"].strip()) <= 500:
                reasons.append("invalid_say_text")
        elif action_type == "sleep":
            if here != bed(me) and not locations[here].get("lodging"):
                reasons.append("sleep_requires_own_bedroom")
            if args["seconds"] > 14 * 3600:
                reasons.append("invalid_duration_seconds")
        elif action_type in ("wait", "rest"):
            if args["seconds"] > 4 * 3600:
                reasons.append("invalid_duration_seconds")
        elif action_type == "check":
            if not any(r.get("resource_id") == args["resource_id"] and reachable(r) for r in rows):
                reasons.append("inspection_resource_not_reachable")
        elif action_type == "eat":
            item = catalog["items"].get(args["item"], {})
            if not item.get("edible") or item.get("kind") != "food":
                reasons.append("item_not_ready_food")
            ok = [
                r for r in rows
                if r.get("item") == args["item"] and r.get("kind") == "food" and reachable(r)
                and (r.get("qty") or 0) >= 1 and ("resource_id" not in args or r.get("resource_id") == args["resource_id"])
            ]
            if not ok:
                reasons.append("food_missing_mismatched_or_out_of_reach")
        elif action_type == "cook":
            rec = recipes.get(args["recipe"])
            if not rec:
                return ["unknown_recipe"]
            if here != "kitchen":
                reasons.append("cooking_requires_kitchen")
            for item, q in rec["consume"].items():
                if not any(r.get("item") == item and r.get("kind") == "food" and reachable(r) and (r.get("qty") or 0) >= q for r in rows):
                    reasons.append("missing_ingredient:" + item)
            for anchor in rec["hold"]:
                if not any(r.get("item") == anchor and r.get("kind") == "anchor" and reachable(r) for r in rows):
                    reasons.append("missing_anchor:" + anchor)
        elif action_type == "begin_activity":
            act = args["activity"]
            if act not in activities:
                return ["activity_not_enabled_in_life1"]
            spec = activities[act]
            if not pack_activity_allowed_at(act, me, here):
                reasons.append("activity_not_allowed_here")
            sec = args.get("seconds", spec["default"])
            if not spec["min"] <= sec <= spec["max"]:
                reasons.append("invalid_duration_seconds")
            for anchor in spec["hold"]:
                if not any(r.get("item") == anchor and r.get("kind") == "anchor" and r.get("location") == here for r in rows):
                    reasons.append("missing_anchor:" + anchor)
            if spec.get("eats"):
                if "snack" not in args:
                    reasons.append("snack_required")
                elif not any(r.get("resource_id") == args["snack"] and pack_snack_ok(r, act, me, here) for r in rows):
                    reasons.append("snack_missing_mismatched_or_out_of_reach")
            elif "snack" in args:
                reasons.append("snack_not_eaten_in_this_activity")
        return reasons

    return SimpleNamespace(
        PACK_DIR=str(pdir),
        MANIFEST=manifest,
        INGREDIENTS=ingredients,
        RECIPE_DATA=recipe_data,
        ACTIVITY_DATA=activity_data,
        PLACES=places,
        CLOTHING=clothing,
        RENDER=render,
        GARMENT_ASSETS=garment_assets,
        PEOPLE=people,
        HOUSE=house,
        MONEY=money,
        ROSTER=roster,
        NAMES=names,
        SELVES=selves,
        COOK_ORDER=cook_order,
        NIGHT_OWLS=night_owls,
        VOCAB_VERSION=manifest["vocab_version"],
        BEDROOM=BEDROOM,
        PLACE=PLACE,
        INSTANT=INSTANT,
        LOCATIONS=locations,
        ROUTES=routes,
        ACTIONS=actions,
        ACTIVITIES=activities,
        RECIPES=recipes,
        MENU=menu,
        CATALOG=catalog,
        MAIN_FOODS=main_foods,
        SNACKS=snacks,
        EDIBLE=edible_set,
        BODY_KEYS=BODY_KEYS,
        BODY_RANGE=BODY_RANGE,
        BASE_DRIFT=BASE_DRIFT,
        bed=bed,
        owner_of=owner_of,
        place_id=place_id,
        label=pack_label,
        public_location=pack_public_location,
        activity_allowed_at=pack_activity_allowed_at,
        route_seconds=pack_route_seconds,
        next_hop=pack_next_hop,
        snack_ok=pack_snack_ok,
        guard=pack_guard,
        edible=lambda v: edible_set,
    )
