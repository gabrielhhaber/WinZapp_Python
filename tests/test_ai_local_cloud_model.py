"""An Ollama "-cloud" / ":cloud" model answers on localhost but forwards the
request to Ollama's cloud: it asks for consent like a remote server (IA-01), and
pressing OK in the provider window says the warning aloud once."""

import pytest

from core.ai_media import auto, config
from tests.test_ai_media_local import LM, OLLAMA, prefs, ready


@pytest.mark.parametrize("model", ["gpt-oss:120b-cloud", "qwen3-coder:480b-cloud",
                                   "kimi-k2:cloud", "Foo-CLOUD", "x:Cloud", " a-cloud "])
def test_cloud_model_ids(model):
    assert config.is_cloud_model(model)


@pytest.mark.parametrize("model", ["", "qwen2.5vl:7b", "cloudy", "cloud:7b", "my-cloud-model", None, 3])
def test_other_ids_are_not_cloud(model):
    assert not config.is_cloud_model(model)


def with_model(model, **extra):
    value = ready(**extra)
    value["models"] = {**value["models"], OLLAMA: model}
    return value


def test_a_cloud_model_on_this_computer_sends_out_and_is_the_cloud_state():
    value = with_model("gpt-oss:120b-cloud")
    assert config.sends_out(value, OLLAMA) is True
    assert config.outbound(value, [OLLAMA, LM]) == [OLLAMA]
    assert config.local_status(value, OLLAMA) == "cloud"
    assert config.sends_out(with_model("qwen2.5vl:7b"), OLLAMA) is False


def test_cloud_model_needs_consent_and_a_locked_chat_asks_every_time():
    from ui.dialogs.ai_result_dialog import needs_consent
    value = with_model("kimi-k2:cloud")
    for kind in ("image", "text", "context", "reply"):
        assert needs_consent(value, [OLLAMA], kind, locked=False) is True
    value["consented"] = [OLLAMA]
    assert needs_consent(value, [OLLAMA], "image", locked=False) is False
    assert needs_consent(value, [OLLAMA], "image", locked=True) is True


def test_automatic_descriptions_wait_for_a_consent_on_a_cloud_model():
    value = with_model("kimi-k2:cloud", auto_describe=True)
    assert auto.usable_providers(value, "image", frozenset()) == []
    value["consented"] = [OLLAMA]
    assert auto.usable_providers(value, "image", frozenset()) == [OLLAMA]


def test_the_notice_names_the_cloud_and_keeps_the_address_one_for_a_remote_server():
    cloud = with_model("a-cloud")
    assert config.notice_keys([OLLAMA], cloud) == ["ai_local_cloud_model_notice"]
    remote = with_model("a-cloud", local_urls={OLLAMA: "https://llm.example.org/v1"})
    assert config.notice_keys([OLLAMA], remote) == [
        "ai_local_remote_notice", "ai_local_cloud_model_notice"]
    assert config.notice_keys([OLLAMA]) == ["ai_local_remote_notice"]
    assert config.notice_keys([OLLAMA], with_model("qwen")) == ["ai_local_remote_notice"]


def test_the_consent_text_carries_the_cloud_notice():
    from ui.dialogs.ai_result_dialog import consent_text

    class T:
        @staticmethod
        def t(key):
            return key if "{" not in key else "{providers}"

    text = consent_text(T, [OLLAMA], "image", False, config=with_model("a-cloud"))
    assert "ai_local_cloud_model_notice" in text


def test_moving_a_model_to_or_from_a_cloud_one_drops_the_consent():
    from ui.dialogs.ai_local_settings import changed_cloud_models
    saved = {OLLAMA: "qwen", LM: "m", "openai": "gpt"}
    assert changed_cloud_models(saved, {OLLAMA: "qwen-cloud", LM: "m", "openai": "x-cloud"}) == {OLLAMA}
    assert changed_cloud_models({OLLAMA: "a-cloud"}, {OLLAMA: "b:cloud"}) == set()
    assert changed_cloud_models({OLLAMA: "a-cloud"}, {OLLAMA: "b"}) == {OLLAMA}


def test_the_window_warns_about_a_cloud_model():
    from ui.dialogs.ai_local_provider_dialog import describe
    t = lambda key: key
    assert describe(t, with_model("a-cloud"), OLLAMA) == "ai_local_cloud_model_warning"
    remote = with_model("a-cloud", local_urls={OLLAMA: "https://llm.example.org/v1"})
    assert describe(t, remote, OLLAMA) == "ai_local_remote_warning\nai_local_cloud_model_warning"


# ── IA-01: OK says the warning aloud, once, only for something new ───────────

def draft(model, url="http://localhost:11434/v1"):
    return {"models": {OLLAMA: model}, "local_urls": {OLLAMA: url}}


def test_ok_speaks_the_warning_of_a_new_remote_address():
    from ui.dialogs.ai_local_provider_dialog import spoken_warning
    t = lambda key: key
    remote = "https://llm.example.org/v1"
    assert spoken_warning(t, draft("m"), draft("m", remote), OLLAMA) == "ai_local_remote_warning"
    assert spoken_warning(t, draft("m", remote), draft("m", "https://other.example.org/v1"),
                          OLLAMA) == "ai_local_remote_warning"


def test_ok_is_silent_when_nothing_changed_or_nothing_leaves():
    from ui.dialogs.ai_local_provider_dialog import spoken_warning
    t = lambda key: key
    remote = "https://llm.example.org/v1"
    assert spoken_warning(t, draft("m", remote), draft("m2", remote), OLLAMA) == ""
    assert spoken_warning(t, draft("m"), draft("m2"), OLLAMA) == ""
    assert spoken_warning(t, draft("m", remote), draft("m"), OLLAMA) == ""


def test_ok_speaks_when_the_model_becomes_a_cloud_one():
    from ui.dialogs.ai_local_provider_dialog import spoken_warning
    t = lambda key: key
    assert spoken_warning(t, draft("m"), draft("m-cloud"), OLLAMA) == "ai_local_cloud_model_warning"
    assert spoken_warning(t, draft("a-cloud"), draft("b:cloud"), OLLAMA) == ""
