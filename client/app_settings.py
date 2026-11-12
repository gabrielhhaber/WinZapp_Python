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

import copy
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
    "message_translation": {},
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
    # Download updates with no question and install them when the user exits
    # WinZapp, without starting it again (update_on_exit.py). Global like the
    # options above.
    "update_on_exit": False,
    # Offer every upstream WPPConnect Server release, not only the approved
    # ones (core/wpp_upstream.py). Global because api/ is shared.
    "wpp_all_updates": False,
    "show_tray_icon": True,
    "autostart": False,
    # Open every paired account (in the background) whenever WinZapp starts,
    # not only the one that would open. Edited by the account manager's check
    # box; has no legacy flat-settings equivalent, so it is not in
    # _GENERAL_GLOBAL.
    "connect_all_accounts": False,
    # The installed build registers itself for whatsapp: links once
    # (core/whatsapp_link_association.py); after that the Settings check box
    # is the only thing that changes it, so turning it off sticks.
    "whatsapp_links_auto_registered": False,
    # install-wide one-time setup prompts (asked once per install, NOT per
    # account — a new account must not re-ask autostart/hotkey/api-type).
    "first_run": True,
    "hotkey_first_run_asked": False,
    "api_type_first_run_asked": False,
    "neonize_offer_asked": False,   # the one-time "Neonize is available" notice
    "switch_behavior": "single",  # "single" (ocultar para a bandeja) ou "keep_open" (manter ambas abertas)
    # Shared WPPConnect host configuration. The port remains per-account.
    "wpp_server": "http://127.0.0.1",
    "wpp_ws_server": "ws://127.0.0.1",
    # Same default as client/core/utils.py DEFAULT_SETTINGS / settings_default.json
    # — a blank key makes /api/<token>//generate-token (double slash) → HTTP 404
    # on every new-account pairing under the multi-account flow.
    "wpp_api_key": "70733f08be1ed195bda1c31b6e135f5ebeb9fb8c6c28530a3a46e4093357b037",
    "wpp_custom_api": False,
    # The connection engine ("wppconnect", "neonize" or "custom",
    # core/connection_engine.py). Global like the keys above: one install, one
    # engine. "" means not stored yet; _apply_global_settings() fills it in
    # from the connection keys an older install already had.
    "wpp_engine": "",
    # Where the Whisper transcription models are downloaded to. Global rather
    # than per-account because the files themselves are shared — a single model
    # is up to 3 GB, which is exactly why model_store.default_models_dir() puts
    # them under global_dir() — so one account pointing somewhere else while
    # its siblings keep reading the old folder is not a state that means
    # anything. Empty means "the default folder": see
    # core.transcription.preferences.resolve_models_dir() for why the resolved
    # path is deliberately not written here. Key name mirrored by that module's
    # MODELS_DIR_SETTING, and a test pins the two spellings together.
    "transcription_models_dir": "",
    # Whisper models the user already had in folders of their own (another
    # program's download, the Hugging Face cache) and pointed WinZapp at
    # instead of downloading them again. Install-wide for the same reason as
    # the folder above: the files belong to the machine, not to an account.
    # A list of plain dicts, read and written only by
    # core.transcription.external_models, whose EXTERNAL_MODELS_SETTING
    # mirrors this key name (pinned by a test).
    "transcription_external_models": [],
    # Folders of CUDA libraries / whisper.cpp builds downloaded earlier (another
    # copy of WinZapp's cuda_runtime and whisper_cpp_runtime folders) that
    # WinZapp reuses instead of downloading them again. Install-wide like the
    # models: the files belong to the machine. "" means none. Read and written
    # through core.transcription.reused_runtimes, whose CUDA_SETTING and
    # WHISPER_CPP_SETTING mirror these names (pinned by a test).
    "transcription_cuda_extra_dir": "",
    "transcription_whisper_cpp_extra_dir": "",
}

