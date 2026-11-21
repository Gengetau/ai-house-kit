"""她们这一周 · 生活细节: the kernel's own record of a day as timeline lines for the console.

Read-only over life1.db (actions, events, garments, wallet_entries). One line per thing she did, only from what the
kernel recorded (no model, no plans, nothing guessed): an activity and how long it lasted, going from one room to
another, changing clothes (into what), shopping (what, how many, what it cost, from whose money), sleep and rest.
`wait` (and check / say / invite, which are not things she does with her body) are not shown.

Cooking, eating a meal, a shared activity (args.party) and arriving at an outside place are already lines in
world.db's script (legacy.Projector.facts): they are not written twice. To keep the day readable:
- a walk that ends where her next thing starts (within FOLD_S) is folded into it: 「由纪去浴室洗澡/泡澡 30 分钟」;
  a walk right before a script-covered thing (going to the kitchen to cook) is left to that line;
- the same activity picked up again in the same room within SAME_S is one line;
- the purchases of one visit to a shop are one line, outfit changes within CLOTHES_S are one line (the last outfit).
Each line carries where (her own room is 自己房间), when and how long, and who else was in the same room for a
while (`with`, from the kernel's arrived events).
"""
import json

from . import vocab_life1 as V
from .legacy import NAMES, _bare

FOLD_S = 600       # a walk is folded into what she does next if it starts within 10 minutes where she arrived
SAME_S = 180       # the same activity resumed in the same room within 3 minutes is one line
CLOTHES_S = 300    # outfit changes within 5 minutes: one line with the last one
WITH_MIN_S = 300   # 同在: at least 5 minutes in the same room (or half of a shorter line, or there at the moment)
TRANSPARENT = {"wait", "check", "say", "invite", "respond_invite"}
OCCASION = {"home": "居家", "out": "出门", "formal": "正式场合", "sport": "运动", "sleep": "睡觉", "onsen": "泡汤"}
# a few catalog labels read oddly after 「在客厅」; the rest are the catalog's own words
SAY = {"tea_time": "喝茶", "working": "做自己的事", "kitchen_help": "打下手"}


def place(loc, actor):
    if not loc:
        return ""
    if loc.startswith("room_"):
        who = loc[5:]
        if who == actor:
            return "自己房间"
        if who in NAMES:
            return NAMES[who] + "的房间"
    return V.label(loc)


def span(sec):
    m = max(1, round(sec / 60))
    if m < 60:
        return f"{m} 分钟"
    h, m = divmod(m, 60)
    return f"{h} 小时" + (f" {m} 分" if m else "")


def _outside(loc):
    return bool(loc) and (loc == "away" or loc.startswith(V.PLACE))


