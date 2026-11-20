"""The "sent" (ack 1) WPPConnect says right after a send, which the bridge
emits itself 0.6 s later: the app only registers the message after the HTTP
response. A real ack for the same id that went out in the meantime wins, or
the row flips back from delivered to sent (see test_synthetic_sent_ack.py)."""


def sent_ack_payload(session, sent_id, chat_w, serial):
    """The `onack` payload for the synthetic ack=1 of `sent_id`, or None when a
    real ack for it was already emitted."""
    if session._real_ack_emitted(sent_id):
        return None
    return {"session": session.name, "ack": 1, "to": chat_w, "from": chat_w,
            "id": {"fromMe": True, "remote": chat_w, "id": sent_id,
                   "_serialized": serial}}
