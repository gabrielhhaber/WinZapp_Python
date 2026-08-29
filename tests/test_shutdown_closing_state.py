"""Tests for the two halves of the shutdown gate telling the truth.

A user's WhatsApp session kept coming back unpaired after a restart, on a build
that already had every previous shutdown fix. Their shutdown_audit.log showed
the clean path completing inside one second:

    00:46:59  _stop_wpp_server START  pre_close_status='CONNECTED'
    00:46:59  close-session POSTed — waiting for flush
    00:47:00  flush poll #1 status='CLOSED' elapsed=0.1s
    00:47:00  FLUSH OK — session reached CLOSED
    00:47:00  taskkill /F /T node pid=2252 (flush done above)
    00:47:09  STARTUP  paired=True   →  "Session Unpaired", QR

That 0.1s is not a fast flush, it is a constant. closeSession() parked
`{status: null}` in clientsArray BEFORE awaiting client.close(), and
getSessionState() answers 'CLOSED' for a null status — so status-session
reported CLOSED the instant the close began, and _wait_for_session_flushed()
was satisfied on poll #1 before anything had been written. Every audited
shutdown in the repo shows the same 0.1s/0.2s signature, including the one
quoted in test_shutdown_flushes_profile.py.

The placeholder is now 'CLOSING', which is deliberately NOT a
session_closed_after_flush() state, so the poll waits for the real transition.

Second half: _on_end_session runs this teardown while Windows is shutting the
machine down. _on_query_end_session registers a ShutdownBlockReason but still
answers TRUE to WM_QUERYENDSESSION (event.Skip()), and registering a reason
without also vetoing buys no time at all — so the real deadline is the hung-app
timeout, ~5s, against per-phase timeouts summing to ~40s. Windows would cut the
teardown off partway: the same mid-write kill, plus a frozen UI.
"""

import inspect
import re
from pathlib import Path

import pytest

import connection_state as cs
import main
from main import MainWindow


PATCHED_CONTROLLER = (
    Path(__file__).resolve().parent.parent
    / "client" / "api_patches" / "src" / "controller" / "sessionController.ts"
)


class TestClosingIsNotClosed:
    def test_closing_is_not_treated_as_flushed(self):
        """The whole point: while the close is in flight the shutdown must keep
        waiting instead of proceeding to taskkill."""
        assert cs.session_closed_after_flush("CLOSING") is False

    @pytest.mark.parametrize("status", ["CLOSED", "DESTROYED", ""])
    def test_the_real_closed_states_still_pass(self, status):
        assert cs.session_closed_after_flush(status) is True

    def test_closing_does_not_stop_wake_recovery(self):
        """CLOSING is transient, not a settled outcome — recovery must keep
        churning rather than declare success or hand off to the pairing UI."""
        assert cs.recovery_connected("CLOSING") is False
        assert cs.recovery_needs_user_action("CLOSING") is False
        assert cs.recovery_should_stop("CLOSING") is False


class TestTheServerParksClosingNotNull:
    """These read the patched TypeScript because that is where the bug was —
    the Python predicate above is only correct while the server actually emits
    the state it expects."""

    @staticmethod
    def _source():
        """Just closeSession()'s body — `req.client.close()` also appears in
        other handlers, so a whole-file index comparison proves nothing."""
        src = PATCHED_CONTROLLER.read_text(encoding="utf-8")
        start = src.index("export async function closeSession(")
        end = src.index("export async function", start + 1)
        return src[start:end]

    def test_the_placeholder_is_closing(self):
        src = self._source()
        assert "const closingMarker = { status: 'CLOSING' };" in src
        assert "(clientsArray as any)[session] = closingMarker;" in src

    def test_the_null_placeholder_is_gone(self):
        """`{status: null}` is what getSessionState() reports as CLOSED."""
        src = self._source()
        assert "[session] = { status: null }" not in src

    def test_the_placeholder_still_precedes_the_close(self):
        """It must stay BEFORE the await: it exists so requests arriving during
        the close see a client without isConnected and get 404 Disconnected,
        which the MessageQueue relies on to not resend."""
        src = self._source()
        # Anchor on the executable guard, not on `req.client.close()` — the
        # comment above the assignment names that call too.
        guard = "if (req.client && typeof req.client.close === 'function') {"
        assert src.index("(clientsArray as any)[session] = closingMarker;") < (
            src.index(guard)
        )

    def test_the_slot_is_cleared_in_a_finally(self):
        """CLOSING blocks a concurrent /start-session (createSessionUtil returns
        early for any non-null, non-CLOSED status). That block must not outlive
        the close, or the session becomes permanently unstartable."""
        src = self._source()
        finally_at = src.index("} finally {")
        assert "(clientsArray as any)[session] = undefined;" in src[finally_at:]
        assert "(clientsArray as any)[session] === closingMarker" in src[finally_at:]

    def test_the_close_is_bounded(self):
        """wppconnect's close() swallows both page.close() and browser.close()
        failures with `.catch(() => null)`, so it can stay pending forever on a
        suspended chrome.exe — and then the finally above never runs."""
        src = self._source()
        assert "Promise.race([" in src
        race_at = src.index("Promise.race([")
        timeout_at = src.index("const closeTimeoutPromise")
        assert "setTimeout" in src[timeout_at:race_at]

    def test_the_losing_close_timeout_is_cancelled(self):
        """Promise.race does not cancel a losing timer. Without clearTimeout,
        that timer force-kills a replacement browser eight seconds after the
        old close already completed — the exact detached-frame field failure."""
        src = self._source()
        race_at = src.index("await Promise.race(")
        clear_at = src.index("clearTimeout(closeTimeout)", race_at)
        slot_clear_at = src.index("[session] = undefined", clear_at)
        assert race_at < clear_at < slot_clear_at


