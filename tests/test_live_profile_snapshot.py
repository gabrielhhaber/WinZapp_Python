"""Settings > Cópia de segurança, in MainWindow: the snapshot at close follows
the chosen interval, and the backup with WinZapp open closes the session,
stages a copy of the released profile, starts the session again and keeps the
copy only once that session is back and stays connected — only when due, only
when nothing else owns the session, and asking first (with the window in
front) unless told not to.

MainWindow is a wx.Frame, so the methods run against small stubs.
"""

import types

import pytest

import main as main_module
from main import MainWindow
from core.profile_recovery import SNAPSHOT_MAX_AGE_SECONDS
from tests.test_restart_wpp_session import _Stub as _RestartStub
from tests.god_modules import patch_main_global

HOUR = 3600


class _Tracker:
    def __init__(self, connected=True):
        self._connected = connected

    def ever_connected(self):
        return self._connected


class _Queue:
    """The outgoing message queue, recording what the backup asks of it."""

    def __init__(self, events, work=False, drains=True):
        self.events = events
        self.work = work
        self.drains = drains
        self.held = False

    def has_work(self):
        return self.work

    def hold(self):
        self.held = True
        self.events.append("hold")

    def release(self):
        self.held = False
        self.events.append("release")

    def is_held(self):
        return self.held

    def wait_until_idle(self, timeout):
        self.events.append("drain")
        return self.drains


class _Stub:
    _maybe_refresh_profile_snapshot_live = MainWindow._maybe_refresh_profile_snapshot_live
    _live_snapshot_blocked_reason = MainWindow._live_snapshot_blocked_reason
    _live_snapshot_session = MainWindow._live_snapshot_session
    _live_snapshot_cancelled = MainWindow._live_snapshot_cancelled
    _ask_live_profile_snapshot = MainWindow._ask_live_profile_snapshot
    _postpone_live_snapshot = MainWindow._postpone_live_snapshot
    _refresh_profile_snapshot_live = MainWindow._refresh_profile_snapshot_live
    _self_inflicted_teardown_expected = MainWindow._self_inflicted_teardown_expected
    _session_restart_owned = MainWindow._session_restart_owned
    _WPP_SESSION_RESTART_COOLDOWN = MainWindow._WPP_SESSION_RESTART_COOLDOWN
    _LIVE_SNAPSHOT_BUDGET_SECONDS = MainWindow._LIVE_SNAPSHOT_BUDGET_SECONDS
    _LIVE_SNAPSHOT_DRAIN_SECONDS = MainWindow._LIVE_SNAPSHOT_DRAIN_SECONDS

    def __init__(self, **backup):
        section = {"live_snapshot_enabled": True, "live_snapshot_interval_hours": 1,
                   "live_snapshot_confirm": True}
        section.update(backup)
        self.settings = {"profile_backup": section}
        self._wa_connected = True
        self._profile_health = _Tracker()
        self.global_dir = "G"
        self.token = "sess:tok"
        self.i18n = types.SimpleNamespace(t=lambda key: key)
        self.pairing = False
        self.active = True
        self.accepted = True
        self.restart_runs = True
        self.spoken, self.audits, self.restarts = [], [], []
        self.events = []
        self.message_queue = _Queue(self.events)
        self.saved = 0
        self.workers = 0

    def _is_pairing_dialog_active(self):
        return self.pairing

    def _window_can_ask(self):
        return self.active

    def _live_snapshot_session_accepted(self):
        return self.accepted

    def output(self, text, interrupt=False):
        self.spoken.append(text)

    def save_settings(self):
        self.saved += 1

    def _shutdown_audit(self, line):
        self.audits.append(line)

    def _start_live_snapshot_worker(self):
        self.workers += 1
        self._refresh_profile_snapshot_live()

    def _restart_wpp_session(self, on_profile_released=None, reason=None):
        self.restarts.append(reason)
        self.events.append("restart")
        if not self.restart_runs:
            return False
        if on_profile_released is not None:
            on_profile_released()
        return True


@pytest.fixture(autouse=True)
def _sync_call_after(monkeypatch):
    monkeypatch.setattr(main_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))


