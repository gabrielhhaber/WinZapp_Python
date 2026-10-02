"""Model discovery is bounded metadata-only HTTP, never a photo or generation probe."""
import json

import pytest
import requests

from core.image_description.errors import DescriptionError
from core.image_description.service import RequestToken
from tests.test_image_description_core import FakeHTTP, FakeResponse


@pytest.fixture
def catalog():
    from core.image_description import model_catalog
    return model_catalog


def response(body, status=200):
    return FakeResponse(status=status, data=json.dumps(body).encode())


@pytest.mark.parametrize("provider,body,expected", [
    ("openai", {"data": [{"id": "gpt-4.1-mini"}, {"id": "tts-1"},
                         {"id": "gpt-image-1"}, {"id": "future-vision"},
                         {"id": "gpt-4.1-mini"}, {"id": "../gpt-4.1"},
                         {"id": "gpt-4.1", "shutdown_date": "2026-01-01"}]},
     ["gpt-4.1-mini"]),
    ("gemini", {"models": [
        {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-3.8-flash-tts", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/future-vision", "supportedGenerationMethods": ["generateContent"]},
    ]}, ["gemini-3.8-flash"]),
])
def test_only_known_photo_to_text_models_are_listed(catalog, provider, body, expected):
    assert [m.id for m in catalog.compatible_models(provider, body)] == expected


def test_catalog_has_real_alternatives_and_uses_trusted_names(catalog):
    for provider, rows in catalog.COMPATIBLE_MODELS.items():
        assert len(rows) >= 2
        body = ({"data": [{"id": key, "displayName": "untrusted text"} for key in rows]}
                if provider == "openai" else {"models": [
                    {"name": "models/" + key, "displayName": "untrusted text",
                     "supportedGenerationMethods": ["generateContent"]} for key in rows]})
        result = catalog.compatible_models(provider, body)
        assert set(m.id for m in result) == set(rows)
        assert all("untrusted" not in m.label for m in result)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_listing_has_auth_headers_no_photo_no_generation_or_redirects(catalog, provider):
    body = {"data": []} if provider == "openai" else {"models": []}
    http = FakeHTTP(response(body))
    assert catalog.fetch_models(provider, "synthetic-secret", RequestToken(seconds=20),
                                session_factory=lambda: http) == ()
    url, kwargs = http.calls[0]
    assert url.endswith("/models") and "synthetic-secret" not in url
    assert "json" not in kwargs and kwargs["allow_redirects"] is False
    assert kwargs["stream"] is True
    auth = kwargs["headers"]
    assert auth == ({"Authorization": "Bearer synthetic-secret"} if provider == "openai"
                    else {"x-goog-api-key": "synthetic-secret"})
    assert kwargs["timeout"].total <= 20 and http.response.closed


