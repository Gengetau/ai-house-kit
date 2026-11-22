"""End-to-end integration tests for ai-house-kit v0.2 turnkey studio, custom WorldPacks,
4-axis Plugin SDK, built-in MCP server, and Qingkong City (晴空都) R0 + Federation linkage.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.daemon import HouseDaemon, make_http_server
from ai_house.plugin_api import PluginRegistry
from worldkernel import city_bridge, city_projection, materials


class TurnkeyV02Tests(unittest.TestCase):
    def test_custom_3_person_worldpack_runs_full_daemon_and_projections(self):
        """Replacing the 6-person cast with the 3-person indie-studio-3 pack works end-to-end with zero code edits."""
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        pack_dir = ROOT / "packs" / "indie-studio-3"
        daemon = HouseDaemon(
            runtime_dir=Path(tmp) / "rt",
            pack_dir=pack_dir,
            packs_root=ROOT / "packs",
            virtual_clock=True,
        )
        self.addCleanup(daemon.close)
        st = daemon.get_state()
        self.assertEqual(st["roster"], ["sora", "rin", "kuro"])
        self.assertEqual(st["residents"]["sora"]["name"], "苍空")
        self.assertEqual(st["residents"]["rin"]["name"], "凛音")
        self.assertEqual(st["residents"]["kuro"]["name"], "黑羽")

        # Fast-forward 45 minutes on virtual clock and verify L0 actions + projections stay healthy
        st2 = daemon.fast_forward(45)
        self.assertNotEqual(st["now_jst"], st2["now_jst"])
        for aid in ("sora", "rin", "kuro"):
            self.assertIn("reality", st2["residents"][aid])

        # Group chat & private chat record promises into IntentsLite / owner_chat_queue
        chat_res = daemon.send_chat("private:sora", "今晚大家一起吃咖喱饭吧！")
        self.assertTrue(chat_res["ok"])
        self.assertEqual(len(chat_res["replies"]), 1)
        self.assertEqual(chat_res["replies"][0]["actor"], "sora")

    def test_http_studio_rest_endpoints_and_mcp_server(self):
        """HTTP server serves Web Studio HTML, REST endpoints, avatar walk-in, restock, and JSON-RPC MCP tools."""
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        daemon = HouseDaemon(
            runtime_dir=Path(tmp) / "rt",
            packs_root=ROOT / "packs",
            virtual_clock=True,
        )
        self.addCleanup(daemon.close)
        srv = make_http_server(daemon, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        base = f"http://127.0.0.1:{port}"

        # 1) Web Studio HTML
        html = urllib.request.urlopen(base + "/").read().decode("utf-8")
        self.assertIn("AI House Studio", html)

        # 2) Restock kitchen & walk owner avatar into living room
        def post(path, doc):
            req = urllib.request.Request(
                base + path,
                data=json.dumps(doc).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            return json.loads(urllib.request.urlopen(req).read().decode("utf-8"))

        r_stock = post("/api/stock", {"item": "cookie", "qty": 3})
        self.assertTrue(r_stock["ok"])
        r_enter = post("/api/avatar", {"op": "enter", "location": "living"})
        self.assertTrue(r_enter["ok"])
        self.assertEqual(r_enter["state"]["owner_presence"]["location"], "living")

        r_say = post("/api/avatar", {"op": "say", "text": "大家下午好！"})
        self.assertTrue(r_say["ok"])

        # 3) Built-in MCP server (`POST /mcp`)
        mcp_init = post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(mcp_init["result"]["serverInfo"]["name"], "ai-house-kit")
        mcp_tools = post("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tool_names = {x["name"] for x in mcp_tools["result"]["tools"]}
        self.assertIn("get_house_state", tool_names)
        self.assertIn("chat_with_resident", tool_names)

        mcp_call = post("/mcp", {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "chat_with_resident", "arguments": {"channel": "private:nell", "text": "奈尔你在忙什么呢？"}},
        })
        self.assertTrue(mcp_call["result"]["structuredContent"]["ok"])

    def test_qingkong_city_r0_labels_and_mappings_v3(self):
        """Qingkong City (晴空都) R0 labels, entrance scrubbing and mappings v3 (the public-city plugin is in test_turnkey_v021_city.py)."""
        # 1) Entrance scrubbing & area labels cover hiyorizaka / ryokogai / kumominato
        scrubbed = city_projection.redact_text("刚走到 hiyorizaka:ai-house:entrance 准备进门")
        self.assertNotIn("hiyorizaka:ai-house:entrance", scrubbed)
        self.assertEqual(city_projection._area_label("ryokogai:kissa-tomoshibi"), "棱光街")
        self.assertEqual(city_projection._area_label("kumominato:muginohi"), "云港")

        # 2) Mappings v3 apparel_bundle + city action normalization
        self.assertEqual(city_bridge.inbound_kernel_item("apparel_bundle"), ("wardrobe_asset", "*"))
        acts = city_bridge.normalize_city_actions([
            {"type": "travel", "to": "ryokogai:shiorido"},
            {"type": "say", "text": "这本文库本批注真有意思"},
        ])
        self.assertEqual(len(acts), 2)


if __name__ == "__main__":
    unittest.main()
