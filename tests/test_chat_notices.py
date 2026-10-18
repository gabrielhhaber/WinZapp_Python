"""Encryption and disappearing-messages notices for one-to-one chats
(core/chat_notices.py). Plain data in, text out: no wx, no network."""

import pytest

from core.chat_notices import conversation_start_text, encryption_lines, is_one_to_one

PN = "5511999990000@s.whatsapp.net"
CUS = "5511999990000@c.us"
LID = "99887766554433@lid"
GROUP = "120363000000000001@g.us"
EMPTY = "No messages available in this chat."


class _I18n:
    TEXT = {
        "chat_encryption_label": "Encryption",
        "chat_encryption_notice": "Messages and calls are end-to-end encrypted. "
        "Only people in this chat can read, listen to or share them.",
        "chat_encryption_notice_short": "Messages and calls are end-to-end encrypted.",
        "chat_disappearing_label": "Disappearing messages",
        "chat_disappearing_off": "Off",
        "chat_disappearing_on": "On",
        "chat_disappearing_24h": "24 hours",
        "chat_disappearing_7d": "7 days",
        "chat_disappearing_90d": "90 days",
    }

    def t(self, key):
        return self.TEXT[key]


class _Owner:
    def __init__(self, chat=None):
        if chat is not None:
            self.get_chat = lambda jid: chat


class TestWhoGetsAnEncryptionClaim:
    @pytest.mark.parametrize("jid", [PN, CUS, LID])
    def test_a_persons_jid(self, jid):
        assert is_one_to_one(jid)

    @pytest.mark.parametrize(
        "jid",
        [GROUP, "status@broadcast", "123@broadcast", "120363@newsletter", "", None, 5],
    )
    def test_groups_lists_channels_and_junk_get_none(self, jid):
        assert not is_one_to_one(jid)


class TestTheInfoDialogLine:
    def test_a_personal_chat_gets_the_full_sentence(self):
        lines = encryption_lines(PN, _I18n())
        assert lines == [
            "Encryption: Messages and calls are end-to-end encrypted. "
            "Only people in this chat can read, listen to or share them."
        ]

    @pytest.mark.parametrize("jid", [GROUP, "120363@newsletter", "", None])
    def test_other_chats_get_no_claim(self, jid):
        assert encryption_lines(jid, _I18n()) == []


class TestTheEmptyConversationRow:
    def _text(self, owner, jid=PN):
        return conversation_start_text(owner, jid, _I18n(), EMPTY)

    def test_the_encryption_notice_follows_the_empty_text(self):
        assert self._text(_Owner()) == (
            f"{EMPTY} Messages and calls are end-to-end encrypted."
        )

    @pytest.mark.parametrize(
        "seconds,words",
        [(86400, "24 hours"), (604800, "7 days"), (7776000, "90 days"), (1234, "On")],
    )
    def test_a_running_timer_is_added(self, seconds, words):
        owner = _Owner({"ephemeralDuration": seconds})
        assert self._text(owner) == (
            f"{EMPTY} Messages and calls are end-to-end encrypted. "
            f"Disappearing messages: {words}."
        )

    @pytest.mark.parametrize(
        "chat",
        [{"ephemeralDuration": 0}, {"remoteJid": PN}, {"ephemeralDuration": "x"}, {}],
    )
    def test_an_off_or_unknown_timer_adds_nothing(self, chat):
        owner = _Owner(chat)
        assert self._text(owner) == (
            f"{EMPTY} Messages and calls are end-to-end encrypted."
        )

    @pytest.mark.parametrize("jid", [GROUP, "120363@newsletter", "", None])
    def test_any_other_chat_keeps_the_original_text(self, jid):
        owner = _Owner({"ephemeralDuration": 86400})
        assert conversation_start_text(owner, jid, _I18n(), EMPTY) == EMPTY

    def test_an_owner_without_get_chat_is_fine(self):
        assert self._text(object()) == (
            f"{EMPTY} Messages and calls are end-to-end encrypted."
        )
