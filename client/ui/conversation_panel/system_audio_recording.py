"""SystemAudioRecordingMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Recording the microphone together with computer/system audio (WASAPI
loopback) for one message, started with Ctrl+Shift+H / the "Gravar
microfone e áudio do computador" button. Kept separate from
voice_recording.py (microphone-only capture) per CLAUDE.md's ~150-line rule
for a new feature: this mixed-capture path has its own session object, its
own two volume sliders (other system sounds, and NVDA specifically when
detected) and its own M4A encode, none of which the plain voice-message path
needs to know about.

Methods run with ``self`` bound to the ConversationsPanel instance, so every
attribute set in ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import os
import threading
import wx
from app_paths import data_path
from core.message_queue import PendingMessage
from core.system_audio_capture import SystemAudioRecorder
from core.utils import encrypt
from ui.accessible import AccessibleRecordingVolumeSlider
from ui.dialogs.system_audio_warning import (
    ask_system_audio,
    system_audio_warning_enabled,
    remember_system_audio_consent,
)


class SystemAudioRecordingMixin:
    """Recording, previewing and sending a mixed microphone + system-audio
    message."""

    def _on_record_system_audio(self, event):
        """Record both sources for one message; never turn this shortcut into Send."""
        if self._is_recording or self._recording_starting or self.conversation is None:
            return
        button = getattr(self, "_record_voice_system_btn", None)
        if button is not None and not button.IsEnabled():
            return
        if system_audio_warning_enabled(self.main_window.settings):
            confirmed, dont_ask_again = ask_system_audio(self, self.main_window.i18n)
            if not confirmed:
                return
            remember_system_audio_consent(self.main_window.settings, dont_ask_again)
            self.main_window.save_settings()
        self._start_system_audio_recording()

    def _create_system_audio_volume_controls(self, voice_sizer):
        """Native keyboard-accessible slider, immediately after Send in Tab order."""
        label = self.main_window.i18n.t("system_audio_recording_volume")
        self._system_audio_volume_label = wx.StaticText(self._voice_panel, label=label)
        voice_sizer.Add(self._system_audio_volume_label, 0, wx.LEFT | wx.BOTTOM, 5)
        value = self.main_window.settings.get("general", {}).get("system_audio_recording_volume", 100)
        if type(value) is not int or not 0 <= value <= 100:
            value = 100
        self._system_audio_volume_slider = wx.Slider(
            self._voice_panel, value=value, minValue=0, maxValue=100,
            style=wx.SL_HORIZONTAL | wx.SL_LABELS, size=(260, -1))
        self._system_audio_volume_slider.SetName(label)
        self._system_audio_volume_accessible = AccessibleRecordingVolumeSlider(self._system_audio_volume_slider)
        self._system_audio_volume_slider.SetAccessible(self._system_audio_volume_accessible)
        self._system_audio_volume_slider.SetLineSize(1)
        self._system_audio_volume_slider.SetPageSize(10)
        self._system_audio_volume_slider.MoveAfterInTabOrder(self._send_voice_btn)
        self._system_audio_volume_slider.Bind(wx.EVT_SLIDER, self._on_system_audio_volume)
        self._system_audio_volume_slider.Bind(wx.EVT_KEY_DOWN, self._on_system_audio_volume_key)
        voice_sizer.Add(self._system_audio_volume_slider, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        self._system_audio_volume_label.Hide()
        self._system_audio_volume_slider.Hide()

    def _create_nvda_volume_controls(self, voice_sizer):
        """Native NVDA gain, after the other computer audio in visual/Tab order."""
        label = self.main_window.i18n.t("system_audio_recording_nvda_volume")
        self._nvda_volume_label = wx.StaticText(self._voice_panel, label=label)
        voice_sizer.Add(self._nvda_volume_label, 0, wx.LEFT | wx.BOTTOM, 5)
        value = self.main_window.settings.get("general", {}).get("system_audio_recording_nvda_volume", 100)
        if type(value) is not int or not 0 <= value <= 100:
            value = 100
        self._nvda_volume_slider = wx.Slider(
            self._voice_panel, value=value, minValue=0, maxValue=100,
            style=wx.SL_HORIZONTAL | wx.SL_LABELS, size=(260, -1))
        self._nvda_volume_slider.SetName(label)
        self._nvda_volume_accessible = AccessibleRecordingVolumeSlider(self._nvda_volume_slider)
        self._nvda_volume_slider.SetAccessible(self._nvda_volume_accessible)
        self._nvda_volume_slider.SetLineSize(1)
        self._nvda_volume_slider.SetPageSize(10)
        self._nvda_volume_slider.MoveAfterInTabOrder(self._system_audio_volume_slider)
        self._nvda_volume_slider.Bind(wx.EVT_SLIDER, self._on_nvda_volume)
        self._nvda_volume_slider.Bind(wx.EVT_KEY_DOWN, self._on_nvda_volume_key)
        voice_sizer.Add(self._nvda_volume_slider, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        self._nvda_volume_label.Hide()
        self._nvda_volume_slider.Hide()

    def _update_system_audio_volume_controls(self):
        """Only mixed capture exposes gain; paused takes can set the next level."""
        mixed = bool(getattr(self, "_recording_system_audio", False))
        session = getattr(self, "_system_audio_session", None)
        self._system_audio_volume_label.Show(mixed)
        self._system_audio_volume_slider.Show(mixed)
        enabled = (mixed and session is not None and session.get("recorder") is not None
                   and not self._recording_starting and not self._system_audio_interrupted
                   and not session.get("transition") and not session.get("stopped")
                   and not session["cancelled"].is_set())
        self._system_audio_volume_slider.Enable(bool(enabled))
        self._nvda_volume_label.Show(mixed)
        self._nvda_volume_slider.Show(mixed)
        available = (mixed and session is not None and session.get("recorder") is not None
                     and not self._recording_starting
                     and session["recorder"].nvda_volume_available)
        self._nvda_volume_slider.Enable(bool(enabled and available))
        self._relabel_system_audio_volume_controls()

    def _relabel_system_audio_volume_controls(self):
        session = self._system_audio_session
        split = (self._recording_system_audio and not self._recording_starting
                 and session is not None and session.get("recorder") is not None
                 and session["recorder"].nvda_volume_available)
        t = self.main_window.i18n.t
        label = t("system_audio_recording_other_volume" if split else "system_audio_recording_volume")
        self._system_audio_volume_label.SetLabel(label)
        self._system_audio_volume_slider.SetName(label)
        label = t("system_audio_recording_nvda_volume")
        self._nvda_volume_label.SetLabel(label)
        self._nvda_volume_slider.SetName(label)

    def _on_nvda_volume(self, event):
        session = self._system_audio_session
        if (not self._recording_system_audio or session is None
                or session.get("recorder") is None or session["cancelled"].is_set()
                or session.get("transition") or session.get("stopped")
                or self._recording_starting or self._system_audio_interrupted
                or not session["recorder"].nvda_volume_available):
            return
        value = self._nvda_volume_slider.GetValue()
        session["recorder"].set_nvda_volume(value)
        self.main_window.settings.setdefault("general", {})["system_audio_recording_nvda_volume"] = value
        self.main_window._schedule_save_settings(delay=0.5)

    def _on_system_audio_volume_key(self, event):
        self._on_recording_volume_key(event, self._system_audio_volume_slider,
                                      self._on_system_audio_volume)

    def _on_nvda_volume_key(self, event):
        self._on_recording_volume_key(event, self._nvda_volume_slider, self._on_nvda_volume)

    def _on_recording_volume_key(self, event, slider, on_change):
        """Use volume directions for vertical/page keys on either native slider."""
        key = event.GetKeyCode()
        if (event.GetModifiers() != wx.MOD_NONE or not slider.IsEnabled()
                or key not in (wx.WXK_UP, wx.WXK_DOWN, wx.WXK_PAGEUP, wx.WXK_PAGEDOWN)):
            event.Skip()  # Tab, left/right, Home/End and shortcuts remain native.
            return
        step = slider.GetPageSize() if key in (wx.WXK_PAGEUP, wx.WXK_PAGEDOWN) else slider.GetLineSize()
        if key in (wx.WXK_DOWN, wx.WXK_PAGEDOWN):
            step = -step
        old = slider.GetValue()
        value = max(slider.GetMin(), min(slider.GetMax(), old + step))
        if value != old:
            slider.SetValue(value)
            # SetValue does not emit EVT_SLIDER. Update capture exactly once,
            # then expose the new native value without a second speech channel.
            on_change(None)
            wx.Accessible.NotifyEvent(wx.ACC_EVENT_OBJECT_VALUECHANGE, slider,
                                      wx.OBJID_CLIENT, wx.ACC_SELF)
        # Consume even at the limits: native handling would move the other way.

    def _on_system_audio_volume(self, event):
        # No system-volume calls, no rescaling of already recorded frames.
        session = getattr(self, "_system_audio_session", None)
        if (not getattr(self, "_recording_system_audio", False) or session is None
                or session.get("recorder") is None or session["cancelled"].is_set()
                or session.get("transition") or session.get("stopped")
                or self._recording_starting or self._system_audio_interrupted):
            return
        value = self._system_audio_volume_slider.GetValue()
        session["recorder"].set_system_audio_volume(value)
        self.main_window.settings.setdefault("general", {})["system_audio_recording_volume"] = value
        self.main_window._schedule_save_settings(delay=0.5)

    def _start_system_audio_recording(self):
        """Open BOTH capture sources off-thread, with a session-local frame buffer.

        Callbacks never dereference the panel's current buffer: a cancelled or
        late worker therefore cannot write into a later recording. The lock
        also freezes the old buffer before Send hands it to the encoder.
        """
        if self.conversation is None or self._is_recording or self._recording_starting:
            return
        self._recording_open_token += 1
        session = {
            "token": self._recording_open_token,
            "frames": [], "lock": threading.Lock(),
            "cancelled": threading.Event(), "accepting": True,
            "recorder": None, "error": None,
        }
        self._system_audio_session = session
        self._recording_frames = session["frames"]
        self._recording_starting = True
        self._recording_paused = False
        self._recording_system_audio = True
        self._system_audio_interrupted = False
        channels = 2
        microphone_name = getattr(self.main_window, "effective_input_device_name", "") or ""
        self._show_system_audio_recording_controls(starting=True)
        # Read wx only on its owning thread, before the asynchronous opener.
        system_audio_volume = self._system_audio_volume_slider.GetValue()
        nvda_volume = self._nvda_volume_slider.GetValue()

        def on_frames(data):
            with session["lock"]:
                if session["accepting"] and not session["cancelled"].is_set():
                    session["frames"].append(data)

        def on_error(error):
            with session["lock"]:
                if session["cancelled"].is_set() or session["error"] is not None:
                    return
                session["error"] = error
                session["accepting"] = False
            wx.CallAfter(self._on_system_audio_error, session)

        def open_sources():
            recorder = None
            error = None
            try:
                recorder = SystemAudioRecorder(
                    on_frames, on_error, microphone_name=microphone_name,
                    channels=channels, rate=48000, system_audio_volume=system_audio_volume,
                    separate_nvda=True, nvda_volume=nvda_volume,
                )
                session["recorder"] = recorder
                if not session["cancelled"].is_set():
                    recorder.start()
            except Exception as exc:
                error = exc
            if error is not None or session["cancelled"].is_set():
                if recorder is not None:
                    try:
                        recorder.close()
                    except Exception:
                        # The original startup failure still has to reach wx;
                        # a driver's shutdown timeout cannot strand the UI.
                        pass
            wx.CallAfter(self._on_system_audio_opened, session, error)

        threading.Thread(target=open_sources, daemon=True).start()

    def _show_system_audio_recording_controls(self, starting=False):
        """Use the existing voice composer, including Discard during opening."""
        if not self._voice_recording_focus_suppression_enabled():
            self.message_field.Hide()
        if hasattr(self, "_emoji_btn"):
            self._emoji_btn.Hide()
        self.send_message_btn.Hide()
        self.record_voice_message_btn.Hide()
        if hasattr(self, "_record_voice_system_btn"):
            self._record_voice_system_btn.Hide()
        self._add_attachment_btn.Hide()
        self._pause_resume_btn.SetLabel(self.main_window.i18n.t("pause_recording"))
        self._pause_resume_btn.Enable(not starting)
        self._send_voice_btn.Enable(not starting)
        self._update_system_audio_volume_controls()
        self._play_recorded_btn.Hide()
        self._voice_panel.Show()
        self.conversation_panel.Layout()
        if not starting:
            focus = self.main_window.settings.get("user_interface", {}).get("voice_record_focus", "send")
            button = self._discard_voice_btn if focus == "discard" else self._send_voice_btn
            self._focus_recording_button_silently(button)

    def _on_system_audio_opened(self, session, error):
        """UI-thread completion; cancelled generations never touch UI state."""
        if (session is not self._system_audio_session
                or session["token"] != self._recording_open_token):
            # Cancellation detached this session and arranged cleanup. If it
            # raced construction, open_sources closes it after start returns.
            return
        self._recording_starting = False
        if error is not None:
            self._stop_system_audio_recording()
            self._recording_frames = []
            self._hide_voice_panel()
            self.main_window.output(self.main_window.i18n.t("system_audio_recording_failed"))
            return
        recorder = session["recorder"]
        self._recording_actual_rate = recorder.rate
        self._recording_actual_ch = recorder.channels
        self._is_recording = True
        self._show_system_audio_recording_controls()
        if session["error"] is not None:
            self._on_system_audio_error(session)
            return
        self.main_window.voicemsg_startrecording_sound.play()
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if jid and not jid.endswith("@newsletter"):
            self.main_window.send_recording_status(jid, True, jid.endswith("@g.us"))

    def _on_system_audio_error(self, session):
        """Keep partial audio for preview/send/discard, but never restart silently."""
        if (session is not self._system_audio_session
                or session["token"] != self._recording_open_token
                or self._recording_starting or self._system_audio_interrupted):
            return
        session["failed_during_send"] = bool(session.get("sending"))
        self._system_audio_interrupted = True
        self._update_system_audio_volume_controls()
        self._recording_paused = True
        self._pause_resume_btn.Disable()
        self._play_recorded_btn.Show()
        self.conversation_panel.Layout()
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if jid and not jid.endswith("@newsletter"):
            self.main_window.send_recording_status(jid, False, jid.endswith("@g.us"))
        self.main_window.output(self.main_window.i18n.t("system_audio_recording_interrupted"))

    def _stop_system_audio_recording(self):
        """Detach synchronously; close this exact recorder off-thread (never the next)."""
        session = getattr(self, "_system_audio_session", None)
        if session is None:
            return
        self._system_audio_session = None
        self._recording_open_token += 1
        self._recording_starting = False
        with session["lock"]:
            session["accepting"] = False
            session["cancelled"].set()
        recorder = session["recorder"]
        if recorder is not None:
            def close_discarded():
                try:
                    recorder.close()
                except Exception:
                    # Cancellation already invalidated all callbacks; a
                    # shutdown timeout must not resurrect or send this audio.
                    pass
            threading.Thread(target=close_discarded, daemon=True).start()

    def _finish_system_audio_for_send(self, event):
        """Flush both sources off-thread before the existing encoder gets the buffer."""
        session = self._system_audio_session
        if session is None or session.get("transition"):
            return
        session["transition"] = True
        session["sending"] = True
        self._update_system_audio_volume_controls()
        self._send_voice_btn.Disable()
        self._pause_resume_btn.Disable()
        self._stop_recorded_audio_preview()
        self._play_recorded_btn.Hide()

        def finish():
            error = None
            try:
                session["recorder"].close()
            except Exception as exc:
                error = exc
            # close() joins the mixer and emits its final PCM before returning.
            with session["lock"]:
                session["accepting"] = False
            wx.CallAfter(self._on_system_audio_ready_to_send, session, error)

        threading.Thread(target=finish, daemon=True).start()

    def _on_system_audio_ready_to_send(self, session, error):
        if (session is not self._system_audio_session
                or session["token"] != self._recording_open_token):
            return
        session["transition"] = False
        session["stopped"] = True
        self._send_voice_btn.Enable()
        if error is not None or (session["error"] is not None and not self._system_audio_interrupted):
            with session["lock"]:
                session["error"] = session["error"] or error
            self._on_system_audio_error(session)
            return
        # A loss reported while close was in progress must not auto-send.
        if session.get("failed_during_send"):
            self._play_recorded_btn.Show()
            self.conversation_panel.Layout()
            return
        self._send_voice_message(None)

    def _toggle_system_audio_pause(self):
        """Pause acknowledgement is a barrier; never run it on wx's thread."""
        session = self._system_audio_session
        if session is None or session.get("transition") or self._system_audio_interrupted:
            return
        paused = not self._recording_paused
        session["transition"] = True
        self._update_system_audio_volume_controls()
        # Keep the focused button enabled, as in microphone-only recording.
        # The transition guard above already ignores repeat activations.
        self._send_voice_btn.Disable()
        if not paused:
            # Stop preview BEFORE the backend can begin capture again.
            self._stop_recorded_audio_preview()
            self._play_recorded_btn.Hide()

        def change_pause():
            error = None
            try:
                if not paused:
                    with session["lock"]:
                        session["accepting"] = session["error"] is None
                session["recorder"].set_paused(paused)
            except Exception as exc:
                error = exc
            with session["lock"]:
                session["accepting"] = not paused and session["error"] is None and error is None
            wx.CallAfter(self._on_system_audio_paused, session, paused, error)

        threading.Thread(target=change_pause, daemon=True).start()

    def _on_system_audio_paused(self, session, paused, error):
        if (session is not self._system_audio_session
                or session["token"] != self._recording_open_token):
            return
        session["transition"] = False
        self._send_voice_btn.Enable()
        if error is not None:
            with session["lock"]:
                session["error"] = session["error"] or error
            self._on_system_audio_error(session)
            return
        if self._system_audio_interrupted or session["error"] is not None:
            self._on_system_audio_error(session)
            return
        self._recording_paused = paused
        self._update_system_audio_volume_controls()
        self._pause_resume_btn.Enable()
        self._pause_resume_btn.SetLabel(self.main_window.i18n.t(
            "resume_recording" if paused else "pause_recording"))
        self._play_recorded_btn.Show(paused)
        self.conversation_panel.Layout()
        # Confirm only an acknowledged transition; failed or stale callbacks
        # must not sound like a successful pause/resume.
        self.main_window.voicemsg_pauserecording_sound.play()

    def _enqueue_system_audio_file(self, wav_path, local_id, remote_jid, quoted_msg, enc_key, virtual_msg):
        """Worker-side M4A encoder for microphone + computer audio; a voice
        message never enters this path."""
        from core.audio_transcode import encode_system_audio_to_m4a
        mw = self.main_window
        m4a_path = cache_path = None
        try:
            m4a_path = encode_system_audio_to_m4a(mw._find_api_ffmpeg(), wav_path)
            if not m4a_path:
                raise RuntimeError("Mixed recording AAC encoding failed")
            voice_dir = data_path("voice_messages")
            os.makedirs(voice_dir, exist_ok=True)
            cache_path = os.path.join(voice_dir, f"{local_id}.msv")
            with open(m4a_path, "rb") as source:
                encrypted = encrypt(source.read(), enc_key)
            with open(cache_path, "wb") as cache:
                cache.write(encrypted)
            pm = PendingMessage(local_id, remote_jid, media_path=m4a_path,
                                media_type="audio", quoted=quoted_msg, owns_media_path=True,
                                custom_filename=f"{mw.i18n.t('default_filename_audio')}.m4a")
        except Exception:
            logging.exception("[mixed_audio] could not prepare recorded attachment")
            for path in (m4a_path, cache_path):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            wx.CallAfter(mw._on_message_failed, local_id,
                         mw.i18n.t("media_audio_convert_failed"), True)
            return
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

        def enqueue_ready():
            # Queue ownership starts only now. A delete while ffmpeg/cache was
            # running must not resurrect the row or transmit the recording.
            if virtual_msg.get("_cancelled_awaiting_id") or self._is_cancelled_pending(local_id):
                for path in (m4a_path, cache_path):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                mw._on_cancelled_message_dropped(local_id)
                return
            mw.message_queue.enqueue(pm)
            mw.mark_conversation_as_read(remote_jid)

        wx.CallAfter(enqueue_ready)
