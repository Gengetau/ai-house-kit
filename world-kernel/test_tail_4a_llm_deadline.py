"""Tail 4a: LLM total deadline, param-cache ordering, empty-reply failure, key/origin isolation,
config-error visibility, bad-port handling and circuit-breaker window/half-open/ping semantics.

Every test talks to a real local fake HTTP server (hang / 4xx / 5xx / empty content) and asserts on
observable behaviour (wall-clock time, which key reached which origin, ledger rows), not on internals.
"""
from __future__ import annotations

import base64
import datetime as dt
import http.server
import itertools
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ai_house.chat_agent import RoommateChatAgent  # noqa: E402
from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, JST  # noqa: E402
from ai_house.llm import LLMAPIError, ModelRouter, post_openai_chat_json  # noqa: E402
from ai_house.plugin_api import (  # noqa: E402
    AttachmentContext,
    AttachmentProcessor,
    PluginDefinition,
    PluginManifest,
)

OK_DOC = {"choices": [{"message": {"role": "assistant", "content": "好的"}, "finish_reason": "stop"}], "usage": {"total_tokens": 7}}
EMPTY_STOP_DOC = {"choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}], "usage": {"total_tokens": 4}}


class FakeServer:
    """Local OpenAI-compatible endpoint. `handler(n, body, headers)` returns (status, doc) or ('hang', seconds)."""

    def __init__(self, handler: Optional[Callable[[int, Dict[str, Any], Any], Tuple[Any, Any]]] = None) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.auth: List[str] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.handler = handler or (lambda n, b, h: (200, OK_DOC))
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                ln = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(ln)
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    body = {}
                with outer.lock:
                    outer.requests.append(body)
                    outer.auth.append(self.headers.get("Authorization", ""))
                    n = len(outer.requests)
                status, doc = outer.handler(n, body, self.headers)
                if status == "hang":
                    outer.stop.wait(float(doc))
                    return
                out = json.dumps(doc).encode("utf-8")
                try:
                    self.send_response(int(status))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass

        class S(http.server.ThreadingHTTPServer):
            daemon_threads = True

        self.srv = S(("127.0.0.1", 0), H)
        self.base_url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def hits(self) -> int:
        with self.lock:
            return len(self.requests)

    def close(self) -> None:
        self.stop.set()
        self.srv.shutdown()
        self.srv.server_close()


class FakeClock:
    def __init__(self, ts: float) -> None:
        self.ts = float(ts)

    def now(self) -> float:
        return self.ts

    def advance(self, s: float) -> None:
        self.ts += float(s)


class Base(unittest.TestCase):
    def server(self, handler: Optional[Callable[..., Tuple[Any, Any]]] = None) -> FakeServer:
        s = FakeServer(handler)
        self.addCleanup(s.close)
        return s

    def tmpdir(self) -> str:
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        return t.name

    def router(self, env: Dict[str, str], **kw: Any) -> ModelRouter:
        kw.setdefault("retry_delay_s", 0.0)
        r = ModelRouter(env, runtime_dir=kw.pop("runtime_dir", self.tmpdir()), **kw)
        self.addCleanup(r.close)
        return r

    def _daemon(self, env: Dict[str, str], **kw: Any) -> HouseDaemon:
        t0 = dt.datetime(2026, 11, 22, 16, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(self.tmpdir(), pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
                        env=env, start_ts=t0, virtual_clock=True, allow_onboarding=False, **kw)
        self.addCleanup(d.close)
        return d

    @staticmethod
    def usage_rows(r: ModelRouter) -> List[Dict[str, Any]]:
        return list(r.get_usage_summary(period="all", recent_limit=200)["recent"])


HANG = lambda n, b, h: ("hang", 60)  # noqa: E731


# --------------------------------------------------------------------------------------------- 1. deadline
class DeadlineTests(Base):
    BUDGET = 2.5

    def env(self, p: FakeServer, f: Optional[FakeServer] = None, **extra: str) -> Dict[str, str]:
        e = {
            "AI_HOUSE_API_KEY": "sk-prim-aaaaaaaa",
            "AI_HOUSE_BASE_URL": p.base_url,
            "AI_HOUSE_MODEL": "m1",
            "AI_HOUSE_LLM_TOTAL_BUDGET_S": str(self.BUDGET),
            "AI_HOUSE_CHAT_TIMEOUT_S": "4",  # per-request timeout > budget/2: retries + fallback would blow the budget
            **extra,
        }
        if f is not None:
            e.update({"AI_HOUSE_FALLBACK_API_KEY": "sk-fall-bbbbbbbb", "AI_HOUSE_FALLBACK_BASE_URL": f.base_url,
                      "AI_HOUSE_FALLBACK_MODEL": "m2"})
        return e

    def test_complete_text_total_deadline_across_retry_and_fallback(self) -> None:
        p, f = self.server(HANG), self.server(HANG)
        r = self.router(self.env(p, f))
        t = time.monotonic()
        with self.assertRaises(LLMAPIError) as cm:
            r.complete_text("s", "u", purpose="chat")
        el = time.monotonic() - t
        self.assertLessEqual(el, self.BUDGET * 3, f"complete_text took {el:.1f}s")  # unbounded would be 60s+
        self.assertGreaterEqual(el, self.BUDGET - 0.5)
        self.assertEqual(cm.exception.status_code, 504)
        self.assertIn("总时限", str(cm.exception))
        # once the budget is gone nothing else is attempted: no more than the one hanging request per endpoint tried
        self.assertLessEqual(p.hits + f.hits, 3)

    def test_complete_messages_budget_is_default_and_env_configurable(self) -> None:
        p = self.server(HANG)
        # no env override -> a finite default is applied (not unlimited); it is only read, not waited out here
        r = self.router({"AI_HOUSE_API_KEY": "sk-x-aaaaaaaa", "AI_HOUSE_BASE_URL": p.base_url})
        self.assertEqual(r.resolve_total_budget("chat"), 45.0)
        r2 = self.router({"AI_HOUSE_API_KEY": "sk-x-aaaaaaaa", "AI_HOUSE_BASE_URL": p.base_url, "AI_HOUSE_LLM_TOTAL_BUDGET_S": "7"})
        self.assertEqual(r2.resolve_total_budget("chat"), 7.0)

    def test_tool_loop_hanging_endpoint_stops_within_budget(self) -> None:
        p, f = self.server(HANG), self.server(HANG)
        r = self.router(self.env(p, f))
        d = self._daemon_agent_loop(r)
        t = time.monotonic()
        with self.assertRaises(Exception):
            d._run_tool_loop("sys", "usr", [], lambda n, a: {}, actor_id="sora")
        el = time.monotonic() - t
        self.assertLessEqual(el, self.BUDGET * 3, f"_run_tool_loop took {el:.1f}s")  # unbounded would be 60s+

    def test_tool_loop_slow_rounds_share_one_budget(self) -> None:
        # each round answers after 1.8s with another tool call: 3 rounds + final = 7.2s >> 2.5s budget
        def slow_tool(n: int, body: Dict[str, Any], h: Any) -> Tuple[Any, Any]:
            time.sleep(1.8)
            return 200, {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{n}", "type": "function", "function": {"name": "inspect_house", "arguments": "{}"}}]},
                "finish_reason": "tool_calls"}], "usage": {"total_tokens": 3}}

        p = self.server(slow_tool)
        r = self.router(self.env(p))
        agent = self._daemon_agent_loop(r)
        t = time.monotonic()
        with self.assertRaises(LLMAPIError) as cm:
            agent._run_tool_loop("sys", "usr", [{"name": "inspect_house", "description": "x", "parameters": {"type": "object", "properties": {}}}],
                                 lambda n, a: {"ok": True}, actor_id="sora")
        el = time.monotonic() - t
        self.assertLessEqual(el, self.BUDGET + 2.5, f"slow tool loop took {el:.1f}s")  # unbounded: 7.2s
        self.assertEqual(cm.exception.status_code, 504)
        self.assertLess(p.hits, 4)

    def _daemon_agent_loop(self, router: ModelRouter) -> RoommateChatAgent:
        class V:
            NAMES: Dict[str, str] = {}
            SELVES: Dict[str, str] = {}
        return RoommateChatAgent(V(), env=router.env, router=router)

    def test_converse_total_time_bounded_with_hanging_endpoint(self) -> None:
        p, f = self.server(HANG), self.server(HANG)
        d = self._daemon({**self.env(p, f), "AI_HOUSE_OWNER_TOKEN": "owner-tail4a"})
        t = time.monotonic()
        res = d.converse("private:sora", "你好呀", owner_speaker("房主"))
        el = time.monotonic() - t
        self.assertTrue(res["ok"])
        # reply <= one budget, then the world step gets its own fresh life budget (tail 7b); unbounded would be 60s+
        self.assertLessEqual(el, self.BUDGET * 4, f"converse took {el:.1f}s")
        txt = " ".join(str(x.get("text") or "") for x in res.get("replies") or [])
        self.assertIn("接口暂不可用", txt)

    def test_group_turn_with_two_slow_healthy_replies_is_not_cut_by_the_turn_ceiling(self) -> None:
        # 模型健康但偏慢：每条回复约 1.6 秒，单条都在 2.5 秒预算内，两位住客合计超过一个预算。
        # 整轮上限按“小屋群最多两位住客回复”放宽，两条回复都应正常产出。
        def slow_ok(n: int, body: Dict[str, Any], h: Any) -> Tuple[Any, Any]:
            sys_txt = " ".join(str(m.get("content") or "") for m in body.get("messages") or [] if m.get("role") == "system")
            if "输出必须是 JSON" in sys_txt:
                return 200, {"choices": [{"message": {"role": "assistant", "content": "{\"steps\": []}"},
                                          "finish_reason": "stop"}], "usage": {"total_tokens": 2}}
            time.sleep(1.6)
            return 200, {"choices": [{"message": {"role": "assistant", "content": "慢慢说完了。"},
                                      "finish_reason": "stop"}], "usage": {"total_tokens": 5}}

        p = self.server(slow_ok)
        d = self._daemon({**self.env(p), "AI_HOUSE_OWNER_TOKEN": "owner-tail4a"})
        names = [d.vocab.NAMES[a] for a in list(d.vocab.NAMES)[:2]]
        t = time.monotonic()
        res = d.converse("group", f"{names[0]}、{names[1]}，你们都说一句", owner_speaker("房主"))
        el = time.monotonic() - t
        replies = res.get("replies") or []
        self.assertEqual(len(replies), 2, replies)
        for rep in replies:
            self.assertEqual(rep.get("mode"), "live_api", rep)
            self.assertNotIn("接口暂不可用", str(rep.get("text") or ""))
        self.assertGreater(el, self.BUDGET, "the two replies together must exceed one budget for this test to mean anything")

    def test_group_turn_ceiling_still_bounds_a_hanging_endpoint(self) -> None:
        p, f = self.server(HANG), self.server(HANG)
        d = self._daemon({**self.env(p, f), "AI_HOUSE_OWNER_TOKEN": "owner-tail4a"})
        names = [d.vocab.NAMES[a] for a in list(d.vocab.NAMES)[:2]]
        t = time.monotonic()
        res = d.converse("group", f"{names[0]}、{names[1]}，你们都说一句", owner_speaker("房主"))
        el = time.monotonic() - t
        self.assertTrue(res["ok"])
        self.assertLessEqual(el, 6 * self.BUDGET, f"group converse took {el:.1f}s")  # unbounded would be 60s+

    def test_attachment_processors_get_timeouts_capped_by_remaining_budget(self) -> None:
        hang = self.server(HANG)
        seen: List[float] = []

        def slow_http_processor(actx: AttachmentContext) -> List[str]:
            # same call shape as vision_understand: fixed 15s timeout against a hanging endpoint
            t = time.monotonic()
            try:
                post_openai_chat_json(base_url=hang.base_url, api_key="sk-att-aaaaaaaa", payload={"model": "m"}, timeout_s=15.0)
            except Exception:
                pass
            seen.append(time.monotonic() - t)
            return []

        later: List[int] = []

        def later_processor(actx: AttachmentContext) -> List[str]:
            later.append(1)
            return []

        d = self._daemon({"AI_HOUSE_OWNER_TOKEN": "owner-tail4a", "AI_HOUSE_ATTACHMENT_BUDGET_S": "2",
                          "AI_HOUSE_API_KEY": "sk-main-aaaaaaaa", "AI_HOUSE_BASE_URL": hang.base_url,
                          "AI_HOUSE_LLM_TOTAL_BUDGET_S": "6"})
        d.registry.register_definition(PluginDefinition(
            manifest=PluginManifest(plugin_id="tp_slow", name="slow", kind="bundle", default_enabled=True),
            attachment_processors=(AttachmentProcessor(processor_id="a_slow", handler=slow_http_processor, priority=10),
                                   AttachmentProcessor(processor_id="b_later", handler=later_processor, priority=20)),
            source="entrypoint"))
        d.reload_plugins()
        t = time.monotonic()
        res = d.converse("private:sora", "看看这个", owner_speaker("房主"),
                         attachments={"images": ["data:image/png;base64," + base64.b64encode(b"x").decode()]})
        el = time.monotonic() - t
        self.assertTrue(res["ok"])
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(seen[0], 2.0 + 3.0, f"processor ran {seen[0]:.1f}s despite 2s attachment budget (15s fixed timeout)")
        self.assertEqual(later, [], "budget exhausted: later processors must not run")
        self.assertLessEqual(el, 6.0 * 3, f"converse took {el:.1f}s")  # reply side <= 6s + fresh life budget; unbounded 60s+

    def test_voice_transcription_timeout_capped_by_budget(self) -> None:
        from ai_house.builtin_plugins.voice_io import transcribe_audio_source
        from ai_house.llm import deadline_scope

        hang = self.server(HANG)
        wav = "data:audio/wav;base64," + base64.b64encode(b"RIFF0000WAVEfmt ").decode()
        t = time.monotonic()
        with deadline_scope(1.5):
            res = transcribe_audio_source(wav, cfg={"api_key": "sk-stt-aaaaaaaa", "base_url": hang.base_url})
        el = time.monotonic() - t
        self.assertFalse(res.get("ok"))
        self.assertLessEqual(el, 1.5 + 4.0, f"transcription took {el:.1f}s with a 1.5s budget (fixed 20s timeout)")


