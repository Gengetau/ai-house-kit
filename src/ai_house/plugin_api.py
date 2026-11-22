"""Four-Axis Plugin & Content Pack SDK (`0.3.0`) for ai-house-kit.

Exposes declarative plugin manifests (`PluginManifest`, `ConfigField`), unified activation switches,
isolated discovery (`load_errors` never crash the daemon), and four extension surfaces:
  1. WorldPack       (`packs/<name>/` or entry_point `ai_house.packs`): declarative house, rooms, recipes & roommates.
  2. ChannelPlugin   (entry_point `ai_house.channels`): chat adapters (e.g. built-in `onebot_v11`).
  3. LifeExtension   (entry_point `ai_house.extensions`): formalizes `life.py`'s 5-hook protocol (`step`, `owns`,
                     `hold_until`, `context`, `shops`).
  4. StudioProvider  (entry_point `ai_house.providers`): wardrobe image renderers & long-term memory backends.
  5. Chat tools      (entry_point `ai_house.tools` or inside `ai_house.plugins` bundles).
"""
from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import inspect
import math
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

MAX_CONFIG_FIELDS = 8
VALID_FIELD_TYPES = ("string", "int", "float", "bool", "select", "list")
VALID_PLUGIN_KINDS = ("channel", "extension", "provider", "tool", "bundle", "pack")
_TRUE_STRS = frozenset({"1", "true", "on", "yes"})
_FALSE_STRS = frozenset({"0", "false", "off", "no"})


def _env_key(plugin_id: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", str(plugin_id).upper())


@dataclass(frozen=True)
class ConfigField:
    """One declarative configuration field rendered by the web settings UI and coerced before use."""

    key: str
    label: str
    field_type: str = "string"
    default: Any = ""
    secret: bool = False
    required: bool = False
    description: str = ""
    env_var: str = ""
    options: Tuple[Tuple[str, str], ...] = ()
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    placeholder: str = ""

    def __post_init__(self) -> None:
        if self.field_type not in VALID_FIELD_TYPES:
            raise ValueError(f"Unsupported field_type {self.field_type!r} for field {self.key!r}")

    def coerce(self, raw: Any) -> Any:
        """Validate and normalize `raw` into this field's declared Python type."""
        if self.field_type == "bool":
            if isinstance(raw, bool):
                return raw
            if raw is None:
                return bool(self.default)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return bool(raw)
            low = str(raw).strip().lower()
            if low in _TRUE_STRS:
                return True
            if low in _FALSE_STRS or low == "":
                return False
            raise ValueError(f"{self.label or self.key} 必须是布尔值 (true/false)")

        if self.field_type == "int":
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                val = int(self.default or 0)
            elif isinstance(raw, bool):
                raise ValueError(f"{self.label or self.key} 必须是整数")
            else:
                try:
                    val = int(str(raw).strip()) if isinstance(raw, str) else int(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{self.label or self.key} 必须是整数") from exc
            if self.min_val is not None and val < self.min_val:
                raise ValueError(f"{self.label or self.key} 不能小于 {int(self.min_val)}")
            if self.max_val is not None and val > self.max_val:
                raise ValueError(f"{self.label or self.key} 不能大于 {int(self.max_val)}")
            return val

        if self.field_type == "float":
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                val_f = float(self.default or 0.0)
            elif isinstance(raw, bool):
                raise ValueError(f"{self.label or self.key} 必须是数字")
            else:
                try:
                    val_f = float(str(raw).strip()) if isinstance(raw, str) else float(raw)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{self.label or self.key} 必须是数字") from exc
            if not math.isfinite(val_f):
                raise ValueError(f"{self.label or self.key} 必须是有限数字")
            if self.min_val is not None and val_f < self.min_val:
                raise ValueError(f"{self.label or self.key} 不能小于 {self.min_val:g}")
            if self.max_val is not None and val_f > self.max_val:
                raise ValueError(f"{self.label or self.key} 不能大于 {self.max_val:g}")
            return val_f

        if self.field_type == "list":
            src = self.default if raw is None else raw
            if isinstance(src, str):
                return [x.strip() for x in re.split(r"[,;\n]+", src) if x.strip()]
            if isinstance(src, (list, tuple)):
                return [str(x).strip() for x in src if str(x).strip()]
            raise ValueError(f"{self.label or self.key} 必须是列表")

        if self.field_type == "select":
            val_s = str(self.default if raw is None else raw).strip()
            allowed = {opt[0] for opt in self.options}
            if allowed and val_s and val_s not in allowed:
                raise ValueError(f"{self.label or self.key} 取值 {val_s!r} 不在允许选项内")
            return val_s

        # "string"
        if raw is None:
            return str(self.default or "").strip()
        return str(raw).strip()

    def to_schema_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "field_type": self.field_type,
            "default": list(self.default) if isinstance(self.default, tuple) else self.default,
            "secret": bool(self.secret),
            "required": bool(self.required),
            "description": self.description,
            "env_var": self.env_var,
            "options": [{"value": k, "label": lb} for k, lb in self.options],
            "min_val": self.min_val,
            "max_val": self.max_val,
            "placeholder": self.placeholder,
        }


@dataclass(frozen=True)
class PluginManifest:
    """Declarative metadata and bounded configuration schema for one plugin."""

    plugin_id: str
    name: str
    kind: str = "bundle"
    version: str = "0.3.0"
    description: str = ""
    default_enabled: bool = False
    requires_packages: Tuple[str, ...] = ()
    config_fields: Tuple[ConfigField, ...] = ()

    def __post_init__(self) -> None:
        if not self.plugin_id or not str(self.plugin_id).strip():
            raise ValueError("plugin_id must not be empty")
        if self.kind not in VALID_PLUGIN_KINDS:
            raise ValueError(f"Unsupported plugin kind {self.kind!r} for {self.plugin_id!r}")
        if len(self.config_fields) > MAX_CONFIG_FIELDS:
            raise ValueError(
                f"Plugin {self.plugin_id!r} declares {len(self.config_fields)} config fields (max {MAX_CONFIG_FIELDS})"
            )

    def missing_packages(self) -> List[str]:
        missing: List[str] = []
        for pkg in self.requires_packages:
            mod = str(pkg).strip()
            if mod and importlib.util.find_spec(mod) is None:
                missing.append(mod)
        return missing

    def to_schema_dict(self) -> Dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "name": self.name,
            "kind": self.kind,
            "version": self.version,
            "description": self.description,
            "default_enabled": bool(self.default_enabled),
            "requires_packages": list(self.requires_packages),
            "missing_packages": self.missing_packages(),
            "config_fields": [f.to_schema_dict() for f in self.config_fields],
        }


