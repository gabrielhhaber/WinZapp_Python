"""The unread discount must not eat real messages behind later system events.

Reported: after an update the window title fell from 18 unread conversations
to 15 and went back to 18 only on F5. Measured on a live session the same
day: 29 conversations unread on WhatsApp Web read 0 in WinZapp. Most were
groups whose newest stored records were group notifications (an invite, a
description change, a member added) stamped after the chat's activity
marker `t` -- WhatsApp does not move a chat for those, so its unread count
was about the real messages just before them. The discount took the newest
N records, found N notifications and wiped the badge.

The rest was a tie: an incoming text and the business notification_template
WhatsApp writes in the same second, in database order, so the tail of 1 was
the template.
"""

from main_window.message_rules import (
    _UNREAD_UNDISCOUNTED,
    _discount_non_countable_unread,
    apply_history_sync_unread_correction,
)


def _msg(message_type, ts, from_me=False):
    return {
        "key": {"fromMe": from_me, "id": f"{message_type}-{ts}"},
        "messageType": message_type,
        "message": {},
        "messageTimestamp": ts,
    }


def _group_with_notifications_after_t():
    # Three real messages up to t=1000, then three notifications that did
    # not move the chat.
    return [
        _msg("conversation", 990),
        _msg("conversation", 995),
        _msg("conversation", 1000),
        _msg("groupNotification", 2000),
        _msg("groupNotification", 2100),
        _msg("groupNotification", 2200),
    ]


def test_notifications_newer_than_t_do_not_cancel_the_unread_messages():
    records = _group_with_notifications_after_t()
    assert _discount_non_countable_unread(records, 3, 1000) == 3


def test_without_a_marker_the_notifications_still_take_nothing():
    """Since 2026-11-12 nothing is cut from a tail: WhatsApp never counts a
    group notification (core/unread_types.py), wherever it is stamped."""
    records = _group_with_notifications_after_t()
    assert _discount_non_countable_unread(records, 3) == 3


def test_a_notification_at_t_is_not_in_the_count_either():
    records = [_msg("conversation", 900), _msg("groupNotification", 1000)]
    assert _discount_non_countable_unread(records, 1, 1000) == 1


def test_a_notice_written_on_our_side_is_not_a_reply():
    """A business template or encryption notice flagged as ours does not
    read the chat: only a message the account sent does."""
    records = [_msg("conversation", 900), _msg("notification_template", 950, from_me=True),
               _msg("e2e_notification", 960, from_me=True)]
    assert _discount_non_countable_unread(records, 1) == 1


def test_own_send_at_t_is_still_discounted():
    records = [_msg("conversation", 900), _msg("conversation", 1000, from_me=True)]
    assert _discount_non_countable_unread(records, 1, 1000) == 0


def test_millisecond_marker_is_read_in_seconds():
    t = 1_790_000_000
    records = [
        _msg("conversation", t),
        _msg("groupNotification", t + 600),
    ]
    assert _discount_non_countable_unread(records, 1, t * 1000) == 1


def test_only_records_after_t_leaves_the_count_as_the_server_said():
    records = [_msg("groupNotification", 2000)]
    assert _discount_non_countable_unread(records, 2, 1000) == 2


def test_same_second_template_does_not_take_the_real_messages_place():
    template = _msg("notification_template", 1000, from_me=True)
    text = _msg("conversation", 1000)
    assert _discount_non_countable_unread([text, template], 1) == 1
    assert _discount_non_countable_unread([template, text], 1) == 1


def test_history_sync_correction_keeps_the_badge_of_such_a_group():
    chat = {
        "t": 1000,
        "unreadCount": 3,
        _UNREAD_UNDISCOUNTED: True,
        "messages": {"messages": {"records": _group_with_notifications_after_t()}},
    }
    assert apply_history_sync_unread_correction("g@g.us", chat) is False
    assert chat["unreadCount"] == 3