# --------------------------------------------------------------------------------------------- 2. caps
class ReasoningCapTests(Base):
    def test_cap_not_written_when_adapted_retry_fails(self) -> None:
        def h(n: int, body: Dict[str, Any], _h: Any) -> Tuple[Any, Any]:
            if "max_tokens" in body or "temperature" in body:
                return 400, {"error": {"message": "Unsupported parameter: 'max_tokens' is not supported."}}
            return 503, {"error": {"message": "overloaded"}}

        p = self.server(h)
        r = self.router({"AI_HOUSE_API_KEY": "sk-p-aaaaaaaa", "AI_HOUSE_BASE_URL": p.base_url, "AI_HOUSE_MODEL": "mc",
                         "AI_HOUSE_CHAT_MAX_RETRIES": "0"})
        with self.assertRaises(LLMAPIError):
            r.complete_text("s", "u", purpose="chat")
        self.assertEqual(r._reasoning_caps, set(), "failed adapted retry must not poison the cache")

    def test_cap_written_when_adapted_retry_succeeds(self) -> None:
        def h(n: int, body: Dict[str, Any], _h: Any) -> Tuple[Any, Any]:
            if "max_tokens" in body or "temperature" in body:
                return 400, {"error": {"message": "Unsupported parameter: 'max_tokens' is not supported."}}
            return 200, OK_DOC

        p = self.server(h)
        r = self.router({"AI_HOUSE_API_KEY": "sk-p-aaaaaaaa", "AI_HOUSE_BASE_URL": p.base_url, "AI_HOUSE_MODEL": "mc"})
        self.assertEqual(r.complete_text("s", "u", purpose="chat")[0], "好的")
        self.assertEqual(r._reasoning_caps, {(p.base_url, "mc")})


