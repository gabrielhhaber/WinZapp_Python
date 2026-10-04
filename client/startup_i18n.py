"""
Translations before MainWindow exists (client/startup_i18n.py)
==============================================================

``I18n`` reads its language from ``main_window.settings``, but a few messages
are shown before any MainWindow (or even any account) exists: an invalid
``--account`` argument, "no account to run", a crash in MainWindow's own
constructor, an unreadable per-account settings.json. The UI language is a
global setting (``global/app.json``, see app_settings.py), so it is known
from the very first line of startup — this module builds an ordinary ``I18n``
on top of it instead of each of those places hardcoding one language.

English is the fallback when no language has been chosen yet (first run) or
the stored one has no locale file.
"""

from __future__ import annotations

import logging

from core.i18n import I18n, LANGUAGE_NAMES

DEFAULT_STARTUP_LANGUAGE = "en-US"


class _SettingsHolder:
    """The one attribute I18n.get_language() reads from its main window."""

    def __init__(self, language: str):
        self.settings = {"general": {"language": language}}


def resolve_startup_language(stored) -> str:
    """The locale to use for *stored* (app.json's "language" value)."""
    if isinstance(stored, str) and stored in LANGUAGE_NAMES:
        return stored
    return DEFAULT_STARTUP_LANGUAGE


def startup_i18n(global_dir: "str | None" = None) -> I18n:
    """An I18n in the install-wide UI language. Never raises: any failure
    reading app.json falls back to DEFAULT_STARTUP_LANGUAGE, since these
    messages are what stands between the user and a silent exit."""
    stored = None
    try:
        if global_dir is None:
            import app_paths
            global_dir = app_paths.global_dir()
        from app_settings import AppSettings
        stored = AppSettings(global_dir).get("language")
    except Exception:
        logging.warning("[startup_i18n] could not read the global language", exc_info=True)
    i18n = I18n(_SettingsHolder(resolve_startup_language(stored)))
    i18n.get_language()
    return i18n
