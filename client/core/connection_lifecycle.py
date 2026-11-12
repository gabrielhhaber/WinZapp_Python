"""Session ownership and background health-check scheduling, without wx."""

from dataclasses import dataclass
import logging
import threading


@dataclass(frozen=True)
class ConnectionContext:
    token: str
    server: str
    port: object
    socket: object


def capture_connection_context(window):
    return ConnectionContext(
        getattr(window, "token", ""), getattr(window, "wpp_server", ""),
        getattr(window, "wpp_port", None), getattr(window, "ws", None),
    )


def connection_context_is_owned(window, context):
    """An HTTP observation belongs to one unchanged, running session.

    Recovery/pairing policy is deliberately separate: normal health polls
    may observe a session while a recovery owns its browser.
    """
    if not context.token or (
        getattr(window, "token", "") != context.token
        or getattr(window, "wpp_server", "") != context.server
        or getattr(window, "wpp_port", None) != context.port
        or getattr(window, "ws", None) is not context.socket
    ):
        return False
    return not (getattr(window, "_shutting_down", False)
                or getattr(window, "_wpp_updating", False))


def connection_context_is_current(window, context):
    """A delayed recovery may act only on the session that requested it."""
    if not connection_context_is_owned(window, context):
        return False
    if any(getattr(window, name, False) for name in (
        "_user_offline",
        "_pairing_in_progress", "_profile_restore_in_flight", "_qr_flood_halted",
        "_low_disk_halted",
    )):
        return False
    pairing_active = getattr(window, "_is_pairing_dialog_active", None)
    return not (pairing_active and pairing_active())


def socket_client_is_current(client):
    """Socket callbacks belong to one concrete client and authenticated token."""
    window = client.main_window
    return (
        getattr(window, "ws", None) is client
        and bool(getattr(window, "token", ""))
        and window.token == client._session_token
        and not getattr(window, "_shutting_down", False)
    )


def schedule_connection_check(window):
    """Keep HTTP recovery off the GUI and coalesce a burst into one worker."""
    context = capture_connection_context(window)
    if not connection_context_is_current(window, context):
        return False
    lock = window.__dict__.setdefault("_connection_check_worker_lock", threading.Lock())
    if not lock.acquire(blocking=False):
        return False

    def run():
        try:
            if connection_context_is_current(window, context):
                window.check_wa_connection_http()
        except Exception:
            logging.exception("[connection] background recovery check failed")
        finally:
            lock.release()

    try:
        threading.Thread(target=run, daemon=True, name="connection-recheck").start()
    except Exception:
        lock.release()
        logging.exception("[connection] could not schedule the recovery check")
        return False
    return True
