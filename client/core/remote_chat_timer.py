"""Mirror a known disappearing-message timer; never infer one from absence."""
import wx

from core.remote_message_state import chat_target, chat_aliases


def timer_target(data):
    if not isinstance(data, dict):
        return None
    jid = chat_target(data.get("chatId"))
    duration, stamp = data.get("duration"), data.get("settingTimestamp")
    if jid is None or any(type(v) is not int or not 0 <= v <= 2**53 - 1 for v in (duration, stamp)):
        return None
    return jid, duration, stamp


def apply_chat_timer(mw, jid, duration, stamp):
    if getattr(mw, "_shutting_down", False):
        return
    panel = getattr(mw, "conversations_panel", None)
    for alias in chat_aliases(mw, jid):
        chat = mw.chats.get(alias)
        if not isinstance(chat, dict):
            continue
        known = chat.get("ephemeralSettingTimestamp") or 0
        if stamp and isinstance(known, (int, float)) and known > stamp:
            continue
        chat["ephemeralDuration"] = duration
        chat["ephemeralSettingTimestamp"] = stamp
        shown = getattr(panel, "conversation", None)
        if isinstance(shown, dict) and shown.get("remoteJid") == alias:
            shown.update(ephemeralDuration=duration, ephemeralSettingTimestamp=stamp)
        mw._schedule_save(dirty_jid=alias)


def on_chat_timer(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    target = timer_target(data)
    if target is not None:
        wx.CallAfter(apply_chat_timer, client.main_window, *target)
