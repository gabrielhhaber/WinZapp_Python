"""Route WPPConnect/Neonize poll notifications to the open poll's refresh."""
import wx


def poll_target(data):
    if not isinstance(data, dict):
        return None
    key, chat = data.get("msgId"), data.get("chatId")
    if isinstance(chat, dict):
        chat = chat.get("_serialized")
    if isinstance(key, dict):
        mid = key.get("id")
    elif isinstance(key, str):
        parts = key.split("_")
        mid = parts[2] if len(parts) > 2 else key
    else:
        return None
    if (not isinstance(chat, str) or not chat.endswith(("@g.us", "@lid", "@c.us", "@s.whatsapp.net"))
            or not isinstance(mid, str) or not mid or len(mid) > 64):
        return None
    return chat.replace("@c.us", "@s.whatsapp.net"), mid


def on_poll_response(client, data):
    if not isinstance(data, dict) or not client._belongs_to_this_session(data):
        return
    target = poll_target(data)
    panel = getattr(client.main_window, "conversations_panel", None)
    if target is not None and panel is not None:
        wx.CallAfter(panel.on_poll_response, *target)
