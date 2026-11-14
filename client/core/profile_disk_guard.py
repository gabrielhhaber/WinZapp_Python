"""Close WhatsApp Web cleanly before a nearly full disk breaks its profile.

Chrome never grants a page all of the free disk: measured on 2026-11-11, with
13,226 MB free and WhatsApp Web using 1,216 MB, `navigator.storage.estimate()`
gave it 11,456 MB, so about 3 GB stay with Chrome. With ~3 GB free the page's
quota is what it already uses, every new IndexedDB write fails with
QuotaExceededError, and WhatsApp Web reloads itself "for storage recovery"
(WAWebStorageErrorHandlingUtils.markAppReloadForStorageRecovery). In
WinZapp's headless Chrome nobody sees WhatsApp's "storage full" popup, and a
refused write of the session's keys comes back logged out: the user's report
was a profile "as if wiped" every time the disk went below ~3 GB.
(docs/traps/profile-recovery.md: the profile is the only copy of the login.)

So, on the bundled WPPConnect (the profile is on this disk; Neonize has no
browser), every health poll reads the free space of the profile's disk:

* below HALT_FREE_BYTES the session is closed through close-session, which
  asks Chrome to close (never a kill) while the page can still write, the
  profile's release is awaited, the account goes offline and the user hears
  why. A latch keeps the health loop from starting the session again
  (connection_state.auto_start_block_reason);
* at RESUME_FREE_BYTES or more the latch is released and the next poll starts
  the session as it does after any CLOSED. The gap between the two keeps a
  disk hovering at the limit from opening and closing Chrome every poll.
"""
import logging
import os
import shutil
import threading

GIB = 1024 ** 3
HALT_FREE_BYTES = 4 * GIB
RESUME_FREE_BYTES = 5 * GIB

HALT, RESUME = "halt", "resume"


def decision(free_bytes, halted):
    """HALT, RESUME or None for *free_bytes* on the profile's disk. An
    unknown reading (None) changes nothing."""
    if free_bytes is None:
        return None
    if not halted and free_bytes < HALT_FREE_BYTES:
        return HALT
    if halted and free_bytes >= RESUME_FREE_BYTES:
        return RESUME
    return None


def gigabytes(free_bytes) -> str:
    return f"{free_bytes / GIB:.1f}"


def _existing(path):
    """The nearest existing folder of *path* (the profile may not exist yet)."""
    while path and not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return path


def free_bytes_of(mw, disk_usage=shutil.disk_usage):
    """Free bytes on the disk of this session's Chrome profile, or None."""
    from core import profile_recovery
    global_dir = getattr(mw, "global_dir", None)
    if not global_dir:
        return None
    session = (getattr(mw, "token", "") or "").split(":")[0]
    folder = _existing(profile_recovery.profile_dir(global_dir, session) if session
                       else global_dir)
    if not folder:
        return None
    try:
        return int(disk_usage(folder).free)
    except OSError:
        return None


def guards(mw) -> bool:
    """Whether this account's Chrome profile is one WinZapp keeps on this disk."""
    from core import profile_recovery
    uses_neonize = getattr(mw, "_uses_neonize", None)
    if callable(uses_neonize) and uses_neonize():
        return False
    return profile_recovery.profile_is_local(mw)


def check(mw, disk_usage=shutil.disk_usage, start=None):
    """One reading, from the health poll (a worker thread). Returns the
    decision taken, or None."""
    halted = bool(getattr(mw, "_low_disk_halted", False))
    if not guards(mw):
        if halted:
            release(mw, None)
            return RESUME
        return None
    free = free_bytes_of(mw, disk_usage)
    taken = decision(free, halted)
    if taken == HALT:
        halt(mw, free, start=start)
    elif taken == RESUME:
        release(mw, free)
    return taken


def halt(mw, free, start=None):
    """Latch, go offline, say why, and close the session cleanly."""
    import wx
    mw._low_disk_halted = True
    logging.warning("[disk-guard] %s GB free on the profile's disk: closing the "
                    "WhatsApp session before Chrome refuses its writes", gigabytes(free))
    audit = getattr(mw, "_shutdown_audit", None)
    if callable(audit):
        audit(f"low disk ({gigabytes(free)} GB free): closing the session cleanly")
    mw._set_wa_connected(False, "low disk space", announce=False)

    def say():
        mw.error_sound.play()
        mw.output(mw.i18n.t("low_disk_session_closed").format(
            free=gigabytes(free), needed=gigabytes(RESUME_FREE_BYTES)), interrupt=False)
    wx.CallAfter(say)
    (start or _start_thread)(lambda: _close_session(mw))


def release(mw, free):
    """Free space is back: the next health poll starts the session."""
    import wx
    mw._low_disk_halted = False
    if free is None:
        return
    logging.info("[disk-guard] %s GB free again: the session may start", gigabytes(free))
    wx.CallAfter(mw.output, mw.i18n.t("low_disk_session_resumed"), interrupt=False)


def _start_thread(work):
    threading.Thread(target=work, name="disk-guard-close", daemon=True).start()


def _close_session(mw):
    """close-session (Chrome asked to close, CONNECTED sessions are closed
    gracefully by WPPConnect), then the wait for Chrome to let the profile go."""
    from core.api_client import api_post, redact_credentials
    token = getattr(mw, "token", "")
    if not token:
        return
    try:
        api_post(f"{mw.wpp_server}:{mw.wpp_port}/api/{token}/close-session",
                 headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except Exception as e:
        logging.warning("[disk-guard] close-session failed: %s: %s",
                        type(e).__name__, redact_credentials(str(e)))
    try:
        released = mw.wait_for_profile_release(token.split(":")[0], timeout=25.0)
        logging.info("[disk-guard] profile released: %s", released)
    except Exception:
        logging.exception("[disk-guard] waiting for the profile")
