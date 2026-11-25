"""Tail-1 regression tests: send-gate quotas, reply visibility, rich-media delivery, bubble splitting, outbox crash
recovery, proactive channel availability and spool pruning.

Everything here drives the real classes: a real local OneBot WebSocket server for `OneBotChannel`, a real
`HouseDaemon` (with its `SendGate` and outbox store) and real SQLite files.  Nothing in the code under test is patched.
"""
from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from websockets.sync.server import serve  # noqa: E402

from ai_house.builtin_plugins.onebot_ws import OneBotChannel, OneBotConfig  # noqa: E402
from ai_house.channel_spool import ChannelSpool, split_bubbles  # noqa: E402
from ai_house.daemon import HouseDaemon, JST  # noqa: E402
from worldkernel.outbox import BlockedDelivery, Dispatcher, NotSent, enqueue_outbound  # noqa: E402

PACK = str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3")
T0 = dt.datetime(2026, 11, 22, 14, 0, tzinfo=JST).timestamp()
IMG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-image-bytes").decode("ascii")
AUD_B64 = base64.b64encode(b"RIFFfake-audio-bytes").decode("ascii")


def _wait_until(pred: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class _FakeOneBotServer:
    """Real forward-WS OneBot server; `fail_types` makes frames carrying that segment type answer with a retcode."""

    def __init__(self) -> None:
        self.frames: List[Dict[str, Any]] = []
        self.fail_types: set = set()
        self._stop = threading.Event()
        self._server = serve(self._handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._server.socket.getsockname()[1]}"
        self._th = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._th.start()

    def _handler(self, ws: Any) -> None:
        for raw in ws:
            if self._stop.is_set():
                return
            frame = json.loads(raw)
            self.frames.append(frame)
            seg_types = {s.get("type") for s in (frame.get("params", {}).get("message") or [])}
            if seg_types & self.fail_types:
                ws.send(json.dumps({"status": "failed", "retcode": 1200, "echo": frame.get("echo")}))
                continue
            ws.send(json.dumps({"status": "ok", "retcode": 0, "data": {"message_id": 900 + len(self.frames)},
                                "echo": frame.get("echo")}))

    def seg_types(self) -> List[str]:
        out: List[str] = []
        for f in self.frames:
            for s in f.get("params", {}).get("message") or []:
                out.append(str(s.get("type")))
        return out

    def close(self) -> None:
        self._stop.set()
        self._server.shutdown()
        self._th.join(timeout=2.0)


class _Host:
    """Host handed to `OneBotChannel`: the real daemon, except `converse` returns a scripted result."""

    def __init__(self, daemon: Any, replies: List[Dict[str, Any]]) -> None:
        self._d = daemon
        self._replies = replies

    def __getattr__(self, name: str) -> Any:
        return getattr(self._d, name)

    def converse(self, channel: str, text: str, speaker: Any, **kw: Any) -> Dict[str, Any]:
        return {"replies": list(self._replies)}


class Base(unittest.TestCase):
    def mk_daemon(self, env: Optional[Dict[str, str]] = None, **kw: Any) -> HouseDaemon:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        e = {"AI_HOUSE_OWNER_TOKEN": "tail1-owner-token"}
        e.update(env or {})
        d = HouseDaemon(tmp.name, pack_dir=PACK, env=e, start_ts=T0, virtual_clock=True,
                        allow_onboarding=False, **kw)
        self.addCleanup(d.close)
        self.tmp = tmp.name
        return d

    def mk_channel(self, d: Any, srv: _FakeOneBotServer, *, start: bool = True, host: Any = None, **cfg: Any) -> OneBotChannel:
        kw: Dict[str, Any] = dict(url=srv.url, owners=frozenset({"8001"}), groups=frozenset({"9001"}), actor="sora",
                                  min_interval_s=0.0, send_retries=0, send_timeout_s=3.0)
        kw.update(cfg)
        ch = OneBotChannel(OneBotConfig(**kw))
        if start:
            ch.start(host or d)
            self.addCleanup(ch.stop)
            self.assertTrue(_wait_until(lambda: ch._ws is not None, 5.0), "channel never connected")
        return ch

    def log_rows(self, d: Any, where: str = "1=1") -> int:
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            return int(c.execute(f"SELECT COUNT(*) FROM send_gate_log WHERE {where}").fetchone()[0])

    def spool_row(self, ch: OneBotChannel, sid: int) -> Dict[str, Any]:
        return next(r for r in ch._spool.list_rows() if r["spool_id"] == sid)

    def new_sid(self, ch: OneBotChannel, mid: str, kind: str = "group", target: str = "9001") -> int:
        sid = ch._spool.enqueue_event("onebot_v11", {"message": "x"}, message_id=mid, session_key=f"{kind}:{target}",
                                      kind=kind, target=target, uid="8001")
        assert sid is not None
        return int(sid)


# ---------------------------------------------------------------------------------------------------------------
class SendGateQuotaTests(Base):
    def test_1_direct_replies_do_not_eat_proactive_daily_quota(self) -> None:
        d = self.mk_daemon()
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv, max_per_hour=1000)
        for i in range(70):
            d.clock.t = T0 + i * 120
            self.assertTrue(ch._send_bubbles("group", "9001", [f"hi{i}"]), f"direct reply {i} was refused")
        d.clock.t = T0 + 70 * 120
        ref = d.send_gate.deliver_gated(
            "onebot_v11", ch, "dlv-tail1-p1",
            {"actor": "sora", "kind": "group", "group_id": "9001", "text": "proactive hello", "source": "proactive",
             "created_ts": d.clock.now()},
            is_webchat=False,
        )
        self.assertTrue(ref)

    def test_1_outbox_traffic_does_not_eat_direct_quota(self) -> None:
        d = self.mk_daemon({"AI_HOUSE_OUTBOX_DAILY_CAP": "2", "AI_HOUSE_OUTBOX_HOURLY_CAP": "50"})
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv)
        for i in range(2):
            d.send_gate.deliver_gated("onebot_v11", ch, f"dlv-{i}", {"actor": "sora", "kind": "group", "group_id": "9001",
                                                                      "text": f"p{i}"}, is_webchat=False)
        with self.assertRaises(NotSent):
            d.send_gate.deliver_gated("onebot_v11", ch, "dlv-2", {"actor": "sora", "kind": "group", "group_id": "9001",
                                                                   "text": "p2"}, is_webchat=False)
        for i in range(5):
            self.assertTrue(ch._send_bubbles("group", "9001", [f"reply{i}"]))

    def test_1_failed_direct_send_is_not_charged(self) -> None:
        d = self.mk_daemon({"AI_HOUSE_DIRECT_DAILY_CAP": "3"})
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv)
        srv.fail_types = {"text"}
        for i in range(5):
            self.assertFalse(ch._send_bubbles("group", "9001", [f"lost{i}"]))
        self.assertEqual(self.log_rows(d), 0, "failed sends were written to the send log")
        srv.fail_types = set()
        self.assertTrue(ch._send_bubbles("group", "9001", ["now it works"]))
        self.assertEqual(self.log_rows(d), 1)

    def test_1_direct_hourly_cap_follows_onebot_max_per_hour(self) -> None:
        d = self.mk_daemon()
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv, max_per_hour=100)
        for i in range(70):
            self.assertTrue(ch._send_bubbles("group", "9001", [f"m{i}"]), f"reply {i} refused by hourly cap")
        # an explicit gate override still wins (a stricter gate than the channel's own limiter)
        d2 = self.mk_daemon({"AI_HOUSE_DIRECT_HOURLY_CAP": "5"})
        ch2 = self.mk_channel(d2, srv, start=False, max_per_hour=100)
        ch2._host = d2
        results = []
        for i in range(7):
            try:
                key = d2.send_gate.authorize_direct_send("onebot_v11", ch2, kind="group", target="9001", actor="sora",
                                                         text=f"x{i}", delivery_id=f"cap-{i}")
                d2.send_gate.record_direct_sent(f"cap-{i}", key)
                results.append("ok")
            except NotSent as exc:
                results.append(exc.reason)
        self.assertEqual(results, ["ok"] * 5 + ["hourly_cap"] * 2)

    def test_1_same_reply_authorised_twice_or_resumed_is_charged_once(self) -> None:
        d = self.mk_daemon({"AI_HOUSE_DIRECT_DAILY_CAP": "1"})
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv)
        sid = self.new_sid(ch, "m-once")
        self.assertTrue(ch._send_bubbles("group", "9001", ["first"], spool_ids=[sid]))
        self.assertEqual(self.log_rows(d), 1)
        # the very same reply is authorised again (reply_ready re-run from part 0): allowed, not charged twice
        self.assertTrue(ch._send_bubbles("group", "9001", ["first"], spool_ids=[sid]))
        self.assertEqual(self.log_rows(d), 1)
        # a resume of a reply that already sent part 1 of 2 goes out without a new charge
        sid2 = self.new_sid(ch, "m-resume")
        ch._spool.save_reply_plan([sid2], ["a", "b"], target="9001", kind="group", at="")
        ch._spool.record_part_sent([sid2], 1)
        d.send_gate.reset()
        key2 = d.send_gate.authorize_direct_send("onebot_v11", ch, kind="group", target="9001", actor="sora", text="a",
                                                 delivery_id=f"ob-spool-{sid2}")
        d.send_gate.record_direct_sent(f"ob-spool-{sid2}", key2)
        before = self.log_rows(d)
        ch._resume_ready_event({"_resume_plan": {"bubbles": ["a", "b"], "kind": "group", "target": "9001", "at": ""},
                                "_sent_parts": 1, "_spool_ids": [sid2]})
        self.assertEqual(self.log_rows(d), before)
        self.assertEqual(self.spool_row(ch, sid2)["state"], "done")

    def test_1_send_log_retention_and_index(self) -> None:
        d = self.mk_daemon()
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv)
        self.assertTrue(ch._send_bubbles("group", "9001", ["warm up"]))
        old = T0 - 20 * 86400
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                for i in range(50):
                    c.execute("INSERT INTO send_gate_log(delivery_id, recip_key, day_jst, sent_at) VALUES(?,?,?,?)",
                              (f"old-{i}", "direct:onebot_v11:9001", "2026-11-02", old))
        d.clock.t = T0 + 700  # past any prune throttle
        self.assertTrue(ch._send_bubbles("group", "9001", ["trigger prune"]))
        self.assertEqual(self.log_rows(d, "delivery_id LIKE 'old-%'"), 0, "old log rows were never cleaned")
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            from ai_house import send_gate as sg
            plan = " ".join(str(r[3]) for r in c.execute("EXPLAIN QUERY PLAN " + sg.DAILY_COUNT_SQL, ("direct", "2026-11-22")))
        self.assertIn("INDEX", plan.upper())
        self.assertNotIn("SCAN SEND_GATE_LOG", plan.upper().replace("SCAN SEND_GATE_LOG USING", ""))

    def test_1_legacy_send_gate_log_is_migrated(self) -> None:
        d = self.mk_daemon()
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv)
        day = dt.datetime.fromtimestamp(T0, JST).date().isoformat()
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                c.execute("DROP TABLE send_gate_log")
                c.execute("CREATE TABLE send_gate_log (id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT UNIQUE, "
                          "recip_key TEXT NOT NULL, day_jst TEXT NOT NULL, sent_at REAL NOT NULL)")
                for i in range(70):
                    c.execute("INSERT INTO send_gate_log(delivery_id, recip_key, day_jst, sent_at) VALUES(?,?,?,?)",
                              (f"legacy-d-{i}", "direct:onebot_v11:9001", day, T0 - 5000 - i))
                for i in range(60):
                    c.execute("INSERT INTO send_gate_log(delivery_id, recip_key, day_jst, sent_at) VALUES(?,?,?,?)",
                              (f"legacy-o-{i}", f"onebot_v11:90{i}", day, T0 - 5000 - i))
        payload = {"actor": "sora", "kind": "group", "group_id": "9001", "text": "p"}
        # 60 legacy outbox rows already used up the outbox day; 70 legacy direct rows must not count towards it
        with self.assertRaises(NotSent) as ctx:
            d.send_gate.deliver_gated("onebot_v11", ch, "dlv-legacy", dict(payload), is_webchat=False)
        self.assertEqual(ctx.exception.reason, "daily_cap")
        self.assertTrue(ch._send_bubbles("group", "9001", ["direct still fine"]))
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(send_gate_log)")}
        self.assertIn("kind", cols)

    def test_1_legacy_direct_rows_do_not_block_outbox(self) -> None:
        d = self.mk_daemon()
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv)
        day = dt.datetime.fromtimestamp(T0, JST).date().isoformat()
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                c.execute("DROP TABLE send_gate_log")
                c.execute("CREATE TABLE send_gate_log (id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT UNIQUE, "
                          "recip_key TEXT NOT NULL, day_jst TEXT NOT NULL, sent_at REAL NOT NULL)")
                for i in range(70):
                    c.execute("INSERT INTO send_gate_log(delivery_id, recip_key, day_jst, sent_at) VALUES(?,?,?,?)",
                              (f"legacy-d-{i}", "direct:onebot_v11:9001", day, T0 - 5000 - i))
        ref = d.send_gate.deliver_gated("onebot_v11", ch, "dlv-after-legacy",
                                        {"actor": "sora", "kind": "group", "group_id": "9001", "text": "p"}, is_webchat=False)
        self.assertTrue(ref)


