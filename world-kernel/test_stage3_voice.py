"""Three-state regression tests for Stage 3.2: voice_io STT & TTS plugin."""
from __future__ import annotations

import base64
import datetime as dt
import http.server
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
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


def _make_sample_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x00" * 1600)  # 0.2s silent PCM
    return buf.getvalue()


SAMPLE_WAV = _make_sample_wav()
SAMPLE_WAV_DATA_URI = "data:audio/wav;base64," + base64.b64encode(SAMPLE_WAV).decode("ascii")


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


class _MockVoiceServer:
    def __init__(self) -> None:
        self.fail_mode = False
        self.secret_key = "sk-voice-secret-4455"
        self.stt_requests: list[bytes] = []
        self.tts_requests: list[dict[str, Any]] = []
        parent = self

        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/voice.wav":
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(SAMPLE_WAV)))
                    self.end_headers()
                    self.wfile.write(SAMPLE_WAV)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                if parent.fail_mode:
                    body = json.dumps({"error": {"message": f"voice down key={parent.secret_key}"}}).encode("utf-8")
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.endswith("/audio/transcriptions"):
                    parent.stt_requests.append(raw)
                    body = json.dumps({"text": "晚上记得买两颗卷心菜回来。"}, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.endswith("/audio/speech"):
                    parent.tts_requests.append(json.loads(raw.decode("utf-8") or "{}"))
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(SAMPLE_WAV)))
                    self.end_headers()
                    self.wfile.write(SAMPLE_WAV)
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


class _SimpleVoiceWSServer:
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
            ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": len(self.frames)}, "echo": frame.get("echo")}))

    def push(self, ev: dict[str, Any]) -> None:
        self._conns[-1].send(json.dumps(ev, ensure_ascii=False))

    def close(self) -> None:
        self._stop.set()
        self._srv.shutdown()
        self._th.join(timeout=2.0)


