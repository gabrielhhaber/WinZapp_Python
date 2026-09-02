"""Tests for playing back a voice recording while it's paused.

New feature: while recording is paused, a "Reproduzir áudio gravado" button
(Ctrl+P, same accelerator "Pin conversation" already used in the
conversations list — mutually exclusive contexts, see _on_accel_pin_list)
plays everything captured so far. It toggles to "Parar reprodução" while
playing (same Ctrl+P, reported via AccessiblePlayRecordedAudio either way),
and reaching the end of the audio is a full stop back to "Reproduzir áudio
gravado", not a pause.

Playback deliberately goes through a plain sl_stream.FileStream — not the
app's Sound/load_sound wrapper, whose .play() reroutes to the separate
"effects" output device (Settings > Dispositivos de Áudio) when one is
configured. That device is meant for short UI cue sounds, not the user's own
recorded voice, so this instead inherits BASS's current process-wide default
device the same way _play_audio() (real incoming voice messages) already
does — i.e. the user's actual configured Output device.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so the methods under test are bound to a small stub — same
approach as tests/test_mass_selection.py. sl_stream.FileStream (BASS-backed)
is faked so no real audio device is touched; the WAV file itself is written
for real via tempfile/wave, so temp-file cleanup is verified against the
real filesystem.
"""

import os

import pytest

import ui.conversations as conversations_module
from ui.conversations import ConversationsPanel


class _FakeI18n:
    _STRINGS = {
        "pause_recording": "Pausar gravação",
        "resume_recording": "Continuar gravação",
        "play_recorded_audio": "Reproduzir áudio gravado",
        "stop_recorded_audio_playback": "Parar reprodução",
    }

    def t(self, key):
        return self._STRINGS[key]


class _FakeSound:
    def __init__(self, *a, **k):
        self.play_calls = 0
        self.stop_calls = 0
        self.is_playing = True

    def play(self):
        self.play_calls += 1
        self.is_playing = True

    def stop(self):
        self.stop_calls += 1
        self.is_playing = False


class _FakeTimer:
    def __init__(self):
        self.running = False

    def Start(self, ms=0):
        self.running = True

    def Stop(self):
        self.running = False


class _FakeWidget:
    def __init__(self):
        self.shown = None
        self.label = None

    def Show(self):
        self.shown = True

    def Hide(self):
        self.shown = False

    def SetLabel(self, text):
        self.label = text


class _FakeMainWindow:
    class _PauseSound:
        def __init__(self, outer):
            self._outer = outer

        def play(self):
            self._outer.pause_sound_plays += 1

    def __init__(self):
        self.i18n = _FakeI18n()
        self.settings = {}
        self.pause_sound_plays = 0
        self.pinned = set()

    def is_chat_pinned(self, jid):
        return jid in self.pinned


class _Panel:
    _toggle_pause_recording = ConversationsPanel._toggle_pause_recording
    _toggle_play_recorded_audio = ConversationsPanel._toggle_play_recorded_audio
    _on_recorded_audio_timer = ConversationsPanel._on_recorded_audio_timer
    _stop_recorded_audio_preview = ConversationsPanel._stop_recorded_audio_preview
    _cleanup_recorded_audio_temp_file = ConversationsPanel._cleanup_recorded_audio_temp_file
    _on_accel_pin_list = ConversationsPanel._on_accel_pin_list
    _silence_send_voice_focus_if_enabled = ConversationsPanel._silence_send_voice_focus_if_enabled
    _voice_recording_silence_enabled = ConversationsPanel._voice_recording_silence_enabled

    def __init__(self):
        self.main_window = _FakeMainWindow()
        self.main_window.voicemsg_pauserecording_sound = _FakeMainWindow._PauseSound(self.main_window)
        self._is_recording = True
        self._recording_paused = False
        self._recording_frames = [b"\x00\x00" * 100]
        self._recording_actual_rate = 48000
        self._recording_actual_ch = 1
        self._recorded_audio_sound = None
        self._recorded_audio_temp_path = None
        self._recorded_audio_timer = _FakeTimer()
        self._pause_resume_btn = _FakeWidget()
        self._play_recorded_btn = _FakeWidget()
        self.conversation_panel = _FakeWidget()
        self.conversation_panel.Layout = lambda: None
        self._pin_calls = []
        self._unpin_calls = []

    def _selected_chat_from_list(self):
        return {"remoteJid": "5511999999999@s.whatsapp.net"}

    def _on_menu_pin(self, jid):
        self._pin_calls.append(jid)

    def _on_menu_unpin(self, jid):
        self._unpin_calls.append(jid)


@pytest.fixture
def fake_file_stream(monkeypatch):
    created = []

    def _fake(*a, **k):
        snd = _FakeSound()
        created.append(snd)
        return snd

    monkeypatch.setattr(conversations_module.sl_stream, "FileStream", _fake)
    return created


