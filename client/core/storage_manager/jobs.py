"""One cancellable job per view/runtime; late results have no new owner."""
from __future__ import annotations

import threading


class JobSlot:
    def __init__(self, dispatch, *, launch=None):
        self.dispatch = dispatch
        self.launch = launch or self._launch
        self._lock = threading.RLock()
        self._generation = 0
        self._cancel = None
        self._closed = False

    @staticmethod
    def _launch(work):
        thread = threading.Thread(target=work, daemon=True, name="winzapp-storage")
        thread.start()

    @property
    def busy(self):
        with self._lock:
            return self._cancel is not None

    def cancel(self):
        with self._lock:
            if self._cancel is not None:
                self._cancel.set()

    def close(self):
        with self._lock:
            self._closed = True
            self._generation += 1
            self.cancel()

    def start(self, work, done):
        with self._lock:
            if self._closed or self._cancel is not None:
                return False
            self._generation += 1
            generation = self._generation
            token = self._cancel = threading.Event()

        def run():
            value, error = None, None
            try:
                value = work(token.is_set)
            except Exception as exc:
                # Only the safe exception type leaves the worker. Exception
                # text can contain private paths and must not enter the UI/log.
                error = type(exc).__name__
            def deliver():
                with self._lock:
                    if self._closed or generation != self._generation:
                        return
                    self._cancel = None
                done(value, error)
            self.dispatch(deliver)
        try:
            self.launch(run)
        except Exception:
            with self._lock:
                self._cancel = None
            raise
        return True
