"""Local model servers (Ollama, LM Studio) in the AI media feature: address
validation, preferences, the chain, requests and answers, consent, timeouts.

Synthetic data, fake transports: nothing here reaches a network or a window.
"""
import json

import pytest

from core.ai_credentials import CredentialError, CredentialStore
from core.ai_media import auto, config, local_llm, service
from core.ai_media.errors import DescriptionError
from core.ai_media.local_url import is_loopback_url, normalize_url, valid_url
from core.ai_media.payload import Media
from core.ai_media.prompts import instructions
from core.ai_media.providers import build_request, parse_answer
from tests.test_ai_media_core import FakeHTTP, FakeResponse

DATA = b"synthetic-bytes"
IMAGE = Media("image", DATA, "image/jpeg", "image.jpg")
PDF = Media("pdf", DATA, "application/pdf", "document.pdf")
AUDIO = Media("audio", DATA, "audio/ogg", "audio.ogg")
TEXT = Media("text", b"line one\nline two", "text/plain", "text.txt")
OLLAMA, LM = "local_ollama", "local_lm_studio"


class Settings:
    def __init__(self, block=None):
        self.block = block

    def get(self, key, default=None):
        return self.block if key == "ai_media" else default


def prefs(**block):
    return config.preferences(Settings({"enabled": True, **block}))


def ready(**more):
    return prefs(models={OLLAMA: "qwen2.5vl:7b"}, **more)


# ── the address ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("typed,expected", [
    ("http://localhost:11434/v1", "http://localhost:11434/v1"),
    ("  http://127.0.0.1:1234/v1/  ", "http://127.0.0.1:1234/v1"),
    ("http://localhost:11434", "http://localhost:11434/v1"),
    ("http://[::1]:11434", "http://[::1]:11434/v1"),
    ("http://127.9.9.9:1/v1", "http://127.9.9.9:1/v1"),
    ("https://llm.example.org/v1", "https://llm.example.org/v1"),
])
def test_valid_addresses_are_normalized(typed, expected):
    assert normalize_url(typed) == expected


@pytest.mark.parametrize("typed", [
    "", "   ", None, 5, "localhost:11434", "ftp://localhost/v1", "file:///c:/x",
    "http://llm.example.org/v1",          # plain http only for this computer
    "http://192.168.1.5:11434/v1",        # a LAN host is a remote server: https only
    "http://user:pass@localhost:11434/v1", "http://user@localhost/v1",
    "http://localhost:11434/v1?key=1", "http://localhost:11434/v1#x", "http://localhost/v1;p=1",
    "http://localhost:99999/v1", "http://localhost:11434/../v1", "http://localhost:11434/v%31",
    "http://localhost:11434/v1 x", "http://localhost\\evil/v1", "http://",
    "https://" + "a" * 300 + ".org/v1",
])
def test_unsafe_or_malformed_addresses_are_refused(typed):
    with pytest.raises(DescriptionError, match="ai_error_local_url"):
        normalize_url(typed)
    assert valid_url(typed) == ""


@pytest.mark.parametrize("typed,loopback", [
    ("http://localhost:11434/v1", True), ("http://127.0.0.1:1234/v1", True), ("http://[::1]:1/v1", True),
    ("https://localhost/v1", True), ("https://llm.example.org/v1", False),
    ("https://127.0.0.1.example.org/v1", False), ("https://10.0.0.2/v1", False), ("nonsense", False),
])
def test_loopback_means_this_computer_and_nothing_that_merely_looks_like_it(typed, loopback):
    assert is_loopback_url(typed) is loopback


# ── preferences, the chain, what is offered ──────────────────────────────

def test_local_providers_are_in_the_catalogue_with_images_and_text_only():
    for provider in (OLLAMA, LM):
        spec = config.PROVIDERS[provider]
        assert spec.local and spec.capabilities == frozenset({"image", "text"}) and spec.model == ""
        assert provider not in config.KEYED_PROVIDERS
    assert config.PROVIDERS[OLLAMA].default_url == "http://localhost:11434/v1"
    assert config.PROVIDERS[LM].default_url == "http://localhost:1234/v1"
    # Message translation's own "ollama"/"lm_studio" are other settings.
    assert OLLAMA != "ollama" and LM != "lm_studio"


