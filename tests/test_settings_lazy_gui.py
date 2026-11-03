"""The real settings window, built a page at a time (ui/dialogs/settings_lazy.py).

Creates a REAL top-level wx dialog, so it only runs in CI: see the wxgui marker
in pytest.ini. tests/test_settings_lazy.py holds what can be read without one.
"""

import copy

import pytest
import wx

from core.i18n import I18n
from core.sound_system import DEFAULT_PACK_ID
from ui.dialogs.settings_dialog import SettingsDialog
from ui.dialogs.settings_lazy import PAGE_UNITS, UNITS
from tests.conftest import destroy_now, hidden_frame
from tests.settings_dialog_frame import give_global_settings

pytestmark = pytest.mark.wxgui


class _FakeSoundSystem:
    def get_output_devices(self):
        return []

    def get_input_devices(self):
        return []

    def apply_output_device(self, name):
        return True

    def apply_effects_device(self, name):
        return True


@pytest.fixture
def make_dialog(wx_app):
    built = []

    def make(settings=None):
        frame = hidden_frame()
        frame.settings = settings if settings is not None else {}
        frame._on_auto_download_settings_changed = lambda old, new: None
        frame.app_name = "WinZapp"
        frame.i18n = I18n(frame)
        frame.i18n.get_language()
        frame.wpp_port = 6300
        frame.wpp_custom_api = False
        frame._sound_packs = {DEFAULT_PACK_ID: {"name": "Default", "path": ""}}
        frame._default_sound_pack = {"name": "Default", "path": ""}
        frame.set_global_hotkey = lambda vk, mod: None
        frame.save_settings = lambda: None
        give_global_settings(frame)
        frame.load_sounds = lambda: None
        frame.apply_language_changes = lambda: None
        frame.sound_system = _FakeSoundSystem()
        frame.refresh_sound_packs = lambda: None
        dialog = SettingsDialog(frame)
        built.append(dialog)
        return dialog

    yield make
    for dialog in built:
        destroy_now(dialog)


def test_opening_builds_the_general_page_and_nothing_else(make_dialog):
    assert make_dialog()._built_units == {"general"}


def test_a_page_is_built_when_it_is_shown(make_dialog):
    dialog = make_dialog()
    dialog._notebook.SetSelection(dialog._notebook.FindPage(dialog._calls_page))
    assert dialog._built_units == {"general", "calls"}
    assert dialog._calls_page.GetChildren()


def test_every_category_opens_and_nothing_fails(make_dialog):
    dialog = make_dialog()
    for index in range(dialog._notebook.GetPageCount()):
        dialog._notebook.SetSelection(index)
    assert {unit for _attribute, unit in PAGE_UNITS} <= dialog._built_units
    assert not getattr(dialog, "_failed_units", None)


def test_a_page_leaves_the_windows_of_its_buttons_unbuilt(make_dialog):
    dialog = make_dialog()
    dialog._notebook.SetSelection(dialog._notebook.FindPage(dialog._ui_page))
    assert "messages" not in dialog._built_units
    assert dialog._messages_btn.GetParent() is dialog._ui_page


def test_a_window_is_built_when_its_button_opens_it(make_dialog, monkeypatch):
    dialog = make_dialog()
    monkeypatch.setattr(dialog._speech_dialog, "ShowModal", lambda: wx.ID_OK)
    dialog._speech_dialog.open()
    assert "speech" in dialog._built_units
    assert dialog._speech_page.GetChildren()


def test_the_search_builds_everything_and_finds_an_option_in_a_window(make_dialog):
    dialog = make_dialog()
    label = dialog._notifications_check.GetLabelText()
    built = set(dialog._built_units)
    dialog._notebook.search_field.SetValue(label)
    assert dialog._built_units == built, "typing built nothing by itself"
    while dialog._notebook.search_loading:
        dialog._build_next_unit()
    assert set(UNITS) <= dialog._built_units
    assert dialog._notebook._found
    dialog._notebook.search_field.SetValue("")


def test_results_keep_their_place_as_pages_arrive(make_dialog):
    dialog = make_dialog()
    book = dialog._notebook
    book.search_field.SetValue("a")
    first = book._results.GetString(0)
    book._results.SetSelection(0)
    while book.search_loading:
        dialog._build_next_unit()
    assert book._results.GetSelection() == 0
    assert book._results.GetString(0) == first
    assert book._results.GetCount() == len(book._found)
    book.search_field.SetValue("")


def test_closing_during_a_search_stops_the_building(make_dialog):
    dialog = make_dialog()
    dialog._notebook.search_field.SetValue("a")
    dialog._stop_unit_loading()
    assert not dialog._unit_timer.IsRunning()


def test_opening_every_unit_changes_no_setting(make_dialog):
    """OK with everything shown and nothing touched."""
    dialog = make_dialog()
    dialog._ensure_all_units()
    dialog._apply_values()
    baseline = copy.deepcopy(dialog.main_window.settings)
    reopened = make_dialog(copy.deepcopy(baseline))
    reopened._ensure_all_units()
    assert reopened._apply_values() is True
    assert reopened.main_window.settings == baseline


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_one_page_opened_never_costs_the_others_a_setting(make_dialog, unit):
    dialog = make_dialog()
    dialog._ensure_all_units()
    dialog._apply_values()
    baseline = copy.deepcopy(dialog.main_window.settings)
    reopened = make_dialog(copy.deepcopy(baseline))
    reopened._ensure_unit(unit)
    assert reopened._apply_values() is True
    assert reopened.main_window.settings == baseline


def test_a_setting_nobody_opened_stays_exactly_as_it_was(make_dialog):
    """Whatever it holds, even what no control here would show."""
    settings = {"calls": {"alerts_enabled": False, "from_a_later_version": 7}}
    dialog = make_dialog(settings)
    assert dialog._apply_values() is True
    assert settings["calls"] == {"alerts_enabled": False, "from_a_later_version": 7}


def test_a_change_waits_through_opening_another_page(make_dialog):
    """Opening a page must not take a pending change for the original value."""
    dialog = make_dialog()
    dialog._updates_check.SetValue(not dialog._updates_check.GetValue())
    wanted_value = dialog._updates_check.GetValue()
    dialog._notebook.SetSelection(dialog._notebook.FindPage(dialog._conn_page))
    assert dialog._apply_values() is True
    assert dialog.main_window.settings["general"]["updates_enabled"] is wanted_value


def test_apply_relabels_what_was_built_and_later_pages_read_the_language(make_dialog):
    dialog = make_dialog()
    dialog._on_apply(None)
    dialog._notebook.SetSelection(dialog._notebook.FindPage(dialog._calls_page))
    assert dialog._call_alerts_check.GetLabel() == dialog.main_window.i18n.t("calls_alerts_enabled_label")
