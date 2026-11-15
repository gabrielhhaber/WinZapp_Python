"""Translate text through the public Google Translate web endpoint.

Chosen over Google Cloud Translation (which requires a billing account) and
over local models: no account, no API key, no billing. The endpoint is the
one the NVDA "Instant Translate" add-on calls; it is undocumented, its
limits are unpublished and it can stop working without notice. So:

* chunks are small and every pause between them is deliberate — burst
  traffic is exactly what an unpublished limit pushes back on;
* a refusal is reported as "try again later", never retried in a loop;
* TLS always verifies (tls_trust), unlike the add-on, which disables
  certificate checks; the add-on's third-party mirror is never used —
  the text would leave Google for an operator we know nothing about.

This module is pure and wx-free: the caller owns threading, cancellation and
the consent shown before anything is sent.
"""

import time

from .. import tls_trust
from .errors import DescriptionError

#: The web endpoint the NVDA add-on "Instant Translate" uses (see its
#: translator.py). client=gtx is the public web client; dj=1 asks for the
#: JSON answer that carries the detected source language.
GTX_URL = "https://translate.googleapis.com/translate_a/single"
_USER_AGENT = "Mozilla/5.0"

#: One request carries at most this much text. The add-on splits at 3000;
#: chat messages are far shorter, so most translations are a single request.
CHUNK_CHARS = 2500
#: Polite pause between chunks of one translation (seconds). Never zero:
#: back-to-back requests are what rate limits answer.
CHUNK_PAUSE_SECONDS = 1.5
#: A draft or a received message longer than this is rejected up front: the
#: pause between chunks would make the wait absurd for a chat feature.
MAX_TEXT_CHARS = 15_000
#: Seconds one request may take.
REQUEST_TIMEOUT = 30

#: Target languages the dialog offers, as (code, i18n key) pairs. Codes are
#: the ones the endpoint accepts; keys name the language in the app's own
#: locale. The list is deliberately short — every entry is a string owed by
#: nine locale catalogs.
TARGET_LANGUAGES = (
    ("es", "translate_lang_es"),
    ("pt", "translate_lang_pt"),
    ("en", "translate_lang_en"),
    ("it", "translate_lang_it"),
    ("fr", "translate_lang_fr"),
    ("de", "translate_lang_de"),
)


def target_for_app_language(app_language):
    """The target code matching the app's language ("es-ES" -> "es"), or the
    first offered target when the app speaks something else."""
    short = str(app_language or "").split("-")[0].lower()
    codes = [code for code, _key in TARGET_LANGUAGES]
    return short if short in codes else codes[0]


def _chunks(text):
    """Split on whitespace near CHUNK_CHARS so a sentence is never cut mid-
    word when it can be helped."""
    start = 0
    while start < len(text):
        end = start + CHUNK_CHARS
        if end >= len(text):
            yield text[start:]
            return
        cut = text.rfind(" ", start, end)
        if cut <= start:
            cut = end
        yield text[start:cut]
        start = cut + 1 if text[cut:cut + 1] == " " else cut


def _translate_chunk(session, text, target, source):
    """One request. Returns (translated, detected_source)."""
    try:
        response = session.get(
            GTX_URL,
            params={"client": "gtx", "sl": source, "tl": target, "dt": "t",
                    "dj": "1", "q": text},
            headers={"User-Agent": _USER_AGENT},
            timeout=REQUEST_TIMEOUT)
    except Exception:
        # requests raises a family of network exceptions; from the person's
        # chair they are all "could not reach the service".
        raise DescriptionError("network") from None
    if response.status_code != 200:
        # 403/429 are the shapes an unpublished limit takes; 5xx is Google's
        # side. Both map to keys the app already speaks.
        if response.status_code == 403:
            raise DescriptionError("refusal")
        if response.status_code == 429:
            raise DescriptionError("quota")
        if response.status_code >= 500:
            raise DescriptionError("server")
        raise DescriptionError("request")
    try:
        body = response.json()
        translated = "".join(sentence.get("trans", "")
                             for sentence in body.get("sentences") or ())
        detected = str(body.get("src") or source)
    except (ValueError, AttributeError, TypeError):
        raise DescriptionError("response") from None
    if not translated:
        raise DescriptionError("response")
    return translated, detected


def translate_text(text, target, source="auto", session_factory=tls_trust.create_session,
                   sleep=time.sleep, cancel_check=None):
    """Translate ``text`` into ``target``. Returns (translated, detected).

    ``cancel_check`` is a callable that raises to abort (the AI window's
    operation token); with it omitted the translation cannot be cancelled.
    Raises DescriptionError with an existing ai_error_* category on any
    failure — safe keys only, never response bodies or the person's text.
    """
    text = (text or "").strip()
    if not text:
        raise DescriptionError("question")
    if len(text) > MAX_TEXT_CHARS:
        raise DescriptionError("media_size")
    targets = [code for code, _key in TARGET_LANGUAGES]
    if target not in targets:
        raise DescriptionError("request")
    session = session_factory()
    parts = []
    detected = source
    chunks = list(_chunks(text))
    for index, chunk in enumerate(chunks):
        if cancel_check is not None:
            cancel_check()
        if index:
            sleep(CHUNK_PAUSE_SECONDS)
            if cancel_check is not None:
                cancel_check()
        piece, detected = _translate_chunk(session, chunk, target, source)
        parts.append(piece)
    return " ".join(parts), detected
