"""SummaryVoiceMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Voice messages in an AI summary carry their words, not a "[Voice message]"
marker. Before the transcript is built:

* a voice message that already has a stored transcription (local Whisper,
  Alt+Shift+T/N or the automatic transcription) is used as it is;
* the newest ones that do not (``MAX_VOICE_TRANSCRIPTIONS``) are transcribed
  on the spot with the same local transcription, one after another on one
  worker thread — the job bulk_transcription.py runs — and each result is
  kept with its message like a bulk run keeps it;
* the run stays behind the progress window of the manual transcription
  (TranscriptionProgressDialog: "voice message 3 of 12" shown and spoken, a
  gauge, Cancel); Cancel drops the whole summary, with no question. The
  window closes by itself when the worker ends (finished, cancelled, failed,
  vault closed, app quitting) and the result opens only after that;
* the person picks how (summary_voice_choice.py): local as above, online with
  one provider (summary_voice_online.py) or not at all; a message that fails,
  is over the limit or is not transcribed stays a marker, and the result
  window says how many words were missing and why;
* the vault hiding the chat stops the run without a word about its messages.

Nothing here logs the text, the contact or a message id.
"""
import logging
import threading

import wx

from core.ai_media.summary import MAX_VOICE_TRANSCRIPTIONS, voice_to_transcribe
from core.ai_media.summary_voice_choice import MODE_LOCAL, MODE_NONE, pending_voice
from core.transcription import auto_queue, errors
from core.transcription import stored as stored_transcription
from core.transcription.management import ProgressTick, progress_percent
from ui.dialogs.transcription_progress import TranscriptionProgressDialog

#: The worker's "the vault hid the chat" ending, told apart from any sentence.
_LOCKED = object()


def mw_language(main_window) -> str:
    """The language the summary is written in: the window's own, which the
    online worker cannot read from the wx thread's i18n."""
    return getattr(main_window.i18n, "language", "") or ""


def voice_progress_tick(index, total):
    """The gauge's report as message ``index`` (1-based) of ``total`` starts:
    the share already transcribed. Never spoken (the status line is)."""
    percent = progress_percent(index - 1, total)
    return ProgressTick(index - 1, total, percent, True, False)


class _SummaryVoiceJob:
    """What the progress window runs: the worker thread of one run, with the
    start()/cancel() the window expects. ``work(run, announce)`` returns the
    sentence it stopped with (or None); the window learns the end through
    ``on_finished`` on this thread and closes itself."""

    def __init__(self, work, run, announce, on_finished):
        self._work = work
        self._run = run
        self._announce = announce
        self._on_finished = on_finished

    def start(self):
        threading.Thread(target=self._thread, daemon=True, name="winzapp-summary-voice").start()

    def _thread(self):
        self._on_finished(self._work(self._run, self._announce), None)

    def cancel(self):
        self._run.cancel()


class VoiceOutcome:
    """What the voice step hands to the summary."""

    def __init__(self):
        #: message id -> transcription text, for every voice message that has one.
        self.texts = {}
        #: Voice messages that failed to transcribe, and older ones over the
        #: per-summary limit: they stay markers and the window says so.
        self.failed = 0
        self.over_limit = 0


