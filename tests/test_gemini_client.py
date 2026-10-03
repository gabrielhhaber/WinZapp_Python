"""resolve_model() is the one place that decides which Gemini model a call
actually uses — every caller in the AI actions goes through it instead of
repeating "or DEFAULT_MODEL" by hand. That single-source-of-truth property is
what this file is protecting: a caller that stops going through it would
silently pin itself to whatever DEFAULT_MODEL happened to be at the time,
instead of following it forward the next time a model gets retired (as
gemini-2.5-flash did — see DEFAULT_MODEL's own comment).
"""

import inspect
from pathlib import Path

from google.genai.errors import ClientError

import core.gemini_client as gemini_client
from core.gemini_client import (
    DEFAULT_MODEL,
    RECOMMENDED_MODELS,
    _classify_client_error,
    resolve_model,
    transcribe_audio,
)


def _client_error(code: int, status: str, message: str) -> ClientError:
    """Monta um ClientError com a mesma forma que o SDK do Gemini realmente
    devolve (`{"error": {"code", "message", "status"}}`), para testar
    _classify_client_error() sem depender de uma chamada de rede real."""
    return ClientError(code, {"error": {"code": code, "message": message, "status": status}})


class TestAutomaticFollowsTheDefault:
    def test_none_falls_back_to_the_default(self):
        assert resolve_model(None) == DEFAULT_MODEL

    def test_empty_string_falls_back_to_the_default(self):
        assert resolve_model("") == DEFAULT_MODEL

    def test_whitespace_only_falls_back_to_the_default(self):
        """A settings.json hand-edited to "   " should behave like "", not
        like a (broken) model id."""
        assert resolve_model("   ") == DEFAULT_MODEL


class TestAConfiguredModelIsPinned:
    def test_a_chosen_model_is_used_as_is(self):
        assert resolve_model("gemini-3.8-flash") == "gemini-3.8-flash"

    def test_surrounding_whitespace_is_stripped(self):
        assert resolve_model("  gemini-3.6-flash  ") == "gemini-3.6-flash"

    def test_a_model_outside_the_recommended_list_is_still_honored(self):
        """Settings.json may hold a model this version of WinZapp doesn't
        list anymore (or doesn't list yet) — resolve_model() itself has no
        opinion on that; it's not the validation layer, just the fallback."""
        assert resolve_model("gemini-9.9-flash") == "gemini-9.9-flash"


class TestTheDefaultIsAlwaysAnOfferedChoice:
    def test_default_model_is_one_of_the_recommended_models(self):
        """Otherwise Settings would offer choices that don't include what
        "Automatic" actually resolves to right now."""
        assert DEFAULT_MODEL in RECOMMENDED_MODELS


class TestEveryCallerGoesThroughResolveModel:
    """Nothing must read ai_accessibility['gemini_model'] (or its OpenAI/Claude
    counterparts) and fall back to a provider's DEFAULT_MODEL by hand — that
    duplicates the rule this file tests above, and the two copies drifting
    apart is exactly how the model Settings shows as selected stops matching
    the model actually called.

    core/ai_providers.py fans a call out across the providers with fallback,
    so ui/conversation_panel/ai_actions.py never calls a provider's
    resolve_model() directly — it hands the whole ai_accessibility dict to
    core/ai_providers.py, which is the one place (_run_chain()) that resolves
    each provider's model right before calling it. That's where this
    invariant actually lives.
    """

    def test_ai_actions_delegates_model_resolution_to_ai_providers(self):
        # Read as text, not imported: the mixin imports wx, which the pure
        # logic tests deliberately do not need.
        src = (
            Path(__file__).resolve().parents[1]
            / "client" / "ui" / "conversation_panel" / "ai_actions.py"
        ).read_text(encoding="utf-8")
        # ai_actions.py must go through the ai_providers orchestrator...
        assert "ai_providers." in src
        # ...and never hand-roll a per-provider model fallback itself.
        assert 'get("gemini_model")) or DEFAULT_MODEL' not in src
        assert '_resolve_model(' not in src

    def test_ai_providers_resolves_every_configured_providers_model(self):
        import core.ai_providers as ai_providers_module

        src = inspect.getsource(ai_providers_module)
        assert "spec.module.resolve_model(" in src
        assert 'get(spec.model_setting) or "").strip()) or ' not in src


