"""Known WPP deny semantics reach shared views without reviving cached last seen."""
from types import SimpleNamespace

import pytest

import core.contact_presence as presence
import core.websocket_client as transport
from core.websocket_client import WebSocketClient
from tests.test_contact_presence_freshness import Owner, PN


@pytest.fixture
def wire(monkeypatch):
    owner, callbacks = Owner(), []
    def receive(jid, presences):
        for participant, data in presences.items():
            presence.merge(owner, participant, data, chat=jid if jid.endswith("@g.us") else None)
    owner.on_presence_update = receive
    monkeypatch.setattr(transport, "wx", SimpleNamespace(CallAfter=callbacks.append))
    peer = SimpleNamespace(main_window=owner, _presence_delivery_generation=0,
                           _belongs_to_this_session=lambda payload: True)

    def deliver(**fields):
        WebSocketClient.on_wpp_presence_changed(peer, {
            "id": PN, "state": "unavailable", "isOnline": False, **fields})
        while callbacks:
            callbacks.pop(0)()
        return presence.cached(owner, PN)

    return owner, deliver


def test_wpp_denial_removes_previously_visible_last_seen(wire):
    owner, deliver = wire
    presence.merge(owner, PN, {"lastSeen": 1700000000, "isOnline": False})
    entry = deliver(isContact=False)
    assert entry["lastSeen"] is None
    assert entry["restricted"] is True


def test_denial_dominates_an_inconsistent_online_flag(wire):
    _, deliver = wire
    entry = deliver(state="available", isOnline=True, isContact=False)
    assert entry["restricted"] is True
    assert entry["isOnline"] is None
    assert not presence.visible_online(entry)


def test_explicit_wpp_permission_can_clear_a_previous_denial(wire):
    owner, deliver = wire
    presence.merge(owner, PN, {"restricted": True})
    entry = deliver(state="available", isOnline=True, isContact=True)
    assert entry["restricted"] is False
    assert presence.visible_online(entry)


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", [], {}])
def test_missing_or_malformed_contact_flag_does_not_clear_a_known_denial(wire, value):
    owner, deliver = wire
    presence.merge(owner, PN, {"restricted": True})
    assert deliver(isContact=value)["restricted"] is True


def test_absent_contact_flag_does_not_erase_denial(wire):
    owner, deliver = wire
    presence.merge(owner, PN, {"restricted": True})
    assert deliver()["restricted"] is True


@pytest.mark.parametrize("flag", [None, 0, "false"])
def test_malformed_push_permission_cannot_use_a_timestamp_to_clear_denial(wire, flag):
    owner, deliver = wire
    presence.merge(owner, PN, {"restricted": True})
    entry = deliver(isContact=flag, lastSeen=1700000000)
    assert entry["restricted"] is True and entry["lastSeen"] is None


@pytest.mark.parametrize("value", [None, 0, 1, "false"])
def test_unknown_merge_permission_does_not_clear_denial(value):
    owner = Owner()
    presence.merge(owner, PN, {"restricted": True})
    entry = presence.merge(owner, PN, {"lastKnownPresence": "available", "isOnline": True, "restricted": value})
    assert entry["restricted"] is True
    assert not presence.visible_online(entry)


def test_authoritative_modern_snapshot_can_restore_visible_last_seen():
    owner = Owner()
    presence.merge(owner, PN, {"restricted": True})
    snapshot = presence.response_snapshot({"status": "success", "presence": {
        "status": "ready", "state": "unavailable", "isOnline": False,
        "restricted": False, "lastSeen": 1700000000}})
    entry = presence.merge(owner, PN, snapshot)
    assert entry["restricted"] is False and entry["lastSeen"] == 1700000000


def test_legacy_positive_timestamp_can_restore_withheld_information():
    owner = Owner()
    presence.merge(owner, PN, {"restricted": True})
    entry = presence.merge(owner, PN, presence.response_snapshot({
        "status": "success", "response": 1700000000}))
    assert entry["restricted"] is False and entry["lastSeen"] == 1700000000


