"""Bound synchronous profile presence I/O without creating a worker or timer."""
import threading
import time

from core.contact_presence import activity, aliases, cached, visible_online
from core.contact_presence_query import query_targets
from core.contact_presence_work import PresenceWork
from core.contact_presence_refresh import same_read_contact

_initialization_lock = threading.Lock()


def refresh_dialog_presence(dialog):
    """Background continuation after profile fields are queued for display."""
    owner = dialog._mw
    closed = getattr(dialog, "_closed", None)
    context = (getattr(owner, "_contact_presence_epoch", 0),
               tuple(getattr(owner, key, None) for key in ("wpp_server", "wpp_port", "token")))
    panel = getattr(owner, "conversations_panel", None)
    if ((closed is not None and closed.is_set())
            or getattr(dialog, "_personal_presence_context", None) != context
            or getattr(dialog, "_personal_presence_visit", None) != (
                id(panel), getattr(panel, "_contact_presence_visit", 0))):
        return
    current = (getattr(panel, "conversation", None) or {}).get("remoteJid", "")
    if (getattr(dialog, "_personal_presence_in_view", False)
            and same_read_contact(owner, dialog._jid, current)):
        return  # The current-chat/modal reader owns its bounded refresh.
    owner.get_last_seen(dialog._jid)


def profile_work(owner):
    # Called by existing background profile workers. Lazy initialization must
    # not mint separate pools when two workers arrive together.
    with _initialization_lock:
        work = getattr(owner, "_profile_presence_work", None)
        if work is None:
            work = owner._profile_presence_work = PresenceWork(time.monotonic, None, None)
        return work


def profile_last_seen(owner, jid, call_after):
    def targets():
        return query_targets(owner._normalize_jid(jid),
                             getattr(owner, "_lid_to_phone", {}),
                             getattr(owner, "_phone_to_lid", {}))

    def routing():
        return tuple(getattr(owner, key, None) for key in ("wpp_server", "wpp_port", "token"))

    if (getattr(owner, "_shutting_down", False)
            or not getattr(owner, "_wa_connected", False)
            or cached(owner, jid, fresh=False).get("restricted")):
        return None
    fresh = cached(owner, jid)
    if activity(fresh) or visible_online(fresh):
        return None
    identity, query_identity = aliases(owner, jid), targets()
    route, epoch = routing(), getattr(owner, "_contact_presence_epoch", 0)
    if not query_identity or not all(route):
        return None
    revision = cached(owner, jid, fresh=False) or None
    panel = getattr(owner, "conversations_panel", None)
    conversation = getattr(panel, "conversation", None)
    current = aliases(owner, (conversation or {}).get("remoteJid", ""))
    in_open_chat = bool(identity and current and identity[0] == current[0])
    visit = getattr(panel, "_contact_presence_visit", None) if in_open_chat else None
    work = profile_work(owner)
    work.reap()  # Timer-free; only expired waits retire, never physical I/O.
    job = work.claim("profile", query_identity, exclusive=True)
    if job is None:
        return None  # No lease: a captured cache value has no acceptance guard.

    def current_context():
        current = aliases(owner, (getattr(panel, "conversation", None) or {}).get("remoteJid", ""))
        same_visit = visit is None or (
            panel is getattr(owner, "conversations_panel", None)
            and visit == getattr(panel, "_contact_presence_visit", None)
            and current and current[0] == identity[0])
        return bool(job.active and not getattr(owner, "_shutting_down", False)
                    and getattr(owner, "_wa_connected", False)
                    and route == routing() and epoch == getattr(owner, "_contact_presence_epoch", 0)
                    and same_visit and identity == aliases(owner, jid)
                    and query_identity == targets()
                    and revision is (cached(owner, jid, fresh=False) or None))

    result = None
    try:
        if current_context():
            result = owner._fetch_contact_presence(jid)
    except Exception:
        result = None  # Unknown; never turn failure into an offline observation.
    finally:
        job.finish()
    if not current_context():
        job.stop_timer()
        return None

    def apply():
        job.stop_timer()
        if result is not None and current_context():
            owner.on_presence_update(jid, {jid: result}, refresh_offline=False)
            owner._refresh_open_contact_presence_note()

    try:
        call_after(apply)
    except Exception:
        job.stop_timer()
        return None
    return result.get("lastSeen") if result is not None else fresh.get("lastSeen")
