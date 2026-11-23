"""Three-state regression tests for Stage 3.1: vision_understand plugin (`unconfigured`, `misconfigured`, `configured`)."""
from __future__ import annotations

import base64
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
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
TINY_DATA_URI = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode("ascii")


def _http_json(url: str, *, token: str = "", body: Any = None) -> tuple[int, dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
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
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8") or "{}")


class _MockVisionAndMediaServer:
    """Loopback server serving a test image and an OpenAI-compatible vision endpoint."""

    def __init__(self) -> None:
        self.fail_mode = False
        self.secret_leak_sentinel = "sk-vision-secret-9988"
        self.seen_requests: list[dict[str, Any]] = []
        parent = self

        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/sample.png":
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(TINY_PNG)))
                    self.end_headers()
                    self.wfile.write(TINY_PNG)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                parent.seen_requests.append(payload)
                if parent.fail_mode:
                    body = json.dumps({
                        "error": {"message": f"upstream failed with {parent.secret_leak_sentinel}"}
                    }).encode("utf-8")
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({
                    "choices": [{
                        "message": {
                            "content": "桌上放着一杯刚泡好的热抹茶拿铁和摊开的笔记本。"
                        }
                    }],
                    "usage": {"total_tokens": 42},
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.port = self._srv.server_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._th = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._th.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._th.join(timeout=2.0)


class _SimpleWSServer:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._conns: list[Any] = []
        self._srv = serve(self._handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._srv.socket.getsockname()[1]}"
        self._th = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._th.start()

    def _handler(self, ws: Any) -> None:
        self._conns.append(ws)
        self.connected.set()
        for raw in ws:
            if self._stop.is_set():
                return
            frame = json.loads(raw)
            self.frames.append(frame)
            ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 1}, "echo": frame.get("echo")}))

    def push(self, ev: dict[str, Any]) -> None:
        self._conns[-1].send(json.dumps(ev, ensure_ascii=False))

    def close(self) -> None:
        self._stop.set()
        self._srv.shutdown()
        self._th.join(timeout=2.0)


