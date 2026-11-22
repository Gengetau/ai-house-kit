"""Last check before a roommate's reply leaves the house (web studio and QQ alike).

A reply is held back, not edited, when it contains
  * the value of any configured secret (tokens, API keys, bridge keys),
  * an absolute server path, the runtime directory, or the names of the database/env files,
  * a long verbatim stretch of a protected text (the persona / system prompt),
  * a raw `[CQ:` code,
  * (for group output) a long verbatim stretch of something said in a private chat.

`check()` returns a short reason code, never the offending text, so logs stay clean.
"""
from __future__ import annotations

import re
import threading
from collections import deque
from typing import Deque, Iterable, Mapping, Optional, Set

_SECRET_KEY = re.compile(
    r"(?:_(?:TOKEN|SECRET|KEY)(?:_|$)|(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|BRIDGE_KEY|_KEY)$)",
    re.I,
)
# ASCII-only boundaries on purpose: Python's \w and \b treat Chinese characters as word characters, which would let
# "文件在/home/me/x里" or "改一下.env文件" slip through.
_PATH = re.compile(
    r"(?<![A-Za-z0-9_:/.])/(?:home|root|srv|etc|var|usr|opt|tmp|app|data|mnt|run|proc|sys|Users)(?:/|(?![A-Za-z0-9_]))"
    r"|(?<![A-Za-z0-9])[A-Za-z]:\\"
    r"|(?<![A-Za-z0-9_])(?:life1|world)\.db(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])\.env(?![A-Za-z0-9_])"
    r"|_secrets(?![A-Za-z0-9_])"
)
_NOISE = re.compile(r"[\W_]+", re.UNICODE)
MIN_SECRET_LEN = 6
SHINGLE_PROTECTED = 20
SHINGLE_PRIVATE = 12


def _norm(text: str) -> str:
    return _NOISE.sub("", str(text or "")).casefold()


def _windows(text: str, n: int) -> Iterable[str]:
    t = _norm(text)
    for i in range(0, max(0, len(t) - n + 1)):
        yield t[i : i + n]


class ReplyGuard:
    def __init__(
        self,
        env: Mapping[str, str],
        runtime_dir: object = "",
        protected_texts: Iterable[str] = (),
        private_memory: int = 60,
    ):
        self._secrets = sorted(
            {str(v).strip() for k, v in env.items() if _SECRET_KEY.search(str(k)) and len(str(v).strip()) >= MIN_SECRET_LEN},
            key=len,
            reverse=True,
        )
        rt = str(runtime_dir or "").strip().rstrip("/\\")
        self._runtime = rt if ((rt.startswith("/") and len(rt) > 1) or (re.match(r"^[A-Za-z]:[\\/]", rt) and len(rt) > 3)) else ""
        self._protected: Set[str] = set()
        for t in protected_texts:
            self._protected.update(_windows(t, SHINGLE_PROTECTED))
        self._private: Deque[Set[str]] = deque(maxlen=private_memory)
        self._lock = threading.Lock()

    def add_protected(self, text: str) -> None:
        with self._lock:
            self._protected.update(_windows(text, SHINGLE_PROTECTED))

    def note_private(self, text: str) -> None:
        """Remember something the owner said in a private chat so it cannot surface verbatim in a group.

        Only the owner's words are remembered: the roommate's own replies repeat stock phrases, and blocking those
        in a group would be a false alarm.
        """
        w = set(_windows(text, SHINGLE_PRIVATE))
        if w:
            with self._lock:
                self._private.append(w)

    def check(self, channel: str, text: str) -> Optional[str]:
        t = str(text or "")
        if "[cq:" in t.lower():
            return "cq_code"
        for s in self._secrets:
            if s in t:
                return "secret"
        if (self._runtime and self._runtime in t) or _PATH.search(t):
            return "internal_path"
        with self._lock:
            if self._protected and any(w in self._protected for w in _windows(t, SHINGLE_PROTECTED)):
                return "system_prompt"
            if not str(channel).startswith("private"):
                mine = set(_windows(t, SHINGLE_PRIVATE))
                if mine and any(mine & p for p in self._private):
                    return "private_content"
        return None
