"""A cleared chat keeps counting messages that arrive after the clear.

"Limpar conversa" records a permanent cutoff in settings["cleared_chats"], and
the list-chats merge used it to zero the unread count of any cleared chat
whose snapshot had no lastMessage newer than the cutoff. list-chats never
carries a lastMessage at all (WPP.chat.list serialises msgs as null), so every
chat the user had ever cleared lost its badge on every 60-second merge.

Reported by several users as a group's unread counter "disappearing on its
own", usually a busy, muted group they had also cleared. Measured on one
log.log: the server counted 62, 63 ... 96 unread while the local count sat at
0 between every live event. Opening the chat then sent no read receipt,
because mark_conversation_as_read() only sent one for a positive local count,
so the phone kept the chat unread too.
"""

import pytest

import main
from main import MainWindow
from main_window.message_rules import clear_hides_unread
from tests.test_get_remote_chats_persistence import _chat, _make, post  # noqa: F401

GROUP = "120363000000000001@g.us"
CLEARED_AT = 1790000000


class TestClearHidesUnread:
    def test_nothing_newer_than_the_clear(self):
        assert clear_hides_unread(CLEARED_AT, 0, CLEARED_AT - 60) is True

    def test_activity_in_the_same_second_as_the_clear(self):
        assert clear_hides_unread(CLEARED_AT, 0, CLEARED_AT) is True

    def test_a_message_after_the_clear(self):
        assert clear_hides_unread(CLEARED_AT, 0, CLEARED_AT + 1) is False

    def test_the_last_message_alone_can_show_it(self):
        assert clear_hides_unread(CLEARED_AT, CLEARED_AT + 5, 0) is False

    def test_millisecond_activity_is_compared_as_seconds(self):
        assert clear_hides_unread(CLEARED_AT, 0, (CLEARED_AT - 1) * 1000) is True
        assert clear_hides_unread(CLEARED_AT, 0, (CLEARED_AT + 1) * 1000) is False

    def test_no_cutoff_hides_nothing(self):
        assert clear_hides_unread(0, 0, 0) is False
        assert clear_hides_unread(None, 0, CLEARED_AT) is False


def _cleared_group(unread):
    existing = {GROUP: {
        "remoteJid": GROUP, "t": CLEARED_AT - 3600, "unreadCount": unread,
        "messages": {"messages": {"records": []}},
    }}
    stub = _make(existing)
    stub.settings["cleared_chats"] = {GROUP: CLEARED_AT}
    return stub, existing


class TestTheChatListMerge:
    def test_messages_after_the_clear_keep_their_badge(self, post):
        """The reported case: a group cleared an hour ago, 62 unread since,
        and list-chats with no lastMessage (it never has one)."""
        stub, existing = _cleared_group(0)
        post["payload"] = [_chat(GROUP, unreadCount=62, t=CLEARED_AT + 3600)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[GROUP]["unreadCount"] == 62

    def test_and_a_live_count_is_not_undone_by_the_next_merge(self, post):
        """The see-saw in the log: a chats-update raised it, the next 60 s
        merge put it back to 0."""
        stub, existing = _cleared_group(62)
        post["payload"] = [_chat(GROUP, unreadCount=62, t=CLEARED_AT + 3600)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[GROUP]["unreadCount"] == 62

    def test_nothing_after_the_clear_stays_cleared(self, post):
        """What the cutoff is for: the pre-clear count does not come back."""
        stub, existing = _cleared_group(0)
        post["payload"] = [_chat(GROUP, unreadCount=40, t=CLEARED_AT - 10)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[GROUP]["unreadCount"] == 0

    def test_the_server_count_is_remembered_either_way(self, post):
        stub, existing = _cleared_group(0)
        post["payload"] = [_chat(GROUP, unreadCount=40, t=CLEARED_AT - 10)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert stub._server_unread[GROUP] == 40

    def test_an_lid_entry_counts_for_the_chat_kept_under_its_phone(self, post):
        """list-chats may answer a 1:1 chat under its @lid while WinZapp keeps
        it under the phone JID; that entry is skipped for the merge, but the
        count is still what opening the chat has to clear on the phone."""
        phone, lid = "5511999990000@s.whatsapp.net", "123456789012345@lid"
        existing = {phone: {"remoteJid": phone, "t": CLEARED_AT, "unreadCount": 0,
                            "messages": {"messages": {"records": []}}}}
        stub = _make(existing)
        stub._lid_to_phone = {lid: phone}
        stub._phone_to_lid = {phone: lid}
        post["payload"] = [_chat(lid, unreadCount=7, t=CLEARED_AT + 60)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert stub._server_unread == {phone: 7}


# ── Opening a chat sends the read the server needs ──────────────────────────


class _Reader:
    """Only what mark_conversation_as_read() touches."""

    mark_conversation_as_read = MainWindow.mark_conversation_as_read
    _note_server_unread = MainWindow._note_server_unread
    _pop_server_unread = MainWindow._pop_server_unread
    _anchor_unread_to_local_read = MainWindow._anchor_unread_to_local_read
    _normalize_jid = staticmethod(MainWindow._normalize_jid)

    def __init__(self, local_unread):
        self.chats = {GROUP: {"remoteJid": GROUP, "unreadCount": local_unread, "t": 1}}
        self.sent = []

    def _persist_locally_read_at(self):
        pass

    def _schedule_save(self, *a, **kw):
        pass

    def _refresh_chat_row_in_list(self, jid):
        pass

    def _schedule_set_chats(self):
        pass

    def _sync_conversation_read_state(self, remote_jid, unread, on_failure, on_success=None):
        self.sent.append(remote_jid)


@pytest.fixture(autouse=True)
def _call_after_inline(monkeypatch):
    monkeypatch.setattr(main.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


class TestOpeningAChat:
    def test_sends_the_read_when_only_the_server_counts_it_unread(self):
        """Local 0 (held down, or zeroed for a reason the server never
        heard of), server 96: the read has to reach the phone."""
        reader = _Reader(local_unread=0)
        reader._note_server_unread(GROUP, 96)

        reader.mark_conversation_as_read(GROUP)

        assert reader.sent == [GROUP]
        assert reader._pop_server_unread(GROUP) == 0

    def test_still_sends_for_a_local_count(self):
        reader = _Reader(local_unread=3)

        reader.mark_conversation_as_read(GROUP)

        assert reader.sent == [GROUP]

    def test_nothing_to_send_when_both_say_read(self):
        reader = _Reader(local_unread=0)
        reader._note_server_unread(GROUP, 0)

        reader.mark_conversation_as_read(GROUP)

        assert reader.sent == []

    def test_a_live_event_records_what_the_server_said(self):
        """on_chat_unread_update() notes the count before any guard decides
        the badge — here the sync gate drops the event entirely."""
        chat = {"remoteJid": GROUP, "unreadCount": 0}

        class _Live:
            on_chat_unread_update = MainWindow.on_chat_unread_update
            _note_server_unread = MainWindow._note_server_unread
            _normalize_jid = staticmethod(MainWindow._normalize_jid)
            _initial_sync_running = True
            _sync_completed = False

            def _resolve_chat_for_event(self, jid):
                return GROUP, chat

        live = _Live()
        live.on_chat_unread_update(GROUP, 96, 95)

        assert live._server_unread[GROUP] == 96
        assert chat["unreadCount"] == 0
