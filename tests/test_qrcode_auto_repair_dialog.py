"""Tests for on_qrcode_update()'s proactive pairing-dialog trigger.

Reported live: after an automatic session recovery attempt found the stored
token already invalid, WPPConnect correctly started generating a fresh QR
code (on_qrcode_update fired repeatedly with real image bytes) — but nothing
in the app surfaced it. The user was left staring at "offline" with no
explanation for however long _AUTO_RESTART_LOGOUT_GRACE_SECONDS or the
multi-minute confirmed-logout detection (several minutes either way) took
before finally showing a dialog.

A real QR/pairing-code event with no pairing dialog already open is a
fairly reliable "you need to re-pair" signal — WPPConnect only ever
generates one once it has decided the stored session can't be restored —
so on_qrcode_update() opens the pairing dialog once that is confirmed by
a second such reading (TestStartupGraceWindow and
TestProactivePairingDialog below cover why one alone is not enough),
decoupled entirely from the slower, destructive confirmed-logout path
(_on_disconnect(), which wipes local data, is never called from here).

WebSocketClient is exercised as a plain function bound onto a small stub
(no real socketio/wx.App needed) — same approach as tests/test_qrcode_event.py
uses for _extract_qr_payload.
"""

import time

import pytest

from core.websocket_client import WebSocketClient, qr_within_startup_grace
from main import MainWindow


class _FakeI18n:
    def t(self, key):
        return key


class _FakeSound:
    def __init__(self):
        self.plays = 0

    def play(self):
        self.plays += 1


class _FakeSpeakOutput:
    def output(self, text):
        pass


class _FakeConnect:
    def __init__(self, main_window=None):
        self.connection_mode = "phone"
        self.main_window = main_window
        self.show_connection_dial_calls = 0

    def show_connection_dial(self):
        self.show_connection_dial_calls += 1
        # Mirrors the real Connect.show_connection_dial(), which drops both
        # unattended-QR guards right before its modal loop. A fake that skips
        # this makes the flood limit look one event closer than production
        # ever reaches it — see tests/test_qrcode_unattended_session.py.
        self.main_window._reset_unattended_qr_guards()

    def display_qrcode_image(self, base64_img):
        pass


