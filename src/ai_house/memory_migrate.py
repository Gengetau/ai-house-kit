"""Carry an existing house's memories from the old store (`memory.db`, written by the earlier self-contained
implementation) into the present one (`memory-common.db`, `CommonMemoryStore`).

    python3 -m ai_house.memory_migrate <data_dir> [--dry-run]

What it promises:

* `memory.db` is opened so that SQLite cannot write to it (`mode=ro`, `query_only`) and is never changed: it is the
  way back.
* Every memory goes in through `CommonMemoryStore.add` (and every pending line through `.observe`), so the shared
  module's own rules stay in force: its secret/instruction filter, its length limits, its kinds, its exact-text
  merge. What those calls cannot take (the times, the use count, the expiry of a diary, the vector, the trust level,
  the `about` of merged rows) is fixed up afterwards with plain SQL on the new row.
* A table `kit_migrated` in `memory-common.db` says which old row became which new row (and, for the trust level,
  what the old row said). Rows already in it are not written again, so a second run changes nothing, and a run that
  was cut off is finished by the next one.
* `dry_run=True` writes nothing in the data directory (SQLite itself may drop an already-emptied `-wal`/`-shm`
  left beside `memory-common.db` when the reader closes): the work is done on a throw-away copy of
  `memory-common.db` made in the system's temporary directory and the same report comes back.
* The report holds counts and old row numbers, never memory text.

What it does not do: edit anything that was changed in `memory.db` after a row was carried (a row is carried once);
carry what the old store no longer shows (invalidated, expired, archived rows), the earlier wordings (`mem_ver`),
the evidence links (`mem_src`), the lines already tidied into memories, or the old log.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time as _time
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from .memory_common import CommonMemoryStore

log = logging.getLogger("ai_house.memory_migrate")

OLD_FILE = "memory.db"
NEW_FILE = "memory-common.db"
TRUST_RANK = {"verified": 3, "group_stated": 2, "hearsay": 1}
_EMBED_ENV = ("AI_HOUSE_MEMORY_EMBED_URL", "AI_HOUSE_MEMORY_EMBED_API_KEY", "AI_HOUSE_MEMORY_EMBED_MODEL",
              "MEMORY_EMBED_URL")
_MEM_COLUMNS = ("id", "owner", "scope", "channel", "about", "kind", "trust", "text", "source", "status", "created",
                "updated", "last_used", "uses", "expires_at", "vec")
_SEEN_COLUMNS = ("id", "actor", "scope", "channel", "speaker", "speaker_role", "speaker_id", "line", "ts", "done")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# reasons a row is not carried although the old store still showed it (codes only, never text)
SKIP_REASONS = ("secret_or_instruction", "too_short", "diary_too_short", "diary_secret", "invalid_actor",
                "invalid_channel", "no_resident_for_shared_row", "refused_other", "line_not_kept")

_MAP_DDL = """
create table if not exists kit_migrated(
    what text not null,            -- 'mem' or 'seen'
    old_id integer not null,
    new_id integer,
    state text not null,           -- started / written / done / skipped
    outcome text not null default '',  -- added / merged / existing / skipped
    reason text not null default '',
    floor_id integer not null default 0,
    old_trust text not null default 'verified',
    old_kind text not null default '',
    trust_applied integer not null default 0,
    at real not null,
    primary key(what, old_id)
);
"""


# ---- small helpers --------------------------------------------------------------------------------------------

def _paths(data_dir: Union[str, Path]) -> Tuple[Path, Path]:
    base = Path(data_dir)
    if base.suffix.lower() in (".db", ".sqlite", ".sqlite3") and not base.is_dir():
        base = base.parent
    return base / OLD_FILE, base / NEW_FILE


def _open_old(path: Path) -> sqlite3.Connection:
    """The old file, opened so that nothing can be written to it (`mode=ro` and `query_only`). With no write-ahead log
    next to it (the old store checkpoints when it closes) it is opened `immutable`, which also keeps SQLite from
    leaving `-wal`/`-shm` files in the data directory; with a log it is opened plain read-only so the log is read."""
    wal = path.with_name(path.name + "-wal")
    mode = "mode=ro" if (wal.exists() and wal.stat().st_size > 0) else "mode=ro&immutable=1"
    uri = "file:" + urllib.parse.quote(str(path.resolve()), safe="/:") + "?" + mode
    con = sqlite3.connect(uri, uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("pragma query_only=1")
    return con


def _open_new(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        uri = "file:" + urllib.parse.quote(str(path.resolve()), safe="/:") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10.0)
        con.execute("pragma query_only=1")
    else:
        con = sqlite3.connect(str(path), timeout=10.0)
        con.execute("pragma journal_mode=wal")
        con.execute("pragma busy_timeout=10000")
    con.row_factory = sqlite3.Row
    return con


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone() is not None


def _read_table(con: sqlite3.Connection, table: str, wanted: Sequence[str]) -> List[Dict[str, Any]]:
    """Rows of an old table, tolerating files from before a column existed (those read as NULL)."""
    if not _has_table(con, table):
        return []
    have = {r[1] for r in con.execute(f"pragma table_info({table})")}
    cols = ", ".join(c if c in have else f"NULL as {c}" for c in wanted)
    return [dict(r) for r in con.execute(f"select {cols} from {table} order by id")]


def _num(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _trust_of(row: Mapping[str, Any]) -> str:
    t = str(row.get("trust") or "verified").strip().lower()
    return t if t in TRUST_RANK else "verified"


def _old_v3_done(old: sqlite3.Connection) -> bool:
    if not _has_table(old, "schema_migrations"):
        return False
    try:
        return old.execute("select 1 from schema_migrations where version>=3").fetchone() is not None
    except sqlite3.Error:
        return False


def _demote_like_v3(rows: List[Dict[str, Any]]) -> int:
    """An old file last opened by a version from before the old store's third schema step still calls every group
    memory verified. That step, which the old store ran on its next start, made the ones nobody had checked against
    the owner's words unverified claims; the old store is gone, so the same rule is applied to the rows as read."""
    n = 0
    for r in rows:
        if (str(r.get("scope")) == "group" and _trust_of(r) == "verified"
                and str(r.get("kind") or "fact") in ("fact", "preference", "person")
                and str(r.get("source") or "") not in ("auto:owner_verified", "owner_verified", "diary")):
            r["kind"], r["trust"] = "claim", "group_stated"
            n += 1
    return n


