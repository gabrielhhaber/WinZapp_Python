"""Tests for on_qrcode_update()'s handling of codes nobody is looking at.

Reported live, and it cost a real WhatsApp account: WhatsApp dropped the
session while the user was in the conversation list. WinZapp said nothing
about the disconnection and kept showing the chat list, but every ~20-30s a
screen reader announced "O QR-CODE foi atualizado" — WPPConnect had gone back
to minting a fresh code, and went on doing it for as long as the app stayed
open. The account was banned for the volume of pairing attempts.

Two independent defects produced that:

1. The two dialog-refresh branches keyed on `connect.connection_mode` alone.
   That attribute is written once, when the user picks a pairing method, and
   never reset — on an install that paired by QR it is "qrcode" forever. So a
   code arriving hours after the dialog closed still took the first branch
   (sound + speech + a redraw of destroyed widgets) and, being first in the
   chain, shadowed the proactive re-pairing branch that is the ONLY thing that
   would have told the user the session was gone. Both branches now require
   the pairing dialog to actually be on screen.

2. Nothing bounded the stream. `autoClose`/`deviceSyncTimeout` are pinned to 0
   server-side on purpose (a blind user needs unbounded time to pair), so the
   session asks WhatsApp for a new code indefinitely unless Python closes it.
   With no dialog on screen nobody can consume those codes, so after
   _UNATTENDED_QR_LIMIT of them the session is closed outright.

Same stub approach as tests/test_qrcode_auto_repair_dialog.py — WebSocketClient
methods bound onto a plain object, no socketio and no wx.App.
"""

import time

import pytest

from core.websocket_client import WebSocketClient
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
    def __init__(self):
        self.spoken = []

    def output(self, text):
        self.spoken.append(text)


class _FakeField:
    def __init__(self, value=""):
        self._value = value

    def GetValue(self):
        return self._value

    def SetValue(self, value):
        self._value = value


class _FakeConnect:
    def __init__(self, mode="qrcode", main_window=None, qr_displayed=True):
        self.connection_mode = mode
        self.main_window = main_window
        self.show_connection_dial_calls = 0
        self.displayed = []
        self.pairing_code_field = _FakeField()
        # Whether a QR is already painted. The refresh announcement is gated on
        # it, because the FIRST code arrives through this same branch — the
        # single status-session poll fires 71 ms after /start-session, measured
        # 5.4 s before the event — and calling that a refresh is what made the
        # QR seem to appear only on the second try. Defaults True: every test
        # here but one is about a rotation, which by definition has one on
        # screen already. See tests/test_qr_is_announced_when_it_exists.py.
        self._qr_displayed = qr_displayed

    def show_connection_dial(self):
        self.show_connection_dial_calls += 1
        # Mirrors the real Connect.show_connection_dial(): a human is now
        # looking at the pairing UI, so both unattended-QR guards are dropped.
        # Without this the fake made the flood limit look one event closer
        # than production ever reaches it.
        #
        # It does NOT mirror the other half: the real dialog is modal, so in
        # production every later code is attended and _update_ui() zeroes the
        # counter on each one, before any branch runs (websocket_client.py) —
        # _pairing_attended() is only the condition it tests, never the thing
        # that writes. Here it keeps counting after the call.
        # No test relies on that difference today — the dismissal tests
        # model a dialog already closed — but a test that opens the dialog and
        # then expects production's counts must set _pairing_dialog_active
        # itself.
        self.main_window._reset_unattended_qr_guards()

    def display_qrcode_image(self, base64_img):
        self.displayed.append(base64_img)


