"""At most a few message notifications per second.

A reconnect on a slow link can hand over hundreds of recent messages inside a
few seconds. Each one is new enough to notify, so each one played its sound:
reported as the notification sound repeating more than ten times in a row
(log of 2026-11-13: 212 notifications in 13 s, 89 of them in one second).

The limiter counts every *attempt*, refused ones included. So a burst gets its
first ``limit`` notifications and then stays quiet until the arrivals slow
down to under ``limit`` per ``window`` seconds, instead of clicking three
times a second for as long as the burst lasts. Nothing else is lost: the
messages are stored, counted as unread and shown in the chat list.
"""

import logging
import time
from collections import deque

#: Notifications let through per window.
BURST_LIMIT = 3
#: Length of the window, in seconds.
BURST_WINDOW_SECONDS = 1.0

_ATTR = "_notification_burst_limiter"


class NotificationBurstLimiter:
    """Sliding window over notification attempts. Main thread only."""

    def __init__(self, limit: int = BURST_LIMIT,
                 window: float = BURST_WINDOW_SECONDS, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self._clock = clock
        # Only the newest `limit` attempts can decide the next answer.
        self._attempts = deque(maxlen=limit)
        self._refusing = False

    def allow(self) -> bool:
        """Record one attempt and say whether it may notify."""
        now = self._clock()
        attempts = self._attempts
        while attempts and now - attempts[0] >= self.window:
            attempts.popleft()
        allowed = len(attempts) < self.limit
        attempts.append(now)
        if not allowed and not self._refusing:
            logging.info(
                "[notif-burst] more than %s notifications in %.1fs; staying "
                "quiet until the burst ends.", self.limit, self.window)
        self._refusing = not allowed
        return allowed


def notification_allowed(window) -> bool:
    """Whether *window* (a MainWindow or a test stub) may notify right now."""
    limiter = getattr(window, _ATTR, None)
    if not isinstance(limiter, NotificationBurstLimiter):
        limiter = NotificationBurstLimiter()
        try:
            setattr(window, _ATTR, limiter)
        except Exception:
            return True
    return limiter.allow()
