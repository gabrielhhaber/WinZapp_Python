"""Native list filter and entry to WhatsApp custom-list management."""

import wx


def list_result_text(i18n, outcome):
    # Literal keys keep the repository's translation completeness check useful.
    messages = {
        "loaded": i18n.t("wa_lists_loaded"),
        "changed": i18n.t("wa_lists_changed"),
        "unconfirmed": i18n.t("wa_lists_unconfirmed"),
        "refused": i18n.t("wa_lists_refused"),
        "unavailable": i18n.t("wa_lists_unavailable"),
        "failed": i18n.t("wa_lists_failed"),
        "busy": i18n.t("wa_lists_busy"),
        "cancelled": i18n.t("wa_lists_cancelled"),
    }
    return messages.get(outcome, messages["failed"])


class WhatsAppListFilterMixin:
    def _build_wa_list_controls(self, outer_sizer):
        i18n = self.main_window.i18n
        self._wa_list_id = None
        self._wa_list_ids = [None]
        self._wa_list_label = wx.StaticText(self, label=i18n.t("wa_lists_filter"))
        outer_sizer.Add(self._wa_list_label, 0, wx.LEFT | wx.TOP, 5)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self._wa_list_choice = wx.Choice(self, choices=[i18n.t("wa_lists_all")])
        self._wa_list_choice.SetName(i18n.t("wa_lists_filter"))
        self._wa_list_choice.SetSelection(0)
        self._wa_list_choice.Bind(wx.EVT_CHOICE, self._on_wa_list_choice)
        row.Add(self._wa_list_choice, 1, wx.RIGHT, 5)
        self._wa_list_reload = wx.Button(self, label=i18n.t("wa_lists_reload"))
        self._wa_list_reload.Bind(wx.EVT_BUTTON, self._on_wa_list_reload)
        row.Add(self._wa_list_reload, 0, wx.RIGHT, 5)
        self._wa_list_manage = wx.Button(self, label=i18n.t("wa_lists_manage"))
        self._wa_list_manage.Bind(wx.EVT_BUTTON, self._on_wa_list_manage)
        row.Add(self._wa_list_manage)
        outer_sizer.Add(row, 0, wx.EXPAND | wx.ALL, 5)

    def _refresh_wa_list_choices(self):
        if getattr(self.main_window, "_shutting_down", False):
            return
        snapshot = self.main_window._wa_lists_state()
        ids = [None] + [item.id for item in snapshot.lists]
        labels = [self.main_window.i18n.t("wa_lists_all")] + [item.name for item in snapshot.lists]
        selected = self._wa_list_id if self._wa_list_id in ids else None
        choice = self._wa_list_choice
        # A stable read must not rewrite/reannounce the focused native control.
        if ids != self._wa_list_ids or labels != list(choice.GetItems()):
            choice.Freeze()
            try:
                choice.SetItems(labels)
                choice.SetSelection(ids.index(selected))
            finally:
                choice.Thaw()
        self._wa_list_ids = ids
        self._wa_list_id = selected
        self.main_window.add_chats_to_ui()

    def _on_wa_list_choice(self, event):
        index = self._wa_list_choice.GetSelection()
        self._wa_list_id = self._wa_list_ids[index] if 0 <= index < len(self._wa_list_ids) else None
        self.main_window.add_chats_to_ui()
        if self.chats_list:
            self.conversations_list.Focus(0)
            self.conversations_list.Select(0)
            self.conversations_list.EnsureVisible(0)
        # Keep keyboard focus on the choice while NVDA announces its selection.

    def _on_wa_list_reload(self, event):
        self.main_window._request_wa_lists(
            lambda result: self.main_window.output(list_result_text(self.main_window.i18n, result.outcome))
        )

    def _on_wa_list_manage(self, event):
        from ui.dialogs.chat_lists import WhatsAppListsDialog
        dialog = WhatsAppListsDialog(self.main_window)
        try:
            dialog.ShowModal()
        finally:
            dialog.close_list_manager()
            dialog.Destroy()

    def _refresh_wa_list_labels(self):
        i18n = self.main_window.i18n
        self._wa_list_label.SetLabel(i18n.t("wa_lists_filter"))
        self._wa_list_choice.SetName(i18n.t("wa_lists_filter"))
        self._wa_list_reload.SetLabel(i18n.t("wa_lists_reload"))
        self._wa_list_manage.SetLabel(i18n.t("wa_lists_manage"))
        self._refresh_wa_list_choices()
