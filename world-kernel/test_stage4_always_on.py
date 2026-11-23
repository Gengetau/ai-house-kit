"""Stage 4.2: running in the background (systemd / compose). A stop signal must shut the house down cleanly."""
from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@unittest.skipIf(os.name == "nt", "POSIX signals")
class SigtermTests(unittest.TestCase):
    def _start(self, rt: Path, port: int) -> subprocess.Popen:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AI_HOUSE_", "ONEBOT_"))}
        env["PYTHONPATH"] = os.pathsep.join(str(ROOT / d) for d in ("src", "world-kernel", "world-catalog"))
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; from ai_house import cli; sys.exit(cli.main(sys.argv[1:]))",
             "up", "--port", str(port), "--data-dir", str(rt), "--env-file", str(rt / "none.env"),
             "--pack", "indie-studio-3"],
            cwd=str(rt), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait(), proc.stdout and proc.stdout.close()))
        end = time.monotonic() + 60
        while time.monotonic() < end:
            if proc.poll() is not None:
                self.fail(f"house exited early: {proc.stdout.read() if proc.stdout else ''}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
                    if r.status == 200:
                        return proc
            except OSError:
                time.sleep(0.1)
        self.fail("house did not come up")

    def test_sigterm_exits_cleanly_releases_the_lock_and_allows_a_restart(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name)
        port = _free_port()
        proc = self._start(rt, port)
        self.assertTrue((rt / ".daemon.lock").exists())
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("正在保存状态并退出", out)
        self.assertFalse((rt / ".daemon.lock").exists(), "the runtime lock must be released on a stop signal")
        # what systemd's Restart= / compose's restart: would do next
        again = self._start(rt, port)
        again.send_signal(signal.SIGTERM)
        out2, _ = again.communicate(timeout=30)
        self.assertEqual(again.returncode, 0, out2)


if __name__ == "__main__":
    unittest.main()
