"""Stage 3.5 optional open-source/attributable sticker & meme library plugin (`meme_library`, default disabled)."""
from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..media_safety import mime_for_path, to_data_uri
from ..plugin_api import (
    ConfigField,
    PluginDefinition,
    PluginManifest,
    PluginRequestContext,
    PluginRoute,
    ToolContext,
    ToolRegistration,
)

PLUGIN_ID = "meme_library"
_BUNDLED_DIR = Path(__file__).resolve().parent.parent / "bundled_stickers"
_ALLOWED_EXTS = frozenset({".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif"})
_MOOD_PALETTES = {
    "happy": (255, 224, 130, 240, 140, 60),
    "relaxed": (198, 226, 200, 78, 128, 86),
    "sleepy": (205, 214, 244, 92, 108, 168),
    "shy": (255, 205, 210, 210, 92, 118),
    "cozy": (255, 228, 196, 178, 108, 62),
}


def _render_sticker_png(mood: str = "relaxed", *, size: int = 64) -> bytes:
    """Render a compact valid 64x64 RGBA PNG badge via stdlib `zlib`+`struct` for channels that reject SVG."""
    bg_r, bg_g, bg_b, fg_r, fg_g, fg_b = _MOOD_PALETTES.get(str(mood or "").lower(), (220, 232, 224, 82, 118, 96))

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)

    rows = bytearray()
    half = size / 2.0
    rad2 = (size * 0.42) ** 2
    inner2 = (size * 0.22) ** 2
    for y in range(size):
        rows.append(0)
        dy = y + 0.5 - half
        for x in range(size):
            dx = x + 0.5 - half
            d2 = dx * dx + dy * dy
            if d2 > rad2:
                rows.extend((0, 0, 0, 0))
            elif d2 <= inner2:
                rows.extend((fg_r, fg_g, fg_b, 255))
            else:
                rows.extend((bg_r, bg_g, bg_b, 255))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(rows), level=6)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _load_pack_dir(
    pack_dir: Path,
    *,
    source: str,
    require_license: bool,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    items: List[Dict[str, Any]] = []
    warnings: List[str] = []
    root = pack_dir.resolve()
    if not root.is_dir():
        warnings.append(f"{source}: 表情包目录不存在 ({pack_dir.name})")
        return items, warnings

    mf = root / "manifest.json"
    if not mf.is_file():
        warnings.append(f"{source}: 缺少 manifest.json")
        return items, warnings

    try:
        doc = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warnings.append(f"{source}: manifest.json 解析失败 ({type(exc).__name__})")
        return items, warnings

    if not isinstance(doc, Mapping) or not isinstance(doc.get("items"), list):
        warnings.append(f"{source}: manifest.json 格式无效")
        return items, warnings

    pack_license = str(doc.get("license") or "").strip()
    pack_author = str(doc.get("author") or "").strip()
    for raw in doc["items"]:
        if not isinstance(raw, Mapping):
            continue
        mid = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or mid).strip()
        rel_file = str(raw.get("file") or "").strip().replace("\\", "/")
        lic = str(raw.get("license") or pack_license).strip()
        auth = str(raw.get("author") or pack_author).strip()
        if not mid or not rel_file:
            warnings.append(f"{source}: 条目缺少 id 或 file")
            continue
        if require_license and (not lic or not auth):
            warnings.append(f"{source}:{mid}: 缺少开源许可证或作者署名，已拒绝加载")
            continue
        try:
            fp = (root / rel_file).resolve()
        except OSError:
            warnings.append(f"{source}:{mid}: 文件路径非法")
            continue
        if not fp.is_relative_to(root) or not fp.is_file() or fp.suffix.lower() not in _ALLOWED_EXTS:
            warnings.append(f"{source}:{mid}: 文件不存在或越界")
            continue

        tags = [str(t).strip() for t in (raw.get("tags") or ()) if str(t).strip()]
        mood = str(raw.get("mood") or "neutral").strip()
        items.append({
            "id": mid,
            "title": title,
            "mood": mood,
            "tags": tags,
            "license": lic or "unspecified",
            "author": auth or "unspecified",
            "source": source,
            "mime": mime_for_path(fp, "image/svg+xml"),
            "_path": str(fp),
        })
    return items, warnings