class _FakeMainWindow:
    def __init__(self, paired=True, pairing_dialog_active=False):
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
        self._auto_repair_dialog_shown = False
        self.halt_calls = 0
        # Whether a snapshot exists to put back before the user is asked to
        # pair by hand. False everywhere in this module: these tests are about
        # what happens when nothing can be repaired, which is the path that
        # still has to reach the dialog and the flood ceiling.
        self.profile_restore_available = False
        self.recover_calls = []
        # Well past the startup grace window by default (see
        # tests/test_qrcode_auto_repair_dialog.py::TestStartupGraceWindow
        # for the dedicated coverage of that window itself) — nothing in
        # this file is testing startup timing, so it should not interact.
        self._wa_connect_announced = True
        self._WA_STARTUP_GRACE_SECONDS = MainWindow._WA_STARTUP_GRACE_SECONDS
        self._wa_startup_time = time.time() - (self._WA_STARTUP_GRACE_SECONDS * 10)

    def _recover_suspect_profile(self, reason=None, on_give_up=None):
        # on_give_up fires only when a restore was started and then failed;
        # a False return means nothing was started. See the real method.
        self.recover_calls.append(reason)
        return bool(self.profile_restore_available)

    def _is_pairing_dialog_active(self):
        return self._pairing_dialog_active

    def restore_window(self):
        self.restore_window_calls += 1

    # The real method, not a copy of it: a fake that drifts from production
    # here would let the flood limit be asserted at a count production never
    # reaches, which is the exact mistake this file's own history records.
    _reset_unattended_qr_guards = MainWindow._reset_unattended_qr_guards

    def _halt_unattended_qr_session(self):
        # Latches exactly like the real one (main.py), which is what makes
        # asking again on every later event a no-op.
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
CODE_EVENT = {"data": {"qrcode": {"base64": "", "pairingCode": "ABCD1234"}}}