# --------------------------------------------------------------------------------------------- 3. empty
class EmptyReplyTests(Base):
    def env(self, p: FakeServer) -> Dict[str, str]:
        return {"AI_HOUSE_API_KEY": "sk-p-aaaaaaaa", "AI_HOUSE_BASE_URL": p.base_url, "AI_HOUSE_MODEL": "m"}

    def test_empty_stop_is_failure_after_one_retry_and_ledger_is_honest(self) -> None:
        p = self.server(lambda n, b, h: (200, EMPTY_STOP_DOC))
        r = self.router(self.env(p))
        with self.assertRaises(LLMAPIError) as cm:
            r.complete_text("s", "u", purpose="chat")
        self.assertEqual(p.hits, 2, "exactly one retry on an empty reply")
        self.assertEqual(cm.exception.detail, "empty_reply")
        rows = self.usage_rows(r)
        self.assertTrue(rows)
        self.assertFalse([x for x in rows if x["ok"] == 1], "an empty reply must never be logged as ok=1")
        self.assertTrue(all(x["error_code"] == "EMPTY_REPLY" for x in rows), rows)

    def test_empty_then_content_succeeds_once(self) -> None:
        p = self.server(lambda n, b, h: (200, EMPTY_STOP_DOC if n == 1 else OK_DOC))
        r = self.router(self.env(p))
        self.assertEqual(r.complete_text("s", "u", purpose="chat")[0], "好的")
        self.assertEqual(p.hits, 2)
        self.assertEqual([x["ok"] for x in self.usage_rows(r)], [1])

    def test_empty_content_with_tool_calls_is_not_empty(self) -> None:
        doc = {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "inspect_house", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}
        p = self.server(lambda n, b, h: (200, doc))
        r = self.router(self.env(p))
        res = r.complete_messages([{"role": "user", "content": "x"}], purpose="chat", tools=[{"name": "inspect_house", "description": "d", "parameters": {"type": "object", "properties": {}}}])
        self.assertTrue(res.message["tool_calls"])
        self.assertEqual(p.hits, 1)

    def test_empty_reply_falls_back_to_fallback_endpoint(self) -> None:
        p = self.server(lambda n, b, h: (200, EMPTY_STOP_DOC))
        f = self.server(lambda n, b, h: (200, OK_DOC))
        r = self.router({**self.env(p), "AI_HOUSE_FALLBACK_API_KEY": "sk-f-bbbbbbbb", "AI_HOUSE_FALLBACK_BASE_URL": f.base_url})
        self.assertEqual(r.complete_text("s", "u", purpose="chat")[0], "好的")
        self.assertEqual(f.hits, 1)

    def test_user_sees_unavailable_notice_not_silent_demo(self) -> None:
        p = self.server(lambda n, b, h: (200, EMPTY_STOP_DOC))
        d = self._daemon({**self.env(p), "AI_HOUSE_OWNER_TOKEN": "owner-tail4a"})
        res = d.converse("private:sora", "你好呀", owner_speaker("房主"))
        txt = " ".join(str(x.get("text") or "") for x in res.get("replies") or [])
        self.assertIn("接口暂不可用", txt)

    def test_custom_llm_fn_returning_empty_is_also_flagged(self) -> None:
        d = self._daemon({"AI_HOUSE_OWNER_TOKEN": "owner-tail4a"}, llm_fn=lambda s, u: ("", 0))
        res = d.converse("private:sora", "你好呀", owner_speaker("房主"))
        txt = " ".join(str(x.get("text") or "") for x in res.get("replies") or [])
        self.assertIn("接口暂不可用", txt)


