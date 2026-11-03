"""Optional typing sound controls; the existing spoken-content box stays put."""
import wx

from core.typing_feedback import MODES, interval, mode, save_preferences, silence_during_capture


class TypingFeedbackControls:
    def __init__(self, dialog, page, sizer):
        self.dialog = dialog
        i18n = dialog.main_window.i18n
        self.label = wx.StaticText(page, label=i18n.t("typing_feedback_label"))
        sizer.Add(self.label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.choice = wx.Choice(page, choices=self._labels())
        sizer.Add(self.choice, 0, wx.EXPAND | wx.ALL, 8)
        self.interval_label = wx.StaticText(page, label=i18n.t("typing_feedback_interval"))
        sizer.Add(self.interval_label, 0, wx.LEFT | wx.RIGHT, 8)
        self.interval = wx.SpinCtrl(page, min=1, max=30, initial=3)
        sizer.Add(self.interval, 0, wx.ALL, 8)
        self.silence = wx.CheckBox(page, label=i18n.t("typing_feedback_silence_capture"))
        sizer.Add(self.silence, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.silence.Bind(wx.EVT_CHECKBOX, dialog._mark_dirty)
        self.hint = wx.StaticText(page, label=i18n.t("typing_feedback_hint"))
        sizer.Add(self.hint, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.choice.Bind(wx.EVT_CHOICE, self._on_mode)
        self.interval.Bind(wx.EVT_SPINCTRL, dialog._mark_dirty)
        dialog._announce_typing_check.Bind(wx.EVT_CHECKBOX, self._on_speech)

    def _labels(self):
        i18n = self.dialog.main_window.i18n
        return [i18n.t("typing_feedback_off"), i18n.t("typing_feedback_speech"),
                i18n.t("typing_feedback_sound"), i18n.t("typing_feedback_both")]

    def _selected(self):
        selected = self.choice.GetSelection()
        return MODES[selected] if 0 <= selected < len(MODES) else "speech"

    def _sync_interval(self):
        audible = self._selected() in ("sound", "both")
        self.interval_label.Enable(audible)
        self.interval.Enable(audible)
        self.silence.Enable(audible)

    def _on_mode(self, event):
        self.dialog._announce_typing_check.SetValue(self._selected() in ("speech", "both"))
        self._sync_interval()
        self.dialog._mark_dirty()
        event.Skip()

    def _on_speech(self, event):
        # Keep the long-standing checkbox, its location and its saved meaning.
        audible = self._selected() in ("sound", "both")
        spoken = self.dialog._announce_typing_check.GetValue()
        selected = "both" if audible and spoken else "sound" if audible else "speech" if spoken else "off"
        self.choice.SetSelection(MODES.index(selected))
        self._sync_interval()
        event.Skip()

    def load(self, settings):
        # The typing announcement box may already hold a change made on the
        # Spoken content page, which is built first: the choice follows it.
        speech = dict(settings.get("speech_content") or {},
                      announce_typing=self.dialog._announce_typing_check.GetValue())
        self.choice.SetSelection(MODES.index(mode({"speech_content": speech})))
        self.interval.SetValue(interval(settings))
        self.silence.SetValue(silence_during_capture(settings))
        self._sync_interval()

    def apply(self, settings):
        # Preserve the old checkbox even when a caller changed it with
        # SetValue(), which does not emit EVT_CHECKBOX.
        selected = mode({"speech_content": {
            "announce_typing": self.dialog._announce_typing_check.GetValue(),
            "typing_sound": self._selected() in ("sound", "both"),
        }})
        save_preferences(settings, selected, self.interval.GetValue(), silence_capture=self.silence.GetValue())

    def relabel(self):
        i18n = self.dialog.main_window.i18n
        self.label.SetLabel(i18n.t("typing_feedback_label"))
        self.interval_label.SetLabel(i18n.t("typing_feedback_interval"))
        self.hint.SetLabel(i18n.t("typing_feedback_hint"))
        self.silence.SetLabel(i18n.t("typing_feedback_silence_capture"))
        selected = self.choice.GetSelection()
        self.choice.Set(self._labels())
        self.choice.SetSelection(selected)
