"""WindowChromeMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import textwrap
import threading
import time
import wx
from main_window.win32_helpers import (
    BOOKMARK_ZERO_HOTKEY_ID,
    _HotkeyManager,
)
from version import __version__
from ui.dialogs.checkbox_confirm import confirm_with_checkbox
from core.utils import (
    effective_unread_count,
    search_normalization_mode,
)
from window_title import format_window_title


class WindowChromeMixin:
    """Menu bar, window title, accounts menu, IPC between account processes,
    bookmark/global hotkeys and the offline toggle.
    """

    # ── Menu bar ─────────────────────────────────────────────────────────────

    def _format_title(self, unread=0):
        """Window title including the account name (plan Zad 2.2/4.2).

        Single/legacy account -> plain 'WinZapp'; multi-account -> 'WinZapp — <name>'.
        Kept as a pure helper so it's unit-testable and reused by tray/refresh.
        """
        return format_window_title("WinZapp", self.account_name, unread,
                                   is_multi=self._is_multi_account())

    def _is_multi_account(self) -> bool:
        """True only when MORE than one account is paired.

        The account name is only meaningful to distinguish windows/alerts when
        there is more than one connected account; with a single paired account
        it is redundant and only adds noise to NVDA announcements, the tray
        tooltip and toast titles — so it is suppressed everywhere (window
        title, tray, notifications) while len(list_paired()) <= 1.
        """
        reg = getattr(self, "registry", None)
        if reg is None:
            return False
        try:
            return len(reg.list_paired()) > 1
        except Exception:
            return False

    def _build_menubar(self):
        """Create the menu bar with Arquivo, Sincronização and Ajuda menus."""
        self._ID_MARK_ALL_READ = wx.NewIdRef()
        self._ID_SETTINGS      = wx.NewIdRef()
        self._ID_EXPORT_SETTINGS = wx.NewIdRef()
        self._ID_IMPORT_SETTINGS = wx.NewIdRef()
        self._ID_DISCONNECT    = wx.NewIdRef()
        self._ID_EXIT          = wx.NewIdRef()
        self._ID_RESYNC_ALL    = wx.NewIdRef()
        self._ID_RESYNC_CONVERSATION = wx.NewIdRef()
        self._ID_SYNC_MEDIA    = wx.NewIdRef()
        self._ID_OFFLINE_MENU  = wx.NewIdRef()
        self._ID_SHORTCUTS     = wx.NewIdRef()
        self._ID_FORCE_UPDATE  = wx.NewIdRef()
        self._ID_FORCE_REINSTALL_ZIP = wx.NewIdRef()
        self._ID_FORCE_REINSTALL_WPP = wx.NewIdRef()
        self._ID_WHATS_NEW     = wx.NewIdRef()
        self._ID_ABOUT         = wx.NewIdRef()

        menubar = wx.MenuBar()

        # ── Arquivo ───────────────────────────────────────────────────────────
        file_menu = wx.Menu()
        file_menu.Append(
            self._ID_MARK_ALL_READ,
            f"{self.i18n.t('menu_mark_all_read')}\tCtrl+Shift+Alt+M",
        )
        file_menu.AppendSeparator()
        file_menu.Append(
            self._ID_SETTINGS,
            f"{self.i18n.t('menu_settings')}\tCtrl+,",
        )
        # Carrying settings to another install. Next to Configurações because
        # that is what they are about, and with no accelerator: they are rare,
        # deliberate actions and every letter here is already spoken for.
        file_menu.Append(self._ID_EXPORT_SETTINGS, self.i18n.t("menu_export_settings"))
        file_menu.Append(self._ID_IMPORT_SETTINGS, self.i18n.t("menu_import_settings"))
        file_menu.AppendSeparator()
        file_menu.Append(
            self._ID_DISCONNECT,
            f"{self.i18n.t('menu_disconnect')}\tCtrl+Alt+Shift+D",
        )
        file_menu.AppendSeparator()
        file_menu.Append(
            self._ID_EXIT,
            f"{self.i18n.t('menu_exit')}\tCtrl+Alt+Shift+Q",
        )
        menubar.Append(file_menu, self.i18n.t("menu_file"))

        # ── Sincronização ─────────────────────────────────────────────────────
        sync_menu = wx.Menu()
        sync_menu.Append(
            self._ID_RESYNC_ALL,
            f"{self.i18n.t('menu_resync_all')}\tF5",
        )
        sync_menu.Append(
            self._ID_RESYNC_CONVERSATION,
            f"{self.i18n.t('menu_resync_conversation')}\tShift+F5",
        )
        sync_menu.Append(
            self._ID_SYNC_MEDIA,
            f"{self.i18n.t('menu_sync_media')}\tCtrl+Shift+Alt+B",
        )
        self._sync_offline_menu_item = sync_menu.AppendCheckItem(
            self._ID_OFFLINE_MENU,
            f"{self.i18n.t('tray_offline_mode')}\tCtrl+Alt+Shift+O",
        )
        self._sync_offline_menu_item.Check(bool(self.offline_mode))
        menubar.Append(sync_menu, self.i18n.t("menu_sync"))

        # ── Konta (multi-account) ─────────────────────────────────────────────
        # Only shown when this process runs under the account system (account_id
        # set). Populated dynamically from the registry (plan Zad 4.2).
        self._accounts_menu_id_map = {}
        if getattr(self, "account_id", None) and getattr(self, "registry", None):
            accounts_menu = wx.Menu()
            try:
                import account_ui
                # Keep the WindowIDRef objects ALIVE: wx.NewIdRef() reserves an id
                # only for as long as the ref object exists. Casting to int() and
                # dropping the ref frees the reservation, so AppendRadioItem then
                # trips "id should first be reserved" (wxAssertionError) and the
                # whole Accounts menu silently fails to build. Store the refs.
                self._accounts_menu_id_refs = []

                def _new_account_menu_id():
                    ref = wx.NewIdRef()
                    self._accounts_menu_id_refs.append(ref)
                    return ref

                self._accounts_menu_id_map = account_ui.build_accounts_menu(
                    accounts_menu, self.registry.list(), self.account_id,
                    self.i18n, _new_account_menu_id)
                for wid, action in self._accounts_menu_id_map.items():
                    self.Bind(wx.EVT_MENU,
                              lambda e, a=action: self._on_accounts_menu(a), id=int(wid))
                menubar.Append(accounts_menu, self.i18n.t("acc_menu_title"))
                # Ctrl+Alt+1..9 → switch to the n-th paired account. Menu-label
                # accelerators alone don't fire reliably here: focused child
                # panels (conversation list, message field, …) install their own
                # wx.AcceleratorTable, which swallows the keystroke before the
                # menu bar sees it. A frame-level EVT_CHAR_HOOK catches the combo
                # regardless of which control has focus (bound once).
                self._account_hotkey_slots = {
                    slot: acc["id"] for slot, acc in
                    account_ui.accelerator_slots(account_ui.switchable_accounts(self.registry.list()))
                }
                # Remember what the menu was built from, so focus-gain can tell
                # whether a live registry change made it stale (see
                # _refresh_accounts_menu_if_stale).
                self._accounts_menu_signature = account_ui.accounts_menu_signature(
                    self.registry.list())
                if not getattr(self, "_account_hotkey_hook_bound", False):
                    self.Bind(wx.EVT_CHAR_HOOK, self._on_account_hotkey_char)
                    self._account_hotkey_hook_bound = True
            except Exception:
                logging.exception("[menu] building Accounts menu failed (non-fatal)")

        # Frame-level Ctrl+0..9 → jump to an existing message bookmark,
        # regardless of which control currently has focus — bound
        # unconditionally (not just in multi-account context, unlike the
        # Ctrl+Alt+1..9 account-switch hook above). Jumping to a bookmark
        # can open a DIFFERENT conversation entirely (see
        # ConversationsPanel._on_bookmark_set_or_jump), so it must not
        # require the messages list to already be focused. Setting a NEW
        # bookmark still needs a focused message, so this only ever handles
        # the jump case; event.Skip() when there's no bookmark for that
        # digit lets it fall through to ConversationsPanel's own
        # AcceleratorTable (Ctrl+0..9 there does both set and jump).
        if not getattr(self, "_bookmark_hotkey_hook_bound", False):
            self.Bind(wx.EVT_CHAR_HOOK, self._on_bookmark_hotkey_char)
            self._bookmark_hotkey_hook_bound = True

        # Ctrl+Shift+0 (remove bookmark 0) never reaches any of the paths
        # above — see _set_bookmark_zero_hotkey() for why, and why it needs a
        # Win32 hotkey registration to be reclaimed at all.
        if (not getattr(self, "_bookmark_zero_hotkey_bound", False)
                and hasattr(wx, "EVT_HOTKEY")):
            self.Bind(wx.EVT_HOTKEY, self._on_bookmark_zero_hotkey,
                      id=BOOKMARK_ZERO_HOTKEY_ID)
            self._bookmark_zero_hotkey_bound = True
            # EVT_ACTIVATE drives register/unregister from here on, but the
            # window may already be the active one by the time the menu bar is
            # built (no further activation event would follow).
            try:
                self._set_bookmark_zero_hotkey(bool(self.IsActive()))
            except Exception:
                logging.exception("[bookmarks] initial Ctrl+Shift+0 registration failed")

        # ── Ajuda ─────────────────────────────────────────────────────────────
        help_menu = wx.Menu()
        help_menu.Append(
            self._ID_SHORTCUTS,
            f"{self.i18n.t('menu_shortcuts')}\tF1",
        )
        help_menu.AppendSeparator()
        help_menu.Append(self._ID_FORCE_UPDATE, self.i18n.t("menu_force_update"))
        help_menu.Append(self._ID_FORCE_REINSTALL_ZIP, self.i18n.t("menu_force_reinstall_zip"))
        help_menu.Append(self._ID_FORCE_REINSTALL_WPP, self.i18n.t("menu_force_reinstall_wpp"))
        help_menu.AppendSeparator()
        help_menu.Append(self._ID_WHATS_NEW, self.i18n.t("menu_whats_new"))
        help_menu.Append(self._ID_ABOUT, self.i18n.t("menu_about"))
        menubar.Append(help_menu, self.i18n.t("menu_help"))

        self.SetMenuBar(menubar)
        self.Bind(wx.EVT_MENU, self._on_mark_all_read, id=self._ID_MARK_ALL_READ)
        self.Bind(wx.EVT_MENU, self.on_ctrl_comma,     id=self._ID_SETTINGS)
        self.Bind(wx.EVT_MENU, self._on_export_settings, id=self._ID_EXPORT_SETTINGS)
        self.Bind(wx.EVT_MENU, self._on_import_settings, id=self._ID_IMPORT_SETTINGS)
        self.Bind(wx.EVT_MENU, self._on_menu_disconnect, id=self._ID_DISCONNECT)
        self.Bind(wx.EVT_MENU, lambda e: self.quit_all_accounts(), id=self._ID_EXIT)
        self.Bind(wx.EVT_MENU, self._on_menu_resync_all, id=self._ID_RESYNC_ALL)
        self.Bind(wx.EVT_MENU, self._on_menu_resync_conversation,
                  id=self._ID_RESYNC_CONVERSATION)
        self.Bind(wx.EVT_MENU, self._on_menu_sync_media, id=self._ID_SYNC_MEDIA)
        self.Bind(wx.EVT_MENU, self._on_menu_toggle_offline, id=self._ID_OFFLINE_MENU)
        self.Bind(wx.EVT_MENU, self.on_f1,             id=self._ID_SHORTCUTS)
        self.Bind(wx.EVT_MENU, self._on_force_update,  id=self._ID_FORCE_UPDATE)
        self.Bind(wx.EVT_MENU, self._on_force_reinstall_zip, id=self._ID_FORCE_REINSTALL_ZIP)
        self.Bind(wx.EVT_MENU, self._on_force_reinstall_wpp, id=self._ID_FORCE_REINSTALL_WPP)
        self.Bind(wx.EVT_MENU, self._on_whats_new,     id=self._ID_WHATS_NEW)
        self.Bind(wx.EVT_MENU, self._on_about,         id=self._ID_ABOUT)

    def _on_account_hotkey_char(self, event):
        """Frame-level Ctrl+Alt+1..9 → switch to the n-th paired account.

        Was Ctrl+Shift+1..9 — moved to Ctrl+Alt to stop colliding with the
        pre-existing, non-multi-account message-bookmark-removal shortcut
        (ConversationsPanel._on_bookmark_remove, Ctrl+Shift+0..9): this
        handler's EVT_CHAR_HOOK intercepts its combo before it can ever
        reach that panel's own AcceleratorTable, so sharing Ctrl+Shift+
        <digit> silently broke bookmark removal for any account count below
        9 (i.e. almost every install). Ctrl+Alt was deliberately avoided at
        first because AltGr reports as Ctrl+Alt on a PL keyboard — kept as
        a known tradeoff by deliberate choice, not an oversight.

        Bound via EVT_CHAR_HOOK so it fires no matter which child control holds
        focus (child panels' own AcceleratorTables would otherwise swallow the
        menu-bar accelerator). Anything that isn't our exact combo is passed
        through untouched with event.Skip().
        """
        try:
            if (event.GetModifiers() == (wx.MOD_CONTROL | wx.MOD_ALT)):
                code = event.GetKeyCode()
                if ord("1") <= code <= ord("9"):
                    slot = code - ord("0")
                    target = getattr(self, "_account_hotkey_slots", {}).get(slot)
                    if target:
                        if target != getattr(self, "account_id", None):
                            self._switch_to_account(target)
                        return  # consume the combo (even a no-op self-switch)
                    # No paired account at this slot (e.g. a single-account
                    # install, or fewer than <slot> paired accounts) — there
                    # is nothing to switch to, so this combo isn't actually
                    # ours; fall through to event.Skip() below.
        except Exception:
            logging.exception("[accounts] hotkey char handler failed")
        event.Skip()

    def _on_bookmark_hotkey_char(self, event):
        """Frame-level Ctrl+0..9 → jump to an existing message bookmark, no
        matter which control currently has focus.

        Bound via EVT_CHAR_HOOK for the same reason _on_account_hotkey_char()
        is: a focused child panel's own AcceleratorTable would otherwise
        require focus to already be inside it for Ctrl+<digit> to fire at
        all — but jumping to a bookmark can open a DIFFERENT conversation
        entirely (ConversationsPanel._on_bookmark_set_or_jump), so it needs
        to work from the conversation list, the navigation panel, or
        anywhere else in the window, not only once already inside the
        messages list.

        Only ever handles the JUMP case — setting a NEW bookmark still
        requires a focused message, which only makes sense with the
        messages list itself focused, so that continues to go through
        ConversationsPanel's own Ctrl+0..9 AcceleratorTable entry
        unchanged. Anything that isn't our exact combo, or a digit with no
        bookmark set, is passed through untouched with event.Skip().
        """
        try:
            if event.GetModifiers() == wx.MOD_CONTROL:
                code = event.GetKeyCode()
                if ord("0") <= code <= ord("9"):
                    digit = code - ord("0")
                    panel = getattr(self, "conversations_panel", None)
                    if panel is not None and digit in getattr(panel, "_msg_bookmarks", {}):
                        panel._on_bookmark_set_or_jump(digit)
                        return  # consume — this was a jump to an existing bookmark
        except Exception:
            logging.exception("[bookmarks] hotkey char handler failed")
        event.Skip()

    # ── Ctrl+Shift+0: reclaimed from the Windows IME hotkey ─────────────────

    def _bookmark_hotkey_panel(self):
        """The ConversationsPanel iff its conversation view currently owns
        focus — i.e. exactly the condition under which that panel's own
        AcceleratorTable (Ctrl+Shift+1..9) would fire.

        Needed only by the Ctrl+Shift+0 path below: a Win32 hotkey is
        window-global and fires wherever focus sits inside this frame, so the
        scope the accelerator table gives the other nine digits for free has
        to be re-checked by hand here to keep all ten behaving identically.
        """
        panel = getattr(self, "conversations_panel", None)
        conv = getattr(panel, "conversation_panel", None)
        if conv is None or not conv.IsShown():
            return None
        focus = wx.Window.FindFocus()
        while focus is not None:
            if focus is conv:
                return panel
            focus = focus.GetParent()
        return None

    def _set_bookmark_zero_hotkey(self, active: bool):
        """Register/unregister Ctrl+Shift+0 as a Win32 hotkey, following this
        window's activation state.

        Windows ships a default IME "direct switch" hotkey bound to exactly
        Ctrl+Shift+0 (HKCU\\Control Panel\\Input Method\\Hot Keys\\00000104 —
        the 0x100-0x11F id range is IME_HOTKEY_DSWITCH, "switch straight to
        input method X": Virtual Key = 0x30 = VK_0, Key Modifiers = 0xC006 =
        MOD_CONTROL | MOD_SHIFT | MOD_LEFT | MOD_RIGHT). Note the entry
        survives even with its target input method not installed at all
        (Target IME = 0xE0010411, the Japanese MS-IME, on a Preload holding
        only pt-BR) — the switch is then a no-op, but the key is swallowed
        all the same, which is why the symptom is total silence rather than a
        visible layout change. No sibling entry exists for VK_1..VK_9, hence
        digits 1-9 never had the problem. The OS consumes the combo in the
        IMM/TSF layer before it is ever dispatched to a window, so the
        WM_KEYDOWN for VK_0 never arrives: neither ConversationsPanel's
        AcceleratorTable nor a frame-level EVT_CHAR_HOOK can see it, which is
        why removing bookmark 0 silently did nothing while Ctrl+Shift+1..9
        worked. ::RegisterHotKey() takes precedence over the IME hotkey and is
        the only way to get the combo back.

        That registration is system-global, though — while it is held, no
        other application can receive Ctrl+Shift+0 either. So it is tied to
        this window's focus (EVT_ACTIVATE): held only while WinZapp is the
        active window, released the moment it isn't, which also matches the
        scope the other nine digits already have.

        A failed registration (another process holding the combo) is logged
        and otherwise ignored — Ctrl+Shift+0 then simply stays unavailable,
        exactly as it was before.
        """
        if not getattr(self, "_bookmark_zero_hotkey_bound", False):
            return
        if bool(active) == getattr(self, "_bookmark_zero_hotkey_on", False):
            return
        try:
            if active:
                ok = bool(self.RegisterHotKey(
                    BOOKMARK_ZERO_HOTKEY_ID, wx.MOD_CONTROL | wx.MOD_SHIFT, ord("0")
                ))
                self._bookmark_zero_hotkey_on = ok
                if not ok:
                    logging.warning(
                        "[bookmarks] Ctrl+Shift+0 hotkey registration refused "
                        "(another application holds it) — bookmark 0 removal unavailable"
                    )
            else:
                self.UnregisterHotKey(BOOKMARK_ZERO_HOTKEY_ID)
                self._bookmark_zero_hotkey_on = False
        except Exception:
            logging.exception("[bookmarks] Ctrl+Shift+0 hotkey toggle failed")
            self._bookmark_zero_hotkey_on = False

    def _on_bookmark_zero_hotkey(self, event):
        """WM_HOTKEY for Ctrl+Shift+0 → remove bookmark 0, mirroring what
        ConversationsPanel's AcceleratorTable does for Ctrl+Shift+1..9."""
        try:
            panel = self._bookmark_hotkey_panel()
            if panel is not None:
                panel._on_bookmark_remove(0)
        except Exception:
            logging.exception("[bookmarks] Ctrl+Shift+0 hotkey handler failed")

    def _on_accounts_menu(self, action: dict):
        """Handle a click in the Accounts menu (plan Zad 4.2-4.5)."""
        try:
            import account_ui
            if action.get("switch"):
                self._switch_to_account(action["switch"])
            elif action.get("open_switch"):
                dlg = account_ui.SwitchAccountDialog(
                    self, self.registry.list(), self.account_id, self.i18n)
                chosen = dlg.show()
                if chosen and chosen != self.account_id:
                    self._switch_to_account(chosen)
            elif action.get("open_manager"):
                account_ui.AccountManagerDialog(
                    self, self.registry, self.account_id, self.i18n,
                    self.global_dir, on_pair=self._switch_to_account).show()
                self._rebuild_accounts_menu()
        except Exception:
            logging.exception("[accounts] menu action failed")

    def _switch_to_account(self, account_id: str):
        """Activation-first switch (plan Zad 4.1): bring the target account's
        running process to the foreground via IPC, or spawn it. This window
        stays open (accounts run in the background, choice 1b)."""
        try:
            from account_launcher import switch_to_account
            result = switch_to_account(self.global_dir, account_id)
            logging.info("[accounts] switch to %s -> %s", account_id, result)
            if result == "failed":
                # Before this, a spawn that raised or crashed immediately
                # (missing DLL, corrupted install, blocked by antivirus)
                # was indistinguishable from a normal spawn — the button
                # just did nothing, with the switch never having actually
                # happened and no indication why. Stay open and visible
                # instead of hiding into a switch that never completed.
                wx.MessageBox(
                    self.i18n.t("account_switch_failed"),
                    self.i18n.t("error").format(app_name=self.app_name),
                    wx.OK | wx.ICON_ERROR,
                )
                return
            switch_behavior = "single"
            if getattr(self, "app_settings", None):
                switch_behavior = self.app_settings.get("switch_behavior")
            elif getattr(self, "settings", None):
                switch_behavior = self.settings.get("general", {}).get("switch_behavior", "single")

            if switch_behavior != "keep_open":
                self.hide_to_tray()
        except Exception:
            logging.exception("[accounts] switch to %s failed", account_id)

    def update_account_name(self, new_name: str):
        """Update the account name of this window dynamically when renamed in registry."""
        if not new_name:
            return
        self.account_name = new_name
        self.SetTitle(self._format_title())

    def _rebuild_accounts_menu(self):
        """Rebuild the whole menu bar so the Accounts list reflects registry
        changes (add/rename/archive/delete), and update current account name/title."""
        try:
            if getattr(self, "account_id", None) and getattr(self, "registry", None):
                acc = self.registry.get(self.account_id)
                if acc and acc.get("name"):
                    self.update_account_name(acc["name"])
            self._build_menubar()
        except Exception:
            logging.exception("[accounts] menu rebuild failed")

    def _refresh_accounts_menu_if_stale(self):
        """Rebuild the menu bar iff the live registry no longer matches what the
        Accounts menu was built from (another process paired/renamed/archived an
        account, or one was still coming up when this menu was first built).

        Fixes the reported bug: an account vanished from the menu and only came
        back after opening 'Switch account'. Cheap: compares a signature and
        no-ops when nothing menu-visible changed, so focus-gain doesn't cause
        menu flicker or screen-reader churn."""
        registry = getattr(self, "registry", None)
        if not (getattr(self, "account_id", None) and registry):
            return
        try:
            import account_ui
            current = account_ui.accounts_menu_signature(registry.list())
            if current != getattr(self, "_accounts_menu_signature", None):
                logging.info("[accounts] menu stale on focus — rebuilding "
                             "(was=%s now=%s)",
                             getattr(self, "_accounts_menu_signature", None), current)
                acc = registry.get(self.account_id)
                if acc and acc.get("name"):
                    self.update_account_name(acc["name"])
                self._build_menubar()  # rebuilds menu + hotkey slots + signature
        except Exception:
            logging.exception("[accounts] stale-menu refresh failed (non-fatal)")

    def _offer_switch_when_unpaired(self) -> bool:
        """Startup helper: the current account is unpaired. If other paired
        accounts exist, offer connect-this / switch-to-other / quit instead of
        dropping straight into this account's pairing dialog (which left the
        user with no way to reach a working account or the menu).

        Returns True if this process should stop __init__ and shut down (the
        user chose to switch to another account, or to quit); False to fall
        through to the normal pairing dialog (no other account to switch to, or
        the user chose to connect this one). Best-effort: any failure returns
        False so startup degrades to the existing pairing flow.
        """
        registry = getattr(self, "registry", None)
        acc_id = getattr(self, "account_id", None)
        if not (registry and acc_id):
            return False
        try:
            import account_ui
            accounts = registry.list()
            if not account_ui.unpaired_start_options(accounts, acc_id):
                return False  # no other paired account — normal pairing flow
            acc = registry.get(acc_id) or {}
            name = acc.get("name", acc_id)
            dlg = account_ui.UnpairedStartDialog(
                None, accounts, acc_id, name, self.i18n)
            result = dlg.show()
            if result == account_ui.UnpairedStartDialog.RESULT_SWITCH and dlg.chosen_account_id:
                logging.info("[accounts] unpaired start: switching to %s", dlg.chosen_account_id)
                from account_launcher import switch_to_account
                switch_result = switch_to_account(self.global_dir, dlg.chosen_account_id)
                if switch_result == "failed":
                    # This path used to exit unconditionally right after —
                    # a failed spawn (missing DLL, corrupted install,
                    # blocked by antivirus) left the user with no WinZapp
                    # process running at all, since this one was about to
                    # quit into a switch that never actually happened.
                    # Fall through to this account's own pairing flow
                    # instead of exiting into nothing.
                    wx.MessageBox(
                        self.i18n.t("account_switch_failed"),
                        self.i18n.t("error").format(app_name=self.app_name),
                        wx.OK | wx.ICON_ERROR,
                    )
                    return False
                wx.CallAfter(self.real_exit)
                return True
            if result == account_ui.UnpairedStartDialog.RESULT_QUIT:
                logging.info("[accounts] unpaired start: user chose to quit")
                wx.CallAfter(self.real_exit)
                return True
            # RESULT_PAIR — fall through to the normal pairing dialog.
            return False
        except Exception:
            logging.exception("[accounts] unpaired-start switch offer failed (non-fatal)")
            return False

    def _start_ipc_listener(self):
        """Start the account-scoped IPC listener so other WinZapp processes can
        ask THIS one to foreground / quit (plan Zad 2.0/4.1). Callbacks marshal
        onto the wx thread via wx.CallAfter. Safe no-op without an account."""
        gd = getattr(self, "global_dir", None)
        acc_id = getattr(self, "account_id", None)
        if not (gd and acc_id):
            logging.info("[ipc] listener NOT started (no account/global_dir) — "
                         "gd=%s acc=%s", bool(gd), bool(acc_id))
            return
        try:
            import ipc
            self._ipc_listener = ipc.IpcListener(
                gd, acc_id,
                on_activate=lambda source: wx.CallAfter(self._ipc_activate, source),
                # NOT wx.CallAfter: _ipc_quit() runs the full graceful
                # teardown (close-session + flush wait + Node stop + the
                # loser path's bounded wait), tens of seconds' worth, and on
                # the wx main thread that freezes this account's window -
                # silently, for a screen-reader user - while a DIFFERENT
                # account is the one quitting. real_exit() already runs the
                # same teardown off the main thread for exactly this reason.
                on_quit=lambda: threading.Thread(
                    target=self._ipc_quit, daemon=True,
                    name="winzapp-ipc-quit").start(),
                released_predicate=lambda: getattr(self, "_ipc_released", False),
                window_ready_predicate=lambda: getattr(self, "_window_ready", False),
            )
            self._ipc_listener.start()
            ready = self._ipc_listener.wait_ready(timeout=3.0)
            logging.info("[ipc] listener started for account %s (ready=%s)", acc_id, ready)
        except Exception:
            logging.exception("[ipc] listener start failed (non-fatal)")

    def _ipc_activate(self, source: str):
        """Bring this window to the foreground on an IPC activate request.

        Delegates to restore_window(), which handles the SW_HIDE state-drift
        (this window was likely hidden via hide_to_tray on a previous switch,
        so a bare wx Show()/Raise() can silently no-op) AND lands keyboard focus
        on the conversation list — without that, repeated switches left the
        window focused but no control focused, so arrow keys did nothing.
        """
        try:
            self.restore_window()
            # A conscious user switch updates last_foreground; an autostart-boot
            # activation does not (plan / GPT r5 #2).
            if source == "user" and getattr(self, "registry", None):
                try:
                    self.registry.set_last_foreground(self.account_id)
                except Exception:
                    pass
        except Exception:
            logging.exception("[ipc] activate failed")

    def _ipc_quit(self):
        """Handle an IPC quit request from the account that owns the 'Exit'.

        Ordering is critical. real_exit() ends in os._exit(0), which never
        returns — so setting the released flag AFTER it (the old code's finally)
        was unreachable, the IPC handler never saw `released`, and the
        initiating account's request_quit() waited its full 10s timeout then
        exited anyway WITHOUT confirming this account had flushed. Combined with
        the old fixed sleep(2), that race hard-killed a still-flushing session
        into a re-pair on next launch.

        Fix: run the full graceful teardown (close-session + wait-for-flush +
        stop Node) FIRST, then flag released so the IPC handler can reply
        `released:True`, give it a beat to send that reply over the pipe, and
        only THEN take the process down for good.

        Runs on its own thread (see _start_ipc_listener), never on the wx
        main thread — everything below is bounded in tens of seconds and a
        blocked main thread means a frozen, silent window.
        """
        did_work = False
        try:
            did_work = self._perform_shutdown()
        finally:
            # Reachable now (teardown does not call os._exit). Let the IPC
            # listener observe this and reply released:True to the initiator.
            self._ipc_released = True
        # Give the listener thread a moment to send the released reply before we
        # vanish (its poll loop checks the predicate every 50ms).
        time.sleep(0.3)
        if not did_work:
            # Another path already owns teardown — wait for it to actually
            # finish rather than killing the process out from under its
            # still-in-progress _stop_wpp_server().
            self._teardown_complete_event.wait(
                timeout=self._TEARDOWN_OWNED_ELSEWHERE_WAIT_SECONDS
            )
        self._terminate_process()

    def quit_all_accounts(self):
        """Quit the WHOLE app: gracefully stop every OTHER account's background
        process, then exit this one. Bound to the 'Exit' menu item and the tray
        'Exit' — the user expects 'quit' to close WinZapp entirely, not leave
        other accounts running invisibly in the tray (reported live: closing one
        window left another account alive, and the teardown order then hard-
        killed its session into a re-pair).

        Each other process handles its own IPC quit by closing its WhatsApp
        session gracefully first (see real_exit → _stop_wpp_server STEP 1), so
        no session is ever hard-killed by the shared-Node taskkill. We ask them
        sequentially and wait for each to confirm RELEASED before we exit, so
        the last-one-out Node teardown happens only after every session is
        cleanly closed. Best-effort: a peer that never confirms is given up on
        after request_quit()'s own timeout and never keeps us alive.

        The peer loop runs on a worker thread, and that is not incidental:
        request_quit()'s timeout is sized against a peer's worst-case
        teardown (tens of seconds) and the loop is sequential, so with
        several accounts open, running it here would freeze this window —
        with no repaint and nothing spoken — for minutes. This method is
        bound straight to the Exit menu item and the tray Exit, i.e. it is
        always entered on the wx main thread. The window is hidden first so
        quitting still looks instant.
        """
        gd = getattr(self, "global_dir", None)
        acc_id = getattr(self, "account_id", None)
        if not (gd and acc_id):
            self.real_exit()
            return

        try:
            self.Hide()
        except Exception:
            pass

        def _quit_peers_then_exit():
            try:
                import ipc
                import node_coord
                import update_coord
                others = [l.get("account_id") for l in node_coord.live_node_leases(
                    gd, is_alive=update_coord.lease_alive) if not l.get("_corrupt")]
                others = [a for a in others if a and a != acc_id]
                for other in others:
                    try:
                        logging.info("[quit-all] asking account %s to quit", other)
                        # No explicit timeout: request_quit()'s own default
                        # is sized to the other account's worst-case teardown.
                        ipc.request_quit(gd, other)
                    except Exception:
                        logging.exception("[quit-all] request_quit failed for %s", other)
            except Exception:
                logging.exception("[quit-all] enumerating peers failed (non-fatal)")
            # real_exit() only touches wx to Hide() (already done above) and
            # then hands off to its own thread, but keep it on the main
            # thread anyway so the wx contract is not quietly widened here.
            wx.CallAfter(self.real_exit)

        threading.Thread(target=_quit_peers_then_exit, daemon=True,
                         name="winzapp-quit-all").start()

    def _refresh_menubar(self):
        """Retranslate the menu bar labels after a language change."""
        mb = self.GetMenuBar()
        if mb is None:
            return
        file_menu = mb.GetMenu(0)
        mb.SetMenuLabel(0, self.i18n.t("menu_file"))
        file_menu.FindItemById(self._ID_MARK_ALL_READ).SetItemLabel(
            f"{self.i18n.t('menu_mark_all_read')}\tCtrl+Shift+Alt+M"
        )
        file_menu.FindItemById(self._ID_SETTINGS).SetItemLabel(
            f"{self.i18n.t('menu_settings')}\tCtrl+,"
        )
        file_menu.FindItemById(self._ID_EXPORT_SETTINGS).SetItemLabel(
            self.i18n.t("menu_export_settings")
        )
        file_menu.FindItemById(self._ID_IMPORT_SETTINGS).SetItemLabel(
            self.i18n.t("menu_import_settings")
        )
        file_menu.FindItemById(self._ID_DISCONNECT).SetItemLabel(
            f"{self.i18n.t('menu_disconnect')}\tCtrl+Alt+Shift+D"
        )
        file_menu.FindItemById(self._ID_EXIT).SetItemLabel(
            f"{self.i18n.t('menu_exit')}\tCtrl+Alt+Shift+Q"
        )
        mb.SetMenuLabel(1, self.i18n.t("menu_sync"))
        mb.GetMenu(1).FindItemById(self._ID_RESYNC_ALL).SetItemLabel(
            f"{self.i18n.t('menu_resync_all')}\tF5"
        )
        mb.GetMenu(1).FindItemById(self._ID_RESYNC_CONVERSATION).SetItemLabel(
            f"{self.i18n.t('menu_resync_conversation')}\tShift+F5"
        )
        mb.GetMenu(1).FindItemById(self._ID_SYNC_MEDIA).SetItemLabel(
            f"{self.i18n.t('menu_sync_media')}\tCtrl+Shift+Alt+B"
        )
        mb.GetMenu(1).FindItemById(self._ID_OFFLINE_MENU).SetItemLabel(
            f"{self.i18n.t('tray_offline_mode')}\tCtrl+Alt+Shift+O"
        )
        # The Help menu is NOT at a fixed index: with multi-account a "Konta"
        # menu sits between Sync and Help (File=0, Sync=1, Konta=2, Help=3),
        # without it Help is at index 2. Locate it by the item it owns rather
        # than hard-coding index 2 — otherwise a language change would relabel
        # the "Konta" menu as "Help" and retranslate the wrong menu.
        help_idx = mb.FindMenu(self.i18n.t("menu_help"))
        if help_idx == wx.NOT_FOUND:
            help_menu = None
            for i in range(mb.GetMenuCount()):
                if mb.GetMenu(i).FindItemById(self._ID_ABOUT) is not None:
                    help_idx, help_menu = i, mb.GetMenu(i)
                    break
        else:
            help_menu = mb.GetMenu(help_idx)
        if help_menu is not None:
            mb.SetMenuLabel(help_idx, self.i18n.t("menu_help"))
            help_menu.FindItemById(self._ID_SHORTCUTS).SetItemLabel(
                f"{self.i18n.t('menu_shortcuts')}\tF1"
            )
            help_menu.FindItemById(self._ID_FORCE_UPDATE).SetItemLabel(
                self.i18n.t("menu_force_update")
            )
            help_menu.FindItemById(self._ID_FORCE_REINSTALL_ZIP).SetItemLabel(
                self.i18n.t("menu_force_reinstall_zip")
            )
            help_menu.FindItemById(self._ID_FORCE_REINSTALL_WPP).SetItemLabel(
                self.i18n.t("menu_force_reinstall_wpp")
            )
            help_menu.FindItemById(self._ID_WHATS_NEW).SetItemLabel(
                self.i18n.t("menu_whats_new")
            )
            help_menu.FindItemById(self._ID_ABOUT).SetItemLabel(
                self.i18n.t("menu_about")
            )

    def _on_whats_new(self, event=None):
        """Help > Novidades: show the full local changelog for the user's
        current language — same WhatsNewDialog the auto-updater's "Quais as
        novidades?" button uses, but with the whole file rather than only
        the entries between two versions. When no changelog_<lang>.txt ships
        with this build (e.g. right after a version with no changelog file
        yet, like this one), show a small "no changelog" dialog instead of a
        blank/empty window."""
        from updater import load_changelog_text, WhatsNewDialog
        i18n = self.i18n
        changelog = load_changelog_text(i18n.language)
        if changelog.strip():
            dlg = WhatsNewDialog(self, changelog)
            dlg.ShowModal()
            dlg.Destroy()
        else:
            wx.MessageBox(
                i18n.t("whats_new_none_message"),
                i18n.t("whats_new_none_title"),
                wx.OK | wx.ICON_INFORMATION,
            )

    def _on_about(self, event=None):
        """Show application authorship, version and license information."""
        i18n = self.i18n
        info = "\n".join(
            textwrap.fill(line, width=100, break_long_words=False, break_on_hyphens=False)
            for line in (
                i18n.t("about_developed_by"),
                "",
                i18n.t("about_special_thanks"),
                i18n.t("about_translation_thanks"),
                i18n.t("about_main_contributors"),
                i18n.t("about_community_thanks"),
                i18n.t("about_translation_thanks_pl"),
                "",
                i18n.t("about_current_version").format(version=__version__),
                i18n.t("about_license"),
            )
        )

        dialog = wx.Dialog(
            self,
            title=self.i18n.t("about_dialog_title"),
            size=(620, 260),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        panel = wx.Panel(dialog)
        sizer = wx.BoxSizer(wx.VERTICAL)
        info_ctrl = wx.TextCtrl(
            panel,
            value=info,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
        )
        sizer.Add(info_ctrl, 1, wx.EXPAND | wx.ALL, 10)
        close_btn = wx.Button(panel, id=wx.ID_OK, label=self.i18n.t("close"))
        sizer.Add(close_btn, 0, wx.ALIGN_RIGHT | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(sizer)
        dialog.ShowModal()
        dialog.Destroy()

    def _on_menu_disconnect(self, event=None):
        """Arquivo > Desconectar / Ctrl+Alt+Shift+D: confirm before disconnecting.

        This is a destructive, easy-to-trigger-by-accident action (wipes the
        paired session and local data) — unlike _on_disconnect() itself,
        which is also called from automatic/internal flows (e.g. WhatsApp
        reporting the device was logged out elsewhere) where a confirmation
        prompt would be wrong, since that already happened without the user
        asking here.
        """
        if wx.MessageBox(
            self.i18n.t("disconnect_confirm_msg"),
            self.i18n.t("disconnect_confirm_title"),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) == wx.YES:
            self._on_disconnect()

    def _on_disconnect(self, event=None, wipe=True):
        """Disconnect from WhatsApp: drop credentials, stop WebSocket and show
        the pairing dialog.

        ``wipe`` (default True) also clears the local database/media — the right
        thing on a *confirmed* logout. Pass wipe=False for a session that simply
        could not be resumed (long QRCODE at startup with no prior connect this
        run): the user needs the pairing dialog, but their history must survive,
        because a failed resume is NOT proof the device was unlinked (a real log
        showed the server logging back in the same second the client wiped)."""
        old_token = self._get_wa_token()
        pi = self.settings.setdefault("privateinfo", {})
        self._set_wa_token("")
        pi.pop("WA_phone_number", None)
        pi.pop("paired", None)
        # WA_phone_number_linked is deliberately NOT dropped here on either
        # path. It describes the data that is on disk, so it goes only when
        # that data goes, and clear_local_data() below owns that — dropping it
        # after the database has been emptied rather than before, which is what
        # keeps a process killed mid-wipe from losing the record while the
        # messages it names are still there. On the wipe=False path it must
        # survive outright: that is precisely the case
        # _wipe_local_data_if_another_number_linked() is for — the history
        # survives, the user is sent to the pairing dialog, another phone scans
        # the code, and with no recorded number the check falls into its "learn
        # it, delete nothing" branch and lets the two accounts merge.
        self.messages_set_completed = False
        self.token = ""
        self.save_settings()
        if wipe:
            self.clear_local_data()
        else:
            logging.warning("[_on_disconnect] resume failed — showing pairing "
                            "dialog WITHOUT wiping local data (history preserved).")
        # Reset the connection state as if this were a fresh app launch, not
        # just a fresh WebSocket. Without this, _wa_connect_announced stayed
        # True from the connection that just ended, which permanently
        # disables _set_wa_connected()'s startup grace window (it only
        # applies while "never_connected_yet") — so the very first
        # not-yet-settled status check after re-pairing (WPPConnect/Chrome
        # still booting a fresh session) looked identical to a real outage
        # and immediately declared full auto-offline, seconds after a
        # successful pairing. Reported live as "reconnected, but the app
        # decided I was offline right away, which was wrong."
        self._wa_connected = False
        self._wa_connect_announced = False
        self._auto_offline = False
        self._wa_offline_strikes = 0
        self._wa_startup_time = time.time()
        self._reset_startup_probe()
        # Best-effort: close the WPPConnect session so Chrome is released.
        if old_token:
            def _close():
                try:
                    import requests as _req
                    _req.post(
                        f"{self.wpp_server}:{self.wpp_port}/api/{old_token}/close-session",
                        headers={"Authorization": f"Bearer {old_token}", "Content-Type": "application/json"},
                        timeout=5,
                    )
                except Exception:
                    pass
            threading.Thread(target=_close, daemon=True).start()
        try:
            if self.ws and self.ws.sio.connected:
                self.ws.sio.disconnect()
        except Exception:
            pass
        self.connect.show_connection_dial()

    def _on_mark_all_read(self, event=None):
        """Mark every conversation with unread messages as read — after asking.

        It is the first item of the Arquivo menu, so a stray Alt followed by
        Enter (or Down, Enter) reaches it. Measured on a real install: an Alt
        at 23:08:59.7 and 1.2 s later every one of 100+ unread chats was read
        on WhatsApp too, with no way back. The dialog defaults to No for
        exactly that keystroke.

        Its "don't show again" checkbox turns that protection off, by the
        user's explicit choice and only together with Yes;
        user_interface.confirm_mark_all_read (Settings > Interface) turns it
        back on.
        """
        unread_jids = [
            jid for jid, chat in list(self.chats.items())
            if int(chat.get("unreadCount") or 0) > 0
        ]
        if not unread_jids:
            self.output(self.i18n.t("mark_all_read_none"), interrupt=True)
            return
        if self.settings.get("user_interface", {}).get("confirm_mark_all_read", True):
            t = self.i18n.t
            confirmed, dont_ask_again = confirm_with_checkbox(
                self,
                t("mark_all_read_confirm").format(count=len(unread_jids)),
                t("menu_mark_all_read"),
                t("mark_all_read_dont_show_again"),
                yes_label=t("yes_button"),
                no_label=t("no_button"),
                checked=False,
                default_yes=False,
            )
            if not confirmed:
                return
            # "Don't show again" is only honoured together with Yes: saying
            # No with it ticked must not turn every later request into an
            # unconfirmed one. Settings > Interface mirrors the same key, so
            # the confirmation can be turned back on from there.
            if dont_ask_again:
                self.settings.setdefault("user_interface", {})["confirm_mark_all_read"] = False
                self.save_settings()
        self.mark_conversations_as_read(unread_jids)

    def _apply_global_hotkey(self):
        """Register (or unregister) the global hotkey from settings."""
        if not hasattr(self, "_hotkey_manager"):
            return
        if self._hotkey_manager is not None:
            self._hotkey_manager.stop()
            self._hotkey_manager = None
        hk = self.settings.get("general", {}).get("global_hotkey")
        if not hk or not isinstance(hk, dict):
            return
        vk  = hk.get("vk", 0)
        mod = hk.get("mod", 0)
        if vk:
            self._hotkey_manager = _HotkeyManager(vk, mod, self.toggle_window_from_hotkey)

    def set_global_hotkey(self, vk: int, mod: int):
        """Save and apply a new global hotkey (vk=0 removes it)."""
        self.settings.setdefault("general", {})
        if vk:
            self.settings["general"]["global_hotkey"] = {"vk": vk, "mod": mod}
        else:
            self.settings["general"].pop("global_hotkey", None)
        self.save_settings()
        self._apply_global_hotkey()

    def _set_status(self, status: str):
        """Update window title and tray tooltip to reflect current status."""
        previous = getattr(self, "_tray_status", "")
        self._tray_status = status
        if status != previous:
            logging.info("[sync-status] %r -> %r", previous, status)
        self._update_title()

    def _set_preparing_status_if_idle(self):
        """Do not let a delayed connection callback regress an active sync."""
        if getattr(self, "_initial_sync_running", False):
            logging.info(
                "[sync-status] Ignoring delayed preparing status; sync is running.")
            return
        self._set_status(self.i18n.t("preparing_to_sync"))

    def _update_title(self):
        """
        Rebuild the frame title from the app name, the account name (multi-
        account), the number of conversations with unread messages and the
        current status, e.g.:
          "WinZapp"
          "WinZapp — Midzi"
          "WinZapp — Midzi (2)"
          "WinZapp — Midzi (2) | modo offline"
          "WinZapp — Midzi (3) | baixando mídias"
        """
        unread_chats = 0
        if not getattr(self, "_initial_sync_running", False):
            deleted = self._deleted_chats
            # Archived conversations are intentionally excluded here — they
            # get their own unread indicator on the "Conversas arquivadas"
            # nav item (see NavigationPanel) instead of inflating the count
            # the user sees in the window title, which used to make the
            # title claim unread conversations that were not visible
            # anywhere in the main conversations list.
            unread_chats = sum(
                1 for jid, chat in list(self.chats.items())
                if jid not in deleted
                and not self.is_chat_archived(jid)
                and not self.is_chat_locked(jid)
                and effective_unread_count(chat) > 0
            )
        # Base title carries the account name (multi-account) + unread count,
        # so the user/screen reader can always tell which account this window
        # is — _format_title is the single source of that logic (previously
        # this method rebuilt the title from scratch and dropped the account
        # name on every refresh).
        title = self._format_title(unread_chats)
        if hasattr(self, "navigation_panel"):
            self.navigation_panel.refresh_archived_label()
        if self.offline_mode:
            title += f" | {self.i18n.t('tray_offline_mode')}"
        if self._tray_status:
            title += f" | {self._tray_status}"
        self.SetTitle(title)
        if getattr(self, "tray_icon", None) is not None:
            self.tray_icon.update_tooltip()

    def _allow_ui_focus_changes(self) -> bool:
        """Return True only when WinZapp is already visible and active."""
        return (
            not self.background_mode
            and not getattr(self, "_window_hidden", False)
            and self.IsShown()
            and not self.IsIconized()
            and self.IsActive()
        )

    def toggle_offline_mode(self):
        """
        Toggle the user-controlled offline mode (tray menu item / Sincronização menu).
        While offline the outgoing message queue is suspended; disabling it
        wakes the queue so pending messages are sent immediately.
        """
        self._user_offline = not self._user_offline
        self.offline_mode_sound.play()
        if self._user_offline:
            self.output(self.i18n.t("offline_mode_enabled"), interrupt=True)
        elif self._auto_offline:
            # Turning the manual switch off does not put us back online when
            # the connection itself is down — say so instead of announcing a
            # state change that did not happen. This is the automatic-offline
            # warning, so it respects the announce_sync_events master mute for
            # the SPEECH; the sound above is the manual toggle's own feedback.
            if self._announce_sync_events_enabled():
                self.output(self.i18n.t("offline_mode_auto_enabled"), interrupt=True)
        else:
            self.output(self.i18n.t("offline_mode_disabled"), interrupt=True)
        self._apply_offline_state()

    def _apply_offline_state(self):
        """Recompute self.offline_mode from its two sources and refresh the UI.

        Safe to call from any thread — the wx work is marshalled with CallAfter.
        """
        effective = bool(self._user_offline or self._auto_offline)
        was_offline = bool(self.offline_mode)
        self.offline_mode = effective
        if was_offline and not effective:
            if getattr(self, "message_queue", None) is not None:
                # Back online: send whatever piled up while we were paused.
                self.message_queue.flush()
            # Leaving offline mode — whether the automatic detector cleared it
            # or the user flipped the tray switch off manually — must force a
            # resync the same way a reconnection does. Previously only the
            # connection health-check path (_set_wa_connected) reset
            # _sync_completed, so toggling the *manual* switch off while the
            # connection itself never actually dropped left the app "online"
            # but permanently stuck on whatever was synced before, since
            # trigger_sync_if_needed() no-ops once _sync_completed is True.
            if getattr(self, "_wa_connected", False):
                self._sync_completed = False
                self._last_sync_attempt_ts = 0
                self.trigger_sync_if_needed()

        def _ui():
            self._update_title()
            if getattr(self, "_sync_offline_menu_item", None) is not None:
                # The menu item reflects the *user* toggle only: an automatic
                # offline caused by a dead connection is not something the user
                # can uncheck, and showing it checked would make the next click
                # a no-op from their point of view.
                self._sync_offline_menu_item.Check(bool(self._user_offline))
        if wx.IsMainThread():
            _ui()
        else:
            wx.CallAfter(_ui)

    def _announce_sync_events_enabled(self) -> bool:
        """Master mute for the sync/media/auto-offline spoken+sound warnings.

        Settings > Geral > "Anunciar sincronização, download de mídias e modo
        offline automático" (on by default). When unchecked, the synchronizing /
        sync-complete / media-download / automatic-offline announcements must
        not fire — no speech, no event sound (status text and the manual
        offline toggle's own feedback are unaffected).
        """
        return bool(self.settings.get("general", {}).get("announce_sync_events", True))

    def _search_normalization_mode(self) -> str:
        """Settings > Geral > "Normalização Unicode nas pesquisas": one of
        "off" (default), "nfd" or "nfkd" — see normalize_for_search().

        Off by default, so searching keeps matching exactly what it always
        did unless the user asks otherwise. Read live on every search rather
        than cached: changing it in Settings then takes effect on the next
        keystroke, with no restart and nothing to invalidate.

        Applies to both searches the user can type into — the conversation
        list (Ctrl+F) and messages inside a conversation (Ctrl+Shift+F) —
        because a single setting that only affected one of them would be its
        own kind of surprise.
        """
        return search_normalization_mode(
            self.settings.get("general", {}).get("search_normalization")
        )
