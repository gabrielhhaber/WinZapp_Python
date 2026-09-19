import wx


class GroupVoiceCallDialog(wx.Dialog):
    """Accessible participant picker for outgoing WhatsApp group voice calls."""

    def __init__(self, parent, i18n, group_name: str, participants: list[tuple[str, str]]):
        title = i18n.t("group_voice_call_title")
        if group_name:
            title = f"{title}: {group_name}"
        super().__init__(parent, title=title, size=(620, 500))
        self._i18n = i18n
        self._participants = list(participants)

        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        root.Add(
            wx.StaticText(panel, label=i18n.t("group_voice_call_participants_label")),
            0,
            wx.LEFT | wx.TOP | wx.RIGHT,
            10,
        )
        root.Add(
            wx.StaticText(panel, label=i18n.t("group_voice_call_toggle_hint")),
            0,
            wx.LEFT | wx.TOP | wx.RIGHT,
            10,
        )

        self.participants_list = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
        )
        self.participants_list.InsertColumn(
            0,
            i18n.t("group_voice_call_participants_column").replace("&", ""),
            width=520,
        )
        self.participants_list.EnableCheckBoxes(True)
        for name, jid in self._participants:
            label = str(name or jid).strip() or jid
            self.participants_list.Append((label,))
        if self.participants_list.GetItemCount() > 0:
            self.participants_list.Focus(0)
            self.participants_list.Select(0)

        self.participants_list.Bind(wx.EVT_KEY_DOWN, self._on_key_down)
        self.participants_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_item_activated)
        self.participants_list.Bind(wx.EVT_LIST_ITEM_CHECKED, self._on_checked_changed)
        self.participants_list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self._on_checked_changed)
        root.Add(self.participants_list, 1, wx.EXPAND | wx.ALL, 10)

        self.selection_label = wx.StaticText(panel, label="")
        root.Add(self.selection_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        buttons = wx.StdDialogButtonSizer()
        self.start_button = wx.Button(
            panel, wx.ID_OK, label=i18n.t("group_voice_call_start_button")
        )
        cancel_button = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
        buttons.AddButton(self.start_button)
        buttons.AddButton(cancel_button)
        buttons.Realize()
        root.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        self.start_button.Bind(wx.EVT_BUTTON, self._on_start)
        cancel_button.Bind(wx.EVT_BUTTON, lambda _event: self.EndModal(wx.ID_CANCEL))

        panel.SetSizer(root)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self._update_selection_state()
        self.participants_list.SetFocus()

    def _selected_indices(self) -> list[int]:
        return [
            index
            for index in range(self.participants_list.GetItemCount())
            if self.participants_list.IsItemChecked(index)
        ]

    def selected_jids(self) -> list[str]:
        return [self._participants[index][1] for index in self._selected_indices()]

    def _toggle(self, index: int):
        if index < 0 or index >= self.participants_list.GetItemCount():
            return
        self.participants_list.CheckItem(
            index, not self.participants_list.IsItemChecked(index)
        )

    def _on_key_down(self, event):
        if event.GetKeyCode() == wx.WXK_SPACE:
            self._toggle(self.participants_list.GetFirstSelected())
            return
        event.Skip()

    def _on_item_activated(self, event):
        self._toggle(event.GetIndex())

    def _on_checked_changed(self, _event):
        self._update_selection_state()

    def _update_selection_state(self):
        count = len(self._selected_indices())
        self.selection_label.SetLabel(
            self._i18n.t("group_voice_call_selected_count").format(count=count)
        )
        self.start_button.Enable(count >= 2)
        self.Layout()

    def _on_start(self, _event):
        if len(self._selected_indices()) < 2:
            return
        self.EndModal(wx.ID_OK)
