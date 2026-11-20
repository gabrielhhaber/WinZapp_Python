"""Telling the user how the phone's history transfer is going (Neonize).

Reported 2026-11-17 (iPhone, newly paired asking for a long period): the
phone sent the history for three minutes, stopped at 40% and said "Não foi
possível completar a sincronização". An iPhone only sends while WhatsApp is
open on its screen, and a blind user has no way to see that the transfer is
still running, when it stopped or when it ended. The bridge now reports the
transfer in history-sync-status (`historyTransfer`: type, progress, stalled;
neonize_bridge/history_resume.py) and asks the phone for the rest while it
is stalled; this module speaks the four moments that matter:

- the transfer started: keep WhatsApp open on the phone until it ends;
- it stalled: open WhatsApp on the phone again (with the progress reached);
- it started again after a stall;
- it ended (100% and no new block for DONE_QUIET seconds: RECENT reaches
  100% a few seconds before FULL starts, so 100% alone is not the end).

`Watch` is the pure state machine (tested in tests/test_history_transfer.py);
`start_watch` runs it on a thread that polls history-sync-status. WPPConnect
reports no `historyTransfer`, so nothing is ever said there.
"""

import logging
import threading
import time

import wx

from core import connection_engine as engines

#: seconds between two reads of history-sync-status
POLL_EVERY = 20
#: seconds at 100% without a change before the transfer counts as ended
DONE_QUIET = 60
#: seconds without any transfer before giving up (an account paired long ago)
NO_TRANSFER_WAIT = 180
#: the longest a watch lasts (a large account's history took 3h34m, #747)
MAX_WATCH = 6 * 3600


class Watch:
    """What to say as the transfer moves. The clock comes in as a parameter."""

    def __init__(self, now):
        self.started = now
        self.announced = False          # the "keep WhatsApp open" was said
        self.stalled = False
        self.seen = None                # (type, progress) last seen
        self.changed_at = None          # when `seen` last changed
        self.finished = False

    def observe(self, transfer, now):
        """[(i18n key, format kwargs)] to speak for this read of
        `historyTransfer` (None when the bridge has none)."""
        if self.finished:
            return []
        if now - self.started >= MAX_WATCH:
            self.finished = True
            return []
        if not isinstance(transfer, dict):
            if self.seen is None and now - self.started >= NO_TRANSFER_WAIT:
                self.finished = True
            return []
        seen = (transfer.get("type"), _percent(transfer.get("progress")))
        if seen != self.seen:
            self.seen, self.changed_at = seen, now
        progress = seen[1]
        stalled = bool(transfer.get("stalled"))
        said = []
        if stalled and not self.stalled:
            said.append(("history_transfer_stalled", {"percent": progress}))
            self.announced = True
        elif self.stalled and not stalled and progress < 100:
            said.append(("history_transfer_resumed", {}))
        elif not self.announced and progress < 100:
            said.append(("history_transfer_started", {}))
            self.announced = True
        self.stalled = stalled
        if progress >= 100 and now - self.changed_at >= DONE_QUIET:
            if self.announced:
                said.append(("history_transfer_done", {}))
            self.finished = True
        return said


def _percent(value):
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def start_watch(main_window):
    """Starts the watch on a thread, on Neonize only; one watch at a time."""
    if engines.loaded_engine(main_window) != engines.NEONIZE:
        return False
    if getattr(main_window, "_history_transfer_watching", False):
        return False
    main_window._history_transfer_watching = True
    threading.Thread(target=_run, args=(main_window,), daemon=True,
                     name="history-transfer").start()
    return True


def _run(main_window, sleep=time.sleep, clock=time.monotonic):
    watch = Watch(clock())
    try:
        while not watch.finished and not getattr(main_window, "_shutting_down", False):
            if getattr(main_window, "_wa_connected", False):
                status = main_window.fetch_history_sync_status()
                # A failed read counts as "no transfer reported", or the
                # give-up limits would never be checked while it keeps failing.
                transfer = status.get("historyTransfer") if isinstance(status, dict) else None
                for key, values in watch.observe(transfer, clock()):
                    logging.info("[history-transfer] %s %s", key, values)
                    text = main_window.i18n.t(key).format(**values)
                    wx.CallAfter(main_window.output, text)
            if not watch.finished:
                sleep(POLL_EVERY)
    except Exception:
        logging.exception("[history-transfer] watch stopped")
    finally:
        main_window._history_transfer_watching = False
