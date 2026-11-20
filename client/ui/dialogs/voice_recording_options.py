"""Settings > Audio's voice-message options: the noise-reduction engine, when
it runs and what happens when the neural one fails, then the native channel
selector and the bitrates. No capture or device probes."""

import wx

from core.voice_nr_options import (
    NR_ENGINES, NR_MOMENTS, NR_ON_FAILURE,
    voice_nr_engine, voice_nr_moment, voice_nr_on_failure, recording_nr_available)
from core.voice_recording_mode import RECORDING_MODES, voice_recording_mode
from core.voice_stereo import (
    VOICE_BITRATES_KBPS, stereo_voice_bitrate_kbps, voice_bitrate_kbps)


class VoiceRecordingOptionsMixin:
    def _build_voice_recording_options(self, parent, sizer):
        i18n = self.main_window.i18n
        # The noise-reduction combos come first: the page builds them right
        # after the master noise-reduction switch and the noise reducer's
        # button (settings_dialog.py), so Tab moves through the whole
        # noise-reduction group before the channel selector. Read-only
        # combos with their own StaticText labels, the same pattern as the
        # bitrate and audio-effects selectors on this page. The moment and
        # on-failure combos only mean something for the NEURAL engine with
        # noise reduction on, so they are enabled conditionally (see
        # _update_voice_nr_options_state).
        self._voice_nr_engine_label = wx.StaticText(
            parent, label=i18n.t("voice_nr_engine_label"))
        sizer.Add(self._voice_nr_engine_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._voice_nr_engine_combo = wx.ComboBox(
            parent, style=wx.CB_READONLY,
            choices=[i18n.t("voice_nr_engine_neural"),
                     i18n.t("voice_nr_engine_basic")])
        sizer.Add(self._voice_nr_engine_combo, 0, wx.EXPAND | wx.ALL, 8)
        self._voice_nr_engine_combo.Bind(
            wx.EVT_COMBOBOX, self._on_voice_nr_engine_changed)
        self._voice_nr_moment_label = wx.StaticText(
            parent, label=i18n.t("voice_nr_moment_label"))
        sizer.Add(self._voice_nr_moment_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._voice_nr_moment_combo = wx.ComboBox(
            parent, style=wx.CB_READONLY,
            choices=[i18n.t("voice_nr_moment_send"),
                     i18n.t("voice_nr_moment_record")])
        sizer.Add(self._voice_nr_moment_combo, 0, wx.EXPAND | wx.ALL, 8)
        self._voice_nr_moment_combo.Bind(
            wx.EVT_COMBOBOX, self._on_voice_nr_moment_changed)
        self._voice_nr_on_failure_label = wx.StaticText(
            parent, label=i18n.t("voice_nr_on_failure_label"))
        sizer.Add(self._voice_nr_on_failure_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._voice_nr_on_failure_combo = wx.ComboBox(
            parent, style=wx.CB_READONLY,
            choices=[i18n.t("voice_nr_on_failure_basic"),
                     i18n.t("voice_nr_on_failure_pending")])
        sizer.Add(self._voice_nr_on_failure_combo, 0, wx.EXPAND | wx.ALL, 8)
        # The master switch is created before this builder runs, on the
        # same page: toggling it re-evaluates the conditional state too.
        nr_check = getattr(self, "_noise_reduction_check", None)
        if nr_check is not None:
            nr_check.Bind(wx.EVT_CHECKBOX, self._on_voice_nr_master_toggled)
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
        settings = self.main_window.settings
        mode = voice_recording_mode(settings)
        self._voice_recording_mode_radio.SetSelection(RECORDING_MODES.index(mode))
        self._voice_nr_engine_combo.SetSelection(
            NR_ENGINES.index(voice_nr_engine(settings)))
        # The stored choice, not the effective one: a graphics-card
        # processor runs it at send time, but switching back to the
        # processor in this window brings "while recording" back.
        general = settings.get("general", {}) if isinstance(settings, dict) else {}
        stored = general.get("voice_nr_moment") if isinstance(general, dict) else None
        self._voice_nr_moment_wanted = NR_MOMENTS.index(
            stored if stored in NR_MOMENTS else voice_nr_moment(settings))
        self._voice_nr_moment_combo.SetSelection(self._voice_nr_moment_wanted)
        self._voice_nr_on_failure_combo.SetSelection(
            NR_ON_FAILURE.index(voice_nr_on_failure(settings)))
        self._update_voice_nr_options_state()
        self._voice_bitrate_choice.SetSelection(
            VOICE_BITRATES_KBPS.index(voice_bitrate_kbps(settings)))
        self._voice_bitrate_stereo_choice.SetSelection(
            VOICE_BITRATES_KBPS.index(stereo_voice_bitrate_kbps(settings)))

    def _save_voice_recording_options(self):
        general = self.main_window.settings.setdefault("general", {})
        index = self._voice_recording_mode_radio.GetSelection()
        general["voice_recording_mode"] = (
            RECORDING_MODES[index] if 0 <= index < len(RECORDING_MODES) else "auto")
        # The only writer of the engine preference.
        index = self._voice_nr_engine_combo.GetSelection()
        engine = NR_ENGINES[index] if 0 <= index < len(NR_ENGINES) else "neural"
        general["noise_reduction_neural"] = engine == "neural"
        # The user's moment is kept even while a graphics-card processor
        # makes it "when sending" (voice_nr_moment() decides at run time).
        index = getattr(self, "_voice_nr_moment_wanted", None)
        if index is None:
            index = self._voice_nr_moment_combo.GetSelection()
        general["voice_nr_moment"] = (
            NR_MOMENTS[index] if 0 <= index < len(NR_MOMENTS) else "send")
        index = self._voice_nr_on_failure_combo.GetSelection()
        general["voice_nr_on_failure"] = (
            NR_ON_FAILURE[index] if 0 <= index < len(NR_ON_FAILURE) else "basic")
        index = self._voice_bitrate_choice.GetSelection()
        if 0 <= index < len(VOICE_BITRATES_KBPS):
            general["voice_recording_bitrate"] = VOICE_BITRATES_KBPS[index]
        index = self._voice_bitrate_stereo_choice.GetSelection()
        if 0 <= index < len(VOICE_BITRATES_KBPS):
            general["voice_recording_bitrate_stereo"] = VOICE_BITRATES_KBPS[index]

    def _update_voice_nr_options_state(self):
        """Conditional enabling for the noise-reduction combos.

        The engine combo tracks the page's master noise-reduction switch;
        the moment and on-failure combos only mean something for the NEURAL
        engine (the basic one always gates at send time), so they stay
        disabled — still visible, but skipped by Tab — with a basic engine
        or the switch off, instead of offering a choice that does nothing.
        A graphics-card processor shows "when sending" (its process cleans
        the note then) without forgetting the user's own choice.
        """
        nr_check = getattr(self, "_noise_reduction_check", None)
        nr_on = bool(nr_check.GetValue()) if nr_check is not None else True
        index = self._voice_nr_engine_combo.GetSelection()
        engine = NR_ENGINES[index] if 0 <= index < len(NR_ENGINES) else "neural"
        self._voice_nr_engine_combo.Enable(nr_on)
        neural = nr_on and engine == "neural"
        available = recording_nr_available(VoiceRecordingOptionsMixin._voice_nr_selected_device(self))
        wanted = getattr(self, "_voice_nr_moment_wanted", None)
        if not available:
            self._voice_nr_moment_combo.SetSelection(NR_MOMENTS.index("send"))
        elif wanted is not None:
            self._voice_nr_moment_combo.SetSelection(wanted)
        self._voice_nr_moment_combo.Enable(neural and available)
        self._voice_nr_on_failure_combo.Enable(neural)

    def _voice_nr_selected_device(self):
        device = getattr(self, "_noise_reducer_device", None)
        if device is None:
            device = self.main_window.settings.get("general", {}).get(
                "noise_reduction_device", "cpu")
        return device

    def _on_voice_nr_engine_changed(self, event):
        self._update_voice_nr_options_state()
        event.Skip()

    def _on_voice_nr_moment_changed(self, event):
        self._voice_nr_moment_wanted = self._voice_nr_moment_combo.GetSelection()
        event.Skip()

    def _on_voice_nr_master_toggled(self, event):
        self._update_voice_nr_options_state()
        event.Skip()

    def _relabel_voice_recording_options(self):
        i18n = self.main_window.i18n
        self._voice_recording_mode_radio.SetLabel(i18n.t("voice_recording_channels_label"))
        self._voice_recording_mode_radio.SetItemLabel(0, i18n.t("voice_recording_channels_auto"))
        self._voice_recording_mode_radio.SetItemLabel(1, i18n.t("voice_recording_channels_mono"))
        self._voice_recording_mode_radio.SetItemLabel(2, i18n.t("voice_recording_channels_stereo"))
        self._voice_nr_engine_label.SetLabel(i18n.t("voice_nr_engine_label"))
        self._voice_nr_engine_combo.SetString(0, i18n.t("voice_nr_engine_neural"))
        self._voice_nr_engine_combo.SetString(1, i18n.t("voice_nr_engine_basic"))
        self._voice_nr_moment_label.SetLabel(i18n.t("voice_nr_moment_label"))
        self._voice_nr_moment_combo.SetString(0, i18n.t("voice_nr_moment_send"))
        self._voice_nr_moment_combo.SetString(1, i18n.t("voice_nr_moment_record"))
        self._voice_nr_on_failure_label.SetLabel(i18n.t("voice_nr_on_failure_label"))
        self._voice_nr_on_failure_combo.SetString(0, i18n.t("voice_nr_on_failure_basic"))
        self._voice_nr_on_failure_combo.SetString(1, i18n.t("voice_nr_on_failure_pending"))
        self._voice_bitrate_label.SetLabel(i18n.t("voice_recording_bitrate_label"))
        self._voice_bitrate_stereo_label.SetLabel(i18n.t("voice_recording_bitrate_stereo_label"))
