"""The address of a language model server the person runs (Ollama, LM Studio).

Pure functions, no network. Ollama and LM Studio answer on this computer and
speak the OpenAI chat protocol (``/chat/completions`` and ``/models`` under a
base such as ``http://localhost:11434/v1``). The address is typed by a person
and later used to send private pictures and messages, so it is read strictly:
plain ``http`` only for this computer, ``https`` for anything else, no user
name or password, no query. An address that is not this computer is a remote
server: callers treat it like any online provider (``config.sends_out``).
"""
import ipaddress
from urllib.parse import urlsplit

from .errors import DescriptionError

MAX_URL_CHARS = 200


def is_loopback_host(host):
    """Whether ``host`` is this computer: ``localhost`` or a loopback address."""
    if not isinstance(host, str) or not host:
        return False
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_url(value):
    """The base URL ``value`` stands for, without a trailing slash and ``/v1``
    when it names no path. ``DescriptionError("local_url")`` for anything else."""
    if not isinstance(value, str):
        raise DescriptionError("local_url")
    text = value.strip()
    try:
        parts = urlsplit(text)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise DescriptionError("local_url") from None
    local = is_loopback_host(host)
    path = parts.path.rstrip("/")
    if (not host or not text or len(text) > MAX_URL_CHARS or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment
            or "\\" in text or any(c.isspace() or ord(c) < 32 for c in text)
            or ".." in path or "%" in path or ";" in path
            or not (parts.scheme == "https" or (local and parts.scheme == "http"))):
        raise DescriptionError("local_url")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return f"{parts.scheme}://{authority}{path or '/v1'}"


def valid_url(value):
    """``normalize_url`` or "" when the address is unusable."""
    try:
        return normalize_url(value)
    except DescriptionError:
        return ""


def is_loopback_url(value):
    """Whether a valid ``value`` points at this computer."""
    url = valid_url(value)
    return bool(url) and is_loopback_host(urlsplit(url).hostname)
