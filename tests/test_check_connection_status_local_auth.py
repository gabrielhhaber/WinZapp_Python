"""Tests for Connect.check_connection_status()'s handling of a 401/403 from
our OWN local Node auth middleware (check-connection-session / status-session).

Reported live once already for the sibling health-check path
(main.py's check_wa_connection_http() / _handle_local_auth_rejected(),
see that method's own docstring): a 401/403 here never reaches WhatsApp at
all — it means our own Node auth middleware refused the request, which can
just as easily mean the local session/secret-key state on a freshly started
Node isn't ready yet as a real unlink. That sibling path was fixed to never
wipe on a single such reading.

check_connection_status() is an older, separate implementation of the same
idea that runs even earlier — before the WebSocket or the health-check timer
exist at all (MainWindow.__init__, before prepare_sync()) — and it still
wiped a paired account's entire local database unconditionally on the very
first 401/403. This mirrors the exact "transient warm-up, keep the paired
session" reasoning the function already applies to its own
INITIALIZING/QRCODE/notLogged branch a few lines below.

Connect is a plain class (no wx.Dialog needed for this method) — same
approach as tests/test_pairing_startup_grace.py.
"""

import pytest

import ui.dialogs.connect as connect_module
from ui.dialogs.connect import Connect


class _Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _FakeMainWindow:
    def __init__(self, paired=True, token="sess123:hash456"):
        self.settings = {
            "general": {"language": "pt-BR"},
            "privateinfo": {"paired": paired},
        }
        self._token = token
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.app_name = "WinZapp"
        self.clear_local_data_calls = 0
        self.set_wa_token_calls = []
        self.save_settings_calls = 0

    def _get_wa_token(self):
        return self._token

    def _set_wa_token(self, value):
        self.set_wa_token_calls.append(value)
        self._token = value

    def save_settings(self):
        self.save_settings_calls += 1

    def clear_local_data(self):
        self.clear_local_data_calls += 1


@pytest.fixture(autouse=True)
def _synchronous_ui(monkeypatch):
    # Neither branch under test should even reach a MessageBox any more, but
    # keep this hermetic regardless.
    monkeypatch.setattr(connect_module.wx, "CallAfter", lambda fn, *a, **kw: fn(*a, **kw))
    monkeypatch.setattr(connect_module.wx, "MessageBox", lambda *a, **kw: None)
    monkeypatch.setattr(connect_module.wx, "IsMainThread", lambda: True)


class TestCheckConnectionSession401:
    def test_paired_account_is_kept_not_wiped(self, monkeypatch):
        monkeypatch.setattr(connect_module, "api_get",
                             lambda *a, **kw: _Response(401))
        mw = _FakeMainWindow(paired=True)
        c = Connect(mw)

        result = c.check_connection_status()

        assert result is True
        assert mw.clear_local_data_calls == 0
        assert mw.set_wa_token_calls == []

    def test_never_paired_account_still_gets_wiped(self, monkeypatch):
        """Nothing to preserve for an account that never finished pairing —
        the original "stuck mid-pairing" case this function exists for."""
        monkeypatch.setattr(connect_module, "api_get",
                             lambda *a, **kw: _Response(403))
        mw = _FakeMainWindow(paired=False)
        c = Connect(mw)

        result = c.check_connection_status()

        assert result is False
        assert mw.clear_local_data_calls == 1
        assert mw.set_wa_token_calls == [""]


class TestStatusSessionFallback401:
    """check-connection-session must return 200/status:false to fall through
    to the status-session fallback the second 401/403 branch guards."""

    def _make_get(self):
        calls = {"n": 0}

        def fake_get(url, headers=None, timeout=None):
            calls["n"] += 1
            if "check-connection-session" in url:
                return _Response(200, {"status": False})
            return _Response(401)

        return fake_get, calls

    def test_paired_account_is_kept_not_wiped(self, monkeypatch):
        fake_get, calls = self._make_get()
        monkeypatch.setattr(connect_module, "api_get", fake_get)
        mw = _FakeMainWindow(paired=True)
        c = Connect(mw)

        result = c.check_connection_status()

        assert result is True
        assert mw.clear_local_data_calls == 0
        assert mw.set_wa_token_calls == []
        assert calls["n"] == 2

    def test_never_paired_account_is_wiped_by_the_first_branch_already(self, monkeypatch):
        """paired=False never reaches the fallback at all: the
        check-connection-session branch above already returns False and
        wipes on its own 200/status:false path — asserted here only to
        document why this class has no "unpaired" fallback case."""
        fake_get, calls = self._make_get()
        monkeypatch.setattr(connect_module, "api_get", fake_get)
        mw = _FakeMainWindow(paired=False)
        c = Connect(mw)

        result = c.check_connection_status()

        assert result is False
        assert mw.clear_local_data_calls == 1
        assert calls["n"] == 1
