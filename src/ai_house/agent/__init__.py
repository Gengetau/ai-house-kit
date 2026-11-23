"""Agent-backed residents: a resident's chat turn is run by an agent shell instead of one direct model call.

`AI_HOUSE_BRAIN` picks the default (`direct`, `claude`, `codex`, `agy`); `AI_HOUSE_ACTOR_BRAIN_<ACTOR>` overrides
it for one resident. `direct` is the built-in model call with its small tool loop and needs nothing installed.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Mapping

from .engine import (
    AgentEngine,
    AgentSession,
    EngineError,
    EngineResult,
    EngineUnavailable,
    EngineUsage,
    ResumeDoubt,
    ResumeFailed,
    Turn,
)
from .runtime import AgentRuntime, TurnRequest, TurnResult

DIRECT = "direct"
_ALIASES = {
    "": DIRECT,
    "api": DIRECT,
    "direct": DIRECT,
    "claude": "claude",
    "claude-code": "claude",
    "claude_code": "claude",
    "codex": "codex",
    "agy": "agy",
    "antigravity": "agy",
}

__all__ = [
    "AgentEngine",
    "AgentRuntime",
    "AgentSession",
    "DIRECT",
    "EngineError",
    "EngineResult",
    "EngineUnavailable",
    "EngineUsage",
    "ResumeDoubt",
    "ResumeFailed",
    "Turn",
    "TurnRequest",
    "TurnResult",
    "brain_for",
    "build_engines",
]


def _actor_key(actor: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", str(actor or "").upper())  # same rule as AI_HOUSE_ACTOR_MODEL_<ACTOR>


def brain_for(env: Mapping[str, str], actor: str = "") -> str:
    """Which brain runs this resident's chat turns. An unknown name is returned as written, so it fails visibly."""
    raw = ""
    if actor:
        raw = str(env.get(f"AI_HOUSE_ACTOR_BRAIN_{_actor_key(actor)}") or "").strip()
    if not raw:
        raw = str(env.get("AI_HOUSE_BRAIN") or "").strip()
    low = raw.lower()
    return _ALIASES.get(low, low)


def build_engines(env: Mapping[str, str], state_dir: str | Path) -> Dict[str, AgentEngine]:
    from .claude_engine import ClaudeCodeEngine
    from .reserved import AntigravityEngine, CodexEngine

    return {
        "claude": ClaudeCodeEngine(env, state_dir),
        "codex": CodexEngine(),
        "agy": AntigravityEngine(),
    }