def entries(db, start, end, now):
    """Lines for everything that started in [start, end), oldest first. `db` is a sqlite3 connection to life1.db
    (row_factory=sqlite3.Row), opened read-only by the caller."""
    people = sorted(NAMES)
    marks = ",".join("?" * len(people))
    when = "COALESCE(a.started_at, a.finished_at)"
    acts = db.execute(
        f"SELECT a.action_id, a.actor_id, a.action_type, a.args, a.state, a.started_at, a.finished_at, {when} AS t"
        f" FROM actions a WHERE a.actor_id IN ({marks}) AND {when}>=? AND {when}<? AND (a.state IN"
        " ('succeeded','running') OR (a.state='cancelled' AND a.started_at IS NOT NULL AND a.finished_at>a.started_at))"
        f" ORDER BY {when}, a.created_at", (*people, start, end)).fetchall()
    evs = {}
    ids = {a["action_id"] for a in acts}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # what a purchase cost, from the wallet's own entries (the outings extension); without it, the event's yen.
    # Only purchases ask the wallet; events are read in one pass over the window (events has no action_id index).
    paid = ("CASE WHEN e.kind='purchased' THEN (SELECT SUM(w.delta) FROM wallet_entries w WHERE w.event_id=e.event_id)"
            " END AS paid, CASE WHEN e.kind='purchased' THEN (SELECT MIN(w.account) FROM wallet_entries w"
            " WHERE w.event_id=e.event_id) END AS account"
            if "wallet_entries" in tables else "NULL AS paid, NULL AS account")
    if ids:  # an action's events fall between its start and its end (a night's sleep at most: within a day after)
        for e in db.execute("SELECT e.event_id, e.kind, e.action_id, e.occurred_at, e.location, e.payload, %s"
                            " FROM events e WHERE e.occurred_at>=? AND e.occurred_at<? AND e.action_id IS NOT NULL"
                            " ORDER BY e.event_id" % paid, (start - 60, end + 86400)):
            if e["action_id"] not in ids:
                continue
            evs.setdefault(e["action_id"], []).append(e)
    garments = {r[0]: r[1] for r in db.execute("SELECT garment_id, label FROM garments")} if "garments" in tables else {}
    stay = _presence(db, people, start, end)

    lines = []
    for actor in people:
        lines += _fold(actor, [a for a in acts if a["actor_id"] == actor], evs, garments, now)
    for x in lines:
        # who else was there: where she arrived (a walk), or while the thing itself lasted (the walk not counted)
        s, f = (x["arrived"], x["arrived"]) if x["kind"] == "move" else (x["began"], x["end"] or now)
        x["with"] = _with(stay, x["actor"], x["loc"], s, f)
        x["people"] = [x["actor"]]  # the line names only her; 同在 is shown beside it, the filter is by who it is about
        x["text"] = _say(x, now)
        # an outside place she reached: world.db's 「X到了超市」 says the same, the caller drops that one
        w = x if x["kind"] == "move" else x["walk"]
        x["reached"] = w["arrived"] if w and str(w["to"] or "").startswith(V.PLACE) else None
    lines.sort(key=lambda x: (x["ts"], x["actor"]))
    return lines


def _presence(db, people, start, end):
    """Where everyone was: per person, [(since, location)] from the kernel's arrived events (and where each action
    started, so someone who has not walked since the kernel took over is still somewhere)."""
    stay = {a: [] for a in people}
    for e in db.execute("SELECT actor_id, occurred_at, location FROM events WHERE kind IN ('arrived','action_started')"
                        " AND occurred_at<? AND occurred_at>=? ORDER BY occurred_at, event_id",
                        (end + 86400, start - 3 * 86400)):
        if e["actor_id"] in stay and e["location"]:
            stay[e["actor_id"]].append((e["occurred_at"], e["location"]))
    return stay


def _with(stay, actor, loc, s, f):
    if not loc or loc == "away":
        return []
    need = min(WITH_MIN_S, max(0.0, (f - s) / 2))
    out = []
    for other, rows in stay.items():
        if other == actor:
            continue
        got = 0.0
        for i, (since, where) in enumerate(rows):
            until = rows[i + 1][0] if i + 1 < len(rows) else float("inf")
            if where != loc:
                continue
            if need == 0:
                if since <= s < until:
                    got = 1.0
                    break
                continue
            got += max(0.0, min(until, f) - max(since, s))
        if got and got >= need:
            out.append(other)
    return out


def _first(evs, kinds):
    for e in evs:
        if e["kind"] in kinds:
            return e
    return None


