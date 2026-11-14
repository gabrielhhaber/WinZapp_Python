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

#: Notifications let through per window, unless Settings > General >
#: Notifications says otherwise (`general.notification_burst_limit`).
BURST_LIMIT = 3
BURST_LIMIT_MIN = 1
BURST_LIMIT_MAX = 20
SETTINGS_KEY = "notification_burst_limit"
#: Length of the window, in seconds.
BURST_WINDOW_SECONDS = 1.0

_ATTR = "_notification_burst_limiter"


class NotificationBurstLimiter:
    """Sliding window over notification attempts. Main thread only."""

    def __init__(self, limit: int = BURST_LIMIT,
                 window: float = BURST_WINDOW_SECONDS, clock=time.monotonic):
        self.window = window
        self._clock = clock
        self._attempts = deque()
        self._refusing = False
        self.set_limit(limit)

    def set_limit(self, limit: int) -> None:
        """Change the limit, keeping the attempts already counted."""
        self.limit = limit
        # Only the newest `limit` attempts can decide the next answer.
        self._attempts = deque(self._attempts, maxlen=limit)

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


def burst_limit(settings) -> int:
    """The saved limit, kept within its bounds; anything else is the default."""
    general = settings.get("general") if isinstance(settings, dict) else None
    value = general.get(SETTINGS_KEY) if isinstance(general, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return BURST_LIMIT
    return max(BURST_LIMIT_MIN, min(BURST_LIMIT_MAX, value))


def notification_allowed(window) -> bool:
    """Whether *window* (a MainWindow or a test stub) may notify right now."""
    limit = burst_limit(getattr(window, "settings", None))
    limiter = getattr(window, _ATTR, None)
    if not isinstance(limiter, NotificationBurstLimiter):
        limiter = NotificationBurstLimiter(limit)
        try:
            setattr(window, _ATTR, limiter)
        except Exception:
            return True
    elif limiter.limit != limit:
        limiter.set_limit(limit)
    return limiter.allow()
