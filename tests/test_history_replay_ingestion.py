"""History replays must not become new tail rows or evict recent cache rows.

Exercise the actual raw-WPP -> normalizer -> dispatcher boundary and the two
ingestion methods on plain stubs. No socket, account, wx.App or window.
"""
from types import SimpleNamespace
from unittest.mock import Mock
from concurrent.futures import Future

import pytest

from core.websocket_client import WebSocketClient
from main_window.message_events import MessageEventsMixin
from main_window.identity import IdentityMixin
from tests.test_archived_chat_sound_tts import _StubMainWindow
from tests.test_lid_merge_keeps_messages import _SyncThread


JID = "5511900000000@s.whatsapp.net"
NOW = 1_800_000_000
LID = "123456789012345@lid"


def _message(mid, timestamp):
    return {
        "key": {"id": mid, "remoteJid": JID, "fromMe": False},
        "messageTimestamp": timestamp,
        "messageType": "conversation",
        "message": {"conversation": "synthetic"},
    }


class _Socket:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid
    _belongs_to_this_session = WebSocketClient._belongs_to_this_session
    on_wpp_message_received = WebSocketClient.on_wpp_message_received
    on_messages_upsert = WebSocketClient.on_messages_upsert

    def __init__(self, *, known=True):
        self.instance_name = "synthetic-session"
        self._connect_time = NOW
        self.main_window = SimpleNamespace(
            chats={JID: {}} if known else {},
            _normalize_jid=lambda jid: jid.replace("@c.us", "@s.whatsapp.net"),
            _extract_lid_mapping=Mock(),
            on_historical_message=Mock(),
            on_new_message=Mock(),
        )


def _raw(timestamp, **extra):
    return {
        "id": "false_5511900000000@c.us_SYNTHETIC",
        "from": "5511900000000@c.us", "to": "5511999998888@c.us",
        "fromMe": False, "timestamp": timestamp, "type": "chat",
        "body": "synthetic", **extra,
    }


@pytest.fixture
def inline_callbacks(monkeypatch):
    monkeypatch.setattr("core.websocket_client.wx.CallAfter", lambda fn, *a, **k: fn(*a, **k))


@pytest.mark.parametrize("stamp", [NOW - 86400, (NOW - 86400) * 1000])
def test_raw_history_replay_reaches_the_silent_handler(stamp, inline_callbacks):
    socket = _Socket()
    socket.on_wpp_message_received({"response": _raw(stamp, isMdHistoryMsg=True)})
    socket.main_window.on_historical_message.assert_called_once()
    socket.main_window.on_new_message.assert_not_called()
    received = socket.main_window.on_historical_message.call_args.args[0]
    assert received["messageTimestamp"] == NOW - 86400


def test_fresh_history_flagged_message_still_uses_live_notifications(inline_callbacks):
    socket = _Socket()
    socket.on_wpp_message_received({"response": _raw(NOW + 1, isMdHistoryMsg=True)})
    socket.main_window.on_new_message.assert_called_once()
    socket.main_window.on_historical_message.assert_not_called()


def test_history_for_an_unknown_chat_stays_silent(inline_callbacks):
    socket = _Socket(known=False)
    socket.on_wpp_message_received({"response": _raw(NOW + 1, isMdHistoryMsg=True)})
    socket.main_window.on_historical_message.assert_called_once()
    socket.main_window.on_new_message.assert_not_called()


@pytest.mark.parametrize("flag", [False, None])
def test_missing_or_false_history_flag_does_not_silence_a_live_event(flag, inline_callbacks):
    socket = _Socket()
    extra = {} if flag is None else {"isMdHistoryMsg": flag}
    socket.on_wpp_message_received({"response": _raw(NOW + 1, **extra)})
    socket.main_window.on_new_message.assert_called_once()
    socket.main_window.on_historical_message.assert_not_called()


def test_a_foreign_session_history_replay_is_still_rejected(inline_callbacks):
    socket = _Socket()
    socket.on_wpp_message_received({"session": "other-session", "response": _raw(NOW - 100, isMdHistoryMsg=True)})
    socket.main_window.on_new_message.assert_not_called()
    socket.main_window.on_historical_message.assert_not_called()


def _window(records):
    window = _StubMainWindow(open_jid=JID)
    window.ws._connect_time = NOW
    window.chats[JID] = {"remoteJid": JID, "unreadCount": 0, "t": NOW,
                         "messages": {"messages": {"records": records}}}
    return window


def test_unflagged_late_delivery_is_stored_in_chronological_order():
    records = [_message("recent", NOW)]
    window = _window(records)
    window.on_new_message(_message("yesterday", NOW - 86400))
    assert [m["key"]["id"] for m in records] == ["yesterday", "recent"]


