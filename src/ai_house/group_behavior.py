"""Group-chat behavioral controls: reply probability, bot streak cap, owner mute & sleep silence (`0.3.0`)."""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .chat_identity import Speaker

_MUTE_CMD_RE = re.compile(
    r"^(?:先)?(?:闭麦|安静一下|别说话|静音)(?:\s*(\d+)\s*(分钟|分|小时|h|m))?[。！!~～]*$",
    re.I,
)
_UNMUTE_CMD_RE = re.compile(
    r"^(?:开麦|解除闭麦|恢复发言|可以说话了)[。！!~～]*$",
    re.I,
)


class GroupBehaviorController:
    """Thread-safe persistent runtime state for group mute timers and non-owner streak counters."""

    STATE_FILENAME = "group_behavior.json"

    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        clock: Any = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.state_path = self.runtime_dir / self.STATE_FILENAME
        self._clock = clock
        self._rng = rng or random.Random()
        self._lock = threading.RLock()
        self._state = self._load()

    def _now(self, now: Optional[float] = None) -> float:
        if now is not None:
            return float(now)
        return float(self._clock.now()) if self._clock is not None else time.time()

    def _load(self) -> Dict[str, Any]:
        empty: Dict[str, Any] = {"muted_until": {}, "streak": {}, "last_bot_ts": {}}
        if not self.state_path.is_file():
            return empty
        try:
            doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
        if not isinstance(doc, dict):
            return empty
        return {
            "muted_until": {str(k): float(v) for k, v in (doc.get("muted_until") or {}).items()},
            "streak": {str(k): int(v) for k, v in (doc.get("streak") or {}).items()},
            "last_bot_ts": {str(k): float(v) for k, v in (doc.get("last_bot_ts") or {}).items()},
        }

    def _save(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.runtime_dir / f".{self.STATE_FILENAME}.tmp.{os.getpid()}"
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def reset(self) -> None:
        with self._lock:
            self._state = {"muted_until": {}, "streak": {}, "last_bot_ts": {}}
            if self.state_path.exists():
                try:
                    self.state_path.unlink()
                except OSError:
                    self._save()

    def check_group_turn(
        self,
        channel: str,
        speaker: Speaker,
        text: str,
        *,
        enabled: bool,
        cfg: Mapping[str, Any],
        now: Optional[float] = None,
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """Evaluate owner voice commands, active mute window, streak cap, and reply probability."""
        if not enabled:
            return None, None
        ts = self._now(now)
        ch = str(channel or "group")
        clean_text = str(text or "").strip()
        default_mute_min = max(1, min(int(cfg.get("mute_minutes") or 30), 1440))
        max_streak = max(1, min(int(cfg.get("max_streak") or 8), 50))
        streak_reset_s = max(10, min(int(cfg.get("streak_reset_s") or 300), 3600))
        reply_prob = max(0.0, min(float(1.0 if cfg.get("reply_prob") is None else cfg.get("reply_prob")), 1.0))

        with self._lock:
            if speaker.is_owner:
                if _UNMUTE_CMD_RE.match(clean_text):
                    self._state["muted_until"].pop(ch, None)
                    self._state["streak"][ch] = 0
                    self._save()
                    return "unmuted_ack", {"action": "unmuted", "channel": ch}
                m_mute = _MUTE_CMD_RE.match(clean_text)
                if m_mute:
                    num = int(m_mute.group(1)) if m_mute.group(1) else default_mute_min
                    unit = str(m_mute.group(2) or "").lower()
                    if unit in ("小时", "h"):
                        mins = max(1, min(num * 60, 1440))
                    else:
                        mins = max(1, min(num, 1440))
                    until = ts + mins * 60.0
                    self._state["muted_until"][ch] = until
                    self._save()
                    return "muted_ack", {"action": "muted", "channel": ch, "minutes": mins, "muted_until": until}

            until = float(self._state["muted_until"].get(ch) or 0.0)
            if until > ts:
                return "muted", {"channel": ch, "muted_until": until}
            if ch in self._state["muted_until"]:
                self._state["muted_until"].pop(ch, None)
                self._save()

            if speaker.is_owner:
                if self._state["streak"].get(ch):
                    self._state["streak"][ch] = 0
                    self._save()
                return None, None

            last_ts = float(self._state["last_bot_ts"].get(ch) or 0.0)
            if last_ts and (ts - last_ts) >= streak_reset_s:
                self._state["streak"][ch] = 0

            cur_streak = int(self._state["streak"].get(ch) or 0)
            if cur_streak >= max_streak:
                return "streak_cap", {"channel": ch, "streak": cur_streak, "max_streak": max_streak}

            if reply_prob < 1.0 and self._rng.random() >= reply_prob:
                return "prob_skipped", {"channel": ch, "reply_prob": reply_prob}

            return None, None

    def note_group_replies(
        self,
        channel: str,
        speaker: Speaker,
        sent_count: int,
        *,
        now: Optional[float] = None,
    ) -> None:
        if speaker.is_owner or sent_count <= 0:
            return
        ts = self._now(now)
        ch = str(channel or "group")
        with self._lock:
            self._state["streak"][ch] = int(self._state["streak"].get(ch) or 0) + 1
            self._state["last_bot_ts"][ch] = ts
            self._save()

    def mute(self, channel: str = "group", *, minutes: int = 30, now: Optional[float] = None) -> Dict[str, Any]:
        ts = self._now(now)
        ch = str(channel or "group")
        mins = max(1, min(int(minutes or 30), 1440))
        until = ts + mins * 60.0
        with self._lock:
            self._state["muted_until"][ch] = until
            self._save()
        return {"channel": ch, "minutes": mins, "muted_until": until}

    def unmute(self, channel: str = "group") -> Dict[str, Any]:
        ch = str(channel or "group")
        with self._lock:
            self._state["muted_until"].pop(ch, None)
            self._state["streak"][ch] = 0
            self._save()
        return {"channel": ch, "muted": False}

    def reset_streak(self, channel: Optional[str] = None) -> None:
        with self._lock:
            if channel:
                self._state["streak"][str(channel)] = 0
            else:
                self._state["streak"].clear()
            self._save()

    def is_muted(self, channel: str = "group", *, now: Optional[float] = None) -> bool:
        ts = self._now(now)
        ch = str(channel or "group")
        with self._lock:
            until = float(self._state["muted_until"].get(ch) or 0.0)
            return until > ts

    def status_view(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        ts = self._now(now)
        with self._lock:
            active_mutes = {
                ch: {"muted_until": until, "remaining_s": max(0.0, until - ts)}
                for ch, until in self._state["muted_until"].items()
                if until > ts
            }
            return {
                "active_mutes": active_mutes,
                "streak": dict(self._state["streak"]),
                "last_bot_ts": dict(self._state["last_bot_ts"]),
            }
