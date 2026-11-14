"""SummaryVoiceMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Voice messages in an AI summary carry their words, not a "[Voice message]"
marker. Before the transcript is built:

* a voice message that already has a stored transcription (local Whisper,
  Alt+Shift+T/N or the automatic transcription) is used as it is;
* the newest ones that do not (``MAX_VOICE_TRANSCRIPTIONS``) are transcribed
  on the spot with the same local transcription, one after another on one
  worker thread — the job bulk_transcription.py runs — and each result is
  kept with its message like a bulk run keeps it;
* progress is spoken ("voice message 3 of 12") and the run is cancellable
  (Ctrl+Shift+U or the summary context-menu item again, with a question);
  cancelling drops the whole summary;
* if the local transcription is not set up, or a message fails, those messages
  stay markers and the result window says how many words were missing;
* the vault hiding the chat stops the run without a word about its messages.

Nothing here logs the text, the contact or a message id.
"""
import logging
import threading

import wx

from core.ai_media.summary import MAX_VOICE_TRANSCRIPTIONS, voice_to_transcribe
from core.transcription import auto_queue, errors
from core.transcription import stored as stored_transcription

#: The worker's "the vault hid the chat" ending, told apart from any sentence.
_LOCKED = object()


class VoiceOutcome:
    """What the voice step hands to the summary."""

    def __init__(self):
        #: message id -> transcription text, for every voice message that has one.
        self.texts = {}


class _VoiceRun:
    """The state of one voice step, shared with its worker thread."""

    def __init__(self, jid, to_run, outcome, on_done, follows_open_chat):
        self.jid = jid
        #: Started from the open chat (not from the conversations list): leaving
        #: that chat drops the summary, so the run stops with it.
        self.follows_open_chat = follows_open_chat
        self.order = to_run
        self.total = len(to_run)
        self.index = 0
        self.outcome = outcome
        self.on_done = on_done
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.current = None
        self.running = True

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            current = self.current
        if current is not None:
            current.cancel()


class SummaryVoiceMixin:
    """Transcribe a summary's voice messages, for ConversationsPanel."""

    def _summary_voice_running(self) -> bool:
        run = getattr(self, "_summary_voice_run", None)
        return run is not None and run.running

    def _summary_stored_voice_text(self, msg):
        """The stored transcription text of a voice message, or None."""
        selection = stored_transcription.selection_from_settings(self.main_window.settings)
        saved = stored_transcription.saved_transcription(msg, selection)
        return (saved or {}).get("text") or None

    def _summary_voice_step(self, messages, jid, on_done, follows_open_chat=False):
        """Collect the words of ``messages``' voice messages, then call
        ``on_done(VoiceOutcome)`` on the wx thread (at once when no run is
        needed; never when the person cancels). The voice messages that stay
        markers (over the limit, failed, not set up) are counted later, from
        the transcript itself."""
        outcome = VoiceOutcome()
        for msg in messages:
            text = self._summary_stored_voice_text(msg) if msg.get("messageType") == "audioMessage" else None
            if text:
                outcome.texts[(msg.get("key") or {}).get("id", "")] = text
        to_run = voice_to_transcribe(
            messages, lambda msg: bool(outcome.texts.get((msg.get("key") or {}).get("id", ""))),
            MAX_VOICE_TRANSCRIPTIONS)
        if not to_run:
            on_done(outcome)
            return
        run = _VoiceRun(jid, to_run, outcome, on_done, follows_open_chat)
        self._summary_voice_run = run
        threading.Thread(target=self._summary_voice_worker, args=(run,),
                         daemon=True, name="winzapp-summary-voice").start()

    def _ask_cancel_summary_voice(self):
        run = getattr(self, "_summary_voice_run", None)
        if run is None or not run.running:
            return
        i18n = self.main_window.i18n
        text = i18n.t("ai_summary_cancel_confirm").format(index=run.index, total=run.total)
        if wx.MessageBox(text, i18n.t("ai_result_summary_title"),
                         wx.YES_NO | wx.ICON_QUESTION, self) == wx.YES:
            run.cancel()

    def cancel_summary_voice(self):
        """Leaving the chat the run was started from: stop it, no question."""
        run = getattr(self, "_summary_voice_run", None)
        if run is not None and run.running and run.follows_open_chat:
            run.cancel()

    def _cancel_summary_voice_now(self):
        """Quitting: cancel a run with no question asked; the transcription
        it is running (to wait for), or None."""
        run = getattr(self, "_summary_voice_run", None)
        if run is None or not run.running:
            return None
        run.cancel()
        with run.lock:
            return run.current

    # ── The worker ───────────────────────────────────────────────────────────

    def _summary_voice_worker(self, run):
        mw = self.main_window
        i18n = mw.i18n
        stop_sentence = None
        try:
            for msg in run.order:
                if run.cancelled.is_set():
                    break
                if mw.is_chat_hidden_by_vault(run.jid):
                    stop_sentence = _LOCKED
                    break
                run.index += 1
                progress = i18n.t("ai_summary_voice_progress").format(index=run.index, total=run.total)
                if run.index == 1:
                    progress = f"{progress} {i18n.t('ai_summary_voice_hint')}"
                wx.CallAfter(mw.output, progress)
                result, error = self._bulk_transcribe_one(run, msg)
                if error is not None:
                    if error.code == errors.CANCELLED:
                        run.cancelled.set()  # whoever cancelled the job, the summary stops
                        break
                    logging.info("[transcription] a summary voice message failed: %s", error.code)
                    if auto_queue.pauses_the_queue(error):
                        stop_sentence = i18n.t(errors.error_i18n_key(error.code)).format(
                            **errors.error_i18n_values(error.code))
                        break
                    continue
                if result is None or result.is_empty:
                    continue  # No speech in it: it stays the marker.
                run.outcome.texts[(msg.get("key") or {}).get("id", "")] = result.text
                wx.CallAfter(self._store_bulk_transcription, run.jid, msg, result, False, None)
        except Exception as exc:
            logging.error("[transcription] the summary voice step stopped: %s", type(exc).__name__)
            stop_sentence = i18n.t(errors.error_i18n_key(errors.BACKEND_ERROR))
        wx.CallAfter(self._finish_summary_voice, run, stop_sentence)

    def _finish_summary_voice(self, run, stop_sentence):
        run.running = False
        mw = self.main_window
        if run.cancelled.is_set():
            mw.output(mw.i18n.t("ai_summary_cancelled"), interrupt=True)
            return
        if stop_sentence is _LOCKED or getattr(mw, "_shutting_down", False):
            return
        if stop_sentence:
            # The reason first: it is what the person has to act on.
            mw.output(stop_sentence)
        run.on_done(run.outcome)
