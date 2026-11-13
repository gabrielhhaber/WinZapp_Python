from dataclasses import replace

import pytest

from core.ai_media.errors import DescriptionError
from core.keyboard_shortcuts import binding_for
from core.message_translation import (
    TranslationSettings, _model_options, cached_translation, credential_id, model_test_identity,
    message_identity, message_text, normalized_base, translation_model_ready, translation_request,
)
from core.shortcut_catalog import SHORTCUTS


@pytest.mark.parametrize(("provider", "shape", "suffix", "header"), [
    ("gemini", "gemini", "/models/demo:generateContent", "x-goog-api-key"),
    ("claude", "claude", "/messages", "x-api-key"),
    ("custom_anthropic", "claude", "/v1/messages", "x-api-key"),
    ("custom_openai", "chat_completions", "/v1/chat/completions", "Authorization"),
    ("grok", "chat_completions", "/chat/completions", "Authorization"),
    ("groq", "chat_completions", "/chat/completions", "Authorization"),
])
def test_translation_sends_text_to_the_selected_protocol(provider, shape, suffix, header):
    config = TranslationSettings(provider, "demo", "Arabic", "https://example.test/v1/")
    protocol, request = translation_request(config, "test-key", "Test message")
    assert protocol == shape
    assert request.url.endswith(suffix)
    assert request.headers[header].endswith("test-key")
    assert "Test message" in str(request.json)
    assert "Arabic" in str(request.json)


def test_custom_credentials_are_bound_to_the_exact_api_and_protocol():
    base = TranslationSettings("custom_openai", "demo", "Arabic", "https://example.test/v1/")
    assert credential_id(base) == credential_id(replace(base, base_url="https://EXAMPLE.test/v1"))
    for variant in (replace(base, base_url="https://other.test/v1"),
                    replace(base, base_url="https://example.test/gateway/v1"),
                    replace(base, provider="custom_anthropic")):
        assert credential_id(base) != credential_id(variant)


@pytest.mark.parametrize("url", [
    "http://example.test/v1", "https://user:pass@example.test/v1",
    "https://example.test/v1?key=value", "https://example.test/v1#fragment",
])
def test_invalid_custom_base_rejected(url):
    with pytest.raises(DescriptionError):
        normalized_base(url)


def test_translation_catalog_skips_known_non_text_models():
    rows = {"data": [{"id": "whisper-large-v3"}, {"id": "demo"},
                     {"id": "vendor/dall-e-3"}, {"id": "gpt-image-1"},
                     {"id": "photo", "output_modalities": ["image"]}]}
    assert [model.id for model in _model_options("groq", rows)] == ["demo"]


def test_translation_cache_cannot_cross_conversations_or_a_message_edit():
    message = {"messageType": "conversation", "message": {"conversation": "Hello"},
               "key": {"remoteJid": "101@s.whatsapp.net", "id": "id"}}
    results = {message_identity(message): (TranslationSettings(), "Hello", "Translated")}
    assert cached_translation(results, message) == "Translated"
    assert not cached_translation(results, {**message, "key": {"remoteJid": "102@s.whatsapp.net", "id": "id"}})
    assert not cached_translation(results, {**message, "message": {"conversation": "Edited"}})
    assert not message_text({**message, "isViewOnce": True})


def test_translation_shortcut_can_be_rebound_or_disabled():
    shortcut = next(s for s in SHORTCUTS if s.id == "message_list.translate")
    assert binding_for(shortcut, {}, str) == (4, 13)
    assert binding_for(shortcut, {shortcut.id: [6, 84]}, str) == (6, 84)
    assert binding_for(shortcut, {shortcut.id: None}, str) is None


@pytest.mark.parametrize(("provider", "header", "pagination"), [
    ("custom_openai", "Authorization", ""),
    ("custom_anthropic", "x-api-key", "anthropic"),
])
def test_custom_model_discovery_uses_the_configured_base_and_protocol(monkeypatch, provider, header, pagination):
    from core import message_translation

    def fetch(request, parse, token, *, pagination):
        assert pagination == expected_pagination
        assert request[0] == "https://example.test/api/v1/models"
        assert request[1][header].endswith("test-key")
        return {row.id: row for row in parse({"data": [{"id": "second"}, {"id": "first"}]})}

    expected_pagination = pagination
    monkeypatch.setattr(message_translation, "fetch_catalog", fetch)
    config = TranslationSettings(provider, "", "", "https://example.test/api/v1")
    assert message_translation.list_models(config, "test-key", object()) == ("first", "second")


