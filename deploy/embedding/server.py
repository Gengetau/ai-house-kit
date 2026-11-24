"""A small local embedding service for the house's memory: `POST /v1/embeddings` in the OpenAI shape.

The memory code's recall and outbound-check thresholds were tuned against one particular model run by one particular
library; another model, or the same model served by another library, gives scores on a different scale (see
`docs/PORTING-PLAN.md` 15.13). This service pins both: `fastembed` at the version in the Dockerfile, and
`BAAI/bge-small-zh-v1.5`.

The model is loaded on the first request, in the background: until then (and while its files are being fetched
into the cache on the very first run) requests get 503, and the house falls back to keyword recall. A house that
never asks costs almost no memory.

Settings (environment): `EMBEDDING_MODEL`, `EMBEDDING_CACHE` (where the model files are), `EMBEDDING_HOST`,
`EMBEDDING_PORT`. No keys; no outbound calls once the model files are in the cache.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
MODEL_CACHE = os.environ.get("EMBEDDING_CACHE", "/models")
MAX_BODY = 4 * 1024 * 1024  # bytes; the house sends at most a few hundred short lines at a time
MAX_INPUTS = 1024

RETRY_AFTER_S = 60  # after a failed load (no network on the first run, say) the next request tries again

_model = None
_state = {"status": "idle", "failed_at": 0.0}  # idle -> loading -> ready, or failed (retried later)
_state_lock = threading.Lock()
_lock = threading.Lock()  # one text batch at a time: the model already uses the cores it is given


def load_model():
    """From the cache when the files are there (no network touched: the library otherwise asks the model hub every
    time, and a hub that is unreachable or rate-limiting would keep a complete cache from loading); else fetch."""
    from fastembed import TextEmbedding

    try:
        return TextEmbedding(model_name=MODEL_NAME, cache_dir=MODEL_CACHE, local_files_only=True)
    except Exception:  # noqa: BLE001 - not in the cache yet, or incomplete
        return TextEmbedding(model_name=MODEL_NAME, cache_dir=MODEL_CACHE)


def _load():
    global _model
    try:
        loaded = load_model()
    except Exception as exc:  # noqa: BLE001
        with _state_lock:
            _state.update(status="failed", failed_at=time.monotonic())
        print(f"embedding service: could not load {MODEL_NAME} ({type(exc).__name__}); will retry", flush=True)
        return
    with _state_lock:
        _model = loaded
        _state["status"] = "ready"
    print(f"embedding service: {MODEL_NAME} ready", flush=True)


def ready(wait=False):
    """Is the model loaded? Starts loading (once) when it is not; `wait` blocks until that attempt ends."""
    with _state_lock:
        status = _state["status"]
        start = status == "idle" or (status == "failed" and time.monotonic() - _state["failed_at"] >= RETRY_AFTER_S)
        if start:
            _state["status"] = "loading"
    if start:
        worker = threading.Thread(target=_load, name="embedding-load", daemon=True)
        worker.start()
        if wait:
            worker.join()
    return _state["status"] == "ready"


def embed(texts):
    with _lock:
        return [[float(x) for x in vec] for vec in _model.embed(list(texts))]


def parse_inputs(doc):
    """The request's texts, or raises ValueError with what is wrong."""
    if not isinstance(doc, dict):
        raise ValueError("body must be a JSON object")
    raw = doc.get("input")
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list) or not items:
        raise ValueError("input must be a string or a non-empty list of strings")
    if len(items) > MAX_INPUTS:
        raise ValueError(f"at most {MAX_INPUTS} inputs per request")
    if any(not isinstance(item, str) for item in items):
        raise ValueError("input must contain only strings")
    return [item if item.strip() else "-" for item in items]


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-house-embedding/1"

    def _send(self, status, doc):
        body = json.dumps(doc).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/health", "/v1/models"):  # answers whether or not the model is loaded yet
            self._send(200, {"status": "ok", "model": MODEL_NAME, "model_state": _state["status"], "object": "list",
                             "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "local"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") not in ("/v1/embeddings", "/embeddings"):
            return self._send(404, {"error": "not found"})
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = -1
        if size <= 0 or size > MAX_BODY:
            return self._send(413 if size > MAX_BODY else 400, {"error": "body missing or too large"})
        try:
            texts = parse_inputs(json.loads(self.rfile.read(size).decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._send(400, {"error": str(exc)[:200]})
        if not ready():
            return self._send(503, {"error": "model not loaded yet", "model_state": _state["status"]})
        try:
            vectors = embed(texts)
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": type(exc).__name__})
        self._send(200, {"object": "list", "model": MODEL_NAME,
                         "data": [{"object": "embedding", "embedding": v, "index": i} for i, v in enumerate(vectors)],
                         "usage": {"prompt_tokens": 0, "total_tokens": 0}})

    def log_message(self, fmt, *args):  # the texts are private: log nothing per request
        pass


def main():
    host = os.environ.get("EMBEDDING_HOST", "127.0.0.1")
    port = int(os.environ.get("EMBEDDING_PORT", "7998"))
    if "--fetch" in sys.argv[1:]:  # put the model files in the cache and stop (to prepare a machine without network)
        return 0 if ready(wait=True) else 1
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"embedding service: {MODEL_NAME} on {host}:{port} (loaded on first use)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
