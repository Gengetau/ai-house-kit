"""World Kernel of P1a: the one writer of physical facts (v1.1 §1/§2/§4/§5/§12).

submit() takes a command, checks who sent it, its idempotency key and request digest, the vocabulary,
expiry, generation and the versions it depends on; then accepts, starts (reserving every resource in a
stable order) or refuses it. tick() runs due jobs in time order and settles actions: state, resource
effects, effect keys, the event, its observers and the game-view row are one transaction. Models, QQ and
any other outside call never run inside it.
"""
import hashlib
import json
import sqlite3

from . import vocab as default_vocab
from .store import dumps
from . import views
from . import guide as G
from .materials import mark_read

PRINCIPAL_KINDS = ("channel", "routine", "cmd", "adapter", "admin", "test")
RESERVED = ("inventory_adjust", "set_inventory", "memory_write", "correction")
FORBIDDEN = ("unknown_principal", "principal_actor_mismatch", "origin_not_allowed",
             "principal_cannot_act_for_housemate", "reserved_for_owner_or_admin", "owner_only", "admin_only")


class Refused(Exception):
    def __init__(self, reason, **extra):
        super().__init__(reason)
        self.reason, self.extra = reason, extra


def digest(cmd):
    body = {k: v for k, v in cmd.items() if k not in ("command_id", "idempotency_key")}
    return hashlib.sha256(dumps(body).encode()).hexdigest()