def load_meme_catalog(cfg: Optional[Mapping[str, Any]] = None) -> Tuple[List[Dict[str, Any]], List[str], str]:
    """Load catalog from bundled CC0 stickers and optional `custom_dir`."""
    conf = dict(cfg or {})
    include_bundled = bool(conf.get("include_bundled", True))
    custom_dir = str(conf.get("custom_dir") or "").strip()
    require_license = bool(conf.get("require_license", True))

    if not include_bundled and not custom_dir:
        return [], ["未启用内置包且未配置 custom_dir"], "unconfigured"

    catalog: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seen_ids: set[str] = set()

    if custom_dir:
        c_items, c_warns = _load_pack_dir(Path(custom_dir), source="custom", require_license=require_license)
        warnings.extend(c_warns)
        for it in c_items:
            seen_ids.add(it["id"])
            catalog.append(it)

    if include_bundled:
        b_items, b_warns = _load_pack_dir(_BUNDLED_DIR, source="bundled", require_license=require_license)
        warnings.extend(b_warns)
        for it in b_items:
            if it["id"] not in seen_ids:
                seen_ids.add(it["id"])
                catalog.append(it)

    if not catalog:
        return [], warnings, "misconfigured" if warnings else "unconfigured"
    return catalog, warnings, "ok"


