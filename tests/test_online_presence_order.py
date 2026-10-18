"""A delayed online write must not overtake offline after leaving the window.

Workers and wx controls are plain stubs: no windows, network or clock sleeps.
"""
from types import SimpleNamespace

import pytest

import core.online_presence as sending
from core.online_presence import OnlinePresenceSender
from main_window.window_lifecycle import WindowLifecycleMixin


@pytest.fixture
def workers(monkeypatch):
    pending = []

    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            pending.append(self.target)

    monkeypatch.setattr(sending.threading, "Thread", Thread)
    return pending


def test_late_worker_reads_latest_state_instead_of_captured_online(workers):
    sent = []
    sender = OnlinePresenceSender(sent.append)
    sender.submit("available")
    sender.submit("unavailable")
    assert len(workers) == 1
    workers.pop()()
    assert sent == ["unavailable"]


def test_focus_change_during_request_is_sent_after_it_without_overlap(workers):
    sent = []

    def post(state):
        sent.append(state)
        if len(sent) == 1:
            # Simulate focus changes while HTTP is still in flight.
            sender.submit("unavailable")
            sender.submit("available")
            sender.submit("unavailable")
            assert workers == []  # No competing HTTP worker.

    sender = OnlinePresenceSender(post)
    sender.submit("available")
    workers.pop()()
    assert sent == ["available", "unavailable"]


def test_failed_online_request_does_not_strand_pending_offline(workers):
    sent = []

    def post(state):
        sent.append(state)
        if state == "available":
            sender.submit("unavailable")
            raise RuntimeError("synthetic request failure")

    sender = OnlinePresenceSender(post)
    sender.submit("available")
    workers.pop()()
    assert sent == ["available", "unavailable"]


def test_idle_worker_can_restart_for_offline_retry(workers):
    sent = []
    sender = OnlinePresenceSender(sent.append)
    for _ in range(2):
        sender.submit("unavailable")
        workers.pop()()
    assert sent == ["unavailable", "unavailable"]


def test_new_online_state_replaces_offline_while_request_is_running(workers):
    sent = []

    def post(state):
        sent.append(state)
        if len(sent) == 1:
            sender.submit("available")

    sender = OnlinePresenceSender(post)
    sender.submit("unavailable")
    workers.pop()()
    assert sent == ["unavailable", "available"]


def test_disconnect_discards_queued_online_and_reconnect_applies_offline(workers):
    sent = []
    sender = OnlinePresenceSender(sent.append)
    sender.submit("available")
    sender.submit(None)
    workers.pop()()
    assert sent == []
    sender.submit("unavailable")
    workers.pop()()
    assert sent == ["unavailable"]


def test_disconnect_during_http_cancels_pending_online(workers):
    sent = []

    def post(state):
        sent.append(state)
        sender.submit("available")
        sender.submit(None)

    sender = OnlinePresenceSender(post)
    sender.submit("unavailable")
    workers.pop()()
    assert sent == ["unavailable"]


def test_worker_start_failure_can_retry(monkeypatch, workers):
    sent = []
    sender = OnlinePresenceSender(sent.append)
    original = sending.threading.Thread

    def fail(**kwargs):
        raise RuntimeError("synthetic thread failure")

    monkeypatch.setattr(sending.threading, "Thread", fail)
    sender.submit("available")
    monkeypatch.setattr(sending.threading, "Thread", original)
    sender.submit("unavailable")
    workers.pop()()
    assert sent == ["unavailable"]


class Timer:
    def __init__(self):
        self.running = False

    def IsRunning(self):
        return self.running

    def Start(self, interval):
        assert interval == 20_000
        self.running = True

    def Stop(self):
        self.running = False


class Window:
    _sync_online_presence = WindowLifecycleMixin._sync_online_presence
    _apply_window_activate = WindowLifecycleMixin._apply_window_activate
    _on_presence_timer = WindowLifecycleMixin._on_presence_timer

    def __init__(self):
        self.token = "synthetic"
        self._wa_connected = True
        self.background_mode = False
        self._window_hidden = False
        self._shutting_down = False
        self.active = self.shown = True
        self.iconized = False
        self._presence_timer = Timer()
        self.sent = []
        self._send_presence = self.sent.append

    def IsActive(self):
        return self.active

    def IsShown(self):
        return self.shown

    def IsIconized(self):
        return self.iconized

    def _refresh_accounts_menu_if_stale(self):
        pass


@pytest.mark.parametrize("flag", ["active", "shown", "iconized",
                                  "background_mode", "_window_hidden"])
def test_queued_heartbeat_rechecks_visibility_instead_of_forcing_online(flag, workers):
    window = Window()
    setattr(window, flag, flag not in ("active", "shown"))
    window._on_presence_timer(None)
    workers.pop()()
    assert window.sent == ["unavailable"]
    assert window._presence_timer.IsRunning()  # Offline also gets retries.


def test_debounced_focus_gain_cannot_mark_an_already_hidden_window_online(workers):
    window = Window()
    window._window_hidden = True
    window._apply_window_activate(True)
    workers.pop()()
    assert window.sent == ["unavailable"]


def test_current_focus_wins_over_old_deactivation_callback(workers):
    window = Window()
    window._apply_window_activate(False)
    workers.pop()()
    assert window.sent == ["available"]