# ---------------------------------------------------------------------------------------------------------------
class VisibilityTests(Base):
    def test_2_limited_reply_leaves_reason_in_spool_and_status_without_spamming(self) -> None:
        d = self.mk_daemon({"AI_HOUSE_DIRECT_DAILY_CAP": "1"})
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv)
        self.assertTrue(ch._send_bubbles("group", "9001", ["one"], spool_ids=[self.new_sid(ch, "v1")]))
        sid = self.new_sid(ch, "v2")
        self.assertFalse(ch._send_bubbles("group", "9001", ["two"], spool_ids=[sid]))
        row = self.spool_row(ch, sid)
        self.assertEqual(row["state"], "skipped")
        self.assertIn("daily_cap", str(row.get("reason")))
        status = ch.status_view()
        self.assertEqual(status["last_limit"]["reason"], "daily_cap")
        self.assertGreaterEqual(status["limited"], 1)
        self.assertIn("direct_limited", d.send_gate.stats_view())
        self.assertGreaterEqual(d.send_gate.stats_view()["direct_limited"], 1)
        self.assertEqual(len(srv.frames), 1, "the limited reply (or a notice about it) reached QQ")


# ---------------------------------------------------------------------------------------------------------------
class RichMediaTests(Base):
    IMG = {"kind": "image", "image_base64": IMG_B64}
    AUD = {"kind": "audio", "audio_base64": AUD_B64}

    def made_for_the_group(self, d: Any, *items: Mapping[str, Any]) -> None:
        """What the built-in media tools do when they make or pick media: bind the bytes to the chat and to the text
        they were made from. A group is sent only media that has this record (SPEC G5/G7/G12)."""
        for it in items:
            b64 = it.get("image_base64") or it.get("audio_base64")
            d.media_provenance.register(b64, chat="qq-group:9001", source_texts=["a small test picture"], kind=it["kind"])

    def test_3_failed_media_send_is_recorded_and_not_reported_done(self) -> None:
        d = self.mk_daemon()
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        srv.fail_types = {"image"}
        ch = self.mk_channel(d, srv)
        self.made_for_the_group(d, self.IMG)  # group media needs its record; this test is about the wire failing
        sid = self.new_sid(ch, "m-fail")
        ok = ch._send_bubbles("group", "9001", ["text first"], spool_ids=[sid], media=[self.IMG])
        self.assertTrue(ok, "the text bubble did go out")
        self.assertGreaterEqual(ch.stats["send_failed"], 1)
        self.assertNotEqual(self.spool_row(ch, sid)["state"], "done")

    def test_3_media_is_resumed_after_reconnect(self) -> None:
        d = self.mk_daemon()
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv, start=False)
        ch._host = d
        ch._spool = ChannelSpool(self.tmp)
        self.addCleanup(ch._spool.close)
        self.made_for_the_group(d, self.IMG, self.AUD)
        sid = self.new_sid(ch, "m-resume-media")
        self.assertFalse(ch._send_bubbles("group", "9001", ["hello"], spool_ids=[sid], media=[self.IMG, self.AUD]))
        self.assertEqual(self.spool_row(ch, sid)["state"], "reply_ready")
        rec = ch._spool.recover_unfinished("onebot_v11", only_ready=True)
        self.assertEqual(len(rec), 1)
        self.assertEqual(len((rec[0]["plan"].get("media") or [])), 2, "media was not saved in the reply plan")
        ch.start(d)  # reconnects; the worker resumes the saved reply
        self.addCleanup(ch.stop)
        self.assertTrue(_wait_until(lambda: self.spool_row(ch, sid)["state"] == "done", 8.0),
                        f"reply never resumed: {self.spool_row(ch, sid)}")
        self.assertEqual(srv.seg_types().count("text"), 1)
        self.assertEqual(srv.seg_types().count("image"), 1)
        self.assertEqual(srv.seg_types().count("record"), 1)

    def test_3_resume_after_text_sent_sends_only_the_media(self) -> None:
        d = self.mk_daemon()
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv)
        self.made_for_the_group(d, self.IMG)
        sid = self.new_sid(ch, "m-resume-2")
        plan_media = [dict(self.IMG)]
        ch._spool.save_reply_plan([sid], ["hello"], target="9001", kind="group", at="", media=plan_media)
        ch._spool.record_part_sent([sid], 1)
        ch._resume_ready_event({
            "_resume_plan": {"bubbles": ["hello"], "kind": "group", "target": "9001", "at": "", "media": plan_media},
            "_sent_parts": 1, "_spool_ids": [sid]})
        self.assertEqual(srv.seg_types(), ["image"])
        self.assertEqual(self.spool_row(ch, sid)["state"], "done")

    def test_3_media_only_reply_is_sent(self) -> None:
        d = self.mk_daemon()
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        host = _Host(d, [{"mode": "live", "text": "", "media": [dict(self.IMG)]}])
        ch = self.mk_channel(d, srv, host=host)
        ev = {"post_type": "message", "message_type": "private", "self_id": "7001", "user_id": "8001",
              "message_id": "m-media-only", "time": int(time.time()), "sender": {"nickname": "boss"},
              "message": [{"type": "text", "data": {"text": "send me a picture"}}]}
        self.assertEqual(ch.handle_event(ev), "replied")
        self.assertEqual(srv.seg_types(), ["image"])

    def test_3_media_goes_through_destination_check_and_quota(self) -> None:
        d = self.mk_daemon({"AI_HOUSE_DIRECT_DAILY_CAP": "2"})
        srv = _FakeOneBotServer()
        self.addCleanup(srv.close)
        ch = self.mk_channel(d, srv, groups=frozenset({"9001"}))
        # a group that is not whitelisted: media-only is blocked before the wire
        self.assertFalse(ch._send_bubbles("group", "99999", [], media=[self.IMG]))
        self.assertEqual(srv.frames, [])
        # changed from "a bare image is sent into the group": an image nobody made for this chat is NOT sent (SPEC G5),
        # and the refused try costs nothing, so the two quota slots below are still there
        self.assertFalse(ch._send_bubbles("group", "9001", [], media=[self.IMG]))
        self.assertEqual(srv.frames, [])
        self.assertEqual(self.log_rows(d), 0)
        self.made_for_the_group(d, self.IMG)
        # media-only replies that have their record are charged like any other reply
        self.assertTrue(ch._send_bubbles("group", "9001", [], media=[self.IMG]))
        self.assertTrue(ch._send_bubbles("group", "9001", [], media=[self.IMG]))
        self.assertFalse(ch._send_bubbles("group", "9001", [], media=[self.IMG]))
        self.assertEqual(srv.seg_types(), ["image", "image"])


