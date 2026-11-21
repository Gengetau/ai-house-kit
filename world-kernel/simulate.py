"""Fast-forward the full-life world (life1) on a virtual clock: `python3 simulate.py --days 7 --out DIR`.

Zero cost by default (persona-shaped fake model). --restart-at H1,H2 kills and rebuilds the runner at those
hours since start (same DB), to prove nothing is redone or lost. Writes DIR/world.db and DIR/week.json (per
person per day: timeline, meals, outfits, plans, invitations, sleep) for the console page.
"""
import argparse
import collections
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from worldkernel.store import Store, VirtualClock  # noqa: E402
from worldkernel.life import Life, ROSTER, NAMES, JST, day_of, run_days  # noqa: E402
from worldkernel.life_model import FakeLifeModel  # noqa: E402
from worldkernel.legacy import Projector, offline_world  # noqa: E402
from worldkernel import vocab_life1 as V  # noqa: E402


def build(path, clock, model):
    store = Store(path, clock, world_id="ai-house/life1-sim", synthetic=True, holder="simulate")
    store.acquire()
    from worldkernel import outings
    return Life(store, model=model, extensions=[outings.install])  # 奈尔 outings, wallet, trips, shopping


def t(ts):
    return dt.datetime.fromtimestamp(ts, JST).strftime("%H:%M")


def report(path):
    import sqlite3
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    out = collections.defaultdict(lambda: collections.defaultdict(lambda: {"timeline": [], "meals": {}, "outfits": [],
                                                                          "sleep": None, "invites": []}))
    for a in c.execute("SELECT actor_id, action_type, args, state, started_at, finished_at, origin FROM actions"
                       " WHERE state='succeeded' AND started_at IS NOT NULL ORDER BY started_at"):
        args = json.loads(a["args"])
        d = out[a["actor_id"]][day_of(a["started_at"])]
        if a["action_type"] == "begin_activity":
            what = V.ACTIVITIES[args["activity"]]["label"] + (" (一起)" if args.get("party") else "")
        elif a["action_type"] == "cook":
            what = "做" + V.RECIPES[args["recipe"]]["label"]
        elif a["action_type"] == "eat":
            what = "吃" + V.CATALOG["items"].get(args["item"], {}).get("label", args["item"])
        elif a["action_type"] == "move":
            what = "去" + V.label(args["to"])
        elif a["action_type"] == "sleep":
            what = f"睡觉（{round(args['seconds'] / 3600, 1)} 小时）"
        else:
            what = a["action_type"]
        d["timeline"].append(f"{t(a['started_at'])}-{t(a['finished_at'])} {what}" + (" *" if a["origin"] == "model" else ""))
    for r in c.execute("SELECT * FROM meal_attendance ORDER BY starts"):
        cell = r["status"] + (f"（{r['reason']}）" if r["reason"] else "")
        out[r["actor"]][r["day"]]["meals"][r["slot"]] = cell
    for r in c.execute("SELECT actor, items, occasion, at FROM outfit_log ORDER BY at"):
        labels = [g["label"] for g in c.execute("SELECT label FROM garments WHERE garment_id IN (%s)" %
                                                ",".join("?" * len(json.loads(r["items"]))), json.loads(r["items"]))]
        out[r["actor"]][day_of(r["at"])]["outfits"].append(f"{t(r['at'])} {r['occasion']}: {'、'.join(labels)}")
    for r in c.execute("SELECT i.*, p.kind, p.what, p.start_at FROM invitations i JOIN plans p USING(plan_id)"):
        line = f"{NAMES[r['inviter']]}→{NAMES[r['invitee']]} {r['kind']}:{r['what']} @{t(r['start_at'])} {r['state']}"
        out[r["inviter"]][day_of(r["created_at"])]["invites"].append(line)
        out[r["invitee"]][day_of(r["created_at"])]["invites"].append(line)
    stats = {"model_calls": c.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0]
             if c.execute("SELECT name FROM sqlite_master WHERE name='budget_reservations'").fetchone() else None,
             "decisions": dict(collections.Counter(r[0] for r in c.execute("SELECT outcome FROM decision_points"))),
             "refusals": dict(collections.Counter(r[0] for r in c.execute(
                 "SELECT reason FROM actions WHERE state='failed'")).most_common(15))}
    return {"people": {a: dict(v) for a, v in out.items()}, "stats": stats}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--out", default="/tmp/life1-sim")
    ap.add_argument("--start", default="2026-11-03 05:00")
    ap.add_argument("--restart-at", default="")
    ap.add_argument("--step", type=int, default=60)
    o = ap.parse_args()
    os.makedirs(o.out, exist_ok=True)
    path = os.path.join(o.out, "world.db")
    legacy = os.path.join(o.out, "legacy.db")  # what the old world (QQ 此刻 card, diary) would read
    for p in (path, legacy):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(p + suffix):
                os.remove(p + suffix)
    offline_world(legacy)
    start = dt.datetime.strptime(o.start, "%Y-%m-%d %H:%M").replace(tzinfo=JST).timestamp()
    clock = VirtualClock(start)
    model = FakeLifeModel()
    cuts = sorted(float(x) for x in o.restart_at.split(",") if x)
    done = 0.0
    life = build(path, clock, model)
    cards = collections.defaultdict(dict)  # actor -> day -> the card as QQ would see it at 21:00

    def live(life, hours):
        proj, end, n = Projector(life, legacy), clock.now() + hours * 3600, 0
        while clock.now() < end:
            life.step()
            n += 1
            if n % 3 == 0:
                proj.run()
                d = dt.datetime.fromtimestamp(clock.now(), JST)
                if d.hour == 21 and d.minute < 3 * o.step // 60 + 1:
                    for a in ROSTER:
                        cards[a].setdefault(d.date().isoformat(), proj.card(a))
            clock.advance(o.step)
        proj.run()

    for cut in cuts + [o.days * 24]:
        live(life, cut - done)
        done = cut
        if cut < o.days * 24:
            life.s.release()
            life = build(path, clock, model)  # a restart: runner memory comes back from the store
            last = life.s.meta("last_step")
            life.k.resume(float(last) if last else clock.now())
        life.s.set_meta("last_step", clock.now())
    life.s.release()
    rep = report(path)
    import sqlite3
    lc = sqlite3.connect(legacy)
    for ts, text, knowers in lc.execute("select ts, text, knowers from script where source='kernel' order by ts"):
        for a in json.loads(knowers or "[]"):
            rep["people"].setdefault(a, {}).setdefault(day_of(ts), {}).setdefault("lines", []).append(
                f"{t(ts)} {text}")
    for a, days in cards.items():
        for day, card in days.items():
            rep["people"].setdefault(a, {}).setdefault(day, {})["card"] = card
    rep["feelings"] = [{"actor": a, "at": t(ts), "day": day_of(ts), "emotion": e, "why": w} for a, ts, e, w in
                       lc.execute("select actor, ts, emotion, why from emotions order by ts")]
    rep["names"] = NAMES
    rep["start"], rep["days"] = o.start, o.days
    with open(os.path.join(o.out, "week.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps(rep["stats"], ensure_ascii=False))


if __name__ == "__main__":
    main()
