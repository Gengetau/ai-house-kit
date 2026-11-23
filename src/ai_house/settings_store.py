"""Persistent plugin configuration store (`plugin_settings.json`) and masked schema serializers."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .onboarding import atomic_write_private_text, mask_secret, upsert_dotenv_vars
from .plugin_api import PluginDefinition, PluginManifest, is_plugin_enabled, resolve_plugin_config, safe_resolve_plugin_config


class SettingsStore:
    """Thread-safe JSON storage for per-plugin enablement toggles and declared config values."""

    FILENAME = "plugin_settings.json"

    def __init__(self, runtime_dir: str | Path) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.path = self.runtime_dir / self.FILENAME
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = self._load_disk()

    def _load_disk(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {"version": 1, "plugins": {}}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "plugins": {}}
        if not isinstance(doc, dict):
            return {"version": 1, "plugins": {}}
        plugins = doc.get("plugins")
        return {"version": int(doc.get("version") or 1), "plugins": dict(plugins) if isinstance(plugins, dict) else {}}

    def _save_disk(self) -> None:
        atomic_write_private_text(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))

    def reload_from_disk(self) -> None:
        with self._lock:
            self._data = self._load_disk()

    def get_plugins_map(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._data.get("plugins") or {})

    def restore_plugin_entry(self, plugin_id: str, prev_entry: Optional[Mapping[str, Any]]) -> None:
        """Restore a plugin entry (or remove it if previously unset) after a failed hot-reload."""
        with self._lock:
            plugins = self._data.setdefault("plugins", {})
            if prev_entry is None:
                plugins.pop(plugin_id, None)
            else:
                plugins[plugin_id] = copy.deepcopy(dict(prev_entry))
            self._save_disk()

    def update_plugin(
        self,
        manifest: PluginManifest,
        *,
        enabled: Optional[bool] = None,
        values: Optional[Mapping[str, Any]] = None,
        clear_secrets: Sequence[str] = (),
        env: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        """Validate and persist updates for one plugin, preserving existing secrets when left blank."""
        pid = manifest.plugin_id
        incoming = dict(values) if isinstance(values, Mapping) else {}
        clear_set = {str(k).strip() for k in (clear_secrets or ()) if str(k).strip()}

        with self._lock:
            plugins = self._data.setdefault("plugins", {})
            prev = copy.deepcopy(plugins.get(pid) if isinstance(plugins.get(pid), dict) else {})
            new_vals: Dict[str, Any] = dict(prev.get("values") if isinstance(prev.get("values"), dict) else {})

            for fld in manifest.config_fields:
                if fld.secret:
                    if fld.key in clear_set:
                        new_vals[fld.key] = ""
                    elif fld.key in incoming:
                        cand = str(incoming[fld.key] or "").strip()
                        if cand != "":
                            new_vals[fld.key] = cand
                elif fld.key in incoming:
                    new_vals[fld.key] = fld.coerce(incoming[fld.key])

            next_entry: Dict[str, Any] = dict(prev)
            if enabled is not None:
                next_entry["enabled"] = bool(enabled)
            next_entry["values"] = new_vals

            if next_entry.get("enabled") is True:
                eff_vals = resolve_plugin_config(manifest, env=env, stored_values=new_vals)
                for fld in manifest.config_fields:
                    if fld.required and not eff_vals.get(fld.key):
                        raise ValueError(f"启用「{manifest.name}」前必须填写必填项：{fld.label}")

            plugins[pid] = next_entry
            self._save_disk()
            return copy.deepcopy(next_entry)

    def sync_env_and_dotenv(
        self,
        definitions: Mapping[str, PluginDefinition],
        target_env: Dict[str, str],
        *,
        env_file: Optional[Path] = None,
    ) -> None:
        """Apply stored `env_var`-backed values to `target_env` and optionally persist them to `.env`."""
        with self._lock:
            plugins = self._data.get("plugins") or {}
            updates: Dict[str, str] = {}
            for pid, defn in definitions.items():
                entry = plugins.get(pid)
                if not isinstance(entry, Mapping):
                    continue
                vals = entry.get("values")
                if isinstance(vals, Mapping):
                    for fld in defn.manifest.config_fields:
                        if not fld.env_var or fld.key not in vals:
                            continue
                        raw = vals[fld.key]
                        if isinstance(raw, list):
                            sval = ",".join(str(x) for x in raw)
                        elif isinstance(raw, bool):
                            sval = "1" if raw else "0"
                        else:
                            sval = str(raw if raw is not None else "")
                        target_env[fld.env_var] = sval
                        updates[fld.env_var] = sval
            if env_file is not None and updates:
                upsert_dotenv_vars(env_file, updates)

    def collect_secrets(
        self,
        definitions: Mapping[str, PluginDefinition],
        env: Mapping[str, str],
    ) -> List[str]:
        """Collect all configured secret field values across plugins for ReplyGuard scrubbing."""
        out: List[str] = []
        seen = set()
        with self._lock:
            plugins = self._data.get("plugins") or {}
            for pid, defn in definitions.items():
                entry = plugins.get(pid) if isinstance(plugins.get(pid), Mapping) else {}
                vals = entry.get("values") if isinstance(entry.get("values"), Mapping) else {}
                for fld in defn.manifest.config_fields:
                    if not fld.secret:
                        continue
                    for cand in (
                        vals.get(fld.key),
                        env.get(fld.env_var) if fld.env_var else None,
                    ):
                        s = str(cand or "").strip()
                        if len(s) >= 6 and s not in seen:
                            seen.add(s)
                            out.append(s)
        return out

    def serialize_plugin_view(
        self,
        defn: PluginDefinition,
        env: Mapping[str, str],
        *,
        active: bool,
        runtime_error: str = "",
    ) -> Dict[str, Any]:
        """Serialize manifest + current effective values, redacting every secret field."""
        plugins_map = self.get_plugins_map()
        entry = plugins_map.get(defn.plugin_id) or {}
        stored_vals = entry.get("values") if isinstance(entry.get("values"), dict) else {}
        resolved, cfg_warnings = safe_resolve_plugin_config(defn.manifest, env=env, stored_values=stored_vals)
        enabled = is_plugin_enabled(defn, env=env, stored_plugins=plugins_map)

        fields_out: List[Dict[str, Any]] = []
        for fld in defn.manifest.config_fields:
            fschema = fld.to_schema_dict()
            eff_val = resolved.get(fld.key)
            if fld.secret:
                explicitly_cleared = fld.key in stored_vals and stored_vals[fld.key] == ""
                raw_sec = "" if explicitly_cleared else str(eff_val or "")
                fields_out.append({
                    **fschema,
                    "value": "",
                    "secret_set": bool(raw_sec),
                    "secret_masked": mask_secret(raw_sec),
                })
            else:
                fields_out.append({
                    **fschema,
                    "value": eff_val,
                })

        return {
            "plugin_id": defn.plugin_id,
            "name": defn.manifest.name,
            "kind": defn.manifest.kind,
            "version": defn.manifest.version,
            "description": defn.manifest.description,
            "source": defn.source,
            "default_enabled": bool(defn.manifest.default_enabled),
            "enabled": enabled,
            "active": bool(active),
            "missing_packages": defn.manifest.missing_packages(),
            "error": runtime_error,
            "config_warnings": cfg_warnings,
            "fields": fields_out,
        }
