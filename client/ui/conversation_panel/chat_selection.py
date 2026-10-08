"""ChatSelectionMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

from ui.shortcut_bindings import command_key_event
import wx
from ui.conversation_panel.chat_list_selection import ChatListSelectionMixin


class ChatSelectionMixin(ChatListSelectionMixin):
    """The conversations list's use of the shared chat selection
    (ChatListSelectionMixin): its key handling and the bulk chat actions.
    """


    def _on_conv_list_key_down(self, event):
        """The selection keys (Ctrl+Space, selection-mode Space, Shift+arrows,
        Ctrl+Shift+Space) are ChatListSelectionMixin's, shared with the
        archived and locked lists. Opening a conversation stayed on Enter /
        double-click. Ctrl+P pins/unpins, Ctrl+Shift+Q archives/unarchives."""
        event = command_key_event(self, 'chats', event)
        event = command_key_event(self, 'chat_selection', event)
        key   = event.GetKeyCode()
        ctrl  = event.ControlDown()
        shift = event.ShiftDown()

        if self._handle_chat_selection_key(event):
            return

        if key in (wx.WXK_PAGEUP, wx.WXK_NUMPAD_PAGEUP):
            self._jump_list_by(self.conversations_list, -self._page_jump_size())
        elif key in (wx.WXK_PAGEDOWN, wx.WXK_NUMPAD_PAGEDOWN):
            self._jump_list_by(self.conversations_list, self._page_jump_size())
        elif ctrl and key == ord("P"):
            idx = self.conversations_list.GetFocusedItem()
            if 0 <= idx < len(self.chats_list):
                jid = self.chats_list[idx].get("remoteJid", "")
                if jid:
                    if self.main_window.is_chat_pinned(jid):
                        self._on_menu_unpin(jid)
                    else:
                        self._on_menu_pin(jid)
        # Ctrl+Shift+Q, not plain Ctrl+Q — archiving isn't reversible from a
        # single accidental keystroke the way pinning is, and plain Ctrl+Q
        # sits right next to other single-Ctrl combos a user can easily
        # fat-finger while just navigating the list.
        elif ctrl and shift and key == ord("Q"):
            idx = self.conversations_list.GetFocusedItem()
            if 0 <= idx < len(self.chats_list):
                jid = self.chats_list[idx].get("remoteJid", "")
                if jid:
                    if self.main_window.is_chat_archived(jid):
                        self._on_menu_unarchive(jid)
                    else:
                        self._on_menu_archive(jid)
        else:
            event.Skip()

    def _selected_chat_from_list(self):
        selected = self.conversations_list.GetFirstSelected()
        if selected < 0:
            selected = self.conversations_list.GetFocusedItem()
        if 0 <= selected < len(self.chats_list):
            return self.chats_list[selected]
        return None

    def _on_accel_conversation_data_list(self, event):
        chat = self._selected_chat_from_list()
        if chat:
            self._show_conversation_data(chat=chat)

    def _on_accel_toggle_read_list(self, event):
        self._on_accel_toggle_read_selection(event, self._toggle_read_focused_chat)

    def _toggle_read_focused_chat(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if int(chat.get("unreadCount") or 0) > 0:
            self._on_menu_mark_read(jid)
        else:
            self._on_menu_mark_unread(jid)

    def _on_accel_mute_list(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        self._popup_mute_menu(jid, self.conversations_list)

    def _on_accel_block_list(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid or jid.endswith("@g.us") or self.main_window._is_self_jid(jid):
            return
        self._on_menu_block(chat, jid, self.main_window.is_contact_blocked(jid))

    def _on_accel_clear_list(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            self._on_mass_clear_chats(event)
            return
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid:
                self._on_menu_clear_chat(jid)

    def _on_accel_archive_list(self, event):
        # No bulk "unarchive" action exists in the mass-actions submenu, so
        # the shortcut always archives when a selection exists — matching
        # what "Ações em massa > Arquivar conversas selecionadas" does.
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            self._on_mass_archive_chats(event)
            return
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if self.main_window.is_chat_archived(jid):
            self._on_menu_unarchive(jid)
        else:
            self._on_menu_archive(jid)

    def _on_accel_lock_list(self, event):
        """Ctrl+Shift+T: lock the focused conversation (the context menu's
        "Lock chat"; sets the vault up first if it was never configured)."""
        chat = self._selected_chat_from_list()
        jid = chat.get("remoteJid", "") if chat else ""
        if jid:
            self.main_window.lock_chat(jid)

    def _on_accel_pin_list(self, event):
        """Play/stop the recorded-audio preview while the voice recording is
        paused; otherwise pin/unpin the focused conversation. Both share
        this one accelerator (Ctrl+P) — mutually exclusive contexts, same
        pattern as _on_ctrl_shift_p uses for Ctrl+Shift+P."""
        if self._is_recording and self._recording_paused:
            self._toggle_play_recorded_audio(event)
            return
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if self.main_window.is_chat_pinned(jid):
            self._on_menu_unpin(jid)
        else:
            self._on_menu_pin(jid)

    def _on_accel_bulk_archive_chats(self, event):
        """Ctrl+Alt+Shift+A: archive every selected conversation."""
        self._run_bulk_chat_action(self._on_mass_archive_chats, event)


    # ── Mass action handlers ────────────────────────────────────────────────
    # Act on the Space-toggled selections (self.selected_chats /
    # self.selected_messages), reached from the "mass actions" submenu both
    # context menus grow while a selection exists.

    def _on_mass_archive_chats(self, event):
        i18n = self.main_window.i18n
        if not self.selected_chats: return
        for jid in list(self.selected_chats):
            self.main_window.archive_chat(jid)
        self.selected_chats.clear()
        self.main_window.add_chats_to_ui()
        self.main_window.output(i18n.t("success_archive"), interrupt=True)
