"""The call window's Participants button is offered only when it can open.

Two ways it used to mislead: enabled for a call whose chat is locked, where
show_call_participants() refuses; and left disabled for a call adopted from
the page, because attaching Python's audio installed a fresh call record and
reset the ACTIVE state the button (and the promote-to-video button) needs.

MainWindow is a wx.Frame, so the methods run unbound against a plain stub;
no window is created.
"""
import threading
from types import SimpleNamespace

import pytest

from main_window import calls as calls_module
from main_window.calls import CallsMixin

PERSON = "5511999999999@s.whatsapp.net"


class _Control:
    def __init__(self):
        self.enabled = None
        self.shown = False

    def Enable(self, enable=True):
        self.enabled = bool(enable)

    def IsShown(self):
        return self.shown

    def Show(self, show=True):
        self.shown = bool(show)

    def Hide(self):
        self.shown = False

    def __getattr__(self, name):
        # SetLabel, SetTitle, Layout, Raise, SetFocus...: nothing to record.
        return lambda *args, **kwargs: None


class _Audio:
    def __init__(self):
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def set_microphone_muted(self, muted):
        pass


class _CallWindow:
    _sync_voice_call_bar = CallsMixin._sync_voice_call_bar
    _attach_audio_to_browser_call = CallsMixin._attach_audio_to_browser_call
    _start_voice_call_audio = CallsMixin._start_voice_call_audio

    def __init__(self, *, locked=()):
        self.i18n = SimpleNamespace(t=lambda key: key)
        self.token = "tok"
        self._call_action_lock = threading.Lock()
        self._call_audio_session = None
        self._call_ring_audio_session = None
        self._active_voice_call = {"call_id": "C1", "peer_jid": PERSON, "is_video": False}
        self._voice_call_last_announced_state = "ACTIVE"
        self._locked = set(locked)
        self.voice_call_window = _Control()
        self.voice_call_window_participants_button = _Control()
        self.call_video_image = _Control()

    def is_chat_locked(self, jid):
        return jid in self._locked

    def _post_call_control(self, endpoint, payload, *, timeout=15):
        return SimpleNamespace(status_code=200)

    def _raise_for_call_response(self, response, action):
        return response


@pytest.fixture(autouse=True)
def _inline_call_after(monkeypatch):
    monkeypatch.setattr(calls_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


def test_mute_shortcut_announces_both_states_without_resyncing_the_call_window():
    audio = SimpleNamespace(microphone_muted=False)
    audio.set_microphone_muted = lambda muted: setattr(audio, "microphone_muted", muted)
    labels = []
    spoken = []
    window = SimpleNamespace(
        _call_audio_session=audio,
        voice_call_window_mute_button=SimpleNamespace(SetLabel=labels.append),
        i18n=SimpleNamespace(t=lambda key: key),
        output=lambda text, *, interrupt: spoken.append((text, interrupt)),
    )

    CallsMixin.toggle_call_microphone(window, object())
    assert audio.microphone_muted is True
    CallsMixin.toggle_call_microphone(window, object())
    assert audio.microphone_muted is False

    assert labels == ["voice_call_unmute_button", "voice_call_mute_button"]
    assert spoken == [
        ("voice_call_microphone_muted", True),
        ("voice_call_microphone_unmuted", True),
    ]


def test_outgoing_call_window_shows_and_announces_neonize_progress():
    window = _CallWindow()
    window._active_voice_call.update(outgoing=True, name="Peer", phase="CALLING")
    window._voice_call_last_announced_state = ""
    window._call_audio_session = _Audio()
    window._normalize_jid = lambda jid: jid
    window._chat_jids_equivalent = lambda left, right: left == right
    shown = []
    spoken = []
    window.i18n.t = {
        "voice_call_starting": "Calling {name}…",
        "voice_call_ringing": "Ringing {name}…",
        "voice_call_connecting": "Connecting to {name}…",
        "voice_call_active_label": "Voice call with {name}",
    }.get
    get_text = window.i18n.t
    window.i18n.t = lambda key: get_text(key) or key
    window.voice_call_window_label = SimpleNamespace(SetLabel=shown.append)
    window.output = lambda text, *, interrupt: spoken.append(text)

    window._sync_voice_call_bar()
    assert shown[-1] == "Calling Peer…"
    for phase, text in (
        ("RINGING", "Ringing Peer…"),
        ("CONNECTING", "Connecting to Peer…"),
        ("ACTIVE", "Voice call with Peer"),
    ):
        event = {"id": "C1", "peerJid": PERSON, "state": phase, "outgoing": True}
        CallsMixin.on_voice_call_state_event(window, event)
        CallsMixin.on_voice_call_state_event(window, event)
        assert shown[-1] == text
    assert spoken == ["Ringing Peer…", "Connecting to Peer…", "voice_call_connected"]


def test_a_connected_call_offers_participants():
    window = _CallWindow()
    window._sync_voice_call_bar()
    assert window.voice_call_window_participants_button.enabled is True


def test_a_locked_chats_call_never_offers_participants():
    window = _CallWindow(locked={PERSON})
    window._sync_voice_call_bar()
    assert window.voice_call_window_participants_button.enabled is False


def test_attaching_audio_to_an_adopted_call_keeps_it_connected():
    """on_voice_call_state_event() records ACTIVE before it starts the attach
    thread; the record _start_voice_call_audio() installs must not erase it."""
    window = _CallWindow()
    audio = window._call_ring_audio_session = _Audio()

    window._attach_audio_to_browser_call(window._active_voice_call)

    assert window._call_audio_session is audio and audio.started
    assert window._active_voice_call["call_id"] == "C1"
    assert window._voice_call_last_announced_state == "ACTIVE"
    assert window.voice_call_window_participants_button.enabled is True
