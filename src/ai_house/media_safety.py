"""Shared stdlib media URL validation, bounded download & runtime media path sandbox (`0.3.0`)."""
from __future__ import annotations

import base64
import http.client
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, List, Optional, Tuple

_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

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
    if ip_obj.version == 4 and ip_obj in _CGNAT_NET:
        return True
    if ip_obj.version == 6 and getattr(ip_obj, "ipv4_mapped", None) is not None:
        return _is_forbidden_ip(ip_obj.ipv4_mapped, allow_loopback=allow_loopback)
    if ip_obj.is_loopback:
        return not allow_loopback
    return bool(
        ip_obj.is_private
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _normalize_host(hostname: Optional[str]) -> str:
    return (hostname or "").strip().lower().rstrip(".")


def _resolve_and_validate_ips(host: str, port: int, *, allow_loopback: bool) -> List[str]:
    clean_host = _normalize_host(host)
    if not clean_host:
        raise ValueError("缺少媒体主机名")
    is_local_alias = (
        clean_host == "localhost"
        or clean_host.endswith(".localhost")
        or clean_host == "localhost.localdomain"
        or clean_host.endswith(".local")
        or clean_host.endswith(".internal")
    )
    if is_local_alias:
        if not (allow_loopback and (clean_host == "localhost" or clean_host.endswith(".localhost"))):
            raise ValueError(f"禁止访问内网域名: {clean_host}")

    try:
        ip_literal = ipaddress.ip_address(clean_host)
    except ValueError:
        ip_literal = None

    if ip_literal is not None:
        if _is_forbidden_ip(ip_literal, allow_loopback=allow_loopback):
            raise ValueError(f"禁止访问受限 IP: {clean_host}")
        return [str(ip_literal)]

    try:
        infos = socket.getaddrinfo(clean_host, int(port), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"无法解析媒体域名: {clean_host}") from exc

    ips: List[str] = []
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise ValueError(f"媒体域名解析返回非法 IP: {addr}") from exc
        if _is_forbidden_ip(ip_obj, allow_loopback=allow_loopback):
            raise ValueError(f"禁止访问解析为内网或保留段的域名: {clean_host}")
        if addr not in ips:
            ips.append(addr)
    if not ips:
        raise ValueError(f"未能解析出可用媒体 IP: {clean_host}")
    return ips


def validate_media_url(url: str, *, allow_loopback: bool = False) -> str:
    """Ensure external URL uses `http(s)://` and does not target forbidden internal/CGNAT/metadata IPs."""
    raw = str(url or "").strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 媒体地址: {parsed.scheme or 'empty'}")
    host = _normalize_host(parsed.hostname)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    _resolve_and_validate_ips(host, port, allow_loopback=allow_loopback)
    return raw


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, allow_loopback: bool) -> None:
        super().__init__()
        self.allow_loopback = allow_loopback

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        joined = urllib.parse.urljoin(req.full_url, str(newurl))
        validate_media_url(joined, allow_loopback=self.allow_loopback)
        return super().redirect_request(req, fp, code, msg, headers, joined)


def _make_pinned_http_handlers(*, allow_loopback: bool) -> Tuple[urllib.request.HTTPHandler, urllib.request.HTTPSHandler]:
    class _PinnedHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            ips = _resolve_and_validate_ips(self.host, int(self.port), allow_loopback=allow_loopback)
            last_exc: Optional[OSError] = None
            for ip_addr in ips:
                try:
                    self.sock = socket.create_connection((ip_addr, int(self.port)), self.timeout, self.source_address)
                    return
                except OSError as exc:
                    last_exc = exc
            if last_exc is not None:
                raise last_exc

    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            ips = _resolve_and_validate_ips(self.host, int(self.port), allow_loopback=allow_loopback)
            sni_host = _normalize_host(self.host)
            last_exc: Optional[OSError] = None
            for ip_addr in ips:
                try:
                    raw_sock = socket.create_connection((ip_addr, int(self.port)), self.timeout, self.source_address)
                    self.sock = self._context.wrap_socket(raw_sock, server_hostname=sni_host)
                    return
                except OSError as exc:
                    last_exc = exc
            if last_exc is not None:
                raise last_exc

    class _PinnedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):  # type: ignore[override]
            return self.do_open(_PinnedHTTPConnection, req)

    class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):  # type: ignore[override]
            return self.do_open(_PinnedHTTPSConnection, req)

    return _PinnedHTTPHandler(), _PinnedHTTPSHandler()


def fetch_media_bytes(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
    timeout_s: float = 10.0,
    allow_loopback: bool = False,
    headers: Optional[dict[str, str]] = None,
) -> Tuple[str, bytes]:
    """Safely download bounded bytes from an `http(s)` URL with DNS-rebinding pinning and redirect validation."""
    safe_url = validate_media_url(url, allow_loopback=allow_loopback)
    req_headers = {"User-Agent": "ai-house-kit/0.3 (+https://github.com/Gengetau/ai-house-kit)"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(safe_url, headers=req_headers, method="GET")
    http_h, https_h = _make_pinned_http_handlers(allow_loopback=allow_loopback)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        http_h,
        https_h,
        _SafeRedirectHandler(allow_loopback=allow_loopback),
    )
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


def resolve_scoped_media_path(
    runtime_dir: Path | str,
    ref: str,
    *,
    scope: str = "private",
    actor: str = "",
    channel: str = "",
    album: Any = None,
) -> Path:
    """Resolve a local `media/` path enforcing strict `(scope, actor, channel)` isolation."""
    cand = resolve_runtime_media_path(runtime_dir, ref)
    media_root = (Path(runtime_dir).resolve() / "media").resolve()
    rel_parts = cand.relative_to(media_root).parts
    if not rel_parts:
        raise PermissionError("非法的媒体相对路径")
    eff_scope = str(scope or "group").strip().lower()
    top = rel_parts[0].lower()

    if eff_scope != "private":
        if "private" in {p.lower() for p in rel_parts}:
            raise PermissionError("非私聊上下文禁止访问 private 作用域媒体")
        if top in ("generated", "tts"):
            if len(rel_parts) < 3 or rel_parts[1].lower() not in ("world", "group"):
                raise PermissionError("非私聊上下文只能访问明确标记为 world/group 的生成媒体")

    if top == "album":
        if album is not None:
            media_rel = "media/" + "/".join(rel_parts)
            item = album.get_by_media_rel(media_rel, include_data_uri=False)
            if item is None:
                raise PermissionError("未知相册媒体路径拒绝访问")
            item_scope = str(item.get("scope") or "").lower()
            if item_scope == "private":
                if eff_scope != "private":
                    raise PermissionError("非私聊上下文禁止访问私密相册")
                if actor and item.get("actor") and item.get("actor") != actor:
                    raise PermissionError("禁止读取其他住客的私密相册媒体")
            elif item_scope == "group":
                if eff_scope not in ("group", "private"):
                    raise PermissionError("不允许读取群相册媒体")
                if eff_scope == "group" and channel and item.get("channel") and item.get("channel") != channel:
                    raise PermissionError("禁止跨群访问相册媒体")
        elif len(rel_parts) >= 2 and rel_parts[1].lower() == "private" and eff_scope != "private":
            raise PermissionError("非私聊上下文禁止访问私密相册")
    return cand
