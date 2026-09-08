"""Tests for waiting out the Chrome profile lock before re-starting a session.

`_bg_pairing_flow` closes any existing WPPConnect session before calling
/start-session, so only one browser owns `userDataDir/<session>` at a time. It
used to signal "closed" as soon as the close-session HTTP call returned — but
that call answers 200 as soon as it has *asked* the session to close. Chrome is
still shutting down and still holds the lock.

Measured on a real run: close-session and start-session landed 60 ms apart, and
the Node side reported

    Error no open browser
    The browser is already running for ...\\userDataDir\\526fc15c...
    Auto Close Called

The new session died on the spot, no pairing code was ever emitted, and the
Python side sat out its full 90-second `_phone_code_event` wait before
reporting the generic failure. Nothing in log.log named the cause.

Note this only bites a *reused* session name — a freshly minted one has no
running browser to collide with, which is why several pairings in the same
session succeeded and this one did not.
"""

import inspect
import os

import pytest

from main import MainWindow
from ui.dialogs.connect import Connect


class TestTheCloseWaitsForTheRealSignal:
    @staticmethod
    def _source():
        return inspect.getsource(Connect.on_continue)

    def test_it_polls_for_closed_instead_of_trusting_the_http_reply(self):
        source = self._source()
        assert "_wait_for_session_flushed" in source

    def test_the_poll_happens_after_close_session_and_before_start_session(self):
        source = self._source()
        close = source.index("/close-session")
        poll = source.index("_wait_for_session_flushed", close)
        start = source.index("/start-session", poll)
        assert close < poll < start

    def test_it_reuses_the_shutdown_poller_rather_than_a_second_one(self):
        """The shutdown path solved this exact problem (a blind sleep landed
        mid-flush and corrupted the profile's leveldb). Two pollers would drift
        apart on timeout and on what counts as closed."""
        assert hasattr(MainWindow, "_wait_for_session_flushed")
        assert "_await_session_closed" not in self._source()

    def test_the_backstop_wait_outlasts_both_polls(self):
        """close_done is only released after BOTH waits return — the CLOSED
        poll and then the profile-release poll. The outer wait has to allow for
        their combined worst case, or it expires first and start-session runs
        while the profile is still locked, which is the bug all over again."""
        source = self._source()
        assert "close_done.wait(timeout=45)" in source
        # flush poll (15s) + profile release (15s) + post-kill grace (5s)
        assert MainWindow._SHUTDOWN_FLUSH_TIMEOUT + 15 + 5 <= 45

    def test_a_missing_old_token_skips_the_poll(self):
        """First pairing on a clean install has nothing to close; the poller
        needs a token and there is none to give it."""
        source = self._source()
        # The call sits on its own line under the guard, so look at the couple
        # of lines immediately before it rather than just the one it shares.
        poll_at = source.index("self.main_window._wait_for_session_flushed")
        preceding = source[:poll_at].rstrip().splitlines()[-2:]
        assert any("if _old_token:" in line for line in preceding)


class TestThePollerContract:
    """What _bg_pairing_flow now depends on."""

    def test_it_reports_closed_or_timeout_without_raising(self):
        source = inspect.getsource(MainWindow._wait_for_session_flushed)
        assert "return True" in source
        assert "return False" in source
        assert "except Exception" in source

    def test_a_connection_error_counts_as_closed(self):
        """If Node has already torn the session down there is nothing left to
        hold the lock — blocking the full timeout would just delay pairing."""
        source = inspect.getsource(MainWindow._wait_for_session_flushed)
        conn_error = source.index("conn-error")
        assert "return True" in source[conn_error : conn_error + 200]

    def test_it_is_bounded(self):
        assert 0 < MainWindow._SHUTDOWN_FLUSH_POLL < MainWindow._SHUTDOWN_FLUSH_TIMEOUT


class TestWaitForProfileRelease:
    """The CLOSED poll was the wrong signal, and a real run proved it:

        17:23:23,377  Closed existing session: cc8d6910...
        17:23:23      flush poll #1 status='CLOSED' elapsed=0.1s
        17:23:23,495  POST /start-session -> 200
        20:23:23,573  The browser is already running for .../userDataDir/cc8d6910
        20:23:24,146  Auto Close Called

    Session status and browser process lifetime are different things. Only the
    process owns the profile lock, so only the process is worth waiting on.
    """

    class _Stub:
        wait_for_profile_release = MainWindow.wait_for_profile_release

        def __init__(self, holder_sequence):
            # One entry per poll: the PIDs still holding the profile.
            self._sequence = list(holder_sequence)
            self.kills = []
            # How long the wait really took now goes to shutdown_audit.log,
            # which survives the log truncation that hides it today.
            self.audits = []

        def _shutdown_audit(self, msg):
            self.audits.append(msg)

        def _chrome_pids_owning_session(self, session_name):
            return self._sequence.pop(0) if self._sequence else []

        def _kill_orphaned_chrome_for_session(self, session_name=None):
            self.kills.append(session_name)

    def test_it_returns_at_once_when_nothing_holds_the_profile(self, monkeypatch):
        monkeypatch.setattr("main.time.sleep", lambda _s: None)
        stub = self._Stub([[]])
        assert stub.wait_for_profile_release("sess") is True
        assert stub.kills == []

    def test_it_waits_for_a_dying_chrome_then_proceeds(self, monkeypatch):
        """The normal case: Chrome is still exiting for a poll or two."""
        monkeypatch.setattr("main.time.sleep", lambda _s: None)
        stub = self._Stub([["123"], ["123"], []])
        assert stub.wait_for_profile_release("sess") is True
        assert stub.kills == []

    def test_it_kills_a_holder_that_never_leaves(self, monkeypatch):
        """A profile nothing can release is worse than one process lost."""
        monkeypatch.setattr("main.time.sleep", lambda _s: None)
        monkeypatch.setattr("main.time.monotonic", _clock([0, 0, 1, 99, 99, 99]))
        stub = self._Stub([["123"], ["123"], []])
        assert stub.wait_for_profile_release("sess", timeout=5) is True
        assert stub.kills == ["sess"]

    def test_it_reports_failure_when_the_kill_does_not_help(self, monkeypatch):
        """Returning False rather than blocking forever: start-session will
        probably be refused, but the caller still gets to report that."""
        monkeypatch.setattr("main.time.sleep", lambda _s: None)
        monkeypatch.setattr("main.time.monotonic", _clock([0, 0, 99]))
        stub = self._Stub([["123"]] * 30)
        assert stub.wait_for_profile_release("sess", timeout=5) is False
        assert stub.kills == ["sess"]

    def test_an_empty_session_name_is_a_no_op(self):
        stub = self._Stub([["123"]])
        assert stub.wait_for_profile_release("") is True
        assert stub.kills == []


def _clock(values):
    """A monotonic() stand-in that walks a fixed sequence, then holds."""
    seq = list(values)

    def _now():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _now


class TestTheKillerAcceptsAnExplicitSession:
    def test_it_defaults_to_the_current_session(self):
        source = inspect.getsource(MainWindow._kill_orphaned_chrome_for_session)
        assert 'session_name = session_name or (getattr(self, "token", "") or "").split(":")[0]' in source

    def test_the_pairing_flow_passes_the_session_it_just_closed(self):
        """Not the live token: the flow closes the PREVIOUS session, which for
        a freshly minted attempt is a different name entirely."""
        source = inspect.getsource(Connect.on_continue)
        assert "wait_for_profile_release(_session_name" in source