class TestClientErrorMessagesAreSpecificNotGeneric:
    """The Gemini SDK raises the same ClientError shape for very different
    situations (wrong key, per-minute quota, prepaid credits at zero) —
    only exc.status/exc.message tell them apart. Before this, every one of
    these reached the user (and their screen reader) as the same "check your
    key and quota" sentence, which doesn't say what to actually do next."""

    def test_prepayment_credits_depleted_points_to_billing(self):
        # Real payload reported by a user (RESOURCE_EXHAUSTED, HTTP 402):
        # prepaid credits ran out, distinct from a temporary quota limit.
        exc = _client_error(
            402,
            "RESOURCE_EXHAUSTED",
            "Your prepayment credits are depleted. Please go to AI Studio "
            "at https://ai.studio/projects to manage your project and "
            "billing.",
        )
        message = _classify_client_error(exc)
        assert "crédito" in message.lower()
        assert "ai.studio/projects" in message
        # A cota/limite temporário e o crédito esgotado precisam de ações
        # diferentes do usuário — não podem cair na mesma frase.
        assert "aguarde" not in message.lower()

    def test_generic_resource_exhausted_reads_as_temporary_quota(self):
        exc = _client_error(
            429,
            "RESOURCE_EXHAUSTED",
            "Quota exceeded for quota metric requests per minute",
        )
        message = _classify_client_error(exc)
        assert "cota" in message.lower()
        assert "aguarde" in message.lower()
        assert "crédito" not in message.lower()

    def test_invalid_key_names_the_settings_screen(self):
        exc = _client_error(401, "UNAUTHENTICATED", "API key not valid")
        message = _classify_client_error(exc)
        assert "chave" in message.lower()
        assert "Configurações" in message

    def test_unrecognized_status_falls_back_to_generic_message(self):
        exc = _client_error(400, "INVALID_ARGUMENT", "Something else entirely")
        message = _classify_client_error(exc)
        assert "recusou o pedido" in message

    def test_every_message_keeps_the_technical_detail(self):
        """Preferência já estabelecida no arquivo: a mensagem amigável nunca
        substitui o detalhe técnico, só vem antes dele."""
        exc = _client_error(402, "RESOURCE_EXHAUSTED", "prepayment credits depleted")
        assert "Detalhe técnico:" in _classify_client_error(exc)


class _FakeModels:
    """Stands in for genai.Client().models — records exactly what
    transcribe_audio() sends, without ever reaching the real API."""

    def __init__(self):
        self.calls = []

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})

        class _Response:
            text = "transcrição fake"

        return _Response()


class _FakeClient:
    def __init__(self):
        self.models = _FakeModels()


class TestTranscribeAudioGuardsAgainstHallucination:
    """A user reported the Gemini transcription inventing sentences that
    were never in the actual audio — unlike OpenAI/Groq, whose transcription
    goes through a dedicated, deterministic Whisper-style endpoint, Gemini
    transcribes audio the same way it answers any other generative prompt,
    which is exactly what makes it prone to "completing" unclear passages
    instead of admitting it couldn't make them out. temperature=0.0 (the
    most literal setting the API offers) and an explicit anti-invention
    instruction in the prompt are the two levers available here — neither
    guarantees zero hallucination on its own, but both together should
    reduce it."""

    def _run_with_fake_client(self, monkeypatch, tmp_path):
        fake_client = _FakeClient()
        monkeypatch.setattr(gemini_client.genai, "Client", lambda api_key: fake_client)
        audio_path = tmp_path / "voice.ogg"
        audio_path.write_bytes(b"fake audio bytes, never actually decoded")
        transcribe_audio(str(audio_path), "fake-key")
        assert len(fake_client.models.calls) == 1
        return fake_client.models.calls[0]

    def test_transcription_asks_for_the_most_literal_output_possible(self, monkeypatch, tmp_path):
        call = self._run_with_fake_client(monkeypatch, tmp_path)
        assert call["config"] is not None
        assert call["config"].temperature == 0.0

    def test_prompt_explicitly_forbids_inventing_or_guessing_content(self, monkeypatch, tmp_path):
        call = self._run_with_fake_client(monkeypatch, tmp_path)
        prompt = call["contents"][1].lower()
        assert "invente" in prompt
        assert "adivinhe" in prompt
        assert "inaudível" in prompt

    def test_other_gemini_calls_are_unaffected_by_the_transcription_temperature(self):
        """temperature is opt-in per call (_generate_text's default is
        None, i.e. the API's own default) — describe/ask/pdf must keep
        using it, not inherit transcribe_audio's temperature=0.0."""
        src = inspect.getsource(gemini_client.describe_visual_media)
        assert "temperature" not in src
