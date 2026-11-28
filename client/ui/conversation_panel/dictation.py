"""DictationMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Dictation into the message field (ACC-03): one key starts listening, the same
key stops, the text is transcribed on this computer with the Whisper the person
already set up and written at the caret, and the message is never sent. The
rules and the state machine are ``core/dictation.py``; this is the wx side.

Things that are easy to get wrong here:

* **Nothing is spoken while the microphone is open.** A screen reader's voice
  through the speakers is what the microphone would transcribe. The start is a
  sound (the voice-recording one), and the sentence comes only once the stream
  is closed. While listening the flag ``_dictation_listening`` also silences
  speech when "silence while recording" is on, like a voice message does
  (``MainWindow._voice_recording_silence_active``).
* **The model is checked before the microphone opens** (``preflight``), so a
  machine with no model says why at once instead of after a long take. Nothing
  is downloaded: the reason is the sentence a message run gives for it.
* **The microphone and the transcription are on worker threads**; every
  result crosses back with ``wx.CallAfter`` and is checked against the
  session's token, so a cancel or a conversation change voids it.
* **The focus is never moved.** The text goes in with ``WriteText()`` (which
  replaces a selection, as an editor does, and Ctrl+Z undoes it) and the field
  keeps the focus it had; if the person went elsewhere meanwhile, the text is
  still put in the field and said, and focus is left where it is.
* **A voice recording or a call owns the microphone**: refused with a sentence
  (``dictation.refusal_key``), and a voice recording refuses to start while
  dictating (``dictation.is_active``).
"""

import logging
import threading

import wx

from coord_locks import LockTimeout
from core import dictation, dictation_capture
from core.transcription import errors, external_models, narration, preferences
from core.transcription.dictation_run import DictationTranscription
from main_window.typing_feedback import stop_typing_for_capture
from ui.shortcut_bindings import popup_item_label


