"""The send-time noise-reduction decision table (core/voice_nr_send_plan.py).
The panel wiring around it is covered by tests/test_incremental_wiring.py."""

from core.voice_nr_send_plan import plan_after_reservation, plan_send_reduction


def _plan(engine="neural", on_failure="basic", model_installed=True,
          session=None, drain_started=False, has_controller=True,
          slot_busy=False):
    return plan_send_reduction(
        engine=engine, on_failure=on_failure, model_installed=model_installed,
        session=session, drain_started=drain_started,
        has_controller=has_controller, slot_busy=slot_busy)


def test_published_default_reserves_and_runs_the_model():
    plan = _plan()
    assert not plan.basic_only and not plan.want_pending and not plan.skip_posterior
    assert plan.reserve_offline and plan.announce_fallback


def test_basic_engine_chosen_runs_the_gate_quietly_even_with_keep_pending():
    plan = _plan(engine="basic", on_failure="keep_pending")
    assert plan.basic_only and not plan.want_pending
    assert not plan.reserve_offline and not plan.announce_fallback


def test_missing_model_is_the_announced_fallback_never_pending():
    plan = _plan(on_failure="keep_pending", model_installed=False)
    assert plan.basic_only and not plan.want_pending and not plan.skip_posterior
    assert plan.announce_fallback


def test_keep_pending_skips_a_second_onnx_while_the_native_model_may_live():
    for flag in ("model_unavailable", "fallback_chosen"):
        plan = _plan(on_failure="keep_pending", session={flag: True})
        assert plan.want_pending and plan.skip_posterior
        assert not plan.reserve_offline and not plan.release_own_slot


def test_slot_busy_at_record_and_still_busy_keeps_pending():
    plan = _plan(on_failure="keep_pending", session={"basic_only": True},
                 slot_busy=True)
    assert plan.skip_posterior


def test_slot_busy_at_record_but_freed_allows_the_posterior_pass():
    plan = _plan(on_failure="keep_pending", session={"basic_only": True})
    assert plan.want_pending and not plan.skip_posterior and not plan.basic_only
    assert plan.reserve_offline


def test_blocked_session_without_keep_pending_takes_the_gate():
    plan = _plan(session={"basic_only": True}, slot_busy=True)
    assert plan.basic_only and not plan.reserve_offline


def test_send_moment_session_frees_its_own_slot_only_when_it_never_started():
    assert _plan(session={"worker": None}).release_own_slot
    assert not _plan(session={"worker": None}, drain_started=True).release_own_slot
    assert not _plan(session={"worker": object()}).release_own_slot
    assert not _plan(session={"worker": None}, has_controller=False).release_own_slot
    assert not _plan(session=None).release_own_slot


def test_refused_reservation_takes_the_occupied_policy():
    pending = plan_after_reservation(_plan(on_failure="keep_pending"), False)
    assert pending.skip_posterior and not pending.basic_only
    gated = plan_after_reservation(_plan(), False)
    assert gated.basic_only and not gated.skip_posterior
    assert plan_after_reservation(_plan(), True) == _plan()


def test_no_controller_means_no_reservation():
    plan = _plan(has_controller=False)
    assert not plan.reserve_offline and not plan.basic_only