# --------------------------------------------------------------------------------------------- 4. key leak
class KeyOriginTests(Base):
    PRIM = "https://primary.example"
    RES = "https://resident.example"
    TASK = "https://task.example"
    FB = "https://fallback.example"

    def build(self, *, res=(False, False), task=(False, False), fb=(False, False)) -> Dict[str, str]:
        env = {"AI_HOUSE_API_KEY": "sk-PRIM-0000000", "AI_HOUSE_BASE_URL": self.PRIM, "AI_HOUSE_MODEL": "m"}
        if res[0]:
            env["AI_HOUSE_ACTOR_BASE_URL_SORA"] = self.RES
        if res[1]:
            env["AI_HOUSE_ACTOR_API_KEY_SORA"] = "sk-RES-00000000"
        if task[0]:
            env["AI_HOUSE_CHAT_BASE_URL"] = self.TASK
        if task[1]:
            env["AI_HOUSE_CHAT_API_KEY"] = "sk-TASK-0000000"
        if fb[0]:
            env["AI_HOUSE_FALLBACK_BASE_URL"] = self.FB
        if fb[1]:
            env["AI_HOUSE_FALLBACK_API_KEY"] = "sk-FB-000000000"
        return env

    def sent(self, env: Dict[str, str], *, purpose: str = "chat", actor: str = "sora") -> List[Tuple[str, str]]:
        r = self.router(env)
        return [(urlparse(ep.base_url).scheme + "://" + urlparse(ep.base_url).netloc, ep.api_key)
                for ep, _ in r.resolve_endpoints(purpose=purpose, actor=actor)]

    # which layer's key is allowed to go to which origin: a key only ever goes to its own layer's address,
    # and a layer without an address of its own is addressed at the primary origin.
    def own_origins(self, env: Dict[str, str]) -> Dict[str, str]:
        return {
            "sk-PRIM-0000000": self.PRIM,
            "sk-RES-00000000": env.get("AI_HOUSE_ACTOR_BASE_URL_SORA") or self.PRIM,
            "sk-TASK-0000000": env.get("AI_HOUSE_CHAT_BASE_URL") or self.PRIM,
            "sk-FB-000000000": env.get("AI_HOUSE_FALLBACK_BASE_URL") or self.PRIM,
        }

    def test_exhaustive_layer_matrix_every_key_only_reaches_its_own_origin(self) -> None:
        flags = [(False, False), (True, False), (False, True), (True, True)]
        n = 0
        for res, task, fb in itertools.product(flags, flags, flags):
            env = self.build(res=res, task=task, fb=fb)
            own = self.own_origins(env)
            for origin, key in self.sent(env):
                n += 1
                self.assertEqual(origin, own[key], f"key {key} sent to {origin}; res={res} task={task} fb={fb}")
        self.assertGreater(n, 60)

    def test_table_fallback_key_only_never_reaches_resident_or_task_url(self) -> None:
        cases = [
            # (label, kwargs, expected [(origin, key)] in order)
            ("fb key only + resident url", dict(res=(True, False), fb=(False, True)), [(self.PRIM, "sk-FB-000000000")]),
            ("fb key only + task url", dict(task=(True, False), fb=(False, True)), [(self.PRIM, "sk-FB-000000000")]),
            ("fb key only + resident url+key", dict(res=(True, True), fb=(False, True)),
             [(self.RES, "sk-RES-00000000"), (self.PRIM, "sk-FB-000000000")]),
            ("fb url+key + resident url only", dict(res=(True, False), fb=(True, True)), [(self.FB, "sk-FB-000000000")]),
            ("fb url, no fb key, resident url", dict(res=(True, False), fb=(True, False)), []),
            ("fb key only, nothing else", dict(fb=(False, True)), [(self.PRIM, "sk-PRIM-0000000"), (self.PRIM, "sk-FB-000000000")]),
            ("resident url only", dict(res=(True, False)), []),
            ("task url only", dict(task=(True, False)), []),
            ("resident key only (addressed at primary)", dict(res=(False, True)), [(self.PRIM, "sk-RES-00000000")]),
            ("task key + resident url", dict(res=(True, False), task=(False, True)), []),
        ]
        for label, kw, want in cases:
            with self.subTest(label):
                self.assertEqual(self.sent(self.build(**kw)), want)

    def test_memory_extract_purpose_matrix(self) -> None:
        env = {"AI_HOUSE_API_KEY": "sk-PRIM-0000000", "AI_HOUSE_BASE_URL": self.PRIM, "AI_HOUSE_MODEL": "m",
               "AI_HOUSE_MEMORY_BASE_URL": self.TASK, "AI_HOUSE_FALLBACK_API_KEY": "sk-FB-000000000"}
        self.assertEqual(self.sent(env, purpose="memory_extract", actor=""), [(self.PRIM, "sk-FB-000000000")])
        env["AI_HOUSE_MEMORY_API_KEY"] = "sk-TASK-0000000"
        self.assertEqual(self.sent(env, purpose="memory_extract", actor=""),
                         [(self.TASK, "sk-TASK-0000000"), (self.PRIM, "sk-FB-000000000")])

    def test_wire_level_fallback_key_never_reaches_custom_resident_server(self) -> None:
        custom = self.server()
        prim = self.server()
        r = self.router({"AI_HOUSE_API_KEY": "sk-PRIM-0000000", "AI_HOUSE_BASE_URL": prim.base_url, "AI_HOUSE_MODEL": "m",
                         "AI_HOUSE_ACTOR_BASE_URL_SORA": custom.base_url, "AI_HOUSE_FALLBACK_API_KEY": "sk-FB-000000000"})
        r.complete_text("s", "u", purpose="chat", actor="sora")
        self.assertEqual(custom.hits, 0, "custom resident server must not receive any request/key")
        self.assertEqual(prim.auth, ["Bearer sk-FB-000000000"])


