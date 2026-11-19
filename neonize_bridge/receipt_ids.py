"""Message receipts that name no message.

A grouped group receipt (<participants key=...>) whose key whatsmeow read as
empty became one event per member with the id "": 16,849 of them in four
minutes on 2026-11-13, right after switching to this engine. Each one was
stored, counted toward the group's ack and sent to the client as an onack for
nothing, about 12 ms apiece, and the events behind them waited: a call in a
group was only delivered with its own end, so it never rang. A receipt with no
id has nothing to mark and is dropped at once.
"""

LOG_EVERY = 1000


def message_ids(ids) -> list:
    """The receipt's message ids without the empty ones."""
    return [i for i in ids or () if i]


class Skipped:
    """Counts the receipts dropped; `note` is True for the first and every
    LOG_EVERY-th, the ones worth a log line."""

    def __init__(self):
        self.count = 0

    def note(self) -> bool:
        self.count += 1
        return self.count == 1 or self.count % LOG_EVERY == 0
