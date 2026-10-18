"""Keep one pending UI callback for a stream of replaceable progress values."""
import threading


class CoalescedCallback:
    """Schedule the latest arguments; intermediate values do not queue repaints.

    The scheduler must post asynchronously (for example wx.CallAfter). This is
    for numeric progress, not ordered events such as message arrivals/stages.
    """

    def __init__(self, schedule, callback):
        self._schedule = schedule
        self._callback = callback
        self._lock = threading.Lock()
        self._pending = False
        self._latest = ()

    def __call__(self, *args):
        with self._lock:
            self._latest = args
            if self._pending:
                return
            self._pending = True
        try:
            self._schedule(self._flush)
        except Exception:
            with self._lock:
                self._pending = False
            raise

    def _flush(self):
        with self._lock:
            args = self._latest
            self._latest = ()
            self._pending = False
        self._callback(*args)
