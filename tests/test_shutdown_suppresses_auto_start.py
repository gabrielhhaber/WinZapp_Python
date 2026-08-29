"""Tests for check_wa_connection_http()'s CLOSED branch skipping the
auto-start-session call during our own deliberate shutdown.

Reported live: quitting WinZapp (Ctrl+Alt+Shift+Q) hit the same class of bug
as on_connection_update()'s self-inflicted-close guard, just on a different
method and a different action. _stop_wpp_server() posts /close-session
itself, which makes status-session correctly read CLOSED a moment later --
but the health-check loop runs on its own independent thread with no idea a
shutdown is in progress, saw that CLOSED, and "helpfully" called
/start-session to bring the session back. That revived the browser right as
taskkill was about to force-kill it, so the kill landed on a session that
had just started re-initializing instead of one that had actually finished
closing -- and it also starved _wait_for_session_flushed() of the CLOSED
reading it needed, timing out its 15s wait. Observed live in connection.log:
status-session read CLOSED, then INITIALIZING, then CONNECTED again, all
inside that 15s window, followed by "flush TIMEOUT ... never saw CLOSED".

check_wa_connection_http() is a large method with many dependencies (HTTP
calls, wx, several other subsystems) that make it impractical to drive end
to end in a unit test -- same situation test_startup_invalid_namespace_no_wipe.py
is in for _post_ui_init, checked the same way: at source level.
"""

import inspect

from main import MainWindow


def _closed_branch_source() -> str:
    """The CLOSED/DESTROYED/'' branch's source, isolated from the rest of
    check_wa_connection_http so assertions can't accidentally match
    unrelated code."""
    src = inspect.getsource(MainWindow.check_wa_connection_http)
    start = src.index('elif status in ("CLOSED", "DESTROYED", ""):')
    end = src.index("Sent auto-start session command", start)
    return src[start:end]


class TestClosedDuringShutdownSkipsAutoStart:
    def test_shutting_down_is_checked_before_auto_starting(self):
        """Consolidated into _self_inflicted_teardown_expected() (covers both
        _shutting_down and _wpp_updating -- see
        tests/test_self_inflicted_teardown_expected.py for that helper's own
        coverage); this branch must actually call it."""
        branch = _closed_branch_source()
        assert "_self_inflicted_teardown_expected" in branch

    def test_the_pairing_dialog_guard_is_unaffected(self):
        """The two pre-existing guards (pairing dialog active, an active
        recovery-restart sequence) must still both be present -- this fix
        adds a third condition, it does not replace either existing one."""
        branch = _closed_branch_source()
        assert "_is_pairing_dialog_active" in branch
        assert "_recovery_restart_active" in branch

    def test_the_shutting_down_check_actually_skips_the_start_session_call(self):
        """Guard against the guard being present but not actually wired to
        skip anything (e.g. a check that never reaches a `return`/skip)."""
        branch = _closed_branch_source()
        shutting_down_clause = branch[branch.index("_self_inflicted_teardown_expected"):]
        # The actual api_post(start_url, ...) call must not appear between
        # the self-inflicted-teardown check and the final `else:` that guards
        # it -- i.e. it is reachable only in that final else, not inside the
        # self-inflicted-teardown branch itself. Searches for the call
        # expression specifically (not the bare words "start-session", which
        # this branch's own explanatory comments also use in prose).
        call_pos = shutting_down_clause.find("api_post(start_url")
        else_pos = shutting_down_clause.find("else:")
        assert else_pos != -1
        assert call_pos == -1 or call_pos > else_pos
