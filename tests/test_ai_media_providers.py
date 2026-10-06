"""Every provider's request and answer shape, the fallback chain and media validation.

Pure payload tests: nothing here reaches the network or a window.
"""
import base64
import json

import pytest

from core.ai_media import config, service
from core.ai_media.errors import DescriptionError
from core.ai_media.payload import Media, prepare_media
from core.ai_media.prompts import first_question, instructions
from core.ai_media.providers import build_request, is_transcription, parse_answer
from tests.test_ai_media_core import FakeHTTP, FakeResponse

DATA = b"synthetic-bytes"
ENCODED = base64.b64encode(DATA).decode()

IMAGE = Media("image", DATA, "image/jpeg", "image.jpg")
PDF = Media("pdf", DATA, "application/pdf", "document.pdf")
VIDEO = Media("video", DATA, "video/mp4", "video.mp4")
AUDIO = Media("audio", DATA, "audio/ogg", "audio.ogg")


def build(provider, media, history=(), question="Read the sign"):
    return build_request(provider, config.PROVIDERS[provider].model, "private-key", media, history,
                         question, instructions("pt-BR", "balanced", media.kind), "balanced")


def serialized(request):
    return json.dumps(request.json)


@pytest.mark.parametrize("provider", ["openai", "gemini", "claude", "groq", "openrouter"])
def test_every_provider_resends_the_picture_inline_each_turn_and_keeps_the_key_in_headers(provider):
    request = build(provider, IMAGE, (("user", "describe"), ("assistant", "red car")))
    assert "private-key" not in request.url and "private-key" not in serialized(request)
    assert "private-key" in json.dumps(request.headers)
    assert ENCODED in serialized(request) and "Read the sign" in serialized(request)
    assert "red car" in serialized(request)  # earlier turns travel as text
    assert request.url.startswith("https://") and request.data is None and request.files is None


def test_openai_payload_is_stateless():
    request = build("openai", IMAGE)
    assert request.json["store"] is False and "previous_response_id" not in request.json
    assert "tools" not in request.json
    assert request.headers == {"Authorization": "Bearer private-key"}
    assert request.url == "https://api.openai.com/v1/responses"


def test_gemini_payload_keeps_the_key_out_of_the_url_and_marks_model_turns():
    request = build("gemini", IMAGE, (("user", "describe"), ("assistant", "red car")))
    assert request.headers == {"x-goog-api-key": "private-key"}
    assert request.json["contents"][1]["role"] == "model"
    assert request.url.endswith(f"{config.PROVIDERS['gemini'].model}:generateContent")


def test_claude_payload_uses_the_messages_api_with_base64_blocks():
    request = build("claude", IMAGE)
    assert request.url == "https://api.anthropic.com/v1/messages"
    assert request.headers == {"x-api-key": "private-key", "anthropic-version": "2023-06-01"}
    block = request.json["messages"][-1]["content"][0]
    assert block == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": ENCODED}}
    assert request.json["system"] and request.json["max_tokens"] == config.MAX_OUTPUT_TOKENS["image"]
    pdf = build("claude", PDF).json["messages"][-1]["content"][0]
    assert pdf["type"] == "document" and pdf["source"]["media_type"] == "application/pdf"


@pytest.mark.parametrize("provider,base", [("groq", "https://api.groq.com/openai/v1"),
                                           ("openrouter", "https://openrouter.ai/api/v1")])
def test_compatible_providers_use_chat_completions_with_a_system_prompt(provider, base):
    request = build(provider, IMAGE)
    assert request.url == f"{base}/chat/completions"
    assert request.json["messages"][0]["role"] == "system"
    part = request.json["messages"][-1]["content"][1]
    assert part["image_url"]["url"] == f"data:image/jpeg;base64,{ENCODED}"


@pytest.mark.parametrize("provider,part", [("openai", "input_file"), ("openrouter", "file")])
def test_pdf_goes_as_a_data_url_file_part(provider, part):
    request = build(provider, PDF)
    found = [p for p in json.loads(serialized(request).replace("\\\\", "\\"))["input" if provider == "openai"
             else "messages"][-1]["content"] if p.get("type") == part]
    assert found and ENCODED in json.dumps(found[0])