def test_preferences_default_to_the_usual_addresses_and_no_model():
    value = prefs()
    assert value["local_urls"] == {OLLAMA: "http://localhost:11434/v1", LM: "http://localhost:1234/v1"}
    assert value["models"][OLLAMA] == "" and value["models"][LM] == ""
    assert OLLAMA in value["order"] and LM in value["order"]
    assert OLLAMA not in value["auto_models"]  # nothing recommended: the person picks


def test_a_saved_address_is_read_strictly_and_never_falls_back_to_another_host():
    value = prefs(local_urls={OLLAMA: "http://evil.example.org/v1", LM: "https://llm.example.org/v1"})
    assert value["local_urls"] == {OLLAMA: "", LM: "https://llm.example.org/v1"}
    assert prefs(local_urls="junk")["local_urls"][OLLAMA] == "http://localhost:11434/v1"


def test_old_settings_without_local_keys_keep_their_order_and_choices():
    value = prefs(order=["gemini", "openai"], models={"openai": "gpt-4.1"}, disabled=["claude"])
    assert value["order"][:2] == ["gemini", "openai"] and value["order"][-2:] == [OLLAMA, LM]
    assert value["models"]["openai"] == "gpt-4.1" and value["disabled"] == ["claude"]


def test_a_local_provider_needs_no_key_but_does_need_a_chosen_model():
    no_keys = lambda provider: False
    assert config.chain(prefs(), "image", no_keys) == []
    assert config.chain(ready(), "image", no_keys) == [OLLAMA]
    assert config.chain(ready(), "text", no_keys) == [OLLAMA]
    assert config.chain(ready(), "writing", no_keys) == [OLLAMA]
    assert config.chain(ready(), "video", no_keys) == [OLLAMA]
    # Neither voice nor PDF: not supported.
    assert config.chain(ready(), "audio", no_keys) == [] and config.chain(ready(), "pdf", no_keys) == []
    # An address that failed validation keeps it out too.
    assert config.chain(ready(local_urls={OLLAMA: "ftp://x"}), "image", no_keys) == []
    # Switched off.
    assert config.chain(ready(disabled=[OLLAMA]), "image", no_keys) == []


def test_the_person_orders_local_and_online_providers_together():
    value = ready(order=[OLLAMA, "gemini"])
    assert config.chain(value, "image", lambda p: True)[:2] == [OLLAMA, "gemini"]
    value = ready(order=["gemini", OLLAMA])
    assert config.chain(value, "image", lambda p: p == "gemini") == ["gemini", OLLAMA]
    assert config.chain(value, "image", lambda p: p == "gemini", prefer=OLLAMA)[0] == OLLAMA


def message(kind="imageMessage"):
    return {"messageType": kind, "key": {"id": "ABC"}, "message": {kind: {}}}


def test_a_picture_is_offered_with_only_a_local_model_and_not_without_a_model():
    assert config.offered_kind(message(), ready(), frozenset()) == "image"
    assert config.offered_kind(message(), prefs(), frozenset()) is None
    # A voice message or PDF is not offered by a local model alone.
    assert config.offered_kind(message("audioMessage"), ready(), frozenset()) is None
    pdf = message("documentMessage")
    pdf["message"]["documentMessage"]["mimetype"] = "application/pdf"
    assert config.offered_kind(pdf, ready(), frozenset()) is None


def test_local_status_says_why_a_provider_is_or_is_not_used():
    assert config.local_status(prefs(), OLLAMA) == "no_model"
    assert config.local_status(ready(), OLLAMA) == "ready"
    assert config.local_status(ready(local_urls={OLLAMA: "https://llm.example.org/v1"}), OLLAMA) == "remote"
    assert config.local_status(ready(local_urls={OLLAMA: "nope"}), OLLAMA) == "bad_url"
    assert config.local_status({}, OLLAMA) == "bad_url"


