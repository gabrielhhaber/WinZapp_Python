"""Native custom-list manager; tests invoke methods on plain recording stubs."""

import re

import wx
from core.chat_lists import list_contains
from ui.conversation_panel.chat_lists import list_editing_unavailable_text, list_result_text


def _manage_title(i18n):
    """The window title: the same words as the main-screen button, whose
    label carries a mnemonic marker a title would show as a literal '&'.
    "&&", a literal ampersand in a label, stays one."""
    return re.sub(r"&(.)", r"\1", i18n.t("wa_lists_manage"))


class WhatsAppListsDialog(wx.Dialog):
    def __init__(self, main_window):
        self._mw, self._closed, self._busy, self._ids = main_window, False, False, []
        super().__init__(main_window, title=_manage_title(main_window.i18n),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        main_window._wa_lists_dialog = self
        self._build_list_manager()
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_list_manager_destroy)
        self.SetMinSize((470, 360))
        self.Fit()
        self.CentreOnParent()
        self._reload_lists(None)

    def _build_list_manager(self):
        i18n, sizer = self._mw.i18n, wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(self, label=i18n.t("wa_lists_scope"))
        explanation.Wrap(460)
        sizer.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(wx.StaticText(self, label=i18n.t("wa_lists_filter")), 0, wx.LEFT, 8)
        self._choice = wx.Choice(self)
        self._choice.SetName(i18n.t("wa_lists_filter"))
        self._choice.Bind(wx.EVT_CHOICE, self._on_list_selected)
        sizer.Add(self._choice, 0, wx.EXPAND | wx.ALL, 8)
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.EXPAND | wx.ALL, 8)
        self._buttons = {}
        for action, label, handler in (
            ("reload", i18n.t("wa_lists_reload"), self._reload_lists),
            ("create", i18n.t("wa_lists_create"), self._create_list),
            ("rename", i18n.t("wa_lists_rename"), self._rename_list),
            ("remove", i18n.t("wa_lists_delete"), self._delete_list),
            ("addChats", i18n.t("wa_lists_add"), self._add_list_chats),
            ("removeChats", i18n.t("wa_lists_remove"), self._remove_list_chats),
        ):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            self._buttons[action] = button
            sizer.Add(button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        # Every action above applies at once, so there is nothing to cancel:
        # the window is closed. CreateStdDialogButtonSizer(wx.CANCEL) gave
        # wx's stock "Cancel", in English whatever the locale and with no
        # mnemonic. ID_CANCEL keeps Escape closing it.
        buttons = wx.StdDialogButtonSizer()
        self._close_button = wx.Button(self, wx.ID_CANCEL, i18n.t("wa_lists_close"))
        buttons.AddButton(self._close_button)
        buttons.Realize()
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

    def close_list_manager(self):
        self._closed = True
        if getattr(self._mw, "_wa_lists_dialog", None) is self:
            self._mw._wa_lists_dialog = None

    def _on_list_manager_destroy(self, event):
        if event.GetEventObject() is self:
            self.close_list_manager()
        event.Skip()

    def invalidate_list_manager(self):
        if not self._closed:
            self.close_list_manager()
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)

    def _selected_list(self):
        index = self._choice.GetSelection()
        return self._mw._wa_lists_state().find(self._ids[index]) if 0 <= index < len(self._ids) else None

    def _refresh_list_manager(self):
        old, snapshot = self._selected_list(), self._mw._wa_lists_state()
        ids, labels = [item.id for item in snapshot.lists], [item.name for item in snapshot.lists]
        if ids != self._ids or labels != list(self._choice.GetItems()):
            self._choice.Freeze()
            try:
                self._choice.SetItems(labels)
                self._choice.SetSelection(ids.index(old.id) if old and old.id in ids else (0 if ids else wx.NOT_FOUND))
            finally:
                self._choice.Thaw()
        self._ids = ids
        item = self._selected_list()
        for action, button in self._buttons.items():
            allowed = action == "reload" or (snapshot.can_edit and (action == "create" or item is not None))
            button.Enable(allowed and not self._busy)

    def _on_list_selected(self, event):
        self._refresh_list_manager()

    def _submit_list_command(self, command=None):
        if self._closed or self._busy:
            return
        self._busy = True
        self._refresh_list_manager()
        self._status.SetLabel(self._mw.i18n.t("wa_lists_loading"))

        def complete(result):
            if self._closed:
                return
            self._busy = False
            self._refresh_list_manager()
            text = list_result_text(self._mw.i18n, result.outcome)
            snapshot = self._mw._wa_lists_state()
            if result.outcome == "loaded" and not snapshot.can_edit:
                text += " " + list_editing_unavailable_text(self._mw.i18n, snapshot)
            self._status.SetLabel(text)
            self.Layout()
            self._mw.output(text)

        self._mw._request_wa_lists(complete, command)

    def _reload_lists(self, event):
        self._submit_list_command()

    def _ask_list_name(self, current=""):
        i18n = self._mw.i18n
        dialog = wx.TextEntryDialog(self, i18n.t("wa_lists_name"), _manage_title(i18n), value=current)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return None
            name = dialog.GetValue().strip()
        finally:
            dialog.Destroy()
        if not name or len(name.encode("utf-16-le")) // 2 > 100:
            self._mw.output(i18n.t("wa_lists_name_invalid"))
            return None
        return name

    def _create_list(self, event):
        name = self._ask_list_name()
        if name is not None:
            self._submit_list_command({"action": "create", "name": name})

    def _rename_list(self, event):
        item = self._selected_list()
        if item is not None:
            name = self._ask_list_name(item.name)
            if name is not None and name != item.name:
                self._submit_list_command({"action": "rename", "id": item.id, "name": name})

    def _delete_list(self, event):
        item, i18n = self._selected_list(), self._mw.i18n
        if item is not None and wx.MessageBox(
            i18n.t("wa_lists_delete_confirm").format(name=item.name), _manage_title(i18n),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, parent=self,
        ) == wx.YES:
            self._submit_list_command({"action": "remove", "id": item.id})

    def _change_list_members(self, action):
        item = self._selected_list()
        if item is None:
            return
        mapping = dict(getattr(self._mw, "_lid_to_phone", {}))
        want_member = action == "removeChats"
        rows = [(jid, name) for jid, name in self._mw._wa_list_candidates()
                if list_contains(item, jid, mapping) == want_member]
        i18n = self._mw.i18n
        if not rows:
            self._mw.output(i18n.t("wa_lists_no_chats"))
            return
        prompt = i18n.t("wa_lists_pick_remove") if want_member else i18n.t("wa_lists_pick_add")
        dialog = wx.MultiChoiceDialog(self, prompt.format(name=item.name),
                                      _manage_title(i18n), [name for _jid, name in rows])
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            indices = dialog.GetSelections()
        finally:
            dialog.Destroy()
        if not indices:
            return
        command = self._mw._wa_list_member_command(item, action, [rows[index][0] for index in indices])
        if command is None:
            self._mw.output(i18n.t("wa_lists_selection_changed"))
            return
        self._submit_list_command(command)

    def _add_list_chats(self, event):
        self._change_list_members("addChats")

    def _remove_list_chats(self, event):
        self._change_list_members("removeChats")