def test_gemini_pagination_keeps_auth_on_the_fixed_endpoint_and_deduplicates(catalog):
    bodies = [
        {"models": [{"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]}],
         "nextPageToken": "opaque-token"},
        {"models": [{"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]}]},
    ]
    class Pages(FakeHTTP):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return response(bodies[len(self.calls) - 1])
    http = Pages()
    result = catalog.fetch_models("gemini", "synthetic", RequestToken(seconds=20), session_factory=lambda: http)
    assert {m.id for m in result} == {"gemini-3.8-flash", "gemini-3.5-flash-lite"}
    assert len(http.calls) == 2 and http.calls[0][0] == http.calls[1][0]
    assert http.calls[1][1]["params"]["pageToken"] == "opaque-token"


@pytest.mark.parametrize("status,category", [(401, "authentication"), (403, "authentication"),
                                            (429, "quota"), (503, "server"), (302, "request")])
def test_listing_http_errors_are_safe_and_not_retried(catalog, status, category):
    http = FakeHTTP(response({"private": "secret"}, status))
    with pytest.raises(DescriptionError, match=f"^ai_error_{category}$"):
        catalog.fetch_models("openai", "synthetic", RequestToken(seconds=20), session_factory=lambda: http)
    assert len(http.calls) == 1 and http.response.closed


@pytest.mark.parametrize("error,category", [(requests.Timeout("private"), "timeout"),
                                           (requests.ConnectionError("private"), "network")])
def test_listing_network_errors_do_not_expose_provider_messages(catalog, error, category):
    http = FakeHTTP(error=error)
    with pytest.raises(DescriptionError, match=f"^ai_error_{category}$"):
        catalog.fetch_models("openai", "synthetic", RequestToken(seconds=20), session_factory=lambda: http)
    assert len(http.calls) == 1


@pytest.mark.parametrize("body", [{}, {"data": None}, {"data": "not a list"}, []])
def test_malformed_catalog_is_not_presented_as_an_empty_success(catalog, body):
    with pytest.raises(DescriptionError, match="^ai_error_response$"):
        catalog.compatible_models("openai", body)


def test_oversized_list_and_repeated_page_token_are_bounded(catalog):
    for body in (b"x" * (256 * 1024 + 1),
                 json.dumps({"models": [], "nextPageToken": "repeated"}).encode()):
        http = FakeHTTP(FakeResponse(data=body))
        with pytest.raises(DescriptionError, match="^ai_error_response$"):
            catalog.fetch_models("gemini", "synthetic", RequestToken(seconds=20), session_factory=lambda: http)
        assert len(http.calls) <= 2


@pytest.mark.parametrize("invalid", ["cancelled", "expired", "no_key", "provider"])
def test_invalid_listing_sends_nothing(catalog, invalid):
    token = RequestToken(seconds=20, clock=lambda: 0)
    if invalid == "cancelled":
        token.cancel()
    if invalid == "expired":
        token.clock = lambda: 21
    http = FakeHTTP()
    with pytest.raises(DescriptionError):
        catalog.fetch_models("invalid" if invalid == "provider" else "openai",
                             "" if invalid == "no_key" else "synthetic", token, session_factory=lambda: http)
    assert not http.calls


def test_late_metadata_is_discarded_and_response_closed(catalog):
    token = RequestToken(seconds=20, clock=lambda: 0)
    class Late(FakeResponse):
        def read1(self, *args, **kwargs):
            token.clock = lambda: 21
            return super().read1(*args, **kwargs)
    http = FakeHTTP(Late(data=b'{"data": []}'))
    with pytest.raises(DescriptionError, match="^ai_error_timeout$"):
        catalog.fetch_models("openai", "synthetic", token, session_factory=lambda: http)
    assert http.response.closed and token.response is None


def test_more_than_five_pages_is_an_error_not_a_partial_success(catalog):
    class Pages(FakeHTTP):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return response({"models": [], "nextPageToken": str(len(self.calls))})
    http = Pages()
    with pytest.raises(DescriptionError, match="^ai_error_response$"):
        catalog.fetch_models("gemini", "synthetic", RequestToken(seconds=20), session_factory=lambda: http)
    assert len(http.calls) == 5


@pytest.mark.parametrize("page_token", [False, 12, {}, "x" * 4097])
def test_invalid_page_token_never_becomes_a_second_request(catalog, page_token):
    http = FakeHTTP(response({"models": [], "nextPageToken": page_token}))
    with pytest.raises(DescriptionError, match="^ai_error_response$"):
        catalog.fetch_models("gemini", "synthetic", RequestToken(seconds=20), session_factory=lambda: http)
    assert len(http.calls) == 1


def test_combined_response_budget_is_bounded_across_pages(catalog):
    class Pages(FakeHTTP):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return response({"models": [], "padding": "x" * 230_000,
                             "nextPageToken": str(len(self.calls))})
    http = Pages()
    with pytest.raises(DescriptionError, match="^ai_error_response$"):
        catalog.fetch_models("gemini", "synthetic", RequestToken(seconds=20), session_factory=lambda: http)
    assert len(http.calls) == 5
