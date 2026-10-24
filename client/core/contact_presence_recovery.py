"""Bounded recovery after repeated explicit pending, never after empty/error.

Policy defaults are conservative offline-tested bounds, not measured provider
latency. Ordinary 30-second active-chat reads drive this state; no extra timer.
"""
from dataclasses import dataclass

PENDING_SECONDS = 90
MAX_OBSERVATION_GAP = 45
RECOVERY_INTERVAL = 120
RECOVERY_LIMIT = 2


@dataclass(eq=False)
class _State:
    started: float | None = None
    last_pending: float | None = None
    attempts: int = 0
    last_attempt: float = float("-inf")
    revision: object = None


class PendingPresenceRecovery:
    def __init__(self):
        self._contacts = {}

    def _state(self, targets):
        states = {self._contacts[target] for target in targets if target in self._contacts}
        if len(states) == 1:
            state = states.pop()
        else:
            # A newly discovered pair shares the remaining budget. Combining
            # two independently visited identities must not mint more attempts.
            state = _State(attempts=sum(s.attempts for s in states),
                           last_attempt=max((s.last_attempt for s in states), default=float("-inf")))
            for target, previous in tuple(self._contacts.items()):
                if previous in states:
                    self._contacts[target] = state
        for target in targets:
            self._contacts[target] = state
        return state

    def has_recovered(self, targets):
        """Normal navigation/retry cannot bypass a recovery already budgeted."""
        return any(self._contacts[t].attempts for t in targets if t in self._contacts)

    def observe(self, targets, outcome, now, revision):
        if not targets:
            return
        state = self._state(targets)
        if outcome != "pending":
            state.started = state.last_pending = None
        else:
            if (state.started is None or state.revision is not revision
                    or state.last_pending is None
                    or not 0 <= now - state.last_pending <= MAX_OBSERVATION_GAP):
                state.started = now
            state.last_pending = now
        state.revision = revision

    def claim(self, targets, now, *, inflight=False):
        if not targets or inflight:
            return False
        state = self._state(targets)
        if (state.started is None or now - state.started < PENDING_SECONDS
                or state.last_pending is None
                or not 0 <= now - state.last_pending <= MAX_OBSERVATION_GAP
                or state.attempts >= RECOVERY_LIMIT
                or now - state.last_attempt < RECOVERY_INTERVAL):
            return False
        state.attempts += 1
        state.last_attempt = now
        state.started = state.last_pending = None
        return True
