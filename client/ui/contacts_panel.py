"""The Contacts panel of the main navigation (issue #770): the address book,
one row per person, and what can be done with a contact.

A panel of the main window like Calls, Communities and Channels: shown by
main_window/content_panels.py from the navigation list, and read again each
time it is shown. Enter on a contact does what "Enter on a contact" says
(open the conversation, call, video call or the contact's details: Settings
key "contacts_tab"); the buttons and the context menu (Applications key,
Shift+F10) offer every action, Delete deletes the contact. The rows come
from core/contacts_tab.py.
"""
import threading

import wx

from core import phone_contacts
from core.contacts_tab import (ENTER_ACTION_KEYS, ENTER_ACTIONS, SETTINGS_SECTION, contact_rows,
                               enter_action, matching_rows, row_label)
from core.utils import format_number
from ui.dialogs.contact_removal import delete_contact_with_confirmation
from ui.dialogs.labelled_control import add_labelled
from ui.panel_labels import TranslatedLabels

#: (catalog key of the label, handler name), in the order buttons and menu show them
_ACTIONS = (
    ("contacts_tab_open_chat", "_on_open"),
    ("contacts_tab_voice_call", "_on_call"),
    ("contacts_tab_video_call", "_on_video"),
    ("contacts_tab_info", "_on_info"),
    ("contacts_tab_delete", "_on_delete"),
)