class TestTheWindowsShutdownBudget:
    def test_end_session_passes_a_budget(self):
        src = inspect.getsource(MainWindow._on_end_session)
        assert "_stop_wpp_server(budget=" in src

    def test_the_budget_fits_the_hung_app_timeout(self):
        """~5s is what Windows gives an app that does not veto. A budget at or
        above that is the unbounded teardown wearing a number."""
        assert 0 < MainWindow._WINDOWS_SHUTDOWN_BUDGET < 5.0

    def test_a_normal_quit_is_not_budgeted(self):
        """Nothing races a user-initiated quit, so it keeps the generous
        per-phase timeouts — they only elapse when something is wrong."""
        # The word itself appears in real_exit's prose; the call is what counts.
        assert "_stop_wpp_server(budget=" not in inspect.getsource(MainWindow.real_exit)
        assert "_stop_wpp_server(budget=" not in inspect.getsource(
            MainWindow._perform_shutdown
        )

    def test_every_waiting_phase_is_clipped_by_the_budget(self):
        """A budget that only caps some phases still overruns. Each of the three
        waits — the POST, the CLOSED poll, the profile release — must go through
        the clipper."""
        src = inspect.getsource(MainWindow._stop_wpp_server)
        assert src.count("_phase_timeout(") >= 3
        assert "timeout=_phase_timeout(self._WPP_GRACEFUL_STOP_SECONDS)" in src
        assert "_phase_timeout(self._SHUTDOWN_FLUSH_TIMEOUT)" in src
        wait_line = next(
            line for line in src.splitlines() if "wait_for_profile_release(" in line
        )
        # The call spans lines; check the argument list that follows it.
        wait_at = src.index("wait_for_profile_release(")
        assert "_phase_timeout(" in src[wait_at:wait_at + 200]

    def test_the_teardown_stays_on_the_end_session_thread(self):
        """Unlike real_exit(), this one must NOT be handed to a background
        thread: when the handler returns, Windows terminates the process, and a
        thread mid-flush is the damage being avoided."""
        src = inspect.getsource(MainWindow._on_end_session)
        assert "threading.Thread" not in src


class _Stub:
    """The attributes _wait_for_session_flushed actually touches."""

    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.audit = []

    _SHUTDOWN_FLUSH_TIMEOUT = MainWindow._SHUTDOWN_FLUSH_TIMEOUT
    _SHUTDOWN_FLUSH_POLL = 0.01

    def _shutdown_audit(self, msg):
        self.audit.append(msg)

    _wait_for_session_flushed = MainWindow._wait_for_session_flushed


class _Response:
    def __init__(self, status):
        self.status_code = 200
        self._status = status

    def json(self):
        return {"status": self._status}


class TestTheFlushPollWaitsOutClosing:
    def test_it_keeps_polling_while_closing(self, monkeypatch):
        """The regression in one assertion: given CLOSING, CLOSING, CLOSED, the
        poll must return only on the third — not on the first, which is what the
        null placeholder produced."""
        stub = _Stub()
        statuses = iter(["CLOSING", "CLOSING", "CLOSED"])
        monkeypatch.setattr(main, "api_get", lambda *a, **kw: _Response(next(statuses)))

        assert stub._wait_for_session_flushed("tok") is True
        polls = [line for line in stub.audit if line.startswith("flush poll")]
        assert len(polls) == 3
        assert "'CLOSING'" in polls[0]

    def test_a_session_stuck_closing_still_gives_up(self, monkeypatch):
        """Bounded: a close that never completes must not hang the shutdown."""
        stub = _Stub()
        monkeypatch.setattr(main, "api_get", lambda *a, **kw: _Response("CLOSING"))

        assert stub._wait_for_session_flushed("tok", timeout=0.05) is False
        assert any("flush TIMEOUT" in line for line in stub.audit)

    def test_the_caller_supplied_timeout_is_reported(self, monkeypatch):
        """The audit line is the only record that survives to the next launch,
        so it has to name the budget actually used, not the class default."""
        stub = _Stub()
        monkeypatch.setattr(main, "api_get", lambda *a, **kw: _Response("CLOSING"))

        stub._wait_for_session_flushed("tok", timeout=0.05)
        timeout_line = next(l for l in stub.audit if "flush TIMEOUT" in l)
        assert "0.1s" in timeout_line
        assert "15.0s" not in timeout_line
