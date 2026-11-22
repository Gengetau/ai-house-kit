"""Stage 2.3 tests: event-driven proactive talk, cursor anchoring, active windows & pre-LLM caps."""
from __future__ import annotations

import contextlib
import datetime as dt
import http.server
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "world-kernel"))

from ai_house.daemon import HouseDaemon, JST, make_http_server  # noqa: E402
from ai_house.proactive import in_active_window  # noqa: E402


class _MockOpenAIServer:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.proactive_calls: List[Dict[str, Any]] = []
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                ln = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(ln).decode("utf-8")
                body = json.loads(raw or "{}")
                parent.calls.append(body)
                if "刚刚发生的生活事件" in raw:
                    parent.proactive_calls.append(body)
                resp = {
                    "id": "mock-pro-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "我刚刚烤好了黄油司康，大家待会儿要不要一起来客厅喝茶呀～",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32},
                }
                data = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt: str, *args: Any) -> None:
                return

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._srv.server_address[1])
        self._th = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._th.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._th.join(timeout=2)


def _http_json(
    url: str,
    *,
    token: str = "",
    body: Any = None,
) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers: Dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


class Stage2ProactiveTests(unittest.TestCase):
    def _insert_script(self, world_db: Path, actors: List[str], text: str, ts: float) -> None:
        knowers = json.dumps(actors, ensure_ascii=False)
        with contextlib.closing(sqlite3.connect(world_db)) as c:
            with c:
                c.execute(
                    "INSERT INTO script(ts, actors, scene, text, source, created, knowers) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (ts, json.dumps(actors, ensure_ascii=False), None, text, "kernel", ts, knowers),
                )

    def _wake_actor(self, d: HouseDaemon, actor: str) -> None:
        with contextlib.closing(sqlite3.connect(d.life_db)) as lc:
            with lc:
                lc.execute("UPDATE actors SET availability='free' WHERE actor_id=?", (actor,))
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute("UPDATE reality SET availability='free', doing='看书' WHERE actor=?", (actor,))

    def _sleep_actor(self, d: HouseDaemon, actor: str) -> None:
        with contextlib.closing(sqlite3.connect(d.life_db)) as lc:
            with lc:
                lc.execute("UPDATE actors SET availability='sleeping' WHERE actor_id=?", (actor,))
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute("UPDATE reality SET availability='sleeping', doing='睡觉' WHERE actor=?", (actor,))

    def test_window_helper_daytime_and_overnight(self) -> None:
        t_open = dt.datetime(2026, 11, 22, 10, 0, tzinfo=JST).timestamp()
        t_late = dt.datetime(2026, 11, 22, 23, 0, tzinfo=JST).timestamp()
        self.assertTrue(in_active_window(t_open, "09:00-22:30"))
        self.assertFalse(in_active_window(t_late, "09:00-22:30"))
        self.assertTrue(in_active_window(t_late, "21:00-01:30"))

    def test_default_off_no_history_replay_no_idle_chatter_and_pre_llm_gates(self) -> None:
        mock_llm = _MockOpenAIServer()
        self.addCleanup(mock_llm.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        owner_tok = "proactive-owner-token-01"
        t0 = dt.datetime(2026, 11, 22, 14, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={
                "AI_HOUSE_OWNER_TOKEN": owner_tok,
                "AI_HOUSE_API_KEY": "mock-key-for-test",
                "AI_HOUSE_BASE_URL": mock_llm.base_url,
                "AI_HOUSE_MODEL": "mock-proactive-v1",
            },
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)
        self._wake_actor(d, "sora")
        self._wake_actor(d, "rin")
        self._wake_actor(d, "kuro")

        # 1) Default disabled: proactive_talk is inactive and does not trigger even with script events
        self._insert_script(d.world_db, ["sora"], "苍空把老剧本留在厨房桌面", d.clock.now())
        d.step_once()
        self.assertEqual(len(mock_llm.proactive_calls), 0)
        self.assertEqual(d.list_chat("group"), [])

        # Enable proactive_talk via settings API with small limits for test
        res_en = d.update_plugin_settings(
            {
                "plugin_id": "proactive_talk",
                "enabled": True,
                "values": {
                    "window": "09:00-22:30",
                    "actor_daily_cap": 1,
                    "house_daily_cap": 2,
                    "min_interval_min": 30,
                    "target_channel": "webchat",
                    "target_scope": "group",
                },
            }
        )
        self.assertTrue(res_en["ok"])

        # 2) Anchoring prevents replay of the older script event inserted before enabling
        d.clock.advance(65)
        d.step_once()
        self.assertEqual(len(mock_llm.proactive_calls), 0)
        self.assertEqual(d.list_chat("group"), [])

        # 3) Idle advance without any new script/evidence event also triggers 0 calls
        d.clock.advance(120)
        d.step_once()
        self.assertEqual(len(mock_llm.proactive_calls), 0)

        # 4) New life event while sora is awake inside window triggers 1 LLM call & delivers via Outbox
        self._insert_script(d.world_db, ["sora"], "苍空在厨房烤好了一盘黄油司康", d.clock.now())
        scan1 = d.proactive.scan(
            d,
            cfg=d.registry.resolve_config("proactive_talk", env=d.env, stored_plugins=d.settings_store.get_plugins_map()),
            now=d.clock.now(),
        )
        self.assertTrue(scan1["triggered"])
        self.assertEqual(scan1["actor"], "sora")
        self.assertEqual(len(mock_llm.proactive_calls), 1)
        group_msgs = d.list_chat("group")
        self.assertEqual(len(group_msgs), 1)
        self.assertIn("黄油司康", group_msgs[0]["text"])

        # Inspect outbox delivery & plugin status route over HTTP
        srv = make_http_server(d, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(th.join, 2.0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{port}"
        st_p, pro_view = _http_json(f"{base}/api/plugins/proactive_talk/status", token=owner_tok)
        self.assertEqual(st_p, 200)
        self.assertEqual(pro_view["proactive"]["house_count"], 1)
        self.assertEqual(pro_view["proactive"]["actor_counts"].get("sora"), 1)

        cfg = d.registry.resolve_config("proactive_talk", env=d.env, stored_plugins=d.settings_store.get_plugins_map())

        # 5) min_interval_min guard blocks BEFORE LLM
        d.clock.advance(600)  # only 10 min < 30 min
        self._insert_script(d.world_db, ["rin"], "凛音刚冲好手冲咖啡", d.clock.now())
        scan_iv = d.proactive.scan(d, cfg=cfg, now=d.clock.now())
        self.assertFalse(scan_iv["triggered"])
        self.assertEqual(scan_iv["reason"], "min_interval")
        self.assertEqual(len(mock_llm.proactive_calls), 1)

        # 6) actor_daily_cap (=1) blocks sora BEFORE LLM even after min_interval
        d.clock.advance(2000)  # > 30 min
        self._insert_script(d.world_db, ["sora"], "苍空又打开了书房窗口", d.clock.now())
        scan_ac = d.proactive.scan(d, cfg=cfg, now=d.clock.now())
        self.assertFalse(scan_ac["triggered"])
        self.assertEqual(scan_ac["reason"], "actor_daily_cap")
        self.assertEqual(len(mock_llm.proactive_calls), 1)

        # 7) Sleeping guard blocks sleeping resident BEFORE LLM
        self._sleep_actor(d, "rin")
        d.clock.advance(2000)
        self._insert_script(d.world_db, ["rin"], "凛音在床上翻了个身", d.clock.now())
        scan_slp = d.proactive.scan(d, cfg=cfg, now=d.clock.now())
        self.assertFalse(scan_slp["triggered"])
        self.assertEqual(scan_slp["reason"], "all_sleeping")
        self.assertEqual(len(mock_llm.proactive_calls), 1)

        # 8) Another awake resident reaches house_daily_cap (=2)
        d.clock.advance(2000)
        self._insert_script(d.world_db, ["kuro"], "黑羽清理好引擎日志缓存", d.clock.now())
        scan2 = d.proactive.scan(d, cfg=cfg, now=d.clock.now())
        self.assertTrue(scan2["triggered"])
        self.assertEqual(len(mock_llm.proactive_calls), 2)

        # house_daily_cap blocks any further event before LLM
        self._wake_actor(d, "rin")
        d.clock.advance(2000)
        self._insert_script(d.world_db, ["rin"], "凛音找出一盘磁带", d.clock.now())
        scan_hc = d.proactive.scan(d, cfg=cfg, now=d.clock.now())
        self.assertFalse(scan_hc["triggered"])
        self.assertEqual(scan_hc["reason"], "house_daily_cap")
        self.assertEqual(len(mock_llm.proactive_calls), 2)

        # 9) Outside active window blocks BEFORE LLM
        late_ts = dt.datetime(2026, 11, 22, 23, 15, tzinfo=JST).timestamp()
        d.clock.advance(max(0.0, late_ts - d.clock.now()))
        self._insert_script(d.world_db, ["rin"], "夜深了凛音想分享一首歌", d.clock.now())
        scan_win = d.proactive.scan(d, cfg=cfg, now=d.clock.now())
        self.assertFalse(scan_win["triggered"])
        self.assertEqual(scan_win["reason"], "outside_window")
        self.assertEqual(len(mock_llm.proactive_calls), 2)

        # 10) Next day JST resets daily quota cleanly
        next_day_ts = dt.datetime(2026, 11, 23, 10, 0, tzinfo=JST).timestamp()
        d.clock.advance(max(0.0, next_day_ts - d.clock.now()))
        self._insert_script(d.world_db, ["sora"], "苍空早起泡了一壶玄米茶", d.clock.now())
        scan_next = d.proactive.scan(d, cfg=cfg, now=d.clock.now())
        self.assertTrue(scan_next["triggered"])
        self.assertEqual(len(mock_llm.proactive_calls), 3)


if __name__ == "__main__":
    unittest.main()
