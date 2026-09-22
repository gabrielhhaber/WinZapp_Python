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
    def send_call_camera_frame(self, _frame, epoch=None):
        pass


class _NoCameraMainWindow:
    _start_call_camera = MainWindow._start_call_camera
    _next_call_camera_epoch = MainWindow._next_call_camera_epoch
    _send_call_camera_stop = MainWindow._send_call_camera_stop

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
    assert 'def _stop_call_camera(self, *, reset_availability: bool = False, native: bool = False):' in source


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


_MAIN_SRC = (Path(__file__).parents[1] / "client" / "main.py").read_text(encoding="utf-8")


def _body_between(source, start_marker, end_marker):
    start = source.index(start_marker)
    return source[start:source.index(end_marker, start)]


def test_camera_starts_after_the_accept_post_not_before_it():
    """REGRESSION: the camera was opened BEFORE the accept POST. Enumerating
    DirectShow devices (a 10 s-timeout ffmpeg probe) plus waiting for the first
    frame therefore sat between the user pressing Answer -- with the ring tone
    already stopped -- and the caller being answered at all. A blind user got
    seconds of total silence, and "answer without video" paid the same cost.

    The camera is a local capability, never a precondition: a video call still
    works with no camera, so it belongs after the peer has been answered.
    """
    body = _body_between(
        _MAIN_SRC,
        "    def accept_incoming_call(self",
        "    def reject_incoming_call(self",
    )
    accept_post = body.index('self._post_call_control("accept", payload)')
    camera_start = body.index("self._start_call_camera(")
    assert accept_post < camera_start


def test_camera_starts_after_the_offer_post_on_an_outgoing_video_call():
    """Same ordering on the dialling side: enumerating the camera must not sit
    between pressing "video call" and the offer actually being placed."""
    body = _body_between(
        _MAIN_SRC,
        "    def _start_individual_call(self",
        "    def _sync_voice_call_bar(self",
    )
    offer_post = body.index('"offer",')
    camera_start = body.index("self._start_call_camera()")
    assert offer_post < camera_start


class _StopCameraWs(_NoCameraWs):
    def __init__(self):
        self.camera_stops = 0
        self.stopped_epochs = []
        self.native_stops = []
        self.camera_starts = 0

    def send_call_camera_stop(self, epoch=None, native=False):
        self.camera_stops += 1
        self.stopped_epochs.append(epoch)
        self.native_stops.append(native)

    def send_call_camera_start(self):
        self.camera_starts += 1


class _StopCameraMainWindow(_NoCameraMainWindow):
    _stop_call_camera = MainWindow._stop_call_camera

    def __init__(self):
        super().__init__()
        self.ws = _StopCameraWs()

    def _sync_voice_call_bar(self):
        pass


class _RecordingCapture:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_stopping_the_camera_tells_the_page_to_stop_transmitting(monkeypatch):
    """REGRESSION: killing ffmpeg only stops US sending. The page draws our
    frames onto a canvas and hands WhatsApp a captureStream() of it, which
    keeps emitting whatever the canvas last held at 10 fps -- so the peer went
    on seeing a frozen picture of the user for the rest of the call, and into
    the next one, while WinZapp announced video was off."""
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    stub = _StopCameraMainWindow()
    capture = _RecordingCapture()
    stub._call_camera_capture = capture

    stub._stop_call_camera()

    assert capture.stopped is True
    assert stub.ws.camera_stops == 1
    assert stub._call_camera_enabled is False


def test_stopping_a_camera_that_was_never_started_does_not_blank_the_page(monkeypatch):
    """A no-op stop must not reach the page: blanking a canvas this call never
    drew on would be pointless work, and _stop_call_camera() runs on teardown
    paths that fire whether or not video was ever on."""
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    stub = _StopCameraMainWindow()
    stub._call_camera_capture = None

    stub._stop_call_camera()

    assert stub.ws.camera_stops == 0


