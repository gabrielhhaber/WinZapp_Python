"""Tests for ConversationsPanel._forward_target_chats() including saved
contacts as forward targets.

The forward picker used to offer only existing conversations (main +
archived panels). A contact the user never opened a chat with was
unreachable from the forward dialog entirely, even though the app knows
them — "Nova conversa" and every other picker list them — so "forward
this to Mom" failed the moment Mom's chat had never been opened (or was
only ever on the phone). Targets now also come from the shared
build_own_contact_rows(), one row per person, with the vault's locked
chats still kept out of the dialog.
"""

from core.utils import format_number
from ui.conversations import ConversationsPanel

_forward_target_chats = ConversationsPanel._forward_target_chats


def _panel(chats, names):
    return type("Panel", (), {"chats_list": chats, "chat_names": names})()


def _mw(contacts, chats=None, **attrs):
    # _normalize_jid/_lid_to_phone stay absent on purpose where a test
    # doesn't need them: contact_dedup_key() must degrade to raw-JID
    # handling, the same way it does for any stub MainWindow.
    return type("MW", (), {
        "contacts": contacts,
        "chats": chats or {},
        **attrs,
    })()


class TestForwardIncludesContacts:
    def test_saved_contact_without_chat_is_offered_after_chats(self):
        chat = {"remoteJid": "g@g.us"}
        mw = _mw(
            {"5511999999999@c.us": {"isMyContact": True, "name": "Mom"}},
            conversations_panel=_panel([chat], ["Group"]),
        )

        chats, names = _forward_target_chats(mw)

        assert chats[0] is chat                      # chats keep priority
        assert chats[1]["remoteJid"] == "5511999999999@c.us"
        assert names == ["Group", "Mom"]

    def test_contact_covered_by_a_chat_is_not_duplicated(self):
        # Same person, bridged JID variants: chat under @s.whatsapp.net,
        # contact under @c.us — contact_dedup_key() folds both to the bare
        # number, so the chat already in the list is the one target.
        chat = {"remoteJid": "551199999999@s.whatsapp.net"}
        mw = _mw(
            {"551199999999@c.us": {"isMyContact": True, "name": "Mom"}},
            conversations_panel=_panel([chat], ["Mom"]),
        )

        chats, names = _forward_target_chats(mw)

        assert len(chats) == 1
        assert names == ["Mom"]

    def test_brazilian_9th_digit_variant_folds_into_same_target(self):
        chat = {"remoteJid": "551199999999@s.whatsapp.net"}   # 8-digit form
        mw = _mw(
            {"5511999999999@c.us": {"isMyContact": True, "name": "Mom"}},  # 9-digit
            conversations_panel=_panel([chat], ["Mom"]),
        )

        chats, _names = _forward_target_chats(mw)

        assert len(chats) == 1

    def test_unsaved_contact_without_chat_is_not_offered(self):
        # Names learned from group presence carry no saved flag and no
        # 1:1 chat — junk in every picker, junk here too.
        mw = _mw(
            {"551177777777@c.us": {"name": "Stranger From A Group"}},
            conversations_panel=_panel([], []),
        )

        chats, names = _forward_target_chats(mw)

        assert chats == []
        assert names == []

    def test_unbridged_lid_contact_is_not_offered(self):
        # A bare @lid that never resolved to a phone number can't be
        # dialed, formatted, or deduped — build_own_contact_rows() drops
        # it and so must the forward list.
        mw = _mw(
            {"123456789@lid": {"isMyContact": True, "name": "Ghost"}},
            conversations_panel=_panel([], []),
        )

        chats, names = _forward_target_chats(mw)

        assert chats == []
        assert names == []

    def test_bridged_lid_saved_contact_is_offered_once(self):
        # The same person stored under @lid AND their phone form collapses
        # to a single target (the phone-JID row, as everywhere else).
        mw = _mw(
            {
                "123456789@lid": {"isMyContact": True, "name": "Mom"},
                "441234567890@c.us": {"isMyContact": True, "name": "Mom"},
            },
            _lid_to_phone={"123456789@lid": "441234567890@s.whatsapp.net"},
            conversations_panel=_panel([], []),
        )

        chats, names = _forward_target_chats(mw)

        assert len(chats) == 1
        assert names == ["Mom"]
        assert not chats[0]["remoteJid"].endswith("@lid")

    def test_contact_whose_only_chat_is_locked_is_not_offered(self):
        # The locked-chat vault promises locked chats stay out of the
        # forward dialog; re-listing the person from the contact list
        # would break that promise by name.
        mw = _mw(
            {"551188888888@c.us": {"isMyContact": True, "name": "Secret"}},
            conversations_panel=_panel([], []),
            _locked_chat_rows=([{"remoteJid": "551188888888@s.whatsapp.net"}], ["Secret"]),
        )

        chats, names = _forward_target_chats(mw)

        assert chats == []
        assert names == []

    def test_nameless_saved_contact_shows_formatted_number_not_raw_jid(self):
        jid = "5511999999999@c.us"
        mw = _mw(
            {jid: {"isMyContact": True, "name": ""}},
            conversations_panel=_panel([], []),
        )

        chats, names = _forward_target_chats(mw)

        assert len(chats) == 1
        assert names[0] == format_number(jid)
        assert "@" not in names[0]
