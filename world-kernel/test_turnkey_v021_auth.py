"""v0.2.1: listen-address rule and per-tier token checks for the daemon HTTP + MCP surface."""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "world-kernel"), str(ROOT / "world-catalog")):
    if p not in sys.path:
        sys.path.insert(0, p)

from ai_house.auth import BindRefused, check_bind, mcp_tier
from ai_house.daemon import HouseDaemon, make_http_server

OWNER = "o" * 24
READER = "r" * 24


class BindRuleTests(unittest.TestCase):
    def test_loopback_hosts_need_no_token(self):
        for host in ("127.0.0.1", "::1", "localhost", "127.0.0.5"):
            check_bind(host, {})

    def test_non_loopback_without_owner_token_is_refused(self):
        for host in ("0.0.0.0", "::", "192.168.1.20", "house.example.net"):
            with self.assertRaises(BindRefused):
                check_bind(host, {})
            with self.assertRaises(BindRefused):
                check_bind(host, {"AI_HOUSE_READ_TOKEN": READER})  # a read token is not an owner token

    def test_non_loopback_with_short_owner_token_is_refused(self):
        with self.assertRaises(BindRefused):
            check_bind("0.0.0.0", {"AI_HOUSE_OWNER_TOKEN": "short"})

    def test_non_loopback_with_strong_owner_token_is_allowed(self):
        check_bind("0.0.0.0", {"AI_HOUSE_OWNER_TOKEN": OWNER})

    def test_weak_tier_token_on_an_open_interface_is_refused(self):
        for tier in ("READ", "WRITE", "PRIVATE"):
            with self.assertRaises(BindRefused):
                check_bind("0.0.0.0", {"AI_HOUSE_OWNER_TOKEN": OWNER, f"AI_HOUSE_{tier}_TOKEN": "1"})
        check_bind("0.0.0.0", {"AI_HOUSE_OWNER_TOKEN": OWNER, "AI_HOUSE_READ_TOKEN": READER})

    def test_mcp_tiers(self):
        call = lambda name, **a: {"method": "tools/call", "params": {"name": name, "arguments": a}}
        self.assertEqual(mcp_tier({"method": "tools/list"}), "read")
        self.assertEqual(mcp_tier(call("get_house_state")), "read")
        self.assertEqual(mcp_tier(call("chat_with_resident", channel="group", text="hi")), "write")
        self.assertEqual(mcp_tier(call("chat_with_resident", channel="private:nell", text="hi")), "private")
        self.assertEqual(mcp_tier(call("owner_avatar_act", op="enter")), "write")
        self.assertEqual(mcp_tier(call("something_new")), "write")
        self.assertIsNone(mcp_tier([{"method": "tools/list"}]))
        self.assertIsNone(mcp_tier({"method": "tools/call", "params": []}))
        self.assertIsNone(mcp_tier({"method": "tools/call", "params": {"name": "x", "arguments": "oops"}}))


class _Served:
    def __init__(self, tc: unittest.TestCase, env: dict):
        tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(tmp.cleanup)
        self.daemon = HouseDaemon(runtime_dir=Path(tmp.name) / "rt", virtual_clock=True, env=env)
        tc.addCleanup(self.daemon.close)
        self.srv = make_http_server(self.daemon, host="127.0.0.1", port=0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        tc.addCleanup(self.srv.server_close)
        tc.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None, token=None, headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw.startswith("{") else raw)
        finally:
            c.close()


def _rpc(name, **args):
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}