# --------------------------------------------------------------------------------------------- 5. config issue
class ConfigIssueTests(Base):
    def test_resident_url_without_key_is_visible_everywhere(self) -> None:
        custom = self.server()
        prim = self.server()
        env = {"AI_HOUSE_API_KEY": "sk-PRIM-0000000", "AI_HOUSE_BASE_URL": prim.base_url, "AI_HOUSE_MODEL": "m",
               "AI_HOUSE_ACTOR_BASE_URL_SORA": custom.base_url}
        r = self.router(env)
        with self.assertLogs("ai_house.llm", level="WARNING") as lg:
            eps = r.resolve_endpoints(purpose="chat", actor="sora")
        self.assertEqual(eps, [], "primary key must not be sent to the custom address")
        self.assertTrue(any("sora" in m and "api_key" in m.lower() for m in lg.output), lg.output)
        self.assertNotIn("sk-PRIM-0000000", "\n".join(lg.output))

        view = r.status_view()
        issues = view["config_issues"]
        self.assertTrue(any(i["actor"] == "sora" and "key" in i["reason"].lower() for i in issues), issues)
        blob = json.dumps(view)
        self.assertNotIn("sk-PRIM-0000000", blob)

        # the usage endpoint shows it, and a call attempt leaves a failed ledger row instead of vanishing
        with self.assertRaises(RuntimeError):
            r.complete_text("s", "u", purpose="chat", actor="sora")
        summ = r.get_usage_summary(period="all")
        self.assertTrue(summ["config_issues"])
        self.assertTrue(any(x["ok"] == 0 and x["error_code"] == "CONFIG_NO_KEY" and x["actor"] == "sora" for x in summ["recent"]), summ["recent"])
        self.assertEqual(custom.hits + prim.hits, 0)

    def test_task_url_without_key_is_reported(self) -> None:
        env = {"AI_HOUSE_API_KEY": "sk-PRIM-0000000", "AI_HOUSE_BASE_URL": "https://primary.example", "AI_HOUSE_MODEL": "m",
               "AI_HOUSE_DIARY_BASE_URL": "http://127.0.0.1:9"}
        r = self.router(env)
        issues = r.status_view()["config_issues"]
        self.assertTrue(any(i["purpose"] == "diary" for i in issues), issues)
        self.assertFalse(any(i["purpose"] == "chat" for i in issues), issues)

    def test_healthy_config_has_no_issues(self) -> None:
        r = self.router({"AI_HOUSE_API_KEY": "sk-PRIM-0000000", "AI_HOUSE_BASE_URL": "https://primary.example"})
        self.assertEqual(r.status_view()["config_issues"], [])

    def test_user_sees_config_error_in_chat_reply(self) -> None:
        custom = self.server()
        prim = self.server()
        d = self._daemon({"AI_HOUSE_OWNER_TOKEN": "owner-tail4a", "AI_HOUSE_API_KEY": "sk-PRIM-0000000",
                                         "AI_HOUSE_BASE_URL": prim.base_url, "AI_HOUSE_ACTOR_BASE_URL_SORA": custom.base_url})
        res = d.converse("private:sora", "你好呀", owner_speaker("房主"))
        txt = " ".join(str(x.get("text") or "") for x in res.get("replies") or [])
        self.assertIn("接口暂不可用", txt)
        self.assertIn("api_key", txt.lower())
        self.assertNotIn("sk-PRIM-0000000", txt)


