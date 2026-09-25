"""Tests for ShortcutsDialog._build_text() — the F1 help content assembly.

Two things are worth pinning here:
- Alt+<letter> to focus the main navigation tracks whichever letter the
  active locale marked with "&" in "main_nav" (see
  MainWindow.create_accelerator_table's own nav_letter extraction), instead
  of hardcoding "N" — English marks "M" ("&Main navigation").
- Ctrl+Alt+1..9 (switch to a paired account) only means anything when this
  process is running under the multi-account system, so it must not show up
  in the help text for a single-account install.

_build_text() is a staticmethod that only touches i18n and (optionally) a
couple of MainWindow attributes, so it is exercised directly against a
plain stub — no wx.App needed.
"""

from ui.dialogs.shortcuts_dialog import ShortcutsDialog


class _FakeI18n:
    def __init__(self, main_nav="&Navegação principal"):
        self._main_nav = main_nav

    def t(self, key):
        if key == "main_nav":
            return self._main_nav
        return key  # every other key is asserted by name/format result


class _FakeMainWindowSingleAccount:
    account_id = None
    registry = None


class _FakeMainWindowMultiAccount:
    account_id = "acc1"
    registry = object()


class TestNavLetterExtraction:
    """_build_text() computes nav_letter internally; verify it against a
    real-looking template (one that actually has a {letter} placeholder,
    like every language file's shortcut_alt_nav_label) rather than the bare
    key-echo fake used above."""

    class _TemplateI18n(_FakeI18n):
        def t(self, key):
            if key == "shortcut_alt_nav_label":
                return "Alt+{letter}: focus the main navigation"
            return super().t(key)

    def test_portuguese_mnemonic_resolves_to_n(self):
        text = ShortcutsDialog._build_text(self._TemplateI18n("&Navegação principal"))
        assert "Alt+N: focus the main navigation" in text

    def test_english_mnemonic_resolves_to_m(self):
        text = ShortcutsDialog._build_text(self._TemplateI18n("&Main navigation"))
        assert "Alt+M: focus the main navigation" in text

    def test_missing_ampersand_falls_back_to_n(self):
        text = ShortcutsDialog._build_text(self._TemplateI18n("Main navigation"))
        assert "Alt+N: focus the main navigation" in text


class TestMultiAccountShortcutVisibility:
    def test_hidden_without_a_main_window(self):
        text = ShortcutsDialog._build_text(_FakeI18n())
        assert "shortcut_ctrl_alt_num_label" not in text

    def test_hidden_for_a_single_account_install(self):
        text = ShortcutsDialog._build_text(_FakeI18n(), _FakeMainWindowSingleAccount())
        assert "shortcut_ctrl_alt_num_label" not in text

    def test_shown_when_multi_account_is_active(self):
        text = ShortcutsDialog._build_text(_FakeI18n(), _FakeMainWindowMultiAccount())
        assert "shortcut_ctrl_alt_num_label" in text


class _FakeVault:
    def __init__(self, configured, hide=True):
        self.configured = configured
        self.hide_navigation = hide


class _FakeMainWindowNoVault:
    account_id = None
    registry = None


class _FakeMainWindowUnconfiguredVault:
    account_id = None
    registry = None
    _chat_lock_vault = _FakeVault(configured=False)


class _FakeMainWindowConfiguredVault:
    account_id = None
    registry = None
    _chat_lock_vault = _FakeVault(configured=True)


class TestChatLockShortcutVisibility:
    """Alt+7 opens the locked-chats vault. Listing it unconditionally would
    itself tell anyone reading this help text that a vault exists — exactly
    what the vault's own hidden-navigation option is meant to prevent — so
    it must only appear once a vault has actually been configured."""

    def test_hidden_without_a_main_window(self):
        text = ShortcutsDialog._build_text(_FakeI18n())
        assert "shortcut_alt7_label" not in text

    def test_hidden_when_no_vault_was_ever_loaded(self):
        text = ShortcutsDialog._build_text(_FakeI18n(), _FakeMainWindowNoVault())
        assert "shortcut_alt7_label" not in text

    def test_shown_while_the_vault_was_never_configured(self):
        """Its navigation row is visible then, so nothing is being hidden."""
        text = ShortcutsDialog._build_text(_FakeI18n(), _FakeMainWindowUnconfiguredVault())
        assert "shortcut_alt7_label" in text

    def test_hidden_once_the_user_hid_the_vault(self):
        text = ShortcutsDialog._build_text(_FakeI18n(), _FakeMainWindowConfiguredVault())
        assert "shortcut_alt7_label" not in text

    def test_shown_when_the_configured_vault_is_not_hidden(self):
        window = _FakeMainWindowConfiguredVault()
        window._chat_lock_vault = _FakeVault(configured=True, hide=False)
        text = ShortcutsDialog._build_text(_FakeI18n(), window)
        assert "shortcut_alt7_label" in text
        assert "shortcut_ctrl_shift_k_vault_label" in text

    def test_emergency_close_shortcut_does_not_reveal_a_hidden_vault(self):
        text = ShortcutsDialog._build_text(
            _FakeI18n(), _FakeMainWindowConfiguredVault()
        )
        assert "shortcut_ctrl_shift_k_vault_label" not in text


class TestNewAppLevelShortcutsAreDocumented:
    def test_disconnect_and_exit_are_present(self):
        text = ShortcutsDialog._build_text(_FakeI18n())
        assert "shortcut_ctrl_alt_shift_d_label" in text
        assert "shortcut_ctrl_alt_shift_q_label" in text


class TestBulkActionShortcutsAreDocumented:
    """Each entry of the "Ações em massa" submenu has its own shortcut (see
    ConversationsPanel._on_accel_bulk_*), reachable whatever the "Substituir
    atalhos por ações em massa..." setting the note below them describes is
    set to — F1 is where a user finds out they exist at all."""

    def test_every_mass_action_shortcut_is_listed(self):
        text = ShortcutsDialog._build_text(_FakeI18n())
        for key in (
            "shortcut_bulk_copy_label",
            "shortcut_bulk_forward_label",
            "shortcut_bulk_star_label",
            "shortcut_bulk_pin_label",
            "shortcut_bulk_save_label",
            "shortcut_bulk_delete_label",
            "shortcut_bulk_clear_chats_label",
            "shortcut_bulk_delete_chats_label",
            "shortcut_bulk_archive_chats_label",
            "shortcut_bulk_read_chats_label",
            "shortcut_bulk_unread_chats_label",
        ):
            assert key in text
        assert "shortcut_bulk_override_note" in text
