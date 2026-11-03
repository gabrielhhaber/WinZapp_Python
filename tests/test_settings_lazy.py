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
import re
import types

import pytest

from ui.dialogs import settings_lazy, settings_pages
from ui.dialogs.settings_dialog import SettingsDialog
from ui.dialogs.settings_lazy import (
    LazyUnitsMixin, PAGE_UNITS, UNITS, attributes_created, ready, unit_of_attribute, wanted,
)
from ui.dialogs.settings_pages import SettingsPages, SubPageDialog, new_entries

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
        assert unit_of_attribute(SettingsDialog, "_noise_reduction_check") == "audio"
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

    def _build_sound_events_controls(self):
        self.built.append("sounds")

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

    def test_what_a_unit_needs_is_built_before_it(self, units, monkeypatch):
        monkeypatch.setitem(settings_lazy.UNITS, "sounds", "_build_sound_events_controls")
        monkeypatch.setattr(settings_lazy, "NEEDS", {"sounds": ("general",)})
        dialog = _Dialog()
        dialog._ensure_unit("sounds")
        assert dialog.built == ["general", "sounds"]
        assert [unit for unit, _ in dialog.loaded] == ["general", "sounds"]

    def test_the_typing_sound_needs_the_spoken_content_page(self):
        """TypingFeedbackControls binds the typing announcement box."""
        assert "speech" in settings_lazy.NEEDS["sounds"]
        assert list(settings_lazy.UNITS).index("speech") < list(settings_lazy.UNITS).index("sounds")

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

    def test_a_unit_that_failed_is_not_tried_again(self, units):
        """A second run would put a second set of controls on the page."""
        dialog = _Dialog()
        dialog._ensure_unit("notifications")
        dialog._ensure_unit("notifications")
        assert dialog.built == ["notifications"]

    def test_a_control_of_a_unit_that_failed_is_an_attribute_error(self, units, monkeypatch):
        """getattr(..., default) catches AttributeError, not KeyError."""
        monkeypatch.setattr(settings_lazy, "unit_of_attribute",
                            lambda cls, name: "notifications" if name == "_bell" else None)
        dialog = _Dialog()
        assert getattr(dialog, "_bell", "none") == "none"

    def test_a_unit_that_failed_is_not_saved_nor_built_again(self, units):
        """Half a page holds half its values: saving it would lose the rest."""
        dialog = _Dialog()
        dialog._ensure_unit("notifications")
        assert not ready(dialog, "notifications")
        assert "notifications" in dialog._failed_units
        assert dialog._loading_values is False


class _Book:
    """What the loading asks of the page book."""

    search_loading = False

    def __init__(self):
        self.refreshed = 0

    def refresh_search(self):
        self.refreshed += 1

    def set_search_loading(self, loading):
        self.search_loading = loading


class _Timer:
    def __init__(self, delay, callback):
        self.delays = [delay]
        self.callback = callback
        self.stopped = False

    def Start(self, delay):
        self.delays.append(delay)

    def Stop(self):
        self.stopped = True


@pytest.fixture
def searching(units, monkeypatch):
    timers = []

    def later(delay, callback):
        timers.append(_Timer(delay, callback))
        return timers[-1]

    monkeypatch.setattr(settings_lazy.wx, "CallLater", later)
    monkeypatch.setattr(_Dialog, "_build_notifications_controls", lambda self: self.built.append("n"))
    dialog = _Dialog()
    dialog._notebook = _Book()
    return dialog, timers


