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
from test_m2_intents_adapter import fake_model, line, queue_size  # noqa: E402
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


def _promise_op(text: str, quote: str, *, for_: Any, agreed: Any, due: str = "", when: str = "vague") -> Dict[str, Any]:
    return {"op": "intent", "text": text, "kind": "promise", "for": for_, "agreed": agreed, "scope": "world",
            "when": when, "due": due, "quote": quote}


class Stage2CommitmentTests(unittest.TestCase):
    def _house(self, env: Dict[str, str], t0: float) -> HouseDaemon:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env=env,
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)
        return d

    def test_errand_verbal_claim_never_closes_until_real_evidence_arrives(self) -> None:
        """Fails if: the extraction does not create the errand (positive control), a chat claim of "bought it", an
        unrelated purchase (other item) or a half-sized purchase closes it, or the full purchase receipts do not."""
        t0 = dt.datetime(2026, 11, 22, 10, 0, tzinfo=JST).timestamp()
        d = self._house({}, t0)

        # Owner asks sora to buy 2 bottles of milk tonight; she says yes; the extraction finds the promise
        ask = "苍空今晚帮我买2盒牛奶回来好不好？"
        d.memory.observe("sora", "group", ask, channel="group", speaker="房主", speaker_role="owner",
                         reply="好呀，我今晚出去顺路买2盒牛奶回来！", now=t0)
        self.assertEqual(d.intents.list_intents(actor="sora"), [])  # the chat turn alone records nothing
        op = _promise_op("苍空买2盒牛奶", "苍空今晚帮我买2盒牛奶回来好不好", for_=["房主"], agreed=["苍空"],
                         due="2026-11-22", when="day")
        d.memory.extract(force=True, now=t0 + 5, llm_fn=fake_model([op]))
        rows = [r for r in d.intents.list_intents(actor="sora") if r["item"] == "milk"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 2)
        self.assertEqual(rows[0]["state"], "open")
        self.assertIsNotNone(rows[0]["due_from"])
        self.assertIsNotNone(rows[0]["due_to"])
        iid = rows[0]["intent_id"]

        def row() -> Dict[str, Any]:
            return [r for r in d.intents.list_intents(actor="sora") if r["intent_id"] == iid][0]

        # Mere chat saying "already bought" MUST NOT close the errand!
        d.converse("private:sora", "牛奶买好了吗？我已经买回来了吧", owner_speaker())
        d.intents.sweep(d.clock.now())
        self.assertEqual(row()["state"], "open")

        with contextlib.closing(sqlite3.connect(d.world_db)) as c:
            stream = c.execute("SELECT v FROM kernel_bind WHERE k='world'").fetchone()[0]

        def receipt(key: str, n: int, at: float, **kw: Any) -> None:
            body = {"key": key, "kind": "purchased", "actor": "sora", "item": "milk", "qty": 1, "at": at,
                    "account": "sora", "action_id": f"act{n}", "resource_id": f"res{n}", "intent_ref": int(iid),
                    "beneficiary": ["owner"]}
            body.update(kw)
            with contextlib.closing(sqlite3.connect(d.world_db)) as c:
                with c:
                    c.execute("INSERT INTO intent_evidence(key, stream, event_id, at, body) VALUES(?,?,?,?,?)",
                              (key, stream, n, at, json.dumps(body)))

        # An unrelated purchase (another item, a full 2) does not close it
        at0 = d.clock.now() + 30
        receipt("purchased:egg:1", 1, at0, item="egg", qty=2)
        d.intents.sweep(at0)
        self.assertEqual(row()["state"], "open")

        # Partial purchase (qty=1 < 2) also stays open until total >= 2
        at1 = d.clock.now() + 60
        receipt("purchased:milk:2", 2, at1)
        d.intents.sweep(at1)
        self.assertEqual(row()["state"], "open")

        # Second purchase (qty=1) satisfies total qty=2 -> closes via real evidence
        at2 = at1 + 30
        receipt("purchased:milk:3", 3, at2)
        d.intents.sweep(at2)
        done = row()
        self.assertEqual(done["state"], "completed")
        self.assertIn(done["evidence_key"], ("purchased:milk:2", "purchased:milk:3"))
        self.assertEqual(d.intents.errands("sora", at2), [])

    def test_non_owner_gate_shared_plan_chatlink_quiet_expiry_and_cancel_501(self) -> None:
        """Fails if: a member's line (or a model naming the member) creates a promise or reaches ChatLink while the
        same batch with the owner does not (positive control), a joint plan is not shared by both housemates or does
        not show "和谁" in ChatLink, a window that has not passed expires a promise (or one that has passed does
        not), expiry writes to the owner's queue, or `POST /api/intents/cancel` stops answering 501 / cancels."""
        priv_tok = "priv-commit-tok-01"
        t0 = dt.datetime(2026, 11, 22, 11, 0, tzinfo=JST).timestamp()
        d = self._house({"AI_HOUSE_PRIVATE_TOKEN": priv_tok}, t0)

        # 1) A member cannot make a commitment for the owner's house. The model is given the member's request and
        # sora's yes and proposes the promise twice (for the owner, and for the member); both are refused.
        ask = "苍空明天帮我买牛奶"
        d.memory.observe("sora", "group", ask, channel="qq-group:9001", speaker="群友甲", speaker_role="member",
                         speaker_id="8005", reply="好呀，我明天买牛奶", now=t0)
        bad = [
            _promise_op("苍空明天买牛奶", ask, for_=["房主"], agreed=["苍空"]),
            _promise_op("苍空明天买牛奶", ask, for_=["群友甲"], agreed=["苍空"]),
        ]
        d.memory.extract(force=True, now=t0 + 5, llm_fn=fake_model(bad))
        self.assertEqual(d.intents.list_intents(), [])
        self.assertEqual(queue_size(d.world_db), 0)  # and the member's words never reach the world as the owner's
        # positive control: the same request from the owner, same model answer, becomes a promise and is queued
        d.memory.observe("sora", "group", ask, channel="group", speaker="房主", speaker_role="owner",
                         reply="好呀，我明天买牛奶", now=t0 + 10)
        d.memory.extract(force=True, now=t0 + 15, llm_fn=fake_model(bad[:1]))
        self.assertEqual([(r["actor"], r["state"]) for r in d.intents.list_intents()], [("sora", "open")])
        self.assertEqual(queue_size(d.world_db), 1)

        # 2) (the allergy memo that used to be recorded here is a memory now: D3, see docs)

        # 3) A joint plan of two housemates is one shared promise; ChatLink gets it with "和谁" (module-level lines)
        ids = d.intents.intents_module.ID
        sq, rq = ids.ACTOR_QQ["sora"], ids.ACTOR_QQ["rin"]
        plan = "明天下午我们一起去晴空都逛逛吧"
        joint = {"op": "intent", "text": "苍空和凛音明天一起去晴空都逛逛", "kind": "joint", "with": ["凛音"],
                 "agreed": ["苍空", "凛音"], "due": "2026-11-23 14:00", "when": "exact", "quote": plan}
        lines = [line("苍空(你)", sq, plan, 1), line("凛音", rq, "好呀，一起去晴空都吧", 2)]
        now = t0 + 20
        got = d.intents.intents_module.apply_chat("sora", "group:1", False, [joint], lines, now=now)
        self.assertEqual(got["intents"], 1)
        lines_rin = [line("苍空", sq, plan, 1), line("凛音(你)", rq, "好呀，一起去晴空都吧", 2)]
        joint_rin = dict(joint, **{"with": ["苍空"]})
        got = d.intents.intents_module.apply_chat("rin", "group:1", False, [joint_rin], lines_rin, now=now + 1)
        self.assertEqual((got["intents"], got["merged"]), (0, 1))  # one shared row, not a copy per housemate
        sora_out = [r for r in d.intents.list_intents(actor="sora") if "晴空都" in r["summary"]][0]
        rin_out = [r for r in d.intents.list_intents(actor="rin") if "晴空都" in r["summary"]][0]
        self.assertEqual(sora_out["intent_id"], rin_out["intent_id"])
        self.assertEqual(set(sora_out["party"]), {"sora", "rin"})
        self.assertEqual(set(sora_out["agreed"]), {"sora", "rin"})
        # the milk promise of section 1 is not part of it
        self.assertEqual(len([r for r in d.intents.list_intents(actor="sora")]), 2)

        link = ChatLink(d.authority.life, d.world_db)
        link.step()
        ctx_sora = link.context("sora", "decide")
        self.assertTrue(ctx_sora.get("qq_chat"))
        plan_entry = next(
            it for it in ctx_sora["qq_chat"]
            if it.get("你的打算") and any("晴空都" in p.get("text", "") for p in it["你的打算"])
        )
        self.assertIn("rin", plan_entry["你的打算"][0]["和谁"])

        # 4) Cancel via HTTP is not supported any more (501) and cancels nothing
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
        self.assertEqual((st_c, res_c["ok"], res_c["code"]), (501, False, "not_supported"))
        st_l, res_l = _http_json(f"{base}/api/intents?limit=10", token=priv_tok)
        self.assertEqual(st_l, 200)
        self.assertEqual({r["state"] for r in res_l["intents"]}, {"open"})

        # 5) Expiry is by time and quiet: still open inside the window, expired long after it, nothing queued
        queued_before = queue_size(d.world_db)
        d.intents.sweep(sora_out["due_to"] + 60)
        self.assertEqual({r["state"] for r in d.intents.list_intents(actor="rin") if "晴空都" in r["summary"]}, {"open"})
        late = sora_out["due_to"] + 13 * 3600
        d.intents.sweep(late)
        after = [r for r in d.intents.list_intents(actor="rin") if r["intent_id"] == rin_out["intent_id"]][0]
        self.assertEqual(after["state"], "expired")
        self.assertEqual(queue_size(d.world_db), queued_before)


if __name__ == "__main__":
    unittest.main()
