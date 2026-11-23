"""Every script the workbench serves must at least parse.

A stray bracket in one page module makes the browser reject the whole module, and the page shows nothing but
"读取失败". No Python test loads these files, so this asks Node to parse each one. Skipped where Node is absent.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

WEB_JS = Path(__file__).resolve().parents[1] / "src" / "ai_house" / "web" / "js"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class WebScriptsParse(unittest.TestCase):
    def test_every_served_script_parses_as_a_module(self) -> None:
        files = sorted(WEB_JS.rglob("*.js"))
        self.assertGreater(len(files), 5, "web scripts not found; has the directory moved?")
        failures = []
        with tempfile.TemporaryDirectory() as tmp:
            for src in files:
                # The pages are ES modules served as .js; a .mjs copy makes `node --check` parse them as modules.
                copy = Path(tmp) / (src.stem + ".mjs")
                copy.write_bytes(src.read_bytes())
                res = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True, timeout=60)
                if res.returncode != 0:
                    detail = next((ln for ln in res.stderr.splitlines() if "Error" in ln), res.stderr.strip()[:200])
                    failures.append(f"{src.relative_to(WEB_JS)}: {detail}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
