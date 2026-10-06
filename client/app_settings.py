"""
Global (cross-account) settings for WinZapp multi-account (client/app_settings.py)
==================================================================================

A handful of settings are install-wide, not per-account (plan Zad 2.3): the UI
language, the auto-updater toggle, the tray-icon toggle, Windows autostart, and
the shared WPPConnect host configuration.  The Node port is per-account because
each account process owns a separate local Node server.
These live in ``global/app.json`` so every account process reads/writes the SAME
value; everything else stays in each account's own settings.json.

``split_legacy_settings`` extracts the global keys out of a pre-split flat
settings.json during migration, returning (global_dict, remaining_per_account).

Guarded to global keys only (get/set raise KeyError on a per-account key) so a
caller can't accidentally route a per-account setting through the shared file.
Writes are atomic (tmp+fsync+os.replace); a corrupt file falls back to defaults.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from coord_locks import app_settings_lock

_FILE = "app.json"

# Global keys and their defaults. 'general.*' names are flattened here; the
# connection block is namespaced with its original keys.
_DEFAULTS: dict[str, Any] = {
    "language": "",
    # Only preferences and consent; credentials have a separate encrypted store.
    "ai_media": {},
    "updates_enabled": True,
    # Opt-in to the alpha channel (one build per commit landed on main, see
    # .github/workflows/alpha-release.yml). Global rather than per-account for
    # the same reason updates_enabled is: one install, one binary — an account
    # can't be on a different build of WinZapp than its siblings. Off by
    # default; alpha builds are untested by definition.
    "alpha_updates_enabled": False,
    # Download WinZapp and WPPConnect Server updates with no progress window
    # and only interrupt the app to install (update_background.py). Global for
    # the same reason as the two above: the install and its api/ are shared.
    "background_update_downloads": False,
    "show_tray_icon": True,
    "autostart": False,
    # install-wide one-time setup prompts (asked once per install, NOT per
    # account — a new account must not re-ask autostart/hotkey/api-type).
    "first_run": True,
    "hotkey_first_run_asked": False,
    "api_type_first_run_asked": False,
    "switch_behavior": "single",  # "single" (ocultar para a bandeja) ou "keep_open" (manter ambas abertas)
    # Shared WPPConnect host configuration. The port remains per-account.
    "wpp_server": "http://127.0.0.1",
    "wpp_ws_server": "ws://127.0.0.1",
    # Same default as client/core/utils.py DEFAULT_SETTINGS / settings_default.json
    # — a blank key makes /api/<token>//generate-token (double slash) → HTTP 404
    # on every new-account pairing under the multi-account flow.
    "wpp_api_key": "70733f08be1ed195bda1c31b6e135f5ebeb9fb8c6c28530a3a46e4093357b037",
    "wpp_custom_api": False,
}

# Which legacy general.* keys are global (the rest stay per-account).
_GENERAL_GLOBAL = ("language", "updates_enabled", "alpha_updates_enabled",
                   "background_update_downloads",
                   "show_tray_icon", "autostart",
                   "first_run", "hotkey_first_run_asked", "api_type_first_run_asked",
                   "switch_behavior")
_CONNECTION_GLOBAL = ("wpp_server", "wpp_ws_server", "wpp_api_key", "wpp_custom_api")


class AppSettings:
    def __init__(self, global_dir: str):
        self.global_dir = os.path.abspath(global_dir)
        self._path = os.path.join(self.global_dir, _FILE)
        os.makedirs(self.global_dir, exist_ok=True)

    def _read_unlocked(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _read(self) -> dict:
        with app_settings_lock(self.global_dir):
            return self._read_unlocked()

    def _write(self, data: dict) -> None:
        tmp = f"{self._path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def get(self, key: str) -> Any:
        if key not in _DEFAULTS:
            raise KeyError(f"{key!r} is not a global setting")
        value = self._read().get(key, _DEFAULTS[key])
        # A blank api key was written by an early multi-account build and makes
        # /api/<token>//generate-token double-slash → HTTP 404 on pairing.
        # Treat empty as "unset" so the real default is used instead.
        if key == "wpp_api_key" and not value:
            return _DEFAULTS[key]
        return value

    def set(self, key: str, value: Any) -> None:
        if key not in _DEFAULTS:
            raise KeyError(f"{key!r} is not a global setting")
        with app_settings_lock(self.global_dir):
            data = self._read_unlocked()
            data[key] = value
            self._write(data)

    def update(self, key: str, transform) -> None:
        """Atomic update for nested install-wide preferences across accounts."""
        if key not in _DEFAULTS:
            raise KeyError(f"{key!r} is not a global setting")
        with app_settings_lock(self.global_dir):
            data = self._read_unlocked()
            data[key] = transform(data.get(key, _DEFAULTS[key]))
            self._write(data)

    def all(self) -> dict:
        merged = dict(_DEFAULTS)
        merged.update({k: v for k, v in self._read().items() if k in _DEFAULTS})
        # Blank stored api key → fall back to the real default (see get()).
        if not merged.get("wpp_api_key"):
            merged["wpp_api_key"] = _DEFAULTS["wpp_api_key"]
        return merged


def split_legacy_settings(legacy: dict) -> tuple[dict, dict]:
    """Extract global keys from a flat legacy settings.json.

    Returns (global_dict, per_account_dict). The per-account dict is a copy of
    ``legacy`` with the global keys removed from its general/connection blocks.
    """
    import copy

    per = copy.deepcopy(legacy)
    glob: dict = {}

    general = per.get("general", {})
    for k in _GENERAL_GLOBAL:
        if k in general:
            glob[k] = general.pop(k)

    connection = per.get("connection", {})
    for k in _CONNECTION_GLOBAL:
        if k in connection:
            glob[k] = connection.pop(k)
    # Keep account-specific connection values such as wpp_port.
    if not connection:
        per.pop("connection", None)

    return glob, per
