"""A yes/no confirmation with one checkbox, built from plain wx controls.

Used where WhatsApp-style confirmations carry an option next to the question:
"keep starred messages" when clearing a chat, "don't show again" when marking
every chat as read.

Plain controls on purpose. `wx.RichMessageDialog.ShowCheckBox()` was tried
first: on Windows it is the TaskDialog's verification checkbox, and NVDA read
it as "caixa de seleção, não marcado, somente leitura" — wrong state and not
operable. A real `wx.CheckBox` is read and toggled like any other.

Tab order is checkbox, Yes, No; the message is read as the dialog text. Esc
and Alt+F4 always answer No. Which button Enter presses — and where focus
starts — is the caller's decision (`default_yes`), because it depends on how
costly an accidental confirmation is.
"""

import wx


class CheckboxConfirmDialog(wx.Dialog):
    def __init__(self, parent, message, title, checkbox_label, yes_label, no_label,
                 *, checked: bool, default_yes: bool):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE)
        sizer = wx.BoxSizer(wx.VERTICAL)

        text = wx.StaticText(self, label=message)
        text.Wrap(self.FromDIP(380))
        sizer.Add(text, 0, wx.ALL, 12)

        self._checkbox = wx.CheckBox(self, label=checkbox_label)
        self._checkbox.SetValue(bool(checked))
        sizer.Add(self._checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        yes_btn = wx.Button(self, wx.ID_YES, label=yes_label)
        no_btn = wx.Button(self, wx.ID_NO, label=no_label)
        btn_sizer.Add(yes_btn, 0, wx.RIGHT, 4)
        btn_sizer.Add(no_btn, 0)
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizer(sizer)

        yes_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_YES))
        no_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_NO))
        self.SetEscapeId(wx.ID_NO)

        # Focus starts on the default button, never on the checkbox wx would
        # pick by creation order: there, a habitual Space toggles the option
        # and the following Enter confirms with it changed.
        default_btn = yes_btn if default_yes else no_btn
        default_btn.SetDefault()
        default_btn.SetFocus()

        self.Fit()
        self.Centre()

    def is_checked(self) -> bool:
        return bool(self._checkbox.GetValue())


def confirm_with_checkbox(parent, message: str, title: str, checkbox_label: str, *,
                          yes_label: str, no_label: str, checked: bool, default_yes: bool):
    """Ask a yes/no question with one checkbox.

    Returns `(confirmed, checkbox_checked)`; the checkbox state is read before
    the dialog is destroyed, and is only meaningful when `confirmed` is True.
    """
    dlg = CheckboxConfirmDialog(
        parent, message, title, checkbox_label, yes_label, no_label,
        checked=checked, default_yes=default_yes,
    )
    try:
        confirmed = dlg.ShowModal() == wx.ID_YES
        is_checked = dlg.is_checked()
    finally:
        dlg.Destroy()
    return confirmed, is_checked
