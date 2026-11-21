"""她的衣柜: read-only kernel view of one resident's garments, outfits, and change log.

Never imported by the kernel itself; runs inside one-shot read-only snapshots (mode=ro + PRAGMA query_only=1)
and reuses public helpers/constants from statuscard and daylog. Missing/unreadable parts return None +
<part>_error rather than fabricating empty lists or guessing labels.
"""
import datetime
import json
import math
import sqlite3

from .daylog import OCCASION, place
from .legacy import NAMES
from .statuscard import CATEGORY, JST, UNKNOWN_GARMENT, outfit_id

CATEGORY_ORDER = {key: idx for idx, key in enumerate(CATEGORY)}
GIFT_KINDS = frozenset()
STATE_LABEL = {"clean": "干净", "worn": "穿过待洗", "dirty": "脏"}


def _loads(raw, default):
    try:
        val = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return val if isinstance(val, type(default)) else default


def _day_jst(ts):
    if isinstance(ts, (int, float)) and math.isfinite(ts):
        return datetime.datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d")
    return None


def _part(fn):
    try:
        return fn(), None
    except sqlite3.Error as e:
        return None, f"{type(e).__name__}: {e}"[:200]


def _pieces(actor, items, garments):
    clean_items = [i for i in items if isinstance(i, str)]
    if garments is None:
        pieces = [{"id": i, "label": i, "category": None, "kind": "单品", "unknown": True} for i in clean_items]
        return clean_items, pieces, clean_items, True
    pieces = []
    for i in clean_items:
        g = garments.get(i)
        mine = g is not None and g["actor"] == actor
        cat = g["category"] if mine else None
        pieces.append({
            "id": i,
            "label": (g["label"] or UNKNOWN_GARMENT) if mine else UNKNOWN_GARMENT,
            "category": cat,
            "kind": CATEGORY.get(cat, "单品") if mine else "单品",
        })
    return clean_items, pieces, [p["label"] for p in pieces], False


def _parse_event_id(source):
    if not isinstance(source, str) or not source.startswith("event:"):
        return None
    tail = source[6:]
    if tail.isdigit():
        return int(tail)
    return None


def _resolve_sources(k, actor, raw_rows):
    event_ids = sorted({eid for r in raw_rows if (eid := _parse_event_id(r["source"])) is not None})
    events = {}
    if event_ids:
        placeholders = ",".join("?" * len(event_ids))
        try:
            for ev in k.execute(
                f"SELECT event_id, kind, actor_id, occurred_at, location FROM events WHERE event_id IN ({placeholders})",
                event_ids,
            ):
                events[ev["event_id"]] = dict(ev)
        except sqlite3.Error:
            events = {}

    resolved = {}
    for r in raw_rows:
        src = r["source"]
        if src == "initial_wardrobe":
            resolved[r["garment_id"]] = {
                "type": "initial", "event_id": None, "at": None, "day": None, "place": None, "from": None,
            }
            continue
        eid = _parse_event_id(src)
        ev = events.get(eid) if eid is not None else None
        if ev is not None:
            kind = ev["kind"]
            if kind == "purchased" and ev["actor_id"] == actor:
                resolved[r["garment_id"]] = {
                    "type": "bought",
                    "event_id": eid,
                    "at": ev["occurred_at"],
                    "day": _day_jst(ev["occurred_at"]),
                    "place": place(ev["location"], actor) or None,
                    "from": None,
                }
                continue
            if kind == "picked_shelf_garment" and ev["actor_id"] == actor:
                resolved[r["garment_id"]] = {
                    "type": "shelf",
                    "event_id": eid,
                    "at": ev["occurred_at"],
                    "day": _day_jst(ev["occurred_at"]),
                    "place": place(ev["location"], actor) or None,
                    "from": None,
                }
                continue
            if kind in GIFT_KINDS:
                resolved[r["garment_id"]] = {
                    "type": "gift",
                    "event_id": eid,
                    "at": ev["occurred_at"],
                    "day": _day_jst(ev["occurred_at"]),
                    "place": place(ev["location"], actor) or None,
                    "from": NAMES.get(ev["actor_id"], ev["actor_id"]),
                }
                continue
        resolved[r["garment_id"]] = {
            "type": "unknown", "event_id": eid, "at": None, "day": None, "place": None, "from": None,
        }
    return resolved


