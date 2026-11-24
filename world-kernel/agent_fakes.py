"""A scripted stand-in for the Claude Agent SDK client, for tests that must run without the SDK or a model.

The message classes carry the SDK's own class names because the engine recognises messages by name. A script is a
list of turns; each turn is a list of steps the "model" takes after one `query()`:

    ("text", "...")              plain text, not sent
    ("tool", name, args)         call a house tool (through the policy, then the handler)
    ("raw_tool", full_name, {})  call a tool by its full name, e.g. a built-in one
    ("stale_result",)            a result that claims to have run for an hour
    ("result_ms", ms)            a result that claims to have run for `ms` milliseconds (session id "late-session")
    ("error", "...")             the turn ends with an error result
    ("die",)                     the process goes away
    ("hang",)                    nothing more ever arrives

What happens after a tool step is not left to scheduling. In the real shell a PostToolUse hook ends the turn once
`Turn.end_requested` is set, so by default (`hub.honor_stop = True`) the fake does the same: it plays no further
steps and returns a normal result. With `hub.honor_stop = False` the hook "did not take effect": the fake then waits
(an Event, bounded by `hub.interrupt_wait_s`) for the engine's `interrupt()` and ends the turn the way an
interrupted shell does; if no interrupt comes it carries on with the script, as a model that was never stopped would.
`client.stopped_by` records which of "hook", "interrupt", "none" ended the turn early.
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: bool = False


@dataclass
class AssistantMessage:
    content: List[Any]
    model: str = "claude-test"
    error: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    message_id: str = ""


@dataclass
class UserMessage:
    content: Any


@dataclass
class ResultMessage:
    subtype: str = "success"
    duration_ms: int = 5
    is_error: bool = False
    session_id: str = ""
    total_cost_usd: float = 0.001
    usage: Optional[Dict[str, Any]] = None
    result: str = ""


_END = object()


class FakeClient:
    def __init__(self, hub: "FakeHub", kwargs: Dict[str, Any], sess: Any) -> None:
        self.hub = hub
        self.kwargs = dict(kwargs)
        self.sess = sess
        self.tool_names = [s["name"] for s in sess.tool_specs]
        self.connected = False
        self.disconnected = False
        self.interrupts = 0
        self.prompts: List[str] = []
        self.executed: List[Any] = []
        self._q: "asyncio.Queue[Any]" = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._interrupted = False
        self._interrupt_evt = asyncio.Event()
        self.stopped_by = ""
        self.session_id = kwargs.get("resume") or hub.new_session_id()

    async def connect(self) -> None:
        self.hub.connects_started += 1
        self.hub.connecting.set()
        if self.hub.on_connect is not None:
            self.hub.on_connect()
        gate = self.hub.connect_gate
        if gate is not None:
            # a connect that is still in progress (a slow process start): the test opens the gate
            while not gate.is_set():
                await asyncio.sleep(0.005)
        if self.hub.connect_errors:
            raise self.hub.connect_errors.pop(0)
        if self.kwargs.get("resume") and self.kwargs["resume"] in self.hub.unresumable:
            raise RuntimeError("No conversation found with session ID")
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True
        if self._task is not None:
            self._task.cancel()

    async def interrupt(self) -> None:
        self.interrupts += 1
        self._interrupted = True
        self._interrupt_evt.set()

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)
        self.hub.prompts.append(prompt)
        self._interrupted = False
        self._interrupt_evt = asyncio.Event()
        steps = self.hub.script.pop(0) if self.hub.script else [("text", "（脚本用完了）")]
        self._task = asyncio.get_running_loop().create_task(self._play(steps))

    async def _play(self, steps: List[Any]) -> None:
        n = 0
        ctx = self.hub.context_tokens
        for step in steps:
            if self._interrupted:
                await self._q.put(ResultMessage(subtype="error_during_execution", is_error=True, session_id=self.session_id))
                return
            n += 1
            kind = step[0]
            usage = {"input_tokens": 10, "cache_read_input_tokens": ctx, "output_tokens": 7}
            self.executed.append(step)
            if kind == "text":
                await self._q.put(AssistantMessage([TextBlock(step[1])], usage=usage, message_id=f"m{n}"))
            elif kind in ("tool", "raw_tool"):
                full = step[1] if kind == "raw_tool" else f"mcp__house__{step[1]}"
                tid = f"tu{n}"
                await self._q.put(AssistantMessage([ToolUseBlock(tid, full, step[2])], usage=usage, message_id=f"m{n}"))
                ok, why = await self.hub.engine.permit(self.sess, full)
                if ok:
                    out = await self.sess.call_tool(full.split("__", 2)[2], step[2])
                    self.hub.tool_results.append((full, out))
                    block = ToolResultBlock(tid, json.dumps(out, ensure_ascii=False))
                else:
                    self.hub.tool_results.append((full, {"denied": why}))
                    block = ToolResultBlock(tid, why, is_error=True)
                await self._q.put(UserMessage([block]))
                turn = self.sess.turn
                if turn is not None and turn.end_requested:
                    if self.hub.honor_stop:
                        # the PostToolUse hook: the turn ends here, before another model call
                        self.stopped_by = "hook"
                        await self._q.put(self._result(n, ctx))
                        return
                    try:
                        await asyncio.wait_for(self._interrupt_evt.wait(), self.hub.interrupt_wait_s)
                    except asyncio.TimeoutError:
                        self.stopped_by = "none"  # nothing stopped the model: it carries on
                    else:
                        self.stopped_by = "interrupt"
                        await self._q.put(
                            ResultMessage(subtype="error_during_execution", is_error=True, session_id=self.session_id)
                        )
                        return
            elif kind == "result_ms":
                await self._q.put(ResultMessage(duration_ms=int(step[1]), session_id="late-session"))
            elif kind == "stale_result":
                await self._q.put(ResultMessage(duration_ms=3_600_000, session_id="stale-session"))
            elif kind == "error":
                await self._q.put(AssistantMessage([TextBlock(step[1])], error="authentication_failed"))
                await self._q.put(ResultMessage(subtype="success", is_error=True, session_id=self.session_id, result=step[1]))
                return
            elif kind == "die":
                await self._q.put(_END)
                return
            elif kind == "hang":
                await asyncio.sleep(3600)
        if self._interrupted:
            await self._q.put(ResultMessage(subtype="error_during_execution", is_error=True, session_id=self.session_id))
            return
        await self._q.put(self._result(n, ctx))

    def _result(self, n: int, ctx: int) -> ResultMessage:
        return ResultMessage(
            session_id=self.session_id,
            usage={"input_tokens": 10 * n, "cache_read_input_tokens": ctx * n, "output_tokens": 7 * n},
        )

    async def receive_messages(self):
        while True:
            msg = await self._q.get()
            if msg is _END:
                return
            yield msg


@dataclass
class FakeHub:
    """Shared by every client one engine creates: the script, and a record of what happened."""

    script: List[List[Any]] = field(default_factory=list)
    clients: List[FakeClient] = field(default_factory=list)
    prompts: List[str] = field(default_factory=list)
    tool_results: List[Any] = field(default_factory=list)
    connect_errors: List[BaseException] = field(default_factory=list)
    unresumable: set = field(default_factory=set)
    context_tokens: int = 500
    engine: Any = None
    honor_stop: bool = True  # the PostToolUse hook ends the turn once she has spoken (see the module docstring)
    interrupt_wait_s: float = 3.0
    connect_gate: Optional[threading.Event] = None  # while set up and not set, `connect()` stays in progress
    connecting: threading.Event = field(default_factory=threading.Event)
    connects_started: int = 0
    on_connect: Optional[Any] = None  # called (no arguments) at the start of every connect()
    _n: int = 0

    def new_session_id(self) -> str:
        self._n += 1
        return f"sess-{self._n}"

    def factory(self, kwargs: Dict[str, Any], sess: Any) -> FakeClient:
        client = FakeClient(self, kwargs, sess)
        self.clients.append(client)
        return client

    def say(self, text: str, **extra: Any) -> List[Any]:
        return [("tool", "send_message", {"text": text, **extra})]
