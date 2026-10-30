"""Tell WinZapp when the Mac wakes from sleep.

MainWindow has two wake triggers and neither exists here. wxOSX never sends
EVT_POWER_RESUME. The health-check loop looks for a gap in time.monotonic(),
which is mach_absolute_time() on macOS and stands still while the Mac sleeps,
so no gap ever shows: a MacBook log with a 49-minute sleep has not one
[power] or [wake-detect] line, and the session came back as a mid-session
drop, with the logout tallies of before the sleep (2026-10-26).

NSWorkspace does know. Its notification runs what the clock-gap detector
runs; _recover_from_suspend() is single-flight, so the two cannot overlap
should the clock ever start to show the gap.
"""

import logging
import threading

_observer = None


def on_wake(window):
    """The recovery the health-check loop runs for a detected wake. Blocks:
    runs on its own thread."""
    if any(getattr(window, name, False)
           for name in ("_user_offline", "_wpp_updating", "_shutting_down")):
        return
    logging.info("[wake_mac] The Mac woke from sleep; forcing recovery.")
    try:
        window._reset_connection_state_for_resume()
        window._recover_from_suspend()
    except Exception:
        logging.exception("[wake_mac] recovery after wake failed")


def _center():
    from AppKit import NSWorkspace
    return NSWorkspace.sharedWorkspace().notificationCenter()


def watch(window):
    """Observe wakes for *window*'s account for as long as the process lives."""
    global _observer
    if _observer is not None:
        return

    def did_wake(_notification):
        # An exception escaping into AppKit's dispatch would end the process.
        try:
            threading.Thread(target=on_wake, args=(window,), daemon=True).start()
        except Exception:
            logging.exception("[wake_mac] could not start recovery")

    try:
        from AppKit import NSWorkspaceDidWakeNotification
        _observer = _center().addObserverForName_object_queue_usingBlock_(
            NSWorkspaceDidWakeNotification, None, None, did_wake)
    except Exception:
        logging.exception("[wake_mac] could not observe wake")


def install():
    from main_window.account_link import AccountLinkMixin

    original = AccountLinkMixin.start_connection_health_checker

    def start_connection_health_checker(self):
        watch(self)
        return original(self)

    AccountLinkMixin.start_connection_health_checker = start_connection_health_checker
