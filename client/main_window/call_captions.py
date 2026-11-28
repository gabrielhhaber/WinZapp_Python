"""Live captions of a call (ACC-04): the command, the window and the engine.

The pure rules are ``core/call_captions.py``, the threads are
``core/call_captions_engine.py``, the Whisper run is
``core/call_captions_transcriber.py`` and the window is
``ui/call_captions_window.py``. This is the glue to the call.

What must hold:

* **The call's audio path is not touched beyond one append.** The session's
  ``enqueue_remote_audio()`` offers each received chunk to a ``RemoteTap``;
  nothing here runs on that thread. The tap is attached when the captions
  start and detached when they stop, when the call ends, or (by
  ``_sync_call_captions_button``) moved to the new session after a device
  switch.
* **The command never takes the focus.** The first press shows the window
  without activating it and says one sentence; pressing the command again
  moves to the window. The call window keeps its controls.
* **It refuses, with a sentence, when there is nothing to caption or Whisper is
  busy** (a message transcription holds the model, or a dictation runs): the
  captions never queue behind or in front of the user's own transcription.
  A machine with no model says why, as a message run does; nothing is
  downloaded.
* **Every callback of the engine crosses with ``wx.CallAfter``** and is checked
  against the run's own state, so a result from a stopped run does nothing.
"""

import logging
import threading

import wx

from coord_locks import LockTimeout
from core import call_captions as cc
from core import dictation
from core.call_captions_engine import CaptionEngine, REASON_PROTECTED
from core.call_captions_transcriber import PhraseTranscriber
from core.transcription import errors, external_models, machine_slot, preferences

#: The panels whose dictation may be running.
_DICTATION_PANELS = ("conversations_panel", "archived_conversations_panel",
                     "locked_conversations_panel")


def stop_call_captions(window, *, call_ended=True):
    """Stop the captions of the call, from any thread. The window, if open,
    stays so its text can still be copied or saved; no wx call happens here."""
    state = getattr(window, "_call_captions", None)
    if state is None or state["stopped"]:
        return False
    state["stopped"] = True
    state["engine"].stop()
    session = state["session"]
    try:
        if getattr(session, "_caption_tap", None) is state["tap"]:
            session.set_caption_tap(None)
    except Exception as exc:
        logging.debug("[call_captions] could not detach the tap: %s", type(exc).__name__)
    state["tap"].close()
    wx.CallAfter(_after_stop, window, state, "ended" if call_ended else "stopped")
    return True


def _after_stop(window, state, why):
    shown = state["window"]
    if shown and not shown.IsBeingDeleted():
        shown.set_status(window.i18n.t(
            "call_captions_status_ended" if why == "ended"
            else "call_captions_status_stopped"))


