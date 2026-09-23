"""Quick reaction ranking and one-emoji selection without opening wx windows."""

from core.reaction_shortcuts import (
    DEFAULT_QUICK_REACTIONS,
    REACTION_HISTORY_LIMIT,
    assign_quick_reaction,
    fixed_quick_reactions,
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


def test_by_default_the_most_used_reaction_comes_first():
    assert quick_reactions(["🥰"])[0] == "🥰"
    assert quick_reactions(["😂"] * 20)[0] == "😂"


# Settings > Reactions > fixed quick reactions: blind users pick a quick
# reaction by counting arrow presses, so with the option on the rows are
# exactly the configured ones and usage never moves or replaces any of them.

_CUSTOM = ["🐶", "🐱", *DEFAULT_QUICK_REACTIONS[2:]]


def test_fixed_slots_are_the_rows_whatever_the_history():
    assert quick_reactions(["😂"] * 50 + ["🐸"] * 50, fixed_slots=_CUSTOM) == _CUSTOM


def test_fixed_slots_offer_the_current_reaction_on_a_row_of_its_own():
    # Replacing row 12 would put 🐸 where the user expects 🥰, and Enter
    # there removes the reaction instead of switching to 🥰.
    picks = quick_reactions([], current="🐸", fixed_slots=_CUSTOM)
    assert picks == _CUSTOM + ["🐸"]
    assert quick_reactions([], current="🐱", fixed_slots=_CUSTOM) == _CUSTOM


@pytest.mark.parametrize("stored", [
    None, "🐶", [], _CUSTOM[:11], _CUSTOM + ["🐸"],
    ["🐶"] * 12, _CUSTOM[:11] + [""], _CUSTOM[:11] + [None],
])
def test_unusable_stored_slots_fall_back_to_the_defaults_as_a_whole(stored):
    assert fixed_quick_reactions(stored) == list(DEFAULT_QUICK_REACTIONS)


def test_valid_stored_slots_are_kept_and_copied():
    rows = fixed_quick_reactions(_CUSTOM)
    assert rows == _CUSTOM
    rows[0] = "🐸"
    assert _CUSTOM[0] == "🐶"


def test_assigning_a_new_emoji_replaces_only_that_row():
    rows = assign_quick_reaction(list(DEFAULT_QUICK_REACTIONS), 2, "🐶")
    assert rows == [*DEFAULT_QUICK_REACTIONS[:2], "🐶", *DEFAULT_QUICK_REACTIONS[3:]]


def test_assigning_an_emoji_already_in_another_row_swaps_the_two():
    rows = assign_quick_reaction(list(DEFAULT_QUICK_REACTIONS), 0, "👎")
    assert rows[0] == "👎"
    assert rows[2] == "❤️"
    assert len(set(rows)) == 12


@pytest.mark.parametrize("position, emoji", [(-1, "🐶"), (12, "🐶"), (0, ""), (0, None)])
def test_assigning_out_of_range_or_nothing_changes_nothing(position, emoji):
    assert assign_quick_reaction(_CUSTOM, position, emoji) == _CUSTOM


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

        texts = None

        def __init__(self, parent, i18n, *, reaction_mode, **texts):
            assert reaction_mode is True
            _FakeDialog.texts = texts

        def ShowModal(self):
            return self.result

        def get_selected_emoji(self):
            return "🐶"

        def Destroy(self):
            pass

    monkeypatch.setattr(emoji_picker, "EmojiPickerDialog", _FakeDialog)
    assert emoji_picker.choose_reaction_emoji(None, None) == "🐶"
    assert _FakeDialog.texts == {}
    _FakeDialog.result = wx.ID_CANCEL
    assert emoji_picker.choose_reaction_emoji(None, None) is None

    _FakeDialog.result = wx.ID_OK
    emoji_picker.choose_reaction_emoji(
        None, None, title="T", hint_text="H", ok_label="&O",
    )
    assert _FakeDialog.texts == {"title": "T", "hint_text": "H", "ok_label": "&O"}


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
@pytest.mark.parametrize("fixed_setting", [None, False, True])
def test_more_reactions_row_opens_picker_and_sends_its_choice(
    monkeypatch, current, activate_index, picker_choice, expected, picker_calls,
    fixed_setting,
):
    # Fixed rows are never overwritten: a current reaction outside them gets
    # its own row after the twelve, which pushes "Add more reactions" down.
    extra = 1 if fixed_setting and current else 0
    if activate_index >= 11:
        activate_index += extra
    more_index = 12 + extra

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
            self.unchecked = []
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
            (self.checked if value else self.unchecked).append(index)

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
        # None: an install from before the option existed has no such section.
        # The configured rows equal the defaults here so every case above
        # (activating row 0 sends ❤️) holds in both modes.
        settings = ({} if fixed_setting is None else
                    {"reactions": {"fixed_quick_reactions": fixed_setting,
                                   "quick_reaction_slots": list(DEFAULT_QUICK_REACTIONS)}})
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
    ranking_calls = []
    real_quick_reactions = conversations.quick_reactions
    def _quick_reactions(*args, **kwargs):
        ranking_calls.append(kwargs)
        return real_quick_reactions(*args, **kwargs)
    monkeypatch.setattr(conversations, "quick_reactions", _quick_reactions)
    ConversationsPanel._on_menu_react(_Panel(), {"key": {"id": "m1"}})

    assert ranking_calls == [
        {"fixed_slots": list(DEFAULT_QUICK_REACTIONS) if fixed_setting else None}
    ]

    assert _List.instance.checkboxes_enabled is True
    assert len(_List.instance.rows) == more_index + 1
    assert _List.instance.rows[-1] == "Daha fazla tepki ekle"
    assert _List.instance.checked == ([11 + extra] if current else [])
    if extra:
        assert _List.instance.rows[:12] == list(DEFAULT_QUICK_REACTIONS)
    assert len(picked) == picker_calls
    assert sends == ([] if expected is None else [({"id": "m1"}, expected)])

    # Space on "Add more reactions" must not leave it announced as checked;
    # checking a real reaction row is left alone.
    on_checked = _List.instance.handlers[wx.EVT_LIST_ITEM_CHECKED]
    on_checked(type("Event", (), {"GetIndex": lambda self: more_index})())
    on_checked(type("Event", (), {"GetIndex": lambda self: 3})())
    assert _List.instance.unchecked == [more_index]


class _KeyEvent:
    def __init__(self, keycode, ctrl):
        self._keycode, self._ctrl = keycode, ctrl
        self.skipped = False

    def GetKeyCode(self):
        return self._keycode

    def ControlDown(self):
        return self._ctrl

    def Skip(self, *args):
        self.skipped = True


def _picker_stub(reaction_mode):
    calls = []

    class _Stub:
        _reaction_mode = reaction_mode
        _list = type("List", (), {"HasFocus": lambda self: True})()

        def _on_ok(self, event):
            calls.append("ok")

        def _queue_current_emoji(self):
            calls.append("queue")

    return _Stub(), calls


@pytest.mark.parametrize("keycode", [wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER])
@pytest.mark.parametrize("reaction_mode, expected", [(True, ["ok"]), (False, ["queue"])])
def test_ctrl_enter_sends_one_reaction_but_queues_in_the_composer(
    keycode, reaction_mode, expected,
):
    stub, calls = _picker_stub(reaction_mode)
    EmojiPickerDialog._on_char_hook(stub, _KeyEvent(keycode, ctrl=True))
    assert calls == expected
