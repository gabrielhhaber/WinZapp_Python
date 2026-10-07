"""Tests for WebSocketClient.on_wpp_presence_changed()'s is_group detection.

Reported live: after a full re-pairing, EVERY group's typing indicator
showed "Participante sem nome" for whoever was actually typing, in every
group, all the time — not just occasionally. The log showed
on_wpp_presence_changed emitting `presences: {"<group jid>@g.us": {...}}`
— keyed by the GROUP's own jid, not a participant — even though the event's
`id` field plainly ended in "@g.us". That shape is exactly what
main.py's _resolve_jid_name() already has a defensive guard for (a
participant JID is never a group JID — see test_resolve_jid_name.py), so it
correctly refused to show the group's name, but had nothing else to show
either: no participant was ever identified, because this method took the
single-chat branch (keying presences by chat_jid) instead of the group
branch (keying by each participant) for a message whose id was clearly a
group's.

Root cause: `is_group = bool(info.get("isGroup", False))` trusted
WPPConnect's own isGroup flag exclusively — and it was reported False for a
real group event, plausibly wa-js's own group-membership Store not being
fully warmed up yet right after a fresh pairing. The @g.us suffix on the
event's own id doesn't depend on that being warmed up, so it's now checked
first and is authoritative regardless of what isGroup says.

WebSocketClient imports wx/socketio at module top; each is stubbed out only
where it is not installed, so this stays headless. wx.CallAfter is faked to
run its callback immediately (main.py's on_presence_update isn't under test
here, only what gets handed to it) — per test, through monkeypatch. Assigning
it on the module once, as this file used to, changed the real wx for every
test after this one: a later pairing test's background thread then ran
connect.py's error handler on the spot and put a real wx.MessageBox on the
desktop of whoever was running the suite.
"""

import importlib
import sys
import types

import pytest

for _name in ("wx", "socketio"):
    try:
        importlib.import_module(_name)
    except ImportError:
        sys.modules.setdefault(_name, types.ModuleType(_name))

from core.websocket_client import WebSocketClient


@pytest.fixture(autouse=True)
def _call_after_runs_now(monkeypatch):
    monkeypatch.setattr(
        sys.modules["wx"], "CallAfter", lambda fn, *a, **k: fn(*a, **k), raising=False
    )


class _FakeMainWindow:
    def __init__(self):
        self.presence_calls = []

    def on_presence_update(self, jid, presences):
        self.presence_calls.append((jid, presences))


class _Stub:
    on_wpp_presence_changed = WebSocketClient.on_wpp_presence_changed
    _belongs_to_this_session = WebSocketClient._belongs_to_this_session

    def __init__(self):
        self.instance_name = ""
        self.main_window = _FakeMainWindow()


def _participant(jid, state):
    return {"id": jid, "state": state}


class TestIsGroupFallsBackToTheJidSuffix:
    def test_group_event_with_isgroup_false_still_keys_by_participant(self):
        """The exact reported shape: isGroup missing/false on a real group
        event — must not fall through to keying presences by the group's
        own jid."""
        stub = _Stub()
        info = {
            "id": "120363427511142886@g.us",
            "isGroup": False,
            "state": "unavailable",
            "t": 1787320530,
            "participants": [_participant("197813359124557@lid", "composing")],
        }

        stub.on_wpp_presence_changed(info)

        (jid, presences), = stub.main_window.presence_calls
        assert jid == "120363427511142886@g.us"
        assert presences == {
            "197813359124557@lid": {"lastKnownPresence": "composing"}
        }

    def test_group_event_with_isgroup_missing_entirely(self):
        stub = _Stub()
        info = {
            "id": "120363427511142886@g.us",
            "state": "unavailable",
            "t": 1787320530,
            "participants": [_participant("197813359124557@lid", "composing")],
        }

        stub.on_wpp_presence_changed(info)

        (_jid, presences), = stub.main_window.presence_calls
        assert "197813359124557@lid" in presences

    def test_group_event_with_isgroup_true_still_works(self):
        """The normal case (isGroup correctly reported) must be unaffected."""
        stub = _Stub()
        info = {
            "id": "120363427511142886@g.us",
            "isGroup": True,
            "t": 1787320530,
            "participants": [_participant("197813359124557@lid", "composing")],
        }

        stub.on_wpp_presence_changed(info)

        (_jid, presences), = stub.main_window.presence_calls
        assert "197813359124557@lid" in presences

    def test_group_event_with_no_participants_emits_nothing(self):
        """Better to show nothing than to key by the group's own jid — no
        participant was ever identified, and a stale "someone is typing"
        outliving its own event would be worse than a missed one."""
        stub = _Stub()
        info = {
            "id": "120363427511142886@g.us",
            "isGroup": False,
            "state": "unavailable",
            "t": 1787320530,
        }

        stub.on_wpp_presence_changed(info)

        assert stub.main_window.presence_calls == []

    def test_a_real_1to1_chat_is_unaffected(self):
        """Not a group at all — id doesn't end @g.us, isGroup False — must
        keep taking the single-chat branch, keyed by the chat's own jid."""
        stub = _Stub()
        info = {
            "id": "5511999999999@s.whatsapp.net",
            "isGroup": False,
            "state": "composing",
            "t": 1787320530,
        }

        stub.on_wpp_presence_changed(info)

        (jid, presences), = stub.main_window.presence_calls
        assert jid == "5511999999999@s.whatsapp.net"
        assert presences == {
            "5511999999999@s.whatsapp.net": {"lastKnownPresence": "composing", "isOnline": None, "eventTimestamp": 1787320530}
        }

    def test_a_lid_1to1_chat_is_unaffected(self):
        stub = _Stub()
        info = {
            "id": "131928795652121@lid",
            "isGroup": False,
            "state": "available",
            "t": 1787320530,
        }

        stub.on_wpp_presence_changed(info)

        (jid, presences), = stub.main_window.presence_calls
        assert jid == "131928795652121@lid"
        assert presences == {
            "131928795652121@lid": {"lastKnownPresence": "available", "isOnline": None, "eventTimestamp": 1787320530}
        }