class _FakeMainWindow:
    def __init__(self, paired=True, pairing_dialog_active=False,
                 wa_connect_announced=True, wa_startup_time=None):
        self.settings = {"privateinfo": {"paired": paired}}
        self._pairing_dialog_active = pairing_dialog_active
        self.pairing_code_updated_sound = _FakeSound()
        self.error_sound = _FakeSound()
        self.speak_output = _FakeSpeakOutput()
        self.app_name = "WinZapp"
        self.restore_window_calls = 0
        self._unattended_qr_events = 0
        self._qr_flood_halted = False
        self._pairing_in_progress = False
        self.halt_calls = 0
        # Whether a snapshot exists to put back. False keeps every test
        # written before the profile-repair step behaving exactly as it did:
        # nothing to restore, so the pairing dialog is the outcome.
        self.profile_restore_available = False
        self.recover_calls = []
        # Every on_give_up handed over, kept so a test can fire one the way
        # the restore thread does — see fail_restore().
        self.give_up_callbacks = []
        # Defaults put every pre-existing test well past the startup grace
        # window (already connected once before, or started long ago) —
        # only the dedicated grace-window tests below override these.
        self._wa_connect_announced = wa_connect_announced
        self._WA_STARTUP_GRACE_SECONDS = MainWindow._WA_STARTUP_GRACE_SECONDS
        self._wa_startup_time = (
            time.time() - (self._WA_STARTUP_GRACE_SECONDS * 10)
            if wa_startup_time is None else wa_startup_time
        )
        # Modelling the restore's own two effects separately, because they
        # land at very different moments — see finish_restore() below.
        self._recovery_spent = False
        self.restore_starts = 0

    def _recover_suspect_profile(self, reason=None, on_give_up=None):
        # Every call is recorded, including the ones the latch refuses: the
        # caller (_handle_unattended_qr) does not guard against a later QR
        # refresh calling this again, so what it does with the NEXT code is
        # exactly what these tests are about.
        self.recover_calls.append(reason)
        self.give_up_callbacks.append(on_give_up)
        # Mirrors the real contract: on_give_up fires only when a restore was
        # started and then failed. A False return means nothing was started,
        # and the caller handles it inline — see _recover_suspect_profile().
        if not self.profile_restore_available or self._recovery_spent:
            return False
        # The latch is what the real one does synchronously: it is set before
        # the restore thread is even started, so a second call is refused
        # whether or not that thread has finished.
        self._recovery_spent = True
        self.restore_starts += 1
        return True

    def finish_restore(self):
        """The restore thread reaching `self._unattended_qr_events = 0`.

        Kept separate from _recover_suspect_profile() on purpose. In
        production that line runs on a background thread, after close-session
        (10 s timeout), wait_for_profile_release (20 s) and a copy of a few
        hundred MB — while codes keep arriving every ~20-30 s. Zeroing the
        counter inline here would model a race production does not reliably
        win, and every test resting on it would be asserting a guarantee the
        app does not have. So the tests say when it lands, and both orderings
        are covered. The production line this stands in for has its own test,
        against the real MainWindow method that runs it:
        tests/test_profile_recovery_wiring.py::
        TestASuccessfulRestoreGivesBackTheQrFloodAllowance.
        """
        self._unattended_qr_events = 0

    def fail_restore(self):
        """The restore thread's give-up path, in the order production runs it.

        _recover_suspect_profile() queues wx.CallAfter(
        self._announce_profile_beyond_repair) — whose *first* statement is
        error_sound.play() — and immediately behind it wx.CallAfter(
        on_give_up), which passes no arguments at all. Both land on the wx
        main thread milliseconds apart, so what the callback does with the
        sound is the whole question here.
        """
        self.error_sound.play()           # _announce_profile_beyond_repair()
        self.give_up_callbacks[-1]()      # wx.CallAfter(on_give_up): no args

    def _is_pairing_dialog_active(self):
        return self._pairing_dialog_active

    def restore_window(self):
        self.restore_window_calls += 1

    # The real method, so this fake cannot drift from what production does
    # when the pairing dialog goes up.
    _reset_unattended_qr_guards = MainWindow._reset_unattended_qr_guards

    def _halt_unattended_qr_session(self):
        self.halt_calls += 1
        self._qr_flood_halted = True


class _Stub:
    on_qrcode_update = WebSocketClient.on_qrcode_update
    _pairing_attended = WebSocketClient._pairing_attended
    _handle_unattended_qr = WebSocketClient._handle_unattended_qr
    _qr_within_startup_grace = WebSocketClient._qr_within_startup_grace
    _show_repair_dialog = WebSocketClient._show_repair_dialog
    _UNATTENDED_QR_LIMIT = WebSocketClient._UNATTENDED_QR_LIMIT
    _REPAIR_DIALOG_CONFIRM_EVENTS = WebSocketClient._REPAIR_DIALOG_CONFIRM_EVENTS
    _extract_qr_payload = staticmethod(WebSocketClient._extract_qr_payload)

    def __init__(self, main_window, connect):
        self.main_window = main_window
        self.connect = connect
        self.i18n = _FakeI18n()


