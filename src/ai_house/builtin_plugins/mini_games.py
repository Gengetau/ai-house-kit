"""Built-in stateless mini-games plugin (`play_rps`, `draw_fortune_slip`, `random_pick`, `word_chain_step`)."""
from __future__ import annotations

import hashlib
import random
import re
from typing import Any, Dict, List, Optional, Sequence, Union

from ..plugin_api import PluginDefinition, PluginManifest, ToolRegistration

_RPS_MAP = {
    "石头": "rock", "石": "rock", "rock": "rock", "拳": "rock",
    "剪刀": "scissors", "剪": "scissors", "scissors": "scissors",
    "布": "paper", "paper": "paper",
}
_RPS_ZH = {"rock": "石头", "scissors": "剪刀", "paper": "布"}
_RPS_BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}

_FORTUNES = (
    ("大吉", "今日宜煮壶热茶慢慢聊天，厨房可能有意外惊喜。"),
    ("中吉", "手头的事情按部就班推进即可，傍晚适合散步透气。"),
    ("小吉", "适合整理桌面或翻几页旧书，会有温柔的小收获。"),
    ("吉", "平顺安稳的一天，记得按时吃饭早点休息。"),
    ("半吉", "小插曲很快会过去，找室友碰一碰杯吧。"),
    ("末吉", "别急着做决定，等太阳落山再看会更清晰。"),
)

_WORD_BANK = (
    "星光", "茶杯", "晨露", "微风", "书页", "庭院", "灯火", "苹果", "果酱", "晚风",
    "风铃", "月色", "雨声", "咖啡", "云朵", "厨房", "屋檐", "阳台", "阳光", "暖茶",
)


def play_rps(choice: str, seed_hint: str = "") -> Dict[str, Any]:
    raw = str(choice or "").strip().lower()
    user_move = _RPS_MAP.get(raw)
    if not user_move:
        raise ValueError("请出 石头、剪刀 或 布")
    rng = random.Random(seed_hint) if seed_hint else random.Random()
    bot_move = rng.choice(["rock", "scissors", "paper"])
    if user_move == bot_move:
        outcome = "draw"
    elif _RPS_BEATS[user_move] == bot_move:
        outcome = "win"
    else:
        outcome = "lose"
    return {
        "ok": True,
        "player": _RPS_ZH[user_move],
        "bot": _RPS_ZH[bot_move],
        "outcome": outcome,
    }


def draw_fortune_slip(topic: str = "今日运势", seed_hint: str = "") -> Dict[str, Any]:
    if seed_hint:
        idx = int(hashlib.sha256(f"{topic}:{seed_hint}".encode("utf-8")).hexdigest()[:8], 16) % len(_FORTUNES)
    else:
        idx = random.randrange(len(_FORTUNES))
    tier, verse = _FORTUNES[idx]
    return {"ok": True, "topic": str(topic or "今日运势"), "tier": tier, "verse": verse}


def random_pick(options: Union[Sequence[str], str], count: int = 1, seed_hint: str = "") -> Dict[str, Any]:
    if isinstance(options, str):
        items = [s.strip() for s in re.split(r"[,，、|\n]+", options) if s.strip()]
    else:
        items = [str(s).strip() for s in (options or ()) if str(s).strip()]
    if not items:
        raise ValueError("options 至少包含一个候选项")
    pick_n = max(1, min(int(count or 1), len(items)))
    rng = random.Random(seed_hint) if seed_hint else random.Random()
    chosen: List[str] = rng.sample(items, pick_n)
    return {"ok": True, "chosen": chosen, "first": chosen[0]}


def word_chain_step(word: str, theme: str = "") -> Dict[str, Any]:
    clean = str(word or "").strip()
    if not clean:
        raise ValueError("请提供接龙起始词")
    tail = clean[-1]
    match = next((w for w in _WORD_BANK if w.startswith(tail) and w != clean), None)
    if match is None:
        idx = sum(ord(c) for c in clean + theme) % len(_WORD_BANK)
        base = _WORD_BANK[idx]
        match = tail + base[-1]
    return {"ok": True, "input": clean, "tail": tail, "reply": match, "next_tail": match[-1]}


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id="mini_games",
        name="群聊无状态小游戏",
        kind="tool",
        version="0.3.0",
        description="提供剪刀石头布、抽签、随机抽选与成语/词语接龙等纯无状态小游戏工具。",
        default_enabled=True,
        config_fields=(),
    ),
    tools=(
        ToolRegistration(
            spec={
                "name": "play_rps",
                "description": "和住客玩一局剪刀石头布。",
                "parameters": {
                    "type": "object",
                    "properties": {"choice": {"type": "string", "description": "石头 / 剪刀 / 布"}},
                    "required": ["choice"],
                },
            },
            handler=play_rps,
            plugin_id="mini_games",
        ),
        ToolRegistration(
            spec={
                "name": "draw_fortune_slip",
                "description": "抽取今日合租屋趣味签文（纯无状态）。",
                "parameters": {
                    "type": "object",
                    "properties": {"topic": {"type": "string", "description": "占卜主题"}},
                },
            },
            handler=draw_fortune_slip,
            plugin_id="mini_games",
        ),
        ToolRegistration(
            spec={
                "name": "random_pick",
                "description": "从给定候选项列表中随机抽取一项或多项。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "options": {"type": "array", "items": {"type": "string"}},
                        "count": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["options"],
                },
            },
            handler=random_pick,
            plugin_id="mini_games",
        ),
        ToolRegistration(
            spec={
                "name": "word_chain_step",
                "description": "根据上一个词的尾字接出一个合租屋日常词语。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "word": {"type": "string"},
                        "theme": {"type": "string"},
                    },
                    "required": ["word"],
                },
            },
            handler=word_chain_step,
            plugin_id="mini_games",
        ),
    ),
)
