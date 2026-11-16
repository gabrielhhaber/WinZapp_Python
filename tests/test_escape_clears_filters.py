"""Esc on a chat list clears the search and the filter radio buttons.

Reported: after searching or picking Groups, getting back to the whole list
meant clearing the search field and the radio buttons by hand. Esc on the
normal, archived and locked lists (or in their search field) now clears both
and says "Filters cleared". A chat selection keeps priority: with chats
selected the first Esc deselects them (when that option is on) and the second
clears the filters. Settings > User Interface > Chats can turn it off.
"""

from unittest.mock import Mock

import wx

from core.utils import DEFAULT_SETTINGS
from ui.conversation_panel.chat_list_selection import ChatListSelectionMixin


class _Field:
    def __init__(self, value=""):
        self.value = value

    def GetValue(self):
        return self.value

    def ChangeValue(self, value):
        self.value = value


class _Radio:
    def __init__(self, selection=0):
        self.selection = selection

    def SetSelection(self, index):
        self.selection = index


class _Event:
    def __init__(self, key=wx.WXK_ESCAPE, ctrl=False, shift=False, alt=False):
        self.key, self.ctrl, self.shift, self.alt = key, ctrl, shift, alt
        self.skipped = False

    def GetKeyCode(self):
        return self.key

    def ControlDown(self):
        return self.ctrl

    def ShiftDown(self):
        return self.shift

    def AltDown(self):
        return self.alt

    def Skip(self):
        self.skipped = True


class _MainWindow:
    def __init__(self, ui):
        self.settings = {"user_interface": ui}
        self.output = Mock()
        self.add_chats_to_ui = Mock()
        self.i18n = type("I18n", (), {"t": staticmethod(lambda key: key)})()


class _Panel:
    _escape_clears_filters_enabled = ChatListSelectionMixin._escape_clears_filters_enabled
    _escape_clears_selection_enabled = ChatListSelectionMixin._escape_clears_selection_enabled
    _chat_list_filters_active = ChatListSelectionMixin._chat_list_filters_active
    _clear_chat_list_filters = ChatListSelectionMixin._clear_chat_list_filters
    _rebuild_chat_list_after_filters_cleared = (
        ChatListSelectionMixin._rebuild_chat_list_after_filters_cleared)

    def __init__(self, query="", conv_filter="all", ui=None, selected=(), radio=True):
        self.main_window = _MainWindow(ui or {})
        self.search_field = _Field(query)
        self.selected_chats = set(selected)
        if radio:
            self._filter_radio = _Radio(["all", "unread", "groups", "individual"].index(conv_filter))
            self._conv_filter = conv_filter


class TestSetting:
    def test_default_is_on(self):
        assert DEFAULT_SETTINGS["user_interface"]["escape_clears_filters"] is True
        assert _Panel()._escape_clears_filters_enabled() is True

    def test_can_be_turned_off(self):
        panel = _Panel(query="ana", ui={"escape_clears_filters": False})
        assert panel._clear_chat_list_filters() is False
        assert panel.search_field.value == "ana"
        panel.main_window.add_chats_to_ui.assert_not_called()


class TestClearing:
    def test_search_text_is_cleared_and_announced(self):
        panel = _Panel(query="ana")
        assert panel._clear_chat_list_filters() is True
        assert panel.search_field.value == ""
        panel.main_window.add_chats_to_ui.assert_called_once()
        panel.main_window.output.assert_called_once_with("filters_cleared", interrupt=True)

    def test_filter_radio_goes_back_to_all(self):
        panel = _Panel(conv_filter="groups")
        assert panel._clear_chat_list_filters() is True
        assert panel._conv_filter == "all"
        assert panel._filter_radio.selection == 0

    def test_search_and_filter_clear_together_with_one_rebuild(self):
        panel = _Panel(query="ana", conv_filter="unread")
        panel._clear_chat_list_filters()
        assert panel.search_field.value == "" and panel._conv_filter == "all"
        panel.main_window.add_chats_to_ui.assert_called_once()

    def test_nothing_to_clear_leaves_esc_alone(self):
        panel = _Panel()
        assert panel._clear_chat_list_filters() is False
        panel.main_window.output.assert_not_called()
        panel.main_window.add_chats_to_ui.assert_not_called()

    def test_list_without_radio_buttons_clears_only_the_search(self):
        """The locked list has a search field and no filter radio buttons."""
        panel = _Panel(query="ana", radio=False)
        assert panel._clear_chat_list_filters() is True
        assert panel.search_field.value == ""
        assert _Panel(radio=False)._clear_chat_list_filters() is False


class TestSelectionComesFirst:
    def test_selection_blocks_the_clearing_while_its_option_is_on(self):
        panel = _Panel(query="ana", selected={"a@s.whatsapp.net"})
        assert panel._clear_chat_list_filters() is False
        assert panel.search_field.value == "ana"

    def test_selection_does_not_block_when_its_option_is_off(self):
        panel = _Panel(query="ana", selected={"a@s.whatsapp.net"},
                       ui={"escape_clears_selection": False})
        assert panel._clear_chat_list_filters() is True


class TestKeyHandler:
    """_handle_chat_selection_key: first Esc deselects, the second clears."""

    class _Keys(_Panel):
        _handle_chat_selection_key = ChatListSelectionMixin._handle_chat_selection_key
        _selection_mode_announcement = ChatListSelectionMixin._selection_mode_announcement

        def __init__(self, **kw):
            super().__init__(**kw)
            self.chats_list = []
            self.conversations_list = Mock(GetFocusedItem=Mock(return_value=-1))
            self._chat_selection_visible = Mock(return_value=True)
            self._selection_mode_enabled = Mock(return_value=True)
            self._repaint_chat_selection = Mock()

    def test_escape_with_filters_and_no_selection_clears_them(self):
        panel = self._Keys(query="ana")
        assert panel._handle_chat_selection_key(_Event()) is True
        assert panel.search_field.value == ""

    def test_first_escape_deselects_then_second_clears_filters(self):
        panel = self._Keys(query="ana", selected={"a@s.whatsapp.net"})
        assert panel._handle_chat_selection_key(_Event()) is True
        assert panel.selected_chats == set() and panel.search_field.value == "ana"
        assert panel._handle_chat_selection_key(_Event()) is True
        assert panel.search_field.value == ""

    def test_escape_with_nothing_to_clear_falls_through(self):
        assert self._Keys()._handle_chat_selection_key(_Event()) is False

    def test_modified_escape_is_not_ours(self):
        panel = self._Keys(query="ana")
        assert panel._handle_chat_selection_key(_Event(ctrl=True)) is False
        assert panel.search_field.value == "ana"