# ---------------------------------------------------------------------------------------------------------------
class SplitBubbleTests(unittest.TestCase):
    def test_4_no_bubble_exceeds_hard_limit_and_total_is_bounded(self) -> None:
        paras = [("今天天气很好，我们去公园散步吧。" * 25) for _ in range(7)]  # ~2800 chars, 7 paragraphs
        text = "\n\n".join(paras)
        self.assertGreater(len(text), 2400)
        parts = split_bubbles(text, max_part_chars=400, max_parts=3)
        self.assertLessEqual(max(len(p) for p in parts), 800)
        self.assertLessEqual(len(parts), 9)
        self.assertNotIn("省略", parts[-1], "nothing was dropped, so no truncation notice is expected")
        self.assertEqual("".join(parts).replace("\n", ""), text.replace("\n", ""))

    def test_4_huge_reply_is_truncated_with_a_visible_notice(self) -> None:
        text = "\n\n".join(("很长的内容，" * 60) for _ in range(60))
        parts = split_bubbles(text, max_part_chars=400, max_parts=3)
        self.assertLessEqual(max(len(p) for p in parts), 800)
        self.assertLessEqual(len(parts), 12)
        self.assertIn("省略", parts[-1])

    def test_4_sep_variants(self) -> None:
        for sep in ("<sep>", "<sep/>", "<sep />", "</sep>", "<SEP>", " <sep/> "):
            self.assertEqual(split_bubbles(f"甲{sep}乙{sep}丙", max_parts=5), ["甲", "乙", "丙"], sep)

    def test_4_code_and_urls_survive_whole(self) -> None:
        code = "```python\n" + "x = 1\n" * 90 + "```"  # ~550 chars, longer than the soft limit
        url = "https://example.com/" + "a" * 120
        text = f"前言<sep>{code}<sep>看这个 {url} 就行<sep>后记"
        parts = split_bubbles(text, max_part_chars=400, max_parts=3)
        self.assertLessEqual(max(len(p) for p in parts), 800)
        self.assertTrue(any(code in p for p in parts), "code block was cut")
        self.assertTrue(any(url in p for p in parts), "URL was cut")

    def test_4_oversized_code_block_with_a_long_first_line_stays_under_the_hard_limit(self) -> None:
        body = "\n".join(f"line {i} = {i * 7}" for i in range(260))  # ~3600 chars: has to be cut
        for first_line in ("x" * 300, "这一行不是语言标记而是一段很长的说明" * 20, "python"):
            with self.subTest(first_line=first_line[:12]):
                parts = split_bubbles(f"```{first_line}\n{body}\n```", max_part_chars=400, max_parts=3)
                self.assertLessEqual(max(len(p) for p in parts), 800)
                joined = "\n".join(parts)
                self.assertIn("line 0 = 0", joined)
                self.assertIn("line 100 = 700", joined)
                for p in parts:
                    if "line " in p:
                        self.assertTrue(p.startswith("```") and p.rstrip().endswith("```"), p[:40])


