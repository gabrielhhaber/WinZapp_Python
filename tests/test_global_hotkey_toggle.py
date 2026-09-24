"""The global hotkey also sends WinZapp back to the tray (issue #258, item 2).

The hotkey only ever restored the window. Pressed while WinZapp is the window
in front, it now hides it to the tray; anywhere else -- hidden, minimised,
behind another program, or with a WinZapp dialog in front -- it still brings
the window forward. Without a tray icon it never hides, since nothing but the
hotkey could then bring the window back.

MainWindow needs a wx.App, so the methods are bound onto a stub and
GetForegroundWindow is replaced for the duration of each test.
"""

import inspect

import pytest

import main
from main import MainWindow

HWND = 0x1234


class TestTheDecision:
    def test_the_window_in_front_is_hidden(self):
        assert MainWindow._hotkey_hides_window(True, HWND, HWND) is True

    def test_another_program_in_front_brings_it_forward(self):
        assert MainWindow._hotkey_hides_window(True, HWND, 0x9999) is False

    def test_a_winzapp_dialog_in_front_brings_it_forward(self):
        """Hiding the frame under an open dialog would strand the dialog."""
        dialog_hwnd = 0x5678
        assert MainWindow._hotkey_hides_window(True, HWND, dialog_hwnd) is False

    def test_without_a_tray_icon_it_is_never_hidden(self):
        assert MainWindow._hotkey_hides_window(False, HWND, HWND) is False

    def test_an_unknown_foreground_brings_it_forward(self):
        assert MainWindow._hotkey_hides_window(True, HWND, None) is False
        assert MainWindow._hotkey_hides_window(True, HWND, 0) is False

    def test_a_missing_handle_is_never_taken_for_the_foreground(self):
        assert MainWindow._hotkey_hides_window(True, 0, 0) is False


class _Window:
    toggle_window_from_hotkey = MainWindow.toggle_window_from_hotkey
    _hotkey_hides_window = staticmethod(MainWindow._hotkey_hides_window)

    def __init__(self, tray=True):
        self.tray_icon = object() if tray else None
        self.calls = []

    def GetHandle(self):
        return HWND

    def hide_to_tray(self):
        self.calls.append("hide")

    def restore_window(self):
        self.calls.append("restore")


@pytest.fixture
def foreground(monkeypatch):
    current = {"hwnd": None}
    monkeypatch.setattr(main.ctypes.windll.user32, "GetForegroundWindow",
                        lambda: current["hwnd"])
    return current


class TestTheToggle:
    def test_pressed_with_winzapp_in_front_it_hides(self, foreground):
        foreground["hwnd"] = HWND
        window = _Window()

        window.toggle_window_from_hotkey()

        assert window.calls == ["hide"]

    def test_pressed_elsewhere_it_restores(self, foreground):
        foreground["hwnd"] = 0x9999
        window = _Window()

        window.toggle_window_from_hotkey()

        assert window.calls == ["restore"]

    def test_without_a_tray_it_only_restores(self, foreground):
        foreground["hwnd"] = HWND
        window = _Window(tray=False)

        window.toggle_window_from_hotkey()

        assert window.calls == ["restore"]

    def test_a_failing_foreground_query_restores(self, monkeypatch):
        def _boom():
            raise OSError("no desktop")
        monkeypatch.setattr(main.ctypes.windll.user32, "GetForegroundWindow", _boom)
        window = _Window()

        window.toggle_window_from_hotkey()

        assert window.calls == ["restore"]


def test_the_global_hotkey_is_wired_to_the_toggle():
    src = inspect.getsource(MainWindow._apply_global_hotkey)
    assert "_HotkeyManager(vk, mod, self.toggle_window_from_hotkey)" in src
