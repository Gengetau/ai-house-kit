"""Guide of P1a (v1.1 §3.1): rule-made scene offers. An offer is a chance, never a cast list.

Rules read only public inputs (time of day, a confirmed owner avatar entering a room). They never name who
comes, what is said, or how it ends, and they cannot hint that someone just talked to the owner in private.
Taking an offer is not accepting anyone's invitation. One ordinary offer per scene per 30 minutes, valid
30 minutes; an event may refresh early but not again within the quiet gap.
"""
import json
import time

from .store import dumps

RULE_VERSION = "guide/p1a-1"
SLOT = 1800
QUIET_GAP = 600  # an event-triggered refresh never follows another offer for the room within 10 minutes

RULES = (
    {"key": "dinner", "location": "kitchen", "jst": (18 * 60, 20 * 60 + 30),
     "opportunity": "晚饭时间，厨房可以用来做饭", "constraints": {"check_yourself": ["fridge"], "invite_separately": True},
     "invite_hint": "想和谁一起吃，需要自己去问"},
    {"key": "evening_living", "location": "living", "jst": (20 * 60 + 30, 23 * 60),
     "opportunity": "客厅晚上空着，可以歇会儿", "constraints": {"invite_separately": True}, "invite_hint": None},
)

# P1b shadow: breakfast and lunch too, so a day has three meals on offer, not one
RULES_P1B = (
    {"key": "breakfast", "location": "kitchen", "jst": (7 * 60, 9 * 60 + 30),
     "opportunity": "早饭时间，厨房可以用，冰箱和桌上可能有现成吃的", "constraints": {"check_yourself": ["fridge"],
                                                                    "invite_separately": True},
     "invite_hint": "想和谁一起吃，需要自己去问"},
    {"key": "lunch", "location": "kitchen", "jst": (12 * 60, 13 * 60 + 30),
     "opportunity": "午饭时间，厨房可以用来做饭", "constraints": {"check_yourself": ["fridge"], "invite_separately": True},
     "invite_hint": "想和谁一起吃，需要自己去问"},
) + RULES


def jst_minute(t):
    tm = time.gmtime(t + 9 * 3600)
    return tm.tm_hour * 60 + tm.tm_min


