"""Who is who, as the shared memory and identity code reads it.

The private deployment keeps a hand-written table in a file of this name. The kit has no fixed housemates: a house
is made from a pack, so the table here is empty and `make()` builds one for a house. `loader.load(roster=...)`
binds a copy of the memory module to it.

One row per housemate:
actor     her key in the world            consumer  her key as a memory owner (the same as actor in the kit)
qq        her account number (stable)     name      what she is called now
account   the account's own nickname, when it is not her name
former    names she had before            nicknames what people also call her
The owner has actor / consumer / qq / name, and may have `aliases`: other names the owner goes by.
"""

import types

ROSTER = ()
OWNER = {"actor": "owner", "consumer": "owner", "qq": 0, "name": "房主"}

_BY = {}


def names(key="actor", order=None):
    """{actor or consumer: name}, in roster order or the given order of keys."""
    rows = ROSTER if order is None else [_BY[k] for k in order]
    return {r[key]: r["name"] for r in rows}


ACTORS = names()
CONSUMER_ACTOR = {}
ACTOR_QQ = {}
ALIASES = {}
SIGNED_AS = {}


def make(rows, owner=None, name="roster"):
    """A roster module for one house. rows: dicts with at least actor, qq and name; owner: a dict with qq and
    optionally name and aliases. Numbers must be distinct and positive: who said a line is told by its number."""
    table = []
    for r in rows:
        actor = str(r["actor"])
        table.append({"actor": actor, "consumer": str(r.get("consumer") or actor), "qq": int(r["qq"]),
                      "name": str(r.get("name") or actor), "account": tuple(r.get("account") or ()),
                      "former": tuple(r.get("former") or ()), "nicknames": tuple(r.get("nicknames") or ())})
    own = {"actor": "owner", "consumer": "owner", "qq": int((owner or {}).get("qq") or 0),
           "name": str((owner or {}).get("name") or "房主")}
    if (owner or {}).get("aliases"):
        own["aliases"] = tuple(str(a) for a in owner["aliases"])
    numbers = [r["qq"] for r in table] + [own["qq"]]
    if any(n <= 0 for n in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("roster numbers must be positive and distinct")
    by = {r["actor"]: r for r in table} | {r["consumer"]: r for r in table}

    def _names(key="actor", order=None):
        rows_ = table if order is None else [by[k] for k in order]
        return {r[key]: r["name"] for r in rows_}

    m = types.ModuleType(name)
    m.ROSTER, m.OWNER, m._BY, m.names = tuple(table), own, by, _names
    m.ACTORS = _names()
    m.CONSUMER_ACTOR = {r["consumer"]: r["actor"] for r in table if r["consumer"] != r["actor"]}
    m.ACTOR_QQ = {r["actor"]: r["qq"] for r in table}
    m.ALIASES = {r["actor"]: (r["name"], *r["account"], *r["former"], *r["nicknames"]) for r in table}
    m.SIGNED_AS = {n: r["name"] for r in table for n in (*r["account"], *r["former"])}
    return m
