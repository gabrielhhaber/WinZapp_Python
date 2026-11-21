"""Picture notices invalidate stale client URLs, without downloading a picture."""
import msgmap


def changed(s, jid):
    chat = msgmap.jid_wpp(jid)
    if chat.endswith(("@c.us", "@lid", "@g.us")):
        s.emit("profile-picture-changed", {"session": s.name, "chatId": chat})
