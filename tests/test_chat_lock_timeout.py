"""Window-free coverage for the locked-chat vault inactivity timeout."""

import inspect

from cryptography.fernet import Fernet

import main as main_module
from core.chat_lock_vault import ChatLockVault
from main import MainWindow
from ui.dialogs.settings_dialog import SettingsDialog


class _Timer:
    def __init__(self, milliseconds, callback):
        self.milliseconds = milliseconds
        self.callback = callback
        self.stopped = False

    def Stop(self):
        self.stopped = True

    def Restart(self, milliseconds):
        self.milliseconds = milliseconds
        self.stopped = False


class _I18n:
    @staticmethod
    def t(key):
        return key


class _TimeoutWindow:
    _cancel_chat_lock_timeout = MainWindow._cancel_chat_lock_timeout
    _arm_chat_lock_timeout = MainWindow._arm_chat_lock_timeout
    _on_chat_lock_timeout = MainWindow._on_chat_lock_timeout
    touch_chat_lock_timeout = MainWindow.touch_chat_lock_timeout
    _on_chat_lock_char_hook = MainWindow._on_chat_lock_char_hook
    set_chat_lock_timeout_minutes = MainWindow.set_chat_lock_timeout_minutes

    def __init__(self):
        self._chat_lock_vault = ChatLockVault(Fernet.generate_key())
        self._chat_lock_vault.configure("246810", "gizli-kod")
        self._chat_lock_unlocked = True
        self._chat_lock_timeout_timer = None
        self.persisted = 0
        self.lock_calls = []
        self.outputs = []
        self.i18n = _I18n()

    def _persist_chat_lock_vault(self):
        self.persisted += 1

    def lock_chat_vault(self, **kwargs):
        self.lock_calls.append(kwargs)
        self._chat_lock_unlocked = False

    def output(self, message, **kwargs):
        self.outputs.append((message, kwargs))


def _capture_timers(monkeypatch):
    timers = []

    def _call_later(milliseconds, callback):
        timer = _Timer(milliseconds, callback)
        timers.append(timer)
        return timer

    monkeypatch.setattr(main_module.wx, "CallLater", _call_later)
    return timers


def test_unlock_arms_the_default_five_minute_inactivity_timeout(monkeypatch):
    timers = _capture_timers(monkeypatch)
    window = _TimeoutWindow()

    window._arm_chat_lock_timeout()

    assert len(timers) == 1
    assert timers[0].milliseconds == 5 * 60 * 1000
    assert window._chat_lock_timeout_timer is timers[0]


def test_changing_timeout_persists_and_restarts_the_running_timer(monkeypatch):
    timers = _capture_timers(monkeypatch)
    window = _TimeoutWindow()
    window._arm_chat_lock_timeout()

    window.set_chat_lock_timeout_minutes(30)

    assert window._chat_lock_vault.auto_lock_minutes == 30
    assert window.persisted == 1
    assert len(timers) == 1
    assert not timers[0].stopped
    assert timers[0].milliseconds == 30 * 60 * 1000


def test_never_option_cancels_the_timer_without_scheduling_another(monkeypatch):
    timers = _capture_timers(monkeypatch)
    window = _TimeoutWindow()
    window._arm_chat_lock_timeout()

    window.set_chat_lock_timeout_minutes(0)

    assert timers[0].stopped
    assert len(timers) == 1
    assert window._chat_lock_timeout_timer is None


def test_timeout_closes_the_vault_silently_then_announces_why(monkeypatch):
    timers = _capture_timers(monkeypatch)
    window = _TimeoutWindow()
    window._arm_chat_lock_timeout()

    timers[0].callback()

    assert window.lock_calls == [{"silent": True}]
    assert window.outputs == [
        ("chat_lock_timed_out", {"interrupt": True})
    ]
    assert window._chat_lock_timeout_timer is None


def test_settings_wire_an_accessible_timeout_choice_without_opening_it():
    source = inspect.getsource(SettingsDialog._build_ui)

    assert "chat_lock_timeout_label" in source
    assert "self._chat_lock_timeout_choice.SetName" in source
    assert "choices=self._chat_lock_timeout_labels()" in source


def test_unlock_and_manual_close_wire_the_timer_lifecycle():
    unlock_source = inspect.getsource(MainWindow.unlock_chat_lock_vault)
    close_source = inspect.getsource(MainWindow.lock_chat_vault)

    assert "self._arm_chat_lock_timeout()" in unlock_source
    assert "self._cancel_chat_lock_timeout()" in close_source


class _KeyEvent:
    def __init__(self, key, modifiers=0):
        self.key = key
        self.modifiers = modifiers
        self.skipped = False

    def GetKeyCode(self):
        return self.key

    def GetModifiers(self):
        return self.modifiers

    def Skip(self):
        self.skipped = True


def test_keyboard_activity_restarts_the_inactivity_timer(monkeypatch):
    timers = _capture_timers(monkeypatch)
    window = _TimeoutWindow()
    window._arm_chat_lock_timeout()
    timers[0].milliseconds = 1
    event = _KeyEvent(ord("A"))

    window._on_chat_lock_char_hook(event)

    assert timers[0].milliseconds == 5 * 60 * 1000
    assert event.skipped


def test_ctrl_shift_k_immediately_closes_an_open_vault(monkeypatch):
    _capture_timers(monkeypatch)
    window = _TimeoutWindow()
    event = _KeyEvent(
        ord("K"), main_module.wx.MOD_CONTROL | main_module.wx.MOD_SHIFT
    )

    window._on_chat_lock_char_hook(event)

    assert window.lock_calls == [{}]
    assert not event.skipped
