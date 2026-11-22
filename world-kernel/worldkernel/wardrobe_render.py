"""Wardrobe selection snapshot, outbox relayer, and idempotent render ledger.

Design invariants:
- Only autonomous model selections (origin == "model") for enabled residents enter the render pipeline;
  routine dressing (origin == "routine") and unfinalized residents change clothes normally without rendering.
- All reference blobs (portrait.png + linked garment asset images) are content-addressed and frozen at swap time
  under wardrobe-render-blobs/sha256/<2>/<64>.
- wardrobe_render.db holds outbox_cursor, jobs, idempotent attempt requests, optional budget counters, and cache.
- A partial unique index on wardrobe_render_requests enforces at most one in-flight external submit per job_id
  (submitting / submitted / unknown_pending_reconcile) so uncertain timeouts/5xx errors never double-pay.
- Publish (generated_staged -> publishing -> completed) is decoupled from API submission.
"""
import contextlib
import datetime
import errno
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

from .statuscard import JST, outfit_id

SHA256_REF_RE = re.compile(r"^sha256/([0-9a-f]{2})/([0-9a-f]{64})$")
SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_ORIGINS = frozenset(("model", "routine"))

KERNEL_OUTBOX_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS garment_asset_links ("
    "garment_id TEXT PRIMARY KEY, "
    "actor TEXT NOT NULL, "
    "asset_id TEXT NOT NULL, "
    "asset_part TEXT NOT NULL, "
    "asset_sha256 TEXT, "
    "blob_ref TEXT, "
    "shelf_name TEXT NOT NULL DEFAULT '未分类', "
    "claimed_at REAL NOT NULL, "
    "claim_event_id INTEGER)",
    "CREATE TABLE IF NOT EXISTS wardrobe_render_outbox ("
    "outbox_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "event_id INTEGER UNIQUE NOT NULL, "
    "actor TEXT NOT NULL, "
    "outfit_key TEXT NOT NULL, "
    "items_json TEXT NOT NULL, "
    "occasion TEXT, "
    "origin TEXT NOT NULL, "
    "render_eligible INTEGER NOT NULL DEFAULT 0, "
    "skip_reason TEXT, "
    "selection_hash TEXT NOT NULL, "
    "snapshot_json TEXT NOT NULL, "
    "created_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_wardrobe_outbox_actor_time ON wardrobe_render_outbox(actor, created_at)",
)

LEDGER_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS outbox_cursor ("
    "consumer TEXT PRIMARY KEY, "
    "last_outbox_id INTEGER NOT NULL, "
    "updated_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS wardrobe_render_jobs ("
    "job_id TEXT PRIMARY KEY, "
    "outbox_id INTEGER UNIQUE NOT NULL, "
    "actor TEXT NOT NULL, "
    "outfit_key TEXT NOT NULL, "
    "occasion TEXT, "
    "event_id INTEGER, "
    "selection_hash TEXT NOT NULL, "
    "snapshot_json TEXT NOT NULL, "
    "status TEXT NOT NULL, "
    "created_at REAL NOT NULL, "
    "debounce_until REAL NOT NULL, "
    "attempts INTEGER NOT NULL DEFAULT 0, "
    "max_attempts INTEGER NOT NULL DEFAULT 3, "
    "claim_token TEXT, "
    "claimed_at REAL, "
    "heartbeat_at REAL, "
    "staged_image_path TEXT, "
    "staged_sha256 TEXT, "
    "published_image_path TEXT, "
    "executor_lock_path TEXT, "
    "error_code TEXT, "
    "error_message TEXT, "
    "updated_at REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_wardrobe_jobs_status ON wardrobe_render_jobs(status, debounce_until)",
    "CREATE INDEX IF NOT EXISTS idx_wardrobe_jobs_actor_status ON wardrobe_render_jobs(actor, status)",
    "CREATE TABLE IF NOT EXISTS wardrobe_render_requests ("
    "request_id TEXT PRIMARY KEY, "
    "job_id TEXT NOT NULL, "
    "attempt_no INTEGER NOT NULL, "
    "attempt_key TEXT NOT NULL UNIQUE, "
    "selection_hash TEXT NOT NULL, "
    "status TEXT NOT NULL, "
    "cost_usd REAL, "
    "provider_request_id TEXT, "
    "executor_lock_path TEXT, "
    "expected_staged_path TEXT, "
    "response_sha256 TEXT, "
    "error_code TEXT, "
    "error_message TEXT, "
    "created_at REAL NOT NULL, "
    "updated_at REAL NOT NULL, "
    "UNIQUE(job_id, attempt_no))",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_wardrobe_req_inflight "
    "ON wardrobe_render_requests(job_id) "
    "WHERE status IN ('submitting', 'submitted', 'unknown_pending_reconcile')",
    "CREATE TABLE IF NOT EXISTS wardrobe_render_budget ("
    "day_jst TEXT NOT NULL, "
    "actor TEXT NOT NULL, "
    "reserved_count INTEGER NOT NULL DEFAULT 0, "
    "succeeded_count INTEGER NOT NULL DEFAULT 0, "
    "uncertain_count INTEGER NOT NULL DEFAULT 0, "
    "failed_count INTEGER NOT NULL DEFAULT 0, "
    "PRIMARY KEY (day_jst, actor))",
    "CREATE TABLE IF NOT EXISTS outfit_render_cache ("
    "cache_key TEXT PRIMARY KEY, "
    "actor TEXT NOT NULL, "
    "outfit_key TEXT NOT NULL, "
    "selection_hash TEXT NOT NULL, "
    "image_path TEXT NOT NULL, "
    "image_sha256 TEXT NOT NULL, "
    "job_id TEXT NOT NULL, "
    "updated_at REAL NOT NULL)",
)


