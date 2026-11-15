"""Live star observations change flags only; content and local stars survive."""
import logging
import time

import wx

from core.message_stars import apply_remote_star, remote_star_state
from core.remote_message_state import message_target, chat_aliases


def apply_star_changed(mw, jid, mid, state):
    """Main thread: mutate resident flags and schedule an atomic disk update."""
    if getattr(mw, "_shutting_down", False):
        return
    aliases = chat_aliases(mw, jid)
    for alias in aliases:
        locked = getattr(mw, "is_chat_locked", lambda j: False)(alias)
        if locked and not getattr(mw, "_chat_lock_unlocked", False):
            continue
        records = getattr(mw, "chats", {}).get(alias, {}).get("messages", {}).get("messages", {}).get("records", [])
        changed = False
        for record in records:
            if (record.get("key") or {}).get("id") == mid:
                changed = apply_remote_star(record, state) or changed
        panel = getattr(mw, "conversations_panel", None)
        if changed and panel is not None and (panel.conversation or {}).get("remoteJid") == alias:
            panel._repaint_or_repopulate([mid])
    def persist():
        try:
            for alias in aliases:
                mw.db.update_message_star_state(alias, mid, state)
        except Exception:
            logging.exception("[message-star-changed] could not persist observation")
    if getattr(mw, "db", None) is not None:
        try:
            mw._msg_bg_executor.submit(persist)
        except RuntimeError:
            pass  # executor closed during shutdown


def on_star_changed(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    target = message_target(data)
    if target is None or not isinstance(data.get("starred"), bool):
        return
    state = remote_star_state({"star": data["starred"]}, time.time_ns())
    wx.CallAfter(apply_star_changed, client.main_window, *target, state)
