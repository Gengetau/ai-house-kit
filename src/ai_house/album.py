"""Scope-isolated resident photo album & diary illustration store (`album.db`, `0.3.0`)."""
from __future__ import annotations

import contextlib
import hashlib
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .media_safety import (
    DEFAULT_MAX_MEDIA_BYTES,
    media_ext_for_mime,
    mime_for_path,
    parse_data_uri,
    resolve_runtime_media_path,
    to_data_uri,
)

VALID_ALBUM_SCOPES = frozenset({"world", "group", "private"})

ALBUM_SCHEMA = """
CREATE TABLE IF NOT EXISTS album_items (
    album_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'photo',
    title TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    media_rel TEXT NOT NULL,
    mime TEXT NOT NULL DEFAULT 'image/png',
    diary_day TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_album_scope_time ON album_items(scope, actor, created_at DESC);
"""
MAX_CHAT_IMAGES_PER_CHANNEL = 200


class AlbumStore:
    """Isolated `album.db` persistence separating `world`, `group`, and `private` visual memories."""

    def __init__(self, runtime_dir: Path | str) -> None:
        self.runtime_dir = Path(runtime_dir).resolve()
        self.db_path = self.runtime_dir / "album.db"
        self._lock = threading.RLock()
        self.init_schema()

    def _conn(self) -> sqlite3.Connection:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(self.db_path), timeout=5)
        c.row_factory = sqlite3.Row
        return c

    def init_schema(self) -> None:
        with self._lock, contextlib.closing(self._conn()) as c:
            with c:
                c.executescript(ALBUM_SCHEMA)
                cols = {r[1] for r in c.execute("PRAGMA table_info(album_items)").fetchall()}
                if "sha256" not in cols:
                    c.execute("ALTER TABLE album_items ADD COLUMN sha256 TEXT NOT NULL DEFAULT ''")
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_album_dedup ON album_items(scope, channel, actor, kind, sha256)"
                )

    def reset(self) -> None:
        with self._lock:
            for p in (
                self.db_path,
                Path(str(self.db_path) + "-wal"),
                Path(str(self.db_path) + "-shm"),
            ):
                p.unlink(missing_ok=True)
            self.init_schema()

    def _validate_scope(self, scope: str) -> str:
        sc = str(scope or "").strip().lower()
        if sc not in VALID_ALBUM_SCOPES:
            raise ValueError(f"非法的相册作用域: {scope!r}")
        return sc

    def add_item(
        self,
        *,
        scope: str,
        actor: str = "",
        channel: str = "",
        kind: str = "photo",
        title: str = "",
        caption: str = "",
        raw_bytes: Optional[bytes] = None,
        data_uri: str = "",
        mime: str = "image/png",
        diary_day: str = "",
        now: float = 0.0,
    ) -> Dict[str, Any]:
        eff_scope = self._validate_scope(scope)
        clean_actor = str(actor or "").strip()
        if raw_bytes is None:
            if data_uri:
                parsed_mime, raw_bytes = parse_data_uri(data_uri, max_bytes=DEFAULT_MAX_MEDIA_BYTES)
                mime = parsed_mime
            else:
                raise ValueError("缺少图像二进制内容")
        if len(raw_bytes) > DEFAULT_MAX_MEDIA_BYTES or not raw_bytes:
            raise ValueError("图像大小无效")

        ext = media_ext_for_mime(mime, default="png")
        clean_channel = str(channel or "").strip()
        eff_kind = str(kind or "photo").strip()[:32]
        digest = hashlib.sha256(raw_bytes).hexdigest()

        self.init_schema()
        with self._lock, contextlib.closing(self._conn()) as c:
            existing = c.execute(
                "SELECT album_id, scope, actor, channel, kind, title, caption, media_rel, mime, diary_day, created_at "
                "FROM album_items WHERE scope=? AND channel=? AND actor=? AND kind=? AND sha256=? AND sha256!='' LIMIT 1",
                (eff_scope, clean_channel, clean_actor, eff_kind, digest),
            ).fetchone()
            if existing is not None:
                return self._hydrate_item(existing, include_data_uri=False)

            aid = f"alb-{int(now or 0)}-{digest[:8]}-{uuid.uuid4().hex[:6]}"
            rel_dir = self.runtime_dir / "media" / "album" / eff_scope
            rel_dir.mkdir(parents=True, exist_ok=True)
            target_path = rel_dir / f"{aid}.{ext}"
            target_path.write_bytes(raw_bytes)
            media_rel = f"media/album/{eff_scope}/{target_path.name}"
            eff_mime = str(mime or mime_for_path(target_path, "image/png"))
            clean_title = str(title or "").strip()[:120]
            clean_caption = str(caption or "").strip()[:240]
            clean_day = str(diary_day or "").strip()
            eff_now = float(now or 0.0)

            with c:
                c.execute(
                    "INSERT INTO album_items(album_id, scope, actor, channel, kind, title, caption, media_rel, mime, diary_day, sha256, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        aid,
                        eff_scope,
                        clean_actor,
                        clean_channel,
                        eff_kind,
                        clean_title,
                        clean_caption,
                        media_rel,
                        eff_mime,
                        clean_day,
                        digest,
                        eff_now,
                    ),
                )
                if eff_kind == "chat_image":
                    overflow = c.execute(
                        "SELECT album_id, media_rel FROM album_items "
                        "WHERE scope=? AND channel=? AND kind='chat_image' "
                        "ORDER BY created_at DESC, album_id DESC LIMIT -1 OFFSET ?",
                        (eff_scope, clean_channel, MAX_CHAT_IMAGES_PER_CHANNEL),
                    ).fetchall()
                    for ov in overflow:
                        c.execute("DELETE FROM album_items WHERE album_id=?", (ov["album_id"],))
                        still = c.execute(
                            "SELECT COUNT(*) FROM album_items WHERE media_rel=?", (ov["media_rel"],)
                        ).fetchone()[0]
                        if int(still) == 0:
                            try:
                                resolve_runtime_media_path(self.runtime_dir, str(ov["media_rel"])).unlink(missing_ok=True)
                            except ValueError:
                                pass

        res_doc: Dict[str, Any] = {
            "album_id": aid,
            "scope": eff_scope,
            "actor": clean_actor,
            "channel": clean_channel,
            "kind": eff_kind,
            "title": clean_title,
            "caption": clean_caption,
            "media_rel": media_rel,
            "mime": eff_mime,
            "diary_day": clean_day,
            "created_at": eff_now,
            "media_url": f"/api/album/media?path={quote(media_rel, safe='')}",
        }
        return res_doc

    def _hydrate_item(self, row: sqlite3.Row, *, include_data_uri: bool) -> Dict[str, Any]:
        doc = dict(row)
        if doc.get("media_rel"):
            doc["media_url"] = f"/api/album/media?path={quote(str(doc['media_rel']), safe='')}"
        if include_data_uri:
            try:
                fp = resolve_runtime_media_path(self.runtime_dir, doc["media_rel"])
                if fp.is_file():
                    doc["data_uri"] = to_data_uri(fp.read_bytes(), doc.get("mime") or "image/png")
            except Exception:  # noqa: BLE001
                doc["data_uri"] = ""
        return doc

    def list_items(
        self,
        *,
        scope: str = "world",
        actor: Optional[str] = None,
        channel: str = "",
        limit: int = 24,
        include_data_uri: bool = True,
    ) -> List[Dict[str, Any]]:
        eff_scope = self._validate_scope(scope)
        clean_limit = max(1, min(int(limit or 24), 100))
        clauses = ["scope=?"]
        params: List[Any] = [eff_scope]
        if actor:
            clauses.append("actor=?")
            params.append(str(actor).strip())
        if channel:
            clauses.append("channel=?")
            params.append(str(channel).strip())
        params.append(clean_limit)
        self.init_schema()
        with self._lock, contextlib.closing(self._conn()) as c:
            rows = c.execute(
                "SELECT album_id, scope, actor, channel, kind, title, caption, media_rel, mime, diary_day, created_at "
                f"FROM album_items WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, album_id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._hydrate_item(r, include_data_uri=include_data_uri) for r in rows]

    def get_item(
        self,
        album_id: str,
        *,
        expected_scope: Optional[str] = None,
        include_data_uri: bool = True,
    ) -> Optional[Dict[str, Any]]:
        aid = str(album_id or "").strip()
        if not aid:
            return None
        self.init_schema()
        with self._lock, contextlib.closing(self._conn()) as c:
            if expected_scope is not None:
                eff = self._validate_scope(expected_scope)
                row = c.execute(
                    "SELECT album_id, scope, actor, channel, kind, title, caption, media_rel, mime, diary_day, created_at "
                    "FROM album_items WHERE album_id=? AND scope=?",
                    (aid, eff),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT album_id, scope, actor, channel, kind, title, caption, media_rel, mime, diary_day, created_at "
                    "FROM album_items WHERE album_id=?",
                    (aid,),
                ).fetchone()
        if row is None:
            return None
        return self._hydrate_item(row, include_data_uri=include_data_uri)

    def get_by_media_rel(
        self,
        media_rel: str,
        *,
        include_data_uri: bool = False,
    ) -> Optional[Dict[str, Any]]:
        clean_rel = str(media_rel or "").strip().replace("\\", "/").lstrip("/")
        if not clean_rel:
            return None
        if not clean_rel.startswith("media/"):
            clean_rel = f"media/{clean_rel}"
        self.init_schema()
        with self._lock, contextlib.closing(self._conn()) as c:
            row = c.execute(
                "SELECT album_id, scope, actor, channel, kind, title, caption, media_rel, mime, diary_day, created_at "
                "FROM album_items WHERE media_rel=?",
                (clean_rel,),
            ).fetchone()
        if row is None:
            return None
        return self._hydrate_item(row, include_data_uri=include_data_uri)
