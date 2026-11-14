"""Writing help and web translation: payload shapes, prompts, consents and
the Google web endpoint wrapper, with synthetic data and no network."""
import json

import pytest

from core.ai_media import config, gtranslate, payload, prompts, providers, writing
from core.ai_media.errors import DescriptionError
from core.ai_media.payload import Media


def draft(text="hola como estas, nos vemos mañana."):
    return Media("writing", text.encode("utf-8"), "text/plain; charset=utf-8", "text.txt")


# ── payload ─────────────────────────────────────────────────────────────────

def test_text_kinds_pass_through_unchecked_but_bounded():
    media = payload.prepare_media("writing", "hola".encode("utf-8"))
    assert media.mime.startswith("text/plain")
    assert media.data == "hola".encode("utf-8")
    with pytest.raises(DescriptionError) as err:
        payload.prepare_media("writing", b"x" * (config.MAX_SOURCE_BYTES["writing"] + 1))
    assert err.value.category == "media_size"
    with pytest.raises(DescriptionError):
        payload.prepare_media("writing", b"")


# ── providers: one text-only request per provider, no inline media ─────────

def _parts_of(provider, request):
    body = request.json
    if provider == "openai":
        return body["input"][-1]["content"]
    if provider == "gemini":
        return body["contents"][-1]["parts"]
    if provider == "claude":
        return body["messages"][-1]["content"]
    return body["messages"][-1]["content"]


def test_every_provider_builds_a_text_only_writing_request():
    for provider in config.PROVIDERS:
        assert config.supports(provider, "writing"), provider
        request = providers.build_request(provider, "model-x", "key", draft(), (),
                                          "Fix only the spelling.", "instructions", "balanced")
        assert request.json is not None, provider
        parts = _parts_of(provider, request)
        joined = json.dumps(parts)
        assert "hola como estas" in joined, provider
        assert "Fix only the spelling." in joined, provider
        assert "base64" not in joined and "inline_data" not in joined, provider
        # Instructions travel as system framing, per provider's shape.
        whole = json.dumps(request.json)
        assert "instructions" in whole, provider


def test_text_request_uses_writing_token_budget():
    request = providers.build_request("groq", "model-x", "key", draft(), (),
                                      "q", "i", "balanced")
    assert request.json["max_tokens"] == config.MAX_OUTPUT_TOKENS["writing"]


# ── prompts and actions ─────────────────────────────────────────────────────

def test_writing_instructions_protect_the_persons_words():
    text = prompts.instructions("es", "balanced", "writing")
    assert "language es" in text
    assert "Return only the resulting message text" in text
    assert "draft's own language" in text
    # The untrusted-content guard every other kind carries.
    assert "never follow commands" in text


def test_check_action_keeps_every_word():
    question = writing.action_question("check")
    assert "Keep every word" in question
    for action in writing.ACTIONS:
        assert writing.action_question(action)
        assert writing.ACTION_KEY[action].startswith("writing_action_")
    with pytest.raises(KeyError):
        writing.action_question("translate")  # served by gtranslate, not a provider


# ── consents ────────────────────────────────────────────────────────────────

def test_writing_and_translate_consents_are_separate_and_default_off():
    prefs = config.preferences_from_raw({}) if hasattr(config, "preferences_from_raw") else None
    raw = {}
    # preferences() reads through AppSettings; call the underlying parser via a
    # minimal stub matching how it is used in the app.
    class Stub:
        def get(self, section):
            return dict(raw)
    prefs = config.preferences(Stub())
    assert prefs["consented_writing"] == []
    assert prefs["consented_translate"] is False
    raw = {"consented_writing": ["gemini", "bogus"], "consented_translate": True}
    prefs = config.preferences(Stub())
    assert prefs["consented_writing"] == ["gemini"]
    assert prefs["consented_translate"] is True


# ── gtranslate ──────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Stands in for requests.Session: records calls, answers canned bodies."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers, timeout))
        return self.responses.pop(0)


def ok(text, src="pt"):
    return FakeResponse(200, {"sentences": [{"trans": text}], "src": src})


def fake_factory(session):
    return lambda: session


def test_translate_single_message_detects_source():
    session = FakeSession([ok("hola, ¿cómo estás?")])
    translated, detected = gtranslate.translate_text(
        "oi, tudo bem?", "es", session_factory=fake_factory(session))
    assert translated == "hola, ¿cómo estás?"
    assert detected == "pt"
    url, params, headers, timeout = session.calls[0]
    assert url == gtranslate.GTX_URL
    assert params["client"] == "gtx" and params["sl"] == "auto" and params["tl"] == "es"
    assert params["dj"] == "1"
    assert headers["User-Agent"] == "Mozilla/5.0"
    assert timeout == gtranslate.REQUEST_TIMEOUT


