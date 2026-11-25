"""Runs agent-backed residents: sessions, turns, and what to do when a turn goes wrong.

The house is threaded and synchronous; agent shells are async. `AgentRuntime` owns one event-loop thread and
exposes a blocking `run_turn`. While a turn is running the caller's thread serves the resident's tool calls, so
house tools run exactly where they run for the direct model path (same thread, same deadlines, same SQLite use).

What is deliberately the same for every engine (and tested here rather than per engine):

- one lock per conversation, a cap on turns running at once, a cap on live clients, idle clients are dropped
- the session id is persisted and resumed; a session the shell says is gone is replaced by a fresh one at once, one
  that merely failed to start (a connection error, a process that exited) is kept and only replaced after
  `RESUME_DOUBT_LIMIT` such turns in a row
- a resident who writes text without sending it is reminded once in the same turn, again at the start of the
  next turn, and gets a fresh session after three such turns in a row; in a private chat the text is sent for her
- a session whose conversation grew past the cap is rotated, with a note carried into the new one
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..llm import LLMDeadlineError, mask_secrets, remaining_budget
from . import prompts
from .engine import (
    MAX_MESSAGE_CHARS,
    MAX_SENDS_PER_TURN,
    QUIET_TOOL_SPEC,
    SPEAK_TOOL_SPEC,
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

UNSENT_STREAK_RESET = 3
RESUME_DOUBT_LIMIT = 2  # turns in a row that failed to start on a stored session before it is replaced
DEFAULT_TURN_BUDGET_S = 120.0
DEFAULT_MAX_PARALLEL = 2
DEFAULT_MAX_CLIENTS = 6
DEFAULT_IDLE_DROP_S = 1800.0
DEFAULT_SESSION_MAX_TOKENS = 60000


def _env_float(env: Mapping[str, str], key: str, default: float, lo: float, hi: float) -> float:
    try:
        val = float(str(env.get(key) or "").strip())
    except ValueError:
        return default
    if not val > 0:
        return default
    return max(lo, min(val, hi))


@dataclass
class TurnRequest:
    engine: str
    actor: str
    channel: str
    where: str
    system: str
    state_lines: Sequence[str]
    said_line: str
    memory_context: str = ""
    # (line, is_self) oldest first, without the message being answered
    history: Sequence[Tuple[str, bool]] = ()
    attachment_block: str = ""
    tool_specs: Sequence[Dict[str, Any]] = ()
    tool_runner: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None
    allow_quiet: bool = False
    max_sends: int = MAX_SENDS_PER_TURN  # `send_message` calls allowed in this turn; 0 = no per-turn limit


@dataclass
class TurnResult:
    messages: List[str] = field(default_factory=list)
    quiet: bool = False
    quiet_reason: str = ""
    engine: str = ""
    model: str = ""
    usage: EngineUsage = field(default_factory=EngineUsage)
    latency_ms: int = 0
    fresh_session: bool = False
    nudged: bool = False
    autosent: bool = False
    rotated: str = ""  # "", "too_long" or "unsent"
    partial: bool = False
    tool_calls: int = 0


class AgentRuntime:
    def __init__(
        self,
        state_dir: str | Path,
        *,
        env: Optional[Mapping[str, str]] = None,
        engines: Optional[Mapping[str, AgentEngine]] = None,
        log: Optional[Callable[[str], None]] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state_dir = Path(state_dir)
        self._log = log or (lambda _msg: None)
        self._mono = monotonic
        self._mu = threading.RLock()
        self._sessions: Dict[str, AgentSession] = {}
        self._stored: Dict[str, Dict[str, Any]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._slots: Optional[asyncio.Semaphore] = None
        self._reaper: Optional[asyncio.Task] = None
        self._closed = False
        self._engines: Dict[str, AgentEngine] = {}
        self.env: Dict[str, str] = {}
        self._explicit_engines = dict(engines) if engines is not None else None
        self.update_env(env if env is not None else os.environ)
        self._load()

    # ------------------------------------------------------------------ configuration
    def update_env(self, env: Mapping[str, str]) -> None:
        with self._mu:
            self.env = dict(env)
            self.turn_budget_s = _env_float(self.env, "AI_HOUSE_AGENT_TURN_BUDGET_S", DEFAULT_TURN_BUDGET_S, 5.0, 600.0)
            self.max_parallel = int(_env_float(self.env, "AI_HOUSE_AGENT_MAX_PARALLEL", DEFAULT_MAX_PARALLEL, 1, 16))
            self.max_clients = int(_env_float(self.env, "AI_HOUSE_AGENT_MAX_CLIENTS", DEFAULT_MAX_CLIENTS, 1, 64))
            self.idle_drop_s = _env_float(self.env, "AI_HOUSE_AGENT_IDLE_DROP_S", DEFAULT_IDLE_DROP_S, 1.0, 86400.0)
            self.session_max_tokens = int(
                _env_float(self.env, "AI_HOUSE_AGENT_SESSION_MAX_TOKENS", DEFAULT_SESSION_MAX_TOKENS, 2000, 2_000_000)
            )
            if self._explicit_engines is not None:
                self._engines = dict(self._explicit_engines)
            else:
                from . import build_engines

                old = self._engines
                self._engines = build_engines(self.env, self.state_dir)
                if old and self._loop is not None:
                    # settings changed under live clients: they were built with the old ones
                    self._submit(self._drop_all(old))

    def set_engines(self, engines: Mapping[str, AgentEngine]) -> None:
        """Replace the engines (an embedding program's own shell, or a scripted one in tests)."""
        with self._mu:
            old = self._engines
            self._explicit_engines = dict(engines)
            self._engines = dict(engines)
            if old and self._loop is not None:
                self._submit(self._drop_all(old))

    def engine(self, name: str) -> Optional[AgentEngine]:
        with self._mu:
            return self._engines.get(name)

    def unavailable_reason(self, name: str) -> str:
        eng = self.engine(name)
        if eng is None:
            return f"没有名为 {name} 的住客大脑引擎"
        return eng.unavailable_reason()

    def secrets(self) -> List[str]:
        with self._mu:
            engines = list(self._engines.values())
        out: List[str] = []
        for eng in engines:
            out.extend(s for s in eng.secrets() if s)
        return out

    def status(self) -> Dict[str, Any]:
        with self._mu:
            engines = dict(self._engines)
            sessions = list(self._sessions.values())
            stored = len(self._stored)
        return {
            "engines": {
                name: {"label": eng.label or name, "available": not eng.unavailable_reason(), "reason": eng.unavailable_reason()}
                for name, eng in engines.items()
            },
            "sessions": stored,
            "live_clients": sum(1 for s in sessions if s.client is not None),
        }

    # ------------------------------------------------------------------ persistence
    @property
    def _store_path(self) -> Path:
        return self.state_dir / "sessions.json"

    def _load(self) -> None:
        try:
            doc = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(doc, dict) and isinstance(doc.get("sessions"), dict):
            self._stored = {str(k): dict(v) for k, v in doc["sessions"].items() if isinstance(v, dict)}

    def _save(self, sess: Optional[AgentSession] = None) -> None:
        with self._mu:
            if sess is not None and self._sessions.get(sess.key) is not sess:
                return  # forgotten while its turn was still running (world reset): do not bring it back
            if sess is not None:
                self._stored[sess.key] = {
                    "engine": sess.engine,
                    "session_id": sess.session_id,
                    "context_tokens": int(sess.context_tokens),
                    "unsent_streak": int(sess.unsent_streak),
                    "resume_failures": int(sess.resume_failures),
                    "pending_reminder": bool(sess.pending_reminder),
                    "blocked_note": bool(sess.blocked_note),
                    "carry_note": sess.carry_note,
                    "turns": int(sess.turns),
                    "updated_at": time.time(),
                }
            if not self._stored and not self._store_path.exists():
                return  # nothing to remember and nothing on disk to correct: do not create the directory for it
            doc = {"version": 1, "sessions": self._stored}
            try:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".sessions-", dir=str(self.state_dir))
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(doc, fp, ensure_ascii=False)
                os.replace(tmp, self._store_path)
            except OSError as exc:
                self._log(f"agent sessions not saved: {type(exc).__name__}: {exc}")

    @staticmethod
    def session_key(actor: str, channel: str) -> str:
        return f"{actor}|{channel}"

    def _session(self, req: TurnRequest) -> AgentSession:
        key = self.session_key(req.actor, req.channel)
        with self._mu:
            sess = self._sessions.get(key)
            if sess is None:
                st = self._stored.get(key) or {}
                sess = AgentSession(
                    key=key,
                    actor=req.actor,
                    channel=req.channel,
                    where=req.where,
                    engine=str(st.get("engine") or ""),
                    session_id=str(st.get("session_id") or ""),
                    context_tokens=int(st.get("context_tokens") or 0),
                    unsent_streak=int(st.get("unsent_streak") or 0),
                    resume_failures=int(st.get("resume_failures") or 0),
                    pending_reminder=bool(st.get("pending_reminder")),
                    blocked_note=bool(st.get("blocked_note")),
                    carry_note=str(st.get("carry_note") or ""),
                    turns=int(st.get("turns") or 0),
                    workdir=str(self.state_dir / "work" / req.actor),
                )
                sess.audit = self._audit
                self._sessions[key] = sess
            return sess

    def _audit(self, event: str, fields: Dict[str, Any]) -> None:
        self._log("agent " + event + " " + " ".join(f"{k}={v}" for k, v in fields.items()))

    # ------------------------------------------------------------------ loop thread
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._mu:
            if self._closed:
                raise EngineUnavailable("住客大脑已经关停")
            if self._loop is None:
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def _main() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    try:
                        loop.run_forever()
                    finally:
                        try:
                            loop.run_until_complete(loop.shutdown_asyncgens())
                        finally:
                            loop.close()

                t = threading.Thread(target=_main, name="ai-house-agent", daemon=True)
                t.start()
                ready.wait(5.0)
                self._loop, self._thread = loop, t
                asyncio.run_coroutine_threadsafe(self._start_reaper(), loop)
            return self._loop

    def _submit(self, coro: Any) -> Any:
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return None
        return asyncio.run_coroutine_threadsafe(coro, loop)

    async def _start_reaper(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.get_running_loop().create_task(self._reap_forever())

    async def _reap_forever(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, min(60.0, self.idle_drop_s / 2.0)))
            try:
                await self.reap_idle()
            except Exception as exc:  # noqa: BLE001
                self._log(f"agent reaper: {type(exc).__name__}: {exc}")

    async def reap_idle(self) -> int:
        """Drop clients nobody has used for `idle_drop_s`. The session id stays; the next turn resumes it."""
        now = self._mono()
        with self._mu:
            cands = [s for s in self._sessions.values() if s.client is not None and now - s.last_used >= self.idle_drop_s]
        dropped = 0
        for sess in cands:
            if sess.lock is not None and sess.lock.locked():
                continue
            await self._safe_drop(sess)
            dropped += 1
        return dropped

    async def _make_room(self, keep: AgentSession) -> None:
        """Keep live clients under the cap by dropping the least recently used idle ones."""
        with self._mu:
            live = [s for s in self._sessions.values() if s.client is not None and s is not keep]
        excess = len(live) + 1 - self.max_clients
        if excess <= 0:
            return
        for sess in sorted(live, key=lambda s: s.last_used):
            if excess <= 0:
                break
            if sess.lock is not None and sess.lock.locked():
                continue
            await self._safe_drop(sess)
            excess -= 1

    async def _safe_drop(self, sess: AgentSession, engine: Optional[AgentEngine] = None) -> None:
        eng = engine or self.engine(sess.engine)
        if eng is None or sess.client is None:
            sess.client = None
            sess.client_fingerprint = ""
            return
        try:
            await eng.drop_client(sess)
        except Exception as exc:  # noqa: BLE001
            self._log(f"agent drop_client: {type(exc).__name__}: {mask_secrets(str(exc))}")
        finally:
            sess.client = None
            sess.client_fingerprint = ""

    async def _drop_all(self, engines: Optional[Mapping[str, AgentEngine]] = None, *, wait: bool = True) -> None:
        with self._mu:
            sessions = list(self._sessions.values())
        for sess in sessions:
            if sess.client is None:
                continue
            eng = (engines or {}).get(sess.engine) if engines is not None else None
            if wait and sess.lock is not None:
                async with sess.lock:
                    await self._safe_drop(sess, eng)
            else:
                await self._safe_drop(sess, eng)

    async def _abandon(self, sess: AgentSession, victim: Any) -> None:
        """Belt and braces for a turn given up on mid-flight. The real work is done inside the turn itself, while it
        still holds the conversation's lock (see `_turn`); this only makes sure `victim`, the client the turn was
        using, is gone, and never touches a newer client that a turn queued behind it has made since."""
        if victim is None or sess.lock is None:
            return
        async with sess.lock:
            if sess.client is victim:
                await self._safe_drop(sess)

    # ------------------------------------------------------------------ housekeeping API
    def note_blocked(self, actor: str, channel: str) -> None:
        """The house held back this resident's last reply; tell her at the start of her next turn."""
        key = self.session_key(actor, channel)
        with self._mu:
            sess = self._sessions.get(key)
            if sess is not None:
                sess.blocked_note = True
                self._save(sess)

    def forget_all(self) -> None:
        """Forget every session (the world was reset: these residents no longer exist)."""
        with self._mu:
            # a client still being connected for one of these must not be kept when it is done: it would hang off a
            # session nobody can reach any more
            for sess in self._sessions.values():
                sess.forgotten = True
            engines = list(self._engines.values())
        # a turn still running on one of these is cut off rather than waited for: its reply is discarded anyway
        fut = self._submit(self._drop_all(wait=False))
        if fut is not None:
            try:
                fut.result(timeout=20)
            except Exception:  # noqa: BLE001
                pass
        with self._mu:
            self._sessions.clear()
            self._stored.clear()
            self._save()
        # what was said lives in the shell's own files too, and those hold private chats: remove them, now that no
        # client has them open
        paths: List[Path] = [self.state_dir / "work"]
        for eng in engines:
            try:
                paths.extend(eng.conversation_paths())
            except Exception as exc:  # noqa: BLE001
                self._log(f"agent conversation paths: {type(exc).__name__}: {exc}")
        for path in paths:
            try:
                if path.exists():
                    shutil.rmtree(path)
            except OSError as exc:
                self._log(f"agent conversation files not deleted: {path}: {type(exc).__name__}: {exc}")

    def close(self) -> None:
        with self._mu:
            if self._closed:
                return
            self._closed = True
            loop, thread = self._loop, self._thread
        if loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._drop_all(), loop)
        try:
            fut.result(timeout=20)
        except Exception:  # noqa: BLE001
            pass

        def _stop() -> None:
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.call_soon(loop.stop)

        loop.call_soon_threadsafe(_stop)
        if thread is not None:
            thread.join(timeout=5)

    # ------------------------------------------------------------------ one turn
    def run_turn(self, req: TurnRequest) -> TurnResult:
        engine = self.engine(req.engine)
        if engine is None:
            raise EngineUnavailable(f"没有名为 {req.engine} 的住客大脑引擎")
        reason = engine.unavailable_reason()
        if reason:
            raise EngineUnavailable(reason)
        loop = self._ensure_loop()
        specs = [dict(s) for s in req.tool_specs if s.get("name") not in (SPEAK_TOOL_SPEC["name"], QUIET_TOOL_SPEC["name"])]
        turn = Turn(
            allow_quiet=req.allow_quiet,
            tool_names=[str(s.get("name") or "") for s in specs],
            max_sends=req.max_sends,
        )
        sess = self._session(req)
        t0 = self._mono()
        cf = asyncio.run_coroutine_threadsafe(self._turn(engine, sess, req, turn, specs), loop)
        budget = self.turn_budget_s
        outer = remaining_budget()  # the conversation's own ceiling, when the caller set one
        if outer is not None:
            budget = max(0.0, min(budget, outer))
        deadline = time.monotonic() + budget
        while True:
            try:
                call = turn.calls.get(timeout=0.02)
            except queue.Empty:
                if cf.done():
                    break
                if time.monotonic() >= deadline:
                    turn.closed = True
                    victim = sess.client
                    cf.cancel()
                    self._submit(self._abandon(sess, victim))
                    self._audit("turn_timeout", {"actor": req.actor, "where": req.where, "engine": engine.name})
                    if turn.spoke:
                        # she already answered; whatever she was still doing is not worth losing the reply for
                        return TurnResult(
                            messages=list(turn.sent), quiet=turn.quiet, quiet_reason=turn.quiet_reason,
                            engine=engine.name, model=engine.default_model_label(), partial=True,
                            latency_ms=int((self._mono() - t0) * 1000), tool_calls=turn.tool_calls,
                        )
                    raise LLMDeadlineError(f"住客大脑这一轮超过了 {budget:g} 秒")
                continue
            self._serve(call, req.tool_runner)
        res = cf.result()
        res.latency_ms = int((self._mono() - t0) * 1000)
        return res

    @staticmethod
    def _serve(call: Any, runner: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]]) -> None:
        name, args, loop, fut = call
        try:
            out: Any = runner(name, args) if runner is not None else {"error": "no_tool_runner"}
        except Exception as exc:  # noqa: BLE001
            out = {"error": mask_secrets(f"{type(exc).__name__}: {exc}")}
        if not isinstance(out, dict):
            out = {"result": out}

        def _resolve() -> None:
            if not fut.done():
                fut.set_result(out)

        loop.call_soon_threadsafe(_resolve)

    async def _turn(
        self, engine: AgentEngine, sess: AgentSession, req: TurnRequest, turn: Turn, specs: List[Dict[str, Any]]
    ) -> TurnResult:
        if sess.lock is None:
            sess.lock = asyncio.Lock()
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.max_parallel)
        async with sess.lock:
            async with self._slots:
                try:
                    return await self._turn_locked(engine, sess, req, turn, specs)
                except asyncio.CancelledError:
                    # The caller gave up on this turn (its deadline): the client is in an unknown state. Close it
                    # here, while the lock is still held, so that a turn queued behind this one starts on a new
                    # client instead of this one. Shielded, so that a second cancellation cannot leave it open.
                    try:
                        await asyncio.shield(self._safe_drop(sess, engine))
                    except BaseException:  # noqa: BLE001 - cancelled again: the shielded drop still finishes
                        pass
                    raise

    def _compose(self, sess: AgentSession, req: TurnRequest, fresh: bool) -> str:
        notes: List[str] = []
        lines = [ln for ln, _ in req.history]
        if fresh:
            if sess.carry_note:
                notes.append(sess.carry_note)
            notes.append(prompts.FRESH_SESSION_NOTE)
        else:
            last_self = max((i for i, (_, mine) in enumerate(req.history) if mine), default=-1)
            lines = lines[last_self + 1:]
            if lines:
                notes.append(prompts.RESUMED_SESSION_NOTE)
        if sess.blocked_note:
            notes.append(prompts.BLOCKED_NOTE)
        if sess.pending_reminder:
            notes.append(prompts.UNSENT_REMINDER)
        return prompts.turn_prompt(
            notes=notes,
            state_lines=req.state_lines,
            memory_context=req.memory_context,
            history_lines=lines,
            said_line=req.said_line,
            attachment_block=req.attachment_block,
        )

    def _begin_replacement(self, sess: AgentSession, engine: AgentEngine, why: str) -> Tuple[str, int]:
        """Start a fresh session in place of the stored one. Returns what to put back if the fresh one does not work:
        an empty id must not reach the disk before a new session has really started."""
        self._audit("session_replaced", {"actor": sess.actor, "where": sess.where, "engine": engine.name, "why": why})
        kept = (sess.session_id, sess.context_tokens)
        sess.session_id = ""
        sess.context_tokens = 0
        return kept

    async def _engine_turn(
        self, engine: AgentEngine, sess: AgentSession, req: TurnRequest, turn: Turn, out: TurnResult
    ) -> Tuple[EngineResult, bool]:
        kept: Optional[Tuple[str, int]] = None
        if sess.session_id and sess.resume_failures >= RESUME_DOUBT_LIMIT:
            # the last turns on this session all failed to start: it may really be gone, with an error text we do not
            # recognise. Stop retrying it for ever.
            kept = self._begin_replacement(sess, engine, "repeated_failures")
        try:
            for attempt in (0, 1):
                fresh = not sess.session_id
                out.fresh_session = fresh
                prompt = self._compose(sess, req, fresh)
                try:
                    res = await engine.run_turn(sess, prompt)
                except ResumeFailed as exc:
                    await self._safe_drop(sess, engine)
                    if attempt == 0 and not fresh and not turn.spoke:
                        kept = kept or self._begin_replacement(sess, engine, "resume_failed")
                        continue
                    raise EngineError(str(exc)) from exc
                except ResumeDoubt as exc:
                    await self._safe_drop(sess, engine)
                    sess.resume_failures += 1
                    self._audit(
                        "resume_doubt",
                        {"actor": sess.actor, "where": sess.where, "engine": engine.name, "in_a_row": sess.resume_failures},
                    )
                    raise EngineError(str(exc)) from exc
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._safe_drop(sess, engine)
                    raise
                if sess.forgotten:
                    await self._safe_drop(sess, engine)
                    raise EngineError("这个会话在这一轮进行中被重置了")
                if res.session_id:
                    sess.session_id = res.session_id
                sess.resume_failures = 0
                kept = None
                return res, fresh
            raise EngineError("会话无法建立")  # pragma: no cover - the loop always returns or raises
        finally:
            if kept is not None and not sess.session_id:
                sess.session_id, sess.context_tokens = kept

    async def _rotate(self, engine: AgentEngine, sess: AgentSession, why: str, note: str, *, keep_reminder: bool = False) -> None:
        await self._safe_drop(sess, engine)
        sess.session_id = ""
        sess.context_tokens = 0
        sess.unsent_streak = 0
        sess.resume_failures = 0
        if not keep_reminder:
            sess.pending_reminder = False
        sess.carry_note = note
        self._audit("session_rotated", {"actor": sess.actor, "where": sess.where, "why": why})

    @staticmethod
    def _notes_delivered(sess: AgentSession) -> None:
        """The notes were delivered with this turn's prompt."""
        sess.carry_note = ""
        sess.blocked_note = False
        sess.pending_reminder = False
        sess.turns += 1

    async def _turn_locked(
        self, engine: AgentEngine, sess: AgentSession, req: TurnRequest, turn: Turn, specs: List[Dict[str, Any]]
    ) -> TurnResult:
        if sess.engine and sess.engine != engine.name:
            # the resident moved to another shell: a session does not carry across shells
            await self._safe_drop(sess, self.engine(sess.engine))
            sess.session_id = ""
            sess.context_tokens = 0
            sess.unsent_streak = 0
        sess.engine = engine.name
        sess.where = req.where
        sess.system = req.system
        sess.tool_specs = [SPEAK_TOOL_SPEC, *([QUIET_TOOL_SPEC] if req.allow_quiet else []), *specs]
        sess.turn = turn
        usage = EngineUsage()
        out = TurnResult(engine=engine.name)
        try:
            await self._make_room(sess)
            try:
                res, fresh = await self._engine_turn(engine, sess, req, turn, out)
            except EngineUnavailable:
                raise
            except EngineError as exc:
                if not turn.spoke:
                    raise
                # She had already answered when the shell failed: keep what she said (as for a deadline). The client
                # was dropped; the session id is kept, so the next turn picks the conversation up.
                self._audit("turn_error_after_speaking", {"actor": sess.actor, "where": sess.where, "error": type(exc).__name__})
                await self._safe_drop(sess, engine)
                out.partial = True
                self._notes_delivered(sess)
                sess.unsent_streak = 0
            else:
                out.fresh_session = fresh
                usage.add(res.usage)
                self._notes_delivered(sess)
                texts = list(res.texts)
                if not turn.spoke and texts:
                    out.nudged = True
                    try:
                        res2 = await engine.run_turn(
                            sess, prompts.TEXT_ONLY_NUDGE_GROUP if req.allow_quiet else prompts.TEXT_ONLY_NUDGE
                        )
                        usage.add(res2.usage)
                        if res2.session_id:
                            sess.session_id = res2.session_id
                        if res2.texts:
                            texts = list(res2.texts)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - the reminder is best effort
                        self._audit("nudge_failed", {"actor": sess.actor, "error": type(exc).__name__})
                        await self._safe_drop(sess, engine)
                if turn.spoke:
                    sess.unsent_streak = 0
                elif texts:
                    if req.where != "group":
                        # a private chat must not go unanswered: send what she wrote
                        turn.sent.append(texts[-1].strip()[:MAX_MESSAGE_CHARS])
                        out.autosent = True
                    sess.unsent_streak += 1
                    if sess.unsent_streak >= UNSENT_STREAK_RESET:
                        await self._rotate(engine, sess, "unsent", prompts.RESET_NOTE)
                        out.rotated = "unsent"
                    else:
                        sess.pending_reminder = True
                elif req.where != "group":
                    raise EngineError("住客这一轮没有给出任何回复")
                if usage.context_tokens:
                    sess.context_tokens = usage.context_tokens
                if not out.rotated and sess.context_tokens > self.session_max_tokens:
                    # the reminder set above is still owed: it goes with her into the new session
                    await self._rotate(engine, sess, "too_long", prompts.ROTATED_NOTE, keep_reminder=True)
                    out.rotated = "too_long"
        finally:
            turn.closed = True
            sess.turn = None
            sess.last_used = self._mono()
            self._save(sess)
            if sess.forgotten and sess.client is not None:
                # the world was reset while this turn ran: the client belongs to nobody now
                await self._safe_drop(sess, engine)
        out.messages = list(turn.sent)
        out.quiet = turn.quiet or not turn.sent
        out.quiet_reason = turn.quiet_reason
        out.usage = usage
        out.model = usage.model or engine.default_model_label()
        out.tool_calls = turn.tool_calls
        self._audit(
            "turn",
            {
                "actor": sess.actor, "where": sess.where, "engine": engine.name, "fresh": int(out.fresh_session),
                "sent": len(out.messages), "quiet": int(out.quiet), "tools": turn.tool_calls,
                "nudged": int(out.nudged), "autosent": int(out.autosent), "rotated": out.rotated or "-",
                "denied": len(turn.denied), "ctx": sess.context_tokens,
            },
        )
        return out
