"""VoiceRecordingMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import os
import sound_lib.stream as sl_stream
import tempfile
import threading
import time
import uuid
import wave
import wx
from core.message_queue import PendingMessage
from ui.conversation_panel.typing_row import append_message_row, message_row_count
from core.voice_stereo import STEREO_BITRATE, STEREO_MIMETYPE, encode_as_stereo
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
                cloak_panel_focus_fallback(
                    self.conversation_panel, *recording_controls_to_hide
                )
            else:
                self.message_field.Hide()
            if hasattr(self, "_emoji_btn"):
                self._emoji_btn.Hide()
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

    def _stop_recording_stream(self):
        """Stop and close the active stream (safe to call when None)."""
        if hasattr(self, "_sd_stream") and self._sd_stream:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception:
                pass
            self._sd_stream = None
        if self._recording_stream is not None:
            try:
                self._recording_stream.stop_stream()
                self._recording_stream.close()
            except Exception:
                pass
            self._recording_stream = None

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

        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(self._recording_actual_ch)
                wf.setsampwidth(2)   # 16-bit PCM — matches how _send_voice_message writes it
                wf.setframerate(self._recording_actual_rate)
                wf.writeframes(b"".join(frames))
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
        # Stop the recording stream in background FIRST so the audio device is fully released
        # without blocking the UI thread before BASS plays the send sound.
        if mixed_audio:
            self._stop_system_audio_recording()
        else:
            threading.Thread(target=self._stop_recording_stream, daemon=True).start()
        self._is_recording     = False
        self._recording_paused = False

        self.main_window.voicemsg_send_sound.play()

        # Notify contacts that recording stopped (runs in its own thread).
        _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if _rec_jid and not _rec_jid.endswith("@newsletter"):
            self.main_window.send_recording_status(_rec_jid, False, _rec_jid.endswith("@g.us"))

        frames = self._recording_frames
        self._recording_frames = []

        if not frames:
            self._hide_voice_panel()
            self.message_field.SetFocus()
            return

        # ── Phase 2: instant UI update ────────────────────────────────────────
        remote_jid      = self.conversation.get("remoteJid", "")
        local_id        = str(uuid.uuid4())
        actual_rate     = self._recording_actual_rate
        actual_ch       = self._recording_actual_ch
        bytes_per_frame = 2 * actual_ch
        quoted_msg      = self._quoted_message
        stereo_out      = not mixed_audio and encode_as_stereo(actual_ch)
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
            "messageTimestamp": int(time.time()),
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

        def _write_and_enqueue():
            import time as _time
            _tw0 = _time.perf_counter()
            logging.info("[VOICE_TIMING] T+%.3fs — _write_and_enqueue thread started",
                         _tw0 - _t0)

            # 1. Join raw PCM frames.
            audio_data = b"".join(frames)
            logging.info("[VOICE_TIMING] T+%.3fs — PCM frames joined (%d bytes, %d frames)",
                         _time.perf_counter() - _t0, len(audio_data), len(frames))

            # Apply microphone noise reduction if enabled in settings
            if not mixed_audio and mw.settings.get("general", {}).get("noise_reduction_enabled", False):
                try:
                    logging.info("[VOICE_TIMING] Applying microphone noise reduction...")
                    from core.audio_processing import apply_noise_gate
                    audio_data = apply_noise_gate(audio_data, actual_rate, actual_ch)
                    logging.info("[VOICE_TIMING] Noise reduction applied successfully")
                except Exception as ex:
                    logging.error("[VOICE_TIMING] Failed to apply noise reduction: %s", ex)

            # 2. Write WAV temp file (used for ffmpeg conversion, backup, and retry fallback).
            tmp = None
            try:
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tmp.close()
                with wave.open(tmp.name, "wb") as wf:
                    wf.setnchannels(actual_ch)
                    wf.setsampwidth(2)   # 16-bit PCM
                    wf.setframerate(actual_rate)
                    wf.writeframes(audio_data)
                wav_path = tmp.name
                logging.info("[VOICE_TIMING] T+%.3fs — WAV written to %s",
                             _time.perf_counter() - _t0, wav_path)
            except Exception as exc:
                logging.error("[_send_voice_message] failed to write WAV: %s", exc)
                if as_audio_file or stereo_out:
                    if tmp is not None:
                        try:
                            os.unlink(tmp.name)
                        except OSError:
                            pass
                    wx.CallAfter(mw._on_message_failed, local_id,
                                 mw.i18n.t("media_audio_convert_failed"), True)
                return

            if as_audio_file:
                self._enqueue_system_audio_file(
                    wav_path, local_id, remote_jid, quoted_msg, enc_key, virtual_msg,
                )
                return

            # 3. Encode: stereo as AAC M4A (core/voice_stereo.py), never quietly
            # downmixed when that fails; mono as OGG Opus via ffmpeg conversion.
            voice_mimetype = ""
            ogg_bytes = None
            if stereo_out:
                ogg_bytes = self._encode_stereo_voice(wav_path)
                if not ogg_bytes:
                    try:
                        os.unlink(wav_path)
                    except OSError:
                        pass
                    wx.CallAfter(mw._on_message_failed, local_id,
                                 mw.i18n.t("media_audio_convert_failed"), True)
                    return
                voice_mimetype = STEREO_MIMETYPE
            _t_enc = _time.perf_counter()
            try:
                ogg_path = None if stereo_out else mw._convert_wav_to_ogg(wav_path)
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
            mw.message_queue.enqueue(pm)
            mw.mark_conversation_as_read(remote_jid)

        threading.Thread(target=_write_and_enqueue, daemon=True).start()

    def _encode_stereo_voice(self, wav_path):
        """The M4A bytes of a stereo voice message, or None if it could not be
        encoded."""
        from core.audio_transcode import encode_system_audio_to_m4a
        m4a_path = encode_system_audio_to_m4a(
            self.main_window._find_api_ffmpeg(), wav_path, bitrate=STEREO_BITRATE)
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
        _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if _rec_jid and not _rec_jid.endswith("@newsletter"):
            self.main_window.send_recording_status(_rec_jid, False, _rec_jid.endswith("@g.us"))
        if mixed:
            self._hide_voice_panel()
        self._voice_panel.Hide()
        self.record_voice_message_btn.Show()
        if hasattr(self, "_record_voice_system_btn"):
            self._record_voice_system_btn.Show()