def _load_garments(k, actor):
    rows = k.execute(
        "SELECT garment_id, actor, label, category, color, material, temp_lo, temp_hi, occasions, liking,"
        " source, state FROM garments WHERE actor=? ORDER BY garment_id",
        (actor,),
    ).fetchall()
    sources = _resolve_sources(k, actor, rows)
    by_id = {}
    ordered = []
    for r in rows:
        row = dict(r)
        by_id[row["garment_id"]] = row
        occasions = [o for o in _loads(row["occasions"], []) if isinstance(o, str)]
        state = row["state"] or "clean"
        ordered.append({
            "id": row["garment_id"],
            "label": row["label"] or UNKNOWN_GARMENT,
            "category": row["category"],
            "kind": CATEGORY.get(row["category"], "单品"),
            "color": row["color"],
            "material": row["material"],
            "temp": [row["temp_lo"], row["temp_hi"]],
            "occasions": occasions,
            "occasion_labels": [OCCASION.get(o, o) for o in occasions],
            "liking": row["liking"],
            "state": state,
            "state_label": STATE_LABEL.get(state, state),
            "wearing": False,
            "source": sources[row["garment_id"]],
            "worn": {"count_window": 0, "count_all": 0, "last_at": None},
        })
    ordered.sort(key=lambda g: (CATEGORY_ORDER.get(g["category"], len(CATEGORY_ORDER)), g["id"]))
    return by_id, ordered


def _load_current_row(k, actor):
    r = k.execute("SELECT items, occasion, since, event_id FROM outfits WHERE actor=?", (actor,)).fetchone()
    if not r:
        return None
    return {
        "items": _loads(r["items"], []),
        "occasion": r["occasion"],
        "since": r["since"],
        "event_id": r["event_id"],
    }


def _scan_outfit_log(k, actor, since):
    rows = k.execute(
        "SELECT items, occasion, at, event_id FROM outfit_log WHERE actor=?"
        " ORDER BY at DESC, COALESCE(event_id, 0) DESC",
        (actor,),
    ).fetchall()
    garment_stats = {}
    outfits = {}
    for r in rows:
        items = [i for i in _loads(r["items"], []) if isinstance(i, str)]
        at = r["at"]
        in_window = since is None or (isinstance(at, (int, float)) and at >= since)
        for gid in dict.fromkeys(items):
            st = garment_stats.setdefault(gid, {"count_window": 0, "count_all": 0, "last_at": None})
            st["count_all"] += 1
            if in_window:
                st["count_window"] += 1
            if isinstance(at, (int, float)) and (st["last_at"] is None or at > st["last_at"]):
                st["last_at"] = at
        if not items:
            continue
        oid = outfit_id(items)
        rec = outfits.get(oid)
        if rec is None:
            rec = {
                "outfit_id": oid,
                "items": items,
                "occasions": {},
                "count_window": 0,
                "count_all": 0,
                "first_at": at,
                "last_at": at,
                "current": False,
            }
            outfits[oid] = rec
        occ = r["occasion"]
        if isinstance(occ, str) and occ:
            rec["occasions"][occ] = rec["occasions"].get(occ, 0) + 1
        rec["count_all"] += 1
        if in_window:
            rec["count_window"] += 1
        if isinstance(at, (int, float)):
            if rec["first_at"] is None or at < rec["first_at"]:
                rec["first_at"] = at
            if rec["last_at"] is None or at > rec["last_at"]:
                rec["last_at"] = at
    return len(rows), garment_stats, outfits


def _load_log_page(k, actor, log_before, log_limit, total, garments_by_id):
    if log_before is None:
        rows = k.execute(
            "SELECT items, occasion, at, event_id FROM outfit_log WHERE actor=?"
            " ORDER BY at DESC, COALESCE(event_id, 0) DESC LIMIT ?",
            (actor, log_limit + 1),
        ).fetchall()
    else:
        before_at, before_eid = float(log_before[0]), int(log_before[1])
        rows = k.execute(
            "SELECT items, occasion, at, event_id FROM outfit_log WHERE actor=?"
            " AND (at < ? OR (at = ? AND COALESCE(event_id, 0) < ?))"
            " ORDER BY at DESC, COALESCE(event_id, 0) DESC LIMIT ?",
            (actor, before_at, before_at, before_eid, log_limit + 1),
        ).fetchall()
    more = len(rows) > log_limit
    out = []
    for r in rows[:log_limit]:
        clean_items, _pieces_list, labels, labels_unknown = _pieces(actor, _loads(r["items"], []), garments_by_id)
        occ = r["occasion"]
        out.append({
            "at": r["at"],
            "day": _day_jst(r["at"]),
            "occasion": occ,
            "occasion_label": OCCASION.get(occ, occ or "未分类"),
            "outfit_id": outfit_id(clean_items),
            "items": clean_items,
            "labels": labels,
            "labels_unknown": labels_unknown,
            "event_id": r["event_id"],
        })
    return {"rows": out, "more": more, "total": total}


