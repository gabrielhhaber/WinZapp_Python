"""From a key on the Status tab to the text of an audio or video status.

The conversation's flow (ui/transcription_flow.py) with the four things that
differ for a status update: where the text is kept, where the media comes
from, what the result window is called, and the row and focus to come back to.
Everything else — the progress dialog, the processor re-run, what is said and
in which order, the result window — is that flow, unchanged.

* **Kept with the status** (MainWindow.store_status_transcription()), so it is
  deleted with it when the status expires.
* **The media** is downloaded for the run into a private folder that lives as
  long as `start()` does: a status is never on disk otherwise.
* **No vault and no withdrawal.** A status belongs to no chat the vault could
  hide, and one deleted by its poster simply stops being listed.
* **No "insert into the message"**: the Status tab has no message field.
"""

from core.transcription import _fileops, message_run, statuses
from core.transcription import stored as stored_transcription
from ui.transcription_flow import MessageTranscriptionFlow


class StatusTranscriptionFlow(MessageTranscriptionFlow):
    """One transcription of one status. Built on the wx thread, used once."""

    _offers_insert = False
    _delete_question_key = "transcription_status_delete_question"

    def __init__(self, panel, status, inline=False):
        super().__init__(panel, status, inline)
        self._jid = statuses.STATUS_JID
        #: The private folder of this run's download, while `start()` runs.
        self._folder = ""

    def _prepare(self):
        # Entered here and left in _cleanup(): a run without the progress
        # window outlives start(), and its download must live as long as it.
        self._folder_context = _fileops.private_temp_dir(statuses.TEMP_PREFIX)
        self._folder = self._folder_context.__enter__()

    def _cleanup(self):
        context, self._folder_context, self._folder = getattr(self, "_folder_context", None), None, ""
        if context is not None:
            context.__exit__(None, None, None)

    def _make_first_run(self, on_progress, on_finished):
        mw = self._main_window
        stored_models_dir, references = self._install_wide_models()
        run = message_run.MessageTranscription(
            self._msg,
            mw.settings,
            mw.key,
            self._folder,
            self._folder,
            stored_models_dir=stored_models_dir,
            ui_language=getattr(self._i18n, "language", ""),
            external_references=references,
            find_ffmpeg=mw._find_api_ffmpeg,
            is_online=lambda: bool(getattr(mw, "_wa_connected", False)),
            fetch_media=mw._fetch_status_media,
            on_phase=self._on_phase,
            on_progress=on_progress,
            on_finished=on_finished,
        )
        self._run = run
        return run

    # ── What differs from a message ──────────────────────────────────────────

    def _hidden_by_vault(self):
        return False

    def _withdrawn_now(self):
        return False

    def _save(self, value, existing=False):
        return self._main_window.store_status_transcription(
            self._msg, value, existing=existing)

    def _delete(self, on_done):
        self._main_window.delete_status_transcription(self._msg, on_done)

    def _title_name(self):
        return self._panel._status_poster_name(self._msg)

    def _repaint_row(self):
        self._panel._repaint_status_row(self._msg_id)

    def _focus_message(self):
        self._panel._focus_status_list()

    def _insert_into_message_field(self, text):
        # Not offered (see _offers_insert); a window that asked anyway only
        # gets the focus put back.
        self._focus_message()


def open_or_transcribe_status(panel, status):
    """Alt+Shift+T on the Status tab: the saved transcription when there is
    one, a run otherwise; the text opens in the result window."""
    flow = StatusTranscriptionFlow(panel, status)
    if stored_transcription.saved_transcription(status, flow._selection) is not None:
        flow.show_saved()
    else:
        flow.start()


def transcribe_status_inline(panel, status):
    """Alt+Shift+N (or Enter when it is set to transcribe): the text goes
    into the status's line of the list and is read aloud. A transcription
    already saved is used instead of a run."""
    flow = StatusTranscriptionFlow(panel, status, inline=True)
    if stored_transcription.saved_transcription(status, flow._selection) is not None:
        flow.inline_saved()
    else:
        flow.start()


def delete_status_transcription(panel, status):
    """Ask, then delete the saved transcription of the status."""
    StatusTranscriptionFlow(panel, status).delete_saved()
