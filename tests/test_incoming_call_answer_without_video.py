""""Answer without video" on an incoming video call.

The popup for an incoming call is shared between voice and video calls
(`IncomingCallDialog`) and used to offer a single "Answer" button regardless
of call type, with no way to answer a video call without the local camera
turning on. A video offer now gets a second button, "answer without video",
right after the relabelled "answer with video" button; a plain voice call is
unaffected.

The important part is what "without video" actually suppresses. The call
TYPE never changes — `_active_voice_call["is_video"]` stays whatever the
offer said, so remote video keeps arriving and the call window opens exactly
as for any other video call. The only WinZapp-side choice is whether the
local camera is left capturing right after accepting: normally it is (today's
unchanged behaviour); "answer without video" still probes the camera (so
`_call_camera_available` becomes True and the existing manual video-toggle
button, `toggle_call_video()`, works for the rest of the call) but leaves it
stopped, in exactly the state a manual toggle-off would.

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so `accept_incoming_call()` is exercised as a plain function bound
onto a stub, the same approach as tests/test_incoming_call_alert.py. The
dialog itself is a real wx.Dialog (its own construction is the point), so
those tests carry the `wxgui` marker like tests/test_private_chat_data_dialog.py.
"""

import threading

import pytest

from main import MainWindow
from tests.conftest import hidden_frame


class _I18n:
    def t(self, key):
        return {
            "voice_call_already_active": "Já em ligação.",
            "incoming_call_answered": "Ligação atendida.",
            "incoming_call_answer_failed": "Falha ao atender: {error}",
            "incoming_call_popup_title": "Chamada recebida",
            "incoming_call_answer_button": "&Answer",
            "incoming_call_answer_with_video_button": "&Answer with video",
            "incoming_call_answer_without_video_button": "Answer without &video",
            "incoming_call_reject_button": "&Reject",
            "incoming_call_silence_button": "&Silence alert",
            "incoming_call_close_button": "&Close window",
        }.get(key, key)


class _MainStub:
    accept_incoming_call = MainWindow.accept_incoming_call
    stop_incoming_call_alert = MainWindow.stop_incoming_call_alert
    _call_control_payload = MainWindow._call_control_payload
    _close_incoming_call_dialog = MainWindow._close_incoming_call_dialog
    _stop_call_camera = MainWindow._stop_call_camera
    _probe_call_camera = MainWindow._probe_call_camera

    def __init__(self):
        self._active_incoming_calls = {}
        self._incoming_call_details = {}
        self._incoming_call_dialogs = {}
        self._active_voice_call = None
        self._call_camera_available = None
        self._call_camera_capture = None
        self._call_camera_enabled = False
        self._call_action_lock = threading.Lock()
        self.i18n = _I18n()
        self.announcements = []
        self.camera_starts = 0
        self.camera_announce_failure_values = []
        self.camera_transmit_values = []
        self.audio_starts = []
        self.posted_controls = []

    # -- collaborators accept_incoming_call() reaches into, stubbed out so
    # this test is only about the video/camera decision, not HTTP or audio.
    def _cancel_incoming_call_watchdog(self, identity):
        pass

    def _sync_incoming_call_bar(self):
        pass

    def _sync_voice_call_bar(self):
        pass

    def _start_call_camera(self, *, announce_failure=True, transmit=True):
        self.camera_starts += 1
        self.camera_announce_failure_values.append(announce_failure)
        self.camera_transmit_values.append(transmit)
        self._call_camera_available = True
        self._call_camera_capture = object()
        self._call_camera_enabled = True
        return True  # like the real one once the capture is running

    def _start_voice_call_audio(self, identity, details):
        self.audio_starts.append(identity)

    def _post_call_control(self, endpoint, payload, *, timeout=15):
        self.posted_controls.append((endpoint, payload))
        return object()

    def _raise_for_call_response(self, response, action):
        return response

    def _call_error_text(self, error):
        return str(error)

    def output(self, text, interrupt=False):
        self.announcements.append((text, interrupt))


