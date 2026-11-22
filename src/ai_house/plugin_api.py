"""Four-Axis Plugin & Content Pack SDK for ai-house-kit.

Exposes four clean extension surfaces without allowing plugins to bypass the single-writer SQLite kernel:
  1. WorldPack       (`packs/<name>/` or entry_point `ai_house.packs`): declarative house, rooms, recipes & roommates.
  2. ChannelPlugin   (entry_point `ai_house.channels`): chat adapters. The built-in web chat needs no plugin;
                     the built-in `onebot_v11` (QQ via your own OneBot server) is only active when ONEBOT_WS_URL is set.
  3. LifeExtension   (entry_point `ai_house.extensions`): formalizes `life.py`'s 5-hook protocol (`step`, `owns`,
                     `hold_until`, `context`, `shops`).
  4. StudioProvider  (entry_point `ai_house.providers`): wardrobe image renderers & long-term memory backends.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class WorldPackSpec:
    pack_id: str
    path: Path
    title: str = ""
    roster: tuple[str, ...] = field(default_factory=tuple)


class LifeExtension(ABC):
    """Formal base class for `worldkernel.life.Life` extensions (`ai_house.extensions`)."""

    shops: bool = False

    def __init__(self, life: Any):
        self.life = life

    @abstractmethod
    def step(self) -> None:
        """Called once per kernel step inside the single-writer loop."""

    def owns(self, actor_id: str) -> bool:
        """Return True while this plugin holds custody of `actor_id` (e.g. outings, city visits)."""
        return False

    def hold_until(self, actor_id: str, now: float) -> Optional[float]:
        """Earliest timestamp this plugin expects `actor_id` free so L0 pastimes finish beforehand."""
        return None

    def context(self, actor_id: str, decision_kind: str) -> Optional[Dict[str, Any]]:
        """Additional read-only perception keys merged into `actor_id`'s model decision context."""
        return None


class ChannelPlugin(ABC):
    """External messaging channel adapter (`ai_house.channels`)."""

    channel_id: str = "custom"

    @abstractmethod
    def start(self, host: Any) -> None:
        """Start listening or attach routes to `host` (`HouseDaemon`)."""

    def stop(self) -> None:
        """Gracefully close connections."""

    def deliver(self, delivery_id: str, payload: Dict[str, Any]) -> bool:
        """Optional outbox relay hook for proactive roommate messages or nightly notes."""
        return False


class StudioProvider(ABC):
    """Wardrobe outfit renderer or semantic memory provider (`ai_house.providers`)."""

    provider_id: str = "custom"

    @abstractmethod
    def step(self, host: Any) -> None:
        """Poll `wardrobe_render_outbox` or `daylog` cursor idempotently."""


class PluginRegistry:
    """Discovers built-in and `importlib.metadata.entry_points` plugins across all four axes."""

    GROUPS = ("ai_house.packs", "ai_house.channels", "ai_house.extensions", "ai_house.providers")

    def __init__(self) -> None:
        self.packs: Dict[str, Callable[..., WorldPackSpec]] = {}
        self.channels: Dict[str, Callable[..., ChannelPlugin]] = {}
        self.extensions: Dict[str, Callable[..., Any]] = {}
        self.providers: Dict[str, Callable[..., StudioProvider]] = {}
        self.tools: Dict[str, Dict[str, Any]] = {}

    def discover(self, packs_root: Optional[Path] = None) -> "PluginRegistry":
        # 1) Built-in plugins
        from .builtin_plugins.city_mcp_outing import CityMcpOutingExtension
        from .builtin_plugins.onebot_ws import OneBotChannel
        from .builtin_plugins.svg_wardrobe_studio import SvgWardrobeStudio
        from .builtin_plugins import book_nook, dice_tabletop, sticker_pack

        self.extensions["city_mcp_outing"] = CityMcpOutingExtension.from_env
        self.extensions["book_nook"] = book_nook.BookNookExtension.from_env
        self.providers["svg_wardrobe"] = SvgWardrobeStudio
        self.channels["onebot_v11"] = OneBotChannel.from_env
        self.tools["sticker_pack"] = {"spec": sticker_pack.TOOL_SPEC, "fn": sticker_pack.pick_mood_sticker}
        self.tools["dice_tabletop"] = {"spec": dice_tabletop.TOOL_SPEC, "fn": dice_tabletop.roll_dice}
        self.tools["book_nook"] = {"spec": book_nook.TOOL_SPEC, "fn": None}

        # 2) Filesystem packs (`packs/<id>/manifest.json`)
        for root in (Path(__file__).with_name("bundled_packs"), packs_root):
            if root and Path(root).is_dir():
                for child in sorted(Path(root).iterdir()):
                    mf = child / "manifest.json"
                    if child.is_dir() and mf.is_file():
                        pid = child.name
                        self.packs[pid] = lambda c=child: WorldPackSpec(pack_id=c.name, path=c, title=c.name)

        # 3) Third-party installed Python packages via standard entry_points
        for group, bucket in (
            ("ai_house.packs", self.packs),
            ("ai_house.channels", self.channels),
            ("ai_house.extensions", self.extensions),
            ("ai_house.providers", self.providers),
        ):
            try:
                eps = importlib.metadata.entry_points(group=group)
            except TypeError:
                eps = importlib.metadata.entry_points().get(group, ())
            for ep in eps:
                bucket[ep.name] = ep.load()
        return self

    def load_dotted(self, spec: str) -> Any:
        """Load `package.module:callable` specified in AI_HOUSE_PLUGINS."""
        mod_name, _, attr = spec.partition(":")
        mod = importlib.import_module(mod_name.strip())
        return getattr(mod, attr.strip()) if attr else mod

    def active_extension_factories(self, env: Optional[Dict[str, str]] = None) -> List[Callable[[Any], Any]]:
        eff = os.environ if env is None else env
        out: List[Callable[[Any], Any]] = []
        raw = [x.strip() for x in eff.get("AI_HOUSE_EXTENSIONS", "").split(",") if x.strip()]
        if eff.get("AI_HOUSE_BOOK_NOOK", "1") != "0" and "book_nook" not in raw:
            raw.append("book_nook")
        if eff.get("CITY_FEDERATION_MCP_URL") and "city_mcp_outing" not in raw:
            raw.append("city_mcp_outing")
        for name in raw:
            factory = self.extensions.get(name) or (self.load_dotted(name) if ":" in name else None)
            if factory is not None:
                out.append(lambda life, f=factory: f(life, env=eff) if _accepts_env(f) else f(life))
        return out

    def active_tools(self, env: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Any]]:
        eff = os.environ if env is None else env
        raw = eff.get("AI_HOUSE_TOOLS", "sticker_pack,dice_tabletop,book_nook").strip()
        if not raw or raw.lower() in ("0", "none", "off"):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for name in [x.strip() for x in raw.split(",") if x.strip()]:
            if name in self.tools:
                out[name] = self.tools[name]
        return out


def _accepts_env(fn: Callable[..., Any]) -> bool:
    import inspect
    try:
        sig = inspect.signature(fn)
        return "env" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        return False