class TestLoadingAroundTheTyping:
    """Controls can only be made on the main thread, so what keeps the window
    answering is going back to the event loop between units."""

    def test_a_key_starts_no_build_at_all(self, searching):
        dialog, timers = searching
        dialog._load_units_for_search()
        assert dialog.built == []
        assert timers[0].delays == [settings_lazy.TYPING_PAUSE_MS]

    def test_the_results_say_the_search_is_not_over(self, searching):
        dialog, _timers = searching
        dialog._load_units_for_search()
        assert dialog._notebook.search_loading is True

    def test_each_key_pushes_the_next_unit_back(self, searching):
        dialog, timers = searching
        dialog._load_units_for_search()
        dialog._load_units_for_search()
        assert len(timers) == 1
        assert timers[0].delays == [settings_lazy.TYPING_PAUSE_MS] * 2

    def test_one_unit_per_turn_of_the_event_loop(self, searching):
        dialog, timers = searching
        dialog._load_units_for_search()
        dialog._build_next_unit()
        assert dialog.built == ["general"]
        assert timers[0].delays[-1] == settings_lazy.BETWEEN_UNITS_MS

    def test_each_unit_reaches_the_results(self, searching):
        dialog, _timers = searching
        dialog._load_units_for_search()
        dialog._build_next_unit()
        assert dialog._notebook.refreshed == 1

    def test_the_last_unit_ends_the_loading(self, searching):
        dialog, timers = searching
        dialog._load_units_for_search()
        dialog._build_next_unit()
        dialog._build_next_unit()
        assert dialog.built == ["general", "n"]
        assert dialog._notebook.search_loading is False
        assert timers[0].delays.count(settings_lazy.BETWEEN_UNITS_MS) == 1

    def test_with_everything_built_a_key_starts_nothing(self, searching):
        dialog, timers = searching
        dialog._ensure_all_units()
        dialog._load_units_for_search()
        assert timers == []
        assert dialog._notebook.search_loading is False

    def test_a_page_opened_meanwhile_is_not_built_twice(self, searching):
        dialog, _timers = searching
        dialog._load_units_for_search()
        dialog._ensure_unit("notifications")
        dialog._build_next_unit()
        dialog._build_next_unit()
        assert dialog.built == ["n", "general"]

    def test_closing_stops_the_loading(self, searching):
        dialog, timers = searching
        dialog._load_units_for_search()
        dialog._stop_unit_loading()
        assert timers[0].stopped is True

    def test_stopping_before_any_search_is_nothing(self, searching):
        searching[0]._stop_unit_loading()

    def test_saving_resumes_a_search_still_loading(self, searching):
        dialog, timers = searching
        dialog._load_units_for_search()
        dialog._stop_unit_loading()
        dialog._resume_unit_loading()
        assert timers[0].delays[-1] == settings_lazy.BETWEEN_UNITS_MS

    def test_saving_with_no_search_resumes_nothing(self, searching):
        dialog, timers = searching
        dialog._resume_unit_loading()
        assert timers == []

    def test_a_timer_outliving_the_window_does_nothing(self, searching, monkeypatch):
        dialog, _timers = searching
        monkeypatch.setattr(_Dialog, "__bool__", lambda self: False, raising=False)
        dialog._build_next_unit()
        assert dialog.built == []

    def test_a_page_the_search_built_is_still_fitted_when_shown(self, searching):
        dialog, _timers = searching
        fitted = []
        page = object()
        dialog._fit_page = fitted.append
        dialog._fitted_pages = set()
        dialog._built_units = {"general"}
        dialog._general_page = page
        dialog._notebook.GetPage = lambda index: page
        dialog._ensure_page_of_index(0)
        dialog._ensure_page_of_index(0)
        assert fitted == [page]

    @pytest.mark.parametrize("method", ["_on_ok", "_on_apply", "_on_cancel"])
    def test_nothing_is_built_while_saving_or_closing(self, method):
        first = METHODS[method].body[0]
        assert first.value.func.attr == "_stop_unit_loading"


def _entry(category, label):
    return types.SimpleNamespace(category=category, label=label)


class TestTheResultsArriving:
    def test_what_was_found_is_not_found_again(self):
        assert new_entries([_entry("Audio", "Noise")], [_entry("Audio", "Noise")]) == []

    def test_a_new_match_is_added(self):
        new = _entry("Calls", "Noise")
        assert new_entries([_entry("Audio", "Noise")], [_entry("Audio", "Noise"), new]) == [new]

    def test_the_same_label_on_another_page_is_another_result(self):
        assert len(new_entries([_entry("Audio", "Device")], [_entry("Calls", "Device")])) == 1

    def _book(self, loading, found=()):
        return types.SimpleNamespace(
            _found=list(found), _loading=loading, _loading_text="still", _no_results="none")

    def test_nothing_found_is_only_said_at_the_end(self):
        assert SettingsPages._result_rows(self._book(True)) == ["still"]
        assert SettingsPages._result_rows(self._book(False)) == ["none"]

    def test_results_show_while_it_loads(self):
        book = self._book(True, [_entry("Audio", "Noise")])
        assert SettingsPages._result_rows(book) == ["Audio: Noise"]


class _Rows:
    """The category list, or the results list: what was done to it."""

    def __init__(self, focused=False):
        self.focused = focused
        self.calls = []

    def HasFocus(self):
        return self.focused

    def GetItemCount(self):
        return 1 + sum(call[0] == "insert" for call in self.calls)

    def InsertItem(self, row, text):
        self.calls.append(("insert", row, text))

    def SetItemText(self, row, text):
        self.calls.append(("rewrite", row, text))

    def Set(self, rows):
        self.calls.append(("set", rows))

    def Append(self, text):
        self.calls.append(("append", text))

    def SetSelection(self, row):
        self.calls.append(("select", row))

    def Freeze(self):
        pass

    def Thaw(self):
        pass