class _VoiceRun:
    """The state of one voice step, shared with its worker thread."""

    def __init__(self, jid, to_run, outcome, on_done, follows_open_chat, choice=None):
        self.jid = jid
        #: The person's choice (local when None) and what the online worker
        #: needs from the wx thread: the summary's profile and language.
        self.choice = choice
        self.profile = "balanced"
        self.language = ""
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

    def _transcription_in_progress(self) -> bool:
        """A bulk run, or a manual one (running without the progress window, or
        waiting its turn), is using the local transcription."""
        mw = self.main_window
        waiting = getattr(mw, "_waiting_transcriptions", None)
        return bool(self._bulk_transcription_running()
                    or getattr(mw, "_background_transcription", None) is not None
                    or (waiting is not None and len(waiting)))

    def _summary_stored_voice_text(self, msg):
        """The stored transcription text of a voice message, or None."""
        selection = stored_transcription.selection_from_settings(self.main_window.settings)
        saved = stored_transcription.saved_transcription(msg, selection)
        return (saved or {}).get("text") or None

    def _summary_voice_plan(self, messages):
        """(VoiceOutcome with the stored transcriptions, the voice messages
        that still need a run)."""
        outcome = VoiceOutcome()
        for msg in messages:
            text = self._summary_stored_voice_text(msg) if msg.get("messageType") == "audioMessage" else None
            if text:
                outcome.texts[(msg.get("key") or {}).get("id", "")] = text
        to_run = voice_to_transcribe(
            messages, lambda msg: bool(outcome.texts.get((msg.get("key") or {}).get("id", ""))),
            MAX_VOICE_TRANSCRIPTIONS)
        pending = pending_voice(messages, lambda msg: bool(outcome.texts.get((msg.get("key") or {}).get("id", ""))))
        outcome.over_limit = max(len(pending) - len(to_run), 0)
        return outcome, to_run

    def _summary_voice_blocked(self, to_run) -> bool:
        """One local transcription at a time: when a run is needed and another
        is using it, say why and refuse, rather than fight it for the model or
        silently skip the words."""
        if to_run and self._transcription_in_progress():
            self.main_window.output(self.main_window.i18n.t("ai_summary_voice_busy"))
            return True
        return False

    def _summary_voice_step(self, messages, jid, on_done, follows_open_chat=False, choice=None):
        """Collect the words of ``messages``' voice messages, then call
        ``on_done(VoiceOutcome)`` on the wx thread (at once when no run is
        needed; never when the person cancels). The voice messages that stay
        markers (over the limit, failed, not set up) are counted later, from
        the transcript itself."""
        outcome, to_run = self._summary_voice_plan(messages)
        mode = choice.mode if choice is not None else MODE_LOCAL
        if mode == MODE_NONE:
            to_run, outcome.over_limit = [], 0  # not asked for: markers, not a shortfall
        if not to_run:
            on_done(outcome)
            return
        if mode == MODE_LOCAL and self._summary_voice_blocked(to_run):
            return
        run = _VoiceRun(jid, to_run, outcome, on_done, follows_open_chat, choice)
        if mode != MODE_LOCAL:
            _app, config = self._ai_settings()
            run.profile = config["profile"]
            run.language = mw_language(self.main_window)
        self._summary_voice_run = run
        try:
            stop_sentence = self._run_summary_voice(run)
        finally:
            run.running = False
        self._finish_summary_voice(run, stop_sentence)

    def _run_summary_voice(self, run):
        """Run the worker inside the progress window's modal loop; the
        sentence it stopped with, or None. The window starts the job from a
        wx.CallAfter queued before ShowModal and ends itself when the job
        reports back, so nothing here calls EndModal."""
        mw = self.main_window
        i18n = mw.i18n
        window = []

        def make_job(on_progress, on_finished):
            def announce(text, tick):
                if text is not None:
                    window[0].set_status(text)  # thread-safe: hops to the wx thread
                on_progress(tick)
            worker = (self._summary_voice_worker if run.choice is None or run.choice.mode == MODE_LOCAL
                      else self._summary_voice_online_worker)
            return _SummaryVoiceJob(worker, run, announce, on_finished)

        first = i18n.t("ai_summary_voice_progress").format(index=1, total=run.total)
        dialog = TranscriptionProgressDialog(
            self, i18n, mw.speak_output, make_job, first, i18n.t("ai_result_summary_title"))
        window.append(dialog)
        try:
            dialog.run()
            if dialog.error is not None:
                return i18n.t(errors.error_i18n_key(errors.BACKEND_ERROR))
            return dialog.result
        finally:
            dialog.Destroy()

    def cancel_summary_voice(self):
        """Leaving the chat the run was started from: stop it, no question."""
        run = getattr(self, "_summary_voice_run", None)
        if run is not None and run.running and run.follows_open_chat:
            run.cancel()

    def cancel_summary_voice_locked(self):
        """The vault locked: a run for a locked chat stops, so its progress
        window closes at once. Nothing is said about the messages."""
        run = getattr(self, "_summary_voice_run", None)
        if run is not None and run.running and self.main_window.is_chat_locked(run.jid):
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

    def _summary_voice_worker(self, run, announce):
        """On the job's thread: transcribe the run's messages one after another,
        announcing each start; the sentence it stopped with, or None."""
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
                # The first line is the window's own opening status.
                progress = (i18n.t("ai_summary_voice_progress").format(index=run.index, total=run.total)
                            if run.index > 1 else None)
                announce(progress, voice_progress_tick(run.index, run.total))
                result, error = self._bulk_transcribe_one(run, msg)
                if error is not None:
                    if error.code == errors.CANCELLED:
                        run.cancelled.set()  # whoever cancelled the job, the summary stops
                        break
                    run.outcome.failed += 1
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
        return stop_sentence

    def _finish_summary_voice(self, run, stop_sentence):
        mw = self.main_window
        if getattr(mw, "_shutting_down", False):
            return
        if mw.is_chat_hidden_by_vault(run.jid):
            return  # The vault locked meanwhile: nothing said about its messages.
        if run.cancelled.is_set():
            mw.output(mw.i18n.t("ai_summary_cancelled"), interrupt=True)
            return
        if stop_sentence is _LOCKED:
            return
        if stop_sentence:
            # The reason first: it is what the person has to act on.
            mw.output(stop_sentence)
        run.on_done(run.outcome)
