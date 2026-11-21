"""The decision table of a voice note's send-time noise reduction.

Pure: what the send thread does about reduction, given the user's choices and
what the recording-time (incremental) session says about its model. Kept out
of the 2,000-line send path so every combination has a direct test.
"""

from typing import Callable, NamedTuple, Optional


class SendPlan(NamedTuple):
    basic_only: bool          # the session or the user chose the basic gate
    want_pending: bool        # a failed neural pass keeps the note pending
    skip_posterior: bool      # no offline neural pass: keep the raw note pending
    reduce_basic_only: bool   # the offline pass is the basic gate, not the model


def session_blocked(session: Optional[dict]) -> bool:
    """The incremental session cannot give this note a neural result."""
    return session is not None and bool(
        session.get("basic_only") or session.get("model_unavailable")
        or session.get("fallback_chosen"))


def plan_send_reduction(chosen_basic: bool, on_failure: str,
                        session: Optional[dict],
                        slot_busy: Callable[[], bool] = lambda: False) -> SendPlan:
    """*chosen_basic*: the user's engine is basic. *on_failure*: the failure
    action already adjusted for a missing model. *session*: the recording's
    incremental session or None. *slot_busy*: asked only when the session was
    basic-only, whether the model slot is still held by another note."""
    blocked = session_blocked(session)
    want_pending = on_failure == "keep_pending" and not chosen_basic
    # No second ONNX next to a live native session: with keep_pending and a
    # session that could not clean the note, the offline pass is skipped.
    skip_posterior = bool(want_pending and session is not None and (
        session.get("model_unavailable") or session.get("fallback_chosen")
        or (session.get("basic_only") and slot_busy())))
    return SendPlan(
        basic_only=chosen_basic or blocked,
        want_pending=want_pending,
        skip_posterior=skip_posterior,
        reduce_basic_only=chosen_basic or (blocked and not want_pending))
