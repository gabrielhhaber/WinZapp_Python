"""A reaction must not cancel an unread message out of the badge.

Reported 2026-09-23: "FETIN 2026 - Equipes" showed 1 unread; someone reacted
with a heart to a document in it, and the counter disappeared while the text
was still unread. The group's records ended:

    documentMessage, conversation (the unread one), reaction x4

_discount_non_countable_unread() cut the last `unread` records -- one -- and
found a reaction, which is not countable, so it discounted it: 1 - 1 = 0.
get_remote_chats() then weighed that 0 against the local 1, and since the
reactions had moved the chat's activity marker forward, the snapshot looked
newer and won.

WinZapp stores each reaction as a record of its own (`_rxn_...`) to decorate
the message it points at; WhatsApp never counted one as unread. So reactions
are left out before the tail is cut, instead of being discounted inside it.
"""

from main import _discount_non_countable_unread

from tests.test_get_remote_chats_persistence import _chat, _make, post  # noqa: F401

JID = "120363000000000001@g.us"


def _text(mid, ts):
    return {"key": {"id": mid, "fromMe": False, "participant": "1@lid"},
            "messageType": "conversation", "message": {"conversation": mid},
            "messageTimestamp": ts}


def _document(mid, ts):
    return {"key": {"id": mid, "fromMe": False, "participant": "1@lid"},
            "messageType": "documentMessage",
            "message": {"documentMessage": {"fileName": "edital.pdf"}},
            "messageTimestamp": ts}


def _reaction(target, ts, n):
    return {"key": {"id": f"_rxn_{target}_{n}", "fromMe": False, "participant": f"{n}@lid"},
            "messageType": "reactionMessage",
            "message": {"reactionMessage": {"key": {"id": target}, "text": "❤️"}},
            "messageTimestamp": ts}


def _fetin_records():
    """The group's tail, as it was stored."""
    return [
        _document("DOC", 1790180376),
        _text("TEXT", 1790180518),
        _reaction("TEXT", 1790180655, 1),
        _reaction("DOC", 1790180655, 2),
        _reaction("TEXT", 1790180658, 3),
        _reaction("DOC", 1790180673, 4),
    ]


class TestTheDiscount:
    def test_trailing_reactions_do_not_cancel_the_unread_text(self):
        assert _discount_non_countable_unread(_fetin_records(), 1) == 1

    def test_the_unread_messages_are_still_the_ones_counted(self):
        assert _discount_non_countable_unread(_fetin_records(), 2) == 2

    def test_a_system_event_after_the_reactions_is_still_discounted(self):
        records = _fetin_records() + [{
            "key": {"id": "SYS", "fromMe": False}, "messageType": "groupNotification",
            "message": {}, "messageTimestamp": 1790180700}]
        assert _discount_non_countable_unread(records, 2) == 1

    def test_an_own_reply_after_the_reactions_is_still_discounted(self):
        records = _fetin_records() + [{
            "key": {"id": "MINE", "fromMe": True}, "messageType": "conversation",
            "message": {"conversation": "eu"}, "messageTimestamp": 1790180700}]
        assert _discount_non_countable_unread(records, 2) == 1

    def test_a_chat_holding_only_reactions_is_left_as_the_server_said(self):
        records = [_reaction("OLD", 1790180655, 1)]
        assert _discount_non_countable_unread(records, 1) == 1


class TestTheBadgeSurvivesTheChatListMerge:
    def test_the_fetin_group_keeps_its_unread_text(self, post):
        """End to end through get_remote_chats(): local 1, snapshot 1 with the
        activity marker the reactions moved forward. It used to store 0."""
        existing = {JID: {
            "remoteJid": JID, "t": 1790180518, "unreadCount": 1,
            "messages": {"messages": {"records": _fetin_records()}},
        }}
        stub = _make(existing)
        stub.conversations_panel = None  # not the open chat
        post["payload"] = [_chat(JID, unreadCount=1, t=1790180673)]

        stub.get_remote_chats(existing, persist_full=False, notify_errors=False)

        assert existing[JID]["unreadCount"] == 1
