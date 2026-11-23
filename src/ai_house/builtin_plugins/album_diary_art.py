"""Stage 3.6 optional scope-isolated album & diary illustration plugin (`album_diary_art`, default disabled)."""
from __future__ import annotations

import html
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..chat_identity import quote_untrusted
from ..media_safety import DEFAULT_MAX_MEDIA_BYTES, fetch_media_bytes, parse_data_uri, resolve_runtime_media_path
from ..plugin_api import (
    AttachmentContext,
    AttachmentProcessor,
    ConfigField,
    PluginDefinition,
    PluginManifest,
    PluginRequestContext,
    PluginRoute,
    ToolContext,
    ToolRegistration,
)
from .image_gen import generate_image_asset

PLUGIN_ID = "album_diary_art"


def _render_fallback_diary_svg(actor_name: str, day_str: str, summary: str) -> bytes:
    safe_name = html.escape(actor_name[:12] or "室友")
    safe_day = html.escape(day_str[:16])
    safe_line = html.escape(summary[:26])
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 240" role="img" '
        f'aria-label="{safe_name} {safe_day} 日记手绘">'
        '<rect width="360" height="240" rx="14" fill="#FAF2E4"/>'
        '<rect x="18" y="18" width="324" height="204" rx="10" fill="#FFFDF8" stroke="#D9C7B0" stroke-width="2"/>'
        '<circle cx="290" cy="64" r="24" fill="#FDE68A"/>'
        '<path d="M40 165 Q120 115 210 155 T320 140 L320 205 L40 205 Z" fill="#D8E8DF"/>'
        '<path d="M90 160 L90 118 L138 96 L186 118 L186 160 Z" fill="#E8B892" stroke="#8C4E32" stroke-width="2.5"/>'
        f'<text x="36" y="48" font-family="sans-serif" font-size="14" font-weight="bold" fill="#2F2923">{safe_day} · {safe_name}</text>'
        f'<text x="36" y="72" font-family="sans-serif" font-size="12" fill="#6F6558">{safe_line}</text>'
        "</svg>"
    )
    return svg.encode("utf-8")


