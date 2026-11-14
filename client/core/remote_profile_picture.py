"""Forget old picture URLs; the next view queries the current picture."""
import logging
import wx

from core.chat_contact_names import find_contact_key
from core.remote_message_state import chat_target, chat_aliases


def apply_picture_changed(mw, jid):
    if getattr(mw, "_shutting_down", False):
        return
    changed = {}
    contacts = mw.contacts
    for alias in chat_aliases(mw, jid):
        key = find_contact_key(contacts, alias)
        if key and contacts[key].get("profilePicUrl"):
            record = dict(contacts[key], profilePicUrl="")
            contacts[key] = changed[key] = record
        chat = getattr(mw, "chats", {}).get(alias)
        if isinstance(chat, dict):
            removed = False
            for field in ("profilePicUrl", "profilePicThumbObj"):
                if field in chat:
                    chat.pop(field)
                    removed = True
            if removed:
                mw._schedule_save(dirty_jid=alias)
    if changed and getattr(mw, "db", None) is not None:
        try:
            mw.db.upsert_contacts_batch(changed)
        except Exception:
            logging.exception("[profile-picture-changed] could not invalidate stored URLs")


def on_picture_changed(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    jid = chat_target(data.get("chatId"))
    if jid is not None:
        wx.CallAfter(apply_picture_changed, client.main_window, jid)
