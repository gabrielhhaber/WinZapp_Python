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
    waiting = threading.Event()

    def second():
        with slot.held(waiting.set):          # called while it waits
            order.append("second")

    with slot.held():
        thread = threading.Thread(target=second)
        thread.start()
        assert waiting.wait(2)
        order.append("first")
    thread.join(2)
    assert order == ["first", "second"]


def test_another_process_waits_too_and_says_it_wants_the_slot():
    a, b, native = two_processes()
    got = []
    waiting = threading.Event()
    with a.held():
        thread = threading.Thread(target=lambda: b.held(waiting.set).__enter__() and got.append(True))
        thread.start()
        assert waiting.wait(2)
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
    released = threading.Event()

    class Backend:
        def release(self):
            released.set()

    here = residency_module.ModelResidency(timer_factory=_NoTimer, slot=a)
    here.configure(True)                          # "Keep the model loaded" on
    backend = Backend()
    with here.using(backend):
        pass
    assert not released.is_set()
    with b.held():                                # the other account loads
        assert released.wait(3)


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


class SlowWantNative(SharedNative):
    """want() through ctypes lets another thread run before the slot is held:
    the watcher saw the event set while this process did not hold it yet."""

    def __init__(self):
        super().__init__()
        self.watcher_saw = threading.Event()

    def want(self):
        super().want()
        self.watcher_saw.wait(0.05)               # the watcher's turn

    def wanted(self, seconds):
        got = super().wanted(seconds)
        if got:
            self.watcher_saw.set()
        return got


def test_its_own_want_does_not_make_a_process_drop_its_model():
    native = SlowWantNative()
    a = machine_slot.MachineSlot(lambda: native, poll_seconds=0.01)
    released = []

    class Backend:
        def release(self):
            released.append(True)

    here = residency_module.ModelResidency(timer_factory=_NoTimer, slot=a)
    here.configure(True)
    backend = Backend()
    with here.using(backend):                     # loaded, then idle and kept
        pass
    for _run in range(10):                        # this process's next runs
        native.watcher_saw.clear()
        with a.held():                            # as TranscriptionJob._decode
            with here.using(backend):
                pass
    assert released == [] and not native.event.is_set()


def test_the_job_takes_the_slot_around_the_load_and_the_run(monkeypatch):
    from core.transcription import job as job_module
    seen = []

    class Slot:
        def held(self, check=None, on_wait=None):
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


def test_waiting_is_said_only_when_another_run_holds_the_slot():
    """PHASE_WAITING_FOR_SLOT: a free slot is taken in silence; a held one
    says once that the run waits, however long the wait."""
    first, second, _native = two_processes()
    told = []
    with first.held(on_wait=lambda: told.append("free?")):
        pass
    assert told == []

    started = threading.Event()

    def other_process():
        with second.held(on_wait=lambda: (told.append("waiting"), started.set())):
            told.append("held")

    with first.held():
        thread = threading.Thread(target=other_process)
        thread.start()
        assert started.wait(2)
    thread.join(2)
    assert told == ["waiting", "held"]


def test_the_job_says_it_waits_for_the_slot(monkeypatch):
    from core.transcription import job as job_module
    phases = []

    class Slot:
        def held(self, check=None, on_wait=None):
            on_wait()
            return _Context([])

    monkeypatch.setattr(machine_slot, "slot", Slot())
    monkeypatch.setattr(job_module.TranscriptionJob, "_decode_held",
                        lambda self, backend, prepared: "result")
    job = job_module.TranscriptionJob.__new__(job_module.TranscriptionJob)
    job._on_phase = phases.append
    job._decode(object(), object())
    assert phases == [job_module.PHASE_WAITING_FOR_SLOT]


def test_a_process_waiting_for_another_still_lets_its_idle_model_go():
    """B finishes a run with a second one queued behind it, and A (another
    account) gets the slot: B's idle model must go while A runs. B's waiting
    run used to hold `_wanting` for the whole wait, so B's watcher took every
    want for its own and two models stayed loaded (the 9 GB case)."""
    native = SharedNative()
    gate = threading.Event()
    gate.set()

    class GatedNative(SharedNative):
        """B's view: while the gate is shut its tries fail, so A (waiting
        already) is the one that gets the slot after B's first run."""

        def __init__(self):
            self.mutex, self.event = native.mutex, native.event

        def acquire(self, seconds):
            if not gate.is_set():
                gate.wait(seconds)
                return False
            return native.acquire(seconds)

    a = machine_slot.MachineSlot(lambda: native, poll_seconds=0.01)
    b = machine_slot.MachineSlot(GatedNative, poll_seconds=0.01)
    released = threading.Event()

    class Backend:
        def release(self):
            released.set()

    here = residency_module.ModelResidency(timer_factory=_NoTimer, slot=b)
    here.configure(True)
    backend = Backend()
    first_in, first_go = threading.Event(), threading.Event()
    second_queued, a_waiting = threading.Event(), threading.Event()
    a_holds, a_done = threading.Event(), threading.Event()

    def b_first():
        with b.held():
            with here.using(backend):
                first_in.set()
                first_go.wait(5)

    def b_second():
        with b.held(second_queued.set):
            with here.using(backend):
                pass

    def a_run():
        with a.held(a_waiting.set):
            a_holds.set()
            gate.set()                          # B may try again: it waits
            a_done.wait(5)

    threads = [threading.Thread(target=b_first)]
    threads[0].start()
    assert first_in.wait(2)
    gate.clear()
    threads.append(threading.Thread(target=b_second))
    threads[1].start()
    assert second_queued.wait(2)
    threads.append(threading.Thread(target=a_run))
    threads[2].start()
    assert a_waiting.wait(2)
    first_go.set()
    try:
        assert a_holds.wait(5)
        assert released.wait(3), "B kept its idle model loaded while A ran"
    finally:
        a_done.set()
        for thread in threads:
            thread.join(5)
