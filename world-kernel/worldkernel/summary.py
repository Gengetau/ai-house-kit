"""Summary index of P1a (v1.1 §9): timed and scene-change summaries over a fixed source range.

The key is scene + audience + source range + summary_version, so a timer and a scene end over the same
events make one summary, and a second trigger only adds itself to its triggers. Every factual line points
at its event; events whose disclosure scope does not allow the audience are left out (room is not public).
Growth or other settlement keys off the original event ids, never the summary, so it counts once.
"""
import json

from .materials import allowed
from .store import dumps

SUMMARY_VERSION = "summary/p1a-1"


def summarize(kernel, scene, audience, event_ids, trigger, version=SUMMARY_VERSION, growth_rule=None):
    s = kernel.s
    ids = sorted(int(e) for e in event_ids)
    if not ids:
        return None
    lo, hi = ids[0], ids[-1]
    with s.tx() as db:
        row = db.execute("SELECT * FROM summaries WHERE scene=? AND audience=? AND source_from=? AND source_to=?"
                         " AND summary_version=?", (scene, audience, lo, hi, version)).fetchone()
        if row:
            triggers = json.loads(row["triggers"])
            if trigger not in triggers:
                triggers.append(trigger)
                db.execute("UPDATE summaries SET triggers=? WHERE summary_id=?", (dumps(triggers), row["summary_id"]))
            return row["summary_id"]
        lines = []
        for eid in ids:
            e = db.execute("SELECT * FROM events WHERE event_id=?", (eid,)).fetchone()
            if e and allowed(e["disclosure_scope"], audience):
                p = json.loads(e["payload"])
                lines.append(f"[{eid}] {e['actor_id'] or '-'} {e['kind']} {p.get('action_type', '')}".strip())
        sid = f"sum-{scene}-{audience}-{lo}-{hi}-{version}"
        db.execute("INSERT INTO summaries(summary_id,scene,audience,source_from,source_to,summary_version,"
                   "source_event_ids,text,triggers,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (sid, scene, audience, lo, hi, version, dumps(ids), "\n".join(lines), dumps([trigger]),
                    s.clock.now()))
        if growth_rule == "one_per_consumption_event":
            for eid in ids:
                e = db.execute("SELECT * FROM events WHERE event_id=?", (eid,)).fetchone()
                if e and e["kind"] == "action_succeeded" and json.loads(e["payload"]).get("action_type") == "eat":
                    kernel._effect(f"growth:{eid}:{e['actor_id']}", eid)
        return sid


def summaries(kernel, audience=None):
    with kernel.s.read() as db:
        q, a = "SELECT * FROM summaries", ()
        if audience:
            q, a = q + " WHERE audience=?", (audience,)
        return [dict(r, source_event_ids=json.loads(r["source_event_ids"]), triggers=json.loads(r["triggers"]))
                for r in db.execute(q + " ORDER BY created_at", a)]
