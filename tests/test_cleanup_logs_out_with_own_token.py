"""Drives the abandoned-session cleanup end to end, instead of reading its source.

`logout-session` is the only call that deregisters a companion device at
WhatsApp — `close-session` merely kills the browser. It used to be sent with
the CURRENT account's token while addressing a DIFFERENT session, which
WPPConnect's verifyToken can only answer 401 to, so every superseded WinZapp
session stayed a linked device on the account. Observed as
`POST /logout-session -> 401 in 3ms`, immediately followed by the store row
being dropped as though the session had been deregistered.

`_logout_abandoned_session` itself is covered behaviourally in
tests/test_abandoned_pairing_session.py. What was NOT covered was the join:
that `_cleanup_abandoned_sessions_worker` reads the token off the store entry
and hands *that* one over. It was asserted with

    assert 'token=s.get("token")' in source

which passes for a line that is present but wrong — a typo in the key, a token
fetched from somewhere else, a call site that stops passing it at all while the
string survives in a comment. This file drives the worker with a fake store and
a fake HTTP layer so the assertion is about what leaves the process.

Still not covered here, and stated so nobody mistakes this for the real thing:
no test in the suite runs this against a live WPPConnect. The 401 was found in
a real log, and the fix has not yet been observed working in one.
"""

import pytest

from main import MainWindow
from tests.god_modules import patch_main_global


SESSION = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OWN_TOKEN = f"{SESSION}:$2b$10$itsownsignature"
CURRENT = "cccccccccccccccccccccccccccccccc:$2b$10$thecurrentone"


class _Store:
    def __init__(self, entries):
        self._entries = entries
        self.removed = []

    def list(self):
        return list(self._entries)

    def remove(self, name):
        self.removed.append(name)


class _Stub:
    _cleanup_abandoned_sessions_worker = MainWindow._cleanup_abandoned_sessions_worker
    _logout_abandoned_session = MainWindow._logout_abandoned_session

    def __init__(self, store):
        self._store = store
        self.token = CURRENT
        # The worker returns early without one; it is only passed to
        # sessions_lock(), which the fixture makes inert.
        self.global_dir = "unused-by-the-fake-lock"
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300

    def _get_session_store(self):
        return self._store

    def _protected_session_names(self):
        return set()


@pytest.fixture
def posts(monkeypatch, tmp_path):
    """Record outgoing requests; make the filesystem and the lock inert."""
    calls = []

    def _fake_post(url, **kwargs):
        calls.append({"url": url, "headers": kwargs.get("headers", {})})

        class _Resp:
            status_code = 200

        return _Resp()

    import main as main_module

    patch_main_global(monkeypatch, "api_post", _fake_post)

    # sessions_lock is imported inside the worker, so patch it at its source.
    import coord_locks

    class _NullLock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(coord_locks, "sessions_lock", lambda *a, **k: _NullLock())

    # Point the profile root somewhere empty so the delete step is a clean
    # "already gone" — this file is about the logout, not the rmtree.
    patch_main_global(monkeypatch, "resource_path",
        lambda *parts: str(tmp_path.joinpath(*parts)),
    )
    return calls


class TestTheWorkerSendsTheSessionsOwnToken:
    def test_the_bearer_is_the_stored_token_not_the_current_one(self, posts):
        store = _Store([
            {"name": SESSION, "status": "abandoned", "token": OWN_TOKEN},
        ])
        _Stub(store)._cleanup_abandoned_sessions_worker()

        logouts = [c for c in posts if "logout-session" in c["url"]]
        assert len(logouts) == 1
        auth = logouts[0]["headers"]["Authorization"]
        assert auth == "Bearer $2b$10$itsownsignature"
        assert "thecurrentone" not in auth

    def test_the_url_names_the_abandoned_session(self, posts):
        store = _Store([
            {"name": SESSION, "status": "abandoned", "token": OWN_TOKEN},
        ])
        _Stub(store)._cleanup_abandoned_sessions_worker()

        url = [c for c in posts if "logout-session" in c["url"]][0]["url"]
        assert f"/api/{SESSION}/logout-session" in url
        assert "itsownsignature" not in url  # a token in a path leaks to logs

    def test_an_entry_with_no_token_is_not_logged_out(self, posts):
        """Firing a request that can only 401 achieves nothing; the local
        profile is still reclaimed."""
        store = _Store([
            {"name": SESSION, "status": "abandoned", "token": None},
        ])
        _Stub(store)._cleanup_abandoned_sessions_worker()

        assert [c for c in posts if "logout-session" in c["url"]] == []
        assert store.removed == [SESSION]

    def test_the_active_session_is_never_logged_out(self, posts):
        """sessions_to_close() only returns abandoned entries — pinned here
        because this worker deletes Chrome profiles."""
        store = _Store([
            {"name": SESSION, "status": "active", "token": OWN_TOKEN},
        ])
        _Stub(store)._cleanup_abandoned_sessions_worker()

        assert posts == []
        assert store.removed == []

    def test_each_abandoned_session_gets_its_own_token(self, posts):
        """The bug was one token used for every session; two entries prove the
        token travels with the row rather than being hoisted."""
        other = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        store = _Store([
            {"name": SESSION, "status": "abandoned", "token": OWN_TOKEN},
            {"name": other, "status": "abandoned", "token": f"{other}:$2b$10$second"},
        ])
        _Stub(store)._cleanup_abandoned_sessions_worker()

        sent = {
            c["url"].split("/api/")[1].split("/")[0]: c["headers"]["Authorization"]
            for c in posts if "logout-session" in c["url"]
        }
        assert sent == {
            SESSION: "Bearer $2b$10$itsownsignature",
            other: "Bearer $2b$10$second",
        }