@pytest.fixture(autouse=True)
def _synchronous_call_after(monkeypatch):
    monkeypatch.setattr("core.websocket_client.wx.CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr("core.websocket_client.wx.MessageBox", lambda *a, **kw: None)


class TestNoAnnouncementWithoutADialog:
    """The reported symptom itself: the app talking about codes over a chat
    list, with the session's death never mentioned."""

    def test_qr_mode_does_not_announce_or_redraw_with_no_dialog_on_screen(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mode="qrcode", main_window=mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert mw.speak_output.spoken == []
        assert mw.pairing_code_updated_sound.plays == 0
        assert connect.displayed == []

    def test_phone_mode_does_not_announce_with_no_dialog_on_screen(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mode="phone", main_window=mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(CODE_EVENT)

        assert mw.speak_output.spoken == []
        assert connect.pairing_code_field.GetValue() == ""

    def test_it_surfaces_the_re_pairing_dialog_instead(self):
        """The branch the mode check used to shadow. This is what the user
        should have seen instead of an endless 'QR code updated' — once
        confirmed by a second event (see
        tests/test_qrcode_auto_repair_dialog.py::TestProactivePairingDialog
        for why a single one is no longer enough)."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mode="qrcode", main_window=mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 1
        assert mw.restore_window_calls == 1
        # Nothing has been closed or announced on this route, so the sound is
        # the only cue the user gets before the modal takes focus. It is the
        # other half of the post-halt route's play_sound=False: asserting one
        # without the other lets the default be flipped with the suite green.
        assert mw.error_sound.plays == 1


class TestNormalRotationStillWorks:
    """The dialog IS open — every one of these events is wanted."""

    def test_qr_mode_announces_and_redraws(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=True)
        connect = _FakeConnect(mode="qrcode", main_window=mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert mw.speak_output.spoken == ["qrcode_image_updated"]
        assert connect.displayed == [QR_EVENT["data"]]
        assert connect.show_connection_dial_calls == 0

    def test_the_very_first_code_is_not_announced_as_an_update(self):
        """It still gets drawn — display_qrcode_image() is what announces it,
        at the moment it is really on screen."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=True)
        connect = _FakeConnect(mode="qrcode", main_window=mw, qr_displayed=False)
        s = _Stub(mw, connect)

        s.on_qrcode_update(QR_EVENT)

        assert mw.speak_output.spoken == []
        assert connect.displayed == [QR_EVENT["data"]]

    def test_phone_mode_updates_the_field(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=True)
        connect = _FakeConnect(mode="phone", main_window=mw)
        s = _Stub(mw, connect)

        s.on_qrcode_update(CODE_EVENT)

        assert mw.speak_output.spoken == ["qrcode_updated"]
        assert connect.pairing_code_field.GetValue() == "ABCD1234"

    def test_an_open_dialog_never_counts_toward_the_flood_limit(self):
        """A user can sit on the pairing dialog for as long as they need —
        that is what autoClose: 0 is for, and the halt must not undo it."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=True)
        s = _Stub(mw, _FakeConnect(mode="qrcode", main_window=mw))

        for i in range(10):
            s.on_qrcode_update({"data": f"data:image/png;base64,code{i}"})

        assert mw.halt_calls == 0
        assert mw._unattended_qr_events == 0


class TestAnOpenDialogIsAttendedWhateverTheEventCarries:
    """The `else:` branch reached WITH the dialog on screen: WPPConnect emits
    both kinds of event during either pairing method (a phone-code pairing
    still gets qrCode events carrying an image, before the code itself is
    generated), so an event the refresh branches do not want is routine while
    a user is pairing. Counting those would close the session out from under
    a dialog the user is looking at, with the latch blocking the restart.

    Nothing else covers this: the open-dialog test above only sends events
    that match the mode, so it never reaches the `else:` at all. Drop
    _pairing_attended()'s dialog check — say, because _update_ui() already
    computes dialog_open — and only these fail."""

    def test_phone_mode_ignores_image_only_events_while_the_dialog_is_up(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=True)
        connect = _FakeConnect(mode="phone", main_window=mw)
        s = _Stub(mw, connect)

        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT * 2):
            s.on_qrcode_update(QR_EVENT)

        assert mw.halt_calls == 0
        assert mw._unattended_qr_events == 0

    def test_qr_mode_ignores_code_only_events_while_the_dialog_is_up(self):
        """The symmetric case: the user is pairing by QR-CODE and a pairing
        code arrives with no image."""
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=True)
        connect = _FakeConnect(mode="qrcode", main_window=mw)
        s = _Stub(mw, connect)

        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT * 2):
            s.on_qrcode_update(CODE_EVENT)

        assert mw.halt_calls == 0
        assert mw._unattended_qr_events == 0


class TestUnattendedFloodIsBounded:
    def test_the_session_is_closed_once_the_limit_is_reached(self):
        """Never paired, so no dialog is offered — but the codes keep coming.
        Nobody can scan them, so the session must stop asking for them."""
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        s = _Stub(mw, _FakeConnect(mode="qrcode", main_window=mw))

        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT - 1):
            s.on_qrcode_update(QR_EVENT)
        assert mw.halt_calls == 0

        s.on_qrcode_update(QR_EVENT)
        assert mw.halt_calls == 1

    def test_it_is_reached_after_the_repair_dialog_was_already_offered(self):
        """The dialog was shown once (latched) and is no longer up — e.g. it
        was dismissed. Without this the stream ran unbounded again, which is
        the exact shape of the original bug.

        The dialog opens on the _REPAIR_DIALOG_CONFIRM_EVENTS'th event, which
        goes through show_connection_dial() and zeroes the counter (a human
        is looking at the pairing UI) — so the counted run towards the halt
        only starts afterwards, and needs its own full _UNATTENDED_QR_LIMIT.
        """
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        connect = _FakeConnect(mode="qrcode", main_window=mw)
        s = _Stub(mw, connect)

        for _ in range(WebSocketClient._REPAIR_DIALOG_CONFIRM_EVENTS):
            s.on_qrcode_update(QR_EVENT)
        assert connect.show_connection_dial_calls == 1
        assert mw.halt_calls == 0

        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT):
            s.on_qrcode_update(QR_EVENT)
        assert mw.halt_calls == 1

    def test_halting_is_asked_for_once_per_outage(self):
        """_halt_unattended_qr_session() latches on its own, so re-asking on
        every later event would only fill the log.

        The pairing dialog was already offered, so nothing resets the counter
        or the latch mid-run — the only thing that ever does is a human
        arriving at the pairing UI (or the connection coming back), and either
        of those is a new outage, not more of this one."""
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        mw._auto_repair_dialog_shown = True
        s = _Stub(mw, _FakeConnect(mode="qrcode", main_window=mw))

        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT * 3):
            s.on_qrcode_update(QR_EVENT)

        assert mw.halt_calls == 1

    def test_a_dialog_opening_midway_resets_the_count(self):
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        s = _Stub(mw, _FakeConnect(mode="qrcode", main_window=mw))

        s.on_qrcode_update(QR_EVENT)
        s.on_qrcode_update(QR_EVENT)
        mw._pairing_dialog_active = True
        s.on_qrcode_update(QR_EVENT)
        mw._pairing_dialog_active = False
        s.on_qrcode_update(QR_EVENT)

        assert mw.halt_calls == 0


class TestPairingInProgressIsNeverTouched:
    """The window on_pairing_complete() opens: EndModal has already closed both
    dialogs, so _is_pairing_dialog_active() is False, but the pairing is not
    finished — _pairing_in_progress stays set until messages.set arrives. On a
    fresh install `paired` is still False in that window, so counting these
    events would have closed the session that had just paired, mid first sync,
    with the halt latch blocking any restart. Same two-signal rule
    check_wa_connection_http() applies for the same reason."""

    def test_codes_during_the_post_dialog_pairing_window_are_not_counted(self):
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        mw._pairing_in_progress = True
        s = _Stub(mw, _FakeConnect(mode="qrcode", main_window=mw))

        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT * 2):
            s.on_qrcode_update(QR_EVENT)

        assert mw.halt_calls == 0
        assert mw._unattended_qr_events == 0

    def test_the_count_resumes_once_the_pairing_window_closes(self):
        """messages.set arrived (or the pairing failed): the same events are
        unattended again from here on."""
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        mw._pairing_in_progress = True
        s = _Stub(mw, _FakeConnect(mode="qrcode", main_window=mw))

        s.on_qrcode_update(QR_EVENT)
        mw._pairing_in_progress = False
        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT):
            s.on_qrcode_update(QR_EVENT)

        assert mw.halt_calls == 1


class TestNeverPairedGetsARouteBack:
    """The case that had none: an install that never paired (so the branch that
    offers the re-pairing dialog never runs) sitting in the tray. The halt
    latches, only show_connection_dial() clears it, and nothing on this path
    called it — the app stayed offline forever with nothing said."""

    def test_the_pairing_dialog_is_opened_after_the_halt(self):
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        connect = _FakeConnect(mode="qrcode", main_window=mw)
        s = _Stub(mw, connect)

        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT):
            s.on_qrcode_update(QR_EVENT)

        assert mw.halt_calls == 1
        assert connect.show_connection_dial_calls == 1
        assert mw.restore_window_calls == 1

    def test_a_dismissed_dialog_is_not_reopened_every_minute(self):
        """show_connection_dial() clears both guards on the way in, so a user
        who closes it would otherwise be handed a fresh one on the next flood."""
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        connect = _FakeConnect(mode="qrcode", main_window=mw)
        s = _Stub(mw, connect)

        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT * 4):
            s.on_qrcode_update(QR_EVENT)

        assert connect.show_connection_dial_calls == 1


class TestAPairedInstallBarredByTheGraceWindowStillGetsTheExplanation:
    """The one case the startup grace and _REPAIR_DIALOG_CONFIRM_EVENTS
    changed the behaviour of, and the one nothing else covers: paired,
    inside the grace window, and a full _UNATTENDED_QR_LIMIT of unattended
    events.

    TestStartupGraceWindow (tests/test_qrcode_auto_repair_dialog.py) stops at
    two events, and the flood tests above are all outside the grace window —
    one with paired=False, the other already past the dialog. In between,
    every event walks past the proactive branch (withheld by the grace) and
    reaches the halt with _auto_repair_dialog_shown still False, which is the
    never-paired route: /close-session, which force-kills without flushing
    auth, and then the generic pairing dialog with no word about the login
    that was just lost. Routing that on `paired` instead is what keeps the
    device_logged_out explanation for the user who has something to re-pair.

    The halt itself must NOT move: it is the half of this protection an
    account was banned for not having, so neither new condition may delay
    it."""

    def test_the_halt_still_fires_at_the_limit_and_the_user_is_told_why(self, monkeypatch):
        boxes = []
        monkeypatch.setattr(
            "core.websocket_client.wx.MessageBox",
            lambda text, *a, **kw: boxes.append(text),
        )
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        # A (re)connect that has never confirmed a live connection, seconds
        # old: exactly the window _qr_within_startup_grace() withholds
        # judgment for. Codes are not deduped on this path, so a boot-time
        # burst spends the whole limit inside it.
        mw._wa_connect_announced = False
        mw._wa_startup_time = time.time()
        connect = _FakeConnect(mode="qrcode", main_window=mw)
        s = _Stub(mw, connect)

        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT - 1):
            s.on_qrcode_update(QR_EVENT)
        # Withheld so far — that is what the grace window is for.
        assert connect.show_connection_dial_calls == 0
        assert mw.halt_calls == 0

        s.on_qrcode_update(QR_EVENT)

        assert mw.halt_calls == 1
        assert boxes == ["device_logged_out"]
        assert connect.show_connection_dial_calls == 1
        assert mw.restore_window_calls == 1
        # play_sound=False on this route: the real halt has just played this
        # very error_sound object and spoken over it, and replaying the same
        # stream ~0 ms later is heard as one truncated blip, not two cues.
        # (_FakeMainWindow._halt_unattended_qr_session() plays nothing, so
        # this counts only what _show_repair_dialog() itself did.) The
        # MessageBox still carries its own MB_ICONERROR system sound, which
        # is a different sound and not subject to that restart.
        assert mw.error_sound.plays == 0

    def test_a_never_paired_install_still_skips_the_logged_out_message(self, monkeypatch):
        """The other side of the same routing: with nothing paired there is
        no logged-out device to explain, and the halt has already played the
        error sound and said what happened — so that install gets the pairing
        dialog and no MessageBox, exactly as before."""
        boxes = []
        monkeypatch.setattr(
            "core.websocket_client.wx.MessageBox",
            lambda text, *a, **kw: boxes.append(text),
        )
        mw = _FakeMainWindow(paired=False, pairing_dialog_active=False)
        connect = _FakeConnect(mode="qrcode", main_window=mw)
        s = _Stub(mw, connect)

        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT):
            s.on_qrcode_update(QR_EVENT)

        assert mw.halt_calls == 1
        assert boxes == []
        assert connect.show_connection_dial_calls == 1

    def test_a_dismissed_dialog_is_not_reopened_for_a_paired_install(self, monkeypatch):
        """The dismissal check has to keep running BEFORE the `paired` split.

        Both guard the same block, and only the order separates them: swap
        the two and a paired user who already dismissed the dialog gets a
        fresh device_logged_out MessageBox on every flood. The pre-existing
        cover for this branch is paired=False, which passes either way."""
        boxes = []
        monkeypatch.setattr(
            "core.websocket_client.wx.MessageBox",
            lambda text, *a, **kw: boxes.append(text),
        )
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw._auto_repair_dialog_shown = True
        connect = _FakeConnect(mode="qrcode", main_window=mw)
        s = _Stub(mw, connect)

        for _ in range(WebSocketClient._UNATTENDED_QR_LIMIT * 3):
            s.on_qrcode_update(QR_EVENT)

        assert mw.halt_calls == 1
        assert boxes == []
        assert connect.show_connection_dial_calls == 0


class TestConnectedSessionStillWins:
    def test_a_live_connection_ignores_the_event_entirely(self):
        mw = _FakeMainWindow(paired=True, pairing_dialog_active=False)
        mw._wa_connected = True
        s = _Stub(mw, _FakeConnect(mode="qrcode", main_window=mw))

        s.on_qrcode_update(QR_EVENT)

        assert mw.halt_calls == 0
        assert mw._unattended_qr_events == 0
