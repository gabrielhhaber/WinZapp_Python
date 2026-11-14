"""Where the messages list lands when a conversation is opened.

Settings > Interface > Conversations, "Posição focada automaticamente na lista
de mensagens", chooses between three (`mode()`):

* UNREAD_OR_LAST — the default, and how WinZapp always behaved: the unread
  separator when there is one, the newest message otherwise;
* LAST_FOCUSED — the message the user was on when they left the conversation
  (closed it or switched to another one), new unread messages or not;
* LAST_FOCUSED_OR_UNREAD — the message the user was on when they left it in
  the middle of the conversation; when they had reached its last message,
  as UNREAD_OR_LAST: the unread separator if new messages arrived since
  (asked for 2026-11-12). `resume_on_open()`.

The setting chooses the row only. Whether the keyboard focus goes to the list
or to the message field when a conversation opens is "focus_on_open", a
different setting, and this one never changes it.

Remembered in memory only, for this run, per conversation.

Pure, no wx, so it is tested directly.
"""

SECTION = "user_interface"
SETTING = "message_list_open_position"

UNREAD_OR_LAST = "unread_or_last"
LAST_FOCUSED = "last_focused"
LAST_FOCUSED_OR_UNREAD = "last_focused_or_unread"
#: In the order the Settings choice lists them.
MODES = (UNREAD_OR_LAST, LAST_FOCUSED, LAST_FOCUSED_OR_UNREAD)
DEFAULT = UNREAD_OR_LAST

#: Rows in a list that are not messages (core of _is_separator()).
_NON_MESSAGE_TYPES = ("unread_separator", "empty_placeholder")

#: Bounds how far back reopening a conversation fetches from the database to
#: reach the remembered message. Above it, the default landing applies.
MAX_RESUME_FETCH = 5000


def _section(settings) -> dict:
    section = settings.get(SECTION) if isinstance(settings, dict) else None
    return section if isinstance(section, dict) else {}


def mode(settings) -> str:
    """The configured mode; DEFAULT for a missing or unknown one."""
    value = _section(settings).get(SETTING)
    return value if value in MODES else DEFAULT


def resumes_last_focused(settings) -> bool:
    """Whether the list goes back to where the user was, in some case."""
    return mode(settings) in (LAST_FOCUSED, LAST_FOCUSED_OR_UNREAD)


def resume_on_open(settings, left_at_end: bool) -> bool:
    """Whether opening a conversation goes back to the remembered message:
    always with LAST_FOCUSED; with LAST_FOCUSED_OR_UNREAD only when it was
    left in the middle (at its last message, the usual landing applies)."""
    chosen = mode(settings)
    if chosen == LAST_FOCUSED:
        return True
    return chosen == LAST_FOCUSED_OR_UNREAD and not left_at_end


def is_last_message(rows: list, msg_id: str) -> bool:
    """Whether *msg_id* is the newest message of *rows* (oldest first)."""
    for row in reversed(rows or ()):
        if _is_message(row):
            return row["key"]["id"] == msg_id
    return False


def _is_message(row) -> bool:
    return (isinstance(row, dict) and row.get("_type") not in _NON_MESSAGE_TYPES
            and bool((row.get("key") or {}).get("id")))


def resume_message_id(rows: list, focused_idx: int) -> str:
    """The id of the message to come back to, given the list's rows and the
    row the cursor was on when the conversation was left; "" for none.

    Only a message can be remembered: a row that is not one never stands for a
    reading position by itself.

    * On the unread separator, the user had read everything above it and
      nothing below: the message right after it, the first one not yet read,
      is where reading continues. (Reopened, that separator is usually gone,
      since opening marked the conversation as read.)
    * Past the end (the "X is typing..." row, which is not in ``rows``), the
      newest message.
    * On the "no messages" placeholder, or nowhere at all: nothing.
    """
    if not rows or focused_idx is None or focused_idx < 0:
        return ""
    if focused_idx >= len(rows):
        candidates = reversed(rows)
    else:
        row = rows[focused_idx]
        if _is_message(row):
            return row["key"]["id"]
        if isinstance(row, dict) and row.get("_type") == "unread_separator":
            # First message below; if none, the last one above.
            candidates = list(rows[focused_idx + 1:]) + list(reversed(rows[:focused_idx]))
        else:
            return ""
    for row in candidates:
        if _is_message(row):
            return row["key"]["id"]
    return ""


def _timestamp(record) -> int:
    try:
        return int((record or {}).get("messageTimestamp") or 0)
    except (TypeError, ValueError):
        return 0


def records_depth(records: list, msg_id: str) -> int:
    """How many of the conversation's records are as new as *msg_id* or newer:
    how many rows the database has to return, newest first, to reach it.
    0 when the message is not among them."""
    target = None
    for record in records or ():
        if isinstance(record, dict) and (record.get("key") or {}).get("id") == msg_id:
            target = record
            break
    if target is None:
        return 0
    ts = _timestamp(target)
    return sum(1 for r in records if isinstance(r, dict) and _timestamp(r) >= ts)


def resume_fetch_limit(base_limit: int, depth: int, unread_count: int,
                       buffer: int = 50, cap: int = MAX_RESUME_FETCH) -> int:
    """The database fetch that also reaches a remembered message *depth*
    records back, plus whatever arrived since (*unread_count*). Never less
    than *base_limit*; a depth beyond *cap* is not chased."""
    try:
        depth = int(depth)
        unread = max(0, int(unread_count or 0))
    except (TypeError, ValueError):
        return base_limit
    if depth <= 0:
        return base_limit
    wanted = depth + unread + buffer
    if wanted > cap:
        return base_limit
    return max(base_limit, wanted)
