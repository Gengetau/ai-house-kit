"""Local city visit state and authority (K1, kernel specification v0.3).

No HTTP, wallet or item implementation lives here. K2/K3 register transactional
hooks; K4 supplies verified bridge receipts and recovery decisions. Public wire
schemas and mappings remain D0/D1's responsibility. All new times are UTC ms.
"""
import datetime as dt
import hashlib
import json
from contextlib import contextmanager

from .kernel import Refused
from .store import dumps

PROTOCOL = "city-bridge/1"
MAPPINGS_VERSION = 2
SUPPORTED_MAPPINGS_VERSIONS = (2, 3)
TERMINAL_HOME = ("closed", "reconcile", "aborted")
TRANSITIONS = {
    "preparing": ("prepared", "aborted"),
    "prepared": ("active", "aborted"),
    "active": ("returning",),
    "returning": ("settling",),
    "settling": ("settled",),
    "settled": ("closed", "reconcile"),
    "reconcile": (),
    "closed": (), "aborted": (),
}
JST = dt.timezone(dt.timedelta(hours=9))


def check_home_write(store, actor, epoch=None, *, action_id=None, require_epoch=False):
    """Shared fence for command and direct-write APIs, including store-only Guide."""
    with store.read() as db:
        row = db.execute("SELECT * FROM actor_domains WHERE actor_id=?", (actor,)).fetchone()
        auth = dict(row) if row else dict(domain="home", domain_epoch=0, visit_id=None)
        if (epoch is not None and (type(epoch) is not int or epoch != auth["domain_epoch"])
                or require_epoch and auth["domain_epoch"] > 0 and epoch is None):
            raise Refused("STALE_DOMAIN_EPOCH", domain_epoch=auth["domain_epoch"])
        visit = db.execute("SELECT * FROM city_visits WHERE actor_id=?"
                           " AND state NOT IN ('closed','reconcile','aborted')", (actor,)).fetchone()
        if auth["domain"] == "home" and not visit:
            returning = db.execute(
                "SELECT a.action_id FROM city_visits v JOIN commands c"
                " ON c.idempotency_key='city:'||v.visit_id||':home'"
                " JOIN actions a ON a.command_id=c.command_id"
                " WHERE a.actor_id=? AND a.state IN ('accepted','running')", (actor,)).fetchone()
            if returning and returning[0] != action_id:
                raise Refused("CITY_DOOR_MOVE_REQUIRED")
            return
        if (visit and action_id and visit["departure_action_id"] == action_id
                and visit["state"] in ("preparing", "prepared") and auth["domain"] == "home"):
            return
        raise Refused("ACTOR_OUT_IN_CITY", visit_id=visit["visit_id"] if visit else auth["visit_id"])


