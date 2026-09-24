"""Tests for main.is_countable_message() — decides whether a message counts
toward the unread badge, the chat-list sort-to-top bump, and notifications.

Regression coverage for a live bug: an old "Victor" conversation nobody had
messaged in months jumped to the top of the list with a phantom "1 unread"
and an empty conversation when opened, plus a toast reading "Nova mensagem
de 133041125077153: Mensagem incompatível" — caused by an e2e_notification
system message (WhatsApp/Baileys internal bookkeeping, not something anyone
sent) arriving live. is_countable_message() used to keep its own small
blocklist (only groupNotification/protocolMessage), so any OTHER system
type — e2e_notification, notification_template, "unknown", and whatever
WhatsApp invents next — silently counted as "real" by default. It's now
built on top of MainWindow._counts_as_last_message() (an ALLOWLIST already
used for chat-list sort/preview, tests/test_chat_ordering.py) instead of
keeping a second list that can drift out of sync with it.
"""

import pytest

from main import MainWindow, is_countable_message


def _msg(message_type, **extra):
    m = {"messageType": message_type, "message": {}}
    m.update(extra)
    return m


class TestRealContentCounts:
    @pytest.mark.parametrize("message_type", [
        "conversation",
        "extendedTextMessage",
        "imageMessage",
        "videoMessage",
        "audioMessage",
        "documentMessage",
        "stickerMessage",
        "contactMessage",
        "locationMessage",
        "liveLocationMessage",
    ])
    def test_real_content_types_count(self, message_type):
        assert is_countable_message(_msg(message_type)) is True


class TestSystemEventsNeverCount:
    """These ARE shown as a timeline entry when the conversation is opened
    (see ConversationsPanel._is_displayable_message()/MainWindow.
    _counts_as_last_message()) but must never drive unread/sort/notify."""

    def test_group_notification_does_not_count(self):
        assert is_countable_message(_msg("groupNotification")) is False

    def test_revoke_protocol_message_does_not_count(self):
        msg = _msg("protocolMessage", message={"protocolMessage": {"type": 3}})
        assert is_countable_message(msg) is False

    def test_non_revoke_protocol_message_does_not_count(self):
        msg = _msg("protocolMessage", message={"protocolMessage": {"type": 14}})
        assert is_countable_message(msg) is False


class TestUnknownWhatsAppSystemTypesNeverCount:
    """The exact bug: types is_countable_message() had never heard of used
    to default to "countable". They must default to NOT countable instead —
    the whole point of building this on an allowlist rather than a
    blocklist of specific known-bad types."""

    @pytest.mark.parametrize("message_type", [
        "e2e_notification",
        "notification_template",
        "unknown",
        "interactive",  # note: no "Message" suffix — distinct from the real "interactiveMessage"
        "senderKeyDistributionMessage",
        "somethingWhatsAppInventsNextYear",
        "",
    ])
    def test_unrecognized_system_types_do_not_count(self, message_type):
        assert is_countable_message(_msg(message_type)) is False


class TestEdgeCases:
    def test_missing_message_type_does_not_count(self):
        assert is_countable_message({"message": {}}) is False

    def test_non_dict_input_does_not_count(self):
        assert is_countable_message(None) is False
        assert is_countable_message("not a dict") is False

    def test_reaction_message_does_not_count(self):
        """reactionMessage never actually reaches is_countable_message() via
        on_new_message() (which special-cases and returns early for it
        before this check), but a stray reactionMessage record CAN reach
        on_historical_message()'s equivalent gate on history-sync redelivery
        (e.g. after an app restart or F5 resync) — it must not count there
        either, or a bare reaction (no real message) could bump a chat's
        unread badge/sort position/notifications. Reactions have their own
        dedicated preview/ordering channel: chat["_last_reaction"], set by
        _track_last_reaction() and consumed directly by _last_msg_preview()
        — see tests/test_chat_ordering.py's
        test_reaction_message_does_not_count() for the companion coverage
        on MainWindow._counts_as_last_message() itself, which this derives
        from."""
        assert is_countable_message(_msg("reactionMessage")) is False


class TestBuiltOnTheSharedAllowlist:
    def test_derives_from_counts_as_last_message_not_a_separate_list(self):
        """Anchor test: is_countable_message() must reject exactly what
        MainWindow._counts_as_last_message() rejects (plus the two
        preview-only exceptions) — not maintain independent judgment that
        could silently drift from it again."""
        for message_type in MainWindow._PREVIEW_MESSAGE_TYPES:
            msg = _msg(message_type)
            if message_type in ("protocolMessage", "groupNotification",
                                "callLogMessage", "call_log"):
                assert is_countable_message(msg) is False
            else:
                assert is_countable_message(msg) is True, message_type
