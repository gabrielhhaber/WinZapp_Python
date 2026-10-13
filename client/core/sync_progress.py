"""Periodic spoken progress for the long synchronization sweeps.

A full sync (first pairing, F5) walks every chat, an update after days away
can walk hundreds, and the media phase walks every candidate media message
after either; on a real account any of them can take minutes. Between
"synchronization started" and "synchronization finished" a screen reader user
heard nothing, so a long sweep could not be told apart from a stuck one.

The sweeps report "X of Y" every few seconds instead. Never per item: with
hundreds or thousands of chats that would flood the screen reader faster than
anyone could follow. And never for the last item, because the sweep's own
completion announcement follows right after it.
"""

import time

#: Seconds between two progress reports of the same sweep.
PROGRESS_INTERVAL_SECONDS = 5.0


class ProgressThrottle:
    """Counts finished items and says when the next report is due.

    Not thread-safe: call ``step()`` from the single thread that collects the
    results (the ``as_completed`` loop), never from the workers.
    """

    def __init__(self, total: int, interval: float = PROGRESS_INTERVAL_SECONDS,
                 clock=None):
        self.total = max(0, int(total))
        self.done = 0
        self._interval = interval
        self._clock = clock or time.monotonic
        self._last_report = self._clock()

    def step(self) -> bool:
        """Record one finished item; True when a report is due now."""
        self.done += 1
        if self.done >= self.total:
            # The sweep's completion announcement covers the end.
            return False
        now = self._clock()
        if now - self._last_report < self._interval:
            return False
        self._last_report = now
        return True
