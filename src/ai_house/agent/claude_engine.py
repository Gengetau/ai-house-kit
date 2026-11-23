"""Claude Code as the agent shell, through the Claude Agent SDK (optional dependency: `ai-house-kit[agent]`).

One session is one Claude Code subprocess with a full custom system prompt, no built-in tools, and the house tools
mounted as an in-process MCP server. Every tool call first passes a policy callback; the shell reads no Claude
configuration from this machine (its config directory is its own) and makes no update or telemetry calls.

Messages are recognised by class name rather than `isinstance`, so the turn loop can be tested without the SDK.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..llm import mask_secrets
from .engine import (
    AgentEngine,
    AgentSession,
    EngineError,
    EngineResult,
    EngineUnavailable,
    EngineUsage,
    ResumeDoubt,
    ResumeFailed,
)

SERVER = "house"
TOOL_PREFIX = f"mcp__{SERVER}__"
MAX_STALE_RESULTS = 3
STALE_SLACK_S = 2.0
DEFAULT_MAX_TURNS = 12
# An alias, which Claude Code resolves to the current model of that family. Left to itself the shell picks its own
# default, which may be its largest model: too costly a surprise for everyday chat.
DEFAULT_MODEL = "sonnet"
DEFAULT_READ_TIMEOUT_S = 600.0
CONNECT_TIMEOUT_S = 60.0
DISCONNECT_TIMEOUT_S = 15.0
INTERRUPT_BEFORE_DROP_S = 2.0

_CRED_KEYS = (
    "AI_HOUSE_AGENT_API_KEY",
    "AI_HOUSE_AGENT_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
_CLOUD_FLAGS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
_RESUME_HINTS = ("no conversation found", "session not found", "could not resume", "invalid session")


def _kind(obj: Any) -> str:
    return type(obj).__name__


def _log_text(text: Any, limit: int = 60) -> str:
    """Text the model chose (a tool name) as it may appear in one log line: no line breaks or control characters."""
    return "".join(ch for ch in str(text) if ch.isprintable())[:limit]


def _says_session_is_gone(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in _RESUME_HINTS)


def _usage_of(raw: Any) -> Tuple[int, int]:
    """(tokens the model was given, tokens it wrote) from an Anthropic usage mapping."""
    if not isinstance(raw, Mapping):
        return 0, 0
    given = sum(int(raw.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return given, int(raw.get("output_tokens") or 0)


def decide_tool(sess: AgentSession, tool_name: str) -> Tuple[bool, str]:
    """The policy every tool call passes first: only this session's house tools, nothing built in."""
    if not tool_name.startswith(TOOL_PREFIX):
        return False, "这个工具在这里不能用"
    if not sess.tool_allowed(tool_name[len(TOOL_PREFIX):]):
        return False, "这个工具现在不能用"
    return True, ""


