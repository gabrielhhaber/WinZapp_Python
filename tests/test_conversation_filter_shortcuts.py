"""Alt+Shift+F focuses the chat filter radio buttons; Alt+Shift+A/U/G/I pick
a filter from anywhere in the chat list panel without moving keyboard focus.

ConversationsPanel is a wx.Panel, so the unbound methods run on a plain stub.
"""

from core.shortcut_catalog import SHORTCUTS
from ui.conversation_panel.conversation_navigation import (
    CONVERSATION_FILTERS,
    ConversationNavigationMixin,
)


class _Radio:
    def __init__(self):
        self.labels = ["Tudo", "Não lidas", "Grupos", "Individuais"]
        self.selection = 0
        self.focused = False

    def SetSelection(self, index):
        self.selection = index

    def GetSelection(self):
        return self.selection

    def GetString(self, index):
        return self.labels[index]

    def SetFocus(self):
        self.focused = True


class _Speech:
    def __init__(self):
        self.spoken = []

    def output(self, text, *args, **kwargs):
        self.spoken.append(text)


class _Window:
    def __init__(self):
        self.speak_output = _Speech()
        self.rebuilds = 0

    def add_chats_to_ui(self):
        self.rebuilds += 1


class _Panel:
    chats_list = []
    _on_filter_changed = ConversationNavigationMixin._on_filter_changed

    def __init__(self):
        self._filter_radio = _Radio()
        self._conv_filter = "all"
        self.conversations_list = object()
        self.main_window = _Window()


def test_each_shortcut_selects_its_filter_and_announces_it():
    for index, key in enumerate(CONVERSATION_FILTERS):
        panel = _Panel()
        ConversationNavigationMixin._on_accel_conversation_filter(panel, key)
        assert panel._filter_radio.selection == index
        assert panel._conv_filter == key
        assert panel.main_window.rebuilds == 1
        assert panel.main_window.speak_output.spoken == [panel._filter_radio.labels[index]]
        assert not panel._filter_radio.focused


def test_alt_shift_f_moves_focus_to_the_radio_buttons():
    panel = _Panel()
    ConversationNavigationMixin._on_accel_focus_conversation_filter(panel, None)
    assert panel._filter_radio.focused


def test_catalog_binds_alt_shift_letters_for_the_filters():
    expected = {
        "chats.ID_FILTER_FOCUS": "F",
        "chats.ID_FILTER_ALL": "A",
        "chats.ID_FILTER_UNREAD": "U",
        "chats.ID_FILTER_GROUPS": "G",
        "chats.ID_FILTER_INDIVIDUAL": "I",
    }
    rows = {s.id: s for s in SHORTCUTS}
    for shortcut_id, letter in expected.items():
        assert (rows[shortcut_id].mod, rows[shortcut_id].key) == (5, ord(letter))
