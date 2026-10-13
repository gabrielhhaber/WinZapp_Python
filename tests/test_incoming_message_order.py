"""Late websocket rows must use the debounced chronological rebuild."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ui.conversation_panel.formatting import FormattingMixin
from ui.conversation_panel.list_refresh import ListRefreshMixin
from ui.conversation_panel.unread_separator import UnreadSeparatorMixin
from tests.test_message_list_refresh import _Stub, _msg


class _Panel(_Stub):
    on_incoming_message = ListRefreshMixin.on_incoming_message
    _extract_timestamp = FormattingMixin._extract_timestamp

    def __init__(self, records):
        super().__init__(records)
        self.main_window = SimpleNamespace(
            _allow_ui_focus_changes=lambda: False,
            _schedule_refresh_messages=Mock(),
        )
        self._matches_open_conversation = lambda jid: jid == "g@g.us"
        self._counts_toward_unread_separator = lambda msg: True
        self._update_unread_separator_for_incoming = Mock()
        self._clear_empty_placeholder = lambda: None
        self.populate_messages(preserve_focus=True)
        self.populate_calls.clear()


@pytest.fixture(autouse=True)
def no_typing_control(monkeypatch):
    monkeypatch.setattr("ui.conversation_panel.list_refresh.dismiss_typing_row_for_message", lambda *a: None)


@pytest.mark.parametrize("stamp", [1_799_913_600, 1_799_913_600_000])
def test_yesterdays_row_is_not_appended_or_marked_as_new_unread(stamp):
    panel = _Panel([_msg("recent", ts=1_800_000_000)])
    incoming = _msg("yesterday", ts=stamp)
    panel._records.append(incoming)
    panel.on_incoming_message("g@g.us", incoming)
    assert [m["key"]["id"] for m in panel._sorted_messages] == ["recent"]
    panel.main_window._schedule_refresh_messages.assert_called_once_with()
    panel._update_unread_separator_for_incoming.assert_not_called()
    panel.refresh_messages_if_changed()
    assert [m["key"]["id"] for m in panel._sorted_messages] == ["yesterday", "recent"]
    assert panel.populate_calls == [True]


@pytest.mark.parametrize("stamp", [1_800_000_000, 1_800_000_001])
def test_genuine_tail_delivery_keeps_the_fast_append(stamp):
    panel = _Panel([_msg("recent", ts=1_800_000_000)])
    incoming = _msg("new", ts=stamp)
    panel._records.append(incoming)
    panel.on_incoming_message("g@g.us", incoming)
    assert [m["key"]["id"] for m in panel._sorted_messages] == ["recent", "new"]
    panel.main_window._schedule_refresh_messages.assert_not_called()
    panel._update_unread_separator_for_incoming.assert_called_once_with(incoming)


def test_live_tail_keeps_the_local_pagination_list_in_step():
    panel = _Panel([_msg("recent", ts=1_800_000_000)])
    incoming = _msg("new", ts=1_800_000_001)
    panel._records.append(incoming)
    panel.on_incoming_message("g@g.us", incoming)
    assert panel._all_sorted_messages[-1] is incoming
    assert panel._all_sorted_messages is not panel._sorted_messages


def test_duplicate_and_other_chat_deliveries_do_nothing():
    panel = _Panel([_msg("recent", ts=1_800_000_000)])
    panel.on_incoming_message("g@g.us", _msg("recent", ts=1_800_000_000))
    panel.on_incoming_message("other@g.us", _msg("old", ts=1_799_000_000))
    assert len(panel._sorted_messages) == 1
    panel.main_window._schedule_refresh_messages.assert_not_called()


def test_unread_separator_insertion_does_not_break_pagination_alignment():
    panel = _Panel([_msg("recent", ts=1_800_000_000)])
    panel.messages_list.InsertItem = lambda index, text: panel.messages_list.rows.insert(index, text)
    panel._update_unread_separator_for_incoming.side_effect = lambda message: (
        UnreadSeparatorMixin._update_unread_separator_for_incoming(panel, message))
    incoming = _msg("new", ts=1_800_000_001)
    panel._records.append(incoming)
    panel.on_incoming_message("g@g.us", incoming)
    assert panel._sorted_messages[-2]["_type"] == "unread_separator"
    assert panel._all_sorted_messages[-1] is incoming
    assert len(panel._all_sorted_messages) == 2


def test_aliased_pagination_lists_cannot_double_append():
    panel = _Panel([_msg("recent", ts=1_800_000_000)])
    panel._all_sorted_messages = panel._sorted_messages
    incoming = _msg("new", ts=1_800_000_001)
    panel._records.append(incoming)
    panel.on_incoming_message("g@g.us", incoming)
    assert len(panel._sorted_messages) == 2
    assert panel._sorted_messages[-1] is incoming


def test_own_sent_row_ahead_of_the_server_clock_does_not_make_a_reply_late():
    # The PC clock runs ahead: the user's own send is stamped after the reply
    # the server stamps a moment later. The reply is still a new tail row.
    sent = _msg("sent", ts=1_800_000_100, _local_id="local-1")
    sent["key"]["fromMe"] = True
    panel = _Panel([_msg("recent", ts=1_800_000_000), sent])
    reply = _msg("reply", ts=1_800_000_050)
    panel._records.append(reply)
    panel.on_incoming_message("g@g.us", reply)
    assert panel._sorted_messages[-1] is reply
    panel.main_window._schedule_refresh_messages.assert_not_called()
    panel._update_unread_separator_for_incoming.assert_called_once_with(reply)


def test_replay_older_than_the_last_server_row_is_late_despite_an_own_send():
    sent = _msg("sent", ts=1_800_000_100, _local_id="local-1")
    sent["key"]["fromMe"] = True
    panel = _Panel([_msg("recent", ts=1_800_000_000), sent])
    replay = _msg("old", ts=1_799_000_000)
    panel._records.append(replay)
    panel.on_incoming_message("g@g.us", replay)
    assert [m["key"]["id"] for m in panel._sorted_messages] == ["recent", "sent"]
    panel.main_window._schedule_refresh_messages.assert_called_once_with()
