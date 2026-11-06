"""Queued completion and cancellation cannot access a destroyed storage view."""
from core.storage_manager.jobs import JobSlot


def slot():
    workers, queue = [], []
    return JobSlot(queue.append, launch=workers.append), workers, queue


def test_single_job_remains_owned_until_completion_is_delivered():
    job, workers, queue = slot()
    results = []
    assert job.start(lambda cancelled: 12, lambda value, error: results.append((value, error)))
    assert not job.start(lambda c: 99, lambda *a: None)
    workers.pop()()
    assert job.busy and not results
    queue.pop()()
    assert not job.busy and results == [(12, None)]


def test_close_discards_a_completion_already_queued():
    job, workers, queue = slot()
    results = []
    job.start(lambda c: 1, lambda *args: results.append(args))
    workers.pop()()
    job.close()
    queue.pop()()
    assert results == [] and not job.start(lambda c: 2, lambda *a: None)


def test_cancel_is_cooperative_and_does_not_start_a_second_job():
    job, workers, queue = slot()
    results = []
    job.start(lambda c: c(), lambda value, error: results.append(value))
    job.cancel()
    assert not job.start(lambda c: None, lambda *a: None)
    workers.pop()()
    queue.pop()()
    assert results == [True]


def test_error_text_does_not_leak_private_paths():
    job, workers, queue = slot()
    results = []
    def fail(cancelled):
        raise OSError("private path and user name")
    job.start(fail, lambda value, error: results.append(error))
    workers.pop()()
    queue.pop()()
    assert results == ["OSError"]
