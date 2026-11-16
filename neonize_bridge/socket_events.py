"""WPPConnect event payloads, checked on wa-js 4.7.2 and 4.8.0 over CDP.

Poll options are sparse option objects (not integer indexes), with a millisecond
timestamp. A revoke's own key and its target key must stay distinct.
"""
import msgmap


def wid(jid):
    """Wid.toJSON() returns the serialized JID, not its enumerable properties."""
    return msgmap.jid_wpp(jid) or None


def wire_key(key):
    """MsgKey with serialized Wids, as JSON.stringify of the browser's model."""
    out = dict(key)
    for field in ("remote", "participant"):
        if isinstance(out.get(field), str):
            out[field] = wid(out[field])
    # Historical stored bridge keys may carry the author only in _serialized.
    parts = str(out.get("_serialized") or "").split("_")
    if not out.get("participant") and len(parts) > 3:
        out["participant"] = wid(parts[3])
    return out


def milliseconds(timestamp):
    value = float(timestamp)
    return int(value * 1000) if value < 1e11 else int(value)


def reaction_message(session, chat, mid, from_me, sender, target_serial, emoji,
                     timestamp, known_target=True):
    """Reaction key and target key are separate MsgKeys, including their authors."""
    parts = target_serial.split("_", 3)
    target = msgmap.message_key(parts[1], parts[0] == "true", parts[2],
                                parts[3] if len(parts) > 3 else "")
    chat = msgmap.jid_wpp(chat)
    key = msgmap.message_key(chat, from_me, mid, sender if chat.endswith("@g.us") else "")
    return {"session": session, "id": wire_key(key), "msgId": wire_key(target),
            "reactionText": emoji, "read": bool(from_me), "sender": wid(sender),
            "orphan": 0 if known_target else 1, "orphanReason": None,
            "timestamp": msgmap.seconds(timestamp)}


def poll_response(session, original, indexes, sender, timestamp):
    options = original.get("pollOptions") or []
    selected = []
    for index in indexes:
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(options):
            continue
        while len(selected) <= index:
            selected.append(None)
        selected[index] = dict(options[index])
    key = wire_key(original["id"])
    return {"session": session, "msgId": key, "chatId": key["remote"],
            "selectedOptions": selected, "sender": wid(sender),
            "timestamp": milliseconds(timestamp)}


def revoked_message(session, message, me):
    chat = message["id"]["remote"]
    own = bool(message.get("fromMe"))
    sender = (message["id"].get("participant") or me) if own else (
        message.get("author") or message.get("from"))
    return {"session": session, "id": message["id"]["_serialized"],
            "refId": message["protocolMessageKey"]["_serialized"],
            "author": wid(sender) if chat.endswith("@g.us") else None,
            "from": wid(me if own else chat), "to": wid(chat if own else me)}
