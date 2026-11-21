"""Alt+Up / Alt+Down on the lists whose order the user sets."""
from types import SimpleNamespace

import pytest
import wx

from core.keyboard_shortcuts import SECTION
from core.shortcut_catalog import SHORTCUTS
from ui import reorder_keys
from ui.dialogs.shortcuts_tab import SCOPE_LABELS, shortcuts_help_text


class Key:
    def __init__(self, code, alt=False, ctrl=False, shift=False):
        self.code, self.alt, self.ctrl, self.shift = code, alt, ctrl, shift
        self.skipped = False

    def GetKeyCode(self):
        return self.code

    def AltDown(self):
        return self.alt

    def ControlDown(self):
        return self.ctrl

    def ShiftDown(self):
        return self.shift

    def MetaDown(self):
        return False

    def Skip(self):
        self.skipped = True


def test_alt_arrows_are_the_default_keys_of_the_reorderable_lists():
    rows = {s.id: s for s in SHORTCUTS if s.scope == 'reorder'}
    assert (rows['reorder.move_up'].mod, rows['reorder.move_up'].key) == (1, wx.WXK_UP)
    assert (rows['reorder.move_down'].mod, rows['reorder.move_down'].key) == (1, wx.WXK_DOWN)
    assert 'reorder' in SCOPE_LABELS


@pytest.mark.parametrize("key,expected", [(wx.WXK_UP, -1), (wx.WXK_DOWN, 1)])
def test_alt_arrow_gives_the_direction(key, expected):
    assert reorder_keys.reorder_direction(object(), Key(key, alt=True)) == expected


@pytest.mark.parametrize("event", [
    Key(wx.WXK_UP), Key(wx.WXK_DOWN), Key(wx.WXK_UP, ctrl=True), Key(wx.WXK_DOWN, alt=True, shift=True),
    Key(wx.WXK_LEFT, alt=True),
])
def test_plain_arrows_keep_walking_the_list(event):
    assert reorder_keys.reorder_direction(object(), event) == 0


def test_the_keys_follow_what_the_user_set_in_settings():
    window = SimpleNamespace(
        settings={SECTION: {'reorder.move_up': [2, wx.WXK_PAGEUP]}},
        i18n=SimpleNamespace(t=lambda key: key))
    owner = SimpleNamespace(main_window=window)
    assert reorder_keys.reorder_direction(owner, Key(wx.WXK_PAGEUP, ctrl=True)) == -1
    assert reorder_keys.reorder_direction(owner, Key(wx.WXK_UP, alt=True)) == 0


def test_the_help_lists_the_reorder_keys_under_their_own_heading():
    i18n = SimpleNamespace(t=lambda key: key, language='en-US')
    window = SimpleNamespace(settings={SECTION: {}, 'general': {}}, i18n=i18n)
    text = shortcuts_help_text(window, i18n)
    assert 'shortcut_scope_reorder' in text
    assert 'shortcut_reorder_up_label' in text and 'shortcut_reorder_down_label' in text


class FakeControl:
    def __init__(self):
        self.handler = None

    def Bind(self, _event, handler):
        self.handler = handler


def test_bound_control_moves_on_alt_arrows_and_lets_other_keys_through():
    moves, control = [], FakeControl()
    reorder_keys.bind_reorder_keys(control, moves.append)
    up, down, plain = Key(wx.WXK_UP, alt=True), Key(wx.WXK_DOWN, alt=True), Key(wx.WXK_DOWN)
    for event in (up, down, plain):
        control.handler(event)
    assert moves == [-1, 1]
    assert not up.skipped and not down.skipped
    assert plain.skipped


def test_the_key_names_read_as_arrow_names_in_english_and_portuguese():
    from ui.shortcut_names import shortcut_name
    assert shortcut_name((1, wx.WXK_UP), None) == 'Alt+Up Arrow'
    assert shortcut_name((1, wx.WXK_DOWN), None) == 'Alt+Down Arrow'
    pt = {'shortcut_key_alt': 'Alt', 'shortcut_key_up': 'Seta para cima',
          'shortcut_key_down': 'Seta para baixo'}
    i18n = SimpleNamespace(t=lambda key: pt[key])
    assert shortcut_name((1, wx.WXK_UP), i18n) == 'Alt+Seta para cima'
    assert shortcut_name((1, wx.WXK_DOWN), i18n) == 'Alt+Seta para baixo'
