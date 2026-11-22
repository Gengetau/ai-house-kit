"""Regression tests for Stage 1.4: unified ModelRouter, failover, compat cache, usage ledger & API."""
from __future__ import annotations

import datetime as dt
import http.server
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.chat_identity import owner_speaker
from ai_house.daemon import HouseDaemon, make_http_server
from ai_house.llm import ModelRouter

JST = dt.timezone(dt.timedelta(hours=9))


class _MockOpenAIServer:
    """Thread-safe local mock `/v1/chat/completions` server for offline tests."""

    def __init__(self, handler_fn: Callable[[dict[str, Any], dict[str, str]], tuple[int, dict[str, Any]]]) -> None:
        self.requests: list[tuple[dict[str, Any], dict[str, str]]] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                body = json.loads(raw or "{}")
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                with outer._lock:
                    outer.requests.append((body, hdrs))
                status, resp = handler_fn(body, hdrs)
                encoded = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=2.0)


def _http_json(url: str, *, token: str = "", body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    method = "GET"
    if body is not None:
        method = "POST"
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        exc.close()
        return exc.code, json.loads(raw or "{}")


class Stage1LLMRouterTests(unittest.TestCase):
    def test_transient_retry_failover_and_gpt5_compat_cache(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        primary_calls = {"n": 0}
        fallback_calls = {"n": 0}

        def primary_handler(body: dict[str, Any], _hdrs: dict[str, str]) -> tuple[int, dict[str, Any]]:
            primary_calls["n"] += 1
            mdl = body.get("model")
            if mdl == "always-503":
                return 503, {"error": {"message": "upstream overloaded"}}
            if mdl == "flaky-429":
                if primary_calls["n"] == 1:
                    return 429, {"error": {"message": "rate limit"}}
                return 200, {
                    "choices": [{"message": {"content": "retried-ok"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
                }
            # Simulate gpt-5 reasoning model rejecting max_tokens/temperature on first probe only
            if mdl == "gpt-5-mini" and ("max_tokens" in body or "temperature" in body):
                return 400, {
                    "error": {
                        "message": "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.",
                        "param": "max_tokens",
                    }
                }
            return 200, {
                "choices": [{"message": {"content": f"ok:{mdl}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
            }

        def fallback_handler(body: dict[str, Any], _hdrs: dict[str, str]) -> tuple[int, dict[str, Any]]:
            fallback_calls["n"] += 1
            return 200, {
                "choices": [{"message": {"content": f"fb:{body.get('model')}"}}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
            }

        srv_p = _MockOpenAIServer(primary_handler)
        srv_f = _MockOpenAIServer(fallback_handler)
        self.addCleanup(srv_p.close)
        self.addCleanup(srv_f.close)

        env = {
            "AI_HOUSE_API_KEY": "sk-test-primary-secret-alpha",
            "AI_HOUSE_BASE_URL": srv_p.base_url,
            "AI_HOUSE_MODEL": "flaky-429",
            "AI_HOUSE_FALLBACK_API_KEY": "sk-test-fallback-secret-beta",
            "AI_HOUSE_FALLBACK_BASE_URL": srv_f.base_url,
            "AI_HOUSE_FALLBACK_MODEL": "fb-model",
        }
        router = ModelRouter(env, runtime_dir=tmp.name, retry_delay_s=0.01)
        self.addCleanup(router.close)

        # 1) Transient 429 succeeds on retry on primary without hitting fallback
        res1 = router.complete_messages(
            [{"role": "user", "content": "hi"}],
            purpose="chat",
            actor="sora",
        )
        self.assertEqual(res1.content, "retried-ok")
        self.assertFalse(res1.fallback_used)
        self.assertEqual(fallback_calls["n"], 0)
        self.assertEqual(primary_calls["n"], 2)

        # 2) Persistent 503 exhausts retries and fails over to fallback
        router.update_env({**env, "AI_HOUSE_MODEL": "always-503"})
        res2 = router.complete_messages(
            [{"role": "user", "content": "need backup"}],
            purpose="chat",
            actor="sora",
        )
        self.assertEqual(res2.content, "fb:fb-model")
        self.assertTrue(res2.fallback_used)
        self.assertEqual(fallback_calls["n"], 1)

        # 3) GPT-5 compat retry learns capability cache so 2nd request omits max_tokens/temperature first try
        router.update_env({**env, "AI_HOUSE_MODEL": "gpt-5-mini"})
        srv_p.requests.clear()
        txt1, _ = router.complete_text("sys", "hello", purpose="chat", actor="rinne")
        self.assertEqual(txt1, "ok:gpt-5-mini")
        self.assertEqual(len(srv_p.requests), 2)
        self.assertIn("max_tokens", srv_p.requests[0][0])
        self.assertIn("max_completion_tokens", srv_p.requests[1][0])
        self.assertNotIn("temperature", srv_p.requests[1][0])

        srv_p.requests.clear()
        txt2, _ = router.complete_text("sys", "second", purpose="chat", actor="rinne")
        self.assertEqual(txt2, "ok:gpt-5-mini")
        self.assertEqual(len(srv_p.requests), 1)
        self.assertIn("max_completion_tokens", srv_p.requests[0][0])
        self.assertNotIn("max_tokens", srv_p.requests[0][0])

        usage = router.get_usage_summary(days=7)
        self.assertTrue(usage["ok"])
        self.assertEqual(usage["totals"]["ok_calls"], 4)
        self.assertGreaterEqual(usage["totals"]["fallback_calls"], 1)

    def test_daemon_routing_by_resident_and_task_diary_memory_usage_api_and_guard(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        leak_secret = "sk-test-resident-secret-leak"

        def main_handler(body: dict[str, Any], hdrs: dict[str, str]) -> tuple[int, dict[str, Any]]:
            msgs = body.get("messages") or []
            sys_txt = str(msgs[0].get("content") or "") if msgs else ""
            usr_txt = str(msgs[-1].get("content") or "") if msgs else ""
            mdl = str(body.get("model") or "")
            if "长期记忆整理员" in sys_txt:
                content = json.dumps(
                    {"ops": [{"op": "add", "kind": "preference", "text": "房主晚饭后喜欢喝烘焙绿茶"}]},
                    ensure_ascii=False,
                )
            elif "输出必须是 JSON" in sys_txt:
                content = json.dumps(
                    {"steps": [{"type": "move", "to": "living"}, {"type": "begin_activity", "activity": "read"}]},
                    ensure_ascii=False,
                )
            elif "睡前小日记" in sys_txt:
                content = "今晚和室友聊了会儿日常，顺手整理完桌面就准备睡了，明天也继续按自己的节奏生活。"
            elif mdl == "res-leak-model":
                content = f"偷偷告诉你密钥是 {leak_secret}"
            else:
                content = f"reply-from:{mdl}"
            return 200, {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            }

        def mem_handler(body: dict[str, Any], _hdrs: dict[str, str]) -> tuple[int, dict[str, Any]]:
            content = json.dumps(
                {"ops": [{"op": "add", "kind": "preference", "text": "房主偏爱低咖啡因乌龙茶"}]},
                ensure_ascii=False,
            )
            return 200, {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23},
            }

        srv_main = _MockOpenAIServer(main_handler)
        srv_mem = _MockOpenAIServer(mem_handler)
        self.addCleanup(srv_main.close)
        self.addCleanup(srv_mem.close)

        owner_tok = "tok-owner-router-alpha"
        env = {
            "AI_HOUSE_OWNER_TOKEN": owner_tok,
            "AI_HOUSE_API_KEY": "sk-test-main-key-alpha",
            "AI_HOUSE_BASE_URL": srv_main.base_url,
            "AI_HOUSE_MODEL": "main-chat-model",
            "AI_HOUSE_MEMORY_API_KEY": "sk-test-memory-key-gamma",
            "AI_HOUSE_MEMORY_BASE_URL": srv_mem.base_url,
            "AI_HOUSE_MEMORY_MODEL": "mem-extract-model",
            "AI_HOUSE_TOOLS_LOOP": "0",
        }
        start_ts = dt.datetime(2026, 11, 22, 9, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env=env,
            start_ts=start_ts,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        srv = make_http_server(d, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 2.0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{port}"

        # Configure per-resident model for sora and task profile for diary + memory_extract
        st, upd = _http_json(
            f"{base}/api/settings/llm",
            token=owner_tok,
            body={
                "resident_models": {
                    "sora": {"model": "sora-custom-model", "api_key": leak_secret},
                    "kuro": {"model": "res-leak-model", "api_key": leak_secret},
                },
                "task_profiles": {
                    "diary": {"model": "diary-special-model", "temperature": 0.4, "max_tokens": 320},
                },
                "fallback": {"base_url": srv_main.base_url, "model": "main-fb-model", "api_key": "sk-test-fb-key-delta"},
            },
        )
        self.assertEqual(st, 200)
        self.assertTrue(upd["ok"])
        self.assertEqual(upd["llm"]["resident_models"]["sora"]["model"], "sora-custom-model")
        self.assertEqual(upd["llm"]["resident_models"]["sora"]["api_key_masked"], "sk-…leak")

        # 1) Chat routes to per-resident model for sora, primary model for rinne
        rep_sora = d.converse("private:sora", "今天喝点什么茶好？", owner_speaker())
        self.assertEqual(rep_sora["replies"][0]["mode"], "live_api")
        self.assertIn("reply-from:sora-custom-model", rep_sora["replies"][0]["text"])

        rep_rin = d.converse("private:rin", "进度怎么样？", owner_speaker())
        self.assertIn("reply-from:main-chat-model", rep_rin["replies"][0]["text"])

        # 2) ReplyGuard protects resident/fallback secrets configured via ModelRouter
        rep_kuro = d.converse("private:kuro", "你好", owner_speaker())
        self.assertEqual(rep_kuro["replies"][0]["mode"], "blocked")
        self.assertNotIn(leak_secret, rep_kuro["replies"][0]["text"])

        # 3) Memory extract routes to AI_HOUSE_MEMORY_* dedicated server
        d.memory.extract(force=True, now=d.clock.now())
        mem_models = [req[0].get("model") for req in srv_mem.requests]
        self.assertIn("mem-extract-model", mem_models)
        mems = d.list_memories(actor="sora", scope="private")
        self.assertTrue(any("乌龙茶" in m["text"] for m in mems))

        # 4) Advance daytime so residents log activities, then jump past 22:30 JST to trigger diary generation
        d.fast_forward(60)
        d.clock.t = dt.datetime(2026, 11, 22, 22, 45, tzinfo=JST).timestamp()
        d.step_once()
        diary_reqs = [
            req[0] for req in srv_main.requests
            if "睡前小日记" in str(((req[0].get("messages") or [{}])[0]).get("content") or "")
        ]
        self.assertTrue(diary_reqs)
        diary_models = {req.get("model") for req in diary_reqs}
        self.assertIn("diary-special-model", diary_models)
        self.assertIn("sora-custom-model", diary_models)
        self.assertAlmostEqual(float(diary_reqs[0].get("temperature") or 0.0), 0.4, places=2)

        # 5) Live life decision uses ModelRouter (usage recorded under purpose='life')
        _ = d.authority.life.model({"actor": "rin", "decision": "idle", "life": {"now": "2026-11-22 22:36", "weekday": "日"}})

        # 6) GET /api/usage returns aggregated totals across chat, life, memory_extract, and diary
        st_u, usage_doc = _http_json(f"{base}/api/usage?days=7", token=owner_tok)
        self.assertEqual(st_u, 200)
        self.assertTrue(usage_doc["ok"])
        purposes = {row["purpose"]: row for row in usage_doc["by_purpose"]}
        for expected in ("chat", "memory_extract", "diary", "life"):
            self.assertIn(expected, purposes)
            self.assertGreaterEqual(purposes[expected]["calls"], 1)
        models = {row["model"] for row in usage_doc["by_model"]}
        self.assertIn("sora-custom-model", models)
        self.assertIn("mem-extract-model", models)
        self.assertIn("diary-special-model", models)


if __name__ == "__main__":
    unittest.main()