def test_automatic_translation_is_scoped_to_chat_and_provider():
    from types import SimpleNamespace
    from ui.conversation_panel.translation_actions import TranslationActionsMixin

    config = TranslationSettings("custom_openai", "model", "Arabic", "https://example.test/v1", True)
    jid, alias = "123@s.whatsapp.net", "55@lid"
    mw = SimpleNamespace(is_chat_locked=lambda value: False, _phone_to_lid={jid: alias})
    panel = SimpleNamespace(main_window=mw, _translation_chat_key=lambda value: jid,
                            _translation_chats=lambda: {alias: {"provider": credential_id(config),
                                                                 "enabled": True}})
    enabled = TranslationActionsMixin._automatic_translation_enabled
    assert enabled(panel, jid, config)
    assert not enabled(panel, jid, replace(config, base_url="https://other.test/v1"))
    assert not enabled(panel, jid, replace(config, automatic=False))
    mw.is_chat_locked = lambda value: True
    assert not enabled(panel, jid, config)


def test_manual_only_translation_does_not_load_automatic_chat_metadata():
    from types import SimpleNamespace
    from ui.conversation_panel.translation_actions import TranslationActionsMixin

    panel = SimpleNamespace(main_window=SimpleNamespace(is_chat_locked=lambda _: False))
    config = TranslationSettings("gemini", "gemini-flash-lite-latest", "Arabic")
    may_be_enabled = TranslationActionsMixin._automatic_translation_may_be_enabled
    assert not may_be_enabled(panel, "123@s.whatsapp.net", config)


@pytest.mark.parametrize(("handled", "expected"), [
    (True, ["translate"]),
    # A row without text: Shift+Enter keeps opening it, as before translation.
    (False, ["translate", "open"]),
])
def test_listbox_offers_shift_enter_to_translation_before_native_activation(handled, expected):
    from types import SimpleNamespace
    import wx
    from ui.accessible import CompatListBoxMessagesCtrl

    calls = []

    def translate(event):
        calls.append("translate")
        return handled

    event = SimpleNamespace(GetKeyCode=lambda: wx.WXK_RETURN,
                            ControlDown=lambda: False, ShiftDown=lambda: True,
                            AltDown=lambda: False, GetSkipped=lambda: False,
                            Skip=lambda: calls.append("skipped"))
    box = SimpleNamespace(HasFocus=lambda: True, GetSelection=lambda: 0,
                          _shortcut_handler=translate,
                          _key_down_handler=lambda _: calls.append("key down"),
                          _activated_handler=lambda _: calls.append("open"))
    CompatListBoxMessagesCtrl._on_char_hook(box, event)
    assert calls == expected


def test_gemini_defaults_to_flash_lite_alias_and_keeps_an_explicit_model():
    from core.message_translation import settings_from

    assert settings_from({"provider": "gemini"}).model == "gemini-flash-lite-latest"
    assert settings_from({"provider": "gemini", "model": "custom-model"}).model == "custom-model"


def test_automatic_translation_age_and_scope_are_independent():
    from core.message_translation import automatic_candidate, chat_allows_automatic, settings_from

    config = settings_from({"provider": "gemini", "automatic": True, "scope": "all",
                            "max_age_minutes": 5})
    jid = "123@s.whatsapp.net"
    message = {"key": {"id": "id", "remoteJid": jid}, "messageType": "conversation",
               "message": {"conversation": "hello"}, "messageTimestamp": 701}
    assert automatic_candidate(message, config, now=1000)
    assert not automatic_candidate({**message, "messageTimestamp": 699}, config, now=1000)
    assert not automatic_candidate({**message, "key": {**message["key"], "fromMe": True}}, config, now=1000)
    assert chat_allows_automatic(config, {}, (jid,))
    assert not chat_allows_automatic(config, {jid: {"provider": "gemini", "enabled": False}}, (jid,))
    assert not chat_allows_automatic(replace(config, scope="selected"), {}, (jid,))


