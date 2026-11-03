"""Native channel selector in Settings > Audio, with no capture/device probes."""

import wx

from core.voice_recording_mode import RECORDING_MODES, voice_recording_mode


class VoiceRecordingOptionsMixin:
    def _build_voice_recording_options(self, parent, sizer):
        i18n = self.main_window.i18n
        self._voice_recording_mode_radio = wx.RadioBox(
            parent,
            label=i18n.t("voice_recording_channels_label"),
            choices=[i18n.t("voice_recording_channels_auto"),
                     i18n.t("voice_recording_channels_mono"),
                     i18n.t("voice_recording_channels_stereo")],
            majorDimension=1,
            style=wx.RA_SPECIFY_COLS,
        )
        sizer.Add(self._voice_recording_mode_radio, 0, wx.EXPAND | wx.ALL, 8)

    def _load_voice_recording_options(self):
        mode = voice_recording_mode(self.main_window.settings)
        self._voice_recording_mode_radio.SetSelection(RECORDING_MODES.index(mode))

    def _save_voice_recording_options(self):
        index = self._voice_recording_mode_radio.GetSelection()
        mode = RECORDING_MODES[index] if 0 <= index < len(RECORDING_MODES) else "auto"
        self.main_window.settings.setdefault("general", {})["voice_recording_mode"] = mode

    def _relabel_voice_recording_options(self):
        i18n = self.main_window.i18n
        self._voice_recording_mode_radio.SetLabel(i18n.t("voice_recording_channels_label"))
        self._voice_recording_mode_radio.SetItemLabel(0, i18n.t("voice_recording_channels_auto"))
        self._voice_recording_mode_radio.SetItemLabel(1, i18n.t("voice_recording_channels_mono"))
        self._voice_recording_mode_radio.SetItemLabel(2, i18n.t("voice_recording_channels_stereo"))