def _item(a, evs, garments):
    """One action as a plain record, or None when it is not shown; kind 'covered' = a script line tells it."""
    args = json.loads(a["args"] or "{}")
    typ = a["action_type"]
    mine = evs.get(a["action_id"], [])
    started = _first(mine, ("action_started",)) or (mine[0] if mine else None)
    loc = started["location"] if started else None
    it = {"id": a["action_id"], "s": a["t"], "f": a["finished_at"] if a["state"] != "running" else None,
          "running": a["state"] == "running", "events": [e["event_id"] for e in mine], "loc": loc}
    if typ in TRANSPARENT:
        return None
    if typ in ("begin_activity", "sleep", "rest", "eat") and it["f"] is not None and it["f"] - it["s"] < 60:
        return None  # begun and stopped within a minute: nothing to tell
    if typ == "cook" or (typ == "eat" and args.get("item") in V.MAIN_FOODS) \
            or (typ == "begin_activity" and args.get("party")):
        return dict(it, kind="covered")
    if typ == "move":
        dep = _first(mine, ("departed",))
        arr = _first(mine, ("arrived",))
        if a["state"] == "running" or not arr:
            return None  # still on the way: the next refresh has it
        return dict(it, kind="move", frm=(dep or started or {"location": None})["location"], to=args.get("to"),
                    arrived=arr["occurred_at"], loc=args.get("to"))
    if typ == "begin_activity":
        return dict(it, kind="activity", act=args.get("activity"))
    if typ in ("sleep", "rest"):
        return dict(it, kind=typ)
    if typ == "eat" and args.get("item") and a["state"] != "cancelled":  # eaten only once the kernel finished it
        return dict(it, kind="snack", item=args.get("item"))
    if typ == "change_clothes":
        e = _first(mine, ("changed_clothes",))
        if not e:
            return None
        p = json.loads(e["payload"] or "{}")
        return dict(it, kind="clothes", loc=e["location"], occasion=p.get("occasion"),
                    wear=[garments.get(g, g) for g in p.get("items") or []])
    if typ == "buy":
        e = _first(mine, ("purchased",))
        if not e:
            return None
        p = json.loads(e["payload"] or "{}")
        yen = -e["paid"] if e["paid"] is not None else p.get("yen")
        return dict(it, kind="shop", loc=e["location"],
                    bought=[{"item": p.get("item"), "name": p.get("name"), "qty": p.get("qty") or 1, "yen": yen,
                             "account": e["account"] or p.get("account")}])
    return None


def _round(w):
    """A walk that ends where it began without going out (to the kitchen to check, and back): not a walk to tell."""
    return w["frm"] == w["to"] and not any(v and _outside(v) for v in w["via"])


def _fold(actor, acts, evs, garments, now):
    out, walk = [], None

    def flush():
        nonlocal walk
        if walk and not _round(walk):
            out.append(dict(walk, kind="move", ts=walk["s"], began=walk["s"], end=walk["f"], walk=None))
        walk = None

    for a in acts:
        it = _item(a, evs, garments)
        if it is None:
            continue
        near = walk is not None and it["s"] - walk["f"] <= FOLD_S
        if it["kind"] == "move":
            if near:
                walk.update(to=it["to"], loc=it["to"], f=it["f"], arrived=it["arrived"],
                            events=walk["events"] + it["events"], via=walk["via"] + [walk["to"]])
            else:
                flush()
                walk = dict(it, actor=actor, via=[])
            continue
        if it["kind"] == "covered":
            if not near:
                flush()
            walk = None  # the walk to the kitchen / the gathering is part of that line
            continue
        came = walk if near and it["loc"] == walk["to"] and not _round(walk) else None
        if not came:
            flush()
        walk = None
        x = dict(it, actor=actor, ts=it["s"], began=it["s"], end=it["f"],
                 walk={"from": came["frm"], "to": came["to"], "via": came["via"], "s": came["s"],
                       "arrived": came["arrived"], "events": came["events"]} if came else None)
        if came:
            x["ts"] = came["s"]
            x["events"] = came["events"] + x["events"]
        prev = out[-1] if out else None
        if prev and _merge(prev, x):
            continue
        out.append(x)
    flush()
    for x in out:
        x.pop("s", None)
        x.pop("f", None)
    return out


