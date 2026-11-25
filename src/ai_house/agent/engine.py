"""The seam between the house and an agent shell that runs a resident.

An engine is swapped by replacing exactly three things: make (or resume) a session's client, drop it, and run one
turn. Everything else (who may speak, what a turn's prompt says, what happens when the model forgets to send its
reply, when a session is rotated) lives in `runtime.AgentRuntime` and is the same for every engine.

Tools are engine-agnostic too: an engine hands each tool call to `AgentSession.call_tool`. The Claude Code engine
mounts them in-process; a CLI engine would expose the same call over a loopback MCP server.
"""
from __future__ import annotations

import asyncio
import queue
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

SPEAK_TOOL = "send_message"
QUIET_TOOL = "stay_quiet"
MAX_SENDS_PER_TURN = 3  # the default for both chat kinds (the private deployment's default too)
SENDS_GROUP_ENV = "AI_HOUSE_MAX_SENDS_PER_TURN_GROUP"
SENDS_PRIVATE_ENV = "AI_HOUSE_MAX_SENDS_PER_TURN_PRIVATE"
MAX_TOOL_CALLS_PER_TURN = 8
MAX_MESSAGE_CHARS = 400

SPEAK_TOOL_SPEC: Dict[str, Any] = {
    "name": SPEAK_TOOL,
    "description": "把一条消息发到当前对话里。你想说的话只有通过这个工具才会被看到。",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要发出的内容"},
            "end_turn": {
                "type": "boolean",
                "description": "这是本轮最后一条就填 true（默认）；后面还要接着再发一条才填 false",
            },
        },
        "required": ["text"],
    },
}

QUIET_TOOL_SPEC: Dict[str, Any] = {
    "name": QUIET_TOOL,
    "description": "这一轮不接话（群里的消息不是对你说的、或者没必要回时用）。调用后本轮结束。",
    "parameters": {
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "一句话说明为什么不接话（不会发出去）"}},
    },
}


def max_sends_per_turn(env: Any, where: str) -> int:
    """How many `send_message` calls one turn may make in this kind of chat: `AI_HOUSE_MAX_SENDS_PER_TURN_GROUP` for a
    group, `AI_HOUSE_MAX_SENDS_PER_TURN_PRIVATE` for a private chat. 0 = no per-turn limit. Unset, empty or not a
    non-negative whole number = the default (3), so an old configuration behaves as before."""
    key = SENDS_GROUP_ENV if where == "group" else SENDS_PRIVATE_ENV
    raw = str((env or {}).get(key) or "").strip()
    if not raw:
        return MAX_SENDS_PER_TURN
    try:
        val = int(raw)
    except ValueError:
        return MAX_SENDS_PER_TURN
    return val if 0 <= val <= 1000 else MAX_SENDS_PER_TURN


class EngineError(RuntimeError):
    """A turn could not be completed by the engine."""


class EngineUnavailable(EngineError):
    """The engine cannot run at all here (not installed, not implemented, not configured)."""


class ResumeFailed(EngineError):
    """The stored session is known to be gone (the shell said so); the caller retries once with a fresh session."""


class ResumeDoubt(EngineError):
    """A turn on a stored session failed in a way that may or may not be the session's fault (the connection failed,
    the process exited before saying anything). The session id is kept: one network hiccup must not wipe a long
    conversation. The runtime counts these in a row and only replaces the session after `RESUME_DOUBT_LIMIT` of them."""


