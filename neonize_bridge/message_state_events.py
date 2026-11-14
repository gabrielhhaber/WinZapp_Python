"""Account message-state notices shared with WPPConnect's page observers."""
import msgmap


def star_changed(s, e, jid_text):
    if not e.Action.HasField("starred"):
        return
    s.db.change_message(e.MessageID, star=bool(e.Action.starred))
    chat = msgmap.jid_wpp(jid_text(e.ChatJID))
    if (not e.FromFullSync and e.MessageID
            and chat.endswith(("@c.us", "@lid", "@g.us"))):
        s.emit("message-star-changed", {"session": s.name, "chatId": chat,
            "messageId": e.MessageID, "starred": bool(e.Action.starred)})
