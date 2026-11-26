"""Settings > Shortcuts: a tree of categories and a search field.

Reported: every row of the list said its category before the command, so the
same word was heard again and again. The list is now a tree: the category is
said once, on its own row, and opens with the right arrow. The search field
above it filters by category, command or keys, and Down arrow goes into the
results, as in the settings' own search.
"""

from types import SimpleNamespace

import wx

from core.keyboard_shortcuts import Shortcut
from ui.dialogs.shortcuts_tab import ShortcutsTabMixin, shortcut_groups

A = Shortcut("main.a", "main", "a", "label_a", 0, 0)
B = Shortcut("main.b", "main", "b", "label_b", 0, 0)
C = Shortcut("status.c", "status", "c", "label_c", 0, 0)
D = Shortcut("status_list.d", "status_list", "d", "label_d", 0, 0)
CATEGORY = {"main": "Navigation", "status": "Status", "status_list": "Status"}
TEXT = {A: "Open chats: Alt+1", B: "Ábrir áudio: Ctrl+P", C: "Reply: Ctrl+R", D: "Delete: Delete"}


def _groups(query=""):
    return shortcut_groups([A, B, C, D], lambda s: CATEGORY[s.scope], lambda s: TEXT[s], query)


def test_each_category_once_in_the_order_it_first_appears():
    assert [(category, [s for s, _t in items]) for category, items in _groups()] == [
        ("Navigation", [A, B]), ("Status", [C, D])]


def test_the_rows_say_the_command_and_keys_without_the_category():
    [(_category, items), _] = _groups()
    assert items[0] == (A, "Open chats: Alt+1")


def test_the_search_keeps_only_matching_commands_and_their_categories():
    assert _groups("reply") == [("Status", [(C, "Reply: Ctrl+R")])]
    assert _groups("ctrl") == [("Navigation", [(B, TEXT[B])]), ("Status", [(C, TEXT[C])])]


def test_the_search_ignores_case_and_accents_and_needs_every_word():
    assert [s for _c, items in _groups("ABRIR audio") for s, _t in items] == [B]
    assert _groups("status delete") == [("Status", [(D, TEXT[D])])]
    assert _groups("reply alt") == []


class _Tree:
    """Enough of a wx.TreeCtrl for the key handlers."""

    def __init__(self, first_item, previous_ok):
        self.first_item = first_item
        self.previous_ok = previous_ok
        self.selected = None
        self.focused = False

    def GetSelection(self):
        return SimpleNamespace(IsOk=lambda: True)

    def GetPrevVisible(self, item):
        return SimpleNamespace(IsOk=lambda: self.previous_ok)

    def EnsureVisible(self, item):
        pass

    def SelectItem(self, item):
        self.selected = item

    def SetFocus(self):
        self.focused = True


class _Tab:
    for _name in ("_on_shortcut_search_key", "_on_shortcut_tree_key"):
        locals()[_name] = getattr(ShortcutsTabMixin, _name)
    del _name

    def __init__(self, first_item="first", previous_ok=True):
        self._shortcut_list = _Tree(first_item, previous_ok)
        self._shortcut_search = SimpleNamespace(SetFocus=self._search_focused)
        self.search_focused = False
        self._first = first_item

    def _first_shortcut_item(self):
        return self._first

    def _search_focused(self):
        self.search_focused = True


class _Key:
    def __init__(self, code):
        self.code = code
        self.skipped = False

    def GetKeyCode(self):
        return self.code

    def Skip(self):
        self.skipped = True


def test_down_from_the_search_goes_to_the_first_command():
    tab, key = _Tab(), _Key(wx.WXK_DOWN)
    tab._on_shortcut_search_key(key)
    assert tab._shortcut_list.selected == "first" and tab._shortcut_list.focused
    assert not key.skipped


def test_down_with_no_result_is_left_to_the_field():
    tab, key = _Tab(first_item=None), _Key(wx.WXK_DOWN)
    tab._on_shortcut_search_key(key)
    assert key.skipped and not tab._shortcut_list.focused


def test_up_from_the_first_row_goes_back_to_the_search():
    tab, key = _Tab(previous_ok=False), _Key(wx.WXK_UP)
    tab._on_shortcut_tree_key(key)
    assert tab.search_focused and not key.skipped


def test_up_from_another_row_moves_in_the_tree():
    tab, key = _Tab(previous_ok=True), _Key(wx.WXK_UP)
    tab._on_shortcut_tree_key(key)
    assert key.skipped and not tab.search_focused


