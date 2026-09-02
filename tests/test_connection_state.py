"""Tests for the pure connection-state classifier (client/connection_state.py).

These lock in the fix for the account-wiping bug: a paired session restoring
from its saved profile briefly reports QRCODE/notLogged, and the client used to
treat that as a logout and wipe the local database — even though the server was
logging back in (a real log showed 'inChat' the same second the client wiped).
"""

import connection_state as cs

LOGOUT_CONFIRM = 4
RESUME_FAIL = 20


def _classify(status, ever, logout_strikes, resume_strikes):
    return cs.classify_unlinked(
        status,
        ever_connected=ever,
        logout_strikes=logout_strikes,
        resume_strikes=resume_strikes,
        logout_confirm_strikes=LOGOUT_CONFIRM,
        resume_fail_strikes=RESUME_FAIL,
    )


def test_good_status_is_online():
    assert _classify("inChat", True, 0, 0) == cs.ONLINE
    assert _classify("CONNECTED", False, 5, 5) == cs.ONLINE


def test_resume_never_wipes_before_connect_this_run():
    # THE bug: unlinked while never connected this run → resuming, NEVER logout,
    # no matter how the logout strike count looks.
    assert _classify("QRCODE", False, 99, 1) == cs.RESUMING
    assert _classify("notLogged", False, 0, 1) == cs.RESUMING


def test_resume_failed_only_after_long_timeout():
    # Still resuming just under the timeout...
    assert _classify("QRCODE", False, 0, RESUME_FAIL - 1) == cs.RESUMING
    # ...and only once the resume has dragged on past the threshold do we offer
    # the pairing dialog (the caller passes wipe=False for this outcome).
    assert _classify("QRCODE", False, 0, RESUME_FAIL) == cs.RESUME_FAILED


def test_logout_only_after_connected_then_unlinked_confirmed():
    # Connected this run, now unlinked, but not enough strikes yet → keep waiting.
    assert _classify("QRCODE", True, LOGOUT_CONFIRM - 1, 0) == cs.RESUMING
    # Enough consecutive unlinked readings after being connected → real logout.
    assert _classify("QRCODE", True, LOGOUT_CONFIRM, 0) == cs.LOGOUT


def test_every_api_unlinked_spelling_is_classified_as_logout():
    """statusFind and onStateChange spell the same phone unlink differently."""
    for status in ("disconnectedMobile", "notLogged", "UNPAIRED", "UNPAIRED_IDLE"):
        assert status in cs.UNLINKED_STATES
        assert _classify(status, True, LOGOUT_CONFIRM, 0) == cs.LOGOUT


def test_logout_needs_connection_first():
    # Even with a huge logout-strike count, without ever connecting this run it
    # must never be classified as a logout (that was the destructive bug).
    assert _classify("QRCODE", False, 1000, 0) == cs.RESUMING


def _classify_candidate(ever, logout_strikes, resume_strikes):
    return cs.classify_unlink_candidate(
        ever_connected=ever,
        logout_strikes=logout_strikes,
        resume_strikes=resume_strikes,
        logout_confirm_strikes=LOGOUT_CONFIRM,
        resume_fail_strikes=RESUME_FAIL,
    )


class TestClassifyUnlinkCandidate:
    """classify_unlink_candidate() is the strike/timing core classify_unlinked()
    delegates to once a WPPConnect status string is confirmed unlinked; it is
    also what MainWindow._handle_local_auth_rejected() uses directly for a
    local HTTP 401/403, which carries no WPPConnect status string to check
    membership of. Same rules, same thresholds, no ONLINE outcome (the caller
    has already decided this reading counts)."""

    def test_matches_classify_unlinked_for_every_outcome(self):
        # Same three outcomes as classify_unlinked(), same thresholds, with no
        # status string to gate on.
        assert _classify_candidate(False, 99, 1) == cs.RESUMING
        assert _classify_candidate(False, 0, RESUME_FAIL - 1) == cs.RESUMING
        assert _classify_candidate(False, 0, RESUME_FAIL) == cs.RESUME_FAILED
        assert _classify_candidate(True, LOGOUT_CONFIRM - 1, 0) == cs.RESUMING
        assert _classify_candidate(True, LOGOUT_CONFIRM, 0) == cs.LOGOUT

    def test_never_logs_out_before_connecting_this_run(self):
        # The same destructive-bug guard as classify_unlinked(): a huge
        # logout-strike count means nothing before ever_connected is True.
        assert _classify_candidate(False, 1000, 0) == cs.RESUMING


# ── the during-sync strike budget (probe_strike_budget) ──────────────────────
# Three call sites in main.py used to carry the same `if initial_sync_running:
# allowed = base * 10` by hand — check_whatsapp_reachable()'s session-probe and
# host-probe branches, and check_wa_connection_http()'s except branch. The
# factor is one decision, and the wall-clock ceiling on it is the part that
# needs a test: x10 at a 30 s health cadence is ~10 minutes of answering "still
# connected" to a probe that has said otherwise every single time.

BUDGET_BASE = 2


