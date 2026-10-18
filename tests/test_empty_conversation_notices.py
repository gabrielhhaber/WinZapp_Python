"""The empty-conversation row carries WhatsApp's top-of-chat notices.

Runs the real ConversationsPanel._render_message_line on a plain stand-in (the
placeholder branch only reads main_window and conversation), so no wx.App. The
sentences themselves are pinned in test_chat_notices.py; this pins the wiring
and that a group or a missing conversation keeps the original text.
"""
from types import SimpleNamespace

from ui.conversations import ConversationsPanel

PN = "5511999990000@s.whatsapp.net"
GROUP = "120363000000000001@g.us"
PLACEHOLDER = {"_type": "empty_placeholder"}


class _I18n:
    TEXT = {
        "no_messages_in_conversation": "No messages available in this chat.",
        "chat_encryption_notice_short": "Messages and calls are end-to-end encrypted.",
        "chat_disappearing_label": "Disappearing messages",
        "chat_disappearing_7d": "7 days",
    }

    def t(self, key):
        return self.TEXT[key]


def _render(jid, chat=None, with_conversation=True):
    main_window = SimpleNamespace(i18n=_I18n())
    if chat is not None:
        main_window.get_chat = lambda j: chat
    panel = SimpleNamespace(main_window=main_window)
    if with_conversation:
        panel.conversation = {"remoteJid": jid}
    return ConversationsPanel._render_message_line(panel, PLACEHOLDER)


def test_a_new_personal_chat_gets_the_encryption_notice():
    assert _render(PN) == (
        "No messages available in this chat. "
        "Messages and calls are end-to-end encrypted."
    )


def test_a_running_timer_is_added_after_it():
    text = _render(PN, chat={"ephemeralDuration": 604800})
    assert text == (
        "No messages available in this chat. "
        "Messages and calls are end-to-end encrypted. "
        "Disappearing messages: 7 days."
    )


def test_a_group_keeps_the_original_text():
    assert _render(GROUP, chat={"ephemeralDuration": 604800}) == (
        "No messages available in this chat."
    )


def test_a_panel_without_a_conversation_keeps_the_original_text():
    assert _render(PN, with_conversation=False) == (
        "No messages available in this chat."
    )
