"""Tests for the stale keep-alive socket retry in api_client.

A connection dropped before any response arrived is retried once — but only
for a method that is safe to repeat. The original version of this file
asserted the opposite: it retried a `POST .../send-reply` and checked that
two calls went out, which is a duplicated WhatsApp message, not a fix.

A dropped socket on a send is ambiguous by nature: the server may have
processed the request and lost the connection while answering, so WhatsApp Web
can already hold the message in its own outbox. CLAUDE.md records that exact
duplication as shipped and fixed once, and MessageQueue's
_classify_send_exception() is where the decision belongs — it is the layer
that can tell a send from a poll. A retry underneath it hides the failure
from it entirely.
"""

from unittest.mock import MagicMock

import pytest
import requests

from core.api_client import api_request


def _dropped_connection():
    return requests.exceptions.ConnectionError(
        "('Connection aborted.', RemoteDisconnected("
        "'Remote end closed connection without response'))"
    )


def _make_caller(calls, fail_first=True):
    def _call(url, headers=None, timeout=None, **kwargs):
        calls.append(url)
        if fail_first and len(calls) == 1:
            raise _dropped_connection()
        resp = MagicMock()
        resp.status_code = 200
        return resp
    return _call


class TestAnIdempotentCallIsRetried:
    def test_a_get_retries_once_and_succeeds(self, monkeypatch):
        calls = []
        monkeypatch.setattr("requests.get", _make_caller(calls))

        response = api_request(
            "GET", "http://127.0.0.1:6300/api/tok/status-session", token="tok")

        assert response.status_code == 200
        assert len(calls) == 2

    def test_a_healthy_get_is_not_retried(self, monkeypatch):
        calls = []
        monkeypatch.setattr("requests.get", _make_caller(calls, fail_first=False))

        api_request("GET", "http://127.0.0.1:6300/api/tok/status-session", token="tok")

        assert len(calls) == 1


class TestASendIsNeverRetriedHere:
    """The guarantee this file exists to protect."""

    def test_a_post_raises_instead_of_resending(self, monkeypatch):
        calls = []
        monkeypatch.setattr("requests.post", _make_caller(calls))

        with pytest.raises(requests.exceptions.ConnectionError):
            api_request(
                "POST", "http://127.0.0.1:6300/api/tok/send-reply", token="tok")

        assert len(calls) == 1, (
            "a dropped socket on a send is ambiguous — resending it here is "
            "how the same message gets delivered twice"
        )

    def test_the_message_send_endpoint_too(self, monkeypatch):
        calls = []
        monkeypatch.setattr("requests.post", _make_caller(calls))

        with pytest.raises(requests.exceptions.ConnectionError):
            api_request(
                "POST", "http://127.0.0.1:6300/api/tok/send-message", token="tok")

        assert len(calls) == 1


class TestAnExplicitlyIdempotentPostIsRetried:
    def test_status_reaction_can_opt_in_to_one_retry(self, monkeypatch):
        calls = []
        monkeypatch.setattr("requests.post", _make_caller(calls))

        response = api_request(
            "POST",
            "http://127.0.0.1:6300/api/tok/react-message",
            token="tok",
            retry_stale_socket=True,
        )

        assert response.status_code == 200
        assert len(calls) == 2


class TestTheRetryDoesNotLeakTheToken:
    def test_the_failure_log_is_scrubbed(self, monkeypatch, caplog):
        """requests copies the failed URL — which carries <session>:<secret> —
        into its own message, so these lines have to go through
        redact_api_error like every other except branch here."""
        def _always_drop(url, headers=None, timeout=None, **kwargs):
            raise requests.exceptions.ConnectionError(
                "HTTPConnectionPool: Max retries exceeded with url: "
                "/api/sess123:s3cr3tv4lue/status-session "
                "(Caused by RemoteDisconnected('Remote end closed connection'))"
            )
        monkeypatch.setattr("requests.get", _always_drop)

        with caplog.at_level("INFO"):
            with pytest.raises(requests.exceptions.ConnectionError):
                api_request(
                    "GET", "http://127.0.0.1:6300/api/sess123:s3cr3tv4lue/status-session",
                    token="sess123:s3cr3tv4lue")

        assert "s3cr3tv4lue" not in caplog.text
        assert "sess123" in caplog.text, "the session name stays, for traceability"


class TestTheRetryDoesNotDoubleTheCallersBudget:
    """_wait_for_session_flushed() polls /status-session with timeout=5 inside
    a _WINDOWS_SHUTDOWN_BUDGET of 4s, and a Node tearing down keep-alives is
    exactly what produces the errors retried here. An uncapped retry could sit
    in one call for 5s past a 4s deadline — the overrun that budget exists to
    prevent, since it is what keeps taskkill off a half-written profile."""

    def test_the_retry_timeout_is_capped(self, monkeypatch):
        seen = []

        def _call(url, headers=None, timeout=None, **kwargs):
            seen.append(timeout)
            if len(seen) == 1:
                raise _dropped_connection()
            resp = MagicMock()
            resp.status_code = 200
            return resp
        monkeypatch.setattr("requests.get", _call)

        api_request("GET", "http://127.0.0.1:6300/api/tok/status-session",
                    token="tok", timeout=5)

        assert seen[0] == 5, "the first attempt keeps the caller's timeout"
        assert seen[1] <= 2.0, f"the retry must be capped, got {seen[1]}"

    def test_a_shorter_caller_timeout_is_not_raised_by_the_cap(self, monkeypatch):
        seen = []

        def _call(url, headers=None, timeout=None, **kwargs):
            seen.append(timeout)
            if len(seen) == 1:
                raise _dropped_connection()
            resp = MagicMock()
            resp.status_code = 200
            return resp
        monkeypatch.setattr("requests.get", _call)

        api_request("GET", "http://127.0.0.1:6300/api/tok/status-session",
                    token="tok", timeout=0.5)

        assert seen[1] == 0.5