@pytest.fixture
def snapshot(monkeypatch):
    """The snapshot's age and every copy, promotion and discard — no disk."""
    state = types.SimpleNamespace(age=None, copies=[], staged=True, promoted=[],
                                  discarded=[], promote_ok=True)
    monkeypatch.setattr("core.profile_recovery.snapshot_age_seconds",
                        lambda global_dir, session_name: state.age)

    def _capture(global_dir, session_name, **kw):
        state.copies.append((global_dir, session_name, kw))
        return state.staged

    def _promote(global_dir, session_name):
        state.promoted.append(session_name)
        return state.promote_ok

    monkeypatch.setattr("core.profile_recovery.capture_snapshot", _capture)
    monkeypatch.setattr("core.profile_recovery.promote_pending_snapshot", _promote)
    monkeypatch.setattr("core.profile_recovery.discard_pending_snapshot",
                        lambda global_dir, session_name: state.discarded.append(session_name))
    return state


@pytest.fixture
def answer(monkeypatch):
    """What the confirmation dialog answers: (confirmed, don't ask again)."""
    state = types.SimpleNamespace(value=(True, False), asked=0)

    def _confirm(*a, **kw):
        state.asked += 1
        assert kw["default_yes"] is False
        return state.value

    patch_main_global(monkeypatch, "confirm_with_checkbox", _confirm)
    return state


def _due(stub, now=HOUR + 10):
    """Start the clock at 0, then poll once an interval later."""
    stub._maybe_refresh_profile_snapshot_live(now=0)
    stub._maybe_refresh_profile_snapshot_live(now=now)


class TestWhenItRuns:
    def test_the_first_poll_only_starts_the_clock(self, snapshot, answer):
        stub = _Stub()
        stub._maybe_refresh_profile_snapshot_live(now=50 * HOUR)
        assert answer.asked == 0 and stub.restarts == []
        assert stub._live_snapshot_last_attempt == 50 * HOUR

    def test_due_asks_and_a_yes_backs_up(self, snapshot, answer):
        stub = _Stub()
        _due(stub)
        assert answer.asked == 1
        assert stub.restarts == ["profile backup"]
        assert len(snapshot.copies) == 1 and snapshot.promoted == ["sess"]
        assert stub._live_snapshot_pending is False

    def test_not_before_an_interval_of_uptime(self, snapshot, answer):
        stub = _Stub()
        _due(stub, now=HOUR - 1)
        assert answer.asked == 0 and stub.restarts == []

    def test_not_while_the_snapshot_is_fresh(self, snapshot, answer):
        snapshot.age = 10
        stub = _Stub()
        _due(stub)
        assert answer.asked == 0 and stub.restarts == []

    def test_never_when_the_option_is_off(self, snapshot, answer):
        stub = _Stub(live_snapshot_enabled=False)
        _due(stub, now=100 * HOUR)
        assert answer.asked == 0 and stub.restarts == []

    def test_without_confirmation_it_backs_up_directly(self, snapshot, answer):
        stub = _Stub(live_snapshot_confirm=False)
        stub.active = False   # nothing to be seen: no question is asked
        _due(stub)
        assert answer.asked == 0
        assert stub.workers == 1 and stub.restarts == ["profile backup"]

    def test_a_pending_backup_is_not_asked_about_twice(self, snapshot, answer, monkeypatch):
        prompts = []
        monkeypatch.setattr(main_module.wx, "CallAfter", lambda fn, *a, **kw: prompts.append(fn))
        stub = _Stub()
        _due(stub)
        stub._maybe_refresh_profile_snapshot_live(now=5 * HOUR)
        assert len(prompts) == 1


