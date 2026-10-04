"""Tests for ConversationsPanel._forward_target_chats() including archived
chats/groups as forward targets.

Reported live: forwarding a message never offered an archived group (or any
archived chat) as a destination. conversations_panel.chats_list only ever
holds non-archived chats — archived ones live in a separate
ArchivedConversationsPanel with its own parallel chats_list/chat_names,
which the forward picker never looked at.
"""

from ui.conversations import ConversationsPanel

_forward_target_chats = ConversationsPanel._forward_target_chats


def _panel(chats, names):
    return type("Panel", (), {"chats_list": chats, "chat_names": names})()


def _mw(**panels):
    # contacts/chats: _forward_target_chats also folds in saved contacts
    # via build_own_contact_rows(), which reads both attributes directly.
    return type("MW", (), {"contacts": {}, "chats": {}, **panels})()


class TestForwardTargetChats:
    def test_includes_archived_chats_alongside_regular_ones(self):
        regular = {"remoteJid": "a@s.whatsapp.net"}
        archived_group = {"remoteJid": "g@g.us"}
        mw = _mw(
            conversations_panel=_panel([regular], ["Alice"]),
            archived_conversations_panel=_panel([archived_group], ["Archived Group"]),
        )

        chats, names = _forward_target_chats(mw)

        assert chats == [regular, archived_group]
        assert names == ["Alice", "Archived Group"]

    def test_no_archived_panel_falls_back_to_regular_chats_only(self):
        regular = {"remoteJid": "a@s.whatsapp.net"}
        mw = _mw(
            conversations_panel=_panel([regular], ["Alice"]),
        )

        chats, names = _forward_target_chats(mw)

        assert chats == [regular]
        assert names == ["Alice"]

    def test_a_chat_present_in_both_lists_is_not_duplicated(self):
        shared = {"remoteJid": "a@s.whatsapp.net"}
        mw = _mw(
            conversations_panel=_panel([shared], ["Alice"]),
            archived_conversations_panel=_panel([dict(shared)], ["Alice (archived copy)"]),
        )

        chats, names = _forward_target_chats(mw)

        assert len(chats) == 1
        assert names == ["Alice"]

    def test_empty_archived_panel_is_a_no_op(self):
        regular = {"remoteJid": "a@s.whatsapp.net"}
        mw = _mw(
            conversations_panel=_panel([regular], ["Alice"]),
            archived_conversations_panel=_panel([], []),
        )

        chats, names = _forward_target_chats(mw)

        assert chats == [regular]
        assert names == ["Alice"]
