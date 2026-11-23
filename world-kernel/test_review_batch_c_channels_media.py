"""PR-C tests: SendGate direct & proactive hardening, OneBot spool durability, Outbox fault recovery & rich media (C1-C10)."""
from __future__ import annotations

import contextlib
import datetime as dt
import http.server
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from websockets.sync.server import serve  # noqa: E402

from ai_house.builtin_plugins.onebot_ws import OneBotChannel, OneBotConfig  # noqa: E402
from ai_house.channel_spool import ChannelSpool, split_bubbles  # noqa: E402
from ai_house.chat_identity import member_speaker, owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, JST, make_http_server  # noqa: E402
from worldkernel.outbox import BlockedDelivery, Dispatcher, enqueue_outbound  # noqa: E402

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc````\x00\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _wait_until(pred: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _http_raw(url: str, *, token: str = "") -> Tuple[int, str, bytes]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, str(resp.headers.get("Content-Type") or ""), resp.read()
    except urllib.error.HTTPError as err:
        raw = err.read()
        err.close()
        return err.code, str(err.headers.get("Content-Type") or ""), raw


class _MockRichMediaOpenAIServer:
    """Mock OpenAI chat+image+tts server that triggers meme, image, and voice tools."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", errors="replace")
                outer.calls.append(self.path)
                if self.path.endswith("/images/generations"):
                    import base64

                    doc = {"data": [{"b64_json": base64.b64encode(TINY_PNG).decode("ascii")}]}
                    data = json.dumps(doc).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path.endswith("/audio/speech"):
                    wav = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(wav)))
                    self.end_headers()
                    self.wfile.write(wav)
                    return

                body = json.loads(raw or "{}")
                msgs = body.get("messages") or []
                tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                if body.get("tools") and not tool_msgs:
                    doc = {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {"id": "tc_m", "type": "function", "function": {"name": "send_meme", "arguments": json.dumps({"meme_id": "cozy_tea"})}},
                                    {"id": "tc_i", "type": "function", "function": {"name": "generate_image", "arguments": json.dumps({"prompt": "warm teacup"})}},
                                    {"id": "tc_v", "type": "function", "function": {"name": "synthesize_voice", "arguments": json.dumps({"text": "早安"})}},
                                ],
                            }
                        }],
                        "usage": {"total_tokens": 30},
                    }
                else:
                    doc = {
                        "choices": [{
                            "message": {"role": "assistant", "content": "给你发了贴纸、插画和语音消息哦！"}
                        }],
                        "usage": {"total_tokens": 20},
                    }
                out = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.srv.server_port}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class _FakeOneBotServer:
    def __init__(self) -> None:
        self.actions: List[Dict[str, Any]] = []
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._server = serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        self._th = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._th.start()

    def _handler(self, ws: Any) -> None:
        self.connected.set()
        for raw in ws:
            if self._stop.is_set():
                return
            frame = json.loads(raw)
            self.actions.append(frame)
            ws.send(json.dumps({
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": 500 + len(self.actions)},
                "echo": frame.get("echo"),
            }))

    def close(self) -> None:
        self._stop.set()
        self._server.shutdown()
        self._th.join(timeout=2.0)


class ReviewBatchCChannelsMediaTests(unittest.TestCase):
    def test_c1_webchat_target_and_direct_onebot_sendgate(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t0 = dt.datetime(2026, 11, 22, 14, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_OWNER_TOKEN": "owner-tok-c1"},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        # C1 (#318): Explicit webchat group target preserved & conflicting targets rejected
        sc, tgt, norm = d.send_gate.resolve_destination(
            "webchat", None, {"kind": "group", "target": "qq-group:9001", "actor": "sora", "text": "hello"}, is_webchat=True
        )
        self.assertEqual((sc, tgt, norm["target_channel"]), ("group", "qq-group:9001", "qq-group:9001"))
        with self.assertRaises(BlockedDelivery):
            d.send_gate.resolve_destination(
                "webchat", None, {"kind": "group", "target": "qq-group:9001", "target_channel": "qq-group:9002", "text": "hi"}, is_webchat=True
            )

        # C1: Direct OneBot _send_bubbles invokes SendGate & blocks private leak or non-whitelisted group
        d.memory.add("sora", "private", "书柜第三层藏着蓝色金箔纪念邮票册", kind="fact", source="chat", now=t0)
        ws_calls: List[Any] = []
        ch = OneBotChannel(OneBotConfig(url="ws://127.0.0.1:1", owners=frozenset({"8001"}), groups=frozenset({"9001"}), actor="sora"))
        ch._host = d
        ch._spool = ChannelSpool(tmp.name)
        self.addCleanup(ch._spool.close)
        ch._call_detailed = lambda action, params: (ws_calls.append((action, params)) or True, "ok", "1", True)  # type: ignore[method-assign]

        sid_leak = ch._spool.enqueue_event("onebot_v11", {"message": "x"}, message_id="m-leak", session_key="group:9001", kind="group", target="9001", uid="8002")
        assert sid_leak is not None
        ok_leak = ch._send_bubbles("group", "9001", ["其实书柜第三层藏着蓝色金箔纪念邮票册"], spool_ids=[sid_leak])
        self.assertFalse(ok_leak)
        self.assertEqual(ws_calls, [])
        self.assertGreaterEqual(ch.stats["blocked"], 1)
        self.assertEqual(ch._spool.list_rows()[0]["state"], "blocked")

        # Non-whitelisted group also blocked before WS wire
        ok_bad_grp = ch._send_bubbles("group", "99999", ["正常文本"])
        self.assertFalse(ok_bad_grp)
        self.assertEqual(ws_calls, [])

    def test_c2_c3_proactive_target_mute_ttl_and_post_send_quota(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t0 = dt.datetime(2026, 11, 22, 14, 0, tzinfo=JST).timestamp()
        llm_calls = 0

        def counting_llm(_s: str, _u: str) -> Tuple[str, int]:
            nonlocal llm_calls
            llm_calls += 1
            return "我在客厅泡了红茶哦", 10

        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_OWNER_TOKEN": "owner-secret-xyz", "ONEBOT_GROUPS": "9001,9002"},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
            llm_fn=counting_llm,
        )
        self.addCleanup(d.close)
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute("UPDATE reality SET availability='free', doing='喝茶' WHERE actor='sora'")
                wc.execute("INSERT INTO script(ts, actors, text, source, created) VALUES(?, ?, ?, 'kernel', ?)", (t0 + 10, '["sora"]', "苍空坐在窗边晒太阳", t0 + 10))
        d.proactive.anchor(d.world_db, now=t0)

        # C2: OneBot group with 2 whitelist groups and no target_group_id skips BEFORE LLM
        res_missing = d.proactive.scan(d, cfg={"target_channel": "onebot_v11", "target_scope": "group", "window": "09:00-22:30"}, now=t0 + 20)
        self.assertFalse(res_missing["triggered"])
        self.assertEqual(res_missing["reason"], "missing_target_group")
        self.assertEqual(llm_calls, 0)

        # C3: Muted group skips BEFORE LLM
        d.group_behavior.mute("qq-group:9001", minutes=10, now=t0 + 25)
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute("INSERT INTO script(ts, actors, text, source, created) VALUES(?, ?, ?, 'kernel', ?)", (t0 + 30, '["sora"]', "苍空整理书架", t0 + 30))
        res_muted = d.proactive.scan(
            d, cfg={"target_channel": "onebot_v11", "target_scope": "group", "target_group_id": "9001", "window": "09:00-22:30"}, now=t0 + 35
        )
        self.assertFalse(res_muted["triggered"])
        self.assertEqual(res_muted["reason"], "group_muted")
        self.assertEqual(llm_calls, 0)

        # C2: Blocked outbound delivery does NOT deduct daily proactive quota
        d.group_behavior.unmute("qq-group:9001")
        d.memory.add("sora", "private", "阳台上藏着蓝色星光银杏标本匣", kind="fact", source="chat", now=t0)
        d.update_plugin_settings({"plugin_id": "proactive_talk", "enabled": True})
        # Mock enqueue_outbound / flush_outbox returning blocked to verify post-send quota preservation
        d.proactive.anchor(d.world_db, now=t0 + 40)
        orig_flush = d.flush_outbox
        d.flush_outbox = lambda **_kw: {"ok": True, "dispatched": {"d-blk": "blocked"}}  # type: ignore[method-assign]
        orig_enq = d.enqueue_outbound
        d.enqueue_outbound = lambda **_kw: "d-blk"  # type: ignore[method-assign]
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute("INSERT INTO script(ts, actors, text, source, created) VALUES(?, ?, ?, 'kernel', ?)", (t0 + 50, '["sora"]', "苍空浇花", t0 + 50))
        d.chat_agent._llm_fn = counting_llm
        res_blk = d.proactive.scan(d, cfg={"target_channel": "webchat", "target_scope": "group", "house_daily_cap": 1, "window": "09:00-22:30"}, now=t0 + 55)
        self.assertEqual(res_blk["reason"], "outbox_blocked")
        self.assertEqual(d.proactive.status_view(now=t0 + 55)["house_count"], 0)
        d.flush_outbox = orig_flush  # type: ignore[method-assign]
        d.enqueue_outbound = orig_enq  # type: ignore[method-assign]

        # C2: Proactive outbox item > 1800s old is blocked with expired_ttl
        d.clock.t = t0 + 2000.0
        did_old = d.enqueue_outbound(
            channel="webchat",
            payload={"actor": "sora", "kind": "group", "scope": "group", "text": "过期的问候", "source": "proactive", "created_ts": t0},
        )
        res_old = d.flush_outbox()
        self.assertEqual(res_old["dispatched"][did_old], "blocked")

    def test_c4_c5_c6_c7_c10_spool_bubbles_dedup_prune_and_prewire_retry(self) -> None:
        # C6: <sep>, code fence & URL preservation, and overflow folding (no truncated drop)
        code_msg = "第一段\n\n```python\nprint(1)\n\nprint(2)\n```\n\n查看 https://example.com/a/very/long/path/that/should/never/break\n\n第四段\n\n第五段"
        parts = split_bubbles(code_msg, max_part_chars=25, max_parts=3)
        self.assertEqual(len(parts), 3)
        joined = "\n".join(parts)
        self.assertIn("```python\nprint(1)\n\nprint(2)\n```", joined)
        self.assertIn("https://example.com/a/very/long/path/that/should/never/break", joined)
        self.assertIn("第四段", joined)
        self.assertIn("第五段", joined)
        self.assertEqual(split_bubbles("甲<sep>乙"), ["甲", "乙"])

        # C5, C7, C10: Scoped dedup, reply_ready stale expiry, and raw_event_json cleanup / prune
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        now_box = [1000.0]
        sp = ChannelSpool(tmp.name, clock=lambda: now_box[0])
        self.addCleanup(sp.close)

        # C7: Same message_id in two different groups MUST both enqueue
        id_g1 = sp.enqueue_event("onebot_v11", {"secret": "big"}, message_id="1001", session_key="group:9001", kind="group", target="9001", uid="8002")
        id_g2 = sp.enqueue_event("onebot_v11", {"secret": "big"}, message_id="1001", session_key="group:9002", kind="group", target="9002", uid="8002")
        id_dup = sp.enqueue_event("onebot_v11", {"secret": "big"}, message_id="1001", session_key="group:9001", kind="group", target="9001", uid="8002")
        self.assertIsNotNone(id_g1)
        self.assertIsNotNone(id_g2)
        self.assertIsNone(id_dup)

        # C10: ack done clears raw_event_json and prune removes rows > 24h
        sp.ack([id_g1], state="done", now=1000.0)
        with contextlib.closing(sqlite3.connect(sp.db_path)) as c:
            raw_after_done = c.execute("SELECT raw_event_json FROM inbound_spool WHERE spool_id=?", (id_g1,)).fetchone()[0]
            self.assertEqual(raw_after_done, "{}")

        # C5: reply_ready row older than reply_stale_s (1800s) expires
        sp.save_reply_plan([id_g2], ["过期回复"], target="9002", kind="group", now=1010.0)
        now_box[0] = 3000.0
        rec = sp.recover_unfinished("onebot_v11", stale_s=120.0, reply_stale_s=1800.0, only_ready=True, now=3000.0)
        self.assertEqual(rec, [])
        self.assertEqual({r["spool_id"]: r["state"] for r in sp.list_rows()}[id_g2], "expired")

        # Prune deletes terminal rows older than 86400s
        deleted = sp.prune(keep_done_s=86400.0, now=100000.0)
        self.assertGreaterEqual(deleted, 2)
        self.assertEqual(sp.list_rows(), [])

        # C4: Pre-wire ConnectionError (left_wire=False) leaves reply_ready pending (NOT failed_wire) & clears _ws
        ch = OneBotChannel(OneBotConfig(url="ws://127.0.0.1:1", owners=frozenset({"8001"}), send_retries=0))
        ch._spool = sp
        class _DeadWS:
            def send(self, _txt: str) -> None:
                raise ConnectionError("broken pipe before send")
        ch._ws = _DeadWS()
        sid_retry = sp.enqueue_event("onebot_v11", {"x": 1}, message_id="m-prewire", session_key="private:8001", kind="private", target="8001", uid="8001")
        assert sid_retry is not None
        ok = ch._send_bubbles("private", "8001", ["断线重连后应补发"], spool_ids=[sid_retry])
        self.assertFalse(ok)
        self.assertIsNone(ch._ws)
        self.assertEqual({r["spool_id"]: r["state"] for r in sp.list_rows()}[sid_retry], "reply_ready")

    def test_c8_outbox_dispatcher_survives_unexpected_exception_and_continues(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t0 = dt.datetime(2026, 11, 22, 12, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(tmp.name, env={}, start_ts=t0, virtual_clock=True, allow_onboarding=False)
        self.addCleanup(d.close)

        class _BoomChannel:
            idempotent = False
            def send(self, _did: str, _payload: Dict[str, Any]) -> str:
                raise RuntimeError("unexpected JSON crash")
            def lookup(self, _did: str) -> None:
                return None

        class _OkChannel:
            idempotent = True
            def send(self, did: str, _payload: Dict[str, Any]) -> str:
                return f"ok-{did}"
            def lookup(self, _did: str) -> None:
                return None

        did_boom = enqueue_outbound(d.authority.store, channel="boom", payload={"text": "bad"})
        did_ok = enqueue_outbound(d.authority.store, channel="okch", payload={"text": "good"})
        disp = Dispatcher(d.authority.store, {"boom": _BoomChannel(), "okch": _OkChannel()})
        res = disp.run_once()
        self.assertEqual(res[did_boom], "unknown")
        self.assertEqual(res[did_ok], "sent")

    def test_c9_multimodal_outbound_media_webchat_and_onebot(self) -> None:
        mock_api = _MockRichMediaOpenAIServer()
        self.addCleanup(mock_api.close)
        ws_srv = _FakeOneBotServer()
        self.addCleanup(ws_srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        read_tok = "read-tok-c9-media-01"
        priv_tok = "priv-tok-c9-media-01"
        t0 = dt.datetime(2026, 11, 22, 15, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={
                "AI_HOUSE_OWNER_TOKEN": "owner-tok-c9-media-01",
                "AI_HOUSE_READ_TOKEN": read_tok,
                "AI_HOUSE_PRIVATE_TOKEN": priv_tok,
                "AI_HOUSE_API_KEY": "sk-mock-c9",
                "AI_HOUSE_BASE_URL": mock_api.base_url,
                "AI_HOUSE_MODEL": "mock-model",
                "ONEBOT_WS_URL": ws_srv.url,
                "ONEBOT_OWNER_QQ": "8001",
                "ONEBOT_GROUPS": "9001",
                "ONEBOT_ACTOR": "sora",
            },
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)
        for pid in ("meme_library", "image_gen", "voice_io"):
            d.update_plugin_settings({
                "plugin_id": pid,
                "enabled": True,
                "values": {"base_url": mock_api.base_url, "api_key": "sk-mock-c9"} if pid != "meme_library" else {},
            })

        # Web group conversation runs meme + image + tts tools -> attached in reply & persisted in chat_messages
        res = d.converse("group", "苍空来点贴纸画画和语音", owner_speaker("房主"), actor="sora")
        rep = res["replies"][0]
        media = rep.get("media") or []
        self.assertEqual([m["kind"] for m in media], ["sticker", "image", "audio"])
        # Sticker PNG rasterized for OneBot even though bundled sticker is SVG
        self.assertTrue(media[0]["data_uri"].startswith("data:image/svg+xml"))
        import base64
        self.assertTrue(base64.b64decode(media[0]["image_base64"]).startswith(b"\x89PNG\r\n\x1a\n"))

        hist = d.list_chat("group", limit=10)
        bot_row = [m for m in hist if m["sender"] == "sora"][-1]
        self.assertEqual([m["kind"] for m in bot_row["media"]], ["sticker", "image", "audio"])
        img_rel = next(m["media_rel"] for m in bot_row["media"] if m["kind"] == "image")
        aud_rel = next(m["media_rel"] for m in bot_row["media"] if m["kind"] == "audio")

        # HTTP /api/album/media enforces scope tier (read OK for group chat media, 403 for private media or traversal)
        srv = make_http_server(d, host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"

        st_img, ctype_i, body_i = _http_raw(f"{base}/api/album/media?path={img_rel}", token=read_tok)
        self.assertEqual(st_img, 200)
        self.assertEqual(ctype_i, "image/png")
        self.assertTrue(body_i.startswith(b"\x89PNG"))

        st_aud, ctype_a, body_a = _http_raw(f"{base}/api/album/media?path={aud_rel}", token=read_tok)
        self.assertEqual(st_aud, 200)
        self.assertEqual(ctype_a, "audio/wav")
        self.assertTrue(body_a.startswith(b"RIFF"))

        # Private generated file refused to read token, allowed to private token; path traversal always 403
        d.converse("private:sora", "私聊发张语音", owner_speaker("房主"))
        priv_hist = [m for m in d.list_chat("private:sora", limit=10) if m["sender"] == "sora"][-1]
        priv_aud_rel = next(m["media_rel"] for m in priv_hist["media"] if m["kind"] == "audio")
        self.assertEqual(_http_raw(f"{base}/api/album/media?path={priv_aud_rel}", token=read_tok)[0], 403)
        self.assertEqual(_http_raw(f"{base}/api/album/media?path={priv_aud_rel}", token=priv_tok)[0], 200)
        self.assertEqual(_http_raw(f"{base}/api/album/media?path=media/generated/group/../../settings.json", token=priv_tok)[0], 403)

        # OneBotChannel delivers both text bubble and image/record segments over WS
        ch = next(c for c in d.channels if isinstance(c, OneBotChannel))
        self.assertTrue(ws_srv.connected.wait(timeout=3.0))
        self.assertTrue(_wait_until(lambda: ch._ws is not None, timeout=3.0))
        ch.handle_event({
            "post_type": "message",
            "message_type": "private",
            "self_id": "7001",
            "user_id": "8001",
            "message_id": "m-rich-1",
            "sender": {"nickname": "老板"},
            "message": [{"type": "text", "data": {"text": "来个全套富媒体"}}],
        })
        self.assertTrue(_wait_until(lambda: len(ws_srv.actions) >= 4, timeout=4.0))
        seg_types = [seg["type"] for a in ws_srv.actions for seg in a["params"]["message"]]
        self.assertIn("text", seg_types)
        self.assertIn("image", seg_types)
        self.assertIn("record", seg_types)


if __name__ == "__main__":
    unittest.main()
