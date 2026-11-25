"""M4: the send gate against the behaviours the private deployment's send-gate tests require (SPEC G/L/R/S ids in the
test names). Everything here drives the real classes: a real local OneBot WebSocket server, a real `HouseDaemon` with
its `SendGate`, real SQLite files. The only stand-ins are scripted model replies and, where a test says so, a memory
check that is made to fail.

QQ numbers and group numbers are made up; nothing here touches a real account. No credential-shaped values: where a
configured secret or a private fact is needed, the test uses an internal path (which the reply guard always blocks) or
makes the memory check itself answer.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from test_stage3_voice import SAMPLE_WAV, _MockVoiceServer  # noqa: E402
from test_tail_1_sendgate_channels import (  # noqa: E402
    AUD_B64,
    IMG_B64,
    T0,
    Base,
    _FakeOneBotServer,
    _Host,
    _wait_until,
)

from ai_house.agent import prompts  # noqa: E402
from ai_house.agent.engine import (  # noqa: E402
    MAX_SENDS_PER_TURN,
    SENDS_GROUP_ENV,
    SENDS_PRIVATE_ENV,
    Turn,
    max_sends_per_turn,
)
from ai_house.builtin_plugins import image_gen, meme_library, voice_io  # noqa: E402
from ai_house.chat_agent import RoommateChatAgent  # noqa: E402
from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.media_provenance import MediaProvenance, sha256_of_b64  # noqa: E402
from ai_house.plugin_api import ToolContext  # noqa: E402
from worldkernel.outbox import BlockedDelivery, NotSent  # noqa: E402

IMG = {"kind": "image", "image_base64": IMG_B64}
AUD = {"kind": "audio", "audio_base64": AUD_B64}
CHAT = "qq-group:9001"
SEG_TEXT = [{"type": "text", "data": {"text": "x"}}]
LEAK_FREE_ENV = {"AI_HOUSE_MEMORY_LEAK_FREE_GROUPS": CHAT}


def made_for(d: Any, item: Mapping[str, Any], chat: str = CHAT, texts: Any = ("a small test picture",)) -> None:
    """What a built-in media tool does: bind the bytes to the chat and to the text they were made from."""
    b64 = item.get("image_base64") or item.get("audio_base64")
    d.media_provenance.register(b64, chat=chat, source_texts=list(texts), kind=str(item["kind"]))


class _NoGateHost(_Host):
    send_gate = None


class _M4Base(Base):
    def setUp(self) -> None:
        self.d = self.mk_daemon()
        self.srv = _FakeOneBotServer()
        self.addCleanup(self.srv.close)

    def channel(self, **cfg: Any) -> Any:
        cfg.setdefault("groups", frozenset({"9001", "9002"}))
        return self.mk_channel(self.d, self.srv, **cfg)

    def assert_no_frames(self, why: str = "") -> None:
        self.assertEqual(self.srv.frames, [], why or "something reached the socket")

    def wire_still_works(self, ch: Any, expect_frames: int) -> None:
        """Positive control after a refusal: the same writer does write when it may, so "no frame" meant "refused"."""
        ok, code, _, _ = ch._call_detailed("send_group_msg", {"group_id": 9001, "message": SEG_TEXT})
        self.assertTrue(ok, code)
        self.assertTrue(_wait_until(lambda: len(self.srv.frames) == expect_frames))


# ======================================================================================================================
# A1: one "may write" decision at the single raw writer, before the socket write
# ======================================================================================================================
class TransportGuardTests(_M4Base):
    def test_g9_raw_writer_refuses_a_muted_group_and_writes_nothing(self) -> None:
        ch = self.channel()
        self.d.group_behavior.mute(CHAT, minutes=30)
        for call in (ch._call_detailed, lambda a, p: ch._call(a, p) + (None, None)):
            res = call("send_group_msg", {"group_id": 9001, "message": SEG_TEXT})
            self.assertEqual(res[:2], (False, "refused_muted"))
        self.assert_no_frames()
        self.d.group_behavior.unmute(CHAT)
        self.wire_still_works(ch, 1)  # the only frame is the control one

    def test_g20_raw_half_unknown_or_raw_segments_and_actions_are_refused_at_the_writer(self) -> None:
        ch = self.channel()
        bad = [
            ("send_group_msg", {"group_id": 9001, "message": "[CQ:face,id=1]"}),  # a raw CQ string is not empty, safe text
            ("send_group_msg", {"group_id": 9001, "message": [{"type": "forward", "data": {}}]}),
            ("send_group_msg", {"group_id": 9001, "message": [*SEG_TEXT, {"type": "json", "data": {}}]}),
            ("send_group_msg", {"group_id": 9001, "message": ["x"]}),
            ("send_group_msg", {"group_id": 9001}),
            ("send_private_msg", {"user_id": 8001, "message": [{"type": "music", "data": {}}]}),
        ]
        for action, params in bad:
            ok, code, _, left = ch._call_detailed(action, params)
            self.assertEqual((ok, code, left), (False, "refused_segment", False), params)
        ok, code, _, _ = ch._call_detailed("set_group_kick", {"group_id": 9001, "user_id": 1})
        self.assertEqual((ok, code), (False, "refused_action"))
        ok, code, _, _ = ch._call_detailed("send_group_msg", {"group_id": 77777, "message": SEG_TEXT})
        self.assertEqual((ok, code), (False, "refused_destination"))
        self.assert_no_frames()
        self.wire_still_works(ch, 1)

    def test_g20_unknown_media_kinds_are_dropped_before_the_gate(self) -> None:
        ch = self.channel()
        kept = ch._sendable_media([
            {"kind": "video", "image_base64": IMG_B64},
            {"kind": "file", "audio_base64": AUD_B64},
            {"kind": "image"},  # no bytes
            {"kind": "audio", "image_base64": IMG_B64},  # a voice item without audio
            "not a mapping",
        ])
        self.assertEqual(kept, [])
        host = _Host(self.d, [{"mode": "live", "text": "", "media": [{"kind": "video", "image_base64": IMG_B64}]}])
        ch2 = self.mk_channel(self.d, self.srv, host=host)
        ev = {"post_type": "message", "message_type": "private", "self_id": "7001", "user_id": "8001",
              "message_id": "m-video", "time": int(time.time()), "sender": {"nickname": "boss"},
              "message": [{"type": "text", "data": {"text": "send a clip"}}]}
        self.assertEqual(ch2.handle_event(ev), "no_reply")
        self.assert_no_frames()

    def test_g9_a_host_without_a_send_gate_sends_nothing(self) -> None:
        ch = self.mk_channel(self.d, self.srv, groups=frozenset({"9001"}), host=_NoGateHost(self.d, []))
        sid = self.new_sid(ch, "m-nogate")
        self.assertFalse(ch._send_bubbles("group", "9001", ["hello"], spool_ids=[sid]))
        self.assert_no_frames()
        row = self.spool_row(ch, sid)
        self.assertEqual(row["state"], "blocked")
        self.assertIn("send_gate:unavailable", str(row["reason"]))
        self.assertEqual(ch.stats["blocked"], 1)
        self.wire_still_works(ch, 1)

    def test_g9_resume_asks_the_free_checks_again_mute_and_destination(self) -> None:
        ch = self.channel()
        plan = {"bubbles": ["a", "b"], "kind": "group", "target": "9001", "at": ""}
        # resumed in the middle of a reply (part 1 of 2 already out) while the group has been muted since
        sid = self.new_sid(ch, "m-res-1")
        ch._spool.save_reply_plan([sid], ["a", "b"], target="9001", kind="group", at="")
        ch._spool.record_part_sent([sid], 1)
        self.d.group_behavior.mute(CHAT, minutes=30)
        ch._resume_ready_event({"_resume_plan": plan, "_sent_parts": 1, "_spool_ids": [sid]})
        self.assertEqual(self.spool_row(ch, sid)["state"], "blocked")
        self.assertIn("group_muted", str(self.spool_row(ch, sid)["reason"]))
        # a reply_ready row from before the mute that never started going out
        sid2 = self.new_sid(ch, "m-res-2")
        ch._spool.save_reply_plan([sid2], ["a", "b"], target="9001", kind="group", at="")
        ch._resume_ready_event({"_resume_plan": plan, "_sent_parts": 0, "_spool_ids": [sid2]})
        self.assertEqual(self.spool_row(ch, sid2)["state"], "blocked")
        self.assert_no_frames()
        # the destination has been taken off the whitelist since
        self.d.group_behavior.unmute(CHAT)
        sid3 = self.new_sid(ch, "m-res-3")
        ch._spool.save_reply_plan([sid3], ["a", "b"], target="9001", kind="group", at="")
        ch._spool.record_part_sent([sid3], 1)
        ch.cfg.groups = frozenset()
        ch._resume_ready_event({"_resume_plan": plan, "_sent_parts": 1, "_spool_ids": [sid3]})
        self.assertEqual(self.spool_row(ch, sid3)["state"], "blocked")
        self.assert_no_frames()

    def test_g9_raw_outbox_sender_cannot_get_around_a_mute_either(self) -> None:
        ch = self.channel()
        self.d.group_behavior.mute(CHAT, minutes=30)
        with self.assertRaises(BlockedDelivery) as cm:
            ch.deliver("dlv-raw-1", {"kind": "group", "group_id": "9001", "text": "hi"})
        self.assertEqual(cm.exception.reason, "refused_muted")
        self.assert_no_frames()

    def test_g21_a_direct_send_is_counted_once_and_takes_no_turn_slot(self) -> None:
        ch = self.channel()
        self.assertTrue(ch._send_bubbles("group", "9001", ["hello"]))
        self.assertEqual(self.log_rows(self.d, "recip_key='direct:onebot_v11:9001' AND kind='direct'"), 1)
        self.assertEqual(ch._window, {}, "a direct send used up a slot of the per-chat reply limiter")
        self.assertEqual(self.d.send_gate.stats_view()["sent"], 1)

    def test_reminders_do_not_pierce_a_group_mute_they_wait_for_it(self) -> None:
        ch = self.channel()
        self.d.channels = [ch]
        payload = {"actor": "sora", "kind": "group", "group_id": "9001", "text": "⏰ 到点提醒：喝水",
                   "urgent": True, "source": "reminder"}
        self.d.group_behavior.mute(CHAT, minutes=30)
        with self.assertRaises(NotSent) as cm:
            self.d.send_gate.deliver_gated("onebot_v11", ch, "rem-1-1", dict(payload), is_webchat=False)
        self.assertEqual(cm.exception.reason, "group_muted")
        self.assertEqual(self.d.send_gate.stats_view()["deferred_muted"], 1)
        # through the real outbox: the row stays pending, nothing is written, nothing is charged
        did = self.d.enqueue_outbound(channel="onebot_v11", payload=dict(payload), delivery_id="rem-2-2")
        self.assertNotEqual(self.d.flush_outbox(reconcile=False)["dispatched"].get(did), "sent")
        self.assert_no_frames()
        self.assertEqual(self.log_rows(self.d), 0)
        self.d.group_behavior.unmute(CHAT)
        self.assertEqual(self.d.flush_outbox(reconcile=False)["dispatched"].get(did), "sent")
        self.assertTrue(_wait_until(lambda: self.srv.seg_types() == ["text"]), self.srv.seg_types())
        self.assertEqual(self.log_rows(self.d), 1)

    def test_web_group_reminder_also_waits_for_a_mute(self) -> None:
        payload = {"actor": "sora", "kind": "group", "text": "⏰ 到点提醒", "urgent": True, "source": "reminder"}
        self.d.group_behavior.mute("group", minutes=30)
        with self.assertRaises(NotSent):
            self.d.send_gate.deliver_gated("webchat", None, "rem-web-1", dict(payload), is_webchat=True)
        self.assertEqual([r for r in self.d.list_chat("group") if "到点提醒" in r["text"]], [])
        self.d.group_behavior.unmute("group")
        self.assertTrue(self.d.send_gate.deliver_gated("webchat", None, "rem-web-1", dict(payload), is_webchat=True))


# ======================================================================================================================
# A2: opaque media in groups needs a provenance record
# ======================================================================================================================
class OpaqueMediaTests(_M4Base):
    def send(self, ch: Any, media: List[Mapping[str, Any]], text: str = "", *, target: str = "9001", kind: str = "group") -> Any:
        sid = self.new_sid(ch, f"m-{time.monotonic_ns()}", kind=kind, target=target)
        ok = ch._send_bubbles(kind, target, [text] if text else [], spool_ids=[sid], media=media)
        return ok, self.spool_row(ch, sid)

    def test_g5_a_harmless_caption_does_not_make_unrecorded_media_safe(self) -> None:
        ch = self.channel()
        ok, row = self.send(ch, [IMG], text="看看这张图")
        self.assertFalse(ok)
        self.assertEqual(row["state"], "blocked")
        self.assertIn("media_provenance:missing", str(row["reason"]))
        self.assert_no_frames("neither the caption nor the picture may go out")
        self.assertEqual(self.log_rows(self.d), 0)
        ok, _ = self.send(ch, [], text="看看这张图")  # the caption alone is fine: only the media was the problem
        self.assertTrue(ok)

    def test_g12_the_record_is_bound_to_chat_and_bytes_and_a_lost_record_fails_closed(self) -> None:
        ch = self.channel()
        made_for(self.d, IMG, chat="qq-group:9002")
        ok, row = self.send(ch, [IMG])  # made for another group
        self.assertEqual((ok, row["state"]), (False, "blocked"))
        self.assertIn("media_provenance:other_chat", str(row["reason"]))
        made_for(self.d, IMG)
        changed = {"kind": "image", "image_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nother-bytes").decode()}
        ok, row = self.send(ch, [changed])  # same chat, but not the bytes that were recorded
        self.assertIn("media_provenance:missing", str(row["reason"]))
        self.d.media_provenance.clear()  # what a restart does
        ok, row = self.send(ch, [IMG])
        self.assertIn("media_provenance:missing", str(row["reason"]))
        self.assert_no_frames()
        made_for(self.d, IMG)
        ok, row = self.send(ch, [IMG])
        self.assertTrue(ok)
        self.assertEqual(row["state"], "done")
        self.assertEqual(self.srv.seg_types(), ["image"])

    def test_g7_the_source_text_is_checked_again_at_send_time(self) -> None:
        ch = self.channel()
        made_for(self.d, IMG, texts=("画一张图，文件放在 /etc/passwd",))  # the reply guard always blocks an internal path
        ok, row = self.send(ch, [IMG])
        self.assertEqual((ok, row["state"]), (False, "blocked"))
        self.assertIn("guard:internal_path", str(row["reason"]))
        made_for(self.d, AUD, texts=("普通的话",))
        with patch.object(self.d.memory, "check_outbound_leak", return_value=[1]):  # the privacy check says "private"
            ok, row = self.send(ch, [AUD])
        self.assertIn("memory_leak:private_to_public", str(row["reason"]))
        self.assert_no_frames()
        ok, row = self.send(ch, [AUD])  # the same media, privacy check quiet again
        self.assertTrue(ok)

    def test_g7_negative_prompt_is_checked_before_generation(self) -> None:
        res = image_gen.generate_image_asset(
            "一只猫", negative_prompt="不要出现 /etc/passwd", scope="group", actor="sora", daemon=self.d,
            cfg={"engine": "custom_python", "model": "none-registered"},
        )
        self.assertEqual((res["ok"], res["status"]), (False, "blocked"))
        self.assertIn("guard:internal_path", res["error"])

    def test_g6_the_owners_private_chat_may_receive_media_without_a_record(self) -> None:
        ch = self.channel()
        ok, row = self.send(ch, [IMG, AUD], kind="private", target="8001")
        self.assertTrue(ok)
        self.assertEqual(self.srv.seg_types(), ["image", "record"])

    def test_l3_a_leak_free_group_still_needs_the_record_but_skips_the_leak_part(self) -> None:
        d = self.mk_daemon(LEAK_FREE_ENV)
        ch = self.mk_channel(d, self.srv, groups=frozenset({"9001"}))
        self.assertTrue(d.memory.leak_free_in(CHAT))
        sid = self.new_sid(ch, "m-lf-1")
        self.assertFalse(ch._send_bubbles("group", "9001", [], spool_ids=[sid], media=[IMG]))
        self.assertIn("media_provenance:missing", str(self.spool_row(ch, sid)["reason"]))
        made_for(d, IMG)
        with patch.object(d.memory, "check_outbound_leak", side_effect=RuntimeError("down")):
            self.assertTrue(ch._send_bubbles("group", "9001", [], media=[IMG]))  # the check is not asked to block here
        self.assertEqual(self.srv.seg_types(), ["image"])

    def test_outbox_payload_media_is_gated_the_same_way(self) -> None:
        ch = self.channel()
        payload = {"actor": "sora", "kind": "group", "group_id": "9001", "text": "给你一张图", "image_base64": IMG_B64}
        with self.assertRaises(BlockedDelivery) as cm:
            self.d.send_gate.deliver_gated("onebot_v11", ch, "dlv-media-1", dict(payload), is_webchat=False)
        self.assertEqual(cm.exception.reason, "media_provenance:missing")
        self.assert_no_frames()
        made_for(self.d, IMG)
        self.assertTrue(self.d.send_gate.deliver_gated("onebot_v11", ch, "dlv-media-1", dict(payload), is_webchat=False))
        self.assertEqual(self.srv.seg_types(), ["text", "image"])

    def test_the_gate_refuses_media_it_was_only_told_the_count_of(self) -> None:
        with self.assertRaises(BlockedDelivery) as cm:
            self.d.send_gate.authorize_direct_send(
                "onebot_v11", self.channel(), kind="group", target="9001", actor="sora", text="", media_count=1)
        self.assertEqual(cm.exception.reason, "media_provenance:unverifiable")

    def test_registry_unit_behaviour(self) -> None:
        reg = MediaProvenance(max_records=16)
        digest = reg.register(IMG_B64, chat=CHAT, source_texts=["a", "b", "a"])
        self.assertEqual(digest, sha256_of_b64(IMG_B64))
        rec, why = reg.lookup(digest, CHAT)
        self.assertEqual((why, rec["texts"]), ("", ["a", "b"]))
        self.assertEqual(reg.lookup(digest, "qq-group:1")[1], "other_chat")
        self.assertEqual(reg.lookup("0" * 64, CHAT)[1], "missing")
        self.assertEqual(reg.register("", chat=CHAT, source_texts=["x"]), "")
        self.assertEqual(reg.register("not base64!!", chat=CHAT, source_texts=["x"]), "")
        for i in range(40):  # bounded: the oldest records go first
            reg.register(base64.b64encode(f"bytes-{i}".encode()).decode(), chat=CHAT, source_texts=["x"])
        self.assertLessEqual(len(reg), 16)
        self.assertEqual(reg.lookup(digest, CHAT)[1], "missing")


class MediaToolsRegisterTests(_M4Base):
    """The built-in tools write the record when they make or pick media; the gate then lets it into its own group only."""

    def ctx(self, chat: str = CHAT, *, config: Any = None, daemon: Any = "house") -> ToolContext:
        return ToolContext(actor="sora", scope="group", channel=chat, speaker=owner_speaker("房主"), now=T0,
                           plugin_config=dict(config or {}), daemon=self.d if daemon == "house" else daemon,
                           pending_media=[])

    def test_generated_image_reaches_its_own_group_only(self) -> None:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8"/></svg>'
        image_gen.register_image_engine("m4-test-engine", lambda spec: svg)
        self.addCleanup(image_gen.unregister_image_engine, "m4-test-engine")
        ctx = self.ctx(config={"engine": "custom_python", "model": "m4-test-engine"})
        res = image_gen._tool_generate_image(prompt="手绘风抹茶松饼", ctx=ctx)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(ctx.pending_media), 1)
        rec, why = self.d.media_provenance.lookup(sha256_of_b64(ctx.pending_media[0]["image_base64"]), CHAT)
        self.assertEqual(why, "")
        self.assertEqual(rec["texts"], ["手绘风抹茶松饼"])
        ch = self.channel()
        sid = self.new_sid(ch, "m-gen-other", target="9002")
        self.assertFalse(ch._send_bubbles("group", "9002", [], spool_ids=[sid], media=ctx.pending_media))
        self.assertIn("other_chat", str(self.spool_row(ch, sid)["reason"]))
        self.assert_no_frames()
        self.assertTrue(ch._send_bubbles("group", "9001", [], media=ctx.pending_media))
        self.assertEqual(self.srv.seg_types(), ["image"])

    def test_picked_sticker_is_registered_with_its_label_and_caption(self) -> None:
        listing = meme_library.search_meme_catalog("", cfg={})
        self.assertTrue(listing["items"], listing)
        item = listing["items"][0]
        ctx = self.ctx()
        res = meme_library._tool_send_meme(meme_id=item["id"], caption="来一个", ctx=ctx)
        self.assertTrue(res["ok"], res)
        wire = ctx.pending_media[0]["image_base64"]
        rec, why = self.d.media_provenance.lookup(sha256_of_b64(wire), CHAT)
        self.assertEqual(why, "")
        self.assertIn("来一个", rec["texts"])
        self.assertIn(item["title"], rec["texts"])

    def test_nothing_is_recorded_when_the_tool_failed_or_there_is_no_registry(self) -> None:
        ctx = self.ctx()
        res = meme_library._tool_send_meme(meme_id="no-such-sticker", caption="x", ctx=ctx)
        self.assertFalse(res["ok"])
        self.assertEqual(len(self.d.media_provenance), 0)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"/>'
        image_gen.register_image_engine("m4-test-engine-2", lambda spec: svg)
        self.addCleanup(image_gen.unregister_image_engine, "m4-test-engine-2")
        # a host without a registry records nothing, quietly
        ctx2 = self.ctx(config={"engine": "custom_python", "model": "m4-test-engine-2"},
                        daemon=SimpleNamespace(guard=None, memory=None))
        self.assertTrue(image_gen._tool_generate_image(prompt="x", ctx=ctx2)["ok"])
        self.assertEqual(len(self.d.media_provenance), 0)


# ======================================================================================================================
# A6 + G4 / G11: behaviours that were already enforced, now proved
# ======================================================================================================================
class VoiceTurnTests(_M4Base):
    def setUp(self) -> None:
        super().setUp()
        self.voice = _MockVoiceServer()
        self.addCleanup(self.voice.close)
        self.d.update_plugin_settings({
            "plugin_id": "voice_io", "enabled": True,
            "values": {"base_url": self.voice.base_url, "api_key": self.voice.secret_key},
        })
        self.reply_as_calls = 0

        def reply_as(aid, text, card, snap, recent, where="private", speaker=None, *, tool_runner=None, **kw):
            self.reply_as_calls += 1
            tool_runner("synthesize_voice", {"text": "好的，念给你听"})  # the model decided to answer with a voice clip
            return {"actor": aid, "name": "Sora", "text": "好的，念给你听", "context_badge": "", "mode": "live_api"}

        patcher = patch.object(self.d.chat_agent, "reply_as", side_effect=reply_as)
        patcher.start()
        self.addCleanup(patcher.stop)

    def group_event(self, mid: str) -> Dict[str, Any]:
        return {"post_type": "message", "message_type": "group", "self_id": "7001", "user_id": "8001",
                "group_id": "9001", "message_id": mid, "time": int(time.time()), "sender": {"nickname": "boss"},
                "message": [{"type": "at", "data": {"qq": "7001"}}, {"type": "text", "data": {"text": " 念一句给我听"}}]}

    def test_g4_a_muted_group_turn_synthesizes_nothing_and_sends_nothing(self) -> None:
        ch = self.channel()
        self.d.group_behavior.mute(CHAT, minutes=30)
        self.assertEqual(ch.handle_event(self.group_event("m-muted")), "no_reply")
        self.assertEqual(self.reply_as_calls, 0, "the model was asked although the group is muted")
        self.assertEqual(self.voice.tts_requests, [], "a voice clip was synthesized in a muted group")
        self.assert_no_frames()
        self.assertEqual(self.log_rows(self.d), 0)

    def test_g11_a_safe_voice_reply_is_synthesized_once_sent_once_charged_once(self) -> None:
        ch = self.channel()
        self.assertEqual(ch.handle_event(self.group_event("m-voice")), "replied")
        self.assertEqual(len(self.voice.tts_requests), 1)
        self.assertEqual(self.srv.seg_types().count("record"), 1)
        self.assertEqual(self.srv.seg_types().count("text"), 1)
        self.assertEqual(self.log_rows(self.d, "kind='direct'"), 1)
        self.assertEqual(len(self.voice.tts_requests), 1, "sending triggered a second synthesis")

    def test_g3_a_private_text_is_checked_before_synthesis(self) -> None:
        with patch.object(self.d.memory, "check_outbound_leak", return_value=[1]):
            res = voice_io.synthesize_speech("私聊里说过的话", scope="group", actor="sora", daemon=self.d,
                                             cfg={"base_url": self.voice.base_url, "api_key": self.voice.secret_key})
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(self.voice.tts_requests, [])

    def test_g10_a_tool_preflight_leaves_no_ledger_row_and_no_slot(self) -> None:
        cfg = {"base_url": self.voice.base_url, "api_key": self.voice.secret_key}
        with patch.object(self.d.memory, "check_outbound_leak", return_value=[1]):
            res = voice_io.synthesize_speech("私聊里说过的话", scope="group", actor="sora", daemon=self.d, cfg=cfg)
        self.assertEqual(res["status"], "blocked")
        ok = voice_io.synthesize_speech("普通的一句话", scope="group", actor="sora", daemon=self.d, cfg=cfg)
        self.assertTrue(ok["ok"], ok)  # a preflight that passes and a result nobody has sent yet
        self.assertEqual(self.log_rows(self.d), 0)
        self.assertEqual(self.d.send_gate.stats_view()["sent"], 0)


# ======================================================================================================================
# A3: the leak check fails closed, explicitly
# ======================================================================================================================
class LeakCheckTests(_M4Base):
    def test_l2_a_failing_check_closes_a_normal_group_and_charges_nothing(self) -> None:
        ch = self.channel()
        sid = self.new_sid(ch, "m-l2")
        with patch.object(self.d.memory, "check_outbound_leak", side_effect=RuntimeError("service down")):
            self.assertFalse(ch._send_bubbles("group", "9001", ["hello"], spool_ids=[sid]))
            with self.assertRaises(BlockedDelivery) as cm:
                self.d.send_gate.deliver_gated(
                    "onebot_v11", ch, "dlv-l2", {"actor": "sora", "kind": "group", "group_id": "9001", "text": "hi"},
                    is_webchat=False)
        self.assertEqual(cm.exception.reason, "privacy_check_unavailable")
        row = self.spool_row(ch, sid)
        self.assertEqual(row["state"], "blocked")
        self.assertIn("privacy_check_unavailable", str(row["reason"]))
        self.assert_no_frames()
        self.assertEqual(self.log_rows(self.d), 0)

    def test_l2_no_memory_store_or_no_known_actor_also_closes_a_group(self) -> None:
        ch = self.channel()
        gate = self.d.send_gate
        for actor in ("", "nobody-here"):
            with self.assertRaises(BlockedDelivery) as cm:
                gate.authorize_direct_send("onebot_v11", ch, kind="group", target="9001", actor=actor, text="hi")
            self.assertEqual(cm.exception.reason, "privacy_check_unavailable", actor)
        real = self.d.memory
        self.d.memory = None
        self.addCleanup(setattr, self.d, "memory", real)
        with self.assertRaises(BlockedDelivery) as cm:
            gate.authorize_direct_send("onebot_v11", ch, kind="group", target="9001", actor="sora", text="hi")
        self.assertEqual(cm.exception.reason, "privacy_check_unavailable")
        # the owner's private chat has no group to leak into: it is not held up by the missing check
        self.assertTrue(gate.authorize_direct_send("onebot_v11", ch, kind="private", target="8001", actor="sora", text="hi"))

    def test_l1_in_a_leak_free_chat_a_failing_check_does_not_block_is_charged_and_is_logged(self) -> None:
        d = self.mk_daemon(LEAK_FREE_ENV)
        ch = self.mk_channel(d, self.srv, groups=frozenset({"9001"}))
        lines: List[str] = []
        with patch.object(d, "log", side_effect=lines.append), \
                patch.object(d.memory, "check_outbound_leak", side_effect=RuntimeError("service down")):
            self.assertTrue(ch._send_bubbles("group", "9001", ["hello"]))
        self.assertTrue(_wait_until(lambda: len(self.srv.frames) == 1))
        self.assertEqual(self.log_rows(d), 1, "the slot was not charged")
        self.assertEqual(d.send_gate.stats_view()["leak_check_skipped"], 1)
        self.assertTrue(any("privacy check skipped" in ln and CHAT in ln for ln in lines), lines)

    def test_l3_the_other_gates_still_block_in_a_leak_free_chat(self) -> None:
        d = self.mk_daemon({**LEAK_FREE_ENV, "AI_HOUSE_DIRECT_DAILY_CAP": "1"})
        ch = self.mk_channel(d, self.srv, groups=frozenset({"9001"}))
        gate = d.send_gate
        ok = dict(kind="group", target="9001", actor="sora")
        d.group_behavior.mute(CHAT, minutes=30)  # mute
        with self.assertRaises(BlockedDelivery) as cm:
            gate.authorize_direct_send("onebot_v11", ch, text="hi", **ok)
        self.assertEqual(cm.exception.reason, "group_muted")
        d.group_behavior.unmute(CHAT)
        with self.assertRaises(BlockedDelivery) as cm:  # whitelist
            gate.authorize_direct_send("onebot_v11", ch, kind="group", target="9777", actor="sora", text="hi")
        self.assertEqual(cm.exception.reason, "group_not_whitelisted")
        with self.assertRaises(BlockedDelivery) as cm:  # reply guard (an internal path)
            gate.authorize_direct_send("onebot_v11", ch, text="see /etc/passwd", **ok)
        self.assertTrue(cm.exception.reason.startswith("guard:"))
        self.assertTrue(ch._send_bubbles("group", "9001", ["one"]))  # quota: one a day here
        with self.assertRaises(NotSent) as cm2:
            gate.authorize_direct_send("onebot_v11", ch, text="two", **ok)
        self.assertEqual(cm2.exception.reason, "daily_cap")

    def test_the_privacy_check_runs_without_the_gates_lock(self) -> None:
        ch = self.channel()
        seen: Dict[str, bool] = {}

        def check(actor: str, scope: str, text: str, **kw: Any) -> List[int]:
            got: List[bool] = []

            def other_thread() -> None:  # an RLock is owned by the thread that took it: it must release it itself
                ok = self.d.send_gate._lock.acquire(timeout=1.0)
                got.append(ok)
                if ok:
                    self.d.send_gate._lock.release()

            t = threading.Thread(target=other_thread)
            t.start()
            t.join(3.0)
            seen["free"] = bool(got and got[0])
            return []

        with patch.object(self.d.memory, "check_outbound_leak", side_effect=check):
            self.d.send_gate.authorize_direct_send("onebot_v11", ch, kind="group", target="9001", actor="sora", text="hi")
            self.assertTrue(seen["free"], "authorize_direct_send held the lock during the check")
            seen.clear()
            self.d.send_gate.deliver_gated(
                "onebot_v11", ch, "dlv-lock", {"actor": "sora", "kind": "group", "group_id": "9001", "text": "hi"},
                is_webchat=False)
            self.assertTrue(seen["free"], "deliver_gated held the lock during the check")


# ======================================================================================================================
# A4 (R9): program notices are not her words
# ======================================================================================================================
class NoticeIsNotHerWordsTests(_M4Base):
    def observed_replies(self) -> List[str]:
        return [c.kwargs.get("reply", "") for c in self.observe.call_args_list]

    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(self.d.memory, "observe", wraps=self.d.memory.observe)
        self.observe = patcher.start()
        self.addCleanup(patcher.stop)

    def web(self, did: str, source: str, text: str) -> Dict[str, Any]:
        payload = {"actor": "sora", "kind": "private", "scope": "private", "text": text,
                   "urgent": True, "source": source}
        self.assertTrue(self.d.send_gate.deliver_gated("webchat", None, did, payload, is_webchat=True))
        return self.d.list_chat("private:sora")[-1]

    def test_r9_a_reminder_keeps_its_rows_but_is_not_remembered_or_echoed_as_hers(self) -> None:
        row = self.web("rem-9-1", "reminder", "⏰ 到点提醒：交房租")
        self.assertEqual(row["text"], "⏰ 到点提醒：交房租")  # the chat row is kept
        self.assertNotEqual(row["sender"], "sora")  # but it is not under her name
        self.assertEqual(row["mode"], "notice")
        self.assertEqual(self.log_rows(self.d), 1)  # and its ledger row too
        self.assertEqual(self.observed_replies(), [], "memory was told she said the reminder")
        # and the next prompt does not hand it back to her as one of her own lines
        self.assertFalse(row["sender"] == "sora")

    def test_r9_her_own_proactive_speech_is_remembered_and_stays_hers(self) -> None:
        row = self.web("pro-9-1", "proactive", "刚路过厨房，想起明天要买牛奶。")
        self.assertEqual(row["sender"], "sora")
        self.assertEqual(row["mode"], "outbox")
        self.assertEqual(self.observed_replies(), ["刚路过厨房，想起明天要买牛奶。"])

    def test_r9_on_the_onebot_outbox_path_her_speech_is_observed_after_a_confirmed_send_only(self) -> None:
        ch = self.channel()
        gate = self.d.send_gate
        now = self.d.clock.now()
        base = {"actor": "sora", "kind": "group", "group_id": "9001"}
        gate.deliver_gated("onebot_v11", ch, "ob-rem", {**base, "text": "⏰ 到点提醒：喝水", "source": "reminder",
                                                        "urgent": True}, is_webchat=False)
        self.assertEqual(self.observed_replies(), [], "a reminder sent to QQ was remembered as hers")
        self.srv.fail_types = {"text"}  # the send fails: nothing was said, nothing is remembered
        with self.assertRaises(NotSent):
            gate.deliver_gated("onebot_v11", ch, "ob-pro-fail", {**base, "text": "没发出去的话", "source": "proactive",
                                                                 "created_ts": now}, is_webchat=False)
        self.assertEqual(self.observed_replies(), [])
        self.srv.fail_types = set()
        gate.deliver_gated("onebot_v11", ch, "ob-pro", {**base, "text": "发出去的话", "source": "proactive",
                                                        "created_ts": now}, is_webchat=False)
        self.assertEqual(self.observed_replies(), ["发出去的话"])
        call = self.observe.call_args_list[-1]
        self.assertEqual(call.kwargs.get("channel"), CHAT)


# ======================================================================================================================
# A5: per-turn limits by chat kind
# ======================================================================================================================
def _speak(turn: Turn, n: int) -> List[Dict[str, Any]]:
    async def go() -> List[Dict[str, Any]]:
        return [await turn.call_tool("send_message", {"text": f"m{i}", "end_turn": False}) for i in range(n)]

    return asyncio.run(go())


class PerTurnLimitTests(unittest.TestCase):
    def test_s1_a_group_turn_allows_three_and_refuses_the_fourth_with_a_message(self) -> None:
        env: Dict[str, str] = {}
        limit = max_sends_per_turn(env, "group")
        self.assertEqual(limit, 3)
        res = _speak(Turn(allow_quiet=True, tool_names=[], max_sends=limit), 4)
        self.assertEqual([r["ok"] for r in res], [True, True, True, False])
        self.assertEqual(res[3]["error"], "too_many_messages")
        self.assertTrue(res[3]["note"])
        self.assertIn("一轮最多发 3 条", prompts.system_prompt("p", where="group", allow_quiet=True, max_sends=limit))

    def test_a_stricter_group_setting_is_enforced_and_shown_in_her_rules(self) -> None:
        env = {SENDS_GROUP_ENV: "1"}
        limit = max_sends_per_turn(env, "group")
        res = _speak(Turn(allow_quiet=True, tool_names=[], max_sends=limit), 2)
        self.assertEqual([r["ok"] for r in res], [True, False])
        self.assertIn("一轮最多发 1 条", prompts.system_prompt("p", where="group", allow_quiet=True, max_sends=limit))
        self.assertEqual(max_sends_per_turn(env, "private"), 3, "the group setting leaked into private chats")

    def test_s3_private_zero_means_no_per_turn_limit_and_the_text_says_so(self) -> None:
        limit = max_sends_per_turn({SENDS_PRIVATE_ENV: "0"}, "private")
        self.assertEqual(limit, 0)
        res = _speak(Turn(allow_quiet=False, tool_names=[], max_sends=limit), 12)
        self.assertTrue(all(r["ok"] for r in res))
        text = prompts.system_prompt("p", where="private", allow_quiet=False, max_sends=limit)
        self.assertIn("不限", text)
        self.assertNotIn("一轮最多发", text)
        self.assertEqual(max_sends_per_turn({SENDS_PRIVATE_ENV: "0"}, "group"), 3)

    def test_s4_old_configuration_keeps_todays_behaviour_and_bad_values_fall_back(self) -> None:
        self.assertEqual((max_sends_per_turn({}, "group"), max_sends_per_turn({}, "private")), (MAX_SENDS_PER_TURN,) * 2)
        for bad in ("", " ", "abc", "-1", "2.5", "99999"):
            self.assertEqual(max_sends_per_turn({SENDS_GROUP_ENV: bad}, "group"), 3, bad)
        res = _speak(Turn(allow_quiet=True, tool_names=[]), 4)  # a Turn made the old way still stops at three
        self.assertEqual([r["ok"] for r in res], [True, True, True, False])

    def test_the_chat_agent_gives_the_runtime_the_limit_of_this_chat_kind(self) -> None:
        seen: List[Any] = []

        class Runtime:
            def run_turn(self, req: Any) -> Any:
                seen.append(req)
                return SimpleNamespace(messages=["好"], quiet=False, quiet_reason="", model="m",
                                       usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0), latency_ms=0)

            def engine(self, name: str) -> Any:
                return None

        router = SimpleNamespace(record_external_usage=lambda **kw: None, collect_secrets=lambda: [])
        env = {SENDS_GROUP_ENV: "2", SENDS_PRIVATE_ENV: "0"}
        agent = RoommateChatAgent(SimpleNamespace(ROSTER=("sora",), NAMES={}, SELVES={}), env=env, router=router,
                                  agent_runtime=Runtime())  # type: ignore[arg-type]
        ctx = {"place_label": "客厅", "doing": "发呆", "roommates_here": [], "wearing": "家居服", "energy": 50,
               "hunger": 10, "mood": "平静", "meals_today": []}
        for where in ("group", "private"):
            agent._reply_via_agent("claude", "sora", "你是索拉。", ctx, where=where, channel="c", said_line="hi",
                                   history=[], memory_context="", attachment_block="", tool_specs=[], tool_runner=None)
        self.assertEqual([r.max_sends for r in seen], [2, 0])
        self.assertIn("一轮最多发 2 条", seen[0].system)
        self.assertIn("不限", seen[1].system)

    def test_s3_the_hourly_budget_still_applies_in_a_private_chat_with_no_per_turn_limit(self) -> None:
        # the per-turn setting is only about one turn; the channel's own hourly budget and the gate's caps are separate
        self.assertEqual(max_sends_per_turn({SENDS_PRIVATE_ENV: "0"}, "private"), 0)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from ai_house.daemon import HouseDaemon
        from test_tail_1_sendgate_channels import PACK

        d = HouseDaemon(tmp.name, pack_dir=PACK, start_ts=T0, virtual_clock=True, allow_onboarding=False,
                        env={SENDS_PRIVATE_ENV: "0", "AI_HOUSE_DIRECT_HOURLY_CAP": "2", "AI_HOUSE_OWNER_TOKEN": "m4-owner-test"})
        self.addCleanup(d.close)
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        from ai_house.builtin_plugins.onebot_ws import OneBotChannel, OneBotConfig

        ch = OneBotChannel(OneBotConfig(url=srv.url, owners=frozenset({"8001"}), groups=frozenset(), actor="sora",
                                        min_interval_s=0.0, send_retries=0, max_per_hour=1000))
        ch.start(d)
        self.addCleanup(ch.stop)
        self.assertTrue(_wait_until(lambda: ch._ws is not None))
        results = [ch._send_bubbles("private", "8001", [f"m{i}"]) for i in range(4)]
        self.assertEqual(results, [True, True, False, False])


if __name__ == "__main__":
    unittest.main()
