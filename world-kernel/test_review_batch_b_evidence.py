"""PR-B tests: group owner-evidence binding, conflict suppression, v3 migration & commitment rigor (B1-B6)."""
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
from pathlib import Path
from typing import Any, Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.chat_identity import owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, JST  # noqa: E402
from ai_house.memory import MemoryStore, SCHEMA_VERSION  # noqa: E402
from worldkernel.store import VirtualClock  # noqa: E402


class _MockDurianPromiseServer:
    """Loopback OpenAI-compatible mock where the model calls `record_promise` for an out-of-catalog item."""

    def __init__(self) -> None:
        self.tool_results: list[Dict[str, Any]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                msgs = body.get("messages") or []
                tool_msgs = [m for m in msgs if m.get("role") == "tool"]
                for tm in tool_msgs:
                    try:
                        outer.tool_results.append(json.loads(tm.get("content") or "{}"))
                    except ValueError:
                        pass
                if body.get("tools") and not tool_msgs:
                    doc = {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [{
                                    "id": "tc_durian",
                                    "type": "function",
                                    "function": {
                                        "name": "record_promise",
                                        "arguments": json.dumps(
                                            {"summary": "帮家里买一个榴莲", "kind": "errand", "item": "榴莲", "qty": 1},
                                            ensure_ascii=False,
                                        ),
                                    },
                                }],
                            }
                        }],
                        "usage": {"total_tokens": 25},
                    }
                else:
                    doc = {
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "好的，已帮你记下买榴莲的待办提醒（不在内置超市词表，需要你确认后再结单哦）。",
                            }
                        }],
                        "usage": {"total_tokens": 20},
                    }
                raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class ReviewBatchBEvidenceTests(unittest.TestCase):
    def _make_memory(self, llm_fn=None) -> Tuple[Path, MemoryStore, VirtualClock]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t0 = dt.datetime(2026, 11, 22, 12, 0, tzinfo=JST).timestamp()
        clock = VirtualClock(t0)
        st = MemoryStore(tmp.name, clock=clock, names={"sora": "苍空", "rin": "凛音"}, llm_fn=llm_fn)
        self.addCleanup(st.close)
        return Path(tmp.name), st, clock

    def test_b1_group_owner_evidence_binding_rejects_omitted_src_or_member_import(self) -> None:
        def batch_llm(_sys: str, _usr: str) -> Dict[str, Any]:
            return {
                "ops": [
                    # 1) Omits src while batch contains owner line L1 + member claim L2 -> must downgrade!
                    {
                        "op": "add",
                        "text": "苍空讨厌吃胡萝卜",
                        "kind": "preference",
                    },
                    # 2) Cites owner line L1 in src, but steals member-only claim "胡萝卜" -> must downgrade!
                    {
                        "op": "add",
                        "text": "房主出门时听说苍空讨厌吃胡萝卜",
                        "kind": "fact",
                        "src": [1],
                    },
                    # 3) Explicitly cites owner statement L1 with lexical alignment and no member words -> verified!
                    {
                        "op": "add",
                        "text": "房主明天要去京都出差",
                        "kind": "fact",
                        "src": [1],
                    },
                    # 4) Group rule without owner evidence -> rejected by gate
                    {
                        "op": "add",
                        "text": "以后群里禁止提胡萝卜",
                        "kind": "rule",
                    },
                ]
            }

        _, st, _ = self._make_memory(llm_fn=batch_llm)
        st.observe("sora", "group", "我明天要去京都出差啦", channel="group", speaker="房主", speaker_role="owner")
        st.observe("sora", "group", "顺便说苍空好像讨厌吃胡萝卜", channel="group", speaker="群友甲", speaker_role="member")
        res = st.extract("sora", scope="group", channel="group", force=True)
        self.assertEqual(res["added"], 3)
        self.assertEqual(res["rejected"], 1)

        rows = st.list_entries("sora", "group", channel="group")
        by_text = {r["text"]: r for r in rows}
        self.assertEqual(by_text["苍空讨厌吃胡萝卜"]["kind"], "claim")
        self.assertEqual(by_text["苍空讨厌吃胡萝卜"]["trust"], "group_stated")
        self.assertEqual(by_text["房主出门时听说苍空讨厌吃胡萝卜"]["kind"], "claim")
        self.assertEqual(by_text["房主出门时听说苍空讨厌吃胡萝卜"]["trust"], "group_stated")
        self.assertEqual(by_text["房主明天要去京都出差"]["kind"], "fact")
        self.assertEqual(by_text["房主明天要去京都出差"]["trust"], "verified")
        self.assertEqual(by_text["房主明天要去京都出差"]["source"], "auto:owner_verified")

    def test_b2_conflict_suppression_ranks_old_verified_above_fresh_conflicting_claim(self) -> None:
        _, st, clock = self._make_memory()
        old_ts = clock.now() - 60 * 86400
        fresh_ts = clock.now()

        # 60-day-old verified allergy fact
        st.add(
            "sora",
            "group",
            "房主对海鲜和虾仁严重过敏，绝对不能吃",
            channel="group",
            about=["房主", "海鲜"],
            kind="fact",
            trust="verified",
            source="owner_verified",
            now=old_ts,
        )
        # Fresh conflicting group claim
        st.add(
            "sora",
            "group",
            "群友说房主最爱吃海鲜和麻辣大虾",
            channel="group",
            about=["房主", "海鲜"],
            kind="claim",
            trust="group_stated",
            source="auto",
            now=fresh_ts,
        )

        # Even with min_score=0.05 to inspect ranking, verified beats conflicting fresh claim by a wide margin
        ranked = st.search("sora", "group", "房主吃海鲜和虾怎么样", channel="group", min_score=0.05, k=5)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0][1]["trust"], "verified")
        self.assertEqual(ranked[1][1]["trust"], "group_stated")
        self.assertTrue(ranked[1][1].get("_conflict_verified"))
        self.assertGreater(ranked[0][0], ranked[1][0] * 1.5)
        self.assertIn("群聊未核实传言·与已知事实冲突，勿采信", st._format_row(ranked[1][1]))

        # Default recall surfaces verified fact and keeps conflicting claim out or warning-tagged below
        recalled = st.recall("sora", "group", "房主能吃海鲜虾仁吗", channel="group", now=fresh_ts)
        self.assertIn("严重过敏", recalled)

    def test_b3_schema_v3_migration_demotes_legacy_polluted_group_facts(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_dir = Path(tmp.name)
        db_path = db_dir / "memory.db"

        # Seed a legacy v2 store and manually insert polluted rows as pre-fix v2 would have
        t0 = dt.datetime(2026, 11, 22, 12, 0, tzinfo=JST).timestamp()
        with contextlib.closing(sqlite3.connect(db_path)) as c:
            from ai_house.memory import BASE_INDEXES_SQL, BASE_TABLES_SQL

            c.executescript(BASE_TABLES_SQL + BASE_INDEXES_SQL)
            c.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(1, ?)", (t0,))
            c.execute("INSERT INTO schema_migrations(version, applied_at) VALUES(2, ?)", (t0,))
            c.execute(
                "INSERT INTO mem(owner, scope, channel, kind, trust, text, source, status, created, updated) "
                "VALUES('sora', 'group', 'group', 'fact', 'verified', '群友乱猜房主喜欢香菜', 'auto', 'active', ?, ?)",
                (t0, t0),
            )
            c.execute(
                "INSERT INTO mem(owner, scope, channel, kind, trust, text, source, status, created, updated) "
                "VALUES('sora', 'group', 'group', 'fact', 'verified', '房主确认喜欢黑咖啡', 'owner_verified', 'active', ?, ?)",
                (t0, t0),
            )
            c.execute(
                "INSERT INTO mem(owner, scope, channel, kind, trust, text, source, status, created, updated) "
                "VALUES('sora', 'world', 'world', 'diary', 'verified', '今天的日记内容很好', 'diary', 'active', ?, ?)",
                (t0, t0),
            )
            c.commit()

        st = MemoryStore(db_dir, clock=VirtualClock(t0 + 10))
        self.addCleanup(st.close)
        with contextlib.closing(sqlite3.connect(db_path)) as c:
            c.row_factory = sqlite3.Row
            max_ver = c.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            self.assertEqual(max_ver, SCHEMA_VERSION)
            rows = {r["text"]: dict(r) for r in c.execute("SELECT * FROM mem").fetchall()}
        self.assertEqual(rows["群友乱猜房主喜欢香菜"]["kind"], "claim")
        self.assertEqual(rows["群友乱猜房主喜欢香菜"]["trust"], "group_stated")
        self.assertEqual(rows["房主确认喜欢黑咖啡"]["kind"], "fact")
        self.assertEqual(rows["房主确认喜欢黑咖啡"]["trust"], "verified")
        self.assertEqual(rows["今天的日记内容很好"]["kind"], "diary")
        self.assertEqual(rows["今天的日记内容很好"]["trust"], "verified")

    def test_b4_out_of_catalog_errand_stays_open_and_ignores_unrelated_purchases(self) -> None:
        srv = _MockDurianPromiseServer()
        self.addCleanup(srv.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t0 = dt.datetime(2026, 11, 22, 14, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={
                "AI_HOUSE_API_KEY": "mock-key",
                "AI_HOUSE_BASE_URL": srv.base_url,
                "AI_HOUSE_MODEL": "mock-model",
            },
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        res = d.converse("private:sora", "今天帮忙买一个榴莲回来吧", owner_speaker("房主"))
        self.assertTrue(res["replies"])
        self.assertTrue(srv.tool_results)
        self.assertEqual(srv.tool_results[0].get("in_catalog"), False)
        self.assertIn("不在内置超市词表", srv.tool_results[0].get("note", ""))

        durian_rows = [r for r in d.intents.list_intents(actor="sora") if "榴莲" in r["summary"]]
        self.assertEqual(len(durian_rows), 1)
        self.assertEqual(durian_rows[0]["kind"], "errand")
        self.assertFalse(durian_rows[0]["item"])
        self.assertEqual(durian_rows[0]["raw_item"], "榴莲")
        self.assertEqual(durian_rows[0]["state"], "open")

        # Also test enqueue_chat fallback without tool calls
        d.intents.enqueue_chat("sora", "明天记得顺路买两节电池", "好呀", where="group", now=t0 + 10)
        battery_rows = [r for r in d.intents.list_intents(actor="sora") if "电池" in r["summary"]]
        self.assertEqual(len(battery_rows), 1)
        self.assertEqual(battery_rows[0]["raw_item"], "电池")
        self.assertEqual(battery_rows[0]["state"], "open")

        # Purchasing an unrelated catalog item (onion) MUST NOT close durian or battery!
        at1 = d.clock.now() + 120
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', 1, ?, ?)",
                    ("purchased:onion:1", at1, json.dumps({"kind": "purchased", "actor": "sora", "item": "onion", "qty": 2, "at": at1})),
                )
        d.intents.sweep(at1)

        after = {r["intent_id"]: r for r in d.intents.list_intents(actor="sora")}
        self.assertEqual(after[durian_rows[0]["intent_id"]]["state"], "open")
        self.assertEqual(after[battery_rows[0]["intent_id"]]["state"], "open")

    def test_b5_private_promises_never_merge_across_residents(self) -> None:
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

        d.intents.enqueue_chat("sora", "周末一起大扫除整理客厅吧", "好呀", where="private", now=t0)
        d.intents.enqueue_chat("rin", "周末一起大扫除整理客厅吧", "嗯呐", where="private", now=t0)

        sora_rows = d.intents.list_intents(actor="sora")
        rin_rows = d.intents.list_intents(actor="rin")
        self.assertEqual(len(sora_rows), 1)
        self.assertEqual(len(rin_rows), 1)
        self.assertNotEqual(sora_rows[0]["plan_ref"], rin_rows[0]["plan_ref"])
        self.assertEqual(sora_rows[0]["party"], ["sora"])
        self.assertEqual(rin_rows[0]["party"], ["rin"])

        # Completing sora's private promise via plan_finished leaves rin's open
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', 11, ?, ?)",
                    ("plan_finished:sora", t0 + 60, json.dumps({"kind": "plan_finished", "actor": "sora", "outcome": "done", "at": t0 + 60})),
                )
        d.intents.sweep(t0 + 60)
        self.assertEqual(d.intents.list_intents(actor="sora")[0]["state"], "completed")
        self.assertEqual(d.intents.list_intents(actor="rin")[0]["state"], "open")

    def test_b6_outing_departed_and_sleep_dialogue_never_false_close(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        t0 = dt.datetime(2026, 11, 22, 15, 0, tzinfo=JST).timestamp()
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={},
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        d.intents.enqueue_chat(
            "sora",
            "下午去图书馆借书吧",
            "好呀",
            where="private",
            intents=[{"text": "去图书馆借书", "kind": "outing", "答应了的": ["sora"]}],
            now=t0,
        )
        d.intents.enqueue_chat(
            "sora",
            "今晚早点睡哦",
            "嗯，今晚早点睡",
            where="private",
            intents=[{"text": "今晚早点睡", "kind": "chat_promise", "答应了的": ["sora"]}],
            now=t0,
        )

        # 1) departed alone + arrived at supermarket (wrong place) + script dialogue mentioning "睡" must NOT close either!
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', 20, ?, ?)",
                    ("departed:1", t0 + 30, json.dumps({"kind": "departed", "actor": "sora", "place": "图书馆", "at": t0 + 30})),
                )
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', 21, ?, ?)",
                    ("arrived:market", t0 + 40, json.dumps({"kind": "arrived", "actor": "sora", "place": "supermarket", "at": t0 + 40})),
                )
                cols = {r[1] for r in c.execute("PRAGMA table_info(script)").fetchall()}
                if "created" in cols:
                    c.execute(
                        "INSERT INTO script(ts, actors, text, created, source) VALUES(?, ?, ?, ?, 'kernel')",
                        (t0 + 50, json.dumps(["sora"]), "我现在还不困，先不想睡觉呢。", t0 + 50),
                    )
                else:
                    c.execute(
                        "INSERT INTO script(ts, actors, text, source) VALUES(?, ?, ?, 'kernel')",
                        (t0 + 50, json.dumps(["sora"]), "我现在还不困，先不想睡觉呢。"),
                    )
        d.intents.sweep(t0 + 60)
        mid = {r["kind"]: r for r in d.intents.list_intents(actor="sora")}
        self.assertEqual(mid["outing"]["state"], "open")
        self.assertEqual(mid["chat_promise"]["state"], "open")

        # 2) Real arrived at library + structured sleep activity DO close them
        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            with c:
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', 22, ?, ?)",
                    ("arrived:lib", t0 + 120, json.dumps({"kind": "arrived", "actor": "sora", "place": "library", "at": t0 + 120})),
                )
                c.execute(
                    "INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?, 'ev', 23, ?, ?)",
                    ("act:sleep", t0 + 180, json.dumps({"kind": "activity", "actor": "sora", "activity": "sleep", "at": t0 + 180})),
                )
        d.intents.sweep(t0 + 180)
        done = {r["kind"]: r for r in d.intents.list_intents(actor="sora")}
        self.assertEqual(done["outing"]["state"], "completed")
        self.assertEqual(done["chat_promise"]["state"], "completed")


if __name__ == "__main__":
    unittest.main()
