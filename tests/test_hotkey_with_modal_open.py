"""The global hotkey with a WinZapp modal dialog open brings the dialog to the
front, not the disabled main window behind it (reported with the update
prompt left waiting in the tray)."""

import types

from core import dialog_foreground as df


def _modal_stub(shown=True, modal=True):
    return types.SimpleNamespace(IsShown=lambda: shown, IsModal=lambda: modal)


def test_the_newest_modal_dialog_on_screen_is_picked():
    frame = types.SimpleNamespace(IsShown=lambda: True)
    older, newer = _modal_stub(), _modal_stub()
    assert df.open_modal_dialog([frame, older, newer]) is newer
    assert df.open_modal_dialog([frame, older, _modal_stub(modal=False)]) is older


def test_no_modal_open_is_none():
    assert df.open_modal_dialog([_modal_stub(shown=False), _modal_stub(modal=False)]) is None
    assert df.open_modal_dialog([]) is None


def test_bringing_it_forward_raises_and_takes_the_foreground(monkeypatch):
    calls = []
    window = types.SimpleNamespace(IsShown=lambda: True, Raise=lambda: calls.append("raise"),
                                   GetHandle=lambda: 42)
    monkeypatch.setattr(df, "force_foreground", lambda hwnd: calls.append(hwnd) or True)
    assert df.bring_modal_to_front(window) is True
    assert calls == ["raise", 42]
    assert df.bring_modal_to_front(types.SimpleNamespace(IsShown=lambda: False)) is False
