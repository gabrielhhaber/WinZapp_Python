"""core.save_dialog_selection.schedule_deselect_extension() must never touch
a real window while pytest is running — see that module's own docstring for
what it does outside of tests (reaches into the native Save dialog via
pywin32 to fix an OS-level selection quirk). These tests only pin the
guards: no wx.App, no base name, and — the one that matters most, since a
suite-wide wx.App can legitimately exist by the time this file's tests run
(tests/conftest.py's session-scoped wx_app fixture) — the explicit
PYTEST_CURRENT_TEST check that keeps it inert regardless.
"""

import wx

import core.save_dialog_selection as save_dialog_selection


def test_does_nothing_with_no_base_name(monkeypatch):
    calls = []
    monkeypatch.setattr(wx, "CallLater", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(wx, "GetApp", lambda: object())

    save_dialog_selection.schedule_deselect_extension("")

    assert calls == []


def test_does_nothing_without_a_running_wx_app(monkeypatch):
    calls = []
    monkeypatch.setattr(wx, "CallLater", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(wx, "GetApp", lambda: None)

    save_dialog_selection.schedule_deselect_extension("voice_message")

    assert calls == []


def test_does_nothing_while_pytest_is_running(monkeypatch):
    """PYTEST_CURRENT_TEST is set by pytest itself for every test — including
    this one — so this also doubles as proof the guard is active for the
    whole suite, not just this direct check."""
    calls = []
    monkeypatch.setattr(wx, "CallLater", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(wx, "GetApp", lambda: object())

    save_dialog_selection.schedule_deselect_extension("voice_message")

    assert calls == []


def test_schedules_a_call_later_outside_of_pytest(monkeypatch):
    """Simulates the real (non-test) environment: a running wx.App and no
    PYTEST_CURRENT_TEST — confirms the guards above are what's suppressing
    it, not some unrelated reason (e.g. a typo in the base_name check)."""
    calls = []
    monkeypatch.setattr(wx, "CallLater", lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(wx, "GetApp", lambda: object())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    save_dialog_selection.schedule_deselect_extension("voice_message", delay_ms=200)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == 200
