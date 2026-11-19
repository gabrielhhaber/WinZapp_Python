"""Release background-prepared work in the order it was started.

A voice message is prepared on its own thread (channel conversion, noise
reduction, encoding). Preparation time varies, so a later recording could
reach the send queue before an earlier one. A ticket is reserved on the UI
thread in recording order; each worker hands its finished action back with
``complete``, and actions run strictly in ticket order. A worker that has
nothing to send (failure, discard) completes with ``None`` so the tickets
behind it are never held back.
"""
import logging
import threading


class OrderedDispatcher:
    def __init__(self):
        self._lock = threading.Lock()
        self._next_ticket = {}
        self._next_release = {}
        self._pending = {}
        self._draining = set()

    def reserve(self, key):
        """Next ticket for ``key``; call it in the order work is started."""
        with self._lock:
            ticket = self._next_ticket.get(key, 0)
            self._next_ticket[key] = ticket + 1
            self._next_release.setdefault(key, 0)
            return ticket

    def complete(self, key, ticket, action):
        """Hand in ``action`` (a callable, or None to skip) for ``ticket``."""
        with self._lock:
            self._pending.setdefault(key, {})[ticket] = action
            if key in self._draining:
                return
            self._draining.add(key)
        while True:
            with self._lock:
                pending = self._pending.get(key, {})
                nxt = self._next_release.get(key, 0)
                if nxt not in pending:
                    self._draining.discard(key)
                    if self._next_release.get(key, 0) >= self._next_ticket.get(key, 0):
                        self._pending.pop(key, None)
                        self._next_release.pop(key, None)
                        self._next_ticket.pop(key, None)
                    return
                run = pending.pop(nxt)
                self._next_release[key] = nxt + 1
            if run is not None:
                try:
                    run()
                except Exception:
                    logging.exception("[ordered_dispatch] action failed")


voice_dispatcher = OrderedDispatcher()
