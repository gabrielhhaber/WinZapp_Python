"""The noise reduction model of the Audio tab: its state, and its download.

The pure functions say what the section tells the user and are checked without
a window. `NoiseModelSectionMixin` is the part of `SettingsDialog` that owns the
controls; the download runs behind the same progress dialog the Transcription
tab uses, so the bar is throttled and the reader is not flooded.

The state is a read-only text field with its own label, not a bare StaticText:
a field can be reached with Tab and read by a screen reader, a StaticText
nobody has focus on stays silent.
"""

import logging

import wx

from core.noise_model_job import NoiseModelDownloadJob
from core.noise_model_text import (
    confirmation_text, download_enabled, installation_state, outcome_text, state_text,
)
from core.transcription import model_store
from ui.dialogs.transcription_progress import TranscriptionProgressDialog

class NoiseModelSectionMixin:
    def _build_noise_model_section(self, parent, sizer):
        i18n = self.main_window.i18n
        self._noise_model_running = False
        self._noise_model_label = wx.StaticText(parent, label=i18n.t("noise_model_label"))
        sizer.Add(self._noise_model_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._noise_model_field = wx.TextCtrl(
            parent, style=wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_NO_VSCROLL,
            size=(-1, 60),
        )
        sizer.Add(self._noise_model_field, 0, wx.EXPAND | wx.ALL, 8)
        self._noise_model_download_btn = wx.Button(
            parent, label=i18n.t("noise_model_download_btn")
        )
        self._noise_model_download_btn.Bind(wx.EVT_BUTTON, self._on_noise_model_download)
        sizer.Add(self._noise_model_download_btn, 0, wx.LEFT | wx.BOTTOM, 8)
        self._refresh_noise_model_section()

    def _relabel_noise_model_section(self):
        i18n = self.main_window.i18n
        self._noise_model_label.SetLabel(i18n.t("noise_model_label"))
        self._noise_model_download_btn.SetLabel(i18n.t("noise_model_download_btn"))
        self._refresh_noise_model_section()

    def _refresh_noise_model_section(self):
        i18n = self.main_window.i18n
        try:
            state = installation_state()
        except Exception as exc:
            logging.warning("[noise_model] could not read the install state: %s", exc)
            state = model_store.STATE_ABSENT
        self._noise_model_field.SetValue(state_text(i18n, state))
        self._noise_model_download_btn.Enable(
            download_enabled(state, self._noise_model_running)
        )

    def _on_noise_model_download(self, _event):
        i18n = self.main_window.i18n
        if wx.MessageBox(
            confirmation_text(i18n), i18n.t("transcription_download_confirm_title"),
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        dialog = TranscriptionProgressDialog(
            self, i18n, self.main_window.speak_output,
            lambda on_progress, on_finished: NoiseModelDownloadJob(
                on_progress=on_progress, on_finished=on_finished
            ),
            i18n.t("noise_model_progress"),
            title=i18n.t("noise_model_label"),
        )
        self._noise_model_running = True
        try:
            dialog.run()
            result, error = dialog.result, dialog.error
        finally:
            dialog.Destroy()
            self._noise_model_running = False
        self.main_window.speak_output.output(outcome_text(i18n, result, error))
        self._refresh_noise_model_section()
        # Never leave the focus on the button that just switched itself off.
        target = (self._noise_model_download_btn
                  if self._noise_model_download_btn.IsEnabled() else self._noise_model_field)
        target.SetFocus()