def _trust_on(env: Optional[Mapping[str, str]]) -> bool:
    e = os.environ if env is None else env
    return str(e.get("AI_HOUSE_MEMORY_TRUST") or "").strip() == "1"


def _clock_now(clock: Any, now: Optional[float]) -> float:
    if now is not None:
        return float(now)
    if clock is not None:
        return float(clock.now() if hasattr(clock, "now") else clock())
    return _time.time()


def _classify_refusal(exc: Exception, diary: bool) -> str:
    msg = str(exc)
    if "太短" in msg:
        return "diary_too_short" if diary else "too_short"
    if "指令" in msg or "隐私" in msg or "凭据" in msg:
        return "diary_secret" if diary else "secret_or_instruction"
    return "refused_other"


def _read_map(new_path: Path) -> Dict[Tuple[str, int], Dict[str, Any]]:
    if not new_path.exists():
        return {}
    try:
        con = _open_new(new_path, read_only=True)
    except sqlite3.Error:
        return {}
    try:
        if not _has_table(con, "kit_migrated"):
            return {}
        return {(r["what"], int(r["old_id"])): dict(r) for r in con.execute("select * from kit_migrated")}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def _backup(new_path: Path, now: float) -> Path:
    stamp = _time.strftime("%Y%m%d-%H%M%S", _time.gmtime(now))
    dest = new_path.with_name(f"{NEW_FILE}.bak-{stamp}")
    n = 0
    while dest.exists():
        n += 1
        dest = new_path.with_name(f"{NEW_FILE}.bak-{stamp}-{n}")
    src = _open_new(new_path, read_only=False)
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def _copy_aside(new_path: Path, tmp_dir: Path) -> None:
    """For a dry run: a consistent copy of the new file in a throw-away directory."""
    if not new_path.exists():
        return
    src = _open_new(new_path, read_only=True)
    try:
        out = sqlite3.connect(str(tmp_dir / NEW_FILE))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


# ---- the work -------------------------------------------------------------------------------------------------