class _RunningTimer:
    def __init__(self, running):
        self.running = running
        self.started = []

    def IsRunning(self):
        return self.running

    def Start(self, delay):
        self.started.append(delay)


class _SearchBook:
    """SettingsPages with its lists replaced, nothing else."""

    refresh_search = SettingsPages.refresh_search
    _refresh_results_row = SettingsPages._refresh_results_row
    _on_list_focus_lost = SettingsPages._on_list_focus_lost
    _results_row_settled = SettingsPages._results_row_settled
    _result_rows = SettingsPages._result_rows
    _results_title = SettingsPages._results_title
    _results_template = "Results: {count}"
    _no_results = "none"
    _loading_text = "still"
    _results_row_stale = False
    _results_row_timer = None
    _rows_shown = None
    _guard = False
    _searching = True
    _loading = True

    def __init__(self, list_focus=False, results_focus=False, found=()):
        self._list = _Rows(list_focus)
        self._results = _Rows(results_focus)
        self._found = list(found)
        self._shown = sorted({e.page_index for e in found})
        self._titles = ["Audio", "Calls"]
        self._search = types.SimpleNamespace(GetValue=lambda: "x")
        self.pending = []

    def _matches(self, query):
        return self._found + self.pending

    def add(self, entry):
        self.pending = [entry]
        self.refresh_search()
        self.pending = []


def _hit(page, label):
    return types.SimpleNamespace(page_index=page, category=f"page {page}", label=label)


class TestTheScreenReaderDuringASearch:
    """The pages arrive on a timer, wherever the focus is. NVDA reads a row whose
    name changes under the focus, so the lists may grow but not be rewritten."""

    def test_a_category_is_added_under_the_focus(self):
        book = _SearchBook(list_focus=True)
        book.add(_hit(1, "Ringtone"))
        assert ("insert", 1, "Calls") in book._list.calls

    def test_the_row_with_the_count_is_not_rewritten_under_the_focus(self):
        book = _SearchBook(list_focus=True)
        for label in ("One", "Two", "Three"):
            book.add(_hit(1, label))
        assert not [call for call in book._list.calls if call[0] == "rewrite"]
        assert book._results_row_stale is True

    def test_with_the_focus_elsewhere_it_follows_the_count(self):
        book = _SearchBook()
        book.add(_hit(1, "One"))
        assert ("rewrite", 0, "Results: 1") in book._list.calls

    def test_it_waits_a_moment_after_the_focus_leaves(self):
        """NVDA finds out late: rewritten now, it would still be read."""
        book = _SearchBook(list_focus=True)
        book.add(_hit(1, "One"))
        book._list.focused = False
        book._results_row_timer = _RunningTimer(True)
        book.add(_hit(1, "Two"))
        assert not [call for call in book._list.calls if call[0] == "rewrite"]

    def test_leaving_the_list_starts_the_wait(self, monkeypatch):
        book = _SearchBook(list_focus=True)
        book.add(_hit(1, "One"))
        book._list.focused = False
        timers = []
        monkeypatch.setattr(settings_pages.wx, "CallLater", lambda delay, fn: timers.append(delay) or "t")
        book._on_list_focus_lost(types.SimpleNamespace(Skip=lambda: None))
        assert timers == [settings_pages.RESULTS_ROW_HOLD_MS]

    def test_leaving_again_starts_the_wait_over(self):
        book = _SearchBook()
        book._results_row_stale = True
        book._results_row_timer = _RunningTimer(True)
        book._on_list_focus_lost(types.SimpleNamespace(Skip=lambda: None))
        assert book._results_row_timer.started == [settings_pages.RESULTS_ROW_HOLD_MS]

    def test_leaving_with_nothing_waiting_still_starts_the_wait(self, monkeypatch):
        """A page arriving just after the focus left is as early for NVDA as
        one that arrived before it."""
        book = _SearchBook()
        timers = []
        monkeypatch.setattr(settings_pages.wx, "CallLater", lambda delay, fn: timers.append(delay) or "t")
        book._on_list_focus_lost(types.SimpleNamespace(Skip=lambda: None))
        assert timers == [settings_pages.RESULTS_ROW_HOLD_MS]

    def test_after_a_search_leaving_starts_no_wait(self):
        book = _SearchBook()
        book._searching = False
        book._on_list_focus_lost(types.SimpleNamespace(Skip=lambda: None))
        assert book._results_row_timer is None

    def test_a_page_that_brings_nothing_leaves_the_placeholder_alone(self):
        """Set again, "still searching" would be read for every page."""
        book = _SearchBook(results_focus=True)
        book.refresh_search()
        book.refresh_search()
        assert book._results.calls == [("set", ["still"]), ("select", 0)]

    def test_after_the_wait_the_count_is_right(self):
        book = _SearchBook(list_focus=True)
        book.add(_hit(1, "One"))
        book._list.focused = False
        book._results_row_timer = _RunningTimer(False)
        book._results_row_settled()
        assert ("rewrite", 0, "Results: 1") in book._list.calls
        assert book._results_row_stale is False

    def test_back_on_the_list_before_the_wait_ends_it_keeps_waiting(self):
        book = _SearchBook(list_focus=True)
        book.add(_hit(1, "One"))
        book._results_row_timer = _RunningTimer(False)
        book._results_row_settled()
        assert not [call for call in book._list.calls if call[0] == "rewrite"]
        assert book._results_row_stale is True

    def test_a_search_that_ended_rewrites_nothing(self):
        book = _SearchBook()
        book._results_row_stale = True
        book._searching = False
        book._results_row_settled()
        assert book._list.calls == []

    def test_a_window_gone_rewrites_nothing(self, monkeypatch):
        book = _SearchBook()
        book._results_row_stale = True
        monkeypatch.setattr(_SearchBook, "__bool__", lambda self: False, raising=False)
        book._results_row_settled()
        assert book._list.calls == []

    def test_a_result_on_the_way_adds_to_the_list_and_keeps_its_rows(self):
        found = _hit(0, "First")
        book = _SearchBook(results_focus=True, found=[found])
        book.add(_hit(0, "Second"))
        assert book._results.calls == [("append", "page 0: Second")]

    def test_the_first_result_replaces_the_row_under_the_focus_and_is_chosen(self):
        book = _SearchBook(results_focus=True)
        book.add(_hit(0, "First"))
        assert book._results.calls == [("set", ["page 0: First"]), ("select", 0)]

    def test_with_the_focus_elsewhere_nothing_is_chosen(self):
        book = _SearchBook()
        book.add(_hit(0, "First"))
        assert book._results.calls == [("set", ["page 0: First"])]

    def test_the_end_of_a_search_with_no_match_reaches_the_focus(self):
        book = _SearchBook(results_focus=True)
        book._loading = False
        book.refresh_search()
        assert book._results.calls == [("set", ["none"]), ("select", 0)]


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



