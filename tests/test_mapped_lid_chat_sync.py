"""A LID snapshot must refresh the existing phone chat before delta planning.

Dropping the alias as a duplicate hid newer linked-device messages while the
account reported a completed sync. Exercise the actual merge on a plain stub.
"""

import pytest

from core.incremental_sync import chat_sync_marker, classify_chat_sync
from tests.test_get_remote_chats_persistence import _chat, _make, post  # noqa: F401


PHONE = "5511900000001@s.whatsapp.net"
LID = "123456789012345@lid"
OLD_T = 1700000000


def _cached():
    message = {"key": {"id": "OLD", "remoteJid": PHONE},
               "messageTimestamp": OLD_T, "messageType": "conversation",
               "message": {"conversation": "cached"}}
    return {PHONE: {"remoteJid": PHONE, "t": OLD_T, "unreadCount": 0,
                    "lastReceivedKey": {"id": "OLD"}, "lastMessage": message,
                    "messages": {"messages": {"records": [message]}}}}


def _mapped(cached):
    stub = _make(cached)
    stub._lid_to_phone = {LID: PHONE}
    stub._phone_to_lid = {PHONE: LID}
    return stub


@pytest.mark.parametrize("advance_time", [True, False])
def test_new_lid_snapshot_selects_cached_phone_chat_for_message_refresh(post, advance_time):
    cached = _cached()
    baseline = chat_sync_marker(cached[PHONE])
    records = cached[PHONE]["messages"]
    stub = _mapped(cached)
    post["payload"] = [_chat(LID, t=OLD_T + int(advance_time),
                             lastReceivedKey={"id": "NEW"}, messages=None,
                             lastMessage=None)]

    merged = stub.get_remote_chats(dict(cached), persist_full=False,
                                  notify_errors=False, defer_chat_save=True)

    assert classify_chat_sync(merged[PHONE], baseline) == (
        "incremental", "activity-changed" if advance_time else "last-received-changed")
    assert set(merged) == {PHONE}
    assert merged[PHONE]["remoteJid"] == PHONE
    assert merged[PHONE]["messages"] is records
    assert merged[PHONE]["lastMessage"]["key"]["id"] == "OLD"
    assert stub.schedule_save_calls == stub.save_data_calls == 0


def test_remote_read_from_lid_reconciles_cached_phone_unread_count(post):
    cached = _cached()
    cached[PHONE]["unreadCount"] = 4
    stub = _mapped(cached)
    post["payload"] = [_chat(LID, unreadCount=0)]

    merged = stub.get_remote_chats(dict(cached), persist_full=False, notify_errors=False)

    assert merged[PHONE]["unreadCount"] == 0


@pytest.mark.parametrize("deleted_form", [PHONE, LID])
def test_deleted_alias_does_not_update_cached_chat(post, deleted_form):
    cached = _cached()
    stub = _mapped(cached)
    stub._deleted_chats = {deleted_form}
    post["payload"] = [_chat(LID, t=OLD_T + 1, lastReceivedKey={"id": "NEW"})]

    merged = stub.get_remote_chats(dict(cached), persist_full=False, notify_errors=False)

    assert merged[PHONE]["t"] == OLD_T
    assert merged[PHONE]["lastReceivedKey"]["id"] == "OLD"
    assert LID not in merged


def test_group_participant_lid_does_not_update_private_chat(post):
    cached = _cached()
    stub = _mapped(cached)
    post["payload"] = [_chat(LID, t=OLD_T + 1,
                             lastReceivedKey={"id": "NEW", "remote": {
                                 "_serialized": "120363000000000001@g.us"}})]

    merged = stub.get_remote_chats(dict(cached), persist_full=False, notify_errors=False)

    assert merged[PHONE]["t"] == OLD_T
    assert merged[PHONE]["lastReceivedKey"]["id"] == "OLD"


def test_stale_lid_snapshot_keeps_stored_activity_floor(post):
    cached = _cached()
    stub = _mapped(cached)
    post["payload"] = [_chat(LID, t=OLD_T - 100, unreadCount=0)]

    merged = stub.get_remote_chats(dict(cached), persist_full=False, notify_errors=False)

    assert merged[PHONE]["t"] == OLD_T


def test_clear_cutoff_uses_phone_chat_for_lid_snapshot(post):
    cached = _cached()
    cached[PHONE]["lastMessage"] = None
    stub = _mapped(cached)
    stub.settings["cleared_chats"] = {PHONE: OLD_T + 1}
    post["payload"] = [_chat(LID, unreadCount=4)]

    merged = stub.get_remote_chats(dict(cached), persist_full=False, notify_errors=False)

    assert merged[PHONE]["unreadCount"] == 0


def test_stale_lid_read_does_not_erase_newer_local_unread(post):
    cached = _cached()
    cached[PHONE]["unreadCount"] = 4
    stub = _mapped(cached)
    post["payload"] = [_chat(LID, t=OLD_T - 1, unreadCount=0)]

    merged = stub.get_remote_chats(dict(cached), persist_full=False, notify_errors=False)

    assert merged[PHONE]["unreadCount"] == 4
