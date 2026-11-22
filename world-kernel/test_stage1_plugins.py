"""Regression tests for Stage 1.1: plugin manifests, config schema, unified switches, isolation & 3P tools."""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house import cli
from ai_house.daemon import HouseDaemon
from ai_house.plugin_api import (
    ConfigField,
    PluginDefinition,
    PluginManifest,
    PluginRegistry,
    StudioProvider,
    ToolRegistration,
    is_plugin_enabled,
    resolve_plugin_config,
)


class _FakeEntryPoint:
    def __init__(self, name: str, loader):
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


class Stage1PluginManifestTests(unittest.TestCase):
    def test_builtin_manifests_and_config_schema_validation(self):
        reg = PluginRegistry().discover()
        expected = {"onebot_v11", "city_mcp_outing", "svg_wardrobe", "book_nook", "dice_tabletop", "sticker_pack"}
        self.assertTrue(expected.issubset(reg.definitions.keys()))
        self.assertEqual(reg.load_errors, [])

        for pid, defn in reg.definitions.items():
            self.assertLessEqual(len(defn.manifest.config_fields), 8, pid)
            schema = defn.manifest.to_schema_dict()
            self.assertEqual(schema["plugin_id"], pid)
            self.assertIsInstance(schema["missing_packages"], list)

        cfg = reg.resolve_config(
            "onebot_v11",
            env={"ONEBOT_MAX_PER_HOUR": "45", "ONEBOT_WS_URL": "ws://127.0.0.1:3001"},
        )
        self.assertEqual(cfg["max_per_hour"], 45)
        self.assertEqual(cfg["ws_url"], "ws://127.0.0.1:3001")

        f_int = ConfigField(key="n", label="Count", field_type="int", default=5, min_val=1, max_val=10)
        self.assertEqual(f_int.coerce("7"), 7)
        with self.assertRaises(ValueError):
            f_int.coerce("99")
        with self.assertRaises(ValueError):
            f_int.coerce("abc")

        f_sel = ConfigField(key="m", label="Mode", field_type="select", default="a", options=(("a", "A"), ("b", "B")))
        self.assertEqual(f_sel.coerce("b"), "b")
        with self.assertRaises(ValueError):
            f_sel.coerce("c")

        with self.assertRaises(ValueError):
            PluginManifest(
                plugin_id="too_many",
                name="Too Many Fields",
                config_fields=tuple(ConfigField(key=f"k{i}", label=f"K{i}") for i in range(9)),
            )

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["plugins", "--packs-root", str(ROOT / "packs")])
        self.assertEqual(rc, 0)
        cli_doc = json.loads(out.getvalue())
        self.assertIn("manifests", cli_doc)
        self.assertIn("load_errors", cli_doc)

    def test_unified_enablement_switches(self):
        reg = PluginRegistry().discover()
        dice = reg.definitions["dice_tabletop"]
        nook = reg.definitions["book_nook"]
        city = reg.definitions["city_mcp_outing"]

        self.assertTrue(is_plugin_enabled(dice, env={}))
        self.assertFalse(is_plugin_enabled(city, env={}))
        self.assertTrue(is_plugin_enabled(city, env={"CITY_FEDERATION_MCP_URL": "http://localhost:7700/mcp"}))
        self.assertFalse(is_plugin_enabled(dice, env={"AI_HOUSE_DISABLED_PLUGINS": "dice_tabletop"}))
        self.assertFalse(is_plugin_enabled(dice, env={"AI_HOUSE_PLUGIN_DICE_TABLETOP_ENABLED": "0"}))
        self.assertTrue(
            is_plugin_enabled(
                dice,
                env={"AI_HOUSE_PLUGIN_DICE_TABLETOP_ENABLED": "0"},
                stored_plugins={"dice_tabletop": {"enabled": True}},
            )
        )
        self.assertFalse(is_plugin_enabled(nook, env={"AI_HOUSE_BOOK_NOOK": "0"}))

        resolved = resolve_plugin_config(
            dice.manifest,
            env={"AI_HOUSE_DICE_SIDES": "20"},
            stored_values={"default_sides": 12},
        )
        self.assertEqual(resolved["default_sides"], 12)

    def test_faulty_third_party_plugin_is_isolated_without_crashing_daemon(self):
        def _boom_import():
            raise ImportError("missing optional vendor module")

        class _ExplodingProvider(StudioProvider):
            provider_id = "exploding_provider"

            def step(self, host):
                raise RuntimeError("provider step exploded")

        def _make_fake_eps(group: str = ""):
            mapping = {
                "ai_house.plugins": [
                    _FakeEntryPoint("broken_bundle", _boom_import),
                ],
                "ai_house.providers": [
                    _FakeEntryPoint("exploding_provider", lambda: _ExplodingProvider),
                ],
                "ai_house.extensions": [
                    _FakeEntryPoint("broken_ext", lambda: (lambda _life: 1 / 0)),
                ],
            }
            if group:
                return mapping.get(group, ())
            return mapping

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch("importlib.metadata.entry_points", side_effect=_make_fake_eps):
            d = HouseDaemon(
                Path(tmp.name) / "rt",
                virtual_clock=True,
                env={"AI_HOUSE_EXTENSIONS": "broken_ext"},
            )
            self.addCleanup(d.close)
            d.step_once()
            st = d.get_state()

        errs = st["plugins"]["load_errors"]
        err_ids = {e["plugin_id"] for e in errs}
        self.assertIn("broken_bundle", err_ids)
        self.assertIn("exploding_provider", err_ids)

    def test_third_party_tool_entry_point_discovered_and_callable_in_chat(self):
        calls = []

        def _weather_ping(city: str = "Tokyo") -> dict:
            calls.append(city)
            return {"ok": True, "city": city, "forecast": "clear skies"}

        tool_reg = ToolRegistration(
            spec={
                "name": "mock_weather_ping",
                "description": "Query offline mock weather by city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
            handler=_weather_ping,
        )

        def _make_tool_eps(group: str = ""):
            if group == "ai_house.tools":
                return (_FakeEntryPoint("mock_weather_tool", lambda: tool_reg),)
            return () if group else {}

        class _MockToolChatHandler(http.server.BaseHTTPRequestHandler):
            turn = 0

            def log_message(self, fmt, *args):
                return

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                _ = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                type(self).turn += 1
                if type(self).turn == 1:
                    doc = {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [{
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "mock_weather_ping", "arguments": '{"city":"Osaka"}'},
                                }],
                            }
                        }],
                        "usage": {"total_tokens": 15},
                    }
                else:
                    doc = {
                        "choices": [{"message": {"role": "assistant", "content": "大阪那边现在天气很晴朗！"}}],
                        "usage": {"total_tokens": 12},
                    }
                raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockToolChatHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch("importlib.metadata.entry_points", side_effect=_make_tool_eps):
            d = HouseDaemon(
                Path(tmp.name) / "rt",
                virtual_clock=True,
                env={
                    "AI_HOUSE_API_KEY": "sk-mocktooltest123456",
                    "AI_HOUSE_BASE_URL": f"http://127.0.0.1:{srv.server_port}",
                    "AI_HOUSE_MODEL": "mock-model",
                },
            )
            self.addCleanup(d.close)
            st = d.get_state()
            self.assertIn("mock_weather_ping", st["tools"])

            res = d.send_chat("private:nell", "帮我看看大阪天气")
            self.assertEqual(calls, ["Osaka"])
            self.assertEqual(res["replies"][0]["mode"], "live_api")
            self.assertIn("晴朗", res["replies"][0]["text"])


if __name__ == "__main__":
    unittest.main()
