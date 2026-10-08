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
from cryptography.fernet import Fernet
from core.chat_lock_vault import ChatLockVault
import pytest

from core import save_location
from core.i18n import I18n
from core.sound_system import DEFAULT_PACK_ID
from ui.dialogs.settings_dialog import SettingsDialog
from tests.conftest import destroy_now, hidden_frame
from tests.settings_dialog_frame import give_global_settings

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


def _make_frame(settings, vault=None):
    frame = hidden_frame()
    # SettingsDialog only adds the "Locked chats" tab when a vault object
    # exists and is not hidden (chat_lock_tab_visible); a never-set-up vault
    # keeps it visible, which is what these index assertions rely on.
    frame._chat_lock_vault = vault if vault is not None else ChatLockVault(Fernet.generate_key())
    frame._chat_lock_unlocked = False
    frame.settings = settings
    # _apply_values() reports storage changes to the main window; the stub
    # has no sweep to start.
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
    return frame


@pytest.fixture
def make_dialog(wx_app):
    created = []

    def _make(settings=None, vault=None):
        dlg = SettingsDialog(_make_frame(settings if settings is not None else {}, vault))
        created.append(dlg)
        return dlg

    yield _make
    for dlg in created:
        destroy_now(dlg)


class TestTheTabIsWhereTheIndicesSayItIs:
    """Every SetPageText() in this dialog is a hardcoded index, and
    main_window/settings.py opens the Connection tab by number too. Inserting
    a page silently shifts every tab below it, so the position is worth
    asserting rather than trusting."""

    def test_it_sits_right_after_storage(self, make_dialog):
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._files_page) == 9

    def test_the_tabs_below_it_moved_with_it(self, make_dialog):
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._audio_page) == 10
        assert dialog._notebook.FindPage(dialog._calls_page) == 11

    def test_the_profile_backup_tab_is_appended_last(self, make_dialog):
        """Added after Calls so no earlier index moved; SetPageText(12) in
        _refresh_dialog_labels() relies on it being the thirteenth page."""
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._profile_backup_page) == 12

    def test_the_reactions_tab_is_appended_after_profile_backup(self, make_dialog):
        """Appended for the same reason; SetPageText(13) relies on it."""
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._reactions_page) == 13

    def test_the_locked_chats_tab_is_appended_after_reactions(self, make_dialog):
        """Rare vault policy stays after every fixed tab and keeps index 14;
        SetPageText(14) relies on it. AI, Transcription and Shortcuts come
        after it, in that order, and are retranslated through FindPage()
        rather than by number."""
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._chat_lock_page) == 14
        assert dialog._notebook.FindPage(dialog._ai_page) == 15
        assert dialog._notebook.FindPage(dialog._transcription_page) == 16
        assert dialog._notebook.FindPage(dialog._shortcuts_page) == 17
        assert dialog._notebook.GetPageCount() == 18

    def test_a_hidden_vault_leaves_the_locked_chats_tab_out(self, make_dialog):
        """A vault the user chose to hide must not be advertised by Settings."""
        vault = ChatLockVault(Fernet.generate_key())
        vault.configure("246810", "gizli-kod")
        vault.set_hide_navigation(True)
        dialog = make_dialog(vault=vault)
        assert dialog._notebook.FindPage(dialog._chat_lock_page) == -1
        assert dialog._notebook.FindPage(dialog._ai_page) == 14
        assert dialog._notebook.GetPageText(14) == dialog.main_window.i18n.t("tab_ai_accessibility")
        assert dialog._notebook.GetPageCount() == 17
        # AI, Transcription and Shortcuts move up to fill the gap.
        assert dialog._notebook.FindPage(dialog._transcription_page) == 15
        assert dialog._notebook.GetPageText(15) == dialog.main_window.i18n.t("tab_transcription")
        assert dialog._notebook.FindPage(dialog._shortcuts_page) == 16
        assert dialog._notebook.GetPageText(16) == dialog.main_window.i18n.t("tab_shortcuts")

    def test_the_tabs_that_are_opened_by_number_did_not_move(self, make_dialog):
        """main_window/settings.py's custom-API first-run flow does
        SetSelection(4), and this file has SetSelection() calls up to 8. The
        new tab is below all of them, which is the whole reason it went here."""
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._conn_page) == 4
        assert dialog._notebook.FindPage(dialog._storage_page) == 8

    def test_shortcuts_are_appended_after_transcription(self, make_dialog):
        """Asserted against the real notebook rather than by reading the source
        for AddPage() calls, which is all a suite that may not construct this
        dialog can do (tests/test_transcription_settings_tab.py). Appending is
        the one position that renumbers nothing — this is where that stops
        being an argument and becomes a measurement."""
        dialog = make_dialog()
        # Constructing the dialog adds every page, and AddPage() fires
        # EVT_NOTEBOOK_PAGE_CHANGED. The handler is bound after _load_values()
        # precisely so that does not count as the user visiting the tab — which
        # would speak and consume a substitution warning nobody heard. A source
        # grep cannot tell a Bind placed before _load_values() from one after;
        # the real notebook can.
        assert dialog._transcription_page_seen is False
        # Shortcuts append after Transcription without renumbering it.
        last = dialog._notebook.GetPageCount() - 1
        assert last == 17
        assert dialog._notebook.FindPage(dialog._transcription_page) == last - 1
        assert dialog._notebook.FindPage(dialog._shortcuts_page) == last
        assert dialog._notebook.GetPageText(last) == dialog.main_window.i18n.t(
            "tab_shortcuts"
        )

    def test_every_page_has_a_translated_title(self, make_dialog):
        """SetPageText() is driven by index; an off-by-one shows up as a tab
        labelled with another tab's name, which nothing else would catch."""
        dialog = make_dialog()
        i18n = dialog.main_window.i18n
        assert dialog._notebook.GetPageText(9) == i18n.t("tab_files_saving")
        assert dialog._notebook.GetPageText(10) == i18n.t("tab_audio_playback")
        assert dialog._notebook.GetPageText(11) == i18n.t("tab_calls")
        assert dialog._notebook.GetPageText(12) == i18n.t("tab_profile_backup")
        assert dialog._notebook.GetPageText(13) == i18n.t("tab_reactions")
        assert dialog._notebook.GetPageText(14) == i18n.t("locked_chats")
        assert dialog._notebook.GetPageText(15) == i18n.t("tab_ai_accessibility")
        assert dialog._notebook.GetPageText(16) == i18n.t("tab_transcription")


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
