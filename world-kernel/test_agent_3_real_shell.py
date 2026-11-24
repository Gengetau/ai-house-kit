"""The real Claude Code shell, end to end, with only the model replaced by a local stand-in.

Skipped when the optional `claude-agent-sdk` is not installed. The probe runs in a child process whose environment
is scrubbed, so it cannot see (or spend) any real credential, and it has no route to anything but 127.0.0.1.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HAS_SDK = importlib.util.find_spec("claude_agent_sdk") is not None
REQUIRED = os.environ.get("AI_HOUSE_REQUIRE_REAL_SHELL") == "1"


@unittest.skipUnless(HAS_SDK or REQUIRED, "claude-agent-sdk is not installed (pip install -e '.[agent]')")
class RealShellTests(unittest.TestCase):
    doc: dict

    @classmethod
    def setUpClass(cls):
        if not HAS_SDK:
            raise AssertionError("AI_HOUSE_REQUIRE_REAL_SHELL=1 but claude-agent-sdk is not installed")
        cls._home = tempfile.TemporaryDirectory()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": cls._home.name,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        proc = subprocess.run(
            [sys.executable, str(HERE / "agent_real_shell_probe.py")],
            env=env, capture_output=True, text=True, timeout=300, cwd=cls._home.name,
        )
        if proc.returncode != 0:
            raise AssertionError(f"probe failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}")
        cls.doc = json.loads(proc.stdout.strip().splitlines()[-1])
        cls.turns = {t["said"]: t for t in cls.doc["turns"]}

    @classmethod
    def tearDownClass(cls):
        home = getattr(cls, "_home", None)
        if home is not None:
            home.cleanup()

    def test_every_turn_completed(self):
        self.assertEqual(self.doc["unavailable_reason"], "")
        errors = {t["said"]: t.get("error") for t in self.doc["turns"] if t.get("error")}
        self.assertEqual(list(errors), ["这次等很久"], self.doc["turns"])
        self.assertEqual(len(self.doc["turns"]), 9)

    def test_she_speaks_through_the_tool_and_the_turn_ends_there(self):
        t = self.turns["暗号是青柠"]
        self.assertEqual(t["messages"], ["收到：房主刚刚说：暗号是青柠"])
        self.assertTrue(t["fresh"])
        self.assertEqual(t["model_calls"], 1, "no further model call is made once she has sent her reply")
        self.assertGreater(t["prompt_tokens"], 0)
        self.assertGreater(t["cost_usd"], 0)

    def test_house_tool_runs_on_the_houses_thread_and_its_result_reaches_the_model(self):
        t = self.turns["看看家里有什么"]
        self.assertEqual(t["tool_calls"], 1)
        self.assertEqual(self.doc["tool_threads"], [True])
        self.assertIn("晴", t["messages"][0])
        self.assertFalse(t["fresh"], "the second turn continues the same session")

    def test_two_messages_in_one_turn(self):
        self.assertEqual(self.turns["发两条给我"]["messages"], ["第一条", "第二条"])

    def test_forgotten_send_is_recovered_by_the_reminder(self):
        t = self.turns["这次直接说"]
        self.assertTrue(t["nudged"])
        self.assertFalse(t["autosent"])
        self.assertEqual(t["messages"], ["补发：刚才那句"])

    def test_a_built_in_tool_is_not_available_to_her(self):
        self.assertEqual(self.turns["用别的工具试试"]["messages"], ["那个工具用不了"])
        self.assertNotIn("Bash", self.doc["tools_offered"])
        self.assertEqual(
            [n for n in self.doc["tools_offered"] if n.startswith("mcp__")],
            ["mcp__house__inspect_house", "mcp__house__send_message"],
        )
        for name in ("Read", "Write", "Edit", "WebFetch", "WebSearch", "Task", "Agent"):
            self.assertNotIn(name, self.doc["tools_offered"])

    def test_group_resident_can_stay_quiet(self):
        t = self.turns["这句不接话"]
        self.assertTrue(t["quiet"])
        self.assertEqual(t["messages"], [])

    def test_session_is_resumed_after_a_restart(self):
        t = self.turns["还记得暗号吗"]
        self.assertFalse(t["fresh"])
        self.assertEqual(t["messages"], ["记得，是青柠"])

    def test_a_turn_given_up_on_drops_the_shell_and_the_conversation_survives(self):
        gone = self.turns["这次等很久"]
        self.assertIn("LLMDeadlineError", gone["error"])
        self.assertLess(gone["seconds"], 9.0, "the house stops waiting at its own limit, not the shell's")
        after = self.turns["超时之后还记得暗号吗"]
        self.assertFalse(after["fresh"], "the same session is picked up again")
        self.assertEqual(after["messages"], ["记得，是青柠"])
        self.assertLess(after["seconds"], 3.0, "closing the stuck shell does not hold the next turn up")

    def test_every_tool_call_passed_the_policy_callback(self):
        names = [p[0] for p in self.doc["permits"]]
        self.assertEqual(names.count("mcp__house__inspect_house"), 1)
        self.assertEqual(names.count("mcp__house__stay_quiet"), 1)
        self.assertGreaterEqual(names.count("mcp__house__send_message"), 8)
        self.assertTrue(all(p[1] for p in self.doc["permits"]))

    def test_isolation_and_settings(self):
        self.assertTrue(self.doc["system_has_persona"])
        self.assertTrue(self.doc["config_dir_used"], "the shell keeps its files in the house's own directory")
        self.assertFalse(self.doc["home_claude_touched"], "nothing is read from or written to ~/.claude")
        self.assertEqual(self.doc["key_sent"], ["sk-ant-dummy-n"])
        self.assertEqual(len(self.doc["models_requested"]), 1)
        self.assertIn("sonnet", self.doc["models_requested"][0])


if __name__ == "__main__":
    unittest.main()