class ContactsPanel(TranslatedLabels, wx.Panel):
    def __init__(self, main_window, parent):
        self._mw = main_window
        self._init_labels()
        self._rows, self._shown, self._deleting = [], [], False
        super().__init__(parent)
        i18n = main_window.i18n
        sizer = wx.BoxSizer(wx.VERTICAL)

        def labelled(key, make, proportion=0):
            return self._keep_labelled(add_labelled(self, sizer, i18n.t(key), make, proportion), key)

        self._search = labelled("contacts_tab_search", lambda: wx.TextCtrl(self))
        self._search.Bind(wx.EVT_TEXT, lambda event: self._show_rows())
        self._list = labelled("contacts_tab_list", lambda: wx.ListBox(self, style=wx.LB_SINGLE), 1)
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, lambda event: self._do_enter_action())
        self._list.Bind(wx.EVT_CONTEXT_MENU, self._on_context_menu)
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT, 8)

        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self._buttons = []
        for key, handler in _ACTIONS + (("contacts_tab_new_contact", "_on_new_contact"),):
            control = wx.Button(self, label=i18n.t(key))
            control.Bind(wx.EVT_BUTTON, getattr(self, handler))
            buttons.Add(control, 0, wx.ALL, 4)
            self._buttons.append(self._keep_label(control, key))
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)

        self._enter = labelled("contacts_tab_enter_label", lambda: wx.Choice(
            self, choices=[i18n.t(ENTER_ACTION_KEYS[a]) for a in ENTER_ACTIONS]))
        self._keep_items(self._enter, tuple(ENTER_ACTION_KEYS[a] for a in ENTER_ACTIONS))
        self._enter.SetSelection(ENTER_ACTIONS.index(enter_action(main_window.settings)))
        self._enter.Bind(wx.EVT_CHOICE, self._on_enter_choice)
        self.SetSizer(sizer)
        # Enter means the chosen action on the list, nothing anywhere else.
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

    # ── shown and translated ──────────────────────────────────────────────

    def on_show(self):
        """The navigation list: the contacts are read again, the list gets
        the focus, and how many there are is said when there is none."""
        self.reload()
        self._list.SetFocus()
        if not self._shown:
            self._mw.output(self._status.GetLabel(), interrupt=True)

    def refresh_labels(self):
        """MainWindow switched language: every label, and the rows."""
        self._relabel()
        self._show_rows()

    def reload(self):
        mw = self._mw
        lid_to_phone = dict(getattr(mw, "_lid_to_phone", None) or {})
        self._rows = contact_rows(
            dict(getattr(mw, "contacts", None) or {}), lid_to_phone,
            lambda jid: mw.chat_display_name({"remoteJid": jid}),
            lambda jid: format_number(jid))
        self._show_rows()

    def _show_rows(self):
        """The rows the search keeps, the selected person kept selected."""
        i18n = self._mw.i18n
        keep = self._selected()
        self._shown = matching_rows(self._rows, self._search.GetValue())
        labels = [row_label(name, number, i18n.t("unknown_contact"))
                  for _jid, name, number in self._shown]
        self._list.Freeze()
        try:
            if list(self._list.GetStrings()) != labels:
                self._list.Set(labels)
            if self._shown:
                jids = [jid for jid, _name, _number in self._shown]
                self._list.SetSelection(jids.index(keep[0]) if keep and keep[0] in jids else 0)
        finally:
            self._list.Thaw()
        if not self._rows:
            status = i18n.t("contacts_tab_empty")
        elif not self._shown:
            status = i18n.t("no_results")
        else:
            status = i18n.t("contacts_tab_count").format(count=len(self._shown))
        if self._status.GetLabel() != status:
            self._status.SetLabel(status)
        for button in self._buttons[:len(_ACTIONS)]:
            button.Enable(bool(self._shown))

    def _selected(self):
        """(jid, name, number) of the selected contact, or None."""
        index = self._list.GetSelection()
        return self._shown[index] if 0 <= index < len(self._shown) else None

    # ── keys ──────────────────────────────────────────────────────────────

    def _on_char_hook(self, event):
        key = event.GetKeyCode()
        if wx.Window.FindFocus() is self._list and not event.HasAnyModifiers():
            if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                self._do_enter_action()
                return
            if key in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE):
                self._on_delete()
                return
        event.Skip()

    def _do_enter_action(self):
        handler = {"open": self._on_open, "call": self._on_call,
                   "video": self._on_video, "info": self._on_info}
        handler[enter_action(self._mw.settings)]()

    def _on_enter_choice(self, _event=None):
        index = self._enter.GetSelection()
        if 0 <= index < len(ENTER_ACTIONS):
            self._mw.settings.setdefault(SETTINGS_SECTION, {})["enter_action"] = ENTER_ACTIONS[index]
            self._mw.save_settings()

    def _on_context_menu(self, _event=None):
        contact = self._selected()
        if contact is None:
            return
        i18n = self._mw.i18n
        menu = wx.Menu()
        for key, handler in _ACTIONS:
            item = menu.Append(wx.ID_ANY, i18n.t(key))
            self.Bind(wx.EVT_MENU, getattr(self, handler), item)
        blocked = self._mw.is_contact_blocked(contact[0])
        item = menu.Append(wx.ID_ANY, i18n.t("unblock_contact" if blocked else "block_contact"))
        self.Bind(wx.EVT_MENU, lambda event: self._on_block(not blocked), item)
        self._list.PopupMenu(menu)
        menu.Destroy()

    # ── actions ───────────────────────────────────────────────────────────

    def _on_open(self, _event=None):
        contact = self._selected()
        if contact is None:
            return
        jid, name, _number = contact
        mw = self._mw
        restore = getattr(mw, "restore_deleted_chat", None)
        if restore is not None:
            restore(jid)
        mw.navigate_to_conversation_jid(jid, name)

    def _on_call(self, _event=None):
        contact = self._selected()
        if contact is not None:
            self._mw.start_voice_call(contact[0], contact[1])

    def _on_video(self, _event=None):
        contact = self._selected()
        if contact is not None:
            self._mw.start_video_call(contact[0], contact[1])

    def _on_info(self, _event=None):
        contact = self._selected()
        if contact is None:
            return
        jid, name, _number = contact
        from ui.dialogs.new_conversation import NewConversationDialog
        from ui.dialogs.conversation_data_dialog import ConversationDataDialog
        _norm, chat = NewConversationDialog._find_existing_chat(self._mw, jid)
        # The dialog's Media tab reads the chat's history: a chat hidden in
        # the lock vault needs the PIN first, as navigate_to_conversation_jid.
        stored = (chat or {}).get("remoteJid") or jid
        if self._mw.is_chat_hidden_by_vault(stored):
            if not self._mw.unlock_chat_lock_vault(show_panel=False):
                return
        dialog = ConversationDataDialog(self._mw, chat or {"remoteJid": jid, "pushName": name})
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
        # the details can edit or delete the contact
        if self:
            self.reload()

    def _on_delete(self, _event=None):
        contact = self._selected()
        if contact is None or self._deleting:
            return
        jid, name, _number = contact
        mw = self._mw
        entry = phone_contacts.existing_contact(mw, jid)
        if entry is None:
            return
        stored = phone_contacts.key_of(mw, entry, jid)

        def finish(key):
            mw.remove_local_contact(key)
            if self:
                self.reload()
                self._mw.output(mw.i18n.t("contacts_tab_deleted").format(name=name), interrupt=True)

        def busy(value):
            self._deleting = value
        delete_contact_with_confirmation(mw, self, mw.i18n, entry, stored, name, finish, busy)

    def _on_block(self, block):
        contact = self._selected()
        if contact is None:
            return
        # the confirmation the chat list's menu asks (chat_menu.py)
        i18n = self._mw.i18n
        if wx.MessageBox(
                i18n.t("block_confirm_msg" if block else "unblock_confirm_msg").format(
                    name=contact[1] or i18n.t("unknown_contact")),
                i18n.t("block_contact" if block else "unblock_contact"),
                wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return
        threading.Thread(target=self._mw.block_contact,
                         args=(contact[0], "block" if block else "unblock"),
                         daemon=True, name="contacts-tab-block").start()

    def _on_new_contact(self, _event=None):
        from ui.dialogs.new_contact import NewContactDialog
        dialog = NewContactDialog(self._mw, self)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
        if self:
            self.reload()
