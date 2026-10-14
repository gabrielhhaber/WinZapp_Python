"""AutoTranscriptionMixin — part of MainWindow (see main_window/__init__.py).

Transcribes received voice messages and videos on arrival and keeps the text
in the message's line, when Settings > Local transcription > "Automatically transcribe
received voice messages" is on (off by default). Which messages qualify and the
serial queue are core/transcription/auto_queue.py; this is the glue to
MainWindow: the arrival hook, building the run, and storing the result.

* **Never a window, never a sound, never speech** for a message: the text shows
  up in the row (the open conversation repaints it) like an inline
  transcription made by hand. The only sentence spoken is the notice that
  the queue was paused because local transcription is not set up: once for
  each fault, with the reason a run made by hand would give, and not again
  until a run works or the settings are applied (auto_queue.PauseNotices).
* **Locked chats are skipped** (vault or phone lock): a text nobody asked for
  must not be produced for a conversation the user keeps hidden.
* The audio is fetched the way a manual run does (``handle_audio_message``);
  when WinZapp's own auto-download is about to fetch it, the run waits a short
  while for that download instead of starting a second one.
* Nothing is logged about a message: counts and error codes only.
"""

import logging
import os
import threading
import time

import wx

from app_paths import data_path
from core.transcription import (
    auto_queue,
    errors as transcription_errors,
    external_models,
    message_run,
    preferences as transcription_preferences,
    statuses,
    stored as stored_transcription,
)
from coord_locks import LockTimeout
from core.utils import auto_download_allows, auto_download_enabled
from ui import transcription_flow

#: How long a run waits for the download already under way before fetching itself.
_DOWNLOAD_WAIT_SECONDS = 45.0
_DOWNLOAD_POLL_SECONDS = 1.0
#: How long quitting waits for the runs it cancelled to clean up after themselves.
_SHUTDOWN_JOIN_SECONDS = 2.0


