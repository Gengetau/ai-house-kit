"""Access control for the ai-house daemon.

Three tiers, checked per request:
  read     state, ingredients, group chat history, event stream
  write    avatar/stock/step/city actions, sending to the group chat
  private  reading or sending private chats with a roommate

`AI_HOUSE_OWNER_TOKEN` opens every tier. `AI_HOUSE_READ_TOKEN`, `AI_HOUSE_WRITE_TOKEN`
and `AI_HOUSE_PRIVATE_TOKEN` each open only their own tier. When no token is configured
the daemon is only allowed to listen on a loopback address (see `check_bind`).
"""
from __future__ import annotations

import hmac
import ipaddress
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlparse

TIERS = ("read", "write", "private")
MIN_REMOTE_TOKEN_LEN = 16


class BindRefused(ValueError):
    """Raised when the listen address is not allowed with the current token settings."""


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def check_bind(host: str, env: Mapping[str, str]) -> None:
    """Refuse to listen on a non-loopback address without a strong owner token."""
    if is_loopback_host(host):
        return
    tok = (env.get("AI_HOUSE_OWNER_TOKEN") or "").strip()
    if not tok:
        raise BindRefused(
            f"refusing to listen on non-loopback address {host!r}: set AI_HOUSE_OWNER_TOKEN "
            f"(at least {MIN_REMOTE_TOKEN_LEN} random characters) or listen on 127.0.0.1"
        )
    if len(tok) < MIN_REMOTE_TOKEN_LEN:
        raise BindRefused(f"AI_HOUSE_OWNER_TOKEN must be at least {MIN_REMOTE_TOKEN_LEN} characters on {host!r}")
    for tier in TIERS:  # a weak tier token on an open interface is just as guessable
        t = (env.get(f"AI_HOUSE_{tier.upper()}_TOKEN") or "").strip()
        if t and len(t) < MIN_REMOTE_TOKEN_LEN:
            raise BindRefused(f"AI_HOUSE_{tier.upper()}_TOKEN must be at least {MIN_REMOTE_TOKEN_LEN} characters on {host!r}")


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class AuthPolicy:
    def __init__(self, env: Mapping[str, str]):
        def g(key: str) -> str:
            return (env.get(key) or "").strip()

        self.owner = g("AI_HOUSE_OWNER_TOKEN")
        self.tier_tokens: Dict[str, str] = {t: g(f"AI_HOUSE_{t.upper()}_TOKEN") for t in TIERS}

    @property
    def enabled(self) -> bool:
        return bool(self.owner or any(self.tier_tokens.values()))

    def authenticated(self, presented: str) -> bool:
        """True if the token is valid for at least one tier (or auth is disabled)."""
        if not self.enabled:
            return True
        return any(self.allows(presented, t) for t in TIERS)

    def allows(self, presented: str, tier: str) -> bool:
        if not self.enabled:
            return True
        presented = (presented or "").strip()
        if not presented:
            return False
        if self.owner and _same(presented, self.owner):
            return True
        want = self.tier_tokens.get(tier, "")
        return bool(want) and _same(presented, want)

    def is_owner(self, presented: str) -> bool:
        """True if no tokens are configured (local loopback mode) or presented equals AI_HOUSE_OWNER_TOKEN."""
        if not self.enabled:
            return True
        presented = (presented or "").strip()
        return bool(self.owner and presented and _same(presented, self.owner))


def tier_for_channel(channel: str, write: bool) -> str:
    """Private chats and non-public channels require `private` tier; only `group` uses `read`/`write`."""
    ch = str(channel or "group").strip().lower()
    if ch != "group":
        return "private"
    return "write" if write else "read"


def mcp_tier(body: Any) -> Optional[str]:
    """Tier needed for one JSON-RPC body, or None if the body is not a single request object."""
    if not isinstance(body, dict):
        return None
    if body.get("method") != "tools/call":
        return "read"
    params = body.get("params")
    params = {} if params is None else params
    if not isinstance(params, dict):
        return None
    args = params.get("arguments")
    args = {} if args is None else args
    if not isinstance(args, dict):
        return None
    name = params.get("name")
    if name in ("get_house_state", "get_resident_card"):
        return "read"
    if name == "chat_with_resident":
        return tier_for_channel(str(args.get("channel") or "group"), write=True)
    return "write"  # owner_avatar_act and any tool we do not know about


def host_is_loopback(host_header: str) -> bool:
    """Host: header check against DNS rebinding (used when no token is configured)."""
    h = (host_header or "").strip()
    if h.startswith("["):
        h = h[1:].split("]", 1)[0]
    elif h.count(":") == 1:
        h = h.rsplit(":", 1)[0]
    return is_loopback_host(h)


def origin_is_loopback(origin: str) -> bool:
    if not origin:
        return True  # non-browser clients send no Origin
    try:
        return is_loopback_host(urlparse(origin).hostname or "")
    except ValueError:
        return False