class Stage3VisionTests(unittest.TestCase):
    def test_vision_three_states_and_ssrf_guard(self) -> None:
        mock_srv = _MockVisionAndMediaServer()
        self.addCleanup(mock_srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        owner_tok = "st3-vision-owner-01"
        t0 = dt.datetime(2026, 11, 23, 14, 0, tzinfo=JST).timestamp()
        seen_prompts: list[str] = []

        def fake_chat_llm(_sys: str, user: str) -> tuple[str, int]:
            seen_prompts.append(user)
            return "嗯嗯，我都听着呢！", 12

        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_OWNER_TOKEN": owner_tok},
            llm_fn=fake_chat_llm,
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

        # 0) Default disabled: chat with image attachment works uninterrupted
        r_def = d.converse(
            "private:sora",
            "看看这张桌面的照片",
            owner_speaker("房主"),
            attachments={"images": [TINY_DATA_URI]},
        )
        self.assertEqual(r_def["replies"][0]["text"], "嗯嗯，我都听着呢！")
        self.assertNotIn("附带图片画面参考", seen_prompts[-1])

        # 1) State UNCONFIGURED: enable plugin without api_key -> degrades quietly, chat still works
        d.update_plugin_settings({
            "plugin_id": "vision_understand",
            "enabled": True,
            "values": {"base_url": mock_srv.base_url, "api_key": ""},
        })
        st_u, res_u = _http_json(
            f"{base}/api/plugins/vision_understand/describe",
            token=owner_tok,
            body={"source": TINY_DATA_URI, "prompt": "桌子上有什么"},
        )
        self.assertEqual(st_u, 200)
        self.assertFalse(res_u["ok"])
        self.assertEqual(res_u["status"], "unconfigured")
        r_unconf = d.converse(
            "private:sora",
            "无密钥看图也别卡住",
            owner_speaker("房主"),
            attachments={"images": [TINY_DATA_URI]},
        )
        self.assertEqual(r_unconf["replies"][0]["text"], "嗯嗯，我都听着呢！")

        # 2) State MISCONFIGURED / FAILING: SSRF blocked, invalid base_url, and HTTP 502 with secret redaction
        d.update_plugin_settings({
            "plugin_id": "vision_understand",
            "enabled": True,
            "values": {
                "base_url": mock_srv.base_url,
                "api_key": mock_srv.secret_leak_sentinel,
                "allow_loopback_media": False,
            },
        })
        st_ssrf, res_ssrf = _http_json(
            f"{base}/api/plugins/vision_understand/describe",
            token=owner_tok,
            body={"source": f"{mock_srv.base_url}/sample.png"},
        )
        self.assertEqual(st_ssrf, 200)
        self.assertFalse(res_ssrf["ok"])
        self.assertEqual(res_ssrf["status"], "invalid_source")

        mock_srv.fail_mode = True
        st_fail, res_fail = _http_json(
            f"{base}/api/plugins/vision_understand/describe",
            token=owner_tok,
            body={"source": TINY_DATA_URI},
        )
        self.assertEqual(st_fail, 200)
        self.assertFalse(res_fail["ok"])
        self.assertEqual(res_fail["status"], "upstream_error")
        self.assertNotIn(mock_srv.secret_leak_sentinel, res_fail["error"])
        r_fail_chat = d.converse(
            "private:sora",
            "上游看图报错时聊天照常进行",
            owner_speaker("房主"),
            attachments={"images": [TINY_DATA_URI]},
        )
        self.assertEqual(r_fail_chat["replies"][0]["text"], "嗯嗯，我都听着呢！")
        mock_srv.fail_mode = False

        # 3) State CONFIGURED: endpoint + allow_loopback_media -> HTTP describe + OneBot image segment work
        d.update_plugin_settings({
            "plugin_id": "vision_understand",
            "enabled": True,
            "values": {
                "base_url": mock_srv.base_url,
                "api_key": mock_srv.secret_leak_sentinel,
                "model": "gpt-4o-mini",
                "allow_loopback_media": True,
            },
        })
        st_ok, res_ok = _http_json(
            f"{base}/api/plugins/vision_understand/describe",
            token=owner_tok,
            body={"source": f"{mock_srv.base_url}/sample.png"},
        )
        self.assertEqual(st_ok, 200)
        self.assertTrue(res_ok["ok"], res_ok)
        self.assertIn("抹茶拿铁", res_ok["summary"])
        user_msg = mock_srv.seen_requests[-1]["messages"][1]
        self.assertEqual(user_msg["content"][1]["type"], "image_url")
        self.assertTrue(user_msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

        # End-to-end OneBot [image] segment enrichment
        ws_srv = _SimpleWSServer()
        self.addCleanup(ws_srv.close)
        ch = OneBotChannel(
            OneBotConfig(url=ws_srv.url, owners=frozenset({"8001"}), groups=frozenset({"9001"}), actor="sora")
        )
        ch.start(d)
        self.addCleanup(ch.stop)
        self.assertTrue(ws_srv.connected.wait(timeout=3.0))
        ws_srv.push({
            "post_type": "message",
            "message_type": "private",
            "self_id": "7001",
            "user_id": "8001",
            "message_id": "v-img-1",
            "sender": {"nickname": "房主"},
            "message": [
                {"type": "text", "data": {"text": "你看我拍了啥"}},
                {"type": "image", "data": {"url": f"{mock_srv.base_url}/sample.png"}},
            ],
        })
        deadline = threading.Event()
        for _ in range(100):
            if ws_srv.frames:
                break
            deadline.wait(0.05)
        self.assertEqual(len(ws_srv.frames), 1)
        self.assertIn("抹茶拿铁", seen_prompts[-1])


if __name__ == "__main__":
    unittest.main()
