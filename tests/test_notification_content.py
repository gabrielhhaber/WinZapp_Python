"""Tests for notification content-building — format_notification_body() and
format_notification_title()'s group-name resolution.

Regression coverage for two bugs found in the same broader audit that fixed
is_countable_message() (tests/test_countable_message.py) and
_is_bad_contact_name() (tests/test_bad_contact_name.py):

1. format_notification_body() had no case for several message types
   is_countable_message() already treats as real, notification-worthy
   content (polls, buttons, lists, templates, interactive messages/replies,
   liveLocationMessage) — they all fell through to "Mensagem incompatível"
   even though ConversationsPanel._get_message_content() already knew how
   to describe every one of them in the open conversation.

2. format_notification_title()'s group branch called
   MainWindow._resolve_contact_name(chat), whose own docstring says
   "Groups are skipped" (always returns None for a group) — dead code —
   and never called _group_name_from_chat_dict(), which the chat list
   itself already relies on for WPPConnect's raw chat shape (a group's
   real name nested under groupMetadata.subject, not a flat "name" key).
   A notification could say "Grupo sem nome" for a group the chat list
   displayed correctly, in the exact stale-metadata window right after a
   fresh pairing.
"""

import pytest

from core.notification_manager import format_notification_body, format_notification_title
from main import MainWindow
from main_window.contacts import ContactsMixin


class _FakeI18n:
    """Returns a recognizable template per key so assertions can check
    exactly which branch fired, without depending on real translations."""

    _STRINGS = {
        "notif_unsupported": "[unsupported]",
        "notif_location": "[location]",
        "notif_poll": "[poll:{name}]",
        "notif_poll_no_name": "[poll-no-name]",
        "notif_template": "[template]",
        "interactive_message": "[interactive]",
        "interactive_reply": "[interactive-reply]",
        "list_reply": "[list-reply]",
        "options": "options",
        "unknown_group": "[unknown-group]",
        "unnamed_participant": "[unnamed-participant]",
        "notif_in_group": "{participant} em {group}",
        "notif_replied_to_you": "",
        "notif_mentioned_you": "",
        "notif_mentioned_all": "",
    }

    def t(self, key):
        return self._STRINGS.get(key, f"[{key}]")


class _WordsI18n:
    _STRINGS = {"ui_self_reference_eu": "Eu", "ui_self_reference_voce": "Você"}

    def t(self, key):
        return self._STRINGS[key]


def _msg(message_type, message=None, **extra):
    m = {"messageType": message_type, "message": message or {}, "key": {}}
    m.update(extra)
    return m


class TestFormatNotificationBodyCoversRealContentTypes:
    """Every type is_countable_message() treats as real content must
    produce SOMETHING other than the generic "unsupported" fallback."""

    def test_poll_with_a_name(self):
        msg = _msg("pollCreationMessage", {"pollCreationMessage": {"name": "Pizza ou sushi?"}})
        body = format_notification_body(msg, None, _FakeI18n())
        assert body == "[poll:Pizza ou sushi?]"

    def test_poll_without_a_name(self):
        msg = _msg("pollCreationMessage", {"pollCreationMessage": {}})
        body = format_notification_body(msg, None, _FakeI18n())
        assert body == "[poll-no-name]"

    @pytest.mark.parametrize("poll_type", [
        "pollCreationMessageV2", "pollCreationMessageV3",
    ])
    def test_poll_variants(self, poll_type):
        msg = _msg(poll_type, {poll_type: {"name": "Enquete"}})
        body = format_notification_body(msg, None, _FakeI18n())
        assert body == "[poll:Enquete]"

    def test_live_location_reuses_location_text(self):
        msg = _msg("liveLocationMessage")
        assert format_notification_body(msg, None, _FakeI18n()) == "[location]"

    def test_buttons_message_shows_its_content_text(self):
        msg = _msg("buttonsMessage", {"buttonsMessage": {"contentText": "Escolha uma opção"}})
        assert format_notification_body(msg, None, _FakeI18n()) == "Escolha uma opção"

    def test_buttons_message_with_no_text_falls_back(self):
        msg = _msg("buttonsMessage", {"buttonsMessage": {}})
        assert format_notification_body(msg, None, _FakeI18n()) == "[interactive]"

    def test_list_message_shows_its_title(self):
        msg = _msg("listMessage", {"listMessage": {"title": "Cardápio"}})
        assert format_notification_body(msg, None, _FakeI18n()) == "Cardápio"

    def test_template_message(self):
        msg = _msg("templateMessage")
        assert format_notification_body(msg, None, _FakeI18n()) == "[template]"

    def test_interactive_message_shows_body_text(self):
        msg = _msg("interactiveMessage", {"interactiveMessage": {"body": {"text": "Olá!"}}})
        assert format_notification_body(msg, None, _FakeI18n()) == "Olá!"

    def test_buttons_response_message(self):
        msg = _msg("buttonsResponseMessage", {"buttonsResponseMessage": {"selectedDisplayText": "Sim"}})
        assert format_notification_body(msg, None, _FakeI18n()) == "Sim"

    def test_buttons_response_with_no_text_falls_back(self):
        msg = _msg("buttonsResponseMessage", {"buttonsResponseMessage": {}})
        assert format_notification_body(msg, None, _FakeI18n()) == "[interactive-reply]"

    def test_list_response_message(self):
        msg = _msg("listResponseMessage", {"listResponseMessage": {"title": "Opção 2"}})
        assert format_notification_body(msg, None, _FakeI18n()) == "Opção 2"

    def test_list_response_falls_back_to_row_id_then_generic(self):
        msg = _msg("listResponseMessage", {"listResponseMessage": {
            "singleSelectReply": {"selectedRowId": "row-2"}
        }})
        assert format_notification_body(msg, None, _FakeI18n()) == "row-2"

        empty = _msg("listResponseMessage", {"listResponseMessage": {}})
        assert format_notification_body(empty, None, _FakeI18n()) == "[list-reply]"


