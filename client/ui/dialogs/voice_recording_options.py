"""Native channel selector in Settings > Audio, with no capture/device probes."""

import wx

from core.voice_nr_options import (
    NR_ENGINES, NR_MOMENTS, voice_nr_engine, voice_nr_moment)
from core.voice_recording_mode import RECORDING_MODES, voice_recording_mode
from core.voice_stereo import (
    VOICE_BITRATES_KBPS, stereo_voice_bitrate_kbps, voice_bitrate_kbps)


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
        self._voice_nr_engine_radio = wx.RadioBox(
            parent,
            label=i18n.t("voice_nr_engine_label"),
            choices=[i18n.t("voice_nr_engine_neural"),
                     i18n.t("voice_nr_engine_basic")],
            majorDimension=1,
            style=wx.RA_SPECIFY_COLS,
        )
        sizer.Add(self._voice_nr_engine_radio, 0, wx.EXPAND | wx.ALL, 8)
        self._voice_nr_moment_radio = wx.RadioBox(
            parent,
            label=i18n.t("voice_nr_moment_label"),
            choices=[i18n.t("voice_nr_moment_send"),
                     i18n.t("voice_nr_moment_record")],
            majorDimension=1,
            style=wx.RA_SPECIFY_COLS,
        )
        sizer.Add(self._voice_nr_moment_radio, 0, wx.EXPAND | wx.ALL, 8)
        self._voice_bitrate_label = wx.StaticText(
            parent, label=i18n.t("voice_recording_bitrate_label"))
        sizer.Add(self._voice_bitrate_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._voice_bitrate_choice = wx.Choice(
            parent, choices=[str(kbps) for kbps in VOICE_BITRATES_KBPS])
        sizer.Add(self._voice_bitrate_choice, 0, wx.ALL, 8)
        self._voice_bitrate_stereo_label = wx.StaticText(
            parent, label=i18n.t("voice_recording_bitrate_stereo_label"))
        sizer.Add(self._voice_bitrate_stereo_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._voice_bitrate_stereo_choice = wx.Choice(
            parent, choices=[str(kbps) for kbps in VOICE_BITRATES_KBPS])
        sizer.Add(self._voice_bitrate_stereo_choice, 0, wx.ALL, 8)

    def _load_voice_recording_options(self):
        mode = voice_recording_mode(self.main_window.settings)
        self._voice_recording_mode_radio.SetSelection(RECORDING_MODES.index(mode))
        self._voice_nr_engine_radio.SetSelection(
            NR_ENGINES.index(voice_nr_engine(self.main_window.settings)))
        self._voice_nr_moment_radio.SetSelection(
            NR_MOMENTS.index(voice_nr_moment(self.main_window.settings)))
        self._voice_bitrate_choice.SetSelection(
            VOICE_BITRATES_KBPS.index(voice_bitrate_kbps(self.main_window.settings)))
        self._voice_bitrate_stereo_choice.SetSelection(
            VOICE_BITRATES_KBPS.index(stereo_voice_bitrate_kbps(self.main_window.settings)))

    def _save_voice_recording_options(self):
        index = self._voice_recording_mode_radio.GetSelection()
        mode = RECORDING_MODES[index] if 0 <= index < len(RECORDING_MODES) else "auto"
        self.main_window.settings.setdefault("general", {})["voice_recording_mode"] = mode
        index = self._voice_nr_engine_radio.GetSelection()
        engine = NR_ENGINES[index] if 0 <= index < len(NR_ENGINES) else "neural"
        self.main_window.settings.setdefault("general", {})["voice_nr_engine"] = engine
        index = self._voice_nr_moment_radio.GetSelection()
        moment = NR_MOMENTS[index] if 0 <= index < len(NR_MOMENTS) else "send"
        self.main_window.settings.setdefault("general", {})["voice_nr_moment"] = moment
        index = self._voice_bitrate_choice.GetSelection()
        if 0 <= index < len(VOICE_BITRATES_KBPS):
            self.main_window.settings.setdefault("general", {})["voice_recording_bitrate"] = VOICE_BITRATES_KBPS[index]
        index = self._voice_bitrate_stereo_choice.GetSelection()
        if 0 <= index < len(VOICE_BITRATES_KBPS):
            self.main_window.settings.setdefault("general", {})["voice_recording_bitrate_stereo"] = VOICE_BITRATES_KBPS[index]

    def _relabel_voice_recording_options(self):
        i18n = self.main_window.i18n
        self._voice_recording_mode_radio.SetLabel(i18n.t("voice_recording_channels_label"))
        self._voice_recording_mode_radio.SetItemLabel(0, i18n.t("voice_recording_channels_auto"))
        self._voice_recording_mode_radio.SetItemLabel(1, i18n.t("voice_recording_channels_mono"))
        self._voice_recording_mode_radio.SetItemLabel(2, i18n.t("voice_recording_channels_stereo"))
        self._voice_nr_engine_radio.SetLabel(i18n.t("voice_nr_engine_label"))
        self._voice_nr_engine_radio.SetItemLabel(0, i18n.t("voice_nr_engine_neural"))
        self._voice_nr_engine_radio.SetItemLabel(1, i18n.t("voice_nr_engine_basic"))
        self._voice_nr_moment_radio.SetLabel(i18n.t("voice_nr_moment_label"))
        self._voice_nr_moment_radio.SetItemLabel(0, i18n.t("voice_nr_moment_send"))
        self._voice_nr_moment_radio.SetItemLabel(1, i18n.t("voice_nr_moment_record"))
        self._voice_bitrate_label.SetLabel(i18n.t("voice_recording_bitrate_label"))
        self._voice_bitrate_stereo_label.SetLabel(i18n.t("voice_recording_bitrate_stereo_label"))