class CityVisits:
    def __init__(self, kernel):
        self.k, self.s = kernel, kernel.s
        self._active_door_key = None
        # fn(db, visit) must perform local DB work only, or raise to roll back.
        self.before_departure = []  # K2 hold / K3 custody / K5 rain and plan checks
        self.on_abort = []          # K2/K3 release only after a verified pre-commit abort
        self.on_settle = []         # K2 net entry / K3 custody settlement, T3
        self.plan_end_resolver = None  # optional fn(plan_row) -> float | None

    def _now(self):
        return int(self.s.clock.now() * 1000)

    @contextmanager
    def _tx(self, name):
        # K5 may call us inside a command handler that catches Refused. A local
        # savepoint keeps its T1/T3 atomic even when that outer transaction lives.
        with self.s.tx() as db:
            self.s.name_tx(name)
            db.execute("SAVEPOINT city_visit_op")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK TO city_visit_op")
                db.execute("RELEASE city_visit_op")
                raise
            else:
                db.execute("RELEASE city_visit_op")

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        out = dict(row)
        out["plan"] = json.loads(out["plan"])
        out["receipt_ids"] = json.loads(out["receipt_ids"])
        return out

    def get(self, visit_id):
        with self.s.read() as db:
            return self._decode(db.execute("SELECT * FROM city_visits WHERE visit_id=?", (visit_id,)).fetchone())

    def current(self, actor):
        """Includes reconcile: assets awaiting reconciliation prevent another visit."""
        with self.s.read() as db:
            return self._decode(db.execute("SELECT * FROM city_visits WHERE actor_id=?"
                                          " AND state NOT IN ('closed','aborted')", (actor,)).fetchone())

    def authority(self, actor):
        with self.s.read() as db:
            row = db.execute("SELECT * FROM actor_domains WHERE actor_id=?", (actor,)).fetchone()
            return dict(row) if row else dict(actor_id=actor, domain="home", domain_epoch=0,
                                              visit_id=None, updated_at_ms=None)

    def home_allowed(self, actor):
        with self.s.read() as db:
            if self.authority(actor)["domain"] != "home":
                return False
            return not db.execute("SELECT 1 FROM city_visits WHERE actor_id=?"
                                  " AND state NOT IN ('closed','reconcile','aborted')", (actor,)).fetchone()

    def check_home(self, actor, epoch=None, action_id=None, require_epoch=False):
        """Only the already accepted door move may finish while preparing."""
        check_home_write(self.s, actor, epoch, action_id=action_id, require_epoch=require_epoch)

    def check_city(self, visit_id, epoch):
        """K6's local fence; the engine must independently apply its own fence."""
        visit = self.get(visit_id)
        if not visit:
            raise Refused("UNKNOWN_CITY_VISIT")
        auth = self.authority(visit["actor_id"])
        if type(epoch) is not int or epoch != auth["domain_epoch"] or auth["visit_id"] != visit_id:
            raise Refused("STALE_DOMAIN_EPOCH", domain_epoch=auth["domain_epoch"])
        if auth["domain"] != "city" or visit["state"] != "active":
            raise Refused("CITY_ACTIONS_DISABLED", state=visit["state"])
        return visit

    def _request(self, db, request_id, payload):
        if not isinstance(request_id, str) or not request_id:
            raise Refused("BAD_CITY_REQUEST_ID")
        digest = hashlib.sha256(dumps(payload).encode()).hexdigest()
        old = db.execute("SELECT * FROM city_visit_requests WHERE request_id=?", (request_id,)).fetchone()
        if old:
            if old["payload_hash"] != digest:
                raise Refused("CITY_REQUEST_CONFLICT")
            return digest, json.loads(old["result"])
        return digest, None

    def _save_request(self, db, request_id, digest, result):
        db.execute("INSERT INTO city_visit_requests VALUES(?,?,?)", (request_id, digest, dumps(result)))
        return result

    def _record(self, db, visit_id):
        visit = self.get(visit_id)
        eid = self.k._emit("city_visit_state_changed", visit["actor_id"], None,
                           self.k.actor(visit["actor_id"])["location"],
                           dict(visit_id=visit_id, state=visit["state"], domain_epoch=visit["domain_epoch"]),
                           scope="self:" + visit["actor_id"], occurred=self.s.clock.now(), observers=())
        db.execute("INSERT INTO city_visit_log(visit_id,state,event_id,recorded_at_ms) VALUES(?,?,?,?)",
                   (visit_id, visit["state"], eid, self._now()))
        return visit

    def _move(self, db, actor, to, key):
        prev = self._active_door_key
        self._active_door_key = (actor, key)
        try:
            result = self.k.submit(dict(idempotency_key=key, principal="routine", origin="routine", actor_id=actor,
                                        action_type="move", args={"to": to},
                                        domain_epoch=self.authority(actor)["domain_epoch"]))
        finally:
            self._active_door_key = prev
        if result["status"] not in ("running", "accepted", "succeeded"):
            raise Refused("CITY_DOOR_MOVE_FAILED", result=result)
        return result["action_id"]

    def is_door_move(self, actor_id, here, args):
        """True only for CityVisits' own T1 living->away or T3/abort away->living move."""
        to = args.get("to") if isinstance(args, dict) else None
        if not ((here == "living" and to == "away") or (here == "away" and to == "living")):
            return False
        with self.s.read() as db:
            rows = db.execute(
                "SELECT a.action_id, a.state, c.principal, c.idempotency_key"
                " FROM actions a JOIN commands c ON c.command_id=a.command_id"
                " WHERE a.actor_id=? AND a.state IN ('running','accepted')",
                (actor_id,),
            ).fetchall()
            for r in rows:
                if r["principal"] != "routine":
                    continue
                key = r["idempotency_key"]
                if here == "living" and to == "away":
                    if r["state"] == "accepted" and self._active_door_key == (actor_id, key):
                        return True
                    if r["state"] == "running" and db.execute(
                        "SELECT 1 FROM city_visits WHERE actor_id=? AND departure_action_id=?"
                        " AND state IN ('preparing','prepared')",
                        (actor_id, r["action_id"]),
                    ).fetchone():
                        return True
                elif here == "away" and to == "living":
                    if db.execute(
                        "SELECT 1 FROM city_visits WHERE actor_id=?"
                        " AND state IN ('closed','reconcile','aborted')"
                        " AND ('city:'||visit_id||':home')=?",
                        (actor_id, key),
                    ).fetchone():
                        return True
        return False

    def _check_plan(self, db, actor, plan, *, plan_id=None):
        if (not isinstance(plan, dict) or not isinstance(plan.get("destination"), str)
                or not plan["destination"] or type(plan.get("start_at_ms")) is not int
                or type(plan.get("return_by_ms")) is not int):
            raise Refused("BAD_CITY_PLAN")
        start, end = plan["start_at_ms"], plan["return_by_ms"]
        now = self._now()
        def life_day(ms):
            return (dt.datetime.fromtimestamp(ms / 1000, JST) - dt.timedelta(hours=5)).date()
        if not 0 <= start <= now < end or life_day(start) != life_day(end):
            raise Refused("CITY_RETURN_MUST_BE_SAME_LIFE_DAY")
        has_plans = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plans'").fetchone()
        if plan_id is not None:
            if not isinstance(plan_id, str) or not plan_id or not has_plans:
                raise Refused("BAD_CITY_PLAN_LINK", plan_id=plan_id)
            linked = db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if (not linked or linked["kind"] not in ("outing", "trip")
                    or linked["state"] not in ("confirmed", "running")
                    or actor not in json.loads(linked["members"])):
                raise Refused("BAD_CITY_PLAN_LINK", plan_id=plan_id)
        if has_plans:
            has_carry = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='city_visit_carry'"
            ).fetchone() is not None
            for row in db.execute("SELECT * FROM plans WHERE state IN ('proposed','confirmed','running')"):
                if plan_id is not None and row["plan_id"] == plan_id:
                    continue
                if actor not in json.loads(row["members"]):
                    continue
                if has_carry and db.execute(
                    "SELECT 1 FROM city_visit_carry c JOIN city_visits v USING(visit_id)"
                    " WHERE c.plan_id=? AND c.actor_id=? AND v.state IN ('closed','reconcile') LIMIT 1",
                    (row["plan_id"], actor),
                ).fetchone():
                    continue
                lo, hi = row["start_at"], row["end_at"]
                if hi is None and lo is not None and self.plan_end_resolver is not None:
                    hi = self.plan_end_resolver(row)
                if lo is not None and lo < end / 1000 and (hi if hi is not None else lo + 14400) > start / 1000:
                    raise Refused("CITY_CONFLICTING_PLAN", plan_id=row["plan_id"])

    def begin(self, visit_id, actor_id, transfer_id, request_id, plan, *, protocol=PROTOCOL,
              city_catalog_version=None, mappings_version=MAPPINGS_VERSION, prepare_response=None,
              plan_id=None, context=None):
        """T1: hooks + ordinary living->away move + preparing + local log, atomically.

        K5 must supply a current plan and a rain check hook. No code calls this on
        production until K2/K3/K4/K5 and the explicit deployment gate are complete.
        The visit reserves the next epoch; home retains its old epoch until T2.
        """
        payload = dict(op="begin", visit_id=visit_id, actor_id=actor_id, transfer_id=transfer_id,
                       plan=plan, protocol=protocol, vocab_version=self.k.V.VOCAB_VERSION,
                       city_catalog_version=city_catalog_version, mappings_version=mappings_version)
        if plan_id is not None:
            payload["plan_id"] = plan_id
        with self._tx("city_begin") as db:
            digest, old = self._request(db, request_id, payload)
            if old is not None:
                return old
            if any(not isinstance(x, str) or not x for x in (visit_id, transfer_id)):
                raise Refused("BAD_CITY_ID")
            if protocol != PROTOCOL:
                raise Refused("CITY_PROTOCOL_MISMATCH")
            if type(mappings_version) is not int or mappings_version not in SUPPORTED_MAPPINGS_VERSIONS:
                raise Refused("VERSION_MISMATCH")
            # K4 supplies an already obtained response; never call HTTP inside
            # this transaction. A known refusal cannot acquire assets or move her.
            if prepare_response is not None:
                self.check_prepare_response(prepare_response)
            actor = self.k._actor(db, actor_id)
            if not actor or not actor["simulated"] or actor_id not in self.k.roster:
                raise Refused("actor_not_simulated")
            prior = db.execute("SELECT begin_hash FROM city_visits WHERE visit_id=? OR transfer_id=?",
                               (visit_id, transfer_id)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise Refused("CITY_TRANSFER_CONFLICT")
                result = json.loads(db.execute("SELECT result FROM city_visit_requests WHERE payload_hash=?",
                                               (digest,)).fetchone()[0])
                return self._save_request(db, request_id, digest, result)
            self.check_home(actor_id)
            if self.current(actor_id):
                raise Refused("CITY_VISIT_ALREADY_OPEN")
            if (actor["location"] != "living" or actor["action_id"]
                    or db.execute("SELECT 1 FROM actions WHERE actor_id=? AND state IN ('accepted','running')",
                                  (actor_id,)).fetchone()):
                raise Refused("CITY_DEPARTURE_NOT_IDLE_AT_LIVING")
            self._check_plan(db, actor_id, plan, plan_id=plan_id)
            auth = self.authority(actor_id)
            visit = dict(payload, state="preparing", domain_epoch=auth["domain_epoch"] + 1)
            if context is not None:
                visit["context"] = context
            for hook in self.before_departure:
                hook(db, visit)
            move = self._move(db, actor_id, "away", "city:" + visit_id + ":depart")
            now = self._now()
            db.execute("INSERT OR IGNORE INTO actor_domains VALUES(?,'home',0,NULL,?)", (actor_id, now))
            db.execute("INSERT INTO city_visits(visit_id,actor_id,state,transfer_id,begin_hash,domain_epoch,protocol,"
                       "vocab_version,city_catalog_version,mappings_version,plan,departure_action_id,created_at_ms,updated_at_ms)"
                       " VALUES(?,?,'preparing',?,?,?,?,?,?,?,?,?,?,?)",
                       (visit_id, actor_id, transfer_id, digest, visit["domain_epoch"], protocol, self.k.V.VOCAB_VERSION,
                        city_catalog_version, mappings_version, dumps(plan), move, now, now))
            return self._save_request(db, request_id, digest, self._record(db, visit_id))

    @staticmethod
    def check_prepare_response(response):
        """Pure K4 boundary for a parsed engine response, not HTTP/authentication.

        Refusals must be checked before begin when already known. A response to
        the actual post-T1 prepare needs K4's confirmed abort/release workflow;
        an unknown/timeout must keep K1 locked, never be passed as a refusal.
        """
        if not isinstance(response, dict):
            raise Refused("BAD_CITY_PREPARE_RESPONSE")
        error = response.get("error")
        if isinstance(error, str):
            code = error
        elif isinstance(error, dict):
            code = error.get("code")
        else:
            code = response.get("code")
        if code == "VERSION_MISMATCH":
            raise Refused("VERSION_MISMATCH")
        if error is not None or code is not None:
            raise Refused("CITY_PREPARE_REJECTED")

    def prepare_payload(self, visit_id, request_id, *, carry, source_hold_ref):
        """Build K4's prepare body from the persisted visit versions, without I/O.

        carry/source_hold_ref belong to K2/K3; K4 must persist them and reuse the
        same request on retry. This is not the jointly frozen D0/D1 schema.
        """
        if not isinstance(request_id, str) or not request_id:
            raise Refused("BAD_CITY_REQUEST_ID")
        visit = self.get(visit_id)
        if visit is None:
            raise Refused("UNKNOWN_CITY_VISIT")
        payload = {key: visit[key] for key in (
            "visit_id", "actor_id", "transfer_id", "domain_epoch", "protocol", "vocab_version",
            "city_catalog_version", "mappings_version", "plan")}
        payload.update(request_id=request_id, carry=carry, source_hold_ref=source_hold_ref)
        payload["payload_hash"] = hashlib.sha256(dumps(payload).encode()).hexdigest()
        return payload

    def _receipt(self, db, visit, state, request_id, receipt):
        if state in ("prepared", "active", "aborted") and receipt is None:
            raise Refused("CITY_RECEIPT_REQUIRED")
        if receipt is None:
            return visit["receipt_ids"]
        if (not isinstance(receipt, dict) or not isinstance(receipt.get("receipt_id"), str)
                or not receipt["receipt_id"] or receipt.get("visit_id") != visit["visit_id"]
                or receipt.get("state") != state or receipt.get("request_id") != request_id
                or type(receipt.get("accepted_at_ms")) is not int or receipt["accepted_at_ms"] < 0):
            raise Refused("BAD_CITY_RECEIPT")
        rid, body = receipt["receipt_id"], dumps(receipt)
        old = db.execute("SELECT payload FROM city_visit_receipts WHERE receipt_id=?", (rid,)).fetchone()
        if old and old[0] != body:
            raise Refused("CITY_RECEIPT_CONFLICT")
        db.execute("INSERT OR IGNORE INTO city_visit_receipts VALUES(?,?,?)", (rid, visit["visit_id"], body))
        return dict(visit["receipt_ids"], **{state: rid})

    def transition(self, visit_id, state, request_id, domain_epoch, *, receipt=None, settlement_id=None):
        """Verified local transition, fenced and durable; same request replays its original result.

        Aborting requires K4's confirmed pre-commit answer (never a timeout).
        settled->closed/reconcile is T3: run hooks, return authority, then the
        ordinary away->living move. reconcile has no outgoing transition in
        K1: asset reconciliation and its closure protocol belong to K10/D0.
        """
        payload = dict(op="transition", visit_id=visit_id, state=state, domain_epoch=domain_epoch,
                       receipt=receipt, settlement_id=settlement_id)
        with self._tx("city_transition") as db:
            digest, old = self._request(db, request_id, payload)
            if old is not None:
                return old
            visit = self.get(visit_id)
            if not visit:
                raise Refused("UNKNOWN_CITY_VISIT")
            if type(domain_epoch) is not int or domain_epoch != visit["domain_epoch"]:
                raise Refused("STALE_DOMAIN_EPOCH", domain_epoch=visit["domain_epoch"])
            if state not in TRANSITIONS[visit["state"]]:
                raise Refused("ILLEGAL_CITY_TRANSITION", state=visit["state"], target=state)
            actor_id = visit["actor_id"]
            auth = self.authority(actor_id)
            ids = self._receipt(db, visit, state, request_id, receipt)
            if state == "active":
                actor = self.k._actor(db, actor_id)
                if actor["location"] != "away" or actor["action_id"]:
                    raise Refused("CITY_DOOR_NOT_REACHED")
                if auth["domain"] != "home" or domain_epoch != auth["domain_epoch"] + 1:
                    raise Refused("STALE_DOMAIN_EPOCH")
                self._authority(db, actor_id, "city", domain_epoch, visit_id)
            if state == "settled":
                if not isinstance(settlement_id, str) or not settlement_id:
                    raise Refused("CITY_SETTLEMENT_ID_REQUIRED")
                visit["settlement_id"] = settlement_id
            elif settlement_id is not None:
                raise Refused("CITY_SETTLEMENT_ID_UNEXPECTED")
            t3 = visit["state"] == "settled" and state in ("closed", "reconcile")
            if t3:
                if auth["domain"] != "city" or auth["domain_epoch"] != domain_epoch:
                    raise Refused("STALE_DOMAIN_EPOCH")
                hook_visit = dict(visit, target_state=state)
                for hook in self.on_settle:
                    hook(db, hook_visit)
                domain_epoch += 1
                self._authority(db, actor_id, "home", domain_epoch, None)
            if state == "aborted":
                if auth["domain"] != "home":
                    raise Refused("CITY_ABORT_AFTER_COMMIT")
                for hook in self.on_abort:
                    hook(db, visit)
                a = self.k._actor(db, actor_id)
                if a["action_id"] == visit["departure_action_id"]:
                    self.k._cancel(db, a["action_id"], "city_visit_aborted", self.s.clock.now())
            db.execute("UPDATE city_visits SET state=?,domain_epoch=?,settlement_id=?,receipt_ids=?,updated_at_ms=?"
                       " WHERE visit_id=?", (state, domain_epoch, visit["settlement_id"], dumps(ids), self._now(), visit_id))
            if t3 or state == "aborted":
                a = self.k._actor(db, actor_id)
                if a["location"] == "away":
                    self._move(db, actor_id, "living", "city:" + visit_id + ":home")
            return self._save_request(db, request_id, digest, self._record(db, visit_id))

    def _authority(self, db, actor_id, domain, epoch, visit_id):
        db.execute("UPDATE actor_domains SET domain=?,domain_epoch=?,visit_id=?,updated_at_ms=? WHERE actor_id=?",
                   (domain, epoch, visit_id, self._now(), actor_id))
        actor = self.k._actor(db, actor_id)
        self.k._set_actor(db, actor_id, generation=actor["generation"] + 1)

    def recover_preparing(self, resolver=None):
        """Startup hook. Snapshot first; resolver may GET/retry/abort outside any DB transaction.

        With no K4 resolver, return the pending visits and keep them locked.
        Exceptions propagate; an unavailable engine must never imply an abort.
        """
        with self.s.read() as db:
            rows = [self._decode(r) for r in db.execute("SELECT * FROM city_visits WHERE state='preparing'"
                                                       " ORDER BY created_at_ms,visit_id")]
        if resolver is not None:
            for row in rows:
                resolver(row)
        return rows

    def close_reconciled(self, visit_id, request_id, *, order_id):
        """Close a reconcile visit once all pending orders are durably resolved (ITEM-AND-LOCKER-SPEC §3.3)."""
        payload = dict(op="close_reconciled", visit_id=visit_id, order_id=order_id)
        with self._tx("city_close_reconciled") as db:
            digest, old = self._request(db, request_id, payload)
            if old is not None:
                return old
            visit = self.get(visit_id)
            if not visit:
                raise Refused("UNKNOWN_CITY_VISIT")
            if visit["state"] == "closed":
                return self._save_request(db, request_id, digest, visit)
            if visit["state"] != "reconcile":
                raise Refused("ILLEGAL_CITY_TRANSITION", state=visit["state"], target="closed")
            db.execute("UPDATE city_visits SET state='closed', updated_at_ms=? WHERE visit_id=?",
                       (self._now(), visit_id))
            return self._save_request(db, request_id, digest, self._record(db, visit_id))
