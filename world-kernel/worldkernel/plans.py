"""Plans and invitations (life1): doing something together is a plan; asking is an invitation; answering is hers.

A plan has a host, a kind (meal | activity | outing | trip), what and where, a time and its members. invite is
an instant kernel action by the host; respond_invite is an instant action by the invitee only (nobody answers
for anyone, and L0 never answers). An invitation that is not answered by its deadline becomes `expired`
(no answer), never a yes or a no. Kind validators (outings/trips rules, money) are registered by their modules.
"""
import json

from .store import dumps
from .kernel import Refused

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS plans (plan_id TEXT PRIMARY KEY, host TEXT NOT NULL, kind TEXT NOT NULL, what TEXT,"
    " location TEXT, start_at REAL, end_at REAL, members TEXT NOT NULL, state TEXT NOT NULL, note TEXT,"
    " created_at REAL, updated_at REAL, event_id INTEGER)",
    "CREATE TABLE IF NOT EXISTS invitations (inv_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, inviter TEXT NOT NULL,"
    " invitee TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, expires_at REAL, created_at REAL, answered_at REAL,"
    " event_id INTEGER, answer_event INTEGER)",
    "CREATE INDEX IF NOT EXISTS inv_by_invitee ON invitations(invitee, state)",
)
KINDS = ("meal", "activity", "outing", "trip")
ANSWERS = ("yes", "no")
MAX_AHEAD_S = 14 * 86400