class TestVisibilityFollowsPauseState:
    def test_pausing_shows_the_button(self, fake_file_stream):
        panel = _Panel()
        panel._toggle_pause_recording(None)
        assert panel._recording_paused is True
        assert panel._play_recorded_btn.shown is True

    def test_resuming_hides_the_button_and_stops_any_preview(self, fake_file_stream):
        panel = _Panel()
        panel._toggle_pause_recording(None)  # pause
        panel._toggle_play_recorded_audio(None)  # start preview
        snd = panel._recorded_audio_sound
        assert snd is not None

        panel._toggle_pause_recording(None)  # resume

        assert panel._play_recorded_btn.shown is False
        assert snd.stop_calls == 1
        assert panel._recorded_audio_sound is None


class TestPlayback:
    def test_play_writes_a_real_wav_and_starts_the_timer(self, fake_file_stream):
        panel = _Panel()
        panel._recording_paused = True
        panel._toggle_play_recorded_audio(None)

        assert len(fake_file_stream) == 1
        assert fake_file_stream[0].play_calls == 1
        assert panel._recorded_audio_timer.running is True
        assert panel._play_recorded_btn.label == "Parar reprodução"
        assert os.path.isfile(panel._recorded_audio_temp_path)

        # Clean up whatever the test itself leaves on disk if the assertion above fails.
        panel._stop_recorded_audio_preview()

    def test_clicking_again_while_playing_stops_it(self, fake_file_stream):
        panel = _Panel()
        panel._recording_paused = True
        panel._toggle_play_recorded_audio(None)
        temp_path = panel._recorded_audio_temp_path
        snd = fake_file_stream[0]

        panel._toggle_play_recorded_audio(None)

        assert snd.stop_calls == 1
        assert panel._recorded_audio_sound is None
        assert panel._play_recorded_btn.label == "Reproduzir áudio gravado"
        assert not os.path.exists(temp_path)

    def test_reaching_the_end_is_a_full_stop_not_a_pause(self, fake_file_stream):
        panel = _Panel()
        panel._recording_paused = True
        panel._toggle_play_recorded_audio(None)
        temp_path = panel._recorded_audio_temp_path
        fake_file_stream[0].is_playing = False  # playback reached EOF on its own

        panel._on_recorded_audio_timer(None)

        assert panel._recorded_audio_sound is None
        assert panel._recorded_audio_timer.running is False
        assert panel._play_recorded_btn.label == "Reproduzir áudio gravado"
        assert not os.path.exists(temp_path)

    def test_the_timer_is_a_no_op_while_still_playing(self, fake_file_stream):
        panel = _Panel()
        panel._recording_paused = True
        panel._toggle_play_recorded_audio(None)
        temp_path = panel._recorded_audio_temp_path

        panel._on_recorded_audio_timer(None)  # still is_playing == True

        assert panel._recorded_audio_sound is not None
        assert os.path.isfile(temp_path)
        panel._stop_recorded_audio_preview()

    def test_not_reachable_while_actively_recording_unpaused(self, fake_file_stream):
        panel = _Panel()
        panel._recording_paused = False
        panel._toggle_play_recorded_audio(None)
        assert fake_file_stream == []
        assert panel._recorded_audio_sound is None

    def test_no_frames_yet_does_nothing(self, fake_file_stream):
        panel = _Panel()
        panel._recording_paused = True
        panel._recording_frames = []
        panel._toggle_play_recorded_audio(None)
        assert fake_file_stream == []

    def test_a_failed_stream_open_cleans_up_the_temp_file(self, monkeypatch):
        def _raise(*a, **k):
            raise RuntimeError("no audio device")

        monkeypatch.setattr(conversations_module.sl_stream, "FileStream", _raise)
        panel = _Panel()
        panel._recording_paused = True
        panel._toggle_play_recorded_audio(None)
        assert panel._recorded_audio_sound is None
        assert panel._recorded_audio_temp_path is None


class TestCtrlPDispatch:
    def test_routes_to_preview_playback_while_paused(self, fake_file_stream):
        panel = _Panel()
        panel._recording_paused = True
        panel._on_accel_pin_list(None)
        assert len(fake_file_stream) == 1
        assert panel._pin_calls == []
        panel._stop_recorded_audio_preview()

    def test_falls_through_to_pin_when_not_recording(self, fake_file_stream):
        panel = _Panel()
        panel._is_recording = False
        panel._on_accel_pin_list(None)
        assert panel._pin_calls == ["5511999999999@s.whatsapp.net"]
        assert fake_file_stream == []

    def test_falls_through_to_pin_when_recording_but_not_paused(self, fake_file_stream):
        panel = _Panel()
        panel._recording_paused = False
        panel._on_accel_pin_list(None)
        assert panel._pin_calls == ["5511999999999@s.whatsapp.net"]
        assert fake_file_stream == []
