"""Where a chat row says how many new messages it has (issue 684).

Settings > Chats: before the chat's name ("3 unread messages Ana") or after
it, as it always was ("Ana 3 unread messages"). The conversations list, the
archived list, the locked chats and the live row refresh all build the row
through `name_with_unread`, so the four can never disagree.

Pure functions, tested in tests/test_chat_row_text.py.
"""

SETTING = "unread_count_position"
BEFORE, AFTER = "before", "after"
DEFAULT = AFTER


def unread_badge(unread, i18n) -> str:
    """'3 unread messages' / '1 unread message', or '' with none."""
    try:
        unread = int(unread or 0)
    except (TypeError, ValueError):
        return ""
    if unread <= 0:
        return ""
    return f"{unread} {i18n.t('unread_messages' if unread > 1 else 'unread_message')}"


def unread_position(settings) -> str:
    """The configured position; anything unknown is the default (after)."""
    ui = settings.get("user_interface") if isinstance(settings, dict) else None
    value = ui.get(SETTING) if isinstance(ui, dict) else None
    return BEFORE if value == BEFORE else AFTER


def name_with_unread(name_text, unread, i18n, position=DEFAULT) -> str:
    """*name_text* with the new-message count before or after it."""
    badge = unread_badge(unread, i18n)
    if not badge:
        return name_text
    if position == BEFORE:
        return f"{badge} {name_text}" if name_text else badge
    return f"{name_text} {badge}"