def test_gemini_takes_video_audio_and_pdf_inline():
    for media in (VIDEO, AUDIO, PDF):
        part = build("gemini", media).json["contents"][-1]["parts"][0]["inline_data"]
        assert part == {"mime_type": media.mime, "data": ENCODED}


@pytest.mark.parametrize("provider,base,model", [
    ("openai", "https://api.openai.com/v1", "gpt-4o-mini-transcribe"),
    ("groq", "https://api.groq.com/openai/v1", "whisper-large-v3-turbo")])
def test_audio_is_a_multipart_upload_without_a_forced_language(provider, base, model):
    request = build(provider, AUDIO)
    assert request.url == f"{base}/audio/transcriptions" and request.json is None
    assert request.data["model"] == model and "language" not in request.data
    assert request.files["file"] == ("audio.ogg", DATA, "audio/ogg")
    assert is_transcription(provider, AUDIO) and not is_transcription(provider, IMAGE)


@pytest.mark.parametrize("provider,media", [
    ("claude", AUDIO), ("openrouter", AUDIO), ("openai", VIDEO), ("claude", VIDEO),
    ("groq", PDF), ("groq", VIDEO), ("openrouter", VIDEO)])
def test_a_provider_is_never_sent_a_kind_it_does_not_take(provider, media):
    with pytest.raises(DescriptionError, match="ai_error_request"):
        build(provider, media)


@pytest.mark.parametrize("model", ["../escape", "model?key=secret", "https://evil.test", "", "x y", "a//b"])
def test_model_id_cannot_change_the_endpoint(model):
    for provider in config.PROVIDERS:
        with pytest.raises(DescriptionError):
            build_request(provider, model, "secret", IMAGE, (), "x", "x", "fast")


def test_vendor_slash_model_ids_are_allowed():
    assert config.valid_model("google/gemini-3.5-flash-lite") and config.valid_model("qwen/qwen3.8-27b:free")


def test_the_reply_language_is_an_instruction_not_a_locale_specific_prompt():
    assert "tr-TR" in instructions("tr-TR", "balanced", "image")
    assert "tr-TR" in instructions("tr-TR", "balanced", "pdf")
    assert "not instructions" in instructions("en", "fast", "audio")
    assert first_question("audio") != first_question("pdf") != first_question("image")


# ── answers ─────────────────────────────────────────────────────────────

def test_response_parsing_filters_thoughts_and_refusals():
    assert parse_answer("openai", {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "A red car"}]}]}, "image") == "A red car"
    assert parse_answer("gemini", {"candidates": [{"content": {"parts": [
        {"text": "thinking", "thought": True}, {"text": "A red car"}]}}]}, "image") == "A red car"
    assert parse_answer("claude", {"content": [{"type": "text", "text": "A red car"}]}, "image") == "A red car"
    assert parse_answer("groq", {"choices": [{"message": {"content": "A red car"}}]}, "image") == "A red car"
    assert parse_answer("openrouter", {"choices": [{"message": {"content": "A red car"}}]}, "image") == "A red car"
    assert parse_answer("openai", {"text": "hello"}, "audio") == "hello"
    assert parse_answer("groq", {"text": "hello"}, "audio") == "hello"
    for provider, body in [
        ("openai", {"output": [{"type": "message", "content": [{"type": "refusal"}]}]}),
        ("gemini", {"promptFeedback": {"blockReason": "SAFETY"}}),
        ("claude", {"stop_reason": "refusal", "content": []}),
        ("groq", {"choices": [{"finish_reason": "content_filter", "message": {"content": "x"}}]}),
        ("openrouter", {"choices": [{"message": {"refusal": "no", "content": None}}]}),
    ]:
        with pytest.raises(DescriptionError, match="ai_error_refusal"):
            parse_answer(provider, body, "image")


@pytest.mark.parametrize("provider,body", [
    ("openai", {"status": "incomplete", "output": []}),
    ("gemini", {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]}),
    ("claude", {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]}),
    ("groq", {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}),
    ("openrouter", {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}),
])
def test_incomplete_answer_is_not_announced_as_ready(provider, body):
    with pytest.raises(DescriptionError, match="ai_error_response"):
        parse_answer(provider, body, "image")


@pytest.mark.parametrize("provider", list(config.PROVIDERS))
@pytest.mark.parametrize("body", [None, {}, {"output": None}, {"output": [3]}, {"choices": []},
                                  {"content": None}, {"candidates": [3]}])