def search_meme_catalog(
    query: str = "",
    *,
    mood: str = "",
    cfg: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    conf = dict(cfg or {})
    max_results = max(1, min(int(conf.get("max_results") or 6), 24))
    catalog, warnings, status = load_meme_catalog(conf)
    q = str(query or "").strip().lower()
    m_filter = str(mood or "").strip().lower()

    matched: List[Dict[str, Any]] = []
    for it in catalog:
        if m_filter and str(it["mood"]).lower() != m_filter:
            continue
        if q:
            hay = " ".join([it["id"], it["title"], it["mood"], *it["tags"]]).lower()
            if q not in hay:
                continue
        matched.append({k: v for k, v in it.items() if k != "_path"})
        if len(matched) >= max_results:
            break

    return {
        "ok": status == "ok",
        "degraded": status != "ok",
        "status": status,
        "items": matched,
        "total_catalog": len(catalog),
        "warnings": warnings,
    }


def get_meme_payload(
    meme_id: str,
    *,
    cfg: Optional[Mapping[str, Any]] = None,
    include_bytes: bool = True,
) -> Dict[str, Any]:
    target_id = str(meme_id or "").strip()
    if not target_id:
        return {"ok": False, "degraded": False, "status": "invalid_input", "error": "缺少 meme_id"}
    catalog, warnings, status = load_meme_catalog(cfg)
    hit = next((it for it in catalog if it["id"] == target_id), None)
    if hit is None:
        return {
            "ok": False,
            "degraded": True,
            "status": status if status != "ok" else "not_found",
            "error": f"未找到表情包: {target_id}",
            "warnings": warnings,
        }
    out = {k: v for k, v in hit.items() if k != "_path"}
    if include_bytes:
        raw = Path(hit["_path"]).read_bytes()
        out["image_base64"] = base64.b64encode(raw).decode("ascii")
        out["data_uri"] = to_data_uri(raw, hit["mime"])
        out["bytes"] = len(raw)
    return {"ok": True, "degraded": False, "status": "ok", "item": out, "warnings": warnings}


def _tool_search_memes(
    *,
    query: str = "",
    mood: str = "",
    ctx: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    cfg = ctx.plugin_config if ctx is not None else {}
    return search_meme_catalog(query=query, mood=mood, cfg=cfg)


def _tool_send_meme(
    *,
    meme_id: str = "",
    caption: str = "",
    ctx: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    cfg = ctx.plugin_config if ctx is not None else {}
    daemon = ctx.daemon if ctx is not None else None
    scope = ctx.scope if ctx is not None else "group"
    actor = ctx.actor if ctx is not None else ""
    clean_cap = str(caption or "").strip()[:120]
    if clean_cap and daemon is not None:
        guard = getattr(daemon, "guard", None)
        if guard is not None:
            why = guard.check(f"private:{actor}" if scope == "private" else "group", clean_cap)
            if why:
                return {"ok": False, "status": "blocked", "error": f"guard:{why}"}
        mem = getattr(daemon, "memory", None)
        if scope != "private" and actor and mem is not None:
            if mem.check_outbound_leak(actor, "group", clean_cap, now=float(daemon.clock.now())):
                return {"ok": False, "status": "blocked", "error": "memory_leak:private_to_public"}

    res = get_meme_payload(meme_id, cfg=cfg, include_bytes=True)
    if not res.get("ok"):
        return res
    item = res["item"]
    if ctx is not None and isinstance(ctx.pending_media, list):
        item_mime = str(item.get("mime") or "image/svg+xml")
        if item_mime == "image/svg+xml":
            wire_png_b64 = base64.b64encode(_render_sticker_png(str(item.get("mood") or ""))).decode("ascii")
        else:
            wire_png_b64 = str(item.get("image_base64") or "")
        ctx.pending_media.append({
            "kind": "sticker",
            "meme_id": item["id"],
            "title": item["title"],
            "mime": item_mime,
            "data_uri": str(item.get("data_uri") or ""),
            "image_base64": wire_png_b64,
        })
    return {
        "ok": True,
        "meme_id": item["id"],
        "title": item["title"],
        "license": item["license"],
        "author": item["author"],
        "caption": clean_cap or f"[表情：{item['title']}]",
    }


def _route_catalog(rctx: PluginRequestContext) -> Dict[str, Any]:
    q = (rctx.query.get("q") or [""])[0]
    mood = (rctx.query.get("mood") or [""])[0]
    return search_meme_catalog(query=q, mood=mood, cfg=rctx.plugin_config)


def _route_item(rctx: PluginRequestContext) -> Dict[str, Any]:
    mid = (rctx.query.get("id") or [""])[0]
    return get_meme_payload(mid, cfg=rctx.plugin_config, include_bytes=True)


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id=PLUGIN_ID,
        name="表情包库（可署名贴纸集）",
        kind="bundle",
        version="0.3.0",
        description="提供可核验开源许可证与作者署名的贴纸检索与发送（内置原创 CC0-1.0 暖灯贴纸组，并支持自建目录扩展）。",
        default_enabled=False,
        config_fields=(
            ConfigField(
                key="custom_dir",
                label="自定义表情包目录",
                field_type="string",
                default="",
                env_var="AI_HOUSE_MEME_CUSTOM_DIR",
                placeholder="/path/to/stickers",
                description="包含 manifest.json 与 SVG/PNG 贴纸的本地目录。",
            ),
            ConfigField(
                key="require_license",
                label="强制校验开源许可证与署名",
                field_type="bool",
                default=True,
                env_var="AI_HOUSE_MEME_REQUIRE_LICENSE",
                description="开启后拒绝缺少 license 或 author 的外部条目，避免版权隐患。",
            ),
            ConfigField(
                key="max_results",
                label="单次最多返回贴纸数",
                field_type="int",
                default=6,
                min_val=1,
                max_val=24,
                env_var="AI_HOUSE_MEME_MAX_RESULTS",
                description="检索工具与目录接口返回条数上限。",
            ),
            ConfigField(
                key="include_bundled",
                label="包含内置 CC0 暖灯贴纸包",
                field_type="bool",
                default=True,
                env_var="AI_HOUSE_MEME_INCLUDE_BUNDLED",
                description="启用随包附带的 8 张原创 CC0-1.0 SVG 贴纸。",
            ),
        ),
    ),
    tools=(
        ToolRegistration(
            spec={
                "name": "search_memes",
                "description": "按关键词或心情检索带开源署名的表情贴纸库。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "关键词，如 茶 / 晚安 / 做饭"},
                        "mood": {"type": "string", "description": "可选心情，如 relaxed / sleepy / happy"},
                    },
                },
            },
            handler=_tool_search_memes,
            plugin_id=PLUGIN_ID,
            takes_context=True,
        ),
        ToolRegistration(
            spec={
                "name": "send_meme",
                "description": "挑选一张表情贴纸搭配回复送出。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "meme_id": {"type": "string", "description": "表情包 ID，例如 cozy_tea"},
                        "caption": {"type": "string", "description": "可选配文"},
                    },
                    "required": ["meme_id"],
                },
            },
            handler=_tool_send_meme,
            plugin_id=PLUGIN_ID,
            takes_context=True,
        ),
    ),
    routes=(
        PluginRoute(method="GET", subpath="/catalog", handler=_route_catalog, tier="read"),
        PluginRoute(method="GET", subpath="/item", handler=_route_item, tier="read"),
    ),
)