def test_old_replay_cannot_evict_newer_messages_at_the_resident_limit(monkeypatch):
    monkeypatch.setattr("main_window.message_events._MAX_RESIDENT_MESSAGES_PER_CHAT", 2)
    records = [_message("recent-1", NOW - 1), _message("recent-2", NOW)]
    window = _window(records)
    window._msg_bg_executor = SimpleNamespace(submit=lambda fn: fn())
    incoming = _message("yesterday", NOW - 86400)
    window.on_new_message(incoming)
    assert [m["key"]["id"] for m in records] == ["recent-1", "recent-2"]
    # Evicting an old resident row is not deleting history from storage.
    window.db.insert_message.assert_called_once_with(JID, incoming)


def test_late_delivery_compares_seconds_and_milliseconds_together():
    records = [_message("older", NOW - 10), _message("recent", NOW * 1000)]
    window = _window(records)
    window.on_new_message(_message("late", NOW - 5))
    assert [m["key"]["id"] for m in records] == ["older", "late", "recent"]


def test_history_does_not_persist_live_arrival_timing(inline_callbacks):
    window = _window([_message("recent", NOW)])
    window._schedule_refresh_messages = Mock()
    message = _message("yesterday", NOW - 86400)
    message["_arrived_at"] = 123.5
    MessageEventsMixin.on_historical_message(window, message)
    assert "_arrived_at" not in message
    assert window.chats[JID]["unreadCount"] == 0
    assert window.spoken == []


def _alias_window(monkeypatch, *, lid_chat=False):
    window = _window([_message("recent", NOW)])
    window.conversations_panel.refresh_messages_if_changed = Mock()
    window._lid_to_phone[LID] = JID
    window._phone_to_lid[JID] = LID
    window._merge_lid_into_phone = lambda lid, phone: IdentityMixin._merge_lid_into_phone(window, lid, phone)
    window._schedule_refresh_messages = Mock()
    window.on_historical_message = lambda msg: MessageEventsMixin.on_historical_message(window, msg)
    window._msg_bg_executor = SimpleNamespace(submit=lambda fn: fn())
    monkeypatch.setattr("main_window.identity.threading.Thread", _SyncThread)
    if lid_chat:
        window.chats[LID] = {"remoteJid": LID,
                             "messages": {"messages": {"records": [_message("lid-cached", NOW - 2 * 86400)]}}}
    return window


@pytest.mark.parametrize("lid_chat", [False, True])
def test_raw_alias_history_is_stored_and_refreshed_in_the_phone_chat(monkeypatch, inline_callbacks, lid_chat):
    window = _alias_window(monkeypatch, lid_chat=lid_chat)
    socket = _Socket()
    socket.main_window = window
    socket.on_wpp_message_received({"response": _raw(
        NOW - 86400, id=f"false_{LID}_REPLAY", **{"from": LID}, isMdHistoryMsg=True)})
    assert LID not in window.chats
    assert "REPLAY" in [m["key"]["id"] for m in window.chats[JID]["messages"]["messages"]["records"]]
    inserted_jid, inserted = window.db.insert_message.call_args.args
    assert inserted_jid == JID
    assert inserted["key"]["id"] == "REPLAY"
    window._schedule_refresh_messages.assert_called_once_with()
    assert window.chats[JID]["unreadCount"] == 0
    assert window.spoken == []
    if lid_chat:
        window.db.merge_or_rename_chat.assert_called_once_with(LID, JID)


def test_fresh_raw_history_flagged_alias_keeps_live_unread_handling(monkeypatch, inline_callbacks):
    window = _alias_window(monkeypatch)
    window.conversations_panel.conversation = None
    socket = _Socket()
    socket.main_window = window
    socket.on_wpp_message_received({"response": _raw(
        NOW + 1, id=f"false_{LID}_FRESH", **{"from": LID}, isMdHistoryMsg=True)})
    assert LID not in window.chats
    assert window.chats[JID]["unreadCount"] == 1


def test_unresolved_history_keeps_its_pending_insert_without_new_resolution(monkeypatch, inline_callbacks):
    window = _alias_window(monkeypatch)
    window._lid_to_phone.clear()
    window._phone_to_lid.clear()
    window._queue_lid_resolutions = Mock()
    futures = []

    def submit(fn):
        future = Future()
        future.set_result(fn())
        futures.append(future)
        return future

    window._msg_bg_executor = SimpleNamespace(submit=submit)
    socket = _Socket()
    socket.main_window = window
    socket.on_wpp_message_received({"response": _raw(
        NOW - 86400, id=f"false_{LID}_UNRESOLVED", **{"from": LID}, isMdHistoryMsg=True)})
    window._queue_lid_resolutions.assert_not_called()
    assert window._pending_lid_inserts[LID] is futures[-1]
    assert window.db.insert_message.call_args.args[0] == LID