class _Run:
    def __init__(self, store: CommonMemoryStore, new_path: Path, old_mem: Dict[int, Dict[str, Any]],
                 now: float, shared_actor: Optional[str]):
        self.store = store
        self.path = new_path
        self.old_mem = old_mem
        self.now = now
        self.shared_actor = shared_actor
        self.touched_mem: Dict[int, str] = {}   # old id -> outcome, rows handled in this run
        self.touched_seen: Dict[int, str] = {}
        self.shortened: List[int] = []
        self.kind_changed: Counter = Counter()
        self.vec_carried = 0

    @property
    def mod(self) -> Any:
        return self.store._mod   # the store may reload its copy when a housemate is added

    # -- map table -----------------------------------------------------------------------------------------------

    def con(self) -> sqlite3.Connection:
        return _open_new(self.path)

    def _max_id(self, con: sqlite3.Connection, table: str) -> int:
        return int(con.execute(f"select coalesce(max(id), 0) from {table}").fetchone()[0])

    def _begin(self, con: sqlite3.Connection, what: str, old_id: int, table: str, old_trust: str, old_kind: str) -> int:
        row = con.execute("select state, floor_id from kit_migrated where what=? and old_id=?", (what, old_id)).fetchone()
        if row is not None and row["state"] in ("started", "written"):
            return int(row["floor_id"])
        floor = self._max_id(con, table)
        with con:
            con.execute("insert or replace into kit_migrated(what, old_id, new_id, state, outcome, reason, floor_id, "
                        "old_trust, old_kind, trust_applied, at) values (?,?,NULL,'started','','',?,?,?,0,?)",
                        (what, old_id, floor, old_trust, old_kind, self.now))
        return floor

    def _skip(self, con: sqlite3.Connection, what: str, old_id: int, reason: str, old_trust: str = "verified",
              old_kind: str = "") -> None:
        with con:
            con.execute("insert or replace into kit_migrated(what, old_id, new_id, state, outcome, reason, floor_id, "
                        "old_trust, old_kind, trust_applied, at) values (?,?,NULL,'skipped','skipped',?,0,?,?,1,?)",
                        (what, old_id, reason, old_trust, old_kind, self.now))
        (self.touched_mem if what == "mem" else self.touched_seen)[old_id] = "skipped"

    # -- memories ------------------------------------------------------------------------------------------------

    def memory(self, r: Dict[str, Any], retry: bool) -> None:
        con = self.con()
        try:
            self._memory(con, r)
        finally:
            con.close()

    def _memory(self, con: sqlite3.Connection, r: Dict[str, Any]) -> None:
        oid = int(r["id"])
        old_trust, old_kind = _trust_of(r), str(r.get("kind") or "fact")
        shared = str(r["owner"]) == "shared"
        actor = self.shared_actor if shared else str(r["owner"])
        scope, channel, kind = str(r["scope"]), str(r["channel"] or ""), old_kind
        if shared and scope not in ("group", "world"):
            return self._skip(con, "mem", oid, "refused_other", old_trust, old_kind)
        if actor is None:
            return self._skip(con, "mem", oid, "no_resident_for_shared_row", old_trust, old_kind)
        try:
            CommonMemoryStore._valid_actor(actor)
        except ValueError:
            return self._skip(con, "mem", oid, "invalid_actor", old_trust, old_kind)
        try:
            CommonMemoryStore._valid_scope(scope)
            CommonMemoryStore._resolve_channel(actor, scope, channel)
        except ValueError:
            return self._skip(con, "mem", oid, "invalid_channel", old_trust, old_kind)

        floor = self._begin(con, "mem", oid, "mem", old_trust, old_kind)
        created = _num(r["created"], self.now)
        exp = r["expires_at"]
        text = str(r["text"] or "")
        diary = kind == "diary"
        new_id: Optional[int] = None
        how = ""
        try:
            if diary:
                # the shared module's diary entry has no merge: look first, so a diary is never written twice
                stored = _CONTROL_RE.sub(" ", text).strip()[:self.mod.DIARY_MAX]
                hit = con.execute("select id from mem where owner=? and kind='diary' and status='active' and text=? "
                                  "order by id limit 1", (actor, stored)).fetchone()
                if hit is not None:
                    new_id, how = int(hit["id"]), "same"
            if new_id is None:
                new_id, how = self.store.add(
                    actor, scope, text, channel=channel, about=str(r["about"] or ""), kind=kind, trust=old_trust,
                    shared=shared, expires_at=None if (diary or exp is None) else float(exp),
                    source=str(r["source"] or "self"), merge=True, now=created)
        except ValueError as exc:
            return self._skip(con, "mem", oid, _classify_refusal(exc, diary), old_trust, old_kind)
        new_id = int(new_id)

        others = con.execute("select 1 from kit_migrated where what='mem' and new_id=? and old_id!=? "
                             "and outcome in ('added','merged')", (new_id, oid)).fetchone()
        if others is not None:
            outcome = "merged"          # an earlier old row already became this new row
        elif how in ("same", "merged") and new_id <= floor:
            outcome = "existing"        # the new store had it before (not made by the migration)
        else:
            outcome = "added"
        with con:
            con.execute("update kit_migrated set new_id=?, state='written', outcome=? where what='mem' and old_id=?",
                        (new_id, outcome, oid))
        if outcome != "existing":
            self._fix_up(con, new_id)
            row = con.execute("select kind, text from mem where id=?", (new_id,)).fetchone()
            if outcome == "added" and row is not None:
                if row["kind"] != old_kind:
                    self.kind_changed[f"{old_kind}->{row['kind']}"] += 1
                if len(str(row["text"])) < len(text.strip()):
                    self.shortened.append(oid)
        with con:
            con.execute("update kit_migrated set state='done' where what='mem' and old_id=?", (oid,))
        self.touched_mem[oid] = outcome

    def _fix_up(self, con: sqlite3.Connection, new_id: int) -> None:
        """Set on the new row what `add` cannot take, from every old row that became it."""
        members = [int(m["old_id"]) for m in con.execute(
            "select old_id from kit_migrated where what='mem' and new_id=? and outcome in ('added','merged') "
            "and state in ('written','done') order by old_id", (new_id,))]
        olds = [self.old_mem[i] for i in members if i in self.old_mem]
        if not olds:
            return
        exps = [o["expires_at"] for o in olds]
        exp = None if any(e is None for e in exps) else max(float(e) for e in exps)
        vec = next((bytes(o["vec"]) for o in olds if o.get("vec") and len(o["vec"]) % 4 == 0), None)
        sets = {
            "created": min(_num(o["created"]) for o in olds),
            "updated": max(_num(o["updated"], _num(o["created"])) for o in olds),
            "last_used": max(_num(o["last_used"]) for o in olds),
            "uses": sum(int(_num(o["uses"])) for o in olds),
            "expires_at": exp,
        }
        if len(olds) > 1:
            parts: List[str] = []
            for o in olds:
                parts += str(o["about"] or "").split("|")
            sets["about"] = self.mod._about(parts)
        cols = {r[1] for r in con.execute("pragma table_info(mem)")}
        strongest = max((_trust_of(o) for o in olds), key=lambda t: TRUST_RANK[t])
        has_trust = "trust" in cols
        if has_trust:
            sets["trust"] = strongest
        with con:
            con.execute(f"update mem set {', '.join(k + '=?' for k in sets)} where id=?", [*sets.values(), new_id])
            if vec is not None:
                if con.execute("update mem set vec=? where id=? and vec is null", (vec, new_id)).rowcount:
                    self.vec_carried += 1
            applied = 1 if (has_trust or strongest == "verified") else 0
            con.execute("update kit_migrated set trust_applied=? where what='mem' and new_id=?", (applied, new_id))

    def trust_catch_up(self) -> int:
        """Rows carried while the trust switch was off: now that the column is there, write the old levels."""
        con = self.con()
        try:
            if "trust" not in {r[1] for r in con.execute("pragma table_info(mem)")}:
                return 0
            ids = sorted({int(r["new_id"]) for r in con.execute(
                "select new_id from kit_migrated where what='mem' and state='done' and trust_applied=0 "
                "and outcome in ('added','merged') and new_id is not null")})
            for nid in ids:
                self._fix_up(con, nid)
            return len(ids)
        finally:
            con.close()

    # -- pending lines -------------------------------------------------------------------------------------------

    def line(self, r: Dict[str, Any], retry: bool) -> None:
        con = self.con()
        try:
            self._line(con, r)
        finally:
            con.close()

    def _line(self, con: sqlite3.Connection, r: Dict[str, Any]) -> None:
        oid = int(r["id"])
        actor, scope, channel = str(r["actor"]), str(r["scope"]), str(r["channel"] or "")
        try:
            CommonMemoryStore._valid_actor(actor)
        except ValueError:
            return self._skip(con, "seen", oid, "invalid_actor")
        try:
            CommonMemoryStore._valid_scope(scope)
            CommonMemoryStore._resolve_channel(actor, scope, channel)
        except ValueError:
            return self._skip(con, "seen", oid, "invalid_channel")
        role = str(r["speaker_role"] or "member").strip().lower()
        speaker = str(r["speaker"] or "")
        line = str(r["line"] or "")
        ts = _num(r["ts"], self.now)
        if role == "self":
            prefix = f"[{speaker}(自己)]: "
            body = line[len(prefix):] if line.startswith(prefix) else line
        else:
            prefix = f"[{speaker}]: "
            body = line[len(prefix):] if line.startswith(prefix) else line

        floor = self._begin(con, "seen", oid, "seen", "verified", role)
        adopted = None
        for c in con.execute("select id, line from seen where id>? and consumer=? and ts=? order by id", (floor, actor, ts)):
            if str(c["line"]).endswith(body):
                adopted = int(c["id"])
                break
        if adopted is None:
            if role == "self":
                n = self.store.observe(actor, scope, "", channel=channel, reply=body, now=ts)
            else:
                n = self.store.observe(actor, scope, body, channel=channel, speaker=speaker, speaker_role=role,
                                       speaker_id=str(r["speaker_id"] or ""), now=ts)
            if not n:
                return self._skip(con, "seen", oid, "line_not_kept")
            adopted = self._max_id(con, "seen")
        with con:
            con.execute("update kit_migrated set new_id=?, state='done', outcome='added' where what='seen' and old_id=?",
                        (adopted, oid))
        self.touched_seen[oid] = "added"


