"""Stage 2.7 tests: multi-period /api/usage ledger & responsive-table Web assets for #/usage."""
from __future__ import annotations

import datetime as dt
import json
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

from ai_house.daemon import HouseDaemon, JST, make_http_server  # noqa: E402
from ai_house.llm import ModelEndpoint  # noqa: E402


def _http_get(url: str, *, token: str = "") -> Tuple[int, str, str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Content-Type", ""), err.read().decode("utf-8", errors="replace")


class Stage2UsagePageTests(unittest.TestCase):
    def test_usage_api_periods_aggregates_and_accessible_table_assets(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        read_tok = "read-usage-tok-01"
        t0 = dt.datetime(2026, 11, 22, 15, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_READ_TOKEN": read_tok},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        ep_main = ModelEndpoint(base_url="https://api.example.com", model="gpt-main", api_key="k")
        ep_alt = ModelEndpoint(base_url="https://alt.example.org", model="gpt-alt", api_key="k2")

        # Seed usage rows across different days/purposes/actors/models
        d.model_router._record_usage(
            purpose="chat", actor="sora", endpoint=ep_main,
            prompt_tokens=120, completion_tokens=45, total_tokens=165,
            latency_ms=310, ok=True, fallback_used=False,
        )
        d.clock.t = t0 - 2 * 86400  # 2 days ago (in 7d, NOT in today)
        d.model_router._record_usage(
            purpose="diary", actor="rin", endpoint=ep_alt,
            prompt_tokens=200, completion_tokens=90, total_tokens=290,
            latency_ms=420, ok=False, fallback_used=True, error_code="http_503",
        )
        d.clock.t = t0 - 22 * 86400  # 22 days ago (in 30d & all, NOT in 7d)
        d.model_router._record_usage(
            purpose="life", actor="kuro", endpoint=ep_main,
            prompt_tokens=80, completion_tokens=20, total_tokens=100,
            latency_ms=190, ok=True, fallback_used=False,
        )
        d.clock.t = t0  # back to t0

        srv = make_http_server(d, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(th.join, 2.0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{port}"

        # Read-tier access verified for today / 7d / 30d / all
        st_t, _, raw_today = _http_get(f"{base}/api/usage?period=today&recent_limit=10", token=read_tok)
        self.assertEqual(st_t, 200)
        doc_today = json.loads(raw_today)
        self.assertEqual(doc_today["totals"]["calls"], 1)
        self.assertEqual(doc_today["totals"]["total_tokens"], 165)

        st_7, _, raw_7d = _http_get(f"{base}/api/usage?period=7d&recent_limit=10", token=read_tok)
        self.assertEqual(st_7, 200)
        doc_7d = json.loads(raw_7d)
        self.assertEqual(doc_7d["totals"]["calls"], 2)
        self.assertEqual(doc_7d["totals"]["failed_calls"], 1)
        self.assertEqual(doc_7d["totals"]["fallback_calls"], 1)
        self.assertEqual(len(doc_7d["recent"]), 2)
        self.assertEqual({x["purpose"] for x in doc_7d["by_purpose"]}, {"chat", "diary"})
        self.assertEqual({x["actor"] for x in doc_7d["by_actor"]}, {"sora", "rin"})
        self.assertTrue(any(m.get("base_url_host") == "alt.example.org" for m in doc_7d["by_model"]))

        st_30, _, raw_30d = _http_get(f"{base}/api/usage?period=30d&recent_limit=10", token=read_tok)
        self.assertEqual(st_30, 200)
        doc_30d = json.loads(raw_30d)
        self.assertEqual(doc_30d["totals"]["calls"], 3)
        self.assertEqual(doc_30d["totals"]["total_tokens"], 555)

        # Static web assets for #/usage & responsive-table compliance
        st_js, ct_js, usage_js = _http_get(f"{base}/web/js/pages/usage.js")
        self.assertEqual(st_js, 200)
        self.assertIn("javascript", ct_js)
        self.assertIn("role: 'region'", usage_js)
        self.assertIn("tabindex: '0'", usage_js)
        self.assertIn("aria-labelledby", usage_js)
        self.assertIn("--label-", usage_js)

        st_css, _, app_css = _http_get(f"{base}/web/css/app.css")
        self.assertEqual(st_css, 200)
        self.assertIn("container-type:inline-size", app_css)
        self.assertIn("@container (width < 600px)", app_css)
        self.assertIn("inset-block-start:0", app_css)

        st_main, _, main_js = _http_get(f"{base}/web/js/main.js")
        self.assertEqual(st_main, 200)
        self.assertIn("usage: 'usage'", main_js)
        self.assertIn("#/usage", main_js)


if __name__ == "__main__":
    unittest.main()
