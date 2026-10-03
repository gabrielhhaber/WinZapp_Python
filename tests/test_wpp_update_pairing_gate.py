"""Tests for holding the WPPConnect Server update back while pairing.

Accepting that update is not like accepting WinZapp's own: it calls
_stop_wpp_server(), reinstalls the API in place and restarts it. Doing that
while someone is pairing pulls the server out from under the flow — observed
live as every request failing with connection-refused, start_sync giving up,
and the UI dropping to "desconectado do WhatsApp" with the pairing attempt
abandoned half-done.

The only thing guarding that was `wx.CallLater(90000, ...)` in MainWindow's
__init__ — 90 seconds of wall clock, which is not the same claim as "pairing
finished". A first run that spent 42 s downloading the headless Chrome shell
put the checker's T+90s squarely on top of an open pairing dialog.

MainWindow is a wx.Frame and WppUpdateChecker owns real threading.Timers, so
both are exercised against small stubs carrying only what the methods touch.
"""

import pytest

from main import MainWindow
from updater import WppUpdateChecker


class _ConnectStub:
    def __init__(self, dialog_shown):
        self.connection_dial = _DialStub(dialog_shown) if dialog_shown is not None else None


class _DialStub:
    def __init__(self, shown):
        self._shown = shown

    def IsShown(self):
        return self._shown

    def __bool__(self):
        return True


class _MainWindowStub:
    """Stand-in for MainWindow for the pairing gate."""

    wpp_update_may_run_now = MainWindow.wpp_update_may_run_now
    _is_pairing_dialog_active = MainWindow._is_pairing_dialog_active

    def __init__(self, dialog_shown=False, pairing_in_progress=False):
        self.connect = _ConnectStub(dialog_shown)
        self._pairing_in_progress = pairing_in_progress


class TestWppUpdateMayRunNow:
    def test_allowed_when_nothing_is_pairing(self):
        assert _MainWindowStub().wpp_update_may_run_now() is True

    def test_blocked_while_the_pairing_dialog_is_on_screen(self):
        mw = _MainWindowStub(dialog_shown=True)
        assert mw.wpp_update_may_run_now() is False

    def test_blocked_while_the_pairing_flow_is_running(self):
        """_pairing_in_progress covers the window the dialog alone misses —
        connect.py sets it for the whole flow, including before the dialog is
        up and after it closes. main.py calls it the authoritative "do not
        touch the session" signal for exactly this reason."""
        mw = _MainWindowStub(dialog_shown=False, pairing_in_progress=True)
        assert mw.wpp_update_may_run_now() is False

    def test_blocked_when_the_state_cannot_be_determined(self):
        """Reached from a background thread during teardown, when
        self.connect may already be gone. "Can't tell" must mean "don't touch
        the session", never "go ahead"."""
        mw = _MainWindowStub()
        del mw.connect
        assert mw.wpp_update_may_run_now() is False

    def test_a_missing_pairing_flag_is_treated_as_not_pairing(self):
        """The attribute is set well into __init__; the checker can be
        scheduled before that point."""
        mw = _MainWindowStub()
        del mw._pairing_in_progress
        assert mw.wpp_update_may_run_now() is True


class _I18nStub:
    def t(self, key):
        return key


class _PromptMainWindowStub:
    def __init__(self, may_run):
        self._may_run = may_run
        self.i18n = _I18nStub()
        self.updated_to = None

    def wpp_update_may_run_now(self):
        return self._may_run

    def _update_wpp_server(self, tag, on_finished=None):
        self.updated_to = tag
        return True


@pytest.fixture
def checker_factory(monkeypatch):
    def _make(may_run):
        checker = WppUpdateChecker.__new__(WppUpdateChecker)
        checker._mw = _PromptMainWindowStub(may_run)
        checker._retry_timer = None
        checker._declined_tag = None
        checker._prompt_token = None
        checker.scheduled = []
        checker._schedule_retry = lambda interval=None: checker.scheduled.append(interval)
        return checker

    return _make


class TestPromptIsDeferredWhilePairing:
    def test_no_dialog_is_shown_while_pairing(self, checker_factory, monkeypatch):
        import updater as updater_module

        shown = []
        monkeypatch.setattr(
            updater_module.wx, "MessageBox", lambda *a, **k: shown.append(a) or 0
        )

        checker = checker_factory(may_run=False)
        checker._prompt_update("2.10.5", "2.10.6", "v2.10.6")

        assert shown == []
        assert checker._mw.updated_to is None

    def test_it_retries_soon_rather_than_in_twelve_hours(self, checker_factory, monkeypatch):
        """A deferral is not a failure: an update IS waiting and nothing went
        wrong, the user just must not be interrupted mid-pairing. The periodic
        interval would effectively drop it for the whole session."""
        import updater as updater_module

        monkeypatch.setattr(updater_module.wx, "MessageBox", lambda *a, **k: 0)

        checker = checker_factory(may_run=False)
        checker._prompt_update("2.10.5", "2.10.6", "v2.10.6")

        assert checker.scheduled == [WppUpdateChecker._PAIRING_RETRY_INTERVAL]
        assert (
            WppUpdateChecker._PAIRING_RETRY_INTERVAL
            < WppUpdateChecker._RETRY_INTERVAL
        )

    def test_the_prompt_still_appears_when_not_pairing(self, checker_factory, monkeypatch):
        import updater as updater_module

        shown = []

        def _fake_message_box(*args, **kwargs):
            shown.append(args)
            return updater_module.wx.YES

        monkeypatch.setattr(updater_module.wx, "MessageBox", _fake_message_box)

        checker = checker_factory(may_run=True)
        checker._prompt_update("2.10.5", "2.10.6", "v2.10.6")

        assert len(shown) == 1
        assert checker._mw.updated_to == "v2.10.6"


class TestScheduleRetryInterval:
    def test_default_is_the_periodic_interval(self):
        checker = WppUpdateChecker.__new__(WppUpdateChecker)
        checker._retry_timer = None
        made = {}

        class _Timer:
            def __init__(self, interval, fn):
                made["interval"] = interval
                self.daemon = False

            def start(self):
                made["started"] = True

            def cancel(self):
                made["cancelled"] = True

        import updater as updater_module

        original = updater_module.threading.Timer
        updater_module.threading.Timer = _Timer
        try:
            checker._schedule_retry()
            assert made["interval"] == WppUpdateChecker._RETRY_INTERVAL
            checker._schedule_retry(42)
            assert made["interval"] == 42
            # The second call must replace the first, not run alongside it —
            # two live timers would double the check rate from then on.
            assert made.get("cancelled") is True
        finally:
            updater_module.threading.Timer = original
