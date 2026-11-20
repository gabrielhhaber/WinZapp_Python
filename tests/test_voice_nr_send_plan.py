"""The send-time noise reduction decision table (core/voice_nr_send_plan.py)."""

import pytest

from core.voice_nr_send_plan import plan_send_reduction, session_blocked


def _plan(chosen_basic=False, on_failure="basic", session=None, busy=False):
    return plan_send_reduction(chosen_basic, on_failure, session, slot_busy=lambda: busy)


def test_default_neural_send_without_a_session():
    plan = _plan()
    assert (plan.basic_only, plan.want_pending, plan.skip_posterior,
            plan.reduce_basic_only) == (False, False, False, False)


def test_chosen_basic_never_keeps_pending():
    plan = _plan(chosen_basic=True, on_failure="keep_pending", session={"model_unavailable": True})
    assert plan.basic_only and plan.reduce_basic_only
    assert not plan.want_pending and not plan.skip_posterior


@pytest.mark.parametrize("flag", ["basic_only", "model_unavailable", "fallback_chosen"])
def test_a_blocked_session_falls_back_to_the_gate_by_default(flag):
    plan = _plan(session={flag: True})
    assert session_blocked({flag: True})
    assert plan.basic_only and plan.reduce_basic_only and not plan.skip_posterior


@pytest.mark.parametrize("flag", ["model_unavailable", "fallback_chosen"])
def test_keep_pending_skips_the_offline_pass_when_the_model_may_be_loaded(flag):
    plan = _plan(on_failure="keep_pending", session={flag: True})
    assert plan.want_pending and plan.skip_posterior and not plan.reduce_basic_only


def test_basic_only_session_skips_only_while_the_slot_is_busy():
    session = {"basic_only": True}
    assert _plan(on_failure="keep_pending", session=session, busy=True).skip_posterior
    assert not _plan(on_failure="keep_pending", session=session, busy=False).skip_posterior


def test_slot_probe_is_not_asked_without_a_basic_only_session():
    def boom():
        raise AssertionError("must not be asked")
    assert not plan_send_reduction(False, "keep_pending", {"stop": True}, slot_busy=boom).skip_posterior