class TokenTierTests(unittest.TestCase):
    def setUp(self):
        self.s = _Served(self, {"AI_HOUSE_OWNER_TOKEN": OWNER, "AI_HOUSE_READ_TOKEN": READER})

    def test_make_http_server_itself_refuses_open_bind(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        tmp = tmp_dir.name
        d = HouseDaemon(runtime_dir=Path(tmp) / "rt", virtual_clock=True, env={})
        self.addCleanup(d.close)
        with self.assertRaises(BindRefused):
            make_http_server(d, host="0.0.0.0", port=0)

    def test_no_token_gets_401_everywhere_except_the_static_page(self):
        st, _ = self.s.req("GET", "/")
        self.assertEqual(st, 200)
        for method, path, body in (
            ("GET", "/api/state", None),
            ("GET", "/api/chat?channel=group", None),
            ("GET", "/api/chat?channel=private:nell", None),
            ("POST", "/api/stock", {"item": "cookie", "qty": 1}),
            ("POST", "/api/avatar", {"op": "enter", "location": "living"}),
            ("POST", "/api/chat", {"channel": "private:nell", "text": "hi"}),
            ("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
            ("POST", "/mcp", _rpc("chat_with_resident", channel="private:nell", text="hi")),
        ):
            st, doc = self.s.req(method, path, body)
            self.assertEqual(st, 401, (method, path))
            self.assertEqual(doc["error"], "unauthorized")

    def test_wrong_token_is_401(self):
        st, _ = self.s.req("GET", "/api/state", token="x" * 24)
        self.assertEqual(st, 401)

    def test_unauthorized_private_send_leaves_no_message_behind(self):
        self.s.req("POST", "/api/chat", {"channel": "private:nell", "text": "secret"})
        self.s.req("POST", "/mcp", _rpc("chat_with_resident", channel="private:nell", text="secret"))
        self.assertEqual(self.s.daemon.list_chat("private:nell"), [])

    def test_read_token_reads_but_cannot_write_or_touch_private(self):
        st, _ = self.s.req("GET", "/api/state", token=READER)
        self.assertEqual(st, 200)
        st, _ = self.s.req("GET", "/api/chat?channel=group", token=READER)
        self.assertEqual(st, 200)
        st, doc = self.s.req("GET", "/api/chat?channel=private:nell", token=READER)
        self.assertEqual(st, 403)
        self.assertEqual(doc["error"], "forbidden_tier:private")
        st, _ = self.s.req("POST", "/api/stock", {"item": "cookie", "qty": 1}, token=READER)
        self.assertEqual(st, 403)
        st, _ = self.s.req("POST", "/api/chat", {"channel": "group", "text": "hi"}, token=READER)
        self.assertEqual(st, 403)
        st, _ = self.s.req("POST", "/api/chat", {"channel": "private:nell", "text": "hi"}, token=READER)
        self.assertEqual(st, 403)
        st, _ = self.s.req("POST", "/mcp", _rpc("owner_avatar_act", op="enter"), token=READER)
        self.assertEqual(st, 403)
        st, _ = self.s.req("POST", "/mcp", _rpc("chat_with_resident", channel="private:nell", text="hi"), token=READER)
        self.assertEqual(st, 403)
        st, _ = self.s.req("POST", "/mcp", _rpc("get_house_state"), token=READER)
        self.assertEqual(st, 200)
        self.assertEqual(self.s.daemon.list_chat("private:nell"), [])

    def test_owner_token_opens_every_tier(self):
        st, _ = self.s.req("POST", "/api/stock", {"item": "cookie", "qty": 1}, token=OWNER)
        self.assertEqual(st, 200)
        st, doc = self.s.req("POST", "/api/chat", {"channel": "private:nell", "text": "你好"}, token=OWNER)
        self.assertEqual(st, 200)
        self.assertTrue(doc["ok"])
        st, doc = self.s.req("GET", "/api/chat?channel=private:nell", token=OWNER)
        self.assertEqual(st, 200)
        self.assertTrue(doc["messages"])
        st, doc = self.s.req("POST", "/mcp", _rpc("chat_with_resident", channel="group", text="大家好"), token=OWNER)
        self.assertEqual(st, 200)
        self.assertTrue(doc["result"]["structuredContent"]["ok"])

    def test_token_in_the_url_is_never_accepted_not_even_for_the_event_stream(self):
        for path in ("/api/state", "/api/events", "/api/chat?channel=group"):
            st, _ = self.s.req("GET", f"{path}{'&' if '?' in path else '?'}token={OWNER}")
            self.assertEqual(st, 401, path)

    def test_event_stream_takes_the_token_from_a_header(self):
        c = http.client.HTTPConnection("127.0.0.1", self.s.port, timeout=20)
        try:
            c.request("GET", "/api/events", headers={"Authorization": f"Bearer {READER}"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertIn("text/event-stream", r.getheader("Content-Type"))
            self.assertTrue(r.read(11).startswith(b": connected"))
        finally:
            c.close()

    def test_malformed_mcp_bodies_are_400_not_a_dropped_connection(self):
        st, _ = self.s.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}, token=OWNER)
        self.assertEqual(st, 400)
        st, _ = self.s.req("POST", "/mcp", [{"method": "tools/list"}], token=OWNER)
        self.assertEqual(st, 400)
        st, _ = self.s.req("POST", "/api/nope", {}, token=OWNER)
        self.assertEqual(st, 404)

    def test_city_return_is_a_write_action(self):
        st, _ = self.s.req("POST", "/api/city/return", {"actor_id": "nell", "force": True})
        self.assertEqual(st, 401)
        st, _ = self.s.req("POST", "/api/city/return", {"actor_id": "nell", "force": True}, token=READER)
        self.assertEqual(st, 403)
        st, doc = self.s.req("POST", "/api/city/return", {"actor_id": "nell", "force": True}, token=OWNER)
        self.assertEqual(st, 400)  # authorized, but no public-city plugin is enabled in this daemon


class StaticPageTests(unittest.TestCase):
    def test_the_page_never_puts_the_token_in_a_url(self):
        html = (ROOT / "src" / "ai_house" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("EventSource", html)
        self.assertNotIn("?token=", html)
        self.assertNotIn("&token=", html)

    def test_server_text_is_escaped_before_it_goes_into_innerhtml(self):
        html = (ROOT / "src" / "ai_house" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("const esc =", html)
        for raw in ("${m.text}", "${m.sender_name}", "${m.badge}", "${n.content}", "${e.text}", "${r.bio ||", "${res.name}"):
            self.assertNotIn(raw, html, raw)


class LoopbackNoTokenTests(unittest.TestCase):
    def setUp(self):
        self.s = _Served(self, {})

    def test_plain_local_use_still_works(self):
        st, _ = self.s.req("GET", "/api/state")
        self.assertEqual(st, 200)
        st, _ = self.s.req("POST", "/api/stock", {"item": "cookie", "qty": 1})
        self.assertEqual(st, 200)

    def test_rebinding_host_header_is_rejected(self):
        st, doc = self.s.req("GET", "/api/state", headers={"Host": "evil.example.com"})
        self.assertEqual(st, 403)
        self.assertEqual(doc["error"], "bad_host")

    def test_cross_site_post_is_rejected(self):
        st, doc = self.s.req("POST", "/api/stock", {"item": "cookie", "qty": 1}, headers={"Origin": "https://evil.example.com"})
        self.assertEqual(st, 403)
        self.assertEqual(doc["error"], "bad_origin")
        st, _ = self.s.req("POST", "/api/stock", {"item": "cookie", "qty": 1}, headers={"Origin": f"http://127.0.0.1:{self.s.port}"})
        self.assertEqual(st, 200)


if __name__ == "__main__":
    unittest.main()
