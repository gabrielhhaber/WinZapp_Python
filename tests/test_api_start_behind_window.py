"""A paired launch opens the main window while WPPConnect starts (issue #407).

The foreground start used to sit inside the modal "Starting WPPConnect, please
wait" dialog and build the main window only once Node answered: 2.5 s on a
measured Mac launch, for a window that needs nothing from Node. An account
that is already paired now gets its window at once and Node starts behind it.

What this file pins is everything the dialog-first order used to guarantee
for free, because each one fails silently when it is lost:

* only a paired account with a complete token takes the new path — pairing,
  token recovery, legacy-token migration and background launches all need Node
  before __init__ can go on, and keep the dialog;
* the threads that reach Node early wait for it — above all the abandoned-
  session cleanup, which with Node refusing deletes profiles without logging
  them out, leaving linked devices on the phone nothing can remove;
* __init__'s pairing check still happens, once Node can answer it;
* a Node that never comes up still ends in the startup error and a full quit;
* update prompts still wait for Node.
"""

import inspect
import threading
import types

import pytest

from main import MainWindow
from main_window import api_start_behind_window as asbw
from main_window.api_start_behind_window import (
    may_start_api_behind_window,
    wait_for_api_start,
)


def _decide(**overrides):
    args = dict(background_mode=False, custom_api=False, api_listening=False,
                api_files_present=True, resume_pending=False, paired=True,
                token="session:hash")
    args.update(overrides)
    return may_start_api_behind_window(**args)


class TestWhoOpensTheWindowFirst:
    def test_a_paired_account_with_a_complete_token(self):
        assert _decide() is True

    @pytest.mark.parametrize("overrides", [
        # Pairing comes next, and the pairing dialog needs the server.
        {"paired": False},
        {"resume_pending": True},
        # _recover_active_session_token() asks Node for the token.
        {"token": ""},
        # retrieve_token() migrates a token without ":<hash>" through Node.
        {"token": "session"},
        # Background launches wait for Node in __init__ on purpose
        # (docs/traps/session-startup.md).
        {"background_mode": True},
        # Not ours to start.
        {"custom_api": True},
        # Adopted in milliseconds by ensure_wpp_running(); nothing to gain.
        {"api_listening": True},
        # ensure_wpp_running() skips a start it cannot make; so must this.
        {"api_files_present": False},
    ])
    def test_every_other_launch_keeps_the_dialog(self, overrides):
        assert _decide(**overrides) is False


class _Window:
    pass


class TestWaitForApiStart:
    def test_an_ordinary_launch_never_waits(self):
        assert wait_for_api_start(_Window()) is True

    def test_a_start_that_came_up(self):
        w = _Window()
        w._api_start_settled = threading.Event()
        w._api_start_settled.set()
        w._api_start_failed = False
        assert wait_for_api_start(w) is True

    def test_a_start_that_failed_stops_the_caller(self):
        w = _Window()
        w._api_start_settled = threading.Event()
        w._api_start_settled.set()
        w._api_start_failed = True
        assert wait_for_api_start(w) is False

    def test_it_really_blocks_while_the_start_runs(self):
        w = _Window()
        w._api_start_settled = threading.Event()
        w._api_start_failed = False
        assert wait_for_api_start(w, timeout=0.05) is False
        threading.Timer(0.05, w._api_start_settled.set).start()
        assert wait_for_api_start(w, timeout=5) is True


class _Clock:
    """time.time()/sleep() that advance instantly."""

    def __init__(self):
        self.now = 1000.0
        self.on_sleep = None

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep()

    def perf_counter(self):
        return self.now


