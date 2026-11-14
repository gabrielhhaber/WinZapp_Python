"""A chat cleared on the phone (app state `clearChat`, collection regular_high).

neonize has no event for it (whatsmeow's events.ClearChat is not forwarded),
but WinZapp's build emits AppStateMutationEv for every mutation
(app_state_mirror.py): its index is ["clearChat", chat, delete starred "1"/"0",
delete media "1"/"0"] and its action's clearChatAction.messageRange says the
newest message the clear covers. The bridge deletes those messages from its
store and tells the client with a `chat-cleared` socket event
(client/core/remote_chat_clear.py), the event the WPPConnect page sends too.

A full sync replays every old mutation: a clear from it is history, not news,
and is dropped (the same rule as phone_chat_delete.py). A removed mutation
undoes nothing on the messages.

The pure functions are at the top (tested in test_phone_chat_clear.py).
"""
import logging

import msgmap

log = logging.getLogger("bridge")

INDEX_NAME = "clearChat"


def cleared(index, remove, from_full_sync, action):
    """(chat, until seconds, keep starred) of a clearChat mutation that is news,
    or None. *action* is the mutation's SyncActionValue (or None)."""
    index = list(index or ())
    if remove or from_full_sync or len(index) < 2 or index[0] != INDEX_NAME:
        return None
    chat = str(index[1] or "")
    if "@" not in chat:
        return None
    keep_starred = not (len(index) > 2 and index[2] == "1")
    until = 0
    clear = getattr(action, "clearChatAction", None) if action is not None else None
    if clear is not None and action.HasField("clearChatAction") \
            and clear.HasField("messageRange"):
        until = msgmap.seconds(clear.messageRange.lastMessageTimestamp)
    if not until and action is not None:
        until = msgmap.seconds(getattr(action, "timestamp", 0))
    if not until:
        return None
    return chat, until, keep_starred


def shown_chat(chat, keys):
    """The address the client knows the chat by: the number when the store
    holds it under one (the mutation's index is often the LID)."""
    for key in keys or ():
        if str(key).endswith("@s.whatsapp.net"):
            return key
    return (keys or [chat])[0]


def payload(session, chat, until, keep_starred):
    return {"session": session, "chatId": msgmap.jid_wpp(chat), "until": int(until),
            "keepStarred": bool(keep_starred)}


def handle(s, index, remove, from_full_sync, action) -> bool:
    """Applies one mutation if it is a clear: the store first, then the event.
    Runs on neonize's thread."""
    found = cleared(index, remove, from_full_sync, action)
    if found is None:
        return False
    chat, until, keep_starred = found
    keys = []
    try:
        keys = list(s.chat_keys(chat) or ())
    except Exception:
        keys = []
    for key in keys or [chat]:
        s.db.clear_chat(key, keep_starred=keep_starred, until=until)
    s.emit("chat-cleared", payload(s.name, shown_chat(chat, keys), until, keep_starred))
    log.info("chat cleared on the phone -> app")
    return True
