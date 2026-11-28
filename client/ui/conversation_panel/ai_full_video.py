"""AIFullVideoMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

The panel's half of "Understand full video" (ui/dialogs/ai_full_video.py is the
window's): what the AI window of a video needs from the conversation to join
what is seen with what is said.

* the transcription already saved with the message (core/transcription/stored.py,
  independent of the AI description kept beside it);
* otherwise one local transcription of the video's sound, run the way a
  summary's voice messages are: the same worker, the same progress window with
  Cancel (summary_voice.py), the result kept with the message like any other.
  The run is registered as the panel's voice run, so leaving the chat, locking
  the vault or quitting cancels it as it cancels a summary's;
* a way to cancel it.

The sound never leaves the computer. Nothing here logs the text, the contact or
a message id.

Methods run with ``self`` bound to the ConversationsPanel instance.
"""
import logging

import wx

from core.ai_media import full_video
from core.transcription import errors
from core.transcription import stored as stored_transcription
from ui.conversation_panel.summary_voice import VoiceOutcome, _VoiceRun


class AIFullVideoMixin:
    def _full_video_hooks(self, snapshot, jid):
        """What the AI window of the video ``snapshot`` calls: ``saved_text()``,
        ``transcribe(parent)`` (blocking, over ``parent``) and ``cancel()``."""
        msg_id = (snapshot.get("key") or {}).get("id", "")

        def live():
            # The panel's own record when it is still there: a transcription
            # stored after the window opened (automatic transcription) is on it.
            return stored_transcription.find_record(
                getattr(self, "_sorted_messages", None), msg_id) or snapshot

        def saved_text():
            selection = stored_transcription.selection_from_settings(self.main_window.settings)
            return full_video.saved_speech(live(), selection)

        return {"saved_text": saved_text,
                "transcribe": lambda parent: self._full_video_transcribe(live(), jid, parent),
                "cancel": self._full_video_cancel}

    def _full_video_cancel(self):
        run = getattr(self, "_summary_voice_run", None)
        if run is not None and run.running:
            run.cancel()

    def _full_video_transcribe(self, msg, jid, parent):
        """Transcribe the video's sound on this computer behind the progress
        window; ``{"status", "text", "reason"}`` (``full_video.SPEECH_*``,
        the words, a sentence for a failure)."""
        mw = self.main_window
        i18n = mw.i18n
        outcome = {"status": full_video.SPEECH_FAILED, "text": "", "reason": ""}
        # One local transcription at a time: say so rather than fight it for the model.
        if self._transcription_in_progress() or self._summary_voice_running():
            outcome["status"] = full_video.SPEECH_BUSY
            return outcome

        def worker(run, announce):
            try:
                if mw.is_chat_hidden_by_vault(run.jid):
                    outcome["status"] = full_video.SPEECH_CANCELLED
                    return None
                result, error = self._bulk_transcribe_one(run, msg)
            except Exception as exc:
                logging.error("[transcription] the full video step stopped: %s", type(exc).__name__)
                return None
            code = error.code if error is not None else None
            text = "" if result is None or error is not None or result.is_empty else result.text
            outcome["status"] = full_video.classify(text, code, run.cancelled.is_set())
            if outcome["status"] == full_video.SPEECH_OK:
                outcome["text"] = text
                # Kept with the message like any transcription, whatever happens next.
                wx.CallAfter(self._store_bulk_transcription, run.jid, msg, result, False, None)
            elif outcome["status"] == full_video.SPEECH_FAILED:
                logging.info("[transcription] the full video transcription failed: %s", code)
                outcome["reason"] = i18n.t(errors.error_i18n_key(code)).format(
                    **errors.error_i18n_values(code))
            return None

        run = _VoiceRun(jid, [msg], VoiceOutcome(), None, True)
        run.worker = worker
        run.parent = parent
        run.status = i18n.t("ai_full_video_progress")
        run.title = i18n.t("ai_full_video").replace("&", "")
        self._summary_voice_run = run
        try:
            self._run_summary_voice(run)
        finally:
            run.running = False
        if outcome["status"] == full_video.SPEECH_FAILED and not outcome["reason"]:
            outcome["reason"] = i18n.t(errors.error_i18n_key(errors.BACKEND_ERROR)).format(
                **errors.error_i18n_values(errors.BACKEND_ERROR))
        return outcome