QR_EVENT = {"data": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg"}


@pytest.fixture(autouse=True)
def _synchronous_call_after(monkeypatch):
    monkeypatch.setattr("core.websocket_client.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr("core.websocket_client.wx.MessageBox", lambda *a, **kw: None)


class TestProactivePairingDialog:
    def test_does_not_open_on_a_single_event(self):
        """Regression: a real log showed one QR event, seconds apart from
        _act_on_unlink_decision() (main.py) independently logging "resuming
        — data preserved" for the very same underlying reading — the two
        mechanisms disagreed because this one used to act on one reading
        while the other, more careful one required several. A single event
        must not be enough on its own any more."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 0

    def test_opens_the_dialog_once_confirmed_by_a_second_event(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 1

    def test_restores_the_window_and_gives_the_classic_logout_cue_first(self):
        """Regression: the first version of this feature jumped straight to
        show_connection_dial() with no sound/MessageBox at all — silent and
        easy to miss entirely if the window was minimized to the tray at the
        time, reported live as exactly that."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert mw.restore_window_calls == 1

    def test_does_not_open_a_second_dialog_on_a_qr_refresh(self):
        """QR codes rotate every ~20-30s while waiting — must not stack
        nested dialogs on every refresh."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 1
        # And the refreshes behind the open dialog are not a flood: opening it
        # resets the counter, so three events never reach the halt. Asserted
        # here because this is exactly where a fake that skipped the reset
        # would diverge from production while still passing the line above.
        assert mw.halt_calls == 0

    def test_does_nothing_when_a_pairing_dialog_is_already_open(self):
        """The dialog is already up (e.g. user-initiated, or already shown
        proactively) — this is the existing display_qrcode_image()/pairing
        code field update path instead."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=True)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 0

    def test_does_nothing_when_never_paired(self):
        """An account that was never paired goes through the normal
        first-run pairing flow already — this path is only for "was paired,
        suddenly needs a fresh QR"."""
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 0

    def test_reconnecting_afterwards_allows_a_future_trigger(self):
        """_auto_repair_dialog_shown is reset by _set_wa_connected(True, ...)
        once the connection genuinely recovers — simulated here directly."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)
        assert connect.show_connection_dial_calls == 1

        mw._auto_repair_dialog_shown = False  # what a real reconnect does
        # _unattended_qr_events is already back at 0: show_connection_dial()
        # (the fake mirrors the real one) calls _reset_unattended_qr_guards()
        # the moment the first dialog opens above.
        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)
        assert connect.show_connection_dial_calls == 2


class TestTheProfileIsRepairedBeforeAskingTheUserToPair:
    """A code minted for a *paired* install means the stored session could not
    be restored — which is exactly the condition the profile recovery exists
    for, and this is the earliest and cleanest evidence of it available.

    Measured on a real install on 2026-09-08. A clean Ctrl+Shift+Q shutdown
    (close-session acknowledged, session observed CLOSED, Chrome confirmed to
    have released the profile), and the next launch logged itself out seven
    seconds into the page load. ProfileHealthTracker counted
    INITIALIZING/CLOSED cycles at ~60 s each and stood at 2 of 3 when the code
    arrived at t+2.4 min — and opening the pairing dialog then froze it there
    for good, because check_wa_connection_http() returns immediately while a
    pairing dialog is up, so the poll that feeds the tracker never ran again.
    The snapshot was restorable by hand the whole time; the app could never
    reach it.
    """

    def test_a_restorable_profile_is_repaired_instead_of_re_paired(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw.profile_restore_available = True
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        # Two events: this branch is also gated by _REPAIR_DIALOG_CONFIRM_EVENTS
        # (TestProactivePairingDialog above), so a single reading is not
        # enough to reach the profile-repair attempt either.
        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert len(mw.recover_calls) == 1
        assert connect.show_connection_dial_calls == 0

    def test_the_reason_says_what_was_observed(self):
        # It lands in shutdown_audit.log, which survives the launch — the one
        # place the next diagnosis can read why a restore was attempted.
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw.profile_restore_available = True
        s = _Stub(mw, _FakeConnect(mw))

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert "pairing code" in (mw.recover_calls[0] or "")

    def test_with_nothing_to_restore_the_user_is_still_sent_to_pair(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw.profile_restore_available = False
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert len(mw.recover_calls) == 1
        assert connect.show_connection_dial_calls == 1

    def test_an_install_that_never_paired_is_not_a_broken_profile(self):
        # Nothing to restore and nothing lost: this is an ordinary first
        # pairing, and the recovery must not run at all.
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        s = _Stub(mw, _FakeConnect(mw))

        s.on_qrcode_update(QR_EVENT)

        assert mw.recover_calls == []

    def test_a_failed_restore_sends_the_user_to_pair_without_a_second_sound(self):
        """The give-up route is a third caller of _show_repair_dialog(), and
        nothing used to bind its play_sound.

        wx.CallAfter(on_give_up) invokes it with no arguments, so it took the
        default — and it is queued directly behind
        wx.CallAfter(self._announce_profile_beyond_repair), whose first
        statement is error_sound.play() and whose MessageBox then pumps the
        queue this callback is sitting in. The two plays therefore landed on
        one stream milliseconds apart, which sound_lib restarts: heard as a
        single truncated blip rather than as two cues, the same defect the
        post-halt route is written around (see
        tests/test_qrcode_unattended_session.py, which pins that one).

        So exactly one error sound belongs on this route — the
        announcement's, which has already explained itself in words the
        second one cannot add to."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw.profile_restore_available = True
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)          # this one starts the restore
        assert mw.restore_starts == 1
        assert mw.error_sound.plays == 0      # nothing has been played yet

        mw.fail_restore()

        # The user is still sent to pair by hand — the repair is what failed,
        # not the reading that prompted it.
        assert connect.show_connection_dial_calls == 1
        assert mw.restore_window_calls == 1
        assert mw.error_sound.plays == 1, (
            "the give-up route played the error sound again on top of "
            "_announce_profile_beyond_repair()'s own")

    def test_a_qr_refresh_after_the_restore_finished_does_not_retry_it(self):
        # Codes rotate every ~20-30 s. _recover_suspect_profile() latches on
        # its own, so the code after the one that started the restore is
        # refused — and when the restore thread's own reset has already
        # landed, that refusal has nothing to fall through into: the counter
        # is back at 0, so the next code is only the first of a fresh run and
        # _REPAIR_DIALOG_CONFIRM_EVENTS is not met.
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw.profile_restore_available = True
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)          # this one starts the restore
        assert mw.restore_starts == 1
        mw.finish_restore()

        s.on_qrcode_update(QR_EVENT)

        assert mw.restore_starts == 1
        assert connect.show_connection_dial_calls == 0
        assert mw.halt_calls == 0

    def test_a_qr_refresh_while_the_restore_still_runs_opens_the_dialog_today(self):
        """The other ordering, which is the one production usually gets: the
        restore thread is still inside close-session / wait_for_profile_release
        / the profile copy when the next code arrives ~20-30 s later.

        Nothing resets the counter in time, so the refused code is still the
        _REPAIR_DIALOG_CONFIRM_EVENTS'th one and the pairing dialog goes up on
        top of a restore still in flight. Pinned as today's behaviour rather
        than as desired behaviour: pairing from that dialog starts a session
        over the very directory restore_snapshot() may still be writing, which
        is the known gap recorded beside _show_repair_dialog()'s call in
        _handle_unattended_qr(). Closing it needs an "in flight" state this
        test would then update."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw.profile_restore_available = True
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)          # this one starts the restore
        s.on_qrcode_update(QR_EVENT)          # the restore thread has not landed

        assert mw.restore_starts == 1         # never retried, whichever way it goes
        assert connect.show_connection_dial_calls == 1

    @pytest.mark.parametrize(
        "reset_after_code, dialog_opens_on_code",
        [
            (None, WebSocketClient._REPAIR_DIALOG_CONFIRM_EVENTS + 1),
            (WebSocketClient._REPAIR_DIALOG_CONFIRM_EVENTS,
             WebSocketClient._REPAIR_DIALOG_CONFIRM_EVENTS * 2),
        ],
        ids=["reset-never-lands", "reset-lands-while-codes-keep-arriving"],
    )
    def test_what_the_restores_counter_reset_costs_in_codes(
            self, reset_after_code, dialog_opens_on_code):
        """The restore thread zeroes _unattended_qr_events when it succeeds,
        and the cost of that has to stay bounded and stated: an account was
        banned over the volume of codes requested from WhatsApp.

        Calling it "one more event" would be optimistic in exactly the wrong
        direction, because the zeroing lands on the restore thread, behind
        close-session, wait_for_profile_release and a copy of a few hundred
        MB — codes counted before it arrives are not given back, and the
        run-up to _REPAIR_DIALOG_CONFIRM_EVENTS starts over. What holds is
        that it can only push the dialog out by that run-up once: the repair
        is attempted only after both gates have passed, both routes out of it
        return above the halt, and the dialog then resets the counter itself.

        So the halt never fires in this flood either way — the dialog is what
        ends it, on the code named by the parametrization. Both are ceilings;
        which one applies is what changes here, and only the dialog's moves.
        """
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw.profile_restore_available = True
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        for code in range(1, dialog_opens_on_code + 1):
            s.on_qrcode_update(QR_EVENT)
            if code == reset_after_code:
                mw.finish_restore()
            # Told apart on purpose: one message for both would read "the
            # dialog opened on code 3, expected it on 3" in the case where it
            # never opened at all, which is the one worth naming plainly.
            if code == dialog_opens_on_code:
                assert connect.show_connection_dial_calls == 1, (
                    "no dialog on code %d, where it was expected" % code)
            else:
                assert connect.show_connection_dial_calls == 0, (
                    "the dialog opened on code %d, expected it on %d"
                    % (code, dialog_opens_on_code))
            assert mw.halt_calls == 0, (
                "the halt fired on code %d; the dialog is what ends this "
                "flood" % code)

        # Once per recovery: the latch means no later code starts a second
        # restore, so the allowance above cannot be taken twice.
        assert mw.restore_starts == 1


class TestStartupGraceWindow:
    """Regression: a real log showed on_qrcode_update firing 11s after
    process start, while /list-chats was still 404ing for another 50s
    because the session itself had not finished starting — WPPConnect's
    first QR event is not immune to the exact slow-boot race
    _WA_STARTUP_GRACE_SECONDS exists for elsewhere. A single such event
    used to open the proactive re-pair dialog immediately; the user then
    followed it into a fresh pairing, which wiped their local history."""

    def test_does_not_open_inside_the_startup_grace_window_even_with_two_events(self):
        mw = _FakeMainWindow(
            paired=True, pairing_dialog_active=False,
            wa_connect_announced=False, wa_startup_time=time.time(),
        )
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 0

    def test_opens_once_the_grace_window_has_elapsed_and_a_second_event_confirms(self):
        mw = _FakeMainWindow(
            paired=True, pairing_dialog_active=False,
            wa_connect_announced=False,
            wa_startup_time=time.time() - (MainWindow._WA_STARTUP_GRACE_SECONDS + 1),
        )
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 1

    def test_a_lone_event_past_the_grace_window_still_is_not_enough(self):
        """The grace window and _REPAIR_DIALOG_CONFIRM_EVENTS are two
        independent requirements — clearing one must not silently satisfy
        the other."""
        mw = _FakeMainWindow(
            paired=True, pairing_dialog_active=False,
            wa_connect_announced=False,
            wa_startup_time=time.time() - (MainWindow._WA_STARTUP_GRACE_SECONDS + 1),
        )
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 0

    def test_opens_once_confirmed_by_a_second_event_once_a_connection_was_ever_confirmed(self):
        """The grace window only protects a (re)connect attempt that has
        never yet succeeded — once _wa_connect_announced is True, a QR event
        is exactly as conclusive as before, even seconds after it fires. The
        _REPAIR_DIALOG_CONFIRM_EVENTS requirement still applies regardless."""
        mw = _FakeMainWindow(
            paired=True, pairing_dialog_active=False,
            wa_connect_announced=True, wa_startup_time=time.time(),
        )
        connect = _FakeConnect(mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 1


class TestQrWithinStartupGraceIsPureLogic:
    """The window itself, with no MainWindow and no socket in the way —
    _qr_within_startup_grace() is the four-value reader in front of it."""

    def test_a_confirmed_connection_ends_the_window_whatever_the_clock_says(self):
        assert qr_within_startup_grace(True, 1000.0, 60.0, 1000.0) is False

    def test_inside_the_window_when_no_connection_was_ever_confirmed(self):
        assert qr_within_startup_grace(False, 1000.0, 60.0, 1030.0) is True

    def test_outside_the_window_once_the_grace_has_elapsed(self):
        assert qr_within_startup_grace(False, 1000.0, 60.0, 1061.0) is False

    def test_a_missing_startup_time_or_grace_never_opens_the_window(self):
        # getattr(..., 0) or 0 is what the caller passes when MainWindow has
        # not written either attribute yet; that must read as "not in a grace
        # window", never as an open-ended one.
        assert qr_within_startup_grace(False, 0, 0, 1000.0) is False
