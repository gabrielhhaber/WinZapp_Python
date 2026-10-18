"""Fast local file transfers must not queue a repaint for every file chunk."""
import pytest

from core.coalesced_callback import CoalescedCallback


def test_a_burst_queues_one_callback_with_the_final_progress():
    queued, seen = [], []
    report = CoalescedCallback(queued.append, lambda *args: seen.append(args))
    # 24 MiB at 256 KiB per HTTP chunk, followed by the completion report.
    for chunk in range(97):
        report("upload", chunk / 96)
    report("upload", 1.0)
    assert len(queued) == 1
    queued.pop()()
    assert seen == [("upload", 1.0)]


def test_a_later_update_gets_another_callback():
    queued, seen = [], []
    report = CoalescedCallback(queued.append, seen.append)
    report(0.1)
    queued.pop()()
    report(0.5)
    report(1.0)
    queued.pop()()
    assert seen == [0.1, 1.0]


def test_updates_during_delivery_are_not_lost():
    queued, seen = [], []

    def deliver(value):
        seen.append(value)
        if value == 0.1:
            report(1.0)

    report = CoalescedCallback(queued.append, deliver)
    report(0.1)
    queued.pop()()
    assert len(queued) == 1
    queued.pop()()
    assert seen == [0.1, 1.0]


def test_independent_transfers_do_not_replace_each_other():
    queued, seen = [], []
    first = CoalescedCallback(queued.append, lambda value: seen.append(("first", value)))
    second = CoalescedCallback(queued.append, lambda value: seen.append(("second", value)))
    first(0.1)
    second(0.2)
    first(1.0)
    for callback in queued:
        callback()
    assert seen == [("first", 1.0), ("second", 0.2)]


def test_a_scheduler_failure_does_not_leave_the_callback_pending():
    queued, seen = [], []

    def schedule(callback):
        if not queued:
            queued.append(None)
            raise RuntimeError("scheduler unavailable")
        queued.append(callback)

    report = CoalescedCallback(schedule, seen.append)
    with pytest.raises(RuntimeError):
        report(0.1)
    report(1.0)
    queued[-1]()
    assert seen == [1.0]