# ── keys ─────────────────────────────────────────────────────────────────

def test_the_credential_store_never_holds_a_local_provider(tmp_path):
    store = CredentialStore(tmp_path)
    for provider in (OLLAMA, LM):
        with pytest.raises(CredentialError):
            store.set(provider, "key")
        with pytest.raises(CredentialError):
            store.get(provider)
    store.set("openai", "secret")
    assert store.saved() == frozenset({"openai"})


# ── consent: only what leaves this computer ──────────────────────────────

def test_consent_names_only_providers_whose_data_leaves_this_computer():
    value = ready(order=[OLLAMA, "gemini"])
    assert config.outbound(value, [OLLAMA, "gemini"]) == ["gemini"]
    assert config.outbound(value, [OLLAMA]) == []
    remote = ready(local_urls={OLLAMA: "https://llm.example.org/v1"})
    assert config.outbound(remote, [OLLAMA]) == [OLLAMA]
    assert config.sends_out({"local_urls": {}}, OLLAMA) is True  # unknown address: ask
    assert config.sends_out({}, "openai") is True


def test_a_local_model_on_this_computer_needs_no_consent_even_for_a_locked_chat():
    from ui.dialogs.ai_result_dialog import needs_consent
    value = ready()
    for kind in ("image", "text", "context", "reply"):
        assert needs_consent(value, [OLLAMA], kind, locked=True) is False
        assert needs_consent(value, [OLLAMA], kind, locked=False) is False


def test_a_chain_that_also_reaches_an_online_provider_still_asks_for_that_one():
    from ui.dialogs.ai_result_dialog import needs_consent
    value = ready()
    assert needs_consent(value, [OLLAMA, "gemini"], "image", locked=False) is True
    value["consented"] = ["gemini"]
    assert needs_consent(value, [OLLAMA, "gemini"], "image", locked=False) is False
    assert needs_consent(value, [OLLAMA, "gemini"], "image", locked=True) is True


def test_a_remote_address_is_treated_as_an_online_provider():
    from ui.dialogs.ai_result_dialog import needs_consent
    value = ready(local_urls={OLLAMA: "https://llm.example.org/v1"})
    assert needs_consent(value, [OLLAMA], "image", locked=False) is True
    value["consented"] = [OLLAMA]
    assert needs_consent(value, [OLLAMA], "image", locked=False) is False
    assert needs_consent(value, [OLLAMA], "image", locked=True) is True
    assert config.notice_keys([OLLAMA, LM]) == ["ai_local_remote_notice"]


def test_automatic_descriptions_run_on_a_local_model_without_a_consent_but_not_on_an_unconsented_remote():
    local = ready(auto_describe=True)
    assert auto.usable_providers(local, "image", frozenset()) == [OLLAMA]
    remote = ready(auto_describe=True, local_urls={OLLAMA: "https://llm.example.org/v1"})
    assert auto.usable_providers(remote, "image", frozenset()) == []
    remote["consented"] = [OLLAMA]
    assert auto.usable_providers(remote, "image", frozenset()) == [OLLAMA]


def test_changing_an_address_drops_what_was_consented_for_the_old_one():
    from ui.dialogs.ai_local_settings import changed_addresses
    saved = {OLLAMA: "https://a.example.org/v1", LM: "http://localhost:1234/v1"}
    staged = {OLLAMA: "https://b.example.org/v1", LM: "http://localhost:1234/v1"}
    assert changed_addresses(saved, staged) == {OLLAMA}
    value = config.drop_consents({"consented": [OLLAMA, "gemini"]}, {OLLAMA})
    assert value["consented"] == ["gemini"]


# ── requests ─────────────────────────────────────────────────────────────

def build(provider, media, *, key="other-providers-key", url="http://localhost:11434/v1", history=()):
    return build_request(provider, "qwen2.5vl:7b", key, media, history, "What is this?",
                         instructions("en-US", "balanced", media.kind), "balanced", base_url=url)