def test_malformed_response_is_a_safe_error(provider, body):
    with pytest.raises(DescriptionError, match="ai_error_response"):
        parse_answer(provider, body, "image")


def test_long_documents_are_not_cut_like_descriptions():
    long_text = "x" * 20_000
    body = {"choices": [{"message": {"content": long_text}}]}
    assert len(parse_answer("groq", body, "image")) == config.MAX_OUTPUT_CHARS["image"]
    assert len(parse_answer("groq", body, "pdf")) == 20_000
    assert len(parse_answer("groq", body, "audio")) == 20_000


def _text_response(provider, kind, text):
    if kind == "audio" and provider in ("openai", "groq"):
        return {"text": text}
    if provider == "openai":
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": text}]}]}
    if provider == "gemini":
        return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]}
    if provider == "claude":
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}
    return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}


@pytest.mark.parametrize("provider,kind", [
    ("openai", "audio"), ("groq", "audio"), ("gemini", "audio"),
    ("openai", "pdf"), ("gemini", "pdf"), ("claude", "pdf"), ("openrouter", "pdf")])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_transcript_and_pdf_text_limits_never_return_silent_partial_success(provider, kind, offset):
    text = "x" * (config.MAX_OUTPUT_CHARS[kind] + offset)
    body = _text_response(provider, kind, "\n " + text + " \n")
    if offset > 0:
        with pytest.raises(DescriptionError, match="ai_error_output_limit"):
            parse_answer(provider, body, kind)
    else:
        assert parse_answer(provider, body, kind) == text


# ── media validation ─────────────────────────────────────────────────────

def test_pdf_must_look_like_a_pdf_and_is_sent_untouched():
    media = prepare_media("pdf", b"%PDF-1.7 body")
    assert media == Media("pdf", b"%PDF-1.7 body", "application/pdf", "document.pdf")
    with pytest.raises(DescriptionError, match="ai_error_media_format"):
        prepare_media("pdf", b"MZ not a pdf")


@pytest.mark.parametrize("mime,filename", [("audio/ogg; codecs=opus", "audio.ogg"), ("audio/mpeg", "audio.mp3"),
                                           ("audio/mp4", "audio.m4a"), ("AUDIO/X-WAV", "audio.wav")])
def test_audio_container_is_taken_from_the_reported_type(mime, filename):
    assert prepare_media("audio", DATA, mime).filename == filename


@pytest.mark.parametrize("kind,mime", [("audio", "application/octet-stream"), ("audio", ""),
                                       ("video", "audio/ogg"), ("video", "application/x-msdownload")])
def test_unknown_containers_are_refused_before_sending(kind, mime):
    with pytest.raises(DescriptionError, match="ai_error_media_format"):
        prepare_media(kind, DATA, mime)


def test_size_limits_apply_per_kind_and_empty_media_is_refused(monkeypatch):
    with pytest.raises(DescriptionError, match="ai_error_media_size"):
        prepare_media("audio", b"", "audio/ogg")
    monkeypatch.setattr(config, "MAX_SOURCE_BYTES", {"audio": 3})
    from core.ai_media import payload
    monkeypatch.setattr(payload, "MAX_SOURCE_BYTES", {"audio": 3})
    with pytest.raises(DescriptionError, match="ai_error_media_size"):
        prepare_media("audio", b"1234", "audio/ogg")


def test_inline_limits_leave_room_for_base64_inside_geminis_20_mb_request():
    for kind in ("audio", "pdf", "video"):
        assert config.MAX_SOURCE_BYTES[kind] * 4 // 3 < 20 * 1000 * 1000


# ── the chain ────────────────────────────────────────────────────────────

def _answer(provider):
    body = {"openai": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "from openai"}]}]},
            "gemini": {"candidates": [{"content": {"parts": [{"text": "from gemini"}]}}]}}
    return json.dumps(body[provider]).encode()


class RoutedHTTP(FakeHTTP):
    """Answers per URL so one chain can mix failures and a success."""

    def __init__(self, routes):
        super().__init__()
        self.routes = routes

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = next(v for host, v in self.routes.items() if host in url)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(status=outcome[0], data=outcome[1])

    get = post


