"""Translation services that are not a language model.

Each is one REST call through the AI media transport (``post_answer``: the
shared deadline, bounded body, no redirects, TLS through tls_trust, errors as
safe ``ai_error_*`` keys), so a failure says what an AI provider's failure
says. The free Google Translate web endpoint is the exception: it is the GET
``core/ai_media/gtranslate.py`` already makes for the AI window, reused as is.

* **LibreTranslate** — open source, self-hosted or a public instance; the key
  is optional (a self-hosted server usually has none). Plain http is accepted
  only for the user's own network (loopback, a private address, a name with
  no dot or ending in ``.local``): that is where a self-hosted server lives,
  and a key typed for it must never cross the internet in clear.
* **DeepL API** — Free and Pro keys; a Free key ends in ``:fx`` and has its own
  host.
* **Google Cloud Translation** (Basic, v2) — an API key with billing.
* **Microsoft Translator** (Azure AI Translator) — a key, and the resource's
  region unless it is a global one.
* **Google Translate** — no account; undocumented and rate limited, see
  gtranslate.py's docstring.

Every service takes ISO codes with a few spellings of its own (``_CODES``).
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote, urlsplit

from core.ai_media.errors import DescriptionError
from core.ai_media.providers import Request
from core.ai_media.service import post_answer

LIBRETRANSLATE = "libretranslate"
DEEPL = "deepl"
GOOGLE_CLOUD = "google_cloud"
MICROSOFT = "microsoft"
GOOGLE_WEB = "google_web"

#: Provider names are not translated (docs/reference/ai-media.md).
NAMES = {
    LIBRETRANSLATE: "LibreTranslate",
    DEEPL: "DeepL",
    GOOGLE_CLOUD: "Google Cloud Translation",
    MICROSOFT: "Microsoft Translator",
    GOOGLE_WEB: "Google Translate",
}
SERVICES = tuple(NAMES)
KEY_REQUIRED = frozenset((DEEPL, GOOGLE_CLOUD, MICROSOFT))
WITH_URL = frozenset((LIBRETRANSLATE,))
WITH_REGION = frozenset((MICROSOFT,))
DEFAULT_URLS = {LIBRETRANSLATE: "http://localhost:5000"}

_CODES = {
    GOOGLE_CLOUD: {"zh": "zh-CN", "nb": "no", "jv": "jw"},
    GOOGLE_WEB: {"zh": "zh-CN", "nb": "no", "jv": "jw"},
    MICROSOFT: {"zh": "zh-Hans", "zh-TW": "zh-Hant", "pt-PT": "pt-pt", "tl": "fil",
                "ku": "kmr", "ckb": "ku", "mn": "mn-Cyrl", "sr": "sr-Cyrl"},
    LIBRETRANSLATE: {"zh": "zh-Hans", "zh-TW": "zh-Hant", "pt-PT": "pt"},
}
_DEEPL_TARGETS = {"en": "EN-US", "pt": "PT-BR", "pt-PT": "PT-PT", "zh": "ZH-HANS",
                  "zh-TW": "ZH-HANT"}
_REGION = re.compile(r"[a-z0-9]{2,40}")


def service_code(service, code, *, target=True):
    """The service's spelling of a language code of ``languages``."""
    if service == DEEPL:
        if target:
            return _DEEPL_TARGETS.get(code, code.upper())
        return code.split("-")[0].upper()
    return _CODES.get(service, {}).get(code, code)


def _local_host(host):
    # An address literal is judged as one first: "2001:4860::8888" has no dot
    # and would pass for a machine name.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return address.is_loopback or address.is_private or address.is_link_local
    if host == "localhost" or host.endswith(".local"):
        return True
    # A single label is a machine on the LAN, unless it is a number in a form
    # the system resolver reads as an internet address ("134744072", "0x08080808").
    return "." not in host and ":" not in host and not (
        host.isdigit() or host.lower().startswith("0x"))


def service_base(value):
    """A LibreTranslate address, normalised; DescriptionError("request") when
    it is not one WinZapp sends text to."""
    value = str(value or "").strip()
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except (AttributeError, ValueError):
        raise DescriptionError("request") from None
    if (not host or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "\\" in value
            or any(char.isspace() or ord(char) < 32 for char in value)
            or not (parts.scheme == "https" or (parts.scheme == "http" and _local_host(host)))):
        raise DescriptionError("request")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return f"{parts.scheme}://{authority}{parts.path.rstrip('/')}"


def valid_region(region):
    return not region or bool(_REGION.fullmatch(region))


def _first(value, *path):
    try:
        for step in path:
            value = value[step]
    except (KeyError, IndexError, TypeError):
        raise DescriptionError("response") from None
    if not isinstance(value, str) or not value.strip():
        raise DescriptionError("response")
    return value


def build_request(service, key, text, target, source=None, base_url="", region=""):
    """(Request, parse) of one translation. ``target``/``source`` are codes of
    ``languages``; ``source`` None lets the service detect it."""
    if service in KEY_REQUIRED and not key:
        raise DescriptionError("credentials")
    to = service_code(service, target)
    origin = service_code(service, source, target=False) if source else None
    if service == LIBRETRANSLATE:
        body = {"q": text, "source": origin or "auto", "target": to, "format": "text"}
        if key:
            body["api_key"] = key
        return (Request(service_base(base_url) + "/translate", {}, body),
                lambda answer: _first(answer, "translatedText"))
    if service == DEEPL:
        host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
        body = {"text": [text], "target_lang": to}
        if origin:
            body["source_lang"] = origin
        return (Request(f"https://{host}/v2/translate",
                        {"Authorization": f"DeepL-Auth-Key {key}"}, body),
                lambda answer: _first(answer, "translations", 0, "text"))
    if service == GOOGLE_CLOUD:
        body = {"q": [text], "target": to, "format": "text"}
        if origin:
            body["source"] = origin
        return (Request("https://translation.googleapis.com/language/translate/v2",
                        {"X-goog-api-key": key}, body),
                lambda answer: _first(answer, "data", "translations", 0, "translatedText"))
    if service == MICROSOFT:
        if not valid_region(region):
            raise DescriptionError("request")
        url = ("https://api.cognitive.microsofttranslator.com/translate?api-version=3.0"
               f"&to={quote(to)}" + (f"&from={quote(origin)}" if origin else ""))
        headers = {"Ocp-Apim-Subscription-Key": key}
        if region:
            headers["Ocp-Apim-Subscription-Region"] = region
        return (Request(url, headers, [{"Text": text}]),
                lambda answer: _first(answer, 0, "translations", 0, "text"))
    raise DescriptionError("request")


def translate(service, key, text, target, token, source=None, base_url="", region=""):
    if service == GOOGLE_WEB:
        from core.ai_media import gtranslate

        translated, _detected = gtranslate.translate_text(
            text, service_code(service, target),
            service_code(service, source, target=False) if source else "auto",
            cancel_check=token.check, any_target=True)
        return translated
    request, parse = build_request(service, key, text, target, source, base_url, region)
    return post_answer(request, parse, token)
