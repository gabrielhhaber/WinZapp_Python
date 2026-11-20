"""VoiceRecordingMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import hashlib
import io
import logging
import os
import sound_lib.stream as sl_stream
import tempfile
import threading
import time
import uuid
import wave
import wx
from core.ordered_dispatch import voice_dispatcher
from core.pending_timestamp import pending_send_timestamp
from ui.conversation_panel.channel_conversation import refuse_in_channel
from main_window.typing_feedback import stop_typing_for_capture
from core.message_queue import PendingMessage
from core.message_resend import RESEND_PATH_KEY
from ui.conversation_panel.typing_row import append_message_row, message_row_count
from core.voice_stereo import (
    STEREO_MIMETYPE, bitrate_arg, drop_empty_side, encode_as_stereo,
    stereo_voice_bitrate_kbps,
)
from core.voice_recording_mode import (
    voice_recording_mode, recording_output_channels, prepare_voice_pcm,
)
from core.voice_nr_options import (
    effective_nr_on_failure, nr_local_id, voice_nr_engine, voice_nr_moment)
from core.focus_cloak import cloak_panel_focus_fallback
from app_paths import data_path
from core.utils import encrypt
from core.audio_devices import (
    fallback_input_device_indices,
    find_input_device_index,
    recording_configs_for,
)
try:
    import pyaudio
except ImportError:
    # No wheel exists for PyAudio on Python 3.14 at the time of writing —
    # see requirements.txt's / pyproject.toml's version marker. Voice recording degrades to a
    # clear "not available" message (see _start_voice_recording()) instead
    # of the whole app failing to import.
    pyaudio = None


#: The send path's SINGLE bounded wait (seconds) for the incremental
#: worker's final completion (drain exit + residual finish()). It is a
#: POLICY — prefer the existing basic gate over a longer wait — never
#: a hang diagnosis: is_alive() cannot tell a slow native call from a
#: stuck one. 2.0 s is an EXPERIMENTAL first value, not a validated
#: margin: the only residual measurements so far are feed+finish
#: microbenchmarks on the Linux development box, and earlier Windows
#: runs measured the TOTAL send path under different conditions, so no
#: headroom factor is claimed. The target-machine run may adjust it.
#: The device
#: close wait is deliberately NOT bounded: neither cleaned nor basic
#: audio may come from a frames list a callback can still append to.
#: Module-level so headless adapter tests can patch it without a real
#: panel class.
_INCREMENTAL_SEND_FINAL_WAIT = 2.0


class VoiceRecordingMixin:
    """Recording, previewing and sending voice messages.
    """

    def on_record_voice_message(self, event):
        """
        Ctrl+R / button handler.
        • When NOT recording → start a new voice recording.
        • When recording is active → send the recorded audio (same shortcut).
        """
        if self._is_recording:
            self._send_voice_message(event)
        elif not self._recording_starting:
            self._start_voice_recording()

    # ── Voice recording ──────────────────────────────────────────────────────

    def _voice_recording_silence_enabled(self):
        """Whether all WinZapp spoken content is muted during recording."""
        if getattr(self, "_recording_system_audio", False):
            return False
        settings = getattr(self.main_window, "settings", None) or {}
        return bool(
            settings.get("speech_content", {}).get("silence_while_recording", False)
        )

    def _voice_recording_focus_suppression_enabled(self):
        """Whether WinZapp's automatic recording-button focus stays silent.

        The dedicated silence setting always enables this. Disabling extended
        screen-reader compatibility also suppresses only this native focus
        announcement, without muting unrelated screen-reader speech.
        """
        settings = getattr(self.main_window, "settings", None) or {}
        silence_recording = settings.get("speech_content", {}).get(
            "silence_while_recording", False
        )
        extended_enabled = settings.get("accessibility", {}).get(
            "extended_sr_compat_enabled", True
        )
        return bool(silence_recording or not extended_enabled)

    def _focus_recording_button_silently(self, button):
        """Apply the configured recording focus without leaking speech.

        When recording-focus suppression is enabled, deliberately do not move
        Windows focus to Send/Discard.  NVDA can receive a wx control focus
        through MSAA or UIA; hiding only the MSAA focused state is therefore
        not sufficient on every machine.  Cancelling speech afterwards is
        also too late and is what produced the audible "env..." fragment.

        The recording shortcuts remain frame accelerators (Ctrl+R sends,
        Ctrl+Shift+P pauses, Ctrl+Shift+D discards), so the silent mode does not
        require a synthetic focus event at all.  With suppression disabled we
        preserve the user's normal Send/Discard focus preference.
        """
        if self._voice_recording_focus_suppression_enabled():
            return False
        button.SetFocus()
        return True

    def _silence_send_voice_focus_if_enabled(self):
        """Fallback: cancel a focus announcement that was produced anyway.

        Recording start avoids the focus event entirely when suppression is
        requested. This cancellation burst remains only for other recording
        state changes that can trigger speech. The button keeps its
        native accessible name and shortcut at all times; blanking the name out
        was tried and removed, because it stripped the control's identity from
        the accessibility tree for every consumer, not just from the one
        announcement we wanted gone.

        The repeats exist because there is no single right moment: a screen
        reader that speaks synchronously is caught by the immediate call, and
        one that queues on its own thread by a later one. The spacing is
        front-loaded so that delayed screen-reader output is caught as early
        as possible. Each call is idempotent, so the
        repeats are harmless.
        """
        if getattr(self, "_recording_system_audio", False):
            return
        if not self._voice_recording_focus_suppression_enabled():
            return
        speak_output = getattr(self.main_window, "speak_output", None)
        silence_focus = getattr(speak_output, "silence_screen_reader_focus", None)
        if not callable(silence_focus):
            return
        # silence() (unlike silence_screen_reader_focus) also reaches the SAPI
        # voice, which is WinZapp's own output when no screen reader is running
        # — cutting it is cutting our own speech, never another app's.
        silence_all = (
            getattr(speak_output, "silence", None)
            if self._voice_recording_silence_enabled()
            else None
        )

        def _silence_now():
            # A previous mic-only recording may have queued this burst before
            # the user discarded it and started mixed capture.
            if getattr(self, "_recording_system_audio", False):
                return
            silence_focus()
            if callable(silence_all):
                silence_all()

        _silence_now()
        wx.CallAfter(_silence_now)
        for delay_ms in (40, 90, 160, 260, 400):
            wx.CallLater(delay_ms, _silence_now)

    def _start_voice_recording(self):
        """
        Start capturing audio from the default input device.

        Quality strategy (highest to lowest preference):
          48 000 Hz stereo → 48 000 Hz mono → 44 100 Hz stereo → 44 100 Hz mono

        PyAudio delivers raw, unprocessed PCM — no noise suppression,
        no automatic-gain control, no resampling.  This preserves full voice
        naturalness and quality.
        """
        if refuse_in_channel(self):
            return
        if self.conversation is None:
            return

        if pyaudio is None:
            # No wheel exists for PyAudio on Python 3.14 at the time of
            # writing — see requirements.txt's / pyproject.toml's version marker and this
            # file's own `import pyaudio` — so recording degrades to a
            # clear message instead of crashing on the first pyaudio.*
            # reference below.
            self.main_window.output(self.main_window.i18n.t("voice_recording_unavailable"))
            return

        stop_typing_for_capture(self.main_window)
        # This preference belongs to the recording, including its preview
        # and delayed send worker. Settings changes affect the next recording.
        self._recording_channel_mode = voice_recording_mode(
            getattr(self.main_window, "settings", None))
        self._recording_frames = []
        # An old mic callback may finish after discard and mixed capture
        # starts. Keep it bound to its own list, never the next session's.
        recording_frames = self._recording_frames
        self._recording_paused = False

        # Define callback once, outside the loop; captures self for pause check.
        def _callback(in_data, frame_count, time_info, status):
            # Runs on PyAudio's internal callback thread.
            # list.append is atomic under the GIL — no explicit lock needed.
            if status:
                # paInputOverflow: the capture buffer filled before we drained
                # it (CPU/GIL contention). Logged so choppy recordings are
                # diagnosable; the larger frames_per_buffer below minimises it.
                logging.debug("[audio] input stream status flag: %s", status)
            if not self._recording_paused:
                recording_frames.append(in_data)
            pa_cont = getattr(pyaudio, "paContinue", 0) if pyaudio is not None else 0
            return (None, pa_cont)

        # The (rate, channels) combinations are resolved per device down in
        # _try_open(), through the same recording_configs_for() that
        # core.audio_devices.test_input_device() uses for the Settings-dialog
        # validation — so a device that validates there is still guaranteed to
        # open here; a recording asks for two channels first
        # (core/voice_stereo.py).
        if self._recording_pa is None and pyaudio is not None:
            try:
                self._recording_pa = pyaudio.PyAudio()
            except Exception as exc:
                logging.error("[audio] Failed to initialize PyAudio: %s", exc)

        if pyaudio is None or (self._recording_pa is None and pyaudio is None):
            try:
                import sounddevice as sd
                def _sd_callback(indata, frames, time_info, status):
                    if not self._recording_paused:
                        recording_frames.append(indata.tobytes())
                self._recording_actual_rate = 48000
                self._recording_actual_ch = 1
                self._is_recording = True
                self._start_incremental_denoise(recording_frames, 48000, 1)
                
                # UI updates INSTANTLY (0.01s)
                self.main_window.voicemsg_startrecording_sound.play()
                _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
                if _rec_jid and not _rec_jid.endswith("@newsletter"):
                    self.main_window.send_recording_status(_rec_jid, True, _rec_jid.endswith("@g.us"))
                if self._voice_recording_focus_suppression_enabled():
                    cloak_panel_focus_fallback(
                        self.conversation_panel,
                        self.send_message_btn,
                        self.record_voice_message_btn,
                        self._add_attachment_btn,
                    )
                self.send_message_btn.Hide()
                self.record_voice_message_btn.Hide()
                if hasattr(self, "_record_voice_system_btn"):
                    self._record_voice_system_btn.Hide()
                self._add_attachment_btn.Hide()
                self._pause_resume_btn.SetLabel(self.main_window.i18n.t("pause_recording"))
                self._voice_panel.Show()
                self.conversation_panel.Layout()
                self._focus_recording_button_silently(self._send_voice_btn)

                def _bg_start_mic():
                    try:
                        sd_stream = sd.InputStream(samplerate=48000, channels=1, dtype='int16', callback=_sd_callback)
                        sd_stream.start()
                        self._sd_stream = sd_stream
                        self._recording_stream = sd_stream
                    except Exception as err:
                        logging.error("[audio] Async sounddevice InputStream error: %s", err)
                threading.Thread(target=_bg_start_mic, daemon=True).start()
                return
            except Exception as sd_exc:
                logging.error("[audio] Failed sounddevice fallback recording: %s", sd_exc)
                return

        pa = self._recording_pa

        def _try_open(device_index):
            # Per device, not the shared list: a Bluetooth headset recording
            # over HFP offers only its own 8/16 kHz mono link and refuses every
            # fixed combination. See recording_configs_for(), which keeps the
            # fixed list as the tail so nothing that worked before changes.
            for rate, ch in recording_configs_for(device_index, pa,
                                                  prefer_stereo=True):
                try:
                    s = pa.open(
                        rate=rate,
                        channels=ch,
                        format=pyaudio.paInt16,
                        input=True,
                        input_device_index=device_index,
                        # Larger buffer (~85 ms at 48 kHz) so the Python callback
                        # can tolerate scheduling delays from background sync/media
                        # threads without PortAudio dropping samples (choppy audio).
                        frames_per_buffer=4096,
                        stream_callback=_callback,
                    )
                    s.start_stream()
                    return s, rate, ch
                except Exception:
                    continue
            return None, None, None

        # Settings > Audio Devices lets the user pin a specific recording
        # device (by friendly name — indices aren't stable across reboots).
        # main_window.effective_input_device_name is "" whenever no device is
        # configured, or a prior failure this session already fell back to
        # the system default.
        configured_name = getattr(self.main_window, "effective_input_device_name", "") or ""

        # pa.open() (and find_input_device_index()'s device enumeration) can
        # block for many seconds negotiating with the audio driver — this
        # used to run directly on the UI thread and froze the whole app
        # (wx MainLoop, screen reader included) for as long as it took.
        # Do the actual opening on a background thread instead; only the
        # quick, non-blocking UI updates below run back on the main thread.
        self._recording_starting = True
        self._recording_open_token += 1
        my_token = self._recording_open_token

        def _bg_open_stream():
            # Everything here runs off the UI thread, so an exception escaping
            # this function would die silently in a daemon thread — and take
            # the wx.CallAfter below with it. _recording_starting would then
            # stay True forever and on_record_voice_message()'s
            # `elif not self._recording_starting` guard would refuse every
            # further attempt: the record button goes dead for the rest of the
            # session, with nothing on screen or in the log to say why.
            # find_input_device_index() is the realistic raiser —
            # _pyaudio_input_devices() falls back to get_default_host_api_info()
            # unguarded when the WASAPI query fails, which is exactly the kind
            # of broken audio stack this background open exists to survive.
            # _on_stream_opened() is therefore scheduled from a finally: it is
            # the only thing that clears the flag, so it must run either way.
            stream = rate = ch = None
            fell_back = False
            try:
                input_device_index = find_input_device_index(configured_name, pa) if configured_name else None
                stream, rate, ch = _try_open(input_device_index)

                if stream is None and input_device_index is not None:
                    fell_back = True
                    stream, rate, ch = _try_open(None)

                if stream is None:
                    # Last resort, and the reason this branch exists at all:
                    # _try_open(None) asks PortAudio for the default device of
                    # its *default* host API — MME on Windows — which is not
                    # the same handle set enumerate_input_devices() reads
                    # (WASAPI). One host API refusing a microphone says
                    # nothing about the others; see
                    # fallback_input_device_indices() for the observed
                    # disagreement. Giving up here used to mean "no recording
                    # this session" while a working path to the same mic sat
                    # one index away, unexamined.
                    for idx in fallback_input_device_indices(pa, exclude=(input_device_index,)):
                        stream, rate, ch = _try_open(idx)
                        if stream is not None:
                            logging.info(
                                "[audio] Default input device failed; recording via "
                                "enumerated device index %s instead.", idx,
                            )
                            break
            except Exception:
                logging.exception(
                    "[audio] Failed to open the recording stream (device=%r).",
                    configured_name,
                )
            finally:
                wx.CallAfter(_on_stream_opened, stream, rate, ch, fell_back)

        def _on_stream_opened(stream, rate, ch, fell_back):
            # Discard the result if the user cancelled, or switched/closed
            # the conversation, while the stream was still opening.
            if my_token != self._recording_open_token:
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                return

            self._recording_starting = False

            if fell_back:
                # The configured device worked earlier this session (at
                # startup, or since) but just failed to open — e.g.
                # unplugged mid-session. Keep the stored setting untouched
                # (retried again next launch) but fall back to the system
                # default for the rest of this run.
                self.main_window.effective_input_device_name = ""
                if stream is not None:
                    # Only worth saying when the fallback actually worked.
                    # If it didn't, recording never started at all, and the
                    # message below is the accurate thing to report instead
                    # of two dialogs in a row.
                    wx.MessageBox(
                        self.main_window.i18n.t("audio_device_failed_input").format(device=configured_name),
                        self.main_window.i18n.t("error").format(app_name=self.main_window.app_name),
                        wx.OK | wx.ICON_WARNING, self,
                    )

            if stream is None:
                # This used to `return` in silence unless a *configured*
                # device had failed (fell_back above) — and a pinned device
                # is not the default state. On a machine with no device
                # pinned, every open failure was invisible: no dialog, no
                # sound, nothing in log.log. Reported live as "I press
                # record and nothing happens at all", against a mic whose
                # every sample-rate/channel combo PortAudio rejected with
                # -9999. For a screen-reader-first app that is the worst
                # outcome available — there is no visual cue either, so
                # nothing tells the user whether the app, the shortcut or
                # the microphone is at fault. StatusPanel already warned in
                # this exact situation; the two panels disagreed, and the
                # busier one was the silent one.
                logging.warning(
                    "[audio] No input stream could be opened — recording not started."
                )
                wx.MessageBox(
                    self.main_window.i18n.t("voice_recording_device_failed"),
                    self.main_window.i18n.t("error").format(app_name=self.main_window.app_name),
                    wx.OK | wx.ICON_WARNING, self,
                )
                return

            self._recording_stream      = stream
            self._recording_actual_rate = rate
            self._recording_actual_ch   = ch

            self._is_recording = True
            self._start_incremental_denoise(recording_frames, rate, ch)
            # Hand the handle to the session this recording JUST started
            # (attaching before _start_incremental_denoise() would land it
            # on a STALE session left over from a previous recording,
            # leaving the new one believing its close was confirmed while
            # this stream's callback could still append). The stop path
            # closes the SESSION's own stream, never whatever the panel
            # attributes point at by then.
            _inc = getattr(self, "_incremental_session", None)
            if _inc is not None and not _inc["stopped_event"].is_set():
                _inc["stream"] = stream

            # UI: play sound, swap buttons, focus the configured recording action.
            self.main_window.voicemsg_startrecording_sound.play()

            # Notify contacts that the user is recording audio
            _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
            if _rec_jid and not _rec_jid.endswith("@newsletter"):
                self.main_window.send_recording_status(_rec_jid, True, _rec_jid.endswith("@g.us"))
            keep_message_field_focused = (
                self._voice_recording_focus_suppression_enabled()
            )
            if keep_message_field_focused:
                # Ctrl+R is commonly pressed while the message editor itself
                # owns Windows focus. Hiding that focused native control makes
                # wx/Windows transfer focus to the parent wx.Panel before our
                # recording controls can do anything, which current NVDA
                # announces simply as "Panel". The robust silent path is to
                # leave the editor alive and focused for the recording
                # session. No focus event means there is nothing for NVDA to
                # announce or for WinZapp to race-cancel.
                #
                # The editor already remains visible in the sounddevice
                # fallback path, so this also makes the normal PyAudio path
                # consistent with that established behaviour.
                recording_controls_to_hide = [
                    self.send_message_btn,
                    self.record_voice_message_btn,
                    self._add_attachment_btn,
                ]
                if hasattr(self, "_emoji_btn"):
                    recording_controls_to_hide.append(self._emoji_btn)
                    if hasattr(self, "_writing_help_btn"):
                        recording_controls_to_hide.append(self._writing_help_btn)
                cloak_panel_focus_fallback(
                    self.conversation_panel, *recording_controls_to_hide
                )
            else:
                self.message_field.Hide()
            if hasattr(self, "_emoji_btn"):
                self._emoji_btn.Hide()
                if hasattr(self, "_writing_help_btn"):
                    self._writing_help_btn.Hide()
            self.send_message_btn.Hide()
            self.record_voice_message_btn.Hide()
            if hasattr(self, "_record_voice_system_btn"):
                self._record_voice_system_btn.Hide()
            self._add_attachment_btn.Hide()
            self._pause_resume_btn.SetLabel(
                self.main_window.i18n.t("pause_recording")
            )
            self._voice_panel.Show()
            self.conversation_panel.Layout()
            voice_focus = self.main_window.settings.get("user_interface", {}).get(
                "voice_record_focus", "send"
            )
            if voice_focus == "discard":
                self._focus_recording_button_silently(self._discard_voice_btn)
            else:
                self._focus_recording_button_silently(self._send_voice_btn)

        threading.Thread(target=_bg_open_stream, daemon=True).start()

    # ── Incremental neural noise reduction (test branch) ───────────────────

    def _start_incremental_denoise(self, frames_list, rate, ch):
        """Start cleaning the recording through the model as frames arrive.

        Storage strategy A: the raw frames stay in memory exactly as before
        and the cleaned PCM16 is spooled to a temp file off the UI thread,
        so recording-time RAM matches the current profile; the engine itself
        retains no per-recording growth. Any failure marks the session
        failed and _write_and_enqueue falls back to the existing offline
        path (that fallback also pays the abandoned session's spent work —
        no worst-case claim until measured). Explicit-mono mode averages the
        two captured channels per chunk BEFORE the model, mirroring the
        offline prepare_voice_pcm step; automatic mode's empty-side drop is
        resolved at send time from the cleaned spool (the model handles
        channels independently, so selecting one cleaned side is bit-exact).

        One incremental slot per process (== per account): the slot is taken
        without waiting BEFORE any model load or thread start; when another
        incremental operation holds it, this recording keeps the current
        offline path. The session's local_id is generated here so the
        controller's send-authority registry can key it from the start.
        """
        # A previous session that was never cancelled (a path that missed
        # _cancel_incremental_denoise) must not be orphaned: its drain would
        # spin on a dead frames list forever.
        previous = getattr(self, "_incremental_session", None)
        if previous is not None:
            previous["cancel"] = True
            controller = getattr(self.main_window, "_incremental_controller", None)
            if controller is not None:
                controller.discard(previous.get("local_id", ""))
        self._incremental_session = None
        try:
            if not self.main_window.settings.get("general", {}).get("noise_reduction_enabled", False):
                return
            # The incremental pass only runs for the neural engine while
            # recording. Neural at send time (default) and the basic engine
            # keep the published flow: no session, no slot, no registry, and
            # the send path reduces offline.
            if (voice_nr_engine(self.main_window.settings) != "neural"
                    or voice_nr_moment(self.main_window.settings) != "record"):
                return
            controller = getattr(self.main_window, "_incremental_controller", None)
            if controller is None:
                return
            mode = getattr(self, "_recording_channel_mode", "auto")
            worker_ch = 1 if (mode == "mono" and ch == 2) else ch
            # Identity first: EVERY note gets its own close confirmation,
            # send authority, cancel and echo exclusion — the neural slot
            # only decides whether a worker/drain/spool exist at all.
            session = {
                "local_id": str(uuid.uuid4()),
                "frames": frames_list, "rate": rate, "ch": ch, "worker_ch": worker_ch,
                "worker": None, "thread": None,
                "stop": False, "cancel": False, "failed": False, "revoked": False,
                # Per-session close confirmation, set by
                # _stop_recording_stream: the send thread waits on it
                # before using the raw frames, so the last callback chunks
                # cannot land after the audio was joined. Kept INSIDE the
                # session: a panel attribute would be overwritten by the
                # next recording while a previous send still waits on it.
                "stopped_event": threading.Event(),
                "device_failed": False,
                # Set when the neural slot is occupied by a previous note:
                # the send goes STRAIGHT to the existing basic gate — no
                # wait, no second ONNX engine while the slot is occupied.
                "basic_only": False,
            }
            if not controller.acquire_slot(session["local_id"], session):
                # Slot busy (typically a previous note's drain/finish that
                # has not yet confirmed exit/cleanup): no model, no thread,
                # no spool — but the note KEEPS its registered identity.
                controller.register(session["local_id"], session)
                session["basic_only"] = True
                self._incremental_session = session
                logging.info("[noise_reduction] incremental slot busy; recording falls back to the basic gate")
                return
            try:
                # EVERYTHING after acquire_slot and before the drain owns
                # the session must clean up on failure: a temp-file error
                # (disk/permissions) or a thread start error must not leak
                # the slot and the send-authority registry entry.
                tmp = tempfile.NamedTemporaryFile(prefix="winzapp-inc-", suffix=".pcm", delete=False)
                tmp.close()
                session["spool"] = tmp.name
                thread = threading.Thread(target=self._incremental_drain, args=(session,), daemon=True)
                session["thread"] = thread
                self._incremental_session = session
                thread.start()
            except Exception:
                self._incremental_session = None
                controller.release_slot(session["local_id"])
                controller.forget(session["local_id"])
                spool = session.get("spool")
                if spool:
                    try:
                        os.unlink(spool)
                    except OSError:
                        pass
                raise
            logging.info("[noise_reduction] incremental session started (%d Hz, %d ch -> %d ch)",
                         rate, ch, worker_ch)
        except Exception:
            logging.exception("[noise_reduction] incremental session could not start; offline path remains")

    def _incremental_drain(self, session):
        """Drain thread: load a PRIVATE model instance off the UI thread, then
        run the wx-free controller (core.incremental_denoise.drain_capture_frames,
        which owns the lag/abandonment policy and the spool cleanup).

        The instance is private to this session because sharing one
        InferenceSession between the drain and the offline path would rely
        on concurrent run() being safe in our pinned onnxruntime — a
        property we have NOT verified (neither serialized calls nor native
        fault isolation are claimed). The single slot bounds this to at
        most one incremental instance at a time; the offline singleton
        (nnr._denoiser) stays untouched.
        """
        from core import neural_noise_reduction as nnr
        from core.incremental_denoise import IncrementalDenoiser, drain_capture_frames
        controller = getattr(self.main_window, "_incremental_controller", None)
        try:
            if not os.path.isfile(nnr.model_path()):
                raise FileNotFoundError(nnr.model_path())
            denoiser = nnr.NeuralDenoiser()
            worker = IncrementalDenoiser(session["rate"], session["worker_ch"], denoiser,
                                         spool_path=session["spool"])
            session["worker"] = worker
            transform = None
            if session["worker_ch"] != session["ch"]:
                # Same int16 (L+R)//2 as the offline prepare_voice_pcm.
                transform = lambda chunk: prepare_voice_pcm(chunk, session["ch"], "mono")[0]
            drain_capture_frames(session, worker, transform)
        except Exception:
            logging.exception("[noise_reduction] incremental drain failed; offline path remains")
            session["failed"] = True
            worker = session.get("worker")
            if worker is None:
                # Failed BEFORE the worker existed: the model could not be
                # loaded (missing file or load/ctor error). Per the agreed
                # contract this is "model unavailable": the send goes
                # STRAIGHT to the basic gate — the offline path must not
                # retry the ONNX load for this note.
                session["model_unavailable"] = True
            else:
                try:
                    worker.abort()
                except Exception:
                    pass
                session["worker"] = None
            # The spool is this session's own: remove it even when the
            # failure happened before the worker (and its handle) existed
            # (e.g. model file missing) — drain_capture_frames's own cleanup
            # makes this a no-op when it already ran.
            try:
                os.unlink(session["spool"])
            except OSError:
                pass
            # This failure path OWNED the cleanup (abort above, spool,
            # and the slot in the finally below): a send-time reaper must
            # never repeat it.
            session["drain_cleaned"] = True
        finally:
            # On a cancel/failure exit the cleanup above already ran (worker
            # dead, spool deleted): the slot is released here. On a normal
            # stop the send thread owns worker+spool and releases instead.
            # The send-authority entry is KEPT while PREPARING: a recording
            # whose drain failed mid-capture still sends (offline fallback)
            # and needs its transfer to pass. A discarded/revoked session has
            # no send coming — forget its entry here.
            if session.get("drain_cleaned") and controller is not None:
                controller.release_slot(session["local_id"])
                if not controller.is_preparing(session["local_id"]):
                    controller.forget(session["local_id"])

    def _cancel_incremental_denoise(self):
        """Ask the drain thread to abort and delete its spool (non-blocking)."""
        session = getattr(self, "_incremental_session", None)
        if session is not None:
            session["cancel"] = True
            controller = getattr(self.main_window, "_incremental_controller", None)
            if controller is not None:
                controller.discard(session.get("local_id", ""))
            self._incremental_session = None

    @staticmethod
    def _close_stream_handle(stream):
        # PyAudio streams use stop_stream(); sounddevice/sound_lib streams
        # use stop(). Both have close().
        # close() always runs: a stop() that raises (unplugged USB/BT
        # headset) must not leak the handle. The stop error still
        # propagates so the caller reports the close as unconfirmed.
        try:
            if hasattr(stream, "stop_stream"):
                stream.stop_stream()
            else:
                stream.stop()
        finally:
            stream.close()

    def _stop_recording_stream(self, session=None):
        """Stop and close the stream that belongs to *session*.

        Handles are PER SESSION, captured at open time: closing recording
        A must never touch the panel attributes a newer recording B already
        owns. The panel attributes are cleared only when they still point
        at THIS session's stream. With no session (noise reduction off),
        the panel attributes are the handles, as upstream.

        Close confirmation is reported PER SESSION: ``stopped_event`` is
        set only when the stream closed cleanly (no capture callback can
        append after a confirmed close); any failure sets
        ``device_failed`` instead and still sets the event so the send
        thread wakes and takes the occupied path — a failed close is never
        reported as confirmed.
        """
        if session is None:
            session = getattr(self, "_incremental_session", None)
        ok = True
        stream = session.pop("stream", None) if session is not None else None
        if stream is not None:
            try:
                self._close_stream_handle(stream)
            except Exception:
                ok = False
                logging.exception("[voice] capture stream did not close cleanly")
            if getattr(self, "_recording_stream", None) is stream:
                self._recording_stream = None
            if getattr(self, "_sd_stream", None) is stream:
                self._sd_stream = None
        elif session is None:
            for attr in ("_sd_stream", "_recording_stream"):
                other = getattr(self, attr, None)
                if other is None:
                    continue
                try:
                    self._close_stream_handle(other)
                except Exception:
                    ok = False
                    logging.exception("[voice] %s did not close cleanly", attr)
                setattr(self, attr, None)
        # A session without an attached stream (open still pending or
        # failed): no callback can be appending to its frames — nothing to
        # close, and the confirmation below is accurate.
        if session is not None:
            session["device_failed"] = not ok
            session["stopped_event"].set()

    def _on_destroy(self, event):
        """Clean up capture resources when the panel itself is destroyed."""
        if event.GetEventObject() is not self:
            event.Skip()
            return
        if getattr(self, "_recording_system_audio", False):
            self._stop_system_audio_recording()
            self._recording_system_audio = False
            self._is_recording = False
            self._recording_frames = []
        # Any active recording — normal mic included — loses its incremental
        # session with the panel (drain cancels, spool cleaned, slot freed).
        self._cancel_incremental_denoise()
        # Live fourth-option attempts lose their context with the panel:
        # in-flight workers finish natively, but their UI dispatch cleans
        # up quietly instead of calling into destroyed controls (review
        # 18:47, contract 2).
        self._nr_invalidate_attempts()
        if self._recording_pa is not None:
            try:
                self._recording_pa.terminate()
            except Exception:
                pass
            self._recording_pa = None
        if getattr(self, "_video_player", None) is not None:
            self._video_player.stop()
        event.Skip()

    def _hide_voice_panel(self):
        """Hide the voice panel and restore the message field / record /
        send button visibility (sent or discarded — both call this)."""
        if getattr(self, "_recording_system_audio", False):
            self._pause_resume_btn.Enable()
            self._send_voice_btn.Enable()
            self._recording_system_audio = False
            self._system_audio_interrupted = False
        self._update_system_audio_volume_controls()
        self._stop_recorded_audio_preview()
        self._play_recorded_btn.Hide()
        self._voice_panel.Hide()
        self.message_field.Show()
        if hasattr(self, "_emoji_btn"):
            self._emoji_btn.Show()
            if hasattr(self, "_writing_help_btn"):
                self._writing_help_btn.Show()
        if self.message_field.GetValue().strip():
            self.send_message_btn.Show()
        else:
            self.record_voice_message_btn.Show()
            if hasattr(self, "_record_voice_system_btn"):
                self._record_voice_system_btn.Show()
        self._add_attachment_btn.Show()
        self.conversation_panel.Layout()

    def _discard_voice_message(self, event):
        """Discard the current recording without sending."""
        mixed = getattr(self, "_recording_system_audio", False)
        if not self._is_recording and not (mixed and self._recording_starting):
            return
        if mixed:
            self._stop_system_audio_recording()
        self.main_window.voicemsg_discard_sound.play()
        if not mixed:
            threading.Thread(target=self._stop_recording_stream, daemon=True).start()
        self._is_recording     = False
        self._recording_paused = False
        self._recording_frames = []
        self._cancel_incremental_denoise()
        # Notify contacts that recording stopped
        _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if _rec_jid and not _rec_jid.endswith("@newsletter"):
            self.main_window.send_recording_status(_rec_jid, False, _rec_jid.endswith("@g.us"))
        self._hide_voice_panel()
        self.message_field.SetFocus()

    def _toggle_pause_recording(self, event):
        """Pause or resume the ongoing recording."""
        if not self._is_recording or getattr(self, "_system_audio_interrupted", False):
            return
        if getattr(self, "_recording_system_audio", False):
            self._toggle_system_audio_pause()
            return
        self.main_window.voicemsg_pauserecording_sound.play()
        self._recording_paused = not self._recording_paused
        label_key = "resume_recording" if self._recording_paused else "pause_recording"
        self._pause_resume_btn.SetLabel(self.main_window.i18n.t(label_key))
        if self._recording_paused:
            self._play_recorded_btn.Show()
        else:
            # Resuming appends new frames again — the paused-audio preview
            # would go on playing a now-stale snapshot, so stop it outright.
            self._stop_recorded_audio_preview()
            self._play_recorded_btn.Hide()
        self.conversation_panel.Layout()
        self._silence_send_voice_focus_if_enabled()

    def _toggle_play_recorded_audio(self, event):
        """Play back everything recorded so far, or stop that playback if
        it's already going. Ctrl+P / the "Reproduzir áudio gravado" button
        next to "Continuar gravação" — only ever reachable while paused,
        both because the button is hidden otherwise and because frames stay
        stable only while paused (the PyAudio callback skips appending while
        self._recording_paused, see on_record_voice_message's _callback)."""
        if not self._is_recording or not self._recording_paused:
            return
        session = getattr(self, "_system_audio_session", None)
        if session is not None and session.get("transition"):
            return
        if self._recorded_audio_sound is not None:
            self._stop_recorded_audio_preview()
            return

        frames = self._recording_frames
        if not frames:
            return

        mode = ("auto" if getattr(self, "_recording_system_audio", False) else
                getattr(self, "_recording_channel_mode", "auto"))
        try:
            preview_pcm, preview_channels = prepare_voice_pcm(
                b"".join(frames), self._recording_actual_ch, mode)
            # The preview plays what the send would, an empty side included.
            if mode == "auto" and not getattr(self, "_recording_system_audio", False):
                preview_pcm, preview_channels = drop_empty_side(preview_pcm, preview_channels)
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(preview_channels)
                wf.setsampwidth(2)   # 16-bit PCM — matches how _send_voice_message writes it
                wf.setframerate(self._recording_actual_rate)
                wf.writeframes(preview_pcm)
        except Exception as exc:
            logging.warning("[voice] failed to write recorded-audio preview WAV: %s", exc)
            return
        self._recorded_audio_temp_path = tmp.name

        # A plain sl_stream.FileStream, not the app's Sound/load_sound
        # wrapper: Sound.play() reroutes to Settings > Audio Devices'
        # separate "effects" device when one is configured, which is meant
        # for short UI cue sounds, not the user's own recorded voice. This
        # is exactly how _play_audio() opens a real incoming voice message
        # (sl_stream.FileStream direct) — it just inherits BASS's current
        # process-wide default device, i.e. the configured Output device.
        try:
            snd = sl_stream.FileStream(file=self._recorded_audio_temp_path)
            snd.play()
        except Exception as exc:
            logging.warning("[voice] failed to play recorded-audio preview: %s", exc)
            self._cleanup_recorded_audio_temp_file()
            return
        self._recorded_audio_sound = snd
        self._play_recorded_btn.SetLabel(self.main_window.i18n.t("stop_recorded_audio_playback"))
        self._recorded_audio_timer.Start(300)

    def _on_recorded_audio_timer(self, event):
        """sound_lib has no playback-finished callback (see
        AlertPreviewController's identical polling in core/sound_system.py)
        — reaching the end of the preview must still be a full stop, not
        just leaving the button stuck on "Parar reprodução"."""
        if self._recorded_audio_sound is None or not self._recorded_audio_sound.is_playing:
            self._stop_recorded_audio_preview()

    def _stop_recorded_audio_preview(self):
        """Full stop (not pause) of the recorded-audio preview and reset of
        the button back to "Reproduzir áudio gravado" — called whether
        playback finished on its own, the user clicked "Parar reprodução",
        the recording was resumed, or the voice panel is going away
        (discard/send). Safe to call when nothing is playing."""
        self._recorded_audio_timer.Stop()
        if self._recorded_audio_sound is not None:
            try:
                self._recorded_audio_sound.stop()
            except Exception:
                pass
            self._recorded_audio_sound = None
        self._cleanup_recorded_audio_temp_file()
        if hasattr(self, "_play_recorded_btn"):
            self._play_recorded_btn.SetLabel(self.main_window.i18n.t("play_recorded_audio"))

    def _cleanup_recorded_audio_temp_file(self):
        if self._recorded_audio_temp_path is not None:
            try:
                os.unlink(self._recorded_audio_temp_path)
            except Exception:
                pass
            self._recorded_audio_temp_path = None

    def _reduce_for_send(self, mw, audio_data, rate, channels, basic_only,
                         on_failure="basic"):
        """Noise-reduction step of the send path.

        basic_only=True means the incremental slot was occupied (the model
        busy with the previous note) when this note was recorded: go
        STRAIGHT to the existing basic gate — no waiting and no second
        ONNX engine while the slot is occupied. basic_only=False is the
        published path unchanged: the neural model with the existing gate
        fallback inside reduce_voice_noise. If the chosen engine fails the
        note goes out unprocessed with the published announcement — there
        is no further escalation step. Returns (audio_data, engine) with
        engine None when both failed.

        on_failure="keep_pending" (the user's choice, only meaningful with
        basic_only=False) changes none of the processing — only the
        announcements: the caller keeps the note pending instead of sending
        the gate/unprocessed result, so neither "basic was used" nor
        "sent unprocessed" may be announced here.
        """
        keep_pending = on_failure == "keep_pending" and not basic_only
        try:
            if basic_only:
                logging.info("[VOICE_TIMING] Applying the basic noise gate "
                             "(incremental slot was occupied)...")
                from core.audio_processing import apply_noise_gate
                audio_data = apply_noise_gate(audio_data, rate, channels, strict=True)
                engine = "gate"
            else:
                logging.info("[VOICE_TIMING] Applying microphone noise reduction...")
                from core.neural_noise_reduction import reduce_voice_noise
                audio_data, engine = reduce_voice_noise(audio_data, rate, channels)
            logging.info("[VOICE_TIMING] Noise reduction engine: %s", engine)
            if engine != "neural" and not keep_pending:
                # Never let the plain gate pass for the neural model.
                announce = getattr(mw, "output", None)
                if announce is not None:
                    wx.CallAfter(announce, mw.i18n.t("noise_reduction_fallback"))
            return audio_data, engine
        except Exception as ex:
            logging.error("[VOICE_TIMING] Failed to apply noise reduction: %s", ex)
            if keep_pending:
                return audio_data, None
            # Both engines failed: the note goes out unprocessed, say so.
            announce = getattr(mw, "output", None)
            if announce is not None:
                wx.CallAfter(announce, mw.i18n.t("noise_reduction_failed"))
            return audio_data, None

    def _nr_pending_row_alive(self, local_id):
        """UI-thread liveness of the kept-pending row (delete wins races).

        Only meaningful for the OPEN conversation: ``_sorted_messages`` holds
        that chat alone. A row of another chat is judged by
        :meth:`_nr_pending_record_alive`.
        """
        if not local_id:
            return True
        return any(
            isinstance(m, dict) and m.get("key", {}).get("id") == local_id
            for m in self._sorted_messages)

    def _nr_pending_record_alive(self, local_id, remote_jid):
        """Whether the pending row still exists in its OWN chat's records.

        Unlike :meth:`_nr_pending_row_alive` this survives a chat switch: the
        send thread can finish while another conversation is open, and a row
        that is merely off screen is not a deleted row.
        """
        if not local_id:
            return True
        chat = self.main_window.get_chat(remote_jid)
        return self._record_position(chat, local_id) >= 0

    def _nr_finish_kept_pending(self, local_id, remote_jid, virtual_msg, key):
        """UI thread: end of the keep-pending write for ONE row.

        Deleted row (gone from its chat's records): the kept file dies with
        it and nothing is announced. Row on screen: the normal failed-row
        path. Row off screen (the user switched chat): the same state is
        written on the record itself so the later "Try sending again" works.
        """
        mw = self.main_window
        path = virtual_msg.get(RESEND_PATH_KEY)
        if not self._nr_pending_record_alive(local_id, remote_jid):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return False
        if self._nr_pending_row_alive(local_id):
            self._mark_message_failed(local_id)
        else:
            virtual_msg["_local_pending"] = False
            virtual_msg["_send_failed"] = True
            self._outgoing_virtual_messages.pop(local_id, None)
            mw._schedule_save(dirty_jid=remote_jid)
        announce = getattr(mw, "output", None)
        if announce is not None:
            announce(mw.i18n.t(key))
        return True

    # --- Fourth-option attempt authority (review 18:47, contract 2) ---
    # An attempt is registered per NOTE (local_id) on the PANEL, never as
    # a flag on the row dict: a row rehydrated as a new dict with the
    # same id cannot slip a second attempt past a per-dict flag, and a
    # panel destroy invalidates every live attempt even when the message
    # list survives the destroy.

    def _nr_attempt_begin(self, local_id, remote_jid):
        """Take attempt authority for a kept-pending/handoff-produced
        note. Returns the attempt token, or None when the panel is dead
        or an attempt is already live for this note. The token pins the
        attempt's context (this panel, this conversation); both are
        re-validated on the UI thread before any handoff."""
        if getattr(self, "_nr_panel_dead", False):
            return None
        registry = getattr(self, "_nr_attempts", None)
        if registry is None:
            registry = self._nr_attempts = {}
        if local_id in registry:
            return None
        token = uuid.uuid4().hex
        registry[local_id] = (token, remote_jid)
        return token

    def _nr_begin_action(self, msg, remote_jid):
        """(local_id, token) for a user action on a kept-pending note.

        A second press while one is live gets no token; it is SPOKEN (the
        action is already running) instead of silently dropped.
        """
        local_id = nr_local_id(msg)
        token = self._nr_attempt_begin(local_id, remote_jid)
        if token is None:
            logging.info("[noise_reduction] an action on this pending note "
                         "is already in flight; ignoring the extra request")
            mw = self.main_window
            mw.output(mw.i18n.t("resend_started"), interrupt=True)
        return local_id, token

    def _nr_attempt_end(self, local_id, token):
        registry = getattr(self, "_nr_attempts", None)
        if registry is not None and registry.get(local_id, (None,))[0] == token:
            registry.pop(local_id, None)

    def _nr_attempt_context_alive(self, local_id, token, remote_jid):
        """UI-thread re-validation before touching widgets: the panel is
        alive, THIS attempt is still the current one for the note, the
        conversation did not switch, and the row is still there."""
        if getattr(self, "_nr_panel_dead", False):
            return False
        registry = getattr(self, "_nr_attempts", None)
        if registry is None or registry.get(local_id, (None,))[0] != token:
            return False
        conversation = getattr(self, "conversation", None) or {}
        if (conversation.get("remoteJid")
                and not self.main_window._chat_jids_equivalent(
                    conversation.get("remoteJid"), remote_jid)):
            return False
        return self._nr_pending_row_alive(local_id)

    def _nr_attempt_may_speak(self, local_id, token, remote_jid):
        """Whether the outcome of an attempt is still worth announcing.

        True when the context is fully alive, and also when the user only
        switched chat while the note was processing: the outcome is spoken
        rather than lost. False for a dead panel, a superseded attempt or a
        row deleted from its own chat (nothing left to tell about).
        """
        if self._nr_attempt_context_alive(local_id, token, remote_jid):
            return True
        if getattr(self, "_nr_panel_dead", False):
            return False
        registry = getattr(self, "_nr_attempts", None)
        if registry is None or registry.get(local_id, (None,))[0] != token:
            return False
        return self._nr_pending_record_alive(local_id, remote_jid)

    def _nr_invalidate_attempts(self):
        """Panel destroy / teardown: every live attempt loses its
        context. In-flight workers still finish their native call, but
        their UI dispatch observes the dead panel and cleans up quietly —
        never a callback into destroyed controls (review 18:47)."""
        self._nr_panel_dead = True
        registry = getattr(self, "_nr_attempts", None)
        if registry is not None:
            registry.clear()

    def _nr_pending_announce(self, local_id, token, remote_jid, key):
        """End the attempt and announce — only when the attempt's
        context is still alive on the UI thread (where deletions run)."""
        mw = self.main_window

        def _do():
            speak = self._nr_attempt_may_speak(local_id, token, remote_jid)
            self._nr_attempt_end(local_id, token)
            if speak:
                mw.output(mw.i18n.t(key), interrupt=True)
        wx.CallAfter(_do)

    def _read_nr_pending(self, msg, path):
        """Fingerprint-checked read of a kept-pending WAV.

        ONE stable copy: the bytes are read a single time and the size /
        sha256 check AND the WAV parse both run over THAT copy — a file
        replaced between two opens can never pass validation with one
        content and be processed as another (review 18:47). The
        fingerprint applies WHEN one was recorded at keep time; a note
        without it is processed unchecked — the fingerprint is not
        claimed mandatory. Returns (pcm, rate, channels, seconds); raises
        on any mismatch or unreadable file — the caller announces and
        keeps the row pending.
        """
        expected_size = msg.get("_nr_pending_size")
        expected_hash = msg.get("_nr_pending_sha256")
        with open(path, "rb") as fh:
            raw = fh.read()
        if expected_size is not None and len(raw) != expected_size:
            raise ValueError("kept note size changed on disk")
        if expected_hash is not None and \
                hashlib.sha256(raw).hexdigest() != expected_hash:
            raise ValueError("kept note content changed on disk")
        with wave.open(io.BytesIO(raw), "rb") as wf:
            channels = wf.getnchannels()
            rate = wf.getframerate()
            if wf.getsampwidth() != 2:
                raise ValueError("unexpected sample width")
            seconds = int(wf.getnframes() / float(rate)) if rate else 0
            data = wf.readframes(wf.getnframes())
        return data, rate, channels, seconds

    def _nr_pending_handoff(self, msg, remote_jid, wav_path,
                            channels, rate, seconds, token,
                            fail_key="noise_reduction_kept_pending"):
        """Hand a fourth-option note's audio to the voice-note pipeline.

        Runs on a worker thread; ends in a UI-thread dispatch. The audio
        goes out AS A VOICE NOTE — same encode the recording would use
        (OGG Opus mono / AAC M4A stereo), ptt, the recording's channels,
        the original reply quote — never as a generic file attachment.

        Honest handoff (review 18:47, contracts 3-5): the REPRESENTATION
        is registered before the queue is activated, so even a
        synchronous echo finds the row — never a late duplicate pending;
        a rejected enqueue rolls that representation back. The kept
        original and the old row are dropped only AFTER the queue
        accepted the copy (queued LOCALLY, not delivered). The new row
        carries RESEND_PATH_KEY to the persisted copy and a
        processed-voice marker, so a later transport failure resends
        THOSE bytes as a voice note again (_resend_processed_voice) —
        never re-reduced, never a generic attachment. The copy's
        ownership then passes to the send path (recording_path reaches
        _on_message_sent; its exact cleanup moment is not asserted here).
        Every exit without delivery removes ALL artifacts this handoff
        created (staged WAV, persisted copy, .msv playback copy) — never
        the valid kept original.
        """
        mw = self.main_window
        local_id = nr_local_id(msg)
        # Encode off the UI thread exactly like the recording path.
        ogg_bytes = None
        voice_mimetype = ""
        if channels >= 2:
            ogg_bytes = self._encode_stereo_voice(wav_path)
            if not ogg_bytes:
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass
                self._nr_pending_announce(local_id, token, remote_jid, fail_key)
                return
            voice_mimetype = STEREO_MIMETYPE
        else:
            ogg_path = None
            try:
                ogg_path = mw._convert_wav_to_ogg(wav_path)
                if ogg_path and os.path.isfile(ogg_path):
                    with open(ogg_path, "rb") as f_in:
                        ogg_bytes = f_in.read()
            except Exception as exc:
                logging.warning("[noise_reduction] pending handoff OGG "
                                "pre-encode failed: %s", exc)
                ogg_bytes = None
            finally:
                # The converter's temp OGG dies here either way — a read
                # failure must not leave it behind (review 18:59).
                if ogg_path:
                    try:
                        os.unlink(ogg_path)
                    except OSError:
                        pass
        # A persistent home for the copy: the new row's RESEND_PATH_KEY
        # points at it; remove_messages_by_id disposes of it with its row.
        new_local_id = str(uuid.uuid4())
        persisted = None
        msv_path = None
        try:
            os.makedirs(data_path("voice_messages"), exist_ok=True)
            persisted = os.path.join(
                data_path("voice_messages"), new_local_id + ".resend.wav")
            os.replace(wav_path, persisted)
        except OSError as exc:
            logging.error("[noise_reduction] could not persist the pending "
                          "handoff: %s", exc)
            try:
                os.unlink(wav_path)
            except OSError:
                pass
            self._nr_pending_announce(local_id, token, remote_jid, fail_key)
            return
        # Local playback copy, same encryption as any recorded note — and
        # the same payload: the encoded bytes, or the raw PCM when the OGG
        # pre-encode was unavailable (never the WAV container). A partial
        # write dies here, not as an orphan (review 18:47, contract 5).
        try:
            with wave.open(persisted, "rb") as wf:
                pcm_bytes = wf.readframes(wf.getnframes())
            msv_path = os.path.join(
                data_path("voice_messages"), new_local_id + ".msv")
            with open(msv_path, "wb") as f_out:
                f_out.write(encrypt(ogg_bytes or pcm_bytes, mw.key))
        except Exception as exc:
            logging.warning("[noise_reduction] could not save the local "
                            "copy of the pending handoff: %s", exc)
            if msv_path is not None:
                try:
                    os.unlink(msv_path)
                except OSError:
                    pass
                msv_path = None
        # Fingerprint of the PROCESSED copy: its own later resend
        # validates it with the same single-stable-read rule. Skipped
        # quietly when unreadable — never claimed mandatory.
        try:
            with open(persisted, "rb") as fh:
                processed_raw = fh.read()
            processed_hash = hashlib.sha256(processed_raw).hexdigest()
            processed_size = len(processed_raw)
        except OSError:
            processed_hash = processed_size = None
        quoted = None
        ctx = msg.get("contextInfo")
        if isinstance(ctx, dict) and ctx.get("stanzaId"):
            quoted = {"key": {"id": ctx.get("stanzaId", ""),
                              "participant": ctx.get("participant", ""),
                              "fromMe": bool(ctx.get("_quotedFromMe", False)),
                              "remoteJid": remote_jid},
                      "message": ctx.get("quotedMessage") or {}}
        pm = PendingMessage(new_local_id, remote_jid,
                            audio_path=persisted,
                            ogg_bytes=ogg_bytes, quoted=quoted,
                            voice_mimetype=voice_mimetype)

        def _cleanup_artifacts():
            # Every artifact THIS handoff created — never the valid kept
            # original, which the old row still owns.
            for artifact in (persisted, msv_path):
                if artifact:
                    try:
                        os.unlink(artifact)
                    except OSError:
                        pass

        def _dispatch():
            # UI thread. The attempt's context must still be alive: a
            # deleted row, a destroyed panel or a switched conversation
            # ends the attempt quietly and the staged artifacts die here.
            if not self._nr_attempt_context_alive(local_id, token, remote_jid):
                speak = self._nr_attempt_may_speak(local_id, token, remote_jid)
                self._nr_attempt_end(local_id, token)
                _cleanup_artifacts()
                if speak:
                    # Chat switched while processing: the note stays kept
                    # pending, and the user is told instead of left guessing.
                    mw.output(mw.i18n.t(fail_key), interrupt=True)
                return
            new_msg = {
                "_local_pending": True,
                "_local_id": new_local_id,
                "_is_voice_recording": True,
                # Handoff product: a later "Try sending again" on THIS row
                # resends these exact bytes as a voice note
                # (_resend_processed_voice) — never re-reduced, never a
                # generic attachment (review 18:47, contract 4).
                "_nr_processed_voice": True,
                "key": {"id": new_local_id, "fromMe": True,
                        "remoteJid": remote_jid},
                "messageType": "audioMessage",
                "message": {"audioMessage": {
                    "seconds": seconds,
                    "ptt": True,
                    **({"mimetype": "audio/mp4",
                        "fileName": new_local_id + ".m4a"}
                       if channels >= 2 else {})}},
                "messageTimestamp": pending_send_timestamp(
                    time.time(), self._sorted_messages),
                "pushName": "",
                RESEND_PATH_KEY: persisted,
            }
            if processed_hash is not None:
                new_msg["_nr_pending_sha256"] = processed_hash
                new_msg["_nr_pending_size"] = processed_size
            if quoted:
                new_msg["contextInfo"] = ctx
            # Representation BEFORE queue activation (review 18:47,
            # contract 3): enqueue() wakes the sender, and an echo that
            # arrives synchronously must already find the row and its
            # virtual registration — or it lands as a late duplicate
            # pending row.
            self._clear_empty_placeholder()
            self._sorted_messages.append(new_msg)
            append_message_row(self, self._render_message_line(new_msg))
            self._register_virtual_msg(new_msg)
            try:
                mw.message_queue.enqueue(pm)
            except Exception as exc:
                logging.error("[noise_reduction] could not queue the pending "
                              "handoff: %s", exc)
                # Roll the representation back, then the artifacts; the
                # old row stays pending, untouched.
                self._outgoing_virtual_messages.pop(new_local_id, None)
                self.remove_messages_by_id({new_local_id}, focus_previous=False)
                self._nr_attempt_end(local_id, token)
                _cleanup_artifacts()
                mw.output(mw.i18n.t(fail_key), interrupt=True)
                return
            # The queue owns the copy now — queued LOCALLY, not delivered:
            # the new row reads as an ordinary pending voice note. Only now
            # does the old failed row go away, taking the kept ORIGINAL
            # with it.
            old_local_id = str(msg.get("_local_id") or "")
            old_ids = {i for i in (old_local_id,
                                   str((msg.get("key") or {}).get("id") or "")) if i}
            if old_local_id:
                mw.message_queue.cancel(old_local_id)
                self._outgoing_virtual_messages.pop(old_local_id, None)
            if old_ids:
                self.remove_messages_by_id(old_ids, focus_previous=True)
            if msg.get("_nr_processed_voice"):
                # The old row was itself a handoff product: its local
                # playback cache (.msv) is still named after the LOCAL id
                # (only a DELIVERED row's cache is renamed to the real
                # WhatsApp id) and remove_messages_by_id does not collect
                # it — it dies with its row here, or a second handoff
                # leaves it orphaned (review 18:59). Valid caches of rows
                # that stay are never touched.
                from ui.conversation_panel.media_paths import \
                    discard_local_media_cache
                discard_local_media_cache(
                    data_path("voice_messages"), data_path("media"),
                    old_local_id)
            self._nr_attempt_end(local_id, token)
            mw._schedule_set_chats()
            mw.output(mw.i18n.t("resend_started"), interrupt=True)

        wx.CallAfter(_dispatch)

    def _retry_nr_pending(self, msg, remote_jid, path):
        """Retry the PROCESSING of a fourth-option kept-pending note.

        The user chose voice_nr_on_failure=keep_pending: when both neural
        processings failed, the note stayed pending instead of going out
        with the basic gate or unprocessed. "Try sending again" therefore
        re-runs the NEURAL reduction on the kept original (off the UI
        thread) and hands the PROCESSED audio to the ordinary voice-note
        pipeline (_nr_pending_handoff). Audited contracts (reviews 18:28
        and 18:47), all local:

        - Never automatic: only this explicit user action runs it.
        - Attempt authority per NOTE, registered on the panel with a
          token + context (panel, conversation), re-validated on the UI
          thread before any handoff: ONE live attempt per note — a second
          action, even from a rehydrated row dict with the same id, is
          ignored; a panel destroy invalidates the attempt.
        - Never the gate: engine != "neural" leaves the row pending,
          untouched. (Sending the ORIGINAL is a separate, equally
          explicit user action: _send_nr_pending_raw.)
        - One model at a time, by RESERVATION not probe: the retry
          reserves the offline side under the controller's lock before
          waiting on the singleton's inference lock, and acquire_slot()
          refuses a capture while the reservation stands — the exclusion
          is effective in both directions (review 18:47, contract 1).
        - Immutability: the kept file is read ONCE and must still match
          the size/sha256 recorded when it was kept (when a fingerprint
          was recorded at all), or nothing is processed.
        - Delete wins: the attempt's context is re-checked on the UI
          thread before any announcement and before the handoff.
        - Destination/account: identical to the existing attachment
          resend (the row's own remoteJid through this panel's live
          session); no new destination surface.
        """
        mw = self.main_window
        local_id, token = self._nr_begin_action(msg, remote_jid)
        if token is None:
            return

        def _work():
            controller = getattr(mw, "_incremental_controller", None)
            reserved = False
            if controller is not None:
                if not controller.try_reserve_offline(local_id):
                    logging.info("[noise_reduction] kept-pending retry "
                                 "deferred: incremental slot or another "
                                 "offline pass busy")
                    self._nr_pending_announce(local_id, token, remote_jid,
                                              "noise_reduction_kept_pending")
                    return
                reserved = True
            try:
                try:
                    data, rate, channels, seconds = self._read_nr_pending(msg, path)
                except Exception as exc:
                    logging.warning("[noise_reduction] kept-pending retry could "
                                    "not read %s: %s", path, exc)
                    self._nr_pending_announce(local_id, token, remote_jid,
                                              "resend_not_available")
                    return
                engine = None
                try:
                    from core.neural_noise_reduction import reduce_voice_noise
                    data, engine = reduce_voice_noise(data, rate, channels)
                except Exception as exc:
                    logging.info("[noise_reduction] kept-pending retry reduction "
                                 "failed again: %s", exc)
                if engine != "neural":
                    self._nr_pending_announce(local_id, token, remote_jid,
                                              "noise_reduction_kept_pending")
                    return
            finally:
                # The model is no longer in use past the reduction: the
                # reservation ends here, before staging/handoff.
                if reserved:
                    controller.release_offline(local_id)
            tmp_name = None
            try:
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tmp_name = tmp.name
                tmp.close()
                with wave.open(tmp_name, "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(2)
                    wf.setframerate(rate)
                    wf.writeframes(data)
            except Exception as exc:
                logging.error("[noise_reduction] could not write the processed "
                              "retry: %s", exc)
                # No partial staging artifact left behind (review 18:47,
                # contract 5).
                if tmp_name is not None:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                self._nr_pending_announce(local_id, token, remote_jid,
                                          "noise_reduction_kept_pending")
                return
            self._nr_pending_handoff(msg, remote_jid, tmp_name,
                                     channels, rate, seconds, token)

        threading.Thread(target=_work, daemon=True).start()

    def _stage_pending_and_handoff(self, msg, remote_jid, path, local_id,
                                   token, fail_key):
        """Worker: read the kept/processed WAV (single stable copy,
        fingerprint validated when one was recorded), stage it as a temp
        WAV and hand it to the voice-note pipeline. Shared by the raw
        send of a kept-pending note and the resend of an already
        processed one."""
        def _work():
            try:
                data, rate, channels, seconds = self._read_nr_pending(msg, path)
            except Exception as exc:
                logging.warning("[noise_reduction] pending voice send could "
                                "not read %s: %s", path, exc)
                self._nr_pending_announce(local_id, token, remote_jid,
                                          "resend_not_available")
                return
            tmp_name = None
            try:
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tmp_name = tmp.name
                tmp.close()
                with wave.open(tmp_name, "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(2)
                    wf.setframerate(rate)
                    wf.writeframes(data)
            except Exception as exc:
                logging.error("[noise_reduction] could not stage the pending "
                              "voice send: %s", exc)
                if tmp_name is not None:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                self._nr_pending_announce(local_id, token, remote_jid, fail_key)
                return
            self._nr_pending_handoff(msg, remote_jid, tmp_name,
                                     channels, rate, seconds, token,
                                     fail_key=fail_key)

        threading.Thread(target=_work, daemon=True).start()

    def _send_nr_pending_raw(self, msg, remote_jid, path):
        """Send a fourth-option kept-pending note AS RECORDED — no
        reduction at all. The other half of Fran's 18:35 decision: the
        choice between reprocessing and sending the original is ALWAYS
        the user's, action by action; nothing ever sends the raw audio on
        its own. Same per-note attempt authority, single-stable-read
        fingerprint check, delete-wins and voice-note handoff as the
        reprocessing retry. No model is touched, so the incremental slot
        does not gate this path.
        """
        local_id, token = self._nr_begin_action(msg, remote_jid)
        if token is None:
            return
        self._stage_pending_and_handoff(msg, remote_jid, path, local_id,
                                        token, "noise_reduction_kept_pending")

    def _resend_processed_voice(self, msg, remote_jid, path):
        """"Try sending again" on a note that ALREADY went through the
        pipeline (a handoff product marked _nr_processed_voice): resend
        THOSE bytes as a voice note — ptt, quote, channels — never
        re-reduced and never through the generic attachment path (review
        18:47, contract 4). Same per-note attempt authority and handoff
        as every other fourth-option action."""
        local_id, token = self._nr_begin_action(msg, remote_jid)
        if token is None:
            return
        self._stage_pending_and_handoff(msg, remote_jid, path, local_id,
                                        token, "resend_not_available")

    def _send_voice_message(self, event):
        """Stop recording and enqueue the audio for delivery."""
        if not self._is_recording:
            return
        session = getattr(self, "_system_audio_session", None)
        if session is not None:
            if session.get("transition"):
                return
            if not session.get("stopped"):
                self._finish_system_audio_for_send(event)
                return

        import time as _time
        _t0 = _time.perf_counter()
        logging.info("[VOICE_TIMING] T+0.000s — user clicked send, stopping recording stream")

        # Snapshot the mode before stop/hide clears it; the worker belongs to
        # this recording even when another conversation/recording is opened.
        mixed_audio = bool(getattr(self, "_recording_system_audio", False))
        channel_mode = ("auto" if mixed_audio else
                        getattr(self, "_recording_channel_mode", "auto"))
        # Stop the recording stream in background FIRST so the audio device is fully released
        # without blocking the UI thread before BASS plays the send sound.
        if mixed_audio:
            self._stop_system_audio_recording()
        else:
            # The session gets its own close confirmation: pass it so a later
            # recording replacing self._incremental_session cannot steal it.
            threading.Thread(target=self._stop_recording_stream,
                             args=(getattr(self, "_incremental_session", None),),
                             daemon=True).start()
        self._is_recording     = False
        self._recording_paused = False

        self.main_window.voicemsg_send_sound.play()

        # Notify contacts that recording stopped (runs in its own thread).
        _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if _rec_jid and not _rec_jid.endswith("@newsletter"):
            self.main_window.send_recording_status(_rec_jid, False, _rec_jid.endswith("@g.us"))

        frames = self._recording_frames
        self._recording_frames = []
        inc_session = getattr(self, "_incremental_session", None)
        self._incremental_session = None
        # inc_session's stop/cancel flags are set by the send thread in
        # _write_and_enqueue, after the capture stream actually stopped.

        if not frames:
            if inc_session is not None:
                # Nothing to send: cancel cooperatively (non-blocking here);
                # the drain aborts the worker and deletes the spool itself.
                inc_session["cancel"] = True
                controller = getattr(self.main_window, "_incremental_controller", None)
                if controller is not None:
                    controller.discard(inc_session.get("local_id", ""))
            self._hide_voice_panel()
            self.message_field.SetFocus()
            return

        # ── Phase 2: instant UI update ────────────────────────────────────────
        remote_jid      = self.conversation.get("remoteJid", "")
        # The incremental session's id was generated at recording start so the
        # controller could key it; reuse it as the message's local id.
        local_id        = (inc_session["local_id"] if inc_session is not None
                           else str(uuid.uuid4()))
        actual_rate     = self._recording_actual_rate
        actual_ch       = self._recording_actual_ch
        bytes_per_frame = 2 * actual_ch
        quoted_msg      = self._quoted_message
        output_ch       = recording_output_channels(actual_ch, channel_mode)
        stereo_out      = not mixed_audio and encode_as_stereo(output_ch)
        # Microphone + computer audio goes out as an audio message
        # (Ctrl+Shift+H's AAC-LC M4A through /send-file).
        as_audio_file   = mixed_audio

        # Duration from frame byte counts — no allocation, no join on UI thread.
        total_bytes  = sum(len(f) for f in frames)
        duration_sec = int(total_bytes / bytes_per_frame / actual_rate)

        virtual_msg = {
            "_local_pending": True,
            "_local_id":      local_id,
            # Distinguishes a recorded voice message from an audio file sent
            # via the attachment picker (both are messageType "audioMessage")
            # — _mark_message_sent() needs this to know whether the sent
            # sound was already played over in _on_message_sent() (recorded
            # voice, at API-confirmation time — see that method's comment)
            # or still needs to play here.
            "_is_voice_recording": True,
            "key": {
                "id":        local_id,
                "fromMe":    True,
                "remoteJid": remote_jid,
            },
            "messageType": "audioMessage",
            "message": {
                "audioMessage": {
                    "seconds": duration_sec,
                    # Microphone + computer audio goes out as an audio message
                    # rather than a voice message — say so already. A stereo
                    # voice message is M4A too (core/voice_stereo.py).
                    "ptt":     not as_audio_file,
                    **({"mimetype": "audio/mp4", "fileName": f"{local_id}.m4a"}
                       if as_audio_file or stereo_out else {}),
                }
            },
            "messageTimestamp": pending_send_timestamp(
                time.time(), self._sorted_messages, quoted_msg),
            "pushName":         "",
        }
        if quoted_msg:
            _qk = quoted_msg.get("key", {})
            virtual_msg["contextInfo"] = {
                "stanzaId":      _qk.get("id", ""),
                "participant":   _qk.get("participant", ""),
                "quotedMessage": quoted_msg.get("message") or {},
                "_quotedFromMe": bool(_qk.get("fromMe", False)),
            }
        if inc_session is not None:
            # Held by the incremental machinery until the send thread hands it
            # to the MessageQueue (transfer). While set, the echo matcher in
            # main_window/message_events.py skips this record so it cannot
            # claim another audio's echo — the pre-enqueue window is normally
            # short, but a natively blocked drain stretches it indefinitely.
            virtual_msg["_incremental_preparing"] = True

        self._clear_empty_placeholder()
        self._sorted_messages.append(virtual_msg)
        append_message_row(self, self._render_message_line(virtual_msg))
        last = message_row_count(self) - 1   # the row just sent, not the typing row
        if last >= 0:
            self.messages_list.EnsureVisible(last)

        self._register_virtual_msg(virtual_msg)
        self.main_window._schedule_set_chats()
        self._on_cancel_reply()
        self._hide_voice_panel()
        self.message_field.SetFocus()

        # ── Phase 3: heavy work off UI thread ─────────────────────────────────
        # • Join PCM frames
        # • Encode OGG Opus directly from PCM (no WAV roundtrip for encoding)
        # • Write WAV backup for .msv / retry
        # • Encrypt + save .msv local copy
        # • Enqueue with ogg_bytes already ready → worker only needs to POST
        mw      = self.main_window
        enc_key = mw.key

        # Recordings reach the send queue in recording order, whichever
        # finishes processing first (core/ordered_dispatch.py).
        ticket = voice_dispatcher.reserve(remote_jid)

        def _process():
            import time as _time
            _tw0 = _time.perf_counter()
            logging.info("[VOICE_TIMING] T+%.3fs — voice processing thread started",
                         _tw0 - _t0)

            # 1. Noise reduction. If the incremental session started at
            # record time stayed healthy, its spool file already holds the
            # cleaned audio with the equivalent channel handling, and
            # finish() is the only model work left in this thread. Otherwise
            # the raw frames are joined and converted through the existing
            # offline path unchanged.
            incremental_cleaned = False
            audio_data = None
            controller = getattr(mw, "_incremental_controller", None)

            def _forget_incremental():
                # Terminal cleanup for a send that ends without transfer:
                # drop the controller's send-authority entry (and free the
                # slot if it is somehow still held by this id).
                if controller is not None and inc_session is not None:
                    controller.forget(local_id)
            if inc_session is not None:
                # A basic-only session (slot was busy at record time) has
                # no worker: only the confirmed-close stage applies.
                use_incremental = (
                    not mixed_audio
                    and not inc_session.get("basic_only", False)
                    and mw.settings.get("general", {}).get("noise_reduction_enabled", False)
                    # The preference can change between recording and
                    # sending; anything but neural-while-recording goes
                    # through the published offline path below.
                    and voice_nr_engine(mw.settings) == "neural"
                    and voice_nr_moment(mw.settings) == "record"
                )
                drain_thread = inc_session.get("thread")

                def _wait_or_revoked(is_done, timeout=None):
                    """Slice-wait until is_done(), honoring a user cancel of
                    the pending row (controller revoke) even while a native
                    call (device close, model load, feed, finish) sits
                    blocked underneath. Native blocks cannot be preempted:
                    a "timeout" result means the send FALLS BACK to the
                    basic gate on the complete raw audio and the late
                    worker result is discarded — it can never kill the
                    thread. Device close (stage 1) has NO timeout: neither
                    cleaned nor basic audio may be built from a frames
                    list a callback can still append to. Returns "done" /
                    "revoked" / "timeout"."""
                    waited = 0.0
                    while not is_done():
                        if inc_session.get("revoked"):
                            return "revoked"
                        time.sleep(0.1)
                        waited += 0.1
                        if timeout is not None and waited >= timeout:
                            return "timeout"
                    return "done"

                # Stage 1 — confirmed device close BEFORE anything else: the
                # frames list is only stable once no capture callback can
                # append. There is no timeout that turns an unconfirmed close
                # into a send: a snapshot of a list that may still be growing
                # is not the recording, so this becomes the occupied path.
                stopped_ev = inc_session.get("stopped_event")
                if stopped_ev is not None:
                    if _wait_or_revoked(stopped_ev.is_set) == "revoked":
                        logging.info("[noise_reduction] send revoked while waiting "
                                     "for the device close")
                        _forget_incremental()
                        return
                    if inc_session.get("device_failed"):
                        # Close NOT confirmed: capture state unknown, frames
                        # may still grow, so nothing is built from them. The
                        # drain is cancelled (it cleans its own spool and
                        # frees the slot) and the note FAILS audibly: the
                        # ticket is released by the return below, so later
                        # notes of this chat are never held behind it.
                        logging.error("[noise_reduction] capture device close was not "
                                      "confirmed; failing the note")
                        inc_session["cancel"] = True
                        _forget_incremental()
                        wx.CallAfter(mw._on_message_failed, local_id,
                                     mw.i18n.t("voice_recording_device_failed"), True)
                        return
                # Stage 2 — ask the drain for its end state: stop → it
                # finishes the audio it has and hands worker+spool to this
                # send; cancel → it aborts and cleans up itself. The cancel
                # case needs NO wait: the raw frames are already stable
                # after the confirmed device close above, and the drain
                # self-cleans on the way out.
                inc_session["stop" if use_incremental else "cancel"] = True
                if (not use_incremental and not inc_session.get("basic_only", False)
                        and (drain_thread is not None
                             or inc_session.get("worker") is not None)):
                    # record→send preference switch with a native session
                    # that may STILL hold its private model: the cancelled
                    # drain cleans itself asynchronously, so the offline
                    # pass below must never start while that model can be
                    # loaded (review 18:47, contract 1 — the exclusion is
                    # effective in both directions, not a boolean probe).
                    # Wait — bounded and revoke-aware — for the drain's
                    # exit, then reclaim an uncleaned remnant exactly once.
                    # A drain that will not die keeps its model: mark the
                    # session occupied so the note takes the published
                    # gate / stays pending, never a second ONNX.
                    if drain_thread is not None and drain_thread.is_alive():
                        outcome = _wait_or_revoked(
                            lambda: not drain_thread.is_alive(),
                            timeout=_INCREMENTAL_SEND_FINAL_WAIT)
                        if outcome == "revoked":
                            logging.info("[noise_reduction] send revoked while "
                                         "waiting for the cancelled drain")
                            _forget_incremental()
                            return
                        if outcome == "timeout":
                            logging.info("[noise_reduction] cancelled drain "
                                         "still busy; offline pass blocked "
                                         "to avoid a second ONNX")
                            inc_session["model_unavailable"] = True

                            def _reap_switched_drain(session=inc_session,
                                                     thread=drain_thread):
                                thread.join()
                                if session.get("drain_cleaned"):
                                    return
                                late_worker = session.get("worker")
                                if late_worker is not None:
                                    try:
                                        late_worker.abort()
                                    except Exception:
                                        pass
                                    session["worker"] = None
                                    try:
                                        os.unlink(session["spool"])
                                    except OSError:
                                        pass
                                    if controller is not None:
                                        controller.release_slot(local_id)

                            threading.Thread(target=_reap_switched_drain,
                                             daemon=True).start()
                    if (not inc_session.get("model_unavailable")
                            and (drain_thread is None
                                 or not drain_thread.is_alive())
                            and not inc_session.get("drain_cleaned")):
                        # The drain is dead (or was never snapshotted) but
                        # left worker/spool/slot behind (normal stop-path
                        # exit that raced past the cancel): reclaim the
                        # remnant here, exactly once, before any offline
                        # pass loads the singleton.
                        late_worker = inc_session.get("worker")
                        if late_worker is not None:
                            try:
                                late_worker.abort()
                            except Exception:
                                pass
                            inc_session["worker"] = None
                            try:
                                os.unlink(inc_session["spool"])
                            except OSError:
                                pass
                            if controller is not None:
                                controller.release_slot(local_id)
                worker = inc_session.get("worker")
                if (use_incremental and worker is None
                        and not inc_session.get("failed")
                        and not inc_session.get("basic_only", False)
                        and drain_thread is not None):
                    # A drain EXISTS but there is no usable worker snapshot.
                    # Policy: prefer the existing basic gate — never spin up
                    # a SECOND ONNX instance through the offline path for
                    # this note — whichever sub-state the drain is in.
                    # (No drain thread at all means no incremental work was
                    # ever started for this note: the published offline
                    # path below is unchanged.)
                    logging.info("[noise_reduction] no incremental worker ready at send; "
                                 "falling back to the basic gate with the full raw audio")
                    inc_session["fallback_chosen"] = True
                    if drain_thread is not None and drain_thread.is_alive():
                        # Model load still in progress: cancel so the late
                        # constructor's drain aborts and self-cleans
                        # (spool + slot) when it returns. But the drain's
                        # last flag check may already have read False and
                        # it can exit through the normal stop path WITHOUT
                        # re-reading cancel — keeping worker/spool/slot for
                        # a send that went basic. A reaper joins the drain
                        # OFF the send path and claims whatever the drain
                        # did not clean (flag set only where cleanup
                        # actually ran), exactly once: never aborting while
                        # native code runs (the join completes first),
                        # never making the send wait, never a second ONNX.
                        inc_session["cancel"] = True

                        def _reap_late_drain(session=inc_session,
                                             thread=drain_thread):
                            thread.join()
                            if session.get("drain_cleaned"):
                                return
                            late_worker = session.get("worker")
                            if late_worker is not None:
                                try:
                                    late_worker.abort()
                                except Exception:
                                    pass
                                session["worker"] = None
                                try:
                                    os.unlink(session["spool"])
                                except OSError:
                                    pass
                                if controller is not None:
                                    controller.release_slot(local_id)

                        threading.Thread(target=_reap_late_drain,
                                         daemon=True).start()
                    elif not inc_session.get("drain_cleaned"):
                        # The drain EXITED between the worker snapshot and
                        # the aliveness check. Cleanup authority keys on
                        # what the drain ACTUALLY did — drain_cleaned is
                        # written only where its cleanup really ran — never
                        # on the cancel flag: a cancel/revoke can land
                        # between the snapshot and this check while the
                        # drain already left through its normal stop path
                        # without re-reading it, keeping worker+spool+slot
                        # for a send that holds no worker reference (a
                        # drain that OBSERVED the cancel cleaned itself
                        # and set the flag, so this branch then skips; the
                        # alive branch above is mutually exclusive by
                        # snapshot). The drain is dead, so the session
                        # fields are stable — clean the remnant here,
                        # exactly once.
                        late_worker = inc_session.get("worker")
                        if late_worker is not None:
                            try:
                                late_worker.abort()
                            except Exception:
                                pass
                            inc_session["worker"] = None
                            try:
                                os.unlink(inc_session["spool"])
                            except OSError:
                                pass
                            if controller is not None:
                                controller.release_slot(local_id)
                if (use_incremental and worker is not None
                        and not inc_session.get("failed")):
                    # Stage 3 — finish() runs the remaining model hops and
                    # can block natively just like load/feed: run it in a
                    # helper that first joins the drain, under ONE
                    # revocation-aware bounded wait for the whole final
                    # completion (see _INCREMENTAL_SEND_FINAL_WAIT) — never
                    # inline here, and never unbounded.
                    #
                    # The helper is created for EVERY session that reached
                    # the drain stop holding its own published worker —
                    # even one already cancelled (user revoke / shutdown /
                    # discard between the stop and this gate): a cancelled
                    # session is never consumed, but the helper is also the
                    # FINALIZER that joins the drain and reclaims a remnant
                    # the drain left uncleaned (its normal stop exit may
                    # have raced past the cancel without re-reading it).
                    # Skipping the helper on cancel leaked worker/spool/
                    # slot: no reaper exists for a session holding its own
                    # worker (Milo 17:50). A FAILED drain owns its cleanup
                    # (drain_cleaned), so it is the one case excluded here.
                    #
                    # Ownership of a late result is indivisible: the
                    # finisher's cleanup decision and this thread's
                    # abandon/consume decision are serialized under
                    # finish_lock, so exactly one party cleans up — a
                    # finish() returning between the wait's timeout and the
                    # abandonment mark can no longer slip through with
                    # nobody aborting the worker, deleting the spool and
                    # releasing the slot.
                    finish_out = {}
                    finish_lock = threading.Lock()

                    def _finish_incremental():
                        superseded = False
                        try:
                            if drain_thread is not None:
                                drain_thread.join()
                            # Re-validate AFTER the join, not the pre-join
                            # state: never call finish() on a worker the
                            # drain may have aborted. A FAILED drain owns
                            # the cleanup; a CANCELLED one may NOT have
                            # cleaned (normal stop-path exit without
                            # re-reading the flag) — the finally below
                            # reclaims that remnant via drain_cleaned.
                            if (inc_session.get("failed")
                                    or inc_session.get("cancel")):
                                superseded = True
                            elif not (inc_session.get("revoked")
                                      or inc_session.get("fallback_chosen")):
                                finish_out["result"] = worker.finish()
                        except BaseException as exc:
                            finish_out["error"] = exc
                        finally:
                            finish_out["superseded"] = superseded
                            with finish_lock:
                                finish_out["ready"] = True
                                # Exactly one claimant ever cleans up, and
                                # the claim is taken under the same lock
                                # that publishes readiness, so the revoked
                                # branch of the send thread can never clean
                                # it a second time. Authority keys on what
                                # the drain ACTUALLY did — drain_cleaned
                                # is written only where its cleanup really
                                # ran — never on session flags alone: a
                                # drain can exit through its normal stop
                                # path without re-reading a cancel/revoke
                                # that landed under the join, keeping
                                # worker+spool+slot for a send that no
                                # longer wants them. Whenever this helper
                                # will NOT process the worker (superseded,
                                # revoked or fallback) and the drain left
                                # the remnant uncleaned, claim it here.
                                # (A drain that failed or observed the
                                # cancel cleaned itself and set the flag:
                                # this then skips.)
                                abandoned = (not finish_out.get("claimed")
                                             and not inc_session.get("drain_cleaned")
                                             and (superseded
                                                  or inc_session.get("revoked")
                                                  or inc_session.get("fallback_chosen")))
                                if abandoned:
                                    finish_out["claimed"] = True
                            if abandoned:
                                # The helper owns the cleanup when the send
                                # thread walked away (revoked, or the
                                # bounded wait chose the basic fallback) or
                                # when the worker was superseded under the
                                # join but the drain left it uncleaned: a
                                # late result is deleted, the worker
                                # aborted and the slot released WHEN the
                                # native call actually returns — never left
                                # dangling.
                                try:
                                    worker.abort()
                                except Exception:
                                    pass
                                paths = {inc_session.get("spool")}
                                result = finish_out.get("result")
                                if result:
                                    paths.add(result[0])
                                for spool_path in paths:
                                    if not spool_path:
                                        continue
                                    try:
                                        os.unlink(spool_path)
                                    except OSError:
                                        pass
                                if controller is not None:
                                    controller.release_slot(local_id)

                    def _claim_completed_result():
                        """Arbitrate a cancel/revoke against a finisher that
                        may have ALREADY published a completed result: its
                        post-join flag check ran before the cancel landed,
                        so it exited expecting this thread to consume the
                        result — and nobody else will clean it. The claim is
                        taken under the same lock that publishes readiness,
                        so exactly one party ever cleans up: a finished,
                        unclaimed result is claimed and cleaned HERE; a
                        finisher still in flight observes the cancel after
                        its join (superseded) and reclaims an uncleaned
                        remnant itself via drain_cleaned/claimed."""
                        with finish_lock:
                            finished = (finish_out.get("ready", False)
                                        and not finish_out.get("superseded")
                                        and not finish_out.get("claimed"))
                            if finished:
                                finish_out["claimed"] = True
                        if not finished:
                            return
                        try:
                            worker.abort()
                        except Exception:
                            pass
                        paths = {inc_session.get("spool")}
                        result = finish_out.get("result")
                        if result:
                            paths.add(result[0])
                        for spool_path in paths:
                            if not spool_path:
                                continue
                            try:
                                os.unlink(spool_path)
                            except OSError:
                                pass
                        if controller is not None:
                            controller.release_slot(local_id)

                    finisher = threading.Thread(target=_finish_incremental, daemon=True)
                    finisher.start()
                    if inc_session["cancel"]:
                        # A cancel landed before this gate (user revoke,
                        # shutdown, or a panel-level discard): the note is
                        # never consumed or enqueued — transfer() would
                        # refuse it anyway — so there is nothing to wait
                        # for. But the send can NOT just walk away: the
                        # finisher may already hold a completed result.
                        # Same arbitration as the stage-3 revoked branch
                        # below; a finisher still in flight owns the
                        # remnant after its join (never aborting
                        # native-active code — the join completes first).
                        logging.info("[noise_reduction] send cancelled before finish(); "
                                     "arbitrating the incremental remnant")
                        _claim_completed_result()
                        _forget_incremental()
                        return
                    stage3 = _wait_or_revoked(lambda: finish_out.get("ready", False),
                                              timeout=_INCREMENTAL_SEND_FINAL_WAIT)
                    if stage3 == "revoked":
                        # finish() may have completed BEFORE the revoke
                        # landed but after its own flag check: nobody
                        # cleaned up — same arbitration as the pre-gate
                        # cancel branch above, exactly once.
                        logging.info("[noise_reduction] send revoked during finish()")
                        _claim_completed_result()
                        _forget_incremental()
                        return
                    if stage3 == "timeout":
                        with finish_lock:
                            if finish_out.get("ready", False):
                                # Finished between the wait's last check
                                # and this claim: consume it normally.
                                stage3 = "done"
                            else:
                                # Policy: prefer the basic gate on the
                                # complete raw over a longer wait; the
                                # finisher cleans up on return. cancel too:
                                # a drain that CAN observe flags between
                                # chunks aborts and self-cleans now instead
                                # of finishing work nobody will consume.
                                inc_session["fallback_chosen"] = True
                                inc_session["cancel"] = True
                        if stage3 == "timeout":
                            logging.warning("[noise_reduction] incremental finish still busy; "
                                            "falling back to the basic gate with the full raw audio")
                    if not inc_session.get('fallback_chosen'):
                        try:
                            _t_inc = _time.perf_counter()
                            with finish_lock:
                                already_claimed = finish_out.get("claimed")
                            if already_claimed:
                                # The wait observed ready, but the revoke
                                # landed before the finisher's flag check:
                                # the finisher already discarded the late
                                # result under the same lock. Nothing left
                                # to consume; the message is cancelled.
                                _forget_incremental()
                                return
                            if finish_out.get("superseded"):
                                # The drain failed or was cancelled under
                                # the join and owns worker/spool/slot:
                                # basic gate on the raw audio; nobody else
                                # touches them.
                                audio_data = None
                            else:
                                if "error" in finish_out:
                                    raise finish_out["error"]
                                spool_path, engine = finish_out["result"]
                                try:
                                    with open(spool_path, "rb") as spool_file:
                                        cleaned_pcm = spool_file.read()
                                finally:
                                    os.unlink(spool_path)
                                if inc_session["worker_ch"] == 1 and actual_ch == 2:
                                    # Explicit mono: averaged before the model per chunk.
                                    audio_data, channels = cleaned_pcm, 1
                                elif actual_ch == 2 and channel_mode == "auto":
                                    # Same empty-side decision as the offline path, on
                                    # the same raw audio; the kept side comes from the
                                    # cleaned spool (channels are model-independent).
                                    import numpy as np
                                    raw_joined = b"".join(frames)
                                    dropped_pcm, dropped_ch = drop_empty_side(raw_joined, 2)
                                    if dropped_ch == 2:
                                        audio_data, channels = cleaned_pcm, 2
                                    else:
                                        _raw = np.frombuffer(raw_joined, dtype="<i2")
                                        if _raw[0::2].tobytes() == dropped_pcm:
                                            _side = 0
                                        elif _raw[1::2].tobytes() == dropped_pcm:
                                            _side = 1
                                        else:
                                            # Never guess the side: fall back to the
                                            # offline path rather than send the wrong one.
                                            raise RuntimeError("empty-side selection did not match either channel")
                                        audio_data = np.ascontiguousarray(
                                            np.frombuffer(cleaned_pcm, dtype="<i2").reshape(-1, 2)[:, _side]
                                        ).tobytes()
                                        channels = 1
                                else:
                                    audio_data, channels = cleaned_pcm, actual_ch
                                incremental_cleaned = True
                                if controller is not None:
                                    # Worker finished, spool consumed and deleted:
                                    # the slot is free. The send-authority entry
                                    # stays until transfer (or a terminal failure).
                                    controller.release_slot(local_id)
                                logging.info("[VOICE_TIMING] T+%.3fs — incremental noise reduction done "
                                             "(engine: %s; finish() residual %.3fs)",
                                             _time.perf_counter() - _t0, engine, _time.perf_counter() - _t_inc)
                        except Exception:
                            logging.exception("[VOICE_TIMING] incremental noise reduction failed; "
                                              "falling back to the offline path")
                            audio_data = None
                            # finish() may have died with the spool still open or
                            # half-written: close the handle, then delete the file.
                            try:
                                worker.abort()
                            except Exception:
                                pass
                            try:
                                os.unlink(inc_session["spool"])
                            except OSError:
                                pass
                            if controller is not None:
                                controller.release_slot(local_id)
            if audio_data is None:
                # Join raw PCM frames.
                audio_data = b"".join(frames)
                logging.info("[VOICE_TIMING] T+%.3fs — PCM frames joined (%d bytes, %d frames)",
                             _time.perf_counter() - _t0, len(audio_data), len(frames))

                # Convert before processing/backup so preview, retries, the
                # queued bytes and local playback all have the chosen channels.
                try:
                    audio_data, _ = prepare_voice_pcm(audio_data, actual_ch, channel_mode)
                except Exception:
                    logging.exception("[voice] Could not prepare recording channels")
                    _forget_incremental()
                    wx.CallAfter(mw._on_message_failed, local_id,
                                 mw.i18n.t("media_audio_convert_failed"), True)
                    return

                # In Automatic, two channels opened says nothing about what is
                # plugged into them (core/voice_stereo.py, #679). Too heavy for the
                # UI thread, so the row that already said M4A is corrected here.
                channels = output_ch
                if stereo_out and channel_mode == "auto":
                    audio_data, channels = drop_empty_side(audio_data, output_ch)
            send_stereo = stereo_out and channels >= 2
            if stereo_out and not send_stereo:
                logging.info("[audio] One side of the recording is empty — sending it in mono.")
                row = virtual_msg["message"]["audioMessage"]
                row.pop("mimetype", None)
                row.pop("fileName", None)

            # Apply microphone noise reduction if enabled in settings
            # (skipped when the incremental session already cleaned the audio)
            if (not incremental_cleaned and not mixed_audio
                    and mw.settings.get("general", {}).get("noise_reduction_enabled", False)):
                # Chosen-basic and temporarily-impossible-neural are NOT
                # the same state (review 18:28, contract 1): the gate is
                # the published fallback for both, but keep_pending only
                # exists for a user who chose NEURAL — a basic engine chosen
                # on purpose sends gated exactly as published.
                chosen_basic = voice_nr_engine(mw.settings) == "basic"
                session_blocked = inc_session is not None and (
                    inc_session.get("basic_only", False)
                    or inc_session.get("model_unavailable", False)
                    or inc_session.get("fallback_chosen", False))
                basic_only = chosen_basic or session_blocked
                from core import noise_model
                try:
                    _model_installed = noise_model.is_installed()
                except Exception:
                    _model_installed = False
                on_failure = effective_nr_on_failure(mw.settings, _model_installed)
                want_pending = on_failure == "keep_pending" and not chosen_basic
                # No second ONNX next to a live native session: with
                # keep_pending, when the incremental side could not clean
                # this note and its model may still be loaded (load failed,
                # finish timed out, or the slot busy at record is STILL
                # busy), the send does NOT run the offline reduction — the
                # raw note goes straight to pending. A slot freed meanwhile
                # makes the posterior pass possible; if even that is not
                # neural, the note stays pending.
                skip_posterior = want_pending and inc_session is not None and (
                    inc_session.get("model_unavailable", False)
                    or inc_session.get("fallback_chosen", False)
                    or (inc_session.get("basic_only", False)
                        and controller is not None and controller.slot_busy()))
                raw_unprocessed = audio_data if want_pending else None
                reduce_basic_only = chosen_basic or (session_blocked and not want_pending)
                offline_reserved = False
                if (inc_session is not None and controller is not None
                        and inc_session.get("worker") is None
                        and drain_thread is None
                        and not inc_session.get("basic_only", False)
                        and not inc_session.get("model_unavailable", False)
                        and not inc_session.get("fallback_chosen", False)):
                    # moment=send from the start: this session NEVER started
                    # incremental work, so no private model can be loaded and
                    # the slot it holds is only send-side authority. The
                    # caller confirms the cleanup HERE and releases its own
                    # slot, so the reservation below stays a pure exclusion
                    # (review 19:07: caller + confirmed cleanup, NEVER ID
                    # equality alone — a retained slot after a native timeout
                    # means the private model may still be alive).
                    controller.release_slot(local_id)
                if not skip_posterior and not reduce_basic_only \
                        and controller is not None:
                    # The NORMAL offline pass takes the same reservation as
                    # the retry (review 18:59, residual 1): while it loads /
                    # runs the singleton, no capture starts a private model
                    # next to it (acquire_slot refuses), and a capture or
                    # another offline pass already holding the exclusion
                    # makes THIS note take the occupied policy — published
                    # gate, or pending when the user chose keep_pending.
                    if controller.try_reserve_offline(local_id):
                        offline_reserved = True
                    elif want_pending:
                        skip_posterior = True
                    else:
                        reduce_basic_only = True
                if skip_posterior:
                    _nr_engine = None
                    logging.info("[noise_reduction] exclusion busy (native "
                                 "session may still hold its model or another "
                                 "note owns the slot); no second ONNX pass")
                else:
                    try:
                        audio_data, _nr_engine = self._reduce_for_send(
                            mw, audio_data, actual_rate, channels,
                            reduce_basic_only,
                            on_failure=on_failure)
                    finally:
                        if offline_reserved:
                            controller.release_offline(local_id)
                logging.info("[VOICE_TIMING] Noise reduction finished (engine: %s)", _nr_engine)
                if raw_unprocessed is not None and _nr_engine != "neural":
                    # Fourth option: BOTH neural processings failed (the
                    # incremental one did not clean this note and the
                    # at-send one failed or was impossible without a second
                    # ONNX). Keep the note pending with its ORIGINAL audio
                    # for a manual "Try sending again" — never gated, never
                    # unprocessed, never automatic. Revoke arbitration
                    # (contract 5): a delete of the pending row during the
                    # processing or the write stops everything — no orphan
                    # WAV, no late announcement on a dead row.
                    if inc_session is not None and inc_session.get("revoked"):
                        logging.info("[noise_reduction] send revoked before "
                                     "the keep-pending write")
                        _forget_incremental()
                        return
                    logging.info("[noise_reduction] neural reduction failed; "
                                 "keeping the note pending for a manual resend")
                    pending_path = None
                    try:
                        os.makedirs(data_path("voice_messages"), exist_ok=True)
                        pending_path = os.path.join(
                            data_path("voice_messages"), local_id + ".resend.wav")
                        with wave.open(pending_path, "wb") as wf:
                            wf.setnchannels(channels)
                            wf.setsampwidth(2)
                            wf.setframerate(actual_rate)
                            wf.writeframes(raw_unprocessed)
                    except Exception as exc:
                        logging.error("[_send_voice_message] could not keep the "
                                      "pending WAV: %s", exc)
                        # Never leave a partial keep behind (review 18:47,
                        # contract 5): the row falls to the save-failed
                        # state with NO resend artifact at all.
                        if pending_path is not None:
                            try:
                                os.unlink(pending_path)
                            except OSError:
                                pass
                            pending_path = None
                    if inc_session is not None and inc_session.get("revoked"):
                        # Deleted while the file was being written: it dies
                        # with its row and nothing is announced.
                        if pending_path is not None:
                            try:
                                os.unlink(pending_path)
                            except OSError:
                                pass
                        _forget_incremental()
                        return
                    if pending_path is not None:
                        virtual_msg[RESEND_PATH_KEY] = pending_path
                        # Marks this row for the media-resend hook in
                        # message_actions: "Try sending again" must RETRY
                        # THE PROCESSING (_retry_nr_pending), not resend the
                        # original audio unprocessed.
                        virtual_msg["_nr_pending_retry"] = True
                        # Fingerprint of what was kept: when it can be
                        # recorded, the retry validates these exact bytes
                        # (single stable read) before processing. An
                        # unreadable file at keep time means NO fingerprint
                        # — the check is then skipped, not claimed
                        # mandatory (review 18:47).
                        try:
                            with open(pending_path, "rb") as fh:
                                virtual_msg["_nr_pending_sha256"] = (
                                    hashlib.sha256(fh.read()).hexdigest())
                            virtual_msg["_nr_pending_size"] = (
                                os.path.getsize(pending_path))
                        except OSError:
                            pass
                    virtual_msg.pop("_incremental_preparing", None)
                    if pending_path is not None:
                        announce_key = "noise_reduction_kept_pending"
                    else:
                        # The note stays FAILED but nothing was kept: never
                        # announce a retry that cannot happen.
                        announce_key = "noise_reduction_pending_save_failed"

                    def _mark_kept_pending(key=announce_key):
                        # UI thread — the same one deletions run on. The
                        # controller's send authority is released HERE, not
                        # before, so a revoke() racing this callback still
                        # wins; a row gone from its own chat's records takes
                        # the kept file with it and announces nothing.
                        try:
                            self._nr_finish_kept_pending(
                                local_id, remote_jid, virtual_msg, key)
                        finally:
                            _forget_incremental()

                    wx.CallAfter(_mark_kept_pending)
                    return

            # 2. Write WAV temp file (used for ffmpeg conversion, backup, and retry fallback).
            tmp = None
            try:
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tmp.close()
                with wave.open(tmp.name, "wb") as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(2)   # 16-bit PCM
                    wf.setframerate(actual_rate)
                    wf.writeframes(audio_data)
                wav_path = tmp.name
                logging.info("[VOICE_TIMING] T+%.3fs — WAV written to %s",
                             _time.perf_counter() - _t0, wav_path)
            except Exception as exc:
                logging.error("[_send_voice_message] failed to write WAV: %s", exc)
                if as_audio_file or send_stereo:
                    if tmp is not None:
                        try:
                            os.unlink(tmp.name)
                        except OSError:
                            pass
                    wx.CallAfter(mw._on_message_failed, local_id,
                                 mw.i18n.t("media_audio_convert_failed"), True)
                _forget_incremental()
                return

            if as_audio_file:
                _forget_incremental()
                return lambda: self._enqueue_system_audio_file(
                    wav_path, local_id, remote_jid, quoted_msg, enc_key, virtual_msg,
                )

            # 3. Encode: stereo as AAC M4A (core/voice_stereo.py), never quietly
            # downmixed when that fails; mono as OGG Opus via ffmpeg conversion.
            voice_mimetype = ""
            ogg_bytes = None
            if send_stereo:
                ogg_bytes = self._encode_stereo_voice(wav_path)
                if not ogg_bytes:
                    try:
                        os.unlink(wav_path)
                    except OSError:
                        pass
                    _forget_incremental()
                    wx.CallAfter(mw._on_message_failed, local_id,
                                 mw.i18n.t("media_audio_convert_failed"), True)
                    return
                voice_mimetype = STEREO_MIMETYPE
            _t_enc = _time.perf_counter()
            try:
                ogg_path = None if send_stereo else mw._convert_wav_to_ogg(wav_path)
                if ogg_path and os.path.isfile(ogg_path):
                    with open(ogg_path, "rb") as f_in:
                        ogg_bytes = f_in.read()
                    try:
                        os.unlink(ogg_path)
                    except Exception:
                        pass
            except Exception as exc:
                logging.warning("[_send_voice_message] OGG pre-encode failed: %s", exc)
            logging.info("[VOICE_TIMING] T+%.3fs — OGG encode done in %.3fs (%s bytes)",
                         _time.perf_counter() - _t0,
                         _time.perf_counter() - _t_enc,
                         len(ogg_bytes) if ogg_bytes else 0)

            # 4. Encrypt OGG (or raw PCM fallback) and save as .msv for offline playback.
            _t_msv = _time.perf_counter()
            try:
                voice_messages_dir = data_path("voice_messages")
                os.makedirs(voice_messages_dir, exist_ok=True)
                local_audio_path = os.path.join(voice_messages_dir, f"{local_id}.msv")
                with open(local_audio_path, "wb") as f_out:
                    f_out.write(encrypt(ogg_bytes or audio_data, enc_key))
                logging.info("[VOICE_TIMING] T+%.3fs — .msv saved in %.3fs",
                             _time.perf_counter() - _t0,
                             _time.perf_counter() - _t_msv)
            except Exception as exc:
                logging.warning("[_send_voice_message] failed to save local audio copy: %s", exc)

            # 5. Enqueue — ogg_bytes pre-encoded so worker skips encoding, just POSTs.
            logging.info("[VOICE_TIMING] T+%.3fs — calling enqueue (ogg_bytes=%s)",
                         _time.perf_counter() - _t0,
                         "yes" if ogg_bytes else "NO — will fallback to WAV")
            pm = PendingMessage(local_id, remote_jid, audio_path=wav_path,
                                ogg_bytes=ogg_bytes, quoted=quoted_msg,
                                voice_mimetype=voice_mimetype)
            def _enqueue():
                if inc_session is not None and controller is not None:
                    def _hand_off():
                        mw.message_queue.enqueue(pm)
                        # Cleared HERE — under the controller's lock, right
                        # after the queue accepted the message, on the same
                        # thread: the echo can only exist after the queue's
                        # worker actually sends, which is after this point, and
                        # the echo matcher additionally re-checks the
                        # controller's PREPARING state, so no window leaves the
                        # record skipping its own echo. (The reverse order —
                        # clearing before enqueue — would let the record steal
                        # ANOTHER pending audio's echo in between.)
                        virtual_msg.pop("_incremental_preparing", None)

                    # The hand-off runs UNDER the controller's lock: a user
                    # delete of the pending row cannot slip between the
                    # authority check and the real enqueue, so after transfer()
                    # succeeds the queue's own cancel machinery gives a
                    # definitive answer.
                    if not controller.transfer(local_id, _hand_off):
                        # Revoke won: the row is already gone — send nothing.
                        # The slot/spool were already released above.
                        logging.info("[noise_reduction] send revoked before transfer; not enqueuing")
                        controller.forget(local_id)
                        try:
                            os.unlink(wav_path)
                        except OSError:
                            pass
                        # The .msv copy may have been written moments ago, after
                        # the user's delete already discarded the cache.
                        from ui.conversation_panel.media_paths import discard_local_media_cache
                        discard_local_media_cache(
                            data_path("voice_messages"), data_path("media"), local_id)
                        return
                else:
                    mw.message_queue.enqueue(pm)
                mw.mark_conversation_as_read(remote_jid)
            return _enqueue

        def _write_and_enqueue():
            action = None
            try:
                action = _process()
            except Exception:
                logging.exception("[voice] Preparing the recording failed")
            finally:
                voice_dispatcher.complete(remote_jid, ticket, action)

        # A worker that waits on an earlier recording holds nothing but its own
        # ticket; later ones are held back by design (order over latency).
        try:
            threading.Thread(target=_write_and_enqueue, daemon=True).start()
        except Exception:
            # No worker will ever complete this ticket: free it.
            voice_dispatcher.complete(remote_jid, ticket, None)
            raise

    def _encode_stereo_voice(self, wav_path):
        """The M4A bytes of a stereo voice message, or None if it could not be
        encoded."""
        from core.audio_transcode import encode_system_audio_to_m4a
        m4a_path = encode_system_audio_to_m4a(
            self.main_window._find_api_ffmpeg(), wav_path,
            bitrate=bitrate_arg(stereo_voice_bitrate_kbps(self.main_window.settings)))
        if not m4a_path:
            return None
        try:
            with open(m4a_path, "rb") as source:
                return source.read()
        except OSError as exc:
            logging.warning("[_send_voice_message] cannot read M4A: %s", exc)
            return None
        finally:
            try:
                os.unlink(m4a_path)
            except OSError:
                pass

    def _cancel_active_recording(self):
        """Stop and discard an in-progress voice recording, if any.

        Recording is scoped to whichever conversation was open when it
        started — there is no "background recording" that survives leaving
        the chat, so closing OR switching away from that conversation must
        cancel it the same way, rather than leaving _is_recording true and
        the voice panel visible while main_window.conversation has already
        moved on. Without this on the switch path specifically, pressing
        Enviar afterwards sent the recording to whatever conversation the
        user had since navigated to — not the one it was actually recorded
        in, since _send_voice_message() reads self.conversation at send
        time, not at record-start time.
        """
        # Bump the token so a PyAudio stream still opening on a background
        # thread (see _start_voice_recording) gets closed and discarded
        # instead of surfacing into whatever conversation is open when it
        # finishes.
        self._recording_open_token += 1
        self._recording_starting = False
        mixed = getattr(self, "_recording_system_audio", False)
        if mixed:
            self._stop_system_audio_recording()
        if not self._is_recording and not mixed:
            return
        if not mixed:
            self._stop_recording_stream()
        self._is_recording     = False
        self._recording_paused = False
        self._recording_frames = []
        self._cancel_incremental_denoise()
        _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if _rec_jid and not _rec_jid.endswith("@newsletter"):
            self.main_window.send_recording_status(_rec_jid, False, _rec_jid.endswith("@g.us"))
        if mixed:
            self._hide_voice_panel()
        self._voice_panel.Hide()
        self.record_voice_message_btn.Show()
        if hasattr(self, "_record_voice_system_btn"):
            self._record_voice_system_btn.Show()
