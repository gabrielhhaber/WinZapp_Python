from tests.locales import load_strings

from main import MainWindow
from ui.conversations import ConversationsPanel


class _I18n:
    TRANSLATIONS = {
        "notif_button": "[button]",
        "notif_deleted": "This message was deleted",
        "notif_list": "[list]",
        "notif_location": "[location]",
        "notif_poll": "[poll: {name}]",
        "poll_row_question": "[poll: {name}]",
        "poll_row_options": "{question} {options}",
        "notif_poll_no_name": "[poll]",
        "notif_system_message": "[system]",
        "notif_template": "[template]",
    }

    def t(self, key):
        return self.TRANSLATIONS.get(key, key)


class _ConversationStub:
    _get_message_content = ConversationsPanel._get_message_content

    def __init__(self):
        self.main_window = type("MainWindowStub", (), {"i18n": _I18n()})()


class _MainWindowStub:
    _counts_as_last_message = classmethod(MainWindow._counts_as_last_message.__func__)
    _last_msg_preview = MainWindow._last_msg_preview
    _PREVIEW_MESSAGE_TYPES = MainWindow._PREVIEW_MESSAGE_TYPES

    def __init__(self):
        self.i18n = _I18n()


def _message(message_type, payload, timestamp=1):
    return {
        "key": {"fromMe": False},
        "messageType": message_type,
        "message": {message_type: payload},
        "messageTimestamp": timestamp,
    }


def _chat_with(message):
    return {"messages": {"messages": {"records": [message]}}}


def test_deleted_message_in_open_conversation_uses_selected_language():
    message = _message("protocolMessage", {"type": 3})

    assert _ConversationStub()._get_message_content(message) == "This message was deleted"


def test_english_deleted_message_translation_matches_whatsapp_wording():
    translations = load_strings("en-US")

    assert translations["notif_deleted"] == "This message was deleted"


def test_deleted_message_in_chat_preview_uses_selected_language():
    message = _message("protocolMessage", {"type": "REVOKE"})

    preview = _MainWindowStub()._last_msg_preview(_chat_with(message))

    assert preview.startswith("This message was deleted")


def test_related_open_conversation_previews_use_translation_keys():
    stub = _ConversationStub()

    assert stub._get_message_content(_message("pollCreationMessage", {"name": "Lunch"})) == "[poll: Lunch]"
    assert stub._get_message_content(_message("liveLocationMessage", {})) == "[location]"
    assert stub._get_message_content(_message("templateMessage", {})) == "[template]"
    assert stub._get_message_content(_message("protocolMessage", {"type": 14})) == "[system]"


def test_related_chat_list_previews_use_translation_keys():
    stub = _MainWindowStub()
    cases = (
        ("pollCreationMessageV2", {"name": "Lunch"}, "[poll: Lunch]"),
        ("liveLocationMessage", {}, "[location]"),
        ("buttonsMessage", {}, "[button]"),
        ("listMessage", {}, "[list]"),
        ("templateMessage", {}, "[template]"),
    )

    for message_type, payload, expected in cases:
        preview = stub._last_msg_preview(_chat_with(_message(message_type, payload)))
        assert preview.startswith(expected)


def test_interactive_content_reaches_the_row_and_chat_preview():
    panel = _ConversationStub()
    main = _MainWindowStub()
    cases = (
        ("buttonsMessage", {"contentText": "Choose a trip", "buttons": [
            {"buttonText": {"displayText": "Tomorrow"}}]}, "Choose a trip"),
        ("templateMessage", {"hydratedContentText": "Your ticket"}, "Your ticket"),
        ("interactiveMessage", {"body": {"text": "Bus at noon"}}, "Bus at noon"),
    )
    for message_type, payload, expected in cases:
        message = _message(message_type, payload)
        assert expected in panel._get_message_content(message)
        assert main._last_msg_preview(_chat_with(message)).startswith(expected)
