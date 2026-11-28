"""The models a local server (Ollama, LM Studio) has installed.

``GET {base}/models`` answers ``{"data": [{"id": "..."}]}`` on both servers.
Unlike the online providers there is no reviewed list to intersect it with:
what is installed is what the person chose to install, and only they know
which ones read pictures, so every listed id is offered and the choice stays
theirs. Bounded, no redirect, no credential; the request to this computer
never goes through a proxy.
"""
from core import tls_trust

from .config import valid_model
from .errors import DescriptionError
from .local_url import is_loopback_url, normalize_url
from .model_catalog import ModelOption, fetch_catalog

#: A server with more than this many models is not a list a person can pick from.
MAX_MODELS = 500


def installed_models(body):
    """The ``ModelOption`` of every usable id in a ``/models`` answer."""
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise DescriptionError("response")
    for row in rows[:MAX_MODELS]:
        identifier = row.get("id") if isinstance(row, dict) else None
        if isinstance(identifier, str) and valid_model(identifier):
            yield ModelOption(identifier, identifier)


def _session():
    session = tls_trust.create_session()
    session.trust_env = False
    return session


def fetch_models(base_url, token, session_factory=None):
    """The installed models of the server at ``base_url``, sorted by id.
    ``DescriptionError("local_url")`` for an unusable address."""
    base = normalize_url(base_url)
    if session_factory is None:
        session_factory = _session if is_loopback_url(base) else tls_trust.create_session
    found = fetch_catalog((f"{base}/models", {}, {}), installed_models, token, session_factory)
    return tuple(sorted(found.values(), key=lambda option: option.id))
