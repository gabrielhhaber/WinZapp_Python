"""Tests for two sides of the same reported bug: a group promote/demote
arriving as a system event.

* ``main._discount_non_countable_unread()`` used to discount system events
  from a server-reported unread count, believing WhatsApp counted a group
  promote. WhatsApp Web's own code says otherwise (read over CDP on
  2026-11-12, core/unread_types.py): a promote, a join, a deletion is never
  added to unreadCount, so there was nothing of theirs to take out, and
  taking them out ate real unread messages. Only an own message still
  corrects the count: it marks the chat as read.

* ``ConversationsPanel._on_menu_reply()`` (Alt+R) and
  ``_on_menu_reply_private()`` (Alt+Shift+R / "Responder em particular")
  must refuse to enter reply mode for a system event: such messages carry
  no quotable content, WhatsApp rejects a quoted reply to them (HTTP 500),
  and the send path would otherwise announce "não foi possível citar a
  mensagem original" only *after* the quote fell back on send.

ConversationsPanel is a wx.Panel and can't be instantiated without a
running wx.App, so its methods are exercised as plain functions against a
small stub — same approach as tests/test_message_bookmarks.py.
"""

import pytest

from main import _discount_non_countable_unread, is_countable_message
from ui.conversations import ConversationsPanel


def _msg(message_type, from_me=False, **extra):
    m = {
        "key": {"fromMe": from_me, "id": "X1"},
        "messageType": message_type,
        "message": {},
    }
    m.update(extra)
    return m


class TestDiscountNonCountableUnread:
    def test_newest_first_records_discount_the_newest_tail(self):
        """The database loads a chat newest-first. The oldest message (an
        encryption notice) must not be discounted for the unread ones."""
        records = [
            _msg("conversation", messageTimestamp=300),
            _msg("conversation", messageTimestamp=200),
            _msg("e2e_notification", messageTimestamp=100),
        ]
        assert _discount_non_countable_unread(records, 1) == 1
        assert _discount_non_countable_unread(records[::-1], 1) == 1

    def test_mixed_order_and_untimestamped_pending_send(self):
        records = [
            _msg("conversation", messageTimestamp=200),
            _msg("e2e_notification", messageTimestamp=100),
            _msg("conversation", messageTimestamp=300),
        ]
        assert _discount_non_countable_unread(records, 1) == 1
        pending = _msg("conversation", from_me=True)
        assert _discount_non_countable_unread([pending] + records, 1) == 0

    def test_a_system_event_never_takes_an_unread_messages_place(self):
        """WhatsApp never counts a group promote: a count of 1 next to one is
        an older message, not the promote."""
        records = [_msg("groupNotification")]
        assert _discount_non_countable_unread(records, 1) == 1

    def test_own_send_in_the_tail_is_still_discounted(self):
        records = [_msg("conversation", from_me=True)]
        assert _discount_non_countable_unread(records, 1) == 0

    def test_real_incoming_content_is_not_discounted(self):
        records = [_msg("conversation")]
        assert _discount_non_countable_unread(records, 1) == 1

    def test_deletions_and_notices_in_the_tail_leave_the_count_alone(self):
        records = [
            _msg("conversation"),
            _msg("groupNotification"),
            _msg("protocolMessage"),
        ]
        assert _discount_non_countable_unread(records, 3) == 3

    def test_only_the_relevant_tail_is_inspected(self):
        """A server count of 1 must not discount an OLDER system event that
        is outside the tail that justifies the count."""
        records = [
            _msg("groupNotification"),
            _msg("conversation"),
        ]
        assert _discount_non_countable_unread(records, 1) == 1

    def test_a_reply_leaves_only_what_arrived_after_it(self):
        records = [_msg("conversation", messageTimestamp=100),
                   _msg("conversation", from_me=True, messageTimestamp=200),
                   _msg("groupNotification", messageTimestamp=250),
                   _msg("imageMessage", messageTimestamp=300)]
        assert _discount_non_countable_unread(records, 5) == 1

    def test_zero_or_negative_count_is_untouched(self):
        assert _discount_non_countable_unread([_msg("conversation")], 0) == 0
        assert _discount_non_countable_unread([_msg("conversation")], -1) == -1

    def test_no_records_returns_the_count_unchanged(self):
        assert _discount_non_countable_unread([], 5) == 5
        assert _discount_non_countable_unread(None, 5) == 5

    def test_tolerates_junk_records(self):
        records = ["nonsense", 42, None, {"key": {}}]
        assert _discount_non_countable_unread(records, 2) == 2

    def test_no_received_type_is_ever_subtracted(self):
        """Whatever the type, a received record never lowers the count: what
        WhatsApp did not count was never in it."""
        for message_type in (
            "conversation", "groupNotification", "protocolMessage",
            "imageMessage", "e2e_notification", "message_history_notice",
        ):
            assert _discount_non_countable_unread([_msg(message_type)], 1) == 1, message_type


