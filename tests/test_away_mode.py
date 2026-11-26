"""Away mode: the countdown, its limits and what it leaves alone."""
from core.away_mode import (
    DEFAULT_MINUTES, MAX_MINUTES, MIN_MINUTES, AwayTracker, away_enabled, away_minutes,
)
from core.online_presence_diagnostics import PresenceDiagnostics


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make():
    clock = Clock()
    return clock, AwayTracker(clock)


def test_settings_defaults_and_limits():
    assert away_enabled({}) is True and away_minutes({}) == DEFAULT_MINUTES == 2
    assert away_enabled({"away_enabled": False}) is False
    assert away_minutes({"away_minutes": 0}) == MIN_MINUTES
    assert away_minutes({"away_minutes": 9999}) == MAX_MINUTES
    assert away_minutes({"away_minutes": "x"}) == DEFAULT_MINUTES


def test_away_only_after_the_time_and_one_interaction_ends_it():
    clock, tracker = make()
    clock.now = 119
    assert tracker.update(enabled=True, minutes=2) is None
    clock.now = 120
    assert tracker.update(enabled=True, minutes=2) == "away"
    assert tracker.update(enabled=True, minutes=2) is None
    assert tracker.touch() is True and not tracker.away
    assert tracker.touch() is False


def test_interaction_restarts_the_countdown():
    clock, tracker = make()
    clock.now = 100
    tracker.touch()
    clock.now = 219
    assert tracker.update(enabled=True, minutes=2) is None


def test_busy_counts_as_present_and_restarts_the_countdown():
    clock, tracker = make()
    clock.now = 500
    assert tracker.update(enabled=True, minutes=2, busy=True) is None
    clock.now = 600
    assert tracker.update(enabled=True, minutes=2) is None
    clock.now = 620
    assert tracker.update(enabled=True, minutes=2) == "away"


def test_turning_it_off_ends_away():
    clock, tracker = make()
    clock.now = 999
    assert tracker.update(enabled=True, minutes=2) == "away"
    assert tracker.update(enabled=False, minutes=2) == "back"
    assert not tracker.away


def test_away_makes_the_account_offline():
    flags = dict(connected=True, token=True, shutdown=False, background=False,
                 hidden=False, shown=True, iconized=False, active=True)
    assert PresenceDiagnostics().decide(**flags, now=0).online
    assert not PresenceDiagnostics().decide(**flags, away=True, now=0).online
