"""WhatsApp Web's "Aguardando mensagem" placeholder is a row, not a hole.

Reported 2026-09-23: in a group, a reply quoted a message that never showed up
in the list, and it read as WinZapp having failed to fetch it. The original was
stored by a sync as a `ciphertext` -- WhatsApp Web had not decrypted it, and
stayed that way for good, like every other message from that sender since he
changed phone. The type was not displayable, so the row was simply skipped.

WhatsApp shows those as "Aguardando mensagem. Isso pode levar alguns
instantes." and so does WinZapp now. It still never bumps a badge nor becomes
a chat's preview, and once the decrypted copy arrives the same record renders
as the real message (MainWindow._fill_stored_placeholder).

ConversationsPanel needs a wx.App, so the methods run against a plain stub.
"""

from main import MainWindow, is_countable_message
from ui.conversations import ConversationsPanel


class _I18n:
    def t(self, key):
        return f"[{key}]"


class _Stub:
    _get_message_content = ConversationsPanel._get_message_content
    _is_displayable_message = ConversationsPanel._is_displayable_message

    def __init__(self):
        self.main_window = type("MW", (), {"i18n": _I18n(), "app_name": "WinZapp"})()
        self._download_progress = {}


def _placeholder():
    return {
        "key": {"remoteJid": "120363409931936700@g.us", "fromMe": False,
                "id": "3EB079236745EBC27DC09C", "participant": "1@lid"},
        "message": {},
        "messageType": "ciphertext",
        "messageTimestamp": 1790159249,
    }


def test_the_placeholder_is_a_row():
    assert _Stub()._is_displayable_message(_placeholder()) is True


def test_it_reads_as_waiting_rather_than_incompatible():
    """"Mensagem incompatível" would say WinZapp cannot show it; the truth is
    that WhatsApp itself does not have the content yet."""
    assert _Stub()._get_message_content(_placeholder()) == "[message_awaiting_decryption]"


def test_it_never_counts_nor_becomes_the_chat_preview():
    assert is_countable_message(_placeholder()) is False
    assert MainWindow._counts_as_last_message(_placeholder()) is False


def test_once_filled_the_same_record_renders_the_real_message():
    record = _placeholder()
    record["message"] = {"conversation": "não chega nem a 1mb"}
    record["messageType"] = "conversation"

    assert _Stub()._get_message_content(record) == "não chega nem a 1mb"
