"""MyStatusTranscriptionKeys — a base class of MyStatusDialog (status_tab/status_dialogs.py).

Not one of StatusPanel's mixins (those are the *Mixin classes of this package):
it belongs to the dialog, and is kept apart from status_dialogs.py only so that
file holds the window and this one its transcription keys.

The Status tab's transcription keys, in the window that shows the account's
own statuses. The list's keys never reached it: the "my status" row of the
list is a summary that opens this window, which shows one status at a time
and has no row of its own for each.

* **Alt+Shift+T** opens the text in the result window (the saved one when
  there is one);
* **Alt+Shift+N** puts it in the line this window shows for the status and
  reads it aloud. No alternation with Enter here: Enter and Space press the
  focused button, as in any dialog;
* **Alt+Shift+Delete** deletes the saved transcription, after asking.

The three are the catalogue's ``status`` rows, so a key changed on the
Keyboard shortcuts tab changes here too.

The window stands in for the panel the flow talks to
(ui/status_transcription_flow.py): `main_window`, `_status_poster_name()`,
`_repaint_status_row()` and `_focus_status_list()`.

Methods run with ``self`` bound to the MyStatusDialog instance.
"""

import wx

from core.transcription import statuses
from ui import status_transcription_flow
from ui.shortcut_bindings import make_shortcut_table


class MyStatusTranscriptionKeys:
    """Transcribing one of the account's own audio or video statuses."""

    @property
    def main_window(self):
        return self._mw

    def _create_transcription_accelerators(self):
        self.ID_ALT_SHIFT_T = wx.NewIdRef()
        self.ID_ALT_SHIFT_N = wx.NewIdRef()
        self.ID_ALT_SHIFT_DELETE = wx.NewIdRef()
        self.SetAcceleratorTable(make_shortcut_table(self, 'status', [
            (wx.ACCEL_ALT | wx.ACCEL_SHIFT, ord("T"),      self.ID_ALT_SHIFT_T),
            (wx.ACCEL_ALT | wx.ACCEL_SHIFT, ord("N"),      self.ID_ALT_SHIFT_N),
            (wx.ACCEL_ALT | wx.ACCEL_SHIFT, wx.WXK_DELETE, self.ID_ALT_SHIFT_DELETE),
        ]))
        self.Bind(wx.EVT_MENU, self._on_transcribe_own_status, id=self.ID_ALT_SHIFT_T)
        self.Bind(wx.EVT_MENU, self._on_transcribe_own_status_inline, id=self.ID_ALT_SHIFT_N)
        self.Bind(wx.EVT_MENU, self._on_delete_own_status_transcription,
                  id=self.ID_ALT_SHIFT_DELETE)

    def _own_status(self):
        """The status being shown, None when there is none."""
        if not self._statuses or not (0 <= self._current < len(self._statuses)):
            return None
        return self._statuses[self._current]

    def _own_status_or_say_none(self):
        status = self._own_status()
        if not statuses.is_transcribable(status):
            self._mw.output(self._mw.i18n.t("transcription_no_audio"))
            return None
        return status

    def _on_transcribe_own_status(self, event=None):
        status = self._own_status_or_say_none()
        if status is not None:
            status_transcription_flow.open_or_transcribe_status(self, status)

    def _on_transcribe_own_status_inline(self, event=None):
        status = self._own_status_or_say_none()
        if status is not None:
            status_transcription_flow.transcribe_status_inline(self, status)

    def _on_delete_own_status_transcription(self, event=None):
        status = self._own_status()
        if not statuses.holds_text(status):
            self._mw.output(self._mw.i18n.t("transcription_none_to_delete"), interrupt=True)
            return
        status_transcription_flow.delete_status_transcription(self, status)

    def _own_status_inline_suffix(self, status, i18n) -> str:
        """", transcription: text" for a status whose text is wanted in its
        line, "" otherwise."""
        written = statuses.inline_text(status, getattr(self._mw, "settings", None))
        return f", {i18n.t('transcription_row_label')}: {written}" if written else ""

    # ── What the flow asks of its panel ──────────────────────────────────────

    def _status_poster_name(self, status) -> str:
        return self._mw.i18n.t("my_status")

    def _repaint_status_row(self, msg_id: str) -> None:
        """Redraw the line of the status being shown, without announcing it
        again or touching what is playing (the flow says the text itself)."""
        status = self._own_status()
        if status is None or statuses.status_id(status) != msg_id:
            return
        self._content_lbl.SetLabel(self._content_label_text())
        self.Layout()

    def _focus_status_list(self) -> None:
        """The focus stays on the button it was on: this window has no list."""
