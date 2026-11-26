"""The keys of the pinned messages window (issue #848), without wx.

The window lists a chat's pinned messages over the conversation and acts on
them with the message list's own shortcuts, read from the person's bindings
(core/shortcut_catalog.py). A command that only reads or changes a message
runs with the window open; one that takes the person into the conversation
(reply, edit, the quoted message, the sender's chat) closes it on the message
first, since the composer behind a modal window cannot take the focus.
"""

from contextlib import contextmanager

#: run on the pinned message with the window open
IN_PLACE = (
    "messages.ID_ALT_SHIFT_D",          # message data
    "messages.ID_CTRL_C",               # copy (text, file or number; the selection)
    "messages.ID_CTRL_SHIFT_C",         # copy caption
    "messages.ID_ALT_C",                # show text
    "messages.ID_ALT_L",                # read more
    "messages.ID_ALT_SHIFT_L",          # status
    "messages.ID_ALT_SHIFT_K",          # date
    "messages.ID_ALT_SHIFT_C",          # copy the sender's number
    "messages.ID_CTRL_SHIFT_E",         # forward
    "messages.ID_CTRL_SHIFT_R",         # react (return a missed call)
    "messages.ID_CTRL_SHIFT_O",         # star
    "messages.ID_CTRL_SHIFT_P",         # pin / unpin (the selection too)
    "messages.ID_CTRL_SHIFT_S",         # save as
    "messages.ID_CTRL_SHIFT_Y",         # try sending again
    "messages.ID_DELETE_MSG",           # delete (the selection too)
    "messages.ID_ALT_SHIFT_T",          # transcribe
    "messages.ID_ALT_SHIFT_N",          # transcribe into the line / play
    "messages.ID_ALT_SHIFT_DELETE",     # delete the transcription
    "messages.ID_ALT_COMMA",            # audio speed
    "messages.ID_ALT_PERIOD",
    "messages.ID_SUMMARIZE_FROM_HERE",
    "messages.ID_SUMMARIZE_SELECTED",
    "messages.ID_TRANSLATE_FROM_HERE",
    "messages.ID_TRANSLATE_SELECTED",
    "messages.ID_BULK_COPY",
    "messages.ID_BULK_FORWARD",
    "messages.ID_BULK_STAR",
    "messages.ID_BULK_PIN",
    "messages.ID_BULK_SAVE",
    "messages.ID_BULK_TRANSCRIBE",
    "messages.ID_BULK_TRANSCRIBE_INLINE",
    "messages.ID_BULK_DELETE_TRANSCRIPTIONS",
    "messages.ID_BULK_DELETE",
    *(f"messages.ID_QUICK_REACTION[{digit}]" for digit in range(10)),
)

#: close the window, focus the message in the conversation, then run
LEAVING = (
    "messages.ID_ALT_R",                # reply
    "messages.ID_ALT_SHIFT_R",          # reply privately
    "messages.ID_ALT_SHIFT_V",          # the sender's chat
    "messages.ID_ALT_E",                # edit
    "messages.ID_ALT_SHIFT_Q",          # the quoted message
)

#: the AI action reads the focused control, so the window hands it the message
AI_ACTION = "messages.ID_AI_ACTION"

#: the message list's selection keys, applied to the window's rows
SELECT = "message_list.select"
SELECT_ALL = "message_list.select_all"
SELECT_NEXT = "message_list.select_next"
SELECT_PREVIOUS = "message_list.select_previous"
SHOW_FOLDER = "message_list.show_folder"
SELECTION_KEYS = (SELECT, SELECT_ALL, SELECT_NEXT, SELECT_PREVIOUS, SHOW_FOLDER)


def command_of(shortcut_id):
    """The panel attribute behind a catalog id: ``messages.ID_ALT_R`` ->
    ``ID_ALT_R``."""
    return shortcut_id.split(".", 1)[1]


def key_action(matches):
    """What a key does in the window: ``(kind, shortcut_id)`` with kind
    ``select``, ``ai``, ``leave`` or ``run``; None when it is not one of the
    message list's. *matches(shortcut_id)* answers with the person's own
    bindings."""
    for shortcut_id in SELECTION_KEYS:
        if matches(shortcut_id):
            return "select", shortcut_id
    if matches(AI_ACTION):
        return "ai", AI_ACTION
    for shortcut_id in LEAVING:
        if matches(shortcut_id):
            return "leave", shortcut_id
    for shortcut_id in IN_PLACE:
        if matches(shortcut_id):
            return "run", shortcut_id
    return None


def message_id(message):
    return ((message or {}).get("key") or {}).get("id", "")


def visible_pins(pins, gone_ids=()):
    """The rows of the window: each pinned message once, without the ones
    deleted while it was open (a pin outlives its message until the next
    read of the chat's pins)."""
    seen, rows = set(gone_ids), []
    for message in pins or ():
        mid = message_id(message)
        if mid and mid not in seen:
            seen.add(mid)
            rows.append(message)
    return rows


def toggle_all(row_ids, selected):
    """Ctrl+Shift+Space on the window: (ids to select, ids to unselect).
    Every row selected already: unselect them; otherwise select the rest."""
    row_ids = [mid for mid in row_ids if mid]
    if row_ids and all(mid in selected for mid in row_ids):
        return [], row_ids
    return [mid for mid in row_ids if mid not in selected], []


def kept_index(rows, keep_id, fallback):
    """The row to stay on after the list is rebuilt: the same message, or the
    one that took the place of a removed row."""
    for index, message in enumerate(rows):
        if keep_id and message_id(message) == keep_id:
            return index
    if not rows:
        return -1
    return min(max(fallback, 0), len(rows) - 1)


@contextmanager
def selection_within(holder, row_ids):
    """While the window runs an action: `holder.selected_messages` holds only
    what is selected among the window's rows, so Copy, Unpin or Delete on a
    pin never reach messages selected in the conversation and not shown here.
    The ones left out come back afterwards; the action's own changes to the
    window's part (a selection cleared, a message deleted) are kept.
    Yields the ids left out."""
    rows = {mid for mid in row_ids if mid}
    outside = set(holder.selected_messages) - rows
    holder.selected_messages.difference_update(outside)
    try:
        yield outside
    finally:
        holder.selected_messages.update(outside)