def maybe_illustrate_diary(
    daemon: Any,
    saved_diary: Mapping[str, Any],
    job: Mapping[str, Any],
    now: float,
    *,
    cfg: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Optionally illustrate a public `world`-scoped evening diary; quiet no-op on missing/faulty image_gen."""
    conf = dict(cfg or {})
    if not bool(conf.get("auto_illustrate_diary", True)):
        return None
    if getattr(daemon, "album", None) is None or getattr(daemon, "diary", None) is None:
        return None

    aid = str(saved_diary.get("actor") or job.get("actor") or "").strip()
    day_str = str(saved_diary.get("day") or job.get("day") or "").strip()
    diary_text = str(saved_diary.get("text") or "").strip()
    name = str(saved_diary.get("name") or aid)
    if not aid or not day_str or not diary_text:
        return None

    style = str(conf.get("style_prompt") or "暖灯手绘绘本水彩风格，柔和室内光线，无文字水印").strip()
    prompt = f"{style}。温馨日常画面：{quote_untrusted(diary_text)[:120]}"

    # Public diary art strictly checks both ReplyGuard and private-to-public memory leaks
    if getattr(daemon, "guard", None) is not None and daemon.guard.check("world", prompt):
        return None
    if getattr(daemon, "memory", None) is not None and aid in getattr(daemon.vocab, "ROSTER", ()):
        if daemon.memory.check_outbound_leak(aid, "group", prompt, now=now):
            return None

    stored_plugins = daemon.settings_store.get_plugins_map()
    active_pids = {d.plugin_id for d in daemon.registry.active_definitions(daemon.env, stored_plugins=stored_plugins)}

    raw_bytes: Optional[bytes] = None
    mime = "image/png"
    if "image_gen" in active_pids:
        img_cfg = daemon.plugin_config("image_gen", stored_plugins)
        gen = generate_image_asset(prompt, scope="world", actor=aid, cfg=img_cfg, daemon=daemon)
        if gen.get("ok") and gen.get("data_uri"):
            try:
                mime, raw_bytes = parse_data_uri(str(gen["data_uri"]), max_bytes=DEFAULT_MAX_MEDIA_BYTES)
            except ValueError:
                raw_bytes = None

    if raw_bytes is None and bool(conf.get("diary_art_fallback_svg", False)):
        raw_bytes = _render_fallback_diary_svg(name, day_str, diary_text)
        mime = "image/svg+xml"

    if raw_bytes is None:
        return None

    item = daemon.album.add_item(
        scope="world",
        actor=aid,
        channel="diary",
        kind="diary_art",
        title=f"{day_str} · {name}的晚间日记配图",
        caption=diary_text[:110],
        raw_bytes=raw_bytes,
        mime=mime,
        diary_day=day_str,
        now=now,
    )
    daemon.diary.set_diary_image(day_str, aid, item["album_id"])
    return item


def archive_chat_images(
    images: Sequence[str],
    *,
    scope: str,
    channel: str,
    actor: str,
    daemon: Any,
    now: float,
    cfg: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Save inbound chat image attachments under their exact conversation scope (`private` stays `private`)."""
    conf = dict(cfg or {})
    if not bool(conf.get("save_chat_images", True)) or getattr(daemon, "album", None) is None:
        return []
    eff_scope = "private" if str(scope).strip().lower() == "private" else "group"
    saved: List[Dict[str, Any]] = []
    for raw_src in list(images or ())[:2]:
        src = str(raw_src or "").strip()
        if not src:
            continue
        try:
            if src.lower().startswith("data:"):
                mime, raw = parse_data_uri(src, max_bytes=DEFAULT_MAX_MEDIA_BYTES)
            elif src.lower().startswith(("http://", "https://")):
                stored = daemon.settings_store.get_plugins_map()
                vcfg = (
                    daemon.plugin_config("vision_understand", stored)
                    if "vision_understand" in daemon.registry.definitions
                    else {}
                )
                mime, raw = fetch_media_bytes(
                    src,
                    max_bytes=DEFAULT_MAX_MEDIA_BYTES,
                    timeout_s=3.0,
                    allow_loopback=bool(vcfg.get("allow_loopback_media", False)),
                )
            else:
                continue
            item = daemon.album.add_item(
                scope=eff_scope,
                actor=actor,
                channel=channel,
                kind="chat_image",
                title="私聊分享照片" if eff_scope == "private" else "群聊分享照片",
                caption="会话图片归档",
                raw_bytes=raw,
                mime=mime,
                now=now,
            )
            saved.append(item)
        except Exception:  # noqa: BLE001
            continue
    return saved


def _media_ref_visible(item: Mapping[str, Any], *, ctx_scope: str, actor: str, channel: str) -> bool:
    """Scope check for `save_to_album(media_rel=...)`: only images the current conversation could already see."""
    sc = str(item.get("scope") or "")
    if sc == "world":
        return True
    if sc == "private":
        return ctx_scope == "private" and bool(actor) and str(item.get("actor") or "") == actor
    if sc == "group":
        return ctx_scope != "private" and bool(channel) and str(item.get("channel") or "") == channel
    return False


def _tool_save_to_album(
    *,
    source: str = "",
    image_index: Any = None,
    media_rel: str = "",
    title: str = "",
    caption: str = "",
    ctx: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    daemon = ctx.daemon if ctx is not None else None
    if daemon is None or getattr(daemon, "album", None) is None:
        return {"ok": False, "degraded": True, "status": "unconfigured", "error": "相册尚未就绪"}
    eff_scope = "private" if (ctx is not None and ctx.scope == "private") else "group"
    actor = ctx.actor if ctx is not None else ""
    channel = ctx.channel if ctx is not None else ""
    now = float(ctx.now) if ctx is not None else 0.0
    clean_title = quote_untrusted(title or "合租屋瞬间")[:80]
    clean_cap = quote_untrusted(caption)[:160]
    if eff_scope != "private" and getattr(daemon, "memory", None) is not None and actor:
        if daemon.memory.check_outbound_leak(actor, "group", f"{clean_title} {clean_cap}", now=now):
            return {"ok": False, "status": "blocked", "error": "memory_leak:private_to_public"}
    invalid = {"ok": False, "degraded": True, "status": "invalid_source", "error": "请用 image_index（当前消息里第几张图，从 1 开始）、media_rel（已归档图片）或合法 data:image URI 指定图片"}
    raw: Optional[bytes] = None
    mime = "image/png"
    try:
        if image_index not in (None, ""):
            imgs = (ctx.attachments or {}).get("images") if (ctx is not None and isinstance(ctx.attachments, Mapping)) else None
            idx = int(image_index)
            if not imgs or not (1 <= idx <= len(imgs)):
                return {**invalid, "error": "当前消息没有这个序号的图片"}
            src = str(imgs[idx - 1] or "").strip()
            if src.lower().startswith("data:"):
                mime, raw = parse_data_uri(src, max_bytes=DEFAULT_MAX_MEDIA_BYTES)
            elif src.lower().startswith(("http://", "https://")):
                stored = daemon.settings_store.get_plugins_map()
                vcfg = (
                    daemon.plugin_config("vision_understand", stored)
                    if "vision_understand" in daemon.registry.definitions
                    else {}
                )
                mime, raw = fetch_media_bytes(
                    src,
                    max_bytes=DEFAULT_MAX_MEDIA_BYTES,
                    timeout_s=3.0,
                    allow_loopback=bool(vcfg.get("allow_loopback_media", False)),
                )
            else:
                return invalid
        elif str(media_rel or "").strip():
            ref = daemon.album.get_by_media_rel(str(media_rel), include_data_uri=False)
            if ref is None or not _media_ref_visible(ref, ctx_scope=eff_scope, actor=actor, channel=channel):
                # same answer for "missing" and "not yours": never reveal what exists in other scopes
                return {"ok": False, "status": "forbidden", "error": "找不到当前会话可见的这张图片"}
            fp = resolve_runtime_media_path(daemon.runtime_dir, str(ref["media_rel"]))
            raw = fp.read_bytes()
            mime = str(ref.get("mime") or "image/png")
        else:
            mime, raw = parse_data_uri(source, max_bytes=DEFAULT_MAX_MEDIA_BYTES)
    except Exception:  # noqa: BLE001
        return invalid
    item = daemon.album.add_item(
        scope=eff_scope,
        actor=actor,
        channel=channel,
        kind="photo",
        title=clean_title,
        caption=clean_cap,
        raw_bytes=raw,
        mime=mime,
        now=now,
    )
    return {"ok": True, "album_id": item["album_id"], "scope": item["scope"], "title": item["title"]}


def _tool_browse_album(
    *,
    scope: str = "",
    actor: str = "",
    ctx: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    daemon = ctx.daemon if ctx is not None else None
    if daemon is None or getattr(daemon, "album", None) is None:
        return {"ok": False, "degraded": True, "status": "unconfigured", "items": []}
    ctx_scope = ctx.scope if ctx is not None else "group"
    req_scope = str(scope or ("private" if ctx_scope == "private" else "world")).strip().lower()
    if ctx_scope != "private" and req_scope == "private":
        return {"ok": False, "status": "forbidden", "error": "非私聊上下文禁止读取私密相册", "items": []}
    if req_scope not in ("world", "group", "private"):
        req_scope = "world"

    eff_actor: Optional[str] = actor or None
    eff_channel = ""
    if req_scope == "private":
        # Strictly bind private album lookups to the conversing resident in ctx.actor
        eff_actor = (ctx.actor if ctx is not None and ctx.actor else eff_actor) or None
    elif req_scope == "group" and ctx is not None and ctx.channel:
        eff_channel = str(ctx.channel).strip()

    items = daemon.album.list_items(
        scope=req_scope,
        actor=eff_actor,
        channel=eff_channel,
        limit=8,
        include_data_uri=False,
    )
    if req_scope == "private":
        items = [
            {k: v for k, v in it.items() if k != "media_rel"}
            for it in items
        ]
    return {"ok": True, "scope": req_scope, "items": items}


def _process_attachments(actx: AttachmentContext) -> List[str]:
    imgs = actx.attachments.get("images") if isinstance(actx.attachments, Mapping) else None
    if not imgs:
        return []
    archive_chat_images(
        list(imgs),
        scope=actx.scope,
        channel=actx.channel,
        actor=actx.actor,
        daemon=actx.daemon,
        now=actx.now,
        cfg=actx.plugin_config,
    )
    return []


def _hint_attachments(actx: AttachmentContext) -> List[str]:
    imgs = actx.attachments.get("images") if isinstance(actx.attachments, Mapping) else None
    if not imgs:
        return []
    return [f"[本条消息附带 {len(imgs)} 张图片；想收进相册可调用 save_to_album，用 image_index=1..{len(imgs)} 指定]"]


def _route_public_items(rctx: PluginRequestContext) -> Dict[str, Any]:
    daemon = rctx.daemon
    if daemon is None or getattr(daemon, "album", None) is None:
        return {"ok": False, "degraded": True, "status": "unconfigured", "items": []}
    sc = (rctx.query.get("scope") or ["world"])[0].strip().lower()
    if sc != "world":
        raise ValueError("公开接口仅支持 scope=world（群聊与私密相册请访问 private 权限端点）")
    act = (rctx.query.get("actor") or [""])[0].strip() or None
    lim = int((rctx.query.get("limit") or [rctx.plugin_config.get("max_items") or 24])[0])
    items = daemon.album.list_items(scope="world", actor=act, limit=lim, include_data_uri=True)
    return {"ok": True, "scope": "world", "items": items}


def _route_private_items(rctx: PluginRequestContext) -> Dict[str, Any]:
    daemon = rctx.daemon
    if daemon is None or getattr(daemon, "album", None) is None:
        return {"ok": False, "degraded": True, "status": "unconfigured", "items": []}
    sc = (rctx.query.get("scope") or ["private"])[0].strip().lower()
    if sc not in ("private", "group"):
        sc = "private"
    act = (rctx.query.get("actor") or [""])[0].strip() or None
    lim = int((rctx.query.get("limit") or [rctx.plugin_config.get("max_items") or 24])[0])
    items = daemon.album.list_items(scope=sc, actor=act, limit=lim, include_data_uri=True)
    return {"ok": True, "scope": sc, "items": items}


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id=PLUGIN_ID,
        name="相册与日记配图（隐私分域）",
        kind="bundle",
        version="0.3.0",
        description="按 world / group / private 严格分域保管住客相册，并可在每晚日记生成后自动绘制配图；未配图或失败时保持纯文字日记不断档。",
        default_enabled=False,
        config_fields=(
            ConfigField(
                key="auto_illustrate_diary",
                label="每晚日记自动生成配图",
                field_type="bool",
                default=True,
                env_var="AI_HOUSE_ALBUM_DIARY_ART",
                description="启用时在 22:30 JST 日记落库后尝试调用 3.3 文生图绘制公开日记插画。",
            ),
            ConfigField(
                key="diary_art_fallback_svg",
                label="未配文生图时绘制离线简笔手帐图",
                field_type="bool",
                default=False,
                env_var="AI_HOUSE_ALBUM_FALLBACK_SVG",
                description="关闭时仅依赖 image_gen 出图，失败则保留纯文字日记。",
            ),
            ConfigField(
                key="save_chat_images",
                label="按聊天作用域自动收录分享图片",
                field_type="bool",
                default=True,
                env_var="AI_HOUSE_ALBUM_SAVE_CHAT_IMAGES",
                description="私聊图片只存 private 域，群聊图片只存 group 域，绝不混入公共相册或日记。",
            ),
            ConfigField(
                key="style_prompt",
                label="日记配图画风提示词",
                field_type="string",
                default="暖灯手绘绘本水彩风格，柔和室内光线，无文字水印",
                env_var="AI_HOUSE_ALBUM_STYLE_PROMPT",
                description="附加到每晚公开日记配图前的统一画风描述。",
            ),
            ConfigField(
                key="max_items",
                label="相册默认加载条数",
                field_type="int",
                default=24,
                min_val=1,
                max_val=100,
                env_var="AI_HOUSE_ALBUM_MAX_ITEMS",
                description="网页端单次最多展示的相册卡片数量。",
            ),
        ),
    ),
    tools=(
        ToolRegistration(
            spec={
                "name": "save_to_album",
                "description": "将当前会话里的图片存入对应作用域的相册（私聊仅存 private，群聊存 group）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_index": {"type": "integer", "description": "当前这条消息附带的第几张图片（从 1 开始）"},
                        "media_rel": {"type": "string", "description": "当前会话可见的已归档图片的 media_rel"},
                        "source": {"type": "string", "description": "data:image URI（一般用不到）"},
                        "title": {"type": "string"},
                        "caption": {"type": "string"},
                    },
                },
            },
            handler=_tool_save_to_album,
            plugin_id=PLUGIN_ID,
            takes_context=True,
        ),
        ToolRegistration(
            spec={
                "name": "browse_album",
                "description": "查看当前授权作用域内的相册记录（群聊严禁读取 private 相册）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string", "enum": ["world", "group", "private"]},
                        "actor": {"type": "string"},
                    },
                },
            },
            handler=_tool_browse_album,
            plugin_id=PLUGIN_ID,
            takes_context=True,
        ),
    ),
    routes=(
        PluginRoute(method="GET", subpath="/items", handler=_route_public_items, tier="read"),
        PluginRoute(method="GET", subpath="/private", handler=_route_private_items, tier="private"),
    ),
    attachment_processors=(
        AttachmentProcessor(processor_id="album_image_hint", handler=_hint_attachments, priority=11),
        AttachmentProcessor(processor_id="album_archive_images", handler=_process_attachments, priority=10, after_reply=True),
    ),
)