# ── Change: the capture window ──────────────────────────────────────────────

def test_change_hands_the_main_window_to_the_capture_window(monkeypatch):
    """Reported from an installed build: Change raised AttributeError
    ('SubPageDialog' object has no attribute 'main_window'), because the
    capture window read it off its parent, the Shortcuts window."""
    made = []

    class _Capture:
        def __init__(self, *args, **kwargs):
            made.append((args, kwargs))

        def ShowModal(self):
            return wx.ID_CANCEL

        def Destroy(self):
            pass

    monkeypatch.setattr("ui.dialogs.shortcuts_tab.ShortcutCaptureDialog", _Capture)
    main_window = SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key), settings={})
    tab = SimpleNamespace(main_window=main_window, _shortcuts_dialog=object(),
                          _selected_shortcut=lambda: A, _shortcut_binding=lambda s: None,
                          _shortcut_is_shared=lambda s: None)
    ShortcutsTabMixin._change_shortcut(tab)
    [(args, kwargs)] = made
    assert args[0] is tab._shortcuts_dialog
    assert kwargs["main_window"] is main_window


def test_the_capture_window_takes_the_main_window_it_is_given():
    """Built without a window: its constructor reads main_window from the
    argument first."""
    import inspect
    from ui.dialogs.shortcut_capture import ShortcutCaptureDialog
    source = inspect.getsource(ShortcutCaptureDialog.__init__)
    assert "main_window if main_window is not None else parent.main_window" in source


# ── The selection waits for the tree to take the focus ─────────────────────
#
# Reported: typing the first letter in the search field "left the field for the
# results", and Apply in Settings "jumped to the Shortcuts tab" (Escape there
# then raised a traceback). Rebuilding the tree selected a row, which moves the
# tree's caret, and the screen reader announced it as a focus change.

from ui.dialogs.settings_pages import SubPageDialog
from ui.dialogs.shortcuts_tab import key_search_action, rows_with_binding


class _Item:
    def __init__(self, data=None):
        self.data = data

    def IsOk(self):
        return True


class _NoItem:
    def IsOk(self):
        return False


class _RefreshTree:
    def __init__(self):
        self.selected = None
        self.items = []

    def Freeze(self):
        pass

    def Thaw(self):
        pass

    def DeleteAllItems(self):
        self.items = []
        self.selected = None

    def AddRoot(self, text):
        return _Item()

    def AppendItem(self, parent, text, data=None):
        item = _Item(data)
        self.items.append(item)
        return item

    def Expand(self, node):
        pass

    def GetFirstChild(self, root):
        return (self.items[0] if self.items else _NoItem(), None)

    def GetSelection(self):
        return self.selected or _NoItem()

    def GetItemData(self, item):
        return item.data

    def EnsureVisible(self, item):
        pass

    def SelectItem(self, item):
        self.selected = item


class _RefreshTab:
    for _name in ("_refresh_shortcut_rows", "_selected_shortcut", "_pending_shortcut",
                  "_shortcut_tree_has_focus", "_on_shortcut_tree_focus",
                  "_first_shortcut_item"):
        locals()[_name] = getattr(ShortcutsTabMixin, _name)
    del _name

    def __init__(self, query=""):
        self._shortcut_list = _RefreshTree()
        self._shortcut_search = SimpleNamespace(GetValue=lambda: query)
        self._shortcut_chord = None
        self.main_window = SimpleNamespace(i18n=SimpleNamespace(t=lambda key: key))

    def _shortcut_groups(self):
        return [("Navigation", [(A, "a"), (B, "b")])]

    def _first_shortcut_item(self):
        return self._shortcut_list.items[1]


def _focus(monkeypatch, window):
    monkeypatch.setattr(wx.Window, "FindFocus", staticmethod(lambda: window))


def test_typing_in_the_search_selects_nothing_until_the_tree_has_the_focus(monkeypatch):
    tab = _RefreshTab(query="open")
    _focus(monkeypatch, tab._shortcut_search)
    assert tab._refresh_shortcut_rows() == 2
    assert tab._shortcut_list.selected is None
    # Tab into the tree: the first result is selected then.
    event = _Key(0)
    tab._on_shortcut_tree_focus(event)
    assert tab._shortcut_list.selected.data is A and event.skipped


def test_a_button_pressed_before_the_tree_had_the_focus_acts_on_the_pending_row(monkeypatch):
    tab = _RefreshTab(query="open")
    _focus(monkeypatch, None)
    tab._refresh_shortcut_rows()
    assert tab._selected_shortcut() is A