class BlobIntegrityError(ValueError):
    """Raised when a frozen reference blob is missing, malformed, or fails SHA-256 verification."""


class LedgerStateError(RuntimeError):
    """Raised when a ledger state transition violates anti-double-pay or lock invariants."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_bytes(dst_path: str, data: bytes, mode: int = 0o644) -> None:
    parent = os.path.dirname(os.path.abspath(dst_path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, dst_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def freeze_bytes(data: bytes, blob_root: str) -> tuple[str, str]:
    """Store bytes immutably under blob_root/sha256/<2>/<64>. Returns (blob_ref, sha256)."""
    if not data:
        raise BlobIntegrityError("empty_blob")
    digest = sha256_bytes(data)
    rel_ref = f"sha256/{digest[:2]}/{digest}"
    dst = os.path.join(blob_root, rel_ref)
    if os.path.exists(dst):
        if sha256_file(dst) != digest:
            raise BlobIntegrityError(f"existing_blob_corrupt:{rel_ref}")
        return rel_ref, digest
    atomic_write_bytes(dst, data)
    return rel_ref, digest


def freeze_file(src_path: str, blob_root: str, *, expected_sha256: str | None = None) -> tuple[str, str]:
    """Read src_path, verify optional expected SHA-256, and freeze it into blob_root."""
    if not src_path or not os.path.isfile(src_path):
        raise BlobIntegrityError(f"missing_source_file:{src_path}")
    with open(src_path, "rb") as f:
        data = f.read()
    digest = sha256_bytes(data)
    if expected_sha256 and digest != expected_sha256:
        raise BlobIntegrityError(f"sha256_mismatch:{digest}")
    rel_ref, _ = freeze_bytes(data, blob_root)
    return rel_ref, digest


def resolve_blob_path(blob_root: str, blob_ref: str) -> str:
    """Resolve a relative sha256/<2>/<64> ref safely inside blob_root."""
    if not isinstance(blob_ref, str):
        raise BlobIntegrityError("bad_blob_ref")
    m = SHA256_REF_RE.fullmatch(blob_ref)
    if not m or m.group(2)[:2] != m.group(1):
        raise BlobIntegrityError(f"malformed_blob_ref:{blob_ref}")
    root_real = os.path.realpath(blob_root)
    cand_real = os.path.realpath(os.path.join(root_real, blob_ref))
    if not cand_real.startswith(root_real + os.sep):
        raise BlobIntegrityError(f"blob_path_escape:{blob_ref}")
    return cand_real


def verify_blob(blob_root: str, blob_ref: str, expected_sha256: str) -> str:
    """Verify blob_ref exists under blob_root and matches expected_sha256. Returns real path."""
    if not isinstance(expected_sha256, str) or not SHA256_HEX_RE.fullmatch(expected_sha256):
        raise BlobIntegrityError("bad_expected_sha256")
    path = resolve_blob_path(blob_root, blob_ref)
    if not os.path.isfile(path):
        raise BlobIntegrityError(f"missing_blob:{blob_ref}")
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise BlobIntegrityError(f"blob_sha256_mismatch:{blob_ref}")
    return path


def resolve_asset_image_path(spec: dict, asset_root: str | None) -> str | None:
    """Resolve an asset's image path, preventing traversal outside asset_root when relative."""
    raw = spec.get("image_path") or spec.get("image")
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return None
    if os.path.isabs(raw):
        return os.path.realpath(raw) if asset_root is None else (
            os.path.realpath(raw) if os.path.realpath(raw).startswith(os.path.realpath(asset_root) + os.sep) else None
        )
    if not asset_root:
        return None
    base = os.path.realpath(asset_root)
    cand = os.path.realpath(os.path.join(base, raw))
    return cand if cand.startswith(base + os.sep) else None


