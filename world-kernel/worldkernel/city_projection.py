"""Read-only city visit projection helpers for world.db, status cards and /world/{consumer}/now (K7).

Reads K1 ``city_visits`` records plus cached projection progress shaped like spec §3.4 outbox
events (``visit_progress`` / ``return_started``) and engine-reachability state (§3.11).
Never mutates ``actors.location`` or any kernel fact table, and never exposes the hidden house
entrance node (``sakuraoka:ai-house:entrance`` or legacy ``oldtown:ai-house:entrance``) or any concrete street address.
"""
import datetime as dt
import json
import re
import sqlite3
import time

from .store import dumps

JST = dt.timezone(dt.timedelta(hours=9))
INACTIVE_STATES = frozenset(("reconcile", "closed", "aborted"))
TRANSIT_MODES = frozenset(("transit", "riding", "boarded", "departed", "moving", "in_transit"))
AREA_LABELS = {
    "sakuraoka": "樱丘",
    "rurigai": "琉璃街",
    "kazeminato": "风港",
    "city": "白帆市",
}
_ENTRANCE_RE = re.compile(
    # 白帆市 R1 ids: the R0 id oldpass stays covered, like oldtown, in case old rows still carry it.
    r"(?:sakuraoka:ai-house:entrance|oldpass:ai-house:entrance|oldtown:ai-house:entrance|ai-house:entrance|sakuraoka_entrance|oldpass_entrance|oldtown_entrance|house_entrance|小屋门口)",
    re.IGNORECASE,
)
_ADDRESS_PATTERNS = (
    # 白帆市 R1: the R0 names 白帆市/旧渡 stay covered, like 旧町 below, in case old text still carries them.
    re.compile(r"(?:白帆市|白帆市)?(?:樱丘|旧渡)(?:[西台北南])?\s*\d+(?:[-−－丁目番地号]+\d*)+[^\s，,。；;、）)]*(?:\s+\d+号室?)?"),
    re.compile(r"(?:樱丘|旧渡)(?:[西台北南])?\s*[0-9一二三四五六七八九十]+(?:丁目|番地|号)[^\s，,。；;、）)]*"),
    re.compile(r"(?:Sakuraoka|Oldpass)[^\n,，。;；]*?\d+-\d+[^\n,，。;；]*", re.IGNORECASE),
    re.compile(r"某都某区旧町[^\s，,。；;、）)]*?\d+(?:[-−－丁目番地号]+\d*)+[^\s，,。；;、）)]*(?:\s+\d+号室?)?"),
    re.compile(r"某区旧町[^\s，,。；;、）)]*?\d+(?:[-−－丁目番地号]+\d*)+[^\s，,。；;、）)]*(?:\s+\d+号室?)?"),
    re.compile(r"旧町(?:[西台北南])?\s*[0-9一二三四五六七八九十]+丁目[^\s，,。；;、）)]*"),
    re.compile(r"Oldtown[^\n,，。;；]*?\d+-\d+[^\n,，。;；]*", re.IGNORECASE),
)
_PROJECTION_DDL = """
CREATE TABLE IF NOT EXISTS city_visit_projections (
  visit_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
  last_record_id TEXT, last_kind TEXT, occurred_at_ms INTEGER,
  payload TEXT NOT NULL DEFAULT '{}', engine_unreachable INTEGER NOT NULL DEFAULT 0,
  unreachable_since_ms INTEGER, updated_at_ms INTEGER NOT NULL)
"""


def jst_hhmm(ms):
    """Format a UTC epoch millisecond timestamp as JST ``HH:MM``."""
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(int(ms) / 1000.0, JST).strftime("%H:%M")


def redact_text(text):
    """Scrub hidden entrance node identifiers and street-level house addresses."""
    if text is None:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    s = _ENTRANCE_RE.sub("樱丘一带", s)
    for pat in _ADDRESS_PATTERNS:
        s = pat.sub("樱丘一带", s)
    return s


def _area_label(raw):
    cleaned = redact_text(raw)
    if not cleaned:
        return ""
    if cleaned.lower() in AREA_LABELS:
        return AREA_LABELS[cleaned.lower()]
    if ":" in cleaned:
        head = cleaned.split(":", 1)[0].strip().lower()
        if head in AREA_LABELS:
            return AREA_LABELS[head]
    return AREA_LABELS.get(cleaned.lower(), cleaned)