def test_a_camera_that_finished_opening_after_the_call_ended_is_discarded(monkeypatch):
    """REGRESSION: the camera now opens OUTSIDE _call_action_lock (holding it
    across the open blocked hang-up), so the call can end while we are in
    there. The guard tested truthiness, and _stop_voice_call_audio()'s grace
    path returns WITHOUT clearing _active_voice_call -- so the webcam lit up
    seconds after the user hung up. Worse, starting another call in that
    window made the guard pass and overwrote the new call's capture, leaking
    the old ffmpeg process with the webcam still open."""
    import core.call_video as call_video
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    stub = _StopCameraMainWindow()
    started = []

    class ReplacingCapture:
        def __init__(self, _ffmpeg, _send_frame, transmit=True):
            self.stopped = False

        def start(self, _preferred_name=""):
            # The user hung up and dialled again while the camera was opening:
            # a DIFFERENT dict, so truthiness alone would have let this pass.
            stub._active_voice_call = {"identity": "call-2", "is_video": True}
            started.append(self)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(call_video, "CameraCapture", ReplacingCapture)

    assert stub._start_call_camera() is False
    assert started[0].stopped is True
    assert stub._call_camera_capture is None
    # The discarded capture was already sending while start() blocked, so its
    # epoch must be stopped on the page too -- otherwise its in-flight frames
    # land after the call's reset() and leave the user's picture on the canvas
    # for the next call to inherit.
    assert stub.ws.camera_stops == 1
    assert isinstance(stub.ws.stopped_epochs[0], int)


class _NoThread:
    def start(self):
        pass


class _SelfChatMainWindow:
    _start_individual_call = MainWindow._start_individual_call
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self):
        self._active_voice_call = None
        self._active_incoming_calls = {}
        self.i18n = _NoOpI18n()
        self.announcements = []
        self.dialled = []

    def _is_self_jid(self, jid):
        return jid == "5511999999999@s.whatsapp.net"

    def output(self, text, interrupt=False):
        self.announcements.append((text, interrupt))

    def _preview_sender_from_jid(self, jid):
        return "Eu"

    def _sync_voice_call_bar(self):
        pass

    def _call_action_lock(self):
        pass


def test_the_self_chat_cannot_be_called_from_any_path():
    """REGRESSION: hiding the call buttons in the self-chat covers the mouse
    and the Tab order, but the accelerators reach _on_voice_call() directly.
    From the message list of "Me" a shortcut still spoke "calling Me..." and
    POSTed an offer to the user's own JID -- and with the button hidden, a
    blind user had no way to know the action even existed there."""
    stub = _SelfChatMainWindow()

    stub._start_individual_call("5511999999999@s.whatsapp.net", "Eu", is_video=False)

    assert stub.announcements == [("voice_call_individual_only", True)]
    assert stub._active_voice_call is None


def test_an_ordinary_contact_is_still_callable(monkeypatch):
    """The guard must not catch everyone: only the self-chat and the kinds
    that were already refused."""
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: None)
    # The dialling itself happens on a worker thread that would POST to a
    # server that is not running here; this test is about the guard, so the
    # thread is never started.
    monkeypatch.setattr("main.threading.Thread", lambda *a, **kw: _NoThread())
    stub = _SelfChatMainWindow()

    stub._start_individual_call("5511888888888@s.whatsapp.net", "Fulano", is_video=True)

    assert stub._active_voice_call is not None
    assert stub._active_voice_call["is_video"] is True


def test_the_camera_is_refused_when_there_is_no_call_at_all(monkeypatch):
    """REGRESSION: the post-open guard compares the call by identity, and
    None-is-None passed as "same call". A camera opened after the call's grace
    cleanup had already run therefore stayed on and kept transmitting with no
    call at all -- a live webcam a blind user has no way to notice."""
    import core.call_video as call_video
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    stub = _StopCameraMainWindow()
    stub._active_voice_call = None
    opened = []

    class OpeningCapture:
        def __init__(self, _ffmpeg, _send_frame, transmit=True):
            pass

        def start(self, _preferred_name=""):
            opened.append(True)

        def stop(self):
            pass

    monkeypatch.setattr(call_video, "CameraCapture", OpeningCapture)

    assert stub._start_call_camera() is False
    assert opened == []  # never even opened the device
    assert stub._call_camera_capture is None


def test_each_capture_gets_a_strictly_increasing_epoch():
    """The page drops frames whose epoch it has seen stopped, so a new capture
    must never reuse or undercut an old epoch -- not even two captures started
    within the same millisecond."""
    stub = _NoCameraMainWindow()
    epochs = [stub._next_call_camera_epoch() for _ in range(50)]
    assert epochs == sorted(set(epochs))


