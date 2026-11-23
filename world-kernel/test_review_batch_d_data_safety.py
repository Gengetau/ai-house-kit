"""PR-D tests: backup retention/media/atomic restore + CLI, reset_house epoch guard & fault-tolerant config (D1-D5)."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.backup import create_backup, restore_backup, verify_backup  # noqa: E402
from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.cli import main as cli_main  # noqa: E402
from ai_house.daemon import HouseDaemon, JST, make_http_server  # noqa: E402
from ai_house.llm import ModelEndpoint  # noqa: E402


def _http_json(url: str, *, token: str = "") -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8") or "{}")


class ReviewBatchDDataSafetyTests(unittest.TestCase):
    def test_d1_d2_d3_wall_clock_prune_media_archive_lock_rollback_and_cli_restore(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        t0 = dt.datetime(2026, 11, 22, 10, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            rt,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_OWNER_TOKEN": "owner-tok-d123"},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        # Register d.close AFTER tmp.cleanup so close runs BEFORE tempdir unlink (Windows LIFO)
        self.addCleanup(d.close)

        # Seed media file & memory
        d.memory.add("sora", "private", "重要手记放在蓝盒子里", kind="fact", source="chat", now=t0)
        m_dir = rt / "media" / "generated" / "world"
        m_dir.mkdir(parents=True, exist_ok=True)
        media_fp = m_dir / "sample_art.png"
        media_fp.write_bytes(b"\x89PNG\r\n\x1a\nfake-media-bytes")

        # D1 + D3: Advancing virtual `now` by 60 days MUST NOT prune fresh real-time backups!
        arc1 = create_backup(rt, keep_count=5, keep_days=14, now=t0)["archive"]
        arc2 = create_backup(rt, keep_count=5, keep_days=14, now=t0 + 60 * 86400.0)["archive"]
        self.assertTrue(Path(arc1).exists(), "virtual clock advance must not expire fresh real-time archive")
        self.assertTrue(Path(arc2).exists())

        with tarfile.open(arc2, "r:gz") as tf:
            names = tf.getnames()
            self.assertIn("media/generated/world/sample_art.png", names)
            self.assertFalse(any(n.endswith(".daemon.lock") for n in names))
        self.assertTrue(verify_backup(arc2)["ok"])

        # D2: Active daemon lock blocks restore_backup on live runtime dir
        self.assertTrue((rt / ".daemon.lock").is_file())
        with self.assertRaises(RuntimeError):
            restore_backup(arc2, rt, overwrite=True)

        # Close daemon -> lock removed -> mutate state + drop stale -wal -> corrupt archive fails atomically without damage
        d.close()
        self.assertFalse((rt / ".daemon.lock").is_file())
        (rt / "memory.db-wal").write_bytes(b"stale-wal-junk")
        media_fp.write_bytes(b"MODIFIED_AFTER_BACKUP")

        tampered = Path(tmp.name) / "bad.tar.gz"
        raw_b = bytearray(Path(arc2).read_bytes())
        raw_b[len(raw_b) // 2] ^= 0xFF
        tampered.write_bytes(bytes(raw_b))
        with self.assertRaises(ValueError):
            restore_backup(tampered, rt, overwrite=True)
        # Original runtime dir stayed intact on verification failure
        self.assertEqual(media_fp.read_bytes(), b"MODIFIED_AFTER_BACKUP")

        # CLI `ai-house restore` restores media + DBs atomically and clears stale -wal
        rc = cli_main(["restore", arc2, "--data-dir", str(rt), "--overwrite", "--json"])
        self.assertEqual(rc, 0)
        self.assertFalse((rt / "memory.db-wal").exists())
        self.assertEqual(media_fp.read_bytes(), b"\x89PNG\r\n\x1a\nfake-media-bytes")
        # Backups directory inside rt/backups survived overwrite cleanly
        self.assertTrue(Path(arc2).exists())

        # Boot restored daemon cleanly
        d2 = HouseDaemon(
            rt,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={},
            start_ts=t0 + 600,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d2.close)
        self.assertIn("蓝盒子", d2.memory.recall("sora", "private", "重要手记蓝盒子"))

    def test_d4_converse_epoch_guard_and_complete_reset_cleanup(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def slow_llm(_sys: str, usr: str) -> Tuple[str, int]:
            if "刚刚说：" in usr:
                entered.set()
                release.wait(timeout=5.0)
            return "差点在reset之后写入旧回复", 10

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        t0 = dt.datetime(2026, 11, 22, 12, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            rt,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
            llm_fn=slow_llm,
        )
        self.addCleanup(d.close)

        # Populate sidecar DBs & state files that reset_house must purge
        d.model_router._record_usage(
            purpose="chat",
            actor="sora",
            endpoint=ModelEndpoint(base_url="https://api.openai.com", api_key="k", model="m1"),
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            latency_ms=10,
            ok=True,
            fallback_used=False,
        )
        d.identity_store.upsert_identity("visitor:qq:999", kind="visitor", display_name="访客999")
        (rt / "proactive_state.json").write_text("{}", encoding="utf-8")
        (rt / "group_behavior.json").write_text("{}", encoding="utf-8")
        (rt / "channel_spool.db").write_bytes(b"dummy")

        result_holder: list[Dict[str, Any]] = []
        th = threading.Thread(
            target=lambda: result_holder.append(d.converse("private:sora", "慢慢思考这句话", owner_speaker("房主"))),
            daemon=True,
        )
        th.start()
        self.assertTrue(entered.wait(timeout=3.0))

        # Reset house while converse is mid-LLM
        reset_res = d.reset_house()
        self.assertTrue(reset_res["ok"])
        self.assertTrue(reset_res["needs_onboarding"])
        release.set()
        th.join(timeout=3.0)

        self.assertTrue(result_holder)
        self.assertFalse(result_holder[0]["ok"])
        self.assertEqual(result_holder[0]["ignored"], "world_reset_during_turn")

        # Verify every runtime DB/state file was wiped & reset cleanly
        for gone_name in ("channel_spool.db", "proactive_state.json", "group_behavior.json"):
            self.assertFalse((rt / gone_name).exists(), f"{gone_name} should be deleted by reset_house")
        # Fresh usage.db & identity.db have zero stale rows from before reset
        with contextlib.closing(sqlite3.connect(rt / "usage.db")) as uc:
            self.assertEqual(uc.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0], 0)
        self.assertEqual([x for x in d.identity_store.list_identities() if x["identity_id"] == "visitor:qq:999"], [])

    def test_d5_invalid_plugin_env_degrades_gracefully_without_500_or_task_abort(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t0 = dt.datetime(2026, 11, 22, 13, 0, tzinfo=JST).timestamp()
        owner_tok = "owner-tok-d5"
        # Corrupt integer env vars across multiple plugins!
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={
                "AI_HOUSE_OWNER_TOKEN": owner_tok,
                "AI_HOUSE_REMINDERS_MAX_ACTIVE": "not_an_int",
                "AI_HOUSE_GROUP_MUTE_MINUTES": "99999999",
            },
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        # GET /api/settings/plugins returns 200 (never 500!) and surfaces config_warnings
        srv = make_http_server(d, host="127.0.0.1", port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"

        st, doc = _http_json(f"{base}/api/settings/plugins", token=owner_tok)
        self.assertEqual(st, 200)
        self.assertTrue(doc["ok"])
        by_id = {p["plugin_id"]: p for p in doc["plugins"]}
        self.assertTrue(by_id["reminders"]["config_warnings"])
        rem_max = next(f for f in by_id["reminders"]["fields"] if f["key"] == "max_active_per_user")
        self.assertEqual(rem_max["value"], 20)
        self.assertTrue(by_id["group_behavior"]["config_warnings"])

        # _run_plugin_tasks catches broken config/task and STILL completes without crashing post-step loop
        d._run_plugin_tasks(t0 + 100.0)


if __name__ == "__main__":
    unittest.main()