def _loads(text, default):
    if not text:
        return default
    try:
        val = json.loads(text)
        return val if isinstance(val, type(default)) else default
    except Exception:
        return default


def _has_table(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _now_ms(now_ms=None):
    return int(round(time.time() * 1000)) if now_ms is None else int(now_ms)


def apply_outbox_event(store_or_conn, event, now_ms=None):
    """Ingest a city-engine outbox event shape (``visit_progress`` / ``return_started``) into projection cache.

    Only updates ``city_visit_projections``; never modifies ``actors`` or ``city_visits``.
    """
    if not isinstance(event, dict):
        raise ValueError("event must be a dict")
    visit_id = str(event.get("visit_id") or "").strip()
    kind = str(event.get("kind") or "").strip()
    if not visit_id or not kind:
        raise ValueError("visit_id and kind are required")
    seq = int(event.get("seq") or 0)
    record_id = str(event.get("record_id") or "") or None
    occurred_at_ms = int(event["occurred_at_ms"]) if event.get("occurred_at_ms") is not None else None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    ts_ms = _now_ms(now_ms)

    def _write(db):
        db.execute(_PROJECTION_DDL)
        visit = db.execute("SELECT actor_id, state FROM city_visits WHERE visit_id=?", (visit_id,)).fetchone()
        if not visit:
            return False
        actor_id = visit["actor_id"] if isinstance(visit, sqlite3.Row) else visit[0]
        cur = db.execute(
            "SELECT last_seq, last_record_id FROM city_visit_projections WHERE visit_id=?", (visit_id,)
        ).fetchone()
        if cur:
            cur_seq = cur["last_seq"] if isinstance(cur, sqlite3.Row) else cur[0]
            cur_rec = cur["last_record_id"] if isinstance(cur, sqlite3.Row) else cur[1]
            if (record_id and cur_rec == record_id) or (seq and seq <= cur_seq):
                return False
        db.execute(
            "INSERT INTO city_visit_projections(visit_id, actor_id, last_seq, last_record_id, last_kind,"
            " occurred_at_ms, payload, engine_unreachable, unreachable_since_ms, updated_at_ms)"
            " VALUES(?,?,?,?,?,?,?,0,NULL,?)"
            " ON CONFLICT(visit_id) DO UPDATE SET"
            " actor_id=excluded.actor_id, last_seq=excluded.last_seq, last_record_id=excluded.last_record_id,"
            " last_kind=excluded.last_kind, occurred_at_ms=excluded.occurred_at_ms, payload=excluded.payload,"
            " engine_unreachable=0, unreachable_since_ms=NULL, updated_at_ms=excluded.updated_at_ms",
            (visit_id, actor_id, seq, record_id, kind, occurred_at_ms, dumps(payload), ts_ms),
        )
        return True

    if hasattr(store_or_conn, "tx"):
        with store_or_conn.tx() as db:
            return _write(db)
    ok = _write(store_or_conn)
    store_or_conn.commit()
    return ok


def set_engine_unreachable(store_or_conn, visit_id, unreachable=True, now_ms=None):
    """Mark or clear the §3.11 engine-unreachable projection flag without mutating kernel facts."""
    ts_ms = _now_ms(now_ms)
    flag = 1 if unreachable else 0
    since = ts_ms if unreachable else None

    def _write(db):
        db.execute(_PROJECTION_DDL)
        visit = db.execute("SELECT actor_id FROM city_visits WHERE visit_id=?", (visit_id,)).fetchone()
        if not visit:
            return False
        actor_id = visit["actor_id"] if isinstance(visit, sqlite3.Row) else visit[0]
        db.execute(
            "INSERT INTO city_visit_projections(visit_id, actor_id, last_seq, last_record_id, last_kind,"
            " occurred_at_ms, payload, engine_unreachable, unreachable_since_ms, updated_at_ms)"
            " VALUES(?,?,0,NULL,NULL,NULL,'{}',?,?,?)"
            " ON CONFLICT(visit_id) DO UPDATE SET"
            " engine_unreachable=excluded.engine_unreachable,"
            " unreachable_since_ms=CASE WHEN excluded.engine_unreachable=1 THEN COALESCE(city_visit_projections.unreachable_since_ms, excluded.unreachable_since_ms) ELSE NULL END,"
            " updated_at_ms=excluded.updated_at_ms",
            (visit_id, actor_id, flag, since, ts_ms),
        )
        return True

    if hasattr(store_or_conn, "tx"):
        with store_or_conn.tx() as db:
            return _write(db)
    ok = _write(store_or_conn)
    store_or_conn.commit()
    return ok


def _format_location_label(payload):
    area = _area_label(payload.get("area_label") or payload.get("area") or payload.get("district"))
    place = redact_text(
        payload.get("place_label")
        or payload.get("venue")
        or payload.get("location_label")
        or payload.get("place")
    )
    if place == "樱丘一带" and not area:
        return "樱丘一带"
    if place.startswith("在"):
        return place
    if area and place:
        combined = place if area in place else f"{area}·{place}"
        return f"在{combined}"
    if place:
        return f"在{place}"
    if area:
        return f"在{area}"
    return ""


def _extract_transit_to(payload):
    raw_to = (
        payload.get("to_label")
        or payload.get("to_station")
        or payload.get("to_name")
        or payload.get("destination_label")
        or payload.get("station")
        or payload.get("to")
        or payload.get("to_node")
    )
    if not raw_to:
        return ""
    return _area_label(raw_to)


def _build_projection(visit_row, proj_row, now=None):
    visit_id = visit_row["visit_id"]
    state = visit_row["state"]
    if state in INACTIVE_STATES:
        return None
    plan = _loads(visit_row["plan"], {})
    payload = _loads(proj_row["payload"], {}) if proj_row else {}
    last_kind = proj_row["last_kind"] if proj_row else None
    occurred_at_ms = proj_row["occurred_at_ms"] if proj_row else None
    unreachable = bool(proj_row["engine_unreachable"]) if proj_row else False

    return_by_ms = plan.get("return_by_ms")
    return_by_jst = jst_hhmm(return_by_ms)
    updated_at_ms = (
        proj_row["updated_at_ms"] if proj_row and proj_row["updated_at_ms"] else visit_row["updated_at_ms"]
    )
    updated_at_jst = jst_hhmm(updated_at_ms)

    visit_updated_ms = visit_row["updated_at_ms"]
    stale_returning_progress = (
        state == "returning"
        and last_kind != "return_started"
        and occurred_at_ms is not None
        and visit_updated_ms is not None
        and int(occurred_at_ms) < int(visit_updated_ms)
    )
    active_payload = (
        {}
        if (state in ("preparing", "prepared", "settling", "settled") or stale_returning_progress)
        else payload
    )

    eta_at_ms = active_payload.get("eta_at_ms")
    if eta_at_ms is None:
        eta_at_ms = active_payload.get("eta_ms")
    if eta_at_ms is None:
        eta_at_ms = active_payload.get("arrival_at_ms")
    if eta_at_ms is not None:
        eta_at_ms = int(eta_at_ms)
    eta_jst = jst_hhmm(eta_at_ms)

    mode = str(active_payload.get("mode") or active_payload.get("status") or active_payload.get("phase") or "").strip().lower()
    formatted_loc = _format_location_label(active_payload)
    has_place = bool(formatted_loc)
    is_returning = state == "returning" or (state == "active" and last_kind == "return_started")
    is_transit = (
        mode in TRANSIT_MODES
        or (eta_at_ms is not None and not has_place)
        or (is_returning and eta_at_ms is not None)
    )

    transit = None
    location_label = None

    if unreachable:
        status_text = "外出中，状态待确认"
        place = "外出中 · 状态待确认"
        doing = "外出中，状态待确认"
        eta_at_ms = None
        eta_jst = None
    elif state in ("preparing", "prepared"):
        dest = _area_label(plan.get("destination"))
        place = f"外出中 · 准备前往{dest}" if dest else "外出中 · 准备出发"
        doing = f"外出中（准备前往{dest}）" if dest else "外出中（准备出发）"
        status_text = "外出中"
        eta_at_ms = None
        eta_jst = None
    elif state in ("settling", "settled"):
        place = "外出中 · 即将到家"
        doing = "外出中（即将到家）"
        status_text = "外出中 · 即将到家"
        eta_at_ms = None
        eta_jst = None
    elif is_transit:
        to_label = _extract_transit_to(active_payload)
        if is_returning and not to_label:
            to_label = "樱丘一带"
        if eta_jst and to_label:
            transit_text = f"乘车中，预计 {eta_jst} 到{to_label}"
        elif eta_jst:
            transit_text = f"乘车中，预计 {eta_jst} 到达"
        elif to_label:
            transit_text = f"乘车中，前往{to_label}"
        else:
            transit_text = "乘车中"
        transit = {"to": to_label or None, "eta_at_ms": eta_at_ms, "eta_jst": eta_jst, "text": transit_text}
        place = "外出中 · 回程乘车中" if is_returning else "外出中 · 乘车中"
        doing = transit_text
        status_text = f"外出中 · {transit_text}"
    elif is_returning:
        place = "外出中 · 回程中"
        doing = redact_text(active_payload.get("doing") or active_payload.get("activity")) or "外出中，回程中"
        status_text = "外出中 · 回程中"
        eta_at_ms = None
        eta_jst = None
    elif has_place:
        location_label = formatted_loc
        place = f"外出中 · {location_label}"
        doing = redact_text(active_payload.get("doing") or active_payload.get("activity")) or location_label
        status_text = f"外出中 · {location_label}"
        eta_at_ms = None
        eta_jst = None
    else:
        dest = _area_label(plan.get("destination"))
        place = f"外出中 · 计划前往{dest}" if dest else "外出中"
        doing = f"外出中（计划前往{dest}）" if dest else "外出中"
        status_text = f"外出中 · 计划前往{dest}" if dest else "外出中"
        eta_at_ms = None
        eta_jst = None

    start_ms = (None if stale_returning_progress else occurred_at_ms) or plan.get("start_at_ms") or visit_row["created_at_ms"]
    started_at_s = float(start_ms) / 1000.0
    until_ms = eta_at_ms or return_by_ms or (int(start_ms) + 3600 * 1000)
    until_at_s = float(until_ms) / 1000.0
    if now is not None and (unreachable or until_at_s <= float(now)):
        until_at_s = float(now) + 1800.0

    return {
        "visit_id": visit_id,
        "state": state,
        "status_text": redact_text(status_text),
        "place": redact_text(place),
        "location_label": redact_text(location_label) if location_label else None,
        "doing": redact_text(doing),
        "transit": (
            {
                "to": redact_text(transit["to"]) if transit["to"] else None,
                "eta_at_ms": transit["eta_at_ms"],
                "eta_jst": transit["eta_jst"],
                "text": redact_text(transit["text"]),
            }
            if transit
            else None
        ),
        "unconfirmed": unreachable,
        "eta_at_ms": eta_at_ms,
        "eta_jst": eta_jst,
        "return_by_ms": return_by_ms,
        "return_by_jst": return_by_jst,
        "occurred_at_ms": occurred_at_ms,
        "updated_at_ms": updated_at_ms,
        "updated_at_jst": updated_at_jst,
        "started_at_s": started_at_s,
        "until_at_s": until_at_s,
    }


def resolve_projection_conn(conn, actor_id, now=None):
    """Read-only city outing projection from an existing sqlite3 connection to ``life1.db``."""
    if not _has_table(conn, "city_visits"):
        return None
    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        visit = conn.execute(
            "SELECT visit_id, actor_id, state, plan, created_at_ms, updated_at_ms"
            " FROM city_visits WHERE actor_id=? AND state NOT IN ('reconcile','closed','aborted')"
            " ORDER BY updated_at_ms DESC, created_at_ms DESC LIMIT 1",
            (actor_id,),
        ).fetchone()
        if not visit:
            return None
        proj = None
        if _has_table(conn, "city_visit_projections"):
            proj = conn.execute(
                "SELECT visit_id, actor_id, last_seq, last_record_id, last_kind, occurred_at_ms,"
                " payload, engine_unreachable, unreachable_since_ms, updated_at_ms"
                " FROM city_visit_projections WHERE visit_id=?",
                (visit["visit_id"],),
            ).fetchone()
        return _build_projection(visit, proj, now=now)
    finally:
        conn.row_factory = prev_factory


def resolve_projection(store, actor_id, now=None):
    """Read-only city outing projection via ``Store``."""
    with store.read() as db:
        return resolve_projection_conn(db, actor_id, now=now)
