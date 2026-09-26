"""Tests for recording a failed pairing attempt's session as abandoned.

A pairing attempt mints a fresh session name and calls /start-session, which
makes WPPConnect create a Chrome profile under `api/userDataDir/`. When the
attempt then failed, connect.py cleared the token with `_set_wa_token("")` —
and that path returns before `_set_wa_token`'s own SessionStore block, so the
name was never written to the store at all.

Never registered means it can never be marked `abandoned`, and
`session_store.sessions_to_close()` only ever returns abandoned entries. The
profile was therefore unreachable by every cleanup path, permanently: one
afternoon of failed pairing left 12 directories and 754 MB behind against a
store that listed a single session.

MainWindow is a wx.Frame, so the method under test is exercised as a plain
function against a stub carrying only what it touches.
"""

import pytest

from main import MainWindow
from tests.god_modules import patch_main_global


TOKEN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:$2b$10$abcdef"
NAME = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _FakeStore:
    def __init__(self, entries=None):
        self.entries = list(entries or [])
        self.registered = []

    def get(self, name):
        for entry in self.entries:
            if entry["name"] == name:
                return entry
        return None

    def register(self, name, token=None, status="active"):
        self.registered.append({"name": name, "token": token, "status": status})
        self.entries = [e for e in self.entries if e["name"] != name]
        self.entries.append({"name": name, "status": status})


class _Stub:
    _register_abandoned_session = MainWindow._register_abandoned_session

    def __init__(self, store=None):
        self._store = store
        self.global_dir = None  # no cross-account lock in these tests

    def _get_session_store(self):
        return self._store


class TestRecordsTheFailedAttempt:
    def test_a_fresh_failed_session_is_registered_as_abandoned(self):
        store = _FakeStore()
        _Stub(store)._register_abandoned_session(TOKEN)

        assert len(store.registered) == 1
        assert store.registered[0]["name"] == NAME
        assert store.registered[0]["status"] == "abandoned"

    def test_the_token_is_kept_so_the_session_can_be_deregistered(self):
        """It is the only credential WPPConnect accepts for this session's
        logout-session call — and logout-session is the sole thing that
        unlinks the companion device at WhatsApp. close-session merely kills
        the browser. An entry without a token can be cleaned up locally but
        leaves the device linked forever."""
        store = _FakeStore()
        _Stub(store)._register_abandoned_session(TOKEN)

        assert store.registered[0]["token"] == TOKEN

    def test_the_token_is_stored_verbatim(self):
        """Stored as received. WPPConnect's encryptSession already URL-safes
        the bcrypt hash before returning it, so re-applying that pass here was
        a no-op — but storing verbatim is the honest contract, and the caller
        splits the signature off itself."""
        store = _FakeStore()
        raw = "name123:$2b$10$abc/def+ghi"
        _Stub(store)._register_abandoned_session(raw)

        assert store.registered[0]["token"] == raw

    def test_the_session_name_is_derived_the_same_way_set_wa_token_derives_it(self):
        """Both must agree, or one writes a row the other can never match."""
        store = _FakeStore()
        _Stub(store)._register_abandoned_session("ab/cd+ef:hash")

        assert store.registered[0]["name"] == "ab_cd-ef"

    def test_an_already_abandoned_entry_is_simply_re_registered(self):
        store = _FakeStore([{"name": NAME, "status": "abandoned"}])
        _Stub(store)._register_abandoned_session(TOKEN)

        assert store.registered[0]["status"] == "abandoned"


class TestNeverAbandonsALiveSession:
    """_can_reuse_existing_session() lets an attempt reuse the token of an
    already-paired session, so a failed attempt's token can BE the live one.
    Abandoning it would hand the working Chrome profile to the cleanup to
    delete — the exact class of destructive mistake this whole area has a
    history of."""

    def test_an_active_session_is_left_alone(self):
        store = _FakeStore([{"name": NAME, "status": "active"}])
        _Stub(store)._register_abandoned_session(TOKEN)

        assert store.registered == []
        assert store.get(NAME)["status"] == "active"

    def test_a_pairing_session_is_still_recordable(self):
        """Only 'active' is protected: a half-finished 'pairing' row is exactly
        the state a failed attempt leaves behind."""
        store = _FakeStore([{"name": NAME, "status": "pairing"}])
        _Stub(store)._register_abandoned_session(TOKEN)

        assert store.registered[0]["status"] == "abandoned"


class TestItIsAlwaysSurvivable:
    """This runs on failure paths. It must never turn a pairing failure the
    user could retry into a crash in the handler reporting it."""

    def test_an_empty_token_is_a_no_op(self):
        store = _FakeStore()
        _Stub(store)._register_abandoned_session("")
        assert store.registered == []

    def test_a_token_with_no_name_is_a_no_op(self):
        store = _FakeStore()
        _Stub(store)._register_abandoned_session(":justahash")
        assert store.registered == []

    def test_a_missing_store_is_a_no_op(self):
        _Stub(None)._register_abandoned_session(TOKEN)  # must not raise

    def test_a_raising_store_is_swallowed(self):
        class _Broken:
            def get(self, name):
                raise RuntimeError("sessions.json is unreadable")

        _Stub(_Broken())._register_abandoned_session(TOKEN)  # must not raise


