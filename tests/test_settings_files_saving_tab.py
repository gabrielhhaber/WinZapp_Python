"""Configurações > Arquivos e salvamento.

Which folder a Save As dialog opens on — see core/save_location.py for why it
is a setting rather than a decision, and tests/test_save_location.py for the
resolution order itself. This file covers the tab: that it reads and writes the
settings it claims to, that the custom-folder controls are only live for the
mode that uses them, and that inserting a tab did not renumber the dialog out
from under the hardcoded indices.

Needs a real wx.App — see tests/test_settings_dialog_apply_button.py's
docstring for why this dialog cannot be exercised against a stub.
"""

import wx
import pytest

from core import save_location
from core.i18n import I18n
from core.sound_system import DEFAULT_PACK_ID
from ui.dialogs.settings_dialog import SettingsDialog


class _FakeSoundSystem:
    def get_output_devices(self):
        return []

    def get_input_devices(self):
        return []

    def apply_output_device(self, name):
        return True

    def apply_effects_device(self, name):
        return True


def _make_frame(settings):
    frame = wx.Frame(None)
    frame.settings = settings
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
    return frame


@pytest.fixture
def make_dialog(wx_app):
    created = []

    def _make(settings=None):
        dlg = SettingsDialog(_make_frame(settings if settings is not None else {}))
        created.append(dlg)
        return dlg

    yield _make
    for dlg in created:
        dlg.Destroy()


class TestTheTabIsWhereTheIndicesSayItIs:
    """Every SetPageText() in this dialog is a hardcoded index, and main.py
    opens the Connection tab by number too. Inserting a page silently shifts
    every tab below it, so the position is worth asserting rather than
    trusting."""

    def test_it_sits_right_after_storage(self, make_dialog):
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._files_page) == 9

    def test_the_tabs_below_it_moved_with_it(self, make_dialog):
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._audio_page) == 10
        assert dialog._notebook.FindPage(dialog._calls_page) == 11

    def test_the_tabs_that_are_opened_by_number_did_not_move(self, make_dialog):
        """main.py's custom-API first-run flow does SetSelection(4), and this
        file has SetSelection() calls up to 8. The new tab is below all of
        them, which is the whole reason it went here."""
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._conn_page) == 4
        assert dialog._notebook.FindPage(dialog._storage_page) == 8

    def test_every_page_has_a_translated_title(self, make_dialog):
        """SetPageText() is driven by index; an off-by-one shows up as a tab
        labelled with another tab's name, which nothing else would catch."""
        dialog = make_dialog()
        i18n = dialog.main_window.i18n
        assert dialog._notebook.GetPageText(9) == i18n.t("tab_files_saving")
        assert dialog._notebook.GetPageText(10) == i18n.t("tab_audio_playback")
        assert dialog._notebook.GetPageText(11) == i18n.t("tab_calls")


class TestLoadingTheCurrentSetting:
    def test_an_install_with_no_files_section_shows_the_default(self, make_dialog):
        dialog = make_dialog({})
        assert dialog._save_folder_radio.GetSelection() == \
            save_location.mode_index(save_location.DEFAULT_MODE)

    def test_the_stored_mode_selects_its_radio_button(self, make_dialog):
        dialog = make_dialog({"files": {"save_dialog_folder_mode": "downloads"}})
        assert dialog._save_folder_radio.GetSelection() == \
            save_location.mode_index(save_location.MODE_DOWNLOADS)

    def test_the_stored_custom_folder_fills_the_field(self, make_dialog):
        dialog = make_dialog({"files": {
            "save_dialog_folder_mode": "custom",
            "save_dialog_custom_folder": "C:/Documentos/WinZapp",
        }})
        assert dialog._save_folder_custom_field.GetValue() == "C:/Documentos/WinZapp"


