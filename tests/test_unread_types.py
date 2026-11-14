"""WhatsApp's unread rules, mirrored (core/unread_types.py).

Reported 2026-11-12: opening the group "WinZapp | desenvolvedores e
testers" with 141 unread put 12 of them above the separator, as read. The
log showed the badge discounted from 141 to 129 ("135 -> 123 after history
sync"), and the separator then walked back over every incoming record,
deletions and notices included. WhatsApp Web's code (read over CDP the same
day) counts by two rules, pinned here.
"""
import pytest

from core.unread_types import (counted_on_arrival, counts_as_unread, first_unread_index,
                               unread_after_own_send)


def _msg(kind, ts, mine=False, **message):
    return {"key": {"id": f"{kind}-{ts}", "fromMe": mine}, "messageType": kind,
            "message": message, "messageTimestamp": ts}


@pytest.mark.parametrize(("kind", "counted", "divider"), [
    ("conversation", True, True), ("audioMessage", True, True),
    ("imageMessage", True, True), ("ciphertext", True, True),       # AMBIENT
    ("message_history_notice", True, False),     # DEFAULT, but not getIsUnreadType
    ("protocolMessage", False, False),           # a deletion for everyone, an edit
    ("groupNotification", False, False), ("reactionMessage", False, False),
    ("callLogMessage", False, False), ("notification_template", False, False),
    ("e2e_notification", False, False), ("pollUpdateMessage", False, False),
])
def test_what_whatsapp_counts_and_where_its_divider_looks(kind, counted, divider):
    assert counted_on_arrival(_msg(kind, 1)) is counted
    assert counts_as_unread(_msg(kind, 1)) is divider
    # nothing the account sent is ever unread
    assert not counted_on_arrival(_msg(kind, 1, mine=True))
    assert not counts_as_unread(_msg(kind, 1, mine=True))


def test_two_group_notifications_are_counted():
    """eventTypeFromMsgType makes these two DEFAULT: they raise unreadCount."""
    owner = _msg("groupNotification", 1, groupNotification={"subtype": "community_owner_update"})
    promote = _msg("groupNotification", 1, groupNotification={"subtype": "promote"})
    assert counted_on_arrival(owner) and not counted_on_arrival(promote)


def _busy_group():
    """The reported shape: 141 unread messages after a read one, with
    deletions, joins and history notices among them, oldest first."""
    records = [_msg("conversation", 1)]                       # read
    ts = 2
    for i in range(141):
        records.append(_msg("conversation" if i % 3 else "audioMessage", ts))
        ts += 1
        if i % 12 == 5:
            records += [_msg("protocolMessage", ts), _msg("groupNotification", ts + 1),
                        _msg("message_history_notice", ts + 2)]
            ts += 3
    return records


def test_the_count_of_the_reported_group_is_kept():
    assert unread_after_own_send(_busy_group(), 141) == 141


def test_the_separator_sits_above_the_oldest_unread_message():
    records = _busy_group()
    index = first_unread_index(records, 141)
    assert index == 1, "the message right after the read one"
    assert sum(1 for m in records[index:] if counts_as_unread(m)) == 141


def test_a_reply_reads_the_chat_and_only_what_came_after_is_unread():
    records = [_msg("conversation", 1), _msg("conversation", 2),
               _msg("audioMessage", 3, mine=True),
               _msg("groupNotification", 4), _msg("conversation", 5)]
    assert unread_after_own_send(records, 3) == 1
    assert unread_after_own_send(list(reversed(records)), 3) == 1   # database order
    # a reply in the same second as a message never hides it
    same_second = [_msg("conversation", 7), _msg("conversation", 7, mine=True)]
    assert unread_after_own_send(same_second, 1) == 1


def test_without_a_reply_the_count_stands_even_beyond_the_records():
    """The unread messages may be older than what is loaded."""
    assert unread_after_own_send([_msg("conversation", 1)], 50) == 50
    assert unread_after_own_send([], 3) == 3


def test_a_failed_or_system_send_is_not_a_reply():
    failed = dict(_msg("conversation", 5, mine=True), _send_failed=True)
    template = _msg("notification_template", 6, mine=True)
    records = [_msg("conversation", 1), failed, template]
    assert unread_after_own_send(records, 1) == 1
