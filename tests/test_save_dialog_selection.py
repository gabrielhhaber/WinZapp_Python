"""core.save_dialog_selection.schedule_deselect_extension() must never touch
a real window while pytest is running — see that module's own docstring for
what it does outside of tests (reaches into the native Save dialog via
pywin32 to fix an OS-level selection quirk, retrying on a short interval
because neither the dialog's readiness nor the first SetForegroundWindow of
a freshly-launched process can be trusted on the first check — measured
live, this is exactly what made the very first Save As after launching
WinZapp behave differently from every one after it).

These tests pin the guards (no wx.App, no base name, PYTEST_CURRENT_TEST —
the one that matters most, since a suite-wide wx.App can legitimately exist
by the time this file's tests run via tests/conftest.py's session-scoped
wx_app fixture) and the retry/give-up bookkeeping, all against a mocked
wx.CallLater and a mocked _deselect_extension — nothing here ever touches a
real window.
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
    monkeypatch.setattr(wx, "CallLater", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(wx, "GetApp", lambda: object())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    save_dialog_selection.schedule_deselect_extension("voice_message")

    assert len(calls) == 1
    assert calls[0][0] == save_dialog_selection._RETRY_INTERVAL_MS


def test_retries_when_an_attempt_is_not_ready_yet(monkeypatch):
    """Measured live: neither the dialog's own child controls nor the first
    SetForegroundWindow after launch are reliably ready on the very first
    check — this retry loop (rather than a single, even delayed, attempt)
    is what a real fresh-launch Save As needs, confirmed on real dialogs."""
    calls = []
    monkeypatch.setattr(wx, "CallLater", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(wx, "GetApp", lambda: object())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    results = iter([False, False, True])
    monkeypatch.setattr(
        save_dialog_selection, "_deselect_extension", lambda base_name: next(results)
    )

    save_dialog_selection.schedule_deselect_extension("voice_message")
    assert len(calls) == 1

    calls[0][1]()  # attempt 1: not ready -> reschedules
    assert len(calls) == 2
    calls[1][1]()  # attempt 2: not ready -> reschedules again
    assert len(calls) == 3
    calls[2][1]()  # attempt 3: succeeded -> stops
    assert len(calls) == 3


def test_gives_up_after_the_attempt_ceiling(monkeypatch):
    """Bounded, not infinite: a Save dialog that never appears (or is closed
    before the fix lands) must not leave a timer rescheduling itself
    forever."""
    calls = []
    monkeypatch.setattr(wx, "CallLater", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(wx, "GetApp", lambda: object())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(save_dialog_selection, "_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(save_dialog_selection, "_deselect_extension", lambda base_name: False)

    save_dialog_selection.schedule_deselect_extension("voice_message")
    i = 0
    while i < len(calls) and i < 10:
        calls[i][1]()
        i += 1

    assert len(calls) == 3
