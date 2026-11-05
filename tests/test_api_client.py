"""Tests for core/api_client.py — the single door to the embedded Node API.

Two things this exists to guarantee, both of which the 74 hand-rolled call
sites in main.py could not:

1. Every request carries an X-Request-Id, so a line in log.log and a line in
   wppconnect.log can be matched up. The Node middleware already generates one
   when the header is absent; it just never had one handed to it.

2. No URL is ever logged in full. WPPConnect authenticates by putting
   <session>:<token> in the PATH, so logging a URL publishes the credential
   that authorises every other call — and log.log is the file users are asked
   to send when something breaks. The log this replaces had 2,360 such lines.
"""

import logging

import pytest
import requests

from core.api_client import (
    api_get,
    api_headers,
    api_post,
    api_request,
    new_request_id,
    redact_api_error,
    redact_api_url,
    redact_credentials,
    redact_token,
)
from main import MainWindow

TOKEN = "$2b$10$RlaSnVmEmmflWvfGGED9xe2xG_5UKxFoeyhB1zz6B._nbf4YOnfBi"
SESSION = "b5f5395519a599e1b7ca3d93817a815d"
URL = f"http://127.0.0.1:6300/api/{SESSION}:{TOKEN}/get-messages/5511@c.us?count=200"


def _transport_failure():
    """The exception requests really raises when nothing answers on 6300.

    Taken from an actual run against a dead port. urllib3 embeds the request
    path in its own message, and the path is where WPPConnect keeps the
    credential — so the exception leaks everything the URL would.
    """
    return requests.exceptions.ConnectionError(
        f"HTTPConnectionPool(host='127.0.0.1', port=6300): Max retries "
        f"exceeded with url: /api/{SESSION}:{TOKEN}/get-messages/5511@c.us "
        f"(Caused by NewConnectionError('<urllib3.connection.HTTPConnection "
        f"object at 0x0000023F1C0>: Failed to establish a new connection: "
        f"[WinError 10061] No connection could be made'))"
    )


class _Response:
    status_code = 200


