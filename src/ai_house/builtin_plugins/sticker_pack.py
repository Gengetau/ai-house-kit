"""Built-in demo tool plugin: safe roommate mood stickers (`pick_mood_sticker`)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..plugin_api import ConfigField, PluginDefinition, PluginManifest, ToolContext, ToolRegistration

STICKERS: Dict[str, Dict[str, str]] = {
    "happy": {"emoji": "😊", "label": "开心比耶"},
    "cozy": {"emoji": "☕", "label": "暖茶贴贴"},
    "hungry": {"emoji": "🍛", "label": "馋咖喱了"},
    "sleepy": {"emoji": "🌙", "label": "困到打哈欠"},
    "cheer": {"emoji": "✨", "label": "元气满满"},
    "focus": {"emoji": "📖", "label": "认真研读"},
    "rainy": {"emoji": "☔", "label": "雨天慢节奏"},
}


def pick_mood_sticker(mood: str = "", ctx: Optional[ToolContext] = None) -> Dict[str, Any]:
    fallback = str((ctx.plugin_config or {}).get("default_mood") or "happy").strip().lower() if ctx is not None else "happy"
    key = str(mood or "").strip().lower() or fallback
    spec = STICKERS.get(key) or STICKERS.get(fallback) or STICKERS["happy"]
    chosen = key if key in STICKERS else (fallback if fallback in STICKERS else "happy")
    return {"ok": True, "mood": chosen, **spec, "tag": f"[{spec['emoji']} {spec['label']}]"}


TOOL_SPEC = {
    "name": "pick_mood_sticker",
    "description": "挑选一个符合当前心情的合租屋表情贴纸标签用于回复。",
    "parameters": {
        "type": "object",
        "properties": {
            "mood": {
                "type": "string",
                "enum": sorted(STICKERS.keys()),
                "description": "心情类别",
            }
        },
        "required": ["mood"],
    },
}


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id="sticker_pack",
        name="情绪贴纸包",
        kind="tool",
        version="0.3.0",
        description="提供内置表情贴纸 pick_mood_sticker 工具，用轻量文案表情点缀回复。",
        default_enabled=True,
        config_fields=(
            ConfigField(
                key="default_mood",
                label="默认情绪贴纸",
                field_type="select",
                default="happy",
                options=tuple((k, v["label"]) for k, v in STICKERS.items()),
                env_var="AI_HOUSE_STICKER_DEFAULT_MOOD",
                description="模型未传或传入未知心情时的兜底贴纸。",
            ),
        ),
    ),
    tools=(ToolRegistration(spec=TOOL_SPEC, handler=pick_mood_sticker, plugin_id="sticker_pack"),),
)
