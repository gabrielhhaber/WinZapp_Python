"""Which noise reduction the send of a voice note runs — a pure decision table.

The send thread (ui/conversation_panel/voice_nr_send.py) reduces a note that
was not already cleaned while it was recorded. What it may run depends on the
user's choices (engine, "if neural reduction fails") and on the state the
recording's incremental session left behind:

- A basic engine chosen on purpose sends gated, exactly as published, and
  nothing about a neural failure is announced.
- A missing model is the published fallback (the gate, announced), never a
  reason to keep every note pending until a download nobody started.
- Keep-pending exists only for a user who chose neural: when the incremental
  side could not clean the note and its private model may still be loaded,
  no second ONNX pass runs and the original goes straight to pending.
- The normal offline pass takes the controller's offline reservation; a
  capture holding the slot makes the note take the occupied policy (the
  gate, or pending when the user chose keep-pending).
"""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SendReductionPlan:
    #: The note stays pending with its original audio unless the neural
    #: model cleans it.
    want_pending: bool
    #: Run no reduction now (the note goes to pending).
    skip_posterior: bool
    #: Run the basic gate instead of the model.
    basic_only: bool
    #: The session never started incremental work: its slot is only send
    #: authority, so the caller frees it before reserving the offline side.
    release_own_slot: bool
    #: Take the controller's offline reservation before reducing.
    reserve_offline: bool
    #: A non-neural result is announced (the user wanted the neural model).
    announce_fallback: bool


def plan_send_reduction(*, engine, on_failure, model_installed, session,
                        drain_started, has_controller, slot_busy):
    """The plan before the offline reservation is tried.

    *session* is the recording's incremental session dict (only its flags
    are read) or None; *drain_started* whether a drain thread exists;
    *slot_busy* whether a capture holds the controller's slot now.
    """
    chosen_basic = engine == "basic"
    model_missing = not chosen_basic and not model_installed
    flags = session or {}
    has_session = session is not None
    slot_was_busy = bool(flags.get("basic_only", False))
    unavailable = bool(flags.get("model_unavailable", False))
    fallback = bool(flags.get("fallback_chosen", False))
    session_blocked = has_session and (slot_was_busy or unavailable or fallback)
    want_pending = (on_failure == "keep_pending"
                    and not chosen_basic and not model_missing)
    skip_posterior = want_pending and has_session and (
        unavailable or fallback
        or (slot_was_busy and has_controller and slot_busy))
    basic_only = (chosen_basic or model_missing
                  or (session_blocked and not want_pending))
    release_own_slot = (has_session and has_controller
                        and flags.get("worker") is None
                        and not drain_started and not session_blocked)
    return SendReductionPlan(
        want_pending=want_pending,
        skip_posterior=skip_posterior,
        basic_only=basic_only,
        release_own_slot=release_own_slot,
        reserve_offline=has_controller and not skip_posterior and not basic_only,
        announce_fallback=not chosen_basic,
    )


def plan_after_reservation(plan, reserved):
    """The plan once the offline reservation was tried: refused means a
    capture holds the slot — pending when the user chose it, else the gate."""
    if not plan.reserve_offline or reserved:
        return plan
    if plan.want_pending:
        return replace(plan, skip_posterior=True, reserve_offline=False)
    return replace(plan, basic_only=True, reserve_offline=False)
