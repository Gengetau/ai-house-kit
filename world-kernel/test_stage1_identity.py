"""Regression tests for Stage 1.5: channel-agnostic IdentityStore, cross-channel mapping & bot filtering."""
from __future__ import annotations

import contextlib
import json
import sqlite3
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

from ai_house.builtin_plugins.onebot_ws import OneBotChannel, OneBotConfig
from ai_house.chat_identity import BOT, OWNER, bot_speaker, owner_speaker
from ai_house.daemon import HouseDaemon, make_http_server
from ai_house.identity import IdentityStore


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


OWNER_HEAD = "|1] (message_id="  # rendered line header of the owner (number 1)


def _seen_lines(rt: Path) -> list:
    with contextlib.closing(sqlite3.connect(rt / "memory-common.db")) as c:
        return [r[0] for r in c.execute("SELECT line FROM seen ORDER BY id").fetchall()]


class Stage1IdentityTests(unittest.TestCase):
    def test_identity_store_resolves_cross_channel_owner_and_auto_visitors(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = IdentityStore(tmp.name, roster=("sora", "rin"), names={"sora": "苍空", "rin": "凛"})
        self.addCleanup(store.close)

        # Default web owner & residents seeded automatically
        web_owner = store.resolve_speaker("web", "owner", fallback_name="房主")
        self.assertTrue(web_owner.is_owner)
        self.assertEqual(web_owner.identity_id, "user:owner")

        ids = {row["identity_id"]: row for row in store.list_identities()}
        self.assertIn("resident:sora", ids)
        self.assertEqual(ids["resident:sora"]["kind"], "resident")

        # Bind OneBot account to primary owner -> same internal identity as web owner
        store.bind_external("onebot", "8001", "user:owner")
        qq_owner = store.resolve_speaker("onebot", "8001", fallback_name="老板号")
        self.assertEqual(qq_owner.identity_id, web_owner.identity_id)
        self.assertTrue(qq_owner.is_owner)
        self.assertTrue(store.is_owner_external("onebot", "8001"))

        targets = store.external_targets_for("user:owner", platform="onebot")
        self.assertEqual([t["external_id"] for t in targets], ["8001"])

        # Unbound visitor gets stable visitor identity across turns
        vis1 = store.resolve_speaker("onebot", "8002", fallback_name="阿晴")
        vis2 = store.resolve_speaker("onebot", "8002", fallback_name="阿晴")
        self.assertEqual(vis1.identity_id, "visitor:onebot:8002")
        self.assertEqual(vis1.identity_id, vis2.identity_id)
        self.assertFalse(vis1.is_owner)
        self.assertFalse(vis1.is_bot)

        # Bot identity resolves as role='bot'
        store.upsert_identity("bot:helper", kind="bot", display_name="值班助手")
        store.bind_external("onebot", "8003", "bot:helper")
        bot_spk = store.resolve_speaker("onebot", "8003", fallback_name="值班助手")
        self.assertEqual(bot_spk.role, BOT)
        self.assertTrue(bot_spk.is_bot)
        self.assertEqual(bot_spk.identity_id, "bot:helper")

    def test_memory_seen_speaker_identity_daemon_api_and_onebot_bot_filter(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        rt.mkdir(parents=True, exist_ok=True)

        owner_tok = "tok-owner-identity-alpha"
        d = HouseDaemon(
            rt,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={"AI_HOUSE_OWNER_TOKEN": owner_tok},
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        srv = make_http_server(d, host="127.0.0.1", port=0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 2.0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{port}"

        # HTTP API: list, upsert, bind, unbind
        st, doc = _http_json(f"{base}/api/identities", token=owner_tok)
        self.assertEqual(st, 200)
        self.assertTrue(any(row["identity_id"] == "user:owner" for row in doc["identities"]))

        st_u, _ = _http_json(
            f"{base}/api/identities",
            token=owner_tok,
            body={"op": "upsert", "identity_id": "visitor:friend_a", "kind": "visitor", "display_name": "小林"},
        )
        self.assertEqual(st_u, 200)
        st_b, _ = _http_json(
            f"{base}/api/identities",
            token=owner_tok,
            body={"op": "bind", "platform": "onebot", "external_id": "8010", "identity_id": "user:owner"},
        )
        self.assertEqual(st_b, 200)
        st_bot, _ = _http_json(
            f"{base}/api/identities",
            token=owner_tok,
            body={"op": "upsert", "identity_id": "bot:noise", "kind": "bot", "display_name": "报时器"},
        )
        self.assertEqual(st_bot, 200)
        st_bb, _ = _http_json(
            f"{base}/api/identities",
            token=owner_tok,
            body={"op": "bind", "platform": "onebot", "external_id": "8019", "identity_id": "bot:noise"},
        )
        self.assertEqual(st_bb, 200)

        # Web owner chat is recorded in `seen` as a line headed with the owner's number (1)
        # (changed: the new store has no speaker_id column; the speaker is part of the rendered line)
        d.converse("private:sora", "今天我做了咖喱饭", owner_speaker())
        seen_rows = _seen_lines(rt)
        self.assertTrue(any(OWNER_HEAD in ln and "今天我做了咖喱饭" in ln for ln in seen_rows), seen_rows)

        # Direct converse with bot speaker is ignored and does not enter seen or intents
        res_bot = d.converse("group", "请帮我买鸡蛋", bot_speaker("报时器", "8019", "bot:noise"))
        self.assertEqual(res_bot["ignored"], "bot_speaker")
        self.assertEqual(res_bot["replies"], [])

        # OneBotChannel attached to daemon honors cross-channel owner binding & skips bot speakers
        ch = OneBotChannel(
            OneBotConfig(url="ws://127.0.0.1:1", owners=frozenset({"8001"}), groups=frozenset({"9001"}), actor="sora")
        )
        ch._host = d  # test handle_event integration without background WS threads
        # Mapped owner QQ '8010' (not in cfg.owners!) can DM cleanly because bound to 'user:owner'
        outcome_dm = ch.handle_event({
            "post_type": "message",
            "message_type": "private",
            "user_id": "8010",
            "self_id": "7001",
            "message": "记得收衣服",
            "sender": {"nickname": "手机端老板"},
        })
        self.assertNotIn("ignored", outcome_dm)
        # the bound QQ account is recorded as the owner (number 1), not as a stranger
        latest = [ln for ln in _seen_lines(rt) if "记得收衣服" in ln]
        self.assertEqual(len(latest), 1, latest)
        self.assertIn(OWNER_HEAD, latest[0])

        # Bound bot account on OneBot group is ignored silently with bot_speaker
        before_cnt = len(_seen_lines(rt))
        self.assertGreaterEqual(before_cnt, 2)  # positive control: real lines were recorded above
        outcome_bot = ch.handle_event({
            "post_type": "message",
            "message_type": "group",
            "group_id": "9001",
            "user_id": "8019",
            "self_id": "7001",
            "message": "[CQ:at,qq=7001] 整点报时",
            "sender": {"nickname": "其他Bot"},
        })
        self.assertEqual(outcome_bot, "ignored:bot_speaker")
        after = _seen_lines(rt)
        self.assertEqual(len(after), before_cnt)
        self.assertFalse(any("整点报时" in ln for ln in after))

        # Unbind works via HTTP API
        st_ub, res_ub = _http_json(
            f"{base}/api/identities",
            token=owner_tok,
            body={"op": "unbind", "platform": "onebot", "external_id": "8010"},
        )
        self.assertEqual(st_ub, 200)
        self.assertTrue(res_ub["removed"])
        self.assertFalse(d.identity_store.is_owner_external("onebot", "8010"))


if __name__ == "__main__":
    unittest.main()
