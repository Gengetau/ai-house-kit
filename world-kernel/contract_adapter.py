"""contract adapter for the kernel: runs world-contract-tests/cases.json against the real kernel and
writes traces/<case>.json in protocol nell-world-cases-v1 for oracle.py (-W7).

Every case starts from a fresh temporary database. Each step goes through a kernel entry point; the trace is
read back from the kernel's own tables (state, effects ledger, events, outbox, audits, summaries, views),
never from the case's expected values. Fixture-only operations (fixture_adjust, private_event, fault
injection) are marked as such. Durations come from the catalog, not from the case's "duration" fields.

  python3 contract_adapter.py --cases ../world-contract-tests/cases.json --out traces [--keep-db dir]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from worldkernel import admin, materials, outbox, summary, views  # noqa: E402
from worldkernel import vocab as V  # noqa: E402
from worldkernel.budget import Budget  # noqa: E402
from worldkernel.cognition import Cognition, FakeModel  # noqa: E402
from worldkernel.guide import Guide  # noqa: E402
from worldkernel.kernel import FORBIDDEN, Kernel  # noqa: E402
from worldkernel.store import Store, VirtualClock  # noqa: E402
from worldcatalog.fixtures import fixture_vocabulary  # noqa: E402  (importable once worldkernel.vocab is)

ROSTER = ("nell", "elsa", "alma", "mina", "yuki", "wanda")
T0 = 1792832400.0  # synthetic base time (2026-10-24 18:00 JST); case times are offsets from it
HOLDER = "contract-adapter"
FAR = 10 ** 9


class Crash(Exception):
    """Injected process death at a named transaction point."""


def http_status(res):
    st, why = res.get("status"), res.get("reason")
    if st == "conflict":
        return 409
    if why in FORBIDDEN:
        return 403
    return {"accepted": 202, "running": 202, "succeeded": 200}.get(st, 422)


def source_ref():
    try:
        return subprocess.check_output(["git", "-C", HERE, "rev-parse", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


class AuditedChannel(outbox.FakeChannel):
    """Fake executor that audits each real (fake) send; lookup can be cut to model a lost receipt."""

    def __init__(self, runner):
        super().__init__()
        self.runner, self.lookup_works = runner, True

    def send(self, delivery_id, payload):
        outcome = self.plan[0] if self.plan else "ok"
        if outcome in ("ok", "timeout_after_send"):
            self.runner.k.audit("fake_external_send", delivery=delivery_id)
        return super().send(delivery_id, payload)

    def lookup(self, delivery_id):
        return super().lookup(delivery_id) if self.lookup_works else None


class Runner:
    def __init__(self, case, workdir):
        self.case, self.init = case, case["initial"]
        self.policy = self.init.get("policy", {})
        self.path = os.path.join(workdir, f"{case['id']}.db")
        self.clock = VirtualClock(T0)
        self.cmds, self.ids, self.saved_ctx, self.saved_views = {}, {}, {}, {}
        self.guides, self.jobs, self.deliveries, self.calls = {}, {}, {}, []
        self.answers, self.stopped_at, self.down = {}, None, False
        self.channel = AuditedChannel(self)
        self.vocab = self.load_vocab()
        self.open(first=True)
        self.seed()

    def load_vocab(self):
        """casepack v2 binds every case to a registered fixture catalog; it is injected, never patched in."""
        prof = self.init.get("fixture_profile")
        if not prof:
            return V
        v = fixture_vocabulary(prof, world_id=self.init["world_id"], synthetic=self.init.get("synthetic"),
                               fake_channels=self.init.get("fake_channels"), paid_calls=self.policy.get("paid_calls"))
        if v.VOCAB_VERSION != self.init.get("vocab_version") or v.validate():
            raise ValueError(f"{self.case['id']}: fixture vocabulary does not match the case")
        return v

    # ---------------------------------------------------------------- lifecycle
    def open(self, first=False):
        self.store = Store(self.path, self.clock, world_id=self.init.get("world_id", "ai-house/p1a"),
                           stream_epoch=self.init.get("stream_epoch", "1"), holder=HOLDER)
        self.store.acquire()
        self.k = Kernel(self.store, ROSTER, vocab=self.vocab)
        self.guide = Guide(self.store, kernel=self.k)
        self.budget = Budget(self.store)
        self.model = FakeModel(self._answer)
        self.cog = Cognition(self.k, self.budget, self.model, self.guide)
        self.dispatch = outbox.Dispatcher(self.store, {"phone": self.channel})
        if not first:
            self.channel.runner = self

    def close(self):
        self.store.close()

    def seed(self):
        actors = {a: {"location": v["location"], "generation": v.get("generation", 0)}
                  for a, v in self.init["actors"].items()}
        if "resource_rows" in self.init:  # v2: the case's authoritative rows
            res = [{"id": r["resource_id"], "kind": r["kind"], "item": r["item"], "location": r.get("location"),
                    "holder": r.get("holder"), "qty": r["qty"]} for r in self.init["resource_rows"]]
        else:
            res = [{"id": item, "kind": self.vocab.CATALOG["items"].get(item, {}).get("kind", "food"), "item": item,
                    "location": "kitchen", "qty": q} for item, q in self.init.get("inventory", {}).items()]
            res += [{"id": a, "kind": "anchor", "item": a, "location": "kitchen", "qty": 1}
                    for a in self.init.get("resources", ())]
        self.k.seed({"actors": actors, "resources": res}, source="synthetic_fixture")
        self.seed_event = self.store.db.execute("SELECT MAX(event_id) FROM events").fetchone()[0]

    def _answer(self, ctx):
        job = self.answers.pop(ctx["actor"])
        self.calls.append({"actor": ctx["actor"], "paid": False, "purpose": job["purpose"]})
        return {"steps": job["steps"], "latency": FAR, "tokens": 0}

    def at(self, t):
        target = T0 + float(t)
        if target > self.clock.now():
            self.clock.t = target
        if self.down:
            return
        self.k.expire_presence()
        self._safe(self.k.tick)
        self.cog.advance_plans()

    def _safe(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Crash:
            self.crashed = True
            return None

    # ---------------------------------------------------------------- steps
    def run(self):
        out = {}
        for step in self.case["steps"]:
            self.at(step["at"])
            op = step["op"]
            getattr(self, "op_" + op)(step) if op != "checkpoint" else out.__setitem__(step["name"], self.evidence(step))
        self.close()
        return out

    def op_offer(self, st):
        self.guides[st["guide_id"]] = self.guide.offer_now(st["guide_id"])

    def op_model_decision(self, st):
        a, choice = st["actor"], st["choice"]
        if choice in ("accept_guide", "decline_guide"):
            r = self.guide.respond(a, self.guides[st["guide_id"]], "adopt" if choice == "accept_guide" else "decline",
                                   f"channel:{a}")
        elif choice == "read_phone":
            mid = (st.get("args") or {}).get("message_id", st.get("message_id"))
            if "read_phone" in self.k.V.ACTIONS:  # registered fixture: a real kernel action with its own guard
                r = self.k.submit({"idempotency_key": f"decision:{a}:{st['at']}", "principal": f"channel:{a}",
                                   "actor_id": a, "origin": "model", "action_type": "read_phone",
                                   "args": {"message_id": mid}})
            else:
                r = materials.read_message(self.k, a, mid, f"channel:{a}")
        else:
            r = {"status": "failed", "reason": "unknown_choice"}
        self.cmds[f"decision:{a}:{st['at']}"] = dict(r, http_status=http_status(r))

    def _cmd(self, st):
        action = st["action"]
        args = {}
        if "args" in st:  # v2: canonical arguments, passed through untouched
            args = dict(st["args"])
        elif action in ("eat",):
            args["item"] = st.get("resource")
        elif action in ("prepare_food", "cook"):
            args["recipe"] = st.get("recipe", st.get("resource"))
        elif action in ("move", "move_to"):
            args["to"] = st.get("destination")
        elif action == "say":
            args["text"] = st.get("text", "")
        elif action in ("wait", "rest", "sleep"):
            args["seconds"] = st.get("seconds", st.get("duration"))
        cmd = {"idempotency_key": st.get("idem", st["id"]), "principal": st.get("identity", f"channel:{st['actor']}"),
               "actor_id": st["actor"], "origin": "self", "action_type": action, "args": args}
        if st.get("interrupt"):
            cmd["interrupt_policy"] = st["interrupt"]  # refused by the kernel unless it equals the catalog
        return cmd

    def op_command(self, st):
        action, ident = st["action"], st.get("identity", f"channel:{st['actor']}")
        if ident.startswith("legacy:"):
            r = admin.legacy_write(self.k, ident)
        elif action == "inventory_adjust":
            r = admin.correction(self.k, ident, st["resource"], st["quantity"], reason="contract fixture",
                                 witnessed=st.get("witnessed", False))
        elif action == "memory_write":
            r = admin.export_memory(self.k, st.get("target_world"), st["actor"], st.get("text"))
        else:
            cmd = self._cmd(st)
            try:
                r = self.k.submit(cmd)
            except Crash:
                r = {"status": "unknown", "reason": "response_lost"}
        if r.get("command_id"):
            self.ids.setdefault(r["command_id"], st["id"])
        r = dict(r, http_status=http_status(r), canonical_id=self.ids.get(r.get("command_id"), st["id"]))
        self.cmds[st["id"]] = r

    def op_fixture_adjust(self, st):
        for rid, q in st.get("inventory", {}).items():  # test-only race injection, generation untouched
            self.store.db.execute("UPDATE resources SET qty=?, version=version+1 WHERE resource_id=?", (q, rid))

    def op_model_begin(self, st):
        plan = st["plan"]
        if "args" in plan:
            step = {"action_type": plan["action"], "args": dict(plan["args"]),
                    "uses": [plan["args"].get("resource_id") or plan["args"].get("item")]}
        else:
            step = {"action_type": plan["action"], "uses": [plan.get("resource")]}
            if plan["action"] == "eat":
                step["args"] = {"item": plan["resource"]}
        self.answers[st["actor"]] = {"steps": [step], "purpose": st["job"]}
        self.cog.begin(st["actor"], st["job"])
        self.jobs[st["job"]] = {"actor": st["actor"], "plan_id": plan["id"],
                                "reservation": self.cog.pending[-1]["reservation"] if self.cog.pending else None}

    def op_model_return(self, st):
        job = self.jobs[st["job"]]
        for p in self.cog.pending:
            if p["reservation"] == job["reservation"]:
                p["due"] = self.clock.now()
        self.cog.run_due()
        row = self.store.db.execute("SELECT * FROM commands WHERE idempotency_key=?",
                                    (f"model:{job['reservation']}:0",)).fetchone()
        if row:
            r = self.k.command(row["command_id"])
            self.ids.setdefault(row["command_id"], job["plan_id"])
            self.cmds[job["plan_id"]] = dict(r, http_status=http_status(r), canonical_id=job["plan_id"])

    def op_owner_talk(self, st):
        r = admin.owner_input(self.k, "talk", st.get("text"), "cmd:owner")
        self.cmds[st.get("id", f"owner:{st['at']}")] = dict(r, http_status=http_status(r))

    def op_phone_receive(self, st):
        materials.phone_receive(self.k, st["actor"], st["message_id"], st["scope"], st["text"])

    def op_observe(self, st):
        materials.observe(self.k, st["actor"], st["source"], st["scope"], st["text"])

    def op_build_context(self, st):
        self.saved_ctx[st["save_as"]] = self.cog.context(st["actor"], st["audience"])

    def op_generate_outputs(self, st):
        self.output_audiences = st["audiences"]

    def op_private_event(self, st):  # fixture: something only she knows, in her own scope
        with self.store.tx():
            self.k._emit("private_note", st["actor"], None, None, {"text": st["text"]}, scope=f"self:{st['actor']}",
                         occurred=self.clock.now(), observers=(), knowers=[st["actor"]], source_kind="fixture")

    def op_fake_delivery(self, st):
        # a receipt-loss fault declared for this delivery strikes during its send (armed before sending)
        if any(x["op"] == "fault" and x.get("delivery_id") == st["id"] and x["kind"] == "external_executed_receipt_lost"
               for x in self.case["steps"]):
            self.channel.plan, self.channel.lookup_works = ["timeout_after_send"], False
        with self.store.tx():
            eid = self.k._emit("timer", None, None, None, {}, scope="system", occurred=self.clock.now(), observers=())
            self.deliveries[st["id"]] = outbox.request_delivery(self.store, eid, "phone", "timer_done", st["actor"])
        self.dispatch.run_once()

    def op_fault(self, st):
        kind = st["kind"]
        if kind == "external_executed_receipt_lost":
            pass  # armed in op_fake_delivery: the send happened, its answer and any later lookup are lost
        elif kind in ("before_commit", "after_commit_before_response"):
            point = "before_commit" if kind == "before_commit" else "after_commit"
            name = f"{st['transaction']}:{point}"

            def boom():
                self.store.faults.pop(name, None)
                raise Crash(name)
            self.store.faults[name] = boom

    def op_restart(self, st):
        self.close()
        self.open()
        self.dispatch.reconcile()
        self._safe(self.k.tick)

    def op_receipt(self, st):
        did = self.deliveries[st["delivery_id"]]
        status = {"succeeded": "delivered"}.get(st["status"], st["status"])
        epoch = (self.store.epoch - 1) if st.get("epoch") == "previous" else self.store.epoch
        self.dispatch.receive(did, status, f"ext-{did}", epoch)

    def op_stop_service(self, st):
        self.stopped_at, self.down = self.clock.now(), True
        self.close()

    def op_resume_service(self, st):
        self.down = False
        self.open()
        self.k.resume(self.stopped_at)

    def op_snapshot(self, st):
        self.saved_views[st["save_as"]] = views.snapshot(self.store, ROSTER, self.k.policy)

    def op_events_after_snapshot(self, st):
        snap = self.saved_views[st["snapshot"]]
        got = views.events(self.store, self.k.policy, snap["cursor"])
        loc = {a: (v or {}).get("location") for a, v in snap["actors"].items()}
        for e in got["events"]:  # apply what the client consumed, not the latest internal state
            if e["kind"] == "arrived":
                loc[e["actor"]] = e["location"]
        mover = next((c["actor"] for c in self.case["steps"] if c["op"] == "command"), "nell")
        self.saved_views[st["save_as"]] = {"events": [dict(e, id=e["seq"]) for e in got["events"]],
                                           "cursor": got["cursor"], "final_locations": loc,
                                           "final_location": loc.get(mover)}

    def op_query_cursor(self, st):
        c = st["cursor"]
        cur = f"{c['world_id']}|{c['stream_epoch']}|{self.k.policy.audience}|{c['seq']}"
        try:
            views.events(self.store, self.k.policy, cur)
            code = 200
        except views.Gone:
            code = 410
        except views.CursorMismatch:
            code = 409
        self.cmds[st["id"]] = {"status": "read", "http_status": code}

    def op_summarize(self, st):
        actions = [self.cmds[c].get("action_id") for c in st["source_commands"] if c in self.cmds]
        ids = [r[0] for a in actions if a for r in self.store.db.execute(
            "SELECT event_id FROM events WHERE action_id=? ORDER BY event_id", (a,))]
        sid = summary.summarize(self.k, st["scene"], st["audience"], ids, st["trigger"],
                                growth_rule=self.policy.get("synthetic_growth_rule"))
        self.summary_sources = getattr(self, "summary_sources", {})
        if sid:
            self.summary_sources[sid] = st["source_commands"]

    def op_owner_enter(self, st):
        self.k.owner_presence("owner-1", "enter", st["location"], lease=float(self.policy.get("owner_presence_ttl", 90)))

    def op_client_disconnect(self, st):
        pass  # the client simply stops sending heartbeats; the lease runs out on its own

    # ---------------------------------------------------------------- evidence
    def evidence(self, st):
        if self.down:
            raise RuntimeError("checkpoint while stopped")
        db = self.store.db
        for cid, r in list(self.cmds.items()):
            if r.get("command_id"):
                cur = self.k.command(r["command_id"])
                if cur:
                    self.cmds[cid] = dict(r, status=cur["status"], reason=cur.get("reason"))
        actors = {r["actor_id"]: {"location": r["location"], "activity": r["activity"], "generation": r["generation"],
                                  "availability": r["availability"]}
                  for r in db.execute("SELECT * FROM actors WHERE simulated=1")}
        inventory = {r["resource_id"]: r["qty"] for r in db.execute("SELECT * FROM resources WHERE kind='food'")}
        events = [{"id": r["event_id"], "kind": r["kind"], "actor": r["actor_id"], "scope": r["disclosure_scope"]}
                  for r in db.execute("SELECT * FROM events WHERE event_id>? ORDER BY event_id", (self.seed_event,))]
        effects = []
        for r in db.execute("SELECT effect_key FROM effects ORDER BY applied_at, effect_key"):
            k = r[0]
            parts = k.split(":")
            if parts[0] == "growth":
                effects.append({"effect_key": k, "kind": "growth", "resource": parts[2]})
            elif len(parts) >= 3:
                effects.append({"effect_key": k, "kind": parts[1], "resource": parts[2]})
            else:
                effects.append({"effect_key": k, "kind": parts[-1], "resource": None})
        state = {"sent": "succeeded"}
        deliveries = [{"id": next((k for k, v in self.deliveries.items() if v == r["delivery_id"]), r["delivery_id"]),
                       "status": state.get(r["state"], r["state"])} for r in db.execute("SELECT * FROM outbox")]
        for r in db.execute("SELECT DISTINCT delivery_id FROM receipts WHERE status='delivered'"):
            for d in deliveries:
                if self.deliveries.get(d["id"]) == r[0]:
                    d["status"] = "succeeded"
        audits = [{"kind": r["kind"]} for r in db.execute("SELECT kind FROM audits ORDER BY audit_id")]
        snap = views.snapshot(self.store, ROSTER, self.k.policy)
        offer = db.execute("SELECT * FROM scene_offers ORDER BY created_at DESC LIMIT 1").fetchone()
        game = {"participants": json.loads(offer["takers"]) if offer else [],
                "guide_state": offer["state"] if offer else None, "snapshot": snap}
        owner = db.execute("SELECT state FROM owner_presence ORDER BY updated_at DESC LIMIT 1").fetchone()
        vws = dict(self.saved_views, game=game, owner={"presence": owner["state"] if owner else "unknown"})
        contexts = {a: self.cog.context(a) for a in actors}
        contexts.update(self.saved_ctx)
        sums = []
        for srow in summary.summaries(self.k):
            sums.append({"summary_id": srow["summary_id"], "source_event_ids": srow["source_event_ids"],
                         "source_commands": getattr(self, "summary_sources", {}).get(srow["summary_id"], []),
                         "audience": srow["audience"], "text": srow["text"], "triggers": srow["triggers"]})
        outputs = self.outputs(sums)
        return {"clock": self.clock.now() - T0, "actors": actors, "inventory": inventory, "events": events,
                "contexts": contexts, "outputs": outputs, "effects": effects, "deliveries": deliveries,
                "calls": list(self.calls), "invites": {}, "commands": dict(self.cmds), "views": vws,
                "summaries": sums, "audits": audits}

    def outputs(self, sums):
        """What each exit would carry, made by the same filters the kernel uses for that audience."""
        game = views.events(self.store, self.k.policy, 0, limit=50)["events"]
        out = {"game": json.dumps(game, ensure_ascii=False),
               "public": "\n".join(s["text"] for s in sums if s["audience"] == "public"),
               "script": "\n".join(s["text"] for s in sums if s["audience"] == "public"),  # legacy script = public
               "shared_summary": "\n".join(s["text"] for s in sums if s["audience"] == "shared"),
               "live_memory": ""}  # P1a has no live memory writer at all
        for name, ctx in self.saved_ctx.items():
            # the fake model's reply toward that audience echoes the materials it was allowed to use
            out[name] = "\n".join(m["text"] for m in ctx["materials"])
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=os.path.join(HERE, "..", "world-contract-tests", "cases.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "traces"))
    ap.add_argument("--keep-db")
    a = ap.parse_args()
    corpus = json.load(open(a.cases, encoding="utf-8"))
    os.makedirs(a.out, exist_ok=True)
    work = tempfile.mkdtemp(prefix="wk-contract-")
    ref = source_ref()
    for case in corpus["cases"]:
        build = "unknown"
        try:
            runner = Runner(case, work)
            build = runner.vocab.VOCAB_VERSION
            cps = runner.run()
            err = None
        except Exception as e:  # an adapter gap is reported, never papered over
            cps, err = {}, f"{type(e).__name__}: {e}"
        trace = {"protocol": corpus["protocol"], "case_id": case["id"], "synthetic": True,
                 "adapter": {"name": "world-kernel-adapter", "build": build, "source_ref": ref},
                 "checkpoints": cps}
        if err:
            trace["adapter_error"] = err
        with open(os.path.join(a.out, f"{case['id']}.json"), "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, indent=1, default=str)
        print(case["id"], "ERROR " + err if err else "ok")
    if a.keep_db:
        shutil.copytree(work, a.keep_db, dirs_exist_ok=True)
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
