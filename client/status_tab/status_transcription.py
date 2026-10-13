"""StatusTranscriptionPanelMixin — part of StatusPanel (see status_tab/__init__.py).

Transcribing an audio or video status on this computer, from the Status tab:

* **Alt+Shift+T** opens the text in the result window (the saved one when
  there is one, never a second run);
* **Alt+Shift+N** puts it in the status's line of the list and reads it aloud;
* with Settings > Local transcription "Enter and Space transcribe statuses"
  on, the two swap as they do in a conversation: Enter and Space transcribe
  into the line, and Alt+Shift+N opens or plays the status.

The status the keys act on is the one the focused row is showing: the status
open in the inline viewer, or the one a contact's row previews. The rules are
core/transcription/statuses.py, the flow ui/status_transcription_flow.py and
the storage main_window/status_transcription.py.

Methods run with ``self`` bound to the StatusPanel instance.
"""

from core.transcription import statuses
from ui import status_transcription_flow


class StatusTranscriptionPanelMixin:
    """The Status tab's transcription keys, row text and focus."""

    def _status_shown_by_row(self, row: int):
        """The status a row of the list is previewing, None for a row that
        shows none (the "my status" row, a header, nothing focused)."""
        contact_idx = self._status_row_contact.get(row, -1)
        if not (0 <= contact_idx < len(self._status_contacts)):
            return None
        shown = self._status_contacts[contact_idx].get("statuses") or []
        if not shown:
            return None
        # A contact opened in the viewer shows whichever status was navigated
        # to (Ctrl+Left/Right); any other row previews the newest one.
        index = self._current_status_idx if contact_idx == self._selected_contact_idx else 0
        return shown[index] if 0 <= index < len(shown) else shown[0]

    def _status_for_transcription(self):
        """The status the transcription keys act on."""
        if self._viewer_panel.IsShown() and isinstance(self._current_status, dict):
            return self._current_status
        return self._status_shown_by_row(self._status_list.GetFocusedItem())

    def _status_or_say_none(self):
        status = self._status_for_transcription()
        if not statuses.is_transcribable(status):
            # One short sentence, as on a message that holds no speech.
            self.main_window.output(self.main_window.i18n.t("transcription_no_audio"))
            return None
        return status

    def _on_transcribe_status(self, event=None):
        """Alt+Shift+T: the text in the result window."""
        status = self._status_or_say_none()
        if status is not None:
            status_transcription_flow.open_or_transcribe_status(self, status)

    def _on_transcribe_status_inline_key(self, event=None):
        """Alt+Shift+N: whichever of the two actions Enter is not doing."""
        if statuses.enter_transcribes(getattr(self.main_window, "settings", None)):
            # Enter transcribes, so this key opens or plays the status.
            row = self._status_list.GetFocusedItem()
            if row >= 0:
                self._open_status_row(row)
            return
        status = self._status_or_say_none()
        if status is not None:
            status_transcription_flow.transcribe_status_inline(self, status)

    def _on_delete_status_transcription(self, event=None):
        """Alt+Shift+Delete: delete the saved transcription of the status the
        focused row is showing, after asking."""
        status = self._status_for_transcription()
        if not statuses.holds_text(status):
            # Never a silent no-op: the key was pressed and nothing changed.
            self.main_window.output(
                self.main_window.i18n.t("transcription_none_to_delete"), interrupt=True)
            return
        status_transcription_flow.delete_status_transcription(self, status)

    def _enter_transcribes_status_row(self, row: int) -> bool:
        """Enter or Space on this row: transcribe into the line instead of
        opening, when the setting is on and the row shows audio or video.
        Returns whether it did."""
        if not statuses.enter_transcribes(getattr(self.main_window, "settings", None)):
            return False
        status = self._status_shown_by_row(row)
        if not statuses.is_transcribable(status):
            return False
        status_transcription_flow.transcribe_status_inline(self, status)
        return True

    # ── What the flow asks of the panel ──────────────────────────────────────

    def _status_inline_suffix(self, status, i18n) -> str:
        """", transcription: text" for a status whose text is wanted in its
        line, "" otherwise."""
        written = statuses.inline_text(status, getattr(self.main_window, "settings", None))
        return f", {i18n.t('transcription_row_label')}: {written}" if written else ""

    def _status_contact_index_of(self, msg_id: str) -> int:
        for index, entry in enumerate(self._status_contacts):
            if statuses.records_with_id([entry.get("statuses")], msg_id):
                return index
        return -1

    def _status_poster_name(self, status) -> str:
        """Who posted the status, by the name its row shows; never a JID."""
        index = self._status_contact_index_of(statuses.status_id(status))
        return self._status_contacts[index].get("name", "") if index >= 0 else ""

    def _repaint_status_row(self, msg_id: str) -> None:
        """Redraw the row of the contact who posted this status, which carries
        (or no longer carries) the transcription from now on."""
        index = self._status_contact_index_of(msg_id)
        row = self._status_contact_row.get(index) if index >= 0 else None
        if row is None:
            return
        if index == self._selected_contact_idx:
            self._update_focused_status_row_text()
            return
        text = self._status_row_text(self._status_contacts[index], self.main_window.i18n)
        if self._status_list.GetItemText(row) != text:
            self._status_list.SetItemText(row, text)

    def _focus_status_list(self) -> None:
        """Back to the list the key was pressed on."""
        if self._status_list.IsShown():
            self._status_list.SetFocus()
