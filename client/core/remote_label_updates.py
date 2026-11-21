"""Refresh account list membership after an explicit chat-label observation."""
import wx

from core.chat_lists import valid_chat_id
from core.sync_lifecycle import capture_sync_context, sync_context_is_current


def _refresh(mw, context):
    if sync_context_is_current(mw, context):
        mw._refresh_wa_lists_after_label_event()


def on_label_update(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    chat = data.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if isinstance(chat_id, dict):
        chat_id = chat_id.get("_serialized")
    if not valid_chat_id(chat_id):
        return
    ids, labels = data.get("ids"), data.get("labels")
    if (data.get("type") not in ("add", "remove") or not isinstance(ids, list)
            or not ids or len(ids) > 128
            or not all(isinstance(value, str) and 1 <= len(value) <= 64 for value in ids)
            or not isinstance(labels, list) or len(labels) > 128):
        return
    mw = client.main_window
    wx.CallAfter(_refresh, mw, capture_sync_context(mw))
