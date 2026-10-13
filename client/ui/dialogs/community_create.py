"""Name, description and first groups of a community about to be created."""

import wx
from core.communities import DESCRIPTION_LIMIT, NAME_LIMIT
from ui.accessible import split_mnemonic


class CommunityCreateDialog(wx.Dialog):
    def __init__(self, parent, main_window, groups):
        self._mw = main_window
        self._i18n = i18n = main_window.i18n
        self._groups = list(groups)  # (jid, name) that may be linked
        self._chosen = []
        self.name = ""
        self.description = ""
        self.group_jids = []
        super().__init__(parent, title=i18n.t("communities_create_title"), size=(480, 400),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)

        def field(key, control, proportion):
            sizer.Add(wx.StaticText(self, label=i18n.t(key)), 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
            control.SetName(split_mnemonic(i18n.t(key))[0])
            sizer.Add(control, proportion, wx.EXPAND | wx.ALL, 8)
            return control

        self._name = field("communities_create_name", wx.TextCtrl(self), 0)
        self._name.SetMaxLength(NAME_LIMIT)
        self._description = field("communities_create_description",
                                  wx.TextCtrl(self, style=wx.TE_MULTILINE), 1)
        self._description.SetMaxLength(DESCRIPTION_LIMIT)
        self._choose = wx.Button(self, label=self._choose_label())
        self._choose.Bind(wx.EVT_BUTTON, self._on_choose)
        self._choose.Enable(bool(self._groups))
        sizer.Add(self._choose, 0, wx.ALL, 8)
        buttons = wx.StdDialogButtonSizer()
        create = wx.Button(self, wx.ID_OK, label=i18n.t("communities_create_action"))
        create.Bind(wx.EVT_BUTTON, self._on_create)
        buttons.AddButton(create)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel")))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)
        self._name.SetFocus()
        self.CentreOnParent()

    def _choose_label(self):
        # The count is part of the label, so the button says what is chosen.
        return self._i18n.t("communities_create_choose_groups").format(count=len(self._chosen))

    def _on_choose(self, event):
        dialog = wx.MultiChoiceDialog(self, self._i18n.t("communities_create_pick_groups"),
                                      self._i18n.t("communities_create_title"),
                                      [name for _jid, name in self._groups])
        try:
            dialog.SetSelections(self._chosen)
            if dialog.ShowModal() != wx.ID_OK:
                return
            self._chosen = list(dialog.GetSelections())
        finally:
            dialog.Destroy()
        self._choose.SetLabel(self._choose_label())

    def _on_create(self, event):
        name = self._name.GetValue().strip()
        if not name:
            self._mw.speak_output.output(self._i18n.t("communities_create_name_required"))
            self._name.SetFocus()
            return
        self.name = name
        self.description = self._description.GetValue().strip()
        self.group_jids = [self._groups[index][0] for index in self._chosen]
        self.EndModal(wx.ID_OK)
