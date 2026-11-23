"""Online SQLite hot backup, SHA-256 verification, retention pruning & restore (`0.3.0`)."""
from __future__ import annotations

import contextlib
import ctypes
import datetime as dt
import hashlib
import io
import json
import os
import re
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


class DaemonRunningError(RuntimeError):
    """Restore refused because a live ai-house daemon holds the runtime directory."""


class BackupVersionError(ValueError):
    """Archive was written by a newer ai-house release than this program."""


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


_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_SECRET_WORDS = frozenset({
    "key", "token", "secret", "password", "passwd", "pwd", "passphrase", "authorization", "auth",
    "cookie", "cookies", "webhook", "credential", "credentials",
    "apikey", "secretkey", "accesskey", "privatekey",
})
_SECRET_WORD_SUFFIXES = ("apikey", "secretkey", "accesskey", "privatekey", "token", "secret", "password", "passwd", "webhook", "cookie", "authorization")
_SECRET_WORD_PREFIXES = ("apikey", "webhook", "password", "passwd", "cookie", "authorization", "secret")
# Words that make a secret-looking key obviously a non-secret (counters, flags, names, env-var references ...).
_NON_SECRET_WORDS = frozenset({
    "count", "counts", "limit", "limits", "max", "min", "usage", "used", "budget", "ttl", "expires", "expiry",
    "env", "name", "names", "file", "path", "type", "id", "ids", "enabled", "required", "mode", "length", "len",
    "size", "kind", "set", "present", "has", "is",
})
_URL_USERINFO_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/\s@?#]*@")
_URL_QUERY_RE = re.compile(r"(?P<sep>[?&;])(?P<name>[^=&;\s#]+)=(?P<val>[^&;\s#]*)")
DEFAULT_SIZE_WARN_BYTES = 512 * 1024 * 1024


# In user-authored content (custom packs, media sidecars) a bare `key` / `auth` field is usually an identifier
# such as `"key": "bedroom_0"`, not a credential; blanking it would corrupt the pack on restore.
_GENERIC_CONTENT_WORDS = frozenset({"key", "auth"})


def _is_secret_key(key: Any, *, content: bool = False) -> bool:
    """True when a JSON key name looks like it holds a credential (`api_key`, `clientSecret`, `Authorization` ...).

    `content=True` is for user-authored content files: a key that is only the generic word `key` or `auth`
    is left alone there, while compound names (`api_key`, `auth_token`) are still treated as credentials.
    """
    words = [w.lower() for w in _WORD_RE.findall(str(key or ""))]
    if not words or any(w in _NON_SECRET_WORDS for w in words):
        return False
    if content and len(words) == 1 and words[0] in _GENERIC_CONTENT_WORDS:
        return False
    for w in words:
        if w in _SECRET_WORDS:
            return True
        if len(w) > 3 and (w.endswith(_SECRET_WORD_SUFFIXES) or w.startswith(_SECRET_WORD_PREFIXES)):
            return True
    return False


def _redact_url_string(text: str) -> str:
    """Strip `user:pass@` userinfo and secret-looking query values (`?token=`, `?key=` ...) from URLs in `text`."""
    out = _URL_USERINFO_RE.sub(lambda m: m.group("scheme"), text)

    def _q(m: "re.Match[str]") -> str:
        if m.group("val") and _is_secret_key(m.group("name")):
            return f"{m.group('sep')}{m.group('name')}="
        return m.group(0)

    return _URL_QUERY_RE.sub(_q, out)


def _join_path(base: str, key: Any) -> str:
    return f"{base}.{key}" if base else str(key)


def _redact_obj(
    obj: Any, path: str = "", found: Optional[List[str]] = None, *, force: bool = False, content: bool = False
) -> Any:
    """Return a redacted copy of `obj`; every redacted location is appended to `found` as `a.b[0].c`."""
    if found is None:
        found = []
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            p = _join_path(path, k)
            out[k] = _redact_obj(v, p, found, force=force or _is_secret_key(k, content=content), content=content)
        return out
    if isinstance(obj, list):
        return [_redact_obj(x, f"{path}[{i}]", found, force=force, content=content) for i, x in enumerate(obj)]
    if isinstance(obj, str) and obj.strip():
        if force:
            found.append(path)
            return ""
        cleaned = _redact_url_string(obj)
        if cleaned != obj:
            found.append(path)
        return cleaned
    return obj


def _copy_json_redacted(src: Path, dst: Path, *, content: bool = False) -> List[str]:
    """Copy a JSON file into the backup stage, stripping secrets; return redacted field paths (never values).

    Raises `ValueError` if the file is not parseable JSON (it is then copied verbatim by the caller's decision).
    """
    raw = src.read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    found: List[str] = []
    cleaned = _redact_obj(doc, "", found, content=content)
    if found:
        dst.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        dst.write_bytes(raw)
    _safe_chmod(dst, 0o600)
    return found


