"""Unexpected WPP worker failures must keep retry/error paths alive, without wx."""
from types import SimpleNamespace

import pytest

import updater


def checker(monkeypatch):
    obj = updater.WppUpdateChecker(SimpleNamespace(
        _get_installed_wpp_version=lambda: "2.10.42"))
    retries, callbacks = [], []
    monkeypatch.setattr(obj, "_schedule_retry", lambda interval=None: retries.append(interval))
    monkeypatch.setattr(updater.wx, "CallAfter", lambda fn, *a, **kw: callbacks.append(fn.__name__))
    return obj, retries, callbacks


@pytest.mark.parametrize("boundary", ["installed", "minimum", "dispatch"])
def test_unexpected_worker_errors_retry_once(monkeypatch, boundary):
    obj, retries, callbacks = checker(monkeypatch)
    monkeypatch.setattr(updater, "homologated_wpp_tag", lambda path: "v2.10.48")
    monkeypatch.setattr(obj, "_other_account_busy", lambda: False)

    def fail(*a, **kw):
        raise LookupError("synthetic unavailable codec")

    if boundary == "installed":
        obj._mw._get_installed_wpp_version = fail
    elif boundary == "minimum":
        monkeypatch.setattr(updater, "homologated_wpp_tag", fail)
    else:
        monkeypatch.setattr(updater.wx, "CallAfter", fail)
    obj._check_once()
    assert retries == [obj._ERROR_RETRY_INTERVAL]
    assert callbacks == []


def test_success_hands_off_without_an_extra_retry(monkeypatch):
    obj, retries, callbacks = checker(monkeypatch)
    monkeypatch.setattr(updater, "homologated_wpp_tag", lambda path: "v2.10.48")
    monkeypatch.setattr(obj, "_other_account_busy", lambda: False)
    obj._check_once()
    assert callbacks == ["_prompt_update"]
    assert retries == []


def test_forced_failure_reports_error_without_automatic_reinstall(monkeypatch):
    obj, retries, callbacks = checker(monkeypatch)
    def fail():
        raise LookupError("synthetic unavailable codec")
    monkeypatch.setattr(obj, "_pinned_tag", fail)
    obj._force_reinstall_worker()
    assert callbacks == ["_show_force_reinstall_error"]
    assert retries == []


def test_stop_prevents_inflight_worker_from_restarting_timer(monkeypatch):
    obj = updater.WppUpdateChecker(SimpleNamespace(_get_installed_wpp_version=lambda: "2.10.42"))
    def stop_and_fail(path):
        obj.stop()
        raise LookupError("synthetic shutdown race")
    monkeypatch.setattr(updater, "homologated_wpp_tag", stop_and_fail)
    monkeypatch.setattr(updater.threading, "Timer", lambda *a, **kw: pytest.fail("timer after stop"))
    obj._check_once()
    assert obj._retry_timer is None


def test_stopped_checker_ignores_queued_forced_check(monkeypatch):
    obj, retries, callbacks = checker(monkeypatch)
    obj.stop()
    monkeypatch.setattr(obj, "_pinned_tag", lambda: pytest.fail("stopped worker"))
    obj._force_reinstall_worker()
    assert not callbacks and not retries


def test_stop_during_timer_creation_cannot_publish_or_start_it(monkeypatch):
    obj = updater.WppUpdateChecker(SimpleNamespace())
    timers = []
    class Timer:
        def __init__(self, *a):
            self.started = self.cancelled = False
            timers.append(self)
            obj.stop()  # deterministic interruption before timer publication
        def start(self):
            self.started = True
        def cancel(self):
            self.cancelled = True
    monkeypatch.setattr(updater.threading, "Timer", Timer)
    obj._schedule_retry()
    assert obj._stopped and obj._retry_timer is None
    assert len(timers) == 1 and timers[0].cancelled and not timers[0].started


def test_stop_during_timer_start_cancels_the_published_timer(monkeypatch):
    obj = updater.WppUpdateChecker(SimpleNamespace())
    timers = []
    class Timer:
        def __init__(self, *a):
            self.cancelled = False
            timers.append(self)
        def start(self):
            assert obj._retry_timer is self
            obj.stop()
        def cancel(self):
            self.cancelled = True
    monkeypatch.setattr(updater.threading, "Timer", Timer)
    obj._schedule_retry()
    assert obj._stopped and obj._retry_timer is None and timers[0].cancelled
