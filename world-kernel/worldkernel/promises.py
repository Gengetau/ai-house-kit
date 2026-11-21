"""kernel evidence exporter. World DB commit precedes the fenced cursor advance."""
import json
import os
import sqlite3
from contextlib import closing

from .store import LockHeld


def enabled():
    # The base intents switch keeps owner reopen receipts draining after a phase-1 rollback.
    return os.environ.get("INTENTS", "0") == "1" or any(os.environ.get(k, "0") not in ("0", "off", "") for k in (
        "INTENTS_KERNEL_MATCH", "INTENTS_DECIDE_CTX", "WORLD_KERNEL_ERRANDS_FROM_INTENTS", "INTENTS_EXPIRY_V2"))


class Promises:
    def __init__(self, life, world_db, projector, intents):
        self.life, self.s, self.path, self.projector, self.intents = life, life.s, world_db, projector, intents

    def context(self, actor, kind):
        try:
            return {"promises": self.intents.kernel_context(actor, self.s.clock.now())}
        except Exception as e:
            print("[promises] context unavailable: " + type(e).__name__, flush=True)
            return {}

    def step(self):
        # Export runs after legacy projection in Authority.step, never inside Life.step.
        pass

    def errands(self, actor):
        return self.intents.errands(actor, self.s.clock.now())

    def convert(self, e):
        kind, payload = e["kind"], json.loads(e["payload"])
        action = self.s.db.execute("SELECT a.*,c.idempotency_key FROM actions a JOIN commands c USING(command_id)"
                                   " WHERE a.action_id=?", (e["action_id"],)).fetchone()
        args = json.loads(action["args"]) if action else {}
        plan_id = payload.get("plan_id") or args.get("party")
        if not plan_id and action and action["idempotency_key"].startswith("journey:"):
            plan_id = action["idempotency_key"].split(":")[1]
        plan = self.life.plans.plan(plan_id) if plan_id else None
        body = {"kind": kind, "actor": e["actor_id"], "plan_id": plan_id, "action_id": e["action_id"],
                "place": e["location"], "at": e["occurred_at"]}
        if kind == "purchased":
            if not payload.get("resource_id") or not plan:
                return None
            body.update({k: payload.get(k) for k in (
                "item", "qty", "resource_id", "account", "intent_ref", "beneficiary", "fingerprint")})
        elif kind == "brought_home":
            body.update(payload)
            body["kind"] = "brought_home"
        elif kind in ("arrived", "departed"):
            if not action or not plan or plan["kind"] not in ("outing", "trip"):
                return None
            if kind == "arrived":
                if args.get("to") != plan["location"] or e["location"] != plan["location"]:
                    return None
            else:
                start = self.s.db.execute("SELECT location FROM events WHERE action_id=? AND kind='action_started'",
                                          (e["action_id"],)).fetchone()
                if args.get("to") != "away" or not start or start[0] != plan["location"]:
                    return None
                body["at"] = action["started_at"]
                body["place"] = start[0]
        elif kind in ("action_succeeded", "action_cancelled"):
            if not action or action["action_type"] != "begin_activity":
                return None
            if plan and e["actor_id"] not in plan["members"]:
                return None
            start = self.s.db.execute("SELECT location FROM events WHERE action_id=? AND kind='action_started'",
                                      (e["action_id"],)).fetchone()
            if not start or action["started_at"] is None or action["finished_at"] is None:
                return None
            # Standalone actions get their own group. A second actor never inherits that group.
            body.update(kind="activity", activity=args["activity"], started_at=action["started_at"],
                        ended_at=action["finished_at"], outcome=action["state"], place=start[0],
                        plan_id=plan_id or "action:" + action["action_id"])
        elif kind in ("invite_answered", "invite_expired"):
            if not plan:
                return None
            # Reconstruct the membership at this event, even when exporting a backlog.
            # A partial yes must not let a two-person agreement claim a three-person plan.
            pending = self.s.db.execute(
                "SELECT 1 FROM invitations WHERE plan_id=? AND created_at<=? AND expires_at>?"
                " AND (answered_at IS NULL OR answered_at>?) LIMIT 1",
                (plan_id, e["occurred_at"], e["occurred_at"], e["occurred_at"])).fetchone()
            if pending:
                return None
            members = [plan["host"]] + [r[0] for r in self.s.db.execute(
                "SELECT invitee FROM invitations WHERE plan_id=? AND state='accepted' AND answered_at<=?",
                (plan_id,e["occurred_at"]))]
            if len(members) < 2:
                return None
            # This is a settled group, not a synthetic yes from the last declining/expired invitee.
            body.update(kind="invite_settled", members=sorted(set(members)), activity=plan["what"],
                        place=plan["location"], plan_kind=plan["kind"],
                        plan_start=plan["start_at"], plan_end=plan["end_at"] if plan["end_at"] is not None
                        else plan["start_at"] + 4 * 3600 if plan["start_at"] is not None else None)
        elif kind == "plan_finished":
            body["outcome"] = payload.get("outcome")
        else:
            return None
        return body

    def run(self):
        mode = os.environ.get("INTENTS_KERNEL_MATCH", "off")
        if mode in ("shadow", "on"):
            # Reuse the legacy projector's stream binding and synthetic/live separation.
            with closing(self.projector._conn()) as c:
                c.execute("begin immediate")
                prior_stream = self.life.get("promises:stream")
                if prior_stream and prior_stream != self.projector.world:
                    raise RuntimeError("promise cursor belongs to a different restored stream")
                cursor = self.life.get("promises:cursor")
                if cursor is None:
                    # First activation starts here. Historical replay is an explicit offline validation step.
                    cursor = self.s.db.execute("SELECT coalesce(max(event_id),0) FROM events").fetchone()[0]
                events = self.s.db.execute("SELECT * FROM events WHERE event_id>? ORDER BY event_id LIMIT 1000",
                                          (cursor,)).fetchall()
                for e in events:
                    body = self.convert(e)
                    if body is None:
                        continue
                    body["key"] = f"{self.projector.world}#{e['event_id']}"
                    body["stream"] = self.projector.world
                    c.execute("insert or ignore into intent_evidence(key,stream,event_id,at,body) values(?,?,?,?,?)",
                              (body["key"], self.projector.world, e["event_id"], body["at"],
                               json.dumps(body, ensure_ascii=False)))
                # Commit may raise: the cursor must stay at its previous value in that case.
                c.commit()
            hook = self.s.faults.get("promises:after_world_commit")
            if hook:
                hook()
            with self.s.tx():
                self.life.put("promises:cursor", events[-1]["event_id"] if events else cursor)
                self.life.put("promises:stream", self.projector.world)
        self.intents.sweep(self.s.clock.now())
