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

Since 2026-11-12 the system events are not subtracted at all: WhatsApp never
counts them (core/unread_types.py, read from WhatsApp Web's code), so that
group's 72 was 72 real unread messages and every "discount" lost some. The
only correction left, an own reply reading the chat, is idempotent; the mark
stays so the merge and the history sync still agree on who corrects.
"""

from main import (
    MainWindow,
    _UNREAD_UNDISCOUNTED,
    _discount_non_countable_unread,
    apply_history_sync_unread_correction,
    records_cover_snapshot,
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


def _replied_tail():
    """The same group with an own reply at record 30: 64 messages after it
    (and five notices, never counted),
    so a count of 72 is one WhatsApp had not lowered after the reply."""
    tail = _group_tail()
    tail[30] = {"key": {"id": "mine", "fromMe": True}, "messageType": "conversation",
                "message": {"conversation": "eu"}, "timestamp": 1700000030}
    return tail


def _group_chat(unread, records=None, **extra):
    chat = {"remoteJid": GROUP, "t": 1700000100, "unreadCount": unread,
            "messages": {"messages": {"records": records if records is not None
                                      else _group_tail()}}}
    chat.update(extra)
    return chat


class TestTheCorrection:
    def test_system_events_among_the_unread_take_nothing(self):
        """The reported group: its 72 were 72 real unread messages."""
        assert _discount_non_countable_unread(_group_tail(), 72) == 72

    def test_correcting_twice_changes_nothing(self):
        records = _replied_tail()
        once = _discount_non_countable_unread(records, 72)
        assert once == 64
        assert _discount_non_countable_unread(records, once) == once


class TestTheHistorySyncCorrection:
    def test_an_already_discounted_count_is_left_alone(self):
        chat = _group_chat(67)

        assert not apply_history_sync_unread_correction(GROUP, chat)
        assert chat["unreadCount"] == 67

    def test_a_raw_count_is_corrected_exactly_once(self):
        chat = _group_chat(72, records=_replied_tail(), **{_UNREAD_UNDISCOUNTED: True})

        assert apply_history_sync_unread_correction(GROUP, chat)
        assert chat["unreadCount"] == 64
        assert _UNREAD_UNDISCOUNTED not in chat
        assert not apply_history_sync_unread_correction(GROUP, chat)
        assert chat["unreadCount"] == 64

    def test_the_mark_survives_until_there_are_records_to_judge_by(self):
        chat = _group_chat(72, records=[], **{_UNREAD_UNDISCOUNTED: True})

        assert not apply_history_sync_unread_correction(GROUP, chat)
        assert chat[_UNREAD_UNDISCOUNTED] is True


class _LiveStub:
    on_chat_unread_update = MainWindow.on_chat_unread_update
    _normalize_jid = staticmethod(MainWindow._normalize_jid)
    _note_server_unread = MainWindow._note_server_unread
    _pop_server_unread = MainWindow._pop_server_unread
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
        assert chat["unreadCount"] == 72

        apply_history_sync_unread_correction(GROUP, chat)
        assert chat["unreadCount"] == 72, (
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
    def test_a_count_merged_against_current_records_is_final(self, post):
        existing = {GROUP: _group_chat(60)}
        stub = _make(existing)
        # The newest stored record (1700000099) is what the snapshot counted to.
        post["payload"] = [_chat(GROUP, unreadCount=72, t=1700000099)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[GROUP]["unreadCount"] == 72
        assert _UNREAD_UNDISCOUNTED not in existing[GROUP]
        assert not apply_history_sync_unread_correction(GROUP, existing[GROUP])
        assert existing[GROUP]["unreadCount"] == 72

    def test_a_count_merged_against_a_stale_tail_stays_raw(self, post):
        """Warm start: the records are what the database had at launch, and the
        snapshot counts a message that arrived while WinZapp was closed -- here
        an own voice note sent from the phone. Discounting against the stale
        tail finds nothing to discount; marked final, the post-fetch correction
        that sees the real tail never ran and "1 unread" stayed on the badge."""
        existing = {GROUP: _group_chat(0, records=[_incoming(i) for i in range(10)])}
        stub = _make(existing)
        post["payload"] = [_chat(GROUP, unreadCount=1, t=1700000500)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[GROUP]["unreadCount"] == 1
        assert existing[GROUP][_UNREAD_UNDISCOUNTED] is True
        own_voice_note = {"key": {"id": "own", "fromMe": True},
                          "messageType": "audioMessage", "message": {},
                          "timestamp": 1700000500}
        existing[GROUP]["messages"]["messages"]["records"].append(own_voice_note)
        assert apply_history_sync_unread_correction(GROUP, existing[GROUP])
        assert existing[GROUP]["unreadCount"] == 0


class TestRecordsCoverSnapshot:
    def test_a_tail_reaching_the_snapshot_covers_it(self):
        assert records_cover_snapshot([_incoming(5)], 1700000005)

    def test_a_tail_older_than_the_snapshot_does_not(self):
        assert not records_cover_snapshot([_incoming(5)], 1700000006)

    def test_no_records_cover_nothing(self):
        assert not records_cover_snapshot([], 1700000006)

    def test_a_snapshot_without_a_time_cannot_say_otherwise(self):
        assert records_cover_snapshot([_incoming(5)], 0)

    def test_millisecond_timestamps_are_compared_in_seconds(self):
        assert records_cover_snapshot([_incoming(5)], 1700000005000)

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


class TestTheLivePathHasNoMarker:
    def test_a_live_count_follows_whatsapp_whatever_the_tail(self):
        """A read group gets a group notification and chats-update says 1
        unread. WhatsApp never adds a promote to unreadCount
        (eventTypeFromMsgType: IGNORE), so the 1 is something else WhatsApp
        counted, and WhatsApp Web shows it too: the badge follows it."""
        records = [{"key": {"id": "m1", "fromMe": False}, "messageType": "conversation",
                    "message": {"conversation": "lida"}, "timestamp": 1000},
                   {"key": {"id": "p1", "fromMe": False}, "messageType": "groupNotification",
                    "message": {}, "timestamp": 2000}]
        chat = {"remoteJid": GROUP, "t": 1000, "unreadCount": 0,
                "messages": {"messages": {"records": records}}}
        stub = _LiveStub(chat)
        stub.on_chat_unread_update(GROUP, 1, previous_unread=0)
        assert chat["unreadCount"] == 1
