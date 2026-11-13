"""The kinds of media of the Online AI page, in a window of their own.

Which received media the online AI handles: pictures, stickers, videos, voice
messages and PDFs. They lived on the page itself, which then had no letter
left for the "Message translation" button in any language (a "(&Z)" after the
label made NVDA read the letter twice). Reached from a button on the page now,
like the automatic options (ai_auto_options.py), with letters of their own.

The check boxes are built once and kept; AISettingsPage reads them as before
(``page.toggles``, apply()), and the settings' search finds them and opens the
window on the option (SettingsPages.add_subpage).
"""

import wx

#: The button on the page that opens the window, and the window's title.
BUTTON_KEY = "ai_kinds_button"


class AIKindsPanel(wx.Panel):
    """One check box per kind of media; *toggles* is ((kind, i18n key), ...)."""

    def __init__(self, parent, page, toggles, kinds):
        super().__init__(parent)
        self._page = page
        self._keys = dict(toggles)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.toggles = {}
        for kind, key in toggles:
            control = wx.CheckBox(self, label=page._t(key))
            control.SetValue(kinds[kind])
            sizer.Add(control, 0, wx.ALL, 8)
            self.toggles[kind] = control
        self.SetSizer(sizer)

    def refresh_labels(self) -> None:
        for kind, control in self.toggles.items():
            control.SetLabel(self._page._t(self._keys[kind]))
