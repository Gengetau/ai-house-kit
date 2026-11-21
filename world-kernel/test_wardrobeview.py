"""她的衣柜: worldkernel.wardrobeview over synthetic life1.db (read-only, offline)."""
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from worldkernel import wardrobeview
from worldkernel.store import SCHEMA
from worldkernel.wardrobe import SCHEMA as WARDROBE

JST = dt.timezone(dt.timedelta(hours=9))
M = dt.datetime(2026, 11, 10, tzinfo=JST).timestamp()
DAY = 86400
NOW = M + 16 * 3600


def build_db(path=":memory:"):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    for stmt in WARDROBE:
        db.execute(stmt)
    db.execute(
        "INSERT INTO events(event_id, kind, actor_id, occurred_at, recorded_at, location, payload, knowers,"
        " disclosure_scope, source_ref, source_kind, trust, retention_class, synthetic, runtime_epoch)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (101, "purchased", "wanda", M - DAY + 11 * 3600, M - DAY + 11 * 3600, "boutique",
         json.dumps({"yen": 4800, "account": "owner_card", "name": "SECRET_ITEM_CODE"}),
         "[]", "public", "act-101", "kernel", "kernel", "permanent", 0, 1),
    )
    db.execute(
        "INSERT INTO events(event_id, kind, actor_id, occurred_at, recorded_at, location, payload, knowers,"
        " disclosure_scope, source_ref, source_kind, trust, retention_class, synthetic, runtime_epoch)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (102, "walk", "wanda", M - DAY + 12 * 3600, M - DAY + 12 * 3600, "living",
         json.dumps({"yen": 999}), "[]", "public", "act-102", "kernel", "kernel", "permanent", 0, 1),
    )
    rows = [
        ("wanda_uniform", "wanda", "深蓝长裙制服装", "dress", "navy", "cotton", 12, 26, '["home","out"]', 5, "initial_wardrobe", "worn", 0),
        ("wanda_apron", "wanda", "打下手围裙", "accessory", "white", "linen", 5, 30, '["home"]', 4, "initial_wardrobe", "clean", 0),
        ("wanda_bought_knit", "wanda", "奶油针织衫", "top", "cream", "wool", 8, 20, '["out"]', 4, "event:101", "clean", 0),
        ("wanda_missing_ev", "wanda", "失踪事件裙", "dress", "black", "silk", 10, 22, '["formal"]', 3, "event:999", "clean", 0),
        ("wanda_other_ev", "wanda", "非购买事件外套", "outer", "gray", "wool", 5, 18, '["out"]', 2, "event:102", "clean", 0),
        ("wanda_blank_lbl", "wanda", "", "top", "blue", "cotton", 10, 25, '["home"]', 3, "custom_src", "clean", 0),
        ("nell_dress", "nell", "奈尔长裙", "dress", "black", "cotton", 12, 24, '["home"]', 4, "initial_wardrobe", "clean", 0),
    ]
    for r in rows:
        db.execute("INSERT INTO garments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    return db


def wear(db, actor, items, occasion, at, eid, current=False):
    db.execute("INSERT INTO outfit_log VALUES(?,?,?,?,?)", (actor, json.dumps(items), occasion, at, eid))
    if current:
        db.execute("INSERT OR REPLACE INTO outfits VALUES(?,?,?,?,?)", (actor, json.dumps(items), occasion, at, eid))


class WardrobeViewTests(unittest.TestCase):
    def test_1_and_2_sources_and_no_yen_or_account_leak(self):
        db = build_db()
        res = wardrobeview.view(db, "wanda", NOW)
        by_id = {g["id"]: g for g in res["garments"]}
        self.assertEqual(by_id["wanda_uniform"]["source"]["type"], "initial")
        self.assertEqual(by_id["wanda_bought_knit"]["source"]["type"], "bought")
        self.assertEqual(by_id["wanda_bought_knit"]["source"]["day"], "2026-11-09")
        self.assertRegex(by_id["wanda_bought_knit"]["source"]["day"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(by_id["wanda_bought_knit"]["source"]["place"])
        self.assertEqual(by_id["wanda_missing_ev"]["source"]["type"], "unknown")
        self.assertEqual(by_id["wanda_missing_ev"]["source"]["event_id"], 999)
        self.assertEqual(by_id["wanda_other_ev"]["source"]["type"], "unknown")
        self.assertEqual(by_id["wanda_other_ev"]["source"]["event_id"], 102)
        self.assertEqual(by_id["wanda_blank_lbl"]["source"]["type"], "unknown")
        self.assertIsNone(by_id["wanda_blank_lbl"]["source"]["event_id"])
        self.assertEqual(by_id["wanda_blank_lbl"]["label"], "衣物名称未记录")
        self.assertNotIn("nell_dress", by_id)
        raw_json = json.dumps(res, ensure_ascii=False).lower()
        self.assertNotIn("yen", raw_json)
        self.assertNotIn("account", raw_json)
        self.assertNotIn("4800", raw_json)
        self.assertNotIn("owner_card", raw_json)
        self.assertNotIn("secret_item_code", raw_json)
        self.assertEqual(res["summary"]["garments"], 6)
        self.assertEqual(res["summary"]["by_source"], {"initial": 2, "bought": 1, "gift": 0, "unknown": 3})
        self.assertEqual(res["summary"]["worn_waiting"], 1)

    def test_3_4_and_9_counts_order_agnostic_outfit_id_and_no_current_double_count(self):
        db = build_db()
        wear(db, "wanda", ["wanda_apron", "wanda_uniform"], "home", NOW - 40 * DAY, 1)
        wear(db, "wanda", ["wanda_uniform", "wanda_apron"], "home", NOW - 2 * DAY, 2)
        wear(db, "wanda", ["wanda_apron", "wanda_uniform"], "out", NOW - 1 * DAY, 3, current=True)
        res = wardrobeview.view(db, "wanda", NOW, days=30)
        self.assertEqual(len(res["outfits"]), 1)
        o = res["outfits"][0]
        self.assertEqual(o["outfit_id"], "wanda_apron+wanda_uniform")
        self.assertEqual(o["count_window"], 2)
        self.assertEqual(o["count_all"], 3)
        self.assertEqual(o["last_at"], NOW - 1 * DAY)
        self.assertTrue(o["current"])
        self.assertEqual(o["occasions"], {"home": 2, "out": 1})
        by_id = {g["id"]: g for g in res["garments"]}
        self.assertTrue(by_id["wanda_uniform"]["wearing"])
        self.assertEqual(by_id["wanda_uniform"]["worn"], {"count_window": 2, "count_all": 3, "last_at": NOW - 1 * DAY})
        self.assertEqual(res["current"]["outfit_id"], "wanda_apron+wanda_uniform")
        self.assertFalse(by_id["wanda_bought_knit"]["wearing"])

    def test_5_garments_table_dropped_degrades_gracefully(self):
        db = build_db()
        wear(db, "wanda", ["wanda_apron", "wanda_uniform"], "home", NOW - 3600, 10, current=True)
        db.execute("DROP TABLE garments")
        res = wardrobeview.view(db, "wanda", NOW)
        self.assertIsNone(res["garments"])
        self.assertIn("garments_error", res)
        self.assertIsNotNone(res["current"])
        self.assertEqual(res["current"]["labels"], ["wanda_apron", "wanda_uniform"])
        self.assertTrue(res["current"]["labels_unknown"])
        self.assertEqual(len(res["outfits"]), 1)
        self.assertTrue(res["outfits"][0]["labels_unknown"])
        self.assertEqual(len(res["log"]["rows"]), 1)
        self.assertEqual(res["log"]["rows"][0]["labels"], ["wanda_apron", "wanda_uniform"])
        self.assertIsNone(res["summary"]["garments"])

    def test_6_read_only_snapshot_does_not_mutate_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "life1.db")
            rw = build_db(path)
            wear(rw, "wanda", ["wanda_uniform"], "home", NOW - 100, 1, current=True)
            rw.commit()
            rw.close()
            mtime_before = os.stat(path).st_mtime_ns
            ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            ro.row_factory = sqlite3.Row
            ro.execute("PRAGMA query_only=1")
            dv_before = ro.execute("PRAGMA data_version").fetchone()[0]
            ro.execute("BEGIN")
            res = wardrobeview.view(ro, "wanda", NOW)
            self.assertEqual(res["summary"]["garments"], 6)
            dv_after = ro.execute("PRAGMA data_version").fetchone()[0]
            ro.close()
            self.assertEqual(os.stat(path).st_mtime_ns, mtime_before)
            self.assertEqual(dv_after, dv_before)

    def test_8_cursor_pagination_same_second_and_null_event_id(self):
        db = build_db()
        same_ts = NOW - 500
        wear(db, "wanda", ["wanda_uniform"], "home", same_ts, None)
        wear(db, "wanda", ["wanda_bought_knit"], "out", same_ts, 21)
        wear(db, "wanda", ["wanda_apron"], "home", same_ts, 22)
        wear(db, "wanda", ["wanda_other_ev"], "out", NOW - 100, 23, current=True)

        p1 = wardrobeview.view(db, "wanda", NOW, log_limit=2)
        self.assertEqual([r["event_id"] for r in p1["log"]["rows"]], [23, 22])
        self.assertTrue(p1["log"]["more"])
        self.assertEqual(p1["log"]["total"], 4)
        self.assertRegex(p1["log"]["rows"][0]["day"], r"^\d{4}-\d{2}-\d{2}$")

        last1 = p1["log"]["rows"][-1]
        p2 = wardrobeview.view(db, "wanda", NOW, log_before=(last1["at"], last1["event_id"] or 0), log_limit=2)
        self.assertEqual([r["event_id"] for r in p2["log"]["rows"]], [21, None])
        self.assertFalse(p2["log"]["more"])

    def test_import_worldkernel_does_not_load_wardrobeview(self):
        proc = subprocess.run(
            [sys.executable, "-c", "import sys, worldkernel; print('worldkernel.wardrobeview' in sys.modules)"],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(proc.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
