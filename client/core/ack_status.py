"""Delivery acknowledgement values shared by UI decisions and queued storage."""
STATUS_RANKS = {"0": 0, "2": 2, "3": 3, "4": 4, "5": 5,
                "SERVER_ACK": 2, "SENT": 2, "ACK": 2,
                "DELIVERED": 3, "DELIVERY_ACK": 3, "READ": 4, "PLAYED": 5}


def status_rank(value):
    text = str(value if value is not None else "").strip().upper()
    if text.startswith("-"):
        return -1
    return STATUS_RANKS.get(text, 0)


def normalize_status(value):
    """Canonical app-scale integer, or None for a malformed/unknown value."""
    text = str(value if value is not None else "").strip().upper()
    if text in STATUS_RANKS:
        return STATUS_RANKS[text]
    try:
        number = int(text)
    except (TypeError, ValueError):
        return None
    return number if number < 0 else None


def reached_status_rank(message):
    ranks = [status_rank(message.get("status"))]
    ranks.extend(status_rank(update.get("status"))
                 for update in message.get("MessageUpdate") or []
                 if isinstance(update, dict))
    return max(ranks)
