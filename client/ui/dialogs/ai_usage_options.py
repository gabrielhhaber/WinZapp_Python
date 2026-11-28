"""The usage summary of the Online AI page, in a window of their own.

How many requests each provider answered or failed, and the last failure's
reason: counters only, never a prompt, an answer or a file
(core/ai_media/usage.py). A read-only multiline field, so the screen reader
reads it line by line, and a button that clears the counters. Reached from a
button on the page, like the automatic options (ai_auto_options.py).
"""

import wx

from core.ai_media import usage as ai_usage
from core.ai_media.config import preferences


def usage_text(t, counters) -> str:
    """The field's text, one line per provider that has counters; *t* is the
    page's translator and *counters* the validated ``usage`` of the preferences."""
    def line(name, ok, failed, last):
        text = t("ai_usage_line").format(provider=name, ok=ok, failed=failed)
        return f"{text} {t('ai_usage_last').format(reason=last)}" if last else text
    return ai_usage.summary_lines(counters, line, lambda category: t(f"ai_error_{category}"),
                                  t("ai_usage_none"))


class AIUsagePanel(wx.Panel):
    """The summary and its reset button; *page* is the AISettingsPage."""

    def __init__(self, parent, page):
        super().__init__(parent)
        self._page = page
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.label = wx.StaticText(self, label=page._t("ai_usage_label"))
        sizer.Add(self.label, 0, wx.LEFT | wx.TOP, 8)
        self.summary = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(420, 150),
                                   name=page._plain("ai_usage_label"))
        sizer.Add(self.summary, 1, wx.EXPAND | wx.ALL, 8)
        self.reset = wx.Button(self, label=page._t("ai_usage_reset"))
        self.reset.Bind(wx.EVT_BUTTON, self._reset)
        sizer.Add(self.reset, 0, wx.ALL, 8)
        self.SetSizer(sizer)
        self.refresh()

    def refresh(self) -> None:
        """Re-read the counters (other account processes add to them too)."""
        page = self._page
        self.summary.ChangeValue(usage_text(page._t, preferences(page.app)["usage"]))

    def refresh_labels(self) -> None:
        page = self._page
        self.label.SetLabel(page._t("ai_usage_label"))
        self.reset.SetLabel(page._t("ai_usage_reset"))
        self.summary.SetName(page._plain("ai_usage_label"))
        self.refresh()

    def _reset(self, event) -> None:
        page = self._page
        page.app.update("ai_media", ai_usage.cleared)
        self.refresh()
        self.summary.SetFocus()
        page.main_window.speak_output(page._t("ai_usage_cleared"))
