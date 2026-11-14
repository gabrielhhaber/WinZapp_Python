"""Sync > "Reconnect now" (issue #765): core/manual_reconnect.py and
main_window/manual_reconnect.py. Reported: after hours on a poor
connection WinZapp stayed in Offline mode until the computer was restarted.
No network, no window: every step is a stub."""
from types import SimpleNamespace

from core import manual_reconnect as mr
from main_window.manual_reconnect import ManualReconnectMixin


def _run(reaches=True, connected=(False,), restarted=True, back=True):
    steps = []
    answers = iter(connected)
    outcome = mr.reconnect(
        lambda: steps.append("probe") or reaches,
        lambda: steps.append("channel"),
        lambda: steps.append("check") or next(answers),
        lambda: steps.append("restart") or restarted,
        lambda: steps.append("wait") or back)
    return outcome, steps


def test_without_internet_nothing_is_restarted():
    assert _run(reaches=False) == (mr.NO_INTERNET, ["probe"])


def test_already_connected_once_the_channel_is_back():
    assert _run(connected=(True,)) == (mr.CONNECTED, ["probe", "channel", "check"])


def test_a_session_that_gave_up_is_restarted_and_waited_for():
    assert _run() == (mr.CONNECTED, ["probe", "channel", "check", "restart", "wait"])
    assert _run(back=False)[0] == mr.RESTARTING
    assert _run(restarted=False)[0] == mr.FAILED


def test_a_failing_channel_does_not_stop_the_restart():
    def broken():
        raise OSError("socket")
    assert mr.reconnect(lambda: True, broken, lambda: False, lambda: True, lambda: True) == mr.CONNECTED


def test_every_outcome_has_its_sentence():
    from tests.test_i18n_keys_exist import _locale
    for code in ("en-US", "pt-BR", "ar"):
        strings = _locale(code)
        for key in list(mr.OUTCOME_KEYS.values()) + ["menu_reconnect_now", "reconnect_started"]:
            assert strings.get(key), (code, key)


class _Window(ManualReconnectMixin):
    def __init__(self):
        self.i18n = SimpleNamespace(t=lambda key: key)
        self.said, self.restarts, self._user_offline, self.applied = [], [], True, 0
        self._last_wpp_session_restart_ts = 999.0
        self._wa_connected = False

    def output(self, text, interrupt=False):
        self.said.append(text)

    def _apply_offline_state(self):
        self.applied += 1

    def _restart_wpp_session(self, reason=None):
        self.restarts.append((self._last_wpp_session_restart_ts, reason))
        return True


def test_asking_to_reconnect_leaves_the_manual_offline_mode_and_runs_once(monkeypatch):
    import main_window.manual_reconnect as module
    started = []
    monkeypatch.setattr(module.threading, "Thread",
                        lambda target, daemon, name: SimpleNamespace(start=lambda: started.append(1)))
    w = _Window()
    w.reconnect_now()
    assert w._user_offline is False and w.applied == 1 and started == [1]
    w.reconnect_now()                       # still running: said, not started again
    assert started == [1] and w.said == ["reconnect_started", "reconnect_started"]


def test_the_users_restart_skips_the_automatic_cooldown():
    w = _Window()
    assert w._manual_reconnect_restart() is True
    assert w.restarts == [(0, "the user asked to reconnect")]


def test_a_session_the_disk_guard_closed_is_not_restarted_by_hand(monkeypatch):
    import main_window.manual_reconnect as module
    from core import profile_disk_guard
    started = []
    monkeypatch.setattr(module.threading, "Thread",
                        lambda target, daemon, name: SimpleNamespace(start=lambda: started.append(1)))
    monkeypatch.setattr(profile_disk_guard, "free_bytes_of", lambda mw: 2 * profile_disk_guard.GIB)
    w = _Window()
    w.i18n = SimpleNamespace(t=lambda key: "{free}/{needed}" if key == "low_disk_session_closed" else key)
    w._low_disk_halted = True
    w.reconnect_now()
    assert started == [] and w.said == ["2.0/5.0"]
    assert w._user_offline is True and w.applied == 0
    assert w._manual_reconnect_restart() is False and w.restarts == []


def test_the_wait_ends_when_the_session_is_back():
    w = _Window()
    checks = iter([False, True])
    w._manual_reconnect_connected = lambda: next(checks)
    assert w._manual_reconnect_wait(timeout=30, sleep=lambda s: None) is True