class DictationMixin:
    """Dictate into the message field, from the keyboard."""

    # ── The key ──────────────────────────────────────────────────────────────

    def _dictation_menu_offered(self):
        """Whether the composer context menu can offer Dictate/Stop dictation."""
        if getattr(self, "conversation", None) is None:
            return False
        field = getattr(self, "message_field", None)
        if field is None:
            return False
        return bool(
            getattr(field, "IsShown", lambda: True)()
            and getattr(field, "IsEnabled", lambda: True)()
            and getattr(field, "IsEditable", lambda: True)()
        )

    def _dictation_menu_label(self):
        """Context-menu label for starting or stopping dictation, with shortcut."""
        action = dictation.press_action(self._dictation_get().state)
        t = self.main_window.i18n.t
        label = t("dictation_menu_start") if action == dictation.ACTION_START else t("dictation_menu_stop")
        return popup_item_label(self, label, "messages.ID_DICTATE")

    def _dictation_get(self):
        state = getattr(self, "_dictation", None)
        if state is None:
            state = self._dictation = dictation.Dictation()
        return state

    def _on_dictation_shortcut(self, event=None):
        if self.conversation is None:
            return
        state = self._dictation_get()
        action = dictation.press_action(state.state)
        if action == dictation.ACTION_START:
            self._start_dictation()
        elif action == dictation.ACTION_STOP:
            self._stop_dictation()
        else:
            self._cancel_dictation(announce=True)

    # ── Starting ─────────────────────────────────────────────────────────────

    def _start_dictation(self):
        mw = self.main_window
        field = self.message_field
        if not (field.IsShown() and field.IsEnabled() and field.IsEditable()):
            return
        in_call = bool(getattr(mw, "_active_voice_call", None)
                       or getattr(mw, "_call_audio_session", None))
        reason = dictation.refusal_key(
            mw._audio_capture_in_progress(), in_call,
            capture_missing=not dictation_capture.is_available())
        if reason:
            self._dictation_say(reason)
            return
        state = self._dictation_get()
        token = state.begin()
        if token is None:
            return
        self._dictation_jid = (self.conversation or {}).get("remoteJid", "")
        stop_typing_for_capture(mw)
        threading.Thread(
            target=self._dictation_open_worker, args=(token,), daemon=True,
            name="winzapp-dictation-open",
        ).start()

    def _dictation_arguments(self):
        """The install-wide half of a run's arguments, read on a worker (the
        settings file can be held by another window for a moment)."""
        mw = self.main_window
        app_settings = getattr(mw, "_app_settings", None)
        try:
            stored_dir = preferences.stored_models_dir(app_settings)
        except LockTimeout:
            logging.warning("[dictation] app.json was held; the run reads no install-wide settings")
            return {"stored_models_dir": "", "external_references": None}
        references, known = external_models.read_references(app_settings)
        return {"stored_models_dir": stored_dir,
                "external_references": references if known else None}

    def _dictation_run(self, chunks=(), rate=16000, channels=1, **callbacks):
        mw = self.main_window
        return DictationTranscription(
            mw.settings, chunks, rate, channels,
            ui_language=getattr(mw.i18n, "language", ""),
            find_ffmpeg=mw._find_api_ffmpeg,
            **self._dictation_arguments(),
            **callbacks,
        )

    def _dictation_open_worker(self, token):
        """Off the wx thread: is there a model to listen for, then the microphone."""
        capture = None
        error = None
        run = None
        try:
            run = self._dictation_run()
            run.preflight()
            capture = dictation_capture.MicCapture(
                getattr(self.main_window, "effective_input_device_name", "") or "",
                on_full=lambda: wx.CallAfter(self._dictation_auto_stop, token),
            )
            if not capture.open():
                capture = None
        except errors.TranscriptionError as exc:
            error = exc
        except Exception as exc:
            logging.error("[dictation] opening failed: %s", type(exc).__name__)
            capture = None
        wx.CallAfter(self._dictation_opened, token, capture, error, run)

    def _dictation_opened(self, token, capture, error, run):
        state = self._dictation_get()
        if not state.is_current(token):
            if capture is not None:
                self._dictation_release(capture)
            return
        if error is not None or capture is None:
            state.end(token)
            if error is not None:
                self._dictation_say_failure(error, run)
            else:
                self._dictation_say("voice_recording_device_failed", failed=True)
            return
        state.opened(token)
        self._dictation_capture = capture
        self._dictation_listening = True
        self._dictation_play("voicemsg_startrecording_sound")

    # ── Stopping ─────────────────────────────────────────────────────────────

    def _dictation_auto_stop(self, token):
        state = self._dictation_get()
        if state.token == token and state.state == dictation.LISTENING:
            self._stop_dictation()

    def _stop_dictation(self):
        state = self._dictation_get()
        token = state.token
        capture = getattr(self, "_dictation_capture", None)
        if capture is None or not state.stop(token):
            return
        self._dictation_capture = None
        self._dictation_listening = False
        self._dictation_play("voicemsg_pauserecording_sound")
        threading.Thread(
            target=self._dictation_transcribe_worker, args=(token, capture),
            daemon=True, name="winzapp-dictation",
        ).start()

    def _dictation_transcribe_worker(self, token, capture):
        """Off the wx thread: close the microphone, then transcribe what it heard."""
        rate, channels = capture.rate, capture.channels
        chunks = capture.finish()
        seconds = dictation.capture_seconds(
            sum(len(c) for c in chunks), rate, channels)
        if dictation.is_too_short(seconds):
            wx.CallAfter(self._dictation_finished, token, None, None, None)
            return
        wx.CallAfter(self._dictation_say, "dictation_transcribing")
        outcome = {}

        def _done(result, error):
            outcome["result"], outcome["error"] = result, error

        run = self._dictation_run(chunks, rate, channels, on_finished=_done)
        del chunks
        wx.CallAfter(self._dictation_note_run, token, run)
        run.start()
        run.join()
        wx.CallAfter(self._dictation_finished, token,
                     outcome.get("result"), outcome.get("error"), run)

    def _dictation_note_run(self, token, run):
        # So a cancel can reach the transcription; a stale session cancels it now.
        if self._dictation_get().is_current(token):
            self._dictation_current_run = run
        else:
            run.cancel()

    # ── The end ──────────────────────────────────────────────────────────────

    def _dictation_finished(self, token, result, error, run):
        state = self._dictation_get()
        if not state.end(token):
            return
        self._dictation_current_run = None
        if error is not None:
            if getattr(error, "code", None) != errors.CANCELLED:
                self._dictation_say_failure(error, run)
            return
        mw = self.main_window
        notes = narration.result_notes(result)
        warnings = [mw.i18n.t(n.i18n_key).format(**n.values) for n in notes
                    if n.i18n_key == narration.VAD_UNAVAILABLE_I18N_KEY]
        if result is None or result.is_empty:
            # Said whether it was a key bounce or silence: nothing was written.
            self._dictation_say("dictation_no_speech", extra=warnings)
            return
        jid = (self.conversation or {}).get("remoteJid", "")
        if jid != getattr(self, "_dictation_jid", None) or mw.is_chat_hidden_by_vault(jid):
            # Nothing about the text: it belongs to a conversation not on screen.
            self._dictation_say("dictation_not_inserted", failed=True)
            return
        text = dictation.normalize_transcript(result.text)
        if not self._dictation_insert(text):
            self._dictation_say("dictation_not_inserted", failed=True)
            return
        excerpt, cut = dictation.spoken_excerpt(text)
        if cut:
            sentence = mw.i18n.t("dictation_inserted_long").format(
                count=len(text), text=excerpt)
        else:
            sentence = mw.i18n.t("dictation_inserted").format(text=excerpt)
        mw.speak_output.output(" ".join([sentence] + warnings), interrupt=True)

    def _dictation_insert(self, text) -> bool:
        """Write `text` at the caret, replacing a selection; the focus stays
        where it is. False when the field cannot take it."""
        field = self.message_field
        if not (field.IsEnabled() and field.IsEditable()):
            return False
        start, end = field.GetSelection()
        before = field.GetRange(start - 1, start) if start > 0 else ""
        piece = dictation.insertion_piece(text, before, field.GetRange(end, end + 1))
        if not piece:
            return False
        field.WriteText(piece)
        return True

    # ── Cancelling ───────────────────────────────────────────────────────────

    def _cancel_dictation(self, announce=False):
        """Stop listening or transcribing and drop everything; said only when
        the person pressed the key, not when a conversation change did it."""
        state = getattr(self, "_dictation", None)
        if state is None or state.state == dictation.IDLE:
            return
        state.cancel()
        self._dictation_listening = False
        capture, self._dictation_capture = getattr(self, "_dictation_capture", None), None
        if capture is not None:
            self._dictation_release(capture)
        run, self._dictation_current_run = getattr(self, "_dictation_current_run", None), None
        if run is not None:
            run.cancel()
        if announce:
            self._dictation_play("voicemsg_discard_sound")
            self._dictation_say("dictation_cancelled")

    def _cancel_dictation_now(self):
        """Quitting: stop everything at once; the run to wait for, or None."""
        run = getattr(self, "_dictation_current_run", None)
        self._cancel_dictation()
        return run

    @staticmethod
    def _dictation_release(capture):
        # Closing a stream can block on the driver: never on the wx thread.
        threading.Thread(target=capture.close, daemon=True,
                         name="winzapp-dictation-close").start()

    # ── Saying and sounding ──────────────────────────────────────────────────

    def _dictation_say(self, key, failed=False, extra=()):
        mw = self.main_window
        if failed:
            self._dictation_play("error_sound")
        mw.speak_output.output(" ".join([mw.i18n.t(key), *extra]), interrupt=True)

    def _dictation_say_failure(self, error, run):
        """The sentence a message run gives for the same failure."""
        from ui.transcription_flow import SETTINGS_OFFER_CODES, model_problem_i18n_key
        mw = self.main_window
        code = getattr(error, "code", None)
        if code in SETTINGS_OFFER_CODES:
            key = model_problem_i18n_key(code, getattr(run, "resolution", None), mw.settings)
            self._dictation_say(key, failed=True)
            return
        announcement = narration.outcome_announcement(None, error)
        self._dictation_play("error_sound")
        mw.speak_output.output(
            mw.i18n.t(announcement.i18n_key).format(**announcement.values), interrupt=True)

    def _dictation_play(self, sound_name):
        """A sound of the window's own; one that raises (the output device
        vanished) must not cost the dictation anything."""
        sound = getattr(self.main_window, sound_name, None)
        if sound is None:
            return
        try:
            sound.play()
        except Exception as exc:
            logging.debug("[dictation] a sound did not play: %s", type(exc).__name__)
