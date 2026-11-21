"""Fake-model cognition adapter of P1a (v1.1 §2/§4/§6/§16).

begin() builds her context for one audience (herself), reserves budget, and asks the fake model; the
answer arrives after its latency on the virtual clock, so the world keeps moving meanwhile and a late plan
can find its basis gone. finish() submits the first step with the generation and versions she saw; later
steps go one at a time after the previous one succeeds. At most 5 steps, one corrective retry per decision.

The context holds only what she perceived: her own body and place, what is lying in her room, what she
saw or heard (words only when she was a knower), and the offers she may see. Never anyone's private talk,
never the global state, never a bridge credential.

Knowing is not saying. Her deliberation (audience self:<her>) may plan to speak, but only as an intention:
to whom, and about which things (refs to materials or events she knows). The words are written in a second,
separate call whose context holds only what may be disclosed to that audience (the room, or a quiet word
between two), and only the refs that pass the same filter. A plan step that already carries words is
refused, so text formed with private knowledge never reaches a room. The kernel accepts a model's line only
with the audience it was expressed for.
"""
import json
import math

from .materials import allowed, context_materials
from .store import dumps

MAX_STEPS = 5
RECENT = 20
STALE = ("stale_generation", "stale_version", "resource_unavailable", "resource_not_found", "busy")
PLAN_TTL_S = 24 * 3600


class FakeModel:
    """script(context) -> {"steps": [...], "latency": seconds, "tokens": n} or raises."""

    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, context):
        self.calls.append(context)
        return self.script(context)


