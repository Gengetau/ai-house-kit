"""Phase 1 & Phase 2 tests for the public 未分类 shelf, autonomous choice, and render outbox/relayer."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from worldkernel import life_model as LM  # noqa: E402
from worldkernel import vocab_life1 as V  # noqa: E402
from worldkernel import wardrobe as W  # noqa: E402
from worldkernel import wardrobeview  # noqa: E402
from worldkernel.cognition import Cognition  # noqa: E402
from worldkernel.kernel import Kernel  # noqa: E402
from worldkernel.life import Life, ROSTER  # noqa: E402
from worldkernel.store import Store, VirtualClock  # noqa: E402
from worldkernel.wardrobe_render import (  # noqa: E402
    WardrobeRenderLedger, _acquire_lock_nb, _release_lock, sha256_bytes, verify_blob,
)

BASE_TS = 1793671200.0  # 2026-11-03 10:00 JST
PNG_A = b"\x89PNG\r\n\x1a\n" + b"PORTRAIT_V1_" * 8
PNG_A_V2 = b"\x89PNG\r\n\x1a\n" + b"PORTRAIT_V2_" * 8
PNG_ASSET_1 = b"\x89PNG\r\n\x1a\n" + b"DRESS_ASSET_" * 8
PNG_ASSET_2 = b"\x89PNG\r\n\x1a\n" + b"COAT_ASSET__" * 8


def make_shelf(asset_root):
    with open(os.path.join(asset_root, "dress.png"), "wb") as f:
        f.write(PNG_ASSET_1)
    with open(os.path.join(asset_root, "bundle.png"), "wb") as f:
        f.write(PNG_ASSET_2)
    return {
        "shelf": "未分类",
        "assets": {
            "shelf_daily_dress": {
                "kind": "onepiece_set",
                "shelf": "未分类",
                "status": "active",
                "owners": ["*"],
                "label": "奶油针织连衣裙套装",
                "image_path": "dress.png",
                "grants": [
                    {
                        "id": "cream_knit_dress",
                        "label": "奶油针织连衣裙",
                        "category": "dress",
                        "color": "cream",
                        "material": "knit",
                        "temp_c": [10, 22],
                        "occasions": ["home", "out", "formal"],
                        "like": 5,
                    }
                ],
            },
            "shelf_autumn_bundle": {
                "kind": "bundle",
                "shelf": "未分类",
                "status": "active",
                "owners": ["elsa", "alma"],
                "label": "秋日风衣组合",
                "image_path": "bundle.png",
                "grants": [
                    {
                        "id": "autumn_shirt",
                        "label": "白衬衫",
                        "category": "top",
                        "color": "white",
                        "material": "cotton",
                        "temp_c": [10, 24],
                        "occasions": ["out", "formal"],
                        "like": 4,
                    },
                    {
                        "id": "autumn_skirt",
                        "label": "格纹半身裙",
                        "category": "bottom",
                        "color": "brown",
                        "material": "wool",
                        "temp_c": [8, 22],
                        "occasions": ["out", "formal"],
                        "like": 4,
                    },
                    {
                        "id": "autumn_trench",
                        "label": "卡其风衣",
                        "category": "outer",
                        "color": "khaki",
                        "material": "gabardine",
                        "temp_c": [5, 18],
                        "occasions": ["out", "formal"],
                        "like": 5,
                    },
                    {
                        "id": "autumn_boots",
                        "label": "棕色短靴",
                        "category": "shoes",
                        "color": "brown",
                        "material": "leather",
                        "temp_c": [0, 24],
                        "occasions": ["out", "formal"],
                        "like": 4,
                    },
                ],
            },
            "shelf_draft_hidden": {
                "kind": "single",
                "shelf": "未分类",
                "status": "draft",
                "owners": ["*"],
                "label": "未上架草稿",
                "grants": [
                    {
                        "id": "draft_cap",
                        "label": "草稿帽子",
                        "category": "accessory",
                        "temp_c": [0, 30],
                        "occasions": ["out"],
                        "like": 3,
                    }
                ],
            },
        },
    }


def write_profile(profiles_dir, actor, *, with_portrait=True, look="brown hair green eyes anime girl"):
    pdir = os.path.join(profiles_dir, actor)
    os.makedirs(pdir, exist_ok=True)
    if with_portrait:
        with open(os.path.join(pdir, "portrait.png"), "wb") as f:
            f.write(PNG_A)
    if look is not None:
        with open(os.path.join(pdir, "display.json"), "w", encoding="utf-8") as f:
            json.dump({"look": look, "focus": "face"}, f)


def seed_world(store):
    with store.tx():
        store.k.seed({
            "actors": {a: {"location": "living"} for a in ROSTER},
            "resources": [],
        })


class ShelfAndWardrobeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.asset_root = os.path.join(self.tmp.name, "assets")
        self.profiles_dir = os.path.join(self.tmp.name, "profiles")
        self.blob_root = os.path.join(self.tmp.name, "blobs")
        os.makedirs(self.asset_root, exist_ok=True)
        os.makedirs(self.profiles_dir, exist_ok=True)
        self.shelf_doc = make_shelf(self.asset_root)
        write_profile(self.profiles_dir, "elsa", look="amber hair gentle anime girl")
        write_profile(self.profiles_dir, "yuki", look="silver hair purple eyes anime girl")
        write_profile(self.profiles_dir, "alma", with_portrait=True, look=None)

        self.clock = VirtualClock(BASE_TS)
        self.life_db = os.path.join(self.tmp.name, "life1.db")
        self.store = Store(self.life_db, synthetic=True, clock=self.clock)
        self.store.acquire()
        self.kernel = Kernel(self.store, ROSTER, vocab=V)
        self.store.k = self.kernel
        seed_world(self.store)
        self.wardrobe = W.Wardrobe(
            self.kernel,
            V.PEOPLE,
            shelf_assets=self.shelf_doc,
            asset_root=self.asset_root,
            profiles_dir=self.profiles_dir,
            blob_root=self.blob_root,
            render_actors=("elsa", "alma"),
        )

    def tearDown(self):
        try:
            if self.store.epoch is not None:
                self.store.release()
        finally:
            self.store.close()

    def test_validate_garment_assets_accepts_bundles_without_count_cap(self):
        self.assertEqual(V.validate_garment_assets(self.shelf_doc), [])

    def test_pick_shelf_single_onepiece_and_bundle_and_disjoint_owners(self):
        elsa_picks = {p["asset_id"]: p for p in self.wardrobe.shelf_picks("elsa")}
        yuki_picks = {p["asset_id"]: p for p in self.wardrobe.shelf_picks("yuki")}
        self.assertIn("shelf_daily_dress", elsa_picks)
        self.assertIn("shelf_autumn_bundle", elsa_picks)
        self.assertNotIn("shelf_draft_hidden", elsa_picks)
        self.assertNotIn("shelf_autumn_bundle", yuki_picks)

        r1 = self.kernel.submit({
            "idempotency_key": "p1", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_daily_dress"},
        })
        r2 = self.kernel.submit({
            "idempotency_key": "p2", "principal": "channel:yuki", "actor_id": "yuki",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_daily_dress"},
        })
        self.assertEqual(r1["status"], "succeeded")
        self.assertEqual(r2["status"], "succeeded")
        self.assertNotEqual(r1["garment_ids"], r2["garment_ids"])

        view_c = wardrobeview.view(self.store.db, "elsa", self.clock.now())
        shelf_g = [g for g in view_c["garments"] if g["id"] == r1["garment_ids"][0]][0]
        self.assertEqual(shelf_g["source"]["type"], "shelf")

        r_dup = self.kernel.submit({
            "idempotency_key": "p3", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_daily_dress"},
        })
        self.assertEqual(r_dup["status"], "failed")

        r_draft = self.kernel.submit({
            "idempotency_key": "p4", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_draft_hidden"},
        })
        self.assertEqual(r_draft["status"], "failed")

        r_b = self.kernel.submit({
            "idempotency_key": "p5", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_autumn_bundle"},
        })
        self.assertEqual(r_b["status"], "succeeded")
        self.assertEqual(len(r_b["garment_ids"]), 4)

    def test_13_column_legacy_garments_db_upgrade(self):
        clock = VirtualClock(BASE_TS)
        s = Store(":memory:", synthetic=True, clock=clock)
        self.addCleanup(s.close)
        s.acquire()
        with s.tx() as db:
            db.execute(
                "CREATE TABLE garments (garment_id TEXT PRIMARY KEY, actor TEXT NOT NULL, label TEXT, category TEXT,"
                " color TEXT, material TEXT, temp_lo REAL, temp_hi REAL, occasions TEXT, liking INTEGER, source TEXT,"
                " state TEXT NOT NULL DEFAULT 'clean', created_at REAL)"
            )
            db.execute(
                "INSERT INTO garments VALUES('old_1','elsa','旧裙子','dress','blue','cotton',15,25,'[\"home\"]',3,'initial_wardrobe','clean',100)"
            )
        k = Kernel(s, ROSTER, vocab=V)
        w = W.Wardrobe(k, {"elsa": {"wardrobe": []}}, shelf_assets=self.shelf_doc)
        tables = {r[0] for r in s.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("garment_asset_links", tables)
        self.assertIn("wardrobe_render_outbox", tables)
        self.assertEqual(len(w.owned("elsa")), 1)

    def test_model_only_and_per_actor_rollout_and_blob_freeze_and_debounce(self):
        ledger = WardrobeRenderLedger(":memory:")
        self.addCleanup(ledger.close)

        self.wardrobe.dress("elsa", "home", 18.0)
        res1 = ledger.relay_outbox(self.store.db, now=self.clock.now())
        self.assertEqual(res1["queued"], 0)
        self.assertEqual(res1["skipped"], 1)

        p_x = self.kernel.submit({
            "idempotency_key": "px1", "principal": "channel:yuki", "actor_id": "yuki",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_daily_dress"},
        })
        self.kernel.submit({
            "idempotency_key": "cx1", "principal": "channel:yuki", "actor_id": "yuki",
            "origin": "model", "action_type": "change_clothes",
            "args": {"items": p_x["garment_ids"], "occasion": "out"},
        })
        row_x = self.store.db.execute(
            "SELECT render_eligible, skip_reason FROM wardrobe_render_outbox WHERE actor='yuki'"
        ).fetchone()
        self.assertEqual((row_x["render_eligible"], row_x["skip_reason"]), (0, "actor_not_enabled"))

        p_a = self.kernel.submit({
            "idempotency_key": "pa1", "principal": "channel:alma", "actor_id": "alma",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_daily_dress"},
        })
        c_a = self.kernel.submit({
            "idempotency_key": "ca1", "principal": "channel:alma", "actor_id": "alma",
            "origin": "model", "action_type": "change_clothes",
            "args": {"items": p_a["garment_ids"], "occasion": "out"},
        })
        self.assertEqual(c_a["status"], "succeeded")
        row_a = self.store.db.execute(
            "SELECT render_eligible, skip_reason FROM wardrobe_render_outbox WHERE actor='alma'"
        ).fetchone()
        self.assertEqual((row_a["render_eligible"], row_a["skip_reason"]), (0, "missing_display_look"))

        p_c1 = self.kernel.submit({
            "idempotency_key": "pc1", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_daily_dress"},
        })
        p_c2 = self.kernel.submit({
            "idempotency_key": "pc2", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_autumn_bundle"},
        })
        c_c1 = self.kernel.submit({
            "idempotency_key": "cc1", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "change_clothes",
            "args": {"items": p_c1["garment_ids"], "occasion": "home"},
        })
        self.assertEqual(c_c1["status"], "succeeded")

        with open(os.path.join(self.profiles_dir, "elsa", "portrait.png"), "wb") as f:
            f.write(PNG_A_V2)

        self.clock.advance(20)
        c_c2 = self.kernel.submit({
            "idempotency_key": "cc2", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "change_clothes",
            "args": {"items": p_c2["garment_ids"], "occasion": "formal"},
        })
        self.assertEqual(c_c2["status"], "succeeded")

        rel = ledger.relay_outbox(self.life_db, now=self.clock.now(), debounce_seconds=120.0)
        self.assertEqual(rel["queued"], 2)
        self.assertEqual(rel["superseded"], 1)
        jobs = [dict(r) for r in ledger.db.execute("SELECT * FROM wardrobe_render_jobs ORDER BY outbox_id")]
        self.assertEqual([j["status"] for j in jobs], ["superseded", "queued"])

        rel_again = ledger.relay_outbox(self.life_db, now=self.clock.now(), debounce_seconds=120.0)
        self.assertEqual(rel_again["queued"], 0)

        snap0 = json.loads(jobs[0]["snapshot_json"])
        snap1 = json.loads(jobs[1]["snapshot_json"])
        self.assertEqual(snap0["portrait"]["sha256"], sha256_bytes(PNG_A))
        self.assertEqual(snap1["portrait"]["sha256"], sha256_bytes(PNG_A_V2))
        verify_blob(self.blob_root, snap0["portrait"]["blob_ref"], snap0["portrait"]["sha256"])

    def test_no_daily_count_limit_when_daily_cap_none(self):
        ledger = WardrobeRenderLedger(":memory:")
        self.addCleanup(ledger.close)
        p = self.kernel.submit({
            "idempotency_key": "p_unlim", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_daily_dress"},
        })
        for idx in range(10):
            self.clock.advance(200)
            self.kernel.submit({
                "idempotency_key": f"c_unlim_{idx}", "principal": "channel:elsa", "actor_id": "elsa",
                "origin": "model", "action_type": "change_clothes",
                "args": {"items": p["garment_ids"], "occasion": "out" if idx % 2 == 0 else "formal"},
            })
            rel = ledger.relay_outbox(self.life_db, now=self.clock.now(), debounce_seconds=10.0)
            self.assertEqual(rel["queued"], 1)
            job = ledger.claim_next_job(now=self.clock.now() + 20, blob_root=self.blob_root)
            self.assertIsNotNone(job)
            att = ledger.begin_submit(job["job_id"], job["claim_token"], now=self.clock.now() + 20, daily_cap=None)
            ledger.finish_submit_failed(job["job_id"], att, "test", "ok", now=self.clock.now() + 20, retryable=False)

    def test_live_model_autonomous_pick_then_change_end_to_end(self):
        clock = VirtualClock(BASE_TS)
        s = Store(":memory:", synthetic=True, clock=clock)
        self.addCleanup(s.close)
        s.acquire()
        shelf = make_shelf(self.asset_root)

        def fake_chat(system, user):
            data = json.loads(user.split("你现在的情况：\n", 1)[1].split("\n让你想一想的原因", 1)[0])
            life = data["life"]
            picks = {p["asset_id"]: p for p in life["shelf_picks"]}
            gids = [g["id"] for g in picks["shelf_autumn_bundle"]["grants"]]
            self.assertLessEqual(len(life["wardrobe_choices"]), 24)
            return json.dumps({
                "steps": [
                    {"action_type": "pick_shelf_garment", "args": {"asset_id": "shelf_autumn_bundle"}},
                    {"action_type": "change_clothes", "args": {"items": gids, "occasion": "formal"}},
                ]
            }), 50

        old_env = os.environ.get("WARDROBE_SHELF_ASSETS")
        shelf_file = os.path.join(self.tmp.name, "shelf.json")
        with open(shelf_file, "w", encoding="utf-8") as f:
            json.dump(shelf, f)
        os.environ["WARDROBE_SHELF_ASSETS"] = shelf_file
        try:
            model = LM.LiveLifeModel(fake_chat, {"elsa": "艾莎"})
            life = Life(s, model=model)
            life._kind["elsa"] = "free_time"
            life.cog.begin("elsa", "free_time")
            r1 = life.cog.run_due()
            self.assertEqual(r1[0]["status"], "succeeded")
            clock.advance(1)
            r2 = life.cog.advance_plans()
            self.assertEqual(r2[0]["status"], "succeeded")
            cur = life.wardrobe.current("elsa")
            self.assertEqual(cur["occasion"], "formal")
            self.assertEqual(len(cur["items"]), 4)
            choices = life.wardrobe.wardrobe_choices("elsa", max_items=24)
            self.assertLessEqual(len(choices), 24)
            self.assertTrue(set(cur["items"]) <= {c["id"] for c in choices})
        finally:
            if old_env is None:
                os.environ.pop("WARDROBE_SHELF_ASSETS", None)
            else:
                os.environ["WARDROBE_SHELF_ASSETS"] = old_env

    def test_lost_submitting_job_recovers_to_unknown_pending_reconcile_without_repay(self):
        p = self.kernel.submit({
            "idempotency_key": "p_lost", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_daily_dress"},
        })
        ch = self.kernel.submit({
            "idempotency_key": "c_lost", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "change_clothes",
            "args": {"items": p["garment_ids"], "occasion": "out"},
        })
        self.assertEqual(ch["status"], "succeeded")

        ledger_path = os.path.join(self.tmp.name, "lost_ledger.db")
        ledger = WardrobeRenderLedger(ledger_path)
        now = self.clock.now() + 200
        ledger.relay_outbox(self.store.db, now=now)
        job = ledger.claim_next_job(now=now, blob_root=self.blob_root)
        self.assertIsNotNone(job)
        ledger.begin_submit(job["job_id"], job["claim_token"], now=now)
        ledger.close()

        ledger = WardrobeRenderLedger(ledger_path)
        self.addCleanup(ledger.close)
        recovered = ledger.claim_next_job(now=now + 3600, blob_root=self.blob_root)
        self.assertIsNone(recovered)
        jrow = ledger.db.execute(
            "SELECT status, claim_token FROM wardrobe_render_jobs WHERE job_id=?", (job["job_id"],)
        ).fetchone()
        self.assertEqual((jrow["status"], jrow["claim_token"]), ("unknown_pending_reconcile", None))
        rrow = ledger.db.execute(
            "SELECT status FROM wardrobe_render_requests WHERE job_id=?", (job["job_id"],)
        ).fetchone()
        self.assertEqual(rrow["status"], "unknown_pending_reconcile")
        brow = ledger.db.execute(
            "SELECT reserved_count, uncertain_count FROM wardrobe_render_budget WHERE actor='elsa'"
        ).fetchone()
        self.assertEqual((brow["reserved_count"], brow["uncertain_count"]), (0, 1))

        ledger.reconcile_uncertain(job["job_id"], "failed", now=now + 3600)
        jrow2 = ledger.db.execute(
            "SELECT status FROM wardrobe_render_jobs WHERE job_id=?", (job["job_id"],)
        ).fetchone()
        self.assertEqual(jrow2["status"], "failed")

    def test_submitting_job_with_live_executor_lock_is_not_recovered(self):
        p = self.kernel.submit({
            "idempotency_key": "p_lock", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "pick_shelf_garment",
            "args": {"asset_id": "shelf_daily_dress"},
        })
        self.kernel.submit({
            "idempotency_key": "c_lock", "principal": "channel:elsa", "actor_id": "elsa",
            "origin": "model", "action_type": "change_clothes",
            "args": {"items": p["garment_ids"], "occasion": "out"},
        })
        ledger_path = os.path.join(self.tmp.name, "lock_ledger.db")
        ledger = WardrobeRenderLedger(ledger_path)
        self.addCleanup(ledger.close)
        now = self.clock.now() + 200
        ledger.relay_outbox(self.store.db, now=now)
        job = ledger.claim_next_job(now=now, blob_root=self.blob_root)
        lock_path = os.path.join(self.tmp.name, "exec.lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        _acquire_lock_nb(fd)
        try:
            ledger.begin_submit(job["job_id"], job["claim_token"], now=now, executor_lock_path=lock_path)
            # Even after 1 hour, live lock prevents recovery.
            self.assertEqual(ledger.recover_stale_submitting(now=now + 3600), [])
            status = ledger.db.execute(
                "SELECT status FROM wardrobe_render_jobs WHERE job_id=?", (job["job_id"],)
            ).fetchone()[0]
            self.assertEqual(status, "submitting")
        finally:
            _release_lock(fd)
            os.close(fd)
        # Once lock is released (dead executor), recovery happens immediately without waiting for lease expiry.
        rec = ledger.recover_stale_submitting(now=now + 1)
        self.assertEqual(rec, [job["job_id"]])
        status2 = ledger.db.execute(
            "SELECT status FROM wardrobe_render_jobs WHERE job_id=?", (job["job_id"],)
        ).fetchone()[0]
        self.assertEqual(status2, "unknown_pending_reconcile")


if __name__ == "__main__":
    unittest.main()
