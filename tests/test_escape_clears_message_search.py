"""Esc inside an open chat clears the message search before closing the chat.

With the search bar (Ctrl+Shift+F) up, the first Esc clears the search, closes
the bar and says "Message search cleared"; the chat stays open. Without a
search bar, or with the option off (Settings > User Interface > Chats, on by
default), Esc closes the chat as before.
"""

from unittest.mock import Mock

import wx

from core.utils import DEFAULT_SETTINGS
from ui.conversation_panel.conversation_navigation import ConversationNavigationMixin
from ui.conversation_panel.message_search import MessageSearchMixin


class _Win:
    def __init__(self, parent=None):
        self.parent = parent
        self.focused = 0

    def GetParent(self):
        return self.parent

    def SetFocus(self):
        self.focused += 1


class _Bar(_Win):
    def __init__(self, shown):
        super().__init__()
        self.shown = shown

    def IsShown(self):
        return self.shown


class _Panel:
    _escape_clears_message_search_enabled = (
        MessageSearchMixin._escape_clears_message_search_enabled)
    _clear_message_search_on_escape = MessageSearchMixin._clear_message_search_on_escape
    _on_escape_conversation = ConversationNavigationMixin._on_escape_conversation

    def __init__(self, monkeypatch, bar_shown=True, ui=None, focus=None, selected=()):
        self.main_window = Mock()
        self.main_window.settings = {"user_interface": ui or {}}
        self.main_window.i18n.t = lambda key: key
        self._search_panel = _Bar(bar_shown)
        self.selected_messages = set(selected)
        self._on_close_search = Mock()
        self._escape_clears_selection_enabled = lambda: True
        self.close_conversation = Mock()
        self._refresh_message_rows_by_ids = Mock()
        self._selection_mode_announcement = lambda base, a, b: base
        monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: focus),
                            raising=False)


def test_default_is_on():
    assert DEFAULT_SETTINGS["user_interface"]["escape_clears_message_search"] is True


def test_open_search_is_cleared_and_the_chat_stays_open(monkeypatch):
    panel = _Panel(monkeypatch)
    panel._on_escape_conversation(None)
    panel._on_close_search.assert_called_once()
    panel.main_window.output.assert_called_once_with("message_search_cleared", interrupt=True)
    panel.close_conversation.assert_not_called()


def test_no_search_bar_closes_the_chat(monkeypatch):
    panel = _Panel(monkeypatch, bar_shown=False)
    panel._on_escape_conversation(None)
    panel._on_close_search.assert_not_called()
    panel.close_conversation.assert_called_once()


def test_option_off_closes_the_chat(monkeypatch):
    panel = _Panel(monkeypatch, ui={"escape_clears_message_search": False})
    panel._on_escape_conversation(None)
    panel._on_close_search.assert_not_called()
    panel.close_conversation.assert_called_once()


def test_selected_messages_are_cleared_before_the_search(monkeypatch):
    panel = _Panel(monkeypatch, selected={"m1"})
    panel._on_escape_conversation(None)
    assert panel.selected_messages == set()
    panel._on_close_search.assert_not_called()
    panel.close_conversation.assert_not_called()


def test_focus_outside_the_bar_stays_where_it_was(monkeypatch):
    composer = _Win()
    panel = _Panel(monkeypatch, focus=composer)
    panel._clear_message_search_on_escape()
    assert composer.focused == 1


def test_focus_inside_the_bar_is_left_to_the_close(monkeypatch):
    panel = _Panel(monkeypatch)
    field = _Win(panel._search_panel)
    monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: field), raising=False)
    panel._clear_message_search_on_escape()
    assert field.focused == 0
    panel._on_close_search.assert_called_once()


def test_the_mention_popup_keeps_its_esc(monkeypatch):
    """The popup is dismissed by its own hook; the search must not also go."""
    panel = _Panel(monkeypatch)
    panel._mention_panel = _Bar(True)
    panel._on_escape_conversation(None)
    panel._on_close_search.assert_not_called()
