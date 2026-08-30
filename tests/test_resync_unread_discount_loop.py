"""The 60s resync must store the count it decided on, not the raw snapshot.

Diagnosed from a real session's log.log, not inferred. The same group produced
39 consecutive lines, one per minute for the whole session, always the same
numbers:

    [unread] 120363151058129530@g.us: 31 -> 30 after history sync
      (own sends / system events the chat-list snapshot counted).

and the periodic delta never once reported zero chats to sync:

    [periodic_contacts_sync] message delta: 0 full/new, 2 incremental, 152 unchanged.

The loop, and why it never settles:

1. list-chats reports 31. get_remote_chats()'s merge computes the discounted
   value (30) into `server_val` and uses it to pick a branch — but the final
   branch never assigned it back to `v`, so `chats[jid]["unreadCount"]` was
   written with the RAW 31.
2. sync_chat_messages() then runs apply_history_sync_unread_correction(),
   which discounts the very same chat back down to 30.
3. _capture_chat_sync_baseline() snapshots 30.
4. Next round merges 31 again; chat_sync_marker_changed() reads 31 != 30 as
   "this chat changed" and schedules it. Back to 2, forever, for a chat where
   nothing whatsoever happened.

The cost is not wasted HTTP. Re-syncing the chat the user has OPEN runs
_refresh_open_conversation_after_sync() -> refresh_messages_if_changed(),
whose signature legitimately differs, so populate_messages() rebuilds the
native ListView with DeleteAllItems() + one Append() per row. A screen reader
is handed an entirely new list once a minute, mid-sentence, and the unread
separator and focus position go with it. Reported exactly that way, and it is
the same failure refresh_messages_if_changed()'s own docstring describes.

The other two branches of that merge already assign `v` from `server_val`
(directly, or through reconcile_*); only the fall-through did not.
"""

import pytest

from main import _discount_non_countable_unread

from tests.test_get_remote_chats_persistence import _Stub, _chat, _make, post  # noqa: F401

JID = "120363151058129530@g.us"


def _records(countable: int, system_events: int):
    """A record tail shaped like the real group's: real incoming messages,
    then a few system events the server counts toward unread and WinZapp
    deliberately does not."""
    records = [
        {"key": {"id": f"m{i}", "fromMe": False}, "messageType": "conversation",
         "message": {"conversation": f"msg {i}"}, "timestamp": 1700000000 + i}
        for i in range(countable)
    ]
    records += [
        {"key": {"id": f"sys{i}", "fromMe": False},
         "messageType": "groupNotification",
         "message": {}, "timestamp": 1700001000 + i}
        for i in range(system_events)
    ]
    return records


class TestTheDiscountIsActuallyStored:
    def test_the_stored_count_matches_what_the_merge_decided(self, post):
        """The regression. The merge discounted 31 -> 30 to choose its branch
        and then stored 31 anyway, so the next round's marker comparison saw a
        change that had not happened."""
        records = _records(countable=30, system_events=1)
        existing = {JID: {
            "remoteJid": JID, "t": 1700000000, "unreadCount": 30,
            "messages": {"messages": {"records": records}},
        }}
        stub = _make(existing)
        stub.conversations_panel = None  # not the open chat
        post["payload"] = [_chat(JID, unreadCount=31, t=1700000500)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[JID]["unreadCount"] == 30, (
            "the merge stored the raw snapshot count instead of the discounted "
            "one it just computed — apply_history_sync_unread_correction() "
            "will discount it again, the baseline will disagree with the next "
            "snapshot, and this chat is re-synced every 60s forever"
        )

    def test_the_stored_count_is_stable_across_a_second_identical_round(self, post):
        """The property that actually matters: an unchanged server value must
        leave the stored value alone, so the sync marker can settle. Running
        the same snapshot twice is the cheapest way to state that."""
        records = _records(countable=30, system_events=1)
        existing = {JID: {
            "remoteJid": JID, "t": 1700000000, "unreadCount": 30,
            "messages": {"messages": {"records": records}},
        }}
        stub = _make(existing)
        stub.conversations_panel = None
        post["payload"] = [_chat(JID, unreadCount=31, t=1700000500)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)
        first = existing[JID]["unreadCount"]
        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[JID]["unreadCount"] == first, (
            "the same snapshot produced a different stored count on the second "
            "round — the marker can never settle and the chat re-syncs forever"
        )

    def test_a_chat_with_nothing_to_discount_is_untouched(self, post):
        """The discount must not become a general haircut: with no system
        events and no own sends in the tail, the server's number stands."""
        records = _records(countable=4, system_events=0)
        existing = {JID: {
            "remoteJid": JID, "t": 1700000000, "unreadCount": 2,
            "messages": {"messages": {"records": records}},
        }}
        stub = _make(existing)
        stub.conversations_panel = None
        post["payload"] = [_chat(JID, unreadCount=4, t=1700000500)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[JID]["unreadCount"] == 4


class TestTheDiscountItself:
    """Guards the arithmetic the fix now depends on being stored."""

    def test_one_trailing_system_event_costs_one(self):
        assert _discount_non_countable_unread(_records(30, 1), 31) == 30

    def test_own_sends_in_the_tail_count_too(self):
        records = _records(3, 0) + [
            {"key": {"id": "mine", "fromMe": True}, "messageType": "conversation",
             "message": {"conversation": "eu"}, "timestamp": 1700002000}
        ]
        assert _discount_non_countable_unread(records, 4) == 3

    def test_nothing_to_discount_leaves_it_alone(self):
        assert _discount_non_countable_unread(_records(5, 0), 3) == 3
