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

    def test_files_and_saving_live_in_the_storage_page(self, make_dialog):
        dialog = make_dialog()
        assert dialog._files_page is dialog._storage_page
        assert dialog._notebook.FindPage(dialog._storage_page) == 5

    def test_alert_tones_live_in_the_event_sounds_page(self, make_dialog):
        dialog = make_dialog()
        assert dialog._alert_page is dialog._sound_events_page
        assert dialog._notebook.FindPage(dialog._sound_events_page) == 4

    def test_the_audio_page_holds_the_devices_and_silencing(self, make_dialog):
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._audio_devices_page) == 3
        assert dialog._silence_while_recording_check.GetParent() is dialog._audio_devices_page

    def test_spoken_content_and_playback_are_not_pages(self, make_dialog):
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._speech_page) == -1
        assert dialog._notebook.FindPage(dialog._audio_page) == -1

    def test_the_calls_tab_follows_storage(self, make_dialog):
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._calls_page) == 6

    def test_profile_backup_and_shortcuts_are_buttons_not_pages(self, make_dialog):
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._profile_backup_page) == -1
        assert dialog._notebook.FindPage(dialog._shortcuts_page) == -1
        assert dialog._profile_backup_btn.GetParent() is dialog._storage_page
        assert dialog._shortcuts_btn.GetParent() is dialog._ui_page
        assert dialog._confirmations_btn.GetParent() is dialog._ui_page

    def test_the_audio_options_moved_to_the_audio_page(self, make_dialog):
        dialog = make_dialog()
        assert dialog._pasted_audio_radio.GetParent() is dialog._audio_devices_page
        assert dialog._auto_focus_next_audio_cb.GetParent() is dialog._audio_page

    def test_reactions_are_a_button_between_messages_and_chats(self, make_dialog):
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._reactions_page) == -1
        assert dialog._reactions_btn.GetParent() is dialog._ui_page
        order = [c for c in dialog._ui_page.GetChildren()
                 if c in (dialog._messages_btn, dialog._reactions_btn, dialog._chats_btn)]
        assert order == [dialog._messages_btn, dialog._reactions_btn, dialog._chats_btn]

    def test_locked_chats_are_a_button_in_the_conversations_window(self, make_dialog):
        dialog = make_dialog()
        assert dialog._notebook.FindPage(dialog._chat_lock_page) == -1
        assert dialog._chat_lock_btn.GetParent() is dialog._chats_page
        assert dialog._chat_lock_btn.IsShown()
        assert dialog._notebook.FindPage(dialog._ai_page) == 7
        assert dialog._notebook.FindPage(dialog._transcription_page) == 8
        assert dialog._notebook.GetPageCount() == 11

    def test_a_hidden_vault_leaves_the_locked_chats_button_out(self, make_dialog):
        """A vault the user chose to hide must not be advertised by Settings."""
        vault = ChatLockVault(Fernet.generate_key())
        vault.configure("246810", "gizli-kod")
        vault.set_hide_navigation(True)
        dialog = make_dialog(vault=vault)
        assert not dialog._chat_lock_btn.IsShown()
        assert dialog._notebook.GetPageCount() == 11

    def test_connection_is_the_last_page(self, make_dialog):
        dialog = make_dialog()
        assert dialog._transcription_page_seen is False
        last = dialog._notebook.GetPageCount() - 1
        assert last == 10
        assert dialog._notebook.FindPage(dialog._conn_page) == last
        assert dialog._notebook.FindPage(dialog._whatsapp_page) == last - 1
        assert dialog._notebook.FindPage(dialog._transcription_page) == last - 2

    def test_whatsapp_settings_are_two_buttons_in_one_category(self, make_dialog):
        dialog = make_dialog()
        assert dialog._whatsapp_privacy_btn.GetParent() is dialog._whatsapp_page
        assert dialog._whatsapp_profile_btn.GetParent() is dialog._whatsapp_page
        # nothing is read from WhatsApp until a window is opened
        assert dialog._whatsapp_privacy_dialog._first_open is not None
        assert dialog._whatsapp_profile_dialog._first_open is not None

    def test_every_page_has_a_translated_title(self, make_dialog):
        """SetPageText() is driven by index; an off-by-one shows up as a tab
        labelled with another tab's name, which nothing else would catch."""
        dialog = make_dialog()
        i18n = dialog.main_window.i18n
        titles = ["tab_general", "tab_ui", "tab_accessibility",
                  "tab_audio", "tab_event_sounds", "tab_storage", "tab_calls",
                  "tab_ai_accessibility",
                  "tab_transcription", "wa_settings_dialog_title", "tab_connection"]
        for index, key in enumerate(titles):
            assert dialog._notebook.GetPageText(index) == i18n.t(key)
        dialog._refresh_dialog_labels()
        for index, key in enumerate(titles):
            assert dialog._notebook.GetPageText(index) == i18n.t(key)


class TestRegroupedSettings:
    def test_the_interface_options_live_behind_buttons(self, make_dialog):
        dialog = make_dialog()
        for button in (dialog._messages_btn, dialog._chats_btn,
                       dialog._confirmations_btn, dialog._shortcuts_btn):
            assert button.GetParent() is dialog._ui_page
        assert dialog._messages_page_size_field.GetParent() is dialog._messages_page
        assert dialog._focus_message_field_rb.GetParent() is dialog._chats_page
        assert dialog._bulk_action_shortcuts_cb.GetParent() is dialog._chats_page
        assert dialog._forwarded_prefix_cb.GetParent() is dialog._messages_page

    def test_spoken_content_needs_extended_compatibility(self, make_dialog):
        dialog = make_dialog({"accessibility": {"extended_sr_compat_enabled": False}})
        assert not dialog._spoken_content_btn.IsEnabled()
        dialog._extended_sr_compat_check.SetValue(True)
        dialog._on_extended_sr_compat_toggle(wx.CommandEvent(wx.EVT_CHECKBOX.typeId))
        assert dialog._spoken_content_btn.IsEnabled()

    def test_the_ai_options_hide_while_the_ai_is_off(self, make_dialog):
        dialog = make_dialog()
        page = dialog._ai_page
        page.enabled.SetValue(False)
        page._show_options(False)
        others = [c for c in page.GetChildren() if c is not page.enabled]
        assert others and not any(c.IsShown() for c in others)
        page._show_options(True)
        assert all(c.IsShown() for c in others)

    def test_the_search_reaches_options_inside_the_buttons(self, make_dialog):
        dialog = make_dialog()
        nb = dialog._notebook
        nb._rebuild_index()
        labels = {(e.category, e.label) for e in nb._index}
        assert any("tab_speech" not in c and e for c, e in labels)
        assert any(c.startswith(nb.GetPageText(2) + ": ") for c, _ in labels)
        assert any(c.startswith(nb.GetPageText(3) + ": ") for c, _ in labels)


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
