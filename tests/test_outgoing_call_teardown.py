"""An outgoing call that never goes live must leave no state behind (#275).

Two defects, reported by an outside contributor and both real:

* ``_start_call()``'s worker held ``_call_action_lock`` across the
  offer POST, whose timeout is 75 seconds. ``end_active_call()`` takes the same
  lock, so while an offer stalled the "end call" button did nothing at all.
* Its ``except`` branch never cleared ``_active_voice_call``. The report says
  background sync then stops "for the remainder of the application session";
  it does not -- ``_voice_call_in_progress()`` is bounded by
  ``_VOICE_CALL_PAUSE_MAX_SECONDS`` (2h), added for exactly this shape of
  failure, and that safety net held. What the orphaned record really costs is
  an up-to-two-hour stand-down of the recurring background work, a call window
  that stays on screen, and ``voice_call_already_active`` refusing every later
  call until the bound expires.

Narrowing the lock buys a race of its own, which is the third thing under
test: with the offer no longer serialised against the hang-up, the user can
end a call whose offer WhatsApp has not received yet, and the offer then lands
and rings the peer. The attempt token settles that -- a cancelled attempt ends
the call it just created and never starts the camera.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the methods are exercised unbound against a plain stub, the same approach
as tests/test_incoming_call_answer_without_video.py.
"""

import threading

import pytest

from main import MainWindow

# Captured at import, before _inline_threads() replaces threading.Thread --
# that fixture patches the attribute on the threading module itself (main
# imports the module, not the class), so it is global while it is in place.
_REAL_THREAD = threading.Thread


class _I18n:
    def t(self, key):
        return {
            "voice_call_starting": "Ligando para {name}...",
            "voice_call_start_failed": "Falha ao iniciar a ligação: {error}",
            "voice_call_already_active": "Já em ligação.",
            "voice_call_individual_only": "Só é possível ligar para contatos.",
            "incoming_call_end_failed": "Falha ao encerrar: {error}",
        }.get(key, key)


class _Response:
    def __init__(self, call_id=""):
        self._call_id = call_id

    def json(self):
        return {"response": {"id": self._call_id}}


