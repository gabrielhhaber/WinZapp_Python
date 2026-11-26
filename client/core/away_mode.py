"""Away mode: after N minutes without a keystroke or click, the person is away.

Plain logic, no wx. ``AwayTracker`` keeps the time of the last interaction with
WinZapp (any of its windows) and says whether the person counts as away; the
MainWindow side that feeds it and acts on it is ``main_window/away.py``.

While away the account is shown offline and the messages that land in the open
conversation are not marked as read. The first interaction afterwards ends it.
A call, a recording or a playing audio keeps the person present: someone
listening to a long voice message does not touch the keyboard.
"""
import time

ENABLED_KEY = "away_enabled"
MINUTES_KEY = "away_minutes"
DEFAULT_ENABLED = True
DEFAULT_MINUTES = 2
MIN_MINUTES = 1
MAX_MINUTES = 120


def away_enabled(settings) -> bool:
    """The option as saved in the ``user_interface`` settings (on by default)."""
    return bool((settings or {}).get(ENABLED_KEY, DEFAULT_ENABLED))


def away_minutes(settings) -> int:
    """Minutes of inactivity before the person is away, kept inside the range."""
    try:
        value = int((settings or {}).get(MINUTES_KEY, DEFAULT_MINUTES))
    except (TypeError, ValueError):
        return DEFAULT_MINUTES
    return max(MIN_MINUTES, min(MAX_MINUTES, value))


class AwayTracker:
    """Last interaction and the away state derived from it."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last = clock()
        self._away = False

    @property
    def away(self) -> bool:
        return self._away

    def touch(self) -> bool:
        """Record an interaction. True when it ended an away period."""
        self._last = self._clock()
        was_away, self._away = self._away, False
        return was_away

    def update(self, *, enabled: bool, minutes: int, busy: bool = False):
        """Re-evaluate the state; "away", "back" or None when nothing changed.

        ``busy`` (a call, a recording, an audio playing) counts as presence and
        restarts the countdown, so it begins when the busy thing ends. Turning
        the option off ends an away period too.
        """
        if not enabled or busy:
            self._last = self._clock()
            return "back" if self._set(False) else None
        idle = self._clock() - self._last
        if idle >= minutes * 60:
            return "away" if self._set(True) else None
        return None

    def _set(self, value: bool) -> bool:
        changed = self._away != value
        self._away = value
        return changed
