"""One transcription model in memory at a time on the whole machine
(core/transcription/machine_slot.py): a second run waits instead of loading
another copy, and an idle model of another account's process is let go.

Two MachineSlot instances sharing one fake native stand for two WinZapp
processes sharing the named mutex and event."""
import threading
import time

import pytest

from core.transcription import errors, machine_slot, residency as residency_module


class SharedNative:
    """The named mutex and event, as two processes see them."""

    def __init__(self):
        self.mutex = threading.Lock()
        self.event = threading.Event()

    def acquire(self, seconds):
        return self.mutex.acquire(timeout=seconds)

    def release(self):
        self.mutex.release()

    def want(self):
        self.event.set()

    def unwant(self):
        self.event.clear()

    def wanted(self, seconds):
        return self.event.wait(seconds)


def two_processes():
    native = SharedNative()
    return (machine_slot.MachineSlot(lambda: native, poll_seconds=0.01),
            machine_slot.MachineSlot(lambda: native, poll_seconds=0.01), native)


def test_a_second_run_of_the_same_process_waits_for_the_first():
    slot, _other, _native = two_processes()
    order = []
    first_in = threading.Event()

    def second():
        with slot.held():
            order.append("second")

    with slot.held():
        first_in.set()
        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.1)
        order.append("first")
    thread.join(2)
    assert order == ["first", "second"]


def test_another_process_waits_too_and_says_it_wants_the_slot():
    a, b, native = two_processes()
    got = []
    with a.held():
        thread = threading.Thread(target=lambda: b.held().__enter__() and got.append(True))
        thread.start()
        time.sleep(0.1)
        assert got == [] and native.event.is_set()
    thread.join(2)
    assert got == [True] and b.holding


def test_a_cancelled_run_stops_waiting():
    a, b, _native = two_processes()

    def cancelled():
        raise errors.TranscriptionError(errors.CANCELLED, "cancelled by the user")

    with a.held():
        with pytest.raises(errors.TranscriptionError):
            with b.held(cancelled):
                pass
    # nothing left held: the next run goes through at once
    with b.held():
        assert b.holding


def test_an_idle_model_is_let_go_when_another_process_wants_the_slot():
    a, b, _native = two_processes()
    released = []

    class Backend:
        def release(self):
            released.append(True)

    here = residency_module.ModelResidency(timer_factory=_NoTimer, slot=a)
    here.configure(True)                          # "Keep the model loaded" on
    backend = Backend()
    with here.using(backend):
        pass
    assert released == []
    with b.held():                                # the other account loads
        deadline = time.monotonic() + 3
        while not released and time.monotonic() < deadline:
            time.sleep(0.02)
    assert released == [True]


def test_a_busy_model_is_not_let_go_and_releasing_twice_does_nothing():
    released = []

    class Backend:
        def release(self):
            released.append(True)

    here = residency_module.ModelResidency(timer_factory=_NoTimer, slot=_QuietSlot())
    backend = Backend()
    with here.using(backend):
        with here.using(backend):
            pass
        here.release_for_other_process()          # still running
        assert released == []
    here.release_for_other_process()
    here.release_for_other_process()
    assert released == [True]


def test_its_own_want_does_not_make_a_process_drop_its_model():
    a, _b, native = two_processes()
    released = []

    class Backend:
        def release(self):
            released.append(True)

    here = residency_module.ModelResidency(timer_factory=_NoTimer, slot=a)
    here.configure(True)
    with here.using(Backend()):
        pass
    with a.held():                                # this process's next run
        time.sleep(0.2)
    assert released == [] and not native.event.is_set()


def test_the_job_takes_the_slot_around_the_load_and_the_run(monkeypatch):
    from core.transcription import job as job_module
    seen = []

    class Slot:
        def held(self, check=None):
            seen.append(check)
            return _Context(seen)

    monkeypatch.setattr(machine_slot, "slot", Slot())
    monkeypatch.setattr(job_module.TranscriptionJob, "_decode_held",
                        lambda self, backend, prepared: seen.append("decoded") or "result")
    job = job_module.TranscriptionJob.__new__(job_module.TranscriptionJob)
    assert job_module.TranscriptionJob._decode(job, object(), object()) == "result"
    assert seen[1:] == ["enter", "decoded", "exit"]
    assert seen[0] == job._check_cancel


class _Context:
    def __init__(self, seen):
        self._seen = seen

    def __enter__(self):
        self._seen.append("enter")

    def __exit__(self, *exc):
        self._seen.append("exit")
        return False


class _NoTimer:
    def __init__(self, *_a, **_k):
        self.daemon = True

    def start(self):
        pass

    def cancel(self):
        pass


class _QuietSlot:
    def release_when_wanted(self, callback):
        pass
