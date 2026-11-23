"""Online SQLite hot backup, SHA-256 verification, retention pruning & restore (`0.3.0`)."""
from __future__ import annotations

import contextlib
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


def _prune_backups(
    backups_dir: Path,
    *,
    keep_count: int,
    keep_days: int,
    protect_path: Optional[Path] = None,
    now: float,
) -> List[str]:
    archives = sorted(
        backups_dir.glob("ai-house-backup-*.tar.gz"),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )
    cutoff = float(now) - max(1, int(keep_days)) * 86400.0
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
        now=ts,
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
    """Verify and restore a verified backup archive into `target_runtime_dir`."""
    verified = verify_backup(archive_path)
    target = Path(target_runtime_dir).resolve()
    if target.exists() and not overwrite and any(target.glob("*.db")):
        raise FileExistsError(f"目标运行目录已存在数据库文件：{target}（如确需覆盖请传 overwrite=True）")
    target.mkdir(parents=True, exist_ok=True)

    restored: List[str] = []
    with tarfile.open(Path(archive_path).resolve(), "r:gz") as tf:
        members = _safe_tar_members(tf)
        for entry in verified["manifest"]["files"]:
            rel = str(entry.get("path") or "").replace("\\", "/").lstrip("./")
            dst = (target / rel).resolve()
            if not dst.is_relative_to(target):
                raise ValueError(f"恢复路径越界：{rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = tf.extractfile(members[rel]).read()
            dst.write_bytes(data)
            _safe_chmod(dst, 0o600)
            restored.append(rel)
    return {
        "ok": True,
        "target_dir": str(target),
        "restored_files": len(restored),
        "dbs": verified["dbs"],
    }


__all__ = [
    "DEFAULT_KEEP_COUNT",
    "DEFAULT_KEEP_DAYS",
    "create_backup",
    "list_backups",
    "restore_backup",
    "verify_backup",
]