class Stage3VoiceTests(unittest.TestCase):
    def test_voice_three_states_privacy_guard_and_onebot_roundtrip(self) -> None:
        mock_srv = _MockVoiceServer()
        self.addCleanup(mock_srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        owner_tok = "st3-voice-owner-01"
        t0 = dt.datetime(2026, 11, 23, 15, 0, tzinfo=JST).timestamp()
        seen_prompts: list[str] = []

        def fake_chat_llm(_sys: str, user: str) -> tuple[str, int]:
            seen_prompts.append(user)
            return "收到语音啦，好嘞！", 10

        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_OWNER_TOKEN": owner_tok, "AI_HOUSE_QUIET_HOURS": "off"},
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

        # Seed private allergy canary in memory to ensure group TTS blocks leaks before calling API
        d.memory.add("sora", "private", "房主的隐私忌口：海胆过敏", kind="fact", source="chat", now=t0)

        # 0) Disabled by default: inbound record attachment never disrupts text chat
        r_off = d.converse(
            "private:sora",
            "[语音]",
            owner_speaker("房主"),
            attachments={"records": [SAMPLE_WAV_DATA_URI]},
        )
        self.assertEqual(r_off["replies"][0]["text"], "收到语音啦，好嘞！")
        self.assertNotIn("语音转写", seen_prompts[-1])

        # 1) UNCONFIGURED: enabled without api_key -> routes report unconfigured, chat unaffected
        d.update_plugin_settings({
            "plugin_id": "voice_io",
            "enabled": True,
            "values": {"base_url": mock_srv.base_url, "api_key": ""},
        })
        _, res_u1 = _http_json(f"{base}/api/plugins/voice_io/transcribe", token=owner_tok, body={"source": SAMPLE_WAV_DATA_URI})
        _, res_u2 = _http_json(f"{base}/api/plugins/voice_io/speak", token=owner_tok, body={"text": "测试朗读"})
        self.assertFalse(res_u1["ok"])
        self.assertEqual(res_u1["status"], "unconfigured")
        self.assertFalse(res_u2["ok"])
        self.assertEqual(res_u2["status"], "unconfigured")

        # 2) MISCONFIGURED / FAILING + PRIVACY LEAK BLOCK
        d.update_plugin_settings({
            "plugin_id": "voice_io",
            "enabled": True,
            "values": {"base_url": "not-a-url", "api_key": mock_srv.secret_key},
        })
        _, res_bad_url = _http_json(f"{base}/api/plugins/voice_io/transcribe", token=owner_tok, body={"source": SAMPLE_WAV_DATA_URI})
        self.assertEqual(res_bad_url["status"], "misconfigured")

        d.update_plugin_settings({
            "plugin_id": "voice_io",
            "enabled": True,
            "values": {"base_url": mock_srv.base_url, "api_key": mock_srv.secret_key, "allow_loopback_media": True},
        })
        mock_srv.fail_mode = True
        _, res_503 = _http_json(f"{base}/api/plugins/voice_io/speak", token=owner_tok, body={"text": "今天天气真好"})
        self.assertEqual(res_503["status"], "upstream_error")
        self.assertNotIn(mock_srv.secret_key, res_503["error"])
        r_fail_chat = d.converse(
            "private:sora",
            "[语音]",
            owner_speaker("房主"),
            attachments={"records": [SAMPLE_WAV_DATA_URI]},
        )
        self.assertEqual(r_fail_chat["replies"][0]["text"], "收到语音啦，好嘞！")
        mock_srv.fail_mode = False

        # Group TTS carrying private secret MUST be blocked BEFORE contacting upstream server
        before_tts = len(mock_srv.tts_requests)
        _, res_leak = _http_json(
            f"{base}/api/plugins/voice_io/speak",
            token=owner_tok,
            body={"text": "其实房主对海胆过敏", "scope": "group", "actor": "sora"},
        )
        self.assertEqual(res_leak["status"], "blocked")
        self.assertEqual(len(mock_srv.tts_requests), before_tts)

        # 3) CONFIGURED: STT + TTS + runtime/media/tts file + WAV metadata + OneBot [record] roundtrip
        _, res_stt = _http_json(
            f"{base}/api/plugins/voice_io/transcribe",
            token=owner_tok,
            body={"source": f"{mock_srv.base_url}/voice.wav", "language": "zh"},
        )
        self.assertTrue(res_stt["ok"], res_stt)
        self.assertIn("卷心菜", res_stt["text"])
        self.assertEqual(res_stt["audio_info"]["sample_rate"], 8000)

        _, res_tts = _http_json(
            f"{base}/api/plugins/voice_io/speak",
            token=owner_tok,
            body={"text": "晚饭准备做热乎乎的咖喱。", "voice": "nova", "scope": "private", "actor": "sora"},
        )
        self.assertTrue(res_tts["ok"], res_tts)
        self.assertTrue(res_tts["media_rel"].startswith("media/tts/"))
        self.assertTrue((Path(tmp.name) / res_tts["media_rel"]).is_file())
        self.assertEqual(res_tts["wav_info"]["channels"], 1)

        ws_srv = _SimpleVoiceWSServer()
        self.addCleanup(ws_srv.close)
        ch = OneBotChannel(
            OneBotConfig(url=ws_srv.url, owners=frozenset({"8001"}), groups=frozenset({"9001"}), actor="sora")
        )
        d.channels = [ch]
        ch.start(d)
        self.addCleanup(ch.stop)
        self.assertTrue(ws_srv.connected.wait(timeout=3.0))

        # Inbound [record] segment automatically transcribes before chat LLM
        ws_srv.push({
            "post_type": "message",
            "message_type": "private",
            "self_id": "7001",
            "user_id": "8001",
            "message_id": "rec-in-1",
            "sender": {"nickname": "房主"},
            "message": [{"type": "record", "data": {"url": f"{mock_srv.base_url}/voice.wav"}}],
        })
        ev_wait = threading.Event()
        for _ in range(100):
            if ws_srv.frames:
                break
            ev_wait.wait(0.05)
        self.assertEqual(len(ws_srv.frames), 1)
        self.assertIn("卷心菜", seen_prompts[-1])

        # Outbound with audio_base64 emits OneBot [record] segment
        did_rec = d.enqueue_outbound(
            channel="onebot_v11",
            payload={
                "actor": "sora",
                "kind": "private",
                "to_identity": "user:owner",
                "text": "给你留了一段语音",
                "audio_base64": res_tts["audio_base64"],
            },
        )
        fl = d.flush_outbox()
        self.assertEqual(fl["dispatched"][did_rec], "sent")
        self.assertEqual(ws_srv.frames[-1]["params"]["message"][0]["type"], "record")


if __name__ == "__main__":
    unittest.main()
