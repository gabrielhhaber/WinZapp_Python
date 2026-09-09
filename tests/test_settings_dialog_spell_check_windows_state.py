"""Tests for the spell-check checkbox's initial value in the Settings
dialog — see SettingsDialog._apply_spell_check_windows_state().

Windows' own spelling setting (Settings > Time & language > Typing >
Spelling), when it can be read at all, decides whether spell checking runs
(tests/test_spell_check_setting.py covers that half, in ConversationsPanel).
This file covers the Settings > Geral checkbox itself: it stays an
ordinary, always-editable checkbox — no extra label, nothing disabled —
whose value simply mirrors Windows' setting each time the dialog opens,
falling back to the stored WinZapp preference when Windows' setting cannot
be read.

Needs a real wx.App — same reasoning and fixture pattern as
tests/test_settings_dialog_apply_button.py.
"""

import pytest

from core.i18n import I18n
from core.sound_system import DEFAULT_PACK_ID
import ui.dialogs.settings_dialog as settings_dialog_module
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


def _make_dialog(wx_app, spell_check_enabled=True):
    frame = hidden_frame()
    frame.settings = {"general": {"spell_check_enabled": spell_check_enabled}}
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

    dlg = SettingsDialog(frame)
    return dlg


class TestTheCheckboxStaysOrdinary:
    """No matter what Windows' setting reads as, this is still just a
    normal checkbox: always shown with its one usual label, always
    editable, always saved back on OK/Apply like every other control on
    this tab."""

    def test_always_editable_regardless_of_windows(self, wx_app, monkeypatch):
        for windows_value in (True, False, None):
            monkeypatch.setattr(
                settings_dialog_module, "is_windows_spellcheck_enabled",
                lambda v=windows_value: v,
            )
            dlg = _make_dialog(wx_app)
            try:
                assert dlg._spell_check_check.IsEnabled() is True
                assert dlg._spell_check_check.GetLabel() == dlg.main_window.i18n.t(
                    "spell_check_enabled_label"
                )
            finally:
                dlg.Destroy()


class TestTheValueMirrorsWindowsWhenReadable:
    def test_seeded_on_when_windows_says_on(self, wx_app, monkeypatch):
        monkeypatch.setattr(
            settings_dialog_module, "is_windows_spellcheck_enabled", lambda: True
        )
        dlg = _make_dialog(wx_app, spell_check_enabled=False)
        try:
            assert dlg._spell_check_check.GetValue() is True
        finally:
            dlg.Destroy()

    def test_seeded_off_when_windows_says_off(self, wx_app, monkeypatch):
        monkeypatch.setattr(
            settings_dialog_module, "is_windows_spellcheck_enabled", lambda: False
        )
        dlg = _make_dialog(wx_app, spell_check_enabled=True)
        try:
            assert dlg._spell_check_check.GetValue() is False
        finally:
            dlg.Destroy()

    def test_falls_back_to_the_stored_preference_when_unreadable(self, wx_app, monkeypatch):
        monkeypatch.setattr(
            settings_dialog_module, "is_windows_spellcheck_enabled", lambda: None
        )
        dlg = _make_dialog(wx_app, spell_check_enabled=False)
        try:
            assert dlg._spell_check_check.GetValue() is False
        finally:
            dlg.Destroy()


class TestSavingAlwaysPersistsTheCheckbox:
    """Unconditional, like every other checkbox on this tab — the Windows
    reading only ever decides what the checkbox starts out showing."""

    def test_a_user_edit_is_saved_even_though_windows_disagrees(self, wx_app, monkeypatch):
        monkeypatch.setattr(
            settings_dialog_module, "is_windows_spellcheck_enabled", lambda: False
        )
        dlg = _make_dialog(wx_app, spell_check_enabled=True)
        try:
            dlg._spell_check_check.SetValue(True)
            dlg._on_apply(None)
            assert dlg.main_window.settings["general"]["spell_check_enabled"] is True
        finally:
            dlg.Destroy()
