"""Tests for MainWindow._restart_wpp_session() — the escalation path when the
Puppeteer page itself has structurally died (Puppeteer's own "Attempted to
use detached Frame" error) after a suspend/resume cycle, and no amount of
nudging (_nudge_whatsapp_socket_stream) can ever succeed on it again.

_restart_wpp_session() calls close-session then start-session on the *same*
running WPPConnect Node process — not a full app/process restart. Since the
session already has a valid stored token, start-session silently restores
the existing WhatsApp session (exactly what already happens on every normal
WinZapp restart), without a new QR code.

MainWindow is a wx.Frame and cannot be instantiated without a running wx.App,
so the method under test is exercised as a plain function against a small
stub — same approach as tests/test_message_bookmarks.py.
"""

import time

import pytest

from main import MainWindow


class _Stub:
    _restart_wpp_session = MainWindow._restart_wpp_session
    _auto_restart_grace_active = MainWindow._auto_restart_grace_active
    _WPP_SESSION_RESTART_COOLDOWN = MainWindow._WPP_SESSION_RESTART_COOLDOWN
    _AUTO_RESTART_LOGOUT_GRACE_SECONDS = MainWindow._AUTO_RESTART_LOGOUT_GRACE_SECONDS
    _RECOVERY_CLOSE_WAIT = MainWindow._RECOVERY_CLOSE_WAIT
    # The real gate the health loop reads, not a reimplementation of it.
    _self_inflicted_teardown_expected = MainWindow._self_inflicted_teardown_expected

    def __init__(self):
        self.wpp_server = "http://127.0.0.1"
        self.wpp_port = 6300
        self.token = "test-token"
        self.waited_statuses = []
        self.closed_status = "CLOSED"
        self._recovery_restart_active = False

    def _wait_for_status(self, predicate, timeout, stop_when_connected=True):
        self.waited_statuses.append((predicate, timeout, stop_when_connected))
        return self.closed_status


