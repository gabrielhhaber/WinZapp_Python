"""Settings > General > Notifications: how many notifications a burst may play
per second (`general.notification_burst_limit`, default 3)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.notification_burst import (
    BURST_LIMIT, BURST_LIMIT_MAX, BURST_LIMIT_MIN, SETTINGS_KEY,
    NotificationBurstLimiter, burst_limit, notification_allowed,
)
from core.utils import DEFAULT_SETTINGS

CLIENT = Path(__file__).resolve().parents[1] / "client"


def test_the_default_is_three_everywhere_it_is_written():
    shipped = json.loads((CLIENT / "data" / "settings_default.json").read_text("utf-8"))
    assert BURST_LIMIT == 3
    assert shipped["general"][SETTINGS_KEY] == BURST_LIMIT
    assert DEFAULT_SETTINGS["general"][SETTINGS_KEY] == BURST_LIMIT


@pytest.mark.parametrize("saved,expected", [
    (1, 1), (3, 3), (10, 10), (BURST_LIMIT_MAX, BURST_LIMIT_MAX),
    (0, BURST_LIMIT_MIN), (-5, BURST_LIMIT_MIN), (999, BURST_LIMIT_MAX),
    (None, BURST_LIMIT), ("7", BURST_LIMIT), (2.5, BURST_LIMIT), (True, BURST_LIMIT),
])
def test_a_saved_limit_is_kept_within_bounds(saved, expected):
    assert burst_limit({"general": {SETTINGS_KEY: saved}}) == expected


@pytest.mark.parametrize("settings", [None, {}, {"general": None}, {"general": {}}, []])
def test_settings_without_the_key_mean_the_default(settings):
    assert burst_limit(settings) == BURST_LIMIT


@pytest.mark.parametrize("limit", [1, 5])
def test_the_window_notifies_as_many_times_as_the_setting_says(limit):
    window = SimpleNamespace(settings={"general": {SETTINGS_KEY: limit}})
    answers = [notification_allowed(window) for _ in range(limit + 4)]
    assert answers == [True] * limit + [False] * 4


def test_changing_the_setting_applies_to_the_next_notification():
    now = [0.0]
    window = SimpleNamespace(settings={"general": {SETTINGS_KEY: 1}})
    window._notification_burst_limiter = NotificationBurstLimiter(1, clock=lambda: now[0])
    assert notification_allowed(window)
    assert not notification_allowed(window)
    limiter = window._notification_burst_limiter

    window.settings["general"][SETTINGS_KEY] = 4
    now[0] = 5.0                            # a new burst
    assert [notification_allowed(window) for _ in range(6)] == [True] * 4 + [False] * 2
    assert window._notification_burst_limiter is limiter

    window.settings["general"][SETTINGS_KEY] = 2
    now[0] = 10.0
    assert [notification_allowed(window) for _ in range(4)] == [True] * 2 + [False] * 2


def test_lowering_the_limit_in_the_middle_of_a_burst_keeps_it_quiet():
    now = [0.0]
    limiter = NotificationBurstLimiter(5, clock=lambda: now[0])
    assert all(limiter.allow() for _ in range(5))
    limiter.set_limit(2)
    assert not limiter.allow()
