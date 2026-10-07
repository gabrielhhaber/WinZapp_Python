"""Connection transitions invalidate presence without opening or probing anything."""
from types import SimpleNamespace

import pytest

from core.websocket_client import WebSocketClient
import core.websocket_client as transport
import main_window.connection as connection
from tests.test_no_offline_during_wpp_update import _Stub as Window
from tests.test_contact_presence_freshness import Owner, PN
from core.contact_presence import cached, merge


class Peer:
    on_connect = WebSocketClient.on_connect
    on_disconnect = WebSocketClient.on_disconnect
    on_wpp_presence_changed = WebSocketClient.on_wpp_presence_changed
    on_presence_update = WebSocketClient.on_presence_update
    _belongs_to_this_session = WebSocketClient._belongs_to_this_session
    _DISCONNECT_CONFIRM_SECONDS = 30
    _recheck_connection_after_connect = lambda self: None

    def __init__(self):
        self.instance_name = "synthetic"
        self._disconnect_timer = None
        self.sio = SimpleNamespace(connected=False)
        self.calls = []
        self.main_window = SimpleNamespace(
            on_presence_update=lambda jid, data: self.calls.append((jid, data)),
            _invalidate_contact_presence=lambda: self.calls.append("invalidate"),
            _refresh_open_contact_presence=lambda: self.calls.append("refresh"),
            _set_wa_connected=lambda *a: self.calls.append("offline"))


@pytest.fixture
def queued(monkeypatch):
    callbacks = []
    monkeypatch.setattr(transport.wx, "CallAfter", lambda fn, *a, **kw: callbacks.append(lambda: fn(*a, **kw)))
    class Timer:
        def __init__(self, *a, **kw):
            pass
        def cancel(self):
            pass
        def start(self):
            pass
    monkeypatch.setattr(transport.threading, "Timer", Timer)
    monkeypatch.setattr(transport.threading, "Thread", Timer)
    return callbacks


def emit(peer, provider, **extra):
    if provider == "wpp":
        peer.on_wpp_presence_changed({"session": "synthetic", "id": PN, "state": "unavailable", "t": 1700000000000, **extra})
    else:
        peer.on_presence_update({"session": "synthetic", "data": {"id": PN, "presences": {PN: {"lastKnownPresence": "unavailable", "lastSeen": 1000}}}})


@pytest.mark.parametrize("provider", ["wpp", "baileys"])
@pytest.mark.parametrize("transition", ["on_disconnect", "on_connect"])
def test_queued_previous_connection_event_is_discarded(queued, provider, transition):
    peer = Peer()
    emit(peer, provider)
    assert len(queued) == 1  # The original event really reached the UI queue.
    getattr(peer, transition)()
    for callback in queued:
        callback()
    assert not any(isinstance(call, tuple) for call in peer.calls)
    assert "invalidate" in peer.calls


def test_new_connection_event_is_delivered_and_does_not_invent_timestamp(queued):
    peer = Peer()
    peer.on_connect()
    emit(peer, "wpp", state="paused", isOnline=True)
    for callback in queued:
        callback()
    jid, data = peer.calls[-1]
    assert jid == PN and "lastSeen" not in data[PN]
    assert data[PN]["eventTimestamp"] == 1700000000000
    owner = Owner()
    merge(owner, jid, data[jid])
    assert cached(owner, PN)["lastKnownPresence"] == "available"


def test_presence_from_another_session_is_ignored(queued):
    peer = Peer()
    emit(peer, "wpp", session="other")
    assert queued == []


def test_confirmed_health_transition_invalidates_then_refreshes(monkeypatch):
    window = Window()
    calls = []
    window._invalidate_contact_presence = lambda: calls.append("invalidate")
    window._refresh_open_contact_presence = lambda: calls.append("refresh")
    monkeypatch.setattr(connection.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    window._set_wa_connected(False, "synthetic", announce=False)
    window._set_wa_connected(True, "synthetic", announce=False)
    assert calls == ["invalidate", "refresh"]
