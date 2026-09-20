from io import BytesIO
import json
from pathlib import Path
from threading import Event

import pytest

from core.call_logic import active_call_label_key, incoming_call_can_answer
from core.call_video import CameraCapture, camera_names, jpeg_frames, list_camera_devices
from main import MainWindow


def test_camera_names_only_reads_video_devices():
    listing = '''[dshow @ 000] DirectShow video devices (some may be both video and audio devices)
[dshow @ 000] "Integrated Camera"
[dshow @ 000]   Alternative name "@device_pnp_abc"
[dshow @ 000] "USB Camera"
[dshow @ 000] DirectShow audio devices
[dshow @ 000] "Microphone"
'''
    assert camera_names(listing) == ["Integrated Camera", "USB Camera"]


def test_camera_names_accepts_current_ffmpeg_directshow_suffix_and_spacing():
    listing = '''[dshow @ 000] DirectShow video devices (some may be both video and audio devices)
[dshow @ 000]  "Integrated Camera" (video)
[dshow @ 000]     Alternative name "@device_pnp_abc"
[dshow @ 000]    "USB Camera"    (VIDEO)
[dshow @ 000] DirectShow audio devices
[dshow @ 000]  "Microphone" (audio)
'''
    assert camera_names(listing) == ["Integrated Camera", "USB Camera"]


class _FakeCompletedProcess:
    def __init__(self, stderr):
        self.stderr = stderr


class _FakeCameraProcess:
    """Fake subprocess.Popen result: a fixed JPEG frame on stdout."""

    def __init__(self, frame=b'\xff\xd8frame\xff\xd9', returncode=None):
        self.stdout = BytesIO(frame)
        self.killed = False
        self._returncode = returncode

    def poll(self):
        """None means "still running", like subprocess.Popen.poll()."""
        return self._returncode

    def kill(self):
        self.killed = True


_CAMERA_LISTING = '''[dshow @ 000] DirectShow video devices (some may be both video and audio devices)
[dshow @ 000] "Integrated Camera"
[dshow @ 000] "USB Camera"
[dshow @ 000] DirectShow audio devices
[dshow @ 000] "Microphone"
'''


def test_list_camera_devices_parses_the_same_listing_as_camera_names(monkeypatch):
    import core.call_video as call_video
    monkeypatch.setattr(call_video.sys, "platform", "win32")
    monkeypatch.setattr(call_video.subprocess, "run",
                         lambda *a, **kw: _FakeCompletedProcess(_CAMERA_LISTING))

    assert list_camera_devices("ffmpeg.exe") == ["Integrated Camera", "USB Camera"]


def test_list_camera_devices_returns_empty_when_ffmpeg_fails(monkeypatch):
    import core.call_video as call_video
    monkeypatch.setattr(call_video.sys, "platform", "win32")

    def boom(*a, **kw):
        raise OSError("ffmpeg not found")

    monkeypatch.setattr(call_video.subprocess, "run", boom)
    assert list_camera_devices("ffmpeg.exe") == []


def test_list_camera_devices_returns_empty_without_ffmpeg():
    assert list_camera_devices("") == []


def test_camera_capture_start_opens_the_preferred_device_when_detected(monkeypatch):
    import core.call_video as call_video
    monkeypatch.setattr(call_video.sys, "platform", "win32")
    monkeypatch.setattr(call_video.subprocess, "run",
                         lambda *a, **kw: _FakeCompletedProcess(_CAMERA_LISTING))
    popen_calls = []

    def fake_popen(cmd, **kw):
        popen_calls.append(cmd)
        return _FakeCameraProcess()

    monkeypatch.setattr(call_video.subprocess, "Popen", fake_popen)

    capture = CameraCapture("ffmpeg.exe", lambda frame: None)
    try:
        capture.start(preferred_name="USB Camera")
        cmd = popen_calls[0]
        assert cmd[cmd.index("-i") + 1] == "video=USB Camera"
    finally:
        capture.stop()


