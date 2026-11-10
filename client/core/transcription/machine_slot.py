"""One transcription model in memory at a time, for the whole machine.

Why this exists. Reported from a user's logs (2026-11-07): switching accounts
while voice notes were being transcribed took 9 GB of RAM in a minute and a
half. Two copies of whisper.cpp's large-v3 had been loaded at once:

* **In one process**, a second transcription found the whisper-server busy
  and ran on whisper-cli, which loads its own copy of the model. With the
  card already full (87 MB of 4 GB free), Windows put that copy in shared
  memory: 5.7 GB of RAM gone in two seconds.
* **Across processes**, each account is a WinZapp process of its own with its
  own whisper-server, and a model kept loaded by one account stays there
  while the next one loads another.

So a run takes this slot before it loads a model and gives it back when it
ends: a second run, in this process or in another account's, waits for the
first instead of loading another copy. A process whose model is still loaded
but idle lets it go when another process wants the slot (`ModelResidency`
watches for that through `release_when_wanted`).

On Windows the slot is a named mutex in the session's `Local\\` namespace, so
the accounts' processes share it; a process that dies holding it hands it on
(an abandoned mutex is still acquired). A manual-reset event says that some
process wants the slot. Elsewhere, and if the named objects cannot be made,
only this process's threads are kept apart.

Nothing here is wx, and nothing here logs a message, a chat or a model id.
"""

from __future__ import annotations

import logging
import sys
import threading

MUTEX_NAME = "Local\\WinZapp.Transcription.Slot"
WANTED_NAME = "Local\\WinZapp.Transcription.Wanted"

#: How often a waiting run checks whether it was cancelled.
POLL_SECONDS = 0.25

_WAIT_OBJECT_0 = 0x0
_WAIT_ABANDONED = 0x80


class _WindowsNative:
    """The named mutex and event, through kernel32."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k.CreateEventW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        k.SetEvent.argtypes = (wintypes.HANDLE,)
        k.ResetEvent.argtypes = (wintypes.HANDLE,)
        self._k = k
        self._mutex = k.CreateMutexW(None, False, MUTEX_NAME)
        self._wanted = k.CreateEventW(None, True, False, WANTED_NAME)
        if not self._mutex or not self._wanted:
            raise OSError(ctypes.get_last_error(), "named slot unavailable")

    def acquire(self, seconds) -> bool:
        answer = self._k.WaitForSingleObject(self._mutex, int(seconds * 1000))
        return answer in (_WAIT_OBJECT_0, _WAIT_ABANDONED)

    def release(self) -> None:
        self._k.ReleaseMutex(self._mutex)

    def want(self) -> None:
        self._k.SetEvent(self._wanted)

    def unwant(self) -> None:
        self._k.ResetEvent(self._wanted)

    def wanted(self, seconds) -> bool:
        return self._k.WaitForSingleObject(self._wanted, int(seconds * 1000)) == _WAIT_OBJECT_0


class _LocalNative:
    """Only this process: nobody else ever wants the slot."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def acquire(self, seconds) -> bool:
        return self._lock.acquire(timeout=seconds)

    def release(self) -> None:
        self._lock.release()

    def want(self) -> None:
        pass

    def unwant(self) -> None:
        pass

    def wanted(self, seconds) -> bool:
        self._stop.wait(seconds)
        return False


def _make_native():
    if sys.platform == "win32":
        try:
            return _WindowsNative()
        except Exception as exc:
            logging.warning("[transcription] machine-wide slot unavailable (%s) — "
                            "keeping only this process's runs apart", type(exc).__name__)
    return _LocalNative()


class MachineSlot:
    """`with slot.held(check): ...` around a model's load and run.

    `check` is called while waiting and may raise to give up (a cancellation).
    """

    def __init__(self, native_factory=_make_native, poll_seconds=POLL_SECONDS):
        self._native_factory = native_factory
        self._native = None
        self._poll = float(poll_seconds)
        self._init_lock = threading.Lock()
        #: This process's runs, one after another; the named mutex is
        #: re-entrant for its owning thread and would not keep them apart.
        self._turn = threading.Lock()
        self._holding = threading.Event()
        #: Set from this process's own want() until the slot is held (or the
        #: wait gave up): the watcher must not take that signal for another
        #: process's and let this process's own idle model go.
        self._wanting = threading.Event()
        self._watchers = []
        self._watcher_thread = None
        #: Never set: only waited on, as a sleep.
        self._pause = threading.Event()

    def _get_native(self):
        with self._init_lock:
            if self._native is None:
                self._native = self._native_factory()
            return self._native

    @property
    def holding(self) -> bool:
        """Whether a run of this process holds the slot right now."""
        return self._holding.is_set()

    def held(self, check=None, on_wait=None):
        """`on_wait()` is called once, only when the slot is not free at once
        (another run holds it): the UI says it is waiting for that run, and an
        uncontended run says nothing more than before."""
        return _Held(self, check, on_wait)

    def _acquire(self, check, on_wait=None) -> None:
        told = False

        def waiting():
            nonlocal told
            if not told and on_wait is not None:
                told = True
                on_wait()

        if not self._turn.acquire(blocking=False):
            waiting()
            while not self._turn.acquire(timeout=self._poll):
                if check is not None:
                    check()
        self._wanting.set()
        try:
            native = self._get_native()
            native.want()
            if not native.acquire(0):
                waiting()
                while not native.acquire(self._poll):
                    native.want()
                    if check is not None:
                        check()
        except BaseException:
            self._wanting.clear()
            self._turn.release()
            raise
        # Said again once held: another waiter that got the slot first may
        # have cleared it when it was done.
        native.want()
        self._holding.set()
        self._wanting.clear()

    def _release(self) -> None:
        native = self._get_native()
        self._holding.clear()
        try:
            native.unwant()
            native.release()
        finally:
            self._turn.release()

    # ── Letting an idle model go for another process ────────────────────────

    def release_when_wanted(self, callback) -> None:
        """Call `callback()` whenever another process wants the slot while
        this one does not hold it. The callback must be cheap when there is
        nothing left to release: it is called again until the slot is free."""
        with self._init_lock:
            self._watchers.append(callback)
            if self._watcher_thread is None:
                self._watcher_thread = threading.Thread(
                    target=self._watch, name="transcription-slot-watch", daemon=True)
                self._watcher_thread.start()

    def _watch(self) -> None:
        native = self._get_native()
        while True:
            try:
                wanted = native.wanted(1.0)
            except Exception:
                return
            if not wanted:
                continue
            if self.holding or self._wanting.is_set():
                # Our own run said it (holding, or still on its way to hold:
                # want() goes through ctypes, which lets this thread run before
                # the slot is held, and 5 runs in 20 lost their own model to
                # it): wait for it to be done.
                while self.holding or self._wanting.is_set():
                    self._pause.wait(self._poll)
                continue
            self.notify_wanted()
            # Not again at once: the event stays set while the other process runs.
            self._pause.wait(self._poll * 4)

    def notify_wanted(self) -> None:
        """Run the callbacks (also what the watcher thread does)."""
        for callback in list(self._watchers):
            try:
                callback()
            except Exception as exc:
                logging.warning("[transcription] releasing a model for another process failed: %s",
                                type(exc).__name__)


class _Held:
    def __init__(self, slot, check, on_wait=None):
        self._slot = slot
        self._check = check
        self._on_wait = on_wait

    def __enter__(self):
        self._slot._acquire(self._check, self._on_wait)
        return self

    def __exit__(self, *exc_info):
        self._slot._release()
        return False


#: The process-wide slot.
slot = MachineSlot()
