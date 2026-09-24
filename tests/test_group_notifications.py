"""Tests for group-update system-message rendering (groupNotification /
Baileys "gp2" events): _get_message_content() and _is_displayable_message()
in ui/conversations.py.

Reported live: WPPConnect's raw, internal subtype codes ("sub_group_link",
"initial_phash_mismatch") were leaking almost verbatim into the message
list — "Atualização do grupo: initial phash mismatch" — because any subtype
the code didn't explicitly recognize fell back to
`subtype.replace("_", " ")`, which is not a translation, just underscores
turned into spaces. Also, resolving the acting participant's display name
for a @lid nobody has a saved contact/chat for used to return an empty
string (via _sender_label()), rendering as a blank name — e.g. a "left the
group" notification with nothing before "saiu do grupo" at all.

Fixes: initial_phash_mismatch/phash_mismatch (pure protocol/device-resync
housekeeping, never shown by the official client either) are now excluded
from _is_displayable_message() entirely; sub_group_link now has a proper
translated sentence; and the participant-name resolver used inside a group
notification is _get_participant_name() (already used elsewhere for group
participants) instead of _sender_label(), which never returns an empty
string — worst case is the person's own phone/@lid digits, never blank.

ConversationsPanel is a wx.Panel and cannot be instantiated without a
running wx.App, so the methods under test are exercised as plain functions
against a small stub — same approach as tests/test_message_bookmarks.py.
"""

import pytest

from main import MainWindow
from ui.conversations import ConversationsPanel


class _FakeI18n:
    _STRINGS = {
        "group_notif_left": "{names} saiu do grupo",
        "group_notif_linked_to_community": "{author} vinculou este grupo a uma comunidade",
        "group_notif_generic_detail": "Atualização do grupo: {detail}",
        "group_notif_generic": "Atualização do grupo",
        "group_notif_generic_author": "{author} fez uma atualização no grupo",
        "unknown_contact": "Contato sem nome",
        "unsupported_message": "Mensagem incompatível",
    }

    def t(self, key):
        return self._STRINGS[key]


class _FakeMainWindow:
    # The real lookup, not chats.get(): it falls back to the mapped
    # @lid/phone variant, which is the whole reason the panel calls it.
    # This stub already carries chats/_lid_to_phone/_phone_to_lid.
    get_chat = MainWindow.get_chat

    def __init__(self):
        self.i18n = _FakeI18n()
        self.contacts = {}
        self.chats = {}
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._presence_pushname_map = {}
        self.app_name = "WinZapp"

    def _is_self_jid(self, jid):
        return False

    def _is_bad_contact_name(self, name):
        return not name

    def self_reference_label(self):
        return "Você"

    # The real tolerant lookup (it only reads self.contacts): the panel's
    # name lookups go through it, so a stub answering None would hide every
    # saved contact.
    _get_contact_tolerant = MainWindow._get_contact_tolerant

    def _normalize_jid(self, jid):
        return jid

    def resolve_lid_jids_via_api(self, jids):
        # _get_participant_name() fires this on a background thread for an
        # unresolved @lid with no other way to learn a name — a no-op stub
        # is enough here since these tests assert on the immediate fallback
        # value, not on resolution actually completing.
        pass


class _Stub:
    _get_message_content = ConversationsPanel._get_message_content
    _sender_label = ConversationsPanel._sender_label
    _get_participant_name = ConversationsPanel._get_participant_name
    _is_displayable_message = ConversationsPanel._is_displayable_message
    _is_system_event = staticmethod(ConversationsPanel._is_system_event)

    def __init__(self):
        self.main_window = _FakeMainWindow()
        self.conversation = {"remoteJid": "12036312345@g.us"}
        self._sorted_messages = []
        self._group_participants_cache = []


def _gp2(subtype, author="133041125077153@lid", recipients=None):
    return {
        "key": {"id": "X", "fromMe": False, "remoteJid": "12036312345@g.us"},
        "messageType": "groupNotification",
        "message": {
            "groupNotification": {
                "subtype": subtype,
                "author": author,
                "recipients": recipients or [],
            }
        },
    }


class TestUnrecognizedSubtypesNowHandled:
    def test_sub_group_link_gets_a_real_translation(self):
        s = _Stub()
        content = s._get_message_content(_gp2("sub_group_link"))
        assert "sub_group_link" not in content
        assert "sub group link" not in content
        assert "vinculou este grupo a uma comunidade" in content

    def test_initial_phash_mismatch_is_not_displayable(self):
        """Pure protocol housekeeping — must never reach the user as a message."""
        s = _Stub()
        assert s._is_displayable_message(_gp2("initial_phash_mismatch")) is False

    def test_phash_mismatch_is_not_displayable(self):
        s = _Stub()
        assert s._is_displayable_message(_gp2("phash_mismatch")) is False

    def test_a_genuinely_unknown_subtype_is_still_displayable(self):
        """Only the specific known-noise subtypes are suppressed — anything
        else still shows (via the generic fallback) rather than vanishing."""
        s = _Stub()
        assert s._is_displayable_message(_gp2("some_new_subtype_wpp_added")) is True


class TestParticipantNameNeverBlank:
    def test_leave_with_no_known_name_is_not_blank(self):
        """Regression: an unresolvable @lid used to make _sender_label()
        return "", rendering as a bare " saiu do grupo" with no name at all."""
        s = _Stub()
        content = s._get_message_content(_gp2("leave"))
        assert content.strip() != "saiu do grupo"
        assert content != " saiu do grupo"
        # No saved contact/chat/pushName anywhere: the last-resort fallback
        # is the @lid's own digits (still concrete, never an empty string).
        assert "133041125077153" in content

    def test_leave_with_a_saved_contact_name_uses_it(self):
        s = _Stub()
        s.main_window.contacts["133041125077153@lid"] = {"name": "Carlos"}
        content = s._get_message_content(_gp2("leave"))
        assert content == "Carlos saiu do grupo"