def _merge(prev, x):
    """Fold x into the line before it (same activity resumed, one shop visit, a quick second outfit)."""
    if prev["kind"] != x["kind"] or prev["loc"] != x["loc"] or prev["end"] is None or x["walk"]:
        return False
    gap = x["ts"] - prev["end"]
    if x["kind"] == "activity" and prev.get("act") == x.get("act") and gap <= SAME_S:
        pass
    elif x["kind"] == "shop" and gap <= FOLD_S:
        prev["bought"] = prev["bought"] + x["bought"]
    elif x["kind"] == "clothes" and gap <= CLOTHES_S:
        prev.update(wear=x["wear"], occasion=x["occasion"])
    else:
        return False
    prev.update(end=x["end"], running=x["running"], events=prev["events"] + x["events"])
    return True


def _going(w, actor, merged):
    """「去浴室」「出门去超市」「从超市回到家，」 for the walk part of a line."""
    frm, to = w["from"], w["to"]
    if to and to.startswith(V.PLACE):
        if frm and frm.startswith(V.PLACE):
            return f"从{place(frm, actor)}去{place(to, actor)}" + ("，" if merged else "")
        return f"出门去{place(to, actor)}" + ("，" if merged else "")
    if to == "away":
        return f"离开{place(frm, actor)}" if frm and frm.startswith(V.PLACE) else "出门"
    # coming home: from the last outside place on the way (kitchen -> away -> supermarket -> away -> living)
    outside = [v for v in [frm, *(w.get("via") or [])] if v and v.startswith(V.PLACE)]
    back = outside[-1] if outside else None
    if back or frm == "away":
        return (f"从{place(back, actor)}回到家，" if back else "回到家，") + (f"在{place(to, actor)}" if merged else
                                                                         f"到了{place(to, actor)}")
    return f"去{place(to, actor)}" if merged else f"从{place(frm, actor)}去了{place(to, actor)}"


def _say(x, now):
    actor, kind = x["actor"], x["kind"]
    name = NAMES[actor]
    if kind == "move":
        return name + _going({"from": x.get("frm"), "to": x["to"], "via": x.get("via")}, actor, False)
    w = x["walk"]
    lead = _going(w, actor, True) if w else f"在{place(x['loc'], actor)}"
    long = (span(now - x["began"]) + "，还在继续") if x["running"] else span((x["end"] or now) - x["began"])
    if kind == "activity":
        act = x.get("act")
        # 「一起看电影」 alone (no party: a shared one is the script's line) is said without 「一起」, as legacy._bare
        what = SAY.get(act) or _bare(V.ACTIVITIES.get(act, {}).get("label") or str(act))
        return f"{name}{lead}{what} {long}"
    if kind == "sleep":
        return f"{name}{lead}睡了 {long}" if not x["running"] else f"{name}{lead}睡觉，{long}"
    if kind == "rest":
        return f"{name}{lead}休息 {long}"
    if kind == "snack":
        return f"{name}{lead}{'正在吃' if x['running'] else '吃了'}{_label(x.get('item'))}"
    if kind == "clothes":
        occ = OCCASION.get(x.get("occasion"))
        return f"{name}{lead}换上了{'、'.join(x['wear']) or '别的衣服'}" + (f"（{occ}）" if occ else "")
    if kind == "shop":
        got = "、".join(f"{_label(b['item'], b.get('name'))} ×{b['qty']}" for b in x["bought"])
        yen = sum(b["yen"] or 0 for b in x["bought"])
        whose = {b["account"] for b in x["bought"]}
        acct = "公账" if whose == {"house"} else "自己的钱" if whose <= set(NAMES) else "公账和自己的钱"
        return f"{name}{lead}买了{got}，共 ¥{yen:,}（{acct}）"
    return name


def _label(item, name=None):
    """The catalog's word for it; a shop's own goods (clothes, a room for the night) by the name the purchase carries."""
    got = V.CATALOG["items"].get(item, {}).get("label")
    return got or (name if name and name != item else None) or item or name or ""
