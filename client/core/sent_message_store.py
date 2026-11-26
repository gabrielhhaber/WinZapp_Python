"""Store a delivered own message under its real id."""
import logging


def persist_sent_message(db, record):
    """Queue *record* for storage; True when it was queued.

    Nothing else stores it on an engine with no echo (Neonize): the delayed ack
    write only merges into a stored row (it must not recreate a deleted one),
    so a message nobody had stored vanished on the next reload. Queued on the
    same ordered gate as the ack, so the row exists before the first ack lands.
    """
    key = (record or {}).get("key") or {}
    if db is None or not key.get("id") or not key.get("remoteJid"):
        return False
    try:
        db.queue_message(key["remoteJid"], record)
    except Exception:
        logging.warning("[sent_message_store] could not store sent message", exc_info=True)
        return False
    return True
