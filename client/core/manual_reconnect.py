"""Sync > "Reconnect now" (issue #765): bring the connection back by hand.

Reported: after hours on a poor connection, or with the network gone and
back, WinZapp stayed in Offline mode and only restarting the computer
brought it back. The health check and Socket.IO keep retrying on their own,
but with a growing back-off, and a session whose page or client gave up
does not come back by retrying. The item does, in order, what a restart
would, minus the restart:

1. the network: when WhatsApp's servers do not answer, say so and stop
   (restarting a session without a route only adds a failure);
2. the live channel (Socket.IO), reconnected at once;
3. the engine asked now: connected already, nothing else to do;
4. else the session restarted in place (close-session + start-session, the
   same restart WinZapp makes on its own after a dead page; no QR code, the
   stored session resumes), and its coming back waited for a while.

Pure: every step is a callable, so the order is tested without a network.
"""

NO_INTERNET = "no_internet"
CONNECTED = "connected"
RESTARTING = "restarting"
FAILED = "failed"

#: what the user hears for each outcome
OUTCOME_KEYS = {
    NO_INTERNET: "reconnect_no_internet",
    CONNECTED: "reconnect_done",
    RESTARTING: "reconnect_restarting",
    FAILED: "reconnect_failed",
}


def reconnect(reaches_whatsapp, reconnect_channel, is_connected, restart_session,
              wait_connected) -> str:
    """Run the steps above; the outcome (one of the constants)."""
    if not reaches_whatsapp():
        return NO_INTERNET
    try:
        reconnect_channel()
    except Exception:
        pass            # the session restart below does not need it
    if is_connected():
        return CONNECTED
    if not restart_session():
        return FAILED
    return CONNECTED if wait_connected() else RESTARTING
