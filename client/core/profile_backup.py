"""When the Chrome profile's restore point is refreshed — the user's policy.

core/profile_recovery.py owns HOW a snapshot is taken and why it may only be
taken from a profile Chrome has released. This module owns WHEN, as chosen in
Settings > Cópia de segurança (settings["profile_backup"]):

- close_snapshot_min_hours — a clean close refreshes the snapshot only when it
  is at least this old; 0 refreshes it on every clean close. The age of the
  snapshot is exactly how much WhatsApp Web forgets if it ever has to be put
  back, so a shorter interval trades a ~1 GB copy at close for less loss.
- live_snapshot_enabled / live_snapshot_interval_hours / live_snapshot_confirm
  — refresh it while WinZapp is open too. A live profile cannot be copied, so
  this closes the session, copies, and starts it again: a short disconnection
  every time, which is why it is off by default and can ask first.

Pure functions over the settings dict, so the policy is testable without wx.
"""

from core.profile_recovery import SNAPSHOT_MAX_AGE_SECONDS

SECTION = "profile_backup"

#: The historical refresh window, now the default of the setting.
DEFAULT_CLOSE_HOURS = SNAPSHOT_MAX_AGE_SECONDS // 3600
DEFAULT_LIVE_HOURS = 24


def stored_hours(value, default, minimum):
    """A whole number of hours not below `minimum`, else `default`.

    Forgiving on purpose: this reads what is already in settings.json, where
    anything unusable must fall back to the default rather than stop the app.
    Settings uses it too, so a field opens on the value WinZapp actually
    applies. What a person types goes through parse_hours_field() instead,
    which refuses rather than guesses."""
    if isinstance(value, bool):
        return default
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return default
    return hours if hours >= minimum else default


#: Smallest value each Settings field accepts: 0 means "every clean close" for
#: the close interval; the live interval has no such sentinel.
CLOSE_HOURS_MINIMUM = 0
LIVE_HOURS_MINIMUM = 1


def parse_hours_field(text, minimum):
    """The whole number of hours typed in a Settings field, or None when it is
    not one: empty, letters, a decimal, or below `minimum`. The dialog refuses
    to save on None, so nothing unusable ever reaches settings.json.

    Strict where stored_hours() is forgiving — do not merge them: a guess about
    stored data keeps the app working, a guess about typed text would save
    something the person did not mean."""
    if not isinstance(text, str):
        return None
    try:
        hours = int(text.strip())
    except ValueError:
        return None
    return hours if hours >= minimum else None


def _section(settings):
    section = (settings or {}).get(SECTION) if isinstance(settings, dict) else None
    return section if isinstance(section, dict) else {}


def close_snapshot_max_age(settings) -> int:
    """Seconds a snapshot may age before a clean close refreshes it."""
    hours = stored_hours(_section(settings).get("close_snapshot_min_hours"), DEFAULT_CLOSE_HOURS, 0)
    return hours * 3600


def live_snapshot_policy(settings):
    """(enabled, interval in seconds, ask first) for the refresh while open.

    The interval has no 0 sentinel: 0 would close the session on every poll,
    so anything below one hour falls back to the default."""
    section = _section(settings)
    enabled = section.get("live_snapshot_enabled", False) is True
    hours = stored_hours(section.get("live_snapshot_interval_hours"), DEFAULT_LIVE_HOURS, 1)
    confirm = section.get("live_snapshot_confirm", True) is not False
    return enabled, hours * 3600, confirm


def live_snapshot_due(enabled, interval_seconds, since_last_attempt, snapshot_age) -> bool:
    """Whether a refresh while open is due.

    Counted from the last attempt — a refresh, a refusal, or the launch — so a
    "No" in the confirmation postpones it by a whole interval rather than
    asking again at the next poll. A snapshot already younger than the
    interval (a clean close just refreshed it) makes it not due either.
    `snapshot_age` None means there is no snapshot at all, which is due.
    """
    if not enabled or interval_seconds <= 0:
        return False
    if since_last_attempt < interval_seconds:
        return False
    return snapshot_age is None or snapshot_age >= interval_seconds
