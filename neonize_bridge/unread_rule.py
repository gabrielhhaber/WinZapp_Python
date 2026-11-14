"""Which arriving messages WhatsApp adds to a chat's unread count.

WhatsApp Web (WAWebMsgDataUtils.eventTypeFromMsgType, read over CDP on
2026-11-12) never counts a message deleted for everyone, a group or system
notification, a call, a reaction, an edit nor anything the account sent; the
client mirrors the same rule (client/core/unread_types.py). The bridge only
stores the ordinary kinds and the deletion as arriving messages, so the
deletion is the one it used to count wrongly: a group where someone deleted
three messages showed three more unread than WhatsApp.
"""

#: WhatsApp Web types that never raise unreadCount when they arrive
NOT_COUNTED = frozenset({"revoked", "protocol", "gp2", "notification", "e2e_notification",
                         "notification_template", "call_log", "reaction", "poll_update"})


def counted(w) -> bool:
    """True when the arriving message *w* (WhatsApp Web shape) is unread."""
    return isinstance(w, dict) and not w.get("fromMe") and w.get("type") not in NOT_COUNTED
