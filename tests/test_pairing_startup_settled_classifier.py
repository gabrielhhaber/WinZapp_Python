"""The pairing startup grace must not burn its whole budget on a dead session.

PR #141 added a 30s grace so closing the connection dialog moments after
clicking "Conectar" does not kill Chrome before it has produced a QR/pairing
code. The QR branch polls status-session until a QR appears — but a session
whose start-session never came up answers CLOSED on every poll, matching no
exit condition, so the most common failure case ("nothing appeared, so I quit")
was the one that waited the full 30 seconds.

The classification is pulled out to module level so it can be tested without a
Connect instance, which needs main_window, a WebSocket, HTTP and wx just to
exist.
"""

import inspect

import pytest

from ui.dialogs.connect import Connect, pairing_startup_settled


class TestSettled:
    @pytest.mark.parametrize("payload", [
        {"status": "CLOSED"},
        {"status": "DESTROYED"},
        {"status": ""},
        {},
        {"response": {"status": "CLOSED"}},
    ])
    def test_a_session_that_is_not_coming_up_settles_immediately(self, payload):
        """This is the regression: each of these used to keep polling for the
        whole grace window."""
        assert pairing_startup_settled(payload) is True

    @pytest.mark.parametrize("payload", [
        {"qrcode": "data:image/png;base64,iVBOR"},
        {"response": {"qrcode": "data:image/png;base64,iVBOR"}},
        {"status": "CONNECTED"},
        {"status": "qrReadSuccess"},
        {"status": "inChat"},
    ])
    def test_an_attempt_that_already_produced_something_settles(self, payload):
        assert pairing_startup_settled(payload) is True

    @pytest.mark.parametrize("payload", [
        {"status": "INITIALIZING"},
        {"status": "STARTING"},
        {"response": {"status": "INITIALIZING"}},
        {"status": "QRCODE", "qrcode": None},
    ])
    def test_a_session_still_starting_is_worth_waiting_for(self, payload):
        """The whole point of the grace: Chrome's first page load takes
        10-25s and killing it here is what produced 'no QR ever appeared'."""
        assert pairing_startup_settled(payload) is False

    def test_a_non_dict_payload_is_not_treated_as_settled(self):
        assert pairing_startup_settled(None) is False
        assert pairing_startup_settled("CLOSED") is False


class TestTheWaitIsOffTheWxThread:
    def test_the_close_handlers_defer_instead_of_blocking(self):
        for name in ("on_dialog_close", "on_quit_from_connect"):
            src = inspect.getsource(getattr(Connect, name))
            assert "_defer_close_for_pairing_startup(" in src, (
                f"{name}() calls the 30s wait inline on the wx main thread"
            )
            assert "self._wait_for_pairing_startup_settled()" not in src

    def test_the_deferral_hands_the_wait_to_a_worker_thread(self):
        src = inspect.getsource(Connect._defer_close_for_pairing_startup)
        assert "threading.Thread(target=" in src
        assert "wx.CallAfter(resume)" in src, (
            "the close must be re-issued on the wx thread once the wait settles"
        )

    def test_closing_a_second_time_is_honored_immediately(self):
        """A user closing again during the grace is telling us they want out;
        a second wait would read as the app ignoring them."""
        class _Stub:
            _pairing_startup_wait_done = True

        assert Connect._defer_close_for_pairing_startup(_Stub(), lambda: None) is False

    def test_no_wait_when_no_pairing_is_in_flight(self):
        class _MW:
            _pairing_in_progress = False

        class _Stub:
            main_window = _MW()

        assert Connect._defer_close_for_pairing_startup(_Stub(), lambda: None) is False
        # ...and the "already waited" flag must not be set by a no-op call.
        assert getattr(_Stub, "_pairing_startup_wait_done", False) is False
