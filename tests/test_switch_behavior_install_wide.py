"""Configurações > Geral > "when switching accounts" goes through the shared file.

`switch_behavior` is install-wide (`app_settings._GENERAL_GLOBAL`). The dialog
read and wrote it through `self.main_window.app_settings` — an attribute
nothing ever sets; the window's is `_app_settings`, written by
`MainWindow._apply_global_settings()`. So the shared file was never read or
written from the dialog. It showed the copy `_apply_global_settings()` left in
`settings["general"]` at startup, and OK wrote that copy back: a change made
in another account since was shown as the old value, and then undone.

The stubs below carry the attribute under the name production uses. A stub
with the other spelling is what hid the same mistake on the transcription tab
(tests/test_transcription_settings_tab.py::TestTheModelsFolder and
::TestTheInstallWideFolderReachesTheAttributeMainWindowActuallyHas).
"""

import json
import threading

import pytest

from app_settings import AppSettings
from main import MainWindow
from main_window import settings as settings_module
from ui.dialogs.settings_dialog import SettingsDialog


@pytest.fixture(autouse=True)
def _call_after_inline(monkeypatch):
    monkeypatch.setattr(settings_module.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


class _Radio:
    def __init__(self, value=False):
        self.value = value

    def SetValue(self, value):
        self.value = value

    def GetValue(self):
        return self.value


class _MainWindow:
    def __init__(self, general, app_settings=None, global_dir=None):
        self.settings = {"general": dict(general)}
        # `_app_settings`, with the underscore: the only name MainWindow writes.
        self._app_settings = app_settings
        self.global_dir = global_dir
        self._save_lock = threading.Lock()
        self.tray_icon = None

    _apply_global_settings = MainWindow._apply_global_settings
    _share_keyboard_shortcuts = MainWindow._share_keyboard_shortcuts
    _persist_global_settings = MainWindow._persist_global_settings
    choose_global_settings = MainWindow.choose_global_settings
    _apply_pulled_global_settings = MainWindow._apply_pulled_global_settings
    _sync_tray_icon_with_setting = MainWindow._sync_tray_icon_with_setting

    def _init_tray(self):
        pass


class _Dialog:
    """SettingsDialog carrying only the two radio buttons and the window."""

    def __init__(self, main_window):
        self.main_window = main_window
        self._switch_behavior_single_rb = _Radio()
        self._switch_behavior_keep_open_rb = _Radio()

    _install_wide_settings = SettingsDialog._install_wide_settings
    _load_switch_behavior = SettingsDialog._load_switch_behavior
    _apply_switch_behavior = SettingsDialog._apply_switch_behavior
    _global_control_changed = SettingsDialog._global_control_changed


def _apply(dialog):
    """What OK does with the radio: collect the choice, then store it."""
    choices = {}
    dialog._apply_switch_behavior(choices)
    dialog.main_window.choose_global_settings(choices)


def _stored(global_dir):
    with open(AppSettings(str(global_dir))._path, encoding="utf-8") as f:
        return json.load(f).get("switch_behavior")


class TestLoading:
    def test_the_shared_file_wins_over_the_startup_copy(self, tmp_path):
        """Another account chose "keep both open" after this one started."""
        app = AppSettings(str(tmp_path))
        app.set("switch_behavior", "keep_open")
        dialog = _Dialog(_MainWindow({"switch_behavior": "single"}, app))

        dialog._load_switch_behavior()

        assert dialog._switch_behavior_keep_open_rb.GetValue() is True
        assert dialog._switch_behavior_single_rb.GetValue() is False

    def test_a_legacy_install_still_reads_its_own_settings(self):
        """No global_dir: _apply_global_settings() returns early and the
        attribute is never set, so settings["general"] is all there is."""
        dialog = _Dialog(_MainWindow({"switch_behavior": "keep_open"}))
        del dialog.main_window._app_settings

        dialog._load_switch_behavior()

        assert dialog._switch_behavior_keep_open_rb.GetValue() is True

    def test_a_value_that_only_lived_in_the_account_is_not_lost(self, tmp_path):
        """What a current user sees does not change: an account whose choice
        predates the shared file has it seeded there at startup, before the
        dialog can read the file."""
        window = _MainWindow({"switch_behavior": "keep_open"}, global_dir=str(tmp_path))
        window._apply_global_settings()
        dialog = _Dialog(window)

        dialog._load_switch_behavior()

        assert dialog._switch_behavior_keep_open_rb.GetValue() is True


class TestSaving:
    def test_the_choice_reaches_the_shared_file(self, tmp_path):
        app = AppSettings(str(tmp_path))
        window = _MainWindow({"switch_behavior": "single"}, app)
        dialog = _Dialog(window)
        dialog._switch_behavior_keep_open_rb.SetValue(True)

        _apply(dialog)

        assert _stored(tmp_path) == "keep_open"
        # And the account's copy agrees: it is what a legacy install reads,
        # and what _persist_global_settings() reconciles on the next save.
        assert window.settings["general"]["switch_behavior"] == "keep_open"

    def test_a_stale_copy_is_not_written_back_over_another_accounts_change(self, tmp_path):
        """Open, change nothing, OK: the shared value stands."""
        app = AppSettings(str(tmp_path))
        window = _MainWindow({"switch_behavior": "single"}, app)
        app.set("switch_behavior", "keep_open")  # another account, since startup
        dialog = _Dialog(window)

        dialog._load_switch_behavior()
        _apply(dialog)

        assert _stored(tmp_path) == "keep_open"
        assert window.settings["general"]["switch_behavior"] == "keep_open"

    def test_a_legacy_install_saves_to_its_own_settings(self):
        window = _MainWindow({"switch_behavior": "single"})
        del window._app_settings
        dialog = _Dialog(window)
        dialog._switch_behavior_keep_open_rb.SetValue(True)

        _apply(dialog)

        assert window.settings["general"]["switch_behavior"] == "keep_open"
