"""Tail 8: the QQ channel follows the house's *current* roster instead of binding a roommate once at start.

Fresh house (no residents) -> channel started -> owner creates the first resident in onboarding -> QQ must work
without a restart. Also: no crash/noise before onboarding, and a configured ONEBOT_ACTOR that vanished after a reset
is ignored with a clear reason. All QQ numbers and tokens are made up.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.builtin_plugins.onebot_ws import ChannelConfigError, OneBotChannel
from ai_house.daemon import HouseDaemon
from test_p1_onebot import OWNER, FakeOneBot, Model, private_msg, wait_until

WS_TOKEN = "tok-" + "x" * 8
OWNER_TOKEN = "own-" + "y" * 8
NO_KEYS = ("AI_HOUSE_API_KEY", "OPENAI_API_KEY", "WORLD_KERNEL_API_KEY", "AI_HOUSE_FALLBACK_API_KEY")


class Rig:
    """A brand-new data dir (onboarding allowed) with the OneBot channel pointed at a fake OneBot server."""

    def __init__(self, tc, *, actor=None):
        self.tc = tc
        self.logs = []
        self.fake = FakeOneBot(token=WS_TOKEN)
        tc.addCleanup(self.fake.close)
        tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(tmp.cleanup)
        env = {
            "ONEBOT_WS_URL": f"ws://127.0.0.1:{self.fake.port}",
            "ONEBOT_ACCESS_TOKEN": WS_TOKEN,
            "ONEBOT_OWNER_QQ": str(OWNER),
            "ONEBOT_MIN_INTERVAL_S": "0",
            "ONEBOT_RECONNECT_MIN_S": "0.05",
            "ONEBOT_RECONNECT_MAX_S": "0.2",
            "ONEBOT_SEND_TIMEOUT_S": "3",
            "AI_HOUSE_OWNER_TOKEN": OWNER_TOKEN,
        }
        for k in NO_KEYS:
            env[k] = ""
        if actor:
            env["ONEBOT_ACTOR"] = actor
        env_patch = patch.dict(os.environ, {k: "" for k in NO_KEYS})
        env_patch.start()
        tc.addCleanup(env_patch.stop)
        log_patch = patch.object(HouseDaemon, "log", lambda _self, msg: self.logs.append(str(msg)))
        log_patch.start()
        tc.addCleanup(log_patch.stop)
        self.model = Model()
        self.daemon = HouseDaemon(runtime_dir=Path(tmp.name) / "rt", virtual_clock=True, env=env, allow_onboarding=True)
        tc.addCleanup(self.daemon.close)
        self.ch = next(c for c in self.daemon.channels if isinstance(c, OneBotChannel))
        tc.assertTrue(wait_until(lambda: self.fake.conns and self.ch.stats["connects"] >= 1), "bot never connected")

    def onboard(self, resident):
        self.daemon.complete_setup({"mode": "first_bot", "resident": resident})
        self.daemon.chat_agent._llm_fn = self.model  # the chat agent is rebuilt by setup/reset; re-attach the fake model

    def owner_says(self, text):
        return self.ch.handle_event(private_msg(OWNER, text))

    def replies(self):
        return [a for a in self.fake.sent("send_private_msg") if a["params"]["user_id"] == OWNER]

    def lines(self, needle):
        return [ln for ln in self.logs if needle in ln]


class OneBotFollowsTheRoster(unittest.TestCase):
    def test_no_actor_configured_follows_first_resident_created_after_start(self):
        r = Rig(self)
        self.assertTrue(r.daemon.needs_onboarding)
        # the default pack's roster is visible on an un-onboarded house; it must not be taken as the roommate
        self.assertTrue(list(r.daemon.vocab.ROSTER))
        self.assertEqual(len(r.lines("roommate=nell")), 0, r.logs)

        # before onboarding: ignored with a distinct reason, no crash, one clear log line
        self.assertEqual(r.owner_says("你好"), "ignored:no_resident")
        self.assertEqual(r.owner_says("还在吗"), "ignored:no_resident")
        self.assertEqual(r.replies(), [])
        self.assertEqual(len(r.lines("还没有住客")), 1, r.logs)
        self.assertEqual(r.ch.stats["ignored"], 2)

        r.onboard({"name": "小一"})
        roster = list(r.daemon.vocab.ROSTER)
        self.assertEqual(len(roster), 1)
        self.assertNotEqual(roster[0], "nell")

        # the same running channel now answers the owner with the new resident
        r.fake.push(private_msg(OWNER, "你好呀"))
        self.assertTrue(wait_until(lambda: r.replies()), "no QQ reply after onboarding")
        self.assertEqual(r.ch.actor, roster[0])
        self.assertTrue(r.daemon.list_chat(f"private:{roster[0]}"), "turn was not stored in the new resident's private room")
        self.assertEqual(len(r.lines("event failed")), 0, r.logs)

        # a world reset empties the house again: quiet ignore, one new log line; then it follows the next resident
        r.daemon.reset_house()
        self.assertEqual(r.owner_says("重置后"), "ignored:no_resident")
        self.assertEqual(r.owner_says("重置后2"), "ignored:no_resident")
        self.assertEqual(len(r.lines("还没有住客")), 2, r.logs)
        r.onboard({"name": "小二"})
        n = len(r.replies())
        r.fake.push(private_msg(OWNER, "又回来了"))
        self.assertTrue(wait_until(lambda: len(r.replies()) > n), "no QQ reply after reset + onboarding")
        self.assertEqual(len(r.lines("event failed")), 0, r.logs)

    def test_logs_never_carry_message_text_or_qq_numbers(self):
        r = Rig(self)
        r.owner_says("绝密暗号-zebra")
        blob = "\n".join(r.logs)
        self.assertNotIn("zebra", blob)
        self.assertNotIn(str(OWNER), blob)
        self.assertNotIn(WS_TOKEN, blob)

    def test_explicit_actor_on_fresh_house_starts_and_works_once_that_resident_exists(self):
        r = Rig(self, actor="nell")  # no residents yet: must not refuse to start
        self.assertEqual(r.owner_says("先打个招呼"), "ignored:no_resident")
        r.onboard({"name": "奈尔", "actor": "nell"})
        r.fake.push(private_msg(OWNER, "你好呀"))
        self.assertTrue(wait_until(lambda: r.replies()), "no QQ reply for the configured roommate")
        self.assertTrue(r.daemon.list_chat("private:nell"))

    def test_explicit_actor_that_vanished_after_reset_is_ignored_with_a_clear_reason(self):
        r = Rig(self, actor="nell")
        r.onboard({"name": "奈尔", "actor": "nell"})
        r.fake.push(private_msg(OWNER, "你好呀"))
        self.assertTrue(wait_until(lambda: r.replies()))

        r.daemon.reset_house()
        r.onboard({"name": "别人"})  # a different resident (not nell) now lives in the house
        self.assertNotIn("nell", list(r.daemon.vocab.ROSTER))
        self.assertEqual(r.owner_says("还是我"), "ignored:actor_missing")
        self.assertEqual(r.owner_says("再来一次"), "ignored:actor_missing")
        gone = r.lines("ONEBOT_ACTOR=nell")
        self.assertEqual(len(gone), 1, r.logs)  # one line naming the configured roommate, not one per event
        self.assertEqual(len(r.replies()), 1)  # nobody else answered in nell's place
        self.assertEqual(len(r.lines("event failed")), 0, r.logs)

    def test_explicit_wrong_actor_on_a_house_with_residents_still_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ChannelConfigError):
                HouseDaemon(
                    runtime_dir=Path(tmp) / "rt", virtual_clock=True,
                    env={"ONEBOT_WS_URL": "ws://127.0.0.1:1", "ONEBOT_OWNER_QQ": "10001", "ONEBOT_ACTOR": "nobody"},
                )


if __name__ == "__main__":
    unittest.main()