def _plan(old_mem: List[Dict[str, Any]], old_seen: List[Dict[str, Any]], mapping: Mapping[Tuple[str, int], Dict[str, Any]],
          now: float, retry_skipped: bool) -> Dict[str, Any]:
    """Which old rows still need to be written, and the old rows the old store no longer shows."""
    not_carried: Dict[str, Counter] = {}
    eligible: List[Dict[str, Any]] = []
    for r in old_mem:
        status = str(r.get("status") or "active")
        exp = r.get("expires_at")
        if status == "active" and exp is not None and float(exp) <= now:
            status = "expired"
        if status != "active":
            not_carried.setdefault(status, Counter())[str(r.get("kind") or "fact")] += 1
        else:
            eligible.append(r)

    def todo(what: str, r: Mapping[str, Any]) -> bool:
        m = mapping.get((what, int(r["id"])))
        return m is None or m["state"] in ("started", "written") or (m["state"] == "skipped" and retry_skipped)

    pending = [r for r in old_seen if not int(_num(r.get("done")))]
    tidied = [r for r in old_seen if int(_num(r.get("done")))]
    return {
        "eligible": eligible,
        "mem_todo": [r for r in eligible if todo("mem", r)],
        "pending": pending,
        "seen_todo": [r for r in pending if todo("seen", r)],
        "not_carried": not_carried,
        "tidied": len(tidied),
    }