class TestRestartWppSession:
    def test_calls_close_session_then_start_session(self, monkeypatch):
        calls = []

        def _fake_post(url, json=None, headers=None, timeout=None, **kw):
            calls.append(url)
            class _Resp:
                status_code = 200
            return _Resp()

        monkeypatch.setattr("main.requests.post", _fake_post)
        s = _Stub()
        s._restart_wpp_session()

        assert calls == [
            "http://127.0.0.1:6300/api/test-token/close-session",
            "http://127.0.0.1:6300/api/test-token/start-session",
        ]
        assert len(s.waited_statuses) == 1
        assert s.waited_statuses[0][1:] == (s._RECOVERY_CLOSE_WAIT, False)

    def test_start_session_runs_if_close_request_fails_but_closed_is_confirmed(self, monkeypatch):
        calls = []

        def _fake_post(url, **kw):
            calls.append(url)
            if url.endswith("/close-session"):
                raise ConnectionError("boom")
            class _Resp:
                status_code = 200
            return _Resp()

        monkeypatch.setattr("main.requests.post", _fake_post)
        s = _Stub()
        s._restart_wpp_session()  # must not raise

        assert calls == [
            "http://127.0.0.1:6300/api/test-token/close-session",
            "http://127.0.0.1:6300/api/test-token/start-session",
        ]

    def test_does_not_start_replacement_until_closed_is_confirmed(self, monkeypatch):
        """A 200 from close-session only means the controller accepted the
        request; it is not permission to put a second browser on the slot."""
        calls = []

        def _post(url, **kw):
            calls.append(url)
            return type("_Resp", (), {"status_code": 200})()

        monkeypatch.setattr("main.requests.post", _post)
        s = _Stub()
        s.closed_status = "CLOSING"

        s._restart_wpp_session()

        assert calls == ["http://127.0.0.1:6300/api/test-token/close-session"]

    def test_a_refused_close_with_no_confirmed_closed_does_not_start_either(self, monkeypatch):
        """The other new branch: close-session answered, but not with a 2xx.
        Nothing may start until the status itself says CLOSED."""
        calls = []

        def _post(url, **kw):
            calls.append(url)
            return type("_Resp", (), {"status_code": 503})()

        monkeypatch.setattr("main.requests.post", _post)
        s = _Stub()
        s.closed_status = "INITIALIZING"

        s._restart_wpp_session()

        assert calls == ["http://127.0.0.1:6300/api/test-token/close-session"]

    def test_health_loop_restart_gate_covers_close_wait_and_start(self, monkeypatch):
        """The health loop must not fire its own start-session for the whole
        close -> wait -> start sequence.

        Asserted through _self_inflicted_teardown_expected(), which is what
        check_wa_connection_http()'s CLOSED branch actually consults, rather
        than through a specific flag: _restarting_wpp_session is already one
        of the four flags that method reads, so the suppression is covered for
        exactly this method's duration without it having to touch a flag that
        belongs to another sequence (see the race test below).
        """
        gate_values = []
        s = _Stub()

        def _wait(*args, **kwargs):
            gate_values.append(s._self_inflicted_teardown_expected())
            return "CLOSED"

        def _post(url, **kwargs):
            gate_values.append(s._self_inflicted_teardown_expected())
            return type("_Resp", (), {"status_code": 200})()

        s._wait_for_status = _wait
        monkeypatch.setattr("main.requests.post", _post)

        s._restart_wpp_session()

        assert gate_values and all(gate_values)
        assert s._self_inflicted_teardown_expected() is False

    def test_it_never_clears_a_recovery_sequence_s_own_flag(self, monkeypatch):
        """_force_whatsapp_session_restart() owns _recovery_restart_active for
        the whole of _run_recovery_attempts(), and neither of this method's
        call sites (the dead-browser strike escalation, start_sync's store
        rebuild) checks whether a recovery is already running.

        A version of this method that set and then unconditionally cleared
        that flag wiped it mid-recovery. Two things broke at once: the health
        loop's CLOSED branch resumed firing start-session on top of the
        recovery's in-flight browser, and — because
        _self_inflicted_teardown_expected() reads the same flag — the
        recovery's OWN close-session started being handled as a real
        phone-side unlink, i.e. a false logout.
        """
        monkeypatch.setattr(
            "main.requests.post",
            lambda url, **kw: type("_Resp", (), {"status_code": 200})(),
        )
        s = _Stub()
        s._recovery_restart_active = True  # a recovery already owns the browser

        s._restart_wpp_session()

        assert s._recovery_restart_active is True, (
            "_restart_wpp_session() cleared a flag it does not own — the "
            "recovery sequence is still running and has just lost its gate"
        )
        assert s._self_inflicted_teardown_expected() is True

    def test_respects_the_cooldown_between_restarts(self, monkeypatch):
        calls = []
        monkeypatch.setattr("main.requests.post", lambda url, **kw: calls.append(url))
        s = _Stub()

        s._restart_wpp_session()
        assert len(calls) == 2

        s._restart_wpp_session()  # immediately again — still within cooldown
        assert len(calls) == 2, "a second restart inside the cooldown window must be a no-op"

    def test_restarts_again_once_the_cooldown_has_elapsed(self, monkeypatch):
        calls = []
        monkeypatch.setattr("main.requests.post", lambda url, **kw: calls.append(url))
        s = _Stub()

        s._restart_wpp_session()
        assert len(calls) == 2

        s._last_wpp_session_restart_ts = time.time() - (s._WPP_SESSION_RESTART_COOLDOWN + 1)
        s._restart_wpp_session()
        assert len(calls) == 4

    def test_reentrant_call_while_already_restarting_is_a_no_op(self, monkeypatch):
        calls = []
        monkeypatch.setattr("main.requests.post", lambda url, **kw: calls.append(url))
        s = _Stub()
        s._restarting_wpp_session = True

        s._restart_wpp_session()

        assert calls == []


class TestAutoRestartGraceWindow:
    """The mechanism that keeps _restart_wpp_session() safe to call
    automatically: check_wa_connection_http()'s "confirmed logout" path
    (which wipes the whole local database via _on_disconnect()) must never
    fire as a side effect of our own restart discovering the stored token
    had already gone bad — see _restart_wpp_session()'s docstring for the
    real incident this prevents.
    """

    def test_no_restart_ever_happened_grace_is_not_active(self, monkeypatch):
        s = _Stub()
        assert s._auto_restart_grace_active() is False

    def test_grace_is_active_right_after_a_restart_attempt(self, monkeypatch):
        monkeypatch.setattr("main.requests.post", lambda *a, **kw: None)
        s = _Stub()
        s._restart_wpp_session()
        assert s._auto_restart_grace_active() is True

    def test_grace_expires_after_the_configured_window(self, monkeypatch):
        monkeypatch.setattr("main.requests.post", lambda *a, **kw: None)
        s = _Stub()
        s._restart_wpp_session()
        s._auto_session_restart_ts = time.time() - (s._AUTO_RESTART_LOGOUT_GRACE_SECONDS + 1)
        assert s._auto_restart_grace_active() is False

    def test_grace_is_set_even_when_the_cooldown_blocks_the_actual_restart(self, monkeypatch):
        """The timestamp is set unconditionally, synchronously, before the
        cooldown/re-entrancy checks — a health check landing on another
        thread right after "decided to restart" must see the window active
        immediately, not after whatever delay the restart's own HTTP calls
        take."""
        calls = []
        monkeypatch.setattr("main.requests.post", lambda url, **kw: calls.append(url))
        s = _Stub()
        s._restarting_wpp_session = True  # forces the early-return path

        s._restart_wpp_session()

        assert calls == []
        assert s._auto_restart_grace_active() is True