class TestNotNow:
    @pytest.mark.parametrize("block", [
        lambda s: setattr(s, "_wa_connected", False),
        lambda s: setattr(s, "_initial_sync_running", True),
        lambda s: setattr(s, "_media_sync_running", True),
        lambda s: setattr(s, "_restarting_wpp_session", True),
        lambda s: setattr(s, "_profile_restore_in_flight", True),
        lambda s: setattr(s, "_recovery_restart_active", True),
        lambda s: setattr(s, "_shutting_down", True),
        lambda s: setattr(s, "_wpp_updating", True),
        lambda s: setattr(s, "_last_wpp_session_restart_ts", main_module.time.time()),
        lambda s: setattr(s, "_pairing_in_progress", True),
        lambda s: setattr(s, "pairing", True),
        lambda s: setattr(s, "_profile_health", _Tracker(connected=False)),
        lambda s: setattr(s, "token", ""),
        lambda s: setattr(s, "active", False),
        lambda s: setattr(s.message_queue, "work", True),
    ], ids=["offline", "initial-sync", "media-sync", "restarting", "restoring",
            "recovering", "shutting-down", "updating", "restart-cooldown", "pairing",
            "pairing-dialog", "never-connected", "no-session", "window-not-active",
            "messages-being-sent"])
    def test_left_alone_and_retried_at_the_next_poll(self, snapshot, answer, block):
        stub = _Stub()
        stub._maybe_refresh_profile_snapshot_live(now=0)
        block(stub)
        stub._maybe_refresh_profile_snapshot_live(now=HOUR + 10)
        assert answer.asked == 0 and stub.restarts == []
        # Not consumed: the clock still says "due".
        assert stub._live_snapshot_last_attempt == 0

    def test_a_situation_that_changed_while_asking_backs_up_nothing(self, snapshot, monkeypatch):
        stub = _Stub()
        stub._maybe_refresh_profile_snapshot_live(now=0)

        def _confirm_then_go_offline(*a, **kw):
            stub._wa_connected = False
            return True, False

        patch_main_global(monkeypatch, "confirm_with_checkbox", _confirm_then_go_offline)
        stub._maybe_refresh_profile_snapshot_live(now=HOUR + 10)
        assert stub.restarts == [] and stub.spoken == []
        assert stub._live_snapshot_pending is False

    def test_turned_off_while_asking_backs_up_nothing(self, snapshot, monkeypatch):
        stub = _Stub()
        stub._maybe_refresh_profile_snapshot_live(now=0)

        def _confirm_then_turn_off(*a, **kw):
            stub.settings["profile_backup"]["live_snapshot_enabled"] = False
            return True, False

        patch_main_global(monkeypatch, "confirm_with_checkbox", _confirm_then_turn_off)
        stub._maybe_refresh_profile_snapshot_live(now=HOUR + 10)
        assert stub.restarts == []


class TestTheQuestion:
    def test_no_postpones_by_a_whole_interval(self, snapshot, answer):
        answer.value = (False, False)
        stub = _Stub()
        _due(stub, now=HOUR + 10)
        assert stub.restarts == [] and stub._live_snapshot_pending is False
        stub._maybe_refresh_profile_snapshot_live(now=HOUR + 70)
        assert answer.asked == 1
        stub._maybe_refresh_profile_snapshot_live(now=2 * HOUR + 20)
        assert answer.asked == 2

    def test_dont_ask_again_with_yes_turns_asking_off(self, snapshot, answer):
        answer.value = (True, True)
        stub = _Stub()
        _due(stub)
        assert stub.settings["profile_backup"]["live_snapshot_confirm"] is False
        assert stub.saved == 1

    def test_dont_ask_again_with_no_changes_nothing(self, snapshot, answer):
        answer.value = (False, True)
        stub = _Stub()
        _due(stub)
        assert stub.settings["profile_backup"]["live_snapshot_confirm"] is True
        assert stub.saved == 0


class TestTheBackup:
    def test_stages_a_copy_of_the_released_profile_whatever_its_age(self, snapshot, answer):
        stub = _Stub(live_snapshot_confirm=False)
        _due(stub)
        (global_dir, session_name, kw), = snapshot.copies
        assert (global_dir, session_name) == ("G", "sess")
        assert kw["max_age"] == 0 and kw["stage_only"] is True
        assert kw["budget"] == MainWindow._LIVE_SNAPSHOT_BUDGET_SECONDS
        assert callable(kw["cancel"])

    def test_a_confirmed_copy_is_promoted_and_announced(self, snapshot, answer):
        stub = _Stub(live_snapshot_confirm=False)
        _due(stub)
        assert snapshot.promoted == ["sess"] and snapshot.discarded == []
        assert stub.spoken == ["profile_backup_live_started", "profile_backup_live_done"]
        assert any("WinZapp open" in line for line in stub.audits)

    def test_a_session_that_does_not_come_back_discards_the_copy(self, snapshot, answer):
        """The copy is exactly what WhatsApp just refused; it must not push the
        known-good generation out of .prev."""
        stub = _Stub(live_snapshot_confirm=False)
        stub.accepted = False
        _due(stub)
        assert snapshot.promoted == [] and snapshot.discarded == ["sess"]
        assert stub.spoken == ["profile_backup_live_started", "profile_backup_live_failed"]
        assert stub.audits == []

    def test_a_copy_that_was_not_written_is_announced(self, snapshot, answer):
        snapshot.staged = False
        stub = _Stub(live_snapshot_confirm=False)
        _due(stub)
        assert snapshot.promoted == [] and snapshot.discarded == []
        assert stub.spoken == ["profile_backup_live_started", "profile_backup_live_failed"]

    def test_a_restart_that_did_not_run_keeps_nothing_and_says_nothing(self, snapshot, answer):
        """Held back by its own cooldown: nothing was disconnected, so nothing
        is announced — not "started" followed by "failed"."""
        stub = _Stub(live_snapshot_confirm=False)
        stub.restart_runs = False
        _due(stub)
        assert snapshot.copies == [] and snapshot.promoted == []
        assert stub.spoken == []

    def test_a_promotion_that_fails_discards_the_copy(self, snapshot, answer):
        snapshot.promote_ok = False
        stub = _Stub(live_snapshot_confirm=False)
        _due(stub)
        assert snapshot.discarded == ["sess"]
        assert stub.spoken[-1] == "profile_backup_live_failed"

    def test_the_copy_stops_when_winzapp_starts_closing(self, snapshot, answer):
        stub = _Stub(live_snapshot_confirm=False)
        _due(stub)
        cancel = snapshot.copies[0][2]["cancel"]
        assert cancel() is False
        stub._shutting_down = True
        assert cancel() is True

    def test_a_crash_never_leaves_it_pending_nor_a_staged_copy(self, snapshot, answer):
        stub = _Stub(live_snapshot_confirm=False)

        def _boom():
            raise RuntimeError("boom")

        stub._live_snapshot_session_accepted = _boom
        _due(stub)
        assert stub._live_snapshot_pending is False
        assert snapshot.discarded == ["sess"]


