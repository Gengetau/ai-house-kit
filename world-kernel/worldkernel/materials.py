"""Scoped materials and audience-first context of P1a (v1.1 §6/§7).

A material is something one housemate came to know: a private chat, a hand at the card table, a quiet
scene in the kitchen, a phone message. It keeps knowers (who knows it) apart from disclosure_scope (to whom
it may be expressed). The context builder picks the output audience first and only then the materials that
may be used for it, so knowing a secret never means using it in public. Phone messages arrive unread: the
notification is a fact, the body joins her knowledge only when she reads it herself.
"""
import json

from .store import dumps

SCOPES = ("public", "owner_private", "room:", "dm:", "table:", "self:")


def scope_ok(scope):
    return scope == "public" or scope == "owner_private" or any(scope.startswith(p) for p in SCOPES[2:])


def allowed(scope, audience):
    """May material with this disclosure_scope be used when speaking to this audience?
    public material goes anywhere; anything else only to exactly its own audience (room is not public).
    self:<her> is her own deliberation, never an output target: every scope she knows, each still tagged."""
    return scope == "public" or scope == audience or audience.startswith("self:")


def observe(kernel, actor_id, source_ref, scope, body, source_kind="synthetic_observation", knowers=None):
    """She learnt something in a given scope (fake input in P1a). Returns the material id."""
    if not scope_ok(scope):
        raise ValueError("unknown disclosure scope")
    s = kernel.s
    mid = s.new_id("mat")
    with s.tx() as db:
        db.execute("INSERT INTO materials(material_id,actor_id,source_ref,source_kind,observed_at,knowers,disclosure_scope,"
                   "body,state,trust,retention_class,synthetic,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (mid, actor_id, source_ref, source_kind, s.clock.now(), dumps(sorted(knowers or [actor_id])), scope,
                    body, "known", "synthetic", "trial", 1 if s.synthetic else 0, s.clock.now()))
    return mid


def phone_receive(kernel, actor_id, message_id, scope, body):
    """A message lands in her phone: the buzz is a world fact she notices; the body stays unread."""
    s = kernel.s
    with s.tx() as db:
        db.execute("INSERT OR IGNORE INTO materials(material_id,actor_id,source_ref,source_kind,observed_at,knowers,"
                   "disclosure_scope,body,state,trust,retention_class,synthetic,created_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (f"msg-{actor_id}-{message_id}", actor_id, message_id, "phone_message", None, "[]", scope, body,
                    "unread", "synthetic", "trial", 1 if s.synthetic else 0, s.clock.now()))
        a = db.execute("SELECT location FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        kernel._emit("phone_notified", actor_id, None, a["location"], {"message": message_id},
                     scope=f"self:{actor_id}", occurred=s.clock.now(), observers=(), knowers=[actor_id])


def read_message(kernel, actor_id, message_id, principal, *, domain_epoch=None):
    """Her own reading. Not while her hands are in a step that must finish (at_end) — v1.1 §4 safe points.
    For a world on the default catalog, where read_phone is P2. A registered fixture catalog (w7-fast.1)
    makes read_phone a real kernel action instead, which ends in the same mark_read()."""
    if principal not in (f"channel:{actor_id}", "test"):
        return {"status": "failed", "reason": "principal_actor_mismatch"}
    s = kernel.s
    with s.tx() as db:
        from .kernel import Refused
        try:
            kernel.city_visits.check_home(actor_id, domain_epoch, require_epoch=principal.startswith("channel:"))
        except Refused as e:
            return {"status": "failed", "reason": e.reason, **e.extra}
        m = db.execute("SELECT * FROM materials WHERE material_id=?", (f"msg-{actor_id}-{message_id}",)).fetchone()
        if not m:
            return {"status": "failed", "reason": "no_such_message"}
        a = db.execute("SELECT action_id FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if a["action_id"]:
            cur = db.execute("SELECT interrupt_policy FROM actions WHERE action_id=?", (a["action_id"],)).fetchone()
            if cur["interrupt_policy"] == "at_end":
                return {"status": "failed", "reason": "busy_until_safe_point"}
        loc = db.execute("SELECT location FROM actors WHERE actor_id=?", (actor_id,)).fetchone()["location"]
        mark_read(kernel, db, actor_id, message_id, loc)
        return {"status": "succeeded"}


def mark_read(kernel, db, actor_id, message_id, location):
    """The body joins her knowledge (inside the caller's transaction). Reading twice changes nothing."""
    s = kernel.s
    m = db.execute("SELECT * FROM materials WHERE material_id=?", (f"msg-{actor_id}-{message_id}",)).fetchone()
    if m and m["state"] == "unread":
        db.execute("UPDATE materials SET state='known', observed_at=?, knowers=? WHERE material_id=?",
                   (s.clock.now(), dumps([actor_id]), m["material_id"]))
        kernel._emit("phone_read", actor_id, None, location, {"message": message_id}, scope=f"self:{actor_id}",
                     occurred=s.clock.now(), observers=(), knowers=[actor_id])


def context_materials(kernel, actor_id, audience):
    """Bodies of what she knows that may be used toward `audience`, each with its scope."""
    with kernel.s.read() as db:
        rows = db.execute("SELECT * FROM materials WHERE state='known' ORDER BY created_at").fetchall()
    out = []
    for r in rows:
        if actor_id in json.loads(r["knowers"]) and allowed(r["disclosure_scope"], audience):
            out.append({"source_ref": r["source_ref"], "scope": r["disclosure_scope"], "text": r["body"]})
    return out
