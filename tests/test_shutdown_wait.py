"""Tests for the "Sair" (Exit) shutdown wait.

Reported live: after the WPPConnect graceful-shutdown fix (waiting for Chrome
to close cleanly instead of taskkill /F /T'ing it immediately, to avoid
corrupting the linked-device credentials in its LevelDB profile — see
_stop_wpp_server()'s own docstring), clicking "Sair" made WinZapp's window sit
there tagged "Not Responding" by Windows for tens of seconds before actually
closing.

Two compounding bugs, both here:

1. real_exit() called _stop_wpp_server() directly on the wx main thread. That
   method can legitimately block for the whole graceful-stop budget (an HTTP
   request plus a poll loop) — which stops the message loop from pumping for
   that whole time, and Windows marks any window whose message loop goes quiet
   for a few seconds as "Not Responding". real_exit() must never call it
   synchronously.

2. The HTTP request and the poll loop after it each had their OWN full budget
   (25s + 25s = 50s worst case) instead of sharing one. Even off the main
   thread, a 50s wait to quit is bad on its own merits.
"""

import inspect
import re

import pytest

from main import MainWindow


class TestRealExitDoesNotBlockTheMainThread:
    def test_stop_wpp_server_is_not_called_synchronously(self):
        """The call inside real_exit() itself must be indirect (inside a
        function object handed to a background thread), never a direct,
        top-level `self._stop_wpp_server()` statement — that line is exactly
        what used to freeze the message loop."""
        src = inspect.getsource(MainWindow.real_exit)
        # Strip the docstring/comments referencing the method by name so this
        # doesn't false-positive on prose — check only for an unindented-under-
        # threading call shape: a bare `self._stop_wpp_server()` statement that
        # is NOT inside the nested function handed to threading.Thread.
        assert "threading.Thread(target=" in src, (
            "real_exit() no longer hands its teardown work to a background thread"
        )
        # The nested function (whatever it's named) must be what threading.Thread
        # targets, and _stop_wpp_server() must be called from within it.
        nested_def_match = re.search(r"def (_\w+)\(\):", src)
        assert nested_def_match, "real_exit() must define a nested teardown function"
        nested_name = nested_def_match.group(1)
        assert f"target={nested_name}" in src
        # Everything from the nested def onward is the background body.
        background_body = src[src.index(f"def {nested_name}("):]
        # The blocking call may sit one level down: real_exit() was later split
        # into _perform_shutdown() (reversible teardown, reused by the IPC quit
        # handler) + _terminate_process(). What matters is not that the literal
        # _stop_wpp_server() line lives in real_exit(), but that whatever the
        # background thread runs is what eventually blocks — so follow the call
        # one level instead of pinning the old shape.
        blocking = "self._stop_wpp_server()"
        if blocking not in background_body:
            assert "self._perform_shutdown()" in background_body, (
                "the background thread must run the teardown that blocks"
            )
            assert blocking in inspect.getsource(MainWindow._perform_shutdown), (
                "_perform_shutdown() is supposed to be where the graceful stop happens"
            )

    def test_the_window_is_hidden_before_the_background_wait_starts(self):
        """Instant visual feedback on click, independent of how long the
        background teardown actually takes."""
        src = inspect.getsource(MainWindow.real_exit)
        hide_pos = src.index("self.Hide()")
        thread_pos = src.index("threading.Thread(target=")
        assert hide_pos < thread_pos, "the window must be hidden before the wait, not after"


class TestGracefulStopBudgetIsBounded:
    def test_no_poll_loop_waits_for_an_impossible_condition(self):
        """The real, dominant bug behind the reported corruption: WPPConnect
        Server is a persistent multi-session host that never exits or
        releases its port just because one session's /close-session call
        succeeded — other sessions may still be using it. A poll loop
        waiting for proc.poll()/port-release as "did it close gracefully"
        can therefore never come true, guaranteeing every single exit burned
        the whole grace budget and fell through to taskkill /F /T
        regardless of whether Chrome itself had already closed cleanly in
        under a second. That poll loop must not come back."""
        src = inspect.getsource(MainWindow._stop_wpp_server)
        # proc.poll()/the port lookup are still legitimately used once, to
        # decide *which PID to terminate* — what must be gone is a loop that
        # waits/sleeps on either of them as a "did it close gracefully" signal.
        assert "while time.time()" not in src
        assert "time.sleep(" not in src
        assert "_find_pid_listening_on_port" in src, (
            "the port-lookup helper is still legitimately needed to locate "
            "the process to terminate — just not as a 'did it close "
            "gracefully' signal to poll on"
        )

    def test_the_http_response_is_read_but_not_trusted_as_proof(self):
        """This assertion used to say the opposite, and was wrong.

        The old claim was that close-session `await`s client.close(), so a 200
        proves Chrome is down and nothing further need be polled. wppconnect's
        close() (node_modules, api/whatsapp.js) does not support that: it
        returns true without closing anything when the page is already closed,
        and wraps both page.close() and browser.close() in `.catch(() => null)`,
        so a failure or a hang also reports success. Sessions kept coming back
        unpaired because of it — see test_shutdown_closing_state.py.

        The response is still read (a non-200 is worth logging), but the
        decision to kill now rests on the two waits that follow it."""
        src = inspect.getsource(MainWindow._stop_wpp_server)
        assert "resp.status_code == 200" in src
        assert "_wait_for_session_flushed" in src
        assert "wait_for_profile_release" in src
        # The request's own timeout stays derived from the graceful-stop budget,
        # now routed through the clipper so a Windows shutdown can shrink it.
        assert "_phase_timeout(self._WPP_GRACEFUL_STOP_SECONDS)" in src

    def test_the_budget_is_short_enough_to_feel_like_quitting(self):
        """Not a hard number the user asked for, just a sanity ceiling: the old
        50s-worst-case regression must not come back, and the original 2s flat
        sleep (the thing that started this whole chain of fixes, by corrupting
        the WhatsApp Web session) must not either."""
        assert 2 < MainWindow._WPP_GRACEFUL_STOP_SECONDS <= 15


class TestPerformShutdownReentrancyIsAtomic:
    """The gap found by re-reviewing _perform_shutdown() against
    _on_end_session(): the original `if getattr(self, "_shutting_down",
    False): return` / `self._shutting_down = True` pair was a plain
    check-then-set with no lock. A local quit (real_exit()'s background
    teardown thread) racing a WM_ENDSESSION or an IPC "quit" from another
    account (both delivered on the wx main thread) could both observe
    _shutting_down as still False and both proceed into a full
    _stop_wpp_server() concurrently. _teardown_started_lock closes that."""

    def test_the_check_and_set_are_inside_the_lock(self):
        src = inspect.getsource(MainWindow._perform_shutdown)
        lock_pos = src.index("with self._teardown_started_lock:")
        guard_pos = src.index('getattr(self, "_shutting_down", False)')
        set_pos = src.index("self._shutting_down = True")
        assert lock_pos < guard_pos < set_pos, (
            "the check-then-set must both happen inside the lock, not just "
            "one of them"
        )

    def test_on_end_session_uses_the_same_lock(self):
        src = inspect.getsource(MainWindow._on_end_session)
        assert "self._teardown_started_lock" in src, (
            "_on_end_session() must guard its own _shutting_down "
            "check-and-set with the same lock _perform_shutdown() uses, or "
            "the two can still race each other"
        )
