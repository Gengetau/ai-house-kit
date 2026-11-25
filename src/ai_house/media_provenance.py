"""Where a picture, a voice clip or a sticker came from, so a group can be sent it only when the house made it there.

Text can be read and checked. An image or a voice clip cannot, so in a group it goes out only when the tool that
made or picked it wrote a record at that moment: the sha256 of the bytes, the chat it was made for and the text it
was made from (the image prompt and negative prompt, the spoken text, the sticker's label). At send time the gate
looks the bytes up by hash, requires the same chat, and runs the reply guard and the privacy check on that text again.

The registry lives in memory on purpose: a restart loses it and everything then fails closed (the media is not sent,
with a reason), which is the safe direction. The owner's private chat does not need a record.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

MAX_RECORDS = 2048
MAX_TEXTS_PER_RECORD = 8
MAX_TEXT_CHARS = 1200

KINDS = ("image", "audio", "sticker")


def sha256_of_b64(b64: Any) -> str:
    """sha256 (hex) of the decoded bytes of a base64 string; "" when it is empty or not base64."""
    raw = str(b64 or "").strip()
    if not raw:
        return ""
    try:
        data = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError):
        return ""
    return hashlib.sha256(data).hexdigest() if data else ""


def media_b64(item: Mapping[str, Any]) -> str:
    """The base64 payload of a reply media item (`image_base64` / `audio_base64`)."""
    return str(item.get("image_base64") or item.get("audio_base64") or "").strip()


def media_kind(item: Mapping[str, Any]) -> str:
    k = str(item.get("kind") or "").strip().lower()
    if k in ("image", "sticker"):
        return "image"
    if k in ("audio", "record", "voice"):
        return "audio"
    return k


class MediaProvenance:
    """Bounded in-memory map `(sha256, chat) -> record`. Thread-safe."""

    def __init__(self, max_records: int = MAX_RECORDS) -> None:
        self._max = max(16, int(max_records))
        self._rows: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def register(self, b64: Any, *, chat: str, source_texts: Sequence[str], kind: str = "image") -> str:
        """Record that these bytes were made for `chat` from `source_texts`. Returns the sha256 ("" = nothing recorded)."""
        digest = sha256_of_b64(b64)
        ch = str(chat or "").strip()
        if not digest or not ch:
            return ""
        texts = [str(t).strip()[:MAX_TEXT_CHARS] for t in source_texts if str(t or "").strip()]
        key = (digest, ch)
        with self._lock:
            row = self._rows.pop(key, None) or {"sha256": digest, "chat": ch, "kind": str(kind or ""), "texts": []}
            for t in texts:
                if t not in row["texts"]:
                    row["texts"].append(t)
            row["texts"] = row["texts"][-MAX_TEXTS_PER_RECORD:]
            self._rows[key] = row
            while len(self._rows) > self._max:
                self._rows.popitem(last=False)
        return digest

    def lookup(self, digest: str, chat: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """`(record, "")` when these bytes were registered for this chat; `(None, "other_chat")` when only another chat
        has them; `(None, "missing")` when nobody does (never registered, replaced bytes, or lost on restart)."""
        d = str(digest or "")
        ch = str(chat or "").strip()
        with self._lock:
            row = self._rows.get((d, ch))
            if row is not None:
                return {**row, "texts": list(row["texts"])}, ""
            if any(k[0] == d for k in self._rows):
                return None, "other_chat"
        return None, "missing"

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)


def register_for_tool(daemon: Any, ctx: Any, b64: Any, *, source_texts: Sequence[str], kind: str) -> str:
    """Called by the built-in media tools right after they made or picked media for the chat in `ctx`.

    A host without a registry (a test double, a plugin proxy) simply records nothing, which later fails closed."""
    reg = getattr(daemon, "media_provenance", None)
    if not isinstance(reg, MediaProvenance):
        return ""
    chat = str(getattr(ctx, "channel", "") or "").strip()
    if not chat:
        scope = str(getattr(ctx, "scope", "") or "")
        chat = f"private:{getattr(ctx, 'actor', '')}" if scope == "private" else "group"
    return reg.register(b64, chat=chat, source_texts=source_texts, kind=kind)


def describe(items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """What the listen-only record keeps about media: kind, sha256 and size. Never the bytes."""
    out: List[Dict[str, Any]] = []
    for m in items or ():
        b64 = media_b64(m)
        try:
            size = len(base64.b64decode(b64, validate=False)) if b64 else 0
        except (binascii.Error, ValueError):
            size = 0
        out.append({"kind": media_kind(m), "sha256": sha256_of_b64(b64), "size": size})
    return out
