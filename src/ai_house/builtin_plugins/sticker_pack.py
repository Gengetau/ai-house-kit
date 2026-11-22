"""Built-in demo tool plugin: safe roommate mood stickers (`pick_mood_sticker`)."""
from __future__ import annotations

from typing import Any, Dict

STICKERS: Dict[str, Dict[str, str]] = {
    "happy": {"emoji": "😊", "label": "开心比耶"},
    "cozy": {"emoji": "☕", "label": "暖茶贴贴"},
    "hungry": {"emoji": "🍛", "label": "馋咖喱了"},
    "sleepy": {"emoji": "🌙", "label": "困到打哈欠"},
    "cheer": {"emoji": "✨", "label": "元气满满"},
    "focus": {"emoji": "📖", "label": "认真研读"},
    "rainy": {"emoji": "☔", "label": "雨天慢节奏"},
}


def pick_mood_sticker(mood: str = "happy") -> Dict[str, Any]:
    key = str(mood or "happy").strip().lower()
    spec = STICKERS.get(key) or STICKERS["happy"]
    return {"ok": True, "mood": key if key in STICKERS else "happy", **spec, "tag": f"[{spec['emoji']} {spec['label']}]"}


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
