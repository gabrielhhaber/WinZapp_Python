"""A sync round is not started while a call (or a recording) is up: it froze the
WhatsApp Web page and cost a link call its media (2026-11-21)."""
from types import SimpleNamespace

from main_window.sync import SyncMixin


def _stub(paused):
    started = []
    return SimpleNamespace(
        _wa_connected=True, _sync_completed=False, _initial_sync_running=False,
        sync_thread=None, _SYNC_RETRY_COOLDOWN=60, _sync_retry_count=0,
        _last_sync_attempt_ts=0, _background_work_paused=lambda: paused,
        _try_start_sync_thread=lambda: started.append(1)), started


def test_no_round_starts_during_a_call():
    stub, started = _stub(paused=True)
    SyncMixin.trigger_sync_if_needed(stub)
    assert started == []


def test_the_round_starts_once_the_call_is_over():
    stub, started = _stub(paused=False)
    SyncMixin.trigger_sync_if_needed(stub)
    assert started == [1]
