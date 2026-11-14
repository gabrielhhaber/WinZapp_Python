"""WPPConnect event payloads, checked on wa-js 4.7.2 over CDP.

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
            "timestamp": int(float(timestamp) * 1000) if float(timestamp) < 1e11 else int(timestamp)}


def revoked_message(session, message, me):
    chat = message["id"]["remote"]
    own = bool(message.get("fromMe"))
    sender = (message["id"].get("participant") or me) if own else (
        message.get("author") or message.get("from"))
    return {"session": session, "id": message["id"]["_serialized"],
            "refId": message["protocolMessageKey"]["_serialized"],
            "author": wid(sender) if chat.endswith("@g.us") else None,
            "from": wid(me if own else chat), "to": wid(chat if own else me)}
