"""A profile restore must be the only thing driving the session while it runs.

Found reviewing issue #203's fix. `_recovery_restart_active` has two owners:
_recover_suspect_profile()'s restore, and _force_whatsapp_session_restart(),
the power-resume zombie restart. The sequence that breaks:

1. The PC resumes and the startup grace reopens.
2. The power restart starts the session and polls it every 2 s.
3. The restarted profile is refused, and its first code starts a restore
   early — which is the new, intended behaviour for a code inside the grace.
4. The power restart then either reads QRCODE and returns, clearing the
   shared flag in its `finally`, or reads the CLOSED the restore produced
   and runs another close/start attempt.

Either way something starts Chrome over a profile being copied back: the
health poll's /start-session once the flag is gone, or the restart's own next
attempt. What that does depends on where the copy is. The restore may
kill the new browser, or the session may come up on an empty profile, with
the good snapshot spent and the original moved to `.broken`.

So the restore gets a flag only it clears (`_profile_restore_in_flight`),
every path that can start a session reads it, and no early restore starts
while another restart owns the session.
"""

import inspect
import time

from core.websocket_client import WebSocketClient
from main import MainWindow
from tests.god_modules import patch_main_global


class TestNothingStartsASessionUnderARestore:
    def test_the_auto_start_block_asks_whether_any_restart_owns_the_session(self):
        """Pinned on the behaviour, not on the source text: an earlier version
        of this test searched the call's source, which the comment beside it
        satisfied on its own."""
        class _Stub:
            _session_restart_owned = MainWindow._session_restart_owned
            _recovery_restart_active = False     # cleared by the other owner
            _profile_restore_in_flight = True

        assert _Stub()._session_restart_owned() is True
        _Stub._profile_restore_in_flight = False
        assert _Stub()._session_restart_owned() is False
        _Stub._recovery_restart_active = True
        assert _Stub()._session_restart_owned() is True

    def test_the_closed_branch_passes_that_answer_to_the_block(self):
        import ast
        import textwrap

        src = textwrap.dedent(inspect.getsource(MainWindow.check_wa_connection_http))
        calls = [
            node for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "auto_start_block_reason"
        ]
        assert len(calls) == 1
        arg = next(k.value for k in calls[0].keywords
                   if k.arg == "recovery_restart_active")
        assert isinstance(arg, ast.Call) and arg.func.attr == "_session_restart_owned", (
            "the CLOSED auto-start guard does not ask _session_restart_owned(), "
            "so a restore whose shared flag was cleared can be started over")

    def test_a_quit_yields_to_a_restore_whose_shared_flag_was_cleared(self):
        class _Stub:
            _yield_to_in_progress_self_restart = MainWindow._yield_to_in_progress_self_restart
            _session_restart_owned = MainWindow._session_restart_owned
            _SELF_RESTART_YIELD_SECONDS = 0.2
            _SELF_RESTART_YIELD_POLL_SECONDS = 0.05
            _recovery_restart_active = False
            _restarting_wpp_session = False
            _profile_restore_in_flight = True

        started = time.monotonic()
        _Stub()._yield_to_in_progress_self_restart()
        assert time.monotonic() - started >= 0.15, (
            "the quit did not wait for a restore still in flight")

    def test_a_restore_in_flight_is_a_self_inflicted_teardown(self):
        class _Stub:
            _self_inflicted_teardown_expected = MainWindow._self_inflicted_teardown_expected
            _shutting_down = False
            _wpp_updating = False
            _recovery_restart_active = False     # cleared by the other owner
            _restarting_wpp_session = False
            _profile_restore_in_flight = True

        assert _Stub()._self_inflicted_teardown_expected() is True

    def test_the_power_resume_restart_stops_before_restarting_under_a_restore(self):
        attempts = []

        class _Stub:
            _run_recovery_attempts = MainWindow._run_recovery_attempts
            _RECOVERY_MAX_ATTEMPTS = MainWindow._RECOVERY_MAX_ATTEMPTS
            _profile_restore_in_flight = True
            _wa_connected = False

            def _restart_session_once(self, token, attempt=1):
                attempts.append(attempt)

        import connection_state as cs

        _Stub()._run_recovery_attempts("tok", cs)

        assert attempts == [], (
            "the power-resume restart ran a close/start cycle while a profile "
            "restore owned the session")


class _WorthTryingStub:
    token = "sess123:tok"
    global_dir = "/g"

    def _profile_recovery_generation(self):
        return 0

    _profile_restore_worth_trying = MainWindow._profile_restore_worth_trying


class TestNoEarlyRestoreWhileAnotherRestartOwnsTheSession:
    def _stub(self, monkeypatch):
        patch_main_global(monkeypatch, "pick_restore_generation",
                            lambda *a, **kw: (False, "ok", False))
        return _WorthTryingStub()

    def test_not_during_the_power_resume_restart(self, monkeypatch):
        stub = self._stub(monkeypatch)
        stub._recovery_restart_active = True
        assert stub._profile_restore_worth_trying() is False

    def test_not_during_the_in_place_restart(self, monkeypatch):
        stub = self._stub(monkeypatch)
        stub._restarting_wpp_session = True
        assert stub._profile_restore_worth_trying() is False

    def test_once_that_restart_lets_go_the_next_code_may_restore(self, monkeypatch):
        """The power restart stops on its own at the QRCODE the refused profile
        reports, so waiting costs at most one code."""
        stub = self._stub(monkeypatch)
        stub._recovery_restart_active = False
        stub._restarting_wpp_session = False
        assert stub._profile_restore_worth_trying() is True


