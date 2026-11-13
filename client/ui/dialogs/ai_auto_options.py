"""The automatic options of the Online AI page, in a window of their own.

Transcribing and describing received messages on arrival, which conversations
that reaches, how old a message may be, and the daily ceiling with what was
spent today. They lived on the page itself, which then had more controls than
there are letters, so two of them always shared an Alt+letter in some
language. Reached from a button on the page now, as the Transcription tab's
groups are (transcription_windows.py), with letters of their own.

The controls are built once and kept; AISettingsPage reads and writes them as
before (apply(), refresh_labels(), _on_auto_toggle()), and the settings' search
finds them and opens the window on the option (SettingsPages.add_subpage).
"""

import wx

from core import conversation_auto_ai
from core.ai_media import auto as ai_auto
from core.transcription import auto_queue

#: The button on the page that opens the window, and the window's title.
BUTTON_KEY = "ai_auto_button"


def window_title(label: str) -> str:
    return label.replace("&", "").rstrip(".… ").strip()


class AIAutoOptionsPanel(wx.Panel):
    """The six automatic options; *page* is the AISettingsPage they belong to."""

    def __init__(self, parent, page, config):
        super().__init__(parent)
        self._page = page
        settings = page.main_window.settings
        sizer = wx.BoxSizer(wx.VERTICAL)

        # On arrival, with no window: the text goes to the message's line.
        self.transcribe = wx.CheckBox(self, label=page._t("ai_auto_transcribe"))
        self.transcribe.SetValue(config["auto_transcribe"])
        sizer.Add(self.transcribe, 0, wx.ALL, 8)
        self.describe = wx.CheckBox(self, label=page._t("ai_auto_describe"))
        self.describe.SetValue(config["auto_describe"])
        sizer.Add(self.describe, 0, wx.ALL, 8)

        # Which conversations the two options reach before the user visits
        # each one (core/conversation_auto_ai.py). Saved with the account's
        # settings, beside the per-conversation boxes it is the default of.
        self.scope_label = wx.StaticText(self, label=page._t("ai_auto_scope"))
        sizer.Add(self.scope_label, 0, wx.LEFT | wx.TOP, 8)
        self.scope = wx.Choice(self, choices=page._scope_labels(), name=page._plain("ai_auto_scope"))
        self.scope.SetSelection(
            1 if conversation_auto_ai.scope(settings) == conversation_auto_ai.SCOPE_SELECTED else 0)
        sizer.Add(self.scope, 0, wx.EXPAND | wx.ALL, 8)

        # How old a message may be to be processed on its own, local or
        # online (core/transcription/auto_queue.max_age_seconds()).
        self.max_age_label = wx.StaticText(self, label=page._t("ai_auto_max_age"))
        sizer.Add(self.max_age_label, 0, wx.LEFT | wx.TOP, 8)
        self.max_age = wx.Choice(self, choices=page._age_labels(), name=page._plain("ai_auto_max_age"))
        minutes = int(auto_queue.max_age_seconds(settings) // 60)
        self.max_age.SetSelection(auto_queue.AGE_CHOICES_MINUTES.index(minutes))
        sizer.Add(self.max_age, 0, wx.EXPAND | wx.ALL, 8)

        # The ceiling on requests made on arrival, and what was spent today.
        self.limit_label = wx.StaticText(self, label=page._t("ai_auto_daily_limit"))
        sizer.Add(self.limit_label, 0, wx.LEFT | wx.TOP, 8)
        self.limit = wx.SpinCtrl(self, min=0, max=10000, initial=config["auto_daily_limit"],
                                 name=page._plain("ai_auto_daily_limit"))
        sizer.Add(self.limit, 0, wx.ALL, 8)
        self.used_label = wx.StaticText(self, label=page._t("ai_auto_used_today"))
        sizer.Add(self.used_label, 0, wx.LEFT | wx.TOP, 8)
        self.used = wx.TextCtrl(self, value=str(ai_auto.spent_today(config)), style=wx.TE_READONLY,
                                name=page._plain("ai_auto_used_today"))
        sizer.Add(self.used, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

    def refresh_labels(self) -> None:
        page = self._page
        t = page._t
        self.transcribe.SetLabel(t("ai_auto_transcribe"))
        self.describe.SetLabel(t("ai_auto_describe"))
        self.scope_label.SetLabel(t("ai_auto_scope"))
        self.max_age_label.SetLabel(t("ai_auto_max_age"))
        self.limit_label.SetLabel(t("ai_auto_daily_limit"))
        self.used_label.SetLabel(t("ai_auto_used_today"))
        scope_selection = self.scope.GetSelection()
        self.scope.SetItems(page._scope_labels())
        self.scope.SetSelection(max(scope_selection, 0))
        self.scope.SetName(page._plain("ai_auto_scope"))
        age_selection = self.max_age.GetSelection()
        self.max_age.SetItems(page._age_labels())
        self.max_age.SetSelection(max(age_selection, 0))
        self.max_age.SetName(page._plain("ai_auto_max_age"))
        self.limit.SetName(page._plain("ai_auto_daily_limit"))
        self.used.SetName(page._plain("ai_auto_used_today"))
