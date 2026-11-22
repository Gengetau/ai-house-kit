"""Regression tests for Stage 1.3: plugin HTTP routes, periodic tasks, context tools & example 3P package."""
from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.builtin_plugins.dice_tabletop import PLUGIN_DEF as DICE_DEF
from ai_house.builtin_plugins.sticker_pack import PLUGIN_DEF as STICKER_DEF
from ai_house.chat_identity import owner_speaker
from ai_house.daemon import HouseDaemon, make_http_server
from ai_house.plugin_api import (
    PluginDefinition,
    PluginManifest,
    PluginRoute,
    PluginTask,
    ToolContext,
    invoke_tool_handler,
)


def _http_json(url: str, *, token: str = "", body: dict | None = None) -> tuple[int, dict]:
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


class _FakeEP:
    def __init__(self, name: str, obj):
        self.name = name
        self._obj = obj

    def load(self):
        return self._obj


class Stage1PluginHooksTests(unittest.TestCase):
    def test_example_hello_tea_plugin_discovered_and_end_to_end(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ep_site = Path(tmp.name) / "site"
        dist_info = ep_site / "hello_tea_plugin-0.1.0.dist-info"
        dist_info.mkdir(parents=True)
        (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: ai-house-hello-tea-plugin\nVersion: 0.1.0\n", encoding="utf-8")
        (dist_info / "entry_points.txt").write_text("[ai_house.plugins]\nhello_tea = hello_tea_plugin:PLUGIN_DEF\n", encoding="utf-8")

        example_src = str(ROOT / "examples" / "hello-tea-plugin")
        ep_site_str = str(ep_site)
        old_path = list(sys.path)
        sys.path.insert(0, example_src)
        sys.path.insert(0, ep_site_str)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), old_path))

        import hello_tea_plugin

        hello_tea_plugin.reset_example_state()
        self.addCleanup(hello_tea_plugin.reset_example_state)

        owner_tok = "tok-owner-hooks-123456"
        d = HouseDaemon(Path(tmp.name) / "rt", virtual_clock=True, env={"AI_HOUSE_OWNER_TOKEN": owner_tok})
        self.addCleanup(d.close)
        srv = make_http_server(d, host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"

        # Disabled by default -> HTTP route 404, tool hidden
        self.assertNotIn("recommend_tea", d.get_state()["tools"])
        code_disabled, _ = _http_json(f"{base}/api/plugins/hello_tea/status", token=owner_tok)
        self.assertEqual(code_disabled, 404)

        # Enable & configure through the generic Settings API with zero kit hardcoding
        code_save, _ = _http_json(
            f"{base}/api/settings/plugins",
            token=owner_tok,
            body={
                "plugin_id": "hello_tea",
                "enabled": True,
                "values": {"tea_of_day": "蜜桃白茶", "secret_code": "teaSecretCode888"},
            },
        )
        self.assertEqual(code_save, 200)
        self.assertIn("recommend_tea", d.get_state()["tools"])

        # Unauthenticated plugin route check -> 401; authenticated -> 200 with no secret leakage
        code_unauth, _ = _http_json(f"{base}/api/plugins/hello_tea/status")
        self.assertEqual(code_unauth, 401)
        code_ok, st_doc = _http_json(f"{base}/api/plugins/hello_tea/status", token=owner_tok)
        self.assertEqual(code_ok, 200)
        self.assertEqual(st_doc["tea_of_day"], "蜜桃白茶")
        self.assertTrue(st_doc["secret_configured"])
        self.assertNotIn("teaSecretCode888", json.dumps(st_doc, ensure_ascii=False))

        # Fast-forward triggers periodic brew_tick task repeatedly once interval (600s) elapses
        d.fast_forward(15)
        _, st_after_1 = _http_json(f"{base}/api/plugins/hello_tea/status", token=owner_tok)
        self.assertGreaterEqual(st_after_1["brew_count"], 1)
        c1 = st_after_1["brew_count"]
        d.fast_forward(15)
        _, st_after_2 = _http_json(f"{base}/api/plugins/hello_tea/status", token=owner_tok)
        self.assertGreater(st_after_2["brew_count"], c1)

        # Mock LLM calling both third-party `recommend_tea` and migrated `browse_bookshelf`
        observed_tool_results = []

        class _MockToolChat(http.server.BaseHTTPRequestHandler):
            turn = 0

            def log_message(self, fmt, *args):
                return

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                for msg in body.get("messages") or []:
                    if msg.get("role") == "tool":
                        observed_tool_results.append(json.loads(msg["content"]))
                type(self).turn += 1
                if type(self).turn == 1:
                    doc = {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {"id": "c1", "type": "function", "function": {"name": "recommend_tea", "arguments": "{}"}},
                                    {"id": "c2", "type": "function", "function": {"name": "browse_bookshelf", "arguments": "{}"}},
                                ],
                            }
                        }],
                        "usage": {"total_tokens": 20},
                    }
                else:
                    doc = {
                        "choices": [{"message": {"role": "assistant", "content": "已经为你泡好蜜桃白茶并翻看了客厅书架。"}}],
                        "usage": {"total_tokens": 18},
                    }
                raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        llm_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockToolChat)
        threading.Thread(target=llm_srv.serve_forever, daemon=True).start()
        self.addCleanup(llm_srv.server_close)
        self.addCleanup(llm_srv.shutdown)

        d.update_llm_settings({
            "base_url": f"http://127.0.0.1:{llm_srv.server_port}",
            "api_key": "sk-hooksToolTestKey123",
            "model": "mock-hooks",
        })
        chat_res = d.send_chat("private:nell", "帮忙泡茶顺便看看书架")
        self.assertEqual(chat_res["replies"][0]["mode"], "live_api")
        tea_out = next(r for r in observed_tool_results if "tea" in r)
        shelf_out = next(r for r in observed_tool_results if "notes" in r)
        self.assertEqual(tea_out["tea"], "蜜桃白茶")
        self.assertEqual(tea_out["actor"], "nell")
        self.assertEqual(tea_out["scope"], "private")
        self.assertEqual(tea_out["for_guest"], "房主")
        self.assertGreaterEqual(tea_out["brew_count"], 2)
        self.assertIsInstance(shelf_out["notes"], list)

    def test_post_route_tiers_failing_task_isolation_and_builtin_context_defaults(self):
        # Direct ToolContext verification on migrated built-ins
        ctx = ToolContext(
            actor="nell",
            scope="group",
            channel="group",
            speaker=owner_speaker("房主"),
            now=1000.0,
            plugin_config={"default_sides": 20, "default_mood": "cozy"},
        )
        dice_res = invoke_tool_handler(DICE_DEF.tools[0], {"seed_hint": "s1"}, ctx)
        self.assertEqual(dice_res["sides"], 20)
        stk_res = invoke_tool_handler(STICKER_DEF.tools[0], {}, ctx)
        self.assertEqual(stk_res["mood"], "cozy")

        ok_runs = []

        def _post_echo(rctx):
            return {"ok": True, "echo": (rctx.body or {}).get("msg"), "plugin": rctx.plugin_id}

        def _broken_task(_tctx):
            raise RuntimeError("periodic boom")

        def _ok_task(_tctx):
            ok_runs.append(_tctx.now)

        plug_def = PluginDefinition(
            manifest=PluginManifest(plugin_id="hook_probe", name="Hook Probe", kind="bundle", default_enabled=True),
            routes=(PluginRoute(method="POST", subpath="/echo", handler=_post_echo, tier="write"),),
            tasks=(
                PluginTask(task_id="bad", interval_s=60.0, handler=_broken_task),
                PluginTask(task_id="good", interval_s=60.0, handler=_ok_task),
            ),
        )

        def _eps(group: str = ""):
            if group == "ai_house.plugins":
                return (_FakeEP("hook_probe", plug_def),)
            return () if group else {}

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        owner_tok = "tok-owner-probe-123456"
        read_tok = "tok-read-probe-123456"

        with mock.patch("importlib.metadata.entry_points", side_effect=_eps):
            d = HouseDaemon(
                Path(tmp.name) / "rt",
                virtual_clock=True,
                env={"AI_HOUSE_OWNER_TOKEN": owner_tok, "AI_HOUSE_READ_TOKEN": read_tok},
            )
            self.addCleanup(d.close)

            srv = make_http_server(d, host="127.0.0.1", port=0)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            base = f"http://127.0.0.1:{srv.server_port}"

            code_forbidden, _ = _http_json(f"{base}/api/plugins/hook_probe/echo", token=read_tok, body={"msg": "hi"})
            self.assertEqual(code_forbidden, 403)
            code_ok, post_doc = _http_json(f"{base}/api/plugins/hook_probe/echo", token=owner_tok, body={"msg": "hi"})
            self.assertEqual(code_ok, 200)
            self.assertEqual(post_doc["echo"], "hi")

            d.step_once()
            self.assertEqual(len(ok_runs), 1)
            errs = d.get_state()["plugins"]["load_errors"]
            self.assertTrue(any(e["plugin_id"] == "hook_probe" and "RuntimeError" in e["error"] for e in errs))


if __name__ == "__main__":
    unittest.main()
