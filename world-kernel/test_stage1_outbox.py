"""Regression tests for Stage 1.6: outbox channel bridge, SendGate & v0.3.0 release checks."""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import threading
import time
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

from ai_house import __version__
from ai_house.builtin_plugins.onebot_ws import OneBotChannel, OneBotConfig
from ai_house.chat_identity import owner_speaker
from ai_house.daemon import HouseDaemon, make_http_server
from worldkernel import outbox

JST = dt.timezone(dt.timedelta(hours=9))


def _http_json(url: str, *, token: str = "", body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
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
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        exc.close()
        return exc.code, json.loads(raw or "{}")


class _MockOneBotWSServer:
    """Loopback OneBot WS server that records outbound actions and toggles echo timeout."""

    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []
        self.drop_echo = False
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._server = serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self, ws: Any) -> None:
        self.connected.set()
        for raw in ws:
            if self._stop.is_set():
                return
            frame = json.loads(raw)
            self.actions.append(frame)
            if self.drop_echo:
                continue
            ws.send(json.dumps({
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": 100 + len(self.actions)},
                "echo": frame.get("echo"),
            }))

    def close(self) -> None:
        self._stop.set()
        self._server.shutdown()
        self._thread.join(timeout=2.0)


class Stage1OutboxAndSendGateTests(unittest.TestCase):
    def test_send_gate_blocks_leaks_and_bots_defers_quiet_and_rate_caps(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        owner_tok = "tok-owner-outbox-alpha-key"
        day_ts = dt.datetime(2026, 11, 22, 14, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={
                "AI_HOUSE_OWNER_TOKEN": owner_tok,
                "AI_HOUSE_QUIET_HOURS": "23:30-07:00",
                "AI_HOUSE_OUTBOX_HOURLY_CAP": "2",
                "AI_HOUSE_OUTBOX_DAILY_CAP": "3",
            },
            start_ts=day_ts,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        srv = make_http_server(d, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 2.0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{port}"

        # Version bump check on /api/health
        self.assertEqual(__version__, "0.3.0")
        st_h, hdoc = _http_json(f"{base}/api/health")
        self.assertEqual(st_h, 200)
        self.assertEqual(hdoc["version"], "0.3.0")

        # Seed private memory leak canary & private secret phrase for sora
        secret_note = "我藏在书柜夹层里的日记密码"
        d.memory.add("sora", "private", f"房主的私密事项：{secret_note}", kind="fact", source="chat", now=day_ts)
        d.identity_store.upsert_identity("bot:timer", kind="bot", display_name="计时机器人")

        # P1a request_delivery stays strictly intact (rejects non-template notices)
        with self.assertRaises(ValueError):
            with d.authority.store.tx():
                outbox.request_delivery(d.authority.store, 1, "webchat", "freeform", "sora")

        # Enqueue: 1) leak guard secret; 2) private-to-public memory leak; 3) bot recipient; 4) unknown channel; 5) valid msg
        did_sec = d.enqueue_outbound(channel="webchat", payload={"actor": "sora", "kind": "group", "text": f"令牌是 {owner_tok}"})
        did_mem = d.enqueue_outbound(channel="webchat", payload={"actor": "sora", "kind": "group", "scope": "group", "text": f"其实{secret_note}"})
        did_bot = d.enqueue_outbound(channel="webchat", payload={"actor": "sora", "kind": "private", "to_identity": "bot:timer", "text": "在吗"})
        did_unreg = d.enqueue_outbound(channel="disabled_ch", payload={"actor": "sora", "text": "未启用渠道"})
        did_ok1 = d.enqueue_outbound(channel="webchat", payload={"actor": "sora", "kind": "private", "to_identity": "user:owner", "text": "茶已经泡好啦"})

        res = d.flush_outbox()
        self.assertEqual(res["dispatched"][did_sec], "blocked")
        self.assertEqual(res["dispatched"][did_mem], "blocked")
        self.assertEqual(res["dispatched"][did_bot], "blocked")
        self.assertNotIn(did_unreg, res["dispatched"])  # disabled channel untouched
        self.assertEqual(res["dispatched"][did_ok1], "sent")

        # Hourly cap = 2 for same recipient: 2nd sends, 3rd defers as pending
        did_ok2 = d.enqueue_outbound(channel="webchat", payload={"actor": "sora", "kind": "private", "to_identity": "user:owner", "text": "第二句提醒"})
        did_cap = d.enqueue_outbound(channel="webchat", payload={"actor": "sora", "kind": "private", "to_identity": "user:owner", "text": "第三句应限流"})
        res2 = d.flush_outbox()
        self.assertEqual(res2["dispatched"][did_ok2], "sent")
        self.assertEqual(res2["dispatched"][did_cap], "pending")

        # Advance 65 minutes: hourly cap clears and daily cap=3 sends the 3rd total message
        d.clock.advance(3900)
        res3 = d.flush_outbox()
        self.assertEqual(res3["dispatched"][did_cap], "sent")

        # Daily cap=3 reached: 4th fails cap until next JST day
        did_daily = d.enqueue_outbound(channel="webchat", payload={"actor": "rin", "kind": "private", "to_identity": "user:owner", "text": "第四句撞日上限"})
        self.assertEqual(d.flush_outbox()["dispatched"][did_daily], "pending")

        # Next day daytime (10:00 JST) clears daily cap, then night (23:45 JST) triggers quiet hours unless urgent=True
        d.clock.t = dt.datetime(2026, 11, 23, 10, 0, tzinfo=JST).timestamp()
        self.assertEqual(d.flush_outbox()["dispatched"][did_daily], "sent")

        d.clock.t = dt.datetime(2026, 11, 23, 23, 45, tzinfo=JST).timestamp()
        did_quiet = d.enqueue_outbound(channel="webchat", payload={"actor": "sora", "kind": "private", "to_identity": "user:owner", "text": "睡前晚安"})
        did_urgent = d.enqueue_outbound(channel="webchat", payload={"actor": "sora", "kind": "private", "to_identity": "user:owner", "text": "紧急提醒", "urgent": True})
        res_q = d.flush_outbox()
        self.assertEqual(res_q["dispatched"][did_quiet], "pending")
        self.assertEqual(res_q["dispatched"][did_urgent], "sent")

        # Morning (08:00 JST) flushes quiet-hours deferred delivery cleanly via HTTP API
        d.clock.t = dt.datetime(2026, 11, 24, 8, 0, tzinfo=JST).timestamp()
        st_f, flush_doc = _http_json(f"{base}/api/outbox/flush", token=owner_tok, body={})
        self.assertEqual(st_f, 200)
        self.assertEqual(flush_doc["dispatched"][did_quiet], "sent")

        st_o, out_doc = _http_json(f"{base}/api/outbox?limit=20", token=owner_tok)
        self.assertEqual(st_o, 200)
        states = {it["delivery_id"]: it["state"] for it in out_doc["items"]}
        self.assertEqual(states[did_sec], "blocked")
        self.assertEqual(states[did_mem], "blocked")
        self.assertEqual(states[did_bot], "blocked")
        self.assertEqual(states[did_unreg], "pending")
        self.assertEqual(states[did_quiet], "sent")

    def test_onebot_outbox_delivery_and_unknown_timeout_reconcile(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ws_srv = _MockOneBotWSServer()
        self.addCleanup(ws_srv.close)

        day_ts = dt.datetime(2026, 11, 22, 12, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_QUIET_HOURS": "23:30-07:00"},
            start_ts=day_ts,
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
                send_timeout_s=0.15,
            )
        )
        d.channels = [ch]
        d._active_channel_ids = {"onebot_v11"}
        ch.start(d)
        self.addCleanup(ch.stop)
        self.assertTrue(ws_srv.connected.wait(timeout=3.0))

        # Wait until _ws is attached on channel client
        deadline = time.monotonic() + 3.0
        while ch._ws is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(ch._ws)

        # 1) Normal outbound DM resolved from to_identity='user:owner' -> bound onebot '8001'
        did_dm = d.enqueue_outbound(
            channel="onebot_v11",
            payload={"actor": "sora", "kind": "private", "to_identity": "user:owner", "text": "午休时间到啦"},
        )
        res = d.flush_outbox()
        self.assertEqual(res["dispatched"][did_dm], "sent")
        self.assertEqual(ws_srv.actions[-1]["action"], "send_private_msg")
        self.assertEqual(ws_srv.actions[-1]["params"]["user_id"], 8001)

        # 2) Drop echo -> send times out after leaving wire -> outbox moves to 'unknown' (not blindly resent)
        ws_srv.drop_echo = True
        before_actions = len(ws_srv.actions)
        did_to = d.enqueue_outbound(
            channel="onebot_v11",
            payload={"actor": "sora", "kind": "group", "target": "9001", "at": "8002", "text": "下午一起喝咖啡吗"},
        )
        res_to = d.flush_outbox()
        self.assertEqual(res_to["dispatched"][did_to], "unknown")
        self.assertEqual(len(ws_srv.actions), before_actions + 1)

        # Second flush while still unknown does NOT re-send on non-idempotent channel
        res_re = d.flush_outbox()
        self.assertEqual(res_re["reconciled"][did_to], "unknown")
        self.assertEqual(len(ws_srv.actions), before_actions + 1)

    def test_send_gate_destination_scope_whitelist_quota_and_webchat_idempotency_across_restart(self) -> None:
        import contextlib
        import sqlite3

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        day_ts = dt.datetime(2026, 11, 22, 14, 0, tzinfo=JST).timestamp()
        env = {
            "AI_HOUSE_QUIET_HOURS": "23:30-07:00",
            "AI_HOUSE_OUTBOX_HOURLY_CAP": "10",
            "AI_HOUSE_OUTBOX_DAILY_CAP": "1",
        }
        d1 = HouseDaemon(
            rt,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env=env,
            start_ts=day_ts,
            virtual_clock=True,
            allow_onboarding=False,
        )
        ws_srv = _MockOneBotWSServer()
        self.addCleanup(ws_srv.close)
        ch1 = OneBotChannel(
            OneBotConfig(url=ws_srv.url, owners=frozenset({"8001"}), groups=frozenset({"9001"}), actor="sora")
        )
        d1.channels = [ch1]
        d1._active_channel_ids = {"onebot_v11"}
        ch1.start(d1)
        try:
            self.assertTrue(ws_srv.connected.wait(timeout=3.0))

            # Seed private secret phrase in sora's private memory
            secret_note = "小仓鼠秘密储藏室"
            d1.memory.add("sora", "private", f"房主的私密事项：{secret_note}", kind="fact", source="chat", now=day_ts)

            # 1a) Spoofed payload with actor + target_channel='group' (omitting kind) must be checked as group & blocked
            did_spoof1 = d1.enqueue_outbound(
                channel="webchat",
                payload={"actor": "sora", "target_channel": "group", "text": f"其实{secret_note}"},
            )
            # 1b) Conflicting explicit scope='private' + target_channel='group' must be rejected as inconsistent_scope
            did_spoof2 = d1.enqueue_outbound(
                channel="webchat",
                payload={"actor": "sora", "kind": "private", "scope": "private", "target_channel": "group", "text": "正常提醒"},
            )
            # 1c) OneBot group outbound to un-whitelisted group 9999 must be blocked with zero frames sent
            did_unwhitelisted = d1.enqueue_outbound(
                channel="onebot_v11",
                payload={"actor": "sora", "kind": "group", "target": "9999", "text": "未加白名单的群"},
            )
            # 1d) First real private delivery uses daily cap=1
            did_first = d1.enqueue_outbound(
                channel="webchat",
                payload={"actor": "sora", "kind": "private", "to_identity": "user:owner", "text": "第一条占用日限"},
            )
            res1 = d1.flush_outbox()
            self.assertEqual(res1["dispatched"][did_spoof1], "blocked")
            self.assertEqual(res1["dispatched"][did_spoof2], "blocked")
            self.assertEqual(res1["dispatched"][did_unwhitelisted], "blocked")
            self.assertEqual(ws_srv.actions, [])
            self.assertEqual(res1["dispatched"][did_first], "sent")

            # Queue 2nd message hitting daily_cap=1, PLUS simulate mid-send crash on `did_crash`:
            # `webchat` wrote chat_messages + webchat_deliveries while outbox row in life_db stayed `sending`.
            did_second_cap = d1.enqueue_outbound(
                channel="webchat",
                payload={"actor": "sora", "kind": "private", "to_identity": "user:owner", "text": "第二条应跨重启受日限拦截"},
            )
            res2 = d1.flush_outbox()
            self.assertEqual(res2["dispatched"][did_second_cap], "pending")

            did_crash = d1.enqueue_outbound(
                channel="webchat",
                payload={"actor": "sora", "kind": "private", "to_identity": "user:owner", "text": "断点恢复不重复落库"},
            )
            crash_ref = d1._deliver_webchat_outbox(
                did_crash,
                {"actor": "sora", "kind": "private", "to_identity": "user:owner", "text": "断点恢复不重复落库"},
            )
            with d1.authority.store.tx() as ldb:
                ldb.execute(
                    "UPDATE outbox SET state='sending', attempts=1 WHERE delivery_id=?",
                    (did_crash,),
                )
        finally:
            ch1.stop()
            d1.close()

        # Restart HouseDaemon cleanly on the SAME runtime_dir & timestamp
        d2 = HouseDaemon(
            rt,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env=env,
            start_ts=day_ts,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d2.close)
        # SendGate quota persists across restart -> did_second_cap STILL stays pending;
        # `did_crash` in state='sending' reconciles to 'sent' via webchat_deliveries without inserting a duplicate!
        res_after = d2.flush_outbox(reconcile=True)
        self.assertEqual(res_after["dispatched"][did_second_cap], "pending")
        self.assertEqual(res_after["reconciled"][did_crash], "sent")
        with contextlib.closing(sqlite3.connect(d2.world_db)) as wc, d2.authority.store.read() as lc:
            cnt = wc.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE text='断点恢复不重复落库'"
            ).fetchone()[0]
            ob_state = lc.execute(
                "SELECT state, external_ref FROM outbox WHERE delivery_id=?",
                (did_crash,),
            ).fetchone()
        self.assertEqual(cnt, 1)
        self.assertEqual(ob_state[0], "sent")
        self.assertEqual(ob_state[1], crash_ref)

        # Even if an old sending row without webchat_deliveries had been reverted to pending, re-sending is idempotent
        again_ref = d2._deliver_webchat_outbox(
            did_crash,
            {"actor": "sora", "kind": "private", "to_identity": "user:owner", "text": "断点恢复不重复落库"},
        )
        self.assertEqual(again_ref, crash_ref)
        with contextlib.closing(sqlite3.connect(d2.world_db)) as wc:
            self.assertEqual(
                wc.execute("SELECT COUNT(*) FROM chat_messages WHERE text='断点恢复不重复落库'").fetchone()[0],
                1,
            )

    def test_webchat_explicit_group_target_preserves_destination_and_rejects_conflict(self) -> None:
        import contextlib
        import sqlite3

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        day_ts = dt.datetime(2026, 11, 22, 15, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            rt,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_QUIET_HOURS": "23:30-07:00"},
            start_ts=day_ts,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        # Plugin specifying target='qq-group:9001' (without target_channel) MUST route to 'qq-group:9001', NOT 'group'
        did_specific = d.enqueue_outbound(
            channel="webchat",
            payload={"actor": "sora", "kind": "group", "target": "qq-group:9001", "text": "仅发往指定群房间提醒"},
        )
        # Conflicting target and target_channel MUST be rejected cleanly
        did_conflict = d.enqueue_outbound(
            channel="webchat",
            payload={
                "actor": "sora",
                "kind": "group",
                "target": "qq-group:9001",
                "target_channel": "qq-group:9002",
                "text": "冲突群目标",
            },
        )
        res = d.flush_outbox()
        self.assertEqual(res["dispatched"][did_specific], "sent")
        self.assertEqual(res["dispatched"][did_conflict], "blocked")

        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            rows_spec = wc.execute(
                "SELECT channel, text FROM chat_messages WHERE channel='qq-group:9001'"
            ).fetchall()
            rows_pub = wc.execute(
                "SELECT channel, text FROM chat_messages WHERE channel='group'"
            ).fetchall()
            rows_conf = wc.execute(
                "SELECT channel, text FROM chat_messages WHERE text='冲突群目标'"
            ).fetchall()
        self.assertEqual(len(rows_spec), 1)
        self.assertEqual(rows_spec[0][1], "仅发往指定群房间提醒")
        self.assertEqual(rows_pub, [])
        self.assertEqual(rows_conf, [])


if __name__ == "__main__":
    unittest.main()
