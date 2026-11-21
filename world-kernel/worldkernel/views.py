"""Audience views of P1a (v1.1 §6/§9/§10): R1 snapshot and R2 events for the one game_view.

A view row is written in the same transaction as the event it shows, from the observers fixed at that
moment, so "who saw it" never comes from where people are now. Each audience has its own seq; private
events never move the public counter. Fields are whitelisted: no QQ text, no private feelings, no item
counts, no reasons behind a choice.
"""
import json

from . import vocab as V
from .store import SCHEMA_VERSION, dumps

WHEREABOUTS = ("action_started", "departed", "arrived", "action_succeeded", "action_cancelled", "action_failed")
OWNER_OWN = ("owner_entered", "owner_left_room", "owner_exited_avatar", "owner_presence_unknown")


class Gone(Exception):
    """410: the cursor is older than what the stream keeps, or from an older stream epoch; re-snapshot."""


class CursorMismatch(Exception):
    """409: the cursor belongs to another world or audience (trial vs live)."""


class GamePolicy:
    """Owner's game client. Whereabouts of every simulated housemate are R1 fields; anything said is shown
    only when the owner's avatar heard it. Whole-house camera rights are still an owner decision (v1.1 §18),
    so this is the conservative default and carries its own version."""

    audience = "game"
    version = "game_view/p1a-1"

    def project(self, ev, obs):
        kind = ev["kind"]
        payload = json.loads(ev["payload"])
        base = {"kind": kind, "actor": ev["actor_id"], "location": ev["location"], "at": ev["occurred_at"]}
        if kind in WHEREABOUTS:
            if kind == "action_started":
                base["activity"] = payload.get("activity")
                if payload.get("action_type") == "move":
                    base["to"] = payload.get("to")
            return base
        if kind in OWNER_OWN:
            return base
        if kind == "guide_offered":
            return dict(base, guide_id=payload.get("guide_id"), expires_at=payload.get("expires_at"))
        if kind == "scene_updated":
            return dict(base, guide_id=payload["guide_id"], state=payload["state"], participants=payload["participants"])
        if kind == "said":
            mode = obs.get("owner")
            if mode == "heard":
                return dict(base, text=payload.get("text"))
            if mode == "saw":
                return dict(base, kind="talked_quietly")
            return None
        return None


def project(store, policy, event_id):
    db = store.db
    ev = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
    obs = {r["observer_id"]: r["mode"] for r in db.execute("SELECT observer_id, mode FROM observations WHERE event_id=?",
                                                           (event_id,))}
    payload = policy.project(ev, obs)
    if payload is None:
        return None
    seq = int(store.meta(f"view_seq:{policy.audience}", "0")) + 1  # a counter, so purging never reuses a seq
    store.set_meta(f"view_seq:{policy.audience}", seq)
    db.execute("INSERT INTO view_events(audience_id,seq,event_id,payload,created_at) VALUES(?,?,?,?,?)",
               (policy.audience, seq, event_id, dumps(payload), store.clock.now()))
    return seq


def cursor(store, audience, seq):
    return f"{store.world_id}|{store.meta('stream_epoch')}|{audience}|{seq}"


def parse_cursor(store, audience, after):
    if after is None or after == "":
        return 0
    if isinstance(after, int) or str(after).isdigit():  # v0.9 integer, valid inside this world/audience only
        return int(after)
    try:
        world, epoch, aud, seq = str(after).rsplit("|", 3)
        seq = int(seq)
    except ValueError:
        raise CursorMismatch("bad cursor")
    if world != store.world_id or aud != audience:
        raise CursorMismatch("cursor is for another world or audience")
    if epoch != store.meta("stream_epoch"):
        raise Gone("cursor is from an older stream epoch; take a new snapshot")
    return seq


def snapshot(store, roster, policy):
    """R1: one read transaction gives both the state and the cursor the events stream continues from."""
    aud = policy.audience
    with store.read() as db:
        seq = int(store.meta(f"view_seq:{aud}", "0"))
        actors, not_sim = {}, []
        for a in roster:
            r = db.execute("SELECT * FROM actors WHERE actor_id=?", (a,)).fetchone()
            if not r or not r["simulated"]:
                actors[a] = None
                not_sim.append(a)
                continue
            actors[a] = {"location": r["location"], "activity": r["activity"], "availability": r["availability"],
                         "bubbles": []}
        # a chat enter that puts his old page tab out writes both rows in one step (same updated_at)
        owner = db.execute("SELECT state, location FROM owner_presence"
                           " ORDER BY updated_at DESC, state='present' DESC LIMIT 1").fetchone()
        scenes = [{"guide_id": r["guide_id"], "location": r["location"], "state": r["state"],
                   "participants": json.loads(r["takers"]), "expires_at": r["expires_at"]}
                  for r in db.execute("SELECT * FROM scene_offers WHERE state IN ('offered','partially_used')"
                                      " ORDER BY created_at")]
        return {"schema_version": SCHEMA_VERSION, "vocab_version": getattr(store, "vocab", V).VOCAB_VERSION, "view_policy": policy.version,
                "world_id": store.world_id, "synthetic": store.synthetic, "server_time": store.clock.now(),
                "view_revision": seq, "cursor": cursor(store, aud, seq), "actors": actors,
                "not_simulated": not_sim, "scenes": scenes, "owner": dict(owner) if owner else {"state": "unknown", "location": None}}