class _WorkerStub:
    _api_start_behind_window_worker = MainWindow._api_start_behind_window_worker
    _run_api_start_behind_window = MainWindow._run_api_start_behind_window
    _fail_api_start_behind_window = MainWindow._fail_api_start_behind_window
    _report_api_start_failure = MainWindow._report_api_start_failure
    _SPAWN_WAIT_POLL_SECONDS = 0.01

    def __init__(self, answers_after=None, shutting_down=False):
        self.events = []
        self._answers_after = answers_after   # polls before the port opens
        self._polls = 0
        self._pre_spawn_asked = False
        self._shutting_down = shutting_down
        self._wpp_updating = False
        self._api_start_failed = False
        self._api_start_settled = threading.Event()
        self._wa_startup_time = 0.0

    def _start_wpp_background(self):
        self.events.append("spawn")

    def _register_node_lease(self):
        self.events.append("lease")

    def _leave_starting_wppconnect_status(self):
        pass

    def _is_wpp_running(self):
        # The UI-thread spawn asks once before spawning (a Node another
        # account started meanwhile is adopted); that ask is not a poll.
        if not self._pre_spawn_asked and not self._shutting_down:
            self._pre_spawn_asked = True
            return False
        self._polls += 1
        return self._answers_after is not None and self._polls > self._answers_after

    def _check_wpp_version_pin(self):
        self.events.append("pin")

    def _reset_startup_probe(self):
        self.events.append("reset_probe")

    def _show_api_startup_failure(self):
        self.events.append("error")

    def real_exit(self):
        self.events.append("exit")


class _CallAfter:
    """wx.CallAfter that records being used and runs the call inline — the
    UI thread's turn, as far as the worker can tell."""

    def __init__(self, run=True):
        self.calls = []
        self.run = run

    def __call__(self, fn, *a, **k):
        self.calls.append(getattr(fn, "__name__", repr(fn)))
        if self.run:
            fn(*a, **k)


@pytest.fixture
def worker_env(monkeypatch):
    clock = _Clock()
    call_after = _CallAfter()
    monkeypatch.setattr(asbw, "time", clock)
    monkeypatch.setattr("core.wa_version_refresh.wait_for_refresh", lambda: None)
    monkeypatch.setattr(asbw.wx, "CallAfter", call_after)
    return types.SimpleNamespace(clock=clock, call_after=call_after)


