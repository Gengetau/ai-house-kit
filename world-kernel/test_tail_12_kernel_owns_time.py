"""House time belongs to the kernel: on the real clock nothing outside it may step or fast-forward.

`POST /api/step` used to be open to the write tier on every house. It is now owner-only and only works on a
house started as a `--virtual-clock` sandbox, where time has no other source.
"""
import http.client
import json
import subprocess
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.daemon import HouseDaemon, make_http_server

OWNER = "o" * 24
WRITER = "w" * 24
ENV = {"AI_HOUSE_OWNER_TOKEN": OWNER, "AI_HOUSE_WRITE_TOKEN": WRITER}


class _Served:
    def __init__(self, tc: unittest.TestCase, *, virtual_clock: bool):
        tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(tmp.cleanup)
        self.daemon = HouseDaemon(runtime_dir=Path(tmp.name) / "rt", virtual_clock=virtual_clock, env=dict(ENV))
        tc.addCleanup(self.daemon.close)
        self.srv = make_http_server(self.daemon, host="127.0.0.1", port=0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        tc.addCleanup(self.srv.server_close)
        tc.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None, token=None):
        h = {}
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw.startswith("{") else raw)
        finally:
            c.close()


class RealClockTests(unittest.TestCase):
    def setUp(self):
        self.s = _Served(self, virtual_clock=False)

    def test_owner_cannot_fast_forward_or_step(self):
        offset_before = self.s.daemon.clock.offset_s
        for body in ({"minutes": 15}, {}):
            status, doc = self.s.req("POST", "/api/step", body, token=OWNER)
            self.assertEqual(status, 409, doc)
            self.assertEqual(doc.get("code"), "kernel_owns_time")
            self.assertIn("--virtual-clock", doc.get("error", ""))
        self.assertEqual(self.s.daemon.clock.offset_s, offset_before)
        self.assertFalse(hasattr(self.s.daemon.clock, "t"), "the wall clock must not have been swapped for a virtual one")
        self.assertFalse((self.s.daemon.runtime_dir / "clock_offset.json").exists())

    def test_write_token_is_refused_before_anything_else(self):
        status, doc = self.s.req("POST", "/api/step", {"minutes": 15}, token=WRITER)
        self.assertEqual(status, 403, doc)

    def test_state_says_time_is_not_manual(self):
        status, doc = self.s.req("GET", "/api/state", token=OWNER)
        self.assertEqual(status, 200)
        self.assertIs(doc.get("manual_time_control"), False)

    def test_owner_actions_still_reach_the_kernel(self):
        # Restocking is an owner action the kernel digests on its own step; it must not need /api/step.
        status, doc = self.s.req(
            "POST", "/api/stock", {"op": "add", "args": {"name": "牛奶", "qty": 1, "class": "drink"}}, token=WRITER
        )
        self.assertEqual(status, 200, doc)
        self.assertEqual(doc.get("status"), "succeeded", doc)


class VirtualClockTests(unittest.TestCase):
    def setUp(self):
        self.s = _Served(self, virtual_clock=True)

    def test_owner_can_fast_forward_the_sandbox(self):
        before = self.s.daemon.clock.now()
        status, doc = self.s.req("POST", "/api/step", {"minutes": 15}, token=OWNER)
        self.assertEqual(status, 200, doc)
        self.assertAlmostEqual(self.s.daemon.clock.now() - before, 15 * 60, delta=1)
        status, doc = self.s.req("POST", "/api/step", {}, token=OWNER)
        self.assertEqual(status, 200, doc)

    def test_write_token_cannot_move_sandbox_time_either(self):
        before = self.s.daemon.clock.now()
        status, doc = self.s.req("POST", "/api/step", {"minutes": 15}, token=WRITER)
        self.assertEqual(status, 403, doc)
        self.assertEqual(self.s.daemon.clock.now(), before)

    def test_state_says_time_is_manual(self):
        status, doc = self.s.req("GET", "/api/state", token=OWNER)
        self.assertEqual(status, 200)
        self.assertIs(doc.get("manual_time_control"), True)


class OnboardingStateTests(unittest.TestCase):
    def test_flag_is_present_before_the_first_resident(self):
        s = _Served(self, virtual_clock=False)
        status, doc = s.req("POST", "/api/setup/reset", {"confirm": True}, token=OWNER)
        self.assertEqual(status, 200, doc)
        status, doc = s.req("GET", "/api/state", token=OWNER)
        self.assertEqual(status, 200)
        self.assertTrue(doc.get("needs_onboarding"))
        self.assertIs(doc.get("manual_time_control"), False)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ConsoleButtonsTests(unittest.TestCase):
    """The console only offers the step buttons when the house says time is manual, and only to the owner."""

    def test_pages_gate_the_buttons_on_the_state_flag(self):
        pages = ROOT / "src" / "ai_house" / "web" / "js" / "pages"
        for name in ("home.js", "ops.js"):
            src = (pages / name).read_text(encoding="utf-8")
            self.assertIn("/api/step", src)
            self.assertIn("state.manual_time_control", src, name)
            self.assertNotIn("hasTier('write'), onclick: () => step(", src, name)
            self.assertNotIn("hasTier('write') ? el('button', {type: 'button', class: 'btn sm primary', onclick: () => stepWorld", src, name)
            with tempfile.TemporaryDirectory() as tmp:
                mjs = Path(tmp) / (name[:-3] + ".mjs")
                mjs.write_text(src, encoding="utf-8")
                r = subprocess.run(["node", "--check", str(mjs)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
