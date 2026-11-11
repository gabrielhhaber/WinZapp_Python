"""Where the history that the phone sends stands: what history-sync-status
tells the client.

The client uses it to decide whether a short chat is really short or the rest
of the history is still on its way (main_window/backfill.py
`refresh_history_still_landing`, `wait_for_restarted_history_sync`). Always
saying "complete" made the first sync of a newly paired account take short
chats as final. neonize does not announce the end: the bridge counts the
blocks that enter `_store_history`, keeps the progress (0-100) each one brings
and calls the history complete when the progress reaches 100 or when a period
passes with no new block (a longer one while the progress is below 100).

Pure logic (the clock comes in as a parameter), tested in test_history_progress.py.
"""
import threading

#: seconds without a new block to call the history complete
SILENCE = 45
#: seconds without a new block, once a block has said the sync is below 100%.
#: A large account's history arrives over hours (3h34m measured, issue #747),
#: with gaps longer than SILENCE between blocks; calling it complete at the
#: first gap let the client treat chats as final while a few percent had landed.
PARTIAL_SILENCE = 600
#: seconds, on an account with nothing stored, waiting for the first block
INITIAL_WAIT = 120


class Progress:
    def __init__(self, now):
        self.created = now
        self.last = None            # when the last block came in or went out
        self.in_flight = 0          # blocks being stored right now
        self.percent = None         # the progress of the last block that brought one
        self._lock = threading.Lock()

    def started(self, now):
        with self._lock:
            self.in_flight += 1
            self.last = now

    def finished(self, now, percent=None):
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
            self.last = now
            if percent is not None:
                self.percent = int(percent)

    def complete(self, now, stored_chats):
        with self._lock:
            if self.in_flight > 0:
                return False
            if self.last is None:
                # no block since the bridge came up: an old account (already has
                # chats) is complete; a new account waits for the first block
                return stored_chats > 0 or now - self.created >= INITIAL_WAIT
            if self.percent is not None and self.percent >= 100:
                return True
            wait = PARTIAL_SILENCE if self.percent is not None else SILENCE
            return now - self.last >= wait

    def state(self, now, stored_chats):
        """The history-sync-status fields that depend on the progress."""
        complete = self.complete(now, stored_chats)
        return {"unprocessedChunks": self.in_flight,
                "initialSyncComplete": complete, "recentCompleted": complete}


def progress_of(s, now):
    """The session's Progress (created on first use)."""
    if getattr(s, "_history_progress", None) is None:
        s._history_progress = Progress(now)
    return s._history_progress