def test_stopping_the_camera_names_the_epoch_it_stops(monkeypatch):
    """The stop has to name the capture it ends, so the page can tell a frame
    that raced the stop (same epoch -- drop it) from the first frame of a
    capture started afterwards (newer epoch -- draw it)."""
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    stub = _StopCameraMainWindow()
    stub._call_camera_capture = _RecordingCapture()
    stub._call_camera_epoch = 1234

    stub._stop_call_camera()

    assert stub.ws.stopped_epochs == [1234]


def test_frames_carry_the_epoch_of_their_capture(monkeypatch):
    import core.call_video as call_video
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    sent = []

    class Ws(_StopCameraWs):
        def send_call_camera_frame(self, frame, epoch=None):
            sent.append((frame, epoch))

    class SendingCapture:
        def __init__(self, _ffmpeg, send_frame, transmit=True):
            self.send_frame = send_frame

        def start(self, _preferred_name=""):
            self.send_frame(b"jpeg")

        def stop(self):
            pass

    monkeypatch.setattr(call_video, "CameraCapture", SendingCapture)
    stub = _StopCameraMainWindow()
    stub.ws = Ws()

    assert stub._start_call_camera() is True
    assert sent == [(b"jpeg", stub._call_camera_epoch)]


class _ToggleMainWindow(_StopCameraMainWindow):
    toggle_call_video = MainWindow.toggle_call_video
    _resume_call_camera = MainWindow._resume_call_camera

    def __init__(self):
        super().__init__()
        self._call_camera_available = True
        self.started = []

    def _start_call_camera(self, **kwargs):
        self.started.append(kwargs)
        return self.start_result


def test_turning_video_off_from_the_call_window_tells_whatsapps_engine(monkeypatch):
    """REGRESSION (live video call, 2026-09-21): WhatsApp Web transmits our
    camera through its own WASM call engine -- a sender report showed no video
    sender on any RTCPeerConnection. Blanking the canvas alone left that engine
    treating the camera as off after the picture went black, and it never came
    back. Video off must go through the engine's own camera toggle."""
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    stub = _ToggleMainWindow()
    stub._call_camera_capture = _RecordingCapture()
    stub._call_camera_epoch = 77

    stub.toggle_call_video()

    assert stub.ws.native_stops == [True]
    assert stub.ws.stopped_epochs == [77]


def test_a_teardown_stop_only_blanks_and_never_touches_the_engine(monkeypatch):
    """Call end or a discarded capture is not the user asking for video off:
    only the page is blanked."""
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    stub = _StopCameraMainWindow()
    stub._call_camera_capture = _RecordingCapture()

    stub._stop_call_camera(reset_availability=True)

    assert stub.ws.native_stops == [False]


def test_turning_video_back_on_resumes_the_engine_once_capture_runs():
    stub = _ToggleMainWindow()
    stub.start_result = True

    stub._resume_call_camera()

    assert stub.ws.camera_starts == 1


def test_a_failed_restart_never_resumes_the_engine_onto_an_empty_canvas():
    stub = _ToggleMainWindow()
    stub.start_result = False

    stub._resume_call_camera()

    assert stub.ws.camera_starts == 0


class _Recorder:
    """Records every call made on it, in order, into a shared log."""

    def __init__(self, name, log, shown=False):
        self._name, self._log, self._shown = name, log, shown

    def __getattr__(self, attr):
        def call(*args, **kwargs):
            self._log.append((self._name, attr, args))
            if attr == "IsShown":
                return self._shown
            if attr == "Show":
                self._shown = bool(args[0]) if args else True
            return None
        return call


class _CallWindowStub:
    _sync_voice_call_bar = MainWindow._sync_voice_call_bar

    def __init__(self, *, is_video, camera):
        self.log = []
        self.i18n = _NoOpI18n()
        self._active_voice_call = {"identity": "c1", "name": "Mae", "is_video": is_video}
        self._call_audio_session = None
        self._call_camera_available = True if camera else None
        self._call_camera_enabled = bool(camera)
        self.voice_call_window = _Recorder("window", self.log)
        self.voice_call_window_mute_button = _Recorder("mute", self.log)
        self.voice_call_window_video_button = _Recorder("video", self.log)
        self.voice_call_window_label = _Recorder("label", self.log)
        self.call_video_image = _Recorder("image", self.log)
        self._call_window_sizer = _Recorder("sizer", self.log)


