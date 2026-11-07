"""Register named modal content so lock transitions can erase it immediately."""
import wx


def show_sensitive(service, dialog):
    service.register_view(dialog)
    try:
        if dialog.privacy_epoch != service.visibility_epoch:
            dialog.clear_private_state()
            return wx.ID_CANCEL
        result = dialog.ShowModal()
        return result if dialog.privacy_epoch == service.visibility_epoch else wx.ID_CANCEL
    finally:
        service.unregister_view(dialog)


class StorageDetailsDialog(wx.Dialog):
    def __init__(self, owner, text, epoch):
        super().__init__(owner, title=owner.i18n.t("storage_details"), size=(580, 400),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.privacy_epoch = epoch
        self.text = wx.TextCtrl(self, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY,
                                name=owner.i18n.t("storage_details"))
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(self.text, 1, wx.EXPAND | wx.ALL, 8)
        box.Add(self.CreateButtonSizer(wx.CLOSE), 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(box)
        self.SetEscapeId(wx.ID_CLOSE)
        self.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)

    def clear_private_state(self):
        self.text.ChangeValue("")
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
