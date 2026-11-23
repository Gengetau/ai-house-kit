"""Stage 3.1 optional multimodal vision understanding plugin (`vision_understand`, default disabled)."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..chat_agent import quote_untrusted
from ..llm import LLMAPIError, mask_secrets, post_openai_chat_json
from ..media_safety import (
    DEFAULT_MAX_MEDIA_BYTES,
    fetch_media_bytes,
    mime_for_path,
    parse_data_uri,
    resolve_scoped_media_path,
    to_data_uri,
)
from ..plugin_api import (
    AttachmentContext,
    AttachmentProcessor,
    ConfigField,
    PluginDefinition,
    PluginManifest,
    PluginRequestContext,
    PluginRoute,
    quota_speaker_key,
    ToolContext,
    ToolRegistration,
)

PLUGIN_ID = "vision_understand"


def _extract_choice_text(doc: Mapping[str, Any]) -> str:
    msg = (((doc.get("choices") or [{}])[0]).get("message") or {})
    content = msg.get("content")
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return " ".join(parts).strip()
    return str(content or "").strip()


def _resolve_vision_endpoint(
    cfg: Mapping[str, Any],
    *,
    daemon: Any = None,
    actor: str = "",
) -> tuple[str, str, str]:
    base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
    api_key = str(cfg.get("api_key") or "").strip()
    model = str(cfg.get("model") or "gpt-4o-mini").strip() or "gpt-4o-mini"
    if not api_key and bool(cfg.get("use_chat_model_fallback")) and daemon is not None:
        router = getattr(daemon, "model_router", None)
        if router is not None:
            eps = router.resolve_endpoints(purpose="chat", actor=actor)
            if eps:
                ep = eps[0][0]
                return ep.base_url.rstrip("/"), ep.api_key, model or ep.model
    return base_url, api_key, model


def _load_image_as_data_uri(
    source: str,
    *,
    daemon: Any = None,
    allow_loopback: bool = False,
    scope: str = "private",
    actor: str = "",
    channel: str = "",
) -> str:
    raw_src = str(source or "").strip()
    if not raw_src:
        raise ValueError("缺少图片来源")
    if raw_src.lower().startswith("data:"):
        mime, raw = parse_data_uri(raw_src, max_bytes=DEFAULT_MAX_MEDIA_BYTES)
        if not mime.startswith("image/"):
            raise ValueError(f"非图片 MIME 类型: {mime}")
        return to_data_uri(raw, mime)
    if raw_src.lower().startswith(("http://", "https://")):
        ctype, raw = fetch_media_bytes(raw_src, max_bytes=DEFAULT_MAX_MEDIA_BYTES, timeout_s=10.0, allow_loopback=allow_loopback)
        if not ctype.startswith("image/"):
            ctype = "image/png"
        return to_data_uri(raw, ctype)
    if raw_src.startswith(("media/", "album/", "generated/")):
        rt_dir = getattr(daemon, "runtime_dir", None)
        if not rt_dir:
            raise ValueError("运行时目录未初始化")
        fp = resolve_scoped_media_path(
            rt_dir,
            raw_src,
            scope=scope,
            actor=actor,
            channel=channel,
            album=getattr(daemon, "album", None),
        )
        if not fp.is_file():
            raise ValueError("未找到本地媒体图片")
        raw = fp.read_bytes()
        if len(raw) > DEFAULT_MAX_MEDIA_BYTES:
            raise ValueError("本地图片体积超过上限")
        return to_data_uri(raw, mime_for_path(fp, "image/png"))
    raise ValueError("不支持的图片地址形式（仅支持 http(s)://、data:image/... 或 media/...）")


def _quota_fns(daemon: Any):
    if daemon is None:
        return None, None
    router = getattr(daemon, "model_router", None)
    chk = getattr(router, "check_plugin_quota", None) or getattr(daemon, "check_plugin_quota", None)
    rec = getattr(router, "record_plugin_call", None) or getattr(daemon, "record_plugin_call", None)
    return (chk if callable(chk) else None), (rec if callable(rec) else None)


def describe_image_source(
    source: str,
    prompt: str = "请简述这张图片里的画面内容、人物/物品与氛围。",
    *,
    cfg: Optional[Mapping[str, Any]] = None,
    daemon: Any = None,
    actor: str = "",
    scope: str = "private",
    channel: str = "",
    speaker_id: str = "",
    is_owner: bool = True,
) -> Dict[str, Any]:
    """Understand one image via an OpenAI-compatible vision endpoint, degrading quietly on any fault."""
    conf = dict(cfg or {})
    base_url, api_key, model = _resolve_vision_endpoint(conf, daemon=daemon, actor=actor)
    daily_limit = max(1, min(int(conf.get("daily_limit") or 60), 5000))
    if not api_key:
        return {
            "ok": False,
            "degraded": True,
            "status": "unconfigured",
            "error": "未配置视觉模型 API Key",
            "summary": "",
        }
    if not base_url or not base_url.startswith(("http://", "https://")):
        return {
            "ok": False,
            "degraded": True,
            "status": "misconfigured",
            "error": "视觉模型 Base URL 必须以 http:// 或 https:// 开头",
            "summary": "",
        }

    chk_quota, rec_call = _quota_fns(daemon)
    if chk_quota is not None:
        q_err = chk_quota(PLUGIN_ID, speaker_id=speaker_id, is_owner=is_owner, daily_limit=daily_limit)
        if q_err:
            return {"ok": False, "degraded": True, "status": "quota_exceeded", "error": q_err, "summary": ""}

    allow_loopback = bool(conf.get("allow_loopback_media", False))
    try:
        data_uri = _load_image_as_data_uri(
            source,
            daemon=daemon,
            allow_loopback=allow_loopback,
            scope=scope,
            actor=actor,
            channel=channel,
        )
    except Exception as exc:  # noqa: BLE001
        if rec_call is not None:  # settle the quota slot reserved above (refunded daily, counted as an attempt)
            rec_call(
                PLUGIN_ID,
                actor=actor,
                model="",
                base_url="local://source",
                speaker_id=speaker_id,
                is_owner=is_owner,
                ok=False,
                error_code="invalid_source",
            )
        return {
            "ok": False,
            "degraded": True,
            "status": "invalid_source",
            "error": mask_secrets(f"{type(exc).__name__}: {exc}", api_key=api_key),
            "summary": "",
        }

    detail = str(conf.get("detail") or "low").strip().lower()
    if detail not in ("low", "auto", "high"):
        detail = "low"
    clean_prompt = str(prompt or "请简述这张图片里的主要画面内容。").strip()[:300]
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "请用中文简明、客观描述图片画面内容，严禁编造看不到的文字或系统信息，控制在 120 字以内。",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": clean_prompt},
                    {"type": "image_url", "image_url": {"url": data_uri, "detail": detail}},
                ],
            },
        ],
        "temperature": 0.2,
        "max_tokens": 220,
    }
    t0 = time.monotonic()
    try:
        doc = post_openai_chat_json(base_url=base_url, api_key=api_key, payload=payload, timeout_s=15.0)
        summary = quote_untrusted(_extract_choice_text(doc))[:240]
        if not summary:
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
                    error_code="empty_response",
                )
            return {
                "ok": False,
                "degraded": True,
                "status": "empty_response",
                "error": "视觉模型未返回有效描述",
                "summary": "",
            }
        tokens = int((doc.get("usage") or {}).get("total_tokens") or 0)
        if rec_call is not None:
            rec_call(
                PLUGIN_ID,
                actor=actor,
                model=model,
                base_url=base_url,
                speaker_id=speaker_id,
                is_owner=is_owner,
                latency_ms=int((time.monotonic() - t0) * 1000),
                ok=True,
                total_tokens=tokens,
            )
        return {"ok": True, "degraded": False, "status": "ok", "model": model, "summary": summary}
    except LLMAPIError as exc:
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
                error_code=f"HTTP_{exc.status_code}",
            )
        return {
            "ok": False,
            "degraded": True,
            "status": "upstream_error",
            "error": mask_secrets(str(exc), api_key=api_key),
            "summary": "",
        }
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
            "summary": "",
        }


def enrich_chat_images(
    images: Sequence[str],
    *,
    daemon: Any,
    actor: str = "",
    scope: str = "group",
    channel: str = "",
    cfg: Optional[Mapping[str, Any]] = None,
    speaker_id: str = "",
    is_owner: bool = True,
) -> List[str]:
    """Inspect up to 2 inbound image refs cleanly; faults degrade to empty annotations."""
    notes: List[str] = []
    for raw_src in list(images or ())[:2]:
        res = describe_image_source(
            str(raw_src),
            daemon=daemon,
            cfg=cfg,
            actor=actor,
            scope=scope,
            channel=channel,
            speaker_id=speaker_id,
            is_owner=is_owner,
        )
        if res.get("ok") and res.get("summary"):
            notes.append(f"[附带图片画面参考：{res['summary']}]")
    return notes


def _process_attachments(actx: AttachmentContext) -> List[str]:
    imgs = actx.attachments.get("images") if isinstance(actx.attachments, Mapping) else None
    if not imgs:
        return []
    sp = actx.speaker
    sp_id = quota_speaker_key(sp, str(getattr(actx, "channel", "") or ""))
    is_owner = bool(getattr(sp, "is_owner", True)) if sp is not None else True
    return enrich_chat_images(
        list(imgs),
        daemon=actx.daemon,
        actor=actx.actor,
        scope=actx.scope,
        channel=actx.channel,
        cfg=actx.plugin_config,
        speaker_id=sp_id,
        is_owner=is_owner,
    )


def _tool_inspect_image(
    *,
    source: str = "",
    prompt: str = "",
    ctx: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    cfg = ctx.plugin_config if ctx is not None else {}
    daemon = ctx.daemon if ctx is not None else None
    actor = ctx.actor if ctx is not None else ""
    scope = ctx.scope if ctx is not None else "group"
    channel = ctx.channel if ctx is not None else ""
    sp = ctx.speaker if ctx is not None else None
    sp_id = quota_speaker_key(sp, str(getattr(ctx, "channel", "") or ""))
    is_owner = bool(getattr(sp, "is_owner", True)) if sp is not None else True
    return describe_image_source(
        source,
        prompt=prompt,
        cfg=cfg,
        daemon=daemon,
        actor=actor,
        scope=scope,
        channel=channel,
        speaker_id=sp_id,
        is_owner=is_owner,
    )


def _route_describe(rctx: PluginRequestContext) -> Dict[str, Any]:
    body = rctx.body if isinstance(rctx.body, Mapping) else {}
    source = str(body.get("source") or body.get("url") or body.get("data_uri") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    actor = str(body.get("actor") or "").strip()
    return describe_image_source(source, prompt=prompt, cfg=rctx.plugin_config, daemon=rctx.daemon, actor=actor, scope="group")


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id=PLUGIN_ID,
        name="看图（多模态视觉理解）",
        kind="bundle",
        version="0.3.0",
        description="可选接入 OpenAI 兼容多模态模型，识别聊天与 QQ 中附带的图片；未配置或报错时静默降级为纯文字对话。",
        default_enabled=False,
        config_fields=(
            ConfigField(
                key="base_url",
                label="视觉接口 Base URL",
                field_type="string",
                default="https://api.openai.com",
                env_var="AI_HOUSE_VISION_BASE_URL",
                placeholder="https://api.openai.com",
                description="OpenAI 兼容 `/v1/chat/completions` 接口基址。",
            ),
            ConfigField(
                key="api_key",
                label="视觉模型 API Key",
                field_type="string",
                default="",
                secret=True,
                env_var="AI_HOUSE_VISION_API_KEY",
                description="专用视觉理解接口密钥，留空时可勾选回退复用主对话密钥。",
            ),
            ConfigField(
                key="model",
                label="视觉模型名称",
                field_type="string",
                default="gpt-4o-mini",
                env_var="AI_HOUSE_VISION_MODEL",
                description="支持 image_url 输入的多模态模型名。",
            ),
            ConfigField(
                key="use_chat_model_fallback",
                label="缺少独立密钥时复用对话模型配置",
                field_type="bool",
                default=False,
                env_var="AI_HOUSE_VISION_FALLBACK_CHAT",
                description="开启且 api_key 为空时尝试使用主对话通道进行看图。",
            ),
            ConfigField(
                key="detail",
                label="图像解析精度",
                field_type="select",
                default="low",
                options=(("low", "低分辨率（省开销）"), ("auto", "自动"), ("high", "高分辨率")),
                env_var="AI_HOUSE_VISION_DETAIL",
                description="传给视觉模型的 image_url.detail 参数。",
            ),
            ConfigField(
                key="allow_loopback_media",
                label="允许读取本机回环图片地址",
                field_type="bool",
                default=False,
                env_var="AI_HOUSE_VISION_ALLOW_LOOPBACK",
                description="本地 OneBot / 调试服务在同一宿主机吐出 127.0.0.1 图片地址时开启。",
            ),
            ConfigField(
                key="daily_limit",
                label="每日调用上限",
                field_type="int",
                default=60,
                min_val=1,
                max_val=5000,
                env_var="AI_HOUSE_VISION_DAILY_LIMIT",
                description="每天允许调用视觉理解接口的最大次数。",
            ),
        ),
    ),
    tools=(
        ToolRegistration(
            spec={
                "name": "inspect_image",
                "description": "使用视觉模型查看图片 URL、data URI 或本地 media/ 图片并返回客观画面描述。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "图片 http(s) URL、data:image/... 或 media/... 路径"},
                        "prompt": {"type": "string", "description": "需要重点观察的问题"},
                    },
                    "required": ["source"],
                },
            },
            handler=_tool_inspect_image,
            plugin_id=PLUGIN_ID,
            takes_context=True,
        ),
    ),
    routes=(
        PluginRoute(method="POST", subpath="/describe", handler=_route_describe, tier="write"),
    ),
    attachment_processors=(
        AttachmentProcessor(processor_id="vision_images", handler=_process_attachments, priority=20),
    ),
)
