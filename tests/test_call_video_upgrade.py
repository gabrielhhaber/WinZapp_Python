"""A voice call upgraded to video must stay one live call, and a call WinZapp
declares over must really be over.

Reported live (2026-09-25): when the other person, on the official client,
switched a voice call to video, WinZapp announced "Ligação encerrada" and
closed the call window -- while the call went on, and the other person's
voice kept coming out of the user's default speaker with nothing left on
screen to hang up with.

What was established from WhatsApp Web's own bundle over CDP (2026-09-26):
an A/V switch keeps the same CallModel and call id; handleVideoStateChange
only flips `isVideo` on it when the engine reports `callMediaStateChanged`.
The switch itself is `getVoipStackInterface().requestVideoUpgrade()`.

MainWindow cannot be instantiated without a wx.App, so the handlers are bound
onto a plain stub, and the Node-side and window wiring are checked against
the source, as the neighbouring call tests do.
"""

import re
import threading
from pathlib import Path
from types import SimpleNamespace

import wx

from main import MainWindow
from ui.accessible import AccessibleCallPromoteVideoButton

ROOT = Path(__file__).resolve().parents[1]
MAIN_SRC = (ROOT / "client" / "main.py").read_text(encoding="utf-8")
CONTROLLER_SRC = (
    ROOT / "client" / "api_patches" / "src" / "controller" / "callController.ts"
).read_text(encoding="utf-8")
ROUTES_SRC = (
    ROOT / "client" / "api_patches" / "src" / "routes" / "index.ts"
).read_text(encoding="utf-8")
SESSION_SRC = (
    ROOT / "client" / "api_patches" / "src" / "util" / "createSessionUtil.ts"
).read_text(encoding="utf-8")
BRIDGE_SRC = (
    ROOT / "client" / "api_patches" / "src" / "util" / "callMediaBridge.ts"
).read_text(encoding="utf-8")

PEER = "5511999999999@s.whatsapp.net"


class _I18n:
    def t(self, key):
        return key


class _Stub:
    on_voice_call_state_event = MainWindow.on_voice_call_state_event
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _chat_jids_equivalent = MainWindow._chat_jids_equivalent
    _jid_address_forms = MainWindow._jid_address_forms
    _mark_call_upgraded_to_video = MainWindow._mark_call_upgraded_to_video
    promote_call_to_video = MainWindow.promote_call_to_video
    _ensure_page_call_ended = MainWindow._ensure_page_call_ended
    _confirm_call_ended = MainWindow._confirm_call_ended
    _apply_confirmed_call_end = MainWindow._apply_confirmed_call_end
    _end_active_call_locally = MainWindow._end_active_call_locally

    def __init__(self, active=None):
        self._active_voice_call = active
        self._voice_call_last_announced_state = ""
        self._call_audio_session = object()
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self.i18n = _I18n()
        self.announcements = []
        self.upgrades = 0
        self.stopped = []
        self.ensured = []
        self.watched = []

    def output(self, text, interrupt=False):
        self.announcements.append(text)

    def _sync_voice_call_bar(self):
        pass

    def _preview_sender_from_jid(self, jid):
        return "Fulano"

    def _stop_voice_call_audio(self, grace_seconds=0.0, **_kw):
        self.stopped.append(grace_seconds)

    def _watch_ended_call_log(self, *args):
        self.watched.append(args)

    def _on_call_upgraded_to_video(self):
        self.upgrades += 1


def _voice_call():
    return {
        "identity": "WA-CALL", "call_id": "WA-CALL", "peer_jid": PEER,
        "name": "Fulano", "outgoing": False, "is_video": False,
    }


def _event(state="ACTIVE", is_video=True, call_id="WA-CALL", event="state"):
    return {"event": event, "state": state, "id": call_id,
            "peerJid": PEER, "isVideo": is_video}


def test_isvideo_flip_on_the_same_call_is_an_upgrade_not_an_end():
    stub = _Stub(_voice_call())
    stub._voice_call_last_announced_state = "ACTIVE"

    stub.on_voice_call_state_event(_event())

    assert stub._active_voice_call["is_video"] is True
    assert stub.upgrades == 1
    assert stub.stopped == []
    assert "voice_call_ended" not in stub.announcements