class _MainStub:
    _uses_neonize = lambda self: False      # these tests are about the WPPConnect engine
    _start_individual_call = MainWindow._start_individual_call
    _start_call = MainWindow._start_call
    _abandon_outgoing_call = MainWindow._abandon_outgoing_call
    end_active_call = MainWindow.end_active_call
    # Bound from the real class on purpose: a stub that invents a truthy
    # answer here makes the background stand-down permanent (see
    # docs/traps/voice-calls.md).
    _voice_call_in_progress = MainWindow._voice_call_in_progress
    _VOICE_CALL_PAUSE_MAX_SECONDS = MainWindow._VOICE_CALL_PAUSE_MAX_SECONDS

    def _ensure_page_call_ended(self, call_id, *, delay=1.5, unless_active=False):
        # end_active_call() follows up with a scoped ensure-ended; it is a
        # separate POST and covered in tests/test_call_video_upgrade.py.
        pass

    def __init__(self, *, offer=None):
        self._call_action_lock = threading.Lock()
        self._active_voice_call = None
        self._active_incoming_calls = {}
        self._outgoing_call_attempt = None
        self._voice_call_last_announced_state = ""
        self._call_audio_session = None
        self.i18n = _I18n()
        self.announcements = []
        self.posted = []
        self.stop_audio_calls = []
        self.sync_bar_calls = 0
        self.camera_starts = 0
        self.lock_free_during_offer = None
        # Called with the stub while the offer POST is in flight, so a test can
        # hang up or swap the active call underneath the worker.
        self._offer_hook = offer

    # -- collaborators, stubbed so these tests are only about call state.
    def _normalize_jid(self, jid):
        return jid

    def _is_self_jid(self, jid):
        return False

    def _preview_sender_from_jid(self, jid):
        return ""

    def _resolve_jid_for_send(self, jid):
        return jid

    def _start_voice_call_audio(self, identity, details=None, *, keep_active_call=False,
                                expected_call=None):
        # Device startup now preserves the worker's exact owning record,
        # including event fields that may have arrived while devices open.
        assert expected_call is self._active_voice_call
        self._call_audio_session = object()
        fields = {
            "identity": identity,
            "call_id": (details or {}).get("call_id") or identity,
            "peer_jid": (details or {}).get("peer_jid") or "",
            "group_jid": (details or {}).get("group_jid") or "",
            "name": (details or {}).get("name") or "",
            "outgoing": True,
            "is_video": bool((details or {}).get("is_video")),
        }
        for key, value in fields.items():
            expected_call.setdefault(key, value)
        return True

    def _stop_voice_call_audio(self, grace_seconds=0.0, *, keep_call=False):
        self.stop_audio_calls.append(grace_seconds)
        if grace_seconds:
            # Mirrors the real grace path: the audio keeps playing the
            # disconnect tail and the call record is cleared only later.
            return
        self._call_audio_session = None
        if not keep_call:
            self._active_voice_call = None

    def _post_call_control(self, endpoint, payload, *, timeout=15):
        self.posted.append(endpoint)
        if endpoint == "offer":
            # The whole point of the fix: end_active_call() must be able to
            # take this lock while the offer blocks here.
            acquired = self._call_action_lock.acquire(blocking=False)
            self.lock_free_during_offer = acquired
            if acquired:
                self._call_action_lock.release()
            if self._offer_hook is not None:
                self._offer_hook(self)
        return _Response("waid-1")

    def _raise_for_call_response(self, response, action):
        return response

    def _call_error_text(self, error):
        return str(error)

    def _start_call_camera(self, *, announce_failure=True, transmit=True, expected_call=None):
        assert expected_call is self._active_voice_call
        self.camera_starts += 1
        return True

    def _sync_voice_call_bar(self):
        self.sync_bar_calls += 1

    def output(self, text, interrupt=False):
        self.announcements.append(text)


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


PEER = "5511999999999@s.whatsapp.net"


def test_a_successful_offer_keeps_the_call_and_starts_the_camera():
    """Regression guard for the unchanged path."""
    stub = _MainStub()

    stub._start_individual_call(PEER, "Fulano", is_video=True)

    assert stub.posted == ["offer"]
    assert stub._active_voice_call is not None
    # The id WhatsApp answered with replaces the provisional one, so later
    # callstate events match this call.
    assert stub._active_voice_call["call_id"] == "waid-1"
    assert stub.camera_starts == 1
    assert stub.stop_audio_calls == []
    assert stub._outgoing_call_attempt is None


def test_join_uses_the_existing_group_call_and_keeps_video_off():
    stub = _MainStub()
    requests = []
    stub._post_call_control = lambda endpoint, payload, **kw: (
        requests.append((endpoint, payload, kw)) or _Response("call-existing"))
    probes = []
    def probe(*, expected_call=None):
        assert expected_call is stub._active_voice_call
        probes.append(expected_call)
    stub._probe_call_camera = probe

    stub._start_call("120363000001@g.us", "Group", is_group=True,
                     is_video=True, join_call_id="call-existing")

    assert requests == [("group/join", {
        "groupJid": "120363000001@g.us", "isVideo": True, "callId": "call-existing",
    }, {"timeout": 75})]
    assert stub._active_voice_call["call_id"] == "call-existing"
    assert stub.camera_starts == 0
    assert probes == [stub._active_voice_call]
    assert probes[0] is stub._active_voice_call


def test_join_failure_releases_audio_and_never_sends_an_offer():
    stub = _MainStub()

    def refuse(endpoint, payload, **kwargs):
        stub.posted.append(endpoint)
        raise RuntimeError("ended")

    stub._post_call_control = refuse
    stub._start_call("120363000001@g.us", "Group", is_group=True,
                     is_video=False, join_call_id="call-existing")

    assert stub.posted == ["group/join"]
    assert stub._active_voice_call is None
    assert stub._voice_call_in_progress() is False


