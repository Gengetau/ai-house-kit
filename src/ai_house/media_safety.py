"""Shared stdlib media URL validation, bounded download & runtime media path sandbox (`0.3.0`)."""
from __future__ import annotations

import base64
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

DEFAULT_MAX_MEDIA_BYTES = 5 * 1024 * 1024
_DATA_URI_RE = re.compile(r"^data:([a-z0-9.+/-]+)(?:;[a-z0-9=_-]+)*;base64,(.+)$", re.IGNORECASE | re.DOTALL)

_SAFE_IMAGE_EXTS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/svg+xml": "svg",
}

_SAFE_AUDIO_EXTS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "opus",
    "audio/opus": "opus",
}


def media_ext_for_mime(mime: str, default: str = "bin") -> str:
    clean = str(mime or "").split(";", 1)[0].strip().lower()
    return _SAFE_IMAGE_EXTS.get(clean) or _SAFE_AUDIO_EXTS.get(clean) or default


def mime_for_path(path: Path | str, default: str = "application/octet-stream") -> str:
    suf = Path(path).suffix.lower()
    mapping = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".opus": "audio/opus",
        ".ogg": "audio/ogg",
    }
    return mapping.get(suf, default)


def to_data_uri(raw: bytes, mime: str = "image/png") -> str:
    b64 = base64.b64encode(raw).decode("ascii")
    clean_mime = str(mime or "image/png").split(";", 1)[0].strip() or "image/png"
    return f"data:{clean_mime};base64,{b64}"


def parse_data_uri(uri: str, *, max_bytes: int = DEFAULT_MAX_MEDIA_BYTES) -> Tuple[str, bytes]:
    m = _DATA_URI_RE.match(str(uri or "").strip())
    if not m:
        raise ValueError("不支持的 data URI 格式")
    mime = m.group(1).lower().strip()
    b64_payload = re.sub(r"\s+", "", m.group(2))
    raw = base64.b64decode(b64_payload, validate=True)
    if len(raw) > max_bytes:
        raise ValueError(f"媒体体积超过上限 ({len(raw)} > {max_bytes})")
    return mime, raw


def _is_forbidden_ip(ip_obj: ipaddress._BaseAddress, *, allow_loopback: bool) -> bool:
    if ip_obj.is_loopback:
        return not allow_loopback
    return bool(
        ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def validate_media_url(url: str, *, allow_loopback: bool = False) -> str:
    """Ensure external URL uses `http(s)://` and does not target forbidden internal/metadata IPs."""
    raw = str(url or "").strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 媒体地址: {parsed.scheme or 'empty'}")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("缺少媒体主机名")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local") or host.endswith(".internal"):
        if not (allow_loopback and host == "localhost"):
            raise ValueError(f"禁止访问内网域名: {host}")

    try:
        ip_literal = ipaddress.ip_address(host)
    except ValueError:
        ip_literal = None

    if ip_literal is not None:
        if _is_forbidden_ip(ip_literal, allow_loopback=allow_loopback):
            raise ValueError(f"禁止访问受限 IP: {host}")
        return raw

    if allow_loopback and host == "localhost":
        return raw

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme.lower() == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        # Offline environments cannot resolve DNS; let downstream urllib raise cleanly without leaking local details.
        return raw

    for info in infos:
        addr = info[4][0].split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_forbidden_ip(ip_obj, allow_loopback=allow_loopback):
            raise ValueError(f"禁止访问解析为内网或保留段的域名: {host}")
    return raw


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, allow_loopback: bool) -> None:
        super().__init__()
        self.allow_loopback = allow_loopback

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        joined = urllib.parse.urljoin(req.full_url, str(newurl))
        validate_media_url(joined, allow_loopback=self.allow_loopback)
        return super().redirect_request(req, fp, code, msg, headers, joined)


def fetch_media_bytes(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
    timeout_s: float = 10.0,
    allow_loopback: bool = False,
    headers: Optional[dict[str, str]] = None,
) -> Tuple[str, bytes]:
    """Safely download bounded bytes from an `http(s)` URL with redirect validation."""
    safe_url = validate_media_url(url, allow_loopback=allow_loopback)
    req_headers = {"User-Agent": "ai-house-kit/0.3 (+https://github.com/Gengetau/ai-house-kit)"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(safe_url, headers=req_headers, method="GET")
    opener = urllib.request.build_opener(_SafeRedirectHandler(allow_loopback=allow_loopback))
    with opener.open(req, timeout=float(timeout_s)) as resp:
        raw = resp.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError(f"远端媒体体积超过上限 ({max_bytes} bytes)")
        ctype = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        return ctype or "application/octet-stream", raw


def resolve_runtime_media_path(runtime_dir: Path | str, ref: str) -> Path:
    """Resolve a path relative to `<runtime_dir>/media/`, rejecting traversal outside it."""
    clean_ref = str(ref or "").strip().replace("\\", "/").lstrip("/")
    if not clean_ref or "\x00" in clean_ref:
        raise ValueError("非法的媒体相对路径")
    if clean_ref.startswith("media/"):
        clean_ref = clean_ref[len("media/"):]
    media_root = (Path(runtime_dir).resolve() / "media").resolve()
    cand = (media_root / clean_ref).resolve()
    if not cand.is_relative_to(media_root) or cand == media_root:
        raise ValueError("媒体路径越界")
    return cand
