"""Which messages WhatsApp counts as unread, in WinZapp's record types.

Two rules of WhatsApp Web, read from its code over CDP on 2026-11-12, and
WinZapp mirrors both:

* **The count** (``unreadCount``) goes up when a message arrives, for a
  received message whose event type is DEFAULT or AMBIENT
  (WAWebHandleNewMsgAction, WAWebMsgDataUtils.eventTypeFromMsgType). That is
  every ordinary message, the "waiting for this message" placeholder
  (``ciphertext``) and the group's "message history" notice, and of the group
  notifications only two, rare ones. Never: a message deleted for everyone,
  the other group notifications (join, leave, promote, subject...), the
  encryption, business and system notices, calls, reactions, edits, nor
  anything the account itself sent. `counted_on_arrival()`.

* **The divider** sits on the ``unreadCount``-th message from the end among
  the types of WAWebMsgGetters.getIsUnreadType, a list of real content
  (WAWebConversationMsgsUtils). The history notice is not in it.
  `counts_as_unread()`.

WinZapp used to count every record for both, and to subtract from the count
whatever it would not show as a message: in a busy group the deleted
messages and notices among the unread ones took their places, and a group
with 141 unread opened with 12 of them above the separator, as read
(reported 2026-11-12).
"""

from core.call_log import CALL_LOG_MESSAGE_TYPE, LEGACY_CALL_LOG_TYPE
from core.view_once import VIEW_ONCE_UNAVAILABLE_TYPE

#: getIsUnreadType's types, by the messageType WinZapp stores them as
UNREAD_MESSAGE_TYPES = frozenset({
    "conversation", "extendedTextMessage",                 # chat
    "imageMessage", "videoMessage", "ptvMessage", "audioMessage",
    "documentMessage", "stickerMessage", VIEW_ONCE_UNAVAILABLE_TYPE,
    "contactMessage", "contactsArrayMessage",              # vcard, multi_vcard
    "locationMessage", "liveLocationMessage",
    "pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3",
    "eventMessage", "interactiveMessage", "listMessage",
    "productMessage", "groupInviteMessage", "groups_v4_invite",
    "newsletterFollowerInviteMessage", "newsletterAdminInviteMessage",
    "ciphertext", "album", "stickerPackMessage",
})

#: what eventTypeFromMsgType never makes DEFAULT or AMBIENT
NOT_COUNTED_ON_ARRIVAL = frozenset({
    "protocolMessage",              # a deletion for everyone, an edit, a setting
    "reactionMessage", "pollUpdateMessage",
    CALL_LOG_MESSAGE_TYPE, LEGACY_CALL_LOG_TYPE,
    "notification_template", "e2e_notification", "notification",
    "broadcast_notification", "revoked",
})

#: the group notifications eventTypeFromMsgType makes DEFAULT
COUNTED_GROUP_NOTIFICATIONS = frozenset({"community_owner_update",
                                         "created_subgroup_suggestion"})


def _received(msg) -> bool:
    return isinstance(msg, dict) and not (msg.get("key") or {}).get("fromMe")


def counts_as_unread(msg) -> bool:
    """A received message of a type WhatsApp Web's divider counts."""
    return _received(msg) and msg.get("messageType") in UNREAD_MESSAGE_TYPES


def counted_on_arrival(msg) -> bool:
    """A received message WhatsApp adds to the chat's unreadCount."""
    if not _received(msg):
        return False
    kind = msg.get("messageType")
    if kind == "groupNotification":
        notice = (msg.get("message") or {}).get("groupNotification") or {}
        return notice.get("subtype") in COUNTED_GROUP_NOTIFICATIONS
    return bool(kind) and kind not in NOT_COUNTED_ON_ARRIVAL


def first_unread_index(displayable, unread_count: int) -> int:
    """Index of the first unread message in *displayable* (oldest first), as
    WhatsApp Web's divider finds it: the *unread_count*-th message from the
    end among those counts_as_unread() accepts. -1 when the loaded history
    does not hold that many."""
    if unread_count <= 0:
        return -1
    seen = 0
    for idx in range(len(displayable) - 1, -1, -1):
        if counts_as_unread(displayable[idx]):
            seen += 1
            if seen == unread_count:
                return idx
    return -1


def unread_after_own_send(records, unread_count: int) -> int:
    """*unread_count* as it can be, given the records (any order): a message
    the account sent marks the chat as read on WhatsApp, so only what arrived
    after the newest one can be unread. A count higher than that is one
    WhatsApp had not lowered yet (the reply came from the phone). Without an
    own message in *records* the count stands: the unread messages may be
    older than what is loaded."""
    if unread_count <= 0 or not records:
        return unread_count
    ordered = sorted((m for m in records if isinstance(m, dict)), key=_arrival_order)
    newest_own = max((i for i, m in enumerate(ordered) if _sent_here(m)), default=-1)
    if newest_own < 0:
        return unread_count
    after = sum(1 for m in ordered[newest_own + 1:] if counted_on_arrival(m))
    return min(unread_count, after)


def _sent_here(msg) -> bool:
    """A message the account sent that reached WhatsApp: real content, not a
    notice WhatsApp writes on the account's side (a business template, an
    encryption notice) nor one that failed or was cancelled."""
    return (bool((msg.get("key") or {}).get("fromMe"))
            and msg.get("messageType") in UNREAD_MESSAGE_TYPES
            and not msg.get("_send_failed") and not msg.get("_cancelled_awaiting_id"))


def _arrival_order(msg):
    """Oldest first, whatever order the records came in (the database gives
    them newest first). A send still pending has no time yet: the newest.
    Within one second an own send is taken as the older, so a reply never
    hides a message that arrived with it."""
    from core.remote_deletions import message_timestamp_seconds
    seconds = message_timestamp_seconds(msg)
    if not seconds:
        return (float("inf"), 0)
    return (seconds, 0 if _sent_here(msg) else 1)
