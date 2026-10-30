"""Chat actions say what they did: a screen reader announces nothing else."""

from unittest.mock import MagicMock

import pytest

from ui.conversation_panel.chat_menu import ChatMenuMixin


def _panel():
    panel = MagicMock()
    panel.conversation = None
    panel.main_window.i18n.t.side_effect = lambda key: key
    panel._confirm_chat_action = lambda key: ChatMenuMixin._confirm_chat_action(panel, key)
    return panel


@pytest.mark.parametrize("handler,action,key", [
    ("_on_menu_mark_unread", "mark_conversation_as_unread", "chat_action_marked_unread"),
    ("_on_menu_unmute", "unmute_chat", "chat_action_unmuted"),
    ("_on_menu_archive", "archive_chat", "chat_action_archived"),
    ("_on_menu_unarchive", "unarchive_chat", "chat_action_unarchived"),
    ("_on_menu_pin", "pin_chat", "chat_action_pinned"),
    ("_on_menu_unpin", "unpin_chat", "chat_action_unpinned"),
])
def test_action_is_confirmed_through_output(handler, action, key):
    panel = _panel()
    getattr(ChatMenuMixin, handler)(panel, "1@s.whatsapp.net")
    getattr(panel.main_window, action).assert_called_once_with("1@s.whatsapp.net")
    panel.main_window.output.assert_called_once_with(key, interrupt=True)


def test_mute_is_confirmed():
    panel = _panel()
    ChatMenuMixin._on_menu_mute(panel, "1@s.whatsapp.net", 3600)
    panel.main_window.output.assert_called_once_with("chat_action_muted", interrupt=True)


def test_mark_read_is_confirmed():
    panel = _panel()
    ChatMenuMixin._on_menu_mark_read(panel, "1@s.whatsapp.net")
    panel.main_window.output.assert_called_once_with("chat_action_marked_read", interrupt=True)
