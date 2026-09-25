"""Window-free checks for the locked-chat section in Settings."""

import inspect

from core.chat_lock_vault import ChatLockVault
from cryptography.fernet import Fernet
from ui.chat_lock import LockedConversationsPanel
from ui.dialogs.settings_dialog import SettingsDialog


class _I18n:
    @staticmethod
    def t(key):
        return key


class _Control:
    def __init__(self):
        self.value = False
        self.enabled = True
        self.label = ""

    def SetValue(self, value):
        self.value = value

    def GetValue(self):
        return self.value

    def Enable(self, enabled=True):
        self.enabled = enabled

    def SetLabel(self, label):
        self.label = label


class _Choice(_Control):
    def __init__(self):
        super().__init__()
        self.selection = -1

    def SetSelection(self, selection):
        self.selection = selection

    def GetSelection(self):
        return self.selection


class _MainWindow:
    def __init__(self, vault, *, unlocked):
        self.i18n = _I18n()
        self._chat_lock_vault = vault
        self._chat_lock_unlocked = unlocked
        self.navigation_changes = []
        self.timeout_changes = []

    def set_chat_lock_navigation_hidden(self, hidden):
        self.navigation_changes.append(hidden)

    def set_chat_lock_timeout_minutes(self, minutes):
        self.timeout_changes.append(minutes)


class _SettingsStub:
    _load_chat_lock_values = SettingsDialog._load_chat_lock_values
    _apply_chat_lock_values = SettingsDialog._apply_chat_lock_values
    _selected_chat_lock_timeout_minutes = (
        SettingsDialog._selected_chat_lock_timeout_minutes
    )
    _set_chat_lock_timeout_minutes = SettingsDialog._set_chat_lock_timeout_minutes

    def __init__(self, main_window):
        self.main_window = main_window
        self._chat_lock_show_navigation_check = _Control()
        self._chat_lock_timeout_choice = _Choice()
        self._chat_lock_change_pin_btn = _Control()
        self._chat_lock_change_reveal_btn = _Control()
        self._chat_lock_unlock_btn = _Control()


def _configured_vault(*, hide_navigation=False, timeout=30):
    vault = ChatLockVault(Fernet.generate_key())
    vault.configure("246810", "gizli-kod")
    vault.set_hide_navigation(hide_navigation)
    vault.set_auto_lock_minutes(timeout)
    return vault


def test_locked_settings_do_not_expose_persisted_privacy_values():
    vault = _configured_vault(hide_navigation=False, timeout=60)
    dialog = _SettingsStub(_MainWindow(vault, unlocked=False))

    dialog._load_chat_lock_values()

    assert not dialog._chat_lock_show_navigation_check.GetValue()
    assert dialog._chat_lock_timeout_choice.GetSelection() == 0
    assert not dialog._chat_lock_show_navigation_check.enabled
    assert not dialog._chat_lock_timeout_choice.enabled
    assert dialog._chat_lock_unlock_btn.enabled
    assert dialog._chat_lock_unlock_btn.label == "chat_lock_settings_unlock"


def test_unlocked_settings_load_the_real_vault_policy():
    vault = _configured_vault(hide_navigation=False, timeout=60)
    dialog = _SettingsStub(_MainWindow(vault, unlocked=True))

    dialog._load_chat_lock_values()

    assert dialog._chat_lock_show_navigation_check.GetValue()
    assert dialog._selected_chat_lock_timeout_minutes() == 60
    assert dialog._chat_lock_show_navigation_check.enabled
    assert dialog._chat_lock_timeout_choice.enabled
    assert not dialog._chat_lock_unlock_btn.enabled
    assert dialog._chat_lock_unlock_btn.label == "chat_lock_settings_ready"


def test_apply_routes_vault_policy_through_main_window():
    vault = _configured_vault(hide_navigation=True, timeout=5)
    main_window = _MainWindow(vault, unlocked=True)
    dialog = _SettingsStub(main_window)
    dialog._load_chat_lock_values()
    dialog._chat_lock_show_navigation_check.SetValue(True)
    dialog._set_chat_lock_timeout_minutes(30)

    dialog._apply_chat_lock_values()

    assert main_window.navigation_changes == [False]
    assert main_window.timeout_changes == [30]


def test_locked_chats_panel_contains_content_actions_not_rare_settings():
    source = inspect.getsource(LockedConversationsPanel._init_ui)

    assert "chat_lock_close" in source
    assert "chat_lock_timeout_label" not in source
    assert "chat_lock_change_pin" not in source
    assert "chat_lock_settings" not in source
    assert "chat_lock_show_navigation" not in source


# ── The tab must not advertise a hidden vault ───────────────────────────────

from ui.dialogs.settings_dialog import chat_lock_tab_visible


def _vault(*, configure=True, hide=True):
    vault = ChatLockVault(Fernet.generate_key())
    if configure:
        vault.configure("246810", "gizli-kod")
        vault.set_hide_navigation(hide)
    return vault


def test_tab_is_visible_while_no_vault_was_set_up():
    assert chat_lock_tab_visible(_MainWindow(_vault(configure=False), unlocked=False))


def test_tab_is_visible_when_the_vault_is_not_hidden():
    assert chat_lock_tab_visible(_MainWindow(_vault(hide=False), unlocked=False))


def test_tab_is_absent_for_a_hidden_locked_vault():
    assert not chat_lock_tab_visible(_MainWindow(_vault(hide=True), unlocked=False))


def test_tab_stays_reachable_once_a_hidden_vault_was_revealed():
    assert chat_lock_tab_visible(_MainWindow(_vault(hide=True), unlocked=True))


def test_tab_is_absent_when_the_vault_state_is_unreadable():
    assert not chat_lock_tab_visible(_MainWindow(None, unlocked=False))


def test_open_settings_relocks_the_vault_when_it_closes():
    import inspect
    from main import MainWindow
    source = inspect.getsource(MainWindow.open_settings)
    assert source.index("dlg.Destroy()") < source.rindex("lock_chat_vault(")
