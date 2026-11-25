"""Stage 2.6 tests: scoped timed reminders (with urgent outbox delivery) & stateless mini-games."""
from __future__ import annotations

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
from typing import Any, Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.builtin_plugins.mini_games import (  # noqa: E402
    draw_fortune_slip,
    play_rps,
    random_pick,
    word_chain_step,
)
from ai_house.builtin_plugins.reminders import ReminderStore, cancel_reminder, list_reminders, set_reminder  # noqa: E402
from ai_house.chat_identity import member_speaker, owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, JST, make_http_server  # noqa: E402
from ai_house.plugin_api import ToolContext  # noqa: E402


def _http_json(url: str, *, token: str = "", body: Any = None) -> Tuple[int, Dict[str, Any]]:
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
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8") or "{}")


class Stage2RemindersAndGamesTests(unittest.TestCase):
    def test_stateless_mini_games_zero_side_effects(self) -> None:
        rps = play_rps("石头", seed_hint="seed-1")
        self.assertTrue(rps["ok"])
        self.assertIn(rps["bot"], ("石头", "剪刀", "布"))
        self.assertIn(rps["outcome"], ("win", "lose", "draw"))

        f1 = draw_fortune_slip("抽签", seed_hint="day-22")
        f2 = draw_fortune_slip("抽签", seed_hint="day-22")
        self.assertEqual(f1, f2)
        self.assertTrue(f1["verse"])

        picked = random_pick(["乌冬面", "寿喜锅", "咖喱饭"], count=2, seed_hint="dinner")
        self.assertEqual(len(picked["chosen"]), 2)
        self.assertEqual(len(set(picked["chosen"])), 2)

        chain = word_chain_step("星光")
        self.assertTrue(chain["ok"])
        self.assertTrue(chain["reply"].startswith("光"))

    def test_reminders_scope_isolation_permissions_due_outbox_and_routes(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        priv_tok = "priv-rem-tok-01"
        t0 = dt.datetime(2026, 11, 22, 23, 40, tzinfo=JST).timestamp()  # inside quiet hours to test urgent bypass!
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_PRIVATE_TOKEN": priv_tok},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        owner = owner_speaker("房主")
        alice = member_speaker("阿晴", "8002")
        bob = member_speaker("阿明", "8003")

        # 1) Create owner private reminder & two group member reminders
        ctx_priv = ToolContext(
            actor="sora", scope="private", channel="private:sora",
            speaker=owner, now=d.clock.now(), plugin_config={"max_active_per_user": 20}, daemon=d,
        )
        r_priv = set_reminder("喝热牛奶", in_minutes=10, ctx=ctx_priv)["reminder"]

        ctx_alice = ToolContext(
            actor="sora", scope="group", channel="group",
            speaker=alice, now=d.clock.now(), plugin_config={"max_active_per_user": 20}, daemon=d,
        )
        r_alice = set_reminder("关阳台灯", in_minutes=5, ctx=ctx_alice)["reminder"]

        ctx_bob = ToolContext(
            actor="sora", scope="group", channel="group",
            speaker=bob, now=d.clock.now(), plugin_config={"max_active_per_user": 20}, daemon=d,
        )
        r_bob = set_reminder("收衣服", in_minutes=15, ctx=ctx_bob)["reminder"]

        # 2) Scope & ownership isolation: group listing NEVER reveals private reminder
        grp_list = list_reminders(ctx=ctx_alice)["reminders"]
        grp_ids = {r["reminder_id"] for r in grp_list}
        self.assertEqual(grp_ids, {r_alice["reminder_id"], r_bob["reminder_id"]})
        self.assertNotIn(r_priv["reminder_id"], grp_ids)

        # Bob CANNOT cancel Alice's or Owner's private reminder; Bob CAN cancel his own
        with self.assertRaises(ValueError):
            cancel_reminder(r_alice["reminder_id"], ctx=ctx_bob)
        with self.assertRaises(ValueError):
            cancel_reminder(r_priv["reminder_id"], ctx=ctx_bob)
        c_bob = cancel_reminder(r_bob["reminder_id"], ctx=ctx_bob)["reminder"]
        self.assertEqual(c_bob["state"], "cancelled")

        # 3) Due reminders fire even during JST 23:45 quiet hours via urgent=True Outbox dispatch
        d.clock.advance(601)  # 10 min -> both r_alice (5m) and r_priv (10m) are due!
        fired = ReminderStore(d.runtime_dir).fire_due(d, now=d.clock.now())
        self.assertEqual(len(fired), 2)

        # Re-firing immediately must not duplicate
        self.assertEqual(ReminderStore(d.runtime_dir).fire_due(d, now=d.clock.now()), [])

        grp_msgs = d.list_chat("group")
        priv_msgs = d.list_chat("private:sora")
        self.assertTrue(any("关阳台灯" in m["text"] for m in grp_msgs))
        self.assertFalse(any("喝热牛奶" in m["text"] for m in grp_msgs))
        self.assertTrue(any("喝热牛奶" in m["text"] for m in priv_msgs))

        # 4) Verify HTTP plugin routes /api/plugins/reminders/list & /cancel
        srv = make_http_server(d, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(th.join, 2.0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{port}"

        r_new = set_reminder("明天寄明信片", at_jst="09:00", ctx=ctx_priv)["reminder"]
        st_l, res_l = _http_json(f"{base}/api/plugins/reminders/list", token=priv_tok)
        self.assertEqual(st_l, 200)
        self.assertTrue(any(r["reminder_id"] == r_new["reminder_id"] for r in res_l["reminders"]))

        st_x, res_x = _http_json(
            f"{base}/api/plugins/reminders/cancel",
            token=priv_tok,
            body={"reminder_id": r_new["reminder_id"]},
        )
        self.assertEqual(st_x, 200)
        self.assertEqual(res_x["reminder"]["state"], "cancelled")


class SetReminderSpecTests(unittest.TestCase):
    def test_the_tool_text_says_a_reminder_goes_to_the_person_who_asked(self) -> None:
        """Fails if the description does not say plainly that the message is delivered at that time to the person who
        asked for it, and that it is for when THEY ask to be reminded."""
        from ai_house.builtin_plugins.reminders import _SET_SPEC

        desc = _SET_SPEC["description"]
        self.assertIn("到点时把这条消息发给请你设提醒的那个人", desc)
        self.assertIn("对方要你提醒他某件事时才用", desc)
        self.assertIn("in_minutes", desc)
        self.assertIn("at_jst", desc)


if __name__ == "__main__":
    unittest.main()
