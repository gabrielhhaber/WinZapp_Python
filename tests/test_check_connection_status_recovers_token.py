"""Tests for check_connection_status() calling
MainWindow._recover_active_session_token() when paired=True but no token
is saved — the wiring half of the issue #155 fix (see
tests/test_session_token_recovery.py for the recovery method itself and
tests/test_dialog_close_preserves_live_session.py for the code path that
used to lose the token reference while leaving the session recoverable).

Connect is a plain class — same approach as tests/test_pairing_startup_grace.py.
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
    def __init__(self, paired, stored_token, recovered_token):
        self.settings = {
            "general": {"language": "pt-BR"},
            "privateinfo": {"paired": paired},
        }
        self._token = stored_token
        self._recovered_token = recovered_token
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.recover_calls = 0

    def _get_wa_token(self):
        return self._token

    def _recover_active_session_token(self):
        self.recover_calls += 1
        if self._recovered_token:
            self._token = self._recovered_token
        return self._recovered_token


@pytest.fixture(autouse=True)
def _connected_api(monkeypatch):
    monkeypatch.setattr(connect_module, "api_get",
                         lambda *a, **kw: _Response(200, {"status": True}))


class TestPairedWithNoToken:
    def test_calls_recovery_and_uses_the_result(self):
        mw = _FakeMainWindow(paired=True, stored_token="", recovered_token="sess1:hash1")
        c = Connect(mw)

        result = c.check_connection_status()

        assert mw.recover_calls == 1
        assert result is True

    def test_recovery_finding_nothing_falls_back_to_token_tk_check(self, monkeypatch, tmp_path):
        monkeypatch.setattr(connect_module, "data_path", lambda *parts: str(tmp_path.joinpath(*parts)))
        mw = _FakeMainWindow(paired=True, stored_token="", recovered_token="")

        result = Connect(mw).check_connection_status()

        assert mw.recover_calls == 1
        assert result is False  # no token.tk in the empty tmp_path either


class TestNeverPairedWithNoToken:
    def test_recovery_is_never_attempted(self, monkeypatch, tmp_path):
        """Nothing to recover for an account that never finished pairing —
        matches _recover_active_session_token()'s own callers-must-check
        contract and avoids restoring a stray abandoned/pairing session as
        if it were a real account."""
        monkeypatch.setattr(connect_module, "data_path", lambda *parts: str(tmp_path.joinpath(*parts)))
        mw = _FakeMainWindow(paired=False, stored_token="", recovered_token="sess1:hash1")

        result = Connect(mw).check_connection_status()

        assert mw.recover_calls == 0
        assert result is False


class TestTokenAlreadyPresent:
    def test_recovery_is_never_attempted(self):
        mw = _FakeMainWindow(paired=True, stored_token="sess1:hash1", recovered_token="")

        result = Connect(mw).check_connection_status()

        assert mw.recover_calls == 0
        assert result is True