class TestTheStartBehindTheWindow:
    def test_spawns_once_then_settles_when_the_port_answers(self, worker_env):
        stub = _WorkerStub(answers_after=3)
        stub._api_start_behind_window_worker()
        assert stub.events == ["spawn", "pin", "reset_probe"]
        assert stub._api_start_settled.is_set() and not stub._api_start_failed
        assert wait_for_api_start(stub) is True

    def test_the_spawn_runs_on_the_ui_thread(self, worker_env):
        """_start_wpp_background() rewrites os.environ; a setenv from a worker
        while Cocoa/wx builds the window can crash on macOS."""
        stub = _WorkerStub(answers_after=0)
        spawned_from = []
        stub._start_wpp_background = lambda: spawned_from.append(list(worker_env.call_after.calls))
        stub._api_start_behind_window_worker()
        assert spawned_from == [["_spawn"]]

    def test_the_offline_grace_starts_when_node_does(self, worker_env):
        """__init__ armed it while Node was still booting; left there, a slow
        start would have spent the grace before WhatsApp was even asked."""
        stub = _WorkerStub(answers_after=20)
        stub._api_start_behind_window_worker()
        assert stub._wa_startup_time == worker_env.clock.now

    def test_a_node_that_never_answers_ends_in_the_startup_error_and_a_quit(self, worker_env):
        stub = _WorkerStub(answers_after=None)
        stub._api_start_behind_window_worker()
        assert stub.events == ["spawn", "error", "exit"]
        assert "_report_api_start_failure" in worker_env.call_after.calls
        assert stub._api_start_failed and stub._api_start_settled.is_set()
        assert wait_for_api_start(stub) is False

    def test_it_waits_the_dialogs_full_budget(self, worker_env):
        start = worker_env.clock.now
        _WorkerStub(answers_after=None)._api_start_behind_window_worker()
        assert worker_env.clock.now - start >= asbw.API_START_TIMEOUT_SECONDS

    def test_a_spawn_that_raises_still_releases_everyone(self, worker_env):
        """_start_wpp_background() re-checks the port under a cross-process
        lock that can time out. Unhandled, the waiters would block forever and
        the app would sit on "connecting" for good."""
        stub = _WorkerStub(answers_after=0)

        def _raise():
            raise TimeoutError("node_port_lock")
        stub._start_wpp_background = _raise
        stub._api_start_behind_window_worker()
        assert stub._api_start_settled.is_set()
        assert wait_for_api_start(stub) is False
        assert stub.events == ["error", "exit"]

    def test_any_later_error_still_releases_everyone(self, worker_env):
        stub = _WorkerStub(answers_after=0)

        def _raise():
            raise RuntimeError("log unreadable")
        stub._check_wpp_version_pin = _raise
        stub._api_start_behind_window_worker()
        assert stub._api_start_settled.is_set()
        assert stub.events == ["spawn", "error", "exit"]

    def test_a_quit_before_the_queued_spawn_runs_spawns_nothing(self, worker_env):
        """real_exit()'s teardown has already looked for a Node to stop; one
        spawned now would outlive the process. Checked on the UI thread, in
        the same turn as the spawn."""
        stub = _WorkerStub(answers_after=None, shutting_down=True)
        stub._api_start_behind_window_worker()
        assert "spawn" not in stub.events
        assert "error" not in stub.events and "exit" not in stub.events
        assert wait_for_api_start(stub) is False

    def test_a_spawn_that_never_runs_because_the_app_quits_does_not_hang(self, worker_env):
        """The main loop can end before a queued CallAfter runs."""
        worker_env.call_after.run = False
        stub = _WorkerStub(answers_after=None)
        stub._shutting_down = True
        stub._api_start_behind_window_worker()
        assert stub.events == []
        assert stub._api_start_settled.is_set() and stub._api_start_failed

    def test_a_cancelled_shutdown_that_restarts_node_is_still_reported_up(self, worker_env):
        """_restart_wpp_after_cancelled_shutdown() spawns Node itself; the
        threads waiting on this start must then be released, not stranded."""
        stub = _WorkerStub(answers_after=5, shutting_down=True)
        stub._api_start_behind_window_worker()
        assert "spawn" not in stub.events
        assert wait_for_api_start(stub) is True

    def test_an_update_in_progress_owns_the_failure(self, worker_env):
        """ensure_wpp_running()'s own rule: no "API failed to start" and no
        quit while a WPPConnect update or reinstall holds the API. Its waiters
        are released as an ordinary start once the update is done — the
        health checker among them is what the update's recovery relies on."""
        stub = _WorkerStub(answers_after=None)
        stub._wpp_updating = True
        ticks = []

        def _update_finishes():
            ticks.append(1)
            if len(ticks) > asbw.API_START_TIMEOUT_SECONDS + 5:
                stub._wpp_updating = False
        worker_env.clock.on_sleep = _update_finishes
        stub._api_start_behind_window_worker()
        assert "error" not in stub.events and "exit" not in stub.events
        assert stub._api_start_settled.is_set()
        assert wait_for_api_start(stub) is True

    def test_start_settles_the_port_before_claiming_a_start(self, monkeypatch):
        order = []

        class _Thread:
            def __init__(self, target=None, name=None, daemon=None):
                pass

            def start(self):
                order.append("thread")

        # Narrowly: only the module under test sees the fake Thread.
        monkeypatch.setattr(asbw, "threading",
                            types.SimpleNamespace(Thread=_Thread, Event=threading.Event))

        class _Stub:
            _start_api_behind_window = MainWindow._start_api_behind_window
            _api_start_behind_window_worker = lambda self: None

            def _ensure_wpp_port_still_free(self):
                order.append(("port", hasattr(self, "_api_start_settled")))

        stub = _Stub()
        stub._start_api_behind_window()
        assert order == [("port", False), "thread"]
        assert stub._api_started_behind_window is True
        assert not stub._api_start_settled.is_set()

    def test_a_port_settle_that_raises_leaves_no_start_to_wait_for(self):
        """__init__ logs the error and carries on; nothing may then block
        forever on an event no worker will ever set."""
        class _Stub:
            _start_api_behind_window = MainWindow._start_api_behind_window

            def _ensure_wpp_port_still_free(self):
                raise OSError("bind")

        stub = _Stub()
        with pytest.raises(OSError):
            stub._start_api_behind_window()
        assert wait_for_api_start(stub) is True