class _FakeI18n:
    _STRINGS = {
        "reply_quote_lost": "não foi possível citar",
        "reply_to": "Responder a {name}",
        "reply_to_group": "Responder a {name} em {group}",
    }

    def t(self, key):
        return self._STRINGS[key]


class _FakeMainWindow:
    def __init__(self):
        self.i18n = _FakeI18n()
        self.announced = []

    def output(self, text):
        self.announced.append(text)

    def self_reference_label(self):
        return "Você"


class _FakeWidget:
    def __init__(self):
        self.set_label = None
        self.shown = False
        self.layout_called = False
        self.focused = False

    def SetLabel(self, text):
        self.set_label = text

    def Show(self):
        self.shown = True

    def Layout(self):
        self.layout_called = True

    def SetFocus(self):
        self.focused = True


class _Stub:
    _is_system_event = staticmethod(ConversationsPanel._is_system_event)
    _sender_label = ConversationsPanel._sender_label

    def __init__(self):
        self.main_window = _FakeMainWindow()
        self._quoted_message = None
        self.conversation = {"remoteJid": "5511999999999@s.whatsapp.net"}
        self.conversation_name = "Grupo Teste"
        self.message_label = _FakeWidget()
        self._remove_quote_btn = _FakeWidget()
        self.conversation_panel = _FakeWidget()
        self.message_field = _FakeWidget()
        self._lid_to_phone = {}
        self._phone_to_lid = {}
        self._presence_pushname_map = {}
        self._group_participants_cache = []


class TestReplyRefusesSystemEvents:
    def test_reply_announces_and_skips_reply_mode(self):
        """Alt+R / menu "Responder" on a system event must announce the
        lost-quote message and NOT enter reply mode (no _quoted_message)."""
        panel = _Stub()
        ConversationsPanel._on_menu_reply(panel, _msg("groupNotification"))
        assert panel.main_window.announced == ["não foi possível citar"]
        assert panel._quoted_message is None

    def test_reply_private_announces_and_does_not_navigate(self):
        """Alt+Shift+R / "Responder em particular" on a system event must
        be refused the same way, before any navigation to a private chat."""
        panel = _Stub()
        ConversationsPanel._on_menu_reply_private(
            panel, _msg("groupNotification"), "5511999999999@lid")
        assert panel.main_window.announced == ["não foi possível citar"]
        assert panel._quoted_message is None

    def test_non_system_message_still_enters_reply_mode(self):
        """Sanity: the guard must not touch real messages.  A normal
        conversation message still stores _quoted_message and shows the
        reply header."""
        panel = _Stub()
        ConversationsPanel._on_menu_reply(panel, _msg("conversation", from_me=True))
        assert panel.main_window.announced == []
        assert panel._quoted_message is not None
        assert panel.message_label.set_label == "Responder a Você"