def test_with_the_tree_focused_the_row_is_selected_at_once(monkeypatch):
    tab = _RefreshTab(query="open")
    _focus(monkeypatch, tab._shortcut_list)
    tab._refresh_shortcut_rows()
    assert tab._shortcut_list.selected.data is A


def test_escape_in_a_window_that_is_not_modal_hides_it_instead_of_asserting():
    calls = []
    dialog = SimpleNamespace(IsModal=lambda: False, Hide=lambda: calls.append("hide"),
                             EndModal=lambda code: calls.append("end"))
    SubPageDialog._on_char(dialog, _Key(wx.WXK_ESCAPE))
    assert calls == ["hide"]
    dialog.IsModal = lambda: True
    SubPageDialog._on_char(dialog, _Key(wx.WXK_ESCAPE))
    assert calls == ["hide", "end"]


# ── Search by keys ──────────────────────────────────────────────────────────

def test_the_keys_search_keeps_every_category_bound_to_the_chord():
    bindings = {A: (2, 70), B: (1, 49), C: (2, 70), D: None}
    assert rows_with_binding([A, B, C, D], bindings.get, (2, 70)) == [A, C]
    assert rows_with_binding([A, B, C, D], bindings.get, [1, 49]) == [B]
    assert rows_with_binding([A, B], bindings.get, None) == [A, B]


def test_the_keys_field_leaves_navigation_and_reserved_keys_alone():
    assert key_search_action("pass") == "pass"                         # Tab, Escape
    assert key_search_action("shortcut_capture_reserved") == "pass"    # Alt+F4
    assert key_search_action("shortcut_capture_wait") == "wait"        # Ctrl alone
    # Space and Delete cannot be assigned without a modifier, but are shipped keys
    assert key_search_action("shortcut_capture_modifier_required") == "search"
    assert key_search_action("") == "search"


class _KeysTab:
    for _name in ("_search_shortcut_keys", "_on_shortcut_search_text", "_clear_shortcut_keys"):
        locals()[_name] = getattr(ShortcutsTabMixin, _name)
    del _name

    def __init__(self, count):
        self.spoken = []
        self.count = count
        self.refreshed = 0
        texts = {"shortcut_keys_search_found": "found {keys} {count}",
                 "shortcut_keys_search_none": "none {keys}",
                 "shortcut_key_control": "Ctrl", "shortcut_key_alt": "Alt",
                 "shortcut_key_shift": "Shift"}
        self.main_window = SimpleNamespace(
            i18n=SimpleNamespace(t=lambda key: texts.get(key, key)),
            speak_output=SimpleNamespace(output=self.spoken.append))
        self._shortcut_chord = None
        self.search_value = "old words"
        self._shortcut_search = SimpleNamespace(ChangeValue=self._set_search)
        self.keys_value = ""
        self._shortcut_keys = SimpleNamespace(ChangeValue=self._set_keys, SetFocus=lambda: None)
        self.clear_enabled = None
        self._shortcut_keys_clear = SimpleNamespace(Enable=self._enable)

    def _set_search(self, value):
        self.search_value = value

    def _set_keys(self, value):
        self.keys_value = value

    def _enable(self, value):
        self.clear_enabled = value

    def _refresh_shortcut_rows(self):
        self.refreshed += 1
        return self.count


def test_a_chord_searches_every_category_and_says_how_many():
    tab = _KeysTab(count=3)
    tab._search_shortcut_keys((2, 70))
    assert tab._shortcut_chord == (2, 70)
    assert tab.search_value == ""            # the name search is emptied
    assert tab.keys_value == "Ctrl+F"
    assert tab.clear_enabled is True
    assert tab.spoken == ["found Ctrl+F 3"]


def test_a_chord_nobody_uses_is_said_so():
    tab = _KeysTab(count=0)
    tab._search_shortcut_keys((7, 90))
    assert tab.spoken == ["none Ctrl+Alt+Shift+Z"]


def test_typing_a_name_or_clearing_ends_the_search_by_keys():
    tab = _KeysTab(count=1)
    tab._search_shortcut_keys((2, 70))
    tab._on_shortcut_search_text(None)
    assert tab._shortcut_chord is None and tab.keys_value == "" and tab.clear_enabled is False
    tab._search_shortcut_keys((2, 70))
    tab._clear_shortcut_keys()
    assert tab._shortcut_chord is None and tab.keys_value == ""
