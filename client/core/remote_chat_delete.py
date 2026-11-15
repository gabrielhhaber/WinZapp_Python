"""An explicit deletion cannot erase a conversation that has newer messages."""
import logging
import wx

from core.remote_conversation_close import close_for_remote_change
from core.remote_message_state import chat_target, chat_aliases


def latest_message_time(chat):
    values = [(chat or {}).get("t"), ((chat or {}).get("lastMessage") or {}).get("messageTimestamp")]
    records = (chat or {}).get("messages", {}).get("messages", {}).get("records", [])
    values += [r.get("messageTimestamp") for r in records]
    valid = [v / 1000 if v > 1e11 else v for v in values if type(v) in (int, float) and v > 0]
    return max(valid, default=0)


def apply_chat_deleted(mw, jid, until, stored_latest=0):
    if getattr(mw, "_shutting_down", False):
        return
    aliases = chat_aliases(mw, jid)
    if stored_latest > until or any(latest_message_time(mw.chats.get(a)) > until for a in aliases):
        return
    panel = getattr(mw, "conversations_panel", None)
    shown = (getattr(panel, "conversation", None) or {}).get("remoteJid")
    if shown in aliases:
        if latest_message_time(panel.conversation) > until:
            return
        if not close_for_remote_change(mw, aliases, "remote_chat_deleted_closed"):
            return  # still on screen: its rows cannot be removed under it
    # Reuse the existing durable deleted-chat markers and lock cleanup.
    # No request is sent to WhatsApp; all known aliases lose their stored rows.
    for alias in aliases:
        mw.delete_chat_local(alias)


def on_chat_deleted(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    jid = chat_target(data.get("chatId"))
    until = data.get("until")
    if jid is None or type(until) is not int or not 0 < until < 2**53:
        return
    mw = client.main_window
    if getattr(mw, "db", None) is None:
        return  # no stored history to compare: no evidence for a destructive cleanup
    def check_stored_history():
        try:
            newest = 0
            for alias in chat_aliases(mw, jid):
                records = mw.db.get_messages(alias, limit=1)
                newest = max(newest, latest_message_time({"messages": {"messages": {"records": records}}}))
            wx.CallAfter(apply_chat_deleted, mw, jid, until, newest)
        except Exception:
            # Missing evidence must not authorize destructive local cleanup.
            logging.exception("[chat-deleted] could not check stored history")
    try:
        mw._msg_bg_executor.submit(check_stored_history)
    except RuntimeError:
        pass  # executor closed during shutdown