def _shared_actor(names: Mapping[str, str], eligible: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """Rows kept for the whole house (owner `shared`) are written by this housemate: the first of the given
    housemates, else the first one found in the old rows. Only the write log mentions her; the row is public."""
    for pool in (sorted(names), sorted({str(r["owner"]) for r in eligible if str(r["owner"]) != "shared"})):
        for a in pool:
            try:
                CommonMemoryStore._valid_actor(a)
                return a
            except ValueError:
                continue
    return None


def _process(new_path: Path, plan: Dict[str, Any], old_mem: Dict[int, Dict[str, Any]], env: Optional[Mapping[str, str]],
             names: Mapping[str, str], now: float, retry_skipped: bool) -> _Run:
    base_env = dict(os.environ if env is None else env)
    for k in _EMBED_ENV:       # the vectors come from the old rows; nothing is embedded here
        base_env.pop(k, None)
    actors = set(names)
    for r in plan["eligible"]:
        actors.add(str(r["owner"]))
    for r in plan["pending"]:
        actors.add(str(r["actor"]))
    actors.discard("shared")
    full: Dict[str, str] = {}
    for a in sorted(actors):
        try:
            CommonMemoryStore._valid_actor(a)
        except ValueError:
            continue
        full[a] = str(names.get(a, a))
    with sqlite3.connect(str(new_path)) as boot:   # the map table exists before the first row is written
        boot.executescript(_MAP_DDL)
    boot.close()
    store = CommonMemoryStore(new_path.parent, clock=lambda: now, names=full, env=base_env, embed_plugin=None)
    run = _Run(store, new_path, old_mem, now, _shared_actor(names, plan["eligible"]))
    try:
        for r in plan["mem_todo"]:
            run.memory(r, retry_skipped)
        for r in plan["seen_todo"]:
            run.line(r, retry_skipped)
        if _trust_on(env):
            run.trust_catch_up()
    finally:
        store.close()
    return run


# ---- entry points ---------------------------------------------------------------------------------------------

def migrate(data_dir: Union[str, Path], *, env: Optional[Mapping[str, str]] = None,
            names: Optional[Mapping[str, str]] = None, dry_run: bool = False, now: Optional[float] = None,
            clock: Any = None, retry_skipped: bool = False) -> Dict[str, Any]:
    """Carry the memories of `<data_dir>/memory.db` into `<data_dir>/memory-common.db` and report what happened.

    `env` and `names` are what the daemon hands its store (the house's settings and its housemates' names); `now` or
    `clock` is the house clock. Nothing is embedded and no model is called. See the module text for the promises."""
    old_path, new_path = _paths(data_dir)
    ts = _clock_now(clock, now)
    names = dict(names or {})
    report: Dict[str, Any] = {"dry_run": bool(dry_run), "old_file": OLD_FILE, "new_file": NEW_FILE,
                              "old_found": old_path.exists(), "wrote": False, "backup": None}
    if not old_path.exists():
        report["memories"] = _empty_counts()
        return report

    old = _open_old(old_path)
    try:
        old_mem_rows = _read_table(old, "mem", _MEM_COLUMNS)
        for r in old_mem_rows:  # a damaged expiry (text, not a time) never came due in the old store either
            try:
                r["expires_at"] = None if r.get("expires_at") is None else float(r["expires_at"])
            except (TypeError, ValueError):
                r["expires_at"] = None
        report["demoted"] = 0 if _old_v3_done(old) else _demote_like_v3(old_mem_rows)
        old_seen_rows = _read_table(old, "seen", _SEEN_COLUMNS)
        old_ver = int(old.execute("select count(*) from mem_ver").fetchone()[0]) if _has_table(old, "mem_ver") else 0
        old_src = int(old.execute("select count(*) from mem_src").fetchone()[0]) if _has_table(old, "mem_src") else 0
    finally:
        old.close()
    old_mem = {int(r["id"]): r for r in old_mem_rows}

    mapping = _read_map(new_path)
    plan = _plan(old_mem_rows, old_seen_rows, mapping, ts, retry_skipped)
    trust_work = _trust_on(env) and any(
        m["what"] == "mem" and m["state"] == "done" and not m["trust_applied"] and m["outcome"] in ("added", "merged")
        for m in mapping.values())
    work = bool(plan["mem_todo"] or plan["seen_todo"] or trust_work)

    run: Optional[_Run] = None
    final_path = new_path
    tmp: Optional[Path] = None
    if work:
        if dry_run:
            tmp = Path(tempfile.mkdtemp(prefix="memory-migrate-dry-"))
            _copy_aside(new_path, tmp)
            final_path = tmp / NEW_FILE
            report["backup"] = (f"{NEW_FILE}.bak-<时间>" if new_path.exists() else None)
        else:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            if new_path.exists():
                report["backup"] = _backup(new_path, ts).name
        try:
            run = _process(final_path, plan, old_mem, env, names, ts, retry_skipped)
            report["wrote"] = not dry_run
        except BaseException:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
            raise

    try:
        final_map = _read_map(final_path) if (work or new_path.exists()) else {}
        report.update(_report(plan, final_map, run, old_ver, old_src, old_mem_rows, env))
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    return report


def _empty_counts() -> Dict[str, Any]:
    return {"in_old_file": 0, "copied": 0, "merged_into_another_row": 0, "matched_existing_row": 0,
            "already_there": 0, "skipped": {}, "skipped_count": 0, "not_carried": {}}


def _report(plan: Dict[str, Any], mapping: Mapping[Tuple[str, int], Dict[str, Any]], run: Optional[_Run], old_ver: int,
            old_src: int, old_mem_rows: Sequence[Mapping[str, Any]], env: Optional[Mapping[str, str]]) -> Dict[str, Any]:
    def tally(what: str, rows: Sequence[Mapping[str, Any]], touched: Mapping[int, str]) -> Dict[str, Any]:
        out = _empty_counts()
        out["in_old_file"] = len(rows)
        skipped: Dict[str, List[int]] = {}
        for r in rows:
            oid = int(r["id"])
            m = mapping.get((what, oid))
            if m is None:
                continue
            if m["state"] == "skipped":
                skipped.setdefault(m["reason"], []).append(oid)
            elif m["state"] == "done":
                if oid in touched:
                    key = {"added": "copied", "merged": "merged_into_another_row", "existing": "matched_existing_row"}[m["outcome"]]
                    out[key] += 1
                else:
                    out["already_there"] += 1
        out["skipped"] = {k: sorted(v) for k, v in sorted(skipped.items())}
        out["skipped_count"] = sum(len(v) for v in skipped.values())
        out["waiting"] = sum(1 for r in rows if mapping.get((what, int(r["id"])), {}).get("state") in (None, "started", "written"))
        return out

    touched_mem = run.touched_mem if run else {}
    touched_seen = run.touched_seen if run else {}
    mem = tally("mem", plan["eligible"], touched_mem)
    mem["in_old_file"] = len(old_mem_rows)
    mem["not_carried"] = {s: {"total": sum(c.values()), "by_kind": dict(sorted(c.items()))}
                          for s, c in sorted(plan["not_carried"].items())}
    mem["earlier_wordings_not_carried"] = old_ver
    mem["shortened_to_new_limit"] = sorted(run.shortened) if run else []
    mem["kind_changed"] = dict(sorted(run.kind_changed.items())) if run else {}
    done_rows = [m for k, m in mapping.items() if k[0] == "mem" and m["state"] == "done"
                 and m["outcome"] in ("added", "merged")]
    wanted_vec = [r for r in plan["eligible"] if mapping.get(("mem", int(r["id"])), {}).get("state") == "done"]
    mem["vectors"] = {"in_old_rows": sum(1 for r in wanted_vec if r.get("vec")),
                      "carried_this_run": run.vec_carried if run else 0,
                      "left_empty": sum(1 for r in wanted_vec if not r.get("vec"))}
    mem["trust"] = {"switch_on": _trust_on(env),
                    "levels_below_verified": sum(1 for m in done_rows if m["old_trust"] != "verified"),
                    "waiting_for_the_switch": sum(1 for m in done_rows if not m["trust_applied"])}

    pend = tally("seen", plan["pending"], touched_seen)
    pend["not_carried"] = {"already_tidied": {"total": plan["tidied"]}} if plan["tidied"] else {}
    return {"memories": mem, "pending_lines": pend,
            "evidence_links": {"in_old_file": old_src, "carried": 0, "not_carried": old_src}}


def id_map(data_dir: Union[str, Path], what: str = "mem") -> Dict[int, int]:
    """old row number -> new row number, for the rows carried (a diary's `house_diaries.mem_id` is one of the old
    numbers). Empty when nothing was migrated."""
    _, new_path = _paths(data_dir)
    return {old: int(m["new_id"]) for (w, old), m in _read_map(new_path).items()
            if w == what and m["state"] == "done" and m["new_id"] is not None}


# ---- the report as sentences ----------------------------------------------------------------------------------

def describe(report: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    dry = "（试运行，什么都没有写）" if report.get("dry_run") else ""
    if not report.get("old_found"):
        return [f"没有找到旧记忆库 {report.get('old_file', OLD_FILE)}，什么都没有做。{dry}"]
    m, p = report["memories"], report["pending_lines"]
    out.append(f"旧记忆库里共 {m['in_old_file']} 条记忆；{dry}".rstrip("；") + "。")
    out.append(f"这次搬进新库 {m['copied']} 条，早先已经搬过 {m['already_there']} 条，"
               f"并入这次另一条的 {m['merged_into_another_row']} 条，新库里原来就有同样一条的 {m['matched_existing_row']} 条。")
    if m["skipped_count"]:
        parts = "；".join(f"{reason}：旧编号 {', '.join(map(str, ids))}" for reason, ids in m["skipped"].items())
        out.append(f"被新库拒收或不能搬的 {m['skipped_count']} 条（只列编号，不列内容）——{parts}。")
    if m.get("waiting"):
        out.append(f"还有 {m['waiting']} 条没搬完（上次中断，再运行一次即可）。")
    for status, info in m["not_carried"].items():
        kinds = "、".join(f"{k} {n}" for k, n in info["by_kind"].items())
        out.append(f"旧库已不再显示、没有搬的「{status}」记忆 {info['total']} 条（{kinds}）。")
    if m["earlier_wordings_not_carried"]:
        out.append(f"旧库保留的记忆旧版本 {m['earlier_wordings_not_carried']} 条没有搬。")
    if m["shortened_to_new_limit"]:
        out.append(f"{len(m['shortened_to_new_limit'])} 条超过新库的字数上限，被截短（旧编号 "
                   f"{', '.join(map(str, m['shortened_to_new_limit']))}）。")
    if m["kind_changed"]:
        out.append("类别按新库的规则改了：" + "、".join(f"{k} {n} 条" for k, n in m["kind_changed"].items()) + "。")
    v = m["vectors"]
    if v["left_empty"]:
        out.append(f"{v['left_empty']} 条记忆在旧库里没有向量，新库里也留空；开了向量服务后运行 backfill_vectors() 补上。")
    if v["in_old_rows"]:
        out.append(f"旧库里有向量的 {v['in_old_rows']} 条原样带过去（默认两边用同一个向量服务）。")
    if report.get("demoted"):
        out.append(f"旧库停在很早的版本，里面有 {report['demoted']} 条群里的记忆没有对过房主原话却标着已核实；"
                   "按旧库下一次启动本来会做的那样，当作未核实的说法带过去。")
    t = m["trust"]
    if t["levels_below_verified"]:
        if t["switch_on"] and not t["waiting_for_the_switch"]:
            out.append(f"{t['levels_below_verified']} 条不是「已核实」的记忆，可信度已写入新库。")
        else:
            out.append(f"{t['levels_below_verified']} 条记忆的旧可信度记在 kit_migrated 里；新库的可信度开关"
                       "（AI_HOUSE_MEMORY_TRUST=1）打开后再运行一次迁移就会写进去。")
    out.append(f"没整理成记忆的旧话：这次搬进 {p['copied']} 行，早先已搬 {p['already_there']} 行"
               + (f"，没搬成 {p['skipped_count']} 行" if p["skipped_count"] else "") + "。")
    for status, info in p["not_carried"].items():
        out.append(f"已经整理过的旧话 {info['total']} 行没有搬（它们的内容已在记忆里）。")
    e = report["evidence_links"]
    if e["not_carried"]:
        out.append(f"旧库里记忆与原话的出处关联 {e['not_carried']} 条没有搬（新库默认不开证据层）。")
    if report.get("backup"):
        out.append(f"写入前已把新库原有文件复制为 {report['backup']}。")
    out.append("旧记忆库 memory.db 没有被改动。")
    return out


def _parse_names(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in re.split(r"[,，]", raw or ""):
        if "=" in item:
            k, _, v = item.partition("=")
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m ai_house.memory_migrate",
                                 description="把旧记忆库 memory.db 里的记忆搬进新库 memory-common.db（旧库只读）。")
    ap.add_argument("data_dir", help="小屋的数据目录（里面有 memory.db）")
    ap.add_argument("--dry-run", action="store_true", help="试运行：不写任何东西，只汇报会怎样")
    ap.add_argument("--names", default="", help="住客名字，如 nia=妮娅,xingli=星璃（可省）")
    ap.add_argument("--retry-skipped", action="store_true", help="再试一次上回被拒收的行")
    ap.add_argument("--json", action="store_true", help="输出原始报告（JSON，不含记忆内容）")
    args = ap.parse_args(argv)
    rep = migrate(args.data_dir, env=None, names=_parse_names(args.names), dry_run=args.dry_run,
                  retry_skipped=args.retry_skipped)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("\n".join(describe(rep)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
