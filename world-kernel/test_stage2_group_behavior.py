"""Regression tests for Stage 2.2: group reply probability, streak cap, mute commands & sleep silence."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
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

from ai_house.chat_identity import member_speaker, owner_speaker
from ai_house.daemon import HouseDaemon, make_http_server

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


class Stage2GroupBehaviorTests(unittest.TestCase):
    def test_reply_prob_streak_cap_owner_mute_and_http_controls(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        owner_tok = "tok-owner-gb-alpha-key"
        day_ts = dt.datetime(2026, 11, 22, 14, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={
                "AI_HOUSE_OWNER_TOKEN": owner_tok,
                "AI_HOUSE_GROUP_REPLY_PROB": "1.0",
                "AI_HOUSE_GROUP_MAX_STREAK": "2",
                "AI_HOUSE_GROUP_STREAK_RESET_S": "300",
                "AI_HOUSE_GROUP_MUTE_MINUTES": "20",
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

        member = member_speaker("路人阿晴", "8002")
        owner = owner_speaker("房主")

        # 1) Member streak cap = 2: first two get replies, 3rd skips with streak_cap
        r1 = d.converse("qq-group:9001", "大家好呀", member, actor="sora")
        r2 = d.converse("qq-group:9001", "在忙什么", member, actor="sora")
        r3 = d.converse("qq-group:9001", "第三句", member, actor="sora")
        self.assertTrue(r1["replies"])
        self.assertTrue(r2["replies"])
        self.assertEqual(r3["replies"], [])
        self.assertEqual(r3["ignored"], "streak_cap")

        # Advancing past streak_reset_s resets streak automatically
        d.clock.advance(305)
        r4 = d.converse("qq-group:9001", "隔五分钟再问", member, actor="sora")
        self.assertTrue(r4["replies"])

        # Owner message resets streak too
        r_own = d.converse("qq-group:9001", "我回来了", owner, actor="sora")
        self.assertTrue(r_own["replies"])
        self.assertEqual(d.group_behavior.status_view()["streak"].get("qq-group:9001", 0), 0)

        # 2) Non-owner saying "闭麦" cannot mute; owner saying "闭麦10分钟" mutes group until "开麦" or timeout
        r_fake_mute = d.converse("qq-group:9001", "闭麦", member, actor="sora")
        self.assertTrue(r_fake_mute["replies"])

        r_mute = d.converse("group", "先闭麦 10 分钟", owner)
        self.assertEqual(r_mute["replies"], [])
        self.assertEqual(r_mute["ignored"], "muted_ack")

        # While muted, even subsequent group messages are silenced, while private DM still works
        self.assertEqual(d.converse("group", "苍空在吗", owner)["ignored"], "muted")
        self.assertTrue(d.converse("private:sora", "私聊问个事", owner)["replies"])

        # Owner voice unmute restores group responses immediately
        self.assertEqual(d.converse("group", "开麦", owner)["ignored"], "unmuted_ack")
        self.assertTrue(d.converse("group", "现在可以说话啦", owner)["replies"])

        # 3) Prob = 0.0 suppresses non-owner group turns cleanly while letting owner through
        st_u, _ = _http_json(
            f"{base}/api/settings/plugins",
            token=owner_tok,
            body={"plugin_id": "group_behavior", "enabled": True, "values": {"reply_prob": 0.0, "max_streak": 8}},
        )
        self.assertEqual(st_u, 200)
        r_prob = d.converse("group", "成员插话", member)
        self.assertEqual(r_prob["replies"], [])
        self.assertEqual(r_prob["ignored"], "prob_skipped")
        self.assertTrue(d.converse("group", "房主问话", owner)["replies"])

        # 4) HTTP /api/group/behavior inspect, mute & unmute
        st_m, muted_view = _http_json(
            f"{base}/api/group/behavior",
            token=owner_tok,
            body={"op": "mute", "channel": "group", "minutes": 5},
        )
        self.assertEqual(st_m, 200)
        self.assertIn("group", muted_view["active_mutes"])
        st_unm, unmuted_view = _http_json(
            f"{base}/api/group/behavior",
            token=owner_tok,
            body={"op": "unmute", "channel": "group"},
        )
        self.assertEqual(st_unm, 200)
        self.assertEqual(unmuted_view["active_mutes"], {})

    def test_sleep_silence_in_group_vs_private(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        night_ts = dt.datetime(2026, 11, 22, 3, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={},
            start_ts=night_ts,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        # Force sora sleeping in life.db and world.db so sleeping guard triggers cleanly
        with contextlib.closing(sqlite3.connect(d.life_db)) as lc:
            with lc:
                lc.execute("UPDATE actors SET availability='sleeping' WHERE actor_id='sora'")
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute("UPDATE reality SET availability='sleeping', doing='睡觉' WHERE actor='sora'")

        # Group message targeting sleeping sora stays silent under sleep_silence=True
        res_grp = d.converse("qq-group:9001", "夜宵吃什么", member_speaker("夜猫子", "8003"), actor="sora")
        self.assertEqual(res_grp["replies"], [])
        self.assertEqual(res_grp["ignored"], "sleep_silence")

        # Private DM still returns sleeping placeholder
        res_priv = d.converse("private:sora", "睡了吗", owner_speaker("房主"))
        self.assertEqual(res_priv["replies"][0]["mode"], "sleeping")


if __name__ == "__main__":
    unittest.main()
