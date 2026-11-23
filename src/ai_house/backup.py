"""Online SQLite hot backup, SHA-256 verification, retention pruning & restore (`0.3.0`)."""
from __future__ import annotations

import contextlib
import ctypes
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__

JST = dt.timezone(dt.timedelta(hours=9))
DEFAULT_KEEP_COUNT = 10
DEFAULT_KEEP_DAYS = 14
_SENSITIVE_NAMES = frozenset({".env", ".env.local", ".env.prod", ".env.production"})
_SECRET_FIELD_KEYS = frozenset({"api_key", "access_token", "token", "secret", "password", "bot_token"})
_REDACT_CONFIG_FILES = frozenset({"llm_config.json", "plugin_settings.json"})


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _safe_chmod(path: Path, mode: int = 0o600) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            kl = str(k).strip().lower()
            if (kl in _SECRET_FIELD_KEYS or kl.endswith(("_api_key", "_secret", "_token", "_password"))) and isinstance(v, str) and v.strip():
                out[k] = ""
            else:
                out[k] = _redact_obj(v)
        return out
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    return obj


def _copy_json_redacted(src: Path, dst: Path) -> bool:
    """Copy a JSON runtime file into the backup stage, stripping plaintext secrets."""
    raw_text = src.read_text(encoding="utf-8")
    redacted = False
    if src.name.lower() in _REDACT_CONFIG_FILES:
        try:
            doc = json.loads(raw_text)
            cleaned = _redact_obj(doc)
            dst.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
            redacted = True
        except ValueError:
            shutil.copy2(src, dst)
    else:
        shutil.copy2(src, dst)
    _safe_chmod(dst, 0o600)
    return redacted


def _is_sensitive_name(name: str) -> bool:
    low = str(name or "").strip().lower()
    if low in _SENSITIVE_NAMES or low.startswith(".env."):
        return True
    if low.endswith((".key", ".pem", ".p12")):
        return True
    return False


def _sqlite_hot_backup(src_db: Path, dst_db: Path) -> None:
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    src_conn: Optional[sqlite3.Connection] = None
    dst_conn: Optional[sqlite3.Connection] = None
    try:
        src_conn = sqlite3.connect(str(src_db), timeout=10.0)
        dst_conn = sqlite3.connect(str(dst_db), timeout=10.0)
        src_conn.backup(dst_conn)
    finally:
        if dst_conn is not None:
            dst_conn.close()
        if src_conn is not None:
            src_conn.close()

    with contextlib.closing(sqlite3.connect(str(dst_db), timeout=5.0)) as chk_conn:
        row = chk_conn.execute("PRAGMA integrity_check").fetchone()
        status = str(row[0] if row else "").strip().lower()
        if status != "ok":
            raise RuntimeError(f"SQLite 完整性校验失败 ({src_db.name}): {row[0] if row else 'empty'}")


