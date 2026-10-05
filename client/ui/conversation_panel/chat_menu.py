"""ChatMenuMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import pyperclip
import threading
import wx
from ui.dialogs.clear_chat_confirm import confirm_clear_chat
from core.utils import format_number


class ChatMenuMixin:
    """The conversations-list context menu and its chat actions (read, mute,
    block, archive, pin, clear, delete, leave).
    """

    # ── Conversations context menu ──────────────────────────────────────────

    def on_conversations_context_menu(self, event):
        selected_index = self.conversations_list.GetFirstSelected()
        if selected_index == -1:
            return
        try:
            chat = self.chats_list[selected_index]
        except IndexError:
            return
        jid      = chat.get("remoteJid", "")
        is_group = jid.endswith("@g.us")
        is_self  = self.main_window._is_self_jid(jid)
        mw       = self.main_window
        i18n     = mw.i18n

        menu = wx.Menu()

        if getattr(self, "selected_chats", None):
            # Each entry carries its own dedicated shortcut (see
            # create_accelerator_table's ID_BULK_*_CHATS) — those work
            # whatever "Substituir atalhos por ações em massa..." is set to,
            # unlike the single-chat shortcuts this submenu's actions used to
            # be reachable through only when that setting was on.
            self._append_chat_mass_menu(menu, [
                ("clear_selected_chats", "Ctrl+Alt+Shift+L", self._on_mass_clear_chats),
                ("delete_selected_chats", "Ctrl+Shift+Delete", self._on_mass_delete_chats),
                ("archive_selected_chats", "Ctrl+Alt+Shift+A", self._on_mass_archive_chats),
                ("mark_selected_read", "Ctrl+Alt+Shift+R", self._on_mass_mark_read_chats),
                ("mark_selected_unread", "Ctrl+Alt+Shift+U", self._on_mass_mark_unread_chats),
            ])

        # ── Conversation / group data ─────────────────────────────────────
        data_label = i18n.t("group_data") if is_group else i18n.t("conversation_data")
        data_item = menu.Append(wx.ID_ANY, f"{data_label}\tCtrl+Shift+D")
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=chat: self._show_conversation_data(chat=c),
            data_item,
        )

        menu.AppendSeparator()

        # ── Read / Unread — mutually exclusive: show only the applicable one ──
        has_unread = int(chat.get("unreadCount") or 0) > 0
        if has_unread:
            read_item = menu.Append(wx.ID_ANY, f"{i18n.t('mark_as_read')}\tCtrl+Shift+M")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_mark_read(j), read_item)
        else:
            unread_item = menu.Append(wx.ID_ANY, f"{i18n.t('mark_as_unread')}\tCtrl+Shift+M")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_mark_unread(j), unread_item)

        menu.AppendSeparator()

        # ── Mute ──────────────────────────────────────────────────────────
        if mw.is_chat_muted(jid):
            unmute_item = menu.Append(wx.ID_ANY, f"{i18n.t('unmute_chat')}\tAlt+Shift+S")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_unmute(j), unmute_item)
        else:
            menu.AppendSubMenu(
                self._build_mute_menu(jid), f"{i18n.t('mute_chat')}\tAlt+Shift+S"
            )

        if not is_group:
            menu.AppendSeparator()
            if not is_self:
                is_blocked = mw.is_contact_blocked(jid)
                label = "unblock_contact" if is_blocked else "block_contact"
                block_item = menu.Append(wx.ID_ANY, f"{i18n.t(label)}\tCtrl+Shift+B")
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, c=chat, j=jid, b=is_blocked: self._on_menu_block(c, j, b),
                    block_item,
                )
            copy_num_item = menu.Append(wx.ID_ANY, f"{i18n.t('copy_number')}\tAlt+Shift+C")
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid: self._on_menu_copy_number(j),
                copy_num_item,
            )

        menu.AppendSeparator()

        # ── Archive / Unarchive ───────────────────────────────────────────
        if mw.is_chat_archived(jid):
            ua_item = menu.Append(wx.ID_ANY, f"{i18n.t('unarchive_chat')}\tCtrl+Shift+Q")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_unarchive(j), ua_item)
        else:
            arch_item = menu.Append(wx.ID_ANY, f"{i18n.t('archive_chat')}\tCtrl+Shift+Q")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_archive(j), arch_item)

        # ── Pin / Unpin ───────────────────────────────────────────────────
        if mw.is_chat_pinned(jid):
            unpin_item = menu.Append(wx.ID_ANY, f"{i18n.t('unpin_chat')}\tCtrl+P")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_unpin(j), unpin_item)
        else:
            pin_item = menu.Append(wx.ID_ANY, f"{i18n.t('pin_chat')}\tCtrl+P")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_pin(j), pin_item)

        lock_item = menu.Append(wx.ID_ANY, f"{i18n.t('lock_chat')}	Ctrl+Shift+T")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: mw.lock_chat(j), lock_item)

        menu.AppendSeparator()

        # ── Clear / Delete / Leave ────────────────────────────────────────
        sync_stars_item = menu.Append(wx.ID_ANY, i18n.t("star_sync_local"))
        self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_sync_local_stars(j), sync_stars_item)
        if getattr(mw, "_star_sync_job", None) is not None:
            stop_stars_item = menu.Append(wx.ID_ANY, i18n.t("star_sync_stop"))
            self.Bind(wx.EVT_MENU, lambda e: self._on_cancel_star_sync(), stop_stars_item)

        clear_item = menu.Append(wx.ID_ANY, f"{i18n.t('clear_chat')}\tCtrl+Shift+L")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_clear_chat(j), clear_item)

        delete_item = menu.Append(wx.ID_ANY, f"{i18n.t('delete_chat')}\tDelete")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_delete_chat(j), delete_item)

        if is_group:
            leave_item = menu.Append(wx.ID_ANY, i18n.t("leave_group"))
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid: self._on_menu_leave_group(j),
                leave_item,
            )
            add_member_item = menu.Append(wx.ID_ANY, i18n.t("add_member"))
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid: self._on_menu_add_member(j),
                add_member_item,
            )

        # "Fechar conversa" only makes sense for the conversation this menu
        # was actually opened on — showing it for every row regardless let
        # the user right-click chat B, pick "Fechar conversa", and have it
        # silently close chat A instead (on_context_menu_close() has no idea
        # which jid the menu was for; it just closes whatever's currently
        # open).
        if (self.conversation and self.conversation.get("remoteJid") == jid
                and self.conversation_panel.IsShown()):
            menu.AppendSeparator()
            close_item = menu.Append(wx.ID_ANY, f"{i18n.t('close_conversation')}\tCtrl+W")
            self.Bind(wx.EVT_MENU, self.on_context_menu_close, close_item)

        self.PopupMenu(menu)
        menu.Destroy()

    def on_context_menu_close(self, event):
        if self.conversation_panel.IsShown():
            self.close_conversation(event)
            return
        # Ctrl+W with nothing open used to do nothing at all, silently — see
        # _no_conversation_open_announced() (issue #86).
        self._no_conversation_open_announced()

    # ── Conversation context menu handlers ───────────────────────────────────

    def _on_menu_mark_read(self, jid: str):
        threading.Thread(
            target=self.main_window.mark_conversation_as_read,
            args=(jid,),
            daemon=True,
        ).start()

    def _on_menu_mark_unread(self, jid: str):
        self.main_window.mark_conversation_as_unread(jid)

    # Mute presets, in the order WhatsApp itself offers them. Kept in one place
    # so the row context menu and the Alt+Shift+S accelerator can never drift
    # apart — they build the exact same menu from this list.
    MUTE_PRESETS = (
        ("mute_1h", 3600),
        ("mute_3h", 10800),
        ("mute_8h", 28800),
        ("mute_1d", 86400),
        ("mute_1w", 604800),
        ("mute_always", -1),
    )

    def _build_mute_menu(self, jid: str) -> wx.Menu:
        """A wx.Menu of mute durations for *jid*, each wired to _on_menu_mute()."""
        i18n = self.main_window.i18n
        mute_sub = wx.Menu()
        for key, secs in self.MUTE_PRESETS:
            item = mute_sub.Append(wx.ID_ANY, i18n.t(key))
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid, s=secs: self._on_menu_mute(j, s),
                item,
            )
        return mute_sub

    def _popup_mute_menu(self, jid: str, anchor: wx.Window):
        """Alt+Shift+S: let the user pick how long to mute for.

        This used to mute for a hardcoded 8 hours with no prompt, so the
        shortcut could not express any of the durations the context menu
        offers — and silently picked one the user never chose. Show the same
        menu instead. Unmuting stays a single keypress: there is nothing to
        choose, and the context menu shows only "unmute" in that state too.
        """
        if self.main_window.is_chat_muted(jid):
            self._on_menu_unmute(jid)
            return
        i18n = self.main_window.i18n
        menu = wx.Menu(i18n.t("mute_chat_menu_title"))
        for key, secs in self.MUTE_PRESETS:
            item = menu.Append(wx.ID_ANY, i18n.t(key))
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid, s=secs: self._on_menu_mute(j, s),
                item,
            )
        # Popped up on the control that has keyboard focus so the screen reader
        # follows it there instead of to an arbitrary screen position.
        (anchor or self).PopupMenu(menu)
        menu.Destroy()

    def _on_menu_mute(self, jid: str, duration_secs: int):
        self.main_window.mute_chat(jid, duration_secs)

    def _on_menu_unmute(self, jid: str):
        self.main_window.unmute_chat(jid)

    def _on_menu_block(self, chat: dict, jid: str, currently_blocked: bool = False):
        name = (
            self.main_window._resolve_contact_name(chat)
            or self.main_window.find_name_through_messages(chat)
            or format_number(jid)
        )
        action = "unblock" if currently_blocked else "block"
        msg_key = "unblock_confirm_msg" if currently_blocked else "block_confirm_msg"
        title_key = "unblock_contact" if currently_blocked else "block_contact"
        msg = self.main_window.i18n.t(msg_key).format(name=name)
        if wx.MessageBox(
            msg,
            self.main_window.i18n.t(title_key),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) == wx.YES:
            threading.Thread(
                target=self.main_window.block_contact,
                args=(jid, action),
                daemon=True,
            ).start()

    def _on_menu_copy_number(self, jid: str):
        number = format_number(jid)
        try:
            pyperclip.copy(number)
        except Exception:
            pass

    def _on_menu_archive(self, jid: str):
        # Close conversation if currently open
        if self.conversation and self.conversation.get("remoteJid") == jid:
            self.close_conversation()
        self.main_window.archive_chat(jid)

    def _on_menu_unarchive(self, jid: str):
        self.main_window.unarchive_chat(jid)

    def _on_menu_pin(self, jid: str):
        self.main_window.pin_chat(jid)

    def _on_menu_unpin(self, jid: str):
        self.main_window.unpin_chat(jid)

    def _reset_view_after_chat_cleared(self, jid: str):
        """Empty the open conversation's list and selection after *jid* was
        cleared, when *jid* is the open one.

        Every "clear chat" entry point has to come through here — the chat
        list's menu, the mass action on marked chats and the archived list.
        Only the first one used to reset the panel, so clearing the open chat
        from either of the other two left its rows on screen and, since the
        selection mode is on whenever selected_messages is non-empty, marked
        messages that no longer existed: Space kept marking instead of playing
        and the first Esc announced "all unmarked" instead of closing.
        """
        if not (self.conversation and self.conversation.get("remoteJid") == jid):
            return
        _old_rows = self._sorted_messages
        self._sorted_messages = []
        self._sync_message_rows(_old_rows, [])
        self.selected_messages.clear()
        # _unread_sep_idx pointed into the list just emptied above — left
        # stale, a live message arriving right after (on_incoming_message,
        # the branch for a separator anchoring an already-read position)
        # would pop() that now-out-of-range index from the now-empty
        # _sorted_messages, crashing with
        # "IndexError: pop from empty list". Same pairing already reset
        # on conversation switch (see close_conversation()).
        self._unread_sep_idx = -1
        self._sep_anchors_read_position = False
        # A âncora vai junto: populate_messages() recria o separador a
        # partir dela, e um id que não existe mais em records deixaria a
        # conversa limpa carregando um separador fantasma.
        self._first_unread_msg_id = None
        self._first_unread_count = 0

    def _on_menu_clear_chat(self, jid: str):
        i18n = self.main_window.i18n
        confirmed, keep_starred = confirm_clear_chat(
            self,
            i18n.t("clear_confirm_msg"),
            i18n.t("clear_chat"),
            i18n.t("clear_chat_keep_starred"),
            yes_label=i18n.t("yes_button"),
            no_label=i18n.t("no_button"),
        )
        if not confirmed:
            return
        self.main_window.clear_chat(jid, keep_starred=keep_starred)
        self._reset_view_after_chat_cleared(jid)
        # Refresh the conversations list so the emptied preview disappears.
        # The conversation itself stays in the list — clearing is not deleting.
        self.main_window._schedule_set_chats()

    def _on_menu_delete_chat(self, jid: str):
        i18n = self.main_window.i18n
        # Deleting a group is local-only (see MainWindow.delete_chat: asking
        # WhatsApp to delete a group chat makes it exit the group first). Say so
        # in the prompt, so the difference from "Sair do grupo" is explicit.
        confirm_key = "delete_group_confirm_msg" if jid.endswith("@g.us") else "delete_confirm_msg"
        if wx.MessageBox(
            i18n.t(confirm_key),
            i18n.t("delete_chat"),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) != wx.YES:
            return
        if self.conversation and self.conversation.get("remoteJid") == jid:
            self.close_conversation()
        self.main_window.delete_chat(jid)

    def _on_menu_leave_group(self, jid: str):
        i18n = self.main_window.i18n
        # Own confirmation text: this one really does remove the user from the
        # group, and it used to share the generic "delete conversation" wording.
        if wx.MessageBox(
            i18n.t("leave_group_confirm_msg"),
            i18n.t("leave_group"),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) != wx.YES:
            return
        if self.conversation and self.conversation.get("remoteJid") == jid:
            self.close_conversation()
        threading.Thread(
            target=self.main_window.leave_group,
            args=(jid,),
            daemon=True,
        ).start()

    def _on_menu_add_member(self, group_jid: str):
        """Open the add-member dialog for a group."""
        from ui.dialogs.add_member_dialog import AddMemberDialog
        dlg = AddMemberDialog(self.main_window, group_jid)
        dlg.ShowModal()
        dlg.Destroy()