class Kernel:
    def __init__(self, store, roster, view_policy=None, vocab=None):
        """roster: every housemate key (six); only those seeded as simulated act. view_policy: views.Policy.
        vocab: the catalog object to run on (default: the catalog); a test world injects a registered
        fixture vocabulary here instead of patching module globals."""
        self.s, self.roster = store, tuple(roster)
        self.V = vocab or default_vocab
        store.vocab = self.V
        self.policy = view_policy or views.GamePolicy()
        self._resume_from = self._resume_at = None
        # life1 extension points: modules (wardrobe, invites, wallet, outings) register here instead of
        # patching the kernel. An instant handler runs inside the command transaction:
        #   handler(kernel, db, action_id, actor, args, cmd) -> {"status": ..., "action_id": ...}
        # A completion hook runs inside the completion transaction, after resource effects:
        #   hook(kernel, db, action_row, actor, args, event_id, location)
        self.instant = {}
        self.on_complete = []
        self.before_submit = []
        # a module may answer a command before it becomes an action, inside the acceptance transaction:
        #   hook(kernel, db, command_id, actor, atype, args, cmd) -> result dict | None (None = carry on as usual)
        # It may raise Refused (the command fails with that reason).
        self.accept_intercepts = []
        from .city_visits import CityVisits
        self.city_visits = CityVisits(self)

    def audit(self, kind, **detail):
        """Append an audit row (inside the caller's transaction when there is one)."""
        with self.s.tx() as db:
            db.execute("INSERT INTO audits(kind,detail,at,runtime_epoch) VALUES(?,?,?,?)",
                       (kind, dumps(detail), self.s.clock.now(), self.s.epoch or 0))

    def resume(self, stopped_at):
        """After an outage: settle only the mechanical steps whose end fell inside it, each marked
        resumed_after_outage; no new decisions are made for the gap (v1.1 §12)."""
        self._resume_from, self._resume_at = stopped_at, self.s.clock.now()
        try:
            return self.tick()
        finally:
            self._resume_from = self._resume_at = None

    # ------------------------------------------------------------------ seeding
    def seed(self, fixture, source="synthetic_fixture"):
        """Write the starting state once (v1.1 §14: source=legacy_genesis or a synthetic fixture)."""
        self.s.require_lock()
        now = self.s.clock.now()
        with self.s.tx() as db:
            if self.s.meta("seeded"):
                raise RuntimeError("world already seeded")
            for a in self.roster:
                spec = fixture.get("actors", {}).get(a)
                if spec:
                    body = {k: float(spec.get("body", {}).get(k, 50.0)) for k in self.V.BODY_KEYS}
                    db.execute("INSERT INTO actors(actor_id,simulated,profile_ref,generation,location,activity,body,body_at,"
                               "updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                               (a, 1, spec.get("profile_ref"), int(spec.get("generation", 0)), spec["location"], "idle",
                                dumps(body), now, now))
                else:
                    db.execute("INSERT INTO actors(actor_id,simulated,updated_at) VALUES(?,0,?)", (a, now))
            for r in fixture.get("resources", ()):
                # a resource may carry its own provenance (an old-world row and the owner's events)
                db.execute("INSERT INTO resources(resource_id,kind,item,location,holder,qty,capacity,source)"
                           " VALUES(?,?,?,?,?,?,?,?)", (r["id"], r["kind"], r.get("item", r["id"]), r.get("location"),
                                                        r.get("holder"), int(r.get("qty", 1)), r.get("capacity"),
                                                        r.get("source") or source))
            self.s.set_meta("seeded", source)
            self._emit("world_seeded", None, None, None, {"source": source}, scope="system", occurred=now,
                       observers=(), source_kind=source)

    # ------------------------------------------------------------------ helpers
    def _actor(self, db, actor_id):
        r = db.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        return dict(r) if r else None

    def actor(self, actor_id):
        with self.s.read() as db:
            a = self._actor(db, actor_id)
        if a:
            a["body"] = json.loads(a["body"])
        return a

    def resource(self, rid):
        with self.s.read() as db:
            r = db.execute("SELECT * FROM resources WHERE resource_id=?", (rid,)).fetchone()
            return dict(r) if r else None

    def _free_qty(self, db, rid):
        r = db.execute("SELECT qty FROM resources WHERE resource_id=?", (rid,)).fetchone()
        if not r:
            return None
        held = db.execute("SELECT COALESCE(SUM(qty),0) FROM reservations WHERE resource_id=? AND state='held'",
                          (rid,)).fetchone()[0]
        return r[0] - held

    def _bump_resource(self, db, rid, delta):
        db.execute("UPDATE resources SET qty=qty+?, version=version+1 WHERE resource_id=?", (delta, rid))

    def _present(self, db, location):
        """Who perceives a room right now: simulated housemates in it and an owner avatar entered there."""
        if not location or self.V.LOCATIONS.get(location, {}).get("kind") not in ("room", "place"):
            return []
        ids = [r[0] for r in db.execute("SELECT actor_id FROM actors WHERE simulated=1 AND location=?"
                                        " ORDER BY actor_id", (location,))]
        now = self.s.clock.now()
        if db.execute("SELECT 1 FROM owner_presence WHERE state='present' AND location=? AND lease_until>?",
                      (location, now)).fetchone():
            ids.append("owner")
        return ids

    def _emit(self, kind, actor_id, action_id, location, payload, scope, occurred, observers=None, knowers=None,
              source_ref=None, source_kind="kernel", trust="verified", retention="world"):
        """Write one event + its fixed observers + view rows. Must run inside the caller's transaction."""
        db = self.s.db
        if observers is None:
            observers = self._present(db, location)
        knowers = sorted(set(knowers if knowers is not None else observers) | ({actor_id} if actor_id else set()))
        cur = db.execute(
            "INSERT INTO events(kind,actor_id,action_id,occurred_at,recorded_at,location,payload,knowers,disclosure_scope,"
            "source_ref,source_kind,trust,retention_class,synthetic,runtime_epoch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, actor_id, action_id, occurred, self.s.clock.now(), location, dumps(payload), dumps(knowers), scope,
             source_ref, source_kind, trust, retention, 1 if self.s.synthetic else 0, self.s.epoch or 0))
        eid = cur.lastrowid
        for o in observers:
            mode = "heard" if o in knowers else "saw"
            db.execute("INSERT OR IGNORE INTO observations(event_id,observer_id,observed_at,mode,location)"
                       " VALUES(?,?,?,?,?)", (eid, o, occurred, mode, location))
        if actor_id and actor_id not in observers:
            db.execute("INSERT OR IGNORE INTO observations(event_id,observer_id,observed_at,mode,location)"
                       " VALUES(?,?,?,?,?)", (eid, actor_id, occurred, "self", location))
        views.project(self.s, self.policy, eid)
        return eid

    def _effect(self, key, event_id):
        """True the first time an effect key is applied; False if it already was (at-most-once)."""
        cur = self.s.db.execute("INSERT OR IGNORE INTO effects(effect_key,event_id,applied_at) VALUES(?,?,?)",
                                (key, event_id, self.s.clock.now()))
        return cur.rowcount == 1

    def _integrate(self, db, actor, upto):
        """The only body integrator: piecewise by the activity that ran since body_at (v1.1 §5)."""
        if not actor["simulated"] or actor["body_at"] is None or upto <= actor["body_at"]:
            return
        minutes = (upto - actor["body_at"]) / 60.0
        body = json.loads(actor["body"])
        rate = {}
        if actor["action_id"]:
            row = db.execute("SELECT action_type, args FROM actions WHERE action_id=?", (actor["action_id"],)).fetchone()
            if row:
                rate = self._effects(row[0], json.loads(row[1] or "{}")).get("rate", {})
        lo, hi = self.V.BODY_RANGE
        for k in self.V.BODY_KEYS:
            body[k] = min(hi, max(lo, body.get(k, 50.0) + (self.V.BASE_DRIFT.get(k, 0) + rate.get(k, 0)) * minutes))
        db.execute("UPDATE actors SET body=?, body_at=? WHERE actor_id=?", (dumps(body), upto, actor["actor_id"]))
        actor["body"], actor["body_at"] = dumps(body), upto

    def _settle_bodies(self, db, upto, actor_ids=None):
        """Bring bodies up to `upto` with the one integrator, idle ones included (after due jobs ran, so an
        action that ended earlier was already integrated up to its own end)."""
        q = "SELECT * FROM actors WHERE simulated=1" + (" AND actor_id IN (%s)" % ",".join("?" * len(actor_ids))
                                                        if actor_ids else "")
        for r in db.execute(q, tuple(actor_ids or ())).fetchall():
            self._integrate(db, dict(r), upto)

    def settle_bodies(self, actor_ids=None):
        """Before reading needs (routine, cognition): her body as of now. Jobs due by now must run first."""
        with self.s.tx() as db:
            if db.execute("SELECT 1 FROM jobs WHERE state='pending' AND due_at<=? LIMIT 1",
                          (self.s.clock.now(),)).fetchone():
                return False  # tick() first; it settles bodies itself after the due jobs
            self._settle_bodies(db, self.s.clock.now(), actor_ids)
            return True

    def _activity(self, atype, args):
        """begin_activity (P1b profile): the catalog activity it names, or None for every other action."""
        if atype != "begin_activity":
            return None
        name = args.get("activity") if isinstance(args, dict) else None
        if not isinstance(name, str):  # a list/object from a model: refuse, never raise (it would strand her plan)
            raise Refused("bad_activity")
        spec = getattr(self.V, "ACTIVITIES", {}).get(name)
        if not spec:
            raise Refused("activity_not_enabled", vocab_version=self.V.VOCAB_VERSION)
        return spec

    def _effects(self, atype, args):
        try:
            act = self._activity(atype, args)
        except Refused:
            return {}
        return act["effects"] if act else self.V.ACTIONS[atype]["effects"]

    def _set_actor(self, db, actor_id, **fields):
        fields["updated_at"] = self.s.clock.now()
        sets = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE actors SET {sets}, version=version+1 WHERE actor_id=?", (*fields.values(), actor_id))

    # ------------------------------------------------------------------ commands
    def _check_principal(self, cmd):
        p, actor, origin = cmd.get("principal", ""), cmd.get("actor_id"), cmd.get("origin")
        kind, _, who = p.partition(":")
        if kind not in PRINCIPAL_KINDS:
            raise Refused("unknown_principal")
        if kind == "channel":
            if who != actor:
                raise Refused("principal_actor_mismatch")  # A's token cannot act for B
            if origin not in ("self", "model"):
                raise Refused("origin_not_allowed")
        elif kind == "routine":
            if origin != "routine":
                raise Refused("origin_not_allowed")
        elif kind == "test":
            if origin not in ("self", "model", "routine"):
                raise Refused("origin_not_allowed")
        else:
            raise Refused("principal_cannot_act_for_housemate")  # owner/adapter/admin never submit her actions

    def submit(self, cmd):
        """Returns {"status": accepted|running|succeeded|failed|cancelled|conflict, ...}. Never raises on bad input."""
        self.s.require_lock()
        cmd = dict(cmd)
        if not cmd.get("idempotency_key"):
            return {"status": "failed", "reason": "missing_idempotency_key"}
        if self.s._in_tx is None:
            for hook in self.before_submit:
                hook(cmd)
        dg = digest(cmd)
        with self.s.tx() as db:
            self.s.name_tx("command_acceptance")
            old = db.execute("SELECT * FROM commands WHERE idempotency_key=?", (cmd["idempotency_key"],)).fetchone()
            if old:
                if old["request_digest"] != dg:
                    return {"status": "conflict", "reason": "idempotency_key_reused_with_other_request",
                            "command_id": old["command_id"]}
                return dict(self._command_view(db, old), replayed=True)
            command_id = cmd.get("command_id") or self.s.new_id("cmd")
            now = self.s.clock.now()
            db.execute("INSERT INTO commands(command_id,idempotency_key,principal,actor_id,origin,request_digest,status,"
                       "created_at,updated_at,synthetic) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (command_id, cmd["idempotency_key"], cmd.get("principal", ""), cmd.get("actor_id"),
                        cmd.get("origin", ""), dg, "accepted", now, now, 1 if self.s.synthetic else 0))
            try:
                res = self._accept(db, command_id, cmd)
            except Refused as e:
                res = {"status": "failed", "reason": e.reason, **e.extra}
            if cmd.get("guide_id") and res["status"] in ("accepted", "running", "succeeded"):
                G.mark_taken(db, cmd["guide_id"], cmd["actor_id"], self)
            db.execute("UPDATE commands SET status=?, action_id=?, result=?, updated_at=? WHERE command_id=?",
                       (res["status"], res.get("action_id"), dumps(res), now, command_id))
            return dict(res, command_id=command_id)

    def _command_view(self, db, row):
        res = json.loads(row["result"] or "{}")
        if row["action_id"]:
            a = db.execute("SELECT state, reason FROM actions WHERE action_id=?", (row["action_id"],)).fetchone()
            if a:
                res["status"] = a["state"] if a["state"] != "proposed" else "accepted"
                res["reason"] = a["reason"]
        res["command_id"] = row["command_id"]
        return res

    def command(self, command_id):
        with self.s.read() as db:
            row = db.execute("SELECT * FROM commands WHERE command_id=?", (command_id,)).fetchone()
            return self._command_view(db, row) if row else None

    def _accept(self, db, command_id, cmd):
        self._check_principal(cmd)
        now = self.s.clock.now()
        atype = self.V.CATALOG["aliases"].get(cmd.get("action_type"), cmd.get("action_type"))  # inspect->check etc.
        if str(atype).startswith("owner_") or atype in RESERVED:
            raise Refused("reserved_for_owner_or_admin")  # a housemate cannot speak or correct as the owner
        spec = self.V.ACTIONS.get(atype)
        if not spec:
            raise Refused("unknown_action", vocab_version=self.V.VOCAB_VERSION)
        if cmd.get("vocab_version") not in (None, self.V.VOCAB_VERSION):
            raise Refused("vocab_version_mismatch", vocab_version=self.V.VOCAB_VERSION)
        actor = self._actor(db, cmd.get("actor_id"))
        if not actor or not actor["simulated"]:
            raise Refused("actor_not_simulated")
        self.city_visits.check_home(actor["actor_id"], cmd.get("domain_epoch"),
                                    require_epoch=cmd.get("principal", "").startswith("channel:"))
        if cmd.get("expires_at") is not None and float(cmd["expires_at"]) <= now:
            raise Refused("expired")
        if cmd.get("generation") is not None and int(cmd["generation"]) != actor["generation"]:
            raise Refused("stale_generation", generation=actor["generation"])
        self._check_versions(db, cmd.get("relevant_versions") or {})
        args = cmd.get("args") or {}
        if not isinstance(args, dict):
            raise Refused("bad_args")
        if cmd.get("guide_id"):
            why = G.check(db, cmd["guide_id"], now)
            if why:
                raise Refused(why)
        action_id = self.s.new_id("act")
        act = self._activity(atype, args)
        # the catalog decides a step's safe point (an activity's own, for begin_activity); no caller can loosen it
        policy = act["interrupt_policy"] if act else spec["interrupt_policy"]
        if cmd.get("interrupt_policy") not in (None, policy):
            raise Refused("interrupt_policy_fixed_by_catalog", catalog=policy)
        answered = None  # checks that can still refuse come before a module writes anything
        for hook in self.accept_intercepts:
            answered = hook(self, db, command_id, actor, atype, args, cmd)
            if answered is not None:
                break
        db.execute("INSERT INTO actions(action_id,command_id,actor_id,origin,goal_id,guide_id,action_type,args,state,"
                   "generation,interrupt_policy,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (action_id, command_id, actor["actor_id"], cmd["origin"], cmd.get("goal_id"), cmd.get("guide_id"),
                    atype, dumps(args), "accepted", actor["generation"], policy, now))
        db.execute("INSERT INTO home_action_epochs VALUES(?,?)",
                   (action_id, self.city_visits.authority(actor["actor_id"])["domain_epoch"]))
        if answered is not None:
            # the module did the work in this transaction; leave a finished action behind like an instant one, so
            # her model plan sees the step succeed (it follows the action_id) and moves on
            self._finish(db, action_id, "succeeded", None, now)
            return dict(answered, status="succeeded", action_id=action_id)
        if not spec["body"]:
            return self._instant(db, action_id, actor, atype, args, cmd)
        if actor["action_id"]:
            cur = db.execute("SELECT * FROM actions WHERE action_id=?", (actor["action_id"],)).fetchone()
            replace = cur["origin"] == "routine" and cmd["origin"] != "routine"
            if not (replace or cmd.get("interrupt")):
                self._finish(db, action_id, "failed", "busy", now)
                return {"status": "failed", "reason": "busy", "action_id": action_id, "busy_with": cur["action_id"]}
            if cur["interrupt_policy"] == "now":
                self._cancel(db, cur["action_id"], "superseded" if replace else "interrupted", now, by=action_id)
            else:  # at_end, also when her choice replaces routine: the step finishes, then this one starts
                try:  # it only starts at the safe point, where the guard runs again with her real attention
                    self._guard(db, atype, args, actor, attention=True)
                except Refused as e:
                    self._finish(db, action_id, "failed", e.reason, now)
                    return {"status": "failed", "reason": e.reason, "action_id": action_id, **e.extra}
                self._job(db, "start", action_id, cur["due_at"])
                return {"status": "accepted", "action_id": action_id, "starts_at": cur["due_at"]}
        return self._start(db, action_id, now)

    def _check_versions(self, db, versions):
        for key, want in versions.items():
            kind, _, rid = key.partition(":")
            table, col = {"resource": ("resources", "resource_id"), "actor": ("actors", "actor_id")}.get(kind, (None, None))
            if not table:
                raise Refused("bad_relevant_version", key=key)
            r = db.execute(f"SELECT version FROM {table} WHERE {col}=?", (rid,)).fetchone()
            if not r or r[0] != int(want):
                raise Refused("stale_version", key=key, version=r[0] if r else None)

    def _job(self, db, kind, ref, due):
        db.execute("INSERT OR REPLACE INTO jobs(job_id,kind,ref,due_at,state,created_at) VALUES(?,?,?,?,?,?)",
                   (f"{kind}:{ref}", kind, ref, due, "pending", self.s.clock.now()))

    def _requirements(self, db, atype, args, actor):
        """[(resource_id, qty, consume)] for an action, in stable resource_id order."""
        need = []
        if atype == "cook":
            rec = self.V.RECIPES.get(args.get("recipe"))
            if not rec:
                raise Refused("unknown_recipe")
            for item, q in rec["consume"].items():
                need.append((self._find(db, item, actor, min_qty=q), q, True))
            for anchor in rec["hold"]:
                need.append((self._find(db, anchor, actor), 1, False))
        elif atype == "eat":
            need.append((self._find(db, args.get("item"), actor, rid=args.get("resource_id")), 1, True))
        elif atype == "begin_activity":
            # every participant holds the anchors herself: the host leaving early never frees what the
            # others still use; sharing them is decided in _party_shared, not by skipping the hold
            for anchor in self._activity(atype, args)["hold"]:  # e.g. the bathtub: one at a time
                need.append((self._find(db, anchor, actor), 1, False))
            if args.get("snack") is not None:
                # the snack she names for an eating activity, held while she eats, gone from the stock when it
                # ends; the vocabulary guard decides whether she may have it (kind, class, reach)
                need.append((str(args["snack"]), 1, True))
        need.sort(key=lambda n: n[0])
        return need

    def _party_shared(self, db, atype, args, actor):
        """Resources she may share with her party: only for a group activity of the catalog (party spec),
        only under a live plan of that activity she hosts or accepted, and only what a running member of the same
        plan, same activity, same room already holds. Anything else (the bathtub, a made-up party) stays exclusive."""
        party = args.get("party") if isinstance(args, dict) else None
        if atype != "begin_activity" or not isinstance(party, str) or not party:
            return set()
        spec = self._activity(atype, args)
        if not spec.get("party"):
            return set()
        try:
            p = db.execute("SELECT host, kind, what, members, state FROM plans WHERE plan_id=?", (party,)).fetchone()
        except sqlite3.OperationalError:  # no plans table: plans are not installed, nothing to share
            return set()
        if not p or p["state"] not in ("confirmed", "running") or p["kind"] != "activity" \
                or p["what"] != args.get("activity"):
            return set()
        me = actor["actor_id"]
        members = {p["host"], *json.loads(p["members"] or "[]")}
        if me not in members:
            return set()
        # the holder must be a member of the same plan too; an uninvited person who named the plan id and
        # took the tv first holds it for herself, not for this plan
        rows = db.execute(
            "SELECT v.resource_id, a.actor_id FROM reservations v JOIN actions a ON a.action_id=v.action_id"
            " JOIN actors b ON b.action_id=a.action_id WHERE v.state='held' AND a.state='running'"
            " AND a.action_type='begin_activity' AND json_extract(a.args,'$.party')=?"
            " AND json_extract(a.args,'$.activity')=? AND b.location=? AND a.actor_id!=?",
            (party, args.get("activity"), actor["location"], me)).fetchall()
        held = {}
        for r in rows:
            held.setdefault(r[0], set()).add(r[1])
        return {rid for rid, who in held.items() if who <= members}

    def _find(self, db, item, actor, rid=None, min_qty=1):
        """A resource she can reach: in her room or held by her. She must look; the kernel will not guess."""
        if rid:
            r = db.execute("SELECT * FROM resources WHERE resource_id=?", (rid,)).fetchone()
        else:
            r = db.execute("SELECT r.*,r.qty-COALESCE((SELECT SUM(qty) FROM reservations v"
                           " WHERE v.resource_id=r.resource_id AND v.state='held'),0) AS free_qty FROM resources r"
                           " WHERE item=? AND (holder=? OR (holder IS NULL AND location=?))"
                           " ORDER BY (free_qty>=?) DESC,resource_id LIMIT 1",
                           (item, actor["actor_id"], actor["location"], min_qty)).fetchone()
        if not r:
            raise Refused("resource_not_found", item=item)
        if r["holder"] not in (None, actor["actor_id"]) or (r["holder"] is None and r["location"] != actor["location"]):
            raise Refused("resource_out_of_reach", item=item)
        return r["resource_id"]

    def _duration(self, db, atype, args, actor):
        rule = self.V.ACTIONS[atype]["duration_rule"]
        if "fixed" in rule:
            return rule["fixed"]
        if "arg" in rule:
            sec = float(args.get(rule["arg"], 0))
            if not 0 < sec <= rule["max"]:
                raise Refused("bad_duration")
            return sec
        if rule.get("route"):
            sec = self.V.route_seconds(actor["location"], args.get("to"))
            if args.get("to") not in self.V.LOCATIONS or sec is None:
                raise Refused("no_route")
            if sec == 0:
                raise Refused("already_there")
            return sec
        if rule.get("recipe"):
            if args.get("recipe") not in self.V.RECIPES:
                raise Refused("unknown_recipe")
            return self.V.RECIPES[args["recipe"]]["seconds"]
        if rule.get("activity"):
            act = self._activity(atype, args)
            sec = args.get("seconds", act["default"])
            if type(sec) not in (int, float) or not act["min"] <= sec <= act["max"]:
                raise Refused("bad_duration")
            return float(sec)
        return 0

    def _guard(self, db, atype, args, actor, own=None, attention=None):
        """The catalog guard on authoritative rows (never on what a model claims is in stock).
        read_phone also gets her fake mailbox rows and whether her attention is free, both decided here."""
        rows = [dict(r) for r in db.execute("SELECT resource_id, kind, item, location, holder, qty FROM resources")]
        who = {"actor_id": actor["actor_id"], "location": actor["location"]}
        if atype == "read_phone":
            box = [{"message_id": m["source_ref"], "recipient": m["actor_id"], "world_id": self.s.world_id,
                    "synthetic": bool(m["synthetic"]), "source": "fake_mailbox"}
                   for m in db.execute("SELECT source_ref, actor_id, synthetic FROM materials"
                                       " WHERE source_kind='phone_message' AND actor_id=?", (actor["actor_id"],))]
            free = self._attention_free(db, actor, own) if attention is None else attention
            why = self.V.guard(atype, args, who, rows, mailbox=box, attention_allowed=free)
        else:
            why = self.V.guard(atype, args, who, rows)
        if why:
            raise Refused("guard:" + why[0], guard=why)

    def _attention_free(self, db, actor, own=None):
        """Safe point (v1.1 §4): nothing running, or only `own`, or a step that may be interrupted now."""
        cur = actor["action_id"]
        if not cur or cur == own:
            return True
        return db.execute("SELECT interrupt_policy FROM actions WHERE action_id=?", (cur,)).fetchone()[0] != "at_end"

    def _prereq(self, actor, atype):
        for p in self.V.ACTIONS[atype]["prerequisites"]:
            if p.startswith("at_location:") and actor["location"] != p.split(":", 1)[1]:
                raise Refused("wrong_location", need=p.split(":", 1)[1])
            if p == "private_rest_location" and actor["location"] != self.V.bed(actor["actor_id"]):
                raise Refused("wrong_location", need=self.V.bed(actor["actor_id"]))

    def _start(self, db, action_id, now):
        a = db.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
        actor = self._actor(db, a["actor_id"])
        args = json.loads(a["args"])
        try:
            epoch = db.execute("SELECT domain_epoch FROM home_action_epochs WHERE action_id=?", (action_id,)).fetchone()
            self.city_visits.check_home(actor["actor_id"], epoch[0] if epoch else 0, action_id=action_id)
            if actor["action_id"]:
                raise Refused("busy")
            self._prereq(actor, a["action_type"])
            dur = self._duration(db, a["action_type"], args, actor)
            need = self._requirements(db, a["action_type"], args, actor)
            self._guard(db, a["action_type"], args, actor)
            shared = self._party_shared(db, a["action_type"], args, actor)
            for rid, q, _ in need:  # all or nothing, stable order
                if rid in shared:  # the same plan's tv/board game, already held by a member who is doing it
                    continue
                free = self._free_qty(db, rid)
                if free is None or free < q:
                    raise Refused("resource_unavailable", resource=rid)
        except Refused as e:
            self._finish(db, action_id, "failed", e.reason, now)
            return {"status": "failed", "reason": e.reason, "action_id": action_id, **e.extra}
        for rid, q, c in need:
            db.execute("INSERT INTO reservations(reservation_id,resource_id,action_id,qty,consume,state,created_at)"
                       " VALUES(?,?,?,?,?,?,?)", (self.s.new_id("rsv", 10), rid, action_id, q, int(c), "held", now))
        self._integrate(db, actor, now)
        activity = self.V.ACTIONS[a["action_type"]]["activity"] or args.get("activity")  # begin_activity names it
        db.execute("UPDATE actions SET state='running', started_at=?, due_at=? WHERE action_id=?",
                   (now, now + dur, action_id))
        self._set_actor(db, actor["actor_id"], action_id=action_id, activity=activity,
                        generation=actor["generation"] + 1)
        payload = {"action_type": a["action_type"], "activity": activity, "origin": a["origin"]}
        if a["action_type"] == "move":
            payload["to"] = args["to"]
        self._emit("action_started", actor["actor_id"], action_id, actor["location"], payload,
                   scope=f"room:{actor['location']}", occurred=now)
        self._job(db, "complete", action_id, now + dur)
        return {"status": "running", "action_id": action_id, "due_at": now + dur}

    @staticmethod
    def speech_audience(actor_id, location, to=None):
        """Who a said line goes to, as a disclosure audience: the room, or a quiet word between two."""
        return f"dm:{','.join(sorted([actor_id, to]))}" if to else f"room:{location}"

    def _instant(self, db, action_id, actor, atype, args, cmd):
        now = self.s.clock.now()
        if atype in self.instant:
            try:
                return self.instant[atype](self, db, action_id, actor, args, cmd)
            except Refused as e:
                self._finish(db, action_id, "failed", e.reason, now)
                return {"status": "failed", "reason": e.reason, "action_id": action_id, **e.extra}
        if atype == "say":
            try:
                self._guard(db, atype, args, actor)
            except Refused as e:
                self._finish(db, action_id, "failed", e.reason, now)
                return {"status": "failed", "reason": e.reason, "action_id": action_id, **e.extra}
            text = str(args["text"])
            to = args.get("to")
            present = self._present(db, actor["location"])
            if to:
                if to not in present:
                    self._finish(db, action_id, "failed", "addressee_not_present", now)
                    return {"status": "failed", "reason": "addressee_not_present", "action_id": action_id}
                knowers = [actor["actor_id"], to]
            else:
                knowers = present
            scope = self.speech_audience(actor["actor_id"], actor["location"], to)
            # A model's line must have been written for exactly this audience, from what may be said to it
            # (Cognition.express attests it). Her deliberation knows more than she may say (v1.1 §6).
            if cmd.get("origin") == "model" and cmd.get("expressed_for") != scope:
                self._finish(db, action_id, "failed", "not_expressed_for_this_audience", now)
                return {"status": "failed", "reason": "not_expressed_for_this_audience", "action_id": action_id,
                        "audience": scope}
            eid = self._emit("said", actor["actor_id"], action_id, actor["location"], {"text": text, "to": to},
                             scope=scope, occurred=now, knowers=knowers)
            self._finish(db, action_id, "succeeded", None, now)
            return {"status": "succeeded", "action_id": action_id, "event_id": eid}
        raise Refused("unknown_action")

    def _finish(self, db, action_id, state, reason, at, by=None):
        db.execute("UPDATE actions SET state=?, reason=?, finished_at=?, superseded_by=? WHERE action_id=?",
                   (state, reason, at, by, action_id))
        db.execute("UPDATE reservations SET state='released' WHERE action_id=? AND state='held'", (action_id,))
        db.execute("UPDATE jobs SET state='cancelled' WHERE ref=? AND state='pending'", (action_id,))

    def _cancel(self, db, action_id, reason, at, by=None):
        a = db.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
        actor = self._actor(db, a["actor_id"])
        self._integrate(db, actor, at)
        self._finish(db, action_id, "cancelled", reason, at, by)
        if actor["action_id"] == action_id:
            self._set_actor(db, actor["actor_id"], action_id=None, activity="idle", generation=actor["generation"] + 1)
        self._emit("action_cancelled", actor["actor_id"], action_id, actor["location"],
                   {"action_type": a["action_type"], "reason": reason}, scope=f"room:{actor['location']}", occurred=at)

    def cancel(self, actor_id, action_id, principal):
        """Her own decision to stop (channel:<her>) — completed effects stay, unused resources are released."""
        self.s.require_lock()
        if principal not in (f"channel:{actor_id}", "test"):
            return {"status": "failed", "reason": "principal_actor_mismatch"}
        with self.s.tx() as db:
            try:
                self.city_visits.check_home(actor_id)
            except Refused as e:
                return {"status": "failed", "reason": e.reason, **e.extra}
            a = db.execute("SELECT * FROM actions WHERE action_id=? AND actor_id=?", (action_id, actor_id)).fetchone()
            if not a or a["state"] not in ("accepted", "running"):
                return {"status": "failed", "reason": "not_cancellable"}
            self._cancel(db, action_id, "own_choice", self.s.clock.now())
            return {"status": "cancelled", "action_id": action_id}

    # ------------------------------------------------------------------ time
    def tick(self):
        """Run every job due by now, oldest first. Returns the number of jobs run."""
        self.s.require_lock()
        n = 0
        while True:
            with self.s.tx() as db:
                j = db.execute("SELECT * FROM jobs WHERE state='pending' AND due_at<=? ORDER BY due_at, job_id LIMIT 1",
                               (self.s.clock.now(),)).fetchone()
                if not j:
                    self._settle_bodies(db, self.s.clock.now())
                    return n
                self.s.name_tx("action_completion" if j["kind"] == "complete" else "action_start")
                db.execute("UPDATE jobs SET state='done', done_at=? WHERE job_id=?", (self.s.clock.now(), j["job_id"]))
                if self._resume_from is not None and j["due_at"] < self._resume_at:
                    self._emit("resumed_after_outage", None, j["ref"], None,
                               {"job": j["kind"], "due_at": j["due_at"], "recovered_at": self.s.clock.now()},
                               scope="system", occurred=self.s.clock.now(), observers=())
                if j["kind"] == "complete":
                    self._complete(db, j["ref"], j["due_at"])
                elif j["kind"] == "start":
                    self._start(db, j["ref"], j["due_at"])
                n += 1

    def _complete(self, db, action_id, at):
        a = db.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
        if a["state"] != "running":
            return
        actor = self._actor(db, a["actor_id"])
        args = json.loads(a["args"])
        self._integrate(db, actor, at)
        spec = self.V.ACTIONS[a["action_type"]]
        held = db.execute("SELECT resource_id, qty, consume FROM reservations WHERE action_id=? AND state='held'"
                          " ORDER BY resource_id", (action_id,)).fetchall()
        for rid, q, _ in held:  # the reservation must still be backed by real stock
            r = db.execute("SELECT qty FROM resources WHERE resource_id=?", (rid,)).fetchone()
            if not r or r[0] < q:
                self._finish(db, action_id, "failed", "resource_vanished", at)
                self._set_actor(db, actor["actor_id"], action_id=None, activity="idle", generation=actor["generation"] + 1)
                self._emit("action_failed", actor["actor_id"], action_id, actor["location"],
                           {"action_type": a["action_type"], "reason": "resource_vanished"},
                           scope=f"room:{actor['location']}", occurred=at)
                return
        try:  # the catalog guard once more before anything is settled
            self._guard(db, a["action_type"], args, actor, own=action_id)
        except Refused as e:
            self._finish(db, action_id, "failed", e.reason, at)
            self._set_actor(db, actor["actor_id"], action_id=None, activity="idle", generation=actor["generation"] + 1)
            self._emit("action_failed", actor["actor_id"], action_id, actor["location"],
                       {"action_type": a["action_type"], "reason": e.reason}, scope=f"room:{actor['location']}",
                       occurred=at)
            return
        location = actor["location"]
        payload = {"action_type": a["action_type"]}
        if a["action_type"] == "move":
            self._emit("departed", actor["actor_id"], action_id, location, {"to": args["to"]},
                       scope=f"room:{location}", occurred=at)
            location = args["to"]
        db.execute("UPDATE actions SET state='succeeded', finished_at=? WHERE action_id=?", (at, action_id))
        self._set_actor(db, actor["actor_id"], action_id=None, activity="idle", location=location,
                        generation=actor["generation"] + 1)
        kind = "arrived" if a["action_type"] == "move" else "action_succeeded"
        eid = self._emit(kind, actor["actor_id"], action_id, location, payload, scope=f"room:{location}", occurred=at)
        for rid, q, consume in held:
            if consume and self._effect(f"{action_id}:consume:{rid}", eid):
                self._bump_resource(db, rid, -q)
        db.execute("UPDATE reservations SET state='used' WHERE action_id=? AND state='held'", (action_id,))
        if a["action_type"] == "cook":
            for item, q in self.V.RECIPES[args["recipe"]]["makes"].items():
                if self._effect(f"{action_id}:make:{item}", eid):
                    rid = f"{item}@{action_id}"
                    db.execute("INSERT INTO resources(resource_id,kind,item,location,holder,qty,source)"
                               " VALUES(?,?,?,?,?,?,?)", (rid, "food", item, location, None, q, f"event:{eid}"))
        if a["action_type"] == "read_phone" and self._effect(f"{action_id}:read:{args['message_id']}", eid):
            mark_read(self, db, actor["actor_id"], args["message_id"], location)
        once = spec["effects"].get("once")
        if once and self._effect(f"{action_id}:body", eid):
            body = json.loads(self._actor(db, actor["actor_id"])["body"])
            lo, hi = self.V.BODY_RANGE
            for k, d in once.items():
                body[k] = min(hi, max(lo, body.get(k, 50.0) + d))
            db.execute("UPDATE actors SET body=? WHERE actor_id=?", (dumps(body), actor["actor_id"]))
        for hook in self.on_complete:
            hook(self, db, a, actor, args, eid, location)

    # ------------------------------------------------------------------ owner presence (fake input in P1a)
    def owner_presence(self, session_id, op, location=None, principal="cmd:owner", lease=90.0):
        """enter/move/heartbeat/leave of an authenticated avatar session; drop-outs become unknown, not 'left'."""
        self.s.require_lock()
        if principal != "cmd:owner":
            return {"status": "failed", "reason": "owner_only"}
        now = self.s.clock.now()
        with self.s.tx() as db:
            row = db.execute("SELECT * FROM owner_presence WHERE session_id=?", (session_id,)).fetchone()
            if op == "enter":
                if location not in self.V.LOCATIONS or self.V.LOCATIONS[location]["kind"] != "room":
                    return {"status": "failed", "reason": "bad_location"}
                db.execute("INSERT OR REPLACE INTO owner_presence(session_id,state,location,lease_until,updated_at)"
                           " VALUES(?,?,?,?,?)", (session_id, "present", location, now + lease, now))
                self._emit("owner_entered", None, None, location, {"session": "avatar"}, scope=f"room:{location}",
                           occurred=now, source_kind="owner_input")
            elif not row or row["state"] != "present":
                return {"status": "failed", "reason": "no_session"}
            elif op == "heartbeat":
                db.execute("UPDATE owner_presence SET lease_until=?, updated_at=? WHERE session_id=?",
                           (now + lease, now, session_id))
            elif op == "move":
                if self.V.route_seconds(row["location"], location) is None or self.V.LOCATIONS.get(location, {}).get("kind") != "room":
                    return {"status": "failed", "reason": "no_route"}
                self._emit("owner_left_room", None, None, row["location"], {}, scope=f"room:{row['location']}",
                           occurred=now, source_kind="owner_input")
                db.execute("UPDATE owner_presence SET location=?, lease_until=?, updated_at=? WHERE session_id=?",
                           (location, now + lease, now, session_id))
                self._emit("owner_entered", None, None, location, {}, scope=f"room:{location}", occurred=now,
                           source_kind="owner_input")
            elif op == "leave":
                db.execute("UPDATE owner_presence SET state='left', updated_at=? WHERE session_id=?", (now, session_id))
                self._emit("owner_exited_avatar", None, None, row["location"], {}, scope=f"room:{row['location']}",
                           occurred=now, source_kind="owner_input")
            else:
                return {"status": "failed", "reason": "bad_op"}
            return {"status": "succeeded"}

    def expire_presence(self):
        """Lease ran out: unknown, never an invented departure (v1.1 §8)."""
        now = self.s.clock.now()
        with self.s.tx() as db:
            for r in db.execute("SELECT * FROM owner_presence WHERE state='present' AND lease_until<=?", (now,)).fetchall():
                db.execute("UPDATE owner_presence SET state='unknown', updated_at=? WHERE session_id=?",
                           (now, r["session_id"]))
                # nobody in the house perceives a client dropping; the game stream still gets the state delta
                self._emit("owner_presence_unknown", None, None, r["location"], {"state": "unknown"}, scope="owner_view",
                           occurred=now, observers=(), source_kind="owner_input")
