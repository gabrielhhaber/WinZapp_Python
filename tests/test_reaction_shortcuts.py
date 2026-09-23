"""Quick reaction ranking and one-emoji selection without opening wx windows."""

from core.reaction_shortcuts import (
    DEFAULT_QUICK_REACTIONS,
    REACTION_HISTORY_LIMIT,
    quick_reactions,
    remember_reaction,
)
from ui.conversations import ConversationsPanel
from ui.dialogs.emoji_picker import EmojiPickerDialog
import ui.conversations as conversations
import ui.dialogs.emoji_picker as emoji_picker

import pytest
import wx


def test_quick_reactions_keep_the_existing_twelve_at_first():
    assert quick_reactions([]) == list(DEFAULT_QUICK_REACTIONS)


def test_repeated_custom_reaction_gradually_enters_the_twelve():
    history = []
    for _ in range(3):
        history = remember_reaction(history, "🐶")
    assert "🐶" not in quick_reactions(history)

    history = remember_reaction(history, "🐶")
    picks = quick_reactions(history)
    assert len(picks) == 12
    assert picks[0] == "🐶"
    assert "🥰" not in picks


def test_current_reaction_is_checked_slot_even_when_not_frequent():
    picks = quick_reactions([], current="🐶")
    assert len(picks) == 12
    assert picks[-1] == "🐶"


def test_history_is_bounded_and_recent_preferences_can_replace_old_ones():
    history = ["🐶"] * 50 + ["🐱"] * REACTION_HISTORY_LIMIT
    history = remember_reaction(history, "🐱")
    assert len(history) == REACTION_HISTORY_LIMIT
    assert "🐶" not in quick_reactions(history)
    assert quick_reactions(history)[0] == "🐱"


def test_invalid_saved_history_is_ignored():
    assert quick_reactions("🐶🐶🐶🐶") == list(DEFAULT_QUICK_REACTIONS)
    assert quick_reactions([None, "", "bad value", "🐶"] * 4)[0] == "🐶"


def test_reaction_picker_never_returns_a_queued_emoji_sequence():
    class _PickerStub:
        _reaction_mode = True
        _queued_emojis = ["❤️", "👍"]

        def _current_emoji(self):
            return "🐶"

    assert EmojiPickerDialog._final_selection(_PickerStub()) == "🐶"


def test_only_successfully_sent_nonempty_reactions_are_remembered():
    class _MainWindow:
        def __init__(self):
            self.settings = {}
            self.saved = 0

        def _schedule_set_chats(self):
            pass

        def _schedule_save_settings(self):
            self.saved += 1

    class _PanelStub:
        _SELF_REACTOR_KEY = ConversationsPanel._SELF_REACTOR_KEY
        _reaction_map = {}
        _sorted_messages = []
        main_window = _MainWindow()

        def _persist_reaction_record(self, *args):
            return None

    panel = _PanelStub()
    ConversationsPanel._on_own_reaction_sent(panel, "chat", {"id": "m1"}, "🐶")
    ConversationsPanel._on_own_reaction_sent(panel, "chat", {"id": "m1"}, "")
    assert panel.main_window.settings["reaction_recent_emojis"] == ["🐶"]
    assert panel.main_window.saved == 1


def test_full_picker_returns_one_emoji_or_none_without_a_real_dialog(monkeypatch):
    class _FakeDialog:
        result = wx.ID_OK

        def __init__(self, parent, i18n, *, reaction_mode):
            assert reaction_mode is True

        def ShowModal(self):
            return self.result

        def get_selected_emoji(self):
            return "🐶"

        def Destroy(self):
            pass

    monkeypatch.setattr(emoji_picker, "EmojiPickerDialog", _FakeDialog)
    assert emoji_picker.choose_reaction_emoji(None, None) == "🐶"
    _FakeDialog.result = wx.ID_CANCEL
    assert emoji_picker.choose_reaction_emoji(None, None) is None


@pytest.mark.parametrize(
    "current, activate_index, picker_choice, expected, picker_calls",
    [
        ("", 0, "🐶", "❤️", 0),
        ("", 12, "🐶", "🐶", 1),
        ("🐶", 11, "🐶", "", 0),
        ("🐶", 12, "🐶", "", 1),
        ("", 12, None, None, 1),
    ],
)
def test_more_reactions_row_opens_picker_and_sends_its_choice(
    monkeypatch, current, activate_index, picker_choice, expected, picker_calls,
):
    class _Control:
        def __init__(self, *args, **kwargs):
            self.handlers = {}

        def Bind(self, event, handler, **kwargs):
            self.handlers[event] = handler

        def SetFocus(self):
            pass

        def SetSizer(self, sizer):
            pass

    class _Dialog(_Control):
        def EndModal(self, result):
            self.result = result

        def ShowModal(self):
            _List.instance.handlers[wx.EVT_LIST_ITEM_ACTIVATED](
                type("Event", (), {"GetIndex": lambda self: activate_index})(),
            )
            return self.result

        def SetSizer(self, sizer):
            pass

        def CentreOnParent(self):
            pass

        def Destroy(self):
            pass

    class _Sizer:
        def __init__(self, *args, **kwargs):
            pass

        def Add(self, *args, **kwargs):
            pass

    class _List(_Control):
        instance = None

        def __init__(self, *args, **kwargs):
            super().__init__()
            self.rows = []
            self.checked = []
            _List.instance = self

        def InsertColumn(self, *args, **kwargs):
            pass

        def Freeze(self):
            pass

        def Thaw(self):
            pass

        def EnableCheckBoxes(self, value):
            self.checkboxes_enabled = value

        def Append(self, row):
            self.rows.append(row[0])

        def CheckItem(self, index, value):
            self.checked.append(index)

        def GetItemCount(self):
            return len(self.rows)

        def Focus(self, index):
            pass

        def Select(self, index):
            pass

    for name, fake in (("Dialog", _Dialog), ("Panel", _Control),
                       ("BoxSizer", _Sizer), ("StaticText", _Control),
                       ("ListCtrl", _List), ("Button", _Control)):
        monkeypatch.setattr(conversations.wx, name, fake)

    sends = []
    class _Thread:
        def __init__(self, *, target, args, daemon):
            sends.append(args)

        def start(self):
            pass

    class _MainWindow:
        settings = {}
        i18n = type("I18n", (), {"t": lambda self, key: "Daha fazla tepki ekle" if key == "react_dialog_more" else key})()

    class _Panel:
        main_window = _MainWindow()
        _reaction_map = {"m1": {ConversationsPanel._SELF_REACTOR_KEY: current}} if current else {}
        _SELF_REACTOR_KEY = ConversationsPanel._SELF_REACTOR_KEY

        def _reject_system_event_action(self, msg):
            return False

        def _do_send_reaction(self, *args):
            pass

    picked = []
    def _pick(*args):
        picked.append(True)
        return picker_choice
    monkeypatch.setattr(conversations, "choose_reaction_emoji", _pick)
    monkeypatch.setattr(conversations.threading, "Thread", _Thread)
    ConversationsPanel._on_menu_react(_Panel(), {"key": {"id": "m1"}})

    assert _List.instance.checkboxes_enabled is True
    assert len(_List.instance.rows) == 13
    assert _List.instance.rows[-1] == "Daha fazla tepki ekle"
    assert _List.instance.checked == ([11] if current else [])
    assert len(picked) == picker_calls
    assert sends == ([] if expected is None else [({"id": "m1"}, expected)])