def events(store, policy, after=None, limit=50):
    """R2: events after the cursor, at most 50. Gone (410) when the cursor fell out of retention."""
    aud = policy.audience
    limit = max(1, min(int(limit), 50))
    with store.read() as db:
        seq = parse_cursor(store, aud, after)
        first = int(store.meta(f"view_first_seq:{aud}", "1"))
        if seq + 1 < first:
            raise Gone(f"cursor {seq} older than retained {first}")
        rows = db.execute("SELECT seq, payload FROM view_events WHERE audience_id=? AND seq>? ORDER BY seq LIMIT ?",
                          (aud, seq, limit)).fetchall()
        last = rows[-1]["seq"] if rows else seq
        return {"events": [dict(json.loads(r["payload"]), seq=r["seq"]) for r in rows],
                "cursor": cursor(store, aud, last), "server_time": store.clock.now()}


def purge(store, audience, before):
    """Drop view rows created before `before` (v1.1 §6: R2 kept about 7 days). Facts and effect keys stay."""
    with store.tx() as db:
        top = int(store.meta(f"view_seq:{audience}", "0"))
        db.execute("DELETE FROM view_events WHERE audience_id=? AND created_at<?", (audience, before))
        first = db.execute("SELECT MIN(seq) FROM view_events WHERE audience_id=?", (audience,)).fetchone()[0]
        if first is None:
            first = top + 1
        store.set_meta(f"view_first_seq:{audience}", max(first, int(store.meta(f"view_first_seq:{audience}", "1"))))


# ---------------------------------------------------------------------------------------------------------
# The R1/R2 contract a client relies on: every public R1 field is either kept current by an R2 delta, or is
# snapshot-only. Snapshot-only fields have no writer in P1a; a future writer must add a delta here (and
# to apply_events) or the client must re-snapshot. public_state() is what "R1 + R2 = later R1" compares.
R2_FOLLOWED = ("actors.location", "actors.activity", "owner.state", "owner.location",
               "scenes.state", "scenes.participants", "scenes.expires_at")
SNAPSHOT_ONLY = ("actors.availability", "actors.bubbles")
LIVE_SCENES = ("offered", "partially_used")


def public_state(snap):
    actors = {a: (dict(v) if v else None) for a, v in snap["actors"].items()}
    scenes = {s["guide_id"]: {"location": s["location"], "state": s["state"], "participants": list(s["participants"]),
                              "expires_at": s["expires_at"]} for s in snap["scenes"]}
    return {"actors": actors, "owner": {"state": snap["owner"]["state"], "location": snap["owner"]["location"]},
            "scenes": scenes}


def apply_events(snap, events):
    """Reference client reducer: R1 snapshot + the R2 events after its cursor -> public state."""
    st = public_state(snap)
    for e in events:
        kind, a = e["kind"], st["actors"].get(e.get("actor"))
        if kind == "action_started" and a:
            a["activity"] = e["activity"]
        elif kind == "arrived" and a:
            a["location"], a["activity"] = e["location"], "idle"
        elif kind in ("action_succeeded", "action_cancelled", "action_failed") and a:
            a["activity"] = "idle"
        elif kind == "owner_entered":
            st["owner"] = {"state": "present", "location": e["location"]}
        elif kind == "owner_exited_avatar":
            st["owner"]["state"] = "left"
        elif kind == "owner_presence_unknown":
            st["owner"]["state"] = "unknown"
        elif kind == "guide_offered":
            st["scenes"][e["guide_id"]] = {"location": e["location"], "state": "offered", "participants": [],
                                           "expires_at": e["expires_at"]}
        elif kind == "scene_updated":
            if e["state"] in LIVE_SCENES:
                sc = st["scenes"].setdefault(e["guide_id"], {"location": e["location"], "expires_at": None})
                sc.update(state=e["state"], participants=list(e["participants"]))
            else:
                st["scenes"].pop(e["guide_id"], None)
    return st

