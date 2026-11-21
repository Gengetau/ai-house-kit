"""Data-compatible vocabulary adapter; see README for mandatory guard hooks."""
import copy
import heapq
from .catalog import load, validate as validate_catalog, guard_request

CATALOG = load()
VOCAB_VERSION = CATALOG["vocab_version"]
LOCATIONS = copy.deepcopy(CATALOG["p1a"]["locations"])
ROUTES = {(e["from"], e["to"]): e["seconds"] for e in CATALOG["p1a"]["routes"]}
ACTIONS = {k: copy.deepcopy(CATALOG["actions"][k]) for k in CATALOG["p1a"]["action_types"]}
RECIPES = copy.deepcopy(CATALOG["recipes"])
BODY_KEYS = tuple(CATALOG["body"]["keys"])
BODY_RANGE = tuple(CATALOG["body"]["range"])
BASE_DRIFT = dict(CATALOG["body"]["base_drift_per_minute"])


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
    return validate_catalog(CATALOG)


def guard(action_type, args, actor, resources=()):
    return guard_request(CATALOG, action_type, args, actor, resources)
