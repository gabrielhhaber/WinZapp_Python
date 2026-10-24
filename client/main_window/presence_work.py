"""UI ownership around the pure bounded presence worker pool."""
import time

from core.contact_presence_refresh import current_scope
from core.contact_presence_work import PresenceWork


def presence_work(owner, ui):
    pool = getattr(owner, "_contact_presence_work", None)
    if pool is None:
        pool = owner._contact_presence_work = PresenceWork(time.monotonic, ui.CallLater, ui.CallAfter)
    return pool


def wake_waiting_presence(owner, *, ensure=False):
    if ensure:
        scope = current_scope(owner)
        if scope is not None:
            owner._defer_contact_presence(scope)
    pending = getattr(owner, "_contact_presence_deferred", None)
    if pending is not None and pending.pending is not None:
        owner._retry_contact_presence(pending.pending)


def expire_presence_read(owner, job, scope, revision, recovery):
    if getattr(owner, "_contact_presence_read_job", None) is not job:
        return
    owner._contact_presence_read_job = None
    owner._contact_presence_inflight = False
    if current_scope(owner) == scope:
        recovery.observe(scope[-1], "error", job.pool.clock(), revision)
    owner._refresh_open_contact_presence_note()
    wake_waiting_presence(owner, ensure=True)