@dataclass(frozen=True)
class ToolRegistration:
    """Chat tool spec paired with its callable handler and plugin provenance."""

    spec: Dict[str, Any]
    handler: Optional[Callable[..., Dict[str, Any]]] = None
    plugin_id: str = ""
    takes_context: bool = False

    @property
    def name(self) -> str:
        return str((self.spec or {}).get("name") or self.plugin_id)


@dataclass(frozen=True)
class ToolContext:
    """Contextual metadata passed to context-aware plugin chat tools."""

    actor: str
    scope: str
    channel: str
    speaker: Any
    now: float
    plugin_config: Mapping[str, Any] = field(default_factory=dict)
    daemon: Any = None


@dataclass(frozen=True)
class PluginRequestContext:
    """Request context passed to plugin-registered HTTP endpoints under `/api/plugins/<plugin_id>/...`."""

    plugin_id: str
    method: str
    path: str
    query: Dict[str, List[str]]
    body: Any
    plugin_config: Mapping[str, Any] = field(default_factory=dict)
    daemon: Any = None
    now: float = 0.0


@dataclass(frozen=True)
class PluginRoute:
    """Declarative HTTP route registered by a plugin under `/api/plugins/<plugin_id>/<subpath>`."""

    method: str
    subpath: str
    handler: Callable[[PluginRequestContext], Any]
    tier: str = "read"

    def __post_init__(self) -> None:
        m = str(self.method or "GET").strip().upper()
        if m not in ("GET", "POST"):
            raise ValueError(f"Unsupported PluginRoute method {self.method!r}")
        raw_sub = "/" + str(self.subpath or "").strip().lstrip("/")
        if ".." in raw_sub or "\\" in raw_sub or "\x00" in raw_sub:
            raise ValueError(f"Unsafe PluginRoute subpath {self.subpath!r}")
        if raw_sub != "/" and raw_sub.endswith("/"):
            raw_sub = raw_sub.rstrip("/")
        tier = str(self.tier or "read").strip().lower()
        if tier not in ("public", "read", "write", "private", "owner"):
            raise ValueError(f"Unsupported PluginRoute tier {self.tier!r}")
        object.__setattr__(self, "method", m)
        object.__setattr__(self, "subpath", raw_sub)
        object.__setattr__(self, "tier", tier)


@dataclass(frozen=True)
class PluginTaskContext:
    """Execution context passed to plugin background periodic tasks."""

    plugin_id: str
    task_id: str
    now: float
    plugin_config: Mapping[str, Any] = field(default_factory=dict)
    daemon: Any = None