class _DecisionStub:
    _may_start_api_behind_window = MainWindow._may_start_api_behind_window

    def __init__(self, paired=True, token="session:hash"):
        self.settings = {"privateinfo": {"paired": paired}}
        self._token = token
        self.background_mode = False
        self.wpp_custom_api = False
        self.resume_pending = False

    def _get_wa_token(self):
        if isinstance(self._token, Exception):
            raise self._token
        return self._token

    def _is_wpp_running(self):
        return False


class TestTheDecisionReadsTheRealState:
    @pytest.fixture(autouse=True)
    def _files_present(self, monkeypatch):
        monkeypatch.setattr(asbw, "bundled_api_present", lambda: True)

    def test_a_paired_account_with_its_token(self):
        assert _DecisionStub()._may_start_api_behind_window() is True

    def test_an_unreadable_token_keeps_the_dialog(self):
        """The vault failing to decrypt is a reason for the old order, never
        for skipping the start."""
        stub = _DecisionStub(token=ValueError("bad key"))
        assert stub._may_start_api_behind_window() is False

    def test_missing_privateinfo_keeps_the_dialog(self):
        stub = _DecisionStub()
        stub.settings = {}
        assert stub._may_start_api_behind_window() is False

    def test_a_pending_account_keeps_the_dialog(self):
        stub = _DecisionStub()
        stub.resume_pending = True
        assert stub._may_start_api_behind_window() is False


class _Connect:
    def __init__(self, answers):
        self._answers = list(answers)
        self.dialogs = 0

    def check_connection_status(self):
        return self._answers.pop(0)

    def show_connection_dial(self):
        self.dialogs += 1


class _Registry:
    def __init__(self):
        self.states = {}

    def set_state(self, account_id, state):
        self.states[account_id] = state


class _PairingStub:
    _confirm_pairing_behind_window = MainWindow._confirm_pairing_behind_window
    _pair_account_at_startup = MainWindow._pair_account_at_startup

    def __init__(self, answers, switch=False):
        self.connect = _Connect(answers)
        self._switch = switch
        self.events = []
        self._just_paired = False
        self.account_id = "acc1"
        self.registry = _Registry()
        self.resume_pending = True

    def run_on_main_thread(self, fn, *a, **k):
        return fn(*a, **k)

    def _offer_switch_when_unpaired(self):
        self.events.append("offer")
        return self._switch

    def _check_quick_tip(self):
        self.events.append("tip")

    def real_exit(self):
        self.events.append("exit")


