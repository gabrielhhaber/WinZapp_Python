"""Typing / recording audio announcements in GROUPS (api_patches bridge).

wa-js 4.6.0 fires ``chat.presence_change`` for a group but with
``participants: []``: current WhatsApp Web keeps who is typing in the presence
model's ``typingUserIds`` / ``recordingUserIds``. The Python client can only say
WHO is typing in a group, so it dropped every such event silently, while
one-to-one chats (state in ``chatstate``) kept working. The Node patch bridges
the two arrays into the event shape the client already understands.

The actual page-side JavaScript and published provider binding are exercised
in test_group_presence_ownership.py; this module exercises the Python half: the
exact payload the bridge emits must produce a per-participant presence.
"""

from core.websocket_client import WebSocketClient


def _handle(info):
    """Run WebSocketClient.on_wpp_presence_changed() on a stub and return what it
    handed to the main window."""
    delivered = []

    class _Stub:
        main_window = type("MW", (), {
            "on_presence_update": lambda self, jid, presences: delivered.append((jid, presences))
        })()

        def _belongs_to_this_session(self, info):
            return True

    import wx
    original = wx.CallAfter
    wx.CallAfter = lambda fn, *a, **k: fn(*a, **k)
    try:
        WebSocketClient.on_wpp_presence_changed(_Stub(), info)
    finally:
        wx.CallAfter = original
    return delivered


GROUP = "120363409931936700@g.us"
TYPER = "68904344899801@lid"


class TestTheClientAcceptsTheBridgedEvent:
    def test_a_group_event_without_participants_is_still_dropped(self):
        """What wa-js emits by itself: nobody to name, nothing to announce."""
        assert _handle({"id": GROUP, "isGroup": True, "state": "typing", "participants": []}) == []

    def test_typing_in_a_group_names_the_participant(self):
        delivered = _handle({
            "id": GROUP, "isGroup": True, "state": "typing", "t": 1,
            "participants": [{"id": TYPER, "state": "typing", "shortName": ""}],
        })
        assert delivered == [(GROUP, {TYPER: {"lastKnownPresence": "composing"}})]

    def test_recording_audio_in_a_group_maps_to_recording(self):
        delivered = _handle({
            "id": GROUP, "isGroup": True, "state": "recording_audio", "t": 2,
            "participants": [{"id": TYPER, "state": "recording_audio", "shortName": ""}],
        })
        assert delivered[0][1][TYPER]["lastKnownPresence"] == "recording"

    def test_a_participant_who_stopped_is_reported_paused(self):
        delivered = _handle({
            "id": GROUP, "isGroup": True, "state": "paused", "t": 3,
            "participants": [{"id": TYPER, "state": "paused", "shortName": ""}],
        })
        assert delivered[0][1][TYPER]["lastKnownPresence"] == "paused"