def test_a_picture_goes_to_the_chat_endpoint_with_no_credential_at_all():
    request = build(OLLAMA, IMAGE, history=(("user", "describe"), ("assistant", "red car")))
    assert request.url == "http://localhost:11434/v1/chat/completions"
    assert request.headers == {} and request.loopback is True
    assert "other-providers-key" not in json.dumps(request.json)
    body = request.json
    assert body["model"] == "qwen2.5vl:7b" and body["max_tokens"] == config.MAX_OUTPUT_TOKENS["image"]
    assert body["messages"][0]["role"] == "system" and "red car" in json.dumps(body)
    part = body["messages"][-1]["content"][1]
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "stream" not in body


def test_lm_studio_uses_its_own_address():
    assert build(LM, IMAGE, url="http://localhost:1234/v1").url == "http://localhost:1234/v1/chat/completions"


def test_text_requests_carry_the_text_and_the_question():
    request = build(OLLAMA, TEXT)
    content = request.json["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "line one\nline two"}
    assert content[1] == {"type": "text", "text": "What is this?"}
    assert request.headers == {} and request.url.endswith("/chat/completions")


def test_a_remote_address_is_https_and_still_gets_no_key_and_keeps_the_proxy_settings():
    request = build(OLLAMA, IMAGE, url="https://llm.example.org/v1")
    assert request.url == "https://llm.example.org/v1/chat/completions"
    assert request.headers == {} and request.loopback is False


@pytest.mark.parametrize("url", ["", "http://llm.example.org/v1", "http://user:p@localhost/v1",
                                 "http://localhost:11434/v1?x=1"])
def test_a_tampered_address_is_refused_when_the_request_is_built(url):
    with pytest.raises(DescriptionError, match="ai_error_local_url"):
        build(OLLAMA, IMAGE, url=url)


@pytest.mark.parametrize("media", [PDF, AUDIO])
def test_voice_and_pdf_are_never_built_for_a_local_model(media):
    with pytest.raises(DescriptionError, match="ai_error_request"):
        build(OLLAMA, media)


def test_online_requests_are_unchanged():
    request = build_request("groq", "m", "k", IMAGE, (), "q", "i", "balanced")
    assert request.url == "https://api.groq.com/openai/v1/chat/completions"
    assert request.headers == {"Authorization": "Bearer k"} and request.loopback is False


# ── answers ──────────────────────────────────────────────────────────────

def chat(content, finish="stop", **message):
    return {"choices": [{"message": {"content": content, **message}, "finish_reason": finish}]}


def test_a_local_answer_is_the_chat_completion_text():
    assert parse_answer(OLLAMA, chat("  A red car.  "), "image") == "A red car."
    assert parse_answer(LM, chat("Summary."), "text") == "Summary."


@pytest.mark.parametrize("body,category", [
    (chat("cut off", finish="length"), "response"),
    (chat("", finish="stop"), "response"),
    (chat(None), "response"),
    (chat("x", refusal="no"), "refusal"),
    ({"choices": []}, "response"),
    ({"unexpected": 1}, "response"),
])
def test_a_local_answer_that_is_empty_cut_off_or_refused_fails_like_any_provider(body, category):
    with pytest.raises(DescriptionError, match=f"ai_error_{category}"):
        parse_answer(OLLAMA, body, "image")


def test_a_reasoning_model_s_thinking_block_is_not_part_of_the_answer():
    assert parse_answer(OLLAMA, chat("<think>hmm\nlet me see</think>\n\nA red car."), "image") == "A red car."
    # Only a block at the very start is thinking; the same words later are kept.
    kept = "A sign says <think>x</think> here."
    assert parse_answer(OLLAMA, chat(kept), "image") == kept
    with pytest.raises(DescriptionError, match="ai_error_response"):
        parse_answer(OLLAMA, chat("<think>only thinking</think>"), "image")


# ── the chain ────────────────────────────────────────────────────────────

class Recorder(FakeHTTP):
    """A session that remembers whether it was told to ignore proxy variables."""
    trust_env = True


def run(http, providers, *, key_for, kind="image", media=IMAGE, endpoints=None, local=True):
    models = {p: ("qwen2.5vl:7b" if config.is_local(p) else config.PROVIDERS[p].model)
              for p in config.PROVIDERS}
    return service.run_chain(service.Operation(kind, local=local), providers, models, key_for, media,
                             (), "q", "i", "balanced", session_factory=lambda: http,
                             endpoints=endpoints or {OLLAMA: "http://localhost:11434/v1"})


def never(provider):
    raise AssertionError("a local provider holds no key: the store must not be asked")


def test_a_local_provider_runs_without_asking_for_a_key_and_ignores_proxy_variables():
    http = Recorder(FakeResponse(data=json.dumps(chat("A red car.")).encode()))
    assert run(http, [OLLAMA], key_for=never) == ("A red car.", OLLAMA)
    url, kwargs = http.calls[0]
    assert url == "http://localhost:11434/v1/chat/completions"
    assert kwargs["headers"] == {} and kwargs["allow_redirects"] is False
    assert http.trust_env is False


def test_an_online_request_keeps_the_proxy_settings():
    http = Recorder(FakeResponse(data=json.dumps(chat("ok")).encode()))
    assert run(http, ["groq"], key_for=lambda p: "key") == ("ok", "groq")
    assert http.trust_env is True and http.calls[0][1]["headers"] == {"Authorization": "Bearer key"}


def test_a_local_server_that_is_down_falls_through_to_the_next_provider():
    import requests

    class Routed(Recorder):
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if "localhost" in url:
                raise requests.ConnectionError("refused")
            return FakeResponse(data=json.dumps(chat("from groq")).encode())

    http = Routed()
    seen = []
    result = run(http, [OLLAMA, "groq"], key_for=lambda p: seen.append(p) or "key")
    assert result == ("from groq", "groq") and seen == ["groq"]


def test_a_local_provider_without_an_address_fails_as_a_request_not_as_credentials():
    http = Recorder()
    with pytest.raises(service.ChainFailed) as caught:
        run(http, [OLLAMA], key_for=never, endpoints={OLLAMA: ""})
    assert caught.value.attempts == ((OLLAMA, "local_url"),)
    assert http.calls == []


def test_a_local_attempt_waits_longer_and_the_online_ones_behind_it_keep_their_time():
    plain = service.Operation("image")
    mixed = service.Operation("image", local=True)
    assert plain.total_seconds == service.ATTEMPT_SECONDS["image"] * service.BUDGET_ATTEMPTS
    extra = service.LOCAL_ATTEMPT_SECONDS["image"] - service.ATTEMPT_SECONDS["image"]
    assert mixed.total_seconds == plain.total_seconds + extra
    assert service.LOCAL_ATTEMPT_SECONDS["image"] > service.ATTEMPT_SECONDS["image"]
    now = [0.0]
    operation = service.Operation("image", clock=lambda: now[0], local=True)
    local_token, online_token = operation.attempt(True), operation.attempt(False)
    assert local_token.deadline - 0 == service.LOCAL_ATTEMPT_SECONDS["image"]
    assert online_token.deadline == service.ATTEMPT_SECONDS["image"]
    # Without a local provider nothing changes, even if asked for a local attempt.
    token = plain.attempt(True)
    assert token.deadline - plain.clock() <= service.ATTEMPT_SECONDS["image"]


def test_every_kind_a_local_model_handles_has_a_longer_ceiling():
    for kind in ("image", "sticker", "video", "text", "writing"):
        assert service.LOCAL_ATTEMPT_SECONDS[kind] > service.ATTEMPT_SECONDS[kind]


# ── probing and listing ──────────────────────────────────────────────────

def test_the_connection_test_is_a_get_of_the_model_list_with_no_key_and_no_model_needed():
    assert service.probe_request(OLLAMA, "", "", "http://localhost:11434/v1") == (
        "http://localhost:11434/v1/models", {})
    with pytest.raises(DescriptionError, match="ai_error_local_url"):
        service.probe_request(OLLAMA, "", "", "http://llm.example.org/v1")


def test_probe_connection_reaches_a_local_server_without_a_proxy():
    http = Recorder(FakeResponse(data=b"{}"))
    assert service.probe_connection(OLLAMA, "", "", service.RequestToken(),
                                    session_factory=lambda: http,
                                    base_url="http://localhost:11434/v1") is True
    assert http.calls[0][0] == "http://localhost:11434/v1/models" and http.trust_env is False
    assert http.calls[0][1]["allow_redirects"] is False
    online = Recorder(FakeResponse(data=b"{}"))
    service.probe_connection("openai", "gpt-4.1", "k", service.RequestToken(),
                             session_factory=lambda: online)
    assert online.trust_env is True


def test_the_installed_models_are_listed_as_the_server_reports_them():
    body = {"data": [{"id": "qwen2.5vl:7b"}, {"id": "llama3.2-vision:11b"}, {"id": "bad id"},
                     {"id": 5}, "junk", {"id": "hf.co/user/model:Q4_K_M"}]}
    assert [o.id for o in local_llm.installed_models(body)] == [
        "qwen2.5vl:7b", "llama3.2-vision:11b", "hf.co/user/model:Q4_K_M"]
    with pytest.raises(DescriptionError):
        list(local_llm.installed_models({"models": []}))


def test_fetch_models_sorts_them_and_refuses_an_unusable_address():
    body = json.dumps({"data": [{"id": "b-model"}, {"id": "a-model"}]}).encode()
    http = Recorder(FakeResponse(data=body))
    found = local_llm.fetch_models("http://localhost:1234/v1", service.RequestToken(),
                                   session_factory=lambda: http)
    assert [o.id for o in found] == ["a-model", "b-model"]
    assert http.calls[0][0] == "http://localhost:1234/v1/models"
    with pytest.raises(DescriptionError, match="ai_error_local_url"):
        local_llm.fetch_models("http://evil.example.org/v1", service.RequestToken())


# ── the settings window's sentences ──────────────────────────────────────

def test_the_window_says_why_a_provider_is_not_used_and_warns_about_a_remote_server():
    from ui.dialogs.ai_local_provider_dialog import describe
    t = lambda key: key
    assert describe(t, prefs(), OLLAMA) == "ai_local_no_model"
    assert describe(t, ready(), OLLAMA) == ""
    assert describe(t, ready(local_urls={OLLAMA: "https://llm.example.org/v1"}), OLLAMA) == \
        "ai_local_remote_warning"
    assert describe(t, prefs(local_urls={OLLAMA: "x"}), OLLAMA) == "ai_error_local_url"
    both = prefs(local_urls={OLLAMA: "https://llm.example.org/v1"})
    assert describe(t, both, OLLAMA) == "ai_local_no_model\nai_local_remote_warning"


def test_the_list_row_says_name_state_and_what_is_missing():
    from ui.dialogs.ai_local_settings import local_row
    t = lambda key: key
    assert local_row(t, "Ollama", True, "no_model") == \
        "Ollama, ai_provider_state_enabled, ai_local_state_no_model"
    assert local_row(t, "LM Studio", False, "remote") == \
        "LM Studio, ai_provider_state_disabled, ai_local_state_remote"


# ── message translation does not regress ─────────────────────────────────

def test_translation_keeps_its_own_ollama_and_lm_studio_and_never_lists_the_media_ones():
    from core import message_translation as mt
    from ui.dialogs.translation_settings import ORDER
    assert {"ollama", "lm_studio"} <= set(mt.CUSTOM) and mt.PROVIDER_NAMES["ollama"] == "Ollama"
    assert not any(name.startswith("local_") for name in mt.PROVIDER_NAMES)
    assert not any(name.startswith("local_") for name in ORDER)
    assert ORDER.count("ollama") == 1 and ORDER.count("lm_studio") == 1
    assert set(config.KEYED_PROVIDERS) <= set(ORDER)
    assert mt.default_url("ollama") == "http://localhost:11434/v1"
    with pytest.raises(DescriptionError):
        mt.list_models(mt.TranslationSettings(provider=OLLAMA, model="m"), "", service.RequestToken())
