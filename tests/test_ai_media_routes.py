"""Provider routes per kind of task (core/ai_media/routes.py and its window).

Synthetic only: no network, no sleeping, no window. Without a route every kind
behaves exactly as before; with one, only its providers are tried, in its
order, and every other rule of the chain and of consent still applies.
"""
from types import SimpleNamespace

import pytest

from core.ai_media import auto, config, routes, service
from tests.test_ai_media_retry_usage import Sequenced
from tests.test_ai_media_providers import IMAGE


def prefs(**ai_media):
    return config.preferences({"ai_media": ai_media})


def keys(*providers):
    return lambda provider: provider in providers


ALL_KEYS = keys("gemini", "openai", "claude", "groq", "openrouter", "mistral")


# -- validated -----------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "x", 3, [], ["openai"], {"image": "openai"},
                                 {"image": []}, {"image": [None, 3, {}]},
                                 {"image": ["nobody"]}, {"nothing": ["openai"]}])
def test_damaged_routes_are_no_routes(raw):
    assert routes.validated(raw) == {}


def test_validated_keeps_the_order_and_drops_unknown_repeated_and_unsuited_providers():
    saved = {"image": ["openai", "nobody", "gemini", "openai", 7],
             # Claude takes no audio: it cannot be in a voice route.
             "audio": ["claude", "groq", "mistral"],
             "writing": ["local_ollama"],
             "nothing": ["openai"]}
    assert routes.validated(saved) == {"image": ["openai", "gemini"],
                                       "audio": ["groq", "mistral"],
                                       "writing": ["local_ollama"]}


def test_every_task_has_a_name_in_the_catalog():
    assert set(routes.TASKS) == set(config.KINDS) | {"text", "writing"}
    assert set(routes.TASK_KEY) == set(routes.TASKS)


def test_preferences_carries_validated_routes_and_survives_garbage():
    assert prefs()["routes"] == {}
    assert prefs(routes="garbage")["routes"] == {}
    assert prefs(routes={"pdf": ["mistral", "x"]})["routes"] == {"pdf": ["mistral"]}
    # The rest of the preferences is untouched by a damaged value.
    assert prefs(routes=[1, 2])["order"] == list(config.PROVIDERS)


# -- chain: compatibility ------------------------------------------------------

@pytest.mark.parametrize("kind", ["image", "sticker", "video", "audio", "pdf", "text", "writing"])
def test_without_a_route_the_chain_is_the_general_order(kind):
    general = prefs(order=["groq", "mistral", "openai", "gemini"])
    without_key = {k: v for k, v in general.items() if k != "routes"}  # an older dict
    expected = [p for p in general["order"] if config.supports(p, kind) and ALL_KEYS(p)]
    assert config.chain(general, kind, ALL_KEYS) == expected
    assert config.chain(without_key, kind, ALL_KEYS) == expected


# -- chain: with routes --------------------------------------------------------

def test_a_route_replaces_the_general_order_for_its_kind_only():
    cfg = prefs(order=["gemini", "openai", "mistral"], routes={"pdf": ["mistral", "openai"]})
    assert config.chain(cfg, "pdf", ALL_KEYS) == ["mistral", "openai"]
    assert config.chain(cfg, "image", ALL_KEYS)[:3] == ["gemini", "openai", "mistral"]
    assert config.chain(cfg, "audio", ALL_KEYS)[:3] == ["gemini", "openai", "mistral"]


def test_a_route_is_a_selection_not_only_an_order():
    cfg = prefs(routes={"image": ["groq"]})
    assert config.chain(cfg, "image", ALL_KEYS) == ["groq"]


def test_a_route_still_drops_disabled_keyless_and_unsuited_providers():
    cfg = prefs(routes={"audio": ["gemini", "openai", "groq"]}, disabled=["openai"])
    assert config.chain(cfg, "audio", keys("gemini", "openai")) == ["gemini"]
    # A provider that is off or has no key is not tried even if the route names it.
    assert config.chain(cfg, "audio", keys("openai")) == []


def test_a_route_with_nothing_usable_does_not_fall_back_to_the_general_order():
    cfg = prefs(order=["gemini", "openai"], routes={"image": ["groq"]})
    # Only OpenAI has a key: the person left it out of the image route on purpose.
    assert config.chain(cfg, "image", keys("openai")) == []
    assert config.chain(cfg, "pdf", keys("openai")) == ["openai"]


def test_an_empty_or_fully_invalid_route_is_the_general_order():
    cfg = prefs(order=["gemini", "openai"], routes={"image": [], "pdf": ["nobody"]})
    assert config.chain(cfg, "image", ALL_KEYS)[:2] == ["gemini", "openai"]
    assert config.chain(cfg, "pdf", ALL_KEYS)[:2] == ["gemini", "openai"]


def test_prefer_still_moves_the_answering_provider_first_within_the_route():
    cfg = prefs(routes={"image": ["gemini", "openai", "groq"]})
    assert config.chain(cfg, "image", ALL_KEYS, prefer="groq") == ["groq", "gemini", "openai"]
    # A provider outside the route is never pulled in by ``prefer``.
    assert config.chain(cfg, "image", ALL_KEYS, prefer="claude") == ["gemini", "openai", "groq"]


