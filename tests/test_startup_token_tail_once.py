"""Tests for MainWindow.retrieve_token()'s once-per-launch tail.

retrieve_token() has two call sites — MainWindow.__init__ (main thread, before
the WebSocketClient is built from self.token) and _post_ui_init() STEP 2 (worker
thread, which re-reads the token because its own STEP 1b can re-pair into a new
session). Neither guarded against the other, so every launch ran the whole
function twice: a machine's shutdown_audit.log held 50 STARTUP lines across 25
launches, always exactly two per launch. The audit line was the visible half;
the real hazard was two concurrent 'session-cleanup' threads issuing logout
POSTs and rmtree'ing Chrome profile dirs under api/userDataDir/.

These tests pin the resulting contract: the token read/refresh still runs on
every call (a re-pair depends on it), while the STARTUP audit line and
_cleanup_abandoned_sessions() run exactly once per launch.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so retrieve_token() is exercised as a plain function against a small stub —
same approach as tests/test_token_storage_migration.py.
"""

import pytest

import main
from main import MainWindow
from tests.god_modules import patch_main_global


class _FakeStore:
    """Stand-in for SessionStore: only what retrieve_token() touches."""

    def __init__(self):
        self.sessions = []
        self.seeded = []

    def list(self):
        return list(self.sessions)

    def ensure_from_legacy_token(self, name, token):
        self.seeded.append((name, token))
        return {"name": name, "status": "active"}


class _Stub:
    """Minimal stand-in for MainWindow for retrieve_token()."""

    retrieve_token = MainWindow.retrieve_token

    def __init__(self, token="sess-abc:hash-xyz"):
        self.settings = {"privateinfo": {"paired": True}}
        self.account_id = "acc-1"
        self.background_mode = False
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.wpp_api_key = "api-key"
        self._stored_token = token
        # Mirrors the flag MainWindow.__init__ sets just before its own
        # retrieve_token() call.
        self._startup_token_tail_done = False
        self.store = _FakeStore()
        self.audit_lines = []
        self.cleanup_calls = 0

    # The token vault itself is covered by tests/test_token_storage_migration.py;
    # here it only has to round-trip.
    def _get_wa_token(self):
        return self._stored_token

    def _set_wa_token(self, token):
        self._stored_token = token

    def _get_session_store(self):
        return self.store

    def _shutdown_audit(self, msg):
        self.audit_lines.append(msg)

    def _cleanup_abandoned_sessions(self):
        self.cleanup_calls += 1


class TestStartupTailRunsOnce:
    def test_first_call_audits_and_cleans_up(self):
        mw = _Stub()

        mw.retrieve_token()

        assert mw.token == "sess-abc:hash-xyz"
        assert len(mw.audit_lines) == 1
        assert mw.audit_lines[0].startswith("STARTUP account=acc-1")
        assert "active_session='sess-abc'" in mw.audit_lines[0]
        assert mw.cleanup_calls == 1

    def test_second_call_adds_no_startup_line_and_no_second_cleanup(self):
        """The doubled-STARTUP bug: __init__ and _post_ui_init STEP 2 both call
        this, so exactly one audit line and one cleanup thread must survive."""
        mw = _Stub()

        mw.retrieve_token()
        mw.retrieve_token()

        assert len(mw.audit_lines) == 1
        assert mw.cleanup_calls == 1

    def test_further_calls_stay_at_one(self):
        mw = _Stub()

        for _ in range(5):
            mw.retrieve_token()

        assert len(mw.audit_lines) == 1
        assert mw.cleanup_calls == 1


class TestTokenStillRefreshedOnEveryCall:
    def test_second_call_picks_up_a_token_paired_in_the_meantime(self):
        """_post_ui_init STEP 1b can re-show the pairing dialog and pair into a
        NEW session; STEP 2's retrieve_token() is what puts that session's token
        on self.token. Guarding the tail must not cost us that refresh."""
        mw = _Stub()
        mw.retrieve_token()
        assert mw.token == "sess-abc:hash-xyz"

        # connect.py persists the freshly paired session's token.
        mw._set_wa_token("sess-new:hash-new")
        mw.retrieve_token()

        assert mw.token == "sess-new:hash-new"
        # ...but still only the first launch's audit/cleanup.
        assert len(mw.audit_lines) == 1
        assert mw.cleanup_calls == 1

    def test_session_store_is_still_seeded_for_the_new_session(self):
        mw = _Stub()
        mw.retrieve_token()
        mw._set_wa_token("sess-new:hash-new")

        mw.retrieve_token()

        assert mw.store.seeded == [
            ("sess-abc", "sess-abc:hash-xyz"),
            ("sess-new", "sess-new:hash-new"),
        ]


class TestGenerateTokenMigration:
    def test_hash_is_generated_once_not_once_per_call(self, monkeypatch):
        """A token with no ':' is missing its auth hash and gets one from
        POST /generate-token. Two unguarded calls used to mean the migration
        could be attempted twice; the write-back of 'raw:hash' has to make the
        second call a no-op."""
        posts = []

        class _Resp:
            status_code = 200

            def json(self):
                return {"token": "hash-xyz"}

        def _fake_post(url, *args, **kwargs):
            posts.append(url)
            return _Resp()

        patch_main_global(monkeypatch, "api_post", _fake_post)
        mw = _Stub(token="sess-abc")

        mw.retrieve_token()
        mw.retrieve_token()

        assert len(posts) == 1
        assert "/api/sess-abc/api-key/generate-token" in posts[0]
        assert mw.token == "sess-abc:hash-xyz"
        assert len(mw.audit_lines) == 1
        assert mw.cleanup_calls == 1
