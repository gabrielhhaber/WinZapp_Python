"""Optional evaluation cadence; sleep/late ticks never produce catch-up jobs."""
from __future__ import annotations


class Schedule:
    def __init__(self):
        self.started = False
        self.last = None
        self.signature = None

    def due(self, preferences, now, *, busy=False):
        signature = (preferences.scan_on_start, preferences.interval_minutes)
        if self.signature is not None and signature != self.signature:
            # Enabling/editing an interval does not replay missed evaluations.
            self.last = now
        self.signature = signature
        if self.last is None:
            self.last = now
        startup = not self.started and preferences.scan_on_start
        interval = preferences.interval_minutes * 60
        periodic = interval > 0 and now - self.last >= interval
        if busy or not (startup or periodic):
            if not preferences.scan_on_start:
                self.started = True
            return False
        self.started = True
        self.last = now
        return True