def _calls(stub, name):
    return [(attr, args) for who, attr, args in stub.log if who == name]


def test_the_call_window_is_fitted_to_its_content_not_to_fixed_sizes():
    """REGRESSION (sighted-assistance description, 2026-09-21): the label and
    four buttons shared one fixed-width row, so "Video call: <name>." pushed
    "turn video off" past the window's right edge -- reachable with Tab,
    invisible on screen. The window is now fitted to what it shows."""
    stub = _CallWindowStub(is_video=True, camera=True)

    stub._sync_voice_call_bar()

    assert ("Fit", (stub.voice_call_window,)) in _calls(stub, "sizer")
    assert not any(attr == "SetSize" for attr, _ in _calls(stub, "window"))
    # The video toggle is shown on a video call with a camera.
    assert ("Show", (True,)) in _calls(stub, "video")


def test_the_fit_measures_the_name_actually_shown():
    """Fitting before SetLabel would size the window for the previous text."""
    stub = _CallWindowStub(is_video=True, camera=True)

    stub._sync_voice_call_bar()

    order = [(who, attr) for who, attr, _ in stub.log]
    assert order.index(("label", "SetLabel")) < order.index(("sizer", "Fit"))


def test_the_label_has_a_row_of_its_own_above_the_buttons():
    source = (Path(__file__).parents[1] / "client" / "main.py").read_text(encoding="utf-8")
    assert "call_sizer.Add(self.voice_call_window_label, 0, wx.EXPAND" in source
    assert "controls.Add(self.voice_call_window_label" not in source


class _JsonResponse:
    def __init__(self, body=None, *, broken=False):
        self._body = body
        self._broken = broken

    def json(self):
        if self._broken:
            raise ValueError("not json")
        return self._body


def test_call_response_route_names_the_native_path_and_state():
    body = {"status": "success", "response": {
        "handled": True, "via": "native-voip",
        "call": {"id": "C1", "peerJid": "5511999999999@c.us", "state": "INCOMING_RING"},
    }}
    route = MainWindow._call_response_route(_JsonResponse(body))
    assert route == "native-voip state=INCOMING_RING"
    # the peer JID in the same body never makes it into the log line
    assert "5511" not in route


def test_call_response_route_reports_the_wa_js_fallback():
    assert MainWindow._call_response_route(
        _JsonResponse({"status": "success", "response": True})
    ) == "wa-js"
    assert MainWindow._call_response_route(
        _JsonResponse({"status": "success", "response": {"id": "C1"}})
    ) == "wa-js"


def test_call_response_route_survives_a_body_that_is_not_json():
    assert MainWindow._call_response_route(_JsonResponse(broken=True)) == "unknown"


def test_reject_logs_the_request_before_waiting_for_the_call_lock():
    body = _body_between(
        _MAIN_SRC,
        "    def reject_incoming_call(self",
        "    def end_active_call(self",
    )
    requested = body.index('"[call] reject requested')
    lock = body.index("with self._call_action_lock:")
    answered = body.index('"[call] reject answered via %s"')
    assert requested < lock < answered


def test_a_second_video_on_press_while_the_camera_opens_starts_nothing(monkeypatch):
    """Review finding: two presses while DirectShow was still opening started
    two captures; with a camera that allows two readers, the first ffmpeg
    kept running with the webcam light on and nothing left to stop it."""
    threads = []

    class _HeldThread:
        def __init__(self, target=None, daemon=None, **_kw):
            self.target = target

        def start(self):
            threads.append(self.target)

    monkeypatch.setattr("main.threading.Thread", _HeldThread)
    stub = _ToggleMainWindow()
    stub.start_result = True

    stub.toggle_call_video()
    stub.toggle_call_video()            # camera still opening
    assert len(threads) == 1

    threads[0]()                        # the first open finishes
    assert stub.started == [{}]
    assert stub._call_camera_resuming is False


def test_a_failed_open_lets_the_user_try_video_on_again(monkeypatch):
    stub = _ToggleMainWindow()
    stub.start_result = False
    stub._call_camera_resuming = True

    stub._resume_call_camera()

    assert stub._call_camera_resuming is False
