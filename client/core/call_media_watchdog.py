"""Decide when a call that looks connected is receiving no media at all.

Observed 2026-11-21: a link call stayed ACTIVE for ten minutes while the
engine's relay refused the participant's bind, so nothing was ever received.
Nothing in the call state said so. The page samples the engine every few
seconds; this module only judges those samples, so it stays a plain function
with a direct test.
"""
from __future__ import annotations

from typing import Optional, Sequence

#: Seconds of unchanged, timed-out reception before the call is called deaf.
STALL_SECONDS = 15.0


def _remotes_connected(sample: dict) -> list:
    return [r for r in (sample.get("remotes") or []) if isinstance(r, dict) and r.get("connected")]


def media_stalled(samples: Sequence[dict], now: float, window: float = STALL_SECONDS) -> bool:
    """True when, for at least ``window`` seconds up to ``now``, a remote was
    connected, every connected remote was timed out, and not a byte arrived.

    A sample is ``{"at": seconds, "bytesReceived": int, "remotes": [{"connected",
    "timedOut"}]}``. A healthy call that is merely quiet is not flagged: it
    does not time out its remotes, and its byte count still moves.
    """
    recent = [s for s in samples if isinstance(s, dict) and now - float(s.get("at", 0)) <= window + 5.0]
    if len(recent) < 2:
        return False
    recent.sort(key=lambda s: float(s.get("at", 0)))
    if float(recent[-1].get("at", 0)) - float(recent[0].get("at", 0)) < window:
        return False
    if now - float(recent[-1].get("at", 0)) > window:
        return False
    for sample in recent:
        connected = _remotes_connected(sample)
        if not connected or not all(r.get("timedOut") for r in connected):
            return False
    received = {int(s.get("bytesReceived") or 0) for s in recent}
    return len(received) == 1


class CallMediaWatchdog:
    """Keeps recent samples of one call and says once per stall."""

    def __init__(self) -> None:
        self.call_id = ""
        self.samples: list = []
        self.alerted = False

    def feed(self, sample: dict, now: float) -> Optional[str]:
        """Return ``"stalled"`` once per stall, ``"recovered"`` when it clears."""
        call_id = str(sample.get("callId") or "")
        if call_id != self.call_id:
            self.call_id, self.samples, self.alerted = call_id, [], False
        self.samples.append({**sample, "at": now})
        self.samples = [s for s in self.samples if now - s["at"] <= STALL_SECONDS + 10.0]
        stalled = media_stalled(self.samples, now)
        if stalled and not self.alerted:
            self.alerted = True
            return "stalled"
        if not stalled and self.alerted:
            self.alerted = False
            return "recovered"
        return None
