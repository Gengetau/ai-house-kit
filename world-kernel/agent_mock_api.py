"""A local stand-in for the Anthropic Messages API, so the real Claude Code shell can be driven without a key.

Only the model is replaced: the shell, its permission callback, the in-process tool server, interrupts and session
files are all the real thing. What the "model" does is decided from the conversation it is sent:

- the last user text contains 直接说        -> plain text (the resident forgets to send)
- it contains 看看家里                      -> call inspect_house, then send what it returned
- it contains 两条                          -> two messages, the first with end_turn=false
- it contains 用别的工具                    -> ask for a tool that is not offered, then send
- it contains 记得暗号                      -> send whether the word 青柠 appears earlier in the conversation
- it contains 不接话                        -> call stay_quiet
- it contains 等很久                        -> answer only after `slow_s` seconds (a turn the house gives up on)
- a tool result came back                   -> follow up as above, otherwise finish with a short text
- anything else                             -> send "收到：<the said line>"
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


def _texts(content: Any) -> List[str]:
    if isinstance(content, str):
        return [content]
    out = []
    for block in content or ():
        if isinstance(block, dict) and block.get("type") == "text":
            out.append(str(block.get("text") or ""))
    return out


def _tool_results(content: Any) -> List[Dict[str, Any]]:
    return [b for b in (content if isinstance(content, list) else ()) if isinstance(b, dict) and b.get("type") == "tool_result"]


class MockAnthropic:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.auth_headers: List[str] = []
        self._n = 0
        self.slow_s = 12.0
        self._lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a: Any) -> None:
                return

            def _json(self, doc: Dict[str, Any], status: int = 200) -> None:
                raw = json.dumps(doc).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                self._json({"data": []})

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    doc = json.loads(body or b"{}")
                except ValueError:
                    doc = {}
                path = self.path.split("?", 1)[0]
                if path.endswith("/count_tokens"):
                    self._json({"input_tokens": 100})
                    return
                if not path.endswith("/messages"):
                    self._json({})
                    return
                with mock._lock:
                    mock.requests.append(doc)
                    mock.auth_headers.append(self.headers.get("x-api-key") or self.headers.get("Authorization") or "")
                    mock._n += 1
                    n = mock._n
                blocks = mock.decide(doc)
                if mock.is_slow(doc):
                    time.sleep(mock.slow_s)
                try:
                    if doc.get("stream"):
                        self._sse(n, doc, blocks)
                    else:
                        self._json(mock.message(n, doc, blocks))
                except OSError:
                    pass  # the shell went away while a slow answer was on its way

            def _sse(self, n: int, doc: Dict[str, Any], blocks: List[Dict[str, Any]]) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                def ev(name: str, data: Dict[str, Any]) -> None:
                    self.wfile.write(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()

                msg = mock.message(n, doc, [])
                msg["stop_reason"] = None
                ev("message_start", {"type": "message_start", "message": msg})
                for i, block in enumerate(blocks):
                    if block["type"] == "text":
                        ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
                        ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block["text"]}})
                    else:
                        start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
                        ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": start})
                        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"], ensure_ascii=False)}
                        ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta})
                    ev("content_block_stop", {"type": "content_block_stop", "index": i})
                stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
                ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 12}})
                ev("message_stop", {"type": "message_stop"})
                self.close_connection = True

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    # ------------------------------------------------------------------ the "model"
    def message(self, n: int, doc: Dict[str, Any], blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
        size = len(json.dumps(doc.get("messages") or [], ensure_ascii=False)) // 3
        return {
            "id": f"msg_mock_{n}",
            "type": "message",
            "role": "assistant",
            "model": str(doc.get("model") or "claude-mock"),
            "content": blocks,
            "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 20 + size, "output_tokens": 12, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }

    def chat_requests(self) -> List[Dict[str, Any]]:
        """Requests that are a resident's turn (they carry the house tools), not the shell's own side calls."""
        return [r for r in self.requests if any(str(t.get("name", "")).endswith("send_message") for t in r.get("tools") or ())]

    @staticmethod
    def _last_said(doc: Dict[str, Any]) -> str:
        messages = [m for m in (doc.get("messages") or []) if m.get("role") in ("user", "assistant")]
        last = messages[-1] if messages else {}
        return "\n".join(_texts(last.get("content"))) if last.get("role") == "user" else ""

    def is_slow(self, doc: Dict[str, Any]) -> bool:
        said = self._last_said(doc)
        return "等很久" in said and "记得暗号" not in said

    def decide(self, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        tools = {str(t.get("name") or "") for t in doc.get("tools") or ()}
        speak = next((t for t in tools if t.endswith("__send_message")), "")
        if not speak:
            return [{"type": "text", "text": "ok"}]
        prefix = speak[: -len("send_message")]
        n = self._n

        def use(name: str, args: Dict[str, Any], full: str = "") -> Dict[str, Any]:
            return {"type": "tool_use", "id": f"toolu_mock_{n}_{name}", "name": full or prefix + name, "input": args}

        # The shell also puts its own notes in the conversation (role "system"), and a new prompt that follows a
        # tool result arrives in the same user message as that result: go by the last user message alone.
        messages = [m for m in (doc.get("messages") or []) if m.get("role") in ("user", "assistant")]
        last = messages[-1] if messages else {}
        said = "\n".join(_texts(last.get("content"))) if last.get("role") == "user" else ""
        results = [] if said.strip() else _tool_results(last.get("content"))
        if results:
            prev_use = next(
                (b for m in reversed(messages) if m.get("role") == "assistant" for b in (m.get("content") or ()) if isinstance(b, dict) and b.get("type") == "tool_use"),
                {},
            )
            prev = str(prev_use.get("name") or "")
            if prev.endswith("inspect_house"):
                raw = results[0].get("content")
                text = raw if isinstance(raw, str) else "".join(_texts(raw))
                return [use("send_message", {"text": "我看了：" + text[:60]})]
            if prev.endswith("send_message") and (prev_use.get("input") or {}).get("end_turn") is False:
                return [use("send_message", {"text": "第二条"})]
            if prev.endswith("no_such_tool") or prev == "Bash":
                return [use("send_message", {"text": "那个工具用不了"})]
            return [{"type": "text", "text": "（这一句本不该被生成）"}]
        if "你刚才把话直接写了出来" in said:
            return [use("send_message", {"text": "补发：刚才那句"})]
        if "直接说" in said:
            return [{"type": "text", "text": "我直接写出来了"}]
        if "看看家里" in said:
            return [use("inspect_house", {})]
        if "两条" in said:
            return [use("send_message", {"text": "第一条", "end_turn": False})]
        if "用别的工具" in said:
            return [use("Bash", {"command": "id"}, full="Bash")]
        if "不接话" in said and (prefix + "stay_quiet") in tools:
            return [use("stay_quiet", {"reason": "不是问我"})]
        if "记得暗号" in said:
            earlier = json.dumps(messages[:-1], ensure_ascii=False)
            return [use("send_message", {"text": "记得，是青柠" if "青柠" in earlier else "不记得了"})]
        line = next((ln for ln in reversed(said.splitlines()) if "刚刚说" in ln), said[-40:])
        return [use("send_message", {"text": "收到：" + line[:60]})]