@pytest.mark.parametrize("value", [False, None])
def test_legacy_empty_response_is_not_permission_to_clear_denial(value):
    owner = Owner()
    presence.merge(owner, PN, {"restricted": True})
    entry = presence.merge(owner, PN, presence.response_snapshot({
        "status": "success", "response": value}))
    assert entry["restricted"] is True


def test_group_privacy_flag_does_not_change_the_participants_private_snapshot(wire):
    owner, deliver = wire
    presence.merge(owner, PN, {"lastSeen": 1700000000})
    deliver(id="999@g.us", isGroup=True, isContact=False,
            participants=[{"id": PN, "state": "composing"}])
    assert presence.cached(owner, PN)["lastSeen"] == 1700000000


@pytest.mark.parametrize("flag", [None, 0, 1, "false", [], {}])
def test_malformed_modern_permission_is_unknown_instead_of_granting_access(flag):
    assert presence.response_snapshot({"status": "success", "presence": {
        "status": "ready", "isOnline": False, "lastSeen": 1700000000, "restricted": flag}}) is None


@pytest.mark.parametrize("flag", [None, 0, 1, "false", [], {}])
def test_pending_with_malformed_permission_cannot_open_an_alias_query(flag):
    from core.contact_presence_query import read_snapshot
    calls = []
    def request(target, timeout):
        calls.append(target)
        return SimpleNamespace(status_code=200, json=lambda: {
            "status": "success", "presence": {"status": "pending", "restricted": flag}})
    assert read_snapshot((PN, "456@lid"), request, lambda: 100) is None
    assert calls == [PN]


def test_missing_modern_permission_does_not_clear_denial_on_a_null_snapshot():
    owner = Owner()
    presence.merge(owner, PN, {"restricted": True})
    snapshot = presence.response_snapshot({"status": "success", "presence": {
        "status": "ready", "isOnline": False, "lastSeen": None}})
    assert presence.merge(owner, PN, snapshot)["restricted"] is True


@pytest.mark.parametrize("fields", [
    {"isContact": True, "restricted": True},
    {"isContact": False, "restricted": False},
])
def test_explicit_denial_dominates_conflicting_push_fields(wire, fields):
    _, deliver = wire
    assert deliver(**fields)["restricted"] is True


def test_pn_lid_denial_is_shared_without_guessing_an_identity(wire):
    from tests.test_contact_presence_freshness import LID
    owner, deliver = wire
    owner._lid_to_phone[LID] = PN
    owner._phone_to_lid[PN] = LID
    presence.merge(owner, PN, {"lastSeen": 1700000000})
    deliver(id=LID, isContact=False)
    assert presence.cached(owner, PN)["restricted"] is True
    assert presence.cached(owner, LID)["lastSeen"] is None


def test_denied_push_clears_all_three_renderers_without_network_or_focus(wire):
    from main_window.chat_events import ChatEventsMixin
    from ui.conversation_panel.conversation_info import ConversationInfoMixin
    from ui.dialogs.personal_presence import presence_line
    from tests.test_contact_presence_views import views
    owner, deliver = wire
    presence.merge(owner, PN, {"lastSeen": 1700000000})
    panel, _, button, _, _ = views(owner)
    assert "Seen" in presence_line(owner, PN, owner.i18n)
    deliver(isContact=False)
    ConversationInfoMixin._refresh_presence_note(panel, PN)
    assert ChatEventsMixin._presence_announcement_for_chat(owner, panel.conversation) == "Unknown"
    assert button.text == "Contact" and button.focuses == 0
    assert presence_line(owner, PN, owner.i18n) == ""


def test_real_main_handler_does_not_query_immediately_after_push_denial():
    from tests.test_presence_speech_gating import _Stub
    owner = _Stub(conversation_jid=PN)
    queries = []
    owner._refresh_open_contact_presence = lambda: queries.append(True)
    presence.merge(owner, PN, {"lastSeen": 1700000000})
    owner.on_presence_update(PN, {PN: {
        "lastKnownPresence": "unavailable", "isOnline": False, "restricted": True}})
    assert queries == [] and presence.cached(owner, PN)["lastSeen"] is None
