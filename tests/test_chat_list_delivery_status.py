"""Tests for the "show delivery status in chat list" setting.

New feature: MainWindow._last_msg_preview() can append the last message's
delivery status (read/delivered/sent/played/pending/failed) after the
timestamp, reusing ConversationsPanel._map_status() — the exact same string
shown when navigating the open conversation's message list — gated by
settings["user_interface"]["show_delivery_status_in_chat_list"] (default
True).
"""

from main import MainWindow
from ui.conversations import ConversationsPanel


class _I18n:
    TRANSLATIONS = {
        "status_read": "Read",
        "status_delivered": "Delivered",
        "status_sent": "Sent",
        "status_pending": "Pending",
        "status_failed": "Failed",
        "status_unconfirmed": "Unconfirmed",
        "status_played": "Played",
    }

    def t(self, key):
        return self.TRANSLATIONS.get(key, key)


class _ConversationsPanelStub:
    _map_status = ConversationsPanel._map_status
    _receipts_are_meaningless = ConversationsPanel._receipts_are_meaningless

    def __init__(self, main_window):
        self.main_window = main_window
        # The panel the list borrows normally has an open conversation; these
        # tests set it only where it matters (it must never leak into another
        # chat's row).
        self.conversation = None


class _MainWindowStub:
    _counts_as_last_message = classmethod(MainWindow._counts_as_last_message.__func__)
    _last_msg_preview = MainWindow._last_msg_preview
    _PREVIEW_MESSAGE_TYPES = MainWindow._PREVIEW_MESSAGE_TYPES

    def __init__(self, show_status=True, self_jid=""):
        self.i18n = _I18n()
        self._self_jid = self_jid
        self.settings = {
            "user_interface": {"show_delivery_status_in_chat_list": show_status}
        }
        self.conversations_panel = _ConversationsPanelStub(self)

    def _is_self_jid(self, jid):
        return bool(self._self_jid) and jid == self._self_jid

    def self_reference_label(self):
        return "You"


def _sent_message(status="READ", ts=1_700_000_000):
    return {
        "key": {"fromMe": True, "id": "abc"},
        "messageType": "conversation",
        "message": {"conversation": "oi"},
        "messageTimestamp": ts,
        "status": status,
    }


def _chat_with(message, jid="jid1"):
    return {"remoteJid": jid, "messages": {"messages": {"records": [message]}}}


class TestChatListDeliveryStatus:
    def test_status_appended_when_enabled(self):
        stub = _MainWindowStub(show_status=True)
        preview = stub._last_msg_preview(_chat_with(_sent_message("READ")))
        assert preview.endswith(" Read")

    def test_status_omitted_when_disabled(self):
        stub = _MainWindowStub(show_status=False)
        preview = stub._last_msg_preview(_chat_with(_sent_message("READ")))
        assert "Read" not in preview

    def test_status_omitted_when_no_conversations_panel(self):
        stub = _MainWindowStub(show_status=True)
        stub.conversations_panel = None
        preview = stub._last_msg_preview(_chat_with(_sent_message("READ")))
        assert "Read" not in preview

    def test_enabled_by_default_when_setting_missing(self):
        stub = _MainWindowStub(show_status=True)
        stub.settings = {}
        preview = stub._last_msg_preview(_chat_with(_sent_message("DELIVERED")))
        assert preview.endswith(" Delivered")

    def test_missing_settings_attribute_defaults_to_enabled(self):
        stub = _MainWindowStub(show_status=True)
        del stub.settings
        preview = stub._last_msg_preview(_chat_with(_sent_message("READ")))
        assert preview.endswith(" Read")

    def test_no_status_for_received_message_unless_played(self):
        message = _sent_message("READ")
        message["key"]["fromMe"] = False
        stub = _MainWindowStub(show_status=True)
        preview = stub._last_msg_preview(_chat_with(message))
        assert "Read" not in preview
        assert "Delivered" not in preview


class TestSelfChatRowInTheList:
    """Issue #95: the "Me" chat's row must not carry a receipt either, and
    the rule has to follow the row being rendered — _last_msg_preview()
    borrows the open conversation's panel for every chat in the list, so
    _map_status() is told which chat it is looking at rather than guessing
    from self.conversation."""

    def test_the_self_chats_own_row_shows_no_receipt(self):
        stub = _MainWindowStub(show_status=True, self_jid="me@s.whatsapp.net")
        preview = stub._last_msg_preview(
            _chat_with(_sent_message("READ"), jid="me@s.whatsapp.net")
        )
        assert not preview.endswith(" Read")

    def test_another_chats_row_still_shows_its_receipt(self):
        """Even when the panel's own open conversation is the "Me" chat: the
        row's JID decides, not whatever happens to be open."""
        stub = _MainWindowStub(show_status=True, self_jid="me@s.whatsapp.net")
        stub.conversations_panel.conversation = {"remoteJid": "me@s.whatsapp.net"}
        preview = stub._last_msg_preview(_chat_with(_sent_message("READ")))
        assert preview.endswith(" Read")

    def test_a_failed_send_still_shows_in_the_self_chats_row(self):
        stub = _MainWindowStub(show_status=True, self_jid="me@s.whatsapp.net")
        preview = stub._last_msg_preview(
            _chat_with(_sent_message("-1"), jid="me@s.whatsapp.net")
        )
        assert preview.endswith(" Failed")