class Cognition:
    def __init__(self, kernel, budget, model, guide=None, routine=None, on_refused=None, before_step=None):
        self.k, self.s, self.budget, self.model = kernel, kernel.s, budget, model
        self.guide, self.routine = guide, routine
        self.on_refused = on_refused
        self.before_step = before_step  # (actor, step): her habit around a decided step, e.g. sleepwear (life1)
        self.pending = []  # [{"actor", "due", "answer"|"error", "reservation", "context", "retries", "kind"}]
        self.plans = {}  # actor -> {"steps": [...], "rid": str, "i": int, "action_id": str, "context": {...}}

        self._recover()

    def _fault(self, phase):
        hook = self.s.faults.get('cognition:' + phase)
        if hook:
            hook()

    def _expired(self, plan):
        return self.s.clock.now() > plan.get('expires_at', float('inf'))

    def _superseded(self, actor, action_id):
        return self.s.db.execute(
            "SELECT 1 FROM actions a JOIN commands c ON c.command_id=a.command_id WHERE a.actor_id=?"
            " AND a.state IN ('accepted','running','succeeded') AND c.rowid>(SELECT c2.rowid FROM commands c2"
            " JOIN actions a2 ON a2.command_id=c2.command_id WHERE a2.action_id=?) LIMIT 1",
            (actor, action_id)).fetchone() is not None

    def _request(self, rid, actor, purpose, state, payload):
        with self.s.tx() as db:
            db.execute('INSERT INTO cognition_requests VALUES(?,?,?,?,?,?) ON CONFLICT(request_id)'
                       ' DO UPDATE SET state=excluded.state,payload=excluded.payload',
                       (rid, actor, purpose, state, dumps(payload), self.s.epoch))

    def _save_plan(self, actor):
        with self.s.tx() as db:
            if actor in self.plans:
                db.execute('INSERT INTO cognition_plans VALUES(?,?) ON CONFLICT(actor_id)'
                           ' DO UPDATE SET payload=excluded.payload', (actor, dumps(self.plans[actor])))
            else:
                db.execute('DELETE FROM cognition_plans WHERE actor_id=?', (actor,))

    def _recover(self):
        """Saved answers resume; interrupted external calls count and are never repeated."""
        with self.s.tx() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cognition_requests (request_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, purpose TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, runtime_epoch INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS cognition_plans (actor_id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
            for r in db.execute("SELECT * FROM cognition_requests WHERE state='calling'").fetchall():
                if r['runtime_epoch'] == self.s.epoch:
                    continue
                job = json.loads(r['payload'])
                self.budget.unknown(job['reservation'], 'interrupted_call')
                db.execute("UPDATE cognition_requests SET state='unknown' WHERE request_id=?", (r['request_id'],))
                self._point(r['actor_id'], r['purpose'], 'interrupted_call', 'unknown')
            self.pending = [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM cognition_requests WHERE purpose='decide' AND state='ready' ORDER BY rowid")]
            self.plans = {r[0]: json.loads(r[1]) for r in db.execute('SELECT * FROM cognition_plans')}
            for actor, plan in list(self.plans.items()):
                key = f"express:{plan['rid']}:{plan['i']}"
                row = db.execute('SELECT state FROM cognition_requests WHERE request_id=?', (key,)).fetchone()
                if row and row[0] == 'unknown':
                    del self.plans[actor]
                    self._save_plan(actor)

    @staticmethod
    def _answer(ans):
        if not isinstance(ans, dict) or not isinstance(ans.get('steps', []), list):
            raise ValueError('invalid model answer')
        latency = ans.get('latency', 0)
        if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
            raise ValueError('invalid latency')
        if ans.get('tokens') is not None and (type(ans['tokens']) is not int or ans['tokens'] < 0):
            raise ValueError('invalid token count')
        for step in ans.get('steps', [])[:MAX_STEPS]:
            if (not isinstance(step, dict) or not isinstance(step.get('action_type'), str)
                    or any(not isinstance(step.get(k, []), list) for k in ('uses', 'about'))):
                raise ValueError('invalid model step')
        dumps(ans)
        return ans

    # ------------------------------------------------------------------ context
    def context(self, actor_id, audience=None):
        """Her context for one output audience (default: her own deliberation, self:<her>)."""
        audience = audience or f"self:{actor_id}"
        with self.s.read() as db:
            a = db.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
            here = self.k._present(db, a["location"])
            items = [dict(r) for r in db.execute(
                "SELECT resource_id, item, qty, version FROM resources WHERE qty>0 AND"
                " (holder=? OR (holder IS NULL AND location=?)) ORDER BY resource_id", (actor_id, a["location"]))]
            recent = []
            for r in db.execute(
                    "SELECT e.event_id, e.kind, e.actor_id, e.location, e.occurred_at, e.payload, e.disclosure_scope,"
                    " o.mode FROM observations o"
                    " JOIN events e ON e.event_id=o.event_id WHERE o.observer_id=? ORDER BY e.event_id DESC LIMIT ?",
                    (actor_id, RECENT)).fetchall():
                # what the owner himself said or handed over in her room may be answered to him alone too
                to_owner = audience.startswith("dm:") and "owner" in audience[3:].split(",") \
                    and r["kind"] in ("owner_input_received", "owner_gave")
                if (not audience.startswith("self:") and r["kind"] != "said" and not to_owner
                        and not allowed(r["disclosure_scope"], audience)):
                    continue  # speaking to a room: only what that room may hear about, not her own phone etc.
                item = {"event_id": r["event_id"], "kind": r["kind"], "actor": r["actor_id"], "location": r["location"],
                        "at": r["occurred_at"]}
                p = json.loads(r["payload"])
                if r["kind"] == "said":
                    if r["mode"] in ("heard", "self") and allowed(r["disclosure_scope"], audience):
                        item["text"], item["to"] = p.get("text"), p.get("to")
                    else:
                        item["kind"] = "talked_quietly"
                elif r["kind"] == "action_started":
                    item["activity"] = p.get("activity")
                elif r["kind"] == "owner_input_received":  # 房主化身: what he said in the room she is in
                    item["actor"] = "owner"
                    item["text"] = p.get("text")
                elif r["kind"] == "owner_gave":
                    item["actor"] = "owner"
                    item.update(to=p.get("to"), item=p.get("item"), label=p.get("label"), resource_id=p.get("resource_id"))
                elif r["kind"] in ("owner_entered", "owner_left_room", "owner_exited_avatar"):
                    item["actor"] = "owner"
                recent.append(item)
        ctx = {"actor": actor_id, "audience": audience, "vocab_version": self.k.V.VOCAB_VERSION,
               "generation": a["generation"], "location": a["location"], "activity": a["activity"],
               "body": json.loads(a["body"]), "here": [p for p in here if p != actor_id], "items_here": items,
               "recent": list(reversed(recent)), "offers": self.guide.offers(actor_id) if self.guide else [],
               "habits": self.routine.habit(actor_id)["version"] if self.routine else None,
               "materials": context_materials(self.k, actor_id, audience)}
        ctx["context_refs"] = [r["event_id"] for r in ctx["recent"]]
        return ctx

    # ------------------------------------------------------------------ decide
    def begin(self, actor_id, kind, retries=0):
        """Start one decision. Returns "asked" or "degraded" (no budget: the caller falls back to L0)."""
        with self.s.tx() as db:
            if not self.k.city_visits.home_allowed(actor_id):
                return "degraded"
            unresolved = db.execute("SELECT 1 FROM cognition_requests WHERE actor_id=? AND purpose='decide'"
                                    " AND state IN ('calling','unknown') AND json_extract(payload,'$.kind')=?",
                                    (actor_id, kind)).fetchone()
            if unresolved:
                self._point(actor_id, kind, 'unresolved_previous_call', 'degraded')
                return 'degraded'
            rid = self.budget.reserve(actor_id, f"decide:{kind}")
            if rid is None:
                self._point(actor_id, kind, "no_budget", "degraded")
                return "degraded"
            self.k.settle_bodies([actor_id])
            ctx = self.context(actor_id)
            ctx["domain_epoch"] = self.k.city_visits.authority(actor_id)["domain_epoch"]
            job = {"actor": actor_id, "kind": kind, "reservation": rid, "context": ctx, "retries": retries,
                   "expires_at": self.s.clock.now() + PLAN_TTL_S}
            self._request(rid, actor_id, 'decide', 'calling', job)
        self._fault('reserved')
        try:  # outside any transaction
            ans = self._answer(self.model(ctx))
            job["answer"], job["due"] = ans, self.s.clock.now() + float(ans.get("latency", 0))
        except Exception as e:  # unknown cost: keep counting it
            self.budget.unknown(rid, type(e).__name__)
            self._request(rid, actor_id, 'decide', 'unknown', job)
            self._point(actor_id, kind, f"model_error:{type(e).__name__}", "failed")
            return "failed"
        self._request(rid, actor_id, 'decide', 'ready', job)
        self._fault('answer_saved')
        self.pending.append(job)
        self._point(actor_id, kind, "asked", "called", [rid])
        return "asked"

    def run_due(self):
        """Deliver every answer whose latency has passed, oldest first. Returns submitted results."""
        out = []
        now = self.s.clock.now()
        for job in sorted([j for j in self.pending if j["due"] <= now], key=lambda j: j["due"]):
            with self.s.tx():
                self.budget.settle(job["reservation"], job["answer"].get("tokens"))
                self._request(job['reservation'], job['actor'], 'decide', 'done', job)
                steps = (job["answer"].get("steps") or [])[:MAX_STEPS]
                if (not self.k.city_visits.home_allowed(job["actor"])
                        or job["context"].get("domain_epoch", 0) !=
                        self.k.city_visits.authority(job["actor"])["domain_epoch"]):
                    steps = []
                if self._expired(job):
                    steps = []
                    self._point(job['actor'], job['kind'], 'answer_expired', 'replan_needed')
                if not steps:
                    # an answer whose steps could not be read is not her choosing nothing; its own
                    # outcome so the reports that count outcomes show it
                    if job["answer"].get("unusable"):
                        self._point(job["actor"], job["kind"], "answer_unusable", "unusable")
                    else:
                        self._point(job["actor"], job["kind"], "chose_nothing", "settled")
                else:
                    if job["answer"].get("unusable"):  # some steps dropped: say so, then still do the rest
                        self._point(job["actor"], job["kind"], "answer_partly_unusable", "partly_unusable")
                    self.plans[job["actor"]] = {"steps": steps, "rid": job["reservation"], "i": 0, "action_id": None,
                                                "context": job["context"], "retries": job["retries"], "kind": job["kind"],
                                                "expires_at": job.get('expires_at', now + PLAN_TTL_S)}
                    self._save_plan(job['actor'])
            self.pending.remove(job)
            if steps:
                out.append(self._submit_step(job["actor"], first=True))
        return out

    def advance_plans(self):
        """After ticks: start the next step of any plan whose current step succeeded; drop failed plans."""
        out = []
        for actor in list(self.plans):
            plan = self.plans[actor]
            if self._expired(plan) or (plan['action_id'] and self._superseded(actor, plan['action_id'])):
                self._point(actor, plan['kind'], 'expired_or_superseded', 'replan_needed')
                del self.plans[actor]
                self._save_plan(actor)
                continue
            if not plan["action_id"]:
                out.append(self._submit_step(actor, first=plan['i'] == 0))
                continue
            st = self.s.db.execute("SELECT state FROM actions WHERE action_id=?", (plan["action_id"],)).fetchone()[0]
            if st in ("accepted", "running"):
                continue
            if st != "succeeded":
                self._point(actor, plan["kind"], f"step_{st}", "replan_needed")
                del self.plans[actor]
                self._save_plan(actor)
                continue
            self._fault('completed_before_advance')
            plan["i"] += 1
            if plan["i"] >= len(plan["steps"]):
                del self.plans[actor]
                self._save_plan(actor)
                continue
            plan['action_id'] = None
            self._save_plan(actor)
            out.append(self._submit_step(actor, first=False))
        return out

    def _submit_step(self, actor, first):
        plan = self.plans[actor]
        if (not self.k.city_visits.home_allowed(actor)
                or plan["context"].get("domain_epoch", 0) != self.k.city_visits.authority(actor)["domain_epoch"]):
            del self.plans[actor]
            self._save_plan(actor)
            return {"status": "failed", "reason": "STALE_DOMAIN_EPOCH"}
        step, ctx = plan["steps"][plan["i"]], plan["context"]
        cmd = {"idempotency_key": f"model:{plan['rid']}:{plan['i']}", "principal": f"channel:{actor}",
               "actor_id": actor, "origin": "model", "action_type": step["action_type"], "args": step.get("args", {}),
               "guide_id": step.get("guide_id"), "goal_id": step.get("goal_id"), "interrupt": step.get("interrupt"),
               "context_refs": ctx["context_refs"], "vocab_version": ctx["vocab_version"]}
        cmd["domain_epoch"] = ctx.get("domain_epoch", 0)
        if step["action_type"] == "say":
            said = self.express(actor, step, plan)
            if "refused" in said:
                del self.plans[actor]
                self._save_plan(actor)
                self._point(actor, plan["kind"], f"say_refused:{said['refused']}", "replan_needed")
                return {"status": "failed", "reason": said["refused"]}
            cmd.update(args=said["args"], expressed_for=said["audience"], context_refs=said["context_refs"])
        if first:  # the first step stands on exactly what she saw; later steps re-read the world
            cmd["generation"] = ctx["generation"]
            seen = {i["resource_id"]: i["version"] for i in ctx["items_here"]}
            touch = [r for r in step.get("uses", ()) if r in seen]
            cmd["relevant_versions"] = {f"resource:{r}": seen[r] for r in touch}
        cmd = {k: v for k, v in cmd.items() if v is not None}
        # before the step's own command, so it never supersedes the step it serves; and only while what the step
        # stands on still holds, so a step refused as stale leaves no habit behind
        if self.before_step and self._still_stands(actor, cmd):
            try:
                shaped = self.before_step(actor, step)
            except Exception as e:  # a habit around her step never blocks the step she decided
                self._point(actor, plan["kind"], f"before_step_failed:{type(e).__name__}", "noted")
            else:
                if isinstance(shaped, dict) and step["action_type"] != "say":
                    # her habit reshaped the step (stopped in time for her next meal); kept in her plan,
                    # saved with the submission below, so a resubmission after a restart is the same command
                    step["args"] = cmd["args"] = shaped
        with self.s.tx():
            res = self.k.submit(cmd)
            self._fault('submitted_before_attach')
            plan['action_id'] = res.get('action_id')
            self._save_plan(actor)
        if res["status"] in ("failed", "conflict"):
            del self.plans[actor]
            self._save_plan(actor)
            if self.on_refused and self.on_refused(cmd, res, plan["kind"]):
                self._point(actor, plan["kind"], "waiting_for_cook_resources", "deferred")
                return res
            if first and res.get("reason") in STALE and plan["retries"] == 0:
                self._point(actor, plan["kind"], f"stale:{res.get('reason')}", "retry")
                self.begin(actor, plan["kind"], retries=1)
            else:
                self._point(actor, plan["kind"], f"refused:{res.get('reason')}", "replan_needed")
        else:
            plan["action_id"] = res.get("action_id")
        return res

    def _still_stands(self, actor, cmd):
        """Whether what her first step stood on still holds: her generation and the versions she relied on, the
        kernel's own staleness checks, read before anything is submitted. Later steps carry neither."""
        if "generation" in cmd and self.k.actor(actor)["generation"] != int(cmd["generation"]):
            return False
        try:
            with self.s.read() as db:
                self.k._check_versions(db, cmd.get("relevant_versions") or {})
        except Exception:  # Refused: a version she relied on has moved
            return False
        return True

    def express(self, actor, step, plan):
        """Write her line for one audience from that audience's context only. Returns {"args", "audience",
        "context_refs"} or {"refused": reason}. Costs one budgeted call, like any other model call."""
        args = step.get("args") or {}
        if not isinstance(args, dict):
            return {'refused': 'bad_args'}
        if "text" in args:
            return {"refused": "say_text_from_deliberation"}
        a = self.k.actor(actor)
        to = args.get("to")
        # review: a quiet word is to one roommate. Written the way an invitation's to is (a list), one name is
        # that one and an empty list (or an empty name) is the room, as no to is; anything else is refused here:
        # unchecked, sorted() in speech_audience raised and, uncaught, every tick re-raised it from this plan,
        # stopping the world until the plan expired.
        if isinstance(to, list) and len(to) <= 1:
            to = to[0] if to else None
        if to == "":
            to = None
        if to is not None and not isinstance(to, str):
            return {"refused": "bad_args"}
        audience = self.k.speech_audience(actor, a["location"], to)
        ctx = self.context(actor, audience)
        allowed_mats = {m["source_ref"] for m in ctx["materials"]}
        allowed_events = set(ctx["context_refs"])
        about = list(step.get("about") or [])
        for ref in about:
            kind, _, rid = str(ref).partition(":")
            ok = (kind == "material" and rid in allowed_mats) or (kind == "event" and rid.isdigit()
                                                                    and int(rid) in allowed_events)
            if not ok:
                return {"refused": "about_not_disclosable_here"}
        key = f"express:{plan['rid']}:{plan['i']}"
        saved = self.s.db.execute('SELECT state,payload FROM cognition_requests WHERE request_id=?', (key,)).fetchone()
        if saved:
            if saved['state'] != 'ready':
                return {'refused': 'expression_result_unknown'}
            result = json.loads(saved['payload'])['result']
            if 'refused' not in result and result['audience'] != audience:
                return {'refused': 'expression_audience_changed'}
            return result
        with self.s.tx():
            rid = self.budget.reserve(actor, f"express:{plan['kind']}")
            if rid is None:
                return {"refused": "no_budget"}
            self._request(key, actor, 'express', 'calling', {'reservation': rid})
        try:
            ans = self.model(dict(ctx, purpose="express", to=to, about=about))
        except Exception as e:
            self.budget.unknown(rid, type(e).__name__)
            self._request(key, actor, 'express', 'unknown', {'reservation': rid})
            return {"refused": f"model_error:{type(e).__name__}"}
        if (not isinstance(ans, dict) or (ans.get('tokens') is not None
                and (type(ans['tokens']) is not int or ans['tokens'] < 0))):
            self.budget.unknown(rid, 'invalid_expression')
            self._request(key, actor, 'express', 'unknown', {'reservation': rid})
            return {'refused': 'invalid_expression'}
        self.budget.settle(rid, ans.get("tokens"))
        text = ans.get("text")
        if not isinstance(text, str) or not text.strip():
            result = {"refused": "nothing_to_say"}
            self._request(key, actor, 'express', 'ready', {'reservation': rid, 'result': result})
            return result
        out = {"text": text}
        if to:
            out["to"] = to
        result = {"args": out, "audience": audience, "context_refs": ctx["context_refs"]}
        self._request(key, actor, 'express', 'ready', {'reservation': rid, 'result': result})
        return result

    def _point(self, actor_id, kind, reason, outcome, refs=()):
        with self.s.tx() as db:
            db.execute("INSERT INTO decision_points(actor_id,at,kind,reason,outcome,refs) VALUES(?,?,?,?,?,?)",
                       (actor_id, self.s.clock.now(), kind, reason, outcome, json.dumps(list(refs))))
