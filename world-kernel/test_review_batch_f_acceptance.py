"""PR-F §4 acceptance tests: non-tautological 32h invariant negative probes & unmocked .dist-info tool discovery."""
from __future__ import annotations

import contextlib
import datetime as dt
import http.server
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog"), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.chat_identity import member_speaker, owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, JST  # noqa: E402
from ai_house.plugin_api import PluginHostProxy  # noqa: E402
from run_stage2_32h_live import verify_stage2_32h_invariants  # noqa: E402


class ReviewBatchFAcceptanceTests(unittest.TestCase):
    def test_verify_stage2_32h_invariants_negative_probes(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t0 = dt.datetime(2026, 11, 21, 9, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        # Seed allergy memo + open onion errand + evidence-completed errand baseline
        d.intents.enqueue_chat(
            "sora",
            "记住我对虾仁过敏",
            "记牢啦",
            where="private",
            intents=[{"text": "房主对虾仁过敏", "kind": "memo", "答应了的": ["sora"]}],
            now=t0,
        )
        d.intents.enqueue_chat("sora", "今天顺便帮我买 2 个洋葱回来", "好，我去买", where="group", now=t0)

        # Probe A: open errand + saw_sleep=False MUST fail both #1 and #2!
        fails_a = verify_stage2_32h_invariants(d, saw_sleep=False)
        self.assertTrue(any("问题#1失败" in f for f in fails_a))
        self.assertTrue(any("问题#2失败：洋葱采购约定" in f for f in fails_a))

        # Complete onion errand with valid evidence -> baseline passes with saw_sleep=True
        ev_at = t0 + 1800.0
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', 1, ?, ?)",
                    ("ev-buy-onion", ev_at, json.dumps({"kind": "purchased", "actor": "sora", "item": "onion", "qty": 2, "at": ev_at})),
                )
        d.intents.sweep(ev_at + 10.0)
        self.assertEqual(verify_stage2_32h_invariants(d, saw_sleep=True), [])

        # Probe B: leak in plain 'group' chat (not only 'qq-group:9001') MUST fail #4
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute(
                    "INSERT INTO chat_messages(channel, sender, sender_name, actor_target, text, badge, mode, created_at) "
                    "VALUES('group', 'sora', '苍空', 'owner', '听说有虾仁呀', '', 'live_api', ?)",
                    (ev_at + 20.0,),
                )
        fails_b = verify_stage2_32h_invariants(d, saw_sleep=True)
        self.assertTrue(any("问题#4失败" in f and "group" in f for f in fails_b))

        # Probe C: closed allergy memo MUST fail #2
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute("DELETE FROM chat_messages WHERE channel='group'")
                wc.execute("UPDATE house_intents SET state='completed' WHERE kind='memo'")
        fails_c = verify_stage2_32h_invariants(d, saw_sleep=True)
        self.assertTrue(any("问题#2失败" in f for f in fails_c))

    def test_real_dist_info_entry_points_tool_discovery_without_mocking_and_proxy_enforced(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        site_dir = Path(tmp.name) / "site"
        dist_info = site_dir / "tp_real_tool_pkg-0.1.0.dist-info"
        dist_info.mkdir(parents=True)
        (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: tp-real-tool-pkg\nVersion: 0.1.0\n", encoding="utf-8")
        (dist_info / "entry_points.txt").write_text("[ai_house.tools]\ntp_real_ping = tp_real_ep_mod:TOOL_REG\n", encoding="utf-8")

        (site_dir / "tp_real_ep_mod.py").write_text(
            "from ai_house.plugin_api import PluginHostProxy, ToolContext, ToolRegistration\n"
            "CALLS = []\n"
            "def _run(tag: str = '', ctx: ToolContext = None):\n"
            "    is_proxy = isinstance(ctx.daemon, PluginHostProxy) if ctx is not None else False\n"
            "    has_auth = hasattr(ctx.daemon, 'authority') if ctx is not None else True\n"
            "    CALLS.append({'tag': tag, 'is_proxy': is_proxy, 'has_auth': has_auth})\n"
            "    return {'pong': tag, 'is_proxy': is_proxy, 'has_auth': has_auth}\n"
            "TOOL_REG = ToolRegistration(\n"
            "    spec={'name': 'tp_real_ping', 'description': 'real ep tool', 'parameters': {'type': 'object', 'properties': {'tag': {'type': 'string'}}}},\n"
            "    handler=_run,\n"
            "    takes_context=True,\n"
            ")\n",
            encoding="utf-8",
        )

        old_path = list(sys.path)
        sys.path.insert(0, str(site_dir))
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), old_path))
        self.addCleanup(lambda: sys.modules.pop("tp_real_ep_mod", None))

        observed_tool_msgs: List[Dict[str, Any]] = []

        class _MockToolLLM(http.server.BaseHTTPRequestHandler):
            turn = 0

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                ln = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                for msg in body.get("messages") or []:
                    if msg.get("role") == "tool":
                        observed_tool_msgs.append(json.loads(msg["content"]))
                type(self).turn += 1
                if type(self).turn == 1:
                    doc = {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [{
                                    "id": "call_real_ep",
                                    "type": "function",
                                    "function": {"name": "tp_real_ping", "arguments": '{"tag":"tokyo"}'},
                                }],
                            }
                        }],
                        "usage": {"total_tokens": 12},
                    }
                else:
                    doc = {
                        "choices": [{"message": {"role": "assistant", "content": "真实 entry_points 工具已执行完毕。"}}],
                        "usage": {"total_tokens": 10},
                    }
                raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _MockToolLLM)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        # No mocking of importlib.metadata.entry_points — genuine .dist-info scan!
        d = HouseDaemon(
            Path(tmp.name) / "rt",
            virtual_clock=True,
            env={
                "AI_HOUSE_API_KEY": "sk-realep-test123",
                "AI_HOUSE_BASE_URL": f"http://127.0.0.1:{srv.server_port}",
                "AI_HOUSE_MODEL": "mock-real-ep",
            },
        )
        self.addCleanup(d.close)

        self.assertIn("tp_real_ping", d.get_state()["tools"])
        res = d.converse("private:nell", "运行真实插件工具", owner_speaker("房主"))
        self.assertEqual(res["replies"][0]["mode"], "live_api")
        import tp_real_ep_mod  # type: ignore

        self.assertEqual(len(tp_real_ep_mod.CALLS), 1)
        self.assertEqual(tp_real_ep_mod.CALLS[0], {"tag": "tokyo", "is_proxy": True, "has_auth": False})
        self.assertTrue(observed_tool_msgs and observed_tool_msgs[0]["is_proxy"] is True)


if __name__ == "__main__":
    unittest.main()
