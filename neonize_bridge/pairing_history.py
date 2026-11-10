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


async def redo_when_qr(session, stored, restart, sleep, wait=30.0, step=0.5):
    """Redoes the pairing of a session that was starting (INITIALIZING) when
    the history choice changed, once its QR code appears: its first code would
    carry the old days. *stored()* reads the choice, *restart()* (async) redoes
    the pairing, *sleep* waits (asyncio.sleep). Nothing when the session paired
    or never showed a code within *wait*, or when the client it is running was
    built with the stored choice already. True when it restarted."""
    waited = 0.0
    while session.status not in ("QRCODE", "CONNECTED") and waited < wait:
        await sleep(step)
        waited += step
    if session.status != "QRCODE":
        return False
    if stored() == getattr(session, "pairing_days", None):
        return False
    redone = bool(await restart())
    log.info("pairing history: redone once the QR appeared (restarted=%s)", redone)
    return redone


#: the statuses whose next QR code is made by the running client
PAIRING = ("QRCODE", "INITIALIZING")


def answer(status, stored, wanted, running=None):
    """What `POST pairing-history` does: (store?, redo the pairing?, reason).
    A paired session asks nothing any more. A session pairing is redone when
    the choice differs from what its running client registered with
    (*running*; the stored choice when unknown), not from the file: a choice
    made while the session was still starting (INITIALIZING, before the QR) was
    stored, the QR then carried the old days, and choosing again said
    "unchanged". The route redoes a QRCODE session at once and an
    INITIALIZING one as soon as its QR appears."""
    if status == "CONNECTED":
        return False, False, "paired"
    built = stored if running is None else running
    store = wanted != stored
    redo = status in PAIRING and wanted != built
    if not store and not redo:
        return False, False, "unchanged"
    return store, redo, "stored"