class TestTheWindowCheck:
    class _WindowStub:
        _window_can_ask = MainWindow._window_can_ask

        def IsActive(self):
            raise AssertionError("wx must not be called from the poll thread")

    def test_the_activation_handler_keeps_the_flag_before_any_early_return(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(MainWindow._on_window_activate)))
        body = tree.body[0].body
        assigns = [i for i, node in enumerate(body)
                   if isinstance(node, ast.Assign)
                   and ast.unparse(node.targets[0]) == "self._main_window_active"
                   and ast.unparse(node.value) == "active"]
        returns = [i for i, node in enumerate(body) if isinstance(node, (ast.Return, ast.If))
                   and any(isinstance(n, ast.Return) for n in ast.walk(node))]
        assert assigns, "_on_window_activate() no longer records _main_window_active"
        assert not returns or assigns[0] < returns[0]

    def test_reads_the_flag_kept_on_the_main_thread_never_wx(self):
        stub = self._WindowStub()
        assert stub._window_can_ask() is False
        stub._main_window_active = True
        assert stub._window_can_ask() is True
        stub._main_window_active = False
        assert stub._window_can_ask() is False


class TestTheSendQueue:
    """A message the user sends around the backup is never lost or failed: the
    queue is held while the session is deliberately closed, and a send already
    on the wire is waited for instead of being cut off."""

    def test_held_before_the_session_closes_and_released_once_it_starts_again(
            self, snapshot, answer):
        stub = _Stub(live_snapshot_confirm=False)
        _due(stub)
        assert stub.events == ["hold", "drain", "restart", "release"]
        assert stub.message_queue.is_held() is False

    def test_a_send_still_on_the_wire_postpones_the_backup(self, snapshot, answer):
        stub = _Stub(live_snapshot_confirm=False)
        stub.message_queue.drains = False
        _due(stub)
        assert stub.events == ["hold", "drain", "release"]
        assert stub.restarts == [] and snapshot.copies == []
        assert stub.message_queue.is_held() is False

    def test_released_even_when_the_backup_crashes(self, snapshot, answer):
        stub = _Stub(live_snapshot_confirm=False)

        def _boom(**kw):
            stub.events.append("restart")
            raise RuntimeError("boom")

        stub._restart_wpp_session = _boom
        _due(stub)
        assert stub.events == ["hold", "drain", "restart", "release"]
        assert stub.message_queue.is_held() is False

    def test_a_backup_that_never_starts_leaves_the_queue_alone(self, snapshot, answer):
        stub = _Stub(live_snapshot_confirm=False)
        stub._wa_connected = False
        _due(stub)
        assert stub.events == []

    def test_a_message_sent_while_answering_the_question_does_not_cost_a_day(
            self, snapshot, monkeypatch):
        """The queue is only consulted before asking. Refusing again after a
        Yes would throw the whole interval away — a day, by default — for a
        message the hold protects anyway."""
        stub = _Stub()

        def _confirm_then_send(*a, **kw):
            stub.message_queue.work = True
            return True, False

        patch_main_global(monkeypatch, "confirm_with_checkbox", _confirm_then_send)
        _due(stub)
        assert stub.restarts == ["profile backup"]
        assert stub.events == ["hold", "drain", "restart", "release"]

    def test_manual_offline_mode_never_blocks_a_backup(self, snapshot, answer):
        """Nothing is on the wire while offline, and the queue never empties,
        so refusing would stop every backup for as long as the switch is on."""
        stub = _Stub(live_snapshot_confirm=False)
        stub.offline_mode = True
        stub.message_queue.work = True
        _due(stub)
        assert stub.restarts == ["profile backup"]

    def test_a_send_on_the_wire_leaves_the_backup_due_again(self, snapshot, answer):
        """The interval is spent before the question is asked, so a worker that
        bows out must give it back rather than wait another whole one."""
        stub = _Stub(live_snapshot_confirm=False)
        stub._maybe_refresh_profile_snapshot_live(now=0)
        stub.message_queue.drains = False
        stub._maybe_refresh_profile_snapshot_live(now=HOUR + 10)
        assert stub.restarts == []

        # A minute later, with the send finished, it runs.
        stub.message_queue.drains = True
        stub._maybe_refresh_profile_snapshot_live(now=HOUR + 70)
        assert stub.restarts == ["profile backup"]

    def test_a_session_that_gets_busy_while_asking_leaves_it_due_again(
            self, snapshot, monkeypatch):
        """The obstacle has to appear AFTER the interval was spent to reach the
        worker: before that, the free check refuses and costs nothing."""
        stub = _Stub()

        def _busy_after_the_question(*a, **kw):
            stub._restarting_wpp_session = True
            return True, False

        patch_main_global(monkeypatch, "confirm_with_checkbox", _busy_after_the_question)
        stub._maybe_refresh_profile_snapshot_live(now=0)
        stub._maybe_refresh_profile_snapshot_live(now=HOUR + 10)
        assert stub.restarts == [] and stub.workers == 1

        stub._restarting_wpp_session = False
        patch_main_global(monkeypatch, "confirm_with_checkbox", lambda *a, **kw: (True, False))
        stub._maybe_refresh_profile_snapshot_live(now=HOUR + 70)
        assert stub.restarts == ["profile backup"]

    def test_a_restart_held_back_by_its_cooldown_leaves_it_due_again(self, snapshot, answer):
        """Nothing was closed, so the round cost nothing — including the interval."""
        stub = _Stub(live_snapshot_confirm=False)
        stub.restart_runs = False
        stub._maybe_refresh_profile_snapshot_live(now=0)
        stub._maybe_refresh_profile_snapshot_live(now=HOUR + 10)
        assert stub.restarts == ["profile backup"] and snapshot.copies == []

        stub.restart_runs = True
        stub._maybe_refresh_profile_snapshot_live(now=HOUR + 70)
        assert len(snapshot.copies) == 1