class Guide:
    def __init__(self, store, rules=RULES, kernel=None):
        self.s, self.rules, self.k = store, rules, kernel

    def _live(self, db, location, now):
        return db.execute("SELECT * FROM scene_offers WHERE location=? AND state IN ('offered','partially_used')"
                          " AND expires_at>? ORDER BY created_at DESC LIMIT 1", (location, now)).fetchone()

    def _offer(self, db, rule, start, now, source_refs):
        gid = f"guide-{rule['key']}-{int(start)}"
        cur = db.execute(
            "INSERT OR IGNORE INTO scene_offers(guide_id,rule_key,rule_version,location,window_start,window_end,opportunity,"
            "constraints,invite_hint,source_refs,audience_scope,expires_at,state,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (gid, rule["key"], RULE_VERSION, rule["location"], start, start + SLOT, rule["opportunity"],
             dumps(rule["constraints"]), rule["invite_hint"], dumps(source_refs), "residents", start + SLOT,
             "offered", now))
        if cur.rowcount and self.k:
            residents = [r[0] for r in db.execute("SELECT actor_id FROM actors WHERE simulated=1 ORDER BY actor_id")]
            self.k._emit("guide_offered", None, None, rule["location"],
                         {"guide_id": gid, "rule": rule["key"], "expires_at": start + SLOT},
                         scope="residents", occurred=now, observers=(), knowers=residents, source_kind="guide")
        return gid if cur.rowcount else None

    def offer_now(self, key):
        """Publish one rule's offer at once (tests and replay fixtures with automatic guides off)."""
        rule = next(r for r in self.rules if r["key"] == key)
        now = self.s.clock.now()
        with self.s.tx() as db:
            if self._live(db, rule["location"], now):
                return self._live(db, rule["location"], now)["guide_id"]
            return self._offer(db, rule, now, now, [{"kind": "fixture", "rule": key}])

    def respond(self, actor_id, guide_id, choice, principal, *, domain_epoch=None):
        """Her own answer to an offer: adopt or decline. Neither names anyone else (v1.1 §3.1)."""
        if principal not in (f"channel:{actor_id}", "test"):
            return {"status": "failed", "reason": "principal_actor_mismatch"}
        if choice not in ("adopt", "decline"):
            return {"status": "failed", "reason": "bad_choice"}
        with self.s.tx() as db:
            from .city_visits import check_home_write
            from .kernel import Refused
            try:
                check_home_write(self.s, actor_id, domain_epoch, require_epoch=principal.startswith("channel:"))
            except Refused as e:
                return {"status": "failed", "reason": e.reason, **e.extra}
            why = check(db, guide_id, self.s.clock.now())
            if why:
                return {"status": "failed", "reason": why}
            if choice == "adopt":
                mark_taken(db, guide_id, actor_id, self.k)
            db.execute("INSERT INTO decision_points(actor_id,at,kind,reason,outcome,refs) VALUES(?,?,?,?,?,?)",
                       (actor_id, self.s.clock.now(), "guide", choice, "own_choice", dumps([guide_id])))
        return {"status": "succeeded"}

    def run(self, triggers=()):
        """Expire old offers, then publish what the rules allow now. triggers: [("owner_entered", location, ref)]."""
        now = self.s.clock.now()
        made = []
        with self.s.tx() as db:
            for r in db.execute("SELECT guide_id, state FROM scene_offers WHERE state IN ('offered','partially_used')"
                                " AND expires_at<=? ORDER BY created_at", (now,)).fetchall():
                new = "used" if r["state"] == "partially_used" else "expired"
                db.execute("UPDATE scene_offers SET state=? WHERE guide_id=?", (new, r["guide_id"]))
                scene_delta(self.k, db, r["guide_id"], now)
            m = jst_minute(now)
            for rule in self.rules:
                lo, hi = rule["jst"]
                if not lo <= m < hi:
                    continue
                start = now - (now % SLOT)
                if self._live(db, rule["location"], now):
                    continue
                gid = self._offer(db, rule, start, now, [{"kind": "clock", "rule": rule["key"]}])
                if gid:
                    made.append(gid)
            for kind, location, ref in triggers:
                if kind != "owner_entered":
                    continue
                rule = next((r for r in self.rules if r["location"] == location
                             and r["jst"][0] <= jst_minute(now) < r["jst"][1]), None)
                last = db.execute("SELECT MAX(created_at) FROM scene_offers WHERE location=?", (location,)).fetchone()[0]
                if rule and not self._live(db, location, now) and (last is None or now - last >= QUIET_GAP):
                    gid = self._offer(db, rule, now, now, [{"kind": "owner_entered", "ref": ref}])
                    if gid:
                        made.append(gid)
        return made

    def withdraw(self, guide_id, reason):
        with self.s.tx() as db:
            cur = db.execute("UPDATE scene_offers SET state='withdrawn', constraints=json_set(constraints,'$.withdrawn',?)"
                             " WHERE guide_id=? AND state IN ('offered','partially_used')", (reason, guide_id))
            if cur.rowcount:
                scene_delta(self.k, db, guide_id, self.s.clock.now())

    def offers(self, actor_id):
        """Live offers she may see (audience 'residents' = every simulated housemate)."""
        now = self.s.clock.now()
        with self.s.read() as db:
            sim = db.execute("SELECT simulated FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
            if not sim or not sim[0]:
                return []
            rows = db.execute("SELECT guide_id, location, opportunity, constraints, invite_hint, expires_at, rule_version"
                              " FROM scene_offers WHERE state IN ('offered','partially_used') AND expires_at>?"
                              " ORDER BY created_at", (now,)).fetchall()
            return [dict(r, constraints=json.loads(r["constraints"])) for r in rows]


def check(db, guide_id, now):
    """Refusal reason for acting on this offer, or None. Runs inside the command transaction."""
    r = db.execute("SELECT state, expires_at FROM scene_offers WHERE guide_id=?", (guide_id,)).fetchone()
    if not r:
        return "unknown_guide"
    if r["state"] not in ("offered", "partially_used") or r["expires_at"] <= now:
        return "guide_not_live"
    return None


def mark_taken(db, guide_id, actor_id, kernel=None):
    """She acted on the offer (the action was accepted). Not an acceptance of anyone's invitation."""
    r = db.execute("SELECT takers, state FROM scene_offers WHERE guide_id=?", (guide_id,)).fetchone()
    takers = json.loads(r["takers"])
    if actor_id in takers and r["state"] == "partially_used":
        return
    if actor_id not in takers:
        takers.append(actor_id)
    db.execute("UPDATE scene_offers SET takers=?, state='partially_used' WHERE guide_id=?", (dumps(takers), guide_id))
    scene_delta(kernel, db, guide_id, kernel.s.clock.now() if kernel else None)


def scene_delta(kernel, db, guide_id, now):
    """R2 delta for a public scene field change (state, participants), so a client can follow R1 scenes."""
    if not kernel:
        return
    r = db.execute("SELECT * FROM scene_offers WHERE guide_id=?", (guide_id,)).fetchone()
    kernel._emit("scene_updated", None, None, r["location"],
                 {"guide_id": guide_id, "state": r["state"], "participants": json.loads(r["takers"])},
                 scope="residents", occurred=now, observers=(), knowers=[], source_kind="guide")