class TestTheCustomControlsFollowTheMode:
    """Disabled rather than hidden: hiding reflows the tab on every radio
    change, and a control that appears and disappears is harder to follow
    under a screen reader than one that is consistently unavailable."""

    def test_they_are_disabled_for_the_default_mode(self, make_dialog):
        dialog = make_dialog()
        assert dialog._save_folder_custom_field.IsEnabled() is False
        assert dialog._save_folder_browse_btn.IsEnabled() is False
        assert dialog._save_folder_custom_label.IsEnabled() is False

    def test_they_are_enabled_for_custom_mode(self, make_dialog):
        dialog = make_dialog({"files": {"save_dialog_folder_mode": "custom"}})
        assert dialog._save_folder_custom_field.IsEnabled() is True
        assert dialog._save_folder_browse_btn.IsEnabled() is True

    def test_switching_the_radio_updates_them(self, make_dialog):
        dialog = make_dialog()
        dialog._save_folder_radio.SetSelection(
            save_location.mode_index(save_location.MODE_CUSTOM))
        dialog._sync_save_folder_controls()
        assert dialog._save_folder_custom_field.IsEnabled() is True

        dialog._save_folder_radio.SetSelection(
            save_location.mode_index(save_location.MODE_DOWNLOADS))
        dialog._sync_save_folder_controls()
        assert dialog._save_folder_custom_field.IsEnabled() is False


class TestApplying:
    def test_the_selected_mode_is_written(self, make_dialog):
        dialog = make_dialog({})
        dialog._save_folder_radio.SetSelection(
            save_location.mode_index(save_location.MODE_DOWNLOADS))
        assert dialog._apply_values() is True
        assert dialog.main_window.settings["files"]["save_dialog_folder_mode"] \
            == "downloads"

    def test_the_custom_folder_is_written_and_trimmed(self, make_dialog, tmp_path):
        dialog = make_dialog({})
        dialog._save_folder_radio.SetSelection(
            save_location.mode_index(save_location.MODE_CUSTOM))
        dialog._save_folder_custom_field.SetValue(f"  {tmp_path}  ")
        assert dialog._apply_values() is True
        assert dialog.main_window.settings["files"]["save_dialog_custom_folder"] \
            == str(tmp_path)

    def test_the_remembered_folder_is_not_touched(self, make_dialog):
        """It is owned by the save dialogs themselves. Writing it from here
        would discard the folder the user last actually saved to."""
        dialog = make_dialog({"files": {
            "save_dialog_folder_mode": "last",
            "save_dialog_last_folder": "C:/algum/lugar",
        }})
        assert dialog._apply_values() is True
        assert dialog.main_window.settings["files"]["save_dialog_last_folder"] \
            == "C:/algum/lugar"


class TestValidation:
    """Accepting a folder that does not exist would leave the user thinking the
    setting does not work: resolve_save_dialog_folder() falls back to Downloads
    and nothing says why."""

    def test_custom_mode_with_a_missing_folder_is_refused(self, make_dialog, tmp_path, monkeypatch):
        shown = []
        monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: shown.append(a))
        dialog = make_dialog({})
        dialog._save_folder_radio.SetSelection(
            save_location.mode_index(save_location.MODE_CUSTOM))
        dialog._save_folder_custom_field.SetValue(str(tmp_path / "nao_existe"))

        assert dialog._apply_values() is False
        assert shown, "the user must be told why"

    def test_custom_mode_with_an_empty_field_is_refused(self, make_dialog, monkeypatch):
        monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: None)
        dialog = make_dialog({})
        dialog._save_folder_radio.SetSelection(
            save_location.mode_index(save_location.MODE_CUSTOM))
        dialog._save_folder_custom_field.SetValue("")
        assert dialog._apply_values() is False

    def test_the_refusal_brings_the_user_to_the_offending_field(self, make_dialog, monkeypatch):
        monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: None)
        dialog = make_dialog({})
        dialog._save_folder_radio.SetSelection(
            save_location.mode_index(save_location.MODE_CUSTOM))
        dialog._save_folder_custom_field.SetValue("")
        dialog._apply_values()
        assert dialog._notebook.GetSelection() == \
            dialog._notebook.FindPage(dialog._files_page)

    def test_a_missing_folder_is_ignored_when_that_mode_is_not_selected(
            self, make_dialog, tmp_path, monkeypatch):
        """A stale custom path left over from an earlier choice must not block
        saving unrelated settings."""
        monkeypatch.setattr(wx, "MessageBox", lambda *a, **k: None)
        dialog = make_dialog({})
        dialog._save_folder_radio.SetSelection(
            save_location.mode_index(save_location.MODE_REMEMBER_LAST))
        dialog._save_folder_custom_field.SetValue(str(tmp_path / "nao_existe"))
        assert dialog._apply_values() is True