# ---------------------------------------------------------------------------------------------------------------
class OutboxCrashTests(Base):
    class _Ch:
        def __init__(self, idempotent: bool) -> None:
            self.idempotent = idempotent
            self.sent: List[str] = []

        def send(self, delivery_id: str, payload: Mapping[str, Any]) -> str:
            self.sent.append(delivery_id)
            return f"ref-{delivery_id}"

        def lookup(self, delivery_id: str) -> Optional[str]:
            return None

    def _state(self, d: Any, did: str) -> str:
        return d.authority.store.db.execute("SELECT state FROM outbox WHERE delivery_id=?", (did,)).fetchone()[0]

    def _crashed_row(self, d: Any) -> str:
        did = enqueue_outbound(d.authority.store, channel="c", payload={"text": "x"})
        with d.authority.store.tx() as db:
            db.execute("UPDATE outbox SET state='sending', attempts=1 WHERE delivery_id=?", (did,))
        return did

    def test_5_crashed_sending_row_on_non_idempotent_channel_becomes_unknown(self) -> None:
        d = self.mk_daemon()
        did = self._crashed_row(d)
        disp = Dispatcher(d.authority.store, {"c": self._Ch(False)})
        self.assertEqual(disp.reconcile(), {did: "unknown"})
        self.assertEqual(self._state(d, did), "unknown")
        self.assertEqual(disp.reconcile(), {did: "unknown"})
        self.assertEqual(disp.run_once(), {})

    def test_5_crashed_sending_row_on_idempotent_channel_is_retried_once(self) -> None:
        d = self.mk_daemon()
        did = self._crashed_row(d)
        ch = self._Ch(True)
        disp = Dispatcher(d.authority.store, {"c": ch})
        self.assertEqual(disp.reconcile(), {did: "pending"})
        self.assertEqual(self._state(d, did), "pending")
        self.assertEqual(disp.run_once(), {did: "sent"})
        self.assertEqual(ch.sent, [did])
        self.assertEqual(disp.reconcile(), {})


