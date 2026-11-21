"""厨房一晚 (v1.1 §17 / review M1–M2): a whole synthetic evening on the P1a kernel, and the R1/R2
recording that Godot stub plays.

18:00 to 07:30 JST of a synthetic day. 奈尔 and 婉妲 think with a scripted fake model (zero cost, budget still
counted); the other four live on L0 habits only. The Guide opens dinner and the evening living room; nobody
is cast. 婉妲 plans for the last cake, loses it to 米娜's routine meal and re-plans once. The owner's avatar
comes into the kitchen at 19:00, says one thing, and then its client drops, so presence becomes unknown.
A phone message reaches 奈尔 while she is cooking; she reads it only once her hands are free.

Everything goes through kernel entry points; the recording is read back with the same views.snapshot and
views.events the game would call. verify() replays the R2 pages over the first R1 snapshot (and over a
mid-evening one) and must land exactly on the final R1 snapshot.

  python3 kitchen_night.py --out recordings/kitchen-night
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile

from worldkernel import admin, materials, views
from worldkernel.budget import Budget
from worldkernel.cognition import Cognition, FakeModel
from worldkernel.guide import Guide, jst_minute
from worldkernel.habits import HabitBook
from worldkernel.kernel import Kernel
from worldkernel.routine import Routine
from worldkernel.store import Store, VirtualClock

ROSTER = ("nell", "elsa", "alma", "mina", "yuki", "wanda")
THINKERS = ("nell", "wanda")
T0 = 1792832400.0  # 2026-10-24 18:00 JST (synthetic)
END = T0 + 13.5 * 3600  # 07:30 JST next morning
STEP = 30
WORLD = "synthetic-kitchen-night"
SEED = 51
PROTOCOL = "kitchen-night/1"

FIXTURE = {
    "actors": {
        "nell": {"location": "kitchen", "body": {"energy": 70, "hunger": 60}, "profile_ref": "persona:nell"},
        "wanda": {"location": "living", "body": {"energy": 55, "hunger": 72}, "profile_ref": "persona:wanda"},
        "mina": {"location": "living", "body": {"energy": 80, "hunger": 76}, "profile_ref": "persona:mina"},
        "elsa": {"location": "living", "body": {"energy": 60, "hunger": 40}, "profile_ref": "persona:elsa"},
        "alma": {"location": "living", "body": {"energy": 45, "hunger": 50}, "profile_ref": "persona:alma"},
        "yuki": {"location": "exit_rooms", "body": {"energy": 30, "hunger": 35}, "profile_ref": "persona:yuki"},
    },
    "resources": [
        {"id": "egg", "kind": "food", "location": "kitchen", "qty": 2},
        {"id": "tofu", "kind": "food", "location": "kitchen", "qty": 1},
        {"id": "miso", "kind": "food", "location": "kitchen", "qty": 1},
        {"id": "rice", "kind": "food", "location": "kitchen", "qty": 2},
        {"id": "stove", "kind": "anchor", "location": "kitchen", "qty": 1},
        {"id": "pan", "kind": "anchor", "location": "kitchen", "qty": 1},
        {"id": "pot", "kind": "anchor", "location": "kitchen", "qty": 1},
        {"id": "rice_cooker", "kind": "anchor", "location": "kitchen", "qty": 1},
        {"id": "cake", "kind": "food", "location": "living", "qty": 1},
    ],
}

# Synthetic persona profiles (P1a): the habit ranges each persona would yield, never persona text.
PROFILES = {
    "nell": {"sleep": "23:30-07:30", "hunger_eat_at": 70, "rest_at_energy": 20},
    "wanda": {"sleep": "00:30-08:30", "hunger_eat_at": 80, "rest_at_energy": 20},
    "mina": {"sleep": "03:30-11:30", "hunger_eat_at": 75, "rest_at_energy": 15},
    "elsa": {"sleep": "23:00-07:00", "hunger_eat_at": 75, "rest_at_energy": 20},
    "alma": {"sleep": "22:30-06:30", "hunger_eat_at": 75, "rest_at_energy": 25},
    "yuki": {"sleep": "01:00-09:00", "hunger_eat_at": 75, "rest_at_energy": 35},
}


def jst(t):
    m = jst_minute(t)
    return f"{m // 60:02d}:{m % 60:02d}:{int(t) % 60:02d}"


class Script:
    """The scripted fake model: reads only the context the kernel built for her and answers with a short
    plan. It never decides for anyone else and knows nothing the context does not carry."""

    def __init__(self):
        self.tries = {}

    def __call__(self, ctx):
        if ctx.get("purpose") == "express":  # writing the line, from what this audience may hear only
            return {"text": "汤好了，一起喝吧？", "tokens": 0}
        me, kind = ctx["actor"], ctx["decision"]
        items = {i["item"]: i for i in ctx["items_here"]}
        dinner = next((o for o in ctx["offers"] if o["guide_id"].startswith("guide-dinner")), None)
        steps = []
        if me == "nell" and kind == "dinner" and dinner and ctx["location"] == "kitchen":
            steps = [{"action_type": "cook", "args": {"recipe": "miso_soup"}, "guide_id": dinner["guide_id"],
                      "uses": ["tofu", "miso"]}]
        elif me == "nell" and kind == "soup_ready" and "miso_soup" in items:
            here = [p for p in ctx["here"] if p != "owner"]
            if "wanda" in here:
                steps.append({"action_type": "say", "args": {"to": "wanda"}, "about": []})  # intention only
            steps.append({"action_type": "eat", "args": {"item": "miso_soup",
                                                          "resource_id": items["miso_soup"]["resource_id"]}})
        elif me == "wanda" and kind == "hungry":
            n = self.tries[me] = self.tries.get(me, 0) + 1
            if "cake" in items and n == 1:
                steps = [{"action_type": "eat", "args": {"item": "cake", "resource_id": "cake"}, "uses": ["cake"]}]
            else:  # the cake plan went stale: walk over to where dinner is happening
                steps = [{"action_type": "move", "args": {"to": "kitchen"},
                          **({"guide_id": dinner["guide_id"]} if dinner else {})}]
        elif me == "wanda" and kind == "invited" and "miso_soup" in items:
            steps = [{"action_type": "eat", "args": {"item": "miso_soup",
                                                      "resource_id": items["miso_soup"]["resource_id"]}}]
        return {"steps": steps, "latency": 30 if me == "wanda" else 20, "tokens": 0}


class Night:
    def __init__(self, workdir):
        self.clock = VirtualClock(T0)
        self.store = Store(os.path.join(workdir, "night.db"), self.clock, world_id=WORLD, holder="kitchen-night",
                           stream_epoch="night-1", seed=SEED)
        self.store.acquire()
        self.k = Kernel(self.store, ROSTER)
        self.k.seed(FIXTURE)
        self.book = HabitBook(self.k)
        for a, p in PROFILES.items():
            self.book.install(dict(p, actor_id=a, profile_ref=f"persona:{a}", profile_version="synthetic-1"))
        self.guide = Guide(self.store, kernel=self.k)
        self.routine = Routine(self.k, book=self.book)
        self.script = Script()
        self.model = FakeModel(self._ask)
        self.cog = Cognition(self.k, Budget(self.store), self.model, self.guide, self.routine)
        self.asked, self.notes, self.mid = set(), [], None
        self.owner_beat_until = None

    def _ask(self, ctx):
        return self.script(dict(ctx, decision=self._kind))

    def think(self, actor, kind):
        if (actor, kind) in self.asked:
            return
        self.asked.add((actor, kind))
        self._kind = kind
        self.cog.begin(actor, kind)
        self.note(actor, f"想了想（{kind}，假模型）")

    def note(self, who, text):
        self.notes.append((self.clock.now(), who, text))

    def busy(self, actor):
        return bool(self.k.actor(actor)["action_id"]) or actor in self.cog.plans or any(
            j["actor"] == actor for j in self.cog.pending)

    def scripted_world(self, now):
        """The owner and the outside world, as fake inputs at fixed times."""
        if now == T0 + 600:
            materials.phone_receive(self.k, "nell", "m1", "owner_private", "房主：今晚有比赛，晚点回")
            self.note("nell", "手机震了一下（正文未读）")
        if now == T0 + 3600:
            self.k.owner_presence("avatar-1", "enter", "kitchen")
            self.owner_beat_until = T0 + 3600 + 1200
            self.note("owner", "房主化身进了厨房")
            return [("owner_entered", "kitchen", "avatar-1")]
        if now == T0 + 3600 + 300:
            admin.owner_input(self.k, "talk", "好香啊", "cmd:owner")
            self.note("owner", "房主说：好香啊")
        if self.owner_beat_until and now < self.owner_beat_until and int(now - T0) % 60 == 0 and now > T0 + 3600:
            self.k.owner_presence("avatar-1", "heartbeat")
        if self.owner_beat_until and now == self.owner_beat_until:
            self.note("owner", "客户端断线（心跳停止）")
        return []

    def step(self):
        now = self.clock.now()
        triggers = self.scripted_world(now)
        self.guide.run(triggers)
        self.k.expire_presence()
        self.k.tick()
        self.cog.run_due()
        self.k.tick()
        self.cog.advance_plans()
        # what she saw decides whether she thinks; nothing here picks for her
        offers = [o["guide_id"] for o in self.guide.offers("nell")]
        if any(g.startswith("guide-dinner") for g in offers) and not self.busy("nell"):
            self.think("nell", "dinner")
        if self.k.actor("wanda")["body"]["hunger"] >= 72 and not self.busy("wanda"):
            self.think("wanda", "hungry")
        ctx_nell = self.cog.context("nell")
        if not self.busy("nell") and any(i["item"] == "miso_soup" for i in ctx_nell["items_here"]):
            self.think("nell", "soup_ready")
        if not self.busy("nell") and ("nell", "read_m1") not in self.asked and ("nell", "dinner") in self.asked:
            self.asked.add(("nell", "read_m1"))
            r = materials.read_message(self.k, "nell", "m1", "channel:nell")  # her own reading at a safe point
            self.note("nell", f"空下手来看了手机（{r['status']}）")
        heard = [e for e in self.cog.context("wanda")["recent"] if e["kind"] == "said" and e.get("to") == "wanda"]
        if heard and not self.busy("wanda"):
            self.think("wanda", "invited")
        for a in ROSTER:
            if not self.busy(a):
                self.routine.check(a)
        if self.mid is None and now >= T0 + 3600 + 600:
            self.mid = views.snapshot(self.store, ROSTER, self.k.policy)

    def run(self):
        first = views.snapshot(self.store, ROSTER, self.k.policy)
        while self.clock.now() <= END:
            self.step()
            self.clock.advance(STEP)
        self.clock.t = END
        final = views.snapshot(self.store, ROSTER, self.k.policy)
        pages, cur = [], first["cursor"]
        while True:
            page = views.events(self.store, self.k.policy, cur, limit=50)
            if not page["events"]:
                break
            pages.append({"after": cur, "cursor": page["cursor"], "events": page["events"]})
            cur = page["cursor"]
        return first, self.mid, final, pages


def verify(first, mid, final, pages):
    """R1/R2 contract checks the Godot stub relies on. Returns a list of problems (empty = ok)."""
    bad = []
    events = [e for p in pages for e in p["events"]]
    seqs = [e["seq"] for e in events]
    if seqs != list(range(first["view_revision"] + 1, first["view_revision"] + 1 + len(seqs))):
        bad.append("R2 seq is not contiguous after the R1 cursor")
    if any(len(p["events"]) > 50 for p in pages):
        bad.append("R2 page over 50")
    for p, nxt in zip(pages, pages[1:]):
        if p["cursor"] != nxt["after"]:
            bad.append("R2 cursor chain broken")
    if events and final["view_revision"] != seqs[-1]:
        bad.append("final R1 revision is not the last R2 seq")
    want = views.public_state(final)  # every public R1 field: whereabouts, owner presence, scenes
    if views.apply_events(first, events) != want:
        bad.append("R1(first)+R2 does not reach R1(final)")
    if mid is not None:
        rest = [e for e in events if e["seq"] > mid["view_revision"]]
        if views.apply_events(mid, rest) != want:
            bad.append("R1(mid)+R2 does not reach R1(final)")
    for s in (mid, final):  # snapshot-only fields have no P1a writer; if one ever changes, R2 cannot follow it
        if s is None:
            continue
        for a, v in s["actors"].items():
            v0 = first["actors"].get(a)
            if v and v0 and any(v[f.split(".")[1]] != v0[f.split(".")[1]] for f in views.SNAPSHOT_ONLY):
                bad.append(f"snapshot-only field changed for {a}: R2 has no delta for it")
    for s in (first, mid, final):
        if s and (s["world_id"] != WORLD or not s["synthetic"] or not s["cursor"].startswith(f"{WORLD}|night-1|game|")):
            bad.append("snapshot identity")
    text = json.dumps(pages, ensure_ascii=False)
    for secret in ("今晚有比赛", "persona text"):
        if secret in text:
            bad.append(f"private text in R2: {secret}")
    return bad


def timeline(night, final):
    db = night.store.db
    rows = []
    for r in db.execute("SELECT event_id, kind, actor_id, location, occurred_at, payload FROM events ORDER BY event_id"):
        p = json.loads(r["payload"])
        what = {"action_started": f"开始 {p.get('action_type')}（{p.get('activity')}，{p.get('origin')}）",
                "departed": f"出发去 {p.get('to')}", "arrived": "到了", "action_succeeded": f"完成 {p.get('action_type')}",
                "action_cancelled": f"中止 {p.get('action_type')}：{p.get('reason')}",
                "action_failed": f"失败 {p.get('action_type')}：{p.get('reason')}",
                "said": f"说：{p.get('text')}" + (f"（对 {p.get('to')}）" if p.get("to") else ""),
                "guide_offered": f"引导：{p.get('rule')}"}.get(r["kind"], r["kind"])
        rows.append((r["occurred_at"], r["actor_id"] or "-", r["location"] or "-", what))
    rows += [(t, who, "-", f"※ {text}") for t, who, text in night.notes]
    for p in db.execute("SELECT actor_id, at, kind, reason, outcome FROM decision_points"
                        " WHERE outcome NOT IN ('called', 'routine_action') ORDER BY point_id"):
        rows.append((p["at"], p["actor_id"], "-", f"※ 决策点 {p['kind']}：{p['reason']} → {p['outcome']}"))
    rows.sort(key=lambda x: x[0])
    used = night.cog.budget.used()
    points = db.execute("SELECT outcome, COUNT(*) FROM decision_points GROUP BY outcome ORDER BY outcome").fetchall()
    out = ["# 厨房一晚（合成回放）", "",
           f"世界 `{WORLD}`，18:00–07:30 JST，假模型、零付费。模型调用 {used} 次（预算上限 60）。", "",
           "决策点：" + "，".join(f"{o} {n}" for o, n in points), "", "| 时间 | 谁 | 在哪 | 发生了什么 |",
           "| --- | --- | --- | --- |"]
    out += [f"| {jst(t)} | {a} | {loc} | {w} |" for t, a, loc, w in rows if w != "world_seeded"]
    out += ["", "## 早上 07:30", ""]
    out += [f"- {a}：{v['location']}，{v['activity']}" for a, v in final["actors"].items() if v]
    return "\n".join(out) + "\n"


def record(out_dir, source_ref=None):
    work = tempfile.mkdtemp(prefix="kitchen-night-")
    try:
        night = Night(work)
        first, mid, final, pages = night.run()
        problems = verify(first, mid, final, pages)
        os.makedirs(out_dir, exist_ok=True)
        files = {"r1_initial.json": first, "r1_mid.json": mid, "r1_final.json": final}
        for name, data in files.items():
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        with open(os.path.join(out_dir, "r2_pages.jsonl"), "w", encoding="utf-8") as f:
            for p in pages:
                f.write(json.dumps(p, ensure_ascii=False, sort_keys=True) + "\n")
        with open(os.path.join(out_dir, "timeline.md"), "w", encoding="utf-8") as f:
            f.write(timeline(night, final))
        digests = {}
        for name in sorted(os.listdir(out_dir)):
            if name != "manifest.json":
                with open(os.path.join(out_dir, name), "rb") as f:
                    digests[name] = hashlib.sha256(f.read()).hexdigest()
        db = night.store.db
        manifest = {"protocol": PROTOCOL, "world_id": WORLD, "synthetic": True, "stream_epoch": "night-1",
                    "seed": SEED, "vocab_version": first["vocab_version"], "view_policy": first["view_policy"],
                    "schema_version": first["schema_version"], "source_ref": source_ref, "paid_calls": 0,
                    "model_calls": night.cog.budget.used(), "r2_events": sum(len(p["events"]) for p in pages),
                    "r2_pages": len(pages), "kernel_events": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    "verify": problems or "ok", "files": digests}
        with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1, sort_keys=True)
        night.store.close()
        return manifest, night
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings",
                                                  "kitchen-night"))
    a = ap.parse_args()
    try:
        ref = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True,
                                      stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        ref = None
    m, _ = record(a.out, ref)
    print(json.dumps({k: v for k, v in m.items() if k != "files"}, ensure_ascii=False))
    return 0 if m["verify"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