def test_automatic_translation_exclusion_survives_a_provider_change():
    from core.message_translation import chat_allows_automatic, settings_from

    jid, alias = "123@s.whatsapp.net", "55@lid"
    excluded = {alias: {"provider": "gemini", "enabled": False}}
    config = settings_from({"provider": "grok", "automatic": True, "scope": "all"})
    assert not config.automatic
    config = replace(config, automatic=True, all_accounts_confirmed=True)
    assert not chat_allows_automatic(config, excluded, (jid, alias))


def test_unknown_model_requires_a_successful_test_for_its_provider_and_endpoint():
    config = TranslationSettings("custom_openai", "custom-model", "Arabic", "https://example.test/v1")
    assert not translation_model_ready(config)
    assert translation_model_ready(replace(config, model_tested=True))
    assert not translation_model_ready(replace(config, model="dall-e-3", model_tested=True))
    assert model_test_identity(config) != model_test_identity(replace(config, model="other-model"))
    assert model_test_identity(config) != model_test_identity(replace(config, base_url="https://other.test/v1"))
    assert translation_model_ready(TranslationSettings("gemini", "gemini-flash-lite-latest", "Arabic"))


def test_translation_daily_limit_is_reserved_in_its_own_settings_block():
    from core.message_translation import reserve_automatic_request, settings_from

    class App:
        def __init__(self):
            self.translation = {"provider": "gemini", "automatic": True, "daily_limit": 1}

        def update(self, key, updater):
            assert key == "message_translation"
            self.translation = updater(self.translation)

    app = App()
    config = settings_from(app.translation)
    reserve_automatic_request(app, config, now=120000)
    with pytest.raises(DescriptionError, match="ai_error_limit"):
        reserve_automatic_request(app, config, now=120000)
    reserve_automatic_request(app, config, now=120000 + 86400)


@pytest.mark.parametrize(("display", "expected"), [
    ("replace", "translated"),
    ("append", "original | Translation: translated"),
    ("separate", "original"),
])
def test_translation_display_preserves_the_original_message(display, expected):
    from core.message_translation import translated_body

    message = {"messageType": "conversation", "message": {"conversation": "original"},
               "key": {"remoteJid": "101@s.whatsapp.net", "id": "id"}}
    config = replace(TranslationSettings(), display=display)
    cached = {message_identity(message): (config, "original", "translated")}
    assert translated_body("original", cached, message, lambda key: "Translation: {text}") == expected
    assert message_text(message) == "original"


def test_translation_reuses_existing_keys_and_keeps_custom_keys_out_of_shared_store(tmp_path):
    from core.ai_credentials import CredentialStore, store_for_translation

    shared = CredentialStore(tmp_path)
    shared.set("gemini", "fake-gemini-key")
    config = TranslationSettings("custom_openai", "model", "Arabic", "https://example.test/v1")
    custom = credential_id(config)
    store_for_translation(shared, custom).set(custom, "fake-custom-key")

    assert store_for_translation(shared, "gemini") is shared
    assert shared.get("gemini") == "fake-gemini-key"
    assert shared.saved() == {"gemini"}
    assert store_for_translation(shared, custom).get(custom) == "fake-custom-key"


def test_every_engine_but_grok_and_custom_endpoints_has_a_ready_default_model():
    from core.ai_media.config import PROVIDERS
    from core.message_translation import default_model, settings_from

    for provider in PROVIDERS:
        config = settings_from({"provider": provider, "language": "Arabic"})
        assert config.model == default_model(provider) != ""
        assert translation_model_ready(config), provider
    for provider in ("grok", "custom_openai", "custom_anthropic"):
        assert default_model(provider) == ""


@pytest.mark.parametrize("model", [
    "mistral-embed", "codestral-embed", "mistral-moderation-latest", "gpt-4o-mini-transcribe",
    "playai-tts", "distil-whisper-large-v3-en", "gpt-realtime", "veo-3",
])
def test_translation_catalog_skips_embedding_speech_and_moderation_models(model):
    assert not [row.id for row in _model_options("mistral", {"data": [{"id": model}]})]
    assert [row.id for row in _model_options("mistral", {"data": [{"id": "mistral-small-latest"}]})]


@pytest.mark.parametrize(("error", "pauses"), [
    ("ai_error_authentication", True), ("ai_error_quota", True), ("ai_error_credits", True),
    ("ai_error_credentials", True), ("ai_error_network", False), ("ai_error_limit", False),
    ("ai_error_cancelled", False), (None, False),
])
def test_only_refusals_that_repeat_pause_automatic_translation(error, pauses):
    from core.message_translation import pauses_automatic

    assert pauses_automatic(error) is pauses


