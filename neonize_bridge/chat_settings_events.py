"""Explicit chat settings shared with the WPPConnect page observer."""
import msgmap


def timer_changed(s, jid, duration, setting_timestamp=0):
    chat = msgmap.jid_wpp(jid)
    if chat.endswith(("@c.us", "@lid", "@g.us")):
        s.emit("chat-timer-changed", {"session": s.name, "chatId": chat,
               "duration": int(duration), "settingTimestamp": int(setting_timestamp)})


def lock_changed(s, e, jid):
    if not e.Action.HasField("locked"):
        return
    locked = bool(e.Action.locked)
    s.db.chat(jid, locked=int(locked))
    chat = msgmap.jid_wpp(jid)
    if not e.FromFullSync and chat.endswith(("@c.us", "@lid", "@g.us")):
        s.emit("chat-settings-changed", {"session": s.name, "chatId": chat,
                                      "setting": "lock", "value": locked})


def mute_changed(s, e, jid):
    if not e.Action.HasField("muted"):
        return
    end = 0
    if e.Action.muted:
        end = msgmap.seconds(e.Action.muteEndTimestamp) if e.Action.muteEndTimestamp > 0 else -1
    # Preserve the bridge store/listing's positive-expiry representation.
    s.db.chat(jid, muted=2**31 - 1 if end == -1 else int(end))
    chat = msgmap.jid_wpp(jid)
    if not e.FromFullSync and chat.endswith(("@c.us", "@lid", "@g.us")):
        s.emit("chat-settings-changed", {"session": s.name, "chatId": chat,
                                      "setting": "mute", "value": int(end)})
