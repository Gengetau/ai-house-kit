"""住客状态卡 (design): one resident's state right now, for the console, read-only.

Only what the kernel recorded, nothing guessed and no model:
- what she wears: life1.db outfits (items in order) with each garment's own label / category from garments;
  `outfit_id` is the outfit's stable key, her garment ids sorted and joined with "+" (the console looks the
  full-body picture up by it in runtime/profiles/outfit_art.json);
- today's changes: every outfit_log row from JST midnight to now, oldest first (one row = one change the kernel made);
  and the latest home / sleep / out outfit she wore, for looking back;
- where and what: world.db reality as the kernel's projector wrote it (the page says the state is stale when the row
  is not the kernel's or was not updated lately); until when: only her running kernel action's own due_at (reality's
  `until` is a made-up now+30 min while she has no action, so the page never shows it as an end time);
- today's meals: meal_attendance + meal_consumption (a dish only when the kernel recorded her eating it);
- mood: reality.feel (mood / degree / why / at), as the rest of the console shows it;
- what comes next: the kernel_card the projector wrote (plans, bedtime), as is.
A part that cannot be read is None with a `<part>_error`, never an empty list that would read as 「没有」.
"""
import datetime
import json

from . import vocab_life1 as V
from . import city_projection
from .legacy import NAMES
from .daylog import OCCASION, place

JST = datetime.timezone(datetime.timedelta(hours=9))
SLOTS = (("breakfast", "早饭"), ("lunch", "午饭"), ("dinner", "晚饭"))
CATEGORY = {"dress": "裙装", "top": "上装", "bottom": "下装", "outer": "外套", "shoes": "鞋", "sleepwear": "睡衣",
            "loungewear": "居家服", "sportswear": "运动服", "swimwear": "泳衣", "yukata": "浴衣", "accessory": "配饰"}
UNKNOWN_GARMENT = "衣物名称未记录"
LOOK_BACK = ("home", "sleep", "out")


def outfit_id(items):
    """The outfit's key: her garment ids, sorted, joined with "+" (the order she put them on does not matter)."""
    return "+".join(sorted(str(i) for i in items))