def test_camera_capture_start_falls_back_to_first_device_when_preference_is_missing(monkeypatch):
    import core.call_video as call_video
    monkeypatch.setattr(call_video.sys, "platform", "win32")
    monkeypatch.setattr(call_video.subprocess, "run",
                         lambda *a, **kw: _FakeCompletedProcess(_CAMERA_LISTING))
    popen_calls = []

    def fake_popen(cmd, **kw):
        popen_calls.append(cmd)
        return _FakeCameraProcess()

    monkeypatch.setattr(call_video.subprocess, "Popen", fake_popen)

    capture = CameraCapture("ffmpeg.exe", lambda frame: None)
    try:
        capture.start(preferred_name="Nonexistent Camera")
        cmd = popen_calls[0]
        assert cmd[cmd.index("-i") + 1] == "video=Integrated Camera"
    finally:
        capture.stop()

    capture = CameraCapture("ffmpeg.exe", lambda frame: None)
    try:
        capture.start()  # blank preference -> same "system default" fallback
        cmd = popen_calls[1]
        assert cmd[cmd.index("-i") + 1] == "video=Integrated Camera"
    finally:
        capture.stop()


def test_camera_capture_probe_with_transmit_false_never_sends_a_frame(monkeypatch):
    import core.call_video as call_video
    monkeypatch.setattr(call_video.sys, "platform", "win32")
    monkeypatch.setattr(call_video.subprocess, "run",
                         lambda *a, **kw: _FakeCompletedProcess(_CAMERA_LISTING))
    monkeypatch.setattr(call_video.subprocess, "Popen",
                         lambda *a, **kw: _FakeCameraProcess())

    sent = []
    capture = CameraCapture("ffmpeg.exe", sent.append, transmit=False)
    try:
        capture.start()
        assert capture.ready.wait(2)
        capture.thread.join(timeout=2)
    finally:
        capture.stop()
    assert sent == []


def test_camera_capture_transmits_by_default(monkeypatch):
    import core.call_video as call_video
    monkeypatch.setattr(call_video.sys, "platform", "win32")
    monkeypatch.setattr(call_video.subprocess, "run",
                         lambda *a, **kw: _FakeCompletedProcess(_CAMERA_LISTING))
    monkeypatch.setattr(call_video.subprocess, "Popen",
                         lambda *a, **kw: _FakeCameraProcess())

    sent = []
    capture = CameraCapture("ffmpeg.exe", sent.append)
    try:
        capture.start()
        assert capture.ready.wait(2)
        # The fake process's BytesIO has exactly one frame and then EOF, so
        # _run() returns on its own; join it rather than racing send_frame().
        capture.thread.join(timeout=2)
    finally:
        capture.stop()
    assert sent == [b'\xff\xd8frame\xff\xd9']


def test_jpeg_pipe_stops_mid_buffer_once_the_stop_event_is_set():
    """REGRESSION: one read() can carry several complete JPEGs, and the inner
    framing loop drained all of them regardless of stop_event. Turning video
    off therefore still sent the peer the frames already decoded from that
    last chunk, after WinZapp had announced video was off."""
    stop_event = Event()
    frames = b'\xff\xd8first\xff\xd9' + b'\xff\xd8second\xff\xd9'
    produced = []
    for frame in jpeg_frames(BytesIO(frames), stop_event):
        produced.append(frame)
        stop_event.set()  # "turn video off" lands right after the first frame
    assert produced == [b'\xff\xd8first\xff\xd9']


def test_camera_start_fails_fast_when_ffmpeg_exits_without_a_frame(monkeypatch):
    """REGRESSION: a camera held by another app makes ffmpeg exit in
    milliseconds, but start() waited out the whole 8 s ready timeout anyway.
    That ran on the answer path, after the ring tone had been stopped, so a
    blind user who pressed Answer got seconds of total silence first."""
    import time as _time

    import core.call_video as call_video
    monkeypatch.setattr(call_video.sys, "platform", "win32")
    monkeypatch.setattr(call_video.subprocess, "run",
                        lambda *a, **kw: _FakeCompletedProcess(_CAMERA_LISTING))
    # Empty stdout and a non-None returncode: ffmpeg refused the device.
    monkeypatch.setattr(call_video.subprocess, "Popen",
                        lambda *a, **kw: _FakeCameraProcess(frame=b'', returncode=1))

    capture = CameraCapture("ffmpeg.exe", lambda _frame: None)
    started = _time.monotonic()
    try:
        with pytest.raises(RuntimeError):
            capture.start()
    finally:
        capture.stop()
    assert _time.monotonic() - started < 3, "start() waited out the full ready timeout"


