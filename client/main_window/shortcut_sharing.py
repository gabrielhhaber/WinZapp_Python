"""Keyboard shortcuts: the install's, or this account's own, command by command.

The install's overrides live in global/app.json (core/shared_shortcuts.py).
Settings > Shortcuts lets an account keep a key of its own: a general check box
("use the same shortcuts in every account", on by default) and one per command
in its Change window, which follows the general one until set apart from it.

Three copies, so the many readers of settings[SECTION] never learn any of this:

- self._shortcut_mirror[SECTION]: the install's overrides as this account
  last took them, in memory only. It is what _persist_global_settings()
  reconciles with app.json, exactly as it did with settings[SECTION] before.
- settings[OWN_KEY]: this account's own overrides, saved with the account.
- settings[SECTION]: what this account answers to, composed from the two by
  _compose_shortcuts() whenever either changes.
"""
import copy
import logging

import wx

from core import shared_shortcuts as sharing
from core.keyboard_shortcuts import SECTION, sanitize_overrides
from core.shortcut_catalog import SHORTCUTS

# _pull_shared_shortcuts() retrying past another account's write of app.json:
# a save holds the lock for milliseconds, so 20 x 150 ms covers it with room.
_PULL_RETRIES = 20
_PULL_RETRY_MS = 150


