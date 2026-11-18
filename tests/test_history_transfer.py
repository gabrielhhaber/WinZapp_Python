"""What WinZapp says about the phone's history transfer
(client/main_window/history_transfer.py)."""
from types import SimpleNamespace as N

import pytest

from main_window import history_transfer as ht


def transfer(progress, stalled=False, kind="FULL"):
    return {"type": kind, "progress": progress, "stalled": stalled,
            "resumeAttempts": 0, "resumeRefused": False}


def keys(said):
    return [key for key, _ in said]


def test_the_reported_run_start_stall_and_reopen():
    """RECENT to 100%, FULL to 40%, the phone stops, then sends again."""
    w = ht.Watch(0)
    assert w.observe(None, 5) == []
    assert keys(w.observe(transfer(5, kind="RECENT"), 10)) == ["history_transfer_started"]
    assert w.observe(transfer(100, kind="RECENT"), 30) == []
    assert w.observe(transfer(10), 50) == []        # FULL started: no new notice
    assert w.observe(transfer(40), 150) == []
    assert w.observe(transfer(40, stalled=True), 220) == [
        ("history_transfer_stalled", {"percent": 40})]
    assert w.observe(transfer(40, stalled=True), 240) == []     # said once
    assert keys(w.observe(transfer(42), 400)) == ["history_transfer_resumed"]
    assert w.observe(transfer(100), 500) == []
    assert keys(w.observe(transfer(100), 500 + ht.DONE_QUIET)) == ["history_transfer_done"]
    assert w.finished
    assert w.observe(transfer(10), 9999) == []


def test_recent_at_100_is_not_the_end_while_full_follows():
    w = ht.Watch(0)
    w.observe(transfer(50, kind="RECENT"), 1)
    w.observe(transfer(100, kind="RECENT"), 2)
    assert w.observe(transfer(100, kind="RECENT"), 2 + ht.DONE_QUIET - 1) == []
    assert w.observe(transfer(3), 2 + ht.DONE_QUIET - 1) == []
    assert not w.finished


def test_an_old_account_ends_quietly():
    w = ht.Watch(0)
    assert w.observe(None, ht.NO_TRANSFER_WAIT - 1) == []
    assert not w.finished
    assert w.observe(None, ht.NO_TRANSFER_WAIT) == []
    assert w.finished


def test_a_transfer_already_done_says_nothing():
    w = ht.Watch(0)
    assert w.observe(transfer(100), 1) == []
    assert w.observe(transfer(100), 1 + ht.DONE_QUIET) == []
    assert w.finished


def test_a_transfer_found_stalled_says_the_stall_first():
    w = ht.Watch(0)
    assert keys(w.observe(transfer(40, stalled=True), 1)) == ["history_transfer_stalled"]
    assert keys(w.observe(transfer(100), 2)) == []
    assert keys(w.observe(transfer(100), 2 + ht.DONE_QUIET)) == ["history_transfer_done"]


def test_the_watch_has_an_end():
    w = ht.Watch(0)
    w.observe(transfer(10), 1)
    assert w.observe(transfer(10), ht.MAX_WATCH) == []
    assert w.finished


@pytest.mark.parametrize("value, expected", [(None, 0), ("40", 40), (150, 100), (-3, 0)])
def test_percent_is_read_safely(value, expected):
    assert ht._percent(value) == expected


def test_start_watch_only_on_neonize_and_once(monkeypatch):
    started = []
    monkeypatch.setattr(ht.threading, "Thread",
                        lambda **kw: N(start=lambda: started.append(kw["name"])))
    assert ht.start_watch(N(wpp_engine="wppconnect")) is False
    window = N(wpp_engine="neonize")
    assert ht.start_watch(window) is True
    assert ht.start_watch(window) is False
    assert started == ["history-transfer"]


def test_run_speaks_through_output_and_ends(monkeypatch):
    statuses = iter([{"historyTransfer": transfer(10)},
                     {"historyTransfer": transfer(100)},
                     {"historyTransfer": transfer(100)}])
    calls = []
    monkeypatch.setattr(ht.wx, "CallAfter", lambda fn, *a: calls.append((fn, a)))
    clock = iter([0, 1, 2, 2 + ht.DONE_QUIET])
    window = N(_wa_connected=True, _history_transfer_watching=True,
               fetch_history_sync_status=lambda: next(statuses),
               i18n=N(t=lambda key: key), output=object())
    ht._run(window, sleep=lambda _s: None, clock=lambda: next(clock))
    assert [a for _, a in calls] == [("history_transfer_started",),
                                     ("history_transfer_done",)]
    assert all(fn is window.output for fn, _ in calls)
    assert window._history_transfer_watching is False