class TestSessionAccepted:
    class _AcceptStub:
        _live_snapshot_session_accepted = MainWindow._live_snapshot_session_accepted
        _live_snapshot_cancelled = MainWindow._live_snapshot_cancelled
        _LIVE_SNAPSHOT_CONFIRM_SECONDS = MainWindow._LIVE_SNAPSHOT_CONFIRM_SECONDS
        _LIVE_SNAPSHOT_STABLE_SECONDS = MainWindow._LIVE_SNAPSHOT_STABLE_SECONDS

        def __init__(self, settled, later):
            self.settled, self.later = settled, later

        def _wait_for_status(self, predicate, timeout, stop_when_connected=True):
            assert stop_when_connected is False
            return self.settled

        def _raw_session_status(self):
            return self.later

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr(main_module.time, "sleep", lambda s: None)

    def test_connected_and_still_connected(self):
        assert self._AcceptStub("CONNECTED", "CONNECTED")._live_snapshot_session_accepted()

    @pytest.mark.parametrize("settled, later", [
        ("QRCODE", "QRCODE"), ("", ""), ("INITIALIZING", "CONNECTED"),
        ("CONNECTED", "QRCODE"), ("CONNECTED", "CLOSED"),
    ])
    def test_anything_else_is_not_accepted(self, settled, later):
        assert not self._AcceptStub(settled, later)._live_snapshot_session_accepted()

    def test_not_when_winzapp_began_closing_meanwhile(self):
        stub = self._AcceptStub("CONNECTED", "CONNECTED")
        stub._shutting_down = True
        assert not stub._live_snapshot_session_accepted()

    def test_not_when_a_restore_began_meanwhile(self):
        stub = self._AcceptStub("CONNECTED", "CONNECTED")
        stub._profile_restore_in_flight = True
        assert not stub._live_snapshot_session_accepted()


