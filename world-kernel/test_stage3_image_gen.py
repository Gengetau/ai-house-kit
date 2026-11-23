"""Three-state regression tests for Stage 3.3: image_gen plugin with BYO engine contract."""
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

from ai_house.builtin_plugins.image_gen import register_image_engine, unregister_image_engine
from ai_house.chat_identity import owner_speaker
from ai_house.daemon import HouseDaemon, make_http_server

JST = dt.timezone(dt.timedelta(hours=9))
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
TINY_B64 = base64.b64encode(TINY_PNG).decode("ascii")


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


class _MockImageServer:
    def __init__(self) -> None:
        self.fail_mode = False
        self.secret_key = "sk-imggen-secret-6677"
        self.seen_byo: list[dict[str, Any]] = []
        self.seen_openai: list[dict[str, Any]] = []
        parent = self

        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                if parent.fail_mode:
                    body = json.dumps({"error": {"message": f"gpu oom key={parent.secret_key}"}}).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/byo/txt2img":
                    parent.seen_byo.append(payload)
                    body = json.dumps({"images": [TINY_B64]}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.endswith("/images/generations"):
                    parent.seen_openai.append(payload)
                    body = json.dumps({"data": [{"b64_json": TINY_B64}]}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.base_url = f"http://127.0.0.1:{self._srv.server_port}"
        self._th = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._th.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._th.join(timeout=2.0)


class Stage3ImageGenTests(unittest.TestCase):
    def test_image_gen_three_states_openai_byo_http_and_custom_python(self) -> None:
        mock_srv = _MockImageServer()
        self.addCleanup(mock_srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        owner_tok = "st3-img-owner-01"
        t0 = dt.datetime(2026, 11, 23, 16, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_OWNER_TOKEN": owner_tok},
            llm_fn=lambda _s, _u: ("今天客厅阳光很好哦！", 8),
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

        d.memory.add("sora", "private", "房主的隐私备忘：「金库小盒子密码」藏在书房床底", kind="fact", source="chat", now=t0)

        # 0) Default disabled -> text chat completely unaffected
        self.assertNotIn("generate_image", d._active_tools)
        r_off = d.converse("private:sora", "你好呀", owner_speaker("房主"))
        self.assertEqual(r_off["replies"][0]["text"], "今天客厅阳光很好哦！")

        # 1) State UNCONFIGURED: openai engine without api_key -> unconfigured, chat unaffected
        d.update_plugin_settings({
            "plugin_id": "image_gen",
            "enabled": True,
            "values": {"engine": "openai", "base_url": mock_srv.base_url, "api_key": ""},
        })
        st_u, res_u = _http_json(
            f"{base}/api/plugins/image_gen/generate",
            token=owner_tok,
            body={"prompt": "午后窗边的热茶"},
        )
        self.assertEqual(st_u, 200)
        self.assertFalse(res_u["ok"])
        self.assertEqual(res_u["status"], "unconfigured")

        # 2) State MISCONFIGURED / FAILING + PRIVACY GUARD
        d.update_plugin_settings({
            "plugin_id": "image_gen",
            "enabled": True,
            "values": {"engine": "byo_http", "base_url": "bad-scheme", "api_key": mock_srv.secret_key},
        })
        _, res_bad = _http_json(f"{base}/api/plugins/image_gen/generate", token=owner_tok, body={"prompt": "小猫"})
        self.assertEqual(res_bad["status"], "misconfigured")

        d.update_plugin_settings({
            "plugin_id": "image_gen",
            "enabled": True,
            "values": {"engine": "openai", "base_url": mock_srv.base_url, "api_key": mock_srv.secret_key},
        })
        mock_srv.fail_mode = True
        _, res_500 = _http_json(f"{base}/api/plugins/image_gen/generate", token=owner_tok, body={"prompt": "晚霞"})
        self.assertEqual(res_500["status"], "upstream_error")
        self.assertNotIn(mock_srv.secret_key, res_500["error"])
        r_fail_chat = d.converse("private:sora", "生图失败时也能聊", owner_speaker("房主"))
        self.assertEqual(r_fail_chat["replies"][0]["text"], "今天客厅阳光很好哦！")
        mock_srv.fail_mode = False

        # Group prompt leaking private memory MUST be blocked before reaching upstream server
        before_cnt = len(mock_srv.seen_openai)
        _, res_leak = _http_json(
            f"{base}/api/plugins/image_gen/generate",
            token=owner_tok,
            body={"prompt": "画一个金库小盒子密码", "scope": "group", "actor": "sora"},
        )
        self.assertEqual(res_leak["status"], "blocked")
        self.assertEqual(len(mock_srv.seen_openai), before_cnt)

        # 3a) State CONFIGURED (openai)
        _, res_oai = _http_json(
            f"{base}/api/plugins/image_gen/generate",
            token=owner_tok,
            body={"prompt": "合租屋温暖客厅像素插画", "scope": "world"},
        )
        self.assertTrue(res_oai["ok"], res_oai)
        self.assertTrue(res_oai["media_rel"].startswith("media/generated/"))
        self.assertTrue((Path(tmp.name) / res_oai["media_rel"]).is_file())

        # 3b) State CONFIGURED (byo_http custom engine HTTP JSON contract)
        d.update_plugin_settings({
            "plugin_id": "image_gen",
            "enabled": True,
            "values": {
                "engine": "byo_http",
                "base_url": f"{mock_srv.base_url}/byo/txt2img",
                "model": "local-flux",
                "size": "512x512",
            },
        })
        _, res_byo = _http_json(
            f"{base}/api/plugins/image_gen/generate",
            token=owner_tok,
            body={"prompt": "雨后樱花丘街道", "negative_prompt": "blurry", "seed": 42, "actor": "sora"},
        )
        self.assertTrue(res_byo["ok"], res_byo)
        self.assertEqual(mock_srv.seen_byo[-1]["seed"], 42)
        self.assertEqual(mock_srv.seen_byo[-1]["negative_prompt"], "blurry")
        self.assertEqual(mock_srv.seen_byo[-1]["size"], "512x512")

        # 3c) State CONFIGURED (custom_python in-process engine hook)
        seen_py: list[dict[str, Any]] = []
        svg_bytes = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8"/></svg>'
        register_image_engine("studio-py", lambda spec: (seen_py.append(spec) or svg_bytes))
        self.addCleanup(unregister_image_engine, "studio-py")
        d.update_plugin_settings({
            "plugin_id": "image_gen",
            "enabled": True,
            "values": {"engine": "custom_python", "model": "studio-py"},
        })
        _, res_py = _http_json(
            f"{base}/api/plugins/image_gen/generate",
            token=owner_tok,
            body={"prompt": "手绘风抹茶松饼", "actor": "rin"},
        )
        self.assertTrue(res_py["ok"], res_py)
        self.assertEqual(res_py["mime"], "image/svg+xml")
        self.assertTrue(res_py["media_rel"].endswith(".svg"))
        self.assertEqual(seen_py[-1]["actor"], "rin")


if __name__ == "__main__":
    unittest.main()
