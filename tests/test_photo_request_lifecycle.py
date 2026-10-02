"""Virtual-time generation delays and safe transport diagnosis; no network/UI."""
import json
import logging

import pytest
import requests
from urllib3.util import Timeout

from core.image_description import service
from core.image_description.errors import DescriptionError
from core.image_description.image_input import ImageInput
from tests.test_image_description_core import FakeHTTP, FakeResponse


def _generate(http, token):
    return service.request_answer("gemini", "gemini-3.8-flash", "synthetic-key",
                                  ImageInput(b"synthetic photo", "image/jpeg", 1, 1),
                                  (), "describe", "instructions", "balanced", token,
                                  session_factory=lambda: http)


@pytest.mark.parametrize("delay", [20, 50])
def test_generation_can_use_remaining_budget_instead_of_15_second_cutoff(delay):
    now = [0.0]
    token = service.RequestToken(clock=lambda: now[0])
    answer = json.dumps({"candidates": [{"content": {"parts": [{"text": "A blue rectangle"}]}}]}).encode()
    class DelayedHTTP(FakeHTTP):
        def post(self, url, **kwargs):
            response = super().post(url, **kwargs)
            timeout = kwargs["timeout"]
            if isinstance(timeout, Timeout):
                timeout = timeout.clone()
                timeout.start_connect()  # urllib3 starts this before connect.
                read_seconds = timeout.read_timeout
            else:
                read_seconds = timeout[1]
            now[0] += delay
            if read_seconds < delay:
                raise requests.ReadTimeout("synthetic provider is still generating")
            return response
    http = DelayedHTTP(FakeResponse(data=answer))
    assert _generate(http, token) == "A blue rectangle"
    assert len(http.calls) == 1 and http.response.closed
    assert token.deadline == 60  # No increase to the overall deadline.


def test_preparation_time_is_subtracted_from_transport_budget():
    now = [0.0]
    token = service.RequestToken(clock=lambda: now[0])
    now[0] = 55.0
    http = FakeHTTP(FakeResponse(data=b'{"candidates":[{"content":{"parts":[{"text":"answer"}]}}]}'))
    _generate(http, token)
    timeout = http.calls[0][1]["timeout"]
    assert isinstance(timeout, Timeout)
    assert timeout.total == 5 and timeout.connect_timeout == 5
    timeout.get_connect_duration = lambda: 1.25
    assert timeout.read_timeout == 3.75  # Connect time consumes this same budget.


def test_real_requests_adapter_accepts_total_timeout_without_opening_socket(monkeypatch):
    from io import BytesIO
    from urllib3.response import HTTPResponse
    calls = []
    class PoolStub:
        def urlopen(self, **kwargs):
            calls.append(kwargs)
            timeout = kwargs["timeout"].clone()
            assert timeout.connect_timeout == 8
            timeout.get_connect_duration = lambda: 2
            assert 57 <= timeout.read_timeout <= 58
            return HTTPResponse(body=BytesIO(b'{"candidates":[{"content":{"parts":[{"text":"answer"}]}}]}'),
                                status=200, preload_content=False)
    session = requests.Session()
    session.trust_env = False
    adapter = session.get_adapter("https://")
    monkeypatch.setattr(adapter, "get_connection_with_tls_context", lambda *args, **kwargs: PoolStub())
    monkeypatch.setattr(adapter, "cert_verify", lambda *args, **kwargs: None)
    answer = service.request_answer("gemini", "gemini-3.8-flash", "synthetic-key",
                                    ImageInput(b"synthetic", "image/jpeg", 1, 1), (), "describe",
                                    "instructions", "balanced", service.RequestToken(),
                                    session_factory=lambda: session)
    assert answer == "answer" and len(calls) == 1


@pytest.mark.parametrize("error", [requests.ConnectTimeout, requests.ReadTimeout])
def test_timeout_diagnostics_record_class_without_secret_or_retry(error, caplog):
    http = FakeHTTP(error=error("private-key-or-response"))
    with caplog.at_level(logging.INFO, logger="winzapp.photo_description"):
        with pytest.raises(DescriptionError, match="^ai_error_timeout$"):
            _generate(http, service.RequestToken())
    assert f"exception={error.__name__}" in caplog.text
    assert "private-key-or-response" not in caplog.text
    assert len(http.calls) == 1


def test_generation_after_global_deadline_still_discards_response():
    now = [0.0]
    token = service.RequestToken(clock=lambda: now[0])
    class TooLateHTTP(FakeHTTP):
        def post(self, url, **kwargs):
            response = super().post(url, **kwargs)
            now[0] = 61.0
            return response
    http = TooLateHTTP()
    with pytest.raises(DescriptionError, match="^ai_error_timeout$"):
        _generate(http, token)
    assert len(http.calls) == 1 and http.response.closed


def test_connection_setup_cannot_restart_an_expired_operation_budget():
    now = [0.0]
    token = service.RequestToken(clock=lambda: now[0])
    class LateSession(FakeHTTP):
        def __enter__(self):
            now[0] = 61.0
            return self
    http = LateSession()
    with pytest.raises(DescriptionError, match="^ai_error_timeout$"):
        _generate(http, token)
    assert not http.calls


def test_cancellation_while_waiting_for_headers_discards_returned_response():
    token = service.RequestToken()
    class CancelledHTTP(FakeHTTP):
        def post(self, url, **kwargs):
            response = super().post(url, **kwargs)
            token.cancel()
            return response
    http = CancelledHTTP()
    with pytest.raises(DescriptionError, match="^ai_error_cancelled$"):
        _generate(http, token)
    assert len(http.calls) == 1 and http.response.closed