class TestExtendedTextMessageLinkPreviewInToastBody:
    """Reported live: a link with a preview (title/description WhatsApp
    generated for the URL) showed the preview text in the conversation list
    (ConversationsPanel._get_message_content()) but the toast notification
    for the same message only ever showed the raw pasted URL — the toast
    body-builder never read extendedTextMessage.title/.description at all.
    """

    class _MW:
        def __init__(self, show_link_previews=True):
            self.settings = {"user_interface": {"show_link_previews": show_link_previews}}
            self._lid_to_phone = {}

        @staticmethod
        def _is_self_jid(jid):
            return False

    def test_preview_title_and_description_precede_the_link(self):
        msg = _msg("extendedTextMessage", {"extendedTextMessage": {
            "text": "https://example.com/artigo",
            "title": "Exemplo",
            "description": "Um artigo de exemplo",
        }})
        body = format_notification_body(msg, self._MW(), _FakeI18n())
        assert body == "Exemplo. Um artigo de exemplo. https://example.com/artigo"

    def test_only_title_is_shown_when_there_is_no_description(self):
        msg = _msg("extendedTextMessage", {"extendedTextMessage": {
            "text": "https://example.com",
            "title": "Exemplo",
        }})
        body = format_notification_body(msg, self._MW(), _FakeI18n())
        assert body == "Exemplo. https://example.com"

    def test_no_preview_fields_leaves_the_text_untouched(self):
        msg = _msg("extendedTextMessage", {"extendedTextMessage": {
            "text": "https://example.com",
        }})
        body = format_notification_body(msg, self._MW(), _FakeI18n())
        assert body == "https://example.com"

    def test_disabled_setting_suppresses_the_preview(self):
        msg = _msg("extendedTextMessage", {"extendedTextMessage": {
            "text": "https://example.com",
            "title": "Exemplo",
            "description": "Um artigo de exemplo",
        }})
        body = format_notification_body(msg, self._MW(show_link_previews=False), _FakeI18n())
        assert body == "https://example.com"

    def test_no_main_window_defaults_to_showing_the_preview(self):
        """format_notification_body() is called with main_window=None in
        some tests/paths above; the setting must default the same way
        ConversationsPanel's own check does (default True)."""
        msg = _msg("extendedTextMessage", {"extendedTextMessage": {
            "text": "https://example.com",
            "title": "Exemplo",
        }})
        body = format_notification_body(msg, None, _FakeI18n())
        assert body == "Exemplo. https://example.com"


