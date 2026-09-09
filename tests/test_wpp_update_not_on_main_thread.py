"""Tests that accepting a WPPConnect update does not freeze the window.

`_update_wpp_server()` used to call `_stop_wpp_server()` inline, on the wx main
thread — against that method's own docstring, which says it must not be. The
two docstrings had contradicted each other for a while, but the block used to be
bounded at roughly close-session (10 s) + the CLOSED flush poll (15 s). Then the
stop grew a `wait_for_profile_release()` call: another 15 s, followed by a
post-kill grace loop of ten "0.5 s" polls that each spawn a PowerShell
`Get-CimInstance Win32_Process` — measured at 0.57 s on a real machine, so
~1.1 s of wall clock apiece. Total: ~45-50 s in which the message loop never
pumps, so Windows paints the window "Not Responding".

`real_exit()`'s docstring already records that exact symptom as reported live
("'Sair' leaving a frozen window on screen for tens of seconds"), and it is
worse here than there: quitting hides the window first, whereas this happens
right after the user pressed Yes on a prompt and is still looking at the app.
For an NVDA/JAWS user a window whose loop has stopped is not a slow window, it
is a UI that vanished without a word.

So the stop moved to a worker thread and the modal dialogs — which genuinely do
need the main thread — resume from `_after_stop()` via `wx.CallAfter`.

MainWindow is a wx.Frame and cannot be instantiated without a running app, so
the method is exercised as a plain function against a stub carrying only the
attributes it touches.
"""

import pytest
import wx

from main import MainWindow


TAG = "v2.10.10"


class _I18n:
    def t(self, key):
        return key


class _Sound:
    def play(self):
        pass


class _Dialog:
    """Stands in for ApiSetupDialog — records that it was constructed at all,
    which is the thing under test: it must not exist before the stop is done."""

    def __init__(self, parent, title_override=None, forced_tag=None):
        parent.dialogs.append(forced_tag)
        self._result = parent.dialog_result

    def ShowModal(self):
        return self._result

    def Destroy(self):
        pass


class _Threads:
    """Captures threads instead of starting them, so a test can decide when —
    and whether — the worker runs, and can look at the world in between."""

    def __init__(self):
        self.started = []

    def Thread(self, target=None, daemon=None, name=None, **kwargs):
        recorder = self

        class _Handle:
            def start(_self):
                recorder.started.append((name, target))

        return _Handle()

    def run_next(self):
        _name, target = self.started.pop(0)
        target()


class _Stub:
    _update_wpp_server = MainWindow._update_wpp_server

    def __init__(self, dialog_result=wx.ID_OK, stop_raises=False):
        # background_mode keeps output()/restore_window out of the picture;
        # these tests are about what runs where, not about the announcements.
        self.background_mode = True
        self.i18n = _I18n()
        self.error_sound = _Sound()
        self.dialog_result = dialog_result
        self._stop_raises = stop_raises
        self._wpp_updating = False
        self.wpp_process = object()
        self.dialogs = []
        self.events = []
        self.flag_during_stop = None

    def _stop_wpp_server(self):
        self.flag_during_stop = self._wpp_updating
        self.events.append("stop")
        if self._stop_raises:
            raise RuntimeError("close-session blew up")

    def _kill_orphaned_chrome_for_session(self):
        self.events.append("kill")

    def wait_for_profile_release(self, session_name, timeout=None):
        # The update path clears the profile through this now: wait for the
        # release, kill only what never lets go. Recorded under the same name
        # so the sequence assertions keep reading as "the profile was freed
        # between the stop and the restart".
        self.events.append("kill")
        return True

    def ensure_wpp_running(self):
        self.events.append("restart")

    def _reconnect_websocket_now(self):
        pass

    def check_wa_connection_http(self):
        pass

    def trigger_sync_if_needed(self):
        pass