class ShortcutSharingMixin:

    def _shared_shortcut_mirror(self):
        """{SECTION: the install's overrides}, or {} before startup took them
        (a legacy install with no shared file never does)."""
        mirror = getattr(self, "_shortcut_mirror", None)
        if mirror is None:
            mirror = self._shortcut_mirror = {}
        return mirror

    def _compose_shortcuts(self):
        """settings[SECTION] from the install's and this account's own. With
        no shared file the account's settings are the only copy: left alone."""
        mirror = self._shared_shortcut_mirror()
        if SECTION not in mirror:
            return
        settings = self.settings
        settings[SECTION] = sharing.effective_overrides(
            mirror[SECTION], sanitize_overrides(settings.get(sharing.OWN_KEY), SHORTCUTS),
            settings.get(sharing.SCOPE_KEY) or {}, sharing.shares_by_default(settings))

    def _share_keyboard_shortcuts(self):
        """The keyboard shortcuts are the install's (core/shared_shortcuts.py):
        take them from the shared file, handing it this account's own the
        first time, and reconcile them from then on like the install-wide keys
        of _apply_global_settings(). Its own step, so a failure here leaves the
        other install-wide keys in step and this account on the shortcuts it
        had."""
        try:
            general = self.settings.setdefault("general", {})
            shared = sharing.share_account_overrides(
                self._app_settings, self.settings, SHORTCUTS,
                lambda: sharing.catalog_translate(general.get("language")))
            self._shared_shortcut_mirror()[SECTION] = shared
            self._global_settings_snapshot[SECTION] = copy.deepcopy(shared)
            self._compose_shortcuts()
        except Exception:
            logging.exception("[settings] sharing the keyboard shortcuts failed (non-fatal)")

    def _pull_shared_shortcuts(self, attempt=0):
        """Take the shortcuts another account changed, as this window comes
        forward.

        A pull otherwise waits for this account's next save, and an account
        hidden in the tray saves little: switching to it with a key just
        changed in the other account found it still on the old one -- the
        very report the shared shortcuts fix. Without _save_lock (a sync can
        hold it for minutes) and without waiting on another process's writer:
        a change of this account's own not saved yet is left for the save to
        write (core.shared_shortcuts.shortcuts_to_pull()).

        Another account holding the shared file's lock at that instant (its
        own save, often the one the switch itself sets off) used to drop the
        pull, leaving this window on the old keys until its next save. It is
        tried again shortly instead, on the main thread and never blocking it,
        for up to _PULL_RETRIES times.
        """
        app = getattr(self, "_app_settings", None)
        snapshot = getattr(self, "_global_settings_snapshot", None)
        if app is None or not snapshot or SECTION not in snapshot:
            return
        from coord_locks import LockTimeout
        mirror = self._shared_shortcut_mirror()
        try:
            pulled = sharing.shortcuts_to_pull(mirror.get(SECTION), snapshot[SECTION],
                                               app.get_snapshot(SECTION))
            if pulled is None:
                return
            mirror[SECTION] = pulled
            snapshot[SECTION] = copy.deepcopy(pulled)
            self._compose_shortcuts()
            from ui.shortcut_bindings import refresh_shortcuts
            refresh_shortcuts(self)
        except LockTimeout:
            # Another account is writing the shared file right now.
            if attempt < _PULL_RETRIES and not getattr(self, "_shutting_down", False):
                wx.CallLater(_PULL_RETRY_MS, self._pull_shared_shortcuts, attempt + 1)
            else:
                logging.info("[settings] shared keyboard shortcuts still locked "
                             "after %d tries; the next save pulls them", attempt + 1)
        except Exception:
            logging.exception("[settings] pulling the shared keyboard shortcuts failed")

    def _route_imported_shortcuts(self):
        """An imported settings file set settings[SECTION] whole: send each
        command to the home its check box names, so the save that follows
        writes the install's part and keeps the account's. Under _save_lock."""
        mirror = self._shared_shortcut_mirror()
        if SECTION not in mirror:
            return
        settings = self.settings
        shared, own = sharing.route_effective(
            sanitize_overrides(settings.get(SECTION), SHORTCUTS), mirror[SECTION],
            sanitize_overrides(settings.get(sharing.OWN_KEY), SHORTCUTS),
            settings.get(sharing.SCOPE_KEY) or {}, sharing.shares_by_default(settings))
        mirror[SECTION] = shared
        settings[sharing.OWN_KEY] = own

    def shortcut_sharing_state(self):
        """What Settings > Shortcuts opens on: {"shared", "own", "scope",
        "default_shared"}, or None where there is nothing to share (no shared
        file: a legacy install, or startup could not read it)."""
        mirror = self._shared_shortcut_mirror()
        if SECTION not in mirror or getattr(self, "_app_settings", None) is None:
            return None
        settings = self.settings
        return {
            "shared": copy.deepcopy(mirror[SECTION]),
            "own": sanitize_overrides(settings.get(sharing.OWN_KEY), SHORTCUTS),
            "scope": {k: v for k, v in (settings.get(sharing.SCOPE_KEY) or {}).items()
                      if isinstance(v, bool)},
            "default_shared": sharing.shares_by_default(settings),
        }

    def choose_keyboard_shortcuts(self, state, shared_before):
        """Store what OK on Settings > Shortcuts left: `state` as
        shortcut_sharing_state() returns it, `shared_before` the install's
        overrides the page opened on.

        The install gets only the commands the page changed in its part
        (apply_changes()), written at once and atomically, so a change another
        account made since the page opened is kept. The account's own part and
        the check boxes are this account's settings; the caller saves them.
        """
        with self._save_lock:
            settings = self.settings
            settings.setdefault("general", {})[sharing.SHARED_FLAG] = bool(state["default_shared"])
            settings[sharing.SCOPE_KEY] = {k: v for k, v in state["scope"].items()
                                           if isinstance(v, bool)}
            settings[sharing.OWN_KEY] = sanitize_overrides(state["own"], SHORTCUTS)
            if state["shared"] != shared_before:
                written = self._app_settings.update(SECTION, lambda current: sanitize_overrides(
                    sharing.apply_changes(sanitize_overrides(current, SHORTCUTS),
                                          shared_before, state["shared"]), SHORTCUTS))
                self._shared_shortcut_mirror()[SECTION] = written
                snapshot = getattr(self, "_global_settings_snapshot", None)
                if snapshot is not None:
                    snapshot[SECTION] = copy.deepcopy(written)
            self._compose_shortcuts()
