"""Ask the user to accept Meta AI's terms before their first message to it.

WhatsApp refuses every message to Meta AI until the account has accepted its
terms of service (see core/meta_ai.py), so WinZapp asks first, the way
WhatsApp itself does: the terms are one link away and nothing is accepted
until the user ticks the checkbox and confirms.

Plain wx controls only. "Accept" stays disabled until the box is ticked, so
Enter on a fresh dialog cannot accept by habit; Esc and Cancel decline.
"""

import wx
import wx.adv

from core.meta_ai import META_AI_TERMS_URL


class MetaAiTermsDialog(wx.Dialog):
    def __init__(self, parent, i18n):
        super().__init__(
            parent, title=i18n.t("meta_ai_terms_title"), style=wx.DEFAULT_DIALOG_STYLE
        )
        sizer = wx.BoxSizer(wx.VERTICAL)

        text = wx.StaticText(self, label=i18n.t("meta_ai_terms_message"))
        text.Wrap(self.FromDIP(420))
        sizer.Add(text, 0, wx.ALL, 12)

        self.link = wx.adv.HyperlinkCtrl(
            self, wx.ID_ANY, i18n.t("meta_ai_terms_link"), META_AI_TERMS_URL
        )
        sizer.Add(self.link, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.checkbox = wx.CheckBox(self, label=i18n.t("meta_ai_terms_checkbox"))
        self.checkbox.Bind(wx.EVT_CHECKBOX, self._on_checkbox)
        sizer.Add(self.checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.accept_button = wx.Button(self, wx.ID_OK, label=i18n.t("meta_ai_terms_accept"))
        self.accept_button.Enable(False)
        cancel_button = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        buttons.Add(self.accept_button, 0, wx.RIGHT, 4)
        buttons.Add(cancel_button, 0)
        sizer.Add(buttons, 0, wx.ALIGN_CENTER | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizer(sizer)

        self.SetEscapeId(wx.ID_CANCEL)
        self.SetAffirmativeId(wx.ID_OK)
        self.Fit()
        self.Centre()
        self.checkbox.SetFocus()

    def _on_checkbox(self, event):
        self.accept_button.Enable(self.checkbox.GetValue())
        self.accept_button.SetDefault() if self.checkbox.GetValue() else None
        event.Skip()
