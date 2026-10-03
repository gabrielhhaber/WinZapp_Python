"""core/openai_compat.py + core/groq_client.py + core/openrouter_client.py.

Groq and OpenRouter speak the OpenAI protocol at another address; what matters
is that each one only claims what its API really accepts (Groq: image + audio,
no PDF; OpenRouter: image + PDF, no audio), that the request goes to the right
base_url with the right shape, and that a refusal reaches the user as a
friendly error. No network: the `OpenAI` class is replaced by a fake.
"""

import types

import pytest

from core import ai_providers, groq_client, openai_compat, openrouter_client
from core.openai_compat import OpenAICompatError


class _FakeOpenAI:
    instances = []
    reply = "texto"

    def __init__(self, api_key, base_url):
        self.api_key, self.base_url = api_key, base_url
        self.chat_calls, self.transcribe_calls = [], []
        _FakeOpenAI.instances.append(self)
        outer = self

        class _Chat:
            class completions:
                @staticmethod
                def create(**kw):
                    outer.chat_calls.append(kw)
                    msg = types.SimpleNamespace(content=_FakeOpenAI.reply)
                    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

        class _Audio:
            class transcriptions:
                @staticmethod
                def create(**kw):
                    outer.transcribe_calls.append(kw)
                    return types.SimpleNamespace(text=_FakeOpenAI.reply)

        self.chat, self.audio = _Chat, _Audio


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch):
    _FakeOpenAI.instances = []
    _FakeOpenAI.reply = "texto"
    monkeypatch.setattr(openai_compat, "OpenAI", _FakeOpenAI)
    monkeypatch.setattr(openai_compat.time, "sleep", lambda s: None)


@pytest.fixture
def image(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff")
    return str(p)


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"%PDF-1.4")
    return str(p)


@pytest.fixture
def audio(tmp_path):
    p = tmp_path / "a.ogg"
    p.write_bytes(b"OggS")
    return str(p)


class TestGroq:
    def test_image_goes_to_groq_with_default_model(self, image):
        assert groq_client.describe_visual_media(image, "k") == "texto"
        c = _FakeOpenAI.instances[0]
        assert c.base_url == "https://api.groq.com/openai/v1"
        assert c.chat_calls[0]["model"] == groq_client.DEFAULT_MODEL
        assert c.chat_calls[0]["messages"][0]["content"][1]["type"] == "image_url"

    def test_audio_uses_whisper(self, audio):
        assert groq_client.transcribe_audio(audio, "k") == "texto"
        assert _FakeOpenAI.instances[0].transcribe_calls[0]["model"] == "whisper-large-v3-turbo"

    def test_pdf_and_video_are_refused(self, pdf, image):
        with pytest.raises(OpenAICompatError):
            groq_client.pdf_to_accessible_text(pdf, "k")
        with pytest.raises(OpenAICompatError):
            groq_client.describe_visual_media(image, "k", is_video=True)
        assert _FakeOpenAI.instances == []  # refused before any network client


class TestOpenRouter:
    def test_pdf_is_sent_as_a_file_part(self, pdf):
        assert openrouter_client.pdf_to_accessible_text(pdf, "k", model="x/y") == "texto"
        c = _FakeOpenAI.instances[0]
        assert c.base_url == "https://openrouter.ai/api/v1"
        part = c.chat_calls[0]["messages"][0]["content"][1]
        assert part["type"] == "file"
        assert part["file"]["filename"] == "a.pdf"
        assert part["file"]["file_data"].startswith("data:application/pdf;base64,")
        assert c.chat_calls[0]["model"] == "x/y"

    def test_audio_is_refused(self, audio):
        with pytest.raises(OpenAICompatError):
            openrouter_client.transcribe_audio(audio, "k")

    def test_default_is_an_offered_choice(self):
        assert openrouter_client.DEFAULT_MODEL in openrouter_client.RECOMMENDED_MODELS
        assert groq_client.DEFAULT_MODEL in groq_client.RECOMMENDED_MODELS


class TestSharedBehaviour:
    def test_blank_key_is_a_friendly_error_naming_the_provider(self, image):
        with pytest.raises(OpenAICompatError, match="Groq"):
            groq_client.describe_visual_media(image, "  ")

    def test_automatic_model_follows_the_default(self):
        assert openrouter_client.resolve_model("") == openrouter_client.DEFAULT_MODEL
        assert openrouter_client.resolve_model(" a/b ") == "a/b"

    def test_empty_reply_is_an_error(self, image):
        _FakeOpenAI.reply = "  "
        with pytest.raises(OpenAICompatError):
            groq_client.describe_visual_media(image, "k")

    def test_oversized_file_is_refused(self, image, monkeypatch):
        monkeypatch.setattr(openai_compat, "_INLINE_SIZE_LIMIT_BYTES", 1)
        with pytest.raises(OpenAICompatError, match="grande demais"):
            groq_client.describe_visual_media(image, "k")


class TestProviderTable:
    """The real PROVIDERS table (not the fakes other test files patch in)."""

    def specs(self):
        return {p.id: p for p in ai_providers.PROVIDERS}

    def test_fallback_order_is_fixed(self):
        assert [p.id for p in ai_providers.PROVIDERS] == [
            "gemini", "openai", "claude", "groq", "openrouter",
        ]

    def test_capabilities_match_what_each_api_accepts(self):
        s = self.specs()
        assert (s["groq"].supports_audio, s["groq"].supports_video, s["groq"].supports_pdf) == (True, False, False)
        assert (s["openrouter"].supports_audio, s["openrouter"].supports_video, s["openrouter"].supports_pdf) == (False, False, True)

    def test_pdf_chain_skips_groq_and_audio_chain_skips_openrouter(self):
        keys = {"groq_api_key": "g", "openrouter_api_key": "o"}
        assert [p.id for p in ai_providers._chain_for(keys, needs_pdf=True)] == ["openrouter"]
        assert [p.id for p in ai_providers._chain_for(keys, needs_audio=True)] == ["groq"]
        assert [p.id for p in ai_providers._chain_for(keys)] == ["groq", "openrouter"]

    def test_configured_ids_include_the_new_providers(self):
        assert ai_providers.configured_provider_ids(
            {"groq_api_key": "x", "openrouter_api_key": "y"}
        ) == ["groq", "openrouter"]