def test_jpeg_pipe_discards_noise_and_yields_complete_frames():
    frame_a = b'\xff\xd8first\xff\xd9'
    frame_b = b'\xff\xd8second\xff\xd9'
    assert list(jpeg_frames(BytesIO(b'junk' + frame_a + frame_b), Event())) == [
        frame_a, frame_b,
    ]


def test_individual_video_is_answerable_and_has_video_label():
    assert incoming_call_can_answer({"is_video": True})
    assert not incoming_call_can_answer({"is_group": True})
    assert active_call_label_key({"is_video": True}) == "video_call_active_label"


def test_video_button_and_labels_exist_in_all_languages():
    languages = Path(__file__).parents[1] / 'client' / 'languages'
    for path in languages.glob('*.json'):
        if path.name == 'language_map.json':
            continue
        entries = json.loads(path.read_text(encoding='utf-8'))
        assert entries['video_call_button']
        assert entries['video_call_window_title']
        assert entries['voice_call_video_off_button']
        assert entries['voice_call_video_on_button']
        assert '{name}' in entries['incoming_video_call_announcement']


def test_video_button_is_restricted_to_individual_chats():
    source = (Path(__file__).parents[1] / 'client' / 'ui' / 'conversations.py').read_text(encoding='utf-8')
    assert 'unavailable = jid.endswith(("@g.us", "@newsletter", "@broadcast"))' in source
    assert 'self._video_call_btn.Show(bool(jid) and not unavailable)' in source
    assert 'self.main_window.start_video_call(jid, name)' in source


class _NoCameraWs:
    def send_call_camera_frame(self, _frame):
        pass


class _NoCameraMainWindow:
    _start_call_camera = MainWindow._start_call_camera

    def __init__(self):
        self.ws = _NoCameraWs()
        self._active_voice_call = {"identity": "call-1", "is_video": True}
        self._call_camera_capture = None
        self.settings = {}
        self.i18n = _NoOpI18n()
        self.announcements = []

    def _find_api_ffmpeg(self):
        return "ffmpeg.exe"

    def output(self, text, interrupt=False):
        self.announcements.append((text, interrupt))


class _NoOpI18n:
    def t(self, key):
        return key


def test_missing_camera_does_not_end_or_clear_video_call(monkeypatch):
    import core.call_video as call_video
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

    class MissingCameraCapture:
        def __init__(self, _ffmpeg, _send_frame, transmit=True):
            self.stopped = False

        def start(self, _preferred_name=""):
            raise RuntimeError("No camera found")

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(call_video, "CameraCapture", MissingCameraCapture)
    stub = _NoCameraMainWindow()
    active_before = stub._active_voice_call

    assert stub._start_call_camera() is False
    assert stub._active_voice_call is active_before
    assert stub._active_voice_call["is_video"] is True
    assert stub._call_camera_capture is None
    assert stub._call_camera_available is False
    assert stub._call_camera_enabled is False
    # Default announce_failure=True: the caller asked for video, so a camera
    # failure is worth speaking.
    assert stub.announcements == [("call_video_no_camera_error", True)]


