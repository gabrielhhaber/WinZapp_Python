"""Tests for MainWindow._set_wa_connected() ignoring connection-state
transitions once the app has started shutting down.

Reported live: quitting via Ctrl+Alt+Shift+Q announced "modo offline
ativado" (sound + speech) right before the process exited.
_stop_wpp_server() (called from real_exit()) POSTs /close-session to shut
WhatsApp Web down cleanly — but while WinZapp's own WebSocket is still
connected, that arrives as an ordinary "connection.update state=close"
event, indistinguishable at that layer from WhatsApp genuinely dropping
the connection. _shutting_down, set at the very top of real_exit(), makes
_set_wa_connected() — the single entry point for every connection-state
transition — a no-op once quitting has started.

MainWindow is a wx.Frame and cannot be instantiated without a running
wx.App, so _set_wa_connected() is exercised as a plain function against a
minimal stub — same approach as tests/test_sender_names.py. The stub
deliberately carries almost no attributes: if the shutdown guard doesn't
return immediately, touching any of the real method's other logic would
raise AttributeError, itself proof the guard didn't fire.
"""

from main import MainWindow


class _Stub:
    _set_wa_connected = MainWindow._set_wa_connected
    _self_inflicted_teardown_expected = MainWindow._self_inflicted_teardown_expected

    def __init__(self, shutting_down):
        self._shutting_down = shutting_down
        self._wa_connected = True
        self._auto_offline = False


class TestShutdownSuppressesConnectionAnnouncements:
    def test_no_op_while_shutting_down(self):
        """Must not raise even though almost nothing else is set up on the
        stub — proof the function returned before touching any of it."""
        mw = _Stub(shutting_down=True)
        mw._set_wa_connected(False, "session closed")
        # Nothing about connection state should have been touched.
        assert mw._wa_connected is True
        assert mw._auto_offline is False

    def test_still_processes_normally_when_not_shutting_down(self):
        """Sanity check the guard is actually conditional, not a permanent
        short-circuit — a stub with the full attribute set the real method
        needs would be exercised elsewhere; here we only need to confirm it
        does NOT bail out early, i.e. it fails past the guard for a
        different reason (missing attributes), not because of the guard.
        """
        mw = _Stub(shutting_down=False)
        try:
            mw._set_wa_connected(False, "session closed")
        except AttributeError:
            pass  # expected: the minimal stub is missing unrelated attributes
        else:
            raise AssertionError(
                "expected _set_wa_connected to proceed past the shutdown "
                "guard (and fail on a missing unrelated attribute)"
            )