def chain(http, providers, *, operation=None, key_for=lambda p: "key-" + p, kind="image", media=IMAGE):
    operation = operation or service.Operation(kind)
    models = {p: config.PROVIDERS[p].model for p in config.PROVIDERS}
    return service.run_chain(operation, providers, models, key_for, media, (), "q", "i", "balanced",
                             session_factory=lambda: http)


def test_a_failing_provider_falls_back_to_the_next_and_names_who_answered():
    http = RoutedHTTP({"openai.com": (429, b"quota"), "googleapis": (200, _answer("gemini"))})
    assert chain(http, ["openai", "gemini"]) == ("from gemini", "gemini")
    assert [url.split("/")[2] for url, _ in http.calls] == ["api.openai.com", "generativelanguage.googleapis.com"]
    assert http.calls[0][1]["headers"] == {"Authorization": "Bearer key-openai"}
    assert http.calls[1][1]["headers"] == {"x-goog-api-key": "key-gemini"}  # each provider gets only its own key


def test_the_first_answer_ends_the_chain_so_media_goes_to_one_provider_only():
    http = RoutedHTTP({"openai.com": (200, _answer("openai")), "googleapis": (200, _answer("gemini"))})
    assert chain(http, ["openai", "gemini"]) == ("from openai", "openai")
    assert len(http.calls) == 1


def test_when_everyone_fails_the_error_says_who_failed_how():
    http = RoutedHTTP({"openai.com": (401, b""), "googleapis": (503, b"")})
    with pytest.raises(service.ChainFailed) as raised:
        chain(http, ["openai", "gemini"])
    assert raised.value.attempts == (("openai", "authentication"), ("gemini", "server"))
    assert raised.value.category == "server" and str(raised.value) == "ai_error_server"


def test_an_empty_chain_and_missing_keys_are_a_clear_error_not_a_crash():
    with pytest.raises(service.ChainFailed) as none_configured:
        chain(RoutedHTTP({}), [])
    assert none_configured.value.category == "providers" and not none_configured.value.attempts
    with pytest.raises(service.ChainFailed) as no_key:
        chain(RoutedHTTP({}), ["openai"], key_for=lambda p: "")
    assert no_key.value.attempts == (("openai", "credentials"),)


def test_an_unreadable_credential_store_counts_as_a_failed_attempt():
    from core.ai_credentials import CredentialError

    def broken(provider):
        raise CredentialError()
    with pytest.raises(service.ChainFailed) as raised:
        chain(RoutedHTTP({}), ["openai"], key_for=broken)
    assert raised.value.attempts == (("openai", "credentials"),)


def test_cancelling_stops_the_chain_instead_of_trying_the_next_provider():
    operation = service.Operation("image")
    operation.cancel()
    http = RoutedHTTP({"openai.com": (200, _answer("openai"))})
    with pytest.raises(DescriptionError, match="ai_error_cancelled"):
        chain(http, ["openai", "gemini"], operation=operation)
    assert not http.calls


def test_a_cancel_during_an_attempt_does_not_start_another():
    import requests
    operation = service.Operation("image")

    class CancelsWhileSending(RoutedHTTP):
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            operation.cancel()
            raise requests.ConnectionError("dropped")
    http = CancelsWhileSending({})
    with pytest.raises(DescriptionError, match="ai_error_cancelled"):
        chain(http, ["openai", "gemini"], operation=operation)
    assert len(http.calls) == 1


def test_one_slow_provider_times_out_alone_and_the_next_still_gets_a_full_attempt():
    now = [0.0]
    operation = service.Operation("image", clock=lambda: now[0])
    first = operation.attempt()
    now[0] += operation.attempt_seconds + 1  # the first provider ran out of its attempt
    with pytest.raises(DescriptionError, match="ai_error_timeout"):
        first.check()
    operation.check()  # the action itself still has budget
    second = operation.attempt()
    second.check()
    assert second.deadline - now[0] == pytest.approx(operation.attempt_seconds)


def test_the_overall_budget_ends_the_chain_even_when_attempts_keep_failing():
    now = [0.0]
    operation = service.Operation("image", clock=lambda: now[0])
    now[0] = operation.total_seconds + 1
    with pytest.raises(DescriptionError, match="ai_error_timeout"):
        operation.attempt()


