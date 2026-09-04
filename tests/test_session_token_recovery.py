"""Tests for MainWindow._recover_active_session_token().

Reported live (issue #155): an account paired and worked normally for an
entire session, then showed the pairing dialog again on the very next
launch — with settings.json holding `paired: true` but no usable token
(WA_token_protected absent, WA_token empty). The account's own
sessions.json still listed one 'active' session whose encrypted token
still decrypted successfully with the account's own secret.key, and its
Chrome userDataDir profile was untouched — manually copying that
decrypted token back into settings.json restored the session, which then
survived further normal restarts. _recover_active_session_token() is that
same recovery, run automatically by check_connection_status() before it
gives up and shows the pairing dialog (see
tests/test_dialog_close_preserves_live_session.py for the code path that
used to cause the loss in the first place).

MainWindow is a wx.Frame, so the method is exercised as a plain function
against a stub carrying only what it touches — same approach as
tests/test_abandoned_pairing_session.py.
"""

import pytest

from main import MainWindow


class _FakeStore:
    def __init__(self, entries):
        self._entries = entries

    def list(self):
        return list(self._entries)


class _Stub:
    _recover_active_session_token = MainWindow._recover_active_session_token

    def __init__(self, store):
        self._store = store
        self.set_token_calls = []

    def _get_session_store(self):
        return self._store

    def _set_wa_token(self, token):
        self.set_token_calls.append(token)


class TestExactlyOneRecoverableSession:
    def test_restores_the_token(self):
        store = _FakeStore([
            {"name": "sess1", "status": "active", "token": "sess1:hash1"},
        ])
        s = _Stub(store)

        result = s._recover_active_session_token()

        assert result == "sess1:hash1"
        assert s.set_token_calls == ["sess1:hash1"]


class TestNoRecoverableSession:
    def test_empty_store_recovers_nothing(self):
        s = _Stub(_FakeStore([]))

        assert s._recover_active_session_token() == ""
        assert s.set_token_calls == []

    def test_no_store_available_recovers_nothing(self):
        class _NoStoreStub:
            _recover_active_session_token = MainWindow._recover_active_session_token

            def _get_session_store(self):
                return None

        assert _NoStoreStub()._recover_active_session_token() == ""

    def test_only_abandoned_sessions_recover_nothing(self):
        store = _FakeStore([
            {"name": "sess1", "status": "abandoned", "token": "sess1:hash1"},
        ])
        s = _Stub(store)

        assert s._recover_active_session_token() == ""
        assert s.set_token_calls == []

    def test_an_active_entry_with_an_undecryptable_token_recovers_nothing(self):
        """SessionStore.list() already returns token=None for anything that
        failed to decrypt — see session_store.py's _decrypt()."""
        store = _FakeStore([
            {"name": "sess1", "status": "active", "token": None},
        ])
        s = _Stub(store)

        assert s._recover_active_session_token() == ""
        assert s.set_token_calls == []


class TestAmbiguousStoreIsNeverGuessed:
    def test_two_active_sessions_recover_nothing(self):
        """Guessing among several candidates could hand back the wrong
        session — the normal pairing flow is the safe fallback here."""
        store = _FakeStore([
            {"name": "sess1", "status": "active", "token": "sess1:hash1"},
            {"name": "sess2", "status": "active", "token": "sess2:hash2"},
        ])
        s = _Stub(store)

        assert s._recover_active_session_token() == ""
        assert s.set_token_calls == []

    def test_one_active_and_one_abandoned_still_recovers_the_active_one(self):
        store = _FakeStore([
            {"name": "sess1", "status": "active", "token": "sess1:hash1"},
            {"name": "sess0", "status": "abandoned", "token": "sess0:hash0"},
        ])
        s = _Stub(store)

        assert s._recover_active_session_token() == "sess1:hash1"


class TestStoreReadFailureIsNonFatal:
    def test_an_exception_reading_the_store_recovers_nothing(self):
        class _BrokenStore:
            def list(self):
                raise RuntimeError("disk error")

        s = _Stub(_BrokenStore())

        assert s._recover_active_session_token() == ""
        assert s.set_token_calls == []