@pytest.fixture(autouse=True)
def _no_wx(monkeypatch):
    monkeypatch.setattr("main.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


@pytest.fixture(autouse=True)
def _inline_threads(monkeypatch):
    class _InlineThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr("main.threading.Thread", _InlineThread)


def _video_offer_stub():
    stub = _MainStub()
    stub._incoming_call_details["call-1"] = {
        "call_id": "call-1",
        "peer_jid": "5511999999999@s.whatsapp.net",
        "name": "Fulano",
        "is_video": True,
    }
    stub._active_incoming_calls["call-1"] = "5511999999999@s.whatsapp.net"
    return stub


def test_default_answer_on_a_video_call_starts_and_leaves_the_camera_on():
    """Regression guard: calling accept_incoming_call() the way the ordinary
    answer button always has (no with_video argument) must behave exactly
    as before this change."""
    stub = _video_offer_stub()

    stub.accept_incoming_call("call-1")

    assert stub._active_voice_call["is_video"] is True
    assert stub.camera_starts == 1
    assert stub._call_camera_available is True
    assert stub._call_camera_capture is not None
    assert stub.audio_starts == ["call-1"]
    # The user asked for video, so a camera failure would be worth announcing,
    # and real frames should reach the peer as always.
    assert stub.camera_announce_failure_values == [True]
    assert stub.camera_transmit_values == [True]


def test_answer_without_video_keeps_the_call_a_video_call():
    stub = _video_offer_stub()

    stub.accept_incoming_call("call-1", with_video=False)

    # The offer's own type is untouched: remote video and the video window
    # still behave as for any other video call.
    assert stub._active_voice_call["is_video"] is True


def test_answer_without_video_probes_the_camera_but_leaves_it_off():
    stub = _video_offer_stub()

    stub.accept_incoming_call("call-1", with_video=False)

    # Probed once (so the manual video-toggle button becomes available and
    # functional for the rest of the call)...
    assert stub.camera_starts == 1
    assert stub._call_camera_available is True
    # ...but not left capturing/sending, same end state toggle_call_video()
    # leaves behind when the user turns their own video off mid-call.
    assert stub._call_camera_capture is None
    assert stub._call_camera_enabled is False
    # The user deliberately chose no video, so a camera failure here is not
    # an error worth speaking, and the probe must never transmit a real
    # frame to the peer — see _start_call_camera()'s ``transmit`` docstring.
    assert stub.camera_announce_failure_values == [False]
    assert stub.camera_transmit_values == [False]


def test_answer_without_video_on_a_voice_only_call_never_touches_the_camera():
    stub = _MainStub()
    stub._incoming_call_details["call-2"] = {
        "call_id": "call-2",
        "peer_jid": "5511888888888@s.whatsapp.net",
        "name": "Beltrana",
        "is_video": False,
    }
    stub._active_incoming_calls["call-2"] = "5511888888888@s.whatsapp.net"

    stub.accept_incoming_call("call-2", with_video=False)

    assert stub._active_voice_call["is_video"] is False
    assert stub.camera_starts == 0
    assert stub._call_camera_available is None


class _FakeParent:
    def __init__(self, i18n):
        self.i18n = i18n


@pytest.mark.wxgui
def test_video_call_dialog_offers_answer_with_and_without_video_buttons(wx_app):
    from ui.dialogs.incoming_call import IncomingCallDialog

    frame = hidden_frame()
    frame.i18n = _I18n()
    calls = {"answer": 0, "answer_without_video": 0}
    dialog = IncomingCallDialog(
        frame,
        "Fulano está te ligando por vídeo.",
        on_answer=lambda: calls.__setitem__("answer", calls["answer"] + 1),
        on_reject=lambda: None,
        on_stop=lambda: None,
        on_closed=lambda: None,
        is_video=True,
        on_answer_without_video=lambda: calls.__setitem__(
            "answer_without_video", calls["answer_without_video"] + 1
        ),
    )
    try:
        assert dialog._answer_button.GetLabelText() == "Answer with video"
        assert hasattr(dialog, "_answer_without_video_button")
        assert dialog._answer_without_video_button.GetLabelText() == "Answer without video"

        dialog._on_answer_without_video(None)
        assert calls["answer_without_video"] == 1
        assert calls["answer"] == 0
    finally:
        # _on_answer_without_video() already Destroy()ed the dialog itself
        # (IncomingCallDialog._finish_with()).
        frame.Destroy()


@pytest.mark.wxgui
def test_voice_call_dialog_keeps_the_single_answer_button(wx_app):
    from ui.dialogs.incoming_call import IncomingCallDialog

    frame = hidden_frame()
    frame.i18n = _I18n()
    dialog = IncomingCallDialog(
        frame,
        "Fulano está te ligando.",
        on_answer=lambda: None,
        on_reject=lambda: None,
        on_stop=lambda: None,
        on_closed=lambda: None,
    )
    try:
        assert dialog._answer_button.GetLabelText() == "Answer"
        assert not hasattr(dialog, "_answer_without_video_button")
        dialog.refresh_labels()  # must not blow up looking for the second button
    finally:
        dialog.Destroy()
        frame.Destroy()
