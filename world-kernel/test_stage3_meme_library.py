"""Three-state regression tests for Stage 3.5: meme_library plugin (`unconfigured`, `misconfigured`, `configured`)."""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from websockets.sync.server import serve

from ai_house.builtin_plugins.onebot_ws import OneBotChannel, OneBotConfig
from ai_house.chat_identity import owner_speaker
from ai_house.daemon import HouseDaemon, make_http_server

JST = dt.timezone(dt.timedelta(hours=9))


def _http_json(url: str, *, token: str = "") -> tuple[int, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8") or "{}")


class _MemeWSServer:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._srv = serve(self._handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._srv.socket.getsockname()[1]}"
        self._th = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._th.start()

    def _handler(self, ws: Any) -> None:
        self.connected.set()
        for raw in ws:
            if self._stop.is_set():
                return
            frame = json.loads(raw)
            self.frames.append(frame)
            ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": len(self.frames)}, "echo": frame.get("echo")}))

    def close(self) -> None:
        self._stop.set()
        self._srv.shutdown()
        self._th.join(timeout=2.0)


class Stage3MemeLibraryTests(unittest.TestCase):
    def test_meme_library_three_states_license_and_traversal_guards(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        bad_pack = Path(tmp.name) / "bad_pack"
        bad_pack.mkdir(parents=True, exist_ok=True)
        (bad_pack / "secret.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        # Custom pack with: 1 valid attributed item, 1 unlicensed item, 1 path-traversal item
        (bad_pack / "manifest.json").write_text(
            json.dumps({
                "items": [
                    {
                        "id": "custom_ok",
                        "title": "自制咖啡杯",
                        "mood": "calm",
                        "tags": ["coffee"],
                        "file": "ok.svg",
                        "license": "CC-BY-4.0",
                        "author": "Community Artist",
                    },
                    {
                        "id": "no_license",
                        "title": "缺署名图",
                        "file": "ok.svg",
                    },
                    {
                        "id": "evil_traversal",
                        "title": "越界图",
                        "file": "../outside.svg",
                        "license": "CC0-1.0",
                        "author": "Tester",
                    },
                ]
            }),
            encoding="utf-8",
        )
        (bad_pack / "ok.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg' width='8' height='8'/>", encoding="utf-8")
        (Path(tmp.name) / "outside.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")

        owner_tok = "st3-meme-owner-01"
        t0 = dt.datetime(2026, 11, 23, 17, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            rt,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_OWNER_TOKEN": owner_tok, "AI_HOUSE_QUIET_HOURS": "off"},
            llm_fn=lambda _s, _u: ("晚安呀，好梦！", 8),
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        srv = make_http_server(d, host="127.0.0.1", port=0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(th.join, 2.0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"

        # 0) Default disabled: text chat completely normal
        self.assertNotIn("search_memes", d._active_tools)
        self.assertEqual(d.converse("private:sora", "晚安", owner_speaker("房主"))["replies"][0]["text"], "晚安呀，好梦！")

        # 1) State UNCONFIGURED: bundled off + no custom_dir -> unconfigured, chat unaffected
        d.update_plugin_settings({
            "plugin_id": "meme_library",
            "enabled": True,
            "values": {"include_bundled": False, "custom_dir": ""},
        })
        st_u, res_u = _http_json(f"{base}/api/plugins/meme_library/catalog", token=owner_tok)
        self.assertEqual(st_u, 200)
        self.assertFalse(res_u["ok"])
        self.assertEqual(res_u["status"], "unconfigured")

        # 2) State MISCONFIGURED: missing custom_dir & bundled off -> degrades cleanly, chat unaffected
        d.update_plugin_settings({
            "plugin_id": "meme_library",
            "enabled": True,
            "values": {"include_bundled": False, "custom_dir": str(Path(tmp.name) / "nonexistent")},
        })
        _, res_mis = _http_json(f"{base}/api/plugins/meme_library/catalog", token=owner_tok)
        self.assertEqual(res_mis["status"], "misconfigured")
        self.assertEqual(d.converse("private:sora", "表情包目录坏了照常聊天", owner_speaker("房主"))["replies"][0]["text"], "晚安呀，好梦！")

        # 3) State CONFIGURED: bundled CC0 pack + custom pack with strict attribution & traversal filtering
        d.update_plugin_settings({
            "plugin_id": "meme_library",
            "enabled": True,
            "values": {"include_bundled": True, "custom_dir": str(bad_pack), "require_license": True, "max_results": 12},
        })
        _, res_cat = _http_json(f"{base}/api/plugins/meme_library/catalog", token=owner_tok)
        self.assertTrue(res_cat["ok"], res_cat)
        ids = {it["id"] for it in res_cat["items"]}
        self.assertIn("cozy_tea", ids)
        self.assertIn("custom_ok", ids)
        self.assertNotIn("no_license", ids)
        self.assertNotIn("evil_traversal", ids)
        self.assertTrue(any("no_license" in w for w in res_cat["warnings"]))
        self.assertTrue(any("evil_traversal" in w for w in res_cat["warnings"]))

        _, res_item = _http_json(f"{base}/api/plugins/meme_library/item?id=cozy_tea", token=owner_tok)
        self.assertTrue(res_item["ok"], res_item)
        self.assertEqual(res_item["item"]["license"], "CC0-1.0")
        self.assertTrue(res_item["item"]["data_uri"].startswith("data:image/svg+xml;base64,"))

        # Deliver sticker over OneBot WS outbox with image_base64
        ws_srv = _MemeWSServer()
        self.addCleanup(ws_srv.close)
        ch = OneBotChannel(
            OneBotConfig(url=ws_srv.url, owners=frozenset({"8001"}), groups=frozenset({"9001"}), actor="sora")
        )
        d.channels = [ch]
        ch.start(d)
        self.addCleanup(ch.stop)
        self.assertTrue(ws_srv.connected.wait(timeout=3.0))

        did = d.enqueue_outbound(
            channel="onebot_v11",
            payload={
                "actor": "sora",
                "kind": "private",
                "to_identity": "user:owner",
                "text": "[表情：热茶慢饮]",
                "image_base64": res_item["item"]["image_base64"],
            },
        )
        fl = d.flush_outbox()
        self.assertEqual(fl["dispatched"][did], "sent")
        self.assertEqual(ws_srv.frames[-1]["params"]["message"][0]["type"], "image")


if __name__ == "__main__":
    unittest.main()
