"""Esc can return to the search results instead of the full chat list.

Opening a conversation from the chat search cleared the search, so closing it
with Esc landed on the full list: someone going through several results had to
type the search again for each one. Settings > User Interface now has "keep
the chat search when opening a chat" (off by default). On, the search field is
left alone at open, the list stays filtered behind the conversation, and the
usual focus restore finds the chat among the results.
"""

from core.utils import DEFAULT_SETTINGS
from ui.conversations import ConversationsPanel


class _Field:
    def __init__(self, value):
        self.value = value
        self.cleared = 0

    def GetValue(self):
        return self.value

    def Clear(self):
        self.value = ""
        self.cleared += 1


class _List:
    def __init__(self):
        self.focused = self.selected = self.visible = None
        self.has_focus = False

    def Focus(self, index):
        self.focused = index

    def Select(self, index):
        self.selected = index

    def EnsureVisible(self, index):
        self.visible = index

    def SetFocus(self):
        self.has_focus = True


class _Panel:
    _clear_chat_search_on_open = ConversationsPanel._clear_chat_search_on_open
    _restore_conversation_selection = ConversationsPanel._restore_conversation_selection

    def __init__(self, query, **ui_settings):
        self.search_field = _Field(query)
        self.main_window = type("MW", (), {"settings": {"user_interface": ui_settings}})()
        self.conversations_list = _List()
        self.chats_list = []
        self._last_list_focus_jid = ""
        self._last_open_jid = ""


class TestOpeningAConversation:
    def test_by_default_the_search_is_cleared(self):
        panel = _Panel("maria")

        panel._clear_chat_search_on_open()

        assert panel.search_field.value == "" and panel.search_field.cleared == 1

    def test_the_option_keeps_it(self):
        panel = _Panel("maria", keep_search_after_open=True)

        panel._clear_chat_search_on_open()

        assert panel.search_field.value == "maria" and panel.search_field.cleared == 0

    def test_turned_off_explicitly_it_clears(self):
        panel = _Panel("maria", keep_search_after_open=False)

        panel._clear_chat_search_on_open()

        assert panel.search_field.value == ""

    def test_an_empty_search_is_never_touched(self):
        """Clear() fires EVT_TEXT, which rebuilds the whole list."""
        for query in ("", "   "):
            panel = _Panel(query)
            panel._clear_chat_search_on_open()
            assert panel.search_field.cleared == 0

    def test_it_is_off_by_default(self):
        assert DEFAULT_SETTINGS["user_interface"]["keep_search_after_open"] is False


class TestEscLandsOnTheResults:
    def test_the_closed_chat_is_found_in_the_filtered_list(self):
        """With the search kept, the list still holds only the results; Esc
        puts the focus on the chat that was open, at its place among them."""
        panel = _Panel("maria", keep_search_after_open=True)
        panel._clear_chat_search_on_open()
        panel.chats_list = [{"remoteJid": "maria1@s.whatsapp.net"},
                            {"remoteJid": "maria2@s.whatsapp.net"}]
        panel._last_open_jid = panel._last_list_focus_jid = "maria2@s.whatsapp.net"

        panel._restore_conversation_selection()

        lst = panel.conversations_list
        assert (lst.focused, lst.selected, lst.has_focus) == (1, 1, True)
        assert panel.search_field.value == "maria"