def _stage_json(src: Path, dst: Path, warnings: List[str], label: str, *, content: bool = False) -> List[str]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _copy_json_redacted(src, dst, content=content)
    except ValueError:
        shutil.copy2(src, dst)
        _safe_chmod(dst, 0o600)
        warnings.append(f"{label} 不是合法 JSON，已原样备份，未做密钥脱敏")
        return []


def _match_list_item(item: Any, index: int, target_list: List[Any]) -> Any:
    if isinstance(item, dict):
        for idk in ("name", "id", "plugin_id", "model", "endpoint"):
            if isinstance(item.get(idk), (str, int)) and not isinstance(item.get(idk), bool):
                for cand in target_list:
                    if isinstance(cand, dict) and cand.get(idk) == item[idk]:
                        return cand
                return None
    return target_list[index] if index < len(target_list) else None


def _merge_redacted(arc: Any, tgt: Any, path: str, key: str, paths: Optional[set], missing: List[str]) -> Any:
    """Merge archive JSON with the target's current JSON: redacted archive fields keep the target's value if any."""
    if isinstance(arc, dict):
        tg = tgt if isinstance(tgt, dict) else {}
        return {k: _merge_redacted(v, tg.get(k), _join_path(path, k), str(k), paths, missing) for k, v in arc.items()}
    if isinstance(arc, list):
        tl = tgt if isinstance(tgt, list) else []
        return [_merge_redacted(v, _match_list_item(v, i, tl), f"{path}[{i}]", key, paths, missing) for i, v in enumerate(arc)]
    if not isinstance(arc, str):
        return arc
    if paths is not None:
        redacted = path in paths
    else:  # legacy archive without `redacted_fields`: blank value under a secret-looking key
        redacted = arc == "" and _is_secret_key(key)
    if not redacted:
        return arc
    if isinstance(tgt, str) and tgt.strip() and (arc == "" or _redact_url_string(tgt) == arc):
        return tgt
    missing.append(path)
    return arc


