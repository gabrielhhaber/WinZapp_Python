"""One deferred active-contact read; no polling, wx imports or clock ownership."""
import math

from core.contact_presence import activity, aliases, cached, visible_online
from core.contact_presence_query import query_targets


def same_read_contact(owner, left, right):
    """A new UI reader may match an exact id or a reciprocal private pair."""
    def targets(value):
        return query_targets(owner._normalize_jid(value),
                             getattr(owner, "_lid_to_phone", {}),
                             getattr(owner, "_phone_to_lid", {}))
    first, second = targets(left), targets(right)
    return bool(first and second and (
        owner._normalize_jid(left) == owner._normalize_jid(right)
        or len(first) == len(second) == 2 and first == second))


def current_scope(owner):
    panel = getattr(owner, "conversations_panel", None)
    conversation = getattr(panel, "conversation", None)
    jid = (conversation or {}).get("remoteJid", "")
    if panel is None or not jid:
        return None
    return (id(panel), getattr(panel, "_contact_presence_visit", 0),
            getattr(owner, "_contact_presence_epoch", 0), aliases(owner, jid),
            query_targets(owner._normalize_jid(jid),
                          getattr(owner, "_lid_to_phone", {}),
                          getattr(owner, "_phone_to_lid", {})))


def needs_snapshot(owner):
    panel = getattr(owner, "conversations_panel", None)
    jid = (getattr(panel, "conversation", None) or {}).get("remoteJid", "")
    entry = cached(owner, jid)
    # An inherited timestamp is not a new observation, even when two events
    # share a monotonic clock tick. Old metadata-free fixtures remain readable.
    refreshed = entry.get("lastSeen") and entry.get("_seen_version") == entry.get("_version")
    return not (entry.get("restricted") or refreshed
                or visible_online(entry) or activity(entry))


def take_readiness_followup(owner, targets):
    """One pending-only continuation after confirmed POST per contact/epoch."""
    used = getattr(owner, "_contact_presence_readiness_used", set())
    confirmed = {owner._normalize_jid(t) for t in
                 getattr(owner, "_contact_presence_readiness_candidates", set())}
    if not targets or used.intersection(targets) or not confirmed.intersection(targets):
        return False
    used.update(targets)
    owner._contact_presence_readiness_used = used
    return True


class DeferredPresenceRefresh:
    def __init__(self):
        self.pending = None
        self.timer = None
        self.generation = 0

    def cancel(self):
        timer, self.timer = self.timer, None
        self.pending = None
        self.generation += 1
        if timer is not None:
            try:
                timer.Stop()
            except RuntimeError:
                pass  # The owning wx control/timer may already be destroyed.

    def remember(self, scope):
        if self.pending != scope:
            self.cancel()
            self.pending = scope

    def schedule(self, scope, delay, call_later, callback):
        self.remember(scope)
        if self.timer is not None:
            return
        self.generation += 1
        generation = self.generation

        def fire():
            if self.generation != generation or self.pending != scope:
                return
            self.timer = None
            callback(scope)

        try:
            self.timer = call_later(max(1, math.ceil(delay * 1000)), fire)
        except Exception:
            self.cancel()
            raise
