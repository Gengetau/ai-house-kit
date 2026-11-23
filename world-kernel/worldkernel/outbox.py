"""Outbox, fake channels and receipts of P1a (v1.1 §7/§12).

A delivery is written in the same transaction as the fact that asks for it; sending happens outside any
transaction. A timeout is "unknown", never "failed and safe to resend": it waits for reconciliation by
delivery_id. Receipts are taken by delivery_id even from an older runtime_epoch, and each (status, ref) is
kept once. In P1a only system template notices exist and every channel is fake.
"""
import json
import uuid

from .store import dumps

TEMPLATES = {
    "phone_buzz": "手机震了一下",  # a notification arrived; never the message body
    "timer_done": "计时器响了",
}


class NotSent(Exception):
    """The channel refused before anything left (provably not delivered); safe to try again."""

    def __init__(self, reason="not_sent"):
        self.reason = str(reason or "not_sent")
        super().__init__(self.reason)


class BlockedDelivery(Exception):
    """Blocked permanently by the outbound send gate (e.g. leak guard or bot recipient)."""

    def __init__(self, reason="blocked"):
        self.reason = str(reason or "blocked")
        super().__init__(self.reason)


def enqueue_outbound(store, *, channel, payload, event_id=0, delivery_id=None, db=None):
    """Write a rich outbound delivery into `outbox` inside the caller's tx or a fresh one."""
    did = str(delivery_id or "").strip() or f"out-{int(event_id)}-{channel}-{uuid.uuid4().hex[:10]}"
    now = store.clock.now()
    row = (
        did,
        int(event_id or 0),
        str(channel),
        dumps(dict(payload or {})),
        "pending",
        now,
        now,
        store.epoch or 0,
    )
    sql = (
        "INSERT OR IGNORE INTO outbox(delivery_id,event_id,channel,payload,state,created_at,updated_at,runtime_epoch) "
        "VALUES(?,?,?,?,?,?,?,?)"
    )
    if db is not None:
        db.execute(sql, row)
    else:
        with store.tx() as tx_db:
            tx_db.execute(sql, row)
    return did


def request_delivery(store, event_id, channel, template, to):
    """Call inside the kernel transaction that commits the event. Returns the delivery id."""
    if template not in TEMPLATES:
        raise ValueError("only system template notices in P1a")
    delivery_id = f"dlv-{event_id}-{channel}-{to}"
    now = store.clock.now()
    store.db.execute("INSERT OR IGNORE INTO outbox(delivery_id,event_id,channel,payload,state,created_at,updated_at,"
                     "runtime_epoch) VALUES(?,?,?,?,?,?,?,?)",
                     (delivery_id, event_id, channel, dumps({"template": template, "to": to}), "pending", now, now,
                      store.epoch or 0))
    return delivery_id


class FakeChannel:
    """Scriptable stand-in for a bridge/phone adapter. plan: list of outcomes for successive sends:
    "ok", "timeout_after_send", "timeout_before_send", "refuse"."""

    idempotent = False

    def __init__(self, plan=None):
        self.plan, self.sent = list(plan or []), {}

    def send(self, delivery_id, payload):
        outcome = self.plan.pop(0) if self.plan else "ok"
        if outcome == "refuse":
            raise NotSent("refused")
        if outcome == "timeout_before_send":
            raise TimeoutError("no answer")
        self.sent.setdefault(delivery_id, []).append(payload)
        if outcome == "timeout_after_send":
            raise TimeoutError("no answer")
        return f"ext-{delivery_id}-{len(self.sent[delivery_id])}"

    def lookup(self, delivery_id):
        """External reference if the channel has it, else None."""
        n = len(self.sent.get(delivery_id, []))
        return f"ext-{delivery_id}-{n}" if n else None


