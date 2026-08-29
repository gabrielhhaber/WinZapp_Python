"""Nothing on a quit path may block the wx main thread.

This is the accessibility invariant, not a performance nicety: a blocked
message loop repaints nothing and pumps no accessibility events, so a
screen-reader user gets total silence on a window Windows has already tagged
"Not Responding" — and their reasonable next move is the force-kill that
corrupts the session (see CLAUDE.md on shutdown_audit.log).

PR #141 raised every teardown budget for good reasons — an in-flight send
drain, a yield to a self-restart, a token-persist wait, a peer's whole
graceful teardown — which turned three paths that were merely slow into ones
that can freeze a window for a minute or more:

  * quit_all_accounts() asks each peer account to quit SEQUENTIALLY, and
    request_quit()'s default went from 10s to 85s. Three peers = 255s.
  * _ipc_quit() runs the full teardown, and the IPC listener dispatched it
    with wx.CallAfter — i.e. onto the main thread of an account that is not
    even the one the user asked to quit.
  * Connect._wait_for_pairing_startup_settled() polls for up to 30s and was
    called inline from the dialog's close handlers.
"""

import inspect

from main import MainWindow


class TestQuitAllAccounts:
    def test_the_peer_loop_runs_on_a_worker_thread(self):
        src = inspect.getsource(MainWindow.quit_all_accounts)
        assert "threading.Thread(target=" in src, (
            "quit_all_accounts() runs request_quit() for every peer inline on "
            "the wx main thread — with request_quit()'s 85s default that "
            "freezes this window for minutes"
        )
        # The request itself must live inside the worker, not before it.
        # (Compared against the nested def, not the Thread() call, so the
        # docstring naming request_quit() doesn't decide the outcome.)
        assert src.index("def _quit_peers_then_exit") < src.index("ipc.request_quit("), (
            "request_quit() is called outside the worker function"
        )

    def test_it_still_exits_when_there_are_no_peers_to_ask(self):
        """The early-out path (no account/global_dir) must reach real_exit()
        directly — deferring it to a thread that never starts would leave the
        app running after the user clicked Exit."""
        src = inspect.getsource(MainWindow.quit_all_accounts)
        assert "self.real_exit()" in src

    def test_the_docstring_no_longer_promises_a_short_wait(self):
        """It used to claim "an unresponsive peer never blocks our exit",
        which stopped being true the moment the timeout became 85s."""
        doc = MainWindow.quit_all_accounts.__doc__ or ""
        assert "never blocks our exit" not in doc


class TestIpcQuit:
    def test_the_listener_does_not_marshal_quit_onto_the_wx_thread(self):
        src = inspect.getsource(MainWindow._start_ipc_listener)
        # activate() is a genuine UI action and must stay on the wx thread.
        assert "wx.CallAfter(self._ipc_activate" in src
        assert "wx.CallAfter(self._ipc_quit)" not in src, (
            "_ipc_quit() runs the whole graceful teardown; on the wx thread "
            "it freezes THIS account's window while a different account quits"
        )
        assert "threading.Thread(" in src

    def test_real_exit_documents_the_same_rule(self):
        doc = MainWindow.real_exit.__doc__ or ""
        assert "_ipc_quit" in doc


class TestStopWppServerDocstring:
    def test_it_names_every_main_thread_caller(self):
        """The docstring is the only thing telling the next person which
        callers are deliberate exceptions. It claimed there was exactly one
        while _on_end_session() was a second."""
        doc = MainWindow._stop_wpp_server.__doc__ or ""
        assert "_update_wpp_server" in doc
        assert "_on_end_session" in doc
