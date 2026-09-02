"""Regression coverage for ConversationsPanel._start_voice_recording()'s
background stream open (_bg_open_stream) and how its failures reach the user.

Opening the PyAudio input stream moved off the UI thread because pa.open()
can block for seconds negotiating with the driver. That handed the method a
new failure mode: _recording_starting is set to True *before* the thread
starts and only _on_stream_opened() ever sets it back to False, so anything
that stops _on_stream_opened() from being scheduled leaves the flag stuck.
on_record_voice_message() gates on `elif not self._recording_starting`, so a
stuck flag means the record button silently stops responding for the rest of
the session — no dialog, no sound, and (in a daemon thread) no traceback
anywhere the user can see.

find_input_device_index() is the realistic raiser: core.audio_devices'
_pyaudio_input_devices() falls back to get_default_host_api_info() when the
WASAPI query fails, which is exactly the kind of broken audio stack the
background open exists to survive in the first place.

The second half of this file covers the outcome once the open genuinely
fails: it must be *announced*. That path used to return in silence unless a
pinned device had failed, which is not the default configuration.

ConversationsPanel is a wx.Panel and can't be instantiated without a running
wx.App, so the method under test is bound onto a plain stub carrying only the
attributes it touches, matching the pattern used throughout this suite (see
test_voice_recording_unavailable.py).
"""

import threading
import types

import pytest

import ui.conversations as conversations_module
from ui.conversations import ConversationsPanel


class _FakeI18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self, device_name="Microfone USB"):
        self.i18n = _FakeI18n()
        self.app_name = "WinZapp"
        self.output_calls = []
        # Non-empty so _bg_open_stream() actually calls
        # find_input_device_index() — the raiser under test.
        self.effective_input_device_name = device_name
        # Only touched once an open succeeds and the panel is armed.
        self.voicemsg_startrecording_sound = types.SimpleNamespace(play=lambda: None)
        self.settings = {"user_interface": {"voice_record_focus": "send"}}
        self.recording_status_calls = []

    def output(self, text):
        self.output_calls.append(text)

    def send_recording_status(self, jid, on, is_group):
        self.recording_status_calls.append((jid, on, is_group))


class _FakeWidget:
    """Every wx control _on_stream_opened() touches once it decides to arm
    the recording UI. None of them assert anything; they exist so the method
    can run to completion off a plain stub."""

    def Hide(self):
        pass

    def Show(self, show=True):
        pass

    def Layout(self):
        pass

    def SetLabel(self, _text):
        pass

    def SetFocus(self):
        pass


class _FakeStream:
    def __init__(self):
        self.closed = False

    def start_stream(self):
        pass

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class _Stub:
    _start_voice_recording = ConversationsPanel._start_voice_recording
    _silence_send_voice_focus_if_enabled = (
        ConversationsPanel._silence_send_voice_focus_if_enabled
    )
    _voice_recording_silence_enabled = (
        ConversationsPanel._voice_recording_silence_enabled
    )
    _focus_recording_button_silently = (
        ConversationsPanel._focus_recording_button_silently
    )

    def __init__(self):
        self.conversation = {"remoteJid": "5511999999999@s.whatsapp.net"}
        self.main_window = _FakeMainWindow()
        # Not None, so the method skips constructing a real pyaudio.PyAudio()
        # (which would talk to the actual audio hardware).
        self._recording_pa = object()
        self._recording_starting = False
        self._recording_open_token = 0
        self._is_recording = False
        self._recording_stream = None
        self.settings = {"user_interface": {"voice_record_focus": "send"}}
        for name in ("message_field", "send_message_btn", "record_voice_message_btn",
                     "_add_attachment_btn", "_voice_panel", "conversation_panel",
                     "_pause_resume_btn", "_send_voice_btn", "_discard_voice_btn"):
            setattr(self, name, _FakeWidget())


# Filled by the `scheduled` fixture's wx.MessageBox stand-in. Kept module-level
# so the fixture's return shape stays (calls, fired) for the tests that predate
# any dialog being shown on this path at all.
_BOXES: list = []


