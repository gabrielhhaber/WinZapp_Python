"""The desktop share re-shares once, quietly, when the pinned NVDA exits.

A share dying on "The pinned NVDA process exited" (log 2026-11-17, two deaths
in one call) stops the desktop audio while the call continues. NVDA's own
watchdog restarts it, and one fresh activation is what pressing Share again
would create — so the share resumes by itself, exactly once, and falls back
to the ordinary failure announcement otherwise. Never a retarget of another
process: every activation re-pins by name+session/image/identity as usual.
"""
from core.call_desktop_audio import (
    NVDA_EXITED_REASON,
    should_auto_retry_desktop_audio,
)


def test_the_pin_death_is_the_one_retry_trigger():
    assert should_auto_retry_desktop_audio(
        ok=False, reason=f"WasapiError: {NVDA_EXITED_REASON}",
        call_running=True, already_retried=False) is True


def test_worker_read_back_of_the_reason_prefixed_class():
    # The mixin stores "ClassName: message" in worker.failure_reason.
    assert should_auto_retry_desktop_audio(
        ok=False, reason="WasapiError: The pinned NVDA process exited",
        call_running=True, already_retried=False) is True


def test_an_unrelated_failure_is_not_retried():
    for reason in (None, "", "RuntimeError: NVDA is not running",
                   "WasapiError: Process loopback activation timeout",
                   "ValueError: device vanished"):
        assert should_auto_retry_desktop_audio(
            ok=False, reason=reason, call_running=True, already_retried=False) is False


def test_success_and_budget_and_dead_call_are_not_retried():
    assert should_auto_retry_desktop_audio(
        ok=True, reason=f"WasapiError: {NVDA_EXITED_REASON}",
        call_running=True, already_retried=False) is False
    assert should_auto_retry_desktop_audio(
        ok=False, reason=f"WasapiError: {NVDA_EXITED_REASON}",
        call_running=True, already_retried=True) is False
    assert should_auto_retry_desktop_audio(
        ok=False, reason=f"WasapiError: {NVDA_EXITED_REASON}",
        call_running=False, already_retried=False) is False