def test_a_local_provider_in_a_route_needs_its_model_and_address():
    cfg = prefs(routes={"writing": ["local_ollama", "gemini"]})
    assert config.chain(cfg, "writing", keys("gemini")) == ["gemini"]
    ready = prefs(routes={"writing": ["local_ollama", "gemini"]}, models={"local_ollama": "llama3"})
    assert config.chain(ready, "writing", keys("gemini")) == ["local_ollama", "gemini"]


def test_offered_kind_follows_the_route():
    message = {"messageType": "imageMessage", "key": {"id": "A1"}}
    cfg = prefs(enabled=True, routes={"image": ["groq"]})
    assert config.offered_kind(message, cfg, {"openai"}) is None
    assert config.offered_kind(message, cfg, {"groq"}) == "image"


# -- consent -------------------------------------------------------------------

def test_the_consent_names_the_providers_of_the_kinds_own_route():
    cfg = prefs(order=["gemini", "openai"], routes={"pdf": ["mistral"]})
    chain = config.chain(cfg, "pdf", ALL_KEYS)
    assert config.outbound(cfg, chain) == ["mistral"]
    # The remembered consent of a provider outside the route does not cover it.
    cfg = prefs(order=["gemini"], routes={"pdf": ["mistral"]}, consented=["gemini"])
    assert [p for p in config.chain(cfg, "pdf", ALL_KEYS) if p not in cfg["consented"]] == ["mistral"]


def test_automatic_requests_reach_only_a_consented_provider_of_the_route():
    saved = {"gemini", "mistral", "groq"}
    cfg = prefs(order=["gemini", "mistral", "groq"], routes={"audio": ["mistral", "groq"]},
                consented=["gemini", "groq"])
    assert auto.usable_providers(cfg, "audio", saved) == ["groq"]
    # Another kind has no route: the general order and its consent, as before.
    assert auto.usable_providers(cfg, "image", saved) == ["gemini", "groq"]


def test_a_local_model_on_this_computer_in_a_route_needs_no_consent():
    cfg = prefs(routes={"image": ["local_ollama"]}, models={"local_ollama": "llava"})
    chain = config.chain(cfg, "image", lambda p: False)
    assert chain == ["local_ollama"]
    assert config.outbound(cfg, chain) == []
    assert auto.usable_providers(cfg, "image", set()) == ["local_ollama"]


def test_an_ollama_cloud_model_in_a_route_is_outbound_and_asks():
    cfg = prefs(routes={"writing": ["local_ollama"]}, models={"local_ollama": "gpt-oss:cloud"})
    chain = config.chain(cfg, "writing", lambda p: False)
    assert config.outbound(cfg, chain) == ["local_ollama"]
    assert auto.usable_providers(cfg, "image", set()) == []


# -- the chain a route builds runs --------------------------------------------

def test_run_chain_asks_the_routes_providers_in_the_routes_order():
    cfg = prefs(order=["gemini", "openai"], routes={"image": ["openai"]})
    chain = config.chain(cfg, "image", ALL_KEYS)
    http = Sequenced([(200, {})])
    operation = service.Operation("image")
    models = {p: config.PROVIDERS[p].model for p in config.PROVIDERS}
    _answer, provider = service.run_chain(operation, chain, models, lambda p: "key-" + p, IMAGE, (),
                                          "q", "i", "balanced", session_factory=lambda: http)
    assert provider == "openai"
    assert len(http.calls) == 1 and "openai" in http.calls[0][0]


# -- the window's pure parts ---------------------------------------------------

def test_candidates_list_the_route_first_then_the_rest_in_the_general_order():
    cfg = {"order": ["gemini", "openai", "groq", "claude"], "routes": {"audio": ["groq"]}}
    items, chosen = routes.candidates(cfg, "audio")
    assert items == ["groq", "gemini", "openai"] and chosen == ["groq"]  # claude takes no audio
    items, chosen = routes.candidates(cfg, "pdf")
    assert items == ["gemini", "openai", "claude"] and chosen == []
    assert routes.candidates(cfg, "pdf", route=["claude"])[0][0] == "claude"


def test_route_of_keeps_list_order():
    from ui.dialogs.ai_routes_options import route_of
    assert route_of(["a", "b", "c"], {"c", "a"}) == ["a", "c"]
    assert route_of(["a"], set()) == []


def test_row_text_says_position_and_readiness():
    from ui.dialogs.ai_routes_options import row_text
    t = {"ai_route_row_in": "in {position}", "ai_route_row_out": "out",
         "ai_route_not_ready": "no"}.__getitem__
    assert row_text(t, "Gemini", 2, True) == "Gemini, in 2"
    assert row_text(t, "Groq", None, True) == "Groq, out"
    assert row_text(t, "Groq", 1, False) == "Groq, in 1, no"


def test_the_panel_saves_only_edited_routes_and_keeps_untouched_ones():
    from ui.dialogs.ai_routes_options import AIRoutesPanel
    panel = SimpleNamespace(
        _saved={"pdf": ["mistral"], "audio": ["groq"]},
        _state={"image": {"items": ["openai", "gemini"], "in": {"gemini", "openai"}, "general": False},
                "audio": {"items": ["groq"], "in": {"groq"}, "general": True},
                "video": {"items": ["gemini"], "in": set(), "general": False}})
    # image: a new route; audio: switched back to the general order; video: empty;
    # pdf: never opened, kept as saved.
    assert AIRoutesPanel.routes(panel) == {"image": ["openai", "gemini"], "pdf": ["mistral"]}