class AutoTranscriptionMixin:
    """Automatic transcription of received voice messages, for MainWindow."""

    def _maybe_auto_transcribe(self, remote_jid: str, msg: dict) -> None:
        """Queue *msg* for automatic transcription if the setting asks for it.

        Called on the wx thread from on_new_message(), for messages that just
        arrived; cheap when the setting is off (one dict lookup).
        """
        if not auto_queue.enabled(self.settings):
            return
        try:
            if not auto_queue.eligible(msg) or self.is_chat_locked(remote_jid):
                return
            # A model chosen explicitly whose cache is off would run for
            # nothing: the result could not be kept in the line.
            _backend, model = stored_transcription.selection_from_settings(self.settings)
            if model != "auto" and not stored_transcription.cache_enabled_for(
                    self.settings, model):
                return
            msg_id = (msg.get("key") or {}).get("id", "")
            self._auto_transcription_queue_get().submit(
                (remote_jid, msg_id), (remote_jid, msg_id, msg))
        except Exception as exc:
            # Whatever this does must never cost the arrival of a message; and
            # when it cannot tell whether the chat is locked, it leaves it alone.
            logging.warning("[transcription] could not queue an automatic transcription: %s",
                            type(exc).__name__)

    def _auto_transcription_queue_get(self):
        queue = getattr(self, "_auto_transcription_queue", None)
        if queue is None:
            queue = auto_queue.AutoTranscriptionQueue(self._run_auto_transcription)
            self._auto_transcription_queue = queue
        return queue

    # ── On the queue's worker thread ─────────────────────────────────────────

    def _run_auto_transcription(self, item) -> None:
        remote_jid, msg_id, msg = item
        if remote_jid == statuses.STATUS_JID:
            # A status update shares this queue (one transcription at a time)
            # and nothing else: its own setting, media and store.
            self._run_status_auto_transcription(msg)
            return
        if not auto_queue.enabled(self.settings):
            return
        try:
            # Checked again here: the queue holds several runs of minutes each,
            # and the chat may have been locked since this one was queued.
            if self.is_chat_locked(remote_jid):
                return
        except Exception:
            return
        outcome = {}

        def _finished(result, error):
            outcome["result"] = result
            outcome["error"] = error

        stored_models_dir, references = self._auto_install_wide_models()
        run = message_run.MessageTranscription(
            msg,
            self.settings,
            self.key,
            data_path("voice_messages"),
            data_path("media"),
            stored_models_dir=stored_models_dir,
            ui_language=getattr(self.i18n, "language", ""),
            external_references=references,
            find_ffmpeg=self._find_api_ffmpeg,
            is_online=lambda: bool(getattr(self, "_wa_connected", False)),
            fetch_media=self._auto_fetch_media,
            on_finished=_finished,
        )
        with self._auto_transcription_lock_get():
            self._auto_transcription_current = run
        queue = getattr(self, "_auto_transcription_queue", None)
        if queue is not None and queue.stopped:
            # Quitting got here first: its cancel missed this run.
            run.cancel()
        try:
            run.start()
            run.join()
        finally:
            with self._auto_transcription_lock_get():
                self._auto_transcription_current = None
        error = outcome.get("error")
        result = outcome.get("result")
        if error is not None:
            self._auto_transcription_failed(error, getattr(run, "resolution", None))
            return
        self._auto_pause_notices_get().forget()
        if result is None or result.is_empty:
            return
        wx.CallAfter(self._store_auto_transcription, remote_jid, msg_id, result)

    def _auto_transcription_lock_get(self):
        lock = getattr(self, "_auto_transcription_lock", None)
        if lock is None:
            lock = self._auto_transcription_lock = threading.Lock()
        return lock

    def stop_transcription_runs(self, timeout=_SHUTDOWN_JOIN_SECONDS) -> None:
        """At shutdown: stop the automatic queue and a bulk run, and cancel the
        transcription each one is running.

        A cancelled run kills its ffmpeg or whisper-cli child and deletes the
        decrypted copy of the audio it made; the process exits right after
        teardown, so without this the child is orphaned and the plaintext
        stays in %TEMP% until a later launch sweeps it. Waits at most
        `timeout` seconds in all.
        """
        running = []
        queue = getattr(self, "_auto_transcription_queue", None)
        if queue is not None:
            queue.stop()
            with self._auto_transcription_lock_get():
                current = getattr(self, "_auto_transcription_current", None)
            if current is not None:
                current.cancel()
                running.append(current)
        panel = getattr(self, "conversations_panel", None)
        cancel_bulk = getattr(panel, "_cancel_bulk_transcription_now", None)
        if callable(cancel_bulk):
            current = cancel_bulk()
            if current is not None:
                running.append(current)
        # A run started by hand with no progress window (ui/transcription_flow.py).
        background = getattr(self, "_background_transcription", None)
        if background is not None:
            current = background.cancel_background()
            if current is not None:
                running.append(current)
        deadline = time.monotonic() + float(timeout)
        for run in running:
            try:
                run.join(max(0.0, deadline - time.monotonic()))
            except Exception as exc:
                logging.info("[transcription] waiting for a cancelled run failed: %s",
                             type(exc).__name__)

    def _auto_install_wide_models(self):
        """(stored models folder, external references or None), as the manual
        flow reads them (ui/transcription_flow.py _install_wide_models)."""
        app_settings = getattr(self, "_app_settings", None)
        try:
            stored_models_dir = transcription_preferences.stored_models_dir(app_settings)
        except LockTimeout:
            return "", None
        references, known = external_models.read_references(app_settings)
        return stored_models_dir, references if known else None

    def _auto_fetch_media(self, msg, media_path) -> bool:
        """Make the audio available on disk; whether it is."""
        will_be_fetched = (
            auto_download_enabled(self.settings) and auto_download_allows(self.settings, msg)
        )
        if will_be_fetched:
            deadline = time.monotonic() + _DOWNLOAD_WAIT_SECONDS
            while time.monotonic() < deadline:
                if os.path.isfile(media_path):
                    return True
                if not getattr(self, "_wa_connected", False):
                    break
                time.sleep(_DOWNLOAD_POLL_SECONDS)
        try:
            if msg.get("messageType") == "audioMessage":
                self.handle_audio_message(msg)
            else:
                self.handle_media_message(msg)
        except Exception as exc:
            logging.info("[transcription] an automatic download raised: %s",
                         type(exc).__name__)
        return os.path.isfile(media_path)

    def _auto_transcription_failed(self, error, resolution=None) -> None:
        """A run that did not produce text: log the code; pause on a setup
        fault, and say so the first time that fault is met."""
        code = getattr(error, "code", "")
        none_reason = getattr(resolution, "model_none_reason", None)
        logging.info("[transcription] automatic transcription ended without text: %s%s",
                     code, f" ({none_reason})" if none_reason else "")
        if not auto_queue.pauses_the_queue(error, resolution):
            return
        self._auto_transcription_queue_get().pause()
        first = self._auto_pause_notices_get().first(code)
        logging.info("[transcription] automatic transcription paused: %s, %s", code,
                     "said" if first else "already said")
        if first:
            wx.CallAfter(self._say_auto_transcription_paused, code, resolution)

    def _auto_pause_notices_get(self):
        notices = getattr(self, "_auto_pause_notices", None)
        if notices is None:
            notices = self._auto_pause_notices = auto_queue.PauseNotices()
        return notices

    def transcription_settings_applied(self) -> None:
        """Settings was applied: what paused the queue may have been fixed
        there, so the next message tries again, and is told about if not."""
        queue = getattr(self, "_auto_transcription_queue", None)
        if queue is not None:
            queue.resume()
        self._auto_pause_notices_get().forget()

    # ── Back on the wx thread ────────────────────────────────────────────────

    def _say_auto_transcription_paused(self, code="", resolution=None) -> None:
        self.output(self.i18n.t("transcription_auto_paused").format(
            reason=self._auto_pause_reason(code, resolution)))

    def _auto_pause_reason(self, code, resolution) -> str:
        """What is wrong, in the sentence a run made by hand gives for it."""
        if code in transcription_flow.SETTINGS_OFFER_CODES:
            key = transcription_flow.model_problem_i18n_key(code, resolution, self.settings)
        else:
            key = transcription_errors.error_i18n_key(code)
        return self.i18n.t(key).format(**transcription_errors.error_i18n_values(code))

    def _store_auto_transcription(self, remote_jid: str, msg_id: str, result) -> None:
        """Keep *result* as the inline transcription of the message and redraw its row."""
        if not auto_queue.enabled(self.settings):
            return
        current = stored_transcription.find_record(
            self._transcription_copies(remote_jid, msg_id), msg_id)
        if current is None:
            return
        previous = current.get(stored_transcription.TRANSCRIPTION_KEY)
        if previous is not None:
            # The user got there first (a manual run, or a deletion): their
            # decision stands.
            return
        value = stored_transcription.with_result(None, stored_transcription.record_from_result(
            result, stored_transcription.next_decision_time(time.time()), inline=True))
        try:
            answer = self.store_message_transcription(remote_jid, msg_id, value)
        except Exception as exc:
            logging.warning("[transcription] storing an automatic transcription failed: %s",
                            type(exc).__name__)
            return
        if answer != stored_transcription.SAVE_STORED:
            return
        if self.is_chat_hidden_by_vault(remote_jid):
            return
        panel = getattr(self, "conversations_panel", None)
        conversation = getattr(panel, "conversation", None)
        if (isinstance(conversation, dict)
                and self._normalize_jid(conversation.get("remoteJid", ""))
                == self._normalize_jid(remote_jid)):
            repaint = getattr(panel, "_repaint_or_repopulate", None)
            if callable(repaint):
                try:
                    repaint([msg_id])
                except Exception as exc:
                    logging.warning("[transcription] repainting an automatic transcription "
                                    "failed: %s", type(exc).__name__)
