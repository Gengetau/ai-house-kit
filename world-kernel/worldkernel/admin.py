"""Owner input, admin corrections and the live-memory fence of P1a (v1.1 §8/§13/§14). Fake input only.

Owner input is its own principal (cmd:owner); a housemate can never submit it. An admin correction changes
the present state with a reason and its own perception rule: unwitnessed means nobody saw it happen, so it
never turns into "the owner just gave me this". Synthetic worlds can never write into live memory.
"""


def owner_input(kernel, kind, text, principal, session_id=None):
    """The owner said/did something through the game client (talk). Received is not agreed."""
    if principal != "cmd:owner":
        return {"status": "failed", "reason": "owner_only"}
    s = kernel.s
    now = s.clock.now()
    with s.tx() as db:
        row = db.execute("SELECT location FROM owner_presence WHERE state='present' AND lease_until>?"
                         " ORDER BY updated_at DESC LIMIT 1", (now,)).fetchone()
        loc = row["location"] if row else None
        eid = kernel._emit("owner_input_received", None, None, loc, {"input": kind, "text": text},
                           scope=f"room:{loc}" if loc else "owner_input", occurred=now, source_kind="owner_input",
                           observers=None if loc else ())
    return {"status": "accepted", "event_id": eid}


def correction(kernel, principal, resource_id, quantity, reason, witnessed=False):
    """admin:world inventory correction. Unwitnessed: the change is real, nobody observed it happen."""
    if principal != "admin:world":
        return {"status": "failed", "reason": "admin_only"}
    s = kernel.s
    now = s.clock.now()
    with s.tx() as db:
        r = db.execute("SELECT * FROM resources WHERE resource_id=?", (resource_id,)).fetchone()
        if not r or int(quantity) < 0:
            return {"status": "failed", "reason": "bad_correction"}
        held = db.execute("SELECT COALESCE(SUM(qty),0) FROM reservations WHERE resource_id=? AND state='held'",
                          (resource_id,)).fetchone()[0]
        if int(quantity) < held:
            return {"status": "failed", "reason": "would_break_reservation"}  # isolate in-flight effects first
        db.execute("UPDATE resources SET qty=?, version=version+1 WHERE resource_id=?", (int(quantity), resource_id))
        eid = kernel._emit("owner_correction", None, None, r["location"],
                           {"resource": resource_id, "from": r["qty"], "to": int(quantity), "reason": reason,
                            "witnessed": bool(witnessed)},
                           scope="admin", occurred=now, source_kind="owner_correction",
                           observers=None if witnessed else (), knowers=None if witnessed else [])
    return {"status": "succeeded", "event_id": eid}


def export_memory(kernel, target_world, actor_id, body):
    """P1a has no memory writer. Anything from a synthetic world aimed at live memory is refused and audited."""
    if kernel.s.synthetic and target_world != kernel.s.world_id:
        kernel.audit("synthetic_live_rejected", target=target_world, actor=actor_id)
        return {"status": "failed", "reason": "synthetic_cannot_write_live"}
    return {"status": "failed", "reason": "no_memory_writer_in_p1a"}


def legacy_write(kernel, principal, **_):
    """Old consumers (summary, script, console CLI) read projections; they are never a physical writer."""
    kernel.audit("legacy_write_refused", principal=principal)
    return {"status": "failed", "reason": "unknown_principal"}

