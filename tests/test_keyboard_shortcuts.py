"""Customized keys must replace every old route without changing command guards.

All wx boundaries are recording stubs: these tests never create an App, window,
hotkey registration or desktop hook.
"""
from types import SimpleNamespace
import pytest
import wx
from core.keyboard_shortcuts import (
    SECTION, Shortcut, binding_for, default_binding, conflicts, logical_key,
    sanitize_overrides, scopes_overlap, action_text,
)
from core.shortcut_catalog import SHORTCUTS
from ui import shortcut_bindings as bindings
from ui.shortcut_bindings import popup_label_chord, refresh_popup_shortcuts
from ui.dialogs.shortcut_capture import capture_error, classify_chord


def translate(key):
    return {'messages': '&Messages', 'main_nav': '&Main navigation',
            'type_message': 'Ty&pe message'}.get(key, key)


def find(shortcut_id):
    return next(s for s in SHORTCUTS if s.id == shortcut_id)


class TestPreferences:
    def test_default_localized_mnemonic_and_override(self):
        spec = find('main.ID_ALT_NAV')
        assert default_binding(spec, translate) == (1, ord('M'))
        assert default_binding(spec, lambda key: '&Navegação') == (1, ord('N'))
        assert binding_for(spec, {spec.id: [2, ord('1')]}, translate) == (2, ord('1'))

    def test_escaped_ampersand_is_not_a_mnemonic(self):
        spec = Shortcut('x', 'main', 'x', 'x', 1, '@label')
        assert default_binding(spec, lambda key: 'A && B &C') == (1, ord('C'))

    def test_disabled_is_distinct_from_default(self):
        spec = find('main.ID_ALT_1')
        assert binding_for(spec, {}, translate) == (1, ord('1'))
        assert binding_for(spec, {spec.id: None}, translate) is None

    def test_sanitizing_preserves_disabled_and_drops_unknown_or_malformed(self):
        spec = find('main.ID_ALT_1')
        assert sanitize_overrides({spec.id: None, 'unknown': [2, 65]}, SHORTCUTS) == {spec.id: None}
        for value in ([True, 65], [2, True], [8, 65], [-1, 65], [2, 0], 'Ctrl+A', {}):
            assert sanitize_overrides({spec.id: value}, SHORTCUTS) == {}
        assert sanitize_overrides({spec.id: [2, ord('a')]}, SHORTCUTS) == {spec.id: [2, ord('A')]}

    @pytest.mark.parametrize('value', [None, [], 'bad', 42])
    def test_corrupted_section_uses_default(self, value):
        spec = find('main.ID_ALT_1')
        assert binding_for(spec, value, translate) == (1, ord('1'))

    def test_action_description_does_not_claim_the_old_key(self):
        spec = find('main.ID_ALT_1')
        assert action_text(spec, lambda key: 'Alt+1: go to chats') == 'go to chats'


class TestConflicts:
    def test_child_and_parent_shipped_overlap_remains_legal(self):
        spec = find('main.ID_ALT_NAV')
        assert conflicts(spec, (1, ord('M')), {}, SHORTCUTS, translate) == []

    def test_new_main_binding_conflicts_with_a_child_command(self):
        spec = find('main.ID_ALT_1')
        found = conflicts(spec, (2, ord('R')), {}, SHORTCUTS, translate)
        assert 'messages.ID_CTRL_R' in {s.id for s in found}

    def test_same_key_in_separate_windows_is_allowed(self):
        spec = find('call.ID_CALL_MUTE')
        assert conflicts(spec, (2, ord('R')), {}, SHORTCUTS, translate) == []

    def test_disabled_command_frees_its_key(self):
        a = Shortcut('a', 'main', 'a', 'a', 1, 65)
        b = Shortcut('b', 'main', 'b', 'b', 1, 66)
        assert conflicts(a, (1, 66), {'b': None}, [a, b], translate) == []
        assert conflicts(a, (1, 66), {}, [a, b], translate) == [b]

    def test_global_key_conflicts_with_every_scope(self):
        spec = Shortcut('global', 'global', 'restore', 'x', 0, 0)
        assert conflicts(spec, (2, ord('M')), {}, SHORTCUTS, translate)
        assert scopes_overlap('global', 'incoming')

    def test_shared_navigation_id_is_not_a_conflict_with_itself(self):
        spec = find('main.ID_ALT_2')
        assert conflicts(spec, (2, ord('J')), {spec.id: [2, ord('J')]}, SHORTCUTS, translate) == []


class TestKeyHandlers:
    def test_new_key_routes_to_original_command_and_old_key_is_disabled(self):
        spec = find('message_list.seek_back')
        prefs = {spec.id: [2, ord('J')]}
        assert logical_key('message_list', (2, ord('J')), prefs, SHORTCUTS, translate) == (4, wx.WXK_LEFT)
        assert logical_key('message_list', (4, wx.WXK_LEFT), prefs, SHORTCUTS, translate) == (0, -1)

    def test_native_unrelated_gestures_are_untouched(self):
        assert logical_key('message_list', (0, wx.WXK_TAB), {'message_list.play': None}, SHORTCUTS, translate) == (0, wx.WXK_TAB)

    def test_alias_slots_can_be_changed_independently(self):
        prefs = {'messages.ID_ALT_U': [2, ord('J')]}
        assert binding_for(find('messages.ID_ALT_U.1'), prefs, translate) == (2, ord('L'))


