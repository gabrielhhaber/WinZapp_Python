"""Communities is a row of the navigation list, after Calls, naming Alt+8."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.locales import load_strings, registered_locale_codes
from ui.navigation import NavigationPanel


def test_every_locale_shows_alt8_on_the_communities_nav_item():
    for locale in registered_locale_codes():
        assert load_strings(locale)["communities_nav"].endswith(" alt+8"), locale


class _List:
    def __init__(self):
        self.rows, self.focused = [], -1

    def GetFocusedItem(self):
        return self.focused

    def Freeze(self): pass
    def Thaw(self): pass
    def DeleteAllItems(self): self.rows = []
    def Append(self, row): self.rows.append(row[0])
    def SetItemText(self, index, text): self.rows[index] = text
    def GetItemText(self, index): return self.rows[index]
    def Focus(self, index): self.focused = index
    def Select(self, index): pass


class _Nav:
    rebuild_items = NavigationPanel.rebuild_items
    refresh_archived_label = NavigationPanel.refresh_archived_label
    on_nav_item_selected = NavigationPanel.on_nav_item_selected

    def __init__(self, lock_visible=False):
        strings = load_strings("en-US")
        i18n = SimpleNamespace(t=lambda key: strings[key])
        self.main_window = SimpleNamespace(
            i18n=i18n, settings={}, get_archived_unread_count=lambda: 0,
            chat_lock_navigation_visible=lambda: lock_visible)
        self.nav_list = _List()


@pytest.mark.parametrize("lock_visible", [False, True])
def test_communities_comes_right_after_calls_and_before_settings(lock_visible):
    nav = _Nav(lock_visible)
    nav.rebuild_items()
    keys = nav._nav_keys
    assert keys.index("communities") == keys.index("calls") + 1
    assert keys[-1] == "settings"
    assert nav.nav_list.rows[keys.index("communities")] == "Communities alt+8"
    assert len(nav.nav_list.rows) == len(keys)


def test_choosing_communities_opens_its_window_and_switches_no_panel(monkeypatch):
    shown = Mock()
    monkeypatch.setattr("ui.dialogs.communities.show_communities", shown)
    nav = _Nav()
    nav.rebuild_items()
    mw = nav.main_window
    mw.lock_chat_vault = Mock()
    mw.conversations_panel = SimpleNamespace(Hide=Mock())
    nav.on_nav_item_selected(SimpleNamespace(GetIndex=lambda: nav._nav_keys.index("communities")))
    shown.assert_called_once_with(mw, mw)
    mw.lock_chat_vault.assert_not_called()
    mw.conversations_panel.Hide.assert_not_called()