class _Session:
    """Stands in for requests, recording what it was asked to do."""

    def __init__(self, exc=None):
        self.calls = []
        self._exc = exc

    def get(self, url, **kwargs):
        return self._record("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._record("POST", url, kwargs)

    def _record(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if self._exc:
            raise self._exc
        return _Response()


class TestTheCredentialNeverReachesTheLog:
    def test_the_token_is_stripped_from_a_logged_url(self):
        label = redact_api_url(URL)

        assert TOKEN not in label
        assert SESSION not in label
        assert label == "/get-messages/5511@c.us?count=200"

    def test_a_real_request_logs_no_token(self, caplog):
        session = _Session()

        with caplog.at_level(logging.INFO):
            api_request("GET", URL, token="tok", session=session)

        assert TOKEN not in caplog.text
        assert SESSION not in caplog.text
        assert "get-messages" in caplog.text

    def test_a_failed_request_logs_no_token_either(self, caplog):
        """The failure path is the one that gets pasted into a bug report."""
        session = _Session(exc=requests.ConnectionError("boom"))

        with caplog.at_level(logging.WARNING):
            with pytest.raises(requests.ConnectionError):
                api_request("POST", URL, token="tok", session=session)

        assert TOKEN not in caplog.text
        assert "boom" in caplog.text

    def test_a_non_api_url_is_still_reduced_to_its_path(self):
        assert redact_api_url("http://127.0.0.1:6300/healthz") == "/healthz"

    def test_an_empty_url_does_not_crash(self):
        assert redact_api_url("") == ""

    def test_redact_token_keeps_the_session_masks_the_secret(self):
        label = redact_token(f"{SESSION}:{TOKEN}")

        assert label == f"{SESSION}:***"
        assert TOKEN not in label

    def test_redact_token_on_an_empty_token_does_not_crash(self):
        assert redact_token("") == ""


class TestATransportErrorCarriesTheCredentialToo:
    """requests puts the failed URL in the exception message, so an exception
    is as much of a leak as a URL — and it is the failure path, not the happy
    one, that ends up in the log a user is asked to send."""

    def test_the_warning_line_masks_it(self, caplog):
        session = _Session(exc=_transport_failure())

        with caplog.at_level(logging.WARNING):
            with pytest.raises(requests.ConnectionError):
                api_request("POST", URL, token="tok", session=session)

        assert TOKEN not in caplog.text
        assert "ConnectionError" in caplog.text
        assert "WinError 10061" in caplog.text  # the detail still survives

    def test_the_re_raised_exception_no_longer_carries_it(self):
        """The whole point of scrubbing at this one door: ~50 call sites in
        main.py and connect.py log the exception after it comes back out, and
        message_queue puts str(exc) into a user-facing failure dialog."""
        session = _Session(exc=_transport_failure())

        with pytest.raises(requests.ConnectionError) as caught:
            api_request("POST", URL, token="tok", session=session)

        assert TOKEN not in str(caught.value)
        assert SESSION in str(caught.value)  # still traceable

    def test_a_nested_exception_is_scrubbed_too(self):
        """requests wraps urllib3: ConnectionError(MaxRetryError(...)), so the
        leaking string sits one exception deep rather than in args[0]."""
        inner = _transport_failure()
        session = _Session(exc=requests.ConnectionError(inner))

        with pytest.raises(requests.ConnectionError) as caught:
            api_request("GET", URL, token="tok", session=session)

        assert TOKEN not in str(caught.value)

    def test_the_exception_type_and_attributes_survive_the_scrub(self):
        """Retry classification is done by type and by .response, so scrubbing
        must not replace the object — only rewrite its message."""
        original = _transport_failure()
        original.response = None
        original.request = "sentinel"
        session = _Session(exc=original)

        with pytest.raises(requests.ConnectionError) as caught:
            api_get(URL, token="tok", session=session)

        assert caught.value is original
        assert caught.value.request == "sentinel"

    def test_an_exception_carrying_no_credential_is_left_bit_identical(self):
        """Nothing to mask means nothing to touch — args is not even
        reassigned, so a caller comparing identity still sees what requests
        raised."""
        original = requests.Timeout("read timed out")
        args_before = original.args
        session = _Session(exc=original)

        with pytest.raises(requests.Timeout) as caught:
            api_get(URL, token="tok", session=session)

        assert caught.value.args is args_before

    def test_an_unscrubbable_exception_fails_open_but_says_so(self, caplog):
        """Failing open is the right call — a scrub that raised would replace
        the failure the caller is about to classify. Failing open *silently*
        is how a leak goes unnoticed, so it has to leave a breadcrumb."""

        class _ReadOnlyArgs(requests.ConnectionError):
            @property
            def args(self):
                return (f"/api/{SESSION}:{TOKEN}/close-session",)

        session = _Session(exc=_ReadOnlyArgs())

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(requests.ConnectionError):
                api_get(URL, token="tok", session=session)

        assert "could not scrub _ReadOnlyArgs args" in caplog.text
        # The breadcrumb names the type, never the unmasked text.
        assert TOKEN not in caplog.records[0].getMessage()


class TestRedactCredentials:
    def test_it_masks_the_secret_inside_arbitrary_prose(self):
        text = f"Max retries exceeded with url: /api/{SESSION}:{TOKEN}/close-session"

        masked = redact_credentials(text)

        assert TOKEN not in masked
        assert f"/api/{SESSION}:***/close-session" in masked

    def test_it_is_idempotent(self):
        once = redact_credentials(f"/api/{SESSION}:{TOKEN}/x")

        assert redact_credentials(once) == once

    def test_text_without_a_credential_is_untouched(self):
        assert redact_credentials("connection refused") == "connection refused"

    def test_empty_text_does_not_crash(self):
        assert redact_credentials("") == ""

    def test_redact_api_error_names_the_failure_kind(self):
        label = redact_api_error(_transport_failure())

        assert label.startswith("ConnectionError: ")
        assert TOKEN not in label


class TestTheGlobalSecretKeyInThePath:
    """WPPConnect also puts its GLOBAL secret key in the URL path — the
    credential that mints session tokens, and a real user secret whenever
    wpp_custom_api is on rather than app_settings.py's public default.

    routes/index.ts puts it in *two* different positions, and assuming only
    one of them is how this leaked twice:

      /api/<key>/<endpoint>            show-all-sessions, start-all,
                                       backup-sessions, restore-sessions
      /api/<session>/<key>/<endpoint>  generate-token, clear-session-data

    show-all-sessions is the live one: _close_orphaned_server_sessions()
    (main.py) calls it on every shutdown under a custom API and logs the
    exception, and a timeout there is the *normal* case because the server
    is on its way out.
    """

    SECRET_KEY = "user-custom-secret-key-ABC123"

    FIRST_POSITION = ("show-all-sessions", "start-all",
                      "backup-sessions", "restore-sessions")
    SECOND_POSITION = ("generate-token", "clear-session-data")

    @pytest.mark.parametrize("endpoint", FIRST_POSITION)
    def test_first_position_key_is_masked_in_prose(self, endpoint):
        masked = redact_credentials(f"/api/{self.SECRET_KEY}/{endpoint}")

        assert self.SECRET_KEY not in masked
        assert masked == f"/api/***/{endpoint}"
        assert redact_credentials(masked) == masked  # idempotent

    @pytest.mark.parametrize("endpoint", SECOND_POSITION)
    def test_second_position_key_is_masked_in_prose(self, endpoint):
        masked = redact_credentials(f"/api/{SESSION}/{self.SECRET_KEY}/{endpoint}")

        assert self.SECRET_KEY not in masked
        assert masked == f"/api/{SESSION}/***/{endpoint}"
        assert redact_credentials(masked) == masked  # idempotent

    @pytest.mark.parametrize("endpoint", SECOND_POSITION)
    def test_second_position_key_is_dropped_from_the_label(self, endpoint):
        url = f"http://127.0.0.1:6300/api/{SESSION}/{self.SECRET_KEY}/{endpoint}"

        label = redact_api_url(url)

        assert self.SECRET_KEY not in label
        assert label == f"/{endpoint}"

    @pytest.mark.parametrize("endpoint", FIRST_POSITION)
    def test_first_position_key_is_dropped_from_the_label(self, endpoint):
        """Already true before the fix — the key happens to be the segment
        redact_api_url() discards anyway. Pinned so it stays true."""
        url = f"http://127.0.0.1:6300/api/{self.SECRET_KEY}/{endpoint}"

        assert redact_api_url(url) == f"/{endpoint}"

    def test_the_live_show_all_sessions_failure_logs_no_key(self, caplog):
        """main.py's orphan sweep, reproduced: dead port, key in the message."""
        url = f"http://127.0.0.1:6399/api/{self.SECRET_KEY}/show-all-sessions"
        exc = requests.exceptions.ConnectTimeout(
            f"HTTPConnectionPool(host='127.0.0.1', port=6399): Max retries "
            f"exceeded with url: /api/{self.SECRET_KEY}/show-all-sessions "
            f"(Caused by ConnectTimeoutError(...))"
        )
        session = _Session(exc=exc)

        with caplog.at_level(logging.WARNING):
            with pytest.raises(requests.ConnectTimeout) as caught:
                api_get(url, session=session)

        assert self.SECRET_KEY not in caplog.text
        # And the re-raised exception the caller logs itself is clean too.
        assert self.SECRET_KEY not in str(caught.value)

    def test_a_generate_token_call_logs_no_key(self, caplog):
        url = f"http://127.0.0.1:6300/api/{SESSION}/{self.SECRET_KEY}/generate-token"
        session = _Session()

        with caplog.at_level(logging.INFO):
            api_post(url, session=session)

        assert self.SECRET_KEY not in caplog.text

    def test_an_ordinary_route_is_unaffected(self):
        """The extra passes must not eat a legitimate two-segment endpoint."""
        assert redact_api_url(URL) == "/get-messages/5511@c.us?count=200"
        assert redact_credentials("/api/sess/5511@c.us/get-messages") == \
            "/api/sess/5511@c.us/get-messages"

    def test_the_route_lists_still_match_the_node_router(self):
        """The enumeration is the whole risk: a `:secretkey` route added to
        routes/index.ts and not to api_client.py leaks silently. This is the
        check that says so out loud."""
        import re as _re
        from pathlib import Path

        import core.api_client as mod

        router = Path(__file__).resolve().parents[1] / \
            "client" / "api_patches" / "src" / "routes" / "index.ts"
        source = router.read_text(encoding="utf-8")

        first = set(_re.findall(r"/api/:secretkey/([\w-]+)", source))
        second = set(_re.findall(r"/api/:session/:secretkey/([\w-]+)", source))

        assert first == {r.lstrip("/") for r in mod._SECRET_KEY_FIRST_ROUTES}
        assert second == {r.lstrip("/") for r in mod._SECRET_KEY_SECOND_ROUTES}


class TestTheScrubKeepsSendClassificationIntact:
    """The expensive invariant, pinned end to end.

    _scrub_exception_args() rewrites an exception that is already on its way
    to main.py's _classify_send_exception(), which decides by isinstance
    whether a failed send was *ambiguous* — WhatsApp Web may have taken the
    message into its own outbox, so it must be dropped rather than resent.
    Retrying an ambiguous send is what produced the reported "30 copies arrive
    when the internet comes back". A scrub that replaced the exception object,
    or its type, would silently turn ambiguous into retry.
    """

    class _Stub:
        """_classify_send_exception reads no instance state — it only inspects
        the exception it is handed — so the stub carries nothing."""

        _classify_send_exception = MainWindow._classify_send_exception

    def test_a_scrubbed_timeout_is_still_ambiguous_and_not_retried(self, caplog):
        session = _Session(exc=_transport_failure())

        with pytest.raises(requests.ConnectionError) as caught:
            api_post(URL, token="tok", session=session)

        with caplog.at_level(logging.ERROR):
            verdict = self._Stub()._classify_send_exception(caught.value, "send_text_message")

        assert verdict["ambiguous"] is True
        assert verdict["retry"] is False
        assert verdict["ok"] is False
        # The verdict's own error string is handed to the failure dialog and
        # read aloud by the screen reader, so it must be clean too.
        assert TOKEN not in verdict["error"]
        assert TOKEN not in caplog.text

    def test_a_definite_failure_is_still_retried_after_a_scrub(self):
        """The other side of the branch: a non-transport error must keep
        retrying, or a genuinely failed send would be dropped silently."""
        exc = ValueError(f"boom at /api/{SESSION}:{TOKEN}/send-message")
        session = _Session(exc=exc)

        with pytest.raises(ValueError) as caught:
            api_post(URL, token="tok", session=session)

        verdict = self._Stub()._classify_send_exception(caught.value, "send_text_message")

        assert verdict["retry"] is True
        assert "ambiguous" not in verdict
        assert TOKEN not in verdict["error"]


class TestWhatRedactCredentialsDoesNotCover:
    """Pinning the *limits* of the helper, because its previous docstring
    claimed to mask every credential anywhere and a reader who believed that
    would skip redacting at their own call site."""

    def test_a_bearer_header_is_not_masked(self):
        text = f"Authorization: Bearer {SESSION}:{TOKEN}"

        assert redact_credentials(text) == text

    def test_a_query_parameter_is_not_masked(self):
        text = f"?token={SESSION}:{TOKEN}"

        assert redact_credentials(text) == text

    def test_the_api_marker_is_case_sensitive(self):
        text = f"/API/{SESSION}:{TOKEN}/close-session"

        assert redact_credentials(text) == text


class TestCorrelation:
    def test_every_request_carries_an_id(self):
        session = _Session()

        api_get(URL, token="tok", session=session)

        _method, _url, kwargs = session.calls[0]
        assert kwargs["headers"]["X-Request-Id"]

    def test_a_supplied_id_is_the_one_sent(self):
        session = _Session()

        api_post(URL, token="tok", request_id="abc123", session=session)

        assert session.calls[0][2]["headers"]["X-Request-Id"] == "abc123"

    def test_the_same_id_appears_in_the_log(self, caplog):
        session = _Session()

        with caplog.at_level(logging.INFO):
            api_get(URL, token="tok", request_id="abc123", session=session)

        assert "rid=abc123" in caplog.text

    def test_the_generated_id_is_one_the_node_side_accepts(self):
        """The middleware only honours /^[A-Za-z0-9._:-]{1,128}$/ and silently
        substitutes its own otherwise — which would break correlation with
        nothing to say why."""
        import re

        for _ in range(20):
            assert re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", new_request_id())


class TestTheHeaders:
    def test_it_authorises_and_correlates(self):
        headers = api_headers("tok", request_id="abc")

        assert headers["Authorization"] == "Bearer tok"
        assert headers["X-Request-Id"] == "abc"
        assert headers["Content-Type"] == "application/json"

    def test_a_body_less_request_asks_for_no_json_content_type(self):
        assert "Content-Type" not in api_headers("tok", json_body=False)

    def test_caller_supplied_headers_are_kept(self):
        session = _Session()

        api_get(URL, token="tok", session=session,
                headers={"Authorization": "Bearer other", "X-Custom": "1"})

        sent = session.calls[0][2]["headers"]
        assert sent["Authorization"] == "Bearer other"  # not overwritten
        assert sent["X-Custom"] == "1"
        assert sent["X-Request-Id"]


class TestTheDurationIsReported:
    def test_a_normal_call_logs_at_info(self, caplog):
        session = _Session()

        with caplog.at_level(logging.INFO):
            api_get(URL, token="tok", session=session)

        assert "-> 200 in" in caplog.text
        assert "WARNING" not in caplog.text

    def test_a_slow_call_is_a_warning(self, caplog, monkeypatch):
        """This is the loopback: a call this slow means the page or Puppeteer
        is busy, which is the thing worth noticing."""
        import core.api_client as mod

        ticks = iter([0.0, 5.0])
        monkeypatch.setattr(mod.time, "monotonic", lambda: next(ticks))
        session = _Session()

        with caplog.at_level(logging.INFO):
            api_get(URL, token="tok", session=session)

        assert "WARNING" in caplog.text


class TestItStaysOutOfTheCallersWay:
    def test_the_exception_reaches_the_caller_unchanged(self):
        """Retry policy stays where the knowledge is: message_queue treats an
        ambiguous timeout differently from a definite failure on purpose."""
        session = _Session(exc=requests.Timeout("slow"))

        with pytest.raises(requests.Timeout):
            api_get(URL, token="tok", session=session)

    def test_extra_kwargs_are_passed_through(self):
        session = _Session()

        api_post(URL, token="tok", session=session, json={"a": 1}, stream=True)

        kwargs = session.calls[0][2]
        assert kwargs["json"] == {"a": 1}
        assert kwargs["stream"] is True
        assert kwargs["timeout"] == 30


class TestAnEmojiSplitInHalvesIsJoinedBeforeItLeaves:
    """A wx char handler can put an emoji into a field as two loose UTF-16
    halves; the Neonize bridge then answered 500 to send-mentioned
    ("surrogates not allowed") and the message never left."""

    def test_the_json_body_reaches_the_server_whole(self):
        session = _Session()
        payload = {"phone": ["123@g.us"], "message": "oi \ud83d\udcbb", "mentioned": ["1@lid"]}

        api_request("POST", URL, json=payload, session=session)

        sent = session.calls[0][2]["json"]
        assert sent["message"] == "oi \U0001F4BB"
        assert sent["mentioned"] == ["1@lid"]

    def test_a_body_with_nothing_to_repair_is_sent_as_is(self):
        session = _Session()
        payload = {"message": "plain"}

        api_request("POST", URL, json=payload, session=session)

        assert session.calls[0][2]["json"] is payload