class ClaudeCodeEngine(AgentEngine):
    name = "claude"
    label = "Claude Code"

    def __init__(
        self,
        env: Mapping[str, str],
        state_dir: str | Path,
        *,
        client_factory: Optional[Callable[[Dict[str, Any], AgentSession], Any]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    ) -> None:
        self.env = dict(env)
        self.state_dir = Path(state_dir)
        self._factory = client_factory
        self._mono = monotonic
        self.read_timeout_s = float(read_timeout_s)
        self.model = str(self.env.get("AI_HOUSE_AGENT_MODEL") or "").strip() or DEFAULT_MODEL
        self.fallback_model = str(self.env.get("AI_HOUSE_AGENT_FALLBACK_MODEL") or "").strip()
        try:
            self.max_turns = max(2, min(int(str(self.env.get("AI_HOUSE_AGENT_MAX_TURNS") or DEFAULT_MAX_TURNS)), 50))
        except ValueError:
            self.max_turns = DEFAULT_MAX_TURNS

    # ------------------------------------------------------------------ availability
    def _sdk(self) -> Any:
        try:
            import claude_agent_sdk  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailable("没有安装 Claude Agent SDK，请先执行 pip install 'ai-house-kit[agent]'") from exc
        return claude_agent_sdk

    def unavailable_reason(self) -> str:
        if self._factory is None:
            try:
                self._sdk()
            except EngineUnavailable as exc:
                return str(exc)
        if not any(str(self.env.get(k) or "").strip() for k in (*_CRED_KEYS, *_CLOUD_FLAGS)):
            return "没有配置 Claude 的密钥（AI_HOUSE_AGENT_API_KEY，或 ANTHROPIC_API_KEY）"
        return ""

    def default_model_label(self) -> str:
        return f"Claude Code（{self.model}）"

    def conversation_paths(self) -> List[Path]:
        # the shell's transcripts of what was said (its other files in the config directory are settings only)
        return [self.state_dir / "claude-config" / "projects"]

    def secrets(self) -> List[str]:
        return [str(self.env.get(k) or "").strip() for k in _CRED_KEYS if str(self.env.get(k) or "").strip()]

    # ------------------------------------------------------------------ options
    def child_env(self) -> Dict[str, str]:
        """What the shell's process gets on top of the house's own environment."""
        out = {
            "CLAUDE_CONFIG_DIR": str(self.state_dir / "claude-config"),
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        for src, dst in (
            ("AI_HOUSE_AGENT_API_KEY", "ANTHROPIC_API_KEY"),
            ("AI_HOUSE_AGENT_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"),
            ("AI_HOUSE_AGENT_BASE_URL", "ANTHROPIC_BASE_URL"),
        ):
            val = str(self.env.get(src) or "").strip()
            if val:
                out[dst] = val
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", *_CLOUD_FLAGS):
            val = str(self.env.get(key) or "").strip()
            if val and key not in out:
                out[key] = val
        return out

    def option_kwargs(self, sess: AgentSession) -> Dict[str, Any]:
        """Plain-data options for one session. The SDK objects (MCP server, callbacks) are added by the factory."""
        kwargs: Dict[str, Any] = {
            "system_prompt": sess.system,
            "tools": [],  # no built-in tools: no files, no shell, no web
            "strict_mcp_config": True,
            "setting_sources": [],
            "permission_mode": "default",
            "max_turns": self.max_turns,
            "cwd": sess.workdir,
            "env": self.child_env(),
        }
        if sess.session_id:
            kwargs["resume"] = sess.session_id
        if self.model:
            kwargs["model"] = self.model
        if self.fallback_model and self.fallback_model != self.model:
            kwargs["fallback_model"] = self.fallback_model
        cli = str(self.env.get("AI_HOUSE_AGENT_CLI_PATH") or "").strip()
        if cli:
            kwargs["cli_path"] = cli
        return kwargs

    def fingerprint(self, sess: AgentSession) -> str:
        kw = self.option_kwargs(sess)
        kw.pop("resume", None)
        doc = [kw, [[s.get("name"), s.get("description"), s.get("parameters")] for s in sess.tool_specs]]
        return hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    async def permit(self, sess: AgentSession, tool_name: str) -> Tuple[bool, str]:
        ok, why = decide_tool(sess, tool_name)
        if not ok:
            turn = sess.turn
            if turn is not None:
                turn.denied.append(tool_name)
            if sess.audit is not None:
                sess.audit("tool_denied", {"actor": sess.actor, "tool": _log_text(tool_name)})
        return ok, why

    def _sdk_client(self, kwargs: Dict[str, Any], sess: AgentSession) -> Any:
        sdk = self._sdk()

        def _make(spec: Dict[str, Any]) -> Any:
            name = str(spec.get("name") or "")
            schema = spec.get("parameters") or {"type": "object", "properties": {}}

            @sdk.tool(name, str(spec.get("description") or name), schema)
            async def _handler(args: Any, _name: str = name) -> Dict[str, Any]:
                out = await sess.call_tool(_name, args if isinstance(args, dict) else {})
                return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]}

            return _handler

        server = sdk.create_sdk_mcp_server(name=SERVER, version="1.0.0", tools=[_make(s) for s in sess.tool_specs])

        async def _can_use(tool_name: str, _tool_input: Dict[str, Any], _ctx: Any) -> Any:
            ok, why = await self.permit(sess, tool_name)
            if ok:
                return sdk.PermissionResultAllow()
            return sdk.PermissionResultDeny(message=why)

        async def _pre(input_data: Any, _tool_use_id: Any, _ctx: Any) -> Dict[str, Any]:
            if sess.audit is not None:
                name = str((input_data or {}).get("tool_name") or "") if isinstance(input_data, Mapping) else ""
                sess.audit("tool_use", {"actor": sess.actor, "tool": _log_text(name)})
            return {}

        async def _post(_input_data: Any, _tool_use_id: Any, _ctx: Any) -> Dict[str, Any]:
            turn = sess.turn
            if turn is not None and turn.end_requested:
                # she has said what she had to say: end the turn here, before another model call is made
                return {"continue_": False, "stopReason": "turn_complete"}
            return {}

        Path(kwargs["cwd"]).mkdir(parents=True, exist_ok=True)
        Path(kwargs["env"]["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
        options = sdk.ClaudeAgentOptions(
            mcp_servers={SERVER: server},
            can_use_tool=_can_use,
            hooks={"PreToolUse": [sdk.HookMatcher(hooks=[_pre])], "PostToolUse": [sdk.HookMatcher(hooks=[_post])]},
            **kwargs,
        )
        return sdk.ClaudeSDKClient(options)

    # ------------------------------------------------------------------ the three operations
    async def ensure_client(self, sess: AgentSession) -> None:
        fp = self.fingerprint(sess)
        if sess.client is not None and sess.client_fingerprint == fp:
            return
        if sess.client is not None:
            # the rules or the tools changed: same session, new client
            await self.drop_client(sess)
        kwargs = self.option_kwargs(sess)
        factory = self._factory or self._sdk_client
        client = factory(kwargs, sess)
        try:
            await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT_S)
        except asyncio.CancelledError:
            # given up on while connecting: nothing else holds this client, so close it here
            try:
                await asyncio.shield(self._close(client))
            except BaseException:  # noqa: BLE001 - cancelled again: the shielded close still finishes
                pass
            raise
        except Exception as exc:  # noqa: BLE001
            await self._close(client)
            if _kind(exc) == "CLINotFoundError":
                raise EngineUnavailable("找不到 Claude Code 可执行文件，请重新安装 claude-agent-sdk") from exc
            text = self._describe(exc)
            if sess.session_id:
                if _says_session_is_gone(text):
                    raise ResumeFailed(text) from exc
                # it may be the network, not the session: keep the id and let the runtime count it
                raise ResumeDoubt(text) from exc
            raise EngineError(text) from exc
        if sess.forgotten:
            # the world was reset while this was connecting; the session it was made for no longer exists
            await self._close(client)
            raise EngineError("这个会话在启动过程中被重置了")
        sess.client = client
        sess.client_fingerprint = fp
        setattr(client, "_house_turns", 0)

    @staticmethod
    async def _close(client: Any) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - a client that will not close cleanly is abandoned
            pass

    async def drop_client(self, sess: AgentSession) -> None:
        client, sess.client, sess.client_fingerprint = sess.client, None, ""
        if client is None:
            return
        if getattr(client, "_house_busy", False):
            # Dropped in the middle of a turn (the house stopped waiting). A shell that is still working takes
            # seconds to notice that it is being closed, and the conversation's lock is held meanwhile; told to
            # stop first, it closes at once.
            try:
                await asyncio.wait_for(client.interrupt(), INTERRUPT_BEFORE_DROP_S)
            except Exception:  # noqa: BLE001 - closing it is what matters
                pass
        try:
            await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - a client that will not close cleanly is abandoned
            pass

    def _describe(self, exc: Any) -> str:
        text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        for sec in self.secrets():
            text = mask_secrets(text, api_key=sec)
        return mask_secrets(text)[:300]

    async def run_turn(self, sess: AgentSession, prompt: str) -> EngineResult:
        # the clock starts before the shell is made ready: its reported run time may include starting up or resuming
        t0 = self._mono()
        await self.ensure_client(sess)
        client = sess.client
        turn = sess.turn
        resumed_first_turn = bool(sess.session_id) and getattr(client, "_house_turns", 0) == 0
        out = EngineResult()
        per_call: Dict[str, Tuple[int, int]] = {}
        pending_tools: set[str] = set()
        api_error = ""
        seen_any = False
        seq = 0

        def _resume_or_error(exc: Any, *, silent_exit: bool = False) -> EngineError:
            """`silent_exit`: the shell went away without telling us why, so on a resumed session's first turn it
            may be the session's fault or may not. An error the shell reported itself is not that."""
            text = self._describe(exc)
            if sess.session_id:
                if _says_session_is_gone(text):
                    return ResumeFailed(text)
                if silent_exit and resumed_first_turn and not seen_any:
                    return ResumeDoubt(text)
            return EngineError(text)

        try:
            setattr(client, "_house_busy", True)  # until this turn's result has been read
            await client.query(prompt)
            stream = client.receive_messages().__aiter__()
            while True:
                try:
                    msg = await asyncio.wait_for(stream.__anext__(), self.read_timeout_s)
                except StopAsyncIteration:
                    raise _resume_or_error("Claude Code 进程在这一轮结束前退出了", silent_exit=True) from None
                except asyncio.TimeoutError:
                    raise EngineError(f"等了 {self.read_timeout_s:g} 秒没有等到这一轮的结果") from None
                kind = _kind(msg)
                if kind == "AssistantMessage":
                    seen_any = True
                    err = getattr(msg, "error", None)
                    if err:
                        api_error = str(err)
                    out.usage.model = str(getattr(msg, "model", "") or out.usage.model)
                    seq += 1
                    # one API message can arrive as several AssistantMessages with the same id (and the same usage):
                    # those count once. Without an id, every message is its own call.
                    mid = str(getattr(msg, "message_id", "") or "") or f"#no-id-{seq}"
                    given, wrote = _usage_of(getattr(msg, "usage", None))
                    if given or wrote:
                        per_call[mid] = (given, wrote)
                        out.usage.context_tokens = max(out.usage.context_tokens, given)
                    for block in getattr(msg, "content", None) or ():
                        bkind = _kind(block)
                        if bkind == "TextBlock" and not err:
                            text = str(getattr(block, "text", "") or "").strip()
                            if text:
                                out.texts.append(text)
                        elif bkind == "ToolUseBlock":
                            pending_tools.add(str(getattr(block, "id", "") or ""))
                elif kind == "UserMessage":
                    seen_any = True
                    content = getattr(msg, "content", None)
                    for block in content if isinstance(content, (list, tuple)) else ():
                        if _kind(block) == "ToolResultBlock":
                            pending_tools.discard(str(getattr(block, "tool_use_id", "") or ""))
                    if turn is not None and turn.end_requested and not pending_tools and not out.interrupted:
                        # She has said what she had to say. The PostToolUse hook normally ends the turn before
                        # the shell asks the model again; this interrupt is the fallback for a shell where it
                        # did not (and the only mechanism for a client that has no hooks).
                        out.interrupted = True
                        try:
                            await client.interrupt()
                        except Exception:  # noqa: BLE001 - the turn then simply runs to its own end
                            pass
                elif kind == "ResultMessage":
                    dur_s = float(getattr(msg, "duration_ms", 0) or 0) / 1000.0
                    if dur_s > (self._mono() - t0) + STALE_SLACK_S and out.stale_skipped < MAX_STALE_RESULTS:
                        # it reports running longer than this turn has existed: a late result of an earlier turn
                        out.stale_skipped += 1
                        continue
                    seen_any = True
                    out.session_id = str(getattr(msg, "session_id", "") or "")
                    given, wrote = _usage_of(getattr(msg, "usage", None))
                    if not (given or wrote):
                        given = sum(v[0] for v in per_call.values())
                        wrote = sum(v[1] for v in per_call.values())
                    out.usage.prompt_tokens, out.usage.completion_tokens = given, wrote
                    out.usage.cost_usd = float(getattr(msg, "total_cost_usd", 0) or 0)
                    failed = bool(getattr(msg, "is_error", False)) or bool(api_error)
                    if failed and not out.interrupted and not (turn is not None and turn.spoke):
                        detail = api_error or str(getattr(msg, "result", "") or getattr(msg, "subtype", "") or "error")
                        raise _resume_or_error(f"Claude Code 报告这一轮失败：{detail}")
                    if failed:
                        out.texts = []  # never mistake an error banner for something she wrote
                    break
        except EngineError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _resume_or_error(exc, silent_exit=True) from exc
        setattr(client, "_house_busy", False)
        setattr(client, "_house_turns", getattr(client, "_house_turns", 0) + 1)
        return out
