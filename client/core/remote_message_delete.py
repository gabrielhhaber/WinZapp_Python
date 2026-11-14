"""An explicit account deletion removes one message, never an absent page."""
import logging
import wx

from core.remote_message_state import message_target, chat_aliases


def apply_message_deleted(mw, jid, mid):
    if getattr(mw, "_shutting_down", False):
        return
    aliases = chat_aliases(mw, jid)
    panel = getattr(mw, "conversations_panel", None)
    shown = ((getattr(panel, "conversation", None) or {}).get("remoteJid", "")
             .replace("@c.us", "@s.whatsapp.net"))
    if panel is not None and shown in aliases:
        # The established path stops playback, drops pagination/selection
        # copies and preserves focus. No SetFocus or spoken announcement.
        panel.messages_list.Freeze()
        try:
            panel.remove_messages_by_id({mid}, focus_previous=True)
        finally:
            panel.messages_list.Thaw()
    db = getattr(mw, "db", None)
    for alias in aliases:
        chat = getattr(mw, "chats", {}).get(alias)
        if isinstance(chat, dict):
            box = chat.get("messages", {}).get("messages", {})
            records = box.get("records") or []
            kept = [r for r in records if (r.get("key") or {}).get("id") != mid]
            last_id = ((chat.get("lastMessage") or {}).get("key") or {}).get("id")
            if len(kept) != len(records) or last_id == mid:
                box["records"] = kept
                mw._recompute_chat_last_message(alias)
                mw._schedule_save(dirty_jid=alias)
        if db is not None:
            try:
                db.delete_message(alias, mid)
            except Exception:
                logging.exception("[message-deleted-for-me] could not remove stored message")
    mw._schedule_set_chats()


def on_message_deleted(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    target = message_target(data)
    if target is not None:
        wx.CallAfter(apply_message_deleted, client.main_window, *target)
