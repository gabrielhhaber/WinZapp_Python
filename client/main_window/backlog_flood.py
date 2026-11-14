"""Old messages still pouring in over the WebSocket after the sync ended.

Plain functions (no mixin): `on_new_message()` and friends are tested on bare
stubs, so the state lives on the window through getattr/setattr.

Reported 2026-11-13 on a notebook that had been offline for weeks, on a slow
link, with the Neonize engine (WPPConnect does the same): the sync round
finished in a second with what the engine already held, the status went back
to idle, and then WhatsApp delivered the backlog as live events — about 1,500
`received-message` events in four minutes. None of them notified (they are
older than the connection), but each one moved its chat to the top of the
list, so the rows kept changing place under the user's cursor.

While such a flood lasts:

* the status reads "updating conversations" instead of idle, because that is
  what is happening;
* the chat list is left alone (as during a sync round) and rebuilt once when
  the flood ends. `self.chats`, the database and unread counts are updated
  as usual, so nothing is lost.

A "late" message is one that arrives more than `LATE_SECONDS` after it was
sent. One of those is ordinary (the sender's phone was offline); a flood is
`START_COUNT` of them within `START_WINDOW` seconds, and it ends after
`QUIET_SECONDS` without any.

"Late" is read off the local clock, so a PC whose clock runs fast by more
than `LATE_SECONDS` sees every message as late and a busy group as a flood.
Two bounds keep that harmless: the threshold is minutes, not seconds (a
backlog is hours old), and a flood that goes on applies the held list every
`MAX_HOLD_SECONDS`, so the list is never older than that.

The Neonize bridge also passes on WhatsApp's own word (`offline-sync`,
neonize_bridge/offline_sync.py): how many messages it kept while the device
was away, and when it has handed all of them over. `note_offline_sync()`
starts the flood from the first, before any message arrives, and lets a
pause of up to `ANNOUNCED_QUIET_SECONDS` pass on a slow link without ending
it; the second puts the usual quiet period back. WPPConnect has no such
signal and relies on the message ages alone.

The hold covers the handlers that fire per backlog event: `on_new_message()`,
`on_historical_message()` and `on_chat_unread_update()`. Anything the user
does (archive, pin, a filter) still rebuilds the list at once.
"""

import logging
import time
from collections import deque

#: A message this much older than "now" did not arrive live.
LATE_SECONDS = 300
#: This many late messages…
START_COUNT = 5
#: …within this many seconds start a flood.
START_WINDOW = 3.0
#: A flood ends after this long without a late message.
QUIET_SECONDS = 5.0
#: The same, while a delivery WhatsApp announced has not been declared over.
ANNOUNCED_QUIET_SECONDS = 60.0
#: A flood that goes on applies the held chat list this often.
MAX_HOLD_SECONDS = 60.0
_POLL_MS = 1000

_ATTR = "_backlog_flood"


class BacklogFlood:
    """Flood detector. Pure: the caller supplies the clock readings."""

    def __init__(self, start_count: int = START_COUNT,
                 start_window: float = START_WINDOW,
                 quiet_seconds: float = QUIET_SECONDS):
        self.start_window = start_window
        self.quiet_seconds = quiet_seconds
        self._recent = deque(maxlen=start_count)
        self.active = False
        self.count = 0
        #: Something asked for a chat-list rebuild while the flood held it.
        self.list_dirty = False
        #: When the list was last left up to date (flood start or a flush).
        self.flushed_at = 0.0
        #: WhatsApp announced a delivery and has not yet said it is over.
        self.announced = False

    def announce(self, now: float) -> bool:
        """WhatsApp says a backlog is coming; True when this starts a flood."""
        self.announced = True
        # The quiet period counts from here, not from an older late message.
        self._recent.append(now)
        if self.active:
            return False
        self.active = True
        self.count = 0
        self.flushed_at = now
        return True

    def delivered(self) -> None:
        """WhatsApp says the announced backlog was all handed over."""
        self.announced = False

    def note(self, now: float) -> bool:
        """Record one late message; True when this one starts a flood."""
        recent = self._recent
        recent.append(now)
        if self.active:
            self.count += 1
            return False
        if len(recent) == recent.maxlen and now - recent[0] <= self.start_window:
            self.active = True
            self.count = len(recent)
            self.flushed_at = now
            return True
        return False

    def cancel(self) -> None:
        self.active = False
        self.announced = False
        self.list_dirty = False
        self.count = 0
        self._recent.clear()

    def flush_due(self, now: float, max_hold: float = MAX_HOLD_SECONDS) -> bool:
        """True when a held list update has waited `max_hold`; the caller
        rebuilds the list now and the hold goes on."""
        if not (self.active and self.list_dirty) or now - self.flushed_at < max_hold:
            return False
        self.list_dirty = False
        self.flushed_at = now
        return True

    def settle(self, now: float) -> bool:
        """True when an active flood has gone quiet; it is then over."""
        if not self.active or not self._recent:
            return False
        quiet = ANNOUNCED_QUIET_SECONDS if self.announced else self.quiet_seconds
        if now - self._recent[-1] < quiet:
            return False
        self.end()
        return True

    def end(self) -> None:
        """The flood is over; `list_dirty` and `count` stay for the caller."""
        self.active = False
        self.announced = False
        self._recent.clear()


