"""Bookmarks that remove themselves after the first jump.

Settings > Interface > Messages has one checkbox per kind: a permanent
(Ctrl+digit) or temporary (Alt+Shift+digit) bookmark is removed by the very
jump that used it, so the same key sets a new one the next time instead of
jumping to the old one. For someone who reuses the same digits and uses each
bookmark once, removing it by hand (Ctrl+Shift+digit) every time is redundant.

Off by default. Pure, no wx, so it is tested directly.
"""

SECTION = "user_interface"
REMOVE_BOOKMARKS_SETTING = "remove_bookmarks_after_use"
REMOVE_TEMP_BOOKMARKS_SETTING = "remove_temp_bookmarks_after_use"


def remove_after_use(settings, temporary: bool) -> bool:
    """Whether a bookmark of that kind goes away after its first jump."""
    section = settings.get(SECTION) if isinstance(settings, dict) else None
    if not isinstance(section, dict):
        return False
    key = REMOVE_TEMP_BOOKMARKS_SETTING if temporary else REMOVE_BOOKMARKS_SETTING
    return bool(section.get(key, False))