@pytest.mark.parametrize(("length", "fits"), [(32000, True), (32001, False)])
def test_a_chat_completions_translation_is_parsed_and_never_cut(length, fits):
    from core.ai_media.providers import parse_answer

    body = {"choices": [{"message": {"content": "x" * length}, "finish_reason": "stop"}]}
    if fits:
        assert parse_answer("chat_completions", body, "text") == "x" * length
    else:
        with pytest.raises(DescriptionError, match="output_limit"):
            parse_answer("chat_completions", body, "text")


def test_anthropic_model_pages_follow_last_id_until_has_more_ends():
    import json
    from core.ai_media.model_catalog import fetch_catalog
    from core.ai_media.service import RequestToken
    from tests.test_ai_media_core import FakeHTTP, FakeResponse

    bodies = [{"data": [{"id": "first"}], "has_more": True, "last_id": "first"},
              {"data": [{"id": "second"}], "has_more": False, "last_id": "second"}]

    class Pages(FakeHTTP):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse(data=json.dumps(bodies[len(self.calls) - 1]).encode())

    http = Pages()
    request = ("https://example.test/v1/models", {"x-api-key": "k"}, {})
    found = fetch_catalog(request, lambda body: _model_options("claude", body),
                          RequestToken(seconds=20), lambda: http, pagination="anthropic")
    assert sorted(found) == ["first", "second"]
    assert "after_id" not in http.calls[0][1]["params"]
    assert http.calls[1][1]["params"]["after_id"] == "first"


def test_anthropic_has_more_without_last_id_is_an_error_not_a_partial_list():
    import json
    from core.ai_media.model_catalog import fetch_catalog
    from core.ai_media.service import RequestToken
    from tests.test_ai_media_core import FakeHTTP, FakeResponse

    http = FakeHTTP(FakeResponse(data=json.dumps({"data": [{"id": "a"}], "has_more": True}).encode()))
    with pytest.raises(DescriptionError, match="response"):
        fetch_catalog(("https://example.test/v1/models", {}, {}),
                      lambda body: _model_options("claude", body), RequestToken(seconds=20),
                      lambda: http, pagination="anthropic")


def test_snapshot_read_never_waits_on_a_held_settings_lock(tmp_path):
    import threading
    from app_settings import AppSettings
    from coord_locks import LockTimeout, app_settings_lock

    app = AppSettings(str(tmp_path))
    app.set("message_translation", {"provider": "gemini"})
    assert app.get_snapshot("message_translation") == {"provider": "gemini"}
    held, release = threading.Event(), threading.Event()

    def hold():
        with app_settings_lock(str(tmp_path)):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(10)
        with pytest.raises(LockTimeout):
            app.get_snapshot("message_translation")
    finally:
        release.set()
        holder.join(10)


def test_grok_and_custom_keys_never_reach_the_shared_vault_and_a_reset_wipes_both(tmp_path):
    from core.ai_credentials import (
        CredentialStore, TranslationCredentialStore, apply_with_translation,
    )

    shared = CredentialStore(tmp_path)
    custom = credential_id(TranslationSettings("custom_openai", "m", "Arabic", "https://example.test/v1"))
    apply_with_translation(shared, {"openai": "fake-openai", "grok": "fake-grok", custom: "fake-custom"})
    translation = TranslationCredentialStore(tmp_path)
    assert shared.saved() == {"openai"}
    assert translation.saved() == {"grok", custom}
    apply_with_translation(shared, {}, reset=True)
    assert shared.saved() == frozenset() and translation.saved() == frozenset()
    assert not translation.data_path.exists() and not translation.key_path.exists()


def test_a_custom_endpoint_key_cannot_be_written_to_the_shared_vault(tmp_path):
    from core.ai_credentials import CredentialError, CredentialStore

    custom = credential_id(TranslationSettings("custom_openai", "m", "Arabic", "https://example.test/v1"))
    with pytest.raises(CredentialError):
        CredentialStore(tmp_path).apply({custom: "fake"})
    with pytest.raises(CredentialError):
        CredentialStore(tmp_path).apply({"grok": "fake"})