@dataclass
class EngineUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    context_tokens: int = 0  # size of the conversation the last model call was given
    cost_usd: float = 0.0
    model: str = ""

    def add(self, other: "EngineUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.context_tokens = max(self.context_tokens, other.context_tokens)
        self.cost_usd += other.cost_usd
        self.model = other.model or self.model


@dataclass
class EngineResult:
    texts: List[str] = field(default_factory=list)  # plain text the model wrote without sending it
    session_id: str = ""
    usage: EngineUsage = field(default_factory=EngineUsage)
    stale_skipped: int = 0
    interrupted: bool = False


class Turn:
    """State of one turn. Created on the caller's thread; the engine's loop thread fills it in."""

    def __init__(self, *, allow_quiet: bool, tool_names: Sequence[str], max_sends: int = MAX_SENDS_PER_TURN) -> None:
        self.allow_quiet = bool(allow_quiet)
        self.max_sends = max(0, int(max_sends))  # 0 = no per-turn limit (the hourly budget still applies elsewhere)
        self.tool_names = set(tool_names)
        self.sent: List[str] = []
        self.quiet = False
        self.quiet_reason = ""
        self.end_requested = False
        self.tool_calls = 0
        self.denied: List[str] = []
        self.closed = False
        # host tools run on the caller's thread: (name, args, loop, future)
        self.calls: "queue.Queue[tuple[str, Dict[str, Any], asyncio.AbstractEventLoop, asyncio.Future]]" = queue.Queue()

    @property
    def spoke(self) -> bool:
        return bool(self.sent) or self.quiet

    def _speak(self, args: Dict[str, Any]) -> Dict[str, Any]:
        text = str(args.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "empty_text", "note": "内容是空的，没有发出"}
        if self.quiet:
            return {"ok": False, "error": "already_quiet", "note": "本轮已经选择不接话"}
        if self.max_sends and len(self.sent) >= self.max_sends:
            self.end_requested = True
            return {"ok": False, "error": "too_many_messages", "note": "这一轮已经发得够多了，到此为止"}
        self.sent.append(text[:MAX_MESSAGE_CHARS])
        if args.get("end_turn", True) is not False:
            self.end_requested = True
        return {"ok": True, "note": "已发出"}

    def _stay_quiet(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not self.allow_quiet:
            return {"ok": False, "error": "not_allowed_here", "note": "私聊里对方在等你回话，请用 send_message 回复"}
        if self.sent:
            return {"ok": False, "error": "already_spoke", "note": "本轮已经发过消息了"}
        self.quiet = True
        self.quiet_reason = str(args.get("reason") or "").strip()[:120]
        self.end_requested = True
        return {"ok": True, "note": "好，本轮不接话"}

    async def call_tool(self, name: str, args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Run one tool call for this turn. Never raises: a failure is a result the model can read."""
        args = dict(args) if isinstance(args, dict) else {}
        if self.closed:
            return {"error": "turn_closed", "note": "这一轮已经结束"}
        if name == SPEAK_TOOL:
            return self._speak(args)
        if name == QUIET_TOOL:
            return self._stay_quiet(args)
        if name not in self.tool_names:
            return {"error": f"unknown_tool:{name}"}
        self.tool_calls += 1
        if self.tool_calls > MAX_TOOL_CALLS_PER_TURN:
            return {"error": "too_many_tool_calls", "note": "这一轮查得够多了，请直接回复"}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.calls.put((name, args, loop, fut))
        return await fut


@dataclass
class AgentSession:
    """One resident in one conversation. `session_id` survives restarts; `client` does not."""

    key: str
    actor: str
    channel: str
    where: str
    engine: str = ""
    session_id: str = ""
    system: str = ""
    tool_specs: List[Dict[str, Any]] = field(default_factory=list)
    workdir: str = ""
    client: Any = None
    client_fingerprint: str = ""
    turn: Optional[Turn] = None
    last_used: float = field(default_factory=time.monotonic)
    turns: int = 0
    context_tokens: int = 0
    unsent_streak: int = 0
    resume_failures: int = 0  # turns in a row that failed with a `ResumeDoubt`; persisted
    forgotten: bool = False  # set when the world is reset: a client that finishes connecting now must be thrown away
    pending_reminder: bool = False
    blocked_note: bool = False
    carry_note: str = ""
    lock: Any = None  # asyncio.Lock, created on the loop thread
    audit: Optional[Callable[[str, Dict[str, Any]], None]] = None

    async def call_tool(self, name: str, args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        turn = self.turn
        if turn is None:
            return {"error": "no_active_turn", "note": "现在没有进行中的对话轮次"}
        return await turn.call_tool(name, args)

    def tool_allowed(self, name: str) -> bool:
        turn = self.turn
        if turn is None:
            return False
        if name == SPEAK_TOOL:
            return True
        if name == QUIET_TOOL:
            return turn.allow_quiet
        return name in turn.tool_names


class AgentEngine:
    """Base class: the three operations an agent shell has to provide."""

    name = ""
    label = ""

    def unavailable_reason(self) -> str:
        """Empty when the engine can run here; otherwise one Chinese sentence saying what is missing."""
        return ""

    def default_model_label(self) -> str:
        return self.label or self.name

    def secrets(self) -> List[str]:
        return []

    def conversation_paths(self) -> List[Path]:
        """Where this shell keeps what was said in its conversations, so a world reset can delete it."""
        return []

    async def ensure_client(self, sess: AgentSession) -> None:
        """Make the shell ready. A client that finishes connecting after `sess.forgotten` was set must be
        disconnected and the call must fail."""
        raise NotImplementedError

    async def drop_client(self, sess: AgentSession) -> None:
        raise NotImplementedError

    async def run_turn(self, sess: AgentSession, prompt: str) -> EngineResult:
        raise NotImplementedError
