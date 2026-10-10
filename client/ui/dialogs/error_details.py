"""Copyable error reports shared by installation and update flows."""

import wx

from core.dialog_foreground import bring_to_front_if_hidden


def error_text(message: str, details: str = "") -> str:
    return f"{message}\n\n{details}" if details else message


class ErrorDetailsDialog(wx.Dialog):
    def __init__(self, parent, i18n, message: str, title: str, details: str = ""):
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._build_ui(i18n, message, details)

    def _build_ui(self, i18n, message, details):
        margin = self.FromDIP(12)
        sizer = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(self, label=i18n.t("error_details_label"))
        sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, margin)
        # A multiline native edit supports navigation, selection and Ctrl+C.
        self.text = wx.TextCtrl(self, value=error_text(message, details),
                                style=wx.TE_MULTILINE | wx.TE_READONLY,
                                size=self.FromDIP((620, 300)))
        sizer.Add(self.text, 1, wx.EXPAND | wx.ALL, margin)
        close = wx.Button(self, wx.ID_CANCEL, label=i18n.t("close"))
        sizer.Add(close, 0, wx.ALIGN_RIGHT | wx.LEFT | wx.RIGHT | wx.BOTTOM, margin)
        self.SetSizerAndFit(sizer)
        self.SetEscapeId(wx.ID_CANCEL)
        self.CentreOnParent()
        self.text.SetInsertionPoint(0)
        self.text.SetFocus()


def show_error_details(parent, i18n, message: str, title: str, details: str = "",
                       announce=None) -> None:
    dialog = ErrorDetailsDialog(parent, i18n, message, title, details)
    try:
        bring_to_front_if_hidden(parent, dialog, announce)
        dialog.ShowModal()
    finally:
        dialog.Destroy()
