"""The Windows teardown has to run at WM_QUERYENDSESSION, not WM_ENDSESSION.

Answering the query is what releases Windows to start ending processes, and
WPPConnect's Node is one of them — a separate console process CSRSS terminates
during the end phase, concurrently with our own WM_ENDSESSION handler and with
no ordering guarantee. Measured on one real shutdown (2026-09-10):

    02:20:14  node answering /list-chats in 79ms, session CONNECTED
    02:20:18  WM_QUERYENDSESSION, answered TRUE
    02:20:18  WM_ENDSESSION — teardown starts, capped at 4s
    02:20:20  close-session -> ConnectTimeout on 127.0.0.1:6300
    02:20:27  "no node pid to kill (proc gone / port free)"

Node was gone within two seconds of us saying yes, so the graceful
close-session had nothing to talk to, Chrome went down with it mid-write, and
the profile came back unable to restore the session. The budget was never the
constraint — the ORDERING was.

So these pin where the work happens, not only that it happens.
"""

import inspect
import threading

from main import MainWindow


class _Stub:
    _on_query_end_session = MainWindow._on_query_end_session
    _on_end_session = MainWindow._on_end_session
    _run_windows_session_teardown = MainWindow._run_windows_session_teardown
    _WINDOWS_SHUTDOWN_BUDGET = 4.0
    _END_SESSION_UNSTICK_SECONDS = 60.0

    def __init__(self):
        self._shutting_down = False
        self._teardown_started_lock = threading.Lock()
        self._teardown_complete_event = threading.Event()
        self.stopped_with = []
        self.flushes = 0
        self.audits = []

    def GetHandle(self):
        raise RuntimeError("no real window in a test")

    def _shutdown_audit(self, msg):
        self.audits.append(msg)

    def _stop_wpp_server(self, budget=None):
        self.stopped_with.append(budget)

    def _flush_pending_debounced_saves(self):
        self.flushes += 1

    def _restart_wpp_after_cancelled_shutdown(self):
        pass


class _Evt:
    def __init__(self):
        self.skipped = False

    def Skip(self):
        self.skipped = True


class TestTheQueryHandlerDoesTheWork:
    def test_answering_the_query_closes_the_session_first(self):
        stub = _Stub()

        stub._on_query_end_session(_Evt())

        assert stub.stopped_with == [stub._WINDOWS_SHUTDOWN_BUDGET], (
            "the teardown must run before we answer the query — by "
            "WM_ENDSESSION Windows may already have killed our Node"
        )
        assert stub.flushes == 1
        assert stub._teardown_complete_event.is_set()

    def test_it_still_answers_true(self):
        """Not answering yet is not answering FALSE, but skipping IS: the event
        would reach wxApp::OnQueryEndSession, which vetoes. A veto makes Windows
        block, time out and kill us mid-flush — the corruption this whole path
        exists to prevent."""
        stub = _Stub()
        evt = _Evt()

        stub._on_query_end_session(evt)

        assert evt.skipped is False

    def test_end_session_afterwards_does_not_tear_down_twice(self):
        """The normal sequence. The second handler must find the work done and
        get out of the way, not start a competing _stop_wpp_server()."""
        stub = _Stub()

        stub._on_query_end_session(_Evt())
        stub._on_end_session(_Evt())

        assert stub.stopped_with == [stub._WINDOWS_SHUTDOWN_BUDGET]
        assert stub.flushes == 1


class TestEndSessionIsStillALastChance:
    def test_a_session_end_with_no_query_before_it_still_closes_the_session(self):
        """`shutdown /f`, some logoff paths, and a session end another top-level
        window answered on our behalf all skip the query. Late is better than
        never — this is the only remaining chance to close the session."""
        stub = _Stub()

        stub._on_end_session(_Evt())

        assert stub.stopped_with == [stub._WINDOWS_SHUTDOWN_BUDGET]
        assert stub.flushes == 1


class TestTheOrderingIsPinnedInTheSource:
    def test_the_query_handler_registers_its_reason_before_working(self):
        """Overrunning Windows' hung-app timeout puts us on the blocking-apps
        screen under this string rather than killing anything — but only if it
        is registered before the work, not after it."""
        src = inspect.getsource(MainWindow._on_query_end_session)
        assert src.index("ShutdownBlockReasonCreate") < src.index(
            "_run_windows_session_teardown")

    def test_the_reason_is_destroyed_once_the_work_is_done(self):
        """Left registered in a process that outlives a cancelled shutdown, it
        would name WinZapp on the next one's blocking-apps screen for a teardown
        that already finished."""
        src = inspect.getsource(MainWindow._on_query_end_session)
        assert src.index("_run_windows_session_teardown") < src.index(
            "ShutdownBlockReasonDestroy")
