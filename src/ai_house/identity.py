"""Channel-agnostic identity registry and cross-platform external account bindings (`0.3.0`)."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .chat_identity import RESIDENT, Speaker, bot_speaker, clean_name, member_speaker, owner_speaker

VALID_IDENTITY_KINDS = ("owner", "resident", "visitor", "bot")
OWNER_IDENTITY_ID = "user:owner"

IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    identity_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('owner', 'resident', 'visitor', 'bot')),
    display_name TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_bindings (
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    identity_id TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(platform, external_id)
);
CREATE INDEX IF NOT EXISTS idx_identity_bindings_id ON identity_bindings(identity_id);
"""


class IdentityStore:
    """SQLite store mapping internal identities (`owner/resident/visitor/bot`) to channel accounts."""

    DB_FILENAME = "identity.db"

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        clock: Any = None,
        roster: Sequence[str] = (),
        names: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.db_path = self.runtime_dir / self.DB_FILENAME
        self._clock = clock
        self._lock = threading.RLock()
        self.migrate(roster=roster, names=names)

    def _now(self) -> float:
        return float(self._clock.now()) if self._clock is not None else time.time()

    @contextlib.contextmanager
    def _connect(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            with conn:
                yield conn
        finally:
            conn.close()

    def close(self) -> None:
        if not self.db_path.exists():
            return
        try:
            with self._connect() as c:
                c.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.Error:
            pass

    def migrate(
        self,
        *,
        roster: Sequence[str] = (),
        names: Optional[Mapping[str, str]] = None,
    ) -> None:
        now = self._now()
        with self._lock, self._connect() as c:
            c.executescript(IDENTITY_SCHEMA)
            c.execute(
                "INSERT OR IGNORE INTO identities(identity_id, kind, display_name, notes, created_at, updated_at) "
                "VALUES(?, 'owner', '房主', 'Primary owner identity', ?, ?)",
                (OWNER_IDENTITY_ID, now, now),
            )
            c.execute(
                "INSERT OR IGNORE INTO identity_bindings(platform, external_id, identity_id, meta_json, created_at, updated_at) "
                "VALUES('web', 'owner', ?, '{}', ?, ?)",
                (OWNER_IDENTITY_ID, now, now),
            )
        if roster:
            self.sync_residents(roster, names)

    def sync_residents(self, roster: Sequence[str], names: Optional[Mapping[str, str]] = None) -> None:
        now = self._now()
        name_map = dict(names or {})
        with self._lock, self._connect() as c:
            for actor in roster:
                aid = str(actor or "").strip()
                if not aid:
                    continue
                dname = str(name_map.get(aid) or aid).strip() or aid
                iid = f"resident:{aid}"
                c.execute(
                    "INSERT INTO identities(identity_id, kind, display_name, notes, created_at, updated_at) "
                    "VALUES(?, 'resident', ?, '', ?, ?) "
                    "ON CONFLICT(identity_id) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at",
                    (iid, dname, now, now),
                )

    def sync_onebot_owners(self, owner_qqs: Iterable[str]) -> None:
        now = self._now()
        wanted = {str(raw_qq or "").strip() for raw_qq in owner_qqs if str(raw_qq or "").strip()}
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT OR IGNORE INTO identities(identity_id, kind, display_name, notes, created_at, updated_at) "
                "VALUES(?, 'owner', '房主', 'Primary owner identity', ?, ?)",
                (OWNER_IDENTITY_ID, now, now),
            )
            existing_rows = c.execute(
                "SELECT external_id, identity_id, meta_json FROM identity_bindings WHERE platform='onebot'"
            ).fetchall()
            existing_by_qq: Dict[str, Dict[str, Any]] = {}
            for r in existing_rows:
                try:
                    meta = json.loads(r["meta_json"] or "{}")
                except ValueError:
                    meta = {}
                existing_by_qq[str(r["external_id"])] = {"identity_id": str(r["identity_id"]), "meta": meta}

            for ext_qq, info in existing_by_qq.items():
                if info["meta"].get("source") == "config" and ext_qq not in wanted:
                    c.execute("DELETE FROM identity_bindings WHERE platform='onebot' AND external_id=?", (ext_qq,))

            for qq in wanted:
                cur = existing_by_qq.get(qq)
                if cur is not None and cur["meta"].get("source") != "config":
                    # Preserve explicit manual bindings instead of overwriting them to owner
                    continue
                c.execute(
                    "INSERT INTO identity_bindings(platform, external_id, identity_id, meta_json, created_at, updated_at) "
                    "VALUES('onebot', ?, ?, '{\"source\":\"config\"}', ?, ?) "
                    "ON CONFLICT(platform, external_id) DO UPDATE SET identity_id=excluded.identity_id, "
                    "meta_json=excluded.meta_json, updated_at=excluded.updated_at",
                    (qq, OWNER_IDENTITY_ID, now, now),
                )

    def upsert_identity(
        self,
        identity_id: str,
        *,
        kind: str,
        display_name: str,
        notes: str = "",
    ) -> Dict[str, Any]:
        iid = str(identity_id or "").strip()
        if not iid:
            raise ValueError("identity_id 不能为空")
        k = str(kind or "").strip().lower()
        if k not in VALID_IDENTITY_KINDS:
            raise ValueError(f"无效的身份类别：{kind}")
        dname = str(display_name or "").strip() or iid
        now = self._now()
        with self._lock, self._connect() as c:
            c.execute(
                "INSERT INTO identities(identity_id, kind, display_name, notes, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(identity_id) DO UPDATE SET kind=excluded.kind, display_name=excluded.display_name, "
                "notes=excluded.notes, updated_at=excluded.updated_at",
                (iid, k, dname, str(notes or ""), now, now),
            )
        got = self.get_identity(iid)
        assert got is not None
        return got

    def bind_external(
        self,
        platform: str,
        external_id: str,
        identity_id: str,
        *,
        meta: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        plat = str(platform or "").strip().lower()
        ext = str(external_id or "").strip()
        iid = str(identity_id or "").strip()
        if not plat or not ext:
            raise ValueError("platform 与 external_id 不能为空")
        if not iid:
            raise ValueError("identity_id 不能为空")
        meta_s = json.dumps(dict(meta or {}), ensure_ascii=False)
        now = self._now()
        with self._lock, self._connect() as c:
            exists = c.execute("SELECT 1 FROM identities WHERE identity_id=?", (iid,)).fetchone()
            if not exists:
                raise ValueError(f"未知身份 ID：{iid}")
            c.execute(
                "INSERT INTO identity_bindings(platform, external_id, identity_id, meta_json, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(platform, external_id) DO UPDATE SET identity_id=excluded.identity_id, "
                "meta_json=excluded.meta_json, updated_at=excluded.updated_at",
                (plat, ext, iid, meta_s, now, now),
            )
        return {"platform": plat, "external_id": ext, "identity_id": iid, "meta": dict(meta or {})}

    def unbind_external(self, platform: str, external_id: str) -> bool:
        plat = str(platform or "").strip().lower()
        ext = str(external_id or "").strip()
        if not plat or not ext:
            return False
        with self._lock, self._connect() as c:
            cur = c.execute("DELETE FROM identity_bindings WHERE platform=? AND external_id=?", (plat, ext))
            return bool(cur.rowcount)

    def get_identity(self, identity_id: str) -> Optional[Dict[str, Any]]:
        iid = str(identity_id or "").strip()
        if not iid:
            return None
        with self._lock, self._connect() as c:
            row = c.execute("SELECT * FROM identities WHERE identity_id=?", (iid,)).fetchone()
            if row is None:
                return None
            binds = c.execute(
                "SELECT platform, external_id, meta_json, created_at, updated_at "
                "FROM identity_bindings WHERE identity_id=? ORDER BY platform, external_id",
                (iid,),
            ).fetchall()
        return {
            "identity_id": row["identity_id"],
            "kind": row["kind"],
            "display_name": row["display_name"],
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "bindings": [
                {
                    "platform": b["platform"],
                    "external_id": b["external_id"],
                    "meta": json.loads(b["meta_json"] or "{}"),
                }
                for b in binds
            ],
        }

    def list_identities(self, *, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as c:
            if kind:
                rows = c.execute("SELECT * FROM identities WHERE kind=? ORDER BY kind, identity_id", (kind,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM identities ORDER BY kind, identity_id").fetchall()
            bind_rows = c.execute(
                "SELECT platform, external_id, identity_id, meta_json FROM identity_bindings ORDER BY platform, external_id"
            ).fetchall()
        by_id: Dict[str, List[Dict[str, Any]]] = {}
        for b in bind_rows:
            by_id.setdefault(str(b["identity_id"]), []).append({
                "platform": b["platform"],
                "external_id": b["external_id"],
                "meta": json.loads(b["meta_json"] or "{}"),
            })
        return [
            {
                "identity_id": r["identity_id"],
                "kind": r["kind"],
                "display_name": r["display_name"],
                "notes": r["notes"],
                "bindings": by_id.get(str(r["identity_id"]), []),
            }
            for r in rows
        ]

    def external_targets_for(self, identity_id: str, *, platform: Optional[str] = None) -> List[Dict[str, Any]]:
        iid = str(identity_id or "").strip()
        if not iid:
            return []
        with self._lock, self._connect() as c:
            if platform:
                rows = c.execute(
                    "SELECT platform, external_id, meta_json FROM identity_bindings WHERE identity_id=? AND platform=?",
                    (iid, platform.strip().lower()),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT platform, external_id, meta_json FROM identity_bindings WHERE identity_id=? ORDER BY platform, external_id",
                    (iid,),
                ).fetchall()
        return [
            {"platform": r["platform"], "external_id": r["external_id"], "meta": json.loads(r["meta_json"] or "{}")}
            for r in rows
        ]

    def is_owner_external(self, platform: str, external_id: str) -> bool:
        plat = str(platform or "").strip().lower()
        ext = str(external_id or "").strip()
        if not plat or not ext:
            return False
        with self._lock, self._connect() as c:
            row = c.execute(
                "SELECT i.kind FROM identity_bindings b JOIN identities i ON i.identity_id=b.identity_id "
                "WHERE b.platform=? AND b.external_id=?",
                (plat, ext),
            ).fetchone()
        return bool(row is not None and row["kind"] == "owner")

    def resolve_speaker(
        self,
        platform: str,
        external_id: str,
        *,
        fallback_name: str = "",
        default_owner: bool = False,
    ) -> Speaker:
        plat = str(platform or "").strip().lower()
        ext = str(external_id or "").strip()
        fname = str(fallback_name or "").strip()
        now = self._now()
        with self._lock, self._connect() as c:
            row = (
                c.execute(
                    "SELECT i.identity_id, i.kind, i.display_name "
                    "FROM identity_bindings b JOIN identities i ON i.identity_id=b.identity_id "
                    "WHERE b.platform=? AND b.external_id=?",
                    (plat, ext),
                ).fetchone()
                if (plat and ext)
                else None
            )
            if row is not None:
                iid = str(row["identity_id"])
                kind = str(row["kind"])
                dname = str(row["display_name"] or "")
                if kind == "owner":
                    chosen = fname if (fname and fname != "房主") else (dname or "房主")
                    return owner_speaker(chosen, uid=ext, identity_id=iid)
                if kind == "bot":
                    return bot_speaker(fname or dname or "机器人", uid=ext, identity_id=iid)
                if kind == "resident":
                    return Speaker(RESIDENT, clean_name(dname or fname, "室友"), uid=ext, identity_id=iid)
                return member_speaker(fname or dname or "群友", uid=ext, identity_id=iid)

            if default_owner:
                c.execute(
                    "INSERT OR IGNORE INTO identities(identity_id, kind, display_name, notes, created_at, updated_at) "
                    "VALUES(?, 'owner', '房主', 'Primary owner identity', ?, ?)",
                    (OWNER_IDENTITY_ID, now, now),
                )
                if plat and ext:
                    c.execute(
                        "INSERT OR IGNORE INTO identity_bindings(platform, external_id, identity_id, meta_json, created_at, updated_at) "
                        "VALUES(?, ?, ?, '{}', ?, ?)",
                        (plat, ext, OWNER_IDENTITY_ID, now, now),
                    )
                return owner_speaker(fname or "房主", uid=ext, identity_id=OWNER_IDENTITY_ID)

            if plat and ext:
                visitor_id = f"visitor:{plat}:{ext}"
                c.execute(
                    "INSERT OR IGNORE INTO identities(identity_id, kind, display_name, notes, created_at, updated_at) "
                    "VALUES(?, 'visitor', ?, '', ?, ?)",
                    (visitor_id, clean_name(fname, ext), now, now),
                )
                c.execute(
                    "INSERT OR IGNORE INTO identity_bindings(platform, external_id, identity_id, meta_json, created_at, updated_at) "
                    "VALUES(?, ?, ?, '{}', ?, ?)",
                    (plat, ext, visitor_id, now, now),
                )
                return member_speaker(fname or ext, uid=ext, identity_id=visitor_id)

        return member_speaker(fname or "群友", uid="", identity_id="")