def test_missing_camera_stays_silent_when_announce_failure_is_false(monkeypatch):
    import core.call_video as call_video
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))

    class MissingCameraCapture:
        def __init__(self, _ffmpeg, _send_frame, transmit=True):
            pass

        def start(self, _preferred_name=""):
            raise RuntimeError("No camera found")

        def stop(self):
            pass

    monkeypatch.setattr(call_video, "CameraCapture", MissingCameraCapture)
    stub = _NoCameraMainWindow()

    assert stub._start_call_camera(announce_failure=False) is False
    # The "answer without video" probe deliberately suppresses this: the user
    # never asked for the camera, so there is nothing to complain about.
    assert stub.announcements == []


def test_start_call_camera_passes_transmit_through_to_camera_capture(monkeypatch):
    import core.call_video as call_video
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    captured_transmit = []

    class RecordingCapture:
        def __init__(self, _ffmpeg, _send_frame, transmit=True):
            captured_transmit.append(transmit)

        def start(self, _preferred_name=""):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(call_video, "CameraCapture", RecordingCapture)
    stub = _NoCameraMainWindow()

    assert stub._start_call_camera(announce_failure=False, transmit=False) is True
    # "Answer without video" probes availability without ever letting a real
    # frame reach send_frame — the flag reaches CameraCapture, not just
    # main.py's own bookkeeping.
    assert captured_transmit == [False]

    stub2 = _NoCameraMainWindow()
    assert stub2._start_call_camera() is True
    assert captured_transmit == [False, True]


class _RemoteVideoMainWindow:
    on_call_remote_video = MainWindow.on_call_remote_video
    _show_call_remote_video = MainWindow._show_call_remote_video

    def __init__(self, is_video):
        self._active_voice_call = {"identity": "call-1", "is_video": is_video}
        self._call_remote_video_gate_blocked = 0


def test_remote_video_blocked_by_is_video_gate_is_counted(monkeypatch):
    import wx
    scheduled = []
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *args: scheduled.append((fn, args)))
    stub = _RemoteVideoMainWindow(is_video=False)

    stub.on_call_remote_video(b"jpeg-bytes")

    assert scheduled == []
    assert stub._call_remote_video_gate_blocked == 1


def test_remote_video_passes_gate_and_schedules_render(monkeypatch):
    import wx
    scheduled = []
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *args: scheduled.append((fn, args)))
    stub = _RemoteVideoMainWindow(is_video=True)

    stub.on_call_remote_video(b"jpeg-bytes")

    assert scheduled == [(stub._show_call_remote_video, (b"jpeg-bytes",))]
    assert stub._call_remote_video_gate_blocked == 0


def test_call_window_video_toggle_is_hidden_without_camera():
    source = (Path(__file__).parents[1] / 'client' / 'main.py').read_text(encoding='utf-8')
    assert 'self.voice_call_window_video_button.Hide()' in source
    assert 'video_button.Show(is_video and local_camera_available)' in source
    assert 'self.voice_call_window_video_button.Bind(wx.EVT_BUTTON, self.toggle_call_video)' in source
    assert 'def _stop_call_camera(self, *, reset_availability: bool = False):' in source


class _ToggleVideoMainWindow:
    toggle_call_video = MainWindow.toggle_call_video

    def __init__(self, *, is_video, camera_available):
        self._active_voice_call = {"is_video": is_video}
        self._call_camera_available = camera_available
        self._call_camera_capture = None
        self.i18n = _NoOpI18n()
        self.announcements = []
        self.started_threads = 0

    def output(self, text, interrupt=False):
        self.announcements.append((text, interrupt))


def test_toggle_call_video_announces_error_when_no_camera_is_available():
    stub = _ToggleVideoMainWindow(is_video=True, camera_available=None)

    stub.toggle_call_video()

    # Runs on the UI thread already (a button/menu handler), so this is
    # spoken directly, no wx.CallAfter needed.
    assert stub.announcements == [("call_video_no_camera_error", True)]


def test_toggle_call_video_does_nothing_silently_on_a_voice_only_call():
    stub = _ToggleVideoMainWindow(is_video=False, camera_available=None)

    stub.toggle_call_video()

    # The toggle button is hidden for a voice-only call, so this path
    # shouldn't normally be reached — and if it is, there is still nothing
    # camera-related to complain about.
    assert stub.announcements == []
