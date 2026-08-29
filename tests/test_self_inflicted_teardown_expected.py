"""Direct tests for MainWindow._self_inflicted_teardown_expected().

This helper consolidates four independent "we did this to ourselves, not
WhatsApp" flags into one check shared by every logout-detection guard
(on_connection_update's "close" branch, on_wpp_status_find,
check_wa_connection_http's CLOSED auto-start guard):

  - _shutting_down       (real_exit -> _perform_shutdown)
  - _wpp_updating        (_update_wpp_server(), which replaces a running
                           WPPConnect Server build without the app closing)
  - _recovery_restart_active (_run_recovery_attempts() /
                           _restart_session_once(), the wake-from-sleep
                           zombie-session close/kill/start cycle)
  - _restarting_wpp_session (_restart_wpp_session(), the detached-Puppeteer-
                           page in-place restart -- also a wake-from-sleep
                           trigger, a *different* one from the one above)

Each was added one at a time, found only by re-checking every OTHER caller
of a close-session-triggering method against the same question, not from a
live report pointing at it: with only _shutting_down checked, a
close/logout-shaped signal arriving during an update (not a shutdown) was
misread as a real WhatsApp unlink; with those two, the same signal arriving
during a zombie-session recovery cycle was ALSO misread the same way; with
all three, _restart_wpp_session()'s own close-session call was STILL
unguarded here even though that function's own docstring already documents
a real incident from exactly this mechanism -- the fix that shipped then
only covered check_wa_connection_http()'s slower strike-based path, never
on_connection_update()'s immediate single-event close branch. Two of the
four triggers are both wake-from-sleep paths, arguably the most common
real-world trigger of all of them, since a laptop sleeps far more often
than WinZapp is quit or WPPConnect updated. Same bug, reached through a
different trigger each time.
"""

from main import MainWindow


class _Stub:
    _self_inflicted_teardown_expected = MainWindow._self_inflicted_teardown_expected


def test_false_when_no_flag_set():
    s = _Stub()
    s._shutting_down = False
    s._wpp_updating = False
    s._recovery_restart_active = False
    s._restarting_wpp_session = False

    assert s._self_inflicted_teardown_expected() is False


def test_true_during_shutdown_only():
    s = _Stub()
    s._shutting_down = True
    s._wpp_updating = False
    s._recovery_restart_active = False
    s._restarting_wpp_session = False

    assert s._self_inflicted_teardown_expected() is True


def test_true_during_update_only():
    s = _Stub()
    s._shutting_down = False
    s._wpp_updating = True
    s._recovery_restart_active = False
    s._restarting_wpp_session = False

    assert s._self_inflicted_teardown_expected() is True


def test_true_during_recovery_restart_only():
    """The gap found by re-reviewing _restart_session_once() (the
    power-resume zombie-session recovery cycle) against the same question
    already asked of the shutdown and update paths: it too posts
    /close-session on this account's own session while the WebSocket stays
    connected, and was previously guarded only inside
    check_wa_connection_http()'s own CLOSED auto-start branch -- not in
    on_connection_update()'s close branch or on_wpp_status_find(), leaving
    the exact same self-inflicted-logout bug reachable through an ordinary
    sleep/wake cycle."""
    s = _Stub()
    s._shutting_down = False
    s._wpp_updating = False
    s._recovery_restart_active = True

    assert s._self_inflicted_teardown_expected() is True


def test_true_during_restarting_wpp_session_only():
    """The gap found by re-checking every OTHER close-session caller against
    the same question one more time: _restart_wpp_session() (the
    detached-Puppeteer-page in-place restart, a DIFFERENT wake-from-sleep
    trigger than _recovery_restart_active) posts /close-session on this
    account's own session too, and was not guarded here at all -- only
    check_wa_connection_http()'s slower strike-based path had a (different,
    narrower) mitigation for it."""
    s = _Stub()
    s._shutting_down = False
    s._wpp_updating = False
    s._recovery_restart_active = False
    s._restarting_wpp_session = True

    assert s._self_inflicted_teardown_expected() is True


def test_true_when_all_set():
    s = _Stub()
    s._shutting_down = True
    s._wpp_updating = True
    s._recovery_restart_active = True
    s._restarting_wpp_session = True

    assert s._self_inflicted_teardown_expected() is True


def test_missing_attributes_default_to_false():
    """None of the four flags is guaranteed to exist yet this early (e.g. a
    live event arriving before __init__ has set any of them) -- must
    degrade to "not self-inflicted", not raise, since _live_events_ready()
    is the gate that decides whether processing happens at all this early."""
    s = _Stub()

    assert s._self_inflicted_teardown_expected() is False
