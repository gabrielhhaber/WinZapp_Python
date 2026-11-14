"""How many messages this device could not decrypt, and how many the phone
gave back.

On 2026-11-13, right after switching from WPPConnect to Neonize, 500 group
messages failed with "no sender key state": 477 were the account's own,
sent by the WhatsApp Web of the WPPConnect engine, which is closed by then
and cannot answer a retry. whatsmeow then asks the phone to resend each one
(AutomaticMessageRerequestFromPhone); only 8 ever reached the store, and
whether the phone answers was only in debug logs. The bridge now says it:
one summary line per window, never one line per message.
"""
import threading
import time

WINDOW = 30.0


class Tally:
    """Counts failures (own account or others) and phone answers; `due`
    returns the summary to log once per WINDOW while something happened."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._since = clock()
        self._counts = {"own": 0, "others": 0, "unavailable": 0, "recovered": 0}

    def failed(self, own, unavailable):
        with self._lock:
            self._counts["own" if own else "others"] += 1
            if unavailable:
                self._counts["unavailable"] += 1

    def recovered(self):
        with self._lock:
            self._counts["recovered"] += 1

    def due(self):
        """The summary text when WINDOW passed and something was counted
        (the counts then restart), else ''."""
        with self._lock:
            now = self._clock()
            if now - self._since < WINDOW or not any(self._counts.values()):
                return ""
            c = self._counts
            text = (f"undecryptable messages in {now - self._since:.0f}s: "
                    f"{c['own']} from this account, {c['others']} from others "
                    f"({c['unavailable']} without the sender's key); "
                    f"{c['recovered']} given back by the phone")
            self._since = now
            self._counts = dict.fromkeys(c, 0)
            return text