def test_a_failed_join_ends_that_call_on_the_page_if_it_connected_late():
    """The page's join can outlive the Node side's wait and connect after
    "join failed" was spoken: the user would be in a call with no window to
    hang up from. Only THAT call id is named, so no other call can be ended."""
    stub = _MainStub()
    ensured, actions = [], []
    stub._ensure_page_call_ended = lambda call_id, **kw: ensured.append((call_id, kw))

    def refuse(response, action):
        actions.append(action)
        raise RuntimeError("HTTP 500")

    stub._raise_for_call_response = refuse
    stub._start_call("120363000001@g.us", "Group", is_group=True,
                     is_video=False, join_call_id="call-existing")

    assert actions == ["join"]
    # unless_active: a late join adopted, or a second join, is that same id.
    assert ensured == [("call-existing", {"unless_active": True})]
    assert "group_call_join_failed" in stub.announcements
    assert stub._active_voice_call is None


def test_a_failed_offer_never_asks_the_page_to_end_a_call():
    stub = _MainStub(offer=lambda s: (_ for _ in ()).throw(RuntimeError("HTTP 500")))
    ensured = []
    stub._ensure_page_call_ended = lambda call_id, **kw: ensured.append(call_id)

    stub._start_individual_call(PEER, "Fulano", is_video=False)

    assert ensured == []


def test_starting_a_group_call_offers_to_the_group_and_follows_it():
    """Starting, not joining: the group offer names the group, and the call
    record follows the group rather than any one participant."""
    stub = _MainStub()
    requests = []
    stub._post_call_control = lambda endpoint, payload, **kw: (
        requests.append((endpoint, payload)) or _Response("group-call"))

    stub._start_call("120363000001@g.us", "Group", is_group=True, is_video=False)

    assert requests[0] == ("group/offer", {"groupJid": "120363000001@g.us", "isVideo": False})
    assert stub._active_voice_call["group_jid"] == "120363000001@g.us"
    assert stub._active_voice_call["call_id"] == "group-call"


def test_join_multiparty_call_from_person_chat_uses_native_join_not_offer():
    stub = _MainStub()
    requests = []
    stub._post_call_control = lambda endpoint, payload, **kw: (
        requests.append((endpoint, payload)) or _Response("multi-existing"))
    stub._start_call(PEER, "Contact", is_group=True, is_video=False,
                     join_call_id="multi-existing")
    assert requests == [("group/join", {
        "chatJid": PEER, "isVideo": False, "callId": "multi-existing",
    })]
    assert stub._active_voice_call["peer_jid"] == PEER


def test_a_failed_offer_clears_the_active_call_and_resyncs_the_bar():
    def _fail(stub):
        raise RuntimeError("HTTP 500")

    stub = _MainStub(offer=_fail)

    stub._start_individual_call(PEER, "Fulano", is_video=True)

    assert stub._active_voice_call is None
    assert stub.stop_audio_calls == [0.0]
    assert stub.camera_starts == 0
    # The call window is told to hide, and the background work stands back up
    # instead of waiting out the two-hour bound.
    assert stub.sync_bar_calls == 2
    assert stub._voice_call_in_progress() is False
    assert stub._outgoing_call_attempt is None
    assert "Falha ao iniciar a ligação: HTTP 500" in stub.announcements


def test_a_failed_offer_does_not_block_the_next_call():
    stub = _MainStub(offer=lambda s: (_ for _ in ()).throw(RuntimeError("timeout")))

    stub._start_individual_call(PEER, "Fulano", is_video=False)
    stub._offer_hook = None
    stub._start_individual_call(PEER, "Fulano", is_video=False)

    assert "Já em ligação." not in stub.announcements
    assert stub.posted == ["offer", "offer"]
    assert stub._active_voice_call is not None


