"""The pairing dialog must not say the QR is ready before it is.

Reported after the move from Evolution API to WPPConnect: the first time the
dialog asks for the phone to be pointed at the code, nothing is on screen, and
the QR only appears on the first WebSocket refresh. Measured on a real install:

    21:47:13.174  POST /start-session -> 200
    21:47:13.245  GET /status-session -> 200      (71 ms later, no qrcode)
    21:47:13.245  "No QR in status-session yet — waiting for the qrCode event"
    21:47:18.683  the QR finally arrives over the WebSocket

Two announcements, both wrong, and together they are the whole report:

* on_switch_to_qrcode() played the "QR loaded" sound and read the instructions
  the instant start_qrcode_connection() returned — 71 ms after asking the
  session to start, five and a half seconds before there was anything to point
  a phone at. A sighted user watches the box fill in; a blind user was told a
  code was on screen when the box was empty.
* the code that did eventually arrive announced itself as an *update*
  ("O QR-CODE foi atualizado"), because on_qrcode_update()'s refresh branch is
  the path it comes through — which is exactly what "it only shows up after the
  first refresh" describes.

The suspicion was that the first read looks at the wrong fields. It does not:
sessionController.ts answers `qrcode` at the top level and display_qrcode_image()
strips the data-URI prefix it carries. The poll is simply fired 71 ms after
/start-session, so on a fresh session it cannot have one yet — it only pays off
when an already-running session is reused.

So the announcement moves to the moment a QR is really painted, and a watchdog
covers the case where none ever is: the dialog's whole content is an image, so
a blind user cannot tell "still generating" from "this will never work", and
every path that fails to paint one is otherwise silent.

Connect builds real wx dialogs, so the methods are exercised unbound against a
stub carrying only what they touch.
"""

import pytest

from ui.dialogs.connect import Connect


class _FakeSound:
    def __init__(self, log, name):
        self._log = log
        self._name = name

    def play(self):
        self._log.append(self._name)


class _FakeI18n:
    def t(self, key):
        return key


class _FakeMainWindow:
    def __init__(self):
        self.i18n = _FakeI18n()
        self.spoken = []
        self.sounds = []
        self.qrcode_loaded_sound = _FakeSound(self.sounds, "loaded")
        self.pairing_code_updated_sound = _FakeSound(self.sounds, "updated")
        self.error_sound = _FakeSound(self.sounds, "error")

    def output(self, text, interrupt=False):
        self.spoken.append(text)


class _Stub:
    _arm_qr_watchdog = Connect._arm_qr_watchdog
    _cancel_qr_watchdog = Connect._cancel_qr_watchdog
    _announce_qr_failure = Connect._announce_qr_failure
    _QR_WATCHDOG_SECONDS = Connect._QR_WATCHDOG_SECONDS

    def __init__(self):
        self.main_window = _FakeMainWindow()
        self.i18n = self.main_window.i18n
        self._qr_displayed = False
        self.armed = []

    @property
    def spoken(self):
        return self.main_window.spoken

    @property
    def sounds(self):
        return self.main_window.sounds


@pytest.fixture
def stub(monkeypatch):
    s = _Stub()
    # wx.CallLater would need a running app and a real 25 s wait; the timer's
    # own scheduling is wx's business, not this feature's.
    calls = []

    class _FakeTimer:
        def __init__(self, ms, fn, *args):
            calls.append((ms, fn, args))
            self.stopped = False

        def Stop(self):
            self.stopped = True

    monkeypatch.setattr("ui.dialogs.connect.wx.CallLater", _FakeTimer)
    s.armed = calls
    return s


class TestNothingClaimsAQrThatIsNotThere:
    def test_the_watchdog_is_armed_for_the_measured_delay_and_then_some(
            self, stub):
        stub._arm_qr_watchdog()
        assert len(stub.armed) == 1
        ms, _fn, _args = stub.armed[0]
        assert ms == stub._QR_WATCHDOG_SECONDS * 1000
        assert ms >= 6000, (
            "the QR took 5.4 s to arrive on the reporting install; a tighter "
            "bound replaces one wrong announcement with another"
        )

    def test_a_qr_that_never_arrives_is_reported(self, stub):
        stub._arm_qr_watchdog()
        _ms, fire, args = stub.armed[0]

        fire(*args)

        assert stub.spoken == ["qrcode_not_generated"]
        assert stub.sounds == ["error"]

    def test_it_is_reported_only_once(self, stub):
        stub._arm_qr_watchdog()
        stub._announce_qr_failure("first")
        stub._announce_qr_failure("second")
        assert stub.spoken == ["qrcode_not_generated"]

    def test_a_second_attempt_can_fail_out_loud_too(self, stub):
        """A user who cancels and tries again has to be told about that
        failure as well."""
        stub._arm_qr_watchdog()
        stub._announce_qr_failure("first attempt")
        stub._arm_qr_watchdog()
        stub._announce_qr_failure("second attempt")
        assert stub.spoken == ["qrcode_not_generated", "qrcode_not_generated"]

    def test_cancelling_stops_the_timer(self, stub):
        stub._arm_qr_watchdog()
        timer = stub._qr_watchdog
        stub._cancel_qr_watchdog()
        assert timer.stopped is True
        assert stub._qr_watchdog is None

    def test_arming_again_stops_the_previous_timer(self, stub):
        stub._arm_qr_watchdog()
        first = stub._qr_watchdog
        stub._arm_qr_watchdog()
        assert first.stopped is True


class TestTheSourceSaysTheRightThingAtTheRightTime:
    """Structural: the two announcements live inside methods that need a real
    wx dialog to reach, and what matters is *where* they sit."""

    def test_the_panel_no_longer_claims_the_qr_on_open(self):
        import inspect
        source = inspect.getsource(Connect.on_switch_to_qrcode)
        body = "\n".join(l for l in source.splitlines()
                         if not l.lstrip().startswith("#"))
        assert "qrcode_generating" in body
        assert "qrcode_instructions" not in body, (
            "the instructions are announced on open again — before "
            "start_qrcode_connection() can possibly have produced a code"
        )
        assert "qrcode_loaded_sound" not in body

    def test_the_instructions_moved_to_the_first_real_paint(self):
        import inspect
        source = inspect.getsource(Connect.display_qrcode_image)
        assert "qrcode_instructions" in source
        assert "qrcode_loaded_sound" in source
        assert "_qr_displayed" in source

    def test_the_first_code_is_not_announced_as_an_update(self):
        """on_qrcode_update()'s refresh branch is the path the first code
        arrives through, and calling it a refresh is what "it only appears
        after the first update" describes."""
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "client" / "core"
                  / "websocket_client.py").read_text(encoding="utf-8")
        start = source.index('self.i18n.t("qrcode_image_updated")')
        guard = source.rindex('_qr_displayed', 0, start)
        assert source.index("if", guard - 60, guard) < guard, (
            "the update announcement is no longer gated on a QR already being "
            "on screen"
        )
