"""The WPPConnect session token must never reach log.log in the clear.

token_vault.py Fernet-protects WA_token everywhere it is stored, but two
call sites in ui/dialogs/connect.py used to log the *retrieved* token
straight into a plain %s — Connect._close_active_session() and
Connect.cleanup_pairing_session(). log.log is recreated fresh on every
launch (see CLAUDE.md's Paths/config/i18n section) but is also the file
users are asked to paste when reporting a bug, so a live session token
sitting in it defeats the point of encrypting it at rest.

Masking the *retrieved token* breadcrumb is only half of it. A few lines
below each of those two, the same function builds the close-session URL
with the raw token in the path and posts it inside a try/except that used
to log the exception straight — and requests copies the whole failed URL
into its own message ("Max retries exceeded with url: /api/<session>:
<secret>/close-session"). A timeout there is not exotic: close-session is
called precisely while switching account, while cancelling a pairing
attempt, and after the Node process is already gone. So the fix that
masked the success breadcrumb left the *failure* breadcrumb — the one a
bug report is most likely to contain — leaking the whole credential.

Both call sites are unbound-method tests against a plain stub, the same
style test_pairing_session_reuse.py uses, since Connect needs a running
wx.App to construct for real.
"""

import logging
import types

import pytest
import requests

from core.api_client import redact_token
from ui.dialogs.connect import Connect

close_active_session = Connect._close_active_session
cleanup_pairing_session = Connect.cleanup_pairing_session

SESSION = "b5f5395519a599e1b7ca3d93817a815d"
SECRET = "$2b$10$RlaSnVmEmmflWvfGGED9xe2xG_5UKxFoeyhB1zz6B._nbf4YOnfBi"
TOKEN = f"{SESSION}:{SECRET}"


class _Response:
    status_code = 200


def _transport_failure():
    """The exception requests really raises when nothing answers on 6300.

    Copied from an actual run against a dead port rather than invented —
    the leak is that urllib3 embeds the request path, and the path is where
    WPPConnect keeps the credential.
    """
    return requests.exceptions.ConnectionError(
        f"HTTPConnectionPool(host='127.0.0.1', port=6300): Max retries "
        f"exceeded with url: /api/{TOKEN}/close-session (Caused by "
        f"NewConnectionError('<urllib3.connection.HTTPConnection object at "
        f"0x0000023F1C0>: Failed to establish a new connection: "
        f"[WinError 10061] No connection could be made'))"
    )


@pytest.fixture
def failing_api_post(monkeypatch):
    """Make the close-session call fail the way a dead Node process does."""
    import ui.dialogs.connect as connect_mod

    def _raise(*_args, **_kwargs):
        raise _transport_failure()

    monkeypatch.setattr(connect_mod, "api_post", _raise)