def test_longer_kinds_get_longer_attempts_and_budgets():
    assert service.Operation("pdf").attempt_seconds > service.Operation("image").attempt_seconds
    assert service.Operation("pdf").total_seconds > service.Operation("pdf").attempt_seconds


def test_bad_media_is_not_retried_on_another_provider():
    class BadMedia(RoutedHTTP):
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            raise DescriptionError("media_format")
    http = BadMedia({})
    with pytest.raises(DescriptionError, match="ai_error_media_format"):
        chain(http, ["openai", "gemini"])
    assert len(http.calls) == 1


@pytest.mark.parametrize("kind,media", [("audio", AUDIO), ("pdf", PDF)])
def test_output_limit_does_not_send_media_to_another_provider(kind, media):
    text = "x" * (config.MAX_OUTPUT_CHARS[kind] + 1)
    http = RoutedHTTP({
        "openai.com": (200, json.dumps(_text_response("openai", kind, text)).encode()),
        "googleapis": (200, json.dumps(_text_response("gemini", kind, "short answer")).encode()),
    })
    with pytest.raises(DescriptionError, match="ai_error_output_limit"):
        chain(http, ["openai", "gemini"], kind=kind, media=media)
    assert len(http.calls) == 1


@pytest.mark.parametrize("kind,media", [("audio", AUDIO), ("pdf", PDF)])
@pytest.mark.parametrize("failure", [401, 403, 429, 500, 400, "network", "timeout"])
def test_provider_failures_still_use_the_next_configured_provider(kind, media, failure):
    import requests
    outcome = (failure, b"") if isinstance(failure, int) else {
        "network": requests.ConnectionError("synthetic network failure"),
        "timeout": requests.ReadTimeout("synthetic attempt timeout"),
    }[failure]
    http = RoutedHTTP({
        "openai.com": outcome,
        "googleapis": (200, json.dumps(_text_response("gemini", kind, "complete text")).encode()),
    })
    assert chain(http, ["openai", "gemini"], kind=kind, media=media) == ("complete text", "gemini")
    assert [url.split("/")[2] for url, _ in http.calls] == [
        "api.openai.com", "generativelanguage.googleapis.com"]


@pytest.mark.parametrize("provider,kind,body", [
    ("openai", "pdf", {"status": "incomplete", "output": []}),
    ("gemini", "audio", {"candidates": [{"finishReason": "MAX_TOKENS",
        "content": {"parts": [{"text": "partial"}]}}]}),
    ("gemini", "pdf", {"candidates": [{"finishReason": "MAX_TOKENS",
        "content": {"parts": [{"text": "partial"}]}}]}),
    ("claude", "pdf", {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "partial"}]}),
    ("openrouter", "pdf", {"choices": [{"finish_reason": "length",
        "message": {"content": "partial"}}]}),
])
def test_provider_output_cutoff_can_fall_back_without_publishing_partial_text(provider, kind, body):
    backup = "gemini" if provider != "gemini" else "openai"
    first_host = build(provider, AUDIO if kind == "audio" else PDF).url.split("/")[2]
    next_host = build(backup, AUDIO if kind == "audio" else PDF).url.split("/")[2]
    http = RoutedHTTP({
        first_host: (200, json.dumps(body).encode()),
        next_host: (200, json.dumps(_text_response(backup, kind, "complete text")).encode()),
    })
    assert chain(http, [provider, backup], kind=kind, media=AUDIO if kind == "audio" else PDF) == (
        "complete text", backup)
    assert [url.split("/")[2] for url, _ in http.calls] == [first_host, next_host]


@pytest.mark.parametrize("kind,media", [("audio", AUDIO), ("pdf", PDF)])
def test_overall_timeout_during_attempt_prevents_another_provider(kind, media):
    now = [0.0]
    operation = service.Operation(kind, clock=lambda: now[0])

    class DeadlineHTTP(RoutedHTTP):
        def post(self, url, **kwargs):
            response = super().post(url, **kwargs)
            now[0] = operation.deadline + 1
            return response

    http = DeadlineHTTP({
        "openai.com": (503, b""),
        "googleapis": (200, json.dumps(_text_response("gemini", kind, "complete text")).encode()),
    })
    with pytest.raises(DescriptionError, match="ai_error_timeout"):
        chain(http, ["openai", "gemini"], kind=kind, media=media, operation=operation)
    assert len(http.calls) == 1
