"""How much history a session asks the phone for when it pairs.

The phone sends a linked device a window of its history once, at pairing,
sized by the DeviceProps the device registers with (`historySyncConfig`).
After that only WhatsApp's on-demand requests remain, and WhatsApp Web stops
those at about a year and at the "Use WhatsApp on your phone to see older
messages" answer (older_history.py). So a chat's 2024 messages reach a device
paired asking for 180 days never: reported from a user whose TeleZapp, paired
asking for everything, showed them.

The choice is the user's, made in the pairing dialog, and kept in the
session's own folder: every start of that session reads it (the client starts
sessions from several places), and it only matters until the session pairs.
`POST pairing-history` stores it; a session showing a QR code is restarted so
the code the user scans carries the new choice.

The pure functions are at the top (tested in test_pairing_history.py).
"""
import json
import logging
import os

log = logging.getLogger("bridge")

#: The ready-made choices: 0 is WhatsApp's own default (recent messages
#: only), 3650 is "everything" (what the phone agrees to send, ten years).
CHOICES = (0, 30, 180, 365, 3650)
#: A custom choice (days, months or years in the dialog) may ask for up to
#: twenty years: older than WhatsApp itself.
MAX_DAYS = 7320
#: When nothing was chosen: the bridge's --history-days (180 by default).
FILE = "pairing_history.json"


def chosen(value):
    """`value` as days to ask for (0, or 1 to MAX_DAYS), or None."""
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    return days if 0 <= days <= MAX_DAYS else None


def load(folder, default):
    """The days stored for the session in `folder`, or `default`."""
    try:
        with open(os.path.join(folder, FILE), encoding="utf-8") as stream:
            days = chosen(json.load(stream).get("days"))
    except (OSError, ValueError, AttributeError):
        return default
    return default if days is None else days


def save(folder, days) -> bool:
    """Store `days` for the session in `folder`. False when it could not."""
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, FILE)
        with open(path + ".tmp", "w", encoding="utf-8") as stream:
            json.dump({"days": int(days)}, stream)
        os.replace(path + ".tmp", path)
        return True
    except OSError as error:
        log.warning("pairing history choice not stored: %s", type(error).__name__)
        return False


def answer(status, stored, wanted):
    """What `POST pairing-history` does: (store?, restart the pairing?, reason).
    A paired session asks nothing any more; a session showing a QR code is
    restarted when the choice changed, so the code carries it."""
    if status == "CONNECTED":
        return False, False, "paired"
    if wanted == stored:
        return False, False, "unchanged"
    return True, status == "QRCODE", "stored"
