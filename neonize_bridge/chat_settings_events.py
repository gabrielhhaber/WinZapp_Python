"""Explicit chat settings shared with the WPPConnect page observer."""
import msgmap


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