def test_upgrade_is_announced_once_and_never_undone():
    stub = _Stub(_voice_call())
    stub._voice_call_last_announced_state = "ACTIVE"

    stub.on_voice_call_state_event(_event())
    stub.on_voice_call_state_event(_event())
    # Both cameras off: WhatsApp drops isVideo again. Still a video call.
    stub.on_voice_call_state_event(_event(is_video=False))

    assert stub.upgrades == 1
    assert stub._active_voice_call["is_video"] is True


def test_a_call_adopted_as_video_is_not_an_upgrade(monkeypatch):
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **k: None)
    stub = _Stub(None)
    stub._call_audio_session = object()  # nothing to attach

    stub.on_voice_call_state_event(_event())

    assert stub._active_voice_call["is_video"] is True
    assert stub.upgrades == 0


class _Response:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


def _run_terminal(monkeypatch, stub, status_body=None, raises=False):
    """Deliver a terminal event with the page's status answer inlined."""
    posts = []

    def post(endpoint, payload, **_kw):
        posts.append((endpoint, payload))
        if raises:
            raise OSError("connection refused")
        return _Response({"status": "success", "response": status_body})

    class _Inline:
        def __init__(self, target=None, **_kw):
            self._target = target

        def start(self):
            self._target()

    stub._post_call_control = post
    ensured = []
    stub._ensure_page_call_ended = ensured.append
    monkeypatch.setattr(threading, "Thread", _Inline)
    monkeypatch.setattr(wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
    stub.on_voice_call_state_event(_event(state="ENDED", event="ended", is_video=False))
    return posts, ensured


def test_terminal_event_for_a_call_the_page_still_holds_is_ignored(monkeypatch):
    """The reported bug: the call went on, WinZapp said it had ended."""
    stub = _Stub(_voice_call())
    posts, ensured = _run_terminal(
        monkeypatch, stub,
        {"live": True, "state": "ACTIVE", "engine": "ongoing", "isVideo": True},
    )

    assert posts == [("status", {"callId": "WA-CALL"})]
    assert "voice_call_ended" not in stub.announcements
    assert stub.stopped == [] and ensured == []
    assert stub._active_voice_call is not None
    # ... and the page also says it is video now: that is the upgrade.
    assert stub._active_voice_call["is_video"] is True
    assert stub.upgrades == 1


def test_terminal_event_the_page_confirms_ends_the_call_for_real(monkeypatch):
    stub = _Stub(_voice_call())
    _posts, ensured = _run_terminal(
        monkeypatch, stub, {"live": False, "state": "ENDED", "engine": "none"},
    )

    assert "voice_call_ended" in stub.announcements
    assert stub.stopped == [1.25]
    assert ensured == ["WA-CALL"]


def test_no_answer_from_the_page_ends_the_call(monkeypatch):
    """Fail safe: a call wrongly kept up can still be hung up from the
    window; a live call wrongly declared over cannot."""
    stub = _Stub(_voice_call())
    _posts, ensured = _run_terminal(monkeypatch, stub, raises=True)

    assert "voice_call_ended" in stub.announcements
    assert ensured == ["WA-CALL"]


def test_a_call_hung_up_meanwhile_is_not_ended_twice(monkeypatch):
    active = _voice_call()
    stub = _Stub(active)
    stub._ensure_page_call_ended = lambda _call_id: None
    stub._apply_confirmed_call_end(active, "WA-CALL", PEER, None)
    stub._active_voice_call = None  # what the real teardown leaves behind
    stub._apply_confirmed_call_end(active, "WA-CALL", PEER, None)

    assert stub.announcements.count("voice_call_ended") == 1


def test_ensure_ended_never_sends_a_placeholder_or_empty_id(monkeypatch):
    started = []
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: started.append(k) or SimpleNamespace(start=lambda: None))
    stub = _Stub(None)

    stub._ensure_page_call_ended("")
    stub._ensure_page_call_ended("outgoing:5511999999999@s.whatsapp.net")
    assert started == []

    stub._ensure_page_call_ended("WA-CALL")
    assert len(started) == 1