def _hang_up_off_thread(stub, timeout=5.0):
    """Press "end call" from another thread while the offer is in flight.

    Off the worker's own thread, and joined with a timeout, so that putting
    the offer POST back inside _call_action_lock fails these tests RED: run
    inline, the hang-up would block on the lock the worker is holding and the
    whole pytest session would hang instead (there is no pytest-timeout here).
    Returns whether the hang-up completed.
    """
    presser = _REAL_THREAD(target=stub.end_active_call, daemon=True)
    presser.start()
    presser.join(timeout)
    return not presser.is_alive()


def test_the_call_action_lock_is_free_while_the_offer_is_in_flight():
    """Defect 1: 'end call' was dead for the offer's whole 75 s timeout."""
    ended = []

    stub = _MainStub(offer=lambda s: ended.append(_hang_up_off_thread(s)))

    stub._start_individual_call(PEER, "Fulano", is_video=False)

    # Both halves matter: the probe inside the POST proves the lock is
    # available, and the hang-up proves a real caller gets through it.
    assert stub.lock_free_during_offer is True
    assert ended == [True]


def test_a_cancel_during_a_successful_offer_ends_the_call_and_skips_the_camera():
    stub = _MainStub(offer=_hang_up_off_thread)

    stub._start_individual_call(PEER, "Fulano", is_video=True)

    # Two ends: the user's own, which named a call WhatsApp did not have yet,
    # and the worker's, once the offer landed and really created one.
    assert stub.posted == ["offer", "end", "end"]
    # The camera must never come up for a call the user already cancelled.
    assert stub.camera_starts == 0
    assert stub._active_voice_call is None
    assert stub._voice_call_in_progress() is False
    assert stub._outgoing_call_attempt is None


def test_a_cancelled_offer_never_ends_the_call_that_replaced_it():
    """The `end` action is unscoped on the Node side, so this is a live call.

    callController.ts's 'end' branch uses the requested callId only to set
    userEndedCall; the action itself is voipStack.endCall(2, true) /
    WPP.call.end() on whatever call the page holds. So a stalled offer that
    posts `end` on "I was cancelled" alone hangs up the call the user started
    (or answered) in the meantime -- the hang-up's own grace period leaves
    1.25 s, and a 75-second offer leaves far more than that.
    """
    other = {"identity": "other", "call_id": "other", "is_video": False}

    def _hang_up_then_redial(stub):
        assert _hang_up_off_thread(stub)
        stub._active_voice_call = other

    stub = _MainStub(offer=_hang_up_then_redial)

    stub._start_individual_call(PEER, "Fulano", is_video=False)

    # Only the user's own end. The second call is left alone...
    assert stub.posted == ["offer", "end"]
    assert stub._active_voice_call is other
    assert stub.camera_starts == 0


def test_a_cancelled_offer_never_ends_a_call_a_newer_attempt_owns():
    """Same protection from the other side: no active record yet, but the
    page belongs to a newer outgoing attempt whose own offer is in flight."""
    newer = {"cancelled": False}

    def _hang_up_then_dial_again(stub):
        assert _hang_up_off_thread(stub)
        stub._active_voice_call = None
        stub._outgoing_call_attempt = newer

    stub = _MainStub(offer=_hang_up_then_dial_again)

    stub._start_individual_call(PEER, "Fulano", is_video=False)

    assert stub.posted == ["offer", "end"]
    # ...and the stale attempt must not clear the newer attempt's token
    # either, or the newer call could no longer be cancelled.
    assert stub._outgoing_call_attempt is newer


def test_a_call_that_replaced_this_one_is_not_torn_down_by_the_failure():
    other = {"identity": "other", "call_id": "other", "is_video": False}

    def _replace_then_fail(stub):
        # A terminal callstate event, or an incoming call answered while the
        # offer was in flight, leaves a different record in place.
        stub._active_voice_call = other
        stub.stop_audio_calls.clear()
        raise RuntimeError("HTTP 500")

    stub = _MainStub(offer=_replace_then_fail)

    stub._start_individual_call(PEER, "Fulano", is_video=False)

    assert stub._active_voice_call is other
    # Neither the other call's audio nor its record may be touched here.
    assert stub.stop_audio_calls == []
