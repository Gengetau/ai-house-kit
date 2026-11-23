"""Engines whose place is reserved but which are not implemented yet.

Both are command-line agent shells. To add one, subclass `AgentEngine` and provide the three operations:

- `ensure_client(sess)`: make the shell ready for this session (resume `sess.session_id` when it is set). The
  resident's rules come from `sess.system`; a shell that reads its instructions from a file gets them written into
  the session's own working directory (`sess.workdir`).
- `run_turn(sess, prompt)`: give the shell one prompt and wait for the turn to end; return an `EngineResult` with
  any plain text the model wrote, the session id to resume next time, and usage.
- `drop_client(sess)`: stop whatever is running for the session. The session id is kept by the runtime.

Tools: the shell must reach the tools in `sess.tool_specs` and nothing else. Each call goes to
`await sess.call_tool(name, args)` after `sess.tool_allowed(name)` says yes. A command-line shell cannot be handed
Python callables, so its engine serves these over an MCP endpoint bound to the loopback address, with a token of its
own per session in the URL, and locks the shell down so that this endpoint is the only thing it may use (no files,
no shell commands, no arbitrary URLs).

Everything else (session persistence, the reminder when a resident forgets to send her reply, rotation, idle
clients, tool calls running on the house's thread) is done by `AgentRuntime` and needs no work per engine.
"""
from __future__ import annotations

from .engine import AgentEngine


class _Reserved(AgentEngine):
    def unavailable_reason(self) -> str:
        return f"{self.label} 引擎的位置已预留，但还没有实现；目前可用的住客大脑是 claude（Claude Code）和 direct（直连模型）"


class CodexEngine(_Reserved):
    name = "codex"
    label = "Codex"


class AntigravityEngine(_Reserved):
    """Antigravity (`agy`). The suggested route for running residents on Gemini models."""

    name = "agy"
    label = "Antigravity"