class Dispatcher:
    def __init__(self, store, channels):
        self.s, self.channels = store, channels

    def _receipt(self, db, delivery_id, status, ref, epoch):
        db.execute("INSERT OR IGNORE INTO receipts(receipt_id,delivery_id,status,external_ref,received_at,runtime_epoch)"
                   " VALUES(?,?,?,?,?,?)", (f"rcp-{delivery_id}-{status}-{ref}", delivery_id, status, ref,
                                            self.s.clock.now(), epoch))

    def run_once(self):
        """Send every pending delivery once. Returns {delivery_id: new state}."""
        out = {}
        with self.s.read() as db:
            rows = db.execute("SELECT * FROM outbox WHERE state='pending' ORDER BY created_at").fetchall()
        for r in rows:
            ch = self.channels.get(r["channel"])
            if ch is None:
                continue
            with self.s.tx() as db:
                cur = db.execute("UPDATE outbox SET state='sending', attempts=attempts+1, updated_at=? WHERE delivery_id=?"
                                 " AND state='pending'", (self.s.clock.now(), r["delivery_id"]))
                if not cur.rowcount:
                    continue
            reason_ref = None
            err_status = "blocked"
            try:
                ref = ch.send(r["delivery_id"], json.loads(r["payload"]))
                state = "sent"
            except BlockedDelivery as exc:
                ref, state, reason_ref = None, "blocked", exc.reason[:64]
            except NotSent:
                ref, state = None, "pending"
            except TimeoutError:
                ref, state = None, "unknown"
            except Exception as exc:
                err_tag = f"error:{type(exc).__name__}"[:64]
                if getattr(ch, "idempotent", False) and int(r["attempts"]) < 3:
                    ref, state, reason_ref = None, "pending", None
                else:
                    ref, state, reason_ref, err_status = None, "unknown", err_tag, "error"
            with self.s.tx() as db:
                db.execute("UPDATE outbox SET state=?, external_ref=COALESCE(?,external_ref), updated_at=?"
                           " WHERE delivery_id=?", (state, ref, self.s.clock.now(), r["delivery_id"]))
                if ref:
                    self._receipt(db, r["delivery_id"], "delivered", ref, self.s.epoch or 0)
                elif reason_ref:
                    self._receipt(db, r["delivery_id"], err_status, reason_ref, self.s.epoch or 0)
            out[r["delivery_id"]] = state
        return out

    MAX_AUTO_RETRIES = 3

    def reconcile(self, sending_grace_s=0.0):
        """Ask the channel about every unknown/stuck delivery and persist the verdict.

        A channel reference means it was delivered. Without one: an idempotent channel gets the delivery back as
        `pending` (bounded by MAX_AUTO_RETRIES attempts); any other channel keeps it `unknown` for a person, never a
        blind resend. A `sending` row left behind by a crash is written back as `pending` or `unknown`, so it can
        no longer stay stuck. `sending_grace_s` skips `sending` rows touched more recently than that (a send that
        may still be in flight in another dispatcher)."""
        out = {}
        now = self.s.clock.now()
        with self.s.read() as db:
            rows = db.execute("SELECT * FROM outbox WHERE state IN ('unknown','sending')").fetchall()
        for r in rows:
            ch = self.channels.get(r["channel"])
            if ch is None:
                continue
            if r["state"] == "sending" and sending_grace_s and (now - float(r["updated_at"])) < sending_grace_s:
                continue
            ref = ch.lookup(r["delivery_id"])
            with self.s.tx() as db:
                if ref:
                    db.execute("UPDATE outbox SET state='sent', external_ref=?, updated_at=? WHERE delivery_id=?",
                               (ref, self.s.clock.now(), r["delivery_id"]))
                    self._receipt(db, r["delivery_id"], "delivered", ref, self.s.epoch or 0)
                    out[r["delivery_id"]] = "sent"
                elif getattr(ch, "idempotent", False) and int(r["attempts"]) <= self.MAX_AUTO_RETRIES:
                    db.execute("UPDATE outbox SET state='pending', updated_at=? WHERE delivery_id=?",
                               (self.s.clock.now(), r["delivery_id"]))
                    out[r["delivery_id"]] = "pending"
                else:
                    # needs a person; never a blind resend. A stuck `sending` row is made visible as `unknown`.
                    if r["state"] == "sending":
                        db.execute("UPDATE outbox SET state='unknown', updated_at=? WHERE delivery_id=?",
                                   (self.s.clock.now(), r["delivery_id"]))
                        self._receipt(db, r["delivery_id"], "error", "interrupted_while_sending", self.s.epoch or 0)
                    out[r["delivery_id"]] = "unknown"
        return out

    def receive(self, delivery_id, status, ref, epoch):
        """An adapter's receipt (delivered/read/...). Old epochs are fine; duplicates are dropped."""
        with self.s.tx() as db:
            if not db.execute("SELECT 1 FROM outbox WHERE delivery_id=?", (delivery_id,)).fetchone():
                return False
            self._receipt(db, delivery_id, status, ref, epoch)
            if status == "delivered":
                db.execute("UPDATE outbox SET state='sent', external_ref=COALESCE(external_ref,?), updated_at=?"
                           " WHERE delivery_id=? AND state!='sent'", (ref, self.s.clock.now(), delivery_id))
            return True
