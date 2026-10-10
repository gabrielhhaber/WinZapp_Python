"""Regression tests for late WPPConnect setup/update callbacks.

The setup worker reports completion with wx.CallAfter. Cancellation can end
ShowModal() after the worker queued success but before that callback executes;
the old callback then called EndModal() on a stopped loop and raised wxWidgets'
``IsRunning(): Use ScheduleExit() on not running loop`` assertion.
"""

import pytest

wx = pytest.importorskip("wx")

from ui.dialogs import api_setup


class _Timer:
    def __init__(self):
        self.stop_calls = 0

    def Stop(self):
        self.stop_calls += 1


class _Gauge:
    def __init__(self):
        self.values = []

    def SetValue(self, value):
        self.values.append(value)


class _I18n:
    def t(self, key):
        return key


class _DialogStub:
    _is_modal_active = api_setup.ApiSetupDialog._is_modal_active
    _end_modal_safely = api_setup.ApiSetupDialog._end_modal_safely
    _on_cancel = api_setup.ApiSetupDialog._on_cancel
    _finish_success = api_setup.ApiSetupDialog._finish_success
    _finish_error = api_setup.ApiSetupDialog._finish_error

    def __init__(self, modal=True):
        self._modal = modal
        self._cancelled = False
        self._finished = False
        self._trickling = True
        self._timer = _Timer()
        self._gauge = _Gauge()
        self._i18n = _I18n()
        self.end_results = []
        self.kill_calls = 0

    def IsModal(self):
        return self._modal

    def EndModal(self, result):
        if not self._modal:
            raise AssertionError("EndModal called without a running modal loop")
        self.end_results.append(result)
        self._modal = False

    def _kill_proc_tree(self):
        self.kill_calls += 1


def test_normal_success_closes_the_running_modal_once(monkeypatch):
    boxes = []
    monkeypatch.setattr(api_setup.wx, "MessageBox", lambda *args: boxes.append(args))
    dialog = _DialogStub()

    dialog._finish_success()
    dialog._finish_success()

    assert dialog.end_results == [wx.ID_OK]
    assert len(boxes) == 1
    assert dialog._gauge.values == [100]
    assert dialog._timer.stop_calls == 1


def test_staged_build_closes_without_announcing_installation_success(monkeypatch):
    boxes = []
    monkeypatch.setattr(api_setup.wx, "MessageBox", lambda *args: boxes.append(args))
    dialog = _DialogStub()
    dialog._api_dir = "api_staging"
    dialog._finish_success()
    assert dialog.end_results == [wx.ID_OK] and boxes == []


def test_queued_success_after_cancel_is_ignored(monkeypatch):
    boxes = []
    monkeypatch.setattr(api_setup.wx, "MessageBox", lambda *args: boxes.append(args))
    dialog = _DialogStub()

    dialog._on_cancel()
    dialog._finish_success()

    assert dialog.end_results == [wx.ID_CANCEL]
    assert dialog.kill_calls == 1
    assert boxes == []


@pytest.mark.parametrize("callback", ["_finish_success", "_finish_error"])
def test_completion_after_modal_loop_already_ended_is_a_noop(monkeypatch, callback):
    boxes = []
    monkeypatch.setattr(api_setup.wx, "MessageBox", lambda *args: boxes.append(args))
    dialog = _DialogStub(modal=False)

    if callback == "_finish_error":
        dialog._finish_error("details")
    else:
        dialog._finish_success()

    assert dialog.end_results == []
    assert boxes == []
    assert dialog._finished is True


def test_normal_error_closes_once_and_late_cancel_does_nothing(monkeypatch):
    boxes = []
    monkeypatch.setattr(api_setup, "show_error_details", lambda *args: boxes.append(args))
    dialog = _DialogStub()

    dialog._finish_error("npm failed")
    dialog._on_cancel()

    assert dialog.end_results == [wx.ID_CANCEL]
    assert len(boxes) == 1
    assert "npm failed" in boxes[0][-1]
    assert dialog.kill_calls == 0
