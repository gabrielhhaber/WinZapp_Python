"""An unread count is discounted once, never twice.

Diagnosed from a real session's log.log. A busy group ("Cegos Brasil (Beta)")
was seen falling from 68 to 62 unread with nothing read; the log showed the
badge see-sawing once a minute between two numbers:

    chats-update in: <group> unread=72 previous=71
    [unread] <group>: 61 -> 67 (previous=71, open=False, read_ack=None).
    [unread] <group>: 67 -> 62 after history sync (own sends / system events
      the chat-list snapshot counted).

on_chat_unread_update() discounted the server's 72 down to 67 against the
record tail. Then sync_chat_messages()'s apply_history_sync_unread_correction()
discounted the stored 67 again. _discount_non_countable_unread() is not
idempotent: the last 67 records still hold the same system events the last
72 did, so they are subtracted a second time. The same shape repeated for
every chat with a system event in its unread tail (`11 -> 10` 28 times in
the same log), which also kept those chats "changed" in every periodic round.

The correction exists for one case only: a count merged from list-chats while
the chat had no records to discount it against. That case is now marked on
the chat, and the correction runs only on the mark.
"""

from main import (
    MainWindow,
    _UNREAD_UNDISCOUNTED,
    _discount_non_countable_unread,
    apply_history_sync_unread_correction,
)

from tests.test_get_remote_chats_persistence import _chat, _make, post  # noqa: F401

GROUP = "120363000000000000@g.us"


def _incoming(i):
    return {"key": {"id": f"m{i}", "fromMe": False}, "messageType": "conversation",
            "message": {"conversation": f"msg {i}"}, "timestamp": 1700000000 + i}


def _system(i):
    return {"key": {"id": f"sys{i}", "fromMe": False},
            "messageType": "groupNotification", "message": {},
            "timestamp": 1700000000 + i}


def _group_tail():
    """100 records, with five system events scattered through the newest 72 —
    the shape that made the real group lose six unread per sync."""
    return [_system(i) if i in (40, 50, 60, 70, 80) else _incoming(i)
            for i in range(100)]


def _group_chat(unread, records=None, **extra):
    chat = {"remoteJid": GROUP, "t": 1700000100, "unreadCount": unread,
            "messages": {"messages": {"records": records if records is not None
                                      else _group_tail()}}}
    chat.update(extra)
    return chat


class TestTheDiscountIsNotIdempotent:
    """Why the mark is needed at all: pins the arithmetic the bug came from."""

    def test_discounting_twice_loses_real_unread_messages(self):
        records = _group_tail()
        once = _discount_non_countable_unread(records, 72)
        assert once == 67
        assert _discount_non_countable_unread(records, once) < once


class TestTheHistorySyncCorrection:
    def test_an_already_discounted_count_is_left_alone(self):
        chat = _group_chat(67)

        assert not apply_history_sync_unread_correction(GROUP, chat)
        assert chat["unreadCount"] == 67

    def test_a_raw_count_is_discounted_exactly_once(self):
        chat = _group_chat(72, **{_UNREAD_UNDISCOUNTED: True})

        assert apply_history_sync_unread_correction(GROUP, chat)
        assert chat["unreadCount"] == 67
        assert _UNREAD_UNDISCOUNTED not in chat
        assert not apply_history_sync_unread_correction(GROUP, chat)
        assert chat["unreadCount"] == 67

    def test_the_mark_survives_until_there_are_records_to_judge_by(self):
        chat = _group_chat(72, records=[], **{_UNREAD_UNDISCOUNTED: True})

        assert not apply_history_sync_unread_correction(GROUP, chat)
        assert chat[_UNREAD_UNDISCOUNTED] is True


class _LiveStub:
    on_chat_unread_update = MainWindow.on_chat_unread_update
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _remote_read_confirmed = staticmethod(MainWindow._remote_read_confirmed)
    _resolve_chat_for_event = MainWindow._resolve_chat_for_event
    _persist_locally_read_at = MainWindow._persist_locally_read_at
    _unread_anchored_to_local_read = MainWindow._unread_anchored_to_local_read

    def __init__(self, chat):
        self.chats = {GROUP: chat}
        self._initial_sync_running = False
        self._sync_completed = True
        self.conversations_panel = None
        self._locally_read_at = {}
        self._new_since_read = {}
        self._unread_read_anchors = set()

    def _schedule_save(self, dirty_jid=None):
        pass

    def _schedule_set_chats(self):
        pass


class TestTheReportedSeeSaw:
    def test_a_live_update_followed_by_a_history_sync_keeps_its_count(self):
        """The regression, in the order the log recorded it."""
        chat = _group_chat(61)
        stub = _LiveStub(chat)

        stub.on_chat_unread_update(GROUP, 72, previous_unread=71)
        assert chat["unreadCount"] == 67

        apply_history_sync_unread_correction(GROUP, chat)
        assert chat["unreadCount"] == 67, (
            "the history sync discounted a count the live update had already "
            "discounted — the badge drops by the system events in the tail "
            "on every sync"
        )

    def test_a_live_update_clears_a_stale_raw_mark(self):
        chat = _group_chat(61, **{_UNREAD_UNDISCOUNTED: True})
        stub = _LiveStub(chat)

        stub.on_chat_unread_update(GROUP, 72, previous_unread=71)

        assert _UNREAD_UNDISCOUNTED not in chat


class TestTheChatListMerge:
    def test_a_count_merged_against_records_is_final(self, post):
        existing = {GROUP: _group_chat(60)}
        stub = _make(existing)
        post["payload"] = [_chat(GROUP, unreadCount=72, t=1700000500)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[GROUP]["unreadCount"] == 67
        assert _UNREAD_UNDISCOUNTED not in existing[GROUP]
        assert not apply_history_sync_unread_correction(GROUP, existing[GROUP])
        assert existing[GROUP]["unreadCount"] == 67

    def test_a_count_merged_without_records_is_marked_raw(self, post):
        existing = {GROUP: _group_chat(0, records=[])}
        stub = _make(existing)
        post["payload"] = [_chat(GROUP, unreadCount=72, t=1700000500)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[GROUP]["unreadCount"] == 72
        assert existing[GROUP][_UNREAD_UNDISCOUNTED] is True

    def test_a_chat_new_to_the_list_is_marked_raw(self, post):
        stub = _make({})
        post["payload"] = [_chat(GROUP, unreadCount=5, t=1700000500,
                                 name="Grupo de teste")]

        result = stub.get_remote_chats({}, persist_full=False, notify_errors=False)

        assert result[GROUP][_UNREAD_UNDISCOUNTED] is True
