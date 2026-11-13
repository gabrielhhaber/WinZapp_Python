"""Explicit metadata discovery intersected with a reviewed photo-to-text catalogue."""
from dataclasses import dataclass
import json
import threading

import requests
from urllib3.util import Timeout

from core import tls_trust
from core.bounded_http import BodyTooLarge, HTTPError, TimeoutError, read_bounded
from .errors import DescriptionError, status_error


@dataclass(frozen=True)
class ModelOption:
    id: str
    name: str

    @property
    def label(self):
        return f"{self.name} ({self.id})"


# Exact IDs, not prefix guesses: the list endpoints do not expose complete
# input-capability metadata. Reviewed against the providers' official model
# pages (sources and maintenance policy in docs/reference/ai-media.md).
# Unknown models remain usable through the explicit advanced model field.
COMPATIBLE_MODELS = {
    "openai": {
        "gpt-4.1-mini": "GPT-4.1 Mini",
        "gpt-4.1-mini-2025-04-14": "GPT-4.1 Mini",
        "gpt-4.1": "GPT-4.1",
        "gpt-4.1-2025-04-14": "GPT-4.1",
        "gpt-4o-mini": "GPT-4o Mini",
        "gpt-4o-mini-2024-07-18": "GPT-4o Mini",
        "gpt-4o": "GPT-4o",
    },
    "gemini": {
        "gemini-3.8-flash": "Gemini 3.8 Flash",
        "gemini-3.6-flash": "Gemini 3.6 Flash",
        "gemini-3.5-flash": "Gemini 3.5 Flash",
        "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite",
    },
    "claude": {
        "claude-sonnet-5": "Claude Sonnet 5",
        "claude-opus-5": "Claude Opus 5",
        "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
    },
    "groq": {
        "qwen/qwen3.8-27b": "Qwen 3.8 27B",
    },
    "openrouter": {
        "google/gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite",
        "google/gemini-3.5-flash": "Gemini 3.5 Flash",
        "anthropic/claude-sonnet-5": "Claude Sonnet 5",
        "qwen/qwen3.8-27b:free": "Qwen 3.8 27B (free)",
    },
    "mistral": {
        # Vision choices only; transcription and OCR have fixed models.
        # Official vision docs + live key metadata checked 2026-10-25.
        "mistral-small-latest": "Mistral Small",
        "mistral-small-2603": "Mistral Small 4",
        "ministral-3b-latest": "Ministral 3 3B",
        "ministral-3b-2512": "Ministral 3 3B",
        "ministral-8b-latest": "Ministral 3 8B",
        "ministral-8b-2512": "Ministral 3 8B",
        "ministral-14b-latest": "Ministral 3 14B",
        "ministral-14b-2512": "Ministral 3 14B",
    },
}
MAX_PAGES = 5
# OpenRouter lists every model it routes to, with descriptions: hundreds of KB.
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024


def compatible_models(provider, body):
    if provider not in COMPATIBLE_MODELS:
        raise DescriptionError("request")
    rows = body.get("models" if provider == "gemini" else "data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise DescriptionError("response")
    found = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = row.get("name" if provider == "gemini" else "id")
        if not isinstance(identifier, str):
            continue
        if provider == "gemini":
            methods = row.get("supportedGenerationMethods")
            if not identifier.startswith("models/") or not isinstance(methods, list) or "generateContent" not in methods:
                continue
            identifier = identifier.removeprefix("models/")
        elif row.get("shutdown_date"):
            continue
        if identifier in COMPATIBLE_MODELS[provider]:
            found.add(identifier)
    return tuple(ModelOption(identifier, name) for identifier, name in COMPATIBLE_MODELS[provider].items()
                 if identifier in found)


def _list_request(provider, key):
    """(url, headers, query) of the provider's model-list endpoint."""
    bearer = {"Authorization": f"Bearer {key}"}
    if provider == "gemini":
        return ("https://generativelanguage.googleapis.com/v1beta/models",
                {"x-goog-api-key": key}, {"pageSize": 1000})
    if provider == "claude":
        return ("https://api.anthropic.com/v1/models",
                {"x-api-key": key, "anthropic-version": "2023-06-01"}, {"limit": 1000})
    if provider == "groq":
        return "https://api.groq.com/openai/v1/models", bearer, {}
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1/models", bearer, {}
    if provider == "mistral":
        return "https://api.mistral.ai/v1/models", bearer, {}
    return "https://api.openai.com/v1/models", bearer, {}


def fetch_models(provider, key, token, session_factory=tls_trust.create_session):
    """GET only; bounded pages/body/deadline, auth never in URLs, no retries."""
    if provider not in COMPATIBLE_MODELS:
        raise DescriptionError("request")
    if not key:
        raise DescriptionError("credentials")
    found = fetch_catalog(_list_request(provider, key),
                          lambda body: compatible_models(provider, body), token,
                          session_factory, pagination="gemini" if provider == "gemini" else "")
    return tuple(found[identifier] for identifier in COMPATIBLE_MODELS[provider]
                 if identifier in found)


def fetch_catalog(request, parse, token, session_factory=tls_trust.create_session, *, pagination=""):
    token.check()
    url, headers, extra = request
    timer = threading.Timer(max(0, token.deadline - token.clock()), token.expire)
    timer.daemon = True
    timer.start()
    found, seen, page_token, total = {}, set(), None, 0
    try:
        with session_factory() as session:
            for _ in range(MAX_PAGES):
                token.check()
                remaining = token.deadline - token.clock()
                params = dict(extra)
                if page_token:
                    params["pageToken" if pagination == "gemini" else "after_id"] = page_token
                with session.get(url, headers=headers, params=params, allow_redirects=False, stream=True,
                                 timeout=Timeout(total=remaining, connect=min(5, remaining))) as response:
                    token.attach(response)
                    token.check()
                    if response.status_code != 200:
                        raise status_error(response.status_code)
                    data = read_bounded(response, min(MAX_PAGE_BYTES, MAX_TOTAL_BYTES - total), token.check)
                    total += len(data)
                    body = json.loads(data)
                    for option in parse(body):
                        found[option.id] = option
                token.response = None
                token.check()
                if pagination == "gemini":
                    page_token = body.get("nextPageToken")
                elif pagination == "anthropic" and body.get("has_more"):
                    page_token = body.get("last_id")
                    if not page_token:
                        raise DescriptionError("response")
                else:
                    page_token = None
                if page_token is None or page_token == "":
                    return found
                if not isinstance(page_token, str) or len(page_token) > 4096 or page_token in seen:
                    raise DescriptionError("response")
                seen.add(page_token)
            raise DescriptionError("response")
    except DescriptionError:
        raise
    except (requests.Timeout, TimeoutError):
        raise DescriptionError("timeout") from None
    except (requests.RequestException, HTTPError):
        token.check()
        raise DescriptionError("network") from None
    except (ValueError, TypeError, BodyTooLarge):
        raise DescriptionError("response") from None
    finally:
        timer.cancel()
        token.response = None
