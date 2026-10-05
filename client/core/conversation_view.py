"""Whether the open conversation is the one being shown.

ConversationsPanel keeps its conversation open while the user is on a chat
list (Alt+1, Alt+4, ...), but hidden: a plain panel switch never shows it
(showing it cost a perceptible delay). Only an explicit ask for it (Alt+M,
Alt+2, Alt+3) or opening a chat brings it back. An open conversation that is
hidden is not being read, so it must not be marked read, notified as "the
current chat" or kept out of the unread counts.
"""

MAIN, ARCHIVED, LOCKED = "main", "archived", "locked"


def conversation_in_view(panel) -> bool:
    """True when `panel` has a conversation open and it is on screen.

    IsShown() of the panel, not IsShownOnScreen(): the panel is a direct child
    of the content area and the panel switches Hide() it, while a window
    hidden to the tray or minimized must keep counting as "open" (the unread
    bookkeeping for a hidden window relies on that). The detail pane counts
    too: a conversation is shown only while the panel it was opened from is
    the visible one (see conversation_visible_in()), and the others hide just
    that pane, leaving the panel itself shown. A stand-in without either
    method counts as shown, so callers keep their old behaviour.
    """
    if panel is None or getattr(panel, "conversation", None) is None:
        return False
    for widget in (panel, getattr(panel, "conversation_panel", None)):
        shown = getattr(widget, "IsShown", None)
        if shown is None:
            continue
        try:
            if not shown():
                return False
        except RuntimeError:  # wx object already destroyed
            return False
    return True


def conversation_visible_in(origin, shown_panel) -> bool:
    """The one rule: an open conversation is on screen only while the panel it
    was opened from (main, archived or locked) is the panel being shown."""
    return origin is not None and origin == shown_panel


def panel_layout(origin, shown_panel, has_conversation, reveal=False, keep=False) -> dict:
    """What is on screen when `shown_panel` (MAIN, ARCHIVED or LOCKED) is the
    chat list being shown. One answer for every way of getting there.

    detail      the open conversation's pane: never shown by a switch from
                another panel; shown on an explicit reveal, and kept (`keep`:
                it is on screen right now) when Alt+1/Alt+4 is pressed inside
                the very panel the conversation belongs to, where nothing was
                switched; in every case only in the panel it belongs to
    own_list    ConversationsPanel's own chat list: it is the MAIN list, so it
                is hidden whenever the conversation sits under another list
    panel       ConversationsPanel itself (the main list and/or the detail)
    list_panel  the archived or locked list: always shown for its own panel,
                the conversation (if any) sitting beneath it
    """
    detail = ((reveal or keep) and has_conversation
              and conversation_visible_in(origin, shown_panel))
    return {
        "detail": detail,
        "own_list": shown_panel == MAIN or not detail,
        "panel": shown_panel == MAIN or detail,
        "list_panel": shown_panel != MAIN,
    }


def resolve_origin(requested, current_origin, list_shown, detail_shown) -> str:
    """The panel a conversation being opened belongs to.

    An explicit request wins. Otherwise a chat opened from inside a
    conversation that sits alone in the panel (the chat list hidden, as it is
    for an archived or locked one: a bookmark, a mention) stays with that
    panel; anything else is the main panel's, an archived chat found through
    the main search box included.
    """
    if requested:
        return requested
    if current_origin in (ARCHIVED, LOCKED) and detail_shown and not list_shown:
        return current_origin
    return MAIN


def mnemonic_letter(label, default) -> str:
    """The letter after the "&" of a label ("&Messages" -> "M"), or `default`."""
    amp = label.find("&")
    if 0 <= amp < len(label) - 1 and label[amp + 1].isalpha():
        return label[amp + 1].upper()
    return default


def archived_chat_stays_silent(is_current_conv: bool, archived_panel_shown: bool) -> bool:
    """An archived chat announces only while it is the conversation in view or
    the archived list itself is on screen; from any other panel it is silent
    (neither the current-chat nor the foreground sound)."""
    return not is_current_conv and not archived_panel_shown


def archived_panel_is_shown(main_window) -> bool:
    panel = getattr(main_window, "archived_conversations_panel", None)
    if panel is None:
        return False
    try:
        return bool(panel.IsShown())
    except RuntimeError:  # wx object already destroyed
        return False
