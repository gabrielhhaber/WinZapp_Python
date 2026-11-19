"""How many messages a conversation shows when it opens.

``messages_page_size`` is a user setting, and it drives the window the
conversation list is built with: every row is a native list item the screen
reader is told about. Set to tens of thousands it froze the interface for
over a minute whenever many messages arrived at once or the window was
rebuilt (issue #812). Nothing is gained above a thousand rows: older history
is loaded on demand with Home, so the setting has a ceiling.
"""

DEFAULT_MESSAGES_PAGE_SIZE = 200
MAX_MESSAGES_PAGE_SIZE = 1000


def clamp_messages_page_size(value) -> int:
    """*value* as a usable page size: an int from 1 to the ceiling, or the
    default when it is not a number at all."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MESSAGES_PAGE_SIZE
    return max(1, min(number, MAX_MESSAGES_PAGE_SIZE))


def messages_page_size(settings) -> int:
    """The configured page size, bounded. Every reader of the setting goes
    through here, so a value saved before the ceiling existed (or edited into
    settings.json by hand) cannot reach the list."""
    try:
        value = settings.get("user_interface", {}).get(
            "messages_page_size", DEFAULT_MESSAGES_PAGE_SIZE)
    except AttributeError:
        return DEFAULT_MESSAGES_PAGE_SIZE
    return clamp_messages_page_size(value)
