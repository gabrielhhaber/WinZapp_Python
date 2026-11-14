"""A chat cleared on another device (the phone, WhatsApp Web elsewhere).

Users reported that clearing a chat on the phone left it full in WinZapp (the
Telezapp client mirrors it). WhatsApp sends the clear as an app state
mutation (`clearChat`, collection regular_high): index [jid, delete starred,
delete media], with the newest message it covers in
``clearChatAction.messageRange.lastMessageTimestamp``. Both engines turn it
into the same ``chat-cleared`` socket event: the WPPConnect page from
WhatsApp Web's own ClearChatSync (chatClearedBridge.ts), the Neonize bridge
from AppStateMutationEv (neonize_bridge/phone_chat_clear.py).

Here the conversation loses what the clear covered: every message up to
``until`` (a message that arrived after it stays), starred ones kept unless
the phone cleared them too. The cutoff is recorded the same way a clear made
here records it, so no sync path brings the cleared messages back. A replay
of an old clear (a full app state sync) is dropped by the engines, never
reaching this module.
"""
import logging

# A clear names the newest message it covers by its second; one sent in the
# same second as the clear's own timestamp may be a few seconds off it.
SLACK_SECONDS = 2


def chat_cleared_payload(data):
    """(jid, until, keep_starred) of a ``chat-cleared`` event, or None."""
    if not isinstance(data, dict):
        return None
    jid = data.get("chatId")
    if not isinstance(jid, str) or "@" not in jid or len(jid) > 128:
        return None
    if jid.endswith(("@broadcast", "@newsletter")):
        return None
    until = data.get("until")
    if isinstance(until, bool) or not isinstance(until, (int, float)) or until <= 0:
        return None
    keep = data.get("keepStarred")
    return jid, int(until), keep is not False


def message_seconds(msg) -> int:
    try:
        return int((msg or {}).get("messageTimestamp", 0) or 0)
    except (TypeError, ValueError):
        return 0


def kept_after_clear(records, until, keep_starred):
    """The records a clear up to *until* leaves: the newer ones, and the
    starred ones when the clear kept them."""
    kept = []
    for msg in records or ():
        if not isinstance(msg, dict):
            continue
        if message_seconds(msg) > until + SLACK_SECONDS:
            kept.append(msg)
        elif keep_starred and msg.get("starred"):
            kept.append(msg)
    return kept


def apply_remote_clear(mw, jid, until, keep_starred):
    """Main thread: empty *jid* the way the other device did."""
    normalize = getattr(mw, "_normalize_jid", None)
    if callable(normalize):
        jid = normalize(jid) or jid
    chat = mw.chats.get(jid)
    if not isinstance(chat, dict):
        # the clear names the chat by its LID or its number: the other one
        other = (getattr(mw, "_lid_to_phone", None) or {}).get(jid) \
            or (getattr(mw, "_phone_to_lid", None) or {}).get(jid)
        if other and isinstance(mw.chats.get(other), dict):
            jid, chat = other, mw.chats[other]
    if not isinstance(chat, dict):
        return False
    box = chat.setdefault("messages", {}).setdefault("messages", {})
    records = box.get("records") or []
    kept = kept_after_clear(records, until, keep_starred)
    cutoffs = mw.settings.setdefault("cleared_chats", {})
    cutoffs[jid] = max(int(cutoffs.get(jid) or 0), until)
    if not keep_starred:
        starred = mw.settings.setdefault("cleared_starred_chats", {})
        starred[jid] = max(int(starred.get(jid) or 0), until)
    mw.save_settings()
    if len(kept) == len(records):
        logging.info("[chat-cleared] cleared elsewhere, nothing held here to remove")
        return True
    box["records"] = kept
    if not any(message_seconds(m) > until + SLACK_SECONDS for m in kept):
        chat["unreadCount"] = 0
    previous_t = chat.get("t")
    mw._recompute_chat_last_message(jid)
    if not chat.get("t"):
        # clearing keeps the chat where it is in the list (clear_chat_messages_local)
        chat["t"] = previous_t
    db = getattr(mw, "db", None)
    if db is not None:
        try:
            db.upsert_chat(jid, chat)
            db.delete_chat_messages_except(
                jid, [m.get("key", {}).get("id", "") for m in kept
                      if m.get("key", {}).get("id")])
        except Exception as exc:
            logging.warning("[chat-cleared] DB update failed: %s", type(exc).__name__)
    mw._schedule_save(dirty_jid=jid)
    mw._schedule_set_chats()
    mw._refresh_open_conversation_after_sync(jid, chat)
    logging.info("[chat-cleared] cleared elsewhere: %d message(s) removed, %d kept",
                 len(records) - len(kept), len(kept))
    return True