def is_pid_alive(pid: int) -> bool:
    """Best-effort OS check whether `pid` currently refers to a live process."""
    try:
        ip = int(pid)
    except (TypeError, ValueError):
        return False
    if ip <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, ip)
        if not handle:
            return ctypes.windll.kernel32.GetLastError() == 5  # ERROR_ACCESS_DENIED => alive
        try:
            code = ctypes.c_ulong(0)
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(ip, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def check_daemon_lock(runtime_dir: Path | str) -> Optional[int]:
    """Return live daemon PID if `<runtime_dir>/.daemon.lock` points to a running process, else `None`."""
    rt = Path(runtime_dir)
    for cand in (rt / ".daemon.lock", rt / "runtime" / ".daemon.lock"):
        if not cand.is_file():
            continue
        try:
            doc = json.loads(cand.read_text(encoding="utf-8"))
            pid = int(doc.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            pid = 0
        if pid > 0 and is_pid_alive(pid):
            return pid
    return None


def _prune_backups(
    backups_dir: Path,
    *,
    keep_count: int,
    keep_days: int,
    protect_path: Optional[Path] = None,
    wall_now: Optional[float] = None,
) -> List[str]:
    archives = sorted(
        backups_dir.glob("ai-house-backup-*.tar.gz"),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )
    # D1: Archive file mtimes live on the OS wall clock, never on virtual simulation clock time.
    cutoff = float(wall_now if wall_now is not None else time.time()) - max(1, int(keep_days)) * 86400.0
    max_keep = max(1, int(keep_count))
    pruned: List[str] = []
    for idx, arc in enumerate(archives):
        if protect_path is not None and arc.resolve() == protect_path.resolve():
            continue
        too_many = idx >= max_keep
        too_old = arc.stat().st_mtime < cutoff
        if too_many or too_old:
            try:
                arc.unlink()
                pruned.append(arc.name)
            except OSError:
                pass
    return pruned


def list_backups(runtime_dir: Path | str, *, out_dir: Optional[Path | str] = None) -> List[Dict[str, Any]]:
    rt = Path(runtime_dir).resolve()
    backups_dir = (Path(out_dir) if out_dir else (rt / "backups")).resolve()
    if not backups_dir.is_dir():
        return []
    items: List[Dict[str, Any]] = []
    for arc in sorted(
        backups_dir.glob("ai-house-backup-*.tar.gz"),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    ):
        st = arc.stat()
        items.append({
            "filename": arc.name,
            "path": str(arc),
            "size": int(st.st_size),
            "mtime": float(st.st_mtime),
            "mtime_jst": dt.datetime.fromtimestamp(st.st_mtime, JST).strftime("%Y-%m-%d %H:%M:%S"),
        })
    return items


def create_backup(
    runtime_dir: Path | str,
    *,
    out_dir: Optional[Path | str] = None,
    keep_count: int = DEFAULT_KEEP_COUNT,
    keep_days: int = DEFAULT_KEEP_DAYS,
    now: Optional[float] = None,
    wall_now: Optional[float] = None,
) -> Dict[str, Any]:
    """Create a verified hot SQLite + redacted runtime config `.tar.gz` backup (excluding `.env` and plaintext keys)."""
    rt = Path(runtime_dir).resolve()
    if not rt.is_dir():
        raise FileNotFoundError(f"运行目录不存在：{rt}")
    ts = float(now if now is not None else time.time())
    backups_dir = (Path(out_dir) if out_dir else (rt / "backups")).resolve()
    backups_dir.mkdir(parents=True, exist_ok=True)
    _safe_chmod(backups_dir, 0o700)

    stamp = dt.datetime.fromtimestamp(ts, JST).strftime("%Y%m%d-%H%M%S")
    archive_path = backups_dir / f"ai-house-backup-{stamp}.tar.gz"
    seq = 1
    while archive_path.exists():
        archive_path = backups_dir / f"ai-house-backup-{stamp}-{seq:02d}.tar.gz"
        seq += 1

    with tempfile.TemporaryDirectory() as stage_tmp:
        stage_root = Path(stage_tmp)
        manifest_files: List[Dict[str, Any]] = []
        db_names: List[str] = []

        # 1) Online hot backup of every .db file directly under runtime_dir
        for db_path in sorted(rt.glob("*.db")):
            if not db_path.is_file() or _is_sensitive_name(db_path.name):
                continue
            dst_db = stage_root / db_path.name
            _sqlite_hot_backup(db_path, dst_db)
            db_names.append(db_path.name)
            manifest_files.append({
                "path": db_path.name,
                "kind": "db",
                "size": int(dst_db.stat().st_size),
                "sha256": _sha256_file(dst_db),
            })

        # 2) Non-secret runtime JSON configs (strip API keys before writing)
        any_redacted = False
        for jf in sorted(rt.glob("*.json")):
            if not jf.is_file() or _is_sensitive_name(jf.name):
                continue
            dst_jf = stage_root / jf.name
            was_redacted = _copy_json_redacted(jf, dst_jf)
            any_redacted = any_redacted or was_redacted
            manifest_files.append({
                "path": jf.name,
                "kind": "json",
                "secrets_redacted": was_redacted,
                "size": int(dst_jf.stat().st_size),
                "sha256": _sha256_file(dst_jf),
            })

        # 3) Custom world pack if created via onboarding/studio
        custom_dir = rt / "custom-pack"
        if custom_dir.is_dir():
            for fp in sorted(custom_dir.rglob("*")):
                if not fp.is_file() or _is_sensitive_name(fp.name):
                    continue
                rel = fp.relative_to(rt).as_posix()
                dst_fp = stage_root / rel
                dst_fp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fp, dst_fp)
                manifest_files.append({
                    "path": rel,
                    "kind": "pack",
                    "size": int(dst_fp.stat().st_size),
                    "sha256": _sha256_file(dst_fp),
                })

        # 4) Generated / album / TTS media assets (`runtime/media/**`)
        media_dir = rt / "media"
        if media_dir.is_dir():
            for fp in sorted(media_dir.rglob("*")):
                if not fp.is_file() or _is_sensitive_name(fp.name):
                    continue
                rel = fp.relative_to(rt).as_posix()
                dst_fp = stage_root / rel
                dst_fp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(fp, dst_fp)
                manifest_files.append({
                    "path": rel,
                    "kind": "media",
                    "size": int(dst_fp.stat().st_size),
                    "sha256": _sha256_file(dst_fp),
                })

        manifest = {
            "version": __version__,
            "created_at": ts,
            "created_jst": dt.datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M:%S JST"),
            "source_name": rt.name,
            "secrets_redacted": any_redacted,
            "files": manifest_files,
        }
        manifest_path = stage_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        tmp_arc = archive_path.with_suffix(".tar.gz.tmp")
        with tarfile.open(tmp_arc, "w:gz") as tf:
            tf.add(manifest_path, arcname="manifest.json")
            for entry in manifest_files:
                tf.add(stage_root / entry["path"], arcname=entry["path"])
        tmp_arc.replace(archive_path)
        _safe_chmod(archive_path, 0o600)

    pruned = _prune_backups(
        backups_dir,
        keep_count=keep_count,
        keep_days=keep_days,
        protect_path=archive_path,
        wall_now=wall_now,
    )
    return {
        "ok": True,
        "archive": str(archive_path),
        "filename": archive_path.name,
        "size": int(archive_path.stat().st_size),
        "files": len(manifest_files),
        "dbs": db_names,
        "pruned": pruned,
        "created_at": ts,
    }


def _safe_tar_members(tf: tarfile.TarFile) -> Dict[str, tarfile.TarInfo]:
    members: Dict[str, tarfile.TarInfo] = {}
    for m in tf.getmembers():
        if m.isdir():
            continue
        if not m.isreg():
            raise ValueError(f"备份归档包含非法非常规成员：{m.name}")
        norm = m.name.replace("\\", "/").lstrip("./")
        parts = [p for p in norm.split("/") if p]
        if not parts or norm.startswith("/") or ".." in parts or ":" in parts[0]:
            raise ValueError(f"备份归档包含不安全路径：{m.name}")
        if _is_sensitive_name(parts[-1]):
            raise ValueError(f"备份归档不应包含敏感文件：{m.name}")
        members[norm] = m
    return members


def verify_backup(archive_path: Path | str) -> Dict[str, Any]:
    """Verify archive manifest hashes, non-secret hygiene, and SQLite PRAGMA integrity."""
    arc = Path(archive_path).resolve()
    if not arc.is_file():
        raise FileNotFoundError(f"备份文件不存在：{arc}")

    try:
        with tarfile.open(arc, "r:gz") as tf:
            members = _safe_tar_members(tf)
            if "manifest.json" not in members:
                raise ValueError("备份缺少 manifest.json")
            raw_manifest = tf.extractfile(members["manifest.json"]).read()
            manifest = json.loads(raw_manifest.decode("utf-8"))
            entries = manifest.get("files")
            if not isinstance(entries, list):
                raise ValueError("manifest.json 缺少 files 列表")

            db_names: List[str] = []
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_root = Path(tmp_dir)
                for entry in entries:
                    rel = str(entry.get("path") or "").replace("\\", "/").lstrip("./")
                    if rel not in members:
                        raise ValueError(f"备份缺失清单文件：{rel}")
                    f_bytes = tf.extractfile(members[rel]).read()
                    if len(f_bytes) != int(entry.get("size", -1)):
                        raise ValueError(f"文件大小校验失败：{rel}")
                    actual_sha = hashlib.sha256(f_bytes).hexdigest()
                    if actual_sha != str(entry.get("sha256") or ""):
                        raise ValueError(f"SHA-256 校验失败：{rel}")
                    if entry.get("kind") == "db" or rel.endswith(".db"):
                        db_names.append(rel)
                        out_db = tmp_root / Path(rel).name
                        out_db.write_bytes(f_bytes)
                        with contextlib.closing(sqlite3.connect(str(out_db), timeout=5.0)) as c:
                            row = c.execute("PRAGMA integrity_check").fetchone()
                            if not row or str(row[0]).strip().lower() != "ok":
                                raise ValueError(f"SQLite integrity_check 失败：{rel}")
    except (tarfile.TarError, EOFError, UnicodeDecodeError, zlib.error, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"备份归档损坏：{exc}") from exc

    return {
        "ok": True,
        "archive": str(arc),
        "verified_files": len(entries),
        "dbs": db_names,
        "manifest": manifest,
    }


def restore_backup(
    archive_path: Path | str,
    target_runtime_dir: Path | str,
    *,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Verify live daemon lock, stage + checksum-verify archive, and atomically restore into `target_runtime_dir`."""
    target = Path(target_runtime_dir).resolve()
    live_pid = check_daemon_lock(target)
    if live_pid is not None:
        raise RuntimeError(f"目标运行目录正被 ai-house 守护进程 (PID {live_pid}) 使用，请先停止服务再执行恢复")

    if target.exists() and not overwrite and any(target.glob("*.db")):
        raise FileExistsError(f"目标运行目录已存在数据库文件：{target}（如确需覆盖请传 overwrite=True）")

    verified = verify_backup(archive_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(dir=str(target.parent), prefix=f".{target.name}.restore_stage_{uuid.uuid4().hex[:6]}_"))
    rollback_dir = Path(tempfile.mkdtemp(dir=str(target.parent), prefix=f".{target.name}.restore_prev_{uuid.uuid4().hex[:6]}_"))
    restored: List[str] = []
    moved_back: List[tuple[Path, Path]] = []
    try:
        # 1) Extract & verify inside staging directory before mutating target.
        with tarfile.open(Path(archive_path).resolve(), "r:gz") as tf:
            members = _safe_tar_members(tf)
            for entry in verified["manifest"]["files"]:
                rel = str(entry.get("path") or "").replace("\\", "/").lstrip("./")
                staged = (stage_dir / rel).resolve()
                if not staged.is_relative_to(stage_dir):
                    raise ValueError(f"恢复路径越界：{rel}")
                staged.parent.mkdir(parents=True, exist_ok=True)
                data = tf.extractfile(members[rel]).read()
                if len(data) != int(entry.get("size", -1)) or hashlib.sha256(data).hexdigest() != str(entry.get("sha256") or ""):
                    raise ValueError(f"恢复校验失败：{rel}")
                staged.write_bytes(data)
                _safe_chmod(staged, 0o600)
                if entry.get("kind") == "db" or rel.endswith(".db"):
                    with contextlib.closing(sqlite3.connect(str(staged), timeout=5.0)) as chk:
                        row = chk.execute("PRAGMA integrity_check").fetchone()
                        if not row or str(row[0]).strip().lower() != "ok":
                            raise ValueError(f"恢复 SQLite 校验失败：{rel}")

        target.mkdir(parents=True, exist_ok=True)
        # 2) Move aside existing runtime state (preserving `backups/` & `.env*`) and drop stale SQLite -wal/-shm.
        for pattern in ("*.db", "*.db-wal", "*.db-shm", "*.json"):
            for cur_p in sorted(target.glob(pattern)):
                if _is_sensitive_name(cur_p.name) or cur_p.name == ".daemon.lock":
                    continue
                if cur_p.name.endswith(("-wal", "-shm")):
                    cur_p.unlink(missing_ok=True)
                    continue
                rb_p = rollback_dir / cur_p.name
                shutil.move(str(cur_p), str(rb_p))
                moved_back.append((cur_p, rb_p))
        for dir_name in ("custom-pack", "media"):
            cur_d = target / dir_name
            if cur_d.exists():
                rb_d = rollback_dir / dir_name
                shutil.move(str(cur_d), str(rb_d))
                moved_back.append((cur_d, rb_d))

        # 3) Move verified files into target cleanly.
        try:
            for entry in verified["manifest"]["files"]:
                rel = str(entry.get("path") or "").replace("\\", "/").lstrip("./")
                src = (stage_dir / rel).resolve()
                dst = (target / rel).resolve()
                if not dst.is_relative_to(target):
                    raise ValueError(f"恢复路径越界：{rel}")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                _safe_chmod(dst, 0o600)
                restored.append(rel)
        except Exception:
            for orig_p, rb_p in reversed(moved_back):
                if orig_p.is_dir():
                    shutil.rmtree(orig_p, ignore_errors=True)
                elif orig_p.exists():
                    orig_p.unlink(missing_ok=True)
                if rb_p.exists():
                    shutil.move(str(rb_p), str(orig_p))
            raise
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
        shutil.rmtree(rollback_dir, ignore_errors=True)

    return {
        "ok": True,
        "target_dir": str(target),
        "restored_files": len(restored),
        "dbs": verified["dbs"],
    }


__all__ = [
    "DEFAULT_KEEP_COUNT",
    "DEFAULT_KEEP_DAYS",
    "check_daemon_lock",
    "create_backup",
    "is_pid_alive",
    "list_backups",
    "restore_backup",
    "verify_backup",
]
