"""SettingsMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import json
import logging
import os
import shutil
import sys
import threading
import uuid
import wx
from core.sound_system import (
    DEFAULT_PACK_ID,
    SOUND_EVENTS,
    Sound,
    discover_sound_packs,
    load_sound,
    resolve_alert_tone_path,
    resolve_sound_event_path,
)
from core.utils import (
    DEFAULT_SETTINGS,
    backfill_missing_defaults,
    migrate_call_exclusive_mode_split,
    migrate_spell_check_mode,
    migrate_typing_row_default,
    migrate_voice_message_mode_default,
    migrate_voice_messages_media_types,
    migrate_wpp_reinstall_notice,
)
from core.i18n import I18n
from version import __version__
from core.settings_transfer import connection_runtime as _connection_runtime
from main_window.win32_helpers import _vk_mod_to_str
from app_paths import (
    data_path,
    resource_path,
)
from core.audio_devices import (
    find_input_device_index,
    repair_stored_input_device_names,
    test_input_device,
)
from traceback import format_exc
from core.quiet_hours import is_quiet_hours_active


class SettingsMixin:
    """First-run checks, settings load/save/migrate, settings export/import, live
    settings application and sounds.
    """

    # ── Language selection ────────────────────────────────────────────────────

    def _ensure_language_selected(self):
        """
        Show the language-selection dialog if no language has been stored yet
        in settings.  On Cancel the application exits immediately.
        """
        lang_already_set = bool(
            self.settings.get("general", {}).get("language")
        )
        if lang_already_set:
            return

        from ui.dialogs.language_dialog import LanguageSelectionDialog
        dlg    = LanguageSelectionDialog(parent=None)
        result = dlg.ShowModal()
        lang   = dlg.selected_language
        dlg.Destroy()

        if result != wx.ID_OK:
            sys.exit(0)

        self.settings.setdefault("general", {})["language"] = lang
        self.save_settings()

    def _check_api_type_first_run(self):
        """
        Check if we need to ask the user to choose between local and custom/remote API on first launch.
        """
        if self.background_mode:
            return

        gen = self.settings.get("general", {})
        if gen.get("api_type_first_run_asked", False):
            return

        msg = self.i18n.t("api_type_ask_message")
        title = self.i18n.t("api_type_ask_title")

        result = wx.MessageBox(
            msg,
            title,
            wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION,
        )

        if result == wx.YES:
            # User wants local API (default)
            self.settings.setdefault("connection", {})["wpp_custom_api"] = False
            self.wpp_custom_api = False
            self.settings.setdefault("general", {})["api_type_first_run_asked"] = True
            self.save_settings()
        elif result == wx.NO:
            # User wants to specify a custom/remote API
            self.settings.setdefault("connection", {})["wpp_custom_api"] = True
            self.wpp_custom_api = True
            self.save_settings()

            # Open settings dialog on the Connection tab (index 4)
            from ui.dialogs.settings_dialog import SettingsDialog
            dlg = SettingsDialog(self)
            dlg._notebook.SetSelection(4)
            settings_res = dlg.ShowModal()
            dlg.Destroy()

            if settings_res == wx.ID_OK:
                # Successfully configured! Mark as asked.
                self.settings.setdefault("general", {})["api_type_first_run_asked"] = True
                self.save_settings()
            else:
                # User cancelled or closed settings dialog. Roll back and exit.
                self.settings.setdefault("connection", {})["wpp_custom_api"] = False
                self.wpp_custom_api = False
                self.save_settings()
                sys.exit(0)
        else:
            # User cancelled or closed the question box. Exit.
            sys.exit(0)

    def _check_first_run(self):
        """
        Show the autostart-offer dialog exactly once per installation.
        The ``first_run`` flag in settings is cleared immediately to prevent
        re-showing on a subsequent launch if the app crashes after this point.
        """
        if not self.settings.get("general", {}).get("first_run", True):
            return
        # Mark as done before showing the dialog
        self.settings.setdefault("general", {})["first_run"] = False
        self.save_settings()

        result = wx.MessageBox(
            self.i18n.t("autostart_ask_message"),
            self.i18n.t("autostart_ask_title"),
            wx.YES_NO | wx.ICON_QUESTION,
        )
        if result == wx.YES:
            self._apply_autostart(enable=True)
        else:
            self.settings.setdefault("general", {})["autostart"] = False
            self.save_settings()

    def _check_hotkey_first_run(self):
        """
        Show a one-time dialog offering the user a global hotkey to open WinZapp
        from any application.  Guards on ``hotkey_first_run_asked`` so it only
        shows once per installation, right after the autostart prompt.

        The chosen (vk, mod) pair is written to settings immediately; the
        _HotkeyManager is created later in init_UI via _apply_global_hotkey().
        """
        gen = self.settings.get("general", {})
        if gen.get("hotkey_first_run_asked", False):
            return
        # Already has a hotkey configured — mark done without asking again.
        if gen.get("global_hotkey"):
            self.settings.setdefault("general", {})["hotkey_first_run_asked"] = True
            self.save_settings()
            return

        self.settings.setdefault("general", {})["hotkey_first_run_asked"] = True
        self.save_settings()

        from ui.dialogs.settings_dialog import _HotkeyCapture

        dlg = wx.Dialog(
            None,
            title=self.i18n.t("hotkey_first_run_title"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)

        msg_ctrl = wx.StaticText(dlg, label=self.i18n.t("hotkey_first_run_message"))
        msg_ctrl.Wrap(480)
        sizer.Add(msg_ctrl, 0, wx.ALL, 15)

        capture = _HotkeyCapture(
            dlg,
            accessible_name=self.i18n.t("global_hotkey_label"),
        )
        capture.SetHint(self.i18n.t("global_hotkey_hint"))
        sizer.Add(capture, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 15)

        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn   = wx.Button(dlg, wx.ID_OK,     self.i18n.t("ok"))
        skip_btn = wx.Button(dlg, wx.ID_CANCEL, self.i18n.t("hotkey_first_run_skip"))
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(skip_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)

        dlg.SetSizer(sizer)
        sizer.Fit(dlg)
        dlg.CenterOnScreen()

        result = dlg.ShowModal()
        vk  = capture._vk
        mod = capture._mod
        dlg.Destroy()

        if result == wx.ID_OK and vk:
            self.settings.setdefault("general", {})["global_hotkey"] = {"vk": vk, "mod": mod}
            self.save_settings()
            wx.MessageBox(
                self.i18n.t("hotkey_first_run_success").format(hotkey=_vk_mod_to_str(vk, mod)),
                self.i18n.t("autostart_success_title"),
                wx.OK | wx.ICON_INFORMATION,
            )

    # ── WPPConnect reinstall notice (2.0) ────────────────────────────────────

    def _show_wpp_reinstall_notice_if_pending(self):
        """One-time recommendation to reinstall WPPConnect, for accounts that
        predate 2.0 (see migrate_wpp_reinstall_notice() in core/utils.py).

        Scheduled from __init__ via wx.CallLater, well after the main window
        (and its chat-navigation list) is visible, and never in
        background_mode — same shape as _announce_previous_update_failure().
        Checking the flag first makes every later launch, after the first,
        free: no dialog was ever built, no i18n lookup happened.

        Whatever the user answers, the pending flag is cleared and saved
        immediately, so this can only ever fire once per install.
        """
        general = self.settings.get("general", {})
        if not general.get("wpp_reinstall_notice_pending", False):
            return

        # Pending means only "this settings.json predates 2.0", not "this
        # WPPConnect lacks calls". An install reinstalled since — every alpha
        # tester was asked to do it by hand — already carries the catalogue the
        # notice offers, and was being told it may be missing calls anyway
        # (reported 2026-09-27, pinned 2.3000.1047835881-alpha of 430). Its
        # catalogue answers the question; unreadable counts as "ask".
        from core.wa_version_catalogue import catalogue_supports_calls, newest_build, read_catalogue
        catalogue = read_catalogue(resource_path(
            "api", "node_modules", "@wppconnect", "wa-version", "versions.json"))
        if catalogue_supports_calls(catalogue):
            logging.info("[wpp_reinstall_notice] not shown: the installed catalogue "
                         "already reaches %s.", newest_build(catalogue))
            self.settings.setdefault("general", {})["wpp_reinstall_notice_pending"] = False
            self.save_settings()
            return

        message = self.i18n.t("wpp_reinstall_notice_message")
        self.output(message, interrupt=False)
        result = wx.MessageBox(
            message,
            self.i18n.t("wpp_reinstall_notice_title"),
            wx.YES_NO | wx.ICON_QUESTION,
        )

        self.settings.setdefault("general", {})["wpp_reinstall_notice_pending"] = False
        self.save_settings()

        if result == wx.YES:
            self._on_force_reinstall_wpp(None)

    def _apply_autostart(self, enable: bool):
        """
        Enable or disable the Windows Run registry entry for WinZapp.

        On success with ``enable=True``: shows a confirmation dialog.
        On failure: shows an error dialog and stores ``autostart=False``.
        Called from ``_check_first_run()`` and from the Settings dialog.
        """
        from autostart import enable_autostart, disable_autostart
        if enable:
            try:
                enable_autostart()
                self.settings.setdefault("general", {})["autostart"] = True
                self.save_settings()
                wx.MessageBox(
                    self.i18n.t("autostart_success_message"),
                    self.i18n.t("autostart_success_title"),
                    wx.OK | wx.ICON_INFORMATION,
                )
            except Exception as exc:
                self.settings.setdefault("general", {})["autostart"] = False
                self.save_settings()
                wx.MessageBox(
                    f"{self.i18n.t('autostart_error_message')}\n\n{exc}",
                    self.i18n.t("error").format(app_name=self.app_name),
                    wx.OK | wx.ICON_ERROR,
                )
        else:
            disable_autostart()
            self.settings.setdefault("general", {})["autostart"] = False
            self.save_settings()

    def _sync_autostart_registry(self):
        """
        Synchronize the Windows Run registry key with the current settings.
        Only runs on Windows. If autostart setting is True, ensures the registry key exists.
        If autostart setting is False (and it's not the first run), ensures the key is removed.
        """
        import sys
        if sys.platform != "win32":
            return

        if self.settings.get("general", {}).get("first_run", True):
            return

        try:
            from autostart import is_autostart_enabled, enable_autostart, disable_autostart
            setting_enabled = self.settings.get("general", {}).get("autostart", False)
            registry_enabled = is_autostart_enabled()

            if setting_enabled and not registry_enabled:
                logging.info("Startup: Autostart is enabled in settings but missing in registry. Enabling...")
                enable_autostart()
            elif not setting_enabled and registry_enabled:
                logging.info("Startup: Autostart is disabled in settings but present in registry. Disabling...")
                disable_autostart()
        except Exception as e:
            logging.error("Startup: Failed to sync autostart registry key: %s", e)

    # ── Quick tip ─────────────────────────────────────────────────────────────

    def _check_quick_tip(self):
        """
        Show the "quick tip" (F1 shortcut hint) once after the user's first
        successful pairing.  Guarded by the ``quick_tip_shown`` setting so it
        never shows twice.
        """
        if self.settings.get("general", {}).get("quick_tip_shown", False):
            return
        self.settings.setdefault("general", {})["quick_tip_shown"] = True
        self.save_settings()
        wx.MessageBox(
            self.i18n.t("quick_tip_message"),
            self.i18n.t("quick_tip_title"),
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

    # ── Terms of service ─────────────────────────────────────────────────────

    def _refuse_network_install_location(self):
        """Stop with a clear explanation when WinZapp runs from a UNC path.

        Before anything is installed: from //server/share npm install fails
        with a page of output that explains nothing (core/install_location.py,
        which also says why a mapped drive letter is not refused). The common
        way to land here is a virtual machine — Parallels on a Mac maps
        Windows' Downloads to the Mac's own folder — so the message names the
        path and says exactly what to do instead.
        """
        from app_paths import global_dir
        from core.install_location import is_unc_path
        location = global_dir()
        if not is_unc_path(location):
            return
        # No path in the log: it carries the Windows user name and the share,
        # and these logs are pasted into public groups.
        logging.warning(
            "[startup] WinZapp is running from a network folder (UNC path) — "
            "refusing to start: npm install cannot run on a UNC path.",
        )
        if not self.background_mode:
            wx.MessageBox(
                self.i18n.t("network_install_location_message").format(
                    path=os.path.dirname(os.path.dirname(location))),
                self.i18n.t("network_install_location_title"),
                wx.OK | wx.ICON_ERROR,
            )
        sys.exit(0)

    def _check_terms_acceptance(self):
        """
        Show the terms-of-service dialog exactly once.
        If the user declines, the application exits immediately.
        """
        if self.settings.get("general", {}).get("terms_alert_displayed", False):
            return

        dlg = wx.Dialog(
            None,
            title=self.i18n.t("terms_title"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)

        msg_ctrl = wx.StaticText(dlg, label=self.i18n.t("terms_message"))
        msg_ctrl.Wrap(480)
        sizer.Add(msg_ctrl, 0, wx.ALL, 15)

        btn_sizer = wx.StdDialogButtonSizer()
        accept_btn = wx.Button(dlg, wx.ID_OK,     self.i18n.t("terms_accept"))
        decline_btn = wx.Button(dlg, wx.ID_CANCEL, self.i18n.t("terms_decline"))
        btn_sizer.AddButton(accept_btn)
        btn_sizer.AddButton(decline_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)

        dlg.SetSizer(sizer)
        sizer.Fit(dlg)
        dlg.CenterOnScreen()

        result = dlg.ShowModal()
        dlg.Destroy()

        if result == wx.ID_OK:
            self.settings.setdefault("general", {})["terms_alert_displayed"] = True
            self.save_settings()
        else:
            sys.exit(0)

    def load_settings(self):
        settings_file = data_path("settings.json")
        default_file = resource_path("data", "settings_default.json")
        fallback_dict = DEFAULT_SETTINGS

        # Bootstrap settings.json if missing
        if not os.path.isfile(settings_file):
            os.makedirs(os.path.dirname(settings_file), exist_ok=True)
            if os.path.isfile(default_file):
                try:
                    shutil.copy2(default_file, settings_file)
                except Exception:
                    pass
            if not os.path.isfile(settings_file):
                try:
                    with open(settings_file, "w", encoding="utf-8") as f:
                        json.dump(fallback_dict, f, indent=4)
                except Exception:
                    pass
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                self.settings = json.load(f)
        except Exception:
            # If load still fails (e.g. corrupt settings.json), reset to defaults
            # rather than crashing the app — but tell the user, since this
            # silently discards any customization (language, hotkeys, audio
            # devices, etc.) they had saved.
            logging.error("[load_settings] settings.json unreadable, resetting to defaults", exc_info=True)
            self.settings = json.loads(json.dumps(fallback_dict))
            try:
                with open(settings_file, "w", encoding="utf-8") as f:
                    json.dump(self.settings, f, indent=4)
            except Exception:
                pass
            if hasattr(self, "i18n"):
                msg   = self.i18n.t("settings_load_failed")
                title = self.i18n.t("error").format(app_name=self.app_name)
            else:
                from startup_i18n import startup_i18n
                _i18n = startup_i18n()
                msg   = _i18n.t("settings_load_failed")
                title = _i18n.t("error").format(app_name=self.app_name)
            if hasattr(self, "error_sound"):
                self.error_sound.play()
            if not self.background_mode:
                wx.CallAfter(wx.MessageBox, f"{msg}\n{format_exc()}", title, wx.OK | wx.ICON_WARNING)


        self._migrate_settings()

        # Backfill any missing default keys/sections so settings are always
        # complete. Strictly AFTER _migrate_settings(): backfilling first
        # creates the post-rename section, which makes the migration's
        # `"ui" in settings and "user_interface" not in settings` condition
        # False — the legacy block is then orphaned and every UI preference an
        # older install had set silently reverts to defaults.
        #
        # Persisted through save_settings() rather than a bare json.dump: that
        # is what takes self._save_lock, and WebSocket handlers and the
        # debounce timer write this same file concurrently.
        if isinstance(self.settings, dict) and backfill_missing_defaults(
            self.settings, fallback_dict
        ):
            self.save_settings()
        self._apply_global_settings()

    def _apply_global_settings(self):
        """Overlay install-wide settings from global/app.json onto self.settings
        (plan Zad 2.3b). Keeps the hundreds of existing self.settings[...] reads
        working unchanged while language/updates/tray/connection are SHARED
        across accounts. Best-effort: no global_dir (legacy) -> leave as-is."""
        gd = getattr(self, "global_dir", None)
        if not gd:
            return
        try:
            from app_settings import AppSettings, _GENERAL_GLOBAL, _CONNECTION_GLOBAL
            app = AppSettings(gd)
            self._app_settings = app
            general = self.settings.setdefault("general", {})
            # One-time backfill (GPT-safe): the install-wide "asked once" flags
            # were per-account before this version. If global app.json doesn't
            # carry a key yet but THIS account already has a value, seed global
            # from it — so an account that already finished first-run setup does
            # not get re-asked once the flag moves to the shared file.
            raw_global = app._read()
            for k in _GENERAL_GLOBAL:
                if k not in raw_global and k in general:
                    app.set(k, general[k])
            for k in _GENERAL_GLOBAL:
                general[k] = app.get(k)
            connection = self.settings.setdefault("connection", {})
            for k in _CONNECTION_GLOBAL:
                connection[k] = app.get(k)
            # What this account last saw in the shared file — the baseline
            # _persist_global_settings() tells this account's own changes from
            # a copy that has merely gone stale. See its docstring.
            self._global_settings_snapshot = {
                **{k: general[k] for k in _GENERAL_GLOBAL},
                **{k: connection[k] for k in _CONNECTION_GLOBAL},
            }
        except Exception:
            logging.exception("[settings] applying global app.json failed (non-fatal)")

    def _persist_global_settings(self, explicit=()):
        """Reconcile the global keys of self.settings with global/app.json
        (plan Zad 2.3b): write the ones this account changed, and take the
        shared value for every other one.

        By difference, not wholesale. This runs on every save_settings(), and
        most of those have nothing to do with settings (an audio speed, a
        recent reaction, the session token). Copying every global key over on
        each of them wrote this account's copy — as old as its last startup —
        over whatever another account had chosen since, so the last account to
        save anything at all won every install-wide setting at once: choose
        "keep both open" in one account, let another store a reaction, and the
        choice was gone. So a key is written only when its local value differs
        from `_global_settings_snapshot`, the value this account last took from
        the shared file; a key that did not change here is pulled instead, and
        is how an account comes to see a change made in another. Both update
        the snapshot. Two accounts changing the same key: the later write wins.

        `explicit` names keys that were just chosen and must be written even
        when they equal the snapshot — a settings import, which can set a key
        back to the value this account started with while another account has
        changed it since; by difference that would read as "unchanged" and be
        pulled over.

        A key the shared file does not hold yet is seeded from the local copy,
        as every save used to do: nobody chose anything there to overwrite.
        One lock around the read and the writes, so another account's change
        cannot land between them and be pulled back out as this one's.

        The pull does not hold this window's own writers off, though, unless
        they take `_save_lock` too. The Settings dialog used not to — its OK
        could land between the comparison and the pull, seen by review as a
        background save reading the file while OK set the language, pulling
        the old one over it, and OK's own save then finding "equal to the
        snapshot" and writing nothing. It now goes through
        choose_global_settings(), under the lock and as `explicit`; the
        first-run questions still write the local copy on their own, and for
        them the check below remains. So a key is pulled only while it still
        holds the value compared; one that moved meanwhile keeps the new value,
        and its snapshot entry becomes the value compared rather than the
        file's. The new value differs from the compared one by definition, so
        the next save always reads it as a change and writes it; a snapshot
        taken from the file would lose it whenever the two happened to agree,
        e.g. OK choosing back the value this save had just pulled away from.
        """
        app = getattr(self, "_app_settings", None)
        if app is None:
            return
        try:
            from app_settings import _GENERAL_GLOBAL, _CONNECTION_GLOBAL
            from coord_locks import app_settings_lock
            # A key the snapshot does not know (startup failed half-way) is
            # never treated as changed: with no baseline, a stale copy and a
            # new choice look the same, and writing would be the old bug.
            snapshot = getattr(self, "_global_settings_snapshot", None) or {}
            sections = (
                (self.settings.setdefault("general", {}), _GENERAL_GLOBAL),
                (self.settings.setdefault("connection", {}), _CONNECTION_GLOBAL),
            )
            compared = {}
            with app_settings_lock(app.global_dir):
                stored = app._read()
                for section, keys in sections:
                    for k in keys:
                        if k not in section:
                            continue
                        compared[k] = section[k]
                        changed = k in explicit or (k in snapshot and section[k] != snapshot[k])
                        if changed or k not in stored:
                            app.set(k, section[k])
                current = app.all()
            new_snapshot = {}
            pulled = {}
            for section, keys in sections:
                for k in keys:
                    if k in compared and section.get(k) != compared[k]:
                        new_snapshot[k] = compared[k]
                        continue
                    if k in section and section[k] != current[k]:
                        pulled[k] = current[k]
                    section[k] = current[k]
                    new_snapshot[k] = current[k]
            self._global_settings_snapshot = new_snapshot
            if pulled:
                # This runs on whatever thread saved, holding _save_lock;
                # applying touches wx, so it waits for the main thread.
                wx.CallAfter(self._apply_pulled_global_settings, pulled)
        except Exception:
            logging.exception("[settings] persisting global app.json failed (non-fatal)")

    def choose_global_settings(self, choices):
        """Store install-wide settings the user just chose: {key: value}.

        The Settings dialog's way in, and the reason it has one. By difference
        alone a choice is lost whenever it happens to equal this account's
        snapshot: the dialog opens on "pt-BR", another account sets "en-US", a
        background save here pulls it (snapshot "en-US"), the other account
        sets "es-ES", and the user picks "en-US" and presses OK -- equal to
        the snapshot, so the save reads "unchanged" and pulls "es-ES" over the
        newest choice of all, without a word. A choice is an explicit write,
        exactly like a settings import, so it goes through the same
        `explicit` of the same _persist_global_settings(): one write path to
        the shared file, not a second one to keep in step with it.

        And under _save_lock, the lock every _persist_global_settings() call
        runs under (save_settings(), the import). The dialog used to write the
        local copy with no lock at all, so its write could land between a
        background save comparing a key and pulling it -- the gap the
        `compared` check in _persist_global_settings() narrows but, being two
        separate steps, cannot close. Holding the lock closes it for the
        dialog; the check stays for the first-run questions, which still write
        the local copy on their own.
        """
        if not choices:
            return
        from app_settings import _CONNECTION_GLOBAL
        with self._save_lock:
            for key, value in choices.items():
                section = "connection" if key in _CONNECTION_GLOBAL else "general"
                self.settings.setdefault(section, {})[key] = value
            self._persist_global_settings(explicit=tuple(choices))

    def install_wide_values(self):
        """{"general": {...}, "connection": {...}} holding every install-wide
        key as the shared file has it now -- what the Settings dialog shows.

        The local copy is only as fresh as this account's last save, so the
        dialog loading from it showed values another account had changed
        since (the account-switch radio alone read the shared file). Read
        here, not reconciled: bringing the copy up to date takes _save_lock,
        which save_data() holds across a whole database write during a sync
        (up to database_bridge's 120 s bulk timeout) -- opening Settings
        would freeze the window for as long. The dialog does not need the
        copy current: what the user changes goes through
        choose_global_settings() as explicit, and the next save pulls the
        rest. Only the shared file's own lock is taken, by AppSettings.all().

        Same shape as self.settings so the dialog reads it the same way; the
        local copy stands in on a legacy install with no shared file, or if
        the file cannot be read.
        """
        from app_settings import _GENERAL_GLOBAL, _CONNECTION_GLOBAL
        shared = None
        app = getattr(self, "_app_settings", None)
        if app is not None:
            try:
                shared = app.all()
            except Exception:
                logging.exception("[settings] reading global app.json failed; "
                                  "showing this account's copy")
        view = {}
        for name, keys in (("general", _GENERAL_GLOBAL), ("connection", _CONNECTION_GLOBAL)):
            local = self.settings.get(name) or {}
            source = shared if shared is not None else local
            view[name] = {k: source[k] for k in keys if k in source}
        return view

    def _apply_pulled_global_settings(self, pulled):
        """Make install-wide values another account chose take effect here,
        where that is safe to do without anyone asking.

        Decided key by key; the tray icon and the language need work now:

        - show_tray_icon: applied (_sync_tray_icon_with_setting()). The same
          setting already switches Windows toasts off the moment it is pulled
          (notification_manager.should_speak_background_message() and the
          background notification path read it live), so an icon left up
          would be a window whose toasts are off while its icon says
          otherwise.
        - language: applied to the whole window, as the Settings dialog's OK
          does (product decision: an install has one interface language) --
          but only while the window is not the active one; see
          _apply_pending_language_switch(). core.i18n makes every helper
          follow the window's language, so nothing switches on its own first.
        - wpp_server / wpp_ws_server / wpp_api_key / wpp_custom_api: NOT
          applied. They say where this process's session lives; moving it
          mid-session abandons the Node it runs. The attributes (self.wpp_*)
          stay the source of truth for this process until the next start.
        - updates_enabled / alpha_updates_enabled / switch_behavior /
          autostart / the first-run flags: nothing to do. Each is read where
          it is used, from this copy or the shared file, or mirrors state
          (the Run entry) that is already shared.
        """
        # Key names only: the connection values are an address and a key.
        logging.info("[settings] install-wide value(s) changed by another account: %s",
                     ", ".join(sorted(pulled)))
        # The teardown's own last saves still pull, and the UI thread keeps
        # pumping through _stop_wpp_server(): a tray icon created now, after
        # _perform_shutdown() removed this window's, is never removed again --
        # os._exit leaves it in the notification area.
        if getattr(self, "_shutting_down", False):
            return
        if "show_tray_icon" in pulled:
            try:
                self._sync_tray_icon_with_setting()
            except Exception:
                logging.exception("[settings] applying the pulled tray setting failed")
        if "language" in pulled:
            self._pending_language_switch = True
            self._apply_pending_language_switch()

    def _apply_pending_language_switch(self):
        """Switch this whole window to the configured language, if another
        account changed it -- now when the window is not the active one,
        otherwise the moment it stops being active.

        apply_language_changes() renames every control and rewrites the
        rendered rows, and NVDA speaks a name change of the object that has
        focus (docs/traps/screen-reader-speech.md). With the window active,
        that is the focused row or button read out again in another language
        with nobody having asked. Hidden, minimised, behind another program
        or behind one of WinZapp's own dialogs, the focus is elsewhere and the
        repaint is silent; populate_messages(preserve_focus=True) only calls
        SetFocus when the list already had it, so it cannot activate the
        window either. _on_window_activate() calls this again on
        deactivation.

        Not while a call window is on screen, either. The voice-call frame and
        the incoming-call popup are top-level windows of their own, so during
        a call the main window is inactive while one of them holds the focus
        -- and apply_language_changes() relabels them too
        (_refresh_call_language_surfaces()): NVDA would read the focused
        Mute or Answer button again in the other language, and a ringing
        popup's Alt+letter shortcuts would move under the user's fingers.
        Retried (_retry_pending_language_switch()) whenever one of those
        guards may have stopped holding: the main window losing the focus, and
        a call window closing -- the call frame hidden by
        _sync_voice_call_bar(), the popup closed or forgotten. Closing a call
        window can hand the activation to another program rather than to the
        main window, which then sees no activation change at all; without the
        second trigger the switch would wait for the user to come in and out
        of the window again. If the main window did get it, the Hide() that
        gave it is synchronous, so it is already active by the time this runs.

        A language equal to what the window already shows (changed and
        changed back, or chosen in this window's dialog meanwhile) costs
        nothing.
        """
        if not getattr(self, "_pending_language_switch", False):
            return
        if getattr(self, "_main_window_active", False) or self._call_window_on_screen():
            return
        self._pending_language_switch = False
        configured = self.settings.get("general", {}).get("language", "pt-BR")
        if configured == self.i18n.language:
            return
        try:
            from core.i18n import I18n
            I18n.invalidate_cache()
            self.i18n.get_language()
            self.apply_language_changes()
            logging.info("[settings] interface language switched to %s "
                         "(chosen in another account)", configured)
        except Exception:
            logging.exception("[settings] switching to the pulled language failed")

    def _retry_pending_language_switch(self):
        """Try a deferred language switch again once the current event is
        done -- see _apply_pending_language_switch() for who calls this."""
        if getattr(self, "_pending_language_switch", False):
            wx.CallAfter(self._apply_pending_language_switch)

    def _call_window_on_screen(self):
        """Whether a call window of this process is showing: a ringing
        incoming-call popup, or the voice-call frame (created once at startup,
        shown only while a call is up). See _apply_pending_language_switch()."""
        if getattr(self, "_incoming_call_dialogs", None):
            return True
        window = getattr(self, "voice_call_window", None)
        if window is None:
            return False
        try:
            return bool(window.IsShown())
        except RuntimeError:  # the C++ window is already gone
            return False

    def _migrate_settings(self):
        """Migrate settings from old section names to current ones."""
        changed = False
        # audio_default_speed: general → audio_playback
        if "audio_default_speed" in self.settings.get("general", {}):
            speed = self.settings["general"].pop("audio_default_speed")
            self.settings.setdefault("audio_playback", {})["audio_default_speed"] = speed
            changed = True
        # ui → user_interface
        if "ui" in self.settings and "user_interface" not in self.settings:
            self.settings["user_interface"] = self.settings.pop("ui")
            changed = True
        # sound_events: flat {event_key: {...}} (pre-soundpack) → nested
        # {pack_id: {event_key: {...}}}. Sound.play() only ever reads the
        # nested shape (settings["sound_events"][pack_id][event_key]); an
        # install that had "sound_events" saved before soundpacks existed
        # still has the flat shape on disk, so every lookup under a real
        # pack_id came back empty and silently defaulted "enabled" to True —
        # any event a user had disabled before that restructuring (e.g. the
        # startup sound) started playing again despite still showing as
        # "disabled" in Settings, because nothing ever migrated it into the
        # new nested shape. Detect the old shape by an unmistakable signal:
        # a top-level key that is itself a known event name (pack ids never
        # collide with those) rather than a soundpack id.
        events = self.settings.get("sound_events")
        if isinstance(events, dict):
            event_keys = {key for key, _ in SOUND_EVENTS}
            if event_keys & events.keys():
                self.settings["sound_events"] = {DEFAULT_PACK_ID: events}
                changed = True
        # "audios" split into "audios" + "voice_messages": a list saved before
        # the split has to inherit the old box's state, or every existing user
        # silently loses voice notes from the Media tab and the auto-download.
        # Runs here rather than at each read site because it can only be done
        # once — see migrate_voice_messages_media_types().
        if migrate_voice_messages_media_types(self.settings):
            changed = True
        # call_audio_devices.exclusive_mode split into exclusive_input +
        # exclusive_output: one checkbox governed both directions, and holding
        # the SPEAKER exclusively silences the screen reader for the whole
        # call while holding the microphone does not. An install that ticked
        # the old box keeps it for the MICROPHONE only: that box never had any
        # effect, so carrying it onto the speaker would silence the screen
        # reader on the first call after updating, with no warning ever seen.
        # One shot, with its own flag — see migrate_call_exclusive_mode_split().
        if migrate_call_exclusive_mode_split(self.settings):
            changed = True
        # Microphone names saved as mojibake by the PyAudio-built combos
        # ("Mixagem estÃ©reo"). Idempotent -- see repair_device_name().
        if repair_stored_input_device_names(self.settings):
            changed = True
        # voice_message_mode default "audio" -> "voice_message": every
        # existing settings.json has the old value written out, so the new
        # default only reaches anyone through a conversion. One shot, with
        # its own flag — see migrate_voice_message_mode_default().
        if migrate_voice_message_mode_default(self.settings):
            changed = True
        # show_typing_row default True -> False: the 2.1.0.0 alphas wrote the
        # old default into every settings.json, so only a conversion reaches
        # them. One shot, with its own flag — see migrate_typing_row_default().
        if migrate_typing_row_default(self.settings):
            changed = True
        # spell_check_enabled (bool) -> spell_check_mode (three-valued).
        # core/spell_checker.py's own read-time fallback cannot reach a real
        # install: backfill_missing_defaults() below invents spell_check_mode
        # before anything reads it. Must run here, ahead of that backfill —
        # see migrate_spell_check_mode().
        if migrate_spell_check_mode(self.settings):
            changed = True
        # Pre-2.0 install detection: no settings.json in existence today can
        # have been written by a 2.0+ build (see migrate_wpp_reinstall_notice's
        # docstring), so its absent flag alone arms the one-time WPPConnect
        # reinstall recommendation. Must run here too, ahead of the backfill
        # that would otherwise plant the (already-True) shipped default first.
        if migrate_wpp_reinstall_notice(self.settings):
            changed = True
        if changed:
            self.save_settings()

    @property
    def messages_set_completed(self) -> bool:
        """Get the messages synchronization status from SQLite metadata."""
        if not hasattr(self, "db") or self.db is None:
            return False
        return self.db.get_metadata_json("messages_set_completed", False)

    @messages_set_completed.setter
    def messages_set_completed(self, val: bool):
        """Set the messages synchronization status in SQLite metadata."""
        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("messages_set_completed", val)

    def remember_save_folder(self, saved_path: str) -> None:
        """Record the folder a Save As dialog just wrote into.

        Called by every save dialog after the user confirms one, so
        Configuracoes > Arquivos e salvamento's "ultima pasta definida" mode
        has something to open on next time. Recorded whatever the active mode
        is — switching to that mode later should not find it empty — and the
        other modes simply ignore the value.

        Writes settings only when the folder actually changed: saving a run of
        files into one folder is the common case, and each of those would
        otherwise be a full settings write for no new information.
        """
        try:
            from core.save_location import remember_save_dialog_folder
            if remember_save_dialog_folder(self.settings, saved_path):
                self.save_settings()
        except Exception as exc:
            # Never let bookkeeping break a save the user already completed.
            logging.info("[remember_save_folder] ignored: %s", exc)

    def save_settings(self):
        try:
            # WebSocket handlers and the debounce timer can save concurrently.
            # Serialize the entire write so they cannot replace the same temp
            # file or overwrite a newer snapshot with an older one.
            with self._save_lock:
                self._save_settings_locked()
        except Exception:
            self.error_sound.play()
            # save_settings() is called from many places, including several
            # background threads (e.g. WebSocketClient event handlers) — a
            # raw wx.MessageBox() call off the main thread is a real crash
            # risk on Windows, so always marshal it through CallAfter.
            msg   = f"{self.i18n.t('settings_save_failed')} {format_exc()}"
            title = self.i18n.t("error").format(app_name=self.app_name)
            wx.CallAfter(wx.MessageBox, msg, title, wx.OK | wx.ICON_ERROR)

    def _save_settings_locked(self):
        target = data_path("settings.json")
        # Write to a temp file in the same directory, then atomically replace
        # the real file. The old file is never observably partial, even if the
        # process dies mid-write.
        tmp = f"{target}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        # Mirror install-wide keys to global/app.json so other accounts see the
        # change (plan Zad 2.3b). Best-effort; never blocks the save.
        self._persist_global_settings()

    def _schedule_save_settings(self, delay=2.0):
        """Debounce save_settings; recording sliders request 0.5 s, others 2 s.

        Used when background events (e.g. presence.update bursts) update settings
        frequently — avoids hammering the disk on every event.
        """
        with self._save_timer_lock:
            existing = getattr(self, "_settings_save_timer", None)
            if existing is not None:
                # Background changes must not postpone a slider's short save.
                if existing.interval < delay:
                    return
                existing.cancel()
            def _fire():
                # Clear the handle FIRST: left dangling after the timer
                # fired, _flush_pending_debounced_saves() reads it as a
                # still-pending write and re-saves settings.json on every
                # single shutdown from the first settings change onwards.
                with self._save_timer_lock:
                    if self._settings_save_timer is not t:
                        return  # superseded or already flushed during shutdown
                    self._settings_save_timer = None
                self.save_settings()

            t = threading.Timer(delay, _fire)
            t.daemon = True
            self._settings_save_timer = t
            t.start()

    def refresh_sound_packs(self):
        """Re-scan client/sounds/*/ for soundpacks (a *.pack.json manifest
        folder each). Call after importing a new pack so it's immediately
        selectable without restarting the app.
        """
        self._sound_packs = discover_sound_packs(self.sound_system.sound_dir)
        self._default_sound_pack = self._sound_packs.get(DEFAULT_PACK_ID)

    def get_active_sound_pack(self) -> "dict | None":
        """The soundpack currently selected in Settings (falls back to the
        default pack if the stored choice no longer exists — e.g. its folder
        was deleted outside the app)."""
        pack_id = self.settings.get("active_sound_pack", DEFAULT_PACK_ID)
        return self._sound_packs.get(pack_id) or self._default_sound_pack

    # ── Carrying settings to another install ─────────────────────────────────
    # settings.json cannot simply be copied: it also holds this install's
    # session and this account's own state. core/settings_transfer.py decides
    # what travels; this is the file dialog, the writing, and making what was
    # imported take effect without a restart.

    _SETTINGS_EXPORT_FILENAME = "winzapp-settings.json"

    def _settings_transfer_folder(self) -> str:
        """Where the file dialogs open: the same folder every other Save As
        dialog uses (Settings > Arquivos e salvamento)."""
        try:
            from core import save_location
            return save_location.resolve_save_dialog_folder(self.settings)
        except Exception:
            return ""

    def _on_export_settings(self, event=None):
        t = self.i18n.t
        dlg = wx.FileDialog(
            self, t("settings_export_dialog_title"),
            defaultDir=self._settings_transfer_folder(),
            defaultFile=self._SETTINGS_EXPORT_FILENAME,
            wildcard=t("settings_file_wildcard"),
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        finally:
            dlg.Destroy()
        error = self.export_settings_to_file(path)
        if error:
            self._announce_settings_transfer(error, error=True)
            return
        # A custom API travels with its address, port and key, so the file holds
        # a credential — the user is told, since where they put it now matters.
        from core.settings_transfer import uses_custom_api
        done = ("settings_export_done_custom_api" if uses_custom_api(self.settings)
                else "settings_export_done")
        try:
            from core import save_location
            if save_location.remember_save_dialog_folder(self.settings, path):
                self.save_settings()
        except Exception:
            logging.exception("[settings-transfer] could not remember the folder")
        self._announce_settings_transfer(done, name=os.path.basename(path))

    def _on_import_settings(self, event=None):
        t = self.i18n.t
        dlg = wx.FileDialog(
            self, t("settings_import_dialog_title"),
            defaultDir=self._settings_transfer_folder(),
            wildcard=t("settings_file_wildcard"),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        finally:
            dlg.Destroy()
        # Asked before anything is written: an import overwrites settings the
        # user may have spent a while on, and No is the default so a stray
        # Enter changes nothing.
        confirm = wx.MessageDialog(
            self, t("settings_import_confirm"), t("settings_import_confirm_title"),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        try:
            if confirm.ShowModal() != wx.ID_YES:
                return
        finally:
            confirm.Destroy()
        error, applied = self.import_settings_from_file(
            path, confirm_api_change=self._confirm_imported_api)
        if error:
            self._announce_settings_transfer(error, error=True)
            return
        self._announce_settings_transfer("settings_import_done", count=applied)

    def _confirm_imported_api(self, change: dict) -> bool:
        """Ask, naming both addresses, before an import moves this install to
        another API. The session token goes to both of them, and the change
        reaches every account on this computer (app_settings'
        _CONNECTION_GLOBAL), so this is not one more preference among the rest.
        No is the default, and No still imports everything else
        (import_settings_from_file())."""
        t = self.i18n.t
        dlg = wx.MessageDialog(
            self, t("settings_import_api_confirm").format(
                server=change.get("server") or "?",
                ws_server=change.get("ws_server") or "?"),
            t("settings_import_confirm_title"),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        # The message tells the person what No does, so the button has to say
        # the same word in WinZapp's language, not in Windows'.
        dlg.SetYesNoLabels(t("yes_button"), t("no_button"))
        try:
            return dlg.ShowModal() == wx.ID_YES
        finally:
            dlg.Destroy()

    def _announce_settings_transfer(self, key: str, error: bool = False, **fields):
        """Show the outcome: this is a rare, deliberate action whose result the
        user has to be sure of, and the error path is the one that matters most
        (nothing was changed). Shown rather than also spoken — the screen
        reader reads the box when it takes focus."""
        message = self.i18n.t(key)
        if fields:
            try:
                message = message.format(**fields)
            except (KeyError, IndexError, ValueError):
                pass
        # Shown, not also spoken: the screen reader reads the box when it takes
        # focus, and saying the same sentence twice is worse than saying it once.
        try:
            wx.MessageBox(
                message,
                self.i18n.t("error").format(app_name=self.app_name) if error
                else self.i18n.t("settings_title"),
                wx.OK | (wx.ICON_ERROR if error else wx.ICON_INFORMATION),
                self,
            )
        except Exception:
            logging.exception("[settings-transfer] could not show the outcome")

    def export_settings_to_file(self, path: str) -> str:
        """Write the shareable settings to `path`; '' or an i18n error key."""
        from core.settings_transfer import build_export
        try:
            payload = build_export(self.settings, __version__)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4, ensure_ascii=False)
            logging.info("[settings-transfer] settings exported to %s", path)
            return ""
        except Exception:
            logging.exception("[settings-transfer] export failed")
            return "settings_export_failed"

    def import_settings_from_file(self, path: str, confirm_api_change=None):
        """Apply the settings in `path`. Returns (i18n error key or '', count).

        Nothing is written until the file has been read and understood, so a
        file that is not an export, or holds nothing this build knows, leaves
        the install exactly as it was.

        `confirm_api_change(change) -> bool` is asked when the file would move
        this install to another API (core/settings_transfer.api_change, a dict
        naming both addresses). With no callback, or a No, the connection is
        left exactly as it is and the rest is still imported — never the other
        way round.
        """
        from core.settings_transfer import api_change, merge_settings, read_export
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                payload = json.load(f)
        except Exception:
            logging.exception("[settings-transfer] could not read %s", path)
            return "settings_import_unreadable", 0
        incoming, error = read_export(payload)
        if error:
            logging.warning("[settings-transfer] refused %s: %s", path, error)
            return error, 0
        server = api_change(self.settings, incoming)
        include_connection = server is None
        if server is not None and confirm_api_change is not None:
            try:
                include_connection = bool(confirm_api_change(server))
            except Exception:
                logging.exception("[settings-transfer] could not ask about the API")
                include_connection = False
        if server is not None:
            logging.info("[settings-transfer] file moves the API to %s: %s",
                         server, "accepted" if include_connection else "kept as it was")
        merged, applied, ignored = merge_settings(
            self.settings, incoming, include_connection=include_connection)
        if ignored:
            # Not an error: a newer export, or one carrying this install's own
            # state, simply leaves those alone.
            logging.info("[settings-transfer] %d setting(s) ignored: %s",
                         len(ignored), ", ".join(sorted(ignored)[:20]))
        if not applied:
            return "settings_import_nothing", 0
        # In place: panels and helpers hold a reference to this very dict. No
        # clear() first — `merged` already holds every key this install had —
        # and under the save lock: a save on another thread in between would
        # otherwise iterate a dict changing size, or write settings.json empty.
        #
        # The install-wide keys the file set are written to the shared file
        # here, as explicit choices, before save_settings() reconciles the
        # rest: by difference alone, one equal to the value this account
        # started with would read as unchanged and be pulled back over.
        from app_settings import _GENERAL_GLOBAL, _CONNECTION_GLOBAL
        imported_global = [
            k for section, keys in (("general", _GENERAL_GLOBAL),
                                    ("connection", _CONNECTION_GLOBAL))
            if isinstance(incoming.get(section), dict) and section not in ignored
            for k in keys
            if k in incoming[section] and f"{section}.{k}" not in ignored
        ]
        with self._save_lock:
            self.settings.update(merged)
            self._persist_global_settings(explicit=imported_global)
        self.save_settings()
        self.apply_settings_live(imported_global)
        logging.info("[settings-transfer] %d setting(s) imported from %s", applied, path)
        return "", applied

    def apply_settings_live(self, imported_global=None):
        """Make the settings currently in self.settings take effect now.

        The Settings dialog applies each control as it saves it; an import
        replaces many at once, so this does the same work driven by the values
        instead. Every step is guarded on its own: one that fails must not
        leave the rest unapplied, and none of them may take the app down — the
        settings are already saved by the time this runs.

        `imported_global` names the install-wide keys the import carried
        (None: all of them). The import's own reconciliation has just pulled
        every other install-wide key into self.settings from the shared file,
        so those hold what another account chose, not what this import did:
        a file with only sounds in it would otherwise move this process onto
        another account's API — the attributes are the API this process uses
        until it starts again — and switch the window to another account's
        language with the user's focus on it. Those follow their own rules
        (_apply_pulled_global_settings(), which the same reconciliation has
        already scheduled).
        """
        # The install-wide copy every account reads is written by the import
        # itself before this runs (_persist_global_settings with the keys the
        # file set as `explicit`) — including the connection block.

        def _step(what, fn):
            try:
                fn()
            except Exception:
                logging.exception("[settings-transfer] could not apply %s", what)

        general = self.settings.get("general", {})

        # The API this account talks to, applied whole: every URL is built from
        # server and port together and authenticated with the key, so moving
        # the server while leaving the other two would point the app at the new
        # host on the old port with the old key — working again only after a
        # restart, which is exactly what an import promises not to need. For
        # the bundled API these are this install's own values, unchanged by any
        # import (core/settings_transfer.py).
        # Outside _step() on purpose: connection_runtime() cannot raise (it is
        # total over anything self.settings may hold), and these five have to
        # move together or not at all. Anything added here needs its own guard.
        in_use = {
            "wpp_custom_api": self.wpp_custom_api,
            "wpp_server": self.wpp_server,
            "wpp_ws_server": self.wpp_ws_server,
            "wpp_port": getattr(self, "wpp_port", None),
            "wpp_api_key": getattr(self, "wpp_api_key", None),
        }
        source = self.settings
        if imported_global is not None:
            from app_settings import _CONNECTION_GLOBAL
            connection = dict(self.settings.get("connection") or {})
            for key in _CONNECTION_GLOBAL:
                if key not in imported_global:
                    connection[key] = in_use[key]
            source = {**self.settings, "connection": connection}
        runtime = _connection_runtime(source, in_use)
        socket_moved = (runtime["wpp_ws_server"] != getattr(self, "wpp_ws_server", None)
                        or runtime["wpp_port"] != getattr(self, "wpp_port", None))
        self.wpp_custom_api = runtime["wpp_custom_api"]
        self.wpp_server = runtime["wpp_server"]
        self.wpp_ws_server = runtime["wpp_ws_server"]
        self.wpp_port = runtime["wpp_port"]
        self.wpp_api_key = runtime["wpp_api_key"]

        def _reconnect_socket():
            # REST calls read the attributes above on every request; the
            # Socket.IO connection was opened against the old address and would
            # keep delivering (or failing to deliver) live events from there
            # until a restart. connect_websocket() disconnects first and blocks
            # on the handshake, hence the thread.
            if socket_moved and getattr(self, "ws", None) is not None:
                threading.Thread(target=self.connect_websocket, daemon=True).start()

        _step("live connection", _reconnect_socket)

        # The imported devices are an explicit choice: a quick switch
        # (Ctrl+Alt+Shift+H/G) stops overriding them, for calls too.
        _step("quick device switch",
              lambda: self.end_session_audio_devices(general=True, call=True))
        _step("audio devices", self._apply_configured_audio_devices)
        _step("sounds", self.load_sounds)

        def _move_active_call():
            # A call in progress moves to the imported call devices too, as
            # the in-call settings do; it would stay on the quick-switched one.
            if getattr(self, "_call_audio_session", None) is not None:
                self._restart_active_voice_call_audio()

        _step("call audio", _move_active_call)

        def _clear_sound_cache():
            cache = getattr(self, "_notification_sound_cache", None)
            if cache is not None:
                cache.clear()

        _step("notification sounds", _clear_sound_cache)

        def _reload_language():
            from core.i18n import I18n
            I18n.invalidate_cache()
            self.i18n.get_language()
            self.apply_language_changes()

        if imported_global is None or "language" in imported_global:
            _step("language", _reload_language)

        def _apply_tray():
            show = general.get("show_tray_icon", True)
            if show and self.tray_icon is None:
                self._init_tray()
            elif not show and self.tray_icon is not None:
                self.tray_icon.RemoveIcon()
                self.tray_icon.Destroy()
                self.tray_icon = None

        _step("tray icon", _apply_tray)

        def _apply_hotkey():
            hotkey = general.get("global_hotkey") or {}
            self.set_global_hotkey(hotkey.get("vk", 0), hotkey.get("mod", 0))

        _step("global hotkey", _apply_hotkey)
        from ui.shortcut_bindings import refresh_shortcuts
        _step("keyboard shortcuts", lambda: refresh_shortcuts(self))

        def _apply_calls():
            if not self.settings.get("calls", {}).get("alerts_enabled", True):
                self.stop_all_incoming_call_alerts()
            self._sync_incoming_call_bar()

        _step("calls", _apply_calls)

        def _apply_conversation():
            cp = getattr(self, "conversations_panel", None)
            if cp is None:
                return
            ui = self.settings.get("user_interface", {})
            cp.apply_message_list_mode(ui.get("message_list_mode", "classic"))
            speed = float(self.settings.get("audio_playback", {}).get(
                "audio_default_speed", 1.0))
            if speed in cp._audio_speed_steps:
                cp._audio_speed_index = cp._audio_speed_steps.index(speed)
                cp.audio_speed_btn.SetLabel(cp._format_speed(speed))
            if getattr(cp, "conversation", None) is not None:
                cp.populate_messages(preserve_focus=True)

        _step("open conversation", _apply_conversation)

        def _rebuild_chat_list():
            # The list embeds settings of its own (the self-reference word, the
            # delivery status, the yesterday label), and its fingerprint has
            # not changed — so it has to be cleared or the rebuild is skipped.
            self._chats_ui_fp = None
            # An imported pin-order choice changes sorting as well as text.
            self._schedule_set_chats()

        _step("chat list", _rebuild_chat_list)

    def load_sounds(self):
        """Load every per-event UI sound from the active soundpack (Settings >
        Sound Events), falling back to the default pack — and then to
        enabled=True/no override — for anything the user hasn't customized.
        message_background is one of these events too (it's what the Alert
        Tones "Padrão" choice and the per-conversation "Padrão" override
        ultimately resolve to — see _resolve_message_background_path()).

        Safe to call again after Settings > Sound Events changes to pick up
        a new active pack / enabled / per-event override without restarting.
        """
        active_pack = self.get_active_sound_pack()
        default_pack = self._default_sound_pack
        pack_id = active_pack.get("id") if active_pack else DEFAULT_PACK_ID
        events_cfg = self.settings.get("sound_events", {}).get(pack_id, {})
        for key, _default_filename in SOUND_EVENTS:
            cfg = events_cfg.get(key) or {}
            override_path = cfg.get("path", "")
            resolved = resolve_sound_event_path(active_pack, default_pack, key, override_path)
            if resolved:
                setattr(
                    self,
                    f"{key}_sound",
                    load_sound(
                        self.sound_system,
                        resolved,
                        event_key=key,
                        pack_id=pack_id,
                        looping=(key == "call_incoming"),
                    ),
                )
            else:
                # Nothing resolves at all (broken install: even the default
                # pack is missing this file) — a silent no-op beats a crash.
                from core.sound_system import NullSound
                setattr(self, f"{key}_sound", NullSound())

    def _apply_configured_audio_devices(self):
        """Finish applying the Settings > Audio Devices output/input device
        choices, and warn if either failed.

        The output device itself was already switched to right after the
        sound system started (before load_sounds(), see __init__) — this
        call is deliberately a no-op if that already succeeded (Output.
        set_device() only does real work when the target device differs
        from the current one) and exists here purely to surface the failure
        message box, which needs i18n (not ready yet at that earlier point).
        A device that isn't found or fails to open falls back to the
        Windows default and warns — the stored setting is left as-is so the
        same device is retried on the next launch (see
        core.sound_system.SoundSystem and the Settings dialog's own
        validation for the other two points this same policy applies at).
        """
        audio_devices = self.settings.get("audio_devices", {})

        output_name = audio_devices.get("output_device_name", "")
        self.sound_system.apply_output_device(output_name, warn_on_failure=True)

        effects_name = audio_devices.get("effects_output_device_name", "")
        self.sound_system.apply_effects_device(effects_name, warn_on_failure=True)

        input_name = audio_devices.get("input_device_name", "")
        self.effective_input_device_name = ""
        if input_name:
            idx = find_input_device_index(input_name)
            if idx is not None and test_input_device(idx):
                self.effective_input_device_name = input_name
            elif not self.background_mode:
                wx.MessageBox(
                    self.i18n.t("audio_device_failed_input").format(device=input_name),
                    self.i18n.t("error").format(app_name=self.app_name),
                    wx.OK | wx.ICON_WARNING,
                )

    def _resolve_message_background_path(self) -> str:
        """Resolve the message_background Sound Events entry: '' if the user
        disabled it, else its custom path override or the active/default
        pack's own file (same fallback chain as any other Sound Events entry).
        """
        active_pack = self.get_active_sound_pack()
        default_pack = self._default_sound_pack
        pack_id = active_pack.get("id") if active_pack else DEFAULT_PACK_ID
        cfg = self.settings.get("sound_events", {}).get(pack_id, {}).get("message_background", {})
        if not cfg.get("enabled", True):
            return ""
        return resolve_sound_event_path(active_pack, default_pack, "message_background", cfg.get("path", ""))

    def _resolve_background_sound_path(self, remote_jid: str) -> str:
        """Pick the .ogg file for a background/toast notification for `remote_jid`.

        Priority: per-conversation override (Settings > conversation data
        dialog) > the private/group default from Settings > Alert Tones >
        the message_background Sound Events entry (active pack, falling back
        to the default pack). Falls through to the next tier whenever a
        chosen path doesn't resolve to an existing file, so a removed/typo'd
        custom path or an active pack missing that file never silently kills
        notification sound.
        """
        active_pack = self.get_active_sound_pack()
        default_pack = self._default_sound_pack

        conv_cfg = self.settings.get("conversation_sounds", {}).get(remote_jid) or {}
        choice = conv_cfg.get("choice", "default")
        if choice and choice != "default":
            path = resolve_alert_tone_path(active_pack, default_pack, choice, conv_cfg.get("custom_path", ""))
            if path and os.path.isfile(path):
                return path

        is_group = remote_jid.endswith("@g.us")
        tones = self.settings.get("alert_tones", {})
        type_key = "group" if is_group else "private"
        type_choice = tones.get(type_key, "default")
        type_custom = tones.get(f"{type_key}_custom_path", "")
        if type_choice and type_choice != "default":
            path = resolve_alert_tone_path(active_pack, default_pack, type_choice, type_custom)
            if path and os.path.isfile(path):
                return path

        return self._resolve_message_background_path()

    def play_background_notification_sound(self, remote_jid: str):
        """Play the resolved background/toast notification sound for `remote_jid`.

        Skipped while Windows is in a state where it would itself suppress a
        toast's sound (Focus Assist, fullscreen app, presentation mode) —
        see core/quiet_hours.py for why this needs its own check: this sound
        is played directly through BASS, entirely outside the WinRT toast
        pipeline Windows actually gates on Focus Assist.
        """
        if is_quiet_hours_active():
            return
        path = self._resolve_background_sound_path(remote_jid)
        if not path:
            return
        cache = self._notification_sound_cache
        snd = cache.get(path)
        if snd is None:
            try:
                snd = Sound(self.sound_system, path)
            except Exception:
                snd = self.message_background_sound
            cache[path] = snd
        snd.play()

    def play_startup_sound(self):
        """Play startup sound exactly once per application run."""
        if getattr(self, "_startup_sound_played", False):
            return
        self._startup_sound_played = True
        try:
            if hasattr(self, "startup_sound") and self.startup_sound:
                logging.info("[sound] Playing startup sound")
                self.startup_sound.play()
        except Exception as e:
            logging.warning("[sound] Error playing startup sound: %s", e)
