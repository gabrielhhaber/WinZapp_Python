"""Window-free checks for recovery-key reading and clipboard actions."""

import inspect

import ui.chat_lock as chat_lock_module
from ui.chat_lock import RecoveryKeyDialog
from ui.dialogs.settings_dialog import SettingsDialog


class _I18n:
    @staticmethod
    def t(key):
        return key


class _RecoveryDialogStub:
    _on_read = RecoveryKeyDialog._on_read
    _on_copy = RecoveryKeyDialog._on_copy

    def __init__(self):
        self._i18n = _I18n()
        self._recovery_code = "ABCD-EFGH-JKLM-2345"
        self.announced = []

    def _announce(self, text):
        self.announced.append(text)


def test_recovery_dialog_exposes_explicit_read_and_copy_buttons():
    source = inspect.getsource(RecoveryKeyDialog.__init__)

    assert "chat_lock_recovery_read" in source
    assert "chat_lock_recovery_copy" in source
    assert "self.read_button.SetFocus()" in source


def test_settings_expose_the_pin_change_action():
    source = inspect.getsource(SettingsDialog._build_ui)

    assert "chat_lock_change_pin" in source
    assert "self._chat_lock_change_pin_btn.Bind" in source
    assert "self._on_chat_lock_change_pin" in source


def test_read_button_speaks_the_key_in_clear_groups():
    dialog = _RecoveryDialogStub()

    dialog._on_read(None)

    assert dialog.announced == [
        "chat_lock_recovery_label: ABCD. EFGH. JKLM. 2345"
    ]


def test_copy_button_places_the_exact_key_on_the_clipboard(monkeypatch):
    copied = []
    monkeypatch.setattr(chat_lock_module.pyperclip, "copy", copied.append)
    dialog = _RecoveryDialogStub()

    dialog._on_copy(None)

    assert copied == ["ABCD-EFGH-JKLM-2345"]
    assert dialog.announced == ["chat_lock_recovery_copied"]


def test_copy_failure_is_announced_without_speaking_the_secret(monkeypatch):
    def _fail(_text):
        raise RuntimeError("clipboard busy")

    monkeypatch.setattr(chat_lock_module.pyperclip, "copy", _fail)
    dialog = _RecoveryDialogStub()

    dialog._on_copy(None)

    assert dialog.announced == ["chat_lock_recovery_copy_failed"]