def view(k, actor, now, days=30, log_before=None, log_limit=50):
    """Read one resident's wardrobe snapshot from life1.db (read-only)."""
    if days == "all" or days is None:
        win_days, since = "all", None
    else:
        win_days = int(days)
        if win_days not in (30, 90):
            raise ValueError("days must be 30, 90, or 'all'")
        since = now - win_days * 86400
    limit = max(1, min(int(log_limit), 200))

    out = {
        "enabled": True,
        "now": now,
        "actor": actor,
        "name": NAMES.get(actor, actor),
        "window": {"days": win_days, "since": since},
    }

    garment_data, g_err = _part(lambda: _load_garments(k, actor))
    garments_by_id = garment_data[0] if garment_data is not None else None
    garments_list = garment_data[1] if garment_data is not None else None
    if g_err:
        out["garments_error"] = g_err

    current_row, c_err = _part(lambda: _load_current_row(k, actor))
    if c_err:
        out["current_error"] = c_err

    scan_data, l_err = _part(lambda: _scan_outfit_log(k, actor, since))
    if l_err:
        out["log_error"] = l_err
        out["outfits_error"] = l_err

    wearing_ids = set()
    if current_row is not None:
        clean_items, pieces, labels, labels_unknown = _pieces(actor, current_row["items"], garments_by_id)
        wearing_ids.update(clean_items)
        occ = current_row["occasion"]
        out["current"] = {
            "outfit_id": outfit_id(clean_items),
            "items": clean_items,
            "garments": pieces,
            "labels": labels,
            "labels_unknown": labels_unknown,
            "occasion": occ,
            "occasion_label": OCCASION.get(occ, occ or "未分类"),
            "since": current_row["since"],
            "event_id": current_row["event_id"],
        }
    else:
        out["current"] = None

    garment_stats = scan_data[1] if scan_data is not None else {}
    if garments_list is not None:
        by_source = {"initial": 0, "bought": 0, "gift": 0, "unknown": 0}
        worn_waiting = 0
        for g in garments_list:
            g["wearing"] = g["id"] in wearing_ids
            if g["id"] in garment_stats:
                g["worn"] = garment_stats[g["id"]]
            st_type = g["source"]["type"]
            by_source[st_type if st_type in by_source else "unknown"] += 1
            if g["state"] == "worn":
                worn_waiting += 1
        out["garments"] = garments_list
    else:
        by_source = None
        worn_waiting = None
        out["garments"] = None

    if scan_data is not None:
        total_logs, _, outfits_map = scan_data
        if out["current"] and out["current"]["items"]:
            cur_oid = out["current"]["outfit_id"]
            if cur_oid in outfits_map:
                # change_clothes already writes outfit_log and outfits at the same moment; mark current without
                # double-counting it as an extra wear.
                outfits_map[cur_oid]["current"] = True
            else:
                cur_at = current_row["since"]
                in_window = since is None or (isinstance(cur_at, (int, float)) and cur_at >= since)
                occ = current_row["occasion"]
                outfits_map[cur_oid] = {
                    "outfit_id": cur_oid,
                    "items": list(out["current"]["items"]),
                    "occasions": {occ: 1} if isinstance(occ, str) and occ else {},
                    "count_window": 1 if in_window else 0,
                    "count_all": 1,
                    "first_at": cur_at,
                    "last_at": cur_at,
                    "current": True,
                }
        outfits_list = []
        for rec in outfits_map.values():
            _clean, pieces, _labels, labels_unknown = _pieces(actor, rec["items"], garments_by_id)
            outfits_list.append({
                "outfit_id": rec["outfit_id"],
                "items": rec["items"],
                "garments": pieces,
                "labels_unknown": labels_unknown,
                "occasions": rec["occasions"],
                "count_window": rec["count_window"],
                "count_all": rec["count_all"],
                "first_at": rec["first_at"],
                "last_at": rec["last_at"],
                "current": rec["current"],
            })
        outfits_list.sort(
            key=lambda o: (
                0 if o["current"] else 1,
                -(o["count_all"] if win_days == "all" else o["count_window"]),
                -o["count_all"],
                -(o["last_at"] or 0),
                o["outfit_id"],
            )
        )
        out["outfits"] = outfits_list
        log_page, page_err = _part(lambda: _load_log_page(k, actor, log_before, limit, total_logs, garments_by_id))
        if page_err:
            out["log"] = None
            out["log_error"] = page_err
        else:
            out["log"] = log_page
    elif current_row is not None and out["current"] and out["current"]["items"]:
        # Keep outfit list unavailable rather than partial if outfit_log itself cannot be read.
        out["outfits"] = None
        out["log"] = None
    else:
        out["outfits"] = None
        out["log"] = None

    out["summary"] = {
        "garments": len(garments_list) if garments_list is not None else None,
        "by_source": by_source,
        "worn_waiting": worn_waiting,
        "outfits": len(out["outfits"]) if out["outfits"] is not None else None,
    }
    return out