@dataclass(frozen=True)
class PluginTask:
    """Declarative periodic background job dispatched during post-step cycles."""

    task_id: str
    interval_s: float
    handler: Callable[[PluginTaskContext], None]
    run_outside_lock: bool = True

    def __post_init__(self) -> None:
        tid = str(self.task_id or "").strip()
        if not tid:
            raise ValueError("PluginTask task_id must not be empty")
        iv = float(self.interval_s)
        if not math.isfinite(iv) or iv <= 0:
            raise ValueError(f"PluginTask interval_s must be positive, got {self.interval_s!r}")
        object.__setattr__(self, "task_id", tid)
        object.__setattr__(self, "interval_s", iv)


def invoke_tool_handler(
    reg: ToolRegistration,
    args: Mapping[str, Any],
    ctx: ToolContext,
) -> Dict[str, Any]:
    """Execute a registered tool handler, injecting `ctx` / `context` when requested."""
    fn = reg.handler
    if not callable(fn):
        return {"error": f"tool_not_callable:{reg.name}"}
    clean_args = dict(args or {})
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
    except (TypeError, ValueError):
        params = {}
    has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    if "ctx" in params or (reg.takes_context and has_varkw):
        clean_args["ctx"] = ctx
    elif "context" in params:
        clean_args["context"] = ctx
    elif reg.takes_context and params:
        first_name = next(iter(params.keys()))
        if first_name not in clean_args:
            return dict(fn(ctx, **clean_args))
    if not has_varkw and params:
        allowed = set(params.keys())
        clean_args = {k: v for k, v in clean_args.items() if k in allowed}
    return dict(fn(**clean_args))


@dataclass(frozen=True)
class PluginLoadErrorRecord:
    """Captured discovery/instantiation failure for an isolated plugin."""

    plugin_id: str
    group: str
    error: str

    def to_dict(self) -> Dict[str, str]:
        return {"plugin_id": self.plugin_id, "group": self.group, "error": self.error}


@dataclass(frozen=True)
class WorldPackSpec:
    pack_id: str
    path: Path
    title: str = ""
    roster: Tuple[str, ...] = field(default_factory=tuple)


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
    idempotent_delivery: bool = False

    @abstractmethod
    def start(self, host: Any) -> None:
        """Start listening or attach routes to `host` (`HouseDaemon`)."""

    def stop(self) -> None:
        """Gracefully close connections."""

    def deliver(self, delivery_id: str, payload: Dict[str, Any]) -> Optional[str]:
        """Optional outbox relay hook for proactive roommate messages or nightly notes."""
        return None

    def lookup_delivery(self, delivery_id: str) -> Optional[str]:
        """Return external receipt reference if `delivery_id` is already known to the channel."""
        return None


class StudioProvider(ABC):
    """Wardrobe outfit renderer or semantic memory provider (`ai_house.providers`)."""

    provider_id: str = "custom"

    @abstractmethod
    def step(self, host: Any) -> None:
        """Poll `wardrobe_render_outbox` or `daylog` cursor idempotently."""


@dataclass
class PluginDefinition:
    """Complete bundle describing one built-in or third-party plugin across axes."""

    manifest: PluginManifest
    channel_factory: Optional[Callable[..., Optional[ChannelPlugin]]] = None
    extension_factory: Optional[Callable[..., Optional[Any]]] = None
    provider_factory: Optional[Callable[..., Optional[StudioProvider]]] = None
    tools: Tuple[ToolRegistration, ...] = ()
    routes: Tuple[PluginRoute, ...] = ()
    tasks: Tuple[PluginTask, ...] = ()
    source: str = "builtin"

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id