@pytest.fixture
def call_after_inline(monkeypatch):
    monkeypatch.setattr(asbw.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


class TestTheSharedStartupPairingCheck:
    """__init__ and the start behind the window run the same check; only how
    the process ends differs (sys.exit() in __init__, real_exit() later)."""

    def test_outcomes(self):
        assert _PairingStub([True])._pair_account_at_startup() == "connected"
        assert _PairingStub([False], switch=True)._pair_account_at_startup() == "switching"
        assert _PairingStub([False, False])._pair_account_at_startup() == "unpaired"
        stub = _PairingStub([False, True])
        assert stub._pair_account_at_startup() == "paired"
        assert stub._just_paired and stub.registry.states == {"acc1": "paired"}

    def test_init_uses_it(self):
        src = inspect.getsource(MainWindow.__init__)
        before_post = src[:src.index("def _post_ui_init")]
        assert "self._pair_account_at_startup()" in before_post
        assert "_offer_switch_when_unpaired" not in before_post


class TestThePairingCheckMovesBehindTheWindow:
    def test_a_paired_session_goes_straight_on(self, call_after_inline):
        stub = _PairingStub([True])
        assert stub._confirm_pairing_behind_window() is True
        assert stub.events == [] and stub.connect.dialogs == 0

    def test_switching_to_another_account_stops_here(self, call_after_inline):
        stub = _PairingStub([False], switch=True)
        assert stub._confirm_pairing_behind_window() is False
        assert stub.events == ["offer"] and stub.connect.dialogs == 0

    def test_pairing_through_the_dialog_does_what_init_did(self, call_after_inline):
        stub = _PairingStub([False, True])
        assert stub._confirm_pairing_behind_window() is True
        assert stub.connect.dialogs == 1
        assert stub._just_paired is True
        assert stub.registry.states == {"acc1": "paired"}
        assert stub.resume_pending is False
        assert stub.events == ["offer", "tip"]

    def test_closing_the_dialog_unpaired_quits_the_whole_app(self, call_after_inline):
        """__init__ ended the process here; with the window already up only
        real_exit() does that without leaving anything behind."""
        stub = _PairingStub([False, False])
        assert stub._confirm_pairing_behind_window() is False
        assert stub.events == ["offer", "exit"]


class TestTheEarlyThreadsWait:
    def _pending(self):
        w = types.SimpleNamespace()
        w._api_start_settled = threading.Event()
        w._api_start_failed = False
        return w

    def test_update_prompts_wait_for_node(self):
        w = self._pending()
        w._ui_ready_event = threading.Event()
        w._ui_ready_event.set()
        w._is_pairing_dialog_active = lambda: False
        w._pairing_in_progress = False
        may_run = MainWindow.wpp_update_may_run_now
        assert may_run(w) is False
        w._api_start_settled.set()
        assert may_run(w) is True

    def test_the_abandoned_session_cleanup_never_runs_against_a_refusing_node(self):
        w = self._pending()
        w._api_start_settled.set()
        w._api_start_failed = True
        w.wpp_custom_api = False

        def _boom():
            raise AssertionError("cleanup went on without Node")
        w._get_session_store = _boom
        MainWindow._cleanup_abandoned_sessions_worker(w)

    def test_the_messages_set_probe_waits(self, monkeypatch):
        import main_window.sync as sync_mod

        class _InlineThread:
            def __init__(self, target=None, daemon=None, **_k):
                self._target = target

            def start(self):
                self._target()

        monkeypatch.setattr(sync_mod, "threading", types.SimpleNamespace(Thread=_InlineThread))
        w = self._pending()
        w._api_start_settled.set()
        w._api_start_failed = True
        w._probe_chats_and_start_sync = lambda: pytest.fail("probed a dead port")
        MainWindow.wait_messages_set(w)

    def test_the_health_checker_and_post_ui_init_wait(self):
        loop = inspect.getsource(MainWindow.start_connection_health_checker)
        assert loop.index("wait_for_api_start(self)") < loop.index("self.check_wa_connection_http()")
        init = inspect.getsource(MainWindow.__init__)
        post = init[init.index("def _post_ui_init"):]
        assert post.index("wait_for_api_start(self)") < post.index("STEP 2")
        assert post.index("_confirm_pairing_behind_window()") < post.index("STEP 2")


class TestInitWiring:
    def test_init_chooses_between_the_two_starts(self):
        src = inspect.getsource(MainWindow.__init__)
        decide = src.index("self._may_start_api_behind_window()")
        assert decide < src.index("self._start_api_behind_window()")
        assert decide < src.index("self.ensure_wpp_running()")

    def test_inits_own_pairing_check_is_skipped_only_for_that_start(self):
        src = inspect.getsource(MainWindow.__init__)
        check = src.index("if not self.background_mode and not self._api_started_behind_window:")
        assert check < src.index("self._pair_account_at_startup()")


class TestStartingWppconnectTitle:
    """The title keeps the phrase the startup dialog used to show."""

    def test_init_starts_the_title_status_as_starting_wppconnect(self):
        src = inspect.getsource(MainWindow.__init__)
        assert '"tray_starting_wppconnect"' in src
        assert "if self._api_started_behind_window" in src

    def test_the_status_is_a_known_tray_status_key(self):
        assert "tray_starting_wppconnect" in MainWindow._TRAY_STATUS_KEYS

    def _stub(self, key):
        calls = []
        stub = types.SimpleNamespace(
            _tray_status_key=key,
            i18n=types.SimpleNamespace(t=lambda k: k),
            _set_status=calls.append)
        return stub, calls

    def test_node_answering_moves_the_title_on_to_connecting(self):
        stub, calls = self._stub("tray_starting_wppconnect")
        asbw.ApiStartBehindWindowMixin._leave_starting_wppconnect_status(stub)
        assert calls == ["tray_connecting"]

    def test_a_later_status_is_left_alone(self):
        stub, calls = self._stub("synchronizing")
        asbw.ApiStartBehindWindowMixin._leave_starting_wppconnect_status(stub)
        assert calls == []


class TestRestartUnderOpenWindow:
    """Update / forced reinstall / rollback restarts show no modal either."""

    def test_window_is_up_only_after_post_ui_init_and_not_in_background(self):
        up = asbw.ApiStartBehindWindowMixin._main_window_is_up
        ready = threading.Event()
        stub = types.SimpleNamespace(_ui_ready_event=ready, background_mode=False)
        assert up(stub) is False
        ready.set()
        assert up(stub) is True
        stub.background_mode = True
        assert up(stub) is False

    def _under_stub(self, monkeypatch, main_thread, running):
        calls = []
        started = []
        monkeypatch.setattr(asbw.wx, "CallAfter", lambda f, *a: calls.append(f))
        monkeypatch.setattr(asbw.wx, "IsMainThread", lambda: main_thread)
        monkeypatch.setattr(asbw.threading, "Thread",
                            lambda **kw: types.SimpleNamespace(start=lambda: started.append(kw["name"])))
        stub = types.SimpleNamespace(
            _ensure_wpp_port_still_free=lambda: None,
            _start_wpp_background_after_catalogue=lambda: calls.append("spawn"),
            _is_wpp_running=lambda: running,
            _check_wpp_version_pin=lambda: None,
            _enter_starting_wppconnect_status=lambda: None,
            _leave_starting_wppconnect_status=lambda: None,
            _clear_starting_status_when_up=lambda: None)
        stub._wait_until_api_listening = lambda: asbw.ApiStartBehindWindowMixin._wait_until_api_listening(stub)
        return stub, calls, started

    def test_on_the_ui_thread_it_queues_the_spawn_and_never_blocks(self, monkeypatch):
        # A Node that never answers would hang the window if this polled.
        stub, calls, started = self._under_stub(monkeypatch, True, running=False)
        assert asbw.ApiStartBehindWindowMixin._start_api_under_open_window(stub) is True
        assert "spawn" in calls
        assert started == ["api-start-title"]

    def test_on_a_worker_it_waits_and_returns_true_once_node_answers(self, monkeypatch):
        stub, calls, _ = self._under_stub(monkeypatch, False, running=True)
        assert asbw.ApiStartBehindWindowMixin._start_api_under_open_window(stub) is True

    def test_the_title_is_not_taken_from_a_running_sync(self):
        calls = []
        stub = types.SimpleNamespace(_tray_status_key="synchronizing",
                                     i18n=types.SimpleNamespace(t=lambda k: k),
                                     _set_status=calls.append)
        asbw.ApiStartBehindWindowMixin._enter_starting_wppconnect_status(stub)
        assert calls == []


    def test_a_node_that_appeared_while_the_window_built_is_adopted_not_respawned(self, worker_env):
        stub = _WorkerStub(answers_after=None)
        stub._pre_spawn_asked = True
        stub._is_wpp_running = lambda: True
        stub._api_start_behind_window_worker()
        assert stub.events[0] == "lease"
        assert "spawn" not in stub.events
