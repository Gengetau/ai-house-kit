"""Stage 3.3 optional text-to-image plugin (`image_gen`, default disabled) with BYO engine hooks."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from ..llm import _extract_http_error_detail, mask_secrets
from ..media_safety import (
    DEFAULT_MAX_MEDIA_BYTES,
    fetch_media_bytes,
    media_ext_for_mime,
    parse_data_uri,
    to_data_uri,
)
from ..plugin_api import (
    ConfigField,
    PluginDefinition,
    PluginManifest,
    PluginRequestContext,
    PluginRoute,
    ToolContext,
    ToolRegistration,
)

PLUGIN_ID = "image_gen"

_ENGINE_LOCK = threading.RLock()
_CUSTOM_ENGINES: Dict[str, Callable[[Dict[str, Any]], Any]] = {}


def register_image_engine(name: str, fn: Callable[[Dict[str, Any]], Any]) -> None:
    """Register an in-process Python image generation engine (`engine='custom_python'`)."""
    clean = str(name or "").strip() or "default"
    with _ENGINE_LOCK:
        _CUSTOM_ENGINES[clean] = fn


def unregister_image_engine(name: str) -> None:
    clean = str(name or "").strip() or "default"
    with _ENGINE_LOCK:
        _CUSTOM_ENGINES.pop(clean, None)


def _sniff_image_mime(raw: bytes, default: str = "image/png") -> str:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
        return "image/gif"
    head = raw[:256].lstrip()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in head.lower():
        return "image/svg+xml"
    return default


def _openai_images_url(base_url: str) -> str:
    url = (base_url or "https://api.openai.com").strip().rstrip("/")
    if url.endswith("/images/generations"):
        return url
    return (url + "/images/generations") if url.endswith("/v1") else (url + "/v1/images/generations")


def _decode_b64_or_data_uri(val: str) -> Tuple[str, bytes]:
    s = str(val or "").strip()
    if s.lower().startswith("data:"):
        return parse_data_uri(s, max_bytes=DEFAULT_MAX_MEDIA_BYTES)
    raw = base64.b64decode(s, validate=True)
    if len(raw) > DEFAULT_MAX_MEDIA_BYTES:
        raise ValueError("生成图像体积超过上限")
    return _sniff_image_mime(raw), raw


def _extract_image_from_payload(doc: Any, *, allow_loopback: bool) -> Tuple[str, bytes]:
    if isinstance(doc, bytes):
        if len(doc) > DEFAULT_MAX_MEDIA_BYTES:
            raise ValueError("生成图像体积超过上限")
        return _sniff_image_mime(doc), doc
    if isinstance(doc, str):
        if doc.lower().startswith(("http://", "https://")):
            ctype, raw = fetch_media_bytes(doc, max_bytes=DEFAULT_MAX_MEDIA_BYTES, timeout_s=15.0, allow_loopback=allow_loopback)
            return (ctype if ctype.startswith("image/") else _sniff_image_mime(raw)), raw
        return _decode_b64_or_data_uri(doc)
    if not isinstance(doc, Mapping):
        raise ValueError("引擎响应格式不支持")

    for k in ("image_base64", "b64_json", "data_uri"):
        if doc.get(k):
            return _decode_b64_or_data_uri(str(doc[k]))

    images = doc.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, str) and first.strip():
            return _decode_b64_or_data_uri(first)

    data_list = doc.get("data")
    if isinstance(data_list, list) and data_list:
        item = data_list[0]
        if isinstance(item, Mapping):
            if item.get("b64_json"):
                return _decode_b64_or_data_uri(str(item["b64_json"]))
            if item.get("url"):
                ctype, raw = fetch_media_bytes(
                    str(item["url"]), max_bytes=DEFAULT_MAX_MEDIA_BYTES, timeout_s=15.0, allow_loopback=allow_loopback
                )
                return (ctype if ctype.startswith("image/") else _sniff_image_mime(raw)), raw

    for uk in ("image_url", "url"):
        if doc.get(uk):
            ctype, raw = fetch_media_bytes(
                str(doc[uk]), max_bytes=DEFAULT_MAX_MEDIA_BYTES, timeout_s=15.0, allow_loopback=allow_loopback
            )
            return (ctype if ctype.startswith("image/") else _sniff_image_mime(raw)), raw

    raise ValueError("响应中缺少 image_base64 / b64_json / image_url")


def _quota_fns(daemon: Any):
    if daemon is None:
        return None, None
    router = getattr(daemon, "model_router", None)
    chk = getattr(router, "check_plugin_quota", None) or getattr(daemon, "check_plugin_quota", None)
    rec = getattr(router, "record_plugin_call", None) or getattr(daemon, "record_plugin_call", None)
    return (chk if callable(chk) else None), (rec if callable(rec) else None)


def generate_image_asset(
    prompt: str,
    *,
    negative_prompt: str = "",
    size: str = "",
    seed: Optional[int] = None,
    scope: str = "private",
    actor: str = "",
    cfg: Optional[Mapping[str, Any]] = None,
    daemon: Any = None,
    speaker_id: str = "",
    is_owner: bool = True,
) -> Dict[str, Any]:
    """Generate an image via OpenAI `/v1/images/generations`, BYO HTTP JSON contract, or Python hook."""
    clean_prompt = str(prompt or "").strip()[:500]
    if not clean_prompt:
        return {"ok": False, "degraded": False, "status": "invalid_input", "error": "文生图提示词不能为空"}

    if daemon is not None:
        check_ch = f"private:{actor}" if scope == "private" else "group"
        guard = getattr(daemon, "guard", None)
        if guard is not None:
            why = guard.check(check_ch, clean_prompt)
            if why:
                return {"ok": False, "degraded": False, "status": "blocked", "error": f"guard:{why}"}
        mem = getattr(daemon, "memory", None)
        if scope != "private" and actor and mem is not None and actor in getattr(daemon.vocab, "ROSTER", ()):
            now = float(daemon.clock.now())
            if mem.check_outbound_leak(actor, "group", clean_prompt, now=now):
                return {"ok": False, "degraded": False, "status": "blocked", "error": "memory_leak:private_to_public"}

    conf = dict(cfg or {})
    engine = str(conf.get("engine") or "openai").strip().lower()
    base_url = str(conf.get("base_url") or "").strip().rstrip("/")
    api_key = str(conf.get("api_key") or "").strip()
    model = str(conf.get("model") or "gpt-image-1").strip() or "gpt-image-1"
    eff_size = str(size or conf.get("size") or "1024x1024").strip() or "1024x1024"
    timeout_s = max(2.0, min(float(conf.get("timeout_s") or 30.0), 120.0))
    allow_loopback = bool(conf.get("allow_loopback_media", False))
    daily_limit = max(1, min(int(conf.get("daily_limit") or 60), 5000))

    req_spec: Dict[str, Any] = {
        "prompt": clean_prompt,
        "negative_prompt": str(negative_prompt or "").strip()[:300],
        "size": eff_size,
        "actor": str(actor or ""),
        "model": model,
    }
    if seed is not None:
        req_spec["seed"] = int(seed)

    chk_quota, rec_call = _quota_fns(daemon)

    if engine == "custom_python":
        with _ENGINE_LOCK:
            fn = _CUSTOM_ENGINES.get(model) or _CUSTOM_ENGINES.get("default")
        if fn is None:
            return {
                "ok": False,
                "degraded": True,
                "status": "unconfigured",
                "error": f"未注册 Python 自带出图引擎 ({model})",
            }
        if chk_quota is not None:
            q_err = chk_quota(PLUGIN_ID, speaker_id=speaker_id, is_owner=is_owner, daily_limit=daily_limit)
            if q_err:
                return {"ok": False, "degraded": True, "status": "quota_exceeded", "error": q_err}
        t0 = time.monotonic()
        try:
            raw_out = fn(dict(req_spec))
            mime, img_bytes = _extract_image_from_payload(raw_out, allow_loopback=allow_loopback)
        except Exception as exc:  # noqa: BLE001
            if rec_call is not None:
                rec_call(
                    PLUGIN_ID,
                    actor=actor,
                    model=model,
                    base_url="custom_python://local",
                    speaker_id=speaker_id,
                    is_owner=is_owner,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    ok=False,
                    error_code=type(exc).__name__,
                )
            return {
                "ok": False,
                "degraded": True,
                "status": "upstream_error",
                "error": mask_secrets(f"{type(exc).__name__}: {exc}", api_key=api_key),
            }
    elif engine == "byo_http":
        if not base_url:
            return {"ok": False, "degraded": True, "status": "unconfigured", "error": "未配置自带引擎 URL"}
        if not base_url.startswith(("http://", "https://")):
            return {
                "ok": False,
                "degraded": True,
                "status": "misconfigured",
                "error": "自带引擎 URL 必须以 http:// 或 https:// 开头",
            }
        if chk_quota is not None:
            q_err = chk_quota(PLUGIN_ID, speaker_id=speaker_id, is_owner=is_owner, daily_limit=daily_limit)
            if q_err:
                return {"ok": False, "degraded": True, "status": "quota_exceeded", "error": q_err}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ai-house-kit/0.3 (+https://github.com/Gengetau/ai-house-kit)",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(
            base_url,
            data=json.dumps(req_spec, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body_bytes = resp.read(DEFAULT_MAX_MEDIA_BYTES * 2)
                ctype = str(resp.headers.get("Content-Type") or "").lower()
            if ctype.startswith("image/"):
                mime, img_bytes = ctype.split(";", 1)[0].strip(), body_bytes
            else:
                doc = json.loads(body_bytes.decode("utf-8", errors="replace"))
                mime, img_bytes = _extract_image_from_payload(doc, allow_loopback=allow_loopback)
        except urllib.error.HTTPError as exc:
            if rec_call is not None:
                rec_call(
                    PLUGIN_ID,
                    actor=actor,
                    model=model,
                    base_url=base_url,
                    speaker_id=speaker_id,
                    is_owner=is_owner,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    ok=False,
                    error_code=f"HTTP_{exc.code}",
                )
            detail = _extract_http_error_detail(exc, api_key=api_key)
            return {"ok": False, "degraded": True, "status": "upstream_error", "error": f"HTTP {exc.code}: {detail}"}
        except Exception as exc:  # noqa: BLE001
            if rec_call is not None:
                rec_call(
                    PLUGIN_ID,
                    actor=actor,
                    model=model,
                    base_url=base_url,
                    speaker_id=speaker_id,
                    is_owner=is_owner,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    ok=False,
                    error_code=type(exc).__name__,
                )
            return {
                "ok": False,
                "degraded": True,
                "status": "upstream_error",
                "error": mask_secrets(f"{type(exc).__name__}: {exc}", api_key=api_key),
            }
    else:
        if not api_key:
            return {"ok": False, "degraded": True, "status": "unconfigured", "error": "未配置文生图 API Key"}
        if not base_url or not base_url.startswith(("http://", "https://")):
            return {
                "ok": False,
                "degraded": True,
                "status": "misconfigured",
                "error": "文生图 Base URL 必须以 http:// 或 https:// 开头",
            }
        if chk_quota is not None:
            q_err = chk_quota(PLUGIN_ID, speaker_id=speaker_id, is_owner=is_owner, daily_limit=daily_limit)
            if q_err:
                return {"ok": False, "degraded": True, "status": "quota_exceeded", "error": q_err}
        oai_payload = {"model": model, "prompt": clean_prompt, "size": eff_size, "n": 1}
        req = urllib.request.Request(
            _openai_images_url(base_url),
            data=json.dumps(oai_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "ai-house-kit/0.3 (+https://github.com/Gengetau/ai-house-kit)",
            },
            method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                doc = json.loads(resp.read().decode("utf-8", errors="replace"))
            mime, img_bytes = _extract_image_from_payload(doc, allow_loopback=allow_loopback)
        except urllib.error.HTTPError as exc:
            if rec_call is not None:
                rec_call(
                    PLUGIN_ID,
                    actor=actor,
                    model=model,
                    base_url=base_url,
                    speaker_id=speaker_id,
                    is_owner=is_owner,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    ok=False,
                    error_code=f"HTTP_{exc.code}",
                )
            detail = _extract_http_error_detail(exc, api_key=api_key)
            return {"ok": False, "degraded": True, "status": "upstream_error", "error": f"HTTP {exc.code}: {detail}"}
        except Exception as exc:  # noqa: BLE001
            if rec_call is not None:
                rec_call(
                    PLUGIN_ID,
                    actor=actor,
                    model=model,
                    base_url=base_url,
                    speaker_id=speaker_id,
                    is_owner=is_owner,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    ok=False,
                    error_code=type(exc).__name__,
                )
            return {
                "ok": False,
                "degraded": True,
                "status": "upstream_error",
                "error": mask_secrets(f"{type(exc).__name__}: {exc}", api_key=api_key),
            }

    if rec_call is not None:
        rec_call(
            PLUGIN_ID,
            actor=actor,
            model=model,
            base_url=base_url or f"{engine}://local",
            speaker_id=speaker_id,
            is_owner=is_owner,
            latency_ms=int((time.monotonic() - t0) * 1000),
            ok=True,
        )

    ext = media_ext_for_mime(mime, default="png")
    media_rel = ""
    rt_dir = getattr(daemon, "runtime_dir", None) if daemon is not None else None
    if rt_dir:
        eff_scope = "private" if str(scope or "").strip().lower() == "private" else ("world" if str(scope or "").strip().lower() == "world" else "group")
        digest = hashlib.sha256(img_bytes).hexdigest()[:16]
        out_dir = Path(rt_dir) / "media" / "generated" / eff_scope
        out_dir.mkdir(parents=True, exist_ok=True)
        fp = out_dir / f"{digest}.{ext}"
        fp.write_bytes(img_bytes)
        media_rel = f"media/generated/{eff_scope}/{fp.name}"

    return {
        "ok": True,
        "degraded": False,
        "status": "ok",
        "engine": engine,
        "model": model,
        "mime": mime,
        "bytes": len(img_bytes),
        "media_rel": media_rel,
        "image_base64": base64.b64encode(img_bytes).decode("ascii"),
        "data_uri": to_data_uri(img_bytes, mime),
    }


def _tool_generate_image(
    *,
    prompt: str = "",
    size: str = "",
    ctx: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    cfg = ctx.plugin_config if ctx is not None else {}
    daemon = ctx.daemon if ctx is not None else None
    scope = ctx.scope if ctx is not None else "group"
    actor = ctx.actor if ctx is not None else ""
    sp = ctx.speaker if ctx is not None else None
    sp_id = str(getattr(sp, "identity_id", "") or getattr(sp, "name", "") or "")
    is_owner = bool(getattr(sp, "is_owner", True)) if sp is not None else True
    res = generate_image_asset(
        prompt,
        size=size,
        scope=scope,
        actor=actor,
        cfg=cfg,
        daemon=daemon,
        speaker_id=sp_id,
        is_owner=is_owner,
    )
    if res.get("ok"):
        if ctx is not None and isinstance(ctx.pending_media, list):
            ctx.pending_media.append({
                "kind": "image",
                "media_rel": str(res.get("media_rel") or ""),
                "mime": str(res.get("mime") or "image/png"),
                "title": str(prompt or "生成插图")[:40],
                "image_base64": str(res.get("image_base64") or ""),
            })
        return {
            "ok": True,
            "media_rel": res.get("media_rel"),
            "mime": res.get("mime"),
            "bytes": res.get("bytes"),
        }
    return res


def _route_generate(rctx: PluginRequestContext) -> Dict[str, Any]:
    body = rctx.body if isinstance(rctx.body, Mapping) else {}
    prompt = str(body.get("prompt") or "").strip()
    negative_prompt = str(body.get("negative_prompt") or "").strip()
    size = str(body.get("size") or "").strip()
    seed = int(body["seed"]) if body.get("seed") is not None else None
    scope = str(body.get("scope") or "private").strip().lower()
    actor = str(body.get("actor") or "").strip()
    return generate_image_asset(
        prompt,
        negative_prompt=negative_prompt,
        size=size,
        seed=seed,
        scope=scope,
        actor=actor,
        cfg=rctx.plugin_config,
        daemon=rctx.daemon,
    )


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id=PLUGIN_ID,
        name="文生图（含自带引擎接口）",
        kind="bundle",
        version="0.3.0",
        description="可选接入 OpenAI /v1/images/generations、自带 HTTP JSON 引擎（byo_http）或 Python 内存出图回调；未配置或出错时静默降级。",
        default_enabled=False,
        config_fields=(
            ConfigField(
                key="engine",
                label="出图引擎模式",
                field_type="select",
                default="openai",
                options=(
                    ("openai", "OpenAI 兼容 /v1/images/generations"),
                    ("byo_http", "自带 HTTP 引擎（POST JSON）"),
                    ("custom_python", "自带 Python 回调引擎"),
                ),
                env_var="AI_HOUSE_IMAGE_ENGINE",
                description="选择标准云端接口或自建本地生图服务契约。",
            ),
            ConfigField(
                key="base_url",
                label="引擎 Base URL / 端点地址",
                field_type="string",
                default="https://api.openai.com",
                env_var="AI_HOUSE_IMAGE_BASE_URL",
                placeholder="https://api.openai.com 或 http://127.0.0.1:7860/sdapi/v1/txt2img",
                description="openai 模式填写基址，byo_http 模式填写完整 POST 端点。",
            ),
            ConfigField(
                key="api_key",
                label="引擎 API Key",
                field_type="string",
                default="",
                secret=True,
                env_var="AI_HOUSE_IMAGE_API_KEY",
                description="云端密钥（本地自带引擎无鉴权时可留空）。",
            ),
            ConfigField(
                key="model",
                label="模型或自带引擎代号",
                field_type="string",
                default="gpt-image-1",
                env_var="AI_HOUSE_IMAGE_MODEL",
                description="模型名称或 custom_python 注册键名。",
            ),
            ConfigField(
                key="size",
                label="默认输出尺寸",
                field_type="select",
                default="1024x1024",
                options=(("512x512", "512x512"), ("1024x1024", "1024x1024"), ("1024x768", "1024x768")),
                env_var="AI_HOUSE_IMAGE_SIZE",
                description="默认画布分辨率。",
            ),
            ConfigField(
                key="timeout_s",
                label="生成超时（秒）",
                field_type="float",
                default=30.0,
                min_val=2.0,
                max_val=120.0,
                env_var="AI_HOUSE_IMAGE_TIMEOUT_S",
                description="单张图片生成 HTTP 超时上限。",
            ),
            ConfigField(
                key="allow_loopback_media",
                label="允许回源拉取本机图片链接",
                field_type="bool",
                default=False,
                env_var="AI_HOUSE_IMAGE_ALLOW_LOOPBACK",
                description="引擎返回 127.0.0.1 URL 而非 base64 时开启。",
            ),
            ConfigField(
                key="daily_limit",
                label="每日调用上限",
                field_type="int",
                default=60,
                min_val=1,
                max_val=5000,
                env_var="AI_HOUSE_IMAGE_DAILY_LIMIT",
                description="每天允许调用文生图引擎的最大次数。",
            ),
        ),
    ),
    tools=(
        ToolRegistration(
            spec={
                "name": "generate_image",
                "description": "根据中文或英文画面描述生成插图并保存到本地媒体目录（发前经过隐私检查）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "不含任何秘密的画面描述"},
                        "size": {"type": "string", "description": "可选尺寸如 1024x1024"},
                    },
                    "required": ["prompt"],
                },
            },
            handler=_tool_generate_image,
            plugin_id=PLUGIN_ID,
            takes_context=True,
        ),
    ),
    routes=(
        PluginRoute(method="POST", subpath="/generate", handler=_route_generate, tier="write"),
    ),
)