class CallCaptionsMixin:
    def _make_call_captions_button(self, panel):
        button = wx.Button(panel, label=self.i18n.t("call_captions_button"))
        button.Bind(wx.EVT_BUTTON, self._on_call_captions_command)
        button.Hide()
        return button

    def _sync_call_captions_button(self, active):
        button = self.voice_call_window_captions_button
        audio = getattr(self, "_call_audio_session", None)
        state = getattr(self, "_call_captions", None)
        running = bool(state and not state["stopped"])
        if running and audio is not None and state["session"] is not audio and audio.running:
            # A device switch built a new session: the tap follows it.
            state["session"] = audio
            audio.set_caption_tap(state["tap"])
        visible = bool(active and audio and audio.running
                       and (running or self._voice_call_last_announced_state == "ACTIVE"))
        if not visible and button.IsShown() and button.HasFocus() and active:
            self.voice_call_window_mute_button.SetFocus()
        button.Show(visible)

    # ── The command ──────────────────────────────────────────────────────────

    def _on_call_captions_command(self, _event=None):
        state = getattr(self, "_call_captions", None)
        audio = getattr(self, "_call_audio_session", None)
        call = getattr(self, "_active_voice_call", None)
        # The window of a stopped run is still this call's to read; a new call
        # starts its own window and leaves the old one (and its text) alone.
        if (state is not None and state["window"]
                and (not state["stopped"] or state["call"] is call)):
            state["window"].focus_list()
            return
        if getattr(self, "_call_captions_starting", None) is not None:
            return
        panels = (getattr(self, name, None) for name in _DICTATION_PANELS)
        reason = cc.refusal_key(
            in_call=bool(audio is not None and call and audio.running),
            call_active=self._voice_call_last_announced_state == "ACTIVE",
            transcription_busy=machine_slot.slot.holding,
            dictating=any(dictation.is_active(panel) for panel in panels if panel is not None),
        )
        if reason:
            self.output(self.i18n.t(reason), interrupt=True)
            return
        token = object()
        self._call_captions_starting = token
        threading.Thread(target=self._call_captions_prepare, args=(token,),
                         daemon=True, name="winzapp-call-captions-open").start()

    def _call_captions_arguments(self):
        """The install-wide half of a run's arguments, read on a worker (the
        settings file can be held by another window for a moment)."""
        app_settings = getattr(self, "_app_settings", None)
        try:
            stored_dir = preferences.stored_models_dir(app_settings)
        except LockTimeout:
            logging.warning("[call_captions] app.json was held; no install-wide settings")
            return {"stored_models_dir": "", "external_references": None}
        references, known = external_models.read_references(app_settings)
        return {"stored_models_dir": stored_dir,
                "external_references": references if known else None}

    def _call_captions_prepare(self, token):
        """Off the wx thread: is there a model to caption with."""
        transcriber = error = None
        try:
            transcriber = PhraseTranscriber(
                self.settings, ui_language=getattr(self.i18n, "language", ""),
                find_ffmpeg=self._find_api_ffmpeg, arguments=self._call_captions_arguments())
            transcriber.preflight()
        except errors.TranscriptionError as exc:
            error = exc
        except Exception as exc:
            logging.error("[call_captions] preparing failed: %s", type(exc).__name__)
            error = exc
        wx.CallAfter(self._call_captions_ready, token, transcriber, error)

    def _call_captions_ready(self, token, transcriber, error):
        if getattr(self, "_call_captions_starting", None) is not token:
            return
        self._call_captions_starting = None
        audio = getattr(self, "_call_audio_session", None)
        call = getattr(self, "_active_voice_call", None)
        if error is not None:
            self.output(self._call_captions_failure_text(error, transcriber), interrupt=True)
            return
        if audio is None or not call or not audio.running or getattr(self, "_shutting_down", False):
            self.output(self.i18n.t(cc.REASON_NO_CALL), interrupt=True)
            return
        self._start_call_captions(audio, call, transcriber)

    def _call_captions_failure_text(self, error, transcriber):
        from ui.transcription_flow import SETTINGS_OFFER_CODES, model_problem_i18n_key
        code = getattr(error, "code", None)
        if code in SETTINGS_OFFER_CODES:
            return self.i18n.t(model_problem_i18n_key(
                code, getattr(transcriber, "resolution", None), self.settings))
        return self.i18n.t("call_captions_failed")

    def _start_call_captions(self, audio, call, transcriber):
        from ui.call_captions_window import CallCaptionsWindow
        group = bool(call.get("is_group") or call.get("translation_group"))
        # The engine does not say who speaks. In a one-to-one call the received
        # audio is the other person; in a group it is a mix, so no name.
        speaker = "" if group else self.chat_display_name({
            "remoteJid": call.get("peer_jid") or "", "name": call.get("name") or ""})
        tap = cc.RemoteTap()
        state = {"tap": tap, "session": audio, "call": call, "stopped": False,
                 "window": None, "engine": None, "closed": False}
        state["window"] = CallCaptionsWindow(self, on_close=self._on_call_captions_window_closed)
        state["engine"] = CaptionEngine(
            tap, transcriber, speaker=speaker,
            on_entries=lambda entries: wx.CallAfter(self._call_captions_entries, state, entries),
            on_stopped=lambda reason, error: wx.CallAfter(
                self._call_captions_engine_stopped, state, reason),
            on_dropped=lambda total: wx.CallAfter(self._call_captions_dropped, state, total),
            health=cc.AudioHealthGuard(), underruns=lambda: audio.remote_underruns)
        self._call_captions = state
        audio.set_caption_tap(tap)
        state["engine"].start()
        state["window"].open_quietly()
        self.output(self.i18n.t("call_captions_started"))

    # ── What the engine reports ──────────────────────────────────────────────

    def _call_captions_entries(self, state, entries):
        shown = state["window"]
        if shown and not state["closed"]:
            shown.add_entries(entries)

    def _call_captions_dropped(self, state, total):
        shown = state["window"]
        if shown and not state["closed"] and not state["stopped"]:
            shown.set_status(self.i18n.t("call_captions_status_dropped").format(count=total))

    def _call_captions_engine_stopped(self, state, reason):
        """The engine stopped itself: said once, in a sentence."""
        if state["stopped"]:
            return
        if getattr(self, "_call_captions", None) is state:
            stop_call_captions(self, call_ended=False)
        key = "call_captions_protected" if reason == REASON_PROTECTED else "call_captions_failed"
        self.output(self.i18n.t(key), interrupt=True)

    def _on_call_captions_window_closed(self, window):
        state = getattr(self, "_call_captions", None)
        if state is None or state["window"] is not window:
            return
        state["closed"] = True
        state["window"] = None
        was_running = not state["stopped"]
        stop_call_captions(self, call_ended=False)
        self._call_captions = None
        if was_running:
            self.output(self.i18n.t("call_captions_stopped"))
        call_window = getattr(self, "voice_call_window", None)
        if getattr(window, "was_active", False) and call_window is not None \
                and call_window.IsShown():
            self.voice_call_window_mute_button.SetFocus()
