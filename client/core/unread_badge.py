"""Which conversations count toward the unread tally of the window title and
the tray icon.

Two options of Settings > Notifications decide it: archived and muted
conversations can each be left out of that tally. Both default to left out.
"""

DISCOUNT_ARCHIVED_KEY = "discount_archived_from_unread_badge"
DISCOUNT_MUTED_KEY = "discount_muted_from_unread_badge"


def badge_discount_flags(settings) -> tuple[bool, bool]:
    """(discount_archived, discount_muted) from the settings, both True when unset."""
    general = (settings or {}).get("general", {}) or {}
    return (
        bool(general.get(DISCOUNT_ARCHIVED_KEY, True)),
        bool(general.get(DISCOUNT_MUTED_KEY, True)),
    )


def counts_toward_badge(settings, *, archived: bool, muted: bool) -> bool:
    """False when the conversation is archived/muted and that kind is discounted."""
    discount_archived, discount_muted = badge_discount_flags(settings)
    if archived and discount_archived:
        return False
    if muted and discount_muted:
        return False
    return True