def resolve_plugin_config(
    manifest: PluginManifest,
    env: Optional[Mapping[str, str]] = None,
    stored_values: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve and type-coerce plugin config values (`stored > env > field.default`)."""
    eff = os.environ if env is None else env
    stored = stored_values if isinstance(stored_values, Mapping) else {}
    resolved: Dict[str, Any] = {}
    for fld in manifest.config_fields:
        has_stored = fld.key in stored and stored[fld.key] not in (None, "")
        if has_stored:
            raw = stored[fld.key]
        elif fld.env_var and (eff.get(fld.env_var) or "").strip():
            raw = eff.get(fld.env_var)
        else:
            raw = fld.default
        resolved[fld.key] = fld.coerce(raw)
    return resolved


def merged_plugin_env(
    defn: PluginDefinition,
    env: Optional[Mapping[str, str]] = None,
    stored_plugins: Optional[Mapping[str, Any]] = None,
) -> Dict[str, str]:
    """Return a shallow copy of `env` overlaid with stored config values bound to `env_var`."""
    eff = dict(os.environ if env is None else env)
    if not isinstance(stored_plugins, Mapping):
        return eff
    entry = stored_plugins.get(defn.plugin_id)
    if not isinstance(entry, Mapping):
        return eff
    vals = entry.get("values")
    if not isinstance(vals, Mapping):
        return eff
    for fld in defn.manifest.config_fields:
        if not fld.env_var or fld.key not in vals:
            continue
        raw = vals[fld.key]
        if raw in (None, "") and not fld.secret:
            continue
        if fld.secret and raw == "":
            continue
        if isinstance(raw, list):
            eff[fld.env_var] = ",".join(str(x) for x in raw)
        elif isinstance(raw, bool):
            eff[fld.env_var] = "1" if raw else "0"
        else:
            eff[fld.env_var] = str(raw)
    return eff


def is_plugin_enabled(
    defn: PluginDefinition,
    env: Optional[Mapping[str, str]] = None,
    stored_plugins: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Unified enablement check across all axes (`stored > per-plugin env > list env > legacy env > default`)."""
    pid = defn.plugin_id
    eff = os.environ if env is None else env

    if isinstance(stored_plugins, Mapping):
        entry = stored_plugins.get(pid)
        if isinstance(entry, Mapping) and isinstance(entry.get("enabled"), bool):
            return bool(entry["enabled"])

    single_raw = (eff.get(f"AI_HOUSE_PLUGIN_{_env_key(pid)}_ENABLED") or "").strip().lower()
    if single_raw in _TRUE_STRS:
        return True
    if single_raw in _FALSE_STRS:
        return False

    disabled = {x.strip().lower() for x in (eff.get("AI_HOUSE_DISABLED_PLUGINS") or "").split(",") if x.strip()}
    if pid.lower() in disabled:
        return False
    enabled = {x.strip().lower() for x in (eff.get("AI_HOUSE_ENABLED_PLUGINS") or "").split(",") if x.strip()}
    if pid.lower() in enabled:
        return True

    # Historical compatibility switches preserved across v0.2 -> v0.3
    if pid == "book_nook" and (eff.get("AI_HOUSE_BOOK_NOOK") or "1").strip() == "0":
        return False
    ext_list = {x.strip().lower() for x in (eff.get("AI_HOUSE_EXTENSIONS") or "").split(",") if x.strip()}
    if pid.lower() in ext_list:
        return True
    if pid == "city_mcp_outing" and (eff.get("CITY_FEDERATION_MCP_URL") or "").strip():
        return True
    if pid == "onebot_v11" and ((eff.get("ONEBOT_WS_URL") or "").strip() or (eff.get("ONEBOT_HTTP_URL") or "").strip()):
        return True

    if defn.manifest.kind == "tool" and "AI_HOUSE_TOOLS" in eff:
        raw_tools = (eff.get("AI_HOUSE_TOOLS") or "").strip()
        if not raw_tools or raw_tools.lower() in ("0", "none", "off"):
            return False
        wanted = {x.strip().lower() for x in raw_tools.split(",") if x.strip()}
        tool_names = {t.name.lower() for t in defn.tools}
        return pid.lower() in wanted or bool(tool_names & wanted)

    return bool(defn.manifest.default_enabled)


class PluginRegistry:
    """Discovers built-in and `importlib.metadata.entry_points` plugins with isolation on errors."""

    GROUPS = (
        "ai_house.plugins",
        "ai_house.packs",
        "ai_house.channels",
        "ai_house.extensions",
        "ai_house.providers",
        "ai_house.tools",
    )

    def __init__(self) -> None:
        self.definitions: Dict[str, PluginDefinition] = {}
        self.packs: Dict[str, Callable[..., WorldPackSpec]] = {}
        self.channels: Dict[str, Callable[..., Any]] = {}
        self.extensions: Dict[str, Callable[..., Any]] = {}
        self.providers: Dict[str, Callable[..., Any]] = {}
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.load_errors: List[PluginLoadErrorRecord] = []

    def register_definition(self, defn: PluginDefinition) -> None:
        """Register one `PluginDefinition` and update compatibility axis views."""
        pid = defn.plugin_id
        self.definitions[pid] = defn
        if defn.channel_factory is not None:
            self.channels[pid] = defn.channel_factory
        if defn.extension_factory is not None:
            self.extensions[pid] = defn.extension_factory
        if defn.provider_factory is not None:
            self.providers[pid] = defn.provider_factory
        for tr in defn.tools:
            tname = tr.name or pid
            self.tools[pid if len(defn.tools) == 1 and pid not in self.tools and tname == pid else tname] = {
                "spec": tr.spec,
                "fn": tr.handler,
                "registration": tr,
                "plugin_id": pid,
            }
            if len(defn.tools) == 1 and pid not in self.tools:
                self.tools[pid] = {
                    "spec": tr.spec,
                    "fn": tr.handler,
                    "registration": tr,
                    "plugin_id": pid,
                }

    def _register_entry_object(self, group: str, name: str, loaded: Any, *, source: str) -> None:
        if isinstance(loaded, PluginDefinition):
            defn = PluginDefinition(
                manifest=loaded.manifest,
                channel_factory=loaded.channel_factory,
                extension_factory=loaded.extension_factory,
                provider_factory=loaded.provider_factory,
                tools=tuple(
                    ToolRegistration(
                        spec=t.spec,
                        handler=t.handler,
                        plugin_id=t.plugin_id or loaded.plugin_id,
                        takes_context=t.takes_context,
                    )
                    for t in loaded.tools
                ),
                routes=loaded.routes,
                tasks=loaded.tasks,
                source=source,
            )
            self.register_definition(defn)
            return

        if group == "ai_house.plugins":
            if callable(loaded):
                built = loaded()
                if isinstance(built, PluginDefinition):
                    self._register_entry_object(group, name, built, source=source)
                    return
            raise TypeError(f"Entry point {name!r} in {group} must be a PluginDefinition or factory returning one")

        if group == "ai_house.packs":
            if isinstance(loaded, WorldPackSpec):
                self.packs[name] = lambda spec=loaded: spec
            elif isinstance(loaded, (str, Path)):
                p = Path(loaded)
                self.packs[name] = lambda c=p, pid=name: WorldPackSpec(pack_id=pid, path=c, title=pid)
            elif callable(loaded):
                self.packs[name] = loaded
            else:
                raise TypeError(f"Unsupported pack entry point {name!r}: {type(loaded).__name__}")
            return

        if group == "ai_house.channels":
            factory = loaded if callable(loaded) else None
            if factory is None:
                raise TypeError(f"Channel entry point {name!r} is not callable")
            self.register_definition(
                PluginDefinition(
                    manifest=PluginManifest(plugin_id=name, name=name, kind="channel", default_enabled=False),
                    channel_factory=factory,
                    source=source,
                )
            )
            return

        if group == "ai_house.extensions":
            factory = loaded if callable(loaded) else None
            if factory is None:
                raise TypeError(f"Extension entry point {name!r} is not callable")
            self.register_definition(
                PluginDefinition(
                    manifest=PluginManifest(plugin_id=name, name=name, kind="extension", default_enabled=False),
                    extension_factory=factory,
                    source=source,
                )
            )
            return

        if group == "ai_house.providers":
            factory = loaded if callable(loaded) else None
            if factory is None:
                raise TypeError(f"Provider entry point {name!r} is not callable")
            self.register_definition(
                PluginDefinition(
                    manifest=PluginManifest(plugin_id=name, name=name, kind="provider", default_enabled=True),
                    provider_factory=factory,
                    source=source,
                )
            )
            return

        if group == "ai_house.tools":
            reg = _coerce_tool_registration(name, loaded)
            self.register_definition(
                PluginDefinition(
                    manifest=PluginManifest(plugin_id=name, name=name, kind="tool", default_enabled=True),
                    tools=(reg,),
                    source=source,
                )
            )

    def discover(
        self,
        packs_root: Optional[Path] = None,
        extra_entry_points: Optional[Mapping[str, Sequence[Any]]] = None,
    ) -> "PluginRegistry":
        """Discover built-in plugins, filesystem packs, and third-party entry points safely."""
        # 1) Built-in declarative plugins
        from .builtin_plugins import book_nook, city_mcp_outing, dice_tabletop, onebot_ws, sticker_pack, svg_wardrobe_studio

        for mod in (
            city_mcp_outing,
            book_nook,
            svg_wardrobe_studio,
            onebot_ws,
            sticker_pack,
            dice_tabletop,
        ):
            self.register_definition(mod.PLUGIN_DEF)

        # 2) Filesystem packs (`packs/<id>/manifest.json`)
        for root in (Path(__file__).with_name("bundled_packs"), packs_root):
            if root and Path(root).is_dir():
                for child in sorted(Path(root).iterdir()):
                    mf = child / "manifest.json"
                    if child.is_dir() and mf.is_file():
                        pid = child.name
                        self.packs[pid] = lambda c=child: WorldPackSpec(pack_id=c.name, path=c, title=c.name)

        # 3) Third-party installed Python packages via standard entry_points (with strict fault isolation)
        for group in self.GROUPS:
            eps: List[Any] = []
            try:
                eps.extend(importlib.metadata.entry_points(group=group))
            except TypeError:
                eps.extend(importlib.metadata.entry_points().get(group, ()))
            except Exception as exc:  # noqa: BLE001
                self.load_errors.append(
                    PluginLoadErrorRecord(plugin_id="*", group=group, error=f"{type(exc).__name__}: {exc}")
                )
            if extra_entry_points and group in extra_entry_points:
                eps.extend(extra_entry_points[group])

            for ep in eps:
                ep_name = str(getattr(ep, "name", "") or "unknown")
                try:
                    loaded = ep.load() if hasattr(ep, "load") else ep
                    self._register_entry_object(group, ep_name, loaded, source="entrypoint")
                except Exception as exc:  # noqa: BLE001
                    self.load_errors.append(
                        PluginLoadErrorRecord(plugin_id=ep_name, group=group, error=f"{type(exc).__name__}: {exc}")
                    )
        return self

    def load_dotted(self, spec: str) -> Any:
        """Load `package.module:callable` specified in AI_HOUSE_PLUGINS / AI_HOUSE_EXTENSIONS."""
        mod_name, _, attr = spec.partition(":")
        mod = importlib.import_module(mod_name.strip())
        return getattr(mod, attr.strip()) if attr else mod

    def active_definitions(
        self,
        env: Optional[Mapping[str, str]] = None,
        stored_plugins: Optional[Mapping[str, Any]] = None,
    ) -> List[PluginDefinition]:
        return [d for d in self.definitions.values() if is_plugin_enabled(d, env=env, stored_plugins=stored_plugins)]

    def resolve_config(
        self,
        plugin_id: str,
        env: Optional[Mapping[str, str]] = None,
        stored_plugins: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        defn = self.definitions.get(plugin_id)
        if defn is None:
            raise KeyError(f"Unknown plugin: {plugin_id}")
        stored_vals = (
            stored_plugins.get(plugin_id, {}).get("values")
            if isinstance(stored_plugins, Mapping) and isinstance(stored_plugins.get(plugin_id), Mapping)
            else None
        )
        return resolve_plugin_config(defn.manifest, env=env, stored_values=stored_vals)

    def active_extension_specs(
        self,
        env: Optional[Mapping[str, str]] = None,
        stored_plugins: Optional[Mapping[str, Any]] = None,
    ) -> List[Tuple[str, Callable[[Any], Any]]]:
        eff = os.environ if env is None else env
        out: List[Tuple[str, Callable[[Any], Any]]] = []
        seen: set[str] = set()
        for defn in self.definitions.values():
            if defn.extension_factory is None:
                continue
            if not is_plugin_enabled(defn, env=eff, stored_plugins=stored_plugins):
                continue
            seen.add(defn.plugin_id)
            plugin_env = merged_plugin_env(defn, eff, stored_plugins)
            factory = defn.extension_factory
            out.append((
                defn.plugin_id,
                self._wrap_isolated_extension_factory(
                    defn.plugin_id, factory, plugin_env, isolate_runtime=(defn.source != "builtin")
                ),
            ))

        raw_extra = [x.strip() for x in (eff.get("AI_HOUSE_EXTENSIONS") or "").split(",") if x.strip()]
        for name in raw_extra:
            if name in seen or ":" not in name:
                continue
            try:
                factory = self.load_dotted(name)
            except Exception as exc:  # noqa: BLE001
                self.load_errors.append(
                    PluginLoadErrorRecord(plugin_id=name, group="ai_house.extensions", error=f"{type(exc).__name__}: {exc}")
                )
                continue
            out.append((
                name,
                self._wrap_isolated_extension_factory(name, factory, dict(eff), isolate_runtime=True),
            ))
        return out

    def active_extension_factories(
        self,
        env: Optional[Mapping[str, str]] = None,
        stored_plugins: Optional[Mapping[str, Any]] = None,
    ) -> List[Callable[[Any], Any]]:
        return [f for _, f in self.active_extension_specs(env=env, stored_plugins=stored_plugins)]

    def _wrap_isolated_extension_factory(
        self,
        plugin_id: str,
        factory: Callable[..., Any],
        plugin_env: Mapping[str, str],
        *,
        isolate_runtime: bool = False,
    ) -> Callable[[Any], Any]:
        errors = self.load_errors

        def _build(life: Any) -> Any:
            try:
                ext = factory(life, env=dict(plugin_env)) if _accepts_env(factory) else factory(life)
                if ext is None:
                    return _DisabledExtensionStub(plugin_id)
                if isolate_runtime:
                    return _IsolatedExtensionProxy(plugin_id, ext, errors)
                return ext
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    PluginLoadErrorRecord(plugin_id=plugin_id, group="ai_house.extensions", error=f"{type(exc).__name__}: {exc}")
                )
                return _DisabledExtensionStub(plugin_id)

        return _build

    def active_channels(
        self,
        env: Optional[Mapping[str, str]] = None,
        stored_plugins: Optional[Mapping[str, Any]] = None,
    ) -> List[Tuple[str, Callable[[], Optional[ChannelPlugin]]]]:
        eff = os.environ if env is None else env
        out: List[Tuple[str, Callable[[], Optional[ChannelPlugin]]]] = []
        for defn in self.definitions.values():
            if defn.channel_factory is None:
                continue
            if not is_plugin_enabled(defn, env=eff, stored_plugins=stored_plugins):
                continue
            plugin_env = merged_plugin_env(defn, eff, stored_plugins)
            factory = defn.channel_factory
            out.append((
                defn.plugin_id,
                lambda f=factory, e=plugin_env: f(e) if _accepts_env(f) else f(),
            ))
        return out

    def active_providers(
        self,
        env: Optional[Mapping[str, str]] = None,
        stored_plugins: Optional[Mapping[str, Any]] = None,
    ) -> List[Tuple[str, StudioProvider]]:
        eff = os.environ if env is None else env
        out: List[Tuple[str, StudioProvider]] = []
        for defn in self.definitions.values():
            if defn.provider_factory is None:
                continue
            if not is_plugin_enabled(defn, env=eff, stored_plugins=stored_plugins):
                continue
            plugin_env = merged_plugin_env(defn, eff, stored_plugins)
            try:
                inst = defn.provider_factory(env=plugin_env) if _accepts_env(defn.provider_factory) else defn.provider_factory()
                if inst is not None:
                    out.append((defn.plugin_id, inst))
            except Exception as exc:  # noqa: BLE001
                self.load_errors.append(
                    PluginLoadErrorRecord(plugin_id=defn.plugin_id, group="ai_house.providers", error=f"{type(exc).__name__}: {exc}")
                )
        return out

    def active_tools(
        self,
        env: Optional[Mapping[str, str]] = None,
        stored_plugins: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        eff = os.environ if env is None else env
        raw_tools_set = "AI_HOUSE_TOOLS" in eff
        raw_tools_val = (eff.get("AI_HOUSE_TOOLS") or "").strip()
        if raw_tools_set and (not raw_tools_val or raw_tools_val.lower() in ("0", "none", "off")):
            # Explicit tool list off only disables non-forced tools; individual stored/plugin switches still count.
            explicit_off = True
            wanted_tools: set[str] = set()
        elif raw_tools_set:
            explicit_off = False
            wanted_tools = {x.strip().lower() for x in raw_tools_val.split(",") if x.strip()}
        else:
            explicit_off = False
            wanted_tools = set()

        out: Dict[str, Dict[str, Any]] = {}
        for defn in self.definitions.values():
            if not defn.tools:
                continue
            enabled = is_plugin_enabled(defn, env=eff, stored_plugins=stored_plugins)
            if not enabled:
                continue
            for tr in defn.tools:
                tname = tr.name or defn.plugin_id
                if explicit_off and not _has_explicit_plugin_enable(defn.plugin_id, eff, stored_plugins):
                    continue
                if raw_tools_set and not explicit_off and not _has_explicit_plugin_enable(defn.plugin_id, eff, stored_plugins):
                    if tname.lower() not in wanted_tools and defn.plugin_id.lower() not in wanted_tools:
                        continue
                key = defn.plugin_id if (len(defn.tools) == 1 and defn.plugin_id in ("sticker_pack", "dice_tabletop", "book_nook")) else tname
                out[key] = {
                    "spec": tr.spec,
                    "fn": tr.handler,
                    "registration": tr,
                    "plugin_id": defn.plugin_id,
                }
        return out

    def active_routes(
        self,
        env: Optional[Mapping[str, str]] = None,
        stored_plugins: Optional[Mapping[str, Any]] = None,
    ) -> Dict[Tuple[str, str], Tuple[PluginDefinition, PluginRoute]]:
        out: Dict[Tuple[str, str], Tuple[PluginDefinition, PluginRoute]] = {}
        for defn in self.active_definitions(env=env, stored_plugins=stored_plugins):
            prefix = f"/api/plugins/{defn.plugin_id}"
            for rt in defn.routes:
                full = prefix if rt.subpath == "/" else (prefix + rt.subpath)
                out[(rt.method, full)] = (defn, rt)
        return out

    def active_tasks(
        self,
        env: Optional[Mapping[str, str]] = None,
        stored_plugins: Optional[Mapping[str, Any]] = None,
    ) -> List[Tuple[PluginDefinition, PluginTask]]:
        out: List[Tuple[PluginDefinition, PluginTask]] = []
        for defn in self.active_definitions(env=env, stored_plugins=stored_plugins):
            for task in defn.tasks:
                out.append((defn, task))
        return out


def _has_explicit_plugin_enable(
    plugin_id: str,
    eff: Mapping[str, str],
    stored_plugins: Optional[Mapping[str, Any]],
) -> bool:
    if isinstance(stored_plugins, Mapping):
        ent = stored_plugins.get(plugin_id)
        if isinstance(ent, Mapping) and ent.get("enabled") is True:
            return True
    if (eff.get(f"AI_HOUSE_PLUGIN_{_env_key(plugin_id)}_ENABLED") or "").strip().lower() in _TRUE_STRS:
        return True
    enabled = {x.strip().lower() for x in (eff.get("AI_HOUSE_ENABLED_PLUGINS") or "").split(",") if x.strip()}
    return plugin_id.lower() in enabled


def _coerce_tool_registration(name: str, loaded: Any) -> ToolRegistration:
    if isinstance(loaded, ToolRegistration):
        return loaded if loaded.plugin_id else ToolRegistration(
            spec=loaded.spec, handler=loaded.handler, plugin_id=name, takes_context=loaded.takes_context
        )
    if isinstance(loaded, Mapping) and isinstance(loaded.get("spec"), dict):
        return ToolRegistration(
            spec=dict(loaded["spec"]),
            handler=loaded.get("fn") or loaded.get("handler"),
            plugin_id=name,
            takes_context=bool(loaded.get("takes_context", False)),
        )
    if isinstance(loaded, tuple) and len(loaded) == 2 and isinstance(loaded[0], dict):
        return ToolRegistration(spec=dict(loaded[0]), handler=loaded[1], plugin_id=name)
    if callable(loaded):
        built = loaded()
        if not callable(built):
            return _coerce_tool_registration(name, built)
    raise TypeError(f"Tool entry point {name!r} must provide ToolRegistration, (TOOL_SPEC, fn), or {{'spec', 'fn'}}")


def _accepts_env(fn: Callable[..., Any]) -> bool:
    try:
        sig = inspect.signature(fn)
        return "env" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        return False


class _DisabledExtensionStub:
    """No-op LifeExtension stand-in used when an isolated extension fails or stays inactive."""

    _is_stub = True
    shops = False

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id

    def step(self) -> None:
        return None

    def owns(self, actor_id: str) -> bool:
        return False

    def hold_until(self, actor_id: str, now: float) -> Optional[float]:
        return None

    def context(self, actor_id: str, decision_kind: str) -> Optional[Dict[str, Any]]:
        return None


class _IsolatedExtensionProxy:
    """Wraps third-party LifeExtension hooks so runtime exceptions degrade instead of crashing ticks."""

    def __init__(self, plugin_id: str, inner: Any, errors: List[PluginLoadErrorRecord]) -> None:
        self.plugin_id = plugin_id
        self._inner = inner
        self._errors = errors
        self.shops = bool(getattr(inner, "shops", False))

    def _record(self, exc: Exception) -> None:
        rec = PluginLoadErrorRecord(plugin_id=self.plugin_id, group="ai_house.extensions", error=f"{type(exc).__name__}: {exc}")
        if rec not in self._errors:
            self._errors.append(rec)

    def step(self) -> None:
        try:
            self._inner.step()
        except Exception as exc:  # noqa: BLE001
            self._record(exc)

    def owns(self, actor_id: str) -> bool:
        fn = getattr(self._inner, "owns", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(actor_id))
        except Exception as exc:  # noqa: BLE001
            self._record(exc)
            return False

    def hold_until(self, actor_id: str, now: float) -> Optional[float]:
        fn = getattr(self._inner, "hold_until", None)
        if not callable(fn):
            return None
        try:
            return fn(actor_id, now)
        except Exception as exc:  # noqa: BLE001
            self._record(exc)
            return None

    def context(self, actor_id: str, decision_kind: str) -> Optional[Dict[str, Any]]:
        fn = getattr(self._inner, "context", None)
        if not callable(fn):
            return None
        try:
            return fn(actor_id, decision_kind)
        except Exception as exc:  # noqa: BLE001
            self._record(exc)
            return None

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)
