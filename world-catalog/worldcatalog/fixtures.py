"""Explicit synthetic catalogue registration; never installs a real phone executor.

Kernel adapters may inject the returned object as their vocabulary dependency.
Use guard at acceptance, queued start, and pre-completion with authoritative rows.
Mailbox and attention are kernel-owned state, never model supplied arguments.
"""
import copy
import heapq
import json
from pathlib import Path
from .catalog import load, guard_request

PROFILE = "w7-fast.1"
VERSION = "world-catalog/1.0.0-p1a+w7-fast.1"
_EXPECTED = {"profile": PROFILE, "base_version": "world-catalog/1.0.0-p1a",
             "vocab_version": VERSION,
             "constraints": {"synthetic": True, "fake_channels": True, "paid_calls": False},
             "eat_seconds": 2, "rice_seconds": 10, "kitchen_living_seconds": 5,
             "read_phone_seconds": 1}


def fixture_vocabulary(profile, *, world_id, synthetic=False, fake_channels=False, paid_calls=True):
    if profile != PROFILE or synthetic is not True or fake_channels is not True or paid_calls is not False:
        raise ValueError("registered synthetic/fake/unpaid profile required")
    if not isinstance(world_id, str) or not world_id:
        raise ValueError("world_id required")
    spec = json.loads(Path(__file__).with_name("profiles").joinpath("w7-fast.v1.json").read_text())
    # Registry revisions require a new version and reviewed code, not arbitrary patches.
    if json.dumps(spec, sort_keys=True) != json.dumps(_EXPECTED, sort_keys=True):
        raise ValueError("unregistered profile revision")
    data = load()
    if data["vocab_version"] != spec["base_version"]:
        raise ValueError("fixture base version mismatch")
    data["vocab_version"] = VERSION
    data["actions"]["eat"]["duration_rule"] = {"fixed": spec["eat_seconds"]}
    data["recipes"]["rice"]["seconds"] = spec["rice_seconds"]
    for edge in data["p1a"]["routes"]:
        if {edge["from"], edge["to"]} == {"kitchen", "living"}:
            edge["seconds"] = spec["kitchen_living_seconds"]
    phone = data["actions"]["read_phone"]
    phone.update(duration_rule={"fixed": spec["read_phone_seconds"]}, phase="P1a",
                 execution="fake_mailbox", argument_schema={"message_id": "message_id"},
                 parameter_status="synthetic_fixture_only")
    data["p1a"]["action_types"].append("read_phone")
    return FixtureVocabulary(data, world_id)


class FixtureVocabulary:
    def __init__(self, data, world_id):
        self.world_id = world_id
        self.CATALOG = data
        self.VOCAB_VERSION = data["vocab_version"]
        self.LOCATIONS = copy.deepcopy(data["p1a"]["locations"])
        self.ROUTES = {(e["from"], e["to"]): e["seconds"] for e in data["p1a"]["routes"]}
        self.ACTIONS = {k: copy.deepcopy(data["actions"][k]) for k in data["p1a"]["action_types"]}
        self.RECIPES = copy.deepcopy(data["recipes"])
        self.BODY_KEYS = tuple(data["body"]["keys"])
        self.BODY_RANGE = tuple(data["body"]["range"])
        self.BASE_DRIFT = dict(data["body"]["base_drift_per_minute"])
        self._snapshot = copy.deepcopy(self.__dict__)

    def validate(self):
        return [] if all(getattr(self, k) == v for k, v in self._snapshot.items()) else ["fixture modified"]

    def route_seconds(self, a, b):
        if a not in self.LOCATIONS or b not in self.LOCATIONS:
            return None
        queue, best = [(0, a)], {a: 0}
        while queue:
            cost, node = heapq.heappop(queue)
            if cost != best[node]:
                continue
            if node == b:
                return cost
            for (x, y), seconds in self.ROUTES.items():
                nxt = y if node == x else x if node == y else None
                if nxt is not None and cost + seconds < best.get(nxt, float("inf")):
                    best[nxt] = cost + seconds
                    heapq.heappush(queue, (cost + seconds, nxt))
        return None

    def next_hop(self, a, b):
        if a == b or self.route_seconds(a, b) is None:
            return None
        choices = []
        for (x, y), seconds in self.ROUTES.items():
            nxt = y if a == x else x if a == y else None
            rest = self.route_seconds(nxt, b) if nxt else None
            if rest is not None:
                choices.append((seconds + rest, nxt))
        return min(choices)[1] if choices else None

    def guard(self, action_type, args, actor, resources=(), *, mailbox=(), attention_allowed=False):
        if self.validate():
            return ["fixture_modified"]
        if action_type != "read_phone":
            return guard_request(self.CATALOG, action_type, args, actor, resources)
        if (not isinstance(args, dict) or set(args) != {"message_id"}
                or not isinstance(args["message_id"], str) or not args["message_id"]):
            return ["invalid_message_arguments"]
        if (not isinstance(actor, dict) or not isinstance(actor.get("actor_id"), str)
                or not actor["actor_id"] or actor.get("location") not in self.LOCATIONS):
            return ["unknown_actor_or_location"]
        if attention_allowed is not True:
            return ["reading_not_permitted"]
        for row in mailbox:
            if (isinstance(row, dict) and row.get("message_id") == args["message_id"]
                    and row.get("recipient") == actor["actor_id"]
                    and row.get("world_id") == self.world_id and row.get("synthetic") is True
                    and row.get("source") == "fake_mailbox"):
                return []
        return ["message_not_in_actor_fake_mailbox"]