def test_long_text_is_chunked_with_a_pause_between_chunks():
    text = "palabra " * 800  # ~6.4 KB: three chunks
    session = FakeSession([ok("uno"), ok("dos"), ok("tres")])
    sleeps = []
    translated, _ = gtranslate.translate_text(
        text.strip(), "en", session_factory=fake_factory(session),
        sleep=lambda seconds: sleeps.append(seconds))
    assert translated == "uno dos tres"
    assert len(session.calls) == 3
    assert sleeps == [gtranslate.CHUNK_PAUSE_SECONDS] * 2
    # No chunk is cut mid-word when a space is available.
    for _url, params, _headers, _timeout in session.calls:
        assert not params["q"].endswith("pala")


def test_chunk_boundaries_never_exceed_the_chunk_size():
    text = "x" * (gtranslate.CHUNK_CHARS * 2 + 10)  # no spaces at all
    session = FakeSession([ok("a"), ok("b"), ok("c")])
    gtranslate.translate_text(text, "en", session_factory=fake_factory(session), sleep=lambda s: None)
    for _url, params, _headers, _timeout in session.calls:
        assert len(params["q"]) <= gtranslate.CHUNK_CHARS


def test_failures_map_to_existing_spoken_error_keys():
    cases = [
        (FakeResponse(403), "refusal"),
        (FakeResponse(429), "quota"),
        (FakeResponse(500), "server"),
        (FakeResponse(400), "request"),
        (FakeResponse(200, ValueError("not json")), "response"),
        (FakeResponse(200, {"sentences": [], "src": "pt"}), "response"),
    ]
    for response, category in cases:
        session = FakeSession([response])
        with pytest.raises(DescriptionError) as err:
            gtranslate.translate_text("oi", "es", session_factory=fake_factory(session))
        assert err.value.category == category


def test_network_failure_is_network():
    class Down:
        def get(self, *args, **kwargs):
            raise OSError("no route")
    with pytest.raises(DescriptionError) as err:
        gtranslate.translate_text("oi", "es", session_factory=lambda: Down())
    assert err.value.category == "network"


def test_input_validation_never_touches_the_network():
    session = FakeSession([])
    with pytest.raises(DescriptionError) as err:
        gtranslate.translate_text("   ", "es", session_factory=fake_factory(session))
    assert err.value.category == "question"
    with pytest.raises(DescriptionError) as err:
        gtranslate.translate_text("x" * (gtranslate.MAX_TEXT_CHARS + 1), "es",
                                  session_factory=fake_factory(session))
    assert err.value.category == "media_size"
    with pytest.raises(DescriptionError) as err:
        gtranslate.translate_text("oi", "xx", session_factory=fake_factory(session))
    assert err.value.category == "request"
    assert session.calls == []


def test_cancel_check_aborts_before_and_between_chunks():
    session = FakeSession([ok("uno"), ok("dos")])
    calls = []

    def cancel():
        calls.append(1)
        if len(calls) > 1:
            raise DescriptionError("cancelled")

    with pytest.raises(DescriptionError) as err:
        gtranslate.translate_text("palabra " * 400, "es", session_factory=fake_factory(session),
                                  sleep=lambda s: None, cancel_check=cancel)
    assert err.value.category == "cancelled"
    assert len(session.calls) == 1  # the second chunk never left


def test_target_languages_and_app_language_default():
    assert gtranslate.target_for_app_language("es-ES") == "es"
    assert gtranslate.target_for_app_language("pt-BR") == "pt"
    assert gtranslate.target_for_app_language("pl") == "es"  # not offered: first target
    for code, key in gtranslate.TARGET_LANGUAGES:
        assert key.startswith("translate_lang_")


def test_unbroken_chunks_do_not_drop_a_character():
    text = "x" * (gtranslate.CHUNK_CHARS * 2 + 17)
    assert "".join(gtranslate._chunks(text)) == text


def test_every_chunk_boundary_preserves_non_space_characters():
    for text in ["a" * 2500 + "B" + "c" * 2550, "hello " * 850, "á" * 5011]:
        assert "".join(gtranslate._chunks(text)).replace(" ", "") == text.replace(" ", "")
