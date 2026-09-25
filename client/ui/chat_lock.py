"""Accessible wx controls for the locked-conversation vault."""

from __future__ import annotations

import pyperclip
import wx

from core.utils import effective_unread_count, normalize_for_search
from ui.accessible import AccessibleMessagesListControl


ID_FORGOT_PIN = wx.NewIdRef()


def _labelled_text(parent, sizer, label, *, password=False, readonly=False):
    caption = wx.StaticText(parent, label=label)
    sizer.Add(caption, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
    style = wx.TE_DONTWRAP
    if password:
        style |= wx.TE_PASSWORD
    if readonly:
        style |= wx.TE_READONLY
    field = wx.TextCtrl(parent, style=style)
    field.SetName(label)
    sizer.Add(field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
    return field


class ChatLockSetupDialog(wx.Dialog):
    def __init__(self, parent, i18n):
        super().__init__(parent, title=i18n.t("chat_lock_setup_title"))
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.pin = _labelled_text(
            self, sizer, i18n.t("chat_lock_pin_label"), password=True
        )
        self.confirm = _labelled_text(
            self, sizer, i18n.t("chat_lock_pin_confirm_label"), password=True
        )
        self.reveal = _labelled_text(
            self, sizer, i18n.t("chat_lock_reveal_label")
        )
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.SetMinSize((460, -1))
        ok_button = self.FindWindow(wx.ID_OK)
        if ok_button:
            ok_button.SetDefault()
        self.pin.SetFocus()


class RecoveryKeyDialog(wx.Dialog):
    def __init__(self, parent, i18n, recovery_code: str):
        super().__init__(parent, title=i18n.t("chat_lock_recovery_title"))
        self._i18n = i18n
        self._recovery_code = recovery_code
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(
            wx.StaticText(self, label=i18n.t("chat_lock_recovery_explanation")),
            0, wx.EXPAND | wx.ALL, 10,
        )
        self.code = _labelled_text(
            self, sizer, i18n.t("chat_lock_recovery_label"), readonly=True
        )
        self.code.SetValue(recovery_code)
        key_actions = wx.BoxSizer(wx.HORIZONTAL)
        self.read_button = wx.Button(
            self, label=i18n.t("chat_lock_recovery_read")
        )
        self.copy_button = wx.Button(
            self, label=i18n.t("chat_lock_recovery_copy")
        )
        self.read_button.Bind(wx.EVT_BUTTON, self._on_read)
        self.copy_button.Bind(wx.EVT_BUTTON, self._on_copy)
        key_actions.Add(self.read_button, 0, wx.ALL, 5)
        key_actions.Add(self.copy_button, 0, wx.ALL, 5)
        sizer.Add(key_actions, 0, wx.ALIGN_LEFT | wx.LEFT | wx.RIGHT, 5)
        self.saved = wx.CheckBox(self, label=i18n.t("chat_lock_recovery_saved"))
        sizer.Add(self.saved, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        buttons = self.CreateSeparatedButtonSizer(wx.OK)
        if buttons:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.SetMinSize((520, -1))
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        ok_button = self.FindWindow(wx.ID_OK)
        if ok_button:
            ok_button.SetDefault()
        # A read-only TextCtrl does not reliably announce its value in NVDA
        # when focus arrives by Tab/Shift+Tab. Put focus on an explicit action
        # that speaks the key on demand; the field remains available for
        # selection and ordinary Ctrl+C.
        self.read_button.SetFocus()

    def _announce(self, text: str):
        output = getattr(self.GetParent(), "output", None)
        if callable(output):
            output(text, interrupt=True)

    def _on_read(self, event):
        spoken_code = ". ".join(self._recovery_code.split("-"))
        self._announce(
            f'{self._i18n.t("chat_lock_recovery_label")}: {spoken_code}'
        )

    def _on_copy(self, event):
        try:
            pyperclip.copy(self._recovery_code)
        except Exception:
            self._announce(self._i18n.t("chat_lock_recovery_copy_failed"))
            return
        self._announce(self._i18n.t("chat_lock_recovery_copied"))

    def _on_ok(self, event):
        if not self.saved.GetValue():
            wx.MessageBox(
                self.GetParent().i18n.t("chat_lock_recovery_must_save"),
                self.GetTitle(), wx.OK | wx.ICON_WARNING, self,
            )
            self.saved.SetFocus()
            return
        self.EndModal(wx.ID_OK)


class ChatLockUnlockDialog(wx.Dialog):
    def __init__(self, parent, i18n):
        super().__init__(parent, title=i18n.t("chat_lock_unlock_title"))
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.pin = _labelled_text(
            self, sizer, i18n.t("chat_lock_pin_label"), password=True
        )
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        open_button = wx.Button(self, wx.ID_OK, i18n.t("chat_lock_open"))
        forgot_button = wx.Button(self, ID_FORGOT_PIN, i18n.t("chat_lock_forgot_pin"))
        cancel_button = wx.Button(self, wx.ID_CANCEL, i18n.t("cancel"))
        forgot_button.Bind(
            wx.EVT_BUTTON,
            lambda event: self.EndModal(int(ID_FORGOT_PIN)),
        )
        buttons.Add(open_button, 0, wx.ALL, 5)
        buttons.Add(forgot_button, 0, wx.ALL, 5)
        buttons.Add(cancel_button, 0, wx.ALL, 5)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 5)
        self.SetSizerAndFit(sizer)
        self.SetMinSize((430, -1))
        open_button.SetDefault()
        self.pin.SetFocus()


class ChatLockRecoveryDialog(wx.Dialog):
    def __init__(self, parent, i18n):
        super().__init__(parent, title=i18n.t("chat_lock_reset_title"))
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.recovery = _labelled_text(
            self, sizer, i18n.t("chat_lock_recovery_label")
        )
        self.pin = _labelled_text(
            self, sizer, i18n.t("chat_lock_new_pin_label"), password=True
        )
        self.confirm = _labelled_text(
            self, sizer, i18n.t("chat_lock_new_pin_confirm_label"), password=True
        )
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.SetMinSize((500, -1))
        ok_button = self.FindWindow(wx.ID_OK)
        if ok_button:
            ok_button.SetDefault()
        self.recovery.SetFocus()


class ChatLockChangePinDialog(wx.Dialog):
    def __init__(self, parent, i18n):
        super().__init__(parent, title=i18n.t("chat_lock_change_pin_title"))
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.current = _labelled_text(
            self, sizer, i18n.t("chat_lock_current_pin_label"), password=True
        )
        self.pin = _labelled_text(
            self, sizer, i18n.t("chat_lock_new_pin_label"), password=True
        )
        self.confirm = _labelled_text(
            self, sizer, i18n.t("chat_lock_new_pin_confirm_label"), password=True
        )
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.SetMinSize((500, -1))
        ok_button = self.FindWindow(wx.ID_OK)
        if ok_button:
            ok_button.SetDefault()
        self.current.SetFocus()


class ChatLockRevealDialog(wx.Dialog):
    def __init__(self, parent, i18n):
        super().__init__(parent, title=i18n.t("chat_lock_change_reveal_title"))
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.pin = _labelled_text(
            self, sizer, i18n.t("chat_lock_pin_label"), password=True
        )
        self.reveal = _labelled_text(
            self, sizer, i18n.t("chat_lock_reveal_label")
        )
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.SetMinSize((460, -1))
        ok_button = self.FindWindow(wx.ID_OK)
        if ok_button:
            ok_button.SetDefault()
        self.pin.SetFocus()


class LockedConversationsPanel(wx.Panel):
    """List of locked chats, only populated while the vault is unlocked."""

    def __init__(self, main_window, parent):
        super().__init__(parent)
        self.main_window = main_window
        self.chats_list: list[dict] = []
        self.chat_names: list[str] = []
        self._all_chats_list: list[dict] = []
        self._all_chat_names: list[str] = []
        self._init_ui()

    def _init_ui(self):
        i18n = self.main_window.i18n
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.heading = wx.StaticText(self, label=i18n.t("locked_chats"))
        sizer.Add(self.heading, 0, wx.LEFT | wx.TOP, 5)

        self.search_label = wx.StaticText(self, label=i18n.t("search_locked_chats"))
        sizer.Add(self.search_label, 0, wx.LEFT | wx.TOP, 5)
        self.search_field = wx.TextCtrl(self, style=wx.TE_DONTWRAP)
        self.search_field.SetName(i18n.t("search_locked_chats"))
        self.search_field.Bind(wx.EVT_TEXT, self._on_search)
        self.search_field.Bind(wx.EVT_KEY_DOWN, self._on_search_key)
        sizer.Add(self.search_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        self.conversations_list = wx.ListCtrl(
            self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL
        )
        self.conversations_list.InsertColumn(0, i18n.t("locked_chats"), width=280)
        self._list_accessible = AccessibleMessagesListControl(i18n.t("locked_chats"))
        self.conversations_list.SetAccessible(self._list_accessible)
        self.conversations_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_open)
        self.conversations_list.Bind(wx.EVT_CONTEXT_MENU, self._on_context_menu)
        self.conversations_list.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        sizer.Add(self.conversations_list, 1, wx.EXPAND | wx.ALL, 5)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.close_button = wx.Button(self, label=i18n.t("chat_lock_close"))
        self.close_button.Bind(wx.EVT_BUTTON, self._on_close)
        actions.Add(self.close_button, 0, wx.ALL, 5)
        sizer.Add(actions, 0, wx.ALIGN_RIGHT)
        self.SetSizer(sizer)

    def refresh_labels(self):
        i18n = self.main_window.i18n
        self.heading.SetLabel(i18n.t("locked_chats"))
        self.search_label.SetLabel(i18n.t("search_locked_chats"))
        self.search_field.SetName(i18n.t("search_locked_chats"))
        self.close_button.SetLabel(i18n.t("chat_lock_close"))
        column = wx.ListItem()
        column.SetText(i18n.t("locked_chats"))
        self.conversations_list.SetColumn(0, column)
        self._list_accessible._label = i18n.t("locked_chats")
        self.refresh()

    def set_all_chats(self, chats: list[dict], names: list[str]):
        self._all_chats_list = list(chats)
        self._all_chat_names = list(names)
        self.refresh()

    def refresh(self):
        fold = self.main_window._search_normalization_mode()
        query = normalize_for_search(self.search_field.GetValue().strip(), fold)
        chats, names, rows = [], [], []
        for index, chat in enumerate(self._all_chats_list):
            name = self._all_chat_names[index] if index < len(self._all_chat_names) else ""
            if query and query not in normalize_for_search(name, fold):
                continue
            unread = effective_unread_count(chat)
            unread_text = ""
            if unread:
                key = "unread_messages" if unread != 1 else "unread_message"
                unread_text = f" {unread} {self.main_window.i18n.t(key)}"
            preview = self.main_window._last_msg_preview(chat)
            row = f"{name}{unread_text}"
            if preview:
                row += f" {preview}"
            chats.append(chat)
            names.append(name)
            rows.append(row)

        focused_jid = ""
        focused = self.conversations_list.GetFocusedItem()
        if 0 <= focused < len(self.chats_list):
            focused_jid = self.chats_list[focused].get("remoteJid", "")
        self.conversations_list.Freeze()
        try:
            self.conversations_list.DeleteAllItems()
            for row in rows:
                self.conversations_list.Append((row,))
            if not rows:
                # Same idea as the message list's empty notice: a bare empty
                # list tells a screen-reader user nothing. chats_list stays
                # empty, so activating this row opens nothing.
                self.conversations_list.Append(
                    (self.main_window.i18n.t("chat_lock_none"),))
        finally:
            self.conversations_list.Thaw()
        self.chats_list = chats
        self.chat_names = names
        if not rows:
            self.conversations_list.Focus(0)
            self.conversations_list.Select(0)
        if chats:
            target = next((
                index for index, chat in enumerate(chats)
                if chat.get("remoteJid", "") == focused_jid
            ), 0)
            self.conversations_list.Focus(target)
            self.conversations_list.Select(target)

    def restore_selection(self):
        if self.conversations_list.GetItemCount():
            self.conversations_list.Focus(0)
            self.conversations_list.Select(0)
            self.conversations_list.EnsureVisible(0)
        self.conversations_list.SetFocus()

    def _selected_chat(self):
        index = self.conversations_list.GetFirstSelected()
        if index < 0:
            index = self.conversations_list.GetFocusedItem()
        return self.chats_list[index] if 0 <= index < len(self.chats_list) else None

    def _on_search(self, event):
        self.refresh()

    def _on_search_key(self, event):
        if event.GetKeyCode() == wx.WXK_DOWN and self.chats_list:
            self.restore_selection()
            return
        event.Skip()

    def _on_list_key(self, event):
        if event.GetKeyCode() == wx.WXK_SPACE:
            chat = self._selected_chat()
            if chat:
                self.main_window.open_locked_conversation(chat)
            return
        event.Skip()

    def _on_open(self, event):
        chat = self._selected_chat()
        if chat:
            self.main_window.open_locked_conversation(chat)

    def _on_context_menu(self, event):
        chat = self._selected_chat()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        menu = wx.Menu()
        open_item = menu.Append(wx.ID_ANY, self.main_window.i18n.t("chat_lock_open_chat"))
        unlock_item = menu.Append(wx.ID_ANY, self.main_window.i18n.t("unlock_chat"))
        self.Bind(wx.EVT_MENU, lambda evt, c=chat: self.main_window.open_locked_conversation(c), open_item)
        self.Bind(wx.EVT_MENU, lambda evt, j=jid: self.main_window.unlock_chat(j), unlock_item)
        self.PopupMenu(menu)
        menu.Destroy()

    def _on_close(self, event):
        self.main_window.lock_chat_vault()
