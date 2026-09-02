"""Tests for MainWindow._halt_unattended_qr_session().

The other half of the unattended-QR fix (see
tests/test_qrcode_unattended_session.py for the detection side, and the ban it
cost a real user): once WhatsApp has dropped the session and nobody is looking
at a pairing dialog, WPPConnect keeps minting a fresh code every ~20-30s
forever — `autoClose`/`deviceSyncTimeout` are pinned to 0 server-side on
purpose, so nothing below the Python layer will ever stop it. This method is
what stops it.

Three things about it are load-bearing:

* the `_qr_flood_halted` latch, which is what makes the close stick —
  check_wa_connection_http()'s CLOSED branch would otherwise fire
  /start-session on the very next poll and restart the same stream ~30s later
  (that guard is covered in tests/test_auto_start_block_reason.py);
* the announcement, because nothing else says a word. By construction this
  only runs while already disconnected, so `_auto_offline` is already True and
  the next poll's `_set_wa_connected(False, ...)` hits its own no-change early
  return in silence. A screen-reader user in the tray got no sound, no speech,
  and a permanently offline app;
* the empty-token early exit, so a session we have no credential for is never
  addressed by URL with an empty token in it.

MainWindow is a wx.Frame, so the method is exercised as a plain function
against a stub carrying only the attributes it touches.
"""

import inspect

import pytest

import main as main_module
from main import MainWindow
from ui.dialogs.connect import Connect


TOKEN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:$2b$10$abcdef"


class _FakeSound:
    def __init__(self):
        self.plays = 0

    def play(self):
        self.plays += 1


class _FakeI18n:
    def t(self, key):
        return key


class _FakeResponse:
    status_code = 200


class _ImmediateThread:
    """threading.Thread stand-in that runs the target synchronously on
    .start(), so the close-session POST has happened by the time the test
    asserts on it."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class _Stub:
    _halt_unattended_qr_session = MainWindow._halt_unattended_qr_session

    def __init__(self, token=TOKEN):
        self.token = token
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.error_sound = _FakeSound()
        self.i18n = _FakeI18n()
        self.spoken = []
        self.audited = []

    def output(self, text, interrupt=False):
        # The real one forwards to speak_output — see MainWindow.output.
        self.spoken.append(text)

    def _shutdown_audit(self, msg):
        self.audited.append(msg)


@pytest.fixture
def posts(monkeypatch):
    calls = []

    def _fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse()

    monkeypatch.setattr(main_module, "api_post", _fake_post)
    monkeypatch.setattr("main.threading.Thread", _ImmediateThread)
    return calls


class TestItClosesTheSessionOnce:
    def test_the_session_is_closed(self, posts):
        s = _Stub()
        s._halt_unattended_qr_session()

        assert len(posts) == 1
        assert posts[0][0].endswith(f"/api/{TOKEN}/close-session")

    def test_the_latch_is_set(self, posts):
        """check_wa_connection_http()'s CLOSED branch reads exactly this — the
        close alone buys one poll cycle, not a fix."""
        s = _Stub()
        s._halt_unattended_qr_session()

        assert s._qr_flood_halted is True

    def test_a_second_call_does_nothing(self, posts):
        """Re-entrancy: _handle_unattended_qr() asks once per outage, but the
        latch is what guarantees a repeat costs nothing — no second
        close-session, and no second announcement over the screen reader."""
        s = _Stub()
        s._halt_unattended_qr_session()
        s._halt_unattended_qr_session()

        assert len(posts) == 1
        assert s.spoken == ["unattended_qr_session_closed"]
        assert s.error_sound.plays == 1


class TestItSaysSoOutLoud:
    """Nothing else in the app will: the flood only happens while already
    disconnected, so the offline transition has already been announced (or
    swallowed) long before this runs."""

    def test_the_error_sound_and_the_announcement_both_fire(self, posts):
        s = _Stub()
        s._halt_unattended_qr_session()

        assert s.error_sound.plays == 1
        assert s.spoken == ["unattended_qr_session_closed"]

    def test_it_is_announced_even_with_no_token_to_close(self, posts):
        """The user's situation is identical either way — the app is offline
        and needs re-pairing — so the announcement must not depend on whether
        there was a session left to close."""
        s = _Stub(token="")
        s._halt_unattended_qr_session()

        assert s.spoken == ["unattended_qr_session_closed"]


class TestItNeverPostsWithoutAToken:
    def test_no_close_session_is_sent(self, posts):
        """An empty token would address /api//close-session and authenticate
        with `Bearer `, which can only fail — and the latch has already been
        set, which is the part that actually matters here."""
        s = _Stub(token="")
        s._halt_unattended_qr_session()

        assert posts == []
        assert s._qr_flood_halted is True


class TestTheLatchIsReleasedForTheUsersOwnRePairing:
    """The other half of the latch, and the one with teeth: it blocks
    /start-session until something clears it, and the only thing that ever
    does is the user arriving at the pairing dialog. Lose this and the app
    sits in the dialog it just opened, offline, with the health loop still
    refusing to start a session."""

    def test_both_guards_are_dropped(self):
        s = _Stub()
        s._unattended_qr_events = 7
        s._qr_flood_halted = True

        MainWindow._reset_unattended_qr_guards(s)

        assert s._unattended_qr_events == 0
        assert s._qr_flood_halted is False

    def test_show_connection_dial_drops_them_immediately_before_showmodal(self):
        """Checked at source level because show_connection_dial() builds real
        wx dialogs and ends in ShowModal() (same approach as
        tests/test_shutdown_suppresses_auto_start.py).

        WHERE the call sits is the whole point, and "after the dialog is
        constructed" is NOT the property to assert — that is what an earlier
        version of this test checked, and it would have passed while the bug
        was back. _is_pairing_dialog_active() is `bool(dial) and
        dial.IsShown()` (main.py): a dialog constructed but not yet shown
        still reads False. So the window that matters is not before the
        `wx.Dialog(...)` call, it is everything up to ShowModal() — including
        the ~125 lines of widget construction in between, native calls that
        release the GIL for tens of milliseconds. A health-check tick (own
        thread, ~30s) landing there would see every guard clear and fire
        /start-session against the same userDataDir the halt's close-session
        is tearing down.

        Hence adjacency, not ordering: the reset must be the statement
        immediately before ShowModal(), with nothing between them."""
        lines = inspect.getsource(Connect.show_connection_dial).splitlines()
        reset = next(i for i, ln in enumerate(lines)
                     if "_reset_unattended_qr_guards()" in ln)
        show = next(i for i, ln in enumerate(lines)
                    if "self.connection_dial.ShowModal()" in ln)
        assert show - reset == 1, (
            "the guard reset must sit on the line directly above ShowModal(); "
            f"found {show - reset - 1} statement(s) in between"
        )