# --------------------------------------------------------------------------------------------- 6. bad port
class BadPortTests(Base):
    def test_origin_tuple_does_not_raise(self) -> None:
        # unparsable origins are a distinguished "invalid" value (None) that the resolver never treats as a match
        self.assertIsNone(ModelRouter._origin_tuple("https://x.example:abc"))
        self.assertIsNone(ModelRouter._origin_tuple("https://y.example:abc"))
        self.assertEqual(ModelRouter._origin_tuple("https://x.example"), ("https", "x.example", 443))

    def test_resident_bad_port_is_config_error_not_crash(self) -> None:
        r = self.router({"AI_HOUSE_API_KEY": "sk-PRIM-0000000", "AI_HOUSE_BASE_URL": "https://primary.example",
                         "AI_HOUSE_ACTOR_BASE_URL_SORA": "https://x.example:abc", "AI_HOUSE_ACTOR_API_KEY_SORA": "sk-RES-00000000"})
        self.assertEqual(r.resolve_endpoints(purpose="chat", actor="sora"), [])
        view = r.status_view()
        self.assertTrue(any("sora" == i["actor"] for i in view["config_issues"]), view["config_issues"])
        self.assertNotIn("sk-RES-00000000", json.dumps(view))
        with self.assertRaises(RuntimeError):
            r.complete_text("s", "u", purpose="chat", actor="sora")

    def test_primary_bad_port_does_not_raise_value_error(self) -> None:
        r = self.router({"AI_HOUSE_API_KEY": "sk-PRIM-0000000", "AI_HOUSE_BASE_URL": "http://localhost:abc"})
        try:
            r.complete_text("s", "u", purpose="chat")
        except ValueError as exc:  # pragma: no cover - the failure mode being fixed
            self.fail(f"ValueError escaped: {exc}")
        except Exception:
            pass

    def test_update_overrides_rejects_bad_port(self) -> None:
        r = self.router({})
        with self.assertRaises(ValueError):
            r.update_overrides({"resident_models": {"sora": {"base_url": "https://x.example:abc", "api_key": "sk-" + "x" * 10}}}, persist=False)


