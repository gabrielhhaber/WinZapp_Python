"""Update prompts for a WinZapp that starts with Windows (--background).

Both checkers were scheduled only `if not self.background_mode`, and
`_start_wpp_update_checker` returned early in background mode too. Restoring
the window from the tray later never scheduled them, so someone who only ever
starts WinZapp with Windows was never offered a WinZapp update nor a
WPPConnect Server one. Their dialogs have a hidden parent in that state, so
they must also bring themselves to the front (core/dialog_foreground.py).
"""

import ast
import inspect
import pathlib

import pytest

import updater
from core import dialog_foreground as front
from main_window.updates import UpdatesMixin

MAIN = pathlib.Path(__file__).resolve().parents[1] / "client" / "main.py"


def _enclosing_conditions(callee):
    """Source of every `if` test wrapping a wx.CallLater(..., self.<callee>)."""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    found = []

    def walk(node, conditions):
        for child in ast.iter_child_nodes(node):
            inner = conditions
            if isinstance(child, ast.If):
                inner = conditions + [ast.unparse(child.test)]
            if (isinstance(child, ast.Call) and ast.unparse(child.func) == "wx.CallLater"
                    and any(ast.unparse(a) == f"self.{callee}" for a in child.args)):
                found.append(conditions)
            walk(child, inner)

    walk(tree, [])
    return found


@pytest.mark.parametrize("callee", ["_start_update_checker", "_start_wpp_update_checker"])
def test_both_checkers_are_scheduled_in_background_mode(callee):
    scheduled = _enclosing_conditions(callee)
    assert len(scheduled) == 1
    assert not any("background_mode" in c for c in scheduled[0])


class _Window:
    _start_wpp_update_checker = UpdatesMixin._start_wpp_update_checker

    def __init__(self, may_run=True, updates_enabled=True):
        self.background_mode = True
        self.settings = {"general": {"updates_enabled": updates_enabled}}
        self._may_run = may_run

    def wpp_update_may_run_now(self):
        return self._may_run


@pytest.fixture
def wpp_checker(monkeypatch):
    started, later = [], []

    class _Checker:
        def __init__(self, mw):
            pass

        def start(self):
            started.append("start")

        def force_check(self):
            started.append("force")

    monkeypatch.setattr(updater, "WppUpdateChecker", _Checker)
    monkeypatch.setattr("main_window.updates.wx.CallLater", lambda ms, fn, *a, **k: later.append(ms))
    return started, later


def test_the_wpp_check_starts_in_background_mode(wpp_checker):
    started, _ = wpp_checker
    _Window()._start_wpp_update_checker()
    assert started == ["start"]


def test_the_wpp_check_still_respects_the_updates_setting(wpp_checker):
    started, _ = wpp_checker
    _Window(updates_enabled=False)._start_wpp_update_checker()
    assert started == []


def test_the_wpp_check_still_waits_for_pairing(wpp_checker):
    started, later = wpp_checker
    _Window(may_run=False)._start_wpp_update_checker()
    assert started == [] and later == [300000]


def test_accepting_a_wpp_update_is_announced_in_background_mode_too():
    # The announcements are unconditional; the one remaining mention is the
    # deliberate "do not un-hide the window of a background start" guard.
    source = inspect.getsource(UpdatesMixin._update_wpp_server)
    assert "if not self.background_mode:" not in source
    assert source.count("background_mode") == 1


# ── a failed fetch at login ───────────────────────────────────────────────────

class _FetchStub:
    _check_once = updater.UpdateChecker._check_once
    _FETCH_RETRY_DELAYS = updater.UpdateChecker._FETCH_RETRY_DELAYS

    def __init__(self, fail=True):
        self._mw = type("MW", (), {})()
        self._fail = fail
        self.retries = []
        self._force = False

    def _alpha_enabled(self):
        return False

    def _fetch_releases(self):
        if self._fail:
            raise OSError("network is down")
        return []

    def _schedule_retry(self, interval=None):
        self.retries.append(interval)


def test_failed_fetches_retry_quickly_a_bounded_number_of_times():
    stub = _FetchStub()
    for _ in range(len(stub._FETCH_RETRY_DELAYS) + 2):
        stub._check_once()
    quick = list(stub._FETCH_RETRY_DELAYS)
    assert stub.retries == quick + [None, None]   # then the 3 h interval
    assert max(quick) < updater.UpdateChecker._RETRY_INTERVAL


def test_a_successful_fetch_resets_the_quick_retry_budget():
    stub = _FetchStub()
    stub._check_once()
    stub._fail = False
    stub._check_once()    # no eligible release: normal interval, counter reset
    stub._fail = True
    stub._check_once()
    assert stub.retries == [30, None, 30]


# ── dialogs with a hidden parent ──────────────────────────────────────────────

def test_hidden_means_tray_hidden_or_never_shown():
    assert front.parent_is_hidden(type("W", (), {"_window_hidden": True})())
    assert front.parent_is_hidden(type("W", (), {"background_mode": True})())
    assert not front.parent_is_hidden(type("W", (), {"_window_hidden": False, "background_mode": False})())
    assert not front.parent_is_hidden(object())


def test_a_visible_main_window_gets_no_foreground_timer(monkeypatch):
    timers = []
    monkeypatch.setattr(front.wx, "CallLater", lambda *a, **k: timers.append(a))
    front.bring_to_front_if_hidden(type("W", (), {})(), object())
    assert timers == []


class _Dlg:
    def __init__(self, shown=True):
        self._shown = shown
        self.raised = False

    def IsShown(self):
        return self._shown

    def Raise(self):
        self.raised = True

    def GetHandle(self):
        return 1234


def _arm(monkeypatch, in_front, dialog, announce):
    timers = []
    monkeypatch.setattr(front.wx, "CallLater", lambda ms, fn: timers.append((ms, fn)))
    monkeypatch.setattr(front, "force_foreground", lambda hwnd: in_front)
    front.bring_to_front_if_hidden(type("W", (), {"_window_hidden": True})(), dialog, announce)
    assert len(timers) == 1
    timers[0][1]()


def test_a_hidden_parent_gets_the_dialog_raised_and_focused(monkeypatch):
    spoken = []
    dlg = _Dlg()
    _arm(monkeypatch, True, dlg, lambda: spoken.append("x"))
    assert dlg.raised and spoken == []


def test_when_windows_refuses_the_foreground_the_dialog_is_spoken(monkeypatch):
    spoken = []
    _arm(monkeypatch, False, _Dlg(), lambda: spoken.append("x"))
    assert spoken == ["x"]


def test_a_dialog_already_closed_is_left_alone(monkeypatch):
    spoken = []
    dlg = _Dlg(shown=False)
    _arm(monkeypatch, False, dlg, lambda: spoken.append("x"))
    assert not dlg.raised and spoken == []


def test_message_box_is_a_plain_message_box_while_the_window_is_visible(monkeypatch):
    calls = []
    monkeypatch.setattr(front.wx, "MessageBox", lambda *a: calls.append(a) or 7)
    mw = type("W", (), {})()
    assert front.message_box(mw, "m", "t", 5) == 7
    assert calls == [("m", "t", 5, mw)]
