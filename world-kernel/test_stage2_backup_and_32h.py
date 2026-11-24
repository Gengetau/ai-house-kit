"""Stage 2.8 tests: online hot backup/verify/prune/restore & 32-hour Stage 2 contract regression."""
from __future__ import annotations

import contextlib
import datetime as dt
import http.server
import json
import sqlite3
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.backup import create_backup, restore_backup, verify_backup  # noqa: E402
from ai_house.chat_identity import member_speaker, owner_speaker  # noqa: E402
from ai_house.cli import main as cli_main  # noqa: E402
from ai_house.daemon import HouseDaemon, JST, make_http_server  # noqa: E402


class _StatefulStage2MockServer:
    def __init__(self) -> None:
        self.calls: List[str] = []
        self.leak_mode = False
        self.guess_reply = False
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                ln = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                msgs = body.get("messages") or []
                sys_txt = str(msgs[0].get("content") or "") if msgs else ""
                usr_txt = str(msgs[-1].get("content") or "") if msgs else ""

                if "记忆整理员" in sys_txt:
                    parent.calls.append("memory_extract")
                    ops: List[Dict[str, Any]] = []
                    if "虾仁和腰果" in usr_txt:
                        ops.append({
                            "op": "add",
                            "kind": "fact",
                            "text": "房主私下说自己对虾仁和腰果过敏",
                            "src": [1],
                        })
                    if "过敏或者忌口" in usr_txt:
                        # Mock hallucinating extractor trying to write guessed allergens from question+reply
                        ops.append({
                            "op": "add",
                            "kind": "fact",
                            "text": "房主可能对花生或芒果过敏",
                            "src": [1, 2],
                        })
                    if "大扫除" in usr_txt:
                        ops.append({
                            "op": "add",
                            "kind": "fact",
                            "text": "下周三大家要一起大扫除",
                        })
                    content = json.dumps({"ops": ops}, ensure_ascii=False)
                elif "睡前小日记" in sys_txt:
                    parent.calls.append("diary")
                    content = "今天在洋馆和大家一起按节奏生活、看书又下厨，晚上早点休息。"
                elif "今天记下的事实" in usr_txt or "只输出 JSON" in usr_txt:
                    parent.calls.append("daynote")
                    content = json.dumps({
                        "text": (
                            "今日樱丘洋馆内各位住客的起居与作息按节奏平稳推进，苍空、凛音与黑羽分别完成了采购、"
                            "看书、工作与晚间餐食准备，大家围绕近期生活安排与晚间休息正常互动，"
                            "全天屋内事务均顺利收尾，夜深后各自回房休整。"
                        )
                    }, ensure_ascii=False)
                elif "输出必须是 JSON" in sys_txt or "action" in sys_txt.lower():
                    parent.calls.append("life")
                    content = json.dumps({"steps": []}, ensure_ascii=False)
                else:
                    parent.calls.append("chat")
                    if parent.leak_mode:
                        # changed: the outbound check needs most of the memory repeated (the old "item + allergy word" rule is gone)
                        content = "我记得房主私下说自己对虾仁和腰果过敏，点菜时记得避开哦！"
                    elif parent.guess_reply:
                        content = "我也不太确定，好像可能是对花生或芒果有点忌口吧？"
                    else:
                        content = "好呀，我已经认真记下来并留意着啦。"

                resp = {
                    "id": "mock-32h",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 24, "completion_tokens": 12, "total_tokens": 36},
                }
                raw = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

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
        self._th.join(timeout=2.0)


