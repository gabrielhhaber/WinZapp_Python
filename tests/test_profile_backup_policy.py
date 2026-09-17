"""The profile backup policy (core/profile_backup.py): how Settings >
Cópia de segurança turns into "refresh the restore point now or not"."""

import pytest

from core import profile_backup
from core.profile_recovery import SNAPSHOT_MAX_AGE_SECONDS
from core.utils import DEFAULT_SETTINGS

HOUR = 3600


class TestDefaults:
    def test_the_shipped_defaults_match_the_module(self):
        section = DEFAULT_SETTINGS["profile_backup"]
        assert section["close_snapshot_min_hours"] == profile_backup.DEFAULT_CLOSE_HOURS
        assert section["live_snapshot_interval_hours"] == profile_backup.DEFAULT_LIVE_HOURS
        assert section["live_snapshot_enabled"] is False
        assert section["live_snapshot_confirm"] is True

    def test_the_close_default_is_the_old_fixed_window(self):
        """An install that never opens the new tab keeps behaving as before."""
        assert profile_backup.close_snapshot_max_age({}) == SNAPSHOT_MAX_AGE_SECONDS
        assert profile_backup.close_snapshot_max_age(DEFAULT_SETTINGS) == SNAPSHOT_MAX_AGE_SECONDS


class TestStoredHours:
    """The forgiving reader for settings.json — also what Settings opens on."""

    @pytest.mark.parametrize("value, minimum, expected", [
        (6, 0, 6), ("6", 0, 6), (1.5, 1, 1), (0, 0, 0),
        (0, 1, 24), (-3, 0, 24), (None, 0, 24), ("abc", 1, 24), (True, 0, 24),
    ])
    def test_falls_back_rather_than_fails(self, value, minimum, expected):
        assert profile_backup.stored_hours(value, 24, minimum) == expected


class TestParseHoursField:
    """What Settings accepts in the two hour fields; None refuses OK/Apply."""

    @pytest.mark.parametrize("text, minimum, hours", [
        ("0", 0, 0), ("24", 0, 24), (" 6 ", 0, 6), ("1", 1, 1), ("48", 1, 48),
    ])
    def test_a_whole_number_of_hours_is_accepted(self, text, minimum, hours):
        assert profile_backup.parse_hours_field(text, minimum) == hours

    @pytest.mark.parametrize("text", ["", "   ", "abc", "12h", "1.5", "1,5", "doze", "-", "2 4"])
    def test_anything_that_is_not_a_whole_number_is_refused(self, text):
        assert profile_backup.parse_hours_field(text, 0) is None
        assert profile_backup.parse_hours_field(text, 1) is None

    def test_below_the_minimum_is_refused(self):
        assert profile_backup.parse_hours_field("-1", profile_backup.CLOSE_HOURS_MINIMUM) is None
        assert profile_backup.parse_hours_field("0", profile_backup.LIVE_HOURS_MINIMUM) is None

    def test_zero_means_every_close_only_for_the_close_field(self):
        assert profile_backup.parse_hours_field("0", profile_backup.CLOSE_HOURS_MINIMUM) == 0

    def test_not_text_is_refused(self):
        assert profile_backup.parse_hours_field(None, 0) is None
        assert profile_backup.parse_hours_field(5, 0) is None


class TestCloseSnapshotMaxAge:
    @pytest.mark.parametrize("hours, seconds", [(0, 0), (1, HOUR), (6, 6 * HOUR), (48, 48 * HOUR)])
    def test_hours_become_seconds(self, hours, seconds):
        settings = {"profile_backup": {"close_snapshot_min_hours": hours}}
        assert profile_backup.close_snapshot_max_age(settings) == seconds

    @pytest.mark.parametrize("bad", [-1, "abc", None, True, [], 1.5j])
    def test_an_unusable_value_falls_back_to_the_default(self, bad):
        settings = {"profile_backup": {"close_snapshot_min_hours": bad}}
        assert profile_backup.close_snapshot_max_age(settings) == SNAPSHOT_MAX_AGE_SECONDS

    def test_a_numeric_string_is_accepted(self):
        assert profile_backup.close_snapshot_max_age(
            {"profile_backup": {"close_snapshot_min_hours": "3"}}) == 3 * HOUR

    def test_a_malformed_section_is_ignored(self):
        assert profile_backup.close_snapshot_max_age({"profile_backup": "x"}) == SNAPSHOT_MAX_AGE_SECONDS
        assert profile_backup.close_snapshot_max_age(None) == SNAPSHOT_MAX_AGE_SECONDS


class TestLiveSnapshotPolicy:
    def test_off_by_default(self):
        assert profile_backup.live_snapshot_policy({}) == (False, 24 * HOUR, True)

    def test_reads_the_three_options(self):
        settings = {"profile_backup": {"live_snapshot_enabled": True,
                                       "live_snapshot_interval_hours": 3,
                                       "live_snapshot_confirm": False}}
        assert profile_backup.live_snapshot_policy(settings) == (True, 3 * HOUR, False)

    @pytest.mark.parametrize("bad", [0, -2, "x", None])
    def test_an_interval_below_one_hour_falls_back(self, bad):
        """0 would close the session on every poll."""
        settings = {"profile_backup": {"live_snapshot_enabled": True,
                                       "live_snapshot_interval_hours": bad}}
        assert profile_backup.live_snapshot_policy(settings)[1] == 24 * HOUR

    def test_only_a_real_true_enables_it(self):
        """Closing the session is not something a stray truthy value may turn on."""
        assert profile_backup.live_snapshot_policy(
            {"profile_backup": {"live_snapshot_enabled": "yes"}})[0] is False


class TestLiveSnapshotDue:
    def test_never_when_disabled(self):
        assert not profile_backup.live_snapshot_due(False, HOUR, 10 * HOUR, None)

    def test_not_before_an_interval_since_the_last_attempt(self):
        assert not profile_backup.live_snapshot_due(True, HOUR, HOUR - 1, None)

    def test_due_with_no_snapshot_at_all(self):
        assert profile_backup.live_snapshot_due(True, HOUR, HOUR, None)

    def test_not_due_while_the_snapshot_is_younger_than_the_interval(self):
        """A clean close refreshed it a moment ago."""
        assert not profile_backup.live_snapshot_due(True, HOUR, 5 * HOUR, HOUR - 1)

    def test_due_once_the_snapshot_is_as_old_as_the_interval(self):
        assert profile_backup.live_snapshot_due(True, HOUR, 5 * HOUR, HOUR)
