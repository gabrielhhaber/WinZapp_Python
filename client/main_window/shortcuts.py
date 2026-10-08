"""ShortcutsMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

from ui.shortcut_bindings import make_shortcut_table
import wx
from core.conversation_view import ARCHIVED, MAIN


class ShortcutsMixin:
    """Accelerator table, Alt+N navigation, settings/language entry points and the
    output() speech funnel.
    """

    def create_accelerator_table(self):
        #Set IDs
        self.ID_ALT_1      = wx.NewIdRef()
        self.ID_ALT_2      = wx.NewIdRef()
        self.ID_ALT_3      = wx.NewIdRef()
        self.ID_ALT_4      = wx.NewIdRef()
        self.ID_ALT_5      = wx.NewIdRef()
        self.ID_ALT_6      = wx.NewIdRef()
        self.ID_ALT_7      = wx.NewIdRef()
        self.ID_ALT_NAV    = wx.NewIdRef()
        self.ID_CTRL_COMMA = wx.NewIdRef()
        self.ID_F1         = wx.NewIdRef()
        self.ID_ALT_T      = wx.NewIdRef()
        self.ID_CTRL_ALT_SHIFT_P = wx.NewIdRef()
        self.ID_CTRL_ALT_SHIFT_H = wx.NewIdRef()
        self.ID_CTRL_ALT_SHIFT_G = wx.NewIdRef()

        # navigation_panel's "&Navegação principal" label mnemonic is meant
        # to redirect Alt+N to nav_list, but that native StaticText-mnemonic
        # redirect proved unreliable elsewhere in this app (see the Alt+D/
        # Alt+M fixes in ConversationsPanel.create_accel_conversation) —
        # reported live as barely ever working. An explicit global
        # accelerator, extracted from the same i18n mnemonic so it still
        # tracks the label instead of hardcoding "N", works unconditionally
        # from anywhere in the window instead of depending on that mechanism.
        nav_letter = "N"
        _nav_label = self.i18n.t("main_nav")
        _amp = _nav_label.find("&")
        if 0 <= _amp < len(_nav_label) - 1 and _nav_label[_amp + 1].isalpha():
            nav_letter = _nav_label[_amp + 1].upper()

        #create accelerator table
        accel_tbl = make_shortcut_table(self, 'main', [
            (wx.ACCEL_ALT,    ord('1'),    self.ID_ALT_1),
            (wx.ACCEL_ALT,    ord('2'),    self.ID_ALT_2),
            (wx.ACCEL_ALT,    ord('3'),    self.ID_ALT_3),
            (wx.ACCEL_ALT,    ord('4'),    self.ID_ALT_4),
            (wx.ACCEL_ALT,    ord('5'),    self.ID_ALT_5),
            (wx.ACCEL_ALT,    ord('6'),    self.ID_ALT_6),
            (wx.ACCEL_ALT,    ord('7'),    self.ID_ALT_7),
            (wx.ACCEL_ALT,    ord(nav_letter), self.ID_ALT_NAV),
            (wx.ACCEL_CTRL,   ord(','),    self.ID_CTRL_COMMA),
            (wx.ACCEL_NORMAL, wx.WXK_F1,  self.ID_F1),
            (wx.ACCEL_ALT,    ord('T'),    self.ID_ALT_T),
            (wx.ACCEL_CTRL | wx.ACCEL_ALT | wx.ACCEL_SHIFT, ord('P'), self.ID_CTRL_ALT_SHIFT_P),
            # Quick device switch (main_window/quick_audio_devices.py): H for
            # output, G for recording, the pair side by side on the keyboard.
            (wx.ACCEL_CTRL | wx.ACCEL_ALT | wx.ACCEL_SHIFT, ord('H'), self.ID_CTRL_ALT_SHIFT_H),
            (wx.ACCEL_CTRL | wx.ACCEL_ALT | wx.ACCEL_SHIFT, ord('G'), self.ID_CTRL_ALT_SHIFT_G),
        ])
        self.SetAcceleratorTable(accel_tbl)
        self.Bind(wx.EVT_MENU, self.on_alt_1,       id=self.ID_ALT_1)
        self.Bind(wx.EVT_MENU, self._on_global_alt2, id=self.ID_ALT_2)
        self.Bind(wx.EVT_MENU, self._on_global_alt3, id=self.ID_ALT_3)
        self.Bind(wx.EVT_MENU, self.on_alt_4,       id=self.ID_ALT_4)
        self.Bind(wx.EVT_MENU, self.on_alt_5,       id=self.ID_ALT_5)
        self.Bind(wx.EVT_MENU, self.on_alt_6,       id=self.ID_ALT_6)
        self.Bind(wx.EVT_MENU, self.on_alt_7,       id=self.ID_ALT_7)
        self.Bind(wx.EVT_MENU, self._on_alt_nav,    id=self.ID_ALT_NAV)
        self.Bind(wx.EVT_MENU, self.on_ctrl_comma,  id=self.ID_CTRL_COMMA)
        self.Bind(wx.EVT_MENU, self.on_f1,          id=self.ID_F1)
        self.Bind(wx.EVT_MENU, self._on_global_alt_t, id=self.ID_ALT_T)
        self.Bind(wx.EVT_MENU, self._on_global_toggle_audio_playback, id=self.ID_CTRL_ALT_SHIFT_P)
        self.Bind(wx.EVT_MENU, self._on_quick_output_devices, id=self.ID_CTRL_ALT_SHIFT_H)
        self.Bind(wx.EVT_MENU, self._on_quick_input_devices, id=self.ID_CTRL_ALT_SHIFT_G)
        from ui.shortcut_bindings import refresh_mnemonics
        refresh_mnemonics(self)

    def _on_alt_nav(self, event):
        """Alt+N (or the localized equivalent): focus the main navigation list."""
        if hasattr(self, "navigation_panel"):
            self.navigation_panel.nav_list.SetFocus()

    def _ensure_conversations_panel_visible(self):
        """Show the open conversation, in the panel it belongs to, for an
        explicit ask (Alt+2, Alt+3, Alt+M from any panel).

        A plain panel switch leaves the conversation open but hidden, so
        these shortcuts would otherwise jump focus inside a hidden pane and
        nothing would visibly happen. This reveals it first
        (ConversationsPanel.reveal_open_conversation) and moves no focus: the
        caller puts it on the messages.
        """
        # A plain panel switch leaves the open conversation hidden, so this
        # explicit ask is what shows it, in the panel it belongs to. The
        # caller puts focus (on the messages), so none is moved here.
        self.conversations_panel.reveal_open_conversation()

    def _on_global_alt2(self, event):
        """Alt+2: jump to last message regardless of which panel has focus."""
        cp = getattr(self, "conversations_panel", None)
        if cp is not None and cp.conversation is not None:
            self._ensure_conversations_panel_visible()
            cp._on_accel_jump_last(event)

    def _on_global_alt3(self, event):
        """Alt+3: jump to unread separator regardless of which panel has focus."""
        cp = getattr(self, "conversations_panel", None)
        if cp is not None and cp.conversation is not None:
            self._ensure_conversations_panel_visible()
            cp._on_accel_jump_unread(event)

    def _on_global_focus_messages(self, event):
        """Alt+M from a panel that has no messages label of its own (the
        archived list): bring the open conversation forward and focus its
        messages list, like _on_global_alt2 does for the last message."""
        cp = getattr(self, "conversations_panel", None)
        if cp is not None and cp.conversation is not None:
            self._ensure_conversations_panel_visible()
        if cp is not None:
            cp._on_accel_focus_list(event)

    def on_f1(self, event):
        from ui.dialogs.shortcuts_dialog import ShortcutsDialog
        dlg = ShortcutsDialog(self)
        dlg.ShowModal()
        dlg.Destroy()

    def on_ctrl_comma(self, event):
        self.open_settings()

    def open_settings(self):
        # A hidden vault can only expose its Settings tab while its secret-code
        # session is still unlocked. Preserve that existing session; only start
        # Settings from a locked state when the vault was not already open.
        was_unlocked = bool(getattr(self, "_chat_lock_unlocked", False))
        if not was_unlocked:
            self.lock_chat_vault(silent=True, show_conversations=False)
        from ui.dialogs.settings_dialog import SettingsDialog
        if was_unlocked:
            # The frame's key hook never sees input while this modal is up, so
            # the inactivity timer would lock the vault under the open tab and
            # Apply would drop the vault edits without a word. Pause it here.
            self._cancel_chat_lock_timeout()
        dlg = SettingsDialog(self)
        dlg.ShowModal()
        dlg.Destroy()
        # If Settings itself authenticated a previously locked vault, close
        # that temporary session again. An already-open session belongs to the
        # caller and must remain open.
        if not was_unlocked and getattr(self, "_chat_lock_unlocked", False):
            self.lock_chat_vault(silent=True, show_conversations=False)
        elif was_unlocked:
            self.touch_chat_lock_timeout()

    def _refresh_call_language_surfaces(self):
        """Re-translate call UI that can stay alive while Settings is open."""
        details_map = getattr(self, "_incoming_call_details", {})
        dialogs = getattr(self, "_incoming_call_dialogs", {})
        for identity, details in list(details_map.items()):
            if not isinstance(details, dict):
                continue
            name = details.get("name") or self.i18n.t("unknown_contact")
            announcement_key = (
                "incoming_video_call_announcement"
                if details.get("is_video")
                else "incoming_call_announcement"
            )
            message = self.i18n.t(announcement_key).format(name=name)
            details["message"] = message
            dialog = dialogs.get(identity)
            refresh = getattr(dialog, "refresh_labels", None)
            if callable(refresh):
                refresh(message=message)

        # The in-window incoming-call bar reads details["message"], so this
        # repaints its text as well as the buttons after the message above was
        # regenerated in the new language.
        if hasattr(self, "incoming_call_bar"):
            self.incoming_call_answer_button.SetLabel(
                self.i18n.t("incoming_call_answer_button")
            )
            self.incoming_call_reject_button.SetLabel(
                self.i18n.t("incoming_call_reject_button")
            )
            self.incoming_call_stop_button.SetLabel(
                self.i18n.t("incoming_call_silence_button")
            )
            self._sync_incoming_call_bar()

        # The call frame is created once at startup and reused for every call.
        # Relabel every persistent control; _sync_voice_call_bar() supplies the
        # active voice/video title, participant text, mute state and video
        # on/off state without recreating the window.
        if hasattr(self, "voice_call_window_end_button"):
            self.voice_call_window_end_button.SetLabel(
                self.i18n.t("voice_call_end_button")
            )
            self.voice_call_window_settings_button.SetLabel(
                self.i18n.t("voice_call_settings_button")
            )
            self.voice_call_window.SetTitle(self.i18n.t("voice_call_window_title"))
            self._sync_voice_call_bar()

    def apply_language_changes(self):
        """Refresh all visible and already-materialized text after a language change."""
        if not hasattr(self, "navigation_panel"):
            return
        self.navigation_panel.refresh_labels()
        self.conversations_panel.refresh_labels()
        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel.refresh_labels()
        if hasattr(self, "locked_conversations_panel"):
            self.locked_conversations_panel.refresh_labels()
        if hasattr(self, "status_panel"):
            self.status_panel.refresh_labels()
        if hasattr(self, "calls_panel"):
            self.calls_panel.refresh_labels()

        self._refresh_call_language_surfaces()

        # Message rows and chat-list previews contain translated runtime text
        # ("Mensagem apagada", media labels, delivery/status wording, dates,
        # etc.). refresh_labels() only changes static controls, so repaint the
        # already-materialized rows too; otherwise those strings keep the old
        # language until the chat is reopened or the application restarts.
        cp = self.conversations_panel
        if getattr(cp, "conversation", None) is not None:
            cp.populate_messages(preserve_focus=True)
        self._chats_ui_fp = None
        self.add_chats_to_ui()

        # Update frame title (unread indicator + any status suffix)
        self._refresh_status_language()
        self._update_title()
        self.main_panel.Layout()
        # Refresh tray icon tooltip with new language
        if self.tray_icon is not None:
            self.tray_icon.refresh_labels()
        # The Accounts menu contains dynamic radio items as well as its title
        # and actions. Rebuild it on a language change; merely relabelling the
        # fixed menus leaves those items in the language used at startup.
        if getattr(self, "account_id", None) and getattr(self, "registry", None):
            self._build_menubar()
        else:
            self._refresh_menubar()
        from ui.shortcut_bindings import refresh_shortcuts
        refresh_shortcuts(self)

    def on_alt_1(self, event):
        self.lock_chat_vault(silent=True, show_conversations=False)
        # Brings the main chat list back (a conversation opened from the
        # archived or locked list had hidden it), hides any conversation that
        # belongs to another panel, and restores focus AND selection in the
        # list so it never ends up empty-focused.
        self.conversations_panel.show_chat_panel(MAIN)

    def on_alt_4(self, event):
        self.lock_chat_vault(silent=True, show_conversations=False)
        if hasattr(self, "archived_conversations_panel"):
            self.conversations_panel.show_chat_panel(ARCHIVED)

    def on_alt_5(self, event):
        self.lock_chat_vault(silent=True, show_conversations=False)
        # A conversation left open (archived or not) while switching to
        # Status kept sending typing/recording presence updates for it in
        # the background — the user has no way to see or act on that once
        # this tab's out of view, so close it the same way Esc would,
        # without close_conversation()'s own focus-restoration (this panel
        # sets its own focus right below).
        if self.conversations_panel.conversation is not None:
            self.conversations_panel.close_conversation_for_panel_switch()
        self.conversations_panel.Hide()
        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel.Hide()
        if hasattr(self, "calls_panel"):
            self.calls_panel.Hide()
        if hasattr(self, "status_panel"):
            self.status_panel.Show()
            self.content_panel.Layout()
            self.status_panel._add_status_btn.SetFocus()
            self.status_panel.on_show()

    def on_alt_6(self, event):
        """Alt+6: the Calls tab (calls_panel.py), same switch as Alt+5."""
        self.lock_chat_vault(silent=True, show_conversations=False)
        if self.conversations_panel.conversation is not None:
            self.conversations_panel.close_conversation_for_panel_switch()
        self.conversations_panel.Hide()
        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel.Hide()
        if hasattr(self, "locked_conversations_panel"):
            self.locked_conversations_panel.Hide()
        if hasattr(self, "status_panel"):
            self.status_panel.Hide()
        if hasattr(self, "calls_panel"):
            self.calls_panel.Show()
            self.content_panel.Layout()
            self.calls_panel.on_show()

    def on_alt_7(self, event):
        """Alt+7: open the locked-chats vault, the same entry point as the
        optional "Conversas trancadas" nav row (prompts for the PIN). A
        vault that was never set up (vault.configured is False) does
        nothing, silently -- this shortcut cannot itself reveal that a
        vault exists to someone who doesn't already know the PIN."""
        self.show_locked_chats_panel()

    def output(self, text, interrupt=False):
        self.speak_output.output(text, interrupt=interrupt)

    def _voice_recording_silence_active(self):
        """True while Settings > Conteúdo Falado's "silence while recording"
        toggle should be muting all speech — i.e. the setting is on AND a
        voice message is actually being recorded right now. Passed as
        AccessibleSpeechOutput's suppressed_getter, so it's ignored entirely
        (returns False) whenever no recording is in progress."""
        if not self.settings.get("speech_content", {}).get("silence_while_recording", False):
            return False
        cp = getattr(self, "conversations_panel", None)
        # Mixed capture intentionally includes screen-reader speech and must
        # also announce device failures while the partial take stays open.
        return bool(cp is not None and getattr(cp, "_is_recording", False)
                    and not getattr(cp, "_recording_system_audio", False))