class Plans:
    def __init__(self, kernel, roster):
        self.k, self.s, self.roster = kernel, kernel.s, tuple(roster)
        self.validators = {}  # kind -> fn(kernel, db, host_actor, plan_args) raising Refused
        self.location_checkers = []  # fn(kind, location, host_actor, plan_args) -> bool
        self.end_resolvers = []  # fn(plan_row) -> float | None
        with self.s.tx() as c:
            for stmt in SCHEMA:
                c.execute(stmt)
        kernel.instant["invite"] = self._invite
        kernel.instant["respond_invite"] = self._respond

    def effective_end(self, row):
        keys = row.keys()
        end = row["end_at"] if "end_at" in keys else None
        if end is not None:
            return float(end)
        start = row["start_at"] if "start_at" in keys else None
        if start is None:
            return None
        for fn in self.end_resolvers:
            resolved = fn(row)
            if resolved is not None:
                return float(resolved)
        return float(start) + 4 * 3600

    # ------------------------------------------------------------------ actions
    def _invite(self, k, db, action_id, actor, args, cmd):
        now = self.s.clock.now()
        me = actor["actor_id"]
        to = args.get("to")
        if isinstance(to, str):
            to = [to]
        if (not isinstance(to, list) or not to or len(set(to)) != len(to)
                or any(t not in self.roster or t == me for t in to)):
            raise Refused("bad_invitees")
        plan_id = args.get("plan_id")
        if plan_id:
            p = db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not p or p["host"] != me or p["state"] not in ("proposed", "confirmed"):
                raise Refused("not_her_open_plan")
            members = json.loads(p["members"])
        else:
            kind = args.get("kind")
            if kind not in KINDS:
                raise Refused("bad_plan_kind")
            start = args.get("start_at")
            if type(start) not in (int, float) or not now - 60 <= start <= now + MAX_AHEAD_S:
                raise Refused("bad_start_time")
            end = args.get("end_at")
            if end is not None and (type(end) not in (int, float) or end <= start):
                raise Refused("bad_end_time")
            where = args.get("location")
            if where is not None and where not in self.k.V.LOCATIONS:
                if not any(fn(kind, where, actor, args) for fn in self.location_checkers):
                    raise Refused("unknown_location")
            if kind in self.validators:
                self.validators[kind](self.k, db, actor, args)
            plan_id = self.s.new_id("plan")
            members = [me]
            db.execute("INSERT INTO plans(plan_id,host,kind,what,location,start_at,end_at,members,state,note,created_at,updated_at,event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                       (plan_id, me, kind, str(args.get("what") or ""), where, float(start),
                        float(end) if end else None, dumps(members), "proposed", str(args.get("note") or "")[:200],
                        now, now))
            p = db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        already = {r[0] for r in db.execute("SELECT invitee FROM invitations WHERE plan_id=? AND state IN"
                                            " ('pending','accepted')", (plan_id,))}
        to = [t for t in to if t not in already and t not in members]
        if not to:
            raise Refused("already_invited")
        expires = args.get("expires_at")
        if type(expires) not in (int, float):
            expires = min(p["start_at"], now + 3 * 3600) if p["start_at"] > now + 600 else p["start_at"] + 900
        expires = max(float(expires), now + 300)
        knowers = sorted(set(to) | {me})
        eid = k._emit("invited", me, action_id, actor["location"],
                      {"plan_id": plan_id, "to": to, "kind": p["kind"], "what": p["what"], "location": p["location"],
                       "start_at": p["start_at"]}, scope="dm:" + ",".join(knowers), occurred=now, knowers=knowers,
                      observers=())
        for t in to:
            db.execute("INSERT INTO invitations VALUES(?,?,?,?,'pending',NULL,?,?,NULL,?,NULL)",
                       (self.s.new_id("inv"), plan_id, me, t, expires, now, eid))
        k._finish(db, action_id, "succeeded", None, now)
        return {"status": "succeeded", "action_id": action_id, "plan_id": plan_id, "event_id": eid}

    def _respond(self, k, db, action_id, actor, args, cmd):
        now = self.s.clock.now()
        me = actor["actor_id"]
        inv = db.execute("SELECT * FROM invitations WHERE inv_id=?", (args.get("inv_id"),)).fetchone()
        if not inv or inv["invitee"] != me:
            raise Refused("not_her_invitation")
        if inv["state"] != "pending":
            raise Refused("invitation_not_pending", state=inv["state"])
        if inv["expires_at"] <= now:
            raise Refused("invitation_expired")
        answer = args.get("answer")
        if answer not in ANSWERS:
            raise Refused("bad_answer")
        plan = db.execute("SELECT * FROM plans WHERE plan_id=?", (inv["plan_id"],)).fetchone()
        if plan["state"] in ("cancelled", "done"):
            raise Refused("plan_closed")
        state = "accepted" if answer == "yes" else "declined"
        reason = str(args.get("reason") or "")[:120]
        knowers = sorted({me, inv["inviter"]})
        eid = k._emit("invite_answered", me, action_id, actor["location"],
                      {"inv_id": inv["inv_id"], "plan_id": plan["plan_id"], "answer": answer, "reason": reason},
                      scope="dm:" + ",".join(knowers), occurred=now, knowers=knowers, observers=())
        db.execute("UPDATE invitations SET state=?, reason=?, answered_at=?, answer_event=? WHERE inv_id=?",
                   (state, reason, now, eid, inv["inv_id"]))
        if state == "accepted":
            members = json.loads(plan["members"])
            if me not in members:
                members.append(me)
            db.execute("UPDATE plans SET members=?, state=CASE WHEN state='proposed' THEN 'confirmed' ELSE state END,"
                       " updated_at=? WHERE plan_id=?", (dumps(members), now, plan["plan_id"]))
        k._finish(db, action_id, "succeeded", None, now)
        return {"status": "succeeded", "action_id": action_id, "event_id": eid, "state": state}

    # ------------------------------------------------------------------ time
    def sync(self):
        """Expire unanswered invitations (no answer), close plans whose time passed without anyone."""
        now = self.s.clock.now()
        with self.s.tx() as db:
            for inv in db.execute("SELECT * FROM invitations WHERE state='pending' AND expires_at<=?", (now,)).fetchall():
                knowers = sorted({inv["invitee"], inv["inviter"]})
                eid = self.k._emit("invite_expired", None, None, None,
                                   {"inv_id": inv["inv_id"], "plan_id": inv["plan_id"], "invitee": inv["invitee"]},
                                   scope="dm:" + ",".join(knowers), occurred=now, knowers=knowers, observers=())
                db.execute("UPDATE invitations SET state='expired', answered_at=?, answer_event=? WHERE inv_id=?",
                           (now, eid, inv["inv_id"]))
            for p in db.execute("SELECT * FROM plans WHERE state IN ('proposed','confirmed') AND start_at<=?", (now,)).fetchall():
                eff_end = self.effective_end(p)
                if eff_end is not None and eff_end < now:
                    self.finish(p["plan_id"], "lapsed")
            # All participants may leave through cancel(), which has no completion callback.
            for p in db.execute("SELECT * FROM plans WHERE kind='activity' AND state='running'").fetchall():
                actions = db.execute("SELECT state FROM actions WHERE action_type='begin_activity'"
                                     " AND json_extract(args,'$.party')=? AND started_at IS NOT NULL",
                                     (p["plan_id"],)).fetchall()
                if actions and not any(a[0] in ("accepted", "running") for a in actions):
                    self.finish(p["plan_id"], "done" if any(a[0] == "succeeded" for a in actions) else "cancelled")

    def finish(self, plan_id, outcome, note=None):
        if outcome not in ("done", "early_home", "cancelled", "lapsed", "missed"):
            raise ValueError("unknown plan outcome")
        with self.s.tx() as db:
            row = db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not row or row["state"] in ("done", "cancelled", "lapsed", "missed"):
                return False
            members = json.loads(row["members"])
            now = self.s.clock.now()
            self.k._emit("plan_finished", row["host"], None, row["location"],
                         {"plan_id": plan_id, "kind": row["kind"], "outcome": outcome,
                          "planned_members": members}, scope="dm:" + ",".join(sorted(members)),
                         occurred=now, knowers=members, observers=())
            state = "done" if outcome == "early_home" else outcome
            db.execute("UPDATE plans SET state=?, note=coalesce(?,note), updated_at=? WHERE plan_id=?",
                       (state, note, now, plan_id))
            return True

    def set_state(self, plan_id, state, note=None):
        if state in ("done", "cancelled", "lapsed", "missed", "early_home"):
            return self.finish(plan_id, state, note)
        with self.s.tx() as db:
            db.execute("UPDATE plans SET state=?, note=COALESCE(?, note), updated_at=? WHERE plan_id=?",
                       (state, note, self.s.clock.now(), plan_id))

    # ------------------------------------------------------------------ reads
    def pending_for(self, actor):
        now = self.s.clock.now()
        with self.s.read() as db:
            return [dict(r) for r in db.execute(
                "SELECT i.inv_id, i.inviter, i.expires_at, p.plan_id, p.kind, p.what, p.location, p.start_at, p.end_at,"
                " p.state AS plan_state FROM invitations i JOIN plans p USING(plan_id) WHERE i.invitee=? AND i.state='pending'"
                " AND i.expires_at>? ORDER BY i.created_at", (actor, now))]

    def plan(self, plan_id):
        with self.s.read() as db:
            r = db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if not r:
                return None
            d = dict(r, members=json.loads(r["members"]))
            d["invitations"] = [dict(x) for x in db.execute("SELECT invitee,state,reason FROM invitations WHERE"
                                                            " plan_id=? ORDER BY created_at", (plan_id,))]
            return d

    def upcoming(self, actor, horizon_s=3 * 86400):
        now = self.s.clock.now()
        with self.s.read() as db:
            rows = db.execute("SELECT * FROM plans WHERE state IN ('proposed','confirmed','running') AND"
                              " start_at<=? ORDER BY start_at",
                              (now + horizon_s,)).fetchall()
        return [
            dict(r, members=json.loads(r["members"]))
            for r in rows
            if (self.effective_end(r) or 0) >= now and actor in json.loads(r["members"])
        ]

    def due(self, within_s=0):
        """Confirmed or proposed plans starting by now + within_s (the runner starts them)."""
        now = self.s.clock.now()
        with self.s.read() as db:
            return [dict(r, members=json.loads(r["members"])) for r in db.execute(
                "SELECT * FROM plans WHERE state IN ('proposed','confirmed') AND start_at<=? ORDER BY start_at",
                (now + within_s,))]