class TestAStuckRestoreCannotSilenceTheFloodForever:
    """Codes are left to a restore in flight — but only for
    _RESTORE_FLIGHT_IGNORE_SECONDS. A restore still producing codes past that
    has failed to stop the browser, and on `main` before this change those
    codes were at least counted towards the halt."""

    def _handler(self, started_at):
        halts = []

        class _MW:
            settings = {"privateinfo": {"paired": True}}
            _unattended_qr_events = 0
            _qr_flood_halted = False
            _auto_repair_dialog_shown = True      # already offered: halt only
            _pairing_in_progress = False
            _profile_restore_in_flight = True
            _profile_restore_started_at = started_at
            _wa_connect_announced = True
            _wa_startup_time = 0
            _WA_STARTUP_GRACE_SECONDS = 45

            def _is_pairing_dialog_active(self):
                return False

            def _profile_restore_worth_trying(self):
                return False

            def _halt_unattended_qr_session(self):
                halts.append(1)
                self._qr_flood_halted = True

        class _Stub:
            _handle_unattended_qr = WebSocketClient._handle_unattended_qr
            _pairing_attended = WebSocketClient._pairing_attended
            _qr_within_startup_grace = WebSocketClient._qr_within_startup_grace
            _UNATTENDED_QR_LIMIT = WebSocketClient._UNATTENDED_QR_LIMIT
            _REPAIR_DIALOG_CONFIRM_EVENTS = WebSocketClient._REPAIR_DIALOG_CONFIRM_EVENTS
            _RESTORE_FLIGHT_IGNORE_SECONDS = WebSocketClient._RESTORE_FLIGHT_IGNORE_SECONDS

            def __init__(self):
                self.main_window = _MW()

        return _Stub(), halts

    def test_a_recent_restore_still_owns_its_codes(self):
        s, halts = self._handler(time.monotonic())
        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT * 2):
            s._handle_unattended_qr()
        assert halts == []
        assert s.main_window._unattended_qr_events == 0

    def test_an_overdue_restore_gives_the_flood_its_ceiling_back(self):
        s, halts = self._handler(
            time.monotonic() - WebSocketClient._RESTORE_FLIGHT_IGNORE_SECONDS - 1)
        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT):
            s._handle_unattended_qr()
        assert halts == [1]

    def test_the_window_covers_the_restores_own_worst_case(self):
        """The restore's own worst case to stop a browser, about 81 s, read
        off wait_for_profile_release(), _chrome_pids_owning_session() and the
        kill. The window must clear it with room to spare, or a healthy slow
        restore would have its stragglers counted."""
        # 10 close + (20 + 15 last scan) release + 15 scan before the kill
        # + ~20.5 settle (its 5 s deadline is only checked after a 15 s scan).
        assert WebSocketClient._RESTORE_FLIGHT_IGNORE_SECONDS >= 2 * (10 + 35 + 15 + 20.5)

    def test_an_overdue_restore_does_not_open_the_dialog_on_a_confirmed_reading(self):
        """Past the window the codes count again, but only towards the halt:
        a confirmed reading must not reach the recovery (refused, it is still
        latched) and fall through to the pairing dialog over the stalled copy."""
        s, halts = self._handler(
            time.monotonic() - WebSocketClient._RESTORE_FLIGHT_IGNORE_SECONDS - 1)
        mw = s.main_window
        mw._auto_repair_dialog_shown = False
        dialogs = []
        s._show_repair_dialog = lambda *a, **kw: dialogs.append(1)
        mw._recover_suspect_profile = lambda *a, **kw: False

        for _ in range(WebSocketClient._REPAIR_DIALOG_CONFIRM_EVENTS):
            s._handle_unattended_qr()

        assert dialogs == []


class TestBothRestartPathsCheckAgainRightBeforeStarting:
    """A restore can begin while either restart is inside its close or its
    release wait — a tracker or confirmed-path restore, at a moment no code is
    flowing. The flag is read at the last moment before each start-session."""

    def _posts(self, monkeypatch):
        posts = []

        class _Resp:
            status_code = 200

        def fake_post(url, *a, **kw):
            posts.append(url.rsplit("/", 1)[-1])
            return _Resp()

        patch_main_global(monkeypatch, "api_post", fake_post)
        monkeypatch.setattr("main.requests.post", fake_post)
        monkeypatch.setattr("main.time.sleep", lambda *_: None)
        return posts

    def test_the_power_resume_restart(self, monkeypatch):
        posts = self._posts(monkeypatch)

        class _Stub:
            _restart_session_once = MainWindow._restart_session_once
            _RECOVERY_CLOSE_WAIT = MainWindow._RECOVERY_CLOSE_WAIT
            wpp_server = "http://127.0.0.1"
            wpp_port = 6300
            token = "sess:tok"
            _profile_restore_in_flight = False

            def _wait_for_status(self, predicate, timeout, stop_when_connected=True):
                return "CLOSED"

            def _kill_orphaned_chrome_for_session(self, *a, **kw):
                pass

            def wait_for_profile_release(self, session_name, timeout=20.0):
                self._profile_restore_in_flight = True   # a restore began meanwhile
                return True

        _Stub()._restart_session_once("sess:tok", 1)

        assert "close-session" in posts
        assert "start-session" not in posts
