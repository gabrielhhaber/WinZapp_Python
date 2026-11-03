"""The settings window builds a page when it is first needed (settings_lazy.py).

Ctrl+, took 4 to 5 seconds, building every control of every page. What must hold
now that a control may not exist yet, above all that no setting is ever lost:

* A unit never opened is left out of validating and saving: nothing it shows was
  loaded, so writing it back would overwrite what is stored.
* A unit that was opened loads everything it saves.
* Nothing that runs on every window (a key press, OK, Cancel) builds a page.

The dialog itself is only built by tests/test_settings_lazy_gui.py, in CI. Here
the rules are read from the source, statement by statement, so a control added
to the wrong place fails on any machine.
"""

import ast
import pathlib
import types

import pytest

from ui.dialogs import settings_lazy
from ui.dialogs.settings_dialog import SettingsDialog
from ui.dialogs.settings_lazy import (
    LazyUnitsMixin, PAGE_UNITS, UNITS, attributes_created, ready, unit_of_attribute, wanted,
)
from ui.dialogs.settings_pages import SettingsPages, SubPageDialog

SOURCE = (pathlib.Path(__file__).resolve().parents[1] / "client" / "ui" / "dialogs"
          / "settings_dialog.py").read_text(encoding="utf-8")
_CLASS = next(n for n in ast.parse(SOURCE).body
              if isinstance(n, ast.ClassDef) and n.name == "SettingsDialog")
METHODS = {n.name: n for n in _CLASS.body if isinstance(n, ast.FunctionDef)}
#: Exist from the start: the pages, the windows, the buttons of the dialog.
SKELETON = {
    t.attr for method in ("__init__", "_build_ui") for node in ast.walk(METHODS[method])
    if isinstance(node, ast.Assign) for t in node.targets if isinstance(t, ast.Attribute)}


def _unit_of(attribute):
    return None if attribute in SKELETON else unit_of_attribute(SettingsDialog, attribute)


def _self_attributes(node):
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "self"}


def _guard_unit(node):
    """The unit an `if ready(self, "x"):` / `if wanted(unit, "x"):` protects."""
    test = node.test if isinstance(node, ast.If) else None
    if (isinstance(test, ast.Call) and isinstance(test.func, ast.Name)
            and test.func.id in ("ready", "wanted") and len(test.args) == 2
            and isinstance(test.args[1], ast.Constant)):
        return test.args[1].value
    return None


def _unguarded_controls(method):
    """(line, attribute, unit it needs, guard it sits under) for each control a
    top-level statement of `method` reaches outside its own unit's guard."""
    found = []
    for statement in METHODS[method].body:
        guard = _guard_unit(statement)
        for attribute in _self_attributes(statement):
            unit = _unit_of(attribute)
            if unit is not None and unit != guard:
                found.append((statement.lineno, attribute, unit, guard))
    return found