# --------------------------------------------------------------------------------------------- 7. breaker
class BreakerTests(Base):
    T0 = 1_700_000_000.0

    def make(self, handler: Callable[..., Tuple[Any, Any]], **env: str) -> Tuple[ModelRouter, FakeServer, FakeClock]:
        p = self.server(handler)
        clock = FakeClock(self.T0)
        e = {"AI_HOUSE_API_KEY": "sk-p-aaaaaaaa", "AI_HOUSE_BASE_URL": p.base_url, "AI_HOUSE_MODEL": "m",
             "AI_HOUSE_CHAT_MAX_RETRIES": "0", **env}
        return self.router(e, clock=clock), p, clock

    def call(self, r: ModelRouter, purpose: str = "chat") -> Optional[BaseException]:
        try:
            r.complete_text("s", "u", purpose=purpose)
            return None
        except Exception as exc:  # noqa: BLE001
            return exc

    def test_failures_are_counted_in_a_time_window(self) -> None:
        r, p, clock = self.make(lambda n, b, h: (503, {"error": {"message": "x"}}))
        self.call(r)
        self.call(r)
        clock.advance(3 * 86400)
        self.call(r)  # 3rd failure, but days after the first two
        before = p.hits
        self.call(r)
        self.assertEqual(p.hits, before + 1, "stale failures must not count: breaker must still be closed")
        # three failures inside the window do open it
        self.call(r)
        self.call(r)
        before = p.hits
        exc = self.call(r)
        self.assertEqual(p.hits, before)
        self.assertEqual(getattr(exc, "detail", ""), "circuit_open")

    def test_half_open_lets_exactly_one_probe_through(self) -> None:
        state = {"mode": "503"}

        def h(n: int, b: Dict[str, Any], hd: Any) -> Tuple[Any, Any]:
            if state["mode"] == "503":
                return 503, {"error": {"message": "x"}}
            time.sleep(1.0)
            return 200, OK_DOC

        r, p, clock = self.make(h)
        for _ in range(3):
            self.call(r)
        self.assertEqual(getattr(self.call(r), "detail", ""), "circuit_open")
        clock.advance(31)
        state["mode"] = "slow_ok"
        base = p.hits
        results: List[Optional[BaseException]] = []
        ths = [threading.Thread(target=lambda: results.append(self.call(r))) for _ in range(5)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        self.assertEqual(p.hits - base, 1, "only one probe request may reach the recovering endpoint")
        self.assertEqual(len([x for x in results if x is None]), 1)
        self.assertEqual(self.call(r), None, "after a successful probe the breaker is closed again")

    def test_failed_probe_reopens_immediately(self) -> None:
        r, p, clock = self.make(lambda n, b, h: (503, {"error": {"message": "x"}}))
        for _ in range(3):
            self.call(r)
        clock.advance(31)
        base = p.hits
        self.call(r)  # probe fails
        self.assertEqual(p.hits - base, 1)
        self.call(r)
        self.assertEqual(p.hits - base, 1, "a failed probe must reopen the breaker, not let traffic through")

    def test_probe_slot_is_released_after_non_transient_error(self) -> None:
        state = {"s": 503}
        r, p, clock = self.make(lambda n, b, h: (state["s"], {"error": {"message": "x"}}))
        for _ in range(3):
            self.call(r)
        clock.advance(31)
        state["s"] = 401  # endpoint answers, but not with a success or a breaker-class failure
        self.call(r)
        base = p.hits
        self.call(r)
        self.assertEqual(p.hits - base, 1, "a stuck probe flag would block the endpoint forever")

    def test_ping_calls_do_not_touch_breaker_state(self) -> None:
        r, p, clock = self.make(lambda n, b, h: (503, {"error": {"message": "x"}}))
        for _ in range(5):
            self.call(r, purpose="ping")
        self.assertEqual(p.hits, 5, "ping must always reach the endpoint")
        self.assertEqual(r._breaker_state, {})
        base = p.hits
        self.call(r, purpose="chat")
        self.assertEqual(p.hits - base, 1, "normal traffic is not blocked by failed connectivity tests")

    def test_ping_bypasses_open_breaker_and_does_not_close_it(self) -> None:
        state = {"s": 503}
        r, p, clock = self.make(lambda n, b, h: (state["s"], OK_DOC if state["s"] == 200 else {"error": {"message": "x"}}))
        for _ in range(3):
            self.call(r)
        state["s"] = 200
        base = p.hits
        self.assertIsNone(self.call(r, purpose="ping"))
        self.assertEqual(p.hits - base, 1)
        self.assertEqual(getattr(self.call(r, purpose="chat"), "detail", ""), "circuit_open", "a ping success must not close the chat breaker")


if __name__ == "__main__":
    unittest.main()
