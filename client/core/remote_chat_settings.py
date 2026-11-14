"""Apply explicit account chat-setting observations without sending actions."""
import time

import wx

from core.remote_message_state import chat_target
from core.remote_chat_lock import apply_phone_lock


def settings_target(data):
    if not isinstance(data, dict):
        return None
    target = chat_target(data.get("chatId"))
    if target is None:
        return None
    setting, value = data.get("setting"), data.get("value")
    if setting == "mute":
        if type(value) is not int or not -1 <= value <= 2**53 - 1:
            return None
    elif setting in ("archive", "pin", "lock"):
        if type(value) is not bool:
            return None
    else:
        return None
    return target, setting, value


def apply_chat_setting(mw, jid, setting, value):
    if getattr(mw, "_shutting_down", False):
        return
    if setting == "mute":
        expiry = value if value == -1 or value > time.time() else None
        mw._apply_mute_state(jid, expiry)
    elif setting == "archive":
        mw.on_chat_archive_update(jid, value)
    elif setting == "pin":
        mw.on_chat_pin_update(jid, value)
    elif setting == "lock":
        apply_phone_lock(mw, jid, value)


def on_chat_settings(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    target = settings_target(data)
    if target is not None:
        wx.CallAfter(apply_chat_setting, client.main_window, *target)