def test_ensure_ended_posts_the_call_id_under_the_call_lock(monkeypatch):
    stub = _Stub(None)
    stub._call_action_lock = threading.Lock()
    posts = []

    class _Response:
        status_code = 200

        def json(self):
            return {"status": "success", "response": {"handled": True, "stillLive": True}}

    def post(endpoint, payload, **_kw):
        assert stub._call_action_lock.locked()
        posts.append((endpoint, payload))
        return _Response()

    stub._post_call_control = post
    stub._raise_for_call_response = MainWindow._raise_for_call_response.__get__(stub)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    class _Inline:
        def __init__(self, target=None, **_kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading, "Thread", _Inline)
    stub._ensure_page_call_ended("WA-CALL")

    assert posts == [("ensure-ended", {"callId": "WA-CALL"})]


def test_promote_does_nothing_without_a_voice_call(monkeypatch):
    started = []
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: started.append(k) or SimpleNamespace(start=lambda: None))

    _Stub(None).promote_call_to_video()
    video = _voice_call()
    video["is_video"] = True
    _Stub(video).promote_call_to_video()

    assert started == []


def test_promote_marks_the_same_call_as_video_once():
    active = _voice_call()
    stub = _Stub(active)

    stub._mark_call_upgraded_to_video(active)
    stub._mark_call_upgraded_to_video(active)
    stub._mark_call_upgraded_to_video({"call_id": "another"})

    assert active["is_video"] is True
    assert stub.upgrades == 1


# ── Wiring checked against the source ─────────────────────────────────────


def test_promote_button_reports_ctrl_p_and_is_bound():
    assert AccessibleCallPromoteVideoButton().GetKeyboardShortcut(0) == (wx.ACC_OK, "Ctrl+P")
    assert '(wx.ACCEL_CTRL,                  ord("P"), self.ID_CALL_PROMOTE),' in MAIN_SRC
    assert "id=self.ID_CALL_PROMOTE)" in MAIN_SRC
    assert "self.voice_call_window_promote_button.SetAccessible(AccessibleCallPromoteVideoButton())" in MAIN_SRC
    # Right after the microphone button, in tab order.
    mute = MAIN_SRC.index("controls.Add(self.voice_call_window_mute_button")
    promote = MAIN_SRC.index("controls.Add(self.voice_call_window_promote_button")
    video = MAIN_SRC.index("controls.Add(self.voice_call_window_video_button")
    assert mute < promote < video
    # Shown only while the call is voice.
    assert "promote_button.Show(not is_video)" in MAIN_SRC


def test_node_side_exposes_upgrade_and_scoped_ensure_ended():
    assert "'/api/:session/call/upgrade-video'" in ROUTES_SRC
    assert "'/api/:session/call/ensure-ended'" in ROUTES_SRC
    assert "'/api/:session/call/status'" in ROUTES_SRC
    assert "voipStack.requestVideoUpgrade()" in CONTROLLER_SRC
    ensure = CONTROLLER_SRC[CONTROLLER_SRC.index("action === 'ensure-ended'"):]
    ensure = ensure[: ensure.index("return { handled: true, stillLive: true")]
    # Scoped to the exact call id: 'end' hangs up whatever the page holds.
    assert "sameCallId(call, callId)" in ensure
    status = CONTROLLER_SRC[CONTROLLER_SRC.index("action === 'status'"):]
    status = status[: status.index("return { live, state, engine")]
    # "Live" needs the same id, a connected state AND no veto from the engine.
    assert "sameCallId(call, callId) && isConnectedCall(call)" in status
    assert "engine !== 'none' && engine !== 'ending'" in status


def test_call_state_poll_reports_an_isvideo_flip():
    signature = SESSION_SRC[SESSION_SRC.index("const signature = ["):]
    signature = signature[: signature.index("].join('|')")]
    assert "isVideo" in signature


def test_call_end_chime_never_unmutes_the_live_call_stream():
    restore = BRIDGE_SRC[BRIDGE_SRC.index("const restorePageAudio = "):]
    restore = restore[: restore.index("};")]
    assert "if (isLiveStreamElement(el)) return;" in restore
    assert re.search(r"isLiveStreamElement = \(el: HTMLMediaElement\): boolean =>[\s\S]{0,80}srcObject instanceof MediaStream", BRIDGE_SRC)
