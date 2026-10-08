"""ArchivedConversationsPanel — the Alt+3 archived chats list.

Moved verbatim out of ui/conversations.py, which re-exports it.
"""

from ui.shortcut_bindings import refresh_popup_shortcuts
from ui.shortcut_bindings import command_key_event
from ui.shortcut_bindings import matches
from ui.shortcut_bindings import make_shortcut_table
import os
import pyperclip
import threading
import wx
from ui.accessible import (
    AccessibleMessagesListControl,
    AccessibleSearchConversations,
)
from ui.dialogs.clear_chat_confirm import confirm_clear_chat
from core.conversation_view import ARCHIVED, mnemonic_letter
from core.utils import format_number
from ui.conversation_panel.chat_menu import ChatMenuMixin
from ui.conversation_panel.chat_list_selection import ChatListSelectionMixin
from core.sound_system import load_sound


class ArchivedConversationsPanel(ChatListSelectionMixin, wx.Panel):
    """
    Shows archived chats in a list.  Activating a chat opens it in the
    main ConversationsPanel.  A context menu allows unarchiving.

    Chats are multi-selectable exactly like the main conversations list — the
    keys, sounds, announcements and settings are ChatListSelectionMixin's, the
    same code the main list runs.
    """

    def __init__(self, main_window, parent):
        super().__init__(parent)
        self.main_window = main_window
        self.chats_list: list = []
        self.chat_names: list = []
        self.selected_chats = set()
        # The same "selected" cue the conversations list plays.
        self.selection_sound = load_sound(
            main_window.sound_system,
            os.path.join("default", "selected.ogg"),
        )
        self._init_ui()
        self.create_accelerator_table()

    def restore_selection(self):
        """Select, focus and give keyboard focus to the first archived
        conversation — mirrors ConversationsPanel._restore_conversation_selection()
        so the list never ends up empty-focused (nothing for a screen reader
        to announce) after navigating here via Alt+4 or the nav-list item."""
        lst = self.conversations_list
        if self.chats_list:
            lst.Focus(0)
            lst.Select(0)
            lst.EnsureVisible(0)
        lst.SetFocus()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _init_ui(self):
        i18n  = self.main_window.i18n
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.conversations_label = wx.StaticText(
            self, label=i18n.t("archived_chats")
        )
        sizer.Add(self.conversations_label, 0, wx.LEFT | wx.TOP, 5)

        # ── Conversation filter tabs ─────────────────────────────────────────
        # Tracks the active filter key: 'all' | 'unread' | 'groups' | 'individual'
        self._conv_filter = 'all'
        self._filter_radio = wx.RadioBox(
            self,
            label=i18n.t("conv_filter_label"),
            choices=[
                i18n.t("conv_filter_all"),
                i18n.t("conv_filter_unread"),
                i18n.t("conv_filter_groups"),
                i18n.t("conv_filter_individual"),
            ],
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
        )
        self._filter_radio.Bind(wx.EVT_RADIOBOX, self._on_filter_changed)
        sizer.Add(self._filter_radio, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        # ── Search ──────────────────────────────────────────────────────────
        # Mirrors ConversationsPanel's own search field (same Ctrl+F shortcut,
        # same Down-arrow-to-first-result behavior) but placed after the
        # filter tabs rather than before them — there is no "Nova conversa"
        # button here to separate the two — and scoped to only this panel's
        # own list: unlike the main panel's search field, which merges in
        # archived results (see MainWindow._conversation_search_candidates()),
        # this one never reaches outside the archived list it sits in.
        self.search_label = wx.StaticText(
            self, label=i18n.t("search_archived_conversations")
        )
        sizer.Add(self.search_label, 0, wx.LEFT | wx.TOP, 5)

        self.search_field = wx.TextCtrl(self, style=wx.TE_DONTWRAP)
        self.search_field.Bind(wx.EVT_TEXT, self.on_search_query_changed)
        self.search_field.Bind(wx.EVT_KEY_DOWN, self._on_search_field_key_down)
        self.search_field.SetAccessible(AccessibleSearchConversations("Ctrl+F"))
        sizer.Add(self.search_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        self.conversations_list = wx.ListCtrl(
            self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL
        )
        self.conversations_list.InsertColumn(0, i18n.t("archived_chats"), width=250)
        # The wx.StaticText above is only a visual caption — on Windows a
        # wx.ListCtrl exposes no accessible name of its own, so NVDA announced
        # this list as a bare, unnamed "list" and the user had no way to tell
        # which of the two conversation lists they had landed in. Give it the
        # same explicit MSAA name treatment the messages list already gets.
        self._list_accessible = AccessibleMessagesListControl(i18n.t("archived_chats"))
        self.conversations_list.SetAccessible(self._list_accessible)
        self.conversations_list.Bind(
            wx.EVT_LIST_ITEM_ACTIVATED, self.on_conversation_selected
        )
        self.conversations_list.Bind(
            wx.EVT_CONTEXT_MENU, self.on_context_menu
        )
        self.conversations_list.Bind(wx.EVT_LIST_ITEM_FOCUSED, self._on_arch_row_focused)
        self.conversations_list.Bind(wx.EVT_KEY_DOWN, self._on_arch_list_key_down)
        sizer.Add(self.conversations_list, 1, wx.EXPAND | wx.ALL, 5)

        self.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook_alt1)

    def _on_char_hook_alt1(self, event):
        if matches(self, event, 'main.ID_ALT_1'):
            self.main_window.on_alt_1(event)
            return
        event.Skip()

    # ── Accelerators ─────────────────────────────────────────────────────────

    def create_accelerator_table(self):
        """Same key combos as ConversationsPanel.create_accelerator_table(),
        applied to this panel instead. The archived list used to have none of
        these at all — Delete and Ctrl+Shift+L (clear) worked in the normal
        list but silently did nothing here, and the row context menu was
        missing everything except unarchive/clear/delete. Ctrl+F now focuses
        this panel's own search field (see _init_ui()), scoped to archived
        chats only — never the main panel's, which additionally merges in
        archived results on a non-empty query. Ctrl+N (new conversation) is
        still left out — there is nowhere to create a conversation from this
        list — and Ctrl+W (close conversation) doesn't apply either: there is
        no split conversation view to close from here. Ctrl+Shift+Q always
        means "unarchive" here rather than toggling, since every row is
        archived by definition.
        """
        self.ID_CTRL_F           = wx.NewIdRef()
        self.ID_DELETE_CONV      = wx.NewIdRef()
        self.ID_ALT_SHIFT_C_LIST = wx.NewIdRef()
        self.ID_CONV_DATA_LIST   = wx.NewIdRef()
        self.ID_TOGGLE_READ_LIST = wx.NewIdRef()
        self.ID_MUTE_LIST        = wx.NewIdRef()
        self.ID_BLOCK_LIST       = wx.NewIdRef()
        self.ID_CLEAR_LIST       = wx.NewIdRef()
        self.ID_UNARCHIVE_LIST   = wx.NewIdRef()
        self.ID_LOCK_LIST        = wx.NewIdRef()
        self.ID_PIN_LIST         = wx.NewIdRef()
        self.ID_ALT_MESSAGES     = wx.NewIdRef()
        # Mass actions (only act while chats are selected) — the same
        # letters as the conversations list's, with "archive" meaning
        # "unarchive" here, like Ctrl+Shift+Q does.
        self.ID_BULK_CLEAR_CHATS     = wx.NewIdRef()  # Ctrl+Alt+Shift+L
        self.ID_BULK_DELETE_CHATS    = wx.NewIdRef()  # Ctrl+Shift+Delete
        self.ID_BULK_UNARCHIVE_CHATS = wx.NewIdRef()  # Ctrl+Alt+Shift+A
        self.ID_BULK_READ_CHATS      = wx.NewIdRef()  # Ctrl+Alt+Shift+R
        self.ID_BULK_UNREAD_CHATS    = wx.NewIdRef()  # Ctrl+Alt+Shift+U
        # Alt+<"&Mensagens" mnemonic>: the messages list of the open
        # conversation, from anywhere in this panel. The label that carries
        # that mnemonic lives in ConversationsPanel, so the native redirect
        # never reaches it from here — an explicit accelerator, on the same
        # letter the i18n label uses (cf. create_accel_conversation).
        messages_letter = mnemonic_letter(
            self.main_window.i18n.t("messages"), "M")
        CS = wx.ACCEL_CTRL | wx.ACCEL_SHIFT
        AS = wx.ACCEL_ALT | wx.ACCEL_SHIFT
        CAS = wx.ACCEL_CTRL | wx.ACCEL_ALT | wx.ACCEL_SHIFT
        accel_tbl = make_shortcut_table(self, 'archived', [
            (wx.ACCEL_CTRL,   ord("F"),        self.ID_CTRL_F),
            (wx.ACCEL_NORMAL, wx.WXK_DELETE, self.ID_DELETE_CONV),
            (AS,              ord("C"),      self.ID_ALT_SHIFT_C_LIST),
            (CS,              ord("D"),      self.ID_CONV_DATA_LIST),
            (CS,              ord("M"),      self.ID_TOGGLE_READ_LIST),
            (AS,              ord("S"),      self.ID_MUTE_LIST),
            (CS,              ord("B"),      self.ID_BLOCK_LIST),
            (CS,              ord("L"),      self.ID_CLEAR_LIST),
            (CS,              ord("Q"),      self.ID_UNARCHIVE_LIST),
            (CS,              ord("T"),      self.ID_LOCK_LIST),
            (wx.ACCEL_CTRL,   ord("P"),      self.ID_PIN_LIST),
            (wx.ACCEL_ALT,    ord(messages_letter), self.ID_ALT_MESSAGES),
            (CAS,             ord("L"),      self.ID_BULK_CLEAR_CHATS),
            (CS,              wx.WXK_DELETE, self.ID_BULK_DELETE_CHATS),
            (CAS,             ord("A"),      self.ID_BULK_UNARCHIVE_CHATS),
            (CAS,             ord("R"),      self.ID_BULK_READ_CHATS),
            (CAS,             ord("U"),      self.ID_BULK_UNREAD_CHATS),
        ])
        self.SetAcceleratorTable(accel_tbl)
        self.Bind(wx.EVT_MENU, self.main_window._on_global_focus_messages,
                  id=self.ID_ALT_MESSAGES)
        self.Bind(wx.EVT_MENU, self.on_ctrl_f,                    id=self.ID_CTRL_F)
        self.Bind(wx.EVT_MENU, self._on_accel_delete,             id=self.ID_DELETE_CONV)
        self.Bind(wx.EVT_MENU, self._on_accel_copy_number,        id=self.ID_ALT_SHIFT_C_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_conversation_data,  id=self.ID_CONV_DATA_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_toggle_read,        id=self.ID_TOGGLE_READ_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_mute,               id=self.ID_MUTE_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_block,              id=self.ID_BLOCK_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_clear,              id=self.ID_CLEAR_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_unarchive,          id=self.ID_UNARCHIVE_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_lock,               id=self.ID_LOCK_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_pin,                id=self.ID_PIN_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_clear_chats,   id=self.ID_BULK_CLEAR_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_delete_chats,  id=self.ID_BULK_DELETE_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_unarchive_chats, id=self.ID_BULK_UNARCHIVE_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_read_chats,    id=self.ID_BULK_READ_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_unread_chats,  id=self.ID_BULK_UNREAD_CHATS)

    def _selected_chat_from_list(self):
        """Mirrors ConversationsPanel._selected_chat_from_list()."""
        selected = self.conversations_list.GetFirstSelected()
        if selected < 0:
            selected = self.conversations_list.GetFocusedItem()
        if 0 <= selected < len(self.chats_list):
            return self.chats_list[selected]
        return None

    def _on_accel_lock(self, event):
        """Ctrl+Shift+T: lock the focused archived conversation."""
        chat = self._selected_chat_from_list()
        jid = chat.get("remoteJid", "") if chat else ""
        if jid:
            self.main_window.lock_chat(jid)

    def _on_accel_delete(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            self._on_mass_delete_chats(event)
            return
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid:
                self._on_delete(jid)

    def _on_accel_copy_number(self, event):
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid and not jid.endswith("@g.us"):
                self._on_copy_number(jid)

    def _on_accel_conversation_data(self, event):
        chat = self._selected_chat_from_list()
        if chat:
            self.main_window.conversations_panel._show_conversation_data(chat=chat)

    def _on_accel_toggle_read(self, event):
        self._on_accel_toggle_read_selection(event, self._toggle_read_focused_chat)

    def _toggle_read_focused_chat(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if int(chat.get("unreadCount") or 0) > 0:
            self._on_mark_read(jid)
        else:
            self._on_mark_unread(jid)

    def _on_accel_mute(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if self.main_window.is_chat_muted(jid):
            self._on_unmute(jid)
        else:
            i18n = self.main_window.i18n
            menu = wx.Menu(i18n.t("mute_chat_menu_title"))
            for key, secs in ChatMenuMixin.MUTE_PRESETS:
                item = menu.Append(wx.ID_ANY, i18n.t(key))
                self.Bind(wx.EVT_MENU, lambda e, j=jid, s=secs: self._on_mute(j, s), item)
            refresh_popup_shortcuts(self, 'archived', menu)
            self.PopupMenu(menu)
            menu.Destroy()

    def _on_accel_block(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid or jid.endswith("@g.us") or self.main_window._is_self_jid(jid):
            return
        self._on_block(chat, jid, self.main_window.is_contact_blocked(jid))

    def _on_accel_clear(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            self._on_mass_clear_chats(event)
            return
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid:
                self._on_clear(jid)

    def _on_accel_unarchive(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            self._on_mass_unarchive_chats(event)
            return
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid:
                self._on_unarchive(jid)

    def _on_accel_pin(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if self.main_window.is_chat_pinned(jid):
            self._on_unpin(jid)
        else:
            self._on_pin(jid)

    # ── Events ────────────────────────────────────────────────────────────────

    def on_search_query_changed(self, event):
        """Mirrors ConversationsPanel.on_search_query_changed(): route through
        add_chats_to_ui() (never _refresh_archived_chats_in_ui() directly) so
        the active filter and this field's own query are applied together and
        every other consequence of a rebuild (focus/selection restore) is
        the one already exercised by the filter tabs above."""
        self.main_window.add_chats_to_ui()

    def on_ctrl_f(self, event):
        self.search_field.SetFocus()

    def _on_search_field_key_down(self, event):
        """Down arrow in the search field moves focus to the first archived
        conversation — mirrors ConversationsPanel._on_search_field_key_down()."""
        if event.GetKeyCode() == wx.WXK_DOWN:
            lst = self.conversations_list
            if lst.GetItemCount() > 0:
                lst.SetFocus()
                lst.Focus(0)
                lst.Select(0)
            return
        event.Skip()

    def _on_filter_changed(self, event):
        """Update the active conversation filter and rebuild the list."""
        _filter_map = ['all', 'unread', 'groups', 'individual']
        sel = self._filter_radio.GetSelection()
        self._conv_filter = _filter_map[sel] if 0 <= sel < len(_filter_map) else 'all'
        self.main_window.add_chats_to_ui()
        # See ConversationsPanel._on_filter_changed's identical comment —
        # same reasoning applies to the archived list's own filter tabs, and
        # keyboard focus (SetFocus()) must stay off the list for the same
        # reason: it cuts NVDA off mid-announcement of the radio option.
        lst = self.conversations_list
        if self.chats_list:
            lst.Focus(0)
            lst.Select(0)
            lst.EnsureVisible(0)

    def _on_arch_row_focused(self, event):
        idx = event.GetIndex()
        if 0 <= idx < len(self.chats_list):
            self._on_chat_row_focused_sound(self.chats_list[idx].get("remoteJid", ""))
        event.Skip()

    def _on_arch_list_key_down(self, event):
        """The selection keys are ChatListSelectionMixin's (identical to the
        conversations list). Plain Space with nothing selected keeps its old
        job here: opening the focused archived chat."""
        event = command_key_event(self, 'archived', event)
        event = command_key_event(self, 'chat_selection', event)
        if self._handle_chat_selection_key(event):
            return
        if event.GetKeyCode() == wx.WXK_SPACE:
            idx = self.conversations_list.GetFocusedItem()
            if idx >= 0:
                self.conversations_list.Select(idx)
                class _E:
                    def GetIndex(self): return idx
                self.on_conversation_selected(_E())
        else:
            event.Skip()

    def on_conversation_selected(self, event):
        index = event.GetIndex()
        try:
            chat = self.chats_list[index]
        except IndexError:
            return
        mw = self.main_window
        # Switch to conversations panel and open the chat there
        mw.archived_conversations_panel.Hide()
        mw.conversations_panel.Show()
        # ConversationsPanel is a split view: its own chat list sits beside the
        # conversation detail pane, both shown together normally. Showing the
        # whole panel here would put that *regular* chat list back on screen
        # and in the Tab order, even though Esc still correctly returns to
        # this archived list — so hide it and show only the detail pane.
        mw.conversations_panel.conversations_label.Hide()
        mw.conversations_panel.conversations_list.Hide()
        mw.content_panel.Layout()
        mw.conversations_panel.navigate_to_conversation(chat, origin=ARCHIVED)

    def on_context_menu(self, event):
        """Same menu, in the same order, as ConversationsPanel.on_conversations_context_menu() —
        this used to offer only Unarchive/Clear/Delete, missing everything
        else the normal list's row menu already had (data, read/unread, mute,
        block, copy number, pin, leave group, add member). Two differences
        from the normal menu: Archive/Unarchive always shows "Desarquivar"
        (every row here is archived by definition, nothing to toggle), and
        "Close conversation" is left out — there is no split conversation view
        to close from this list, unlike the normal one.
        """
        selected = self.conversations_list.GetFirstSelected()
        if selected < 0 or selected >= len(self.chats_list):
            return
        chat = self.chats_list[selected]
        jid  = chat.get("remoteJid", "")
        is_group = jid.endswith("@g.us")
        mw   = self.main_window
        is_self = mw._is_self_jid(jid)
        i18n = mw.i18n
        menu = wx.Menu()

        if self.selected_chats:
            self._append_chat_mass_menu(menu, [
                ("clear_selected_chats", "Ctrl+Alt+Shift+L", self._on_mass_clear_chats),
                ("delete_selected_chats", "Ctrl+Shift+Delete", self._on_mass_delete_chats),
                ("unarchive_selected_chats", "Ctrl+Alt+Shift+A", self._on_mass_unarchive_chats),
                ("mark_selected_read", "Ctrl+Alt+Shift+R", self._on_mass_mark_read_chats),
                ("mark_selected_unread", "Ctrl+Alt+Shift+U", self._on_mass_mark_unread_chats),
            ])

        # ── Conversation / group data ─────────────────────────────────────
        data_label = i18n.t("group_data") if is_group else i18n.t("conversation_data")
        data_item = menu.Append(wx.ID_ANY, f"{data_label}\tCtrl+Shift+D")
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=chat: mw.conversations_panel._show_conversation_data(chat=c),
            data_item,
        )

        menu.AppendSeparator()

        # ── Read / Unread ───────────────────────────────────────────────────
        has_unread = int(chat.get("unreadCount") or 0) > 0
        if has_unread:
            read_item = menu.Append(wx.ID_ANY, f"{i18n.t('mark_as_read')}\tCtrl+Shift+M")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_mark_read(j), read_item)
        else:
            unread_item = menu.Append(wx.ID_ANY, f"{i18n.t('mark_as_unread')}\tCtrl+Shift+M")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_mark_unread(j), unread_item)

        menu.AppendSeparator()

        # ── Mute ──────────────────────────────────────────────────────────
        if mw.is_chat_muted(jid):
            unmute_item = menu.Append(wx.ID_ANY, f"{i18n.t('unmute_chat')}\tAlt+Shift+S")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_unmute(j), unmute_item)
        else:
            mute_sub = wx.Menu()
            for key, secs in ChatMenuMixin.MUTE_PRESETS:
                item = mute_sub.Append(wx.ID_ANY, i18n.t(key))
                self.Bind(wx.EVT_MENU, lambda e, j=jid, s=secs: self._on_mute(j, s), item)
            menu.AppendSubMenu(mute_sub, f"{i18n.t('mute_chat')}\tAlt+Shift+S")

        if not is_group:
            menu.AppendSeparator()
            if not is_self:
                is_blocked = mw.is_contact_blocked(jid)
                label = "unblock_contact" if is_blocked else "block_contact"
                block_item = menu.Append(wx.ID_ANY, f"{i18n.t(label)}\tCtrl+Shift+B")
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, c=chat, j=jid, b=is_blocked: self._on_block(c, j, b),
                    block_item,
                )
            copy_num_item = menu.Append(wx.ID_ANY, f"{i18n.t('copy_number')}\tAlt+Shift+C")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_copy_number(j), copy_num_item)

        menu.AppendSeparator()

        # ── Unarchive — always, every row here is already archived ─────────
        unarch_item = menu.Append(wx.ID_ANY, f"{i18n.t('unarchive_chat')}\tCtrl+Shift+Q")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_unarchive(j), unarch_item)

        # ── Pin / Unpin ───────────────────────────────────────────────────
        if mw.is_chat_pinned(jid):
            unpin_item = menu.Append(wx.ID_ANY, f"{i18n.t('unpin_chat')}\tCtrl+P")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_unpin(j), unpin_item)
        else:
            pin_item = menu.Append(wx.ID_ANY, f"{i18n.t('pin_chat')}\tCtrl+P")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_pin(j), pin_item)

        lock_item = menu.Append(wx.ID_ANY, f"{i18n.t('lock_chat')}	Ctrl+Shift+T")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: mw.lock_chat(j), lock_item)

        menu.AppendSeparator()

        # ── Clear / Delete / Leave ────────────────────────────────────────
        # Clearing an archived conversation used to require opening it first
        # (there was no direct action here) — unlike the main conversations
        # list, whose row context menu can clear a chat in a single step.
        # Offering the same action directly on the archived row removes that
        # extra "open it, then clear it" round trip.
        clear_item = menu.Append(wx.ID_ANY, f"{i18n.t('clear_chat')}\tCtrl+Shift+L")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_clear(j), clear_item)

        del_item = menu.Append(wx.ID_ANY, f"{i18n.t('delete_chat')}\tDelete")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_delete(j), del_item)

        if is_group:
            leave_item = menu.Append(wx.ID_ANY, i18n.t("leave_group"))
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_leave_group(j), leave_item)
            add_member_item = menu.Append(wx.ID_ANY, i18n.t("add_member"))
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid: mw.conversations_panel._on_menu_add_member(j),
                add_member_item,
            )

        refresh_popup_shortcuts(self, 'archived', menu)
        self.PopupMenu(menu)
        menu.Destroy()

    # ── Context menu / accelerator handlers ─────────────────────────────────
    # Mirrors ConversationsPanel's own _on_menu_* handlers. Reimplemented here
    # (rather than delegated to mw.conversations_panel's methods) specifically
    # because several of them show a wx.MessageBox parented on `self` — that
    # has to be THIS visible panel, not the hidden ConversationsPanel a plain
    # delegate call would bind confirmation dialogs to.

    def _on_mark_read(self, jid: str):
        threading.Thread(
            target=self.main_window.mark_conversation_as_read,
            args=(jid,),
            daemon=True,
        ).start()

    def _on_mark_unread(self, jid: str):
        self.main_window.mark_conversation_as_unread(jid)

    def _on_mute(self, jid: str, duration_secs: int):
        self.main_window.mute_chat(jid, duration_secs)

    def _on_unmute(self, jid: str):
        self.main_window.unmute_chat(jid)

    def _on_block(self, chat: dict, jid: str, currently_blocked: bool = False):
        mw = self.main_window
        name = (
            mw._resolve_contact_name(chat)
            or mw.find_name_through_messages(chat)
            or format_number(jid)
        )
        action = "unblock" if currently_blocked else "block"
        msg_key = "unblock_confirm_msg" if currently_blocked else "block_confirm_msg"
        title_key = "unblock_contact" if currently_blocked else "block_contact"
        msg = mw.i18n.t(msg_key).format(name=name)
        if wx.MessageBox(
            msg, mw.i18n.t(title_key), wx.YES_NO | wx.ICON_QUESTION, self,
        ) == wx.YES:
            threading.Thread(
                target=mw.block_contact, args=(jid, action), daemon=True,
            ).start()

    def _on_copy_number(self, jid: str):
        try:
            pyperclip.copy(format_number(jid))
        except Exception:
            pass

    def _on_pin(self, jid: str):
        self.main_window.pin_chat(jid)

    def _on_unpin(self, jid: str):
        self.main_window.unpin_chat(jid)

    def _on_leave_group(self, jid: str):
        i18n = self.main_window.i18n
        if wx.MessageBox(
            i18n.t("leave_group_confirm_msg"), i18n.t("leave_group"),
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        threading.Thread(
            target=self.main_window.leave_group, args=(jid,), daemon=True,
        ).start()

    def _on_unarchive(self, jid: str):
        self.main_window.unarchive_chat(jid)

    def _on_clear(self, jid: str):
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
        # An archived chat can be the one open in the conversation panel.
        panel = getattr(self.main_window, "conversations_panel", None)
        if panel is not None:
            panel._reset_view_after_chat_cleared(jid)
        # Refresh this list so the emptied preview disappears immediately —
        # mirrors ConversationsPanel._on_menu_clear_chat's own refresh call.
        self.main_window._schedule_set_chats()

    def _on_delete(self, jid: str):
        i18n = self.main_window.i18n
        # Mirrors ConversationsPanel._on_menu_delete_chat: delete_chat() (not
        # delete_chat_local()) is what actually sends the delete to the
        # WPPConnect API for a non-group chat. Using the local-only variant
        # here meant a deleted archived 1:1 conversation reappeared on the
        # next full sync, since the server was never told about it.
        confirm_key = "delete_group_confirm_msg" if jid.endswith("@g.us") else "delete_confirm_msg"
        if wx.MessageBox(
            i18n.t(confirm_key),
            i18n.t("delete_chat"),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) == wx.YES:
            self.main_window.delete_chat(jid)

    # ── Selection hooks / mass actions ───────────────────────────────────────

    def _repaint_chat_selection(self):
        """Rows carry the "selecionado" suffix, so a changed set needs a
        repaint — in place (SetItem) since the jids did not change."""
        self.main_window._refresh_archived_chats_in_ui()

    def _reset_after_chat_cleared(self, jid: str):
        # An archived chat can be the one open in the conversation panel.
        panel = getattr(self.main_window, "conversations_panel", None)
        if panel is not None:
            panel._reset_view_after_chat_cleared(jid)

    def _on_mass_unarchive_chats(self, event):
        i18n = self.main_window.i18n
        if not self.selected_chats: return
        for jid in list(self.selected_chats):
            self.main_window.unarchive_chat(jid)
        self.selected_chats.clear()
        self._repaint_chat_selection()
        self.main_window.output(i18n.t("success_unarchive"), interrupt=True)

    def _on_accel_bulk_unarchive_chats(self, event):
        """Ctrl+Alt+Shift+A: unarchive every selected conversation."""
        self._run_bulk_chat_action(self._on_mass_unarchive_chats, event)

    def refresh_labels(self):
        i18n = self.main_window.i18n
        self.conversations_label.SetLabel(i18n.t("archived_chats"))
        col = wx.ListItem()
        col.SetText(i18n.t("archived_chats"))
        self.conversations_list.SetColumn(0, col)
        if getattr(self, "_list_accessible", None) is not None:
            self._list_accessible._label = i18n.t("archived_chats")
        if hasattr(self, "search_label"):
            self.search_label.SetLabel(i18n.t("search_archived_conversations"))

        if hasattr(self, "_filter_radio"):
            self._filter_radio.SetLabel(i18n.t("conv_filter_label"))
            for _fi, _fk in enumerate([
                "conv_filter_all", "conv_filter_unread",
                "conv_filter_groups", "conv_filter_individual",
            ]):
                self._filter_radio.SetItemLabel(_fi, i18n.t(_fk))