@pytest.fixture
def threads(monkeypatch):
    """Capture threads, run wx.CallAfter inline (the continuation really does
    run on the main thread in production), and swap the real setup dialog out."""
    import main as main_module
    from ui.dialogs import api_setup

    captured = _Threads()
    # Only main's own binding is replaced, never the threading module itself.
    monkeypatch.setattr(main_module, "threading", captured)
    monkeypatch.setattr(main_module.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(main_module.wx, "MessageBox", lambda *a, **k: wx.ID_OK)
    monkeypatch.setattr(api_setup, "ApiSetupDialog", _Dialog)
    return captured


class TestTheStopIsHandedToAWorker:
    def test_the_call_returns_before_anything_blocking_happens(self, threads):
        stub = _Stub()
        stub._update_wpp_server(TAG)

        assert stub.events == []
        assert [name for name, _ in threads.started] == ["winzapp-wpp-update-stop"]

    def test_the_dialog_is_not_opened_until_the_stop_finished(self, threads):
        """The dialogs are the reason the main thread is needed at all — they
        must come after the stop, not instead of waiting for it."""
        stub = _Stub()
        stub._update_wpp_server(TAG)
        assert stub.dialogs == []

        threads.run_next()
        assert stub.dialogs == [TAG]

    def test_the_sequence_is_unchanged(self, threads):
        """Off-thread or not, it is still stop, then release the profile, then
        reinstall and restart."""
        stub = _Stub()
        stub._update_wpp_server(TAG)
        threads.run_next()

        assert stub.events == ["stop", "kill", "restart"]

    def test_the_update_flag_is_already_set_while_the_stop_runs(self, threads):
        """The health checker polls every 30 s on its own thread and would
        otherwise catch the server mid-stop and announce an outage — that is
        the whole point of _wpp_updating, and the stop is now the longest part
        of the window it has to cover."""
        stub = _Stub()
        stub._update_wpp_server(TAG)
        threads.run_next()

        assert stub.flag_during_stop is True

    def test_the_post_update_reconnect_still_gets_its_own_thread(self, threads):
        stub = _Stub()
        stub._update_wpp_server(TAG)
        threads.run_next()

        assert len(threads.started) == 1


class TestTheFlagAlwaysClears:
    """Another fix in this branch depends on _wpp_updating clearing: while it
    is True every genuine disconnection is suppressed, so a run that leaves it
    set silences the app for the rest of the session."""

    def test_after_a_successful_update(self, threads):
        stub = _Stub()
        stub._update_wpp_server(TAG)
        threads.run_next()

        assert stub._wpp_updating is False

    def test_after_a_cancelled_or_failed_update(self, threads):
        stub = _Stub(dialog_result=wx.ID_CANCEL)
        stub._update_wpp_server(TAG)
        threads.run_next()

        assert stub._wpp_updating is False
        # The server still has to come back up on this branch.
        assert stub.events == ["stop", "kill", "restart"]

    def test_when_the_stop_itself_raises(self, threads):
        """The worker swallows the failure and hands over anyway: the user
        asked for a reinstall, and abandoning here would leave the flag set
        AND the server down."""
        stub = _Stub(stop_raises=True)
        stub._update_wpp_server(TAG)
        threads.run_next()

        assert stub._wpp_updating is False
        assert stub.events == ["stop", "restart"]

    def test_when_the_hand_back_to_the_main_thread_fails(self, threads, monkeypatch):
        """CallAfter can fail if the app is already tearing down. Nothing runs
        the continuation then, so the worker has to clear the flag itself."""
        import main as main_module

        def _boom(*_a, **_k):
            raise RuntimeError("no app")

        monkeypatch.setattr(main_module.wx, "CallAfter", _boom)
        stub = _Stub()
        stub._update_wpp_server(TAG)
        threads.run_next()

        assert stub._wpp_updating is False
        assert stub.dialogs == []


class TestASecondUpdateIsRefusedWhileOneRuns:
    """New hazard, created by this fix: the message loop keeps pumping during
    the stop, so 'Forçar reinstalação da WPPConnect' and a second update prompt
    stay reachable. Two of these flows would fight over the same Node process
    and the same install directory."""

    def test_a_second_request_mid_stop_is_ignored(self, threads):
        stub = _Stub()
        stub._update_wpp_server(TAG)
        stub._update_wpp_server("v2.10.11")

        assert len(threads.started) == 1

    def test_a_later_request_is_accepted_again(self, threads):
        """The guard is the flag, and the flag clears — refusing forever would
        be a worse bug than the double run."""
        stub = _Stub()
        stub._update_wpp_server(TAG)
        threads.run_next()
        threads.started.clear()

        stub._update_wpp_server("v2.10.11")
        threads.run_next()

        assert stub.dialogs == [TAG, "v2.10.11"]


class _Clock:
    """A monotonic() stand-in the stub advances explicitly, so a poll can be
    made to cost what it really costs."""

    def __init__(self, poll_cost):
        self.now = 0.0
        self.poll_cost = poll_cost


class _ProfileStub:
    wait_for_profile_release = MainWindow.wait_for_profile_release

    def __init__(self, clock):
        self.clock = clock
        self.polls = 0
        self.kills = []

    def _chrome_pids_owning_session(self, session_name):
        self.polls += 1
        self.clock.now += self.clock.poll_cost
        return ["4242"]          # a holder that never lets go

    def _kill_orphaned_chrome_for_session(self, session_name=None):
        self.kills.append(session_name)


@pytest.fixture
def clock(monkeypatch):
    def _make(poll_cost):
        c = _Clock(poll_cost)
        monkeypatch.setattr("main.time.monotonic", lambda: c.now)
        monkeypatch.setattr("main.time.sleep", lambda s: setattr(c, "now", c.now + s))
        return c

    return _make


class TestThePostKillGraceIsBounded:
    """The other half of the same defect: whatever `wait_for_profile_release`'s
    caller passes as `timeout`, the post-kill grace loop used to run ten more
    polls with no deadline of its own, and each `_chrome_pids_owning_session`
    call carries a 15 s subprocess timeout. A PowerShell stalled by antivirus
    or load therefore bought ~150 s *after* the timeout had already elapsed —
    on a caller (the WPPConnect update, shutdown, pairing) that budgeted for
    timeout + 5 s."""

    def test_a_stalled_powershell_no_longer_multiplies(self, clock):
        c = clock(poll_cost=15.0)          # every poll hits its own timeout
        stub = _ProfileStub(c)

        assert stub.wait_for_profile_release("sess", timeout=15.0) is False
        assert stub.kills == ["sess"]
        # One poll in the wait, one in the grace, then the deadline stops it.
        # The old loop took eleven, for ~170 s.
        assert stub.polls == 2
        assert c.now <= 15.0 + 5.0 + 15.0

    def test_the_grace_ends_on_its_deadline_at_real_poll_cost(self, clock):
        c = clock(poll_cost=0.57)          # the measured Get-CimInstance cost
        stub = _ProfileStub(c)

        assert stub.wait_for_profile_release("sess", timeout=1.0) is False
        # 0.57 + 0.5 per iteration means the 5 s grace runs out well before the
        # tenth poll, which is exactly the "0.5 s poll" the count assumed.
        assert stub.polls < 11
        assert c.now <= 1.0 + 5.0 + 1.1

    def test_the_poll_count_still_caps_a_free_clock(self, clock):
        """The deadline is an addition, not a replacement: when the polls cost
        nothing the loop must still give up rather than spin."""
        c = clock(poll_cost=0.0)
        stub = _ProfileStub(c)

        assert stub.wait_for_profile_release("sess", timeout=1.0) is False
        assert stub.polls == 12            # 2 in the wait + the 10 grace polls