class TestMentionOfTheUserFollowsHowToReferToMe:
    """Reported live: with "Como se referir a mim?" set to "Você" or a custom
    word, the message list showed that word for a mention of the user but the
    toast still said a fixed "eu"."""

    class _MW:
        _is_self_jid = staticmethod(lambda jid: jid == "5511@s.whatsapp.net")
        _lid_to_phone = {}

        def __init__(self, **ui):
            self.settings = {"user_interface": ui}
            self.i18n = _WordsI18n()

        self_reference_label = ContactsMixin.self_reference_label

    @staticmethod
    def _toast(main_window):
        msg = _msg("extendedTextMessage", {"extendedTextMessage": {
            "text": "oi @5511 tudo bem?",
            "contextInfo": {"mentionedJid": ["5511@s.whatsapp.net"]},
        }})
        return format_notification_body(msg, main_window, _FakeI18n())

    def test_first_person_is_the_default(self):
        assert self._toast(self._MW()) == "oi @Eu tudo bem?"

    def test_voce_mode(self):
        assert self._toast(self._MW(self_reference_mode="voce")) == "oi @Você tudo bem?"

    def test_a_custom_word(self):
        mw = self._MW(self_reference_mode="custom", self_reference_custom_word="Chefe")
        assert self._toast(mw) == "oi @Chefe tudo bem?"


class TestFormatNotificationTitleGroupName:
    def test_uses_chat_name_when_present(self):
        class _MW:
            chats = {"g@g.us": {"remoteJid": "g@g.us", "name": "Família"}}

            @staticmethod
            def _group_name_from_chat_dict(chat):
                return ""  # should not even be needed here

        msg = _msg("conversation", key={"remoteJid": "g@g.us"})
        title = format_notification_title(msg, _MW(), _FakeI18n())
        assert "Família" in title

    def test_falls_back_to_group_name_from_chat_dict_when_name_is_missing(self):
        """The actual bug: WPPConnect's raw chat shape nests the real name
        under groupMetadata.subject, not a flat "name" key."""
        class _MW:
            chats = {"g@g.us": {"remoteJid": "g@g.us", "groupMetadata": {"subject": "Turma 2026"}}}

            @staticmethod
            def _group_name_from_chat_dict(chat):
                return (chat.get("groupMetadata") or {}).get("subject", "")

        msg = _msg("conversation", key={"remoteJid": "g@g.us"})
        title = format_notification_title(msg, _MW(), _FakeI18n())
        assert "Turma 2026" in title

    def test_unknown_group_only_when_nothing_resolves(self):
        class _MW:
            chats = {}

            @staticmethod
            def _group_name_from_chat_dict(chat):
                return ""

        msg = _msg("conversation", key={"remoteJid": "g@g.us"})
        title = format_notification_title(msg, _MW(), _FakeI18n())
        assert "[unknown-group]" in title


class TestChatListPreviewOfAMentionOfTheUser:
    """The same fixed "eu" lived in the chat-list preview: with "Você" chosen
    the toast said "@Você" and the row of the same message said "@eu"."""

    class _MW:
        _counts_as_last_message = classmethod(MainWindow._counts_as_last_message.__func__)
        _last_msg_preview = MainWindow._last_msg_preview
        _PREVIEW_MESSAGE_TYPES = MainWindow._PREVIEW_MESSAGE_TYPES
        self_reference_label = ContactsMixin.self_reference_label
        _is_self_jid = staticmethod(lambda jid: jid == "5511@s.whatsapp.net")
        _lid_to_phone = {}

        def __init__(self, **ui):
            self.settings = {"user_interface": {
                "show_delivery_status_in_chat_list": False, **ui}}
            self.i18n = _WordsI18n()

    @staticmethod
    def _chat():
        message = {
            "key": {"fromMe": False, "id": "abc"},
            "messageType": "extendedTextMessage",
            "message": {"extendedTextMessage": {
                "text": "oi @5511 tudo bem?",
                "contextInfo": {"mentionedJid": ["5511@s.whatsapp.net"]}}},
            "messageTimestamp": 1_700_000_000,
        }
        return {"remoteJid": "5522@s.whatsapp.net",
                "messages": {"messages": {"records": [message]}}}

    def test_first_person_is_the_default(self):
        assert "oi @Eu tudo bem?" in self._MW()._last_msg_preview(self._chat())

    def test_voce_mode(self):
        preview = self._MW(self_reference_mode="voce")._last_msg_preview(self._chat())
        assert "oi @Você tudo bem?" in preview

    def test_a_custom_word(self):
        mw = self._MW(self_reference_mode="custom", self_reference_custom_word="Chefe")
        assert "oi @Chefe tudo bem?" in mw._last_msg_preview(self._chat())
