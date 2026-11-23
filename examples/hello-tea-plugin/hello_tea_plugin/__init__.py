"""Example third-party plugin for ai-house-kit (`examples/hello-tea-plugin`).

Demonstrates how an external Python package registers:
- declarative `ConfigField` settings (string + secret) rendered dynamically by `#/settings`,
- a `GET /status` HTTP route mounted under `/api/plugins/hello_tea/status`,
- a periodic `PluginTask` executed during post-step cycles, and
- a context-aware chat tool (`recommend_tea`) receiving `ToolContext`.

Third-party plugins never get the real daemon: `ctx.daemon` is a `PluginHostProxy`. This example uses only its
documented, narrow surface: `ctx.daemon.plugin_dir` (a private per-plugin directory where the brew counter is
persisted) and, in a real channel plugin, `resolve_speaker` / `converse` / `authorize_send` (see `PluginHostProxy`).
A channel authorizes a reply first, sends it, and calls `record_sent(delivery_id, key)` only once it really went
out; that is what fills the gate's hourly / daily caps (`authorize_send` alone never counts anything).
Secrets are not reachable through it: `env` is a whitelist (only variables this manifest declares via `env_var`,
plus harmless basics), and settings come via `plugin_config`. Private-room data is for channel plugins only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ai_house.plugin_api import (
    ConfigField,
    PluginDefinition,
    PluginManifest,
    PluginRequestContext,
    PluginRoute,
    PluginTask,
    PluginTaskContext,
    ToolContext,
    ToolRegistration,
)

_BREW_STATE: Dict[str, Any] = {"brew_count": 0, "last_brew_at": 0.0}


def reset_example_state() -> None:
    _BREW_STATE["brew_count"] = 0
    _BREW_STATE["last_brew_at"] = 0.0


def _brew_tick(ctx: PluginTaskContext) -> None:
    _BREW_STATE["brew_count"] = int(_BREW_STATE["brew_count"]) + 1
    _BREW_STATE["last_brew_at"] = float(ctx.now)
    # persist into this plugin's own directory (`PluginHostProxy.plugin_dir`); skipped for hosts without one
    plugin_dir = getattr(ctx.daemon, "plugin_dir", None)
    if plugin_dir is not None:
        (Path(plugin_dir) / "brew.json").write_text(json.dumps(_BREW_STATE), encoding="utf-8")


def _get_status(ctx: PluginRequestContext) -> Dict[str, Any]:
    tea = str(ctx.plugin_config.get("tea_of_day") or "桂花乌龙")
    return {
        "ok": True,
        "plugin_id": ctx.plugin_id,
        "tea_of_day": tea,
        "brew_count": int(_BREW_STATE["brew_count"]),
        "last_brew_at": float(_BREW_STATE["last_brew_at"]),
        "secret_configured": bool(ctx.plugin_config.get("secret_code")),
    }


def _recommend_tea(guest: str = "", ctx: ToolContext | None = None) -> Dict[str, Any]:
    tea = str(ctx.plugin_config.get("tea_of_day") or "桂花乌龙") if ctx is not None else "桂花乌龙"
    who = guest.strip() or (getattr(ctx.speaker, "name", "") if ctx and ctx.speaker else "客人")
    return {
        "ok": True,
        "tea": tea,
        "for_guest": who,
        "actor": ctx.actor if ctx is not None else "",
        "scope": ctx.scope if ctx is not None else "",
        "brew_count": int(_BREW_STATE["brew_count"]),
        "note": f"为{who}温了一壶「{tea}」，请慢用。",
    }


TOOL_SPEC: Dict[str, Any] = {
    "name": "recommend_tea",
    "description": "根据茶室配置与当前对话上下文，为房主或客人泡一杯今日推荐热茶。",
    "parameters": {
        "type": "object",
        "properties": {
            "guest": {"type": "string", "description": "饮茶人称呼（可选）"},
        },
    },
}


PLUGIN_DEF = PluginDefinition(
    manifest=PluginManifest(
        plugin_id="hello_tea",
        name="暮色茶室示例插件",
        kind="bundle",
        version="0.1.0",
        description="第三方插件包示例：含配置表单、/api/plugins/hello_tea/status 路由、周期煮茶任务与上下文工具。",
        default_enabled=False,
        config_fields=(
            ConfigField(
                key="tea_of_day",
                label="今日推荐茶饮",
                field_type="string",
                default="桂花乌龙",
                description="tea room daily tea blend shown in status API and chat tool.",
            ),
            ConfigField(
                key="secret_code",
                label="茶室暗语密钥",
                field_type="string",
                default="",
                secret=True,
                description="示例密钥字段，仅脱敏展示且不会被 /status 明文返回。",
            ),
        ),
    ),
    tools=(ToolRegistration(spec=TOOL_SPEC, handler=_recommend_tea, plugin_id="hello_tea", takes_context=True),),
    routes=(PluginRoute(method="GET", subpath="/status", handler=_get_status, tier="read"),),
    tasks=(PluginTask(task_id="brew_tick", interval_s=600.0, handler=_brew_tick, run_outside_lock=True),),
)