class TestTheSnapshotAtClose:
    class _CloseStub:
        _capture_profile_snapshot = MainWindow._capture_profile_snapshot

        def __init__(self, settings):
            self.settings = settings
            self.global_dir = "G"
            self.audits = []

        def _shutdown_audit(self, line):
            self.audits.append(line)

    @pytest.mark.parametrize("settings, max_age", [
        ({}, SNAPSHOT_MAX_AGE_SECONDS),
        ({"profile_backup": {"close_snapshot_min_hours": 0}}, 0),
        ({"profile_backup": {"close_snapshot_min_hours": 6}}, 6 * HOUR),
    ])
    def test_the_refresh_window_is_the_chosen_one(self, snapshot, settings, max_age):
        stub = self._CloseStub(settings)
        stub._capture_profile_snapshot("sess", True, None)
        (_, _, kw), = snapshot.copies
        assert kw == {"max_age": max_age, "lock_wait": 2.0}

    def test_a_staged_copy_nobody_will_promote_is_dropped_at_close(self, snapshot):
        snapshot.staged = False     # the snapshot was fresh: nothing written
        stub = self._CloseStub({})
        stub._capture_profile_snapshot("sess", True, None)
        assert snapshot.discarded == ["sess"]


class _ReleaseStub(_RestartStub):
    def __init__(self, released=True):
        super().__init__()
        self.released = released

    def wait_for_profile_release(self, session_name, timeout=20.0):
        return self.released


@pytest.fixture
def posts(monkeypatch):
    calls = []

    def _fake_post(url, **kw):
        calls.append(url.rsplit("/", 1)[-1])

        class _Resp:
            status_code = 200
        return _Resp()

    monkeypatch.setattr("main.requests.post", _fake_post)
    return calls


class TestTheStepBetweenCloseAndStart:
    def test_runs_after_the_release_and_before_the_start(self, posts):
        stub = _ReleaseStub()
        assert stub._restart_wpp_session(on_profile_released=lambda: posts.append("copy"),
                                         reason="x") is True
        assert posts == ["close-session", "copy", "start-session"]

    def test_is_skipped_when_chrome_never_released_the_profile(self, posts):
        stub = _ReleaseStub(released=False)
        stub._restart_wpp_session(on_profile_released=lambda: posts.append("copy"), reason="x")
        assert posts == ["close-session", "start-session"]

    def test_is_skipped_until_closed_is_confirmed(self, posts):
        stub = _ReleaseStub()
        stub.closed_status = "CONNECTED"
        assert stub._restart_wpp_session(on_profile_released=lambda: posts.append("copy"),
                                         reason="x") is False
        assert posts == ["close-session"]

    def test_a_failing_step_still_starts_the_session(self, posts):
        stub = _ReleaseStub()

        def _boom():
            raise OSError("disk full")

        stub._restart_wpp_session(on_profile_released=_boom, reason="x")
        assert posts == ["close-session", "start-session"]

    def test_a_restore_that_began_during_the_step_owns_the_session(self, posts):
        stub = _ReleaseStub()
        stub._restart_wpp_session(
            on_profile_released=lambda: setattr(stub, "_profile_restore_in_flight", True),
            reason="x")
        assert posts == ["close-session"]

    @pytest.mark.parametrize("flag", ["_shutting_down", "_wpp_updating"])
    def test_no_browser_is_started_under_a_teardown_that_began_meanwhile(self, posts, flag):
        """Closing WinZapp mid-copy: a Chrome opened now is killed mid-write."""
        stub = _ReleaseStub()
        assert stub._restart_wpp_session(
            on_profile_released=lambda: setattr(stub, flag, True), reason="x") is False
        assert posts == ["close-session"]

    def test_the_health_loop_stays_out_while_the_step_runs(self, posts):
        stub = _ReleaseStub()
        seen = []
        stub._restart_wpp_session(
            on_profile_released=lambda: seen.append(stub._self_inflicted_teardown_expected()),
            reason="x")
        assert seen == [True]
        assert stub._self_inflicted_teardown_expected() is False

    def test_a_restart_blocked_by_its_cooldown_reports_it_did_not_run(self, posts):
        stub = _ReleaseStub()
        stub._last_wpp_session_restart_ts = main_module.time.time()
        assert stub._restart_wpp_session(on_profile_released=lambda: posts.append("copy"),
                                         reason="x") is False
        assert posts == []
