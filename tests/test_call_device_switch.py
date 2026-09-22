"""Switching call audio devices must not touch the call's camera.

_restart_active_voice_call_audio() used to stop the audio through the same
path as the end of a call, which stopped the camera and replaced
_active_voice_call with a fresh dict. _start_call_camera() compares that
object by identity to tell "same call" from "another call", so a camera being
reopened alongside the switch was always discarded: the other person saw black
for the rest of the call and nothing was spoken.

MainWindow cannot be instantiated without a wx.App, so the three methods are
bound onto a plain stub; the worker thread is run inline.
"""

import threading

import pytest

import main
from main import MainWindow


class _Audio:
    def __init__(self, fail=False):
        self.fail = fail
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1
        if self.fail:
            raise OSError("device refused")

    def stop(self):
        self.stopped += 1


class _Ws:
    instance_name = "session"

    def __init__(self):
        self.audio_stream_stops = 0

    def stop_call_audio_stream(self):
        self.audio_stream_stops += 1


class _I18n:
    def t(self, key):
        return key


class _CallWindow:
    _restart_active_voice_call_audio = MainWindow._restart_active_voice_call_audio
    _stop_voice_call_audio = MainWindow._stop_voice_call_audio
    _start_voice_call_audio = MainWindow._start_voice_call_audio

    def __init__(self, *, fail_new_devices=False):
        self._call_action_lock = threading.Lock()
        self._call_audio_session = _Audio()
        self._call_ring_audio_session = None
        self._call_audio_stop_timer = None
        self._call_audio_restart_pending = False
        self._incoming_call_details = {}
        self._voice_call_last_announced_state = "ACTIVE"
        self._active_voice_call = {
            "identity": "CALL1", "call_id": "CALL1", "peer_jid": "p@s.whatsapp.net",
            "name": "Ana", "outgoing": True, "is_video": True,
        }
        self._call_camera_capture = object()
        self.camera_stops = []
        self.spoken = []
        self.ws = _Ws()
        self.token = "session"
        self.i18n = _I18n()
        self._fail_new_devices = fail_new_devices

    def _build_call_audio_session(self):
        return _Audio(fail=self._fail_new_devices), "session"

    def _stop_incoming_call_audio_monitor(self):
        pass

    def _stop_call_camera(self, *, reset_availability=False, native=False):
        self.camera_stops.append(reset_availability)
        self._call_camera_capture = None

    def _sync_voice_call_bar(self):
        pass

    def output(self, text, interrupt=False):
        self.spoken.append(text)


class _InlineThread:
    def __init__(self, target=None, args=(), daemon=None, **_kw):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


@pytest.fixture(autouse=True)
def _inline(monkeypatch):
    monkeypatch.setattr(main.threading, "Thread", _InlineThread)
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)


def test_switching_devices_keeps_the_camera_and_the_same_call_record():
    window = _CallWindow()
    call = window._active_voice_call
    capture = window._call_camera_capture
    old_audio = window._call_audio_session

    window._restart_active_voice_call_audio()

    assert old_audio.stopped == 1
    assert window._call_audio_session is not old_audio
    assert window._call_audio_session.started == 1
    # the object a concurrent _start_call_camera() captured is still the call
    assert window._active_voice_call is call
    assert window._call_camera_capture is capture
    assert window.camera_stops == []
    assert window._voice_call_last_announced_state == "ACTIVE"
    assert window.spoken == []


def test_a_switch_that_cannot_open_any_device_still_tears_the_call_down():
    window = _CallWindow(fail_new_devices=True)

    window._restart_active_voice_call_audio()

    assert window._call_audio_session is None
    assert window._active_voice_call is None
    assert window.camera_stops == [True]
    assert window.spoken == ["voice_call_device_switch_failed"]


def test_ending_a_call_still_stops_the_camera():
    window = _CallWindow()

    window._stop_voice_call_audio()

    assert window.camera_stops == [True]
    assert window._active_voice_call is None


def test_a_new_call_still_gets_its_own_record():
    window = _CallWindow()
    window._call_audio_session = None
    window._active_voice_call = None

    window._start_voice_call_audio("CALL2", {"call_id": "CALL2", "is_video": False})

    assert window._active_voice_call["call_id"] == "CALL2"
    assert window._voice_call_last_announced_state == ""


def test_a_call_that_ends_during_the_switch_is_not_reopened():
    """Review nit: ENDED arrives without _call_action_lock, so it can land
    between the switch's stop and its start. Reopening then would capture
    the microphone for a call that is already over."""
    window = _CallWindow()
    window._call_audio_session = None
    window._active_voice_call = None

    opened = window._start_voice_call_audio("CALL1", {"call_id": "CALL1"}, keep_active_call=True)

    assert opened is False
    assert window._call_audio_session is None
    assert window._active_voice_call is None


def test_the_call_settings_camera_reopen_uses_the_guarded_path():
    import inspect
    source = inspect.getsource(MainWindow.open_call_audio_settings)
    assert 'not getattr(self, "_call_camera_resuming", False)' in source
    assert "target=self._resume_call_camera" in source
    assert "target=self._start_call_camera" not in source