def _http_json(url: str, *, token: str = "", body: Any = None) -> Tuple[int, Dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
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
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8") or "{}")


class Stage2BackupAnd32hTests(unittest.TestCase):
    def test_backup_verify_tamper_prune_restore_and_cli_http(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        rt.mkdir(parents=True, exist_ok=True)
        # Put a fake .env inside runtime dir to prove backup never packs secrets
        (rt / ".env").write_text("AI_HOUSE_OWNER_TOKEN=secret-do-not-backup\n", encoding="utf-8")

        owner_tok = "bk-owner-tok-0001"
        t0 = dt.datetime(2026, 11, 22, 10, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            rt,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_OWNER_TOKEN": owner_tok},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)
        d.memory.add("sora", "private", "秘密书架抽屉里放着蓝色笔记本", kind="fact")

        # 1) Create multiple backups and check keep_count pruning
        arc1 = create_backup(rt, keep_count=2, now=t0)["archive"]
        arc2 = create_backup(rt, keep_count=2, now=t0 + 60)["archive"]
        arc3 = create_backup(rt, keep_count=2, now=t0 + 120)["archive"]
        self.assertFalse(Path(arc1).exists())
        self.assertTrue(Path(arc2).exists())
        self.assertTrue(Path(arc3).exists())

        # 2) Verify clean archive and confirm .env is absent
        v_res = verify_backup(arc3)
        self.assertTrue(v_res["ok"])
        self.assertIn("memory-common.db", v_res["dbs"])
        with tarfile.open(arc3, "r:gz") as tf:
            names = tf.getnames()
            self.assertFalse(any(n.endswith(".env") for n in names))

        # 3) Byte tampering fails verification
        tampered = Path(tmp.name) / "tampered.tar.gz"
        raw_bytes = bytearray(Path(arc3).read_bytes())
        raw_bytes[len(raw_bytes) // 2] ^= 0xFF
        tampered.write_bytes(bytes(raw_bytes))
        with self.assertRaises(ValueError):
            verify_backup(tampered)

        # 4) Restore into fresh runtime dir & boot fresh HouseDaemon
        restored_dir = Path(tmp.name) / "restored"
        r_res = restore_backup(arc3, restored_dir)
        self.assertTrue(r_res["ok"])
        d_restored = HouseDaemon(
            restored_dir,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={},
            start_ts=t0 + 180,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d_restored.close)
        recalled = d_restored.memory.recall("sora", "private", "蓝色笔记本")
        self.assertIn("蓝色笔记本", recalled)

        # 5) CLI `ai-house backup` and HTTP `/api/backups` & `/api/backup`
        rc = cli_main(["backup", "--data-dir", str(rt), "--keep-count", "3", "--json"])
        self.assertEqual(rc, 0)
        rc_v = cli_main(["backup", "--verify", arc3, "--json"])
        self.assertEqual(rc_v, 0)

        srv = make_http_server(d, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(th.join, 2.0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{port}"

        st_p, post_bk = _http_json(f"{base}/api/backup", token=owner_tok, body={"keep_count": 3})
        self.assertEqual(st_p, 200)
        self.assertTrue(post_bk["ok"])
        st_g, list_bk = _http_json(f"{base}/api/backups", token=owner_tok)
        self.assertEqual(st_g, 200)
        self.assertGreaterEqual(len(list_bk["backups"]), 2)

    def test_32h_virtual_time_stage2_contract_regression(self) -> None:
        mock_srv = _StatefulStage2MockServer()
        self.addCleanup(mock_srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        owner_tok = "stage2-32h-owner-01"
        t0 = dt.datetime(2026, 11, 21, 9, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={
                "AI_HOUSE_OWNER_TOKEN": owner_tok,
                "AI_HOUSE_API_KEY": "mock-key-32h",
                "AI_HOUSE_BASE_URL": mock_srv.base_url,
                "AI_HOUSE_MODEL": "mock-32h-model",
                "AI_HOUSE_MEMORY_TRUST": "1",  # trust levels are off by default; this case is about them
            },
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        # Day 1 09:00: private allergy secret + Issue #2 (errand + allergy memo)
        d.converse("private:sora", "我对虾仁和腰果过敏，先别在群里说", owner_speaker("房主"))
        d.intents.enqueue_chat(
            "sora",
            "苍空今天顺便买2个洋葱好不好？",
            "好呀，我出门时去买2个洋葱！",
            where="group",
            now=d.clock.now(),
        )
        d.intents.enqueue_chat(
            "sora",
            "记住我对虾仁过敏，晚饭别放虾",
            "记牢了！做菜绝对避开虾仁。",
            where="private",
            intents=[{"text": "房主对虾仁过敏", "kind": "cook", "答应了的": ["sora"]}],
            now=d.clock.now(),
        )

        # Issue #4: Group member asks open question + bot guesses peanut/mango -> MUST NOT write fact
        mock_srv.guess_reply = True
        d.converse("qq-group:9001", "房主有什么过敏或者忌口吗？", member_speaker("阿晴", "8002"), actor="sora")
        mock_srv.guess_reply = False
        d.converse("qq-group:9001", "听说下周三大家要一起大扫除", member_speaker("阿晴", "8002"), actor="sora")
        d.memory.extract(force=True, now=d.clock.now())

        grp_mems = d.memory.list_entries("sora", "group", channel="qq-group:9001", now=d.clock.now())
        self.assertFalse(any("花生" in m["text"] or "芒果" in m["text"] for m in grp_mems))
        cleanup_claims = [m for m in grp_mems if "大扫除" in m["text"]]
        self.assertEqual(len(cleanup_claims), 1)
        self.assertEqual(cleanup_claims[0]["kind"], "claim")
        self.assertEqual(cleanup_claims[0]["trust"], "group_stated")

        # Daytime verbal mention "买好了" does NOT close onion errand
        d.converse("private:sora", "洋葱我已经买好啦", owner_speaker("房主"))
        d.intents.sweep(d.clock.now())
        onion_mid = [r for r in d.intents.list_intents(actor="sora") if r["item"] == "onion"][0]
        self.assertEqual(onion_mid["state"], "open")

        # Advance Day 1 daytime -> evening -> night (diary & sleep) -> Day 2 afternoon (32h total!)
        saw_sleep = False
        for hour_step in range(32):
            d.fast_forward(60)
            if hour_step in (15, 16, 17, 18):  # 00:00 - 04:00 JST Day 2
                st = d.get_state()
                if any((res.get("reality") or {}).get("availability") == "sleeping" for res in (st.get("residents") or {}).values()):
                    saw_sleep = True
        self.assertAlmostEqual(d.clock.now() - t0, 32 * 3600, delta=120.0)
        self.assertTrue(saw_sleep)

        # Genuine autonomous supermarket execution MUST complete the onion errand without manual evidence injection
        onion_after_32h = [r for r in d.intents.list_intents(actor="sora") if r["item"] == "onion"][0]
        self.assertEqual(onion_after_32h["state"], "completed")
        self.assertTrue(str(onion_after_32h.get("evidence_key") or "").startswith("ai-house/"))

        allergy_memo = [r for r in d.intents.list_intents(actor="sora") if "虾仁过敏" in r["summary"]][0]
        self.assertEqual(allergy_memo["kind"], "memo")
        self.assertEqual(allergy_memo["state"], "open")

        # Verify evening diaries generated with zero private allergy leaks
        diaries = d.diary.list_diaries()
        self.assertTrue(diaries)
        self.assertFalse(any("虾仁" in x["text"] or "腰果" in x["text"] for x in diaries))

        # Verify outfit log or cooking/life logs & usage ledger
        with contextlib.closing(sqlite3.connect(d.life_db)) as lc:
            outfits_cnt = int(lc.execute("SELECT COUNT(*) FROM outfit_log").fetchone()[0])
            self.assertGreater(outfits_cnt, 0)

        usage = d.get_usage_summary(period="all")
        self.assertGreater(usage["totals"]["calls"], 0)
        purposes = {p["purpose"] for p in usage["by_purpose"]}
        self.assertIn("chat", purposes)
        self.assertIn("diary", purposes)

        # Outbound leak guard blocks leaking private shrimp/cashew allergy into group
        # positive control: the allergy memory exists and is recalled in private
        self.assertIn("虾仁", d.memory.recall("sora", "private", "房主对虾仁和腰果过敏吗", now=d.clock.now()))
        mock_srv.leak_mode = True
        leaked_rep = d.converse("qq-group:9001", "晚饭买什么食材呀？", member_speaker("阿晴", "8002"), actor="sora")
        self.assertEqual(leaked_rep["replies"][0]["mode"], "blocked")

        # Verify `tools/run_stage2_32h_live.py` invariant checker passes cleanly on valid state
        sys.path.insert(0, str(ROOT / "tools"))
        from run_stage2_32h_live import verify_stage2_32h_invariants  # noqa: E402

        self.assertEqual(verify_stage2_32h_invariants(d, saw_sleep=saw_sleep), [])
        # Injecting a leaked group message triggers non-empty failure list (non-zero exit in live runner)
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute(
                    "INSERT INTO chat_messages(channel, sender, sender_name, actor_target, text, badge, mode, created_at) "
                    "VALUES('qq-group:9001', 'sora', '苍空', 'member', '房主其实对虾仁过敏', '', 'live_api', ?)",
                    (d.clock.now(),),
                )
        fails = verify_stage2_32h_invariants(d)
        self.assertTrue(any("问题#4失败" in f for f in fails))
        with contextlib.closing(sqlite3.connect(d.world_db)) as wc:
            with wc:
                wc.execute("UPDATE house_intents SET state='completed' WHERE kind='memo'")
        fails2 = verify_stage2_32h_invariants(d)
        self.assertTrue(any("问题#2失败" in f for f in fails2))


if __name__ == "__main__":
    unittest.main()
