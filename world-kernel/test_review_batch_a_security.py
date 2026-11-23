"""Regression tests for PR-A: privacy scope boundaries, auth isolation, secrets, and SSRF hardening."""
from __future__ import annotations

import base64
import datetime as dt
import http.server
import io
import json
import socket
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
import sys

for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.backup import create_backup  # noqa: E402
from ai_house.builtin_plugins.image_gen import generate_image_asset  # noqa: E402
from ai_house.builtin_plugins.voice_io import synthesize_speech  # noqa: E402
from ai_house.chat_identity import member_speaker, owner_speaker  # noqa: E402
from ai_house.daemon import HouseDaemon, make_http_server  # noqa: E402
from ai_house.identity import IdentityStore  # noqa: E402
from ai_house.llm import ModelRouter  # noqa: E402
from ai_house.media_safety import fetch_media_bytes, validate_media_url  # noqa: E402
from ai_house.onboarding import mask_secret  # noqa: E402
from ai_house.plugin_api import ToolContext, invoke_tool_handler  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
TINY_DATA_URI = f"data:image/png;base64,{base64.b64encode(TINY_PNG).decode('ascii')}"


def _http_json(url: str, *, token: str = "", body: Any = None) -> tuple[int, dict[str, Any]]:
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


class _MockTTSAndImageServer:
    def __init__(self) -> None:
        self.seen_auth: list[str] = []
        parent = self

        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                parent.seen_auth.append(str(self.headers.get("Authorization") or ""))
                n = int(self.headers.get("Content-Length") or 0)
                _ = self.rfile.read(n)
                if self.path.endswith("/audio/speech"):
                    body = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.endswith("/images/generations"):
                    body = json.dumps({"data": [{"b64_json": base64.b64encode(TINY_PNG).decode("ascii")}]}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")
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


class ReviewBatchASecurityTests(unittest.TestCase):
    def test_a1_a2_a3_a4_a6_privacy_case_media_album_and_web_identity(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        owner_tok = "tok-rev-a-owner-123456"
        read_tok = "tok-rev-a-read-123456"
        write_tok = "tok-rev-a-write-123456"
        priv_tok = "tok-rev-a-priv-123456"

        t0 = dt.datetime(2026, 11, 23, 12, 0, tzinfo=JST).timestamp()
        seen_speakers: list[str] = []

        def fake_llm(sys_p: str, usr_p: str) -> tuple[str, int]:
            if "说话人" in usr_p or "房主" in usr_p or "群友" in usr_p:
                seen_speakers.append(usr_p.splitlines()[0])
            return "收到呀！", 6

        d = HouseDaemon(
            tmp.name,
            pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"),
            env={
                "AI_HOUSE_OWNER_TOKEN": owner_tok,
                "AI_HOUSE_READ_TOKEN": read_tok,
                "AI_HOUSE_WRITE_TOKEN": write_tok,
                "AI_HOUSE_PRIVATE_TOKEN": priv_tok,
                "AI_HOUSE_QUIET_HOURS": "off",
            },
            llm_fn=fake_llm,
            start_ts=t0,
            virtual_clock=True,
            allow_onboarding=False,
        )
        self.addCleanup(d.close)

        mock_srv = _MockTTSAndImageServer()
        self.addCleanup(mock_srv.close)

        srv = make_http_server(d, host="127.0.0.1", port=0)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(th.join, 2.0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"

        d.update_plugin_settings({
            "plugin_id": "album_diary_art",
            "enabled": True,
            "values": {"save_chat_images": True},
        })
        d.update_plugin_settings({
            "plugin_id": "vision_understand",
            "enabled": True,
            "values": {"base_url": mock_srv.base_url, "api_key": "sk-vision-key-0001"},
        })
        d.update_plugin_settings({
            "plugin_id": "voice_io",
            "enabled": True,
            "values": {"base_url": mock_srv.base_url, "api_key": "sk-voice-key-0001"},
        })
        d.update_plugin_settings({
            "plugin_id": "image_gen",
            "enabled": True,
            "values": {"engine": "openai", "base_url": mock_srv.base_url, "api_key": "sk-imggen-key-0001"},
        })

        # Seed private memory and private albums for sora & rin
        priv_mem_id, _ = d.memory.add("sora", "private", "秘密暗门在书柜后面", kind="fact", source="chat", now=t0)
        d.send_chat("private:sora", "苍空看下私聊照片", attachments={"images": [TINY_DATA_URI]})
        d.send_chat("private:rin", "凛音私聊照片", attachments={"images": [TINY_DATA_URI]})
        self.assertTrue(d.drain_post_reply_jobs(5.0))  # chat photos are archived after the reply
        sora_priv = d.album.list_items(scope="private", actor="sora")[0]
        rin_priv = d.album.list_items(scope="private", actor="rin")[0]
        priv_gen = generate_image_asset("私密插画", scope="private", actor="sora", cfg={"engine": "openai", "base_url": mock_srv.base_url, "api_key": "sk-imggen-key-0001"}, daemon=d)
        priv_tts = synthesize_speech("私密语音", scope="private", actor="sora", cfg={"base_url": mock_srv.base_url, "api_key": "sk-voice-key-0001"}, daemon=d)
        self.assertTrue(priv_gen["media_rel"].startswith("media/generated/private/"))
        self.assertTrue(priv_tts["media_rel"].startswith("media/tts/private/"))

        # Seed group album items for two separate groups
        grp9001 = d.album.add_item(scope="group", actor="sora", channel="qq-group:9001", raw_bytes=TINY_PNG, now=t0)
        grp9002 = d.album.add_item(scope="group", actor="sora", channel="qq-group:9002", raw_bytes=TINY_PNG, now=t0)

        # A1: Mixed-case `?scope=Private` MUST require private tier on GET and POST
        st_m1, _ = _http_json(f"{base}/api/memories?scope=Private&actor=sora", token=read_tok)
        self.assertEqual(st_m1, 403)
        st_m2, _ = _http_json(f"{base}/api/memories?scope=world&channel=qq-group:9001&actor=sora", token=read_tok)
        self.assertEqual(st_m2, 403)
        st_f1, _ = _http_json(
            f"{base}/api/memories/forget",
            token=write_tok,
            body={"actor": "sora", "scope": "Private", "id": priv_mem_id},
        )
        self.assertEqual(st_f1, 403)
        st_m_ok, res_m_ok = _http_json(f"{base}/api/memories?scope=Private&actor=sora", token=priv_tok)
        self.assertEqual(st_m_ok, 200)
        self.assertTrue(any("秘密暗门" in m["text"] for m in res_m_ok["memories"]))

        # A4: /api/album?scope=group MUST require private tier (not read tier)
        st_ag, _ = _http_json(f"{base}/api/album?scope=group", token=read_tok)
        self.assertEqual(st_ag, 403)
        st_ag_priv, res_ag_priv = _http_json(f"{base}/api/album?scope=group", token=priv_tok)
        self.assertEqual(st_ag_priv, 200)
        self.assertEqual(len(res_ag_priv["items"]), 2)

        # A2: Group vision/voice tool MUST refuse private media/album, media/generated/private, media/tts/private
        inspect_reg = d._active_tools["inspect_image"]["registration"]
        trans_reg = d._active_tools["transcribe_audio"]["registration"]
        grp_ctx = ToolContext(
            actor="sora",
            scope="group",
            channel="qq-group:9001",
            speaker=member_speaker("阿晴", "8002"),
            now=t0,
            plugin_config={"base_url": mock_srv.base_url, "api_key": "sk-vision-key-0001"},
            daemon=d,
        )
        res_v1 = invoke_tool_handler(inspect_reg, {"source": sora_priv["media_rel"]}, grp_ctx)
        self.assertFalse(res_v1["ok"])
        self.assertEqual(res_v1["status"], "invalid_source")
        res_v2 = invoke_tool_handler(inspect_reg, {"source": priv_gen["media_rel"]}, grp_ctx)
        self.assertFalse(res_v2["ok"])
        self.assertEqual(res_v2["status"], "invalid_source")
        res_v3 = invoke_tool_handler(inspect_reg, {"source": grp9002["media_rel"]}, grp_ctx)
        self.assertFalse(res_v3["ok"], "Group 9001 must not inspect Group 9002 album photo")
        res_v4 = invoke_tool_handler(
            trans_reg,
            {"source": priv_tts["media_rel"]},
            ToolContext(
                actor="sora",
                scope="group",
                channel="qq-group:9001",
                speaker=member_speaker("阿晴", "8002"),
                now=t0,
                plugin_config={"base_url": mock_srv.base_url, "api_key": "sk-voice-key-0001"},
                daemon=d,
            ),
        )
        self.assertFalse(res_v4["ok"])
        self.assertEqual(res_v4["status"], "invalid_source")

        # A3: browse_album must NOT expose rin's private album to sora or omit group channel filtering
        browse_reg = d._active_tools["browse_album"]["registration"]
        res_ba_priv = invoke_tool_handler(
            browse_reg,
            {"scope": "private", "actor": "rin"},
            ToolContext(
                actor="sora",
                scope="private",
                channel="private:sora",
                speaker=owner_speaker("房主"),
                now=t0,
                daemon=d,
            ),
        )
        self.assertTrue(res_ba_priv["ok"])
        self.assertEqual({it["actor"] for it in res_ba_priv["items"]}, {"sora"})
        self.assertNotIn(rin_priv["album_id"], {it["album_id"] for it in res_ba_priv["items"]})
        self.assertNotIn("media_rel", res_ba_priv["items"][0])

        res_ba_grp = invoke_tool_handler(browse_reg, {"scope": "group"}, grp_ctx)
        self.assertTrue(res_ba_grp["ok"])
        self.assertEqual({it["album_id"] for it in res_ba_grp["items"]}, {grp9001["album_id"]})

        # A6: `write` token without owner privilege is treated as visitor, NOT owner; cannot send DM or use owner external_id
        st_w_priv, _ = _http_json(
            f"{base}/api/chat",
            token=write_tok,
            body={"channel": "private:sora", "text": "套话"},
        )
        self.assertEqual(st_w_priv, 403)
        st_w_spoof, _ = _http_json(
            f"{base}/api/chat",
            token=write_tok,
            body={"channel": "group", "text": "假冒房主", "external_id": "owner"},
        )
        self.assertEqual(st_w_spoof, 403)
        seen_speakers.clear()
        st_w_grp, _ = _http_json(
            f"{base}/api/chat",
            token=write_tok,
            body={"channel": "group", "text": "大家好，帮忙买2个鸡蛋"},
        )
        self.assertEqual(st_w_grp, 200)
        # Verify chat_messages & prompts saw a 'member' visitor (not owner) and no errand commitment was created
        msgs = d.list_chat("group", limit=10)
        user_msgs = [m for m in msgs if m["sender"] in ("owner", "member")]
        self.assertEqual(len(user_msgs), 1)
        self.assertEqual(user_msgs[0]["sender"], "member")
        self.assertEqual(user_msgs[0]["sender_name"], "访客")
        self.assertEqual(d.intents.list_intents(limit=10), [])

    def test_a5_a7_a8_e6_e8_origin_keys_owner_qq_backup_ssrf_and_masking(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rt = Path(tmp.name) / "rt"
        rt.mkdir(parents=True, exist_ok=True)

        # A5: Custom resident/task base_url without its own api_key MUST NOT receive primary api_key
        srv_evil = _MockTTSAndImageServer()
        self.addCleanup(srv_evil.close)
        router = ModelRouter(
            {
                "AI_HOUSE_API_KEY": "sk-primary-secret-9999",
                "AI_HOUSE_BASE_URL": "https://api.openai.com",
                "AI_HOUSE_MODEL": "gpt-4o-mini",
                "AI_HOUSE_ACTOR_BASE_URL_SORA": srv_evil.base_url,
            },
            runtime_dir=rt,
        )
        self.addCleanup(router.close)
        eps_sora = router.resolve_endpoints(purpose="chat", actor="sora")
        self.assertEqual(eps_sora, [])
        self.assertFalse(router.has_live_endpoint(purpose="chat", actor="sora"))
        # Whereas same-origin override or missing URL inherits primary key normally
        router2 = ModelRouter(
            {
                "AI_HOUSE_API_KEY": "sk-primary-secret-9999",
                "AI_HOUSE_BASE_URL": srv_evil.base_url,
                "AI_HOUSE_ACTOR_MODEL_SORA": "sora-custom",
            },
            runtime_dir=rt,
        )
        self.addCleanup(router2.close)
        self.assertEqual(router2.resolve_endpoints(purpose="chat", actor="sora")[0][0].api_key, "sk-primary-secret-9999")

        # A7: Removing QQ from ONEBOT_OWNER_QQ deletes stale config binding without overriding manual visitor binding
        ist = IdentityStore(rt)
        self.addCleanup(ist.close)
        ist.upsert_identity("visitor:qq:8002", kind="visitor", display_name="阿晴")
        ist.bind_external("onebot", "8002", "visitor:qq:8002", meta={"source": "manual"})
        ist.sync_onebot_owners(["8001", "8002"])
        self.assertTrue(ist.is_owner_external("onebot", "8001"))
        self.assertFalse(ist.is_owner_external("onebot", "8002"), "manual binding must not be hijacked")
        ist.sync_onebot_owners([])
        self.assertFalse(ist.is_owner_external("onebot", "8001"), "removed config owner QQ must lose owner status")

        # A8 & E8: Short secrets masked without prefix/suffix leak; backup strips api_key from JSON files
        self.assertEqual(mask_secret("abc12345"), "••••••••")
        self.assertEqual(mask_secret("123456789012"), "••••••••")
        self.assertTrue(mask_secret("sk-very-long-secret-key-xyz").startswith("sk-"))

        d = HouseDaemon(rt, pack_dir=str(ROOT / "src" / "ai_house" / "bundled_packs" / "indie-studio-3"), env={}, allow_onboarding=False)
        self.addCleanup(d.close)
        d.update_llm_settings({
            "fallback": {"base_url": srv_evil.base_url, "model": "fb-m", "api_key": "sk-fb-plaintext-secret-1111"},
            "resident_models": {"sora": {"model": "m1", "api_key": "sk-res-plaintext-secret-2222"}},
        })
        d.update_plugin_settings({
            "plugin_id": "web_search",
            "enabled": True,
            "values": {"provider": "tavily", "api_key": "sk-plugin-plaintext-secret-3333"},
        })
        arc_doc = create_backup(rt)
        arc_raw = Path(arc_doc["archive"]).read_bytes()
        with tarfile.open(fileobj=io.BytesIO(arc_raw), mode="r:gz") as tf:
            llm_backed = tf.extractfile("llm_config.json").read().decode("utf-8")
            plg_backed = tf.extractfile("plugin_settings.json").read().decode("utf-8")
        self.assertNotIn("sk-fb-plaintext-secret-1111", llm_backed)
        self.assertNotIn("sk-res-plaintext-secret-2222", llm_backed)
        self.assertNotIn("sk-plugin-plaintext-secret-3333", plg_backed)

        # E6: CGNAT, localhost., DNS failure, and DNS rebinding (TOCTOU) blocked
        for bad_url in (
            "http://100.64.0.1/img.png",
            "http://localhost./img.png",
            "http://sub.localhost/img.png",
        ):
            with self.assertRaises(ValueError):
                validate_media_url(bad_url, allow_loopback=False)

        with mock.patch("ai_house.media_safety.socket.getaddrinfo", side_effect=socket.gaierror("offline")):
            with self.assertRaises(ValueError):
                validate_media_url("https://unresolvable.example.invalid/pic.png", allow_loopback=False)

        # Simulate DNS rebinding: first resolution returns public 93.184.216.34, connect-time resolution flips to 127.0.0.1!
        pub_info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        rebind_info = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with mock.patch("ai_house.media_safety.socket.getaddrinfo", side_effect=[pub_info, rebind_info]):
            with self.assertRaises(ValueError):
                fetch_media_bytes("http://rebind.example.com/pic.png", allow_loopback=False)


if __name__ == "__main__":
    unittest.main()
