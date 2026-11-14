"""What WhatsApp still owes this device after a reconnect.

Right after the connection opens, WhatsApp says how much it kept while the
device was away (`OfflineSyncPreview`: messages, receipts, notifications) and,
once all of it has been handed over, that it is done (`OfflineSyncCompleted`).
In between, the missed messages arrive as ordinary message events.

The bridge passes both on as the Socket.IO event `offline-sync`, so WinZapp
can show that the conversations are still being updated and leave the chat
list alone until the delivery ends, instead of guessing it from the age of
the messages (client/main_window/backlog_flood.py). WPPConnect has no such
signal; there the client keeps guessing.
"""
import logging

log = logging.getLogger("bridge")

EVENT = "offline-sync"
STARTED = "started"
COMPLETED = "completed"


def started_payload(session, event):
    return {"session": session, "state": STARTED,
            "messages": int(event.Message), "total": int(event.Total)}


def completed_payload(session, event):
    return {"session": session, "state": COMPLETED, "count": int(event.Count)}


def register_events(cli, s):
    """Wires the two events to the session `s`. Called on neonize's thread."""
    import neonize.events as nev

    if hasattr(nev, "OfflineSyncPreviewEv"):
        @cli.event(nev.OfflineSyncPreviewEv)
        def _preview(c, e):
            try:
                payload = started_payload(s.name, e)
                log.info("offline delivery announced: %s message(s), %s item(s)",
                         payload["messages"], payload["total"])
                s.emit(EVENT, payload)
            except Exception:
                log.exception("offline sync preview")

    if hasattr(nev, "OfflineSyncCompletedEv"):
        @cli.event(nev.OfflineSyncCompletedEv)
        def _completed(c, e):
            try:
                payload = completed_payload(s.name, e)
                log.info("offline delivery completed: %s item(s)", payload["count"])
                s.emit(EVENT, payload)
            except Exception:
                log.exception("offline sync completed")