def _loads(s, default):
    try:
        v = json.loads(s) if s else default
    except (TypeError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


def _outfit(actor, items, garments, **extra):
    if garments is None:  # the garments table could not be read: no names guessed, the part says 读不到
        raise RuntimeError("garments unreadable")
    items = [i for i in items if isinstance(i, str)]
    pieces = []
    for i in items:
        g = garments.get(i)
        mine = g is not None and g["actor"] == actor
        pieces.append({"id": i, "label": (g["label"] or UNKNOWN_GARMENT) if mine else UNKNOWN_GARMENT,
                       "category": g["category"] if mine else None,
                       "kind": CATEGORY.get(g["category"], "单品") if mine else "单品"})
    occ = extra.get("occasion")
    return {"outfit_id": outfit_id(items), "items": items, "garments": pieces,
            "labels": [p["label"] for p in pieces], "occasion_label": OCCASION.get(occ, occ or "未分类"), **extra}


def _meals(k, actor, day, now):
    att = {r["slot"]: dict(r) for r in k.execute(
        "SELECT slot, starts, ends, status, reason FROM meal_attendance WHERE actor=? AND day=?", (actor, day))}
    ate = {}
    for r in k.execute("SELECT at, item, slot FROM meal_consumption WHERE actor=? AND day=? AND slot IS NOT NULL"
                       " ORDER BY at", (actor, day)):
        ate.setdefault(r["slot"], []).append(
            {"at": r["at"], "item": r["item"], "label": V.CATALOG["items"].get(r["item"], {}).get("label", r["item"])})
    out = []
    for slot, label in SLOTS:
        m, eaten = att.get(slot), ate.get(slot, [])
        if eaten:
            state, text = "eaten", "、".join(dict.fromkeys(x["label"] for x in eaten))
        elif not m:
            state, text = "unknown", "未记录"
        elif now < m["starts"]:
            state, text = "future", "还没到饭点"
        elif m["status"] == "missed" and m["reason"] == "sleeping":
            state, text = "missed", "睡过去了"
        elif m["status"] in ("missed", "declined"):
            state, text = "missed", "没吃"
        elif m["status"] == "eaten":
            state, text = "eaten", "吃了 · 菜名未记录"
        else:
            state, text = "pending", "还没有进餐记录"
        out.append({"slot": slot, "label": label, "state": state, "text": text, "eaten": eaten,
                    "starts": m["starts"] if m else None, "ends": m["ends"] if m else None,
                    "status": m["status"] if m else None, "reason": m["reason"] if m else None})
    return out


def _part(rec, name, fn):
    try:
        rec[name] = fn()
    except Exception as e:  # noqa: BLE001 - shown as 读不到; the rest of the card stays
        rec[name] = None
        rec[name + "_error"] = type(e).__name__


def _kernel(k, actor):
    a = k.execute("SELECT location, activity, availability, action_id, updated_at FROM actors WHERE actor_id=?",
                  (actor,)).fetchone()
    if not a:
        return None
    a = dict(a)
    a["place"] = place(a["location"], actor)
    x = k.execute("SELECT action_type, state, args, started_at, due_at FROM actions WHERE action_id=?",
                  (a["action_id"],)).fetchone() if a["action_id"] else None
    a["action"] = None
    if x:
        what = _loads(x["args"], {}).get("activity")
        a["action"] = {"type": x["action_type"], "state": x["state"], "started": x["started_at"], "due": x["due_at"],
                       "activity": what, "label": V.ACTIVITIES.get(what, {}).get("label") if what else None}
    return a


def _current(k, actor, garments):
    o = k.execute("SELECT items, occasion, since, event_id FROM outfits WHERE actor=?", (actor,)).fetchone()
    if not o:
        return None
    return _outfit(actor, _loads(o["items"], []), garments, occasion=o["occasion"], since=o["since"],
                   event_id=o["event_id"])


def _changes(k, actor, garments, since, now):
    return [_outfit(actor, _loads(x["items"], []), garments, occasion=x["occasion"], at=x["at"], event_id=x["event_id"])
            for x in k.execute("SELECT items, occasion, at, event_id FROM outfit_log WHERE actor=? AND at>=? AND at<=?"
                               " ORDER BY at, event_id", (actor, since, now))]


def _history(k, actor, garments, now):
    got = {}
    for occ in LOOK_BACK:
        x = k.execute("SELECT items, at, event_id FROM outfit_log WHERE actor=? AND occasion=? AND at<=?"
                      " ORDER BY at DESC, event_id DESC LIMIT 1", (actor, occ, now)).fetchone()
        got[occ] = _outfit(actor, _loads(x["items"], []), garments, occasion=occ, at=x["at"],
                           event_id=x["event_id"]) if x else None
    return got


def cards(k, w, now):
    """All six residents. k: life1.db, w: world.db (both opened read-only by the caller); now: epoch seconds."""
    today = datetime.datetime.fromtimestamp(now, JST).date()
    midnight = datetime.datetime.combine(today, datetime.time(), JST).timestamp()
    out = {"now": now, "day": today.isoformat(), "residents": {}}
    try:
        garments = {r["garment_id"]: r for r in k.execute("SELECT garment_id, actor, label, category FROM garments")}
    except Exception as e:  # noqa: BLE001 - outfit / changes / history then say 读不到; the rest of the card stays
        garments, out["garments_error"] = None, type(e).__name__
    reality, nxt = {}, {}
    try:
        reality = {r["actor"]: dict(r) for r in w.execute(
            "SELECT actor, location, place, activity, doing, mood, availability, started, until, source, updated, feel"
            " FROM reality")}
    except Exception as e:  # noqa: BLE001 - the card says where she is is unknown, not that she is nowhere
        out["reality_error"] = type(e).__name__
    try:
        nxt = {r["actor"]: _loads(r["data"], {}).get("next") for r in w.execute("SELECT actor, data FROM kernel_card")}
    except Exception as e:  # noqa: BLE001
        out["next_error"] = type(e).__name__
    for actor, name in NAMES.items():
        rec = {"actor": actor, "name": name}
        _part(rec, "kernel", lambda: _kernel(k, actor))
        r = reality.get(actor)
        if r is not None:
            feel = _loads(r.pop("feel"), {})
            r["feel"] = {key: feel.get(key) for key in ("mood", "degree", "why", "at")}
        rec["reality"] = r
        _part(rec, "outfit", lambda: _current(k, actor, garments))
        _part(rec, "changes", lambda: _changes(k, actor, garments, midnight, now))
        _part(rec, "history", lambda: _history(k, actor, garments, now))
        _part(rec, "meals", lambda: _meals(k, actor, today.isoformat(), now))
        rec["next"] = nxt.get(actor) if isinstance(nxt.get(actor), list) else None
        _part(rec, "city_outing", lambda: city_projection.resolve_projection_conn(k, actor))
        out["residents"][actor] = rec
    return out
