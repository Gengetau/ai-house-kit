"""Three-state & privacy isolation regression tests for Stage 3.6: album_diary_art."""
from __future__ import annotations

import base64
import datetime as dt
import http.server
import json
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
from ai_house.plugin_api import ToolContext, invoke_tool_handler

JST = dt.timezone(dt.timedelta(hours=9))
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
TINY_B64 = base64.b64encode(TINY_PNG).decode("ascii")
TINY_DATA_URI = f"data:image/png;base64,{TINY_B64}"


def _http_req(url: str, *, token: str = "") -> tuple[int, bytes]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def _http_json(url: str, *, token: str = "") -> tuple[int, dict[str, Any]]:
    st, raw = _http_req(url, token=token)
    return st, json.loads(raw.decode("utf-8") or "{}")


class _MockImageServer:
    def __init__(self) -> None:
        self.fail_mode = True
        self.secret_key = "sk-alb-secret-8899"
        self.calls = 0
        parent = self

        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                _ = self.rfile.read(n)
                parent.calls += 1
                if parent.fail_mode:
                    body = json.dumps({"error": {"message": f"upstream down {parent.secret_key}"}}).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({"data": [{"b64_json": TINY_B64}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.base_url = f"http://127.0.0.1:{self._srv.server_port}"
        self._th = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._th.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._th.join(timeout=2.0)


def _advance_day_to_evening(d: HouseDaemon, day_date: dt.date) -> None:
    """Walk VirtualClock to 18:00 JST (generate daytime facts) then 22:32 JST (trigger due diary)."""
    t_afternoon = dt.datetime(day_date.year, day_date.month, day_date.day, 18, 0, tzinfo=JST).timestamp()
    if t_afternoon > d.clock.now():
        d.clock.advance(t_afternoon - d.clock.now())
    d.fast_forward(6)
    t_night = dt.datetime(day_date.year, day_date.month, day_date.day, 22, 27, tzinfo=JST).timestamp()
    if t_night > d.clock.now():
        d.clock.advance(t_night - d.clock.now())
    d.fast_forward(6)


class Stage3AlbumAndDiaryArtTests(unittest.TestCase):
    def test_three_states_diary_art_and_strict_scope_isolation(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        owner_tok = "st3-alb-owner-01"
        read_tok = "st3-alb-read-01"
        day1 = dt.date(2026, 11, 23)
        t0 = dt.datetime(2026, 11, 23, 17, 50, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={
                "AI_HOUSE_OWNER_TOKEN": owner_tok,
                "AI_HOUSE_READ_TOKEN": read_tok,
                "AI_HOUSE_QUIET_HOURS": "off",
            },
            llm_fn=lambda _s, _u: ("今天客厅茶香很暖，大家各自忙得开心。", 8),
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        mock_srv = _MockImageServer()
        self.addCleanup(mock_srv.close)

        srv = make_http_server(d, host="127.0.0.1", port=0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(th.join, 2.0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"

        # 0) Manifest & Default-Disabled state
        defn = d.registry.definitions["album_diary_art"]
        self.assertFalse(defn.manifest.default_enabled)
        self.assertLessEqual(len(defn.manifest.config_fields), 8)
        self.assertNotIn("save_to_album", d._active_tools)
        st0, alb0 = _http_json(f"{base}/api/album?scope=world", token=read_tok)
        self.assertEqual(st0, 200)
        self.assertFalse(alb0["enabled"])
        self.assertEqual(alb0["items"], [])
        st_r0, _ = _http_json(f"{base}/api/plugins/album_diary_art/items?scope=world", token=read_tok)
        self.assertEqual(st_r0, 404)

        # 1) State UNCONFIGURED: album_diary_art enabled, image_gen disabled, fallback SVG off -> plain-text diary works
        d.update_plugin_settings({
            "plugin_id": "album_diary_art",
            "enabled": True,
            "values": {
                "auto_illustrate_diary": True,
                "diary_art_fallback_svg": False,
                "save_chat_images": True,
            },
        })
        _advance_day_to_evening(d, day1)
        _, dia1 = _http_json(f"{base}/api/diaries?limit=30", token=read_tok)
        d1_rows = [row for row in dia1["diaries"] if row["day"] == day1.isoformat()]
        self.assertTrue(d1_rows, "Day 1 diary should be generated")
        for row in d1_rows:
            self.assertEqual(row.get("image_album_id", ""), "")
            self.assertFalse(row.get("image_data_uri"))
        _, alb1 = _http_json(f"{base}/api/album?scope=world", token=read_tok)
        self.assertTrue(alb1["enabled"])
        self.assertEqual(alb1["items"], [])
        self.assertEqual(d.send_chat("group", "晚上好呀")["replies"][0]["text"], "今天客厅茶香很暖，大家各自忙得开心。")

        # 2) State MISCONFIGURED / UPSTREAM 500: text diary saves cleanly without crash
        d.update_plugin_settings({
            "plugin_id": "image_gen",
            "enabled": True,
            "values": {
                "engine": "openai",
                "base_url": mock_srv.base_url,
                "api_key": mock_srv.secret_key,
            },
        })
        day2 = dt.date(2026, 11, 24)
        _advance_day_to_evening(d, day2)
        self.assertGreater(mock_srv.calls, 0)
        _, dia2 = _http_json(f"{base}/api/diaries?limit=30", token=read_tok)
        d2_rows = [row for row in dia2["diaries"] if row["day"] == day2.isoformat()]
        self.assertTrue(d2_rows, "Day 2 plain-text diary must survive HTTP 500 image_gen")
        for row in d2_rows:
            self.assertFalse(row.get("image_data_uri"))
        self.assertEqual(d.send_chat("group", "生图挂了也不影响聊天")["replies"][0]["text"], "今天客厅茶香很暖，大家各自忙得开心。")

        # 3a) State CONFIGURED: mock image_gen succeeds -> Day 3 diaries receive PNG world art
        mock_srv.fail_mode = False
        day3 = dt.date(2026, 11, 25)
        _advance_day_to_evening(d, day3)
        _, dia3_plain = _http_json(f"{base}/api/diaries?limit=30", token=read_tok)
        for row in (r for r in dia3_plain["diaries"] if r["day"] == day3.isoformat()):
            self.assertNotIn("image_data_uri", row, "the diary list does not embed pictures unless asked")
            self.assertTrue(str(row.get("image_media_url", "")).startswith("/api/album/media?path="))
        _, dia3 = _http_json(f"{base}/api/diaries?limit=30&inline=1", token=read_tok)
        d3_rows = [row for row in dia3["diaries"] if row["day"] == day3.isoformat()]
        self.assertTrue(d3_rows)
        self.assertTrue(all(row.get("image_album_id") for row in d3_rows))
        self.assertTrue(all(str(row.get("image_data_uri", "")).startswith("data:image/png;base64,") for row in d3_rows))

        _, alb3 = _http_json(f"{base}/api/album?scope=world&limit=24&inline=1", token=read_tok)
        self.assertEqual(len(alb3["items"]), len(d3_rows))
        self.assertTrue(all(it["kind"] == "diary_art" and it["scope"] == "world" for it in alb3["items"]))
        self.assertTrue(all(it["data_uri"].startswith("data:image/png;base64,") for it in alb3["items"]))

        # 3b) Fallback SVG diary illustration when image_gen is disabled & diary_art_fallback_svg=True
        d.update_plugin_settings({"plugin_id": "image_gen", "enabled": False, "values": {}})
        d.update_plugin_settings({
            "plugin_id": "album_diary_art",
            "enabled": True,
            "values": {"auto_illustrate_diary": True, "diary_art_fallback_svg": True, "save_chat_images": True},
        })
        day4 = dt.date(2026, 11, 26)
        _advance_day_to_evening(d, day4)
        _, dia4 = _http_json(f"{base}/api/diaries?limit=30&inline=1", token=read_tok)
        d4_rows = [row for row in dia4["diaries"] if row["day"] == day4.isoformat()]
        self.assertTrue(d4_rows)
        self.assertTrue(all(str(row.get("image_data_uri", "")).startswith("data:image/svg+xml;base64,") for row in d4_rows))

        # 4) Strict privacy scope isolation (`private` never leaks to `world`/`group` or read-tier)
        actor_a = d.vocab.ROSTER[0]
        d.send_chat(
            f"private:{actor_a}",
            "这是只给我们看的晚安拍立得",
            attachments={"images": [TINY_DATA_URI]},
        )
        self.assertTrue(d.drain_post_reply_jobs(5.0))  # chat photos are archived after the reply
        st_priv, alb_priv = _http_json(f"{base}/api/album?scope=private", token=owner_tok)
        self.assertEqual(st_priv, 200)
        self.assertEqual(len(alb_priv["items"]), 1)
        priv_id = alb_priv["items"][0]["album_id"]
        self.assertEqual(alb_priv["items"][0]["actor"], actor_a)
        self.assertEqual(alb_priv["items"][0]["kind"], "chat_image")

        # Never appears in world/group album, public plugin route, or public diaries
        # Never appears in world/group album, public plugin route, or public diaries
        for world_url in (
            f"{base}/api/album?scope=world&limit=50",
            f"{base}/api/plugins/album_diary_art/items?scope=world&limit=50",
        ):
            st_p, res_p = _http_json(world_url, token=read_tok)
            self.assertEqual(st_p, 200)
            self.assertNotIn(priv_id, {it["album_id"] for it in res_p["items"]})

        # `read` token must NOT read group album photos (`private` tier required)
        st_read_grp, _ = _http_json(f"{base}/api/album?scope=group&limit=50", token=read_tok)
        self.assertEqual(st_read_grp, 403)
        st_items_grp, _ = _http_json(f"{base}/api/plugins/album_diary_art/items?scope=group&limit=50", token=read_tok)
        self.assertEqual(st_items_grp, 400)
        for priv_scope_url in (
            f"{base}/api/album?scope=group&limit=50",
            f"{base}/api/plugins/album_diary_art/private?scope=group&limit=50",
        ):
            st_g2, res_g2 = _http_json(priv_scope_url, token=owner_tok)
            self.assertEqual(st_g2, 200)
            self.assertNotIn(priv_id, {it["album_id"] for it in res_g2["items"]})

        _, res_diaries_all = _http_json(f"{base}/api/diaries?limit=50", token=read_tok)
        self.assertNotIn(priv_id, {row.get("image_album_id") for row in res_diaries_all["diaries"]})

        # Token tier enforcement on HTTP routes
        st_read_priv, err_read_priv = _http_json(f"{base}/api/album?scope=private", token=read_tok)
        self.assertEqual(st_read_priv, 403)
        self.assertEqual(err_read_priv["error"], "forbidden_tier:private")

        st_plg_read_priv, _ = _http_json(f"{base}/api/plugins/album_diary_art/private", token=read_tok)
        self.assertEqual(st_plg_read_priv, 403)

        st_plg_bad_sc, _ = _http_json(f"{base}/api/plugins/album_diary_art/items?scope=private", token=read_tok)
        self.assertEqual(st_plg_bad_sc, 400)

        st_plg_own_priv, plg_own_priv = _http_json(f"{base}/api/plugins/album_diary_art/private", token=owner_tok)
        self.assertEqual(st_plg_own_priv, 200)
        self.assertIn(priv_id, {it["album_id"] for it in plg_own_priv["items"]})

        # Tool context boundary + private-to-public leak block
        browse_reg = d._active_tools["browse_album"]["registration"]
        save_reg = d._active_tools["save_to_album"]["registration"]
        res_grp_browse = invoke_tool_handler(
            browse_reg,
            {"scope": "private"},
            ToolContext(
                actor=actor_a,
                scope="group",
                channel="group",
                speaker=member_speaker("7001", "访客"),
                now=d.clock.now(),
                daemon=d,
            ),
        )
        self.assertEqual(res_grp_browse["status"], "forbidden")
        self.assertEqual(res_grp_browse["items"], [])

        res_priv_browse = invoke_tool_handler(
            browse_reg,
            {"scope": "private"},
            ToolContext(
                actor=actor_a,
                scope="private",
                channel=f"private:{actor_a}",
                speaker=owner_speaker("房主"),
                now=d.clock.now(),
                daemon=d,
            ),
        )
        self.assertTrue(res_priv_browse["ok"])
        self.assertIn(priv_id, {it["album_id"] for it in res_priv_browse["items"]})

        d.memory.add(
            actor_a,
            "private",
            "房主的私密备忘：「阁楼铁盒暗码」放在书桌夹层",
            kind="fact",
            source="chat",
            now=d.clock.now(),
        )
        res_leak_save = invoke_tool_handler(
            save_reg,
            {
                "source": TINY_DATA_URI,
                "title": "桌面笔记",
                "caption": "房主的私密备忘：「阁楼铁盒暗码」",
            },
            ToolContext(
                actor=actor_a,
                scope="group",
                channel="group",
                speaker=owner_speaker("房主"),
                now=d.clock.now(),
                daemon=d,
            ),
        )
        self.assertEqual(res_leak_save["status"], "blocked")

        # 5) Web Studio UI assets verify modern album gallery + CSS grid rules
        st_js, js_bytes = _http_req(f"{base}/web/js/pages/keep.js")
        self.assertEqual(st_js, 200)
        js_txt = js_bytes.decode("utf-8")
        self.assertIn("renderAlbum", js_txt)
        self.assertIn("小屋相册", js_txt)
        self.assertIn("fetchpriority", js_txt)

        st_css, css_bytes = _http_req(f"{base}/web/css/app.css")
        self.assertEqual(st_css, 200)
        css_txt = css_bytes.decode("utf-8")
        self.assertIn("subgrid", css_txt)
        self.assertIn("overflow:clip", css_txt)


if __name__ == "__main__":
    unittest.main()