def test_budget_is_the_base_outside_a_sync():
    assert cs.probe_strike_budget(BUDGET_BASE, initial_sync_running=False) == BUDGET_BASE
    # ...even with an ancient strike run behind it: no sync, no widening, so
    # the ceiling never enters into it.
    assert cs.probe_strike_budget(
        BUDGET_BASE, initial_sync_running=False,
        first_strike_ts=1000.0, now=1000.0 + 10_000) == BUDGET_BASE


def test_budget_widens_during_a_sync():
    widened = BUDGET_BASE * cs.SYNC_TOLERANCE_FACTOR
    # No run yet (ts 0) — this reading is starting one, so it gets the full
    # window rather than being judged against a clock that has not started.
    assert cs.probe_strike_budget(
        BUDGET_BASE, initial_sync_running=True,
        first_strike_ts=0.0, now=1000.0) == widened
    # A run one health-check tick old is still well inside the window.
    assert cs.probe_strike_budget(
        BUDGET_BASE, initial_sync_running=True,
        first_strike_ts=1000.0, now=1030.0) == widened


def test_budget_returns_to_the_base_once_the_run_outlives_the_ceiling():
    t0 = 1000.0
    assert cs.probe_strike_budget(
        BUDGET_BASE, initial_sync_running=True, first_strike_ts=t0,
        now=t0 + cs.SYNC_TOLERANCE_MAX_SECONDS - 0.1
    ) == BUDGET_BASE * cs.SYNC_TOLERANCE_FACTOR
    # At the ceiling the sync stops being an excuse: a probe that has been
    # negative for this long is an outage, and holding "connected" any longer
    # keeps _should_abort_sync_for_offline() from ever firing.
    assert cs.probe_strike_budget(
        BUDGET_BASE, initial_sync_running=True, first_strike_ts=t0,
        now=t0 + cs.SYNC_TOLERANCE_MAX_SECONDS) == BUDGET_BASE


def test_the_ceiling_can_be_disabled_for_the_branch_already_in_production():
    """check_wa_connection_http()'s except branch passes max_seconds=None: its
    x10 is the one already proven against a Node blocked by a history download,
    and tightening it now would trade a known-good behaviour for a guess."""
    assert cs.probe_strike_budget(
        BUDGET_BASE, initial_sync_running=True, first_strike_ts=1000.0,
        now=1000.0 + 10_000, max_seconds=None
    ) == BUDGET_BASE * cs.SYNC_TOLERANCE_FACTOR


def test_the_ceiling_sits_between_a_page_reload_and_the_raw_ten_minutes():
    """Both bounds are the numbers the constant was picked from: the measured
    28-second WhatsApp Web reload it must ride out, and the ~10 minutes the raw
    factor buys at the 30 s health-check cadence."""
    assert cs.SYNC_TOLERANCE_MAX_SECONDS > 28.0 * 2
    assert cs.SYNC_TOLERANCE_MAX_SECONDS < BUDGET_BASE * cs.SYNC_TOLERANCE_FACTOR * 30


def test_wake_from_suspend_detects_long_gap():
    # A 30s loop that really took 5 minutes = the machine was asleep.
    assert cs.is_wake_from_suspend(300.0, 30, 90) is True
    # Right at the threshold is not "over" it yet.
    assert cs.is_wake_from_suspend(90.0, 30, 90) is False


def test_wake_from_suspend_ignores_normal_cycles():
    # A normal ~30s cycle (even a slightly slow one) is not a wake.
    assert cs.is_wake_from_suspend(31.0, 30, 90) is False
    assert cs.is_wake_from_suspend(0.0, 30, 90) is False


def test_wake_from_suspend_guards_against_misconfig():
    # A gap threshold below the sleep interval would fire every cycle — refuse.
    assert cs.is_wake_from_suspend(60.0, 30, 20) is False


def test_chrome_cmdline_owns_session_matches_this_profile():
    sess = "9a8957e87373a353bd9d0bcbe764506a"
    cmd = (r'chrome.exe --user-data-dir=C:\Users\m\AppData\Local\WinZapp\api'
           rf'\userDataDir\{sess} --headless')
    assert cs.chrome_cmdline_owns_session(cmd, sess) is True


def test_chrome_cmdline_ignores_other_profiles_and_regular_chrome():
    sess = "9a8957e87373a353bd9d0bcbe764506a"
    other = "d8b3338f4c0552a17cd3a51c9f972fdc"
    # Another account's WPPConnect browser must NOT match.
    other_cmd = rf'chrome.exe --user-data-dir=C:\...\userDataDir\{other}'
    assert cs.chrome_cmdline_owns_session(other_cmd, sess) is False
    # The user's own regular Chrome (no userDataDir of ours) must NOT match.
    regular = r'chrome.exe --type=gpu-process --lang=pl'
    assert cs.chrome_cmdline_owns_session(regular, sess) is False
    # Empty / missing inputs are safe.
    assert cs.chrome_cmdline_owns_session("", sess) is False
    assert cs.chrome_cmdline_owns_session(other_cmd, "") is False
