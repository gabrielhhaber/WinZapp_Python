"""Explicit metadata discovery intersected with a reviewed photo-to-text catalogue."""
from dataclasses import dataclass
import json
import threading

import requests
from urllib3.util import Timeout

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
# image-input/endpoint capability metadata. Reviewed against official model
# pages on 2026-10-03 (sources and maintenance policy in photo-description.md).
# Unknown models remain usable through the explicit advanced model field.
COMPATIBLE_MODELS = {
    "openai": {
        "gpt-4.1-mini": "GPT-4.1 Mini",
        "gpt-4.1-mini-2025-04-14": "GPT-4.1 Mini",
        "gpt-4.1": "GPT-4.1",
        "gpt-4.1-2025-04-14": "GPT-4.1",
        "gpt-4o-mini": "GPT-4o Mini",
        "gpt-4o-mini-2024-07-18": "GPT-4o Mini",
    },
    "gemini": {
        "gemini-3.8-flash": "Gemini 3.8 Flash",
        "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite",
    },
}
MAX_PAGES = 5
MAX_PAGE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024


def compatible_models(provider, body):
    if provider not in COMPATIBLE_MODELS:
        raise DescriptionError("request")
    rows = body.get("data" if provider == "openai" else "models") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise DescriptionError("response")
    found = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = row.get("id") if provider == "openai" else row.get("name")
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


def fetch_models(provider, key, token, session_factory=requests.Session):
    """GET only; bounded pages/body/deadline, auth never in URLs, no retries."""
    if provider not in COMPATIBLE_MODELS:
        raise DescriptionError("request")
    if not key:
        raise DescriptionError("credentials")
    token.check()
    url = ("https://api.openai.com/v1/models" if provider == "openai" else
           "https://generativelanguage.googleapis.com/v1beta/models")
    headers = {"Authorization": f"Bearer {key}"} if provider == "openai" else {"x-goog-api-key": key}
    timer = threading.Timer(max(0, token.deadline - token.clock()), token.expire)
    timer.daemon = True
    timer.start()
    found, seen, page_token, total = {}, set(), None, 0
    try:
        with session_factory() as session:
            for _ in range(MAX_PAGES):
                token.check()
                remaining = token.deadline - token.clock()
                params = {} if provider == "openai" else {"pageSize": 1000}
                if page_token:
                    params["pageToken"] = page_token
                with session.get(url, headers=headers, params=params, allow_redirects=False, stream=True,
                                 timeout=Timeout(total=remaining, connect=min(5, remaining))) as response:
                    token.attach(response)
                    token.check()
                    if response.status_code != 200:
                        raise status_error(response.status_code)
                    data = read_bounded(response, min(MAX_PAGE_BYTES, MAX_TOTAL_BYTES - total), token.check)
                    total += len(data)
                    body = json.loads(data)
                    for option in compatible_models(provider, body):
                        found[option.id] = option
                token.response = None
                token.check()
                page_token = body.get("nextPageToken") if provider == "gemini" else None
                if page_token is None or page_token == "":
                    return tuple(found[identifier] for identifier in COMPATIBLE_MODELS[provider] if identifier in found)
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
