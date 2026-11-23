"""Regression tests for Stage 2.1: ChannelSpool durability, dedup, debounce, bubble split & restart replay."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from websockets.sync.server import serve

from ai_house.builtin_plugins.onebot_ws import OneBotChannel, OneBotConfig
from ai_house.channel_spool import ChannelSpool, split_bubbles
from ai_house.daemon import HouseDaemon


def _wait_until(pred: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class _ScriptableOneBotWSServer:
    """Loopback fake OneBot server capturing sent actions and simulating restarts."""

    def __init__(self) -> None:
        self.sent_frames: list[dict[str, Any]] = []
        self.close_after_acks: int = 0
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._conns: list[Any] = []
        self._server = serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self, ws: Any) -> None:
        self._conns.append(ws)
        self.connected.set()
        for raw in ws:
            if self._stop.is_set():
                return
            frame = json.loads(raw)
            self.sent_frames.append(frame)
            ws.send(json.dumps({
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": 200 + len(self.sent_frames)},
                "echo": frame.get("echo"),
            }))
            if self.close_after_acks > 0 and len(self.sent_frames) >= self.close_after_acks:
                self.close_after_acks = 0
                ws.close()
                return

    def push(self, payload: dict[str, Any]) -> None:
        ws = self._conns[-1]
        ws.send(json.dumps(payload, ensure_ascii=False))

    def close(self) -> None:
        self._stop.set()
        self._server.shutdown()
        self._thread.join(timeout=2.0)


class Stage2ChannelReliabilityTests(unittest.TestCase):
    def test_split_bubbles_paragraphs_natural_breaks_and_cap(self) -> None:
        # Paragraph split preserves short paragraphs as separate bubbles
        self.assertEqual(split_bubbles("第一段\n\n第二段"), ["第一段", "第二段"])

        # Long text splits at sentence boundary before max_part_chars rather than mid-word
        long_txt = ("今天天气很好，适合在阳台喝热茶。" * 4) + ("晚上大家准备一起吃火锅。" * 4)
        bubbles = split_bubbles(long_txt, max_part_chars=65, max_parts=3)
        self.assertGreaterEqual(len(bubbles), 2)
        self.assertLessEqual(len(bubbles), 3)
        for b in bubbles:
            self.assertLessEqual(len(b), 65)
            self.assertTrue(b.endswith(("。", "，", "！", "？")) or len(b) == 65)

        # Unbroken text falls back to clean char slice (`test_p1_onebot` compat)
        self.assertEqual(split_bubbles("长" * 500, max_part_chars=400, max_parts=3), ["长" * 400, "长" * 100])

    def test_spool_dedup_partial_send_resume_and_stale_expiry(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        now_box = [1000.0]
        spool = ChannelSpool(tmp.name, clock=lambda: now_box[0], dedup_ttl_s=600.0)
        self.addCleanup(spool.close)

        ev = {"post_type": "message", "message_type": "private", "user_id": "8001", "message_id": "m-1", "message": "hi"}
        s1 = spool.enqueue_event("onebot_v11", ev, message_id="m-1", session_key="private:8001", kind="private", target="8001", uid="8001")
        self.assertIsNotNone(s1)
        # Duplicate message_id ignored cleanly
        s_dup = spool.enqueue_event("onebot_v11", ev, message_id="m-1", session_key="private:8001", kind="private", target="8001", uid="8001")
        self.assertIsNone(s_dup)

        # Save multi-bubble reply plan and record partial part sent before simulated crash
        spool.save_reply_plan([s1], ["第一泡", "第二泡"], target="8001", kind="private")
        spool.record_part_sent([s1], 1)

        # Also enqueue an old queued event that expires vs a fresh queued event
        now_box[0] = 1005.0
        s_old = spool.enqueue_event("onebot_v11", ev, message_id="m-old", session_key="group:9001", kind="group", target="9001", uid="8002")
        now_box[0] = 1200.0
        rec = spool.recover_unfinished("onebot_v11", stale_s=120.0)
        self.assertEqual([r["spool_id"] for r in rec], [s1])
        self.assertEqual(rec[0]["state"], "reply_ready")
        self.assertEqual(rec[0]["sent_parts"], 1)
        self.assertIsNotNone(s_old)

        spool.ack([s1], state="done")
        self.assertEqual(spool.recover_unfinished("onebot_v11", stale_s=120.0), [])

    def test_onebot_dedup_debounce_and_restart_replay(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws_srv = _ScriptableOneBotWSServer()
        self.addCleanup(ws_srv.close)

        calls: list[str] = []

        def fake_llm(_sys: str, user: str) -> tuple[str, int]:
            calls.append(user)
            return "收到两连发！\n\n这就来帮你准备。", 20

        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_TOOLS_LOOP": "0"},
            llm_fn=fake_llm,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        ch = OneBotChannel(
            OneBotConfig(
                url=ws_srv.url,
                owners=frozenset({"8001"}),
                groups=frozenset({"9001"}),
                actor="sora",
                debounce_ms=150,
                send_retries=2,
                retry_backoff_s=0.05,
            )
        )
        d.channels = [ch]
        d._active_channel_ids = {"onebot_v11"}
        ch.start(d)
        self.addCleanup(ch.stop)
        self.assertTrue(ws_srv.connected.wait(timeout=3.0))

        # 1) Rapid two-part owner DM with debounce_ms=150 aggregates into 1 model call & splits "\n\n" into 2 bubbles
        ws_srv.push({
            "post_type": "message",
            "message_type": "private",
            "self_id": "7001",
            "user_id": "8001",
            "message_id": "9101",
            "sender": {"nickname": "老板"},
            "message": [{"type": "text", "data": {"text": "第一句话"}}],
        })
        ws_srv.push({
            "post_type": "message",
            "message_type": "private",
            "self_id": "7001",
            "user_id": "8001",
            "message_id": "9102",
            "sender": {"nickname": "老板"},
            "message": [{"type": "text", "data": {"text": "第二句话"}}],
        })
        # Exact duplicate message_id '9102' must be deduplicated cleanly
        ws_srv.push({
            "post_type": "message",
            "message_type": "private",
            "self_id": "7001",
            "user_id": "8001",
            "message_id": "9102",
            "sender": {"nickname": "老板"},
            "message": [{"type": "text", "data": {"text": "第二句话"}}],
        })

        self.assertTrue(_wait_until(lambda: len(ws_srv.sent_frames) == 2 and ch.stats["duplicates"] >= 1, timeout=5.0))
        chat_calls = [c for c in calls if "刚刚说：" in c]
        self.assertEqual(len(chat_calls), 1)
        self.assertIn("第一句话", chat_calls[0])
        self.assertIn("第二句话", chat_calls[0])
        texts_sent = [f["params"]["message"][0]["data"]["text"] for f in ws_srv.sent_frames]
        self.assertEqual(texts_sent, ["收到两连发！", "这就来帮你准备。"])

        # Wait until spool rows finish ack
        assert ch._spool is not None
        self.assertTrue(_wait_until(lambda: all(r["state"] == "done" for r in ch._spool.list_rows())))

        # 2) Simulate restart crash recovery when a `reply_ready` multi-bubble response was half-sent
        ch.stop()
        ws_srv.sent_frames.clear()
        calls_before = len(calls)

        rec_spool = ChannelSpool(tmp.name)
        sid = rec_spool.enqueue_event(
            "onebot_v11",
            {"post_type": "message", "message_type": "private", "user_id": "8001", "message": "恢复前消息"},
            message_id="9999",
            session_key="private:8001",
            kind="private",
            target="8001",
            uid="8001",
            actor="sora",
        )
        assert sid is not None
        rec_spool.save_reply_plan([sid], ["已发第一段", "重启后补发第二段"], target="8001", kind="private")
        rec_spool.record_part_sent([sid], 1)
        rec_spool.close()

        ch2 = OneBotChannel(
            OneBotConfig(
                url=ws_srv.url,
                owners=frozenset({"8001"}),
                groups=frozenset({"9001"}),
                actor="sora",
                send_retries=3,
                retry_backoff_s=0.05,
            )
        )
        ch2.start(d)
        self.addCleanup(ch2.stop)

        self.assertTrue(_wait_until(lambda: len(ws_srv.sent_frames) == 1, timeout=5.0))
        self.assertEqual(ws_srv.sent_frames[0]["params"]["message"][0]["data"]["text"], "重启后补发第二段")
        self.assertEqual(len(calls), calls_before)  # Did NOT re-invoke LLM on restart recovery!

    def test_debounced_two_message_batch_replays_remaining_bubble_only_once(self) -> None:
        # 1) Direct ChannelSpool batch plan test
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sp = ChannelSpool(tmp.name)
        self.addCleanup(sp.close)
        s1 = sp.enqueue_event(
            "onebot_v11",
            {"post_type": "message", "message_type": "private", "user_id": "8001", "message": "a"},
            message_id="b-1",
            session_key="private:8001",
            kind="private",
            target="8001",
            uid="8001",
            actor="sora",
        )
        s2 = sp.enqueue_event(
            "onebot_v11",
            {"post_type": "message", "message_type": "private", "user_id": "8001", "message": "b"},
            message_id="b-2",
            session_key="private:8001",
            kind="private",
            target="8001",
            uid="8001",
            actor="sora",
        )
        assert s1 is not None and s2 is not None
        sp.save_reply_plan([s1, s2], ["合并泡一", "合并泡二"], target="8001", kind="private")
        sp.record_part_sent([s1, s2], 1)
        rec = sp.recover_unfinished("onebot_v11", only_ready=True)
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["spool_id"], s2)
        self.assertEqual(rec[0]["sent_parts"], 1)

        # 2) End-to-end OneBotChannel debounce + mid-reply disconnect + restart replay -> remaining bubble sent ONCE
        tmp_ws = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_ws.cleanup)
        ws_srv = _ScriptableOneBotWSServer()
        self.addCleanup(ws_srv.close)
        ws_srv.close_after_acks = 1

        def _batch_llm(_sys: str, _user: str) -> tuple[str, int]:
            return "连发合并第一段。\n\n连发合并第二段。", 16

        d = HouseDaemon(
            tmp_ws.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_TOOLS_LOOP": "0"},
            llm_fn=_batch_llm,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        ch1 = OneBotChannel(
            OneBotConfig(
                url=ws_srv.url,
                owners=frozenset({"8001"}),
                groups=frozenset({"9001"}),
                actor="sora",
                debounce_ms=120,
                send_retries=0,
                reconnect_min_s=10.0,
                reconnect_max_s=10.0,
            )
        )
        d.channels = [ch1]
        ch1.start(d)
        self.addCleanup(ch1.stop)
        self.assertTrue(ws_srv.connected.wait(timeout=3.0))

        ws_srv.push({
            "post_type": "message",
            "message_type": "private",
            "self_id": "7001",
            "user_id": "8001",
            "message_id": "9301",
            "sender": {"nickname": "房主"},
            "message": [{"type": "text", "data": {"text": "连发一"}}],
        })
        ws_srv.push({
            "post_type": "message",
            "message_type": "private",
            "self_id": "7001",
            "user_id": "8001",
            "message_id": "9302",
            "sender": {"nickname": "房主"},
            "message": [{"type": "text", "data": {"text": "连发二"}}],
        })

        assert ch1._spool is not None
        self.assertTrue(_wait_until(lambda: len(ws_srv.sent_frames) == 1, timeout=5.0))
        self.assertTrue(_wait_until(lambda: any(r["state"] == "reply_ready" and r["sent_parts"] == 1 for r in ch1._spool.list_rows()), timeout=5.0))
        ch1.stop()

        ws_srv.sent_frames.clear()
        ws_srv.connected.clear()
        ch2 = OneBotChannel(
            OneBotConfig(
                url=ws_srv.url,
                owners=frozenset({"8001"}),
                groups=frozenset({"9001"}),
                actor="sora",
                send_retries=1,
                retry_backoff_s=0.05,
            )
        )
        ch2.start(d)
        self.addCleanup(ch2.stop)
        self.assertTrue(ws_srv.connected.wait(timeout=3.0))
        assert ch2._spool is not None
        self.assertTrue(_wait_until(lambda: all(r["state"] == "done" for r in ch2._spool.list_rows()), timeout=5.0))
        self.assertEqual(len(ws_srv.sent_frames), 1)
        self.assertEqual(ws_srv.sent_frames[0]["params"]["message"][0]["data"]["text"], "连发合并第二段。")


if __name__ == "__main__":
    unittest.main()