# ---------------------------------------------------------------------------------------------------------------
class ProactiveAndSpoolTests(Base):
    def test_6_proactive_without_loaded_channel_does_not_trigger_or_charge(self) -> None:
        d = self.mk_daemon({"ONEBOT_GROUPS": "9001"}, llm_fn=lambda s, u: ("我在泡茶", 5))
        d.update_plugin_settings({"plugin_id": "proactive_talk", "enabled": True})
        d.proactive.anchor(d.world_db, now=T0)
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute("UPDATE reality SET availability='free', doing='喝茶' WHERE actor='sora'")
                wc.execute("INSERT INTO script(ts, actors, text, source, created) VALUES(?,?,?,'kernel',?)",
                           (T0 + 30, '["sora"]', "苍空坐在窗边", T0 + 30))
        r = d.proactive.scan(d, cfg={"target_channel": "onebot_v11", "target_scope": "group",
                                     "window": "09:00-22:30"}, now=T0 + 60)
        self.assertFalse(r["triggered"], r)
        self.assertEqual(r["reason"], "channel_unavailable")
        self.assertEqual(d.proactive.status_view(now=T0 + 60).get("house_count"), 0)
        n = d.authority.store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.assertEqual(n, 0)

    def test_6_prune_removes_long_stuck_processing_rows(self) -> None:
        now = [1000.0]
        with tempfile.TemporaryDirectory() as tmp:
            sp = ChannelSpool(tmp, clock=lambda: now[0])
            self.addCleanup(sp.close)
            sid = sp.enqueue_event("onebot_v11", {"m": 1}, message_id="p1", session_key="private:8001", kind="private",
                                   target="8001", uid="8001")
            sp.mark_processing([sid])
            self.assertEqual(sp.prune(now=1000.0 + 3600), 0, "a recent processing row must stay")
            self.assertEqual(len(sp.list_rows()), 1)
            sp.prune(now=1000.0 + 3 * 86400)
            self.assertEqual(sp.list_rows(), [])


if __name__ == "__main__":
    unittest.main()
