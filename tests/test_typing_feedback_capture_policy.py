"""Capture policy using state flags and preflight sentinels; no capture or call."""
from types import SimpleNamespace
import pytest

from core.typing_feedback import SILENCE_CAPTURE_KEY, mode, save_preferences, silence_during_capture
from main_window.calls import CallsMixin
from ui.conversation_panel import voice_recording
from ui.conversation_panel.system_audio_recording import SystemAudioRecordingMixin
from tests.test_typing_feedback_integration import rig  # noqa: F401
from tests.test_typing_feedback_controls import controls


@pytest.mark.parametrize("value", [None, True, 0, "false", [], {}])
def test_missing_or_malformed_silence_preference_uses_the_safe_default(value):
    assert silence_during_capture({})
    assert silence_during_capture({"speech_content": {SILENCE_CAPTURE_KEY: value}})
    assert not silence_during_capture({"speech_content": {SILENCE_CAPTURE_KEY: False}})


@pytest.mark.parametrize("silence", [True, False])
@pytest.mark.parametrize("selected", ["off", "speech", "sound", "both"])
def test_capture_preference_round_trips_without_changing_notification_mode(silence, selected):
    c = controls()
    settings = {}
    save_preferences(settings, selected, 3, silence_capture=silence)
    c.load(settings)
    c.dialog._announce_typing_check.SetValue(selected in ("speech", "both"))
    assert c.silence.GetValue() is silence
    c.apply(settings)
    assert settings["speech_content"][SILENCE_CAPTURE_KEY] is silence
    assert mode(settings) == selected
    save_preferences(settings, selected, 5)  # Changing other preferences preserves this choice.
    assert settings["speech_content"][SILENCE_CAPTURE_KEY] is silence


@pytest.mark.parametrize("kind", ["_is_recording", "_recording_starting", "_recording_system_audio",
                                  "_active_voice_call", "_call_audio_session"])
@pytest.mark.parametrize("silence", [True, False])
@pytest.mark.parametrize("already_playing", [True, False])
def test_capture_and_call_gates_respect_the_users_choice(rig, kind, silence, already_playing):
    w = rig.window
    w.settings["speech_content"][SILENCE_CAPTURE_KEY] = silence
    old = None
    if already_playing:
        rig.presence("composing")
        old = w._typing_sound_cadence.timer
    target = w if kind.startswith("_active") or kind.startswith("_call") else w.conversations_panel
    setattr(target, kind, True)
    if old is None:
        rig.presence("composing")
        assert rig.played == ([] if silence else [0])
    else:
        rig.now = 3
        old.callback()
        assert rig.played == ([0] if silence else [0, 3])
    # Existing spoken-content behavior is deliberately independent.
    assert w.speak_output.outputs == ["Fulano está digitando..."]


class CaptureBoundary(Exception):
    """Stop the real UI handler just BEFORE it can construct any capture object."""


@pytest.mark.parametrize("kind", ["microphone", "system audio"])
@pytest.mark.parametrize("silence", [True, False])
def test_recording_preflight_stops_long_cue_before_capture_state_is_created(rig, monkeypatch, kind, silence):
    w = rig.window
    w.settings["speech_content"][SILENCE_CAPTURE_KEY] = silence
    rig.presence("composing")
    old = w._typing_sound_cadence.timer
    class Panel:
        main_window = w
        conversation = {"remoteJid": "synthetic@s.whatsapp.net"}
        _is_recording = False
        _recording_starting = False

        @property
        def _recording_frames(self):
            return None

        @_recording_frames.setter
        def _recording_frames(self, value):
            raise CaptureBoundary()

        @property
        def _recording_open_token(self):
            raise CaptureBoundary()
    with pytest.raises(CaptureBoundary):
        if kind == "microphone":
            monkeypatch.setattr(voice_recording, "pyaudio", object())
            voice_recording.VoiceRecordingMixin._start_voice_recording(Panel())
        else:
            SystemAudioRecordingMixin._start_system_audio_recording(Panel())
    assert old.stopped is silence
    assert (w._typing_sound_cadence.timer is None) is silence


@pytest.mark.parametrize("kind", ["incoming", "outgoing"])
@pytest.mark.parametrize("silence", [True, False])
def test_call_preflight_stops_cue_before_worker_dispatch_without_making_a_call(rig, kind, silence):
    w = rig.window
    w.settings["speech_content"][SILENCE_CAPTURE_KEY] = silence
    rig.presence("composing")
    old = w._typing_sound_cadence.timer
    w._uses_neonize = lambda: False
    w._is_self_jid = lambda jid: False
    w._call_control_payload = lambda identity: {}
    w.stop_incoming_call_alert = lambda *args, **kwargs: None
    w.output = lambda *args, **kwargs: None
    def boundary():
        raise CaptureBoundary()  # wx.CallAfter is fake and inline; no worker follows.
    w._sync_voice_call_bar = boundary
    with pytest.raises(CaptureBoundary):
        if kind == "incoming":
            CallsMixin.accept_incoming_call(w, "synthetic-call")
        else:
            CallsMixin._start_call(w, "5511222333444@s.whatsapp.net", "Synthetic",
                                   is_video=False, is_group=False)
    assert old.stopped is silence


@pytest.mark.parametrize("silence", [True, False])
def test_call_bar_state_sync_honors_preference_even_if_the_call_window_does_not_exist(rig, silence):
    w = rig.window
    w.settings["speech_content"][SILENCE_CAPTURE_KEY] = silence
    rig.presence("composing")
    old = w._typing_sound_cadence.timer
    w._active_voice_call = {"call_id": "synthetic"}
    CallsMixin._sync_voice_call_bar(w)
    assert old.stopped is silence


def test_recording_end_requires_fresh_presence_and_never_revives_cancelled_timer(rig):
    w = rig.window
    rig.presence("composing")
    old = w._typing_sound_cadence.timer
    w.conversations_panel._is_recording = True
    old.callback()
    w.conversations_panel._is_recording = False
    old.callback()
    assert rig.played == [0]
    rig.presence("composing")
    assert rig.played == [0, 0]
