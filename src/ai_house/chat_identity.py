"""Who is speaking in a chat turn.

The role (`owner` / `member`) is decided by the *transport* from something it verified
(an API token tier for the web studio, a bound QQ number for OneBot). It is never decided from
text the speaker typed: nicknames, group cards and message bodies are only ever labels.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

OWNER = "owner"
MEMBER = "member"

_NAME_BAD = re.compile(r"[\[\]|<>{}`【】［］｜\r\n\t]")
_NAME_TAGS = re.compile(r"管理员|房主|主人|室友|系统|已验证|\bowner\b|\badmin\b|\bbot\b|\bsystem\b", re.I)
MAX_NAME_LEN = 24


def clean_name(name: object, fallback: str = "群友") -> str:
    """A display label that is safe to put into a prompt or a chat log.

    No brackets or control characters (they delimit prompt structure), no words that claim a role,
    no long digit runs (QQ-number lookalikes), bounded length.
    """
    n = _NAME_BAD.sub(" ", str(name or ""))
    n = _NAME_TAGS.sub("·", n)
    n = re.sub(r"\d{5,}", "#", n)
    n = re.sub(r"\s+", " ", n).strip()[:MAX_NAME_LEN].strip()
    return n or fallback


@dataclass(frozen=True)
class Speaker:
    role: str
    name: str
    uid: str = ""

    @property
    def is_owner(self) -> bool:
        return self.role == OWNER

    @property
    def label(self) -> str:
        """How the roommate's prompt refers to this person."""
        return "房主" if self.is_owner else f"群友「{self.name}」"


def one_line(text: object) -> str:
    """Collapse every run of whitespace (including newlines) so a message cannot start a fake line of its own."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def quote_untrusted(text: object) -> str:
    """Text from someone who is not verified as the owner, made safe to sit inside a prompt line."""
    t = one_line(text).replace("「", "『").replace("」", "』")
    # lookalikes of the program's own speaker labels
    return t.replace("刚刚说：", "刚刚说:").replace("房主:", "房主 :").replace("房主：", "房主 :")


def owner_speaker(name: object = "房主", uid: str = "") -> Speaker:
    n = str(name or "").strip()
    return Speaker(OWNER, "房主" if n in ("", "房主") else clean_name(n, "房主"), str(uid or ""))


def member_speaker(name: object, uid: str = "") -> Speaker:
    return Speaker(MEMBER, clean_name(name), str(uid or ""))