def is_late_message(msg: dict, now: float) -> bool:
    """Whether *msg* was sent long before *now* (wall clock, seconds)."""
    try:
        ts = float(msg.get("messageTimestamp") or 0)
    except (TypeError, ValueError, AttributeError):
        return False
    if ts > 1_000_000_000_000:
        ts /= 1000
    return 0 < ts < now - LATE_SECONDS


def _state(window):
    flood = getattr(window, _ATTR, None)
    if isinstance(flood, BacklogFlood):
        return flood
    flood = BacklogFlood()
    try:
        setattr(window, _ATTR, flood)
    except Exception:
        return None
    return flood


def backlog_flood_active(window) -> bool:
    flood = getattr(window, _ATTR, None)
    return isinstance(flood, BacklogFlood) and flood.active


def _start_poll(window) -> None:
    import wx
    wx.CallLater(_POLL_MS, _poll, window)


def note_incoming_message(window, msg: dict) -> bool:
    """Called for every live message. True when it is a late one."""
    if not is_late_message(msg, time.time()):
        return False
    flood = _state(window)
    if flood is None:
        return True
    if flood.note(time.monotonic()):
        try:
            _start_poll(window)
        except Exception:
            # No timer, nothing to end the flood: do not start one.
            flood.cancel()
            return True
        logging.info(
            "[backlog-flood] started: %s late messages within %.0fs; holding "
            "the chat list.", flood.count, flood.start_window)
        _refresh_status(window)
    return True


def note_offline_sync(window, state: str, messages: int = 0) -> None:
    """WhatsApp's own account of a backlog, from the Neonize bridge. Main
    thread. `state` is "started" (with how many messages are waiting) or
    "completed"."""
    flood = _state(window)
    if flood is None:
        return
    if state == "completed":
        if flood.announced:
            logging.info("[backlog-flood] WhatsApp finished the announced delivery.")
        flood.delivered()
        return
    if state != "started" or messages < START_COUNT:
        return
    if flood.announce(time.monotonic()):
        try:
            _start_poll(window)
        except Exception:
            flood.cancel()
            return
        logging.info(
            "[backlog-flood] started: WhatsApp announced %s waiting message(s); "
            "holding the chat list.", messages)
        _refresh_status(window)


def defer_list_update(window) -> bool:
    """True when a flood is holding the chat list; the update is remembered
    and done once when the flood ends."""
    flood = getattr(window, _ATTR, None)
    if not isinstance(flood, BacklogFlood) or not flood.active:
        return False
    flood.list_dirty = True
    return True


def held_status(window, status: str) -> str:
    """The status `_set_status()` should show: an idle one becomes "updating
    conversations" while a flood lasts."""
    try:
        window._idle_status_requested = not status
    except Exception:
        return status
    if not status and backlog_flood_active(window):
        return window.i18n.t("updating_conversations")
    return status


def _refresh_status(window) -> None:
    # Only an idle status is ours to change: "disconnected", "syncing" and
    # the like say something more specific and stay.
    if getattr(window, "_idle_status_requested", True):
        try:
            window._set_status("")
        except Exception:
            logging.exception("[backlog-flood] could not update the status")


def _poll(window) -> None:
    if not window:      # a wx window destroyed meanwhile is falsy
        return
    flood = getattr(window, _ATTR, None)
    if not isinstance(flood, BacklogFlood) or not flood.active:
        return
    now = time.monotonic()
    if not flood.settle(now):
        try:
            _start_poll(window)
            if flood.flush_due(now):
                window._schedule_set_chats()
        except Exception:
            logging.exception("[backlog-flood] poll failed; ending the flood")
            flood.end()
        else:
            return
    dirty, flood.list_dirty = flood.list_dirty, False
    logging.info("[backlog-flood] ended after %s late messages.", flood.count)
    _refresh_status(window)
    if dirty:
        window._schedule_set_chats()
