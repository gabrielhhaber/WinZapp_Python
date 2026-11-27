"""The connection dialog must not stay behind the main window (File >
Disconnect left the chat list in front of the QR code)."""

from ui.dialogs.connect_dialog_placement import (
    bring_to_front, connection_dialog_parent,
)


class Frame:
    def __init__(self, shown=True, iconized=False, broken=False):
        self._shown, self._iconized, self._broken = shown, iconized, broken

    def IsShown(self):
        if self._broken:
            raise RuntimeError("wrapped C/C++ object has been deleted")
        return self._shown

    def IsIconized(self):
        return self._iconized


def test_visible_main_window_owns_the_dialog():
    frame = Frame()
    assert connection_dialog_parent(frame) is frame


def test_no_parent_when_main_window_is_not_shown_or_minimised():
    assert connection_dialog_parent(Frame(shown=False)) is None
    assert connection_dialog_parent(Frame(iconized=True)) is None


def test_no_parent_without_or_with_a_dead_main_window():
    assert connection_dialog_parent(None) is None
    assert connection_dialog_parent(Frame(broken=True)) is None


def test_no_parent_when_main_window_is_hidden_to_the_tray():
    hidden = Frame()
    hidden._window_hidden = True
    assert connection_dialog_parent(hidden) is None
    background = Frame()
    background.background_mode = True
    assert connection_dialog_parent(background) is None


def test_show_connection_dial_uses_the_helpers():
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "client", "ui", "dialogs", "connect.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "connection_dialog_parent(self.main_window)" in src
    assert "bring_to_front" in src


def test_bring_to_front_raises_and_survives_errors():
    calls = []

    class Dlg:
        def Raise(self):
            calls.append("Raise")

    bring_to_front(Dlg())
    assert calls == ["Raise"]

    class Dead:
        def Raise(self):
            raise RuntimeError

    bring_to_front(Dead())  # must not raise