class TestWhatIsTouched:
    @pytest.mark.parametrize("method", [
        "_load_values", "_validate", "_apply_values", "_refresh_dialog_labels"])
    def test_every_control_sits_under_the_guard_of_its_own_unit(self, method):
        """A control read outside its guard builds a page on every OK; one
        written under another's would be loaded without being saved, or saved
        without being loaded, and that second one is a setting lost."""
        assert _unguarded_controls(method) == []

    @pytest.mark.parametrize("method", ["_on_ok", "_on_cancel", "_on_apply", "_mark_dirty",
                                        "_on_dialog_char_hook", "_on_settings_page_changed",
                                        "_build_ui", "__init__", "_maybe_warn_restart_required"])
    def test_what_runs_on_every_window_builds_nothing(self, method):
        """Reaching a control here would still work (__getattr__), and open the
        window as slowly as before without anybody noticing."""
        body = METHODS[method]
        # A lambda runs when its button is pressed, not here; a guard is the point.
        safe = {id(inner) for outer in ast.walk(body)
                if isinstance(outer, ast.Lambda) or (isinstance(outer, ast.If) and _guard_unit(outer))
                for inner in ast.walk(outer)}
        offenders = [
            (node.lineno, node.attr) for node in ast.walk(body)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self" and _unit_of(node.attr) and id(node) not in safe]
        assert offenders == []

    def test_each_page_of_the_list_has_a_unit(self):
        added = [node for node in ast.walk(METHODS["_build_ui"])
                 if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "AddPage"]
        assert [n.args[0].attr for n in added] == [attribute for attribute, _unit in PAGE_UNITS]
        assert {unit for _attribute, unit in PAGE_UNITS} <= set(UNITS)

    def test_each_unit_has_its_builder(self):
        for builder in UNITS.values():
            assert callable(getattr(SettingsDialog, builder)), builder

    def test_the_controls_of_a_page_belong_to_its_unit(self):
        assert unit_of_attribute(SettingsDialog, "_lang_combo") == "general"
        assert unit_of_attribute(SettingsDialog, "_notifications_check") == "notifications"
        assert unit_of_attribute(SettingsDialog, "_custom_api_check") == "connection"
        assert unit_of_attribute(SettingsDialog, "_voice_channels_radio") == "audio"
        assert unit_of_attribute(SettingsDialog, "_transcription_auto_check") == "transcription"
        assert unit_of_attribute(SettingsDialog, "_shortcut_list") == "shortcuts"

    def test_a_name_nobody_builds_has_no_unit(self):
        assert unit_of_attribute(SettingsDialog, "_no_such_control") is None

    def test_a_window_title_and_a_category_name_follow_the_language_unopened(self):
        """They exist from the start, so they may not be under a guard: the
        window would keep the language it was created in."""
        refresh = METHODS["_refresh_dialog_labels"]
        guarded = [s for s in refresh.body if _guard_unit(s)]
        for statement in guarded:
            for node in ast.walk(statement):
                assert getattr(node, "attr", "") not in ("SetTitle", "SetPageText")


class TestTheGuards:
    def test_ready_only_for_what_was_built(self):
        owner = types.SimpleNamespace(_built_units={"general"})
        assert ready(owner, "general") and not ready(owner, "calls")

    def test_a_stub_has_everything(self):
        assert ready(types.SimpleNamespace(), "calls")

    def test_a_load_covers_its_own_unit(self):
        assert wanted("calls", "calls") and not wanted("calls", "general")

    def test_a_load_with_no_unit_covers_everything(self):
        assert wanted(None, "calls")


class _Dialog(LazyUnitsMixin):
    def __init__(self):
        self._built_units = set()
        self._building_units = set()
        self._loading_values = False
        self.loaded = []
        self.built = []

    def _build_general_controls(self):
        self.built.append("general")
        self._general_field = "field"

    def _build_notifications_controls(self):
        self.built.append("notifications")
        raise RuntimeError("no such control")

    def _load_values(self, unit):
        self.loaded.append((unit, self._loading_values))


@pytest.fixture
def units(monkeypatch):
    monkeypatch.setattr(settings_lazy, "UNITS", {
        "general": "_build_general_controls",
        "notifications": "_build_notifications_controls"})
    settings_lazy._owners_of_class.pop(_Dialog, None)
    yield
    settings_lazy._owners_of_class.pop(_Dialog, None)


class TestBuildingAUnit:
    def test_it_is_built_and_loaded_once(self, units):
        dialog = _Dialog()
        dialog._ensure_unit("general")
        dialog._ensure_unit("general")
        assert dialog.built == ["general"]
        assert dialog.loaded == [("general", True)]

    def test_nothing_it_sets_counts_as_a_change(self, units):
        dialog = _Dialog()
        dialog._ensure_unit("general")
        assert dialog._loading_values is False

    def test_a_control_reached_early_is_built_instead_of_raising(self, units):
        dialog = _Dialog()
        assert dialog._general_field == "field"
        assert "general" in dialog._built_units

    def test_a_name_nobody_builds_still_raises(self, units):
        dialog = _Dialog()
        with pytest.raises(AttributeError):
            dialog._nothing_like_this
        assert getattr(dialog, "_nothing_like_this", "default") == "default"

    def test_a_unit_that_failed_is_not_saved_nor_built_again(self, units):
        """Half a page holds half its values: saving it would lose the rest."""
        dialog = _Dialog()
        dialog._ensure_unit("notifications")
        assert not ready(dialog, "notifications")
        assert "notifications" in dialog._failed_units
        assert dialog._loading_values is False


class TestTheInstallWideBaseline:
    """What was loaded is what OK compares with (_global_control_changed())."""

    class _Stub:
        _load_install_wide_values = SettingsDialog._load_install_wide_values
        _lang_codes = ["pt-BR"]

        def __init__(self):
            self._built_units = {"general"}
            self.values = {"language": "pt-BR"}
            self.main_window = types.SimpleNamespace(install_wide_values=lambda: {})
            self._wpp_all_updates_check = types.SimpleNamespace(SetValue=lambda value: None)

        def _global_control_values(self):
            return dict(self.values)

    def test_opening_a_page_does_not_take_a_pending_change_for_the_original(self):
        """Language changed and not saved yet, then another page opened: taken
        as the baseline, the change would look like no change and be lost."""
        stub = self._Stub()
        stub._loaded_global_values = {"language": "pt-BR"}
        stub.values = {"language": "en-US", "wpp_all_updates": True}
        stub._load_install_wide_values("advanced")
        assert stub._loaded_global_values == {"language": "pt-BR", "wpp_all_updates": True}

    def test_a_page_with_no_install_wide_option_reads_nothing(self):
        stub = self._Stub()
        stub.main_window = None
        stub._load_install_wide_values("calls")


class TestTheWindowOfAButton:
    def _dialog(self, first_open=None, build=None):
        events = []
        stub = types.SimpleNamespace(
            _first_open=first_open, _build=build,
            CentreOnParent=lambda: events.append("centre"),
            ShowModal=lambda: events.append("modal"))
        return stub, events

    def test_it_is_built_the_first_time_only(self):
        stub, events = self._dialog()
        stub._build = lambda: events.append("build")
        SubPageDialog.open(stub)
        SubPageDialog.open(stub)
        assert events == ["build", "centre", "modal", "centre", "modal"]

    def test_it_is_built_before_its_first_open_hook(self):
        stub, events = self._dialog(first_open=lambda: events.append("load"))
        stub._build = lambda: events.append("build")
        SubPageDialog.open(stub)
        assert events == ["build", "centre", "load", "modal"]


class TestTheSearchReachesWindowsNotBuiltYet:
    class _Book:
        _subpage_window = SettingsPages._subpage_window

        def __init__(self, parent):
            self.parent = parent

        def GetParent(self):
            return self.parent

    def test_a_window_not_built_is_nothing_yet(self):
        book = self._Book(types.SimpleNamespace())
        assert book._subpage_window("_speech_page") is None

    def test_looking_builds_nothing(self):
        """Read off the instance, so __getattr__ is never asked."""
        class _Owner:
            def __getattr__(self, name):
                raise AssertionError(f"{name} was asked for")

        assert self._Book(_Owner())._subpage_window("_speech_page") is None

    def test_a_built_window_is_found(self):
        window = object()
        book = self._Book(types.SimpleNamespace(_speech_page=window))
        assert book._subpage_window("_speech_page") is window

    def test_a_dotted_path_goes_through_the_page(self):
        panel = object()
        page = types.SimpleNamespace(auto_panel=panel)
        book = self._Book(types.SimpleNamespace(_ai_page=page))
        assert book._subpage_window("_ai_page.auto_panel") is panel

    def test_a_window_given_directly_is_itself(self):
        window = object()
        assert self._Book(None)._subpage_window(window) is window
