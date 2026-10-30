"""One error box per outage, for a failure a retry loop keeps hitting."""

import threading


class FailureNotice:
    """Decides when a repeating failure may show its modal box.

    The chat list fails once per sync, and the health checker restarts a sync
    for as long as WhatsApp is down. Each round queued its own wx.MessageBox
    behind the one on screen: a MacBook that slept with its browser page dead
    came back to 27 of them, long after the session had recovered, and its
    user pressed OK eleven times to clear the backlog (2026-10-29).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._reported = False

    def claim(self) -> bool:
        """True for the first failure of an outage; the caller shows the box."""
        with self._lock:
            if self._reported:
                return False
            self._reported = True
            return True

    def clear(self):
        """The operation worked: the outage is over, the next failure reports."""
        with self._lock:
            self._reported = False

    def still_failing(self) -> bool:
        """Read when the queued box is about to show: False once it recovered."""
        with self._lock:
            return self._reported