class TestWhatTheReviewFound:
    """Checks that ran for every OK before the pages were built lazily, and
    must still run when the page that holds them was never opened."""

    def test_the_auto_download_follows_only_a_storage_page_that_was_built(self):
        """REGRESSION: with Storage unopened and no `storage` key, OK raised
        KeyError after saving, and the window stayed open."""
        at = SOURCE.index("_on_auto_download_settings_changed(")
        assert 'if ready(self, "storage"):' in SOURCE[at - 120:at]

    def test_a_new_global_hotkey_builds_the_shortcuts_to_be_checked(self):
        at = SOURCE.index("def _apply_values")
        body = SOURCE[at:SOURCE.index("if not self._validate():", at)]
        assert 'self._global_hotkey_changed()' in body
        assert body.index('self._ensure_unit("shortcuts")') < body.index("_validate_shortcut_values")

    @pytest.mark.parametrize("stored,field,changed", [
        (None, (0, 0), False),
        ({"vk": 0, "mod": 3}, (0, 0), False),
        ({"vk": 65, "mod": 3}, (65, 3), False),
        ({"vk": 65, "mod": 3}, (66, 3), True),
        (None, (65, 2), True),
    ])
    def test_global_hotkey_changed(self, stored, field, changed):
        general = {} if stored is None else {"global_hotkey": stored}
        stub = types.SimpleNamespace(
            main_window=types.SimpleNamespace(settings={"general": general}),
            _hotkey_field=types.SimpleNamespace(_vk=field[0], _mod=field[1]))
        assert SettingsDialog._global_hotkey_changed(stub) is changed

    def test_both_automatic_transcriptions_are_checked_from_either_page(self):
        assert 'if ready(self, "transcription") or ready(self, "ai"):' in SOURCE

    def test_the_ai_page_asks_before_replacing_the_local_transcription(self):
        at = SOURCE.index("def _build_ai_controls")
        body = SOURCE[at:SOURCE.index("def ", at + 10)]
        assert "confirm_replaces_local = self._confirm_online_replaces_local" in body

    def test_errors_point_at_their_page_not_at_a_number(self):
        assert not re.search(r"_notebook\.SetSelection\(\d+\)", SOURCE)