class Owner:
    def __init__(self):
        self.settings = {}
        self.i18n = SimpleNamespace(t=translate)
        self.ID_ALT_1 = 101
        self.tables = []

    def SetAcceleratorTable(self, table):
        self.tables.append(table)


class TestLiveTables:
    def test_refresh_changes_existing_table_without_binding_new_handlers(self, monkeypatch):
        monkeypatch.setattr(bindings.wx, 'AcceleratorTable', lambda rows: tuple(rows))
        owner = Owner()
        original = bindings.make_shortcut_table(owner, 'main', [(1, ord('1'), 101)])
        assert original == ((1, ord('1'), 101),)
        owner.settings[SECTION] = {'main.ID_ALT_1': [2, ord('1')]}
        bindings.refresh_shortcuts(owner)
        assert owner.tables[-1] == ((2, ord('1'), 101),)
        owner.settings[SECTION]['main.ID_ALT_1'] = None
        bindings.refresh_shortcuts(owner)
        assert owner.tables[-1] == ()

    def test_child_reads_frame_preferences(self, monkeypatch):
        monkeypatch.setattr(bindings.wx, 'AcceleratorTable', lambda rows: tuple(rows))
        window = Owner()
        window.settings[SECTION] = {'main.ID_ALT_2': [2, ord('J')]}
        child = Owner()
        del child.settings
        child.main_window = window
        child.ID_ALT_2_LIST = 123
        table = bindings.make_shortcut_table(child, 'chats', [(1, ord('2'), 123)])
        assert table == ((2, ord('J'), 123),)

    def test_uncatalogued_entry_is_caught(self, monkeypatch):
        owner = Owner()
        with pytest.raises(ValueError, match='Uncatalogued'):
            bindings.make_shortcut_table(owner, 'main', [(2, 65, 999)])


class TestCapture:
    def test_letters_need_modifier_but_special_keys_do_not(self):
        assert capture_error(0, ord('A')) == 'shortcut_capture_modifier_required'
        assert capture_error(2, ord('A')) == ''
        assert capture_error(0, wx.WXK_F8) == ''
        assert capture_error(0, wx.WXK_DELETE) == ''
        assert capture_error(0, wx.WXK_RETURN) == ''

    def test_global_key_needs_control_or_alt(self):
        assert capture_error(0, wx.WXK_F8, True)
        assert capture_error(2, wx.WXK_F8, True) == ''

    def test_system_reserved_keys_are_refused(self):
        assert capture_error(1, wx.WXK_F4) == 'shortcut_capture_reserved'
        assert capture_error(1, wx.WXK_TAB) == 'shortcut_capture_reserved'


class TestClassifyChord:
    def test_tab_and_bare_escape_reach_the_dialog(self):
        assert classify_chord(0, wx.WXK_TAB) == 'pass'
        assert classify_chord(4, wx.WXK_TAB) == 'pass'
        assert classify_chord(0, wx.WXK_ESCAPE) == 'pass'

    def test_modified_escape_is_still_captured_or_reserved(self):
        assert classify_chord(4, wx.WXK_ESCAPE) != 'pass'
        assert classify_chord(2, wx.WXK_ESCAPE) == 'shortcut_capture_reserved'

    def test_windows_key_chords_are_reserved(self):
        assert classify_chord(2 | 8, ord('A')) == 'shortcut_capture_reserved'

    def test_plain_valid_chord_is_accepted(self):
        assert classify_chord(2, ord('A')) == ''


class _Item:
    def __init__(self, label, submenu=None):
        self.label = label
        self.submenu = submenu

    def GetSubMenu(self):
        return self.submenu

    def GetItemLabel(self):
        return self.label

    def SetItemLabel(self, label):
        self.label = label


class _Menu:
    def __init__(self, *items):
        self.items = list(items)

    def GetMenuItems(self):
        return self.items


class TestPopupLabels:
    @pytest.mark.parametrize('label, chord', [
        ('Mute\tAlt+Shift+S', (1 | 4, ord('S'))),
        ('Delete\tDelete', (0, wx.WXK_DELETE)),
        ('Pin\tCtrl+P', (2, ord('P'))),
        ('Menu\tShift+F10', (4, wx.WXK_F10)),
    ])
    def test_label_chord_is_read_without_a_window(self, label, chord):
        assert popup_label_chord(label) == chord

    def test_unreadable_label_is_skipped_not_fatal(self):
        assert popup_label_chord('Item\tNot a key+') is None

    def test_refresh_relabels_nested_items_and_never_raises(self):
        window = SimpleNamespace(
            settings={'keyboard_shortcuts': {'chats.ID_DELETE_CONV': [2, ord('K')]}},
            i18n=SimpleNamespace(t=translate))
        owner = SimpleNamespace(main_window=window)
        delete = _Item('Delete chat\tDelete')
        plain = _Item('No accelerator')
        broken = _Item('Odd\tNot a key+')
        refresh_popup_shortcuts(owner, 'chats',
                                _Menu(plain, broken, _Item('More', _Menu(delete))))
        assert delete.label.startswith('Delete chat\t')
        assert delete.label != 'Delete chat\tDelete'
        assert plain.label == 'No accelerator'
        assert broken.label == 'Odd\tNot a key+'
