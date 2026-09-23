"""Configurações > Reações, without opening a window.

The tab's handlers are bound onto a plain stub (SettingsDialog cannot be built
without a wx.App) with the list, label and button reduced to recorders. What a
real dialog does with them — the tab being the fourteenth page, the checkbox
and the twelve rows surviving Apply and a reopen — is covered by the wxgui
tests in test_settings_checkboxes_roundtrip.py, test_settings_dialog_roundtrip.py
and test_settings_files_saving_tab.py, in CI only.
"""

import pytest

import ui.dialogs.settings_dialog as settings_dialog
from core.reaction_shortcuts import DEFAULT_QUICK_REACTIONS
from ui.dialogs.settings_dialog import SettingsDialog


class _I18n:
    _STRINGS = {
        "reactions_slot_row": "Posição {position}: {emoji}",
        "reactions_pick_title": "Emoji da posição {position}",
    }

    def t(self, key):
        return self._STRINGS.get(key, key)


class _List:
    def __init__(self):
        self.rows = []
        self.focused = self.selected = None
        self.frozen = 0
        self.shown = True

    def GetItemCount(self):
        return len(self.rows)

    def SetItem(self, index, column, text):
        self.rows[index] = text

    def Append(self, row):
        self.rows.append(row[0])

    def Freeze(self):
        self.frozen += 1

    def Thaw(self):
        self.frozen -= 1

    def Focus(self, index):
        self.focused = index

    def Select(self, index):
        self.selected = index

    def Show(self, show):
        self.shown = show


class _Control:
    def __init__(self, value=False):
        self.value = value
        self.shown = True

    def GetValue(self):
        return self.value

    def Show(self, show):
        self.shown = show


class _Page:
    def __init__(self):
        self.layouts = 0

    def Layout(self):
        self.layouts += 1


class _MainWindow:
    def __init__(self):
        self.i18n = _I18n()
        self.spoken = []

    def output(self, text, interrupt=False):
        self.spoken.append((text, interrupt))


class _Stub:
    _quick_reaction_slot_text = SettingsDialog._quick_reaction_slot_text
    _fill_quick_reaction_slots = SettingsDialog._fill_quick_reaction_slots
    _set_quick_reaction_slots = SettingsDialog._set_quick_reaction_slots
    _update_quick_reaction_fields = SettingsDialog._update_quick_reaction_fields
    _on_quick_reaction_slot_activated = SettingsDialog._on_quick_reaction_slot_activated
    _on_reset_quick_reactions = SettingsDialog._on_reset_quick_reactions

    def __init__(self, fixed=True):
        self.main_window = _MainWindow()
        self._quick_reaction_slots_list = _List()
        self._quick_reaction_slots_label = _Control()
        self._reset_quick_reactions_btn = _Control()
        self._fixed_quick_reactions_cb = _Control(fixed)
        self._reactions_page = _Page()
        self._quick_reaction_slots = list(DEFAULT_QUICK_REACTIONS)
        self._fill_quick_reaction_slots()
        self.dirty = 0

    def _mark_dirty(self, event=None):
        self.dirty += 1


def _activate(index):
    return type("Event", (), {"GetIndex": lambda self: index})()


def test_rows_name_their_position_and_emoji():
    stub = _Stub()
    assert stub._quick_reaction_slots_list.rows[0] == "Posição 1: ❤️"
    assert stub._quick_reaction_slots_list.rows[11] == "Posição 12: 🥰"
    assert len(stub._quick_reaction_slots_list.rows) == 12
    assert stub._quick_reaction_slots_list.frozen == 0


def test_stored_rows_are_written_in_place_without_rebuilding_the_list():
    stub = _Stub()
    custom = ["🐶", *DEFAULT_QUICK_REACTIONS[1:]]
    stub._set_quick_reaction_slots(custom)
    assert stub._quick_reaction_slots == custom
    assert stub._quick_reaction_slots_list.rows[0] == "Posição 1: 🐶"
    assert len(stub._quick_reaction_slots_list.rows) == 12


def test_a_broken_stored_value_shows_the_defaults():
    stub = _Stub()
    stub._set_quick_reaction_slots(["🐶"] * 12)
    assert stub._quick_reaction_slots == list(DEFAULT_QUICK_REACTIONS)


def test_enter_opens_the_picker_for_that_row_and_stores_the_choice(monkeypatch):
    calls = []

    def _choose(parent, i18n, **texts):
        calls.append(texts)
        return "🐶"

    monkeypatch.setattr(settings_dialog, "choose_reaction_emoji", _choose)
    stub = _Stub()
    stub._on_quick_reaction_slot_activated(_activate(2))

    assert calls == [{
        "title": "Emoji da posição 3",
        "hint_text": "reactions_pick_hint",
        "ok_label": "reactions_pick_ok",
    }]
    assert stub._quick_reaction_slots[2] == "🐶"
    assert stub._quick_reaction_slots_list.rows[2] == "Posição 3: 🐶"
    assert stub.dirty == 1
    # Focus comes back to the row that was just changed.
    assert stub._quick_reaction_slots_list.focused == 2
    assert stub._quick_reaction_slots_list.selected == 2


def test_choosing_an_emoji_from_another_row_swaps_the_two_rows(monkeypatch):
    monkeypatch.setattr(settings_dialog, "choose_reaction_emoji", lambda *a, **k: "👎")
    stub = _Stub()
    stub._on_quick_reaction_slot_activated(_activate(0))
    assert stub._quick_reaction_slots_list.rows[0] == "Posição 1: 👎"
    assert stub._quick_reaction_slots_list.rows[2] == "Posição 3: ❤️"


def test_cancelling_the_picker_changes_nothing(monkeypatch):
    monkeypatch.setattr(settings_dialog, "choose_reaction_emoji", lambda *a, **k: None)
    stub = _Stub()
    stub._on_quick_reaction_slot_activated(_activate(4))
    assert stub._quick_reaction_slots == list(DEFAULT_QUICK_REACTIONS)
    assert stub.dirty == 0
    assert stub._quick_reaction_slots_list.focused == 4


@pytest.mark.parametrize("index", [-1, 12])
def test_an_index_outside_the_twelve_rows_opens_nothing(monkeypatch, index):
    monkeypatch.setattr(
        settings_dialog, "choose_reaction_emoji",
        lambda *a, **k: pytest.fail("picker opened"),
    )
    _Stub()._on_quick_reaction_slot_activated(_activate(index))


def test_restoring_the_defaults_rewrites_the_rows_and_says_so():
    stub = _Stub()
    stub._set_quick_reaction_slots(["🐶", *DEFAULT_QUICK_REACTIONS[1:]])
    stub._on_reset_quick_reactions(None)
    assert stub._quick_reaction_slots == list(DEFAULT_QUICK_REACTIONS)
    assert stub._quick_reaction_slots_list.rows[0] == "Posição 1: ❤️"
    assert stub.dirty == 1
    # Focus stays on the button, so the change has to be spoken.
    assert stub.main_window.spoken == [("reactions_reset_done", True)]


@pytest.mark.parametrize("fixed", [True, False])
def test_the_rows_are_only_reachable_while_fixed_reactions_are_on(fixed):
    stub = _Stub(fixed=fixed)
    stub._update_quick_reaction_fields()
    assert stub._quick_reaction_slots_label.shown is fixed
    assert stub._quick_reaction_slots_list.shown is fixed
    assert stub._reset_quick_reactions_btn.shown is fixed
    assert stub._reactions_page.layouts == 1
