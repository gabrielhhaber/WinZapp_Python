"""Tests for the spell-check control in Settings > Geral — see
SettingsDialog._apply_spell_check_mode().

Windows has a spelling setting of its own (Settings > Time & language >
Typing > Spelling), and following it is the default; tests/
test_spell_check_setting.py covers what that resolves to at typing time. This
file covers the control itself, and the property that made it a radio group
rather than a checkbox: it shows the user's own stored *choice*, so
"following Windows" is visibly distinct from "on" and from "off". A checkbox
seeded from the registry could not express that difference — it would report
"on" for a user who never chose anything, and a user who unticked it would
watch it come back ticked, which on a screen reader is a control lying about
itself.

Needs a real wx.App — same reasoning and fixture pattern as
tests/test_settings_dialog_apply_button.py.
"""

import pytest

from core.i18n import I18n
from core.sound_system import DEFAULT_PACK_ID
from core.spell_checker import SPELL_CHECK_MODES
from ui.dialogs.settings_dialog import SettingsDialog
from tests.conftest import hidden_frame

# Creates a REAL top-level wx dialog - see the wxgui marker in pytest.ini.
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


def _make_dialog(wx_app, general=None):
    frame = hidden_frame()
    frame.settings = {"general": dict(general or {})}
    frame.app_name = "WinZapp"
    frame.i18n = I18n(frame)
    frame.i18n.get_language()
    frame.wpp_port = 6300
    frame.wpp_custom_api = False
    frame._sound_packs = {DEFAULT_PACK_ID: {"name": "Default", "path": ""}}
    frame._default_sound_pack = {"name": "Default", "path": ""}
    frame.set_global_hotkey = lambda vk, mod: None
    frame.save_settings = lambda: None
    frame.load_sounds = lambda: None
    frame.apply_language_changes = lambda: None
    frame.sound_system = _FakeSoundSystem()
    frame.refresh_sound_packs = lambda: None

    return SettingsDialog(frame)


class TestTheControlOffersAllThreeModes:
    def test_one_option_per_mode_each_with_a_real_label(self, wx_app):
        dlg = _make_dialog(wx_app)
        try:
            radio = dlg._spell_check_radio
            assert radio.GetCount() == len(SPELL_CHECK_MODES)
            i18n = dlg.main_window.i18n
            for index, mode in enumerate(SPELL_CHECK_MODES):
                key = f"spell_check_mode_{mode}"
                # I18n.t() returns the key itself when it is missing, which
                # is exactly what a user would hear read out loud.
                assert radio.GetString(index) == i18n.t(key) != key
            assert radio.GetLabel() == i18n.t("spell_check_label")
        finally:
            dlg.Destroy()

    def test_it_is_enabled(self, wx_app):
        """Nothing about Windows' setting disables this — following Windows
        is one of the choices, not a reason to take the choice away."""
        dlg = _make_dialog(wx_app)
        try:
            assert dlg._spell_check_radio.IsEnabled() is True
        finally:
            dlg.Destroy()


class TestItShowsTheStoredChoice:
    def test_each_stored_mode_selects_its_own_option(self, wx_app):
        for index, mode in enumerate(SPELL_CHECK_MODES):
            dlg = _make_dialog(wx_app, {"spell_check_mode": mode})
            try:
                assert dlg._spell_check_radio.GetSelection() == index, mode
            finally:
                dlg.Destroy()

    def test_nothing_stored_selects_follow_windows(self, wx_app):
        dlg = _make_dialog(wx_app)
        try:
            assert dlg._spell_check_radio.GetSelection() == SPELL_CHECK_MODES.index(
                "windows"
            )
        finally:
            dlg.Destroy()

    def test_a_legacy_disabled_install_selects_off(self, wx_app):
        """The migration has to be visible in the dialog too, or a user who
        turned checking off before this change would open Settings and be
        told it is following Windows."""
        dlg = _make_dialog(wx_app, {"spell_check_enabled": False})
        try:
            assert dlg._spell_check_radio.GetSelection() == SPELL_CHECK_MODES.index(
                "off"
            )
        finally:
            dlg.Destroy()


class TestSavingPersistsTheChoice:
    def test_each_selection_is_written_back_as_its_mode(self, wx_app):
        for index, mode in enumerate(SPELL_CHECK_MODES):
            dlg = _make_dialog(wx_app)
            try:
                dlg._spell_check_radio.SetSelection(index)
                dlg._on_apply(None)
                assert dlg.main_window.settings["general"]["spell_check_mode"] == mode
            finally:
                dlg.Destroy()

    def test_an_explicit_override_survives_being_reopened(self, wx_app):
        """The bug this control replaced: the stored value was overwritten
        from Windows every time the dialog opened, so an override could never
        be kept."""
        dlg = _make_dialog(wx_app)
        try:
            dlg._spell_check_radio.SetSelection(SPELL_CHECK_MODES.index("on"))
            dlg._on_apply(None)
            saved = dict(dlg.main_window.settings["general"])
        finally:
            dlg.Destroy()

        reopened = _make_dialog(wx_app, saved)
        try:
            assert reopened._spell_check_radio.GetSelection() == SPELL_CHECK_MODES.index(
                "on"
            )
        finally:
            reopened.Destroy()
