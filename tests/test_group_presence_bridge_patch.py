"""Typing / recording audio announcements in GROUPS (api_patches bridge).

wa-js 4.6.0 fires ``chat.presence_change`` for a group but with
``participants: []``: current WhatsApp Web keeps who is typing in the presence
model's ``typingUserIds`` / ``recordingUserIds``. The Python client can only say
WHO is typing in a group, so it dropped every such event silently, while
one-to-one chats (state in ``chatstate``) kept working. The Node patch bridges
the two arrays into the event shape the client already understands.

The page-side JavaScript cannot run here, so the TypeScript source is checked
for the behaviours that matter, and the Python half is exercised for real: the
exact payload the bridge emits must produce a per-participant presence.
"""

from pathlib import Path

from core.websocket_client import WebSocketClient

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "client" / "api_patches" / "src" / "util" / "createSessionUtil.ts"
          ).read_text(encoding="utf-8")


def _bridge() -> str:
    start = SOURCE.index("async onGroupPresenceBridge")
    return SOURCE[start:SOURCE.index("async onReactionMessage", start)]


class TestTheBridgeSource:
    def test_it_is_wired_next_to_the_presence_listener(self):
        wire = SOURCE[SOURCE.index("async wireListeners"):]
        wire = wire[:wire.index("onUnreadCountChanged")]
        assert "this.onPresenceChanged(client, req)" in wire
        assert "this.onGroupPresenceBridge(client, req)" in wire

    def test_it_watches_both_arrays_of_every_group_model(self):
        bridge = _bridge()
        assert "'change:typingUserIds'" in bridge
        assert "'change:recordingUserIds'" in bridge
        assert "endsWith('@g.us')" in bridge
        # Groups that appear later are picked up too, and never twice.
        assert "store.on('add', attach)" in bridge
        assert "__winzappGroupPresence" in bridge

    def test_it_reports_the_states_the_client_maps(self):
        bridge = _bridge()
        assert "'typing'" in bridge
        assert "'recording_audio'" in bridge
        assert "'paused'" in bridge

    def test_one_keystroke_raising_two_changes_emits_once(self):
        bridge = _bridge()
        assert "now.size === before.size" in bridge

    def test_it_uses_the_wppconnect_exposed_function_and_survives_reloads(self):
        bridge = _bridge()
        assert "win.onPresenceChanged(" in bridge
        assert "client.page.on('load'" in bridge
        assert "attempt < 120" in bridge


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