class _ImmediateThread:
    """Stands in for threading.Thread: runs target() synchronously on
    .start() so the test doesn't race a real background thread — and
    doesn't need a real socket for the close-session call either."""

    def __init__(self, target=None, daemon=None, **_kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


class _MainWindow:
    def __init__(self, token=TOKEN):
        self.token = token
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.settings = {"privateinfo": {}}
        self._wa_token = token

    def _get_wa_token(self):
        return self._wa_token

    def _set_wa_token(self, value):
        self._wa_token = value

    def _abandon_closed_session(self, token):
        # _close_active_session() now marks the store entry of the session it
        # just closed as abandoned; this stub only has to accept the call.
        self.abandoned = token


class _ConnectStub:
    def __init__(self, main_window):
        self.main_window = main_window
        self.raw_token = None
        self._last_started_qr_token = None
        self._started_new_session_token = ""

    def _wpp_headers(self, use_global_key=False):
        return {}


@pytest.fixture(autouse=True)
def _no_real_threads_or_network(monkeypatch):
    import ui.dialogs.connect as connect_mod

    monkeypatch.setattr(connect_mod.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(connect_mod, "api_post", lambda *a, **k: _Response())


class TestRedactToken:
    def test_keeps_the_session_name_masks_the_secret(self):
        label = redact_token(TOKEN)

        assert label == f"{SESSION}:***"
        assert SECRET not in label

    def test_empty_token_is_empty(self):
        assert redact_token("") == ""

    def test_a_token_without_a_colon_is_fully_masked(self):
        """Defensive: a malformed/legacy value must never pass through raw."""
        assert redact_token("not-a-real-token-shape") == "***"


class TestCloseActiveSessionNeverLogsTheRawToken:
    def test_via_raw_token(self, caplog):
        stub = _ConnectStub(_MainWindow(token=""))
        stub.raw_token = TOKEN

        with caplog.at_level(logging.INFO):
            close_active_session(stub, sync=True)

        assert SECRET not in caplog.text
        assert SESSION in caplog.text  # still traceable, just not the secret

    def test_via_main_window_token(self, caplog):
        stub = _ConnectStub(_MainWindow(token=TOKEN))

        with caplog.at_level(logging.INFO):
            close_active_session(stub, sync=False)

        assert SECRET not in caplog.text

    def test_no_token_at_all_still_logs_without_crashing(self, caplog):
        stub = _ConnectStub(_MainWindow(token=""))

        with caplog.at_level(logging.INFO):
            close_active_session(stub, sync=True)

        assert "Active token retrieved" in caplog.text


class TestCleanupPairingSessionNeverLogsTheRawToken:
    def test_logs_the_token_masked(self, caplog):
        stub = _ConnectStub(_MainWindow(token=TOKEN))

        with caplog.at_level(logging.INFO):
            cleanup_pairing_session(stub)

        assert SECRET not in caplog.text
        assert "Retrieved token" in caplog.text

    def test_no_token_still_logs_without_crashing(self, caplog):
        stub = _ConnectStub(_MainWindow(token=""))

        with caplog.at_level(logging.INFO):
            cleanup_pairing_session(stub)

        assert "Retrieved token" in caplog.text


class TestAFailedCloseSessionLeaksNothingEither:
    """The sibling breadcrumb: `except Exception as e` a few lines below.

    The suite that shipped with the original fix always stubbed api_post to
    succeed, so this whole path went unexercised and the leak survived the
    change meant to close it.
    """

    def test_close_active_session_masks_the_transport_error(
            self, caplog, failing_api_post):
        stub = _ConnectStub(_MainWindow(token=TOKEN))

        with caplog.at_level(logging.INFO):
            close_active_session(stub, sync=True)

        assert SECRET not in caplog.text
        assert "Error sending close-session request" in caplog.text
        # The kind of failure still has to be readable, or the line is useless.
        assert "ConnectionError" in caplog.text

    def test_cleanup_pairing_session_masks_the_transport_error(
            self, caplog, failing_api_post):
        stub = _ConnectStub(_MainWindow(token=TOKEN))

        with caplog.at_level(logging.INFO):
            cleanup_pairing_session(stub)

        assert SECRET not in caplog.text
        assert "Error sending close-session request" in caplog.text
        assert "ConnectionError" in caplog.text

    def test_the_session_name_survives_so_the_line_is_still_traceable(
            self, caplog, failing_api_post):
        stub = _ConnectStub(_MainWindow(token=TOKEN))

        with caplog.at_level(logging.INFO):
            close_active_session(stub, sync=True)

        assert SESSION in caplog.text


class TestTheHostDeviceProbeMasksItsTransportError:
    """The same leak, in the probe this change made reachable.

    MainWindow._still_linked_on_server() is the last gate before a
    destructive wipe, and it existed with no caller at all until the
    local-401 fix started calling it. Its URL carries the token in the path
    exactly like close-session's does, so the transport failure it logs
    publishes the credential the same way -- and this probe runs precisely
    when the local Node is unhealthy, i.e. when the failure is likely.
    check_wa_connection_http()'s own host-device call has the same shape.
    """

    @staticmethod
    def _stub():
        import main

        stub = types.SimpleNamespace(
            token=TOKEN, wpp_server="http://127.0.0.1", wpp_port=6300)
        stub._still_linked_on_server = types.MethodType(
            main.MainWindow._still_linked_on_server, stub)
        return stub

    @pytest.fixture
    def failing_api_get(self, monkeypatch):
        import main

        def _raise(*_args, **_kwargs):
            raise requests.exceptions.ConnectionError(
                f"HTTPConnectionPool(host='127.0.0.1', port=6300): Max retries "
                f"exceeded with url: /api/{TOKEN}/host-device")

        monkeypatch.setattr(main, "api_get", _raise)

    def test_the_probe_masks_the_transport_error(self, caplog, failing_api_get):
        import connection_state as cs

        with caplog.at_level(logging.INFO):
            outcome = self._stub()._still_linked_on_server()

        assert SECRET not in caplog.text
        # An unreadable probe must never be read as permission to wipe.
        assert outcome == cs.LINK_PROBE_UNKNOWN
        # The line still has to say what went wrong, and for which session.
        assert "ConnectionError" in caplog.text
        assert SESSION in caplog.text
