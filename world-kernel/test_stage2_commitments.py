"""Stage 2.5 tests: full commitments with owner gating, evidence closing, shared plans & memo protection."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3
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

from ai_house.chat_identity import member_speaker, owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, JST, make_http_server  # noqa: E402
from worldkernel.chatlink import ChatLink  # noqa: E402


def _http_json(url: str, *, token: str = "", body: Any = None) -> Tuple[int, Dict[str, Any]]:
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
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8") or "{}")


class Stage2CommitmentTests(unittest.TestCase):
    def test_errand_verbal_claim_never_closes_until_real_evidence_arrives(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t0 = dt.datetime(2026, 11, 22, 10, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        # Owner asks sora to buy 2 bottles of milk tonight
        d.intents.enqueue_chat(
            "sora",
            "苍空今晚帮我买2盒牛奶回来好不好？",
            "没问题，我今晚出去顺路带 2 盒牛奶回来！",
            where="group",
            now=t0,
            speaker_role="owner",
            proposer_id="user:owner",
            party=["sora"],
        )
        rows = [r for r in d.intents.list_intents(actor="sora") if r["item"] == "milk"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "errand")
        self.assertEqual(rows[0]["qty"], 2)
        self.assertEqual(rows[0]["state"], "open")
        self.assertIsNotNone(rows[0]["due_from"])
        self.assertIsNotNone(rows[0]["due_to"])

        # Mere chat saying "already bought" MUST NOT close the errand!
        d.converse("private:sora", "牛奶买好了吗？我已经买回来了吧", owner_speaker())
        d.intents.sweep(d.clock.now())
        rows_after_talk = [r for r in d.intents.list_intents(actor="sora") if r["intent_id"] == rows[0]["intent_id"]]
        self.assertEqual(rows_after_talk[0]["state"], "open")

        # Partial purchase (qty=1 < 2) also stays open until total >= 2
        at1 = d.clock.now() + 60
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', 1, ?, ?)",
                    (
                        "purchased:milk:1",
                        at1,
                        json.dumps({"kind": "purchased", "actor": "sora", "item": "milk", "qty": 1, "at": at1}),
                    ),
                )
        d.intents.sweep(at1)
        rows_partial = [r for r in d.intents.list_intents(actor="sora") if r["intent_id"] == rows[0]["intent_id"]]
        self.assertEqual(rows_partial[0]["state"], "open")

        # Second purchase (qty=1) satisfies total qty=2 -> closes via real evidence
        at2 = at1 + 30
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', 2, ?, ?)",
                    (
                        "brought_home:milk:2",
                        at2,
                        json.dumps({"kind": "brought_home", "actor": "sora", "item": "milk", "qty": 1, "at": at2}),
                    ),
                )
        d.intents.sweep(at2)
        rows_done = [r for r in d.intents.list_intents(actor="sora") if r["intent_id"] == rows[0]["intent_id"]]
        self.assertEqual(rows_done[0]["state"], "completed")
        self.assertEqual(rows_done[0]["evidence_key"], "brought_home:milk:2")

    def test_allergy_memo_non_owner_gate_shared_plan_chatlink_expiry_and_cancel_api(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        priv_tok = "priv-commit-tok-01"
        t0 = dt.datetime(2026, 11, 22, 11, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_PRIVATE_TOKEN": priv_tok},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        # 1) Non-owner member cannot create commitments via converse or enqueue_chat
        res_mem = d.converse("qq-group:9001", "苍空明天帮我买牛奶", member_speaker("群友甲", "8005"), actor="sora")
        self.assertTrue(res_mem["replies"])
        self.assertEqual(d.intents.list_intents(), [])
        self.assertIsNone(
            d.intents.enqueue_chat("sora", "去买菜", "好", speaker_role="member", proposer_id="qq:8005", now=t0)
        )
        self.assertEqual(d.intents.list_intents(), [])

        # 2) Owner allergy note -> memo, never completed by unrelated cooking or expired after 72h
        d.intents.enqueue_chat(
            "sora",
            "记住我对虾仁过敏，一点都别放",
            "记牢啦，做菜绝对避开虾仁！",
            where="private",
            intents=[{"text": "房主对虾仁过敏", "kind": "cook", "答应了的": ["sora"]}],
            now=t0,
        )
        memo_rows = [r for r in d.intents.list_intents(actor="sora") if "虾仁过敏" in r["summary"]]
        self.assertEqual(len(memo_rows), 1)
        self.assertEqual(memo_rows[0]["kind"], "memo")

        # Unrelated kernel cook activity + 72h clock advance leaves memo open
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', 10, ?, ?)",
                    (
                        "act:cook:1",
                        t0 + 300,
                        json.dumps({"kind": "activity", "actor": "sora", "activity": "cook", "at": t0 + 300}),
                    ),
                )
        d.intents.sweep(t0 + 72 * 3600)
        memo_after = [r for r in d.intents.list_intents(actor="sora") if r["intent_id"] == memo_rows[0]["intent_id"]]
        self.assertEqual(memo_after[0]["state"], "open")

        # 3) Multi-resident shared plan shares plan_ref & produces ChatLink-compatible owner_chat_queue body
        d.intents.enqueue_chat(
            "sora",
            "明天下午我们一起去晴空都逛逛吧",
            "好呀！",
            where="group",
            intents=[{"text": "明天一起去晴空都逛逛", "kind": "outing", "答应了的": ["sora"]}],
            now=t0 + 10,
            party=["sora", "rin"],
            agreed=["sora"],
        )
        d.intents.enqueue_chat(
            "rin",
            "明天下午我们一起去晴空都逛逛吧",
            "我也去！",
            where="group",
            intents=[{"text": "明天一起去晴空都逛逛", "kind": "outing", "答应了的": ["rin"]}],
            now=t0 + 10,
            party=["sora", "rin"],
            agreed=["rin"],
        )
        sora_out = [r for r in d.intents.list_intents(actor="sora") if r["kind"] == "outing"][0]
        rin_out = [r for r in d.intents.list_intents(actor="rin") if r["kind"] == "outing"][0]
        self.assertTrue(sora_out["plan_ref"])
        self.assertEqual(sora_out["plan_ref"], rin_out["plan_ref"])
        self.assertEqual(set(sora_out["party"]), {"sora", "rin"})
        self.assertEqual(set(sora_out["agreed"]), {"sora", "rin"})

        # Verify ChatLink consumes queued rows without crashing and emits expected structure
        link = ChatLink(d.authority.life, d.world_db)
        link.step()
        ctx_sora = link.context("sora", "decide")
        self.assertTrue(ctx_sora.get("qq_chat"))
        plan_entry = next(
            it for it in ctx_sora["qq_chat"]
            if it.get("你的打算") and any("晴空都" in p.get("text", "") for p in it["你的打算"])
        )
        self.assertIn("rin", plan_entry["你的打算"][0]["和谁"])

        # 4) Cancel commitment via HTTP POST /api/intents/cancel & test expiration of overdue outing
        srv = make_http_server(d, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(th.join, 2.0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{port}"

        st_c, res_c = _http_json(
            f"{base}/api/intents/cancel",
            token=priv_tok,
            body={"intent_id": sora_out["intent_id"], "reason": "owner_rescheduled"},
        )
        self.assertEqual(st_c, 200)
        self.assertEqual(res_c["intent"]["state"], "cancelled")

        # Rin's unfulfilled open outing expires after due_to window passes
        d.intents.sweep(rin_out["expires_at"] + 60)
        rin_after = [r for r in d.intents.list_intents(actor="rin") if r["intent_id"] == rin_out["intent_id"]][0]
        self.assertEqual(rin_after["state"], "expired")


if __name__ == "__main__":
    unittest.main()