def _canonical_hash(payload: dict) -> str:
    return sha256_bytes(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def build_swap_snapshot(
    *,
    actor: str,
    items: list[str],
    garment_rows: dict[str, dict],
    links_by_gid: dict[str, dict],
    shelf_assets: dict[str, dict],
    occasion: str | None,
    origin: str,
    render_actors: frozenset[str],
    blob_root: str | None,
    profiles_dir: str | None,
    asset_root: str | None,
    require_display_look: bool = True,
    require_garment_assets: bool = True,
) -> tuple[bool, str | None, str, str]:
    """Freeze swap-time inputs for wardrobe_render_outbox without failing the clothes swap.

    Returns (render_eligible, skip_reason, selection_hash, snapshot_json).
    """
    sorted_items = sorted(str(i) for i in items)
    oid = outfit_id(sorted_items)
    outfit_key = f"{actor}|{oid}"
    labels = [garment_rows[i]["label"] for i in items if i in garment_rows]
    categories = [garment_rows[i]["category"] for i in items if i in garment_rows]
    base = {
        "actor": actor,
        "outfit_key": outfit_key,
        "outfit_comma_key": f"{actor}|{','.join(sorted_items)}",
        "items": list(items),
        "sorted_items": sorted_items,
        "labels": labels,
        "categories": categories,
        "occasion": occasion,
        "origin": origin,
    }

    def ineligible(reason: str) -> tuple[bool, str, str, str]:
        snap = dict(base, skip_reason=reason)
        return False, reason, _canonical_hash(snap), json.dumps(snap, ensure_ascii=False, sort_keys=True)

    if origin != "model":
        return ineligible("origin_not_model")
    if actor not in render_actors:
        return ineligible("actor_not_enabled")
    if not blob_root or not profiles_dir:
        return ineligible("render_disabled")

    portrait_path = os.path.join(profiles_dir, actor, "portrait.png")
    if not os.path.isfile(portrait_path):
        return ineligible("missing_portrait")

    look_text = ""
    display_path = os.path.join(profiles_dir, actor, "display.json")
    if os.path.isfile(display_path):
        try:
            with open(display_path, encoding="utf-8") as f:
                disp = json.load(f)
            if isinstance(disp, dict) and isinstance(disp.get("look"), str):
                look_text = disp["look"].strip()
        except (OSError, ValueError):
            look_text = ""
    if require_display_look and not look_text:
        return ineligible("missing_display_look")

    try:
        portrait_ref, portrait_sha = freeze_file(portrait_path, blob_root)
    except (OSError, BlobIntegrityError):
        return ineligible("portrait_freeze_failed")

    asset_entries = []
    seen_asset_ids = set()
    for gid in items:
        link = links_by_gid.get(gid)
        if not link:
            continue
        aid = link["asset_id"]
        if aid in seen_asset_ids:
            continue
        seen_asset_ids.add(aid)
        blob_ref = link.get("blob_ref")
        asset_sha = link.get("asset_sha256")
        if blob_ref and asset_sha:
            try:
                verify_blob(blob_root, blob_ref, asset_sha)
            except BlobIntegrityError:
                return ineligible(f"missing_asset_blob:{aid}")
        else:
            spec = shelf_assets.get(aid) or {}
            img_path = resolve_asset_image_path(spec, asset_root)
            if not img_path or not os.path.isfile(img_path):
                return ineligible(f"missing_asset_blob:{aid}")
            try:
                blob_ref, asset_sha = freeze_file(img_path, blob_root, expected_sha256=spec.get("sha256"))
            except (OSError, BlobIntegrityError):
                return ineligible(f"missing_asset_blob:{aid}")
        asset_entries.append({
            "asset_id": aid,
            "asset_part": link.get("asset_part") or aid,
            "blob_ref": blob_ref,
            "sha256": asset_sha,
        })

    if require_garment_assets and not asset_entries:
        return ineligible("no_garment_assets")

    snapshot = dict(
        base,
        portrait={"blob_ref": portrait_ref, "sha256": portrait_sha},
        look=look_text,
        assets=asset_entries,
    )
    sel_hash = _canonical_hash({
        "actor": actor,
        "outfit_key": outfit_key,
        "sorted_items": sorted_items,
        "portrait_sha256": portrait_sha,
        "look": look_text,
        "assets": [(a["asset_id"], a["sha256"]) for a in asset_entries],
    })
    return True, None, sel_hash, json.dumps(snapshot, ensure_ascii=False, sort_keys=True)


def day_jst(ts: float | None = None) -> str:
    t = time.time() if ts is None else ts
    return datetime.datetime.fromtimestamp(t, JST).strftime("%Y-%m-%d")


def _acquire_lock_nb(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _release_lock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _is_executor_alive(lock_path: str | None) -> bool | None:
    """Check whether an external executor still holds lock_path.

    Returns True if locked by a live executor, False if lock exists and was
    acquired+released (proving executor exited), or None if no lock info exists.
    """
    if not lock_path:
        return None
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            _acquire_lock_nb(fd)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return True
            return None
        with contextlib.suppress(OSError):
            _release_lock(fd)
        return False
    finally:
        os.close(fd)


class WardrobeRenderLedger:
    """Separate SQLite ledger (wardrobe_render.db) owning outbox cursor, jobs, requests, budget, cache."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.db = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        if db_path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        with self.tx() as c:
            for stmt in LEDGER_SCHEMA:
                c.execute(stmt)
            self._ensure_columns(c)

    @staticmethod
    def _ensure_columns(c: sqlite3.Connection) -> None:
        upgrades = {
            "wardrobe_render_jobs": (("executor_lock_path", "TEXT"),),
            "wardrobe_render_requests": (
                ("executor_lock_path", "TEXT"),
                ("expected_staged_path", "TEXT"),
                ("response_sha256", "TEXT"),
            ),
        }
        for table, cols in upgrades.items():
            existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, typ in cols:
                if col not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    def _shift_budget(
        self, c: sqlite3.Connection, *, actor: str, budget_day: str, dec_col: str | None, inc_col: str
    ) -> None:
        self._ensure_budget_row(c, budget_day, actor)
        if dec_col:
            c.execute(
                f"UPDATE wardrobe_render_budget SET "
                f"{dec_col} = MAX(0, {dec_col} - 1), {inc_col} = {inc_col} + 1 "
                f"WHERE day_jst=? AND actor=?",
                (budget_day, actor),
            )
        else:
            c.execute(
                f"UPDATE wardrobe_render_budget SET {inc_col} = {inc_col} + 1 WHERE day_jst=? AND actor=?",
                (budget_day, actor),
            )

    def close(self) -> None:
        self.db.close()

    @contextlib.contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self.db.execute("ROLLBACK")
            raise

    def _cursor(self, c) -> int:
        row = c.execute("SELECT last_outbox_id FROM outbox_cursor WHERE consumer='wardrobe_render'").fetchone()
        return int(row["last_outbox_id"]) if row else 0

    def relay_outbox(
        self,
        life_db: str | sqlite3.Connection,
        *,
        now: float | None = None,
        debounce_seconds: float = 120.0,
        gallery_dir: str | None = None,
    ) -> dict:
        """Read new wardrobe_render_outbox rows (RO when given path) and enqueue/debounce render jobs."""
        t = time.time() if now is None else float(now)
        last_id = self._cursor(self.db)

        close_src = False
        if isinstance(life_db, sqlite3.Connection):
            src = life_db
        else:
            src = sqlite3.connect(Path(life_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            src.row_factory = sqlite3.Row
            src.execute("PRAGMA query_only=1")
            src.execute("BEGIN")
            close_src = True
        try:
            rows = src.execute(
                "SELECT outbox_id, event_id, actor, outfit_key, occasion, origin, render_eligible, "
                "selection_hash, snapshot_json, created_at "
                "FROM wardrobe_render_outbox WHERE outbox_id > ? ORDER BY outbox_id ASC",
                (last_id,),
            ).fetchall()
            rows = [dict(r) for r in rows]
        finally:
            if close_src:
                src.close()

        if not rows:
            return {"advanced_to": last_id, "queued": 0, "superseded": 0, "cache_hits": 0, "skipped": 0}

        queued, superseded, cache_hits, skipped = 0, 0, 0, 0
        max_id = last_id
        with self.tx() as c:
            for r in rows:
                oid = int(r["outbox_id"])
                if oid > max_id:
                    max_id = oid
                if int(r["render_eligible"]) != 1 or r["origin"] != "model":
                    skipped += 1
                    continue
                actor = r["actor"]
                # Supersede any unstarted queued jobs for this actor (debounce rapid swaps in one burst).
                cur = c.execute(
                    "UPDATE wardrobe_render_jobs SET status='superseded', updated_at=? "
                    "WHERE actor=? AND status='queued'",
                    (t, actor),
                )
                superseded += cur.rowcount
                cache_key = f"{r['outfit_key']}|{r['selection_hash']}"
                cached = c.execute(
                    "SELECT image_path FROM outfit_render_cache WHERE cache_key=?", (cache_key,)
                ).fetchone()
                if cached:
                    img_ok = True if gallery_dir is None else os.path.isfile(cached["image_path"])
                    if img_ok:
                        cache_hits += 1
                        continue
                job_id = f"wr-{oid}"
                debounce_until = float(r["created_at"]) + float(debounce_seconds)
                c.execute(
                    "INSERT OR IGNORE INTO wardrobe_render_jobs("
                    "job_id, outbox_id, actor, outfit_key, occasion, event_id, selection_hash, snapshot_json, "
                    "status, created_at, debounce_until, attempts, max_attempts, updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?, ?,?,0,3,?)",
                    (
                        job_id, oid, actor, r["outfit_key"], r["occasion"], r["event_id"],
                        r["selection_hash"], r["snapshot_json"], "queued",
                        float(r["created_at"]), debounce_until, t,
                    ),
                )
                queued += c.execute("SELECT changes()").fetchone()[0]
            c.execute(
                "INSERT OR REPLACE INTO outbox_cursor(consumer, last_outbox_id, updated_at) VALUES('wardrobe_render', ?, ?)",
                (max_id, t),
            )
        return {
            "advanced_to": max_id,
            "queued": queued,
            "superseded": superseded,
            "cache_hits": cache_hits,
            "skipped": skipped,
        }

    def _recover_stale_submitting_tx(
        self,
        c: sqlite3.Connection,
        *,
        now: float,
        submit_lease_seconds: float = 600.0,
    ) -> list[str]:
        """Move orphaned submitting jobs (no live executor) to unknown_pending_reconcile without blind retry."""
        recovered: list[str] = []
        rows = c.execute(
            "SELECT * FROM wardrobe_render_jobs WHERE status='submitting' ORDER BY created_at ASC, outbox_id ASC"
        ).fetchall()
        for job in rows:
            req = c.execute(
                "SELECT * FROM wardrobe_render_requests "
                "WHERE job_id=? AND status IN ('submitting', 'submitted') ORDER BY attempt_no DESC LIMIT 1",
                (job["job_id"],),
            ).fetchone()
            lock_path = (req["executor_lock_path"] if req else None) or job["executor_lock_path"]
            alive = _is_executor_alive(lock_path)
            if alive is True:
                continue
            if alive is None:
                hb = job["heartbeat_at"] if job["heartbeat_at"] is not None else job["updated_at"]
                if hb is None or float(hb) >= now - float(submit_lease_seconds):
                    continue
            staged_path = job["staged_image_path"]
            staged_sha = job["staged_sha256"]
            err_code = "lost_submitting_reconcile"
            err_msg = "executor lost while submitting before result recorded"
            exp_path = req["expected_staged_path"] if req else None
            if (not staged_path or not staged_sha) and exp_path and os.path.isfile(exp_path) and os.path.getsize(exp_path) > 0:
                staged_path = exp_path
                staged_sha = sha256_file(exp_path)
                err_code = "lost_submitting_staged_recovered"
                err_msg = "executor lost after staging image before ledger commit"
            if req:
                c.execute(
                    "UPDATE wardrobe_render_requests SET status='unknown_pending_reconcile', error_code=?, "
                    "error_message=?, response_sha256=COALESCE(response_sha256, ?), updated_at=? WHERE request_id=?",
                    (err_code[:60], err_msg[:200], staged_sha, now, req["request_id"]),
                )
            c.execute(
                "UPDATE wardrobe_render_jobs SET status='unknown_pending_reconcile', claim_token=NULL, "
                "executor_lock_path=NULL, staged_image_path=?, staged_sha256=?, error_code=?, error_message=?, "
                "updated_at=? WHERE job_id=?",
                (staged_path, staged_sha, err_code[:60], err_msg[:200], now, job["job_id"]),
            )
            budget_day = day_jst(float(req["created_at"])) if req else day_jst(float(job["heartbeat_at"] or job["updated_at"] or now))
            self._shift_budget(
                c, actor=job["actor"], budget_day=budget_day,
                dec_col="reserved_count", inc_col="uncertain_count",
            )
            recovered.append(job["job_id"])
        return recovered

    def recover_stale_submitting(
        self,
        *,
        now: float | None = None,
        submit_lease_seconds: float = 600.0,
    ) -> list[str]:
        t = time.time() if now is None else float(now)
        with self.tx() as c:
            return self._recover_stale_submitting_tx(c, now=t, submit_lease_seconds=submit_lease_seconds)

    def claim_next_job(
        self,
        *,
        now: float | None = None,
        lease_seconds: float = 300.0,
        submit_lease_seconds: float = 600.0,
        blob_root: str | None = None,
    ) -> dict | None:
        """Claim the oldest ready queued job, verifying frozen blobs before returning."""
        t = time.time() if now is None else float(now)
        with self.tx() as c:
            # Stale pre-HTTP 'claimed' leases return to 'queued'; lost 'submitting' moves to unknown_pending_reconcile.
            c.execute(
                "UPDATE wardrobe_render_jobs SET status='queued', claim_token=NULL, updated_at=? "
                "WHERE status='claimed' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?",
                (t, t - float(lease_seconds)),
            )
            self._recover_stale_submitting_tx(c, now=t, submit_lease_seconds=submit_lease_seconds)
            row = c.execute(
                "SELECT * FROM wardrobe_render_jobs "
                "WHERE status='queued' AND debounce_until <= ? "
                "ORDER BY created_at ASC, outbox_id ASC LIMIT 1",
                (t,),
            ).fetchone()
            if not row:
                return None
            job = dict(row)
            if blob_root:
                try:
                    snap = json.loads(job["snapshot_json"])
                    p = snap.get("portrait") or {}
                    verify_blob(blob_root, p.get("blob_ref"), p.get("sha256"))
                    for a in snap.get("assets") or ():
                        verify_blob(blob_root, a.get("blob_ref"), a.get("sha256"))
                except (ValueError, TypeError, BlobIntegrityError) as e:
                    c.execute(
                        "UPDATE wardrobe_render_jobs SET status='failed', error_code='blob_integrity_failed', "
                        "error_message=?, updated_at=? WHERE job_id=?",
                        (str(e)[:200], t, job["job_id"]),
                    )
                    return None
            token = uuid.uuid4().hex
            c.execute(
                "UPDATE wardrobe_render_jobs SET status='claimed', claim_token=?, claimed_at=?, heartbeat_at=?, "
                "updated_at=? WHERE job_id=? AND status='queued'",
                (token, t, t, t, job["job_id"]),
            )
            job["status"] = "claimed"
            job["claim_token"] = token
            return job

    def _ensure_budget_row(self, c, d: str, actor: str) -> dict:
        c.execute(
            "INSERT OR IGNORE INTO wardrobe_render_budget(day_jst, actor) VALUES(?, ?)", (d, actor)
        )
        return dict(c.execute(
            "SELECT * FROM wardrobe_render_budget WHERE day_jst=? AND actor=?", (d, actor)
        ).fetchone())

    def begin_submit(
        self,
        job_id: str,
        claim_token: str,
        *,
        now: float | None = None,
        daily_cap: int | None = None,
        executor_lock_path: str | None = None,
        expected_staged_path: str | None = None,
    ) -> int:
        """Transition claimed -> submitting atomically with an idempotent request row (no double-pay)."""
        t = time.time() if now is None else float(now)
        d = day_jst(t)
        with self.tx() as c:
            job = c.execute("SELECT * FROM wardrobe_render_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job or job["status"] != "claimed" or job["claim_token"] != claim_token:
                raise LedgerStateError("job_not_claimed")
            inflight = c.execute(
                "SELECT request_id, status FROM wardrobe_render_requests "
                "WHERE job_id=? AND status IN ('submitting', 'submitted', 'unknown_pending_reconcile')",
                (job_id,),
            ).fetchone()
            if inflight:
                raise LedgerStateError(f"inflight_request_exists:{inflight['status']}")
            b = self._ensure_budget_row(c, d, job["actor"])
            if daily_cap is not None and (b["reserved_count"] + b["succeeded_count"] + b["uncertain_count"]) >= daily_cap:
                c.execute(
                    "UPDATE wardrobe_render_jobs SET status='failed', claim_token=NULL, executor_lock_path=NULL, "
                    "error_code='daily_cap_exceeded', updated_at=? WHERE job_id=?",
                    (t, job_id),
                )
                raise LedgerStateError("daily_cap_exceeded")
            attempt_no = int(job["attempts"]) + 1
            attempt_key = f"{job_id}:a{attempt_no}"
            c.execute(
                "INSERT INTO wardrobe_render_requests("
                "request_id, job_id, attempt_no, attempt_key, selection_hash, status, "
                "executor_lock_path, expected_staged_path, created_at, updated_at"
                ") VALUES(?,?,?,?,?,'submitting',?,?,?,?)",
                (
                    uuid.uuid4().hex, job_id, attempt_no, attempt_key, job["selection_hash"],
                    executor_lock_path, expected_staged_path, t, t,
                ),
            )
            c.execute(
                "UPDATE wardrobe_render_jobs SET status='submitting', attempts=?, heartbeat_at=?, "
                "executor_lock_path=?, updated_at=? WHERE job_id=?",
                (attempt_no, t, executor_lock_path, t, job_id),
            )
            c.execute(
                "UPDATE wardrobe_render_budget SET reserved_count = reserved_count + 1 WHERE day_jst=? AND actor=?",
                (d, job["actor"]),
            )
            return attempt_no

    def heartbeat_submit(self, job_id: str, claim_token: str, *, now: float | None = None) -> None:
        t = time.time() if now is None else float(now)
        with self.tx() as c:
            c.execute(
                "UPDATE wardrobe_render_jobs SET heartbeat_at=?, updated_at=? "
                "WHERE job_id=? AND status='submitting' AND claim_token=?",
                (t, t, job_id, claim_token),
            )

    def finish_submit_staged(
        self,
        job_id: str,
        attempt_no: int,
        staged_path: str,
        staged_sha256: str,
        *,
        provider_request_id: str | None = None,
        cost_usd: float | None = None,
        now: float | None = None,
    ) -> None:
        t = time.time() if now is None else float(now)
        d = day_jst(t)
        with self.tx() as c:
            job = c.execute("SELECT actor FROM wardrobe_render_jobs WHERE job_id=?", (job_id,)).fetchone()
            req = c.execute(
                "SELECT created_at FROM wardrobe_render_requests WHERE job_id=? AND attempt_no=?",
                (job_id, attempt_no),
            ).fetchone()
            c.execute(
                "UPDATE wardrobe_render_requests SET status='succeeded', provider_request_id=?, cost_usd=?, updated_at=? "
                "WHERE job_id=? AND attempt_no=?",
                (provider_request_id, cost_usd, t, job_id, attempt_no),
            )
            c.execute(
                "UPDATE wardrobe_render_jobs SET status='generated_staged', claim_token=NULL, executor_lock_path=NULL, "
                "staged_image_path=?, staged_sha256=?, error_code=NULL, error_message=NULL, updated_at=? WHERE job_id=?",
                (staged_path, staged_sha256, t, job_id),
            )
            if job:
                budget_day = day_jst(float(req["created_at"])) if req else d
                self._shift_budget(
                    c, actor=job["actor"], budget_day=budget_day,
                    dec_col="reserved_count", inc_col="succeeded_count",
                )

    def finish_submit_failed(
        self,
        job_id: str,
        attempt_no: int,
        error_code: str,
        error_message: str,
        *,
        now: float | None = None,
        retryable: bool = True,
    ) -> str:
        t = time.time() if now is None else float(now)
        d = day_jst(t)
        with self.tx() as c:
            job = c.execute("SELECT actor, attempts, max_attempts FROM wardrobe_render_jobs WHERE job_id=?", (job_id,)).fetchone()
            req = c.execute(
                "SELECT created_at FROM wardrobe_render_requests WHERE job_id=? AND attempt_no=?",
                (job_id, attempt_no),
            ).fetchone()
            c.execute(
                "UPDATE wardrobe_render_requests SET status='failed', error_code=?, error_message=?, updated_at=? "
                "WHERE job_id=? AND attempt_no=?",
                (error_code[:60], error_message[:200], t, job_id, attempt_no),
            )
            new_status = "queued" if (job and retryable and int(job["attempts"]) < int(job["max_attempts"])) else "failed"
            c.execute(
                "UPDATE wardrobe_render_jobs SET status=?, claim_token=NULL, executor_lock_path=NULL, "
                "error_code=?, error_message=?, updated_at=? WHERE job_id=?",
                (new_status, error_code[:60], error_message[:200], t, job_id),
            )
            if job:
                budget_day = day_jst(float(req["created_at"])) if req else d
                self._shift_budget(
                    c, actor=job["actor"], budget_day=budget_day,
                    dec_col="reserved_count", inc_col="failed_count",
                )
            return new_status

    def finish_submit_uncertain(
        self,
        job_id: str,
        attempt_no: int,
        error_code: str,
        error_message: str,
        *,
        now: float | None = None,
        provider_request_id: str | None = None,
        cost_usd: float | None = None,
        staged_path: str | None = None,
        staged_sha256: str | None = None,
        response_sha256: str | None = None,
    ) -> None:
        """Record ambiguous timeout/5xx outcome as unknown_pending_reconcile so no blind retry happens."""
        t = time.time() if now is None else float(now)
        d = day_jst(t)
        with self.tx() as c:
            job = c.execute("SELECT actor FROM wardrobe_render_jobs WHERE job_id=?", (job_id,)).fetchone()
            req = c.execute(
                "SELECT created_at FROM wardrobe_render_requests WHERE job_id=? AND attempt_no=?",
                (job_id, attempt_no),
            ).fetchone()
            c.execute(
                "UPDATE wardrobe_render_requests SET status='unknown_pending_reconcile', "
                "provider_request_id=COALESCE(?, provider_request_id), cost_usd=COALESCE(?, cost_usd), "
                "response_sha256=COALESCE(?, response_sha256), error_code=?, error_message=?, "
                "updated_at=? WHERE job_id=? AND attempt_no=?",
                (provider_request_id, cost_usd, response_sha256, error_code[:60], error_message[:200], t, job_id, attempt_no),
            )
            c.execute(
                "UPDATE wardrobe_render_jobs SET status='unknown_pending_reconcile', claim_token=NULL, "
                "executor_lock_path=NULL, staged_image_path=COALESCE(?, staged_image_path), "
                "staged_sha256=COALESCE(?, staged_sha256), error_code=?, error_message=?, updated_at=? WHERE job_id=?",
                (staged_path, staged_sha256, error_code[:60], error_message[:200], t, job_id),
            )
            if job:
                budget_day = day_jst(float(req["created_at"])) if req else d
                self._shift_budget(
                    c, actor=job["actor"], budget_day=budget_day,
                    dec_col="reserved_count", inc_col="uncertain_count",
                )

    def reconcile_uncertain(
        self,
        job_id: str,
        outcome: str,
        *,
        staged_path: str | None = None,
        staged_sha256: str | None = None,
        error_code: str = "reconcile_failed",
        error_message: str = "",
        now: float | None = None,
    ) -> None:
        """Resolve an unknown_pending_reconcile job to generated_staged (if generated) or failed (never blind-retried)."""
        if outcome not in ("succeeded", "failed"):
            raise ValueError("outcome must be 'succeeded' or 'failed'")
        t = time.time() if now is None else float(now)
        d = day_jst(t)
        with self.tx() as c:
            job = c.execute(
                "SELECT actor, status, staged_image_path, staged_sha256 FROM wardrobe_render_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if not job or job["status"] != "unknown_pending_reconcile":
                raise LedgerStateError("job_not_unknown_pending_reconcile")
            req = c.execute(
                "SELECT attempt_no, created_at, response_sha256 FROM wardrobe_render_requests "
                "WHERE job_id=? AND status='unknown_pending_reconcile'",
                (job_id,),
            ).fetchone()
            budget_day = day_jst(float(req["created_at"])) if req else d
            if outcome == "succeeded":
                effective_path = staged_path or job["staged_image_path"]
                effective_sha = staged_sha256 or job["staged_sha256"]
                if not effective_path or not effective_sha:
                    raise ValueError("staged_path and staged_sha256 required for succeeded reconcile")
                if not isinstance(effective_sha, str) or not SHA256_HEX_RE.fullmatch(effective_sha):
                    raise BlobIntegrityError("bad_expected_sha256")
                if not os.path.isfile(effective_path):
                    raise BlobIntegrityError(f"missing_staged_image:{job_id}")
                if sha256_file(effective_path) != effective_sha:
                    raise BlobIntegrityError(f"staged_sha256_mismatch:{job_id}")
                if req and req["response_sha256"] and effective_sha != req["response_sha256"]:
                    raise BlobIntegrityError(f"response_sha256_mismatch:{job_id}")
                if req:
                    c.execute(
                        "UPDATE wardrobe_render_requests SET status='succeeded', updated_at=? "
                        "WHERE job_id=? AND attempt_no=?",
                        (t, job_id, req["attempt_no"]),
                    )
                c.execute(
                    "UPDATE wardrobe_render_jobs SET status='generated_staged', staged_image_path=?, staged_sha256=?, "
                    "error_code=NULL, error_message=NULL, updated_at=? WHERE job_id=?",
                    (effective_path, effective_sha, t, job_id),
                )
                self._shift_budget(
                    c, actor=job["actor"], budget_day=budget_day,
                    dec_col="uncertain_count", inc_col="succeeded_count",
                )
            else:
                if req:
                    c.execute(
                        "UPDATE wardrobe_render_requests SET status='failed', error_code=?, error_message=?, updated_at=? "
                        "WHERE job_id=? AND attempt_no=?",
                        (error_code[:60], error_message[:200], t, job_id, req["attempt_no"]),
                    )
                c.execute(
                    "UPDATE wardrobe_render_jobs SET status='failed', error_code=?, error_message=?, updated_at=? "
                    "WHERE job_id=?",
                    (error_code[:60], error_message[:200], t, job_id),
                )
                self._shift_budget(
                    c, actor=job["actor"], budget_day=budget_day,
                    dec_col="uncertain_count", inc_col="failed_count",
                )

    def staged_jobs(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM wardrobe_render_jobs WHERE status IN ('generated_staged', 'publishing') "
            "ORDER BY created_at ASC, outbox_id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_publishing(self, job_id: str, *, now: float | None = None) -> None:
        t = time.time() if now is None else float(now)
        with self.tx() as c:
            cur = c.execute(
                "UPDATE wardrobe_render_jobs SET status='publishing', updated_at=? "
                "WHERE job_id=? AND status IN ('generated_staged', 'publishing')",
                (t, job_id),
            )
            if cur.rowcount == 0:
                raise LedgerStateError("job_not_publishable")

    def finish_publish(self, job_id: str, published_image_path: str, image_sha256: str, *, now: float | None = None) -> None:
        t = time.time() if now is None else float(now)
        with self.tx() as c:
            job = c.execute("SELECT actor, outfit_key, selection_hash FROM wardrobe_render_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job:
                raise LedgerStateError("unknown_job")
            c.execute(
                "UPDATE wardrobe_render_jobs SET status='completed', published_image_path=?, updated_at=? WHERE job_id=?",
                (published_image_path, t, job_id),
            )
            cache_key = f"{job['outfit_key']}|{job['selection_hash']}"
            c.execute(
                "INSERT OR REPLACE INTO outfit_render_cache("
                "cache_key, actor, outfit_key, selection_hash, image_path, image_sha256, job_id, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (cache_key, job["actor"], job["outfit_key"], job["selection_hash"], published_image_path, image_sha256, job_id, t),
            )