@pytest.mark.parametrize("flag,value", [("_wa_connected", False),
                                       ("token", ""), ("_shutting_down", True)])
def test_disconnect_or_shutdown_drops_work_and_stops_heartbeat(flag, value, workers):
    window = Window()
    window._sync_online_presence()
    setattr(window, flag, value)
    window._on_presence_timer(None)
    workers.pop()()
    assert window.sent == []
    assert not window._presence_timer.IsRunning()


def test_connection_callback_before_ui_is_safe():
    WindowLifecycleMixin._sync_online_presence(SimpleNamespace())


@pytest.mark.parametrize("method", ["hide_to_tray", "_on_close"])
def test_hiding_to_tray_queues_offline_without_waiting_for_activation(method, workers, monkeypatch):
    import ctypes
    from main_window import window_lifecycle
    window = Window()
    window.GetHandle = lambda: 123
    window.tray_icon = SimpleNamespace(update_tooltip=lambda: None)
    window.lock_chat_vault = lambda **kwargs: None
    # Even if wx still reports active/shown, the explicit hidden flag wins.
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        user32=SimpleNamespace(ShowWindow=lambda *args: None)), raising=False)
    event = SimpleNamespace(Veto=lambda: None)
    action = getattr(window_lifecycle.WindowLifecycleMixin, method)
    action(window, event) if method == "_on_close" else action(window)
    workers.pop()()
    assert window.sent == ["unavailable"]


def test_background_reconnect_does_not_wait_for_focus_change(workers):
    from tests.test_no_offline_during_wpp_update import _Stub
    from main_window import connection
    window = _Stub()
    window.background_mode = True
    window._sync_online_presence = lambda: Window._sync_online_presence(window)
    window._presence_timer = Timer()
    window._send_presence = lambda state: sent.append(state)
    sent = []
    queued = []
    # Only UI scheduling is simulated; call the actual connection transition.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(connection.wx, "CallAfter", lambda fn, *a, **kw: queued.append(lambda: fn(*a, **kw)))
        window._set_wa_connected(False, "synthetic", announce=False)
        window._set_wa_connected(True, "synthetic", announce=False)
        for callback in queued:
            callback()
    workers.pop()()
    assert sent == ["unavailable"]


@pytest.mark.parametrize("restart_fails", [False, True])
@pytest.mark.parametrize("background", [False, True])
def test_cancelled_windows_shutdown_restarts_presence_without_connection_edge(
        workers, monkeypatch, restart_fails, background):
    from main_window import session_lifecycle
    from tests.test_end_session_unstick import _Stub

    window = Window()
    window.background_mode = background
    teardown = _Stub()
    window._teardown_started_lock = teardown._teardown_started_lock
    window._teardown_complete_event = teardown._teardown_complete_event
    window._WINDOWS_SHUTDOWN_BUDGET = 4
    window._END_SESSION_UNSTICK_SECONDS = 60
    window._shutdown_audit = lambda *args: None
    window._stop_wpp_server = lambda **kwargs: None
    window._flush_pending_debounced_saves = lambda: None

    def restart():
        if restart_fails:
            raise RuntimeError("synthetic restart failure")

    window._restart_wpp_after_cancelled_shutdown = restart
    timers = []
    queued = []

    class RecoveryTimer:
        def __init__(self, seconds, callback):
            timers.append(callback)

        def start(self):
            pass

    monkeypatch.setattr(session_lifecycle.threading, "Timer", RecoveryTimer)
    monkeypatch.setattr(session_lifecycle.wx, "CallAfter",
                        lambda fn: queued.append(fn))
    session_lifecycle.SessionLifecycleMixin._run_windows_session_teardown(
        window, "synthetic")
    assert window._shutting_down
    window._on_presence_timer(None)
    assert not window._presence_timer.IsRunning()
    timers.pop()()  # Windows cancelled shutdown; connection flag stayed True.
    assert not window._shutting_down
    assert len(queued) == 1
    queued.pop()()
    workers.pop()()
    assert window.sent == ["unavailable" if background else "available"]
    assert window._presence_timer.IsRunning()


def test_restoring_background_window_reapplies_online_after_flags_clear(workers, monkeypatch):
    import ctypes
    from main_window import window_lifecycle
    window = Window()
    window.background_mode = window._window_hidden = True
    window.GetHandle = lambda: 123
    window._sync_tray_icon_with_setting = lambda: None
    window._focus_primary_control = lambda: None
    user32 = SimpleNamespace(
        ShowWindow=lambda *a: None, SetForegroundWindow=lambda *a: None,
        GetForegroundWindow=lambda: 0, BringWindowToTop=lambda *a: None,
        SetActiveWindow=lambda *a: None)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        user32=user32, kernel32=SimpleNamespace(GetCurrentThreadId=lambda: 1)),
        raising=False)
    queued = []
    monkeypatch.setattr(window_lifecycle.wx, "CallAfter", lambda fn: queued.append(fn))
    WindowLifecycleMixin.restore_window(window)
    assert not window.background_mode and not window._window_hidden
    for callback in queued:
        callback()
    workers.pop()()
    assert window.sent == ["available"]
