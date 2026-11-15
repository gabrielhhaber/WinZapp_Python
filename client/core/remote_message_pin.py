import logging
import wx

from core.message_pin_state import apply_pin_state
from core.remote_message_state import message_target, chat_aliases


def apply_message_pin(mw, jid, mid, pinned, timestamp, duration):
    if getattr(mw, "_shutting_down", False):
        return
    aliases = chat_aliases(mw, jid)
    panel = getattr(mw, "conversations_panel", None)
    shown = getattr(panel, "conversation", None) or {}
    shown_jid = shown.get("remoteJid")
    changed = []
    for alias in aliases:
        records = mw.chats.get(alias, {}).get("messages", {}).get("messages", {}).get("records", [])
        materialized = list(records)
        if shown_jid == alias:
            materialized += list(shown.get("messages", {}).get("messages", {}).get("records", []))
            materialized += list(getattr(panel, "_sorted_messages", []))
        for record in materialized:
            if (record.get("key") or {}).get("id") == mid and apply_pin_state(record, pinned, timestamp, duration):
                changed.append(record)
    hidden = getattr(mw, "is_chat_hidden_by_vault", lambda j: False)(jid)
    if panel is not None and shown_jid in aliases and not hidden:
        if changed:
            panel._pinned_messages_state_changed(changed, shown_jid)
            panel._repaint_or_repopulate([mid])
        else:
            # The target may be outside the loaded page: fetch the pin overview
            # from the linked device's local store, never phone history.
            panel._load_pinned_messages(announce=False)
    if getattr(mw, "db", None) is not None:
        def persist():
            try:
                for alias in aliases:
                    mw.db.update_message_pin_state(alias, mid, pinned, timestamp, duration)
            except Exception:
                logging.exception("[message-pin-changed] could not persist observation")
        try:
            mw._msg_bg_executor.submit(persist)
        except RuntimeError:
            pass  # executor closed during shutdown


def on_message_pin(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    target = message_target(data)
    pinned, timestamp, duration = data.get("pinned"), data.get("timestamp"), data.get("duration")
    if (target is None or type(pinned) is not bool or type(timestamp) is not int or not 0 < timestamp < 2**53
            or type(duration) is not int or not 0 <= duration < 2**53):
        return
    wx.CallAfter(apply_message_pin, client.main_window, *target, pinned, timestamp, duration)