def _merge_json_secrets(data: bytes, current: Path, entry: Dict[str, Any]) -> tuple[bytes, List[str]]:
    try:
        arc_doc = json.loads(data.decode("utf-8"))
    except ValueError:
        return data, []
    tgt_doc: Any = None
    try:
        if current.is_file():
            tgt_doc = json.loads(current.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tgt_doc = None
    fields = entry.get("redacted_fields")
    paths = {str(x) for x in fields} if isinstance(fields, list) else None
    missing: List[str] = []
    merged = _merge_redacted(arc_doc, tgt_doc, "", "", paths, missing)
    return json.dumps(merged, ensure_ascii=False, indent=2).encode("utf-8"), missing


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


def _resolve_size_warn(explicit: Optional[int]) -> int:
    if explicit is not None:
        return int(explicit)
    raw = os.environ.get("AI_HOUSE_BACKUP_WARN_MB", "").strip()
    if raw:
        try:
            return int(float(raw) * 1048576)
        except ValueError:
            pass
    return DEFAULT_SIZE_WARN_BYTES


def create_backup(
    runtime_dir: Path | str,
    *,
    out_dir: Optional[Path | str] = None,
    keep_count: int = DEFAULT_KEEP_COUNT,
    keep_days: int = DEFAULT_KEEP_DAYS,
    now: Optional[float] = None,
    wall_now: Optional[float] = None,
    size_warn_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Create a verified hot SQLite + redacted runtime config `.tar.gz` backup (excluding `.env` and plaintext keys).

    Every JSON file in the archive is scrubbed by key-name pattern (see `_is_secret_key`) and URL credentials.
    Archives larger than `size_warn_bytes` (default 512 MiB, or `AI_HOUSE_BACKUP_WARN_MB`) still succeed but
    carry a size hint in the returned `warnings`.
    """
    rt = Path(runtime_dir).resolve()
    if not rt.is_dir():
        raise FileNotFoundError(f"运行目录不存在：{rt}")
    ts = float(now if now is not None else time.time())
    backups_dir = (Path(out_dir) if out_dir else (rt / "backups")).resolve()
    dir_created = not backups_dir.exists()
    backups_dir.mkdir(parents=True, exist_ok=True)
    if dir_created:  # never touch the permissions of a directory the caller already owns (may be shared)
        _safe_chmod(backups_dir, 0o700)
    warnings: List[str] = []

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

        # 2) Runtime JSON configs (all secrets scrubbed by key-name pattern before writing)
        any_redacted = False
        for jf in sorted(rt.glob("*.json")):
            if not jf.is_file() or _is_sensitive_name(jf.name):
                continue
            dst_jf = stage_root / jf.name
            fields = _stage_json(jf, dst_jf, warnings, jf.name)
            any_redacted = any_redacted or bool(fields)
            entry: Dict[str, Any] = {
                "path": jf.name,
                "kind": "json",
                "secrets_redacted": bool(fields),
                "size": int(dst_jf.stat().st_size),
                "sha256": _sha256_file(dst_jf),
            }
            if fields:
                entry["redacted_fields"] = fields
            manifest_files.append(entry)

        # 3) Custom world pack if created via onboarding/studio
        custom_dir = rt / "custom-pack"
        if custom_dir.is_dir():
            for fp in sorted(custom_dir.rglob("*")):
                if not fp.is_file() or _is_sensitive_name(fp.name):
                    continue
                rel = fp.relative_to(rt).as_posix()
                dst_fp = stage_root / rel
                dst_fp.parent.mkdir(parents=True, exist_ok=True)
                fields: List[str] = []
                if fp.suffix.lower() == ".json":
                    fields = _stage_json(fp, dst_fp, warnings, rel, content=True)
                    any_redacted = any_redacted or bool(fields)
                else:
                    shutil.copy2(fp, dst_fp)
                entry = {
                    "path": rel,
                    "kind": "pack",
                    "size": int(dst_fp.stat().st_size),
                    "sha256": _sha256_file(dst_fp),
                }
                if fields:
                    entry["secrets_redacted"] = True
                    entry["redacted_fields"] = fields
                manifest_files.append(entry)

        # 4) Generated / album / TTS media assets (`runtime/media/**`)
        media_dir = rt / "media"
        if media_dir.is_dir():
            for fp in sorted(media_dir.rglob("*")):
                if not fp.is_file() or _is_sensitive_name(fp.name):
                    continue
                rel = fp.relative_to(rt).as_posix()
                dst_fp = stage_root / rel
                dst_fp.parent.mkdir(parents=True, exist_ok=True)
                fields: List[str] = []
                if fp.suffix.lower() == ".json":
                    fields = _stage_json(fp, dst_fp, warnings, rel, content=True)
                    any_redacted = any_redacted or bool(fields)
                else:
                    shutil.copy2(fp, dst_fp)
                entry = {
                    "path": rel,
                    "kind": "media",
                    "size": int(dst_fp.stat().st_size),
                    "sha256": _sha256_file(dst_fp),
                }
                if fields:
                    entry["secrets_redacted"] = True
                    entry["redacted_fields"] = fields
                manifest_files.append(entry)

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
    final_size = int(archive_path.stat().st_size)
    limit = _resolve_size_warn(size_warn_bytes)
    if limit > 0 and final_size > limit:
        warnings.append(
            f"备份体积 {final_size / 1048576:.1f} MiB 超过提示阈值 {limit / 1048576:.1f} MiB"
            "（主要来自 media/ 目录；可清理旧媒体或调整 --warn-size-mb），备份已正常完成"
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
        "secrets_redacted": any_redacted,
        "warnings": warnings,
    }


def _normalize_member_path(name: Any) -> str:
    """Canonical relative archive path. Only a leading `./` is stripped; anything unsafe raises `ValueError`."""
    raw = str(name or "").replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    parts = raw.split("/")
    if not raw or raw.startswith("/") or any(p in ("", ".", "..") for p in parts) or ":" in parts[0]:
        raise ValueError(f"备份归档包含不安全路径：{name}")
    if any(_is_sensitive_name(p) for p in parts):
        raise ValueError(f"备份归档不应包含敏感文件：{name}")
    return raw


def _safe_tar_members(tf: tarfile.TarFile) -> Dict[str, tarfile.TarInfo]:
    members: Dict[str, tarfile.TarInfo] = {}
    for m in tf.getmembers():
        if m.isdir():
            continue
        if not m.isreg():
            raise ValueError(f"备份归档包含非法非常规成员：{m.name}")
        members[_normalize_member_path(m.name)] = m
    return members


def _parse_major_minor(version: Any) -> Optional[tuple[int, int]]:
    m = re.match(r"^\s*v?(\d+)\.(\d+)", str(version or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _check_backup_version(manifest: Dict[str, Any], *, force: bool) -> List[str]:
    """Reject archives from a newer major/minor release (unless `force`); return notices for legacy archives."""
    notices: List[str] = []
    ver = manifest.get("version")
    if ver is None or str(ver).strip() == "":
        notices.append("备份清单没有 version 字段（旧版归档），已按兼容模式恢复，建议恢复后检查配置")
        return notices
    cur = _parse_major_minor(__version__) or (0, 0)
    arc_ver = _parse_major_minor(ver)
    if arc_ver is None or arc_ver > cur:
        msg = (
            f"备份由 ai-house {ver} 创建，当前程序为 {__version__}，请升级 ai-house 后再恢复"
            if arc_ver is not None
            else f"备份清单 version 无法识别（{ver!r}），请升级 ai-house 或确认归档来源"
        )
        if not force:
            raise BackupVersionError(msg + "（确需强制恢复请使用 --force / force=True）")
        notices.append(msg + "；已按 --force 强制恢复，可能出现不兼容")
    return notices


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
                    rel = _normalize_member_path(entry.get("path"))
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
    force: bool = False,
) -> Dict[str, Any]:
    """Verify live daemon lock, stage + checksum-verify archive, and atomically restore into `target_runtime_dir`.

    Secrets are never stored in the archive. JSON fields that were redacted at backup time keep the value found in
    the target's current file (merged field by field, nested structures included); fields with no existing value
    are returned in `secrets_missing` (as `file: path.to.field`, never values) and must be re-entered by the user.
    Archives from a newer major/minor release are refused unless `force=True`.
    """
    target = Path(target_runtime_dir).resolve()
    live_pid = check_daemon_lock(target)
    if live_pid is not None:
        raise DaemonRunningError(
            f"目标运行目录正被 ai-house 守护进程 (PID {live_pid}) 使用，请先停止服务再执行恢复"
        )

    if target.exists() and not overwrite and any(target.glob("*.db")):
        raise FileExistsError(f"目标运行目录已存在数据库文件：{target}（确实要覆盖的话，命令行加上 --overwrite 再运行一次）")

    verified = verify_backup(archive_path)
    warnings = _check_backup_version(verified["manifest"], force=force)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(dir=str(target.parent), prefix=f".{target.name}.restore_stage_{uuid.uuid4().hex[:6]}_"))
    rollback_dir = Path(tempfile.mkdtemp(dir=str(target.parent), prefix=f".{target.name}.restore_prev_{uuid.uuid4().hex[:6]}_"))
    restored: List[str] = []
    secrets_missing: List[str] = []
    moved_back: List[tuple[Path, Path]] = []
    try:
        # 1) Extract & verify inside staging directory before mutating target.
        with tarfile.open(Path(archive_path).resolve(), "r:gz") as tf:
            members = _safe_tar_members(tf)
            for entry in verified["manifest"]["files"]:
                rel = _normalize_member_path(entry.get("path"))
                staged = (stage_dir / rel).resolve()
                if not staged.is_relative_to(stage_dir):
                    raise ValueError(f"恢复路径越界：{rel}")
                staged.parent.mkdir(parents=True, exist_ok=True)
                data = tf.extractfile(members[rel]).read()
                if len(data) != int(entry.get("size", -1)) or hashlib.sha256(data).hexdigest() != str(entry.get("sha256") or ""):
                    raise ValueError(f"恢复校验失败：{rel}")
                if entry.get("secrets_redacted") and rel.lower().endswith(".json"):
                    data, missing = _merge_json_secrets(data, target / rel, entry)
                    secrets_missing.extend(f"{rel}: {m}" for m in missing)
                staged.write_bytes(data)
                _safe_chmod(staged, 0o600)
                if entry.get("kind") == "db" or rel.endswith(".db"):
                    with contextlib.closing(sqlite3.connect(str(staged), timeout=5.0)) as chk:
                        row = chk.execute("PRAGMA integrity_check").fetchone()
                        if not row or str(row[0]).strip().lower() != "ok":
                            raise ValueError(f"恢复 SQLite 校验失败：{rel}")

        target.mkdir(parents=True, exist_ok=True)
        try:
            # 2) Move aside existing runtime state (preserving `backups/` & `.env*`), including stale SQLite
            #    -wal/-shm, so a failure below can put everything back.
            for pattern in ("*.db", "*.db-wal", "*.db-shm", "*.json"):
                for cur_p in sorted(target.glob(pattern)):
                    if _is_sensitive_name(cur_p.name) or cur_p.name == ".daemon.lock":
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
            for entry in verified["manifest"]["files"]:
                rel = _normalize_member_path(entry.get("path"))
                src = (stage_dir / rel).resolve()
                dst = (target / rel).resolve()
                if not dst.is_relative_to(target):
                    raise ValueError(f"恢复路径越界：{rel}")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                _safe_chmod(dst, 0o600)
                restored.append(rel)
        except Exception:
            for rel in restored:
                with contextlib.suppress(OSError):
                    (target / rel).unlink()
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
        "secrets_redacted": bool(verified["manifest"].get("secrets_redacted")),
        "secrets_missing": secrets_missing,
        "warnings": warnings,
    }


__all__ = [
    "BackupVersionError",
    "DEFAULT_SIZE_WARN_BYTES",
    "DaemonRunningError",
    "DEFAULT_KEEP_COUNT",
    "DEFAULT_KEEP_DAYS",
    "check_daemon_lock",
    "create_backup",
    "is_pid_alive",
    "list_backups",
    "restore_backup",
    "verify_backup",
]
