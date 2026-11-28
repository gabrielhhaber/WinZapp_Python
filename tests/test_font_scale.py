"""Font scale for low vision: the arithmetic, the walk over native controls (with
stub windows, no wx object is created), the settings round trip and the keys.

Off by default: at 100 % nothing is touched, which is what keeps the app
reading exactly as before for everyone who never asks.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import font_scale
from core.shortcut_catalog import SHORTCUTS
from core.utils import DEFAULT_SETTINGS

ROOT = Path(__file__).resolve().parents[1] / "client"


class Font:
    def __init__(self, points):
        self.points = points

    def IsOk(self):
        return True

    def GetFractionalPointSize(self):
        return self.points

    def SetFractionalPointSize(self, points):
        self.points = points


class Window:
    """A window with a font and children; a child with no font of its own
    follows its parent's, as wx does when the parent's SetFont runs."""

    def __init__(self, points=9.0, children=(), own_font=True):
        self.font = Font(points)
        self.children = list(children)
        self.own_font = own_font
        self.frozen = 0
        self.log = []

    def GetChildren(self):
        return self.children

    def GetFont(self):
        return Font(self.font.points)

    def SetFont(self, font):
        self.font = Font(font.points)
        self.own_font = True
        for child in self.children:
            if not child.own_font:
                child.font = Font(font.points)

    def Freeze(self):
        self.frozen += 1
        self.log.append("freeze")

    def Thaw(self):
        self.frozen -= 1
        self.log.append("thaw")


def test_the_default_is_the_current_behaviour():
    assert font_scale.DEFAULT == 100 == font_scale.SCALES[0]
    assert DEFAULT_SETTINGS["user_interface"]["font_scale_percent"] == 100
    assert font_scale.scale_of({}) == 100
    assert font_scale.scale_of({"user_interface": {}}) == 100
    assert font_scale.scale_of({"user_interface": []}) == 100


@pytest.mark.parametrize("stored,expected", [
    (100, 100), (125, 125), (200, 200), (130, 125), (400, 200), (0, 100), (-5, 100),
    (149.9, 150), ("150", 100), (None, 100), (True, 100), ([], 100),
])
def test_a_stored_value_snaps_to_an_offered_size(stored, expected):
    assert font_scale.normalize(stored) == expected


def test_step_walks_the_offered_sizes_and_stops_at_the_ends():
    assert font_scale.step(100, 1) == 110
    assert font_scale.step(110, -1) == 100
    assert font_scale.step(100, -1) == 100
    assert font_scale.step(200, 1) == 200
    assert font_scale.step(175, 0) == 100
    assert font_scale.step(130, 1) == 150      # snapped to 125 first


def test_point_size_is_a_ratio_of_the_base_in_quarters():
    assert font_scale.scaled_point_size(9, 100) == 9
    assert font_scale.scaled_point_size(9, 110) == 10.0
    assert font_scale.scaled_point_size(10, 200) == 20
    assert font_scale.scaled_point_size(9.0, 999) == 18      # a bad percent is normalized


def test_the_walk_scales_every_native_control_inside_the_window():
    leaf_a, leaf_b = Window(9), Window(11)
    panel = Window(9, [leaf_a, leaf_b])
    top = Window(9, [panel])
    font_scale.apply_scale(top, 150)
    assert [w.font.points for w in (leaf_a, leaf_b, panel, top)] == [13.5, 16.5, 13.5, 13.5]


def test_changing_the_scale_twice_never_compounds():
    leaf = Window(10)
    top = Window(10, [leaf])
    for percent in (125, 150, 125, 100):
        font_scale.apply_scale(top, percent)
    assert (leaf.font.points, top.font.points) == (10, 10)


def test_a_child_that_inherits_its_font_is_scaled_once():
    inheriting = Window(9, own_font=False)
    top = Window(9, [inheriting])
    font_scale.apply_scale(top, 200)
    assert inheriting.font.points == 18
    font_scale.apply_scale(top, 200)
    assert inheriting.font.points == 18
    font_scale.apply_scale(top, 100)
    assert (inheriting.font.points, top.font.points) == (9, 9)


def test_at_the_default_a_window_never_scaled_is_left_alone():
    top = Window(9, [Window(11)])
    font_scale.apply_scale(top, 100)
    assert top.font.points == 9 and top.children[0].font.points == 11
    assert not hasattr(top, "_winzapp_font_base_pt")


def test_the_window_is_frozen_while_the_fonts_change_and_always_thawed():
    top = Window(9)
    font_scale.apply_scale(top, 125)
    assert top.log == ["freeze", "thaw"] and top.frozen == 0

    class Boom(Window):
        def SetFont(self, font):
            raise ValueError("no")

    bad = Boom(9)
    with pytest.raises(ValueError):
        font_scale.apply_scale(bad, 125)
    assert bad.frozen == 0


def test_a_destroyed_child_does_not_stop_the_walk():
    class Gone(Window):
        def GetChildren(self):
            raise RuntimeError("wrapped C/C++ object has been deleted")

    top = Window(9, [Gone(9), Window(9)])
    font_scale.apply_scale(top, 150)
    assert top.children[1].font.points == 13.5


class ListLike(Window):
    """A list that loses its selected row when its font changes."""

    def __init__(self):
        super().__init__(9)
        self.selected = 4
        self.focused_row = 2

    def GetCount(self):
        return 10

    def GetSelection(self):
        return self.selected

    def SetSelection(self, row):
        self.selected = row

    def GetFocusedItem(self):
        return self.focused_row

    def Focus(self, row):
        self.focused_row = row

    def SetFont(self, font):
        super().SetFont(font)
        self.selected = -1
        self.focused_row = -1


def test_a_lists_selected_and_focused_rows_survive_a_font_change():
    messages = ListLike()
    top = Window(9, [messages])
    font_scale.apply_scale(top, 150)
    assert (messages.selected, messages.focused_row) == (4, 2)
    assert messages.font.points == 13.5


def test_a_text_fields_selection_range_is_never_treated_as_a_row():
    class Field(Window):
        def GetSelection(self):
            return (1, 3)

        def SetSelection(self, start, end):      # would raise with one argument
            raise AssertionError("must not be called")

    font_scale.apply_scale(Window(9, [Field(9)]), 125)


def test_focus_is_put_back_if_the_change_moved_it():
    field, other = Window(9), Window(9)
    current = {"focus": field}

    def find_focus():
        return current["focus"]

    field.SetFocus = lambda: current.update(focus=field)
    original = Window.SetFont

    def losing(self, font):
        original(self, font)
        current["focus"] = other

    other_top = Window(9, [field])
    other_top.GetChildren()[0].SetFont = lambda font: losing(field, font)
    font_scale.apply_scale(other_top, 125, find_focus)
    assert current["focus"] is field


class Sized(Window):
    def __init__(self, size, needed):
        super().__init__(9)
        self.size, self.needed = size, needed
        self.layouts = 0

    def Layout(self):
        self.layouts += 1

    def GetSize(self):
        return SimpleNamespace(GetWidth=lambda: self.size[0], GetHeight=lambda: self.size[1])

    def GetEffectiveMinSize(self):
        return SimpleNamespace(GetWidth=lambda: self.needed[0], GetHeight=lambda: self.needed[1])

    def SetSize(self, size):
        self.size = tuple(size)


def test_a_dialog_grows_to_fit_larger_text_but_never_shrinks():
    dialog = Sized((400, 300), (520, 280))
    assert font_scale.fit_to_content(dialog) is True
    assert dialog.size == (520, 300)
    assert font_scale.fit_to_content(Sized((400, 300), (300, 200))) is False


def test_growth_stops_at_the_screen():
    dialog = Sized((400, 300), (2400, 1600))
    font_scale.fit_to_content(dialog, (1920, 1040))
    assert dialog.size == (1920, 1040)


# ── settings round trip and wiring ────────────────────────────────────────────

def test_the_settings_mixin_saves_and_loads_the_choice():
    from ui.dialogs.font_scale_options import FontScaleOptionsMixin, font_scale_choice_labels

    class Choice:
        selection = -1

        def SetSelection(self, index):
            self.selection = index

        def GetSelection(self):
            return self.selection

    dialog = SimpleNamespace(main_window=SimpleNamespace(settings={}), _font_scale_choice=Choice())
    save = FontScaleOptionsMixin._save_font_scale_options
    load = FontScaleOptionsMixin._load_font_scale_options
    dialog._font_scale_choice.selection = 3
    save(dialog)
    assert dialog.main_window.settings["user_interface"]["font_scale_percent"] == 150
    dialog._font_scale_choice.selection = -1
    load(dialog)
    assert dialog._font_scale_choice.selection == 3
    dialog.main_window.settings["user_interface"]["font_scale_percent"] = "garbage"
    load(dialog)
    assert dialog._font_scale_choice.selection == 0
    dialog._font_scale_choice.selection = -1          # nothing chosen: the default
    save(dialog)
    assert dialog.main_window.settings["user_interface"]["font_scale_percent"] == 100
    labels = font_scale_choice_labels(SimpleNamespace(t=lambda key: key))
    assert labels == ["ui_font_scale_default", "110%", "125%", "150%", "175%", "200%"]


def test_the_settings_apply_reaches_the_main_window_only_when_it_can():
    from ui.dialogs.font_scale_options import FontScaleOptionsMixin
    calls = []
    dialog = SimpleNamespace(main_window=SimpleNamespace(apply_font_scale=lambda: calls.append(1)))
    FontScaleOptionsMixin._apply_font_scale_live(dialog)
    assert calls == [1]
    FontScaleOptionsMixin._apply_font_scale_live(SimpleNamespace(main_window=SimpleNamespace()))


# ── the main window ───────────────────────────────────────────────────────────

def _main_window(percent=None):
    from main_window.font_scale import FontScaleMixin
    spoken, saved = [], []
    settings = {} if percent is None else {"user_interface": {"font_scale_percent": percent}}
    window = SimpleNamespace(
        settings=settings,
        i18n=SimpleNamespace(t=lambda key: "Font {percent}%"),
        speak_output=SimpleNamespace(output=spoken.append),
        save_settings=lambda: saved.append(dict(settings)),
        apply_font_scale=lambda: saved.append("applied"))
    window.change_font_scale = lambda direction: FontScaleMixin.change_font_scale(window, direction)
    return window, spoken, saved


@pytest.mark.parametrize("start,direction,spoken_text,saved_percent", [
    (None, 1, "Font 110%", 110), (110, -1, "Font 100%", 100), (175, 0, "Font 100%", 100),
])
def test_the_shortcuts_save_apply_and_say_the_new_size(start, direction, spoken_text, saved_percent):
    window, spoken, saved = _main_window(start)
    window.change_font_scale(direction)
    assert spoken == [spoken_text]
    assert window.settings["user_interface"]["font_scale_percent"] == saved_percent
    assert saved[-1] == "applied"


def test_at_the_limit_nothing_is_saved_but_the_size_is_still_said():
    window, spoken, saved = _main_window(200)
    window.change_font_scale(1)
    assert spoken == ["Font 200%"] and saved == []


def test_a_window_shown_at_the_default_is_never_touched():
    from main_window.font_scale import FontScaleMixin
    touched = []
    window = SimpleNamespace(settings={}, _scale_top_level=lambda *a: touched.append(a))
    other = SimpleNamespace(IsTopLevel=lambda: True)
    event = SimpleNamespace(Skip=lambda: None, IsShown=lambda: True, GetEventObject=lambda: other)
    FontScaleMixin._on_font_scale_show(window, event)
    assert touched == []
    window.settings = {"user_interface": {"font_scale_percent": 150}}
    FontScaleMixin._on_font_scale_show(window, event)
    assert touched == [(other, 150)]
    # a control shown inside a window, and the main window itself, are skipped
    for skipped in (SimpleNamespace(IsTopLevel=lambda: False), window):
        touched.clear()
        event = SimpleNamespace(Skip=lambda: None, IsShown=lambda: True,
                                GetEventObject=lambda skipped=skipped: skipped)
        FontScaleMixin._on_font_scale_show(window, event)
        assert touched == []


# ── the shortcuts ─────────────────────────────────────────────────────────────

def _row(shortcut_id):
    [row] = [s for s in SHORTCUTS if s.id == shortcut_id]
    return row


def test_the_catalog_has_the_font_shortcuts_on_free_keys():
    assert [(_row(i).mod, _row(i).key) for i in
            ("main.ID_FONT_BIGGER", "main.ID_FONT_BIGGER.1", "main.ID_FONT_SMALLER",
             "main.ID_FONT_RESET")] == [(2, ord("=")), (6, ord("=")), (2, ord("-")), (6, ord("Z"))]
    taken = {}
    for shortcut in SHORTCUTS:
        taken.setdefault((shortcut.mod, shortcut.key), set()).add(shortcut.command)
    for shortcut_id in ("main.ID_FONT_BIGGER", "main.ID_FONT_BIGGER.1",
                        "main.ID_FONT_SMALLER", "main.ID_FONT_RESET"):
        row = _row(shortcut_id)
        assert taken[(row.mod, row.key)] == {row.command}, shortcut_id


def test_the_main_window_table_and_handlers_are_wired():
    source = (ROOT / "main_window" / "shortcuts.py").read_text(encoding="utf-8-sig")
    for name in ("ID_FONT_BIGGER", "ID_FONT_SMALLER", "ID_FONT_RESET"):
        assert f"self.{name} = wx.NewIdRef()" in re.sub(r"\s+=", " =", source)
    assert "self._on_font_bigger, id=self.ID_FONT_BIGGER" in source
    assert "self._on_font_smaller, id=self.ID_FONT_SMALLER" in source
    assert "self._on_font_reset, id=self.ID_FONT_RESET" in source
    main = (ROOT / "main.py").read_text(encoding="utf-8-sig")
    assert "FontScaleMixin," in main and "self.install_font_scale()" in main


def test_every_choice_and_message_is_in_every_language_file():
    import json
    keys = ("ui_font_scale_label", "ui_font_scale_default", "font_scale_announce",
            "shortcut_font_bigger_label", "shortcut_font_smaller_label", "shortcut_font_reset_label")
    codes = json.loads((ROOT / "languages" / "language_map.json").read_text(encoding="utf-8"))
    for code in (codes if isinstance(codes, list) else codes.keys()):
        po = (ROOT.parent / "translations" / code / "LC_MESSAGES" / "winzapp.po").read_text(encoding="utf-8")
        for key in keys:
            assert f'msgctxt "{key}"' in po, (code, key)
        assert "{percent}" in po.split('msgctxt "font_scale_announce"')[1].split("msgctxt")[0]


def test_the_default_never_walks_a_window_until_a_size_was_chosen(monkeypatch):
    import wx
    from main_window.font_scale import FontScaleMixin
    walked = []
    monkeypatch.setattr(wx, "GetTopLevelWindows", lambda: [object()])
    window = SimpleNamespace(settings={}, _scale_top_level=lambda w, percent: walked.append(percent))
    FontScaleMixin.apply_font_scale(window)
    assert walked == []                       # startup at 100 %: nothing touched
    window.settings = {"user_interface": {"font_scale_percent": 150}}
    FontScaleMixin.apply_font_scale(window)
    window.settings = {}
    FontScaleMixin.apply_font_scale(window)   # back to 100 %: windows get their size back
    FontScaleMixin.apply_font_scale(window)   # and then nothing more to do
    assert walked == [150, 100]
