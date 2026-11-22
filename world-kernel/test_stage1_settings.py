"""Regression tests for Stage 1.2: declarative SettingsStore, /api/settings/plugins, ReplyGuard & hot reload."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.daemon import HouseDaemon, make_http_server


def _http_json(url: str, *, token: str = "", body: dict | None = None) -> tuple[int, dict]:
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


class Stage1SettingsTests(unittest.TestCase):
    def test_settings_plugins_auth_tiers_and_static_page(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        owner_tok = "tok-owner-settings-123456"
        read_tok = "tok-read-settings-123456"

        d = HouseDaemon(
            Path(tmp.name) / "rt",
            virtual_clock=True,
            env={
                "AI_HOUSE_OWNER_TOKEN": owner_tok,
                "AI_HOUSE_READ_TOKEN": read_tok,
            },
        )
        self.addCleanup(d.close)
        srv = make_http_server(d, host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"

        code_unauth, _ = _http_json(f"{base}/api/settings/plugins")
        self.assertEqual(code_unauth, 401)
        code_read, _ = _http_json(f"{base}/api/settings/plugins", token=read_tok)
        self.assertEqual(code_read, 403)
        code_post_read, _ = _http_json(
            f"{base}/api/settings/plugins",
            token=read_tok,
            body={"plugin_id": "dice_tabletop", "enabled": False},
        )
        self.assertEqual(code_post_read, 403)

        code_ok, doc = _http_json(f"{base}/api/settings/plugins", token=owner_tok)
        self.assertEqual(code_ok, 200)
        self.assertTrue(doc["ok"])
        pids = {p["plugin_id"] for p in doc["plugins"]}
        self.assertIn("onebot_v11", pids)
        self.assertIn("dice_tabletop", pids)

        with urllib.request.urlopen(f"{base}/web/js/pages/settings.js", timeout=5) as r:
            self.assertEqual(r.status, 200)
            js_txt = r.read().decode("utf-8")
        self.assertIn("/api/settings/plugins", js_txt)
        self.assertIn("wireFormValidation", js_txt)

    def test_secret_masking_preserve_clear_and_reply_guard_protection(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        owner_tok = "tok-owner-secrets-123456"
        rt = Path(tmp.name) / "rt"
        secret_val = "pluginSecretToken987654"

        d = HouseDaemon(rt, virtual_clock=True, env={"AI_HOUSE_OWNER_TOKEN": owner_tok})
        self.addCleanup(d.close)
        srv = make_http_server(d, host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"

        code, res = _http_json(
            f"{base}/api/settings/plugins",
            token=owner_tok,
            body={
                "plugin_id": "city_mcp_outing",
                "enabled": False,
                "values": {"house_tag": "风车小屋", "shared_token": secret_val},
            },
        )
        self.assertEqual(code, 200, res)
        raw_res = json.dumps(res, ensure_ascii=False)
        self.assertNotIn(secret_val, raw_res)

        _, got = _http_json(f"{base}/api/settings/plugins", token=owner_tok)
        self.assertNotIn(secret_val, json.dumps(got, ensure_ascii=False))
        city = next(p for p in got["plugins"] if p["plugin_id"] == "city_mcp_outing")
        sec_fld = next(f for f in city["fields"] if f["key"] == "shared_token")
        self.assertEqual(sec_fld["value"], "")
        self.assertTrue(sec_fld["secret_set"])
        self.assertIn("…", sec_fld["secret_masked"])
        self.assertNotIn("SecretToken", sec_fld["secret_masked"])

        self.assertEqual(d.guard.check("group", f"秘钥是 {secret_val}"), "secret")

        # Empty secret string preserves existing secret; setting unrelated field stays intact
        code2, _ = _http_json(
            f"{base}/api/settings/plugins",
            token=owner_tok,
            body={
                "plugin_id": "city_mcp_outing",
                "values": {"house_tag": "新风车小屋", "shared_token": ""},
            },
        )
        self.assertEqual(code2, 200)
        self.assertEqual(d.guard.check("group", secret_val), "secret")

        # Explicit clear_secrets removes stored secret and stops protecting old cleared token
        code3, _ = _http_json(
            f"{base}/api/settings/plugins",
            token=owner_tok,
            body={
                "plugin_id": "city_mcp_outing",
                "clear_secrets": ["shared_token"],
            },
        )
        self.assertEqual(code3, 200)
        _, got3 = _http_json(f"{base}/api/settings/plugins", token=owner_tok)
        city3 = next(p for p in got3["plugins"] if p["plugin_id"] == "city_mcp_outing")
        sec_fld3 = next(f for f in city3["fields"] if f["key"] == "shared_token")
        self.assertFalse(sec_fld3["secret_set"])
        self.assertIsNone(d.guard.check("group", secret_val))

    def test_hot_reload_persistence_and_atomic_validation_rollback(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        owner_tok = "tok-owner-hotreload-1234"
        rt = Path(tmp.name) / "rt"
        env_file = Path(tmp.name) / ".env"

        d = HouseDaemon(
            rt,
            virtual_clock=True,
            env_file=env_file,
            env={"AI_HOUSE_OWNER_TOKEN": owner_tok},
        )
        self.addCleanup(d.close)
        self.assertIn("dice_tabletop", d.get_state()["tools"])

        # Disable dice_tabletop online -> tools update immediately
        res_off = d.update_plugin_settings({"plugin_id": "dice_tabletop", "enabled": False})
        self.assertFalse(res_off["plugin"]["enabled"])
        self.assertFalse(res_off["plugin"]["active"])
        self.assertNotIn("dice_tabletop", res_off["state"]["tools"])

        # Attempting to enable onebot_v11 without required ws_url / owner_qq rejects & rolls back
        with self.assertRaises(ValueError):
            d.update_plugin_settings({"plugin_id": "onebot_v11", "enabled": True})
        self.assertNotIn("onebot_v11", d.settings_store.get_plugins_map())

        # Invalid int field fails cleanly and keeps dice_tabletop disabled
        with self.assertRaises(ValueError):
            d.update_plugin_settings({"plugin_id": "dice_tabletop", "values": {"default_sides": 999}})
        self.assertNotIn("dice_tabletop", d.get_state()["tools"])

        # Re-enable with valid config -> persists across daemon restart
        res_on = d.update_plugin_settings({
            "plugin_id": "dice_tabletop",
            "enabled": True,
            "values": {"default_sides": 20},
        })
        self.assertTrue(res_on["plugin"]["active"])
        self.assertIn("dice_tabletop", res_on["state"]["tools"])
        self.assertIn("AI_HOUSE_DICE_SIDES=20", env_file.read_text(encoding="utf-8"))
        d.close()

        d2 = HouseDaemon(rt, virtual_clock=True, env={"AI_HOUSE_OWNER_TOKEN": owner_tok})
        self.addCleanup(d2.close)
        got = d2.get_plugins_settings_view()
        dice = next(p for p in got["plugins"] if p["plugin_id"] == "dice_tabletop")
        sides_fld = next(f for f in dice["fields"] if f["key"] == "default_sides")
        self.assertEqual(sides_fld["value"], 20)
        self.assertTrue(dice["active"])


if __name__ == "__main__":
    unittest.main()
