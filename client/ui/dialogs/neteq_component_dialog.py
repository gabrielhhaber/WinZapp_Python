"""The call playback component's window, opened from Settings > Calls.

The component (NetEq, core/call_neteq_component.py) is an optional download,
like the noise reduction model: the window says whether it is there, how big
it is, and downloads or removes it. The download runs behind the progress
dialog the Transcription tab uses (or in the background, Settings > General),
so the bar is throttled and the reader is not flooded.

The state is a read-only text field with its own label, not a bare
StaticText: a field can be reached with Tab and read by a screen reader.
"""

import logging

import wx

from core import call_neteq, call_neteq_component
from core.call_neteq_component_job import NetEqComponentDownloadJob
from core.call_neteq_component_text import (
    confirmation_text, download_enabled, outcome_text, remove_enabled, removal_text,
    section_state, state_text,
)
from core.transcription import model_store
from ui.dialogs.transcription_progress import TranscriptionProgressDialog


def read_states():
    """(what is downloaded, what the window shows); never raises."""
    try:
        downloaded = call_neteq_component.installation_state()
    except Exception as exc:
        logging.warning("[neteq_component] could not read the install state: %s",
                        type(exc).__name__)
        downloaded = model_store.STATE_ABSENT
    try:
        bundled = call_neteq.bundled_dll() is not None
    except Exception:
        bundled = False
    return downloaded, section_state(downloaded, call_neteq_component.is_pinned(), bundled)


class NetEqComponentDialog(wx.Dialog):
    def __init__(self, parent, main_window):
        i18n = main_window.i18n
        super().__init__(parent, title=i18n.t("neteq_component_label").replace("&", ""),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.main_window = main_window
        self._running = False
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._label = wx.StaticText(self, label=i18n.t("neteq_component_label"))
        sizer.Add(self._label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._field = wx.TextCtrl(
            self, style=wx.TE_READONLY | wx.TE_MULTILINE | wx.TE_NO_VSCROLL, size=(420, 80))
        sizer.Add(self._field, 0, wx.EXPAND | wx.ALL, 8)
        self._download_btn = wx.Button(self, label=i18n.t("neteq_component_download_btn"))
        self._download_btn.Bind(wx.EVT_BUTTON, self._on_download)
        sizer.Add(self._download_btn, 0, wx.LEFT | wx.BOTTOM, 8)
        self._remove_btn = wx.Button(self, label=i18n.t("neteq_component_remove_btn"))
        self._remove_btn.Bind(wx.EVT_BUTTON, self._on_remove)
        sizer.Add(self._remove_btn, 0, wx.LEFT | wx.BOTTOM, 8)
        # ID_CANCEL, so Escape closes the window too.
        close = wx.Button(self, wx.ID_CANCEL, label=i18n.t("close"))
        sizer.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.SetSizerAndFit(sizer)
        self._refresh()
        self._field.SetFocus()

    def _refresh(self):
        downloaded, shown = read_states()
        self._field.SetValue(state_text(self.main_window.i18n, shown))
        self._download_btn.Enable(download_enabled(shown, self._running))
        self._remove_btn.Enable(remove_enabled(downloaded, self._running))

    def _focus_after(self, button):
        """Never leave the focus on a button that just switched itself off."""
        (button if button.IsEnabled() else self._field).SetFocus()

    def _background_busy(self) -> bool:
        """True (and says so) while a background download runs: removing what
        it installs, or starting another, waits for it to end."""
        mw = self.main_window
        if not mw.transcription_background_running():
            return False
        mw.speak_output.output(mw.i18n.t("noise_reducer_background_busy"))
        return True

    def _on_download(self, _event):
        mw = self.main_window
        i18n = mw.i18n
        if self._background_busy():
            return
        if wx.MessageBox(confirmation_text(i18n), i18n.t("transcription_download_confirm_title"),
                         wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return

        def make_job(on_progress, on_finished):
            return NetEqComponentDownloadJob(on_progress=on_progress, on_finished=on_finished)

        from update_background import background_downloads_enabled
        if background_downloads_enabled(mw.settings):
            self._start_in_background(make_job, i18n.t("neteq_component_progress"))
            return
        dialog = TranscriptionProgressDialog(
            self, i18n, mw.speak_output, make_job, i18n.t("neteq_component_progress"),
            title=i18n.t("neteq_component_label").replace("&", ""),
        )
        self._running = True
        try:
            dialog.run()
            result, error = dialog.result, dialog.error
        finally:
            dialog.Destroy()
            self._running = False
        mw.speak_output.output(outcome_text(i18n, result, error))
        self._refresh()
        self._focus_after(self._download_btn)

    def _start_in_background(self, make_job, status):
        """Hand the download to the main window, which says its start and end;
        this window refreshes once the run is over."""
        mw = self.main_window
        if not mw.start_transcription_background_job(make_job, status):
            mw.speak_output.output(mw.i18n.t("noise_reducer_background_busy"))
            return False
        mw.watch_transcription_background(self._background_ended)
        self._download_btn.Disable()
        self._field.SetFocus()
        return True

    def _background_ended(self, job, result, error, last):
        """watch_transcription_background's listener; the main window says
        the outcome."""
        if not self or not last:
            return False
        self._refresh()
        return False

    def _on_remove(self, _event):
        mw = self.main_window
        i18n = mw.i18n
        if self._background_busy():
            return
        if wx.MessageBox(i18n.t("neteq_component_remove_confirm"),
                         i18n.t("neteq_component_label").replace("&", ""),
                         wx.YES_NO | wx.ICON_QUESTION, self) != wx.YES:
            return
        try:
            gone = call_neteq_component.remove()
        except Exception as exc:
            # MODELS_BUSY: another account's window holds the folder.
            logging.warning("[neteq_component] removal failed: %s", type(exc).__name__)
            gone = False
        mw.speak_output.output(removal_text(i18n, gone))
        self._refresh()
        self._focus_after(self._remove_btn)


class NetEqComponentButtonMixin:
    """Settings > Calls' button opening the component's window."""

    def _build_neteq_component_button(self, parent, sizer):
        i18n = self.main_window.i18n
        self._neteq_component_btn = wx.Button(parent, label=i18n.t("neteq_component_button"))
        self._neteq_component_btn.Bind(wx.EVT_BUTTON, self._on_neteq_component_button)
        sizer.Add(self._neteq_component_btn, 0, wx.ALL, 8)

    def _relabel_neteq_component_button(self):
        self._neteq_component_btn.SetLabel(self.main_window.i18n.t("neteq_component_button"))

    def _on_neteq_component_button(self, _event):
        dialog = NetEqComponentDialog(self, self.main_window)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
        self._neteq_component_btn.SetFocus()