# Which legacy general.* keys are global (the rest stay per-account).
_GENERAL_GLOBAL = ("language", "updates_enabled", "alpha_updates_enabled",
                   "background_update_downloads", "update_on_exit", "wpp_all_updates",
                   "show_tray_icon", "autostart",
                   "first_run", "hotkey_first_run_asked", "api_type_first_run_asked",
                   "neonize_offer_asked",
                   "switch_behavior")
_CONNECTION_GLOBAL = ("wpp_server", "wpp_ws_server", "wpp_api_key", "wpp_custom_api",
                      "wpp_engine")


def _default(key: str) -> Any:
    return copy.deepcopy(_DEFAULTS[key])


class AppSettings:
    def __init__(self, global_dir: str):
        self.global_dir = os.path.abspath(global_dir)
        self._path = os.path.join(self.global_dir, _FILE)
        os.makedirs(self.global_dir, exist_ok=True)

    def _read_unlocked(self, strict: bool = False) -> dict:
        """The stored values; {} for a file that is absent or unreadable.

        `strict` keeps "absent" (nothing stored yet: the defaults are the
        truth) apart from "there and unreadable", which then raises OSError or
        ValueError instead of reading as the defaults.
        """
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            if strict:
                raise
            return {}
        if isinstance(data, dict):
            return data
        if strict:
            raise ValueError("app.json does not hold an object")
        return {}

    def _read(self, strict: bool = False) -> dict:
        with app_settings_lock(self.global_dir):
            return self._read_unlocked(strict)

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
        # A copy, here and in update()/all(): the default of a list setting is
        # one object shared by the whole process, and a caller appending to
        # what it was handed would change the default for everybody after it.
        value = self._read().get(key, _default(key))
        # A blank api key was written by an early multi-account build and makes
        # /api/<token>//generate-token double-slash → HTTP 404 on pairing.
        # Treat empty as "unset" so the real default is used instead.
        if key == "wpp_api_key" and not value:
            return _DEFAULTS[key]
        return value

    def get_strict(self, key: str) -> Any:
        """get(), for a caller that acts on what is *missing* from the value.

        get() reads a file it could not open or parse as the defaults, which is
        right for a toggle and wrong for a list whose absent entries mean
        something: an empty list of external transcription models read during
        a moment the file was unreadable would have a stored choice of one of
        them rewritten to "automatic" for good. Raises OSError or ValueError
        for a file that is there and unreadable (and LockTimeout, like get());
        an absent file is the defaults, as always.
        """
        if key not in _DEFAULTS:
            raise KeyError(f"{key!r} is not a global setting")
        return self._read(strict=True).get(key, _default(key))

    def get_snapshot(self, key: str) -> Any:
        """Read a setting without waiting on another process's writer."""
        if key not in _DEFAULTS:
            raise KeyError(f"{key!r} is not a global setting")
        with app_settings_lock(self.global_dir, timeout=0):
            return self._read_unlocked().get(key, _default(key))

    def set(self, key: str, value: Any) -> None:
        if key not in _DEFAULTS:
            raise KeyError(f"{key!r} is not a global setting")
        with app_settings_lock(self.global_dir):
            data = self._read_unlocked()
            data[key] = value
            self._write(data)

    def update(self, key: str, change) -> Any:
        """Replace `key` with `change(current value)`, as one locked step.

        The atomic update for the nested install-wide values several account
        processes write to (the `ai_media` preferences, the list of external
        transcription models). get() followed by set() is two acquisitions of
        the lock with a gap between them, which is fine for a scalar the user
        sets from one dialog and wrong for a list two account processes can
        each append to: both read the same list, each writes back its own
        addition, and one of the two is silently lost. Returns what was
        written.
        """
        if key not in _DEFAULTS:
            raise KeyError(f"{key!r} is not a global setting")
        with app_settings_lock(self.global_dir):
            data = self._read_unlocked()
            value = change(data.get(key, _default(key)))
            data[key] = value
            self._write(data)
            return value

    def all(self) -> dict:
        merged = copy.deepcopy(_DEFAULTS)
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