class TestEveryFailureExitCallsIt:
    """_bg_pairing_flow has three exits that leave a created session behind:
    superseded after the phoneCode wait, no code received, and an unexpected
    exception. Missing any one of them reopens the leak for that path only,
    which is how this went unnoticed in the first place."""

    def test_all_three_exits_are_covered(self):
        import inspect

        from ui.dialogs.connect import Connect

        source = inspect.getsource(Connect.on_continue)
        assert source.count("_register_abandoned_session(_attempt_token)") == 3

    def test_the_attempt_token_is_bound_before_the_try(self):
        """The exception handler reads it, so it cannot be assigned only
        inside the block that can throw."""
        import inspect

        from ui.dialogs.connect import Connect

        source = inspect.getsource(Connect.on_continue)
        init = source.index('_attempt_token = ""')
        try_start = source.index("            try:", init)
        assert init < try_start

    def test_it_is_recorded_before_the_token_is_cleared(self):
        """_set_wa_token("") is what loses the name; recording has to happen
        while it is still known."""
        import inspect

        from ui.dialogs.connect import Connect

        source = inspect.getsource(Connect.on_continue)
        marker = source.index("# No code received")
        # Match the calls, not the prose: the comment above them names
        # _set_wa_token("") too, and searching for the bare text finds that
        # first.
        register = source.index(
            "self.main_window._register_abandoned_session(_attempt_token)", marker
        )
        clear = source.index('self.main_window._set_wa_token("")', marker)
        assert register < clear


class _LogoutStub:
    _logout_abandoned_session = MainWindow._logout_abandoned_session

    def __init__(self, current_token="CURRENT:tok"):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self._current_token = current_token

    def _get_wa_token(self):
        return self._current_token


@pytest.fixture
def captured_post(monkeypatch):
    calls = []

    def _fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})

        class _Resp:
            status_code = 200

        return _Resp()

    import main as main_module

    patch_main_global(monkeypatch, "api_post", _fake_post)
    return calls


class TestLogoutUsesTheSessionsOwnToken:
    """WPPConnect's verifyToken middleware checks the bearer against the
    session named in the URL. This used to send self._get_wa_token() — the
    CURRENT account's token — for a logout addressed to a *different* session,
    which can only ever answer 401. Seen in a real run:
    `POST /logout-session -> 401 in 2ms`, immediately followed by the store row
    being dropped as though the session had been deregistered.

    It had not been. close-session only kills the browser; logout-session is
    the sole call that unlinks the companion device at WhatsApp. So every
    superseded WinZapp session stayed a linked device on the account.
    """

    def test_the_bearer_is_the_sessions_own_signature(self, captured_post):
        """Only the bcrypt signature, never the whole "<session>:<signature>"
        token. verifyToken reads the signature out of the URL's session
        parameter first and falls back to the header only when the path has no
        ":<signature>" — which is this call. That fallback ends in
        bcrypt.compare(session + secretKey, headerValue), so a header carrying
        the full token can never match, and every cleanup run answered
        `POST /logout-session -> 401 in 3ms`."""
        _LogoutStub()._logout_abandoned_session("othersession", token="othersession:sig123")

        assert len(captured_post) == 1
        assert captured_post[0]["kwargs"]["headers"]["Authorization"] == "Bearer sig123"

    def test_a_token_with_no_separator_is_sent_as_is(self, captured_post):
        _LogoutStub()._logout_abandoned_session("othersession", token="baresig")
        assert captured_post[0]["kwargs"]["headers"]["Authorization"] == "Bearer baresig"

    def test_the_current_account_token_is_never_used(self, captured_post):
        _LogoutStub(current_token="CURRENT:tok")._logout_abandoned_session(
            "othersession", token="othersession:sig123"
        )

        assert "CURRENT" not in captured_post[0]["kwargs"]["headers"]["Authorization"]

    def test_the_url_carries_the_name_and_never_the_token(self, captured_post):
        """A token in a URL path can be mis-parsed and leaks into access logs;
        it belongs in the Authorization header only."""
        _LogoutStub()._logout_abandoned_session("othersession", token="othersession:sig123")

        url = captured_post[0]["url"]
        assert url.endswith("/api/othersession/logout-session")
        assert "sig123" not in url

    def test_without_a_token_no_request_is_made(self, captured_post):
        """Firing a request that can only 401 achieves nothing; the caller
        should still get True so it goes on to reclaim the local profile."""
        result = _LogoutStub()._logout_abandoned_session("othersession", token=None)

        assert captured_post == []
        assert result is True

    def test_a_missing_token_does_not_fall_back_to_the_current_one(self, captured_post):
        _LogoutStub(current_token="CURRENT:tok")._logout_abandoned_session(
            "othersession", token=""
        )
        assert captured_post == []


class TestLogoutCircuitBreaker:
    def test_skip_short_circuits(self, captured_post):
        assert _LogoutStub()._logout_abandoned_session(
            "s", token="t", skip=True
        ) is False
        assert captured_post == []

    def test_an_empty_name_is_a_no_op(self, captured_post):
        assert _LogoutStub()._logout_abandoned_session("", token="t") is True
        assert captured_post == []

    def test_node_down_reports_false_so_the_caller_stops_trying(self, monkeypatch):
        import requests

        import main as main_module

        def _refuse(url, **kwargs):
            raise requests.exceptions.ConnectionError("refused")

        patch_main_global(monkeypatch, "api_post", _refuse)

        assert _LogoutStub()._logout_abandoned_session("s", token="t") is False

    def test_other_errors_keep_the_caller_going(self, monkeypatch):
        import main as main_module

        def _boom(url, **kwargs):
            raise ValueError("something else")

        patch_main_global(monkeypatch, "api_post", _boom)

        assert _LogoutStub()._logout_abandoned_session("s", token="t") is True


# The join — that _cleanup_abandoned_sessions_worker actually hands this
# function the token off the store row — used to be asserted here by reading
# the source for the literal `token=s.get("token")`. That passes for a line
# that is present but wrong: a typo in the key, a token fetched from somewhere
# else, or a call site that stops passing it while the string survives in a
# comment. It is now driven for real, against a fake store and a fake HTTP
# layer, in tests/test_cleanup_logs_out_with_own_token.py.