@pytest.fixture
def scheduled(monkeypatch):
    """Capture wx.CallAfter(...) instead of dispatching it, and expose an
    Event so the test can wait for the background thread deterministically.
    wx.MessageBox is captured into _BOXES for the same reason: there is no
    wx.App here, and a real modal dialog would hang the run."""
    calls = []
    fired = threading.Event()
    _BOXES.clear()

    def _fake_call_after(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        fired.set()

    monkeypatch.setattr(conversations_module.wx, "CallAfter", _fake_call_after)
    monkeypatch.setattr(conversations_module.wx, "MessageBox",
                        lambda *args, **kwargs: _BOXES.append(args))
    # A stand-in for the real module: present (so the method doesn't take the
    # "PyAudio not installed" early return) but never actually touched,
    # because find_input_device_index() is stubbed in every test below.
    monkeypatch.setattr(
        conversations_module,
        "pyaudio",
        types.SimpleNamespace(paInt16=8, paContinue=0),
    )
    monkeypatch.setattr(conversations_module, "find_input_device_index",
                        lambda *a, **kw: None)
    return calls, fired


def _boom(*_args, **_kwargs):
    raise OSError("no default host API")


def _dispatch(calls, *override):
    """Run what wx would have run on the main thread, optionally overriding
    the arguments the background thread produced."""
    func, args, _kwargs = calls[0]
    func(*(override or args))


class TestCallbackAlwaysRuns:
    """_on_stream_opened() is the only thing that clears _recording_starting,
    so it has to be reached no matter how _bg_open_stream() ends."""

    def test_stream_open_failure_still_schedules_the_callback(self, scheduled, monkeypatch):
        calls, fired = scheduled
        monkeypatch.setattr(conversations_module, "find_input_device_index", _boom)

        stub = _Stub()
        stub._start_voice_recording()

        assert fired.wait(timeout=5), "background thread never scheduled _on_stream_opened"
        assert len(calls) == 1
        _func, args, _kwargs = calls[0]
        stream, rate, ch, fell_back = args
        # Nothing opened, and no device fallback was reached before the raise.
        assert stream is None
        assert rate is None
        assert ch is None
        assert fell_back is False

    def test_recording_flag_is_released_after_a_failed_open(self, scheduled, monkeypatch):
        """The whole point of the callback still running: the flag it clears is
        what on_record_voice_message() checks before allowing another attempt."""
        calls, fired = scheduled
        monkeypatch.setattr(conversations_module, "find_input_device_index", _boom)

        stub = _Stub()
        stub._start_voice_recording()
        assert stub._recording_starting is True, "flag should be set while opening"

        assert fired.wait(timeout=5)
        _dispatch(calls)

        assert stub._recording_starting is False
        # A failed open must not leave the panel believing it is recording.
        assert stub._is_recording is False
        assert stub._recording_stream is None

    def test_record_button_still_responds_after_a_failed_open(self, scheduled, monkeypatch):
        """End-to-end guard on the actual user-visible symptom: the second press
        of the record button must reach _start_voice_recording() again."""
        calls, fired = scheduled
        monkeypatch.setattr(conversations_module, "find_input_device_index", _boom)

        stub = _Stub()
        stub._start_voice_recording()
        assert fired.wait(timeout=5)
        _dispatch(calls)

        # Replay on_record_voice_message()'s own guard rather than trusting the
        # flag by name: this is the condition that used to be permanently False.
        reached = []
        stub._start_voice_recording = lambda: reached.append(True)
        if stub._is_recording:
            pass
        elif not stub._recording_starting:
            stub._start_voice_recording()

        assert reached == [True], "record button stayed dead after a failed open"


class TestFailedOpenIsAnnounced:
    """A microphone that cannot be opened used to be reported to the user only
    when a *pinned* device had failed — and pinning one in Settings is not the
    default state. With none pinned (input_device_name: ""), fell_back stays
    False and the failure path returned in silence: no dialog, no sound,
    nothing in log.log.

    Reported live against an H510-PRO headset whose every sample-rate/channel
    combo PortAudio rejected with -9999: "I press record and nothing happens at
    all". For a screen-reader-first app that is the worst outcome available —
    there is no visual cue either, so nothing tells the user whether the app,
    the shortcut or the microphone is at fault. StatusPanel already warned in
    this same situation; the two panels disagreed, and the busier one was the
    silent one.
    """

    def test_open_failure_with_no_pinned_device_still_warns(self, scheduled):
        calls, fired = scheduled

        stub = _Stub()
        # No device pinned — the default state, and the one that used to be
        # silent because fell_back can never become True without one.
        stub.main_window.effective_input_device_name = ""
        stub._start_voice_recording()
        assert fired.wait(timeout=5)
        _dispatch(calls)

        assert [b[0] for b in _BOXES] == ["voice_recording_device_failed"], \
            "open failure was reported to nobody"
        assert stub._recording_starting is False
        assert stub._is_recording is False

    def test_exception_path_is_announced_too(self, scheduled, monkeypatch):
        """The raise-in-the-thread case ends in the same stream=None callback,
        so it must reach the user as well, not just the log."""
        calls, fired = scheduled
        monkeypatch.setattr(conversations_module, "find_input_device_index", _boom)

        stub = _Stub()
        stub._start_voice_recording()
        assert fired.wait(timeout=5)
        _dispatch(calls)

        assert [b[0] for b in _BOXES] == ["voice_recording_device_failed"]

    def test_a_failed_fallback_reports_once_not_twice(self, scheduled):
        """fell_back means "the pinned device failed, trying the default". If
        the default failed too, recording never started — saying "falling back
        to the default" and then "could not open the mic" stacks two dialogs,
        and the first is misleading on its own."""
        calls, fired = scheduled

        stub = _Stub()
        stub._start_voice_recording()
        assert fired.wait(timeout=5)
        _dispatch(calls, None, None, None, True)      # stream=None, fell_back=True

        assert [b[0] for b in _BOXES] == ["voice_recording_device_failed"]
        # The broken pinned device is still dropped for the rest of the session.
        assert stub.main_window.effective_input_device_name == ""

    def test_a_successful_fallback_still_reports_the_broken_device(self, scheduled):
        """The opposite guard: when the fallback works, the user must still be
        told their pinned device is gone. That message is not collateral of the
        new one."""
        calls, fired = scheduled

        stub = _Stub()
        stub._start_voice_recording()
        assert fired.wait(timeout=5)
        _dispatch(calls, _FakeStream(), 48000, 1, True)   # fallback succeeded

        assert [b[0] for b in _BOXES] == ["audio_device_failed_input"]
        assert stub._is_recording is True
        assert stub._recording_starting is False


class _FakeSelectivePyAudio:
    """A PyAudio stand-in where exactly one device index can be opened and
    every other attempt raises -9999, mirroring a machine whose default host
    API refuses the microphone that a different host API accepts. Records the
    index of every attempt, in order, so a test can assert what was tried and
    in which sequence."""

    def __init__(self, working_index=None):
        self.working_index = working_index
        self.opened_indices = []

    def open(self, **kwargs):
        idx = kwargs.get("input_device_index")
        self.opened_indices.append(idx)
        if self.working_index is None or idx != self.working_index:
            raise OSError(-9999, "Unanticipated host error")
        return _FakeStream()


class TestHostApiFallback:
    """_try_open(None) asks PortAudio for the default device of its *default*
    host API — MME on Windows — which is not the handle set
    enumerate_input_devices() reads (WASAPI). One host API refusing a
    microphone establishes nothing about the others, so giving up after the
    default failed could abandon recording for the session while a working
    path to the same microphone sat one index away, never attempted.
    """

    def test_a_failed_default_falls_back_to_an_enumerated_device(self, scheduled, monkeypatch):
        calls, fired = scheduled
        monkeypatch.setattr(conversations_module, "fallback_input_device_indices",
                            lambda pa, exclude=(): [7])

        stub = _Stub()
        stub.main_window.effective_input_device_name = ""   # nothing pinned
        stub._recording_pa = _FakeSelectivePyAudio(working_index=7)
        stub._start_voice_recording()
        assert fired.wait(timeout=5)
        _dispatch(calls)

        # The system default is still preferred and still tried first.
        assert stub._recording_pa.opened_indices[0] is None
        assert 7 in stub._recording_pa.opened_indices
        assert stub._is_recording is True
        assert stub._recording_starting is False
        assert _BOXES == [], "recording started — there was nothing to warn about"

    def test_the_pinned_device_is_not_tried_a_second_time(self, scheduled, monkeypatch):
        """It already failed as the first attempt; re-opening it would spend
        another full round of driver negotiation on a known answer."""
        calls, fired = scheduled
        seen = {}

        def _candidates(pa, exclude=()):
            seen["exclude"] = exclude
            return []

        monkeypatch.setattr(conversations_module, "find_input_device_index", lambda *a, **kw: 3)
        monkeypatch.setattr(conversations_module, "fallback_input_device_indices", _candidates)

        stub = _Stub()
        stub._start_voice_recording()
        assert fired.wait(timeout=5)
        _dispatch(calls)

        assert 3 in seen["exclude"]

    def test_every_candidate_failing_still_warns(self, scheduled, monkeypatch):
        """Non-regression guard on the fix this branch already carries: the
        fallback adds attempts, it must not swallow the announcement when all
        of them fail."""
        calls, fired = scheduled
        monkeypatch.setattr(conversations_module, "fallback_input_device_indices",
                            lambda pa, exclude=(): [7, 9])

        stub = _Stub()
        stub.main_window.effective_input_device_name = ""
        stub._recording_pa = _FakeSelectivePyAudio(working_index=None)
        stub._start_voice_recording()
        assert fired.wait(timeout=5)
        _dispatch(calls)

        assert 7 in stub._recording_pa.opened_indices
        assert 9 in stub._recording_pa.opened_indices
        assert [b[0] for b in _BOXES] == ["voice_recording_device_failed"]
        assert stub._is_recording is False
