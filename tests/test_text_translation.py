"""Message translation without an online AI (issue #798): the languages, the
offline model catalogue and engine, the translation services and how the
settings and the conversation reach them. No network, no model, no window."""
import os
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core import message_translation as mt
from core.ai_credentials import TranslationCredentialStore
from core.ai_media import gtranslate
from core.ai_media.errors import DescriptionError
from core.message_translation import LOCAL, TranslationSettings
from core.text_translation import (
    languages, local_catalog, local_engine, local_store, local_text, services,
)
from core.transcription import errors as transcription_errors
from tests.locales import load_strings


# ── languages ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("value", "code"), [
    ("português (Brasil)", "pt"), ("Portuguese", "pt"), ("pt", "pt"), ("  POLSKI ", "pl"),
    ("Português (Brasil)", "pt"), ("English (United States)", "en"),
    ("Português (Portugal)", "pt-PT"), ("中文（繁體）", "zh-TW"), ("Klingon", None), ("", None),
])
def test_a_saved_language_resolves_to_its_code(value, code):
    names = {"pt-BR": "Português (Brasil)", "en-US": "English (United States)",
             "pt-PT": "Português (Portugal)"}
    assert languages.language_code(value, names) == code


def test_every_engine_code_has_the_shape_its_model_expects():
    assert all(re.fullmatch(r"[a-z]{3}_[A-Z][a-z]{3}", code) for code in languages.NLLB.values())
    assert languages.m2m100_code("nb") == "no" and languages.m2m100_code("zh-TW") is None
    assert languages.madlad_code("zh-TW") == "zh_Hant" and languages.madlad_code("tl") == "fil"
    assert languages.mbart50_code("pt-PT") == "pt_XX" and languages.mbart50_code("eo") is None
    assert len(languages.MBART50_TOKENS) == 53  # 52 languages and pt-PT's alias
    assert len(set(languages.CODES)) == len(languages.CODES)


def test_the_picker_puts_the_interface_language_first_and_filters():
    rows = languages.choices(lambda code: code in ("de", "pt", "en"), "pt-BR")
    assert [code for code, _name in rows] == ["pt", "de", "en"]
    assert languages.detected_code("no") == "nb" and languages.detected_code("xx") is None


# ── catalogue ────────────────────────────────────────────────────────────────

def test_every_model_is_pinned():
    ids = [model.id for model in local_catalog.MODELS]
    assert len(ids) == len(set(ids)) and local_catalog.DEFAULT_MODEL_ID in ids
    for model in local_catalog.MODELS:
        assert re.fullmatch(r"[0-9a-f]{40}", model.revision), model.id
        names = [name for name, _size in model.files]
        assert {"config.json", "model.bin", model.tokenizer_file} <= set(names), model.id
        assert re.fullmatch(r"[0-9a-f]{64}", model.sha256_of("model.bin")), model.id
        assert re.fullmatch(r"[0-9a-f]{64}", model.sha256_of(model.tokenizer_file)), model.id
        assert all(size > 0 for _name, size in model.files)
        assert model.download_bytes == sum(size for _name, size in model.files)


def test_the_list_runs_from_the_smallest_download_to_the_largest():
    sizes = [model.download_bytes for model in local_catalog.MODELS]
    assert sizes == sorted(sizes)


# ── offline engine (pure parts) ──────────────────────────────────────────────

def test_segments_keep_the_line_breaks_and_cut_long_lines_at_sentences():
    text = "Oi!\n\n" + "Frase um. " * 60 + "Fim.\nTchau"
    parts = local_engine.segments(text)
    assert "".join(piece + sep for piece, sep in parts) == text
    assert all(len(piece) <= local_engine.MAX_SEGMENT_CHARS for piece, _sep in parts)
    assert parts[1] == ("", "\n")


@pytest.mark.parametrize(("model_id", "expected"), [
    ("nllb-200-distilled-600m-int8", (["por_Latn"], ["</s>"], ["eng_Latn"])),
    ("m2m100-418m-int8", (["__pt__"], ["</s>"], ["__en__"])),
    ("mbart-50-int8", (["pt_XX"], ["</s>"], ["en_XX"])),
    ("small100-int8", (["__en__"], ["</s>"], None)),
    ("madlad400-3b-int8", (["▁", "<2en>"], ["</s>"], None)),
])
def test_each_family_frames_the_tokens_its_own_way(model_id, expected):
    model = local_catalog.get_model(model_id)
    assert local_engine.language_tokens(model, "pt", "en") == expected
    assert local_engine.strip_target_prefix(["eng_Latn", "▁Hi"], ["eng_Latn"]) == ["▁Hi"]


def test_the_source_is_detected_only_for_the_families_that_need_it():
    nllb = local_catalog.get_model("nllb-200-distilled-600m-int8")
    small = local_catalog.get_model("small100-int8")
    portuguese = lambda text: ("pt", -10.0)  # noqa: E731
    assert local_engine.resolve_source(nllb, "Oi", None, "pt-PT", portuguese) == ("pt", True)
    assert local_engine.resolve_source(nllb, "Oi", None, "en", portuguese) == ("pt", False)
    assert local_engine.resolve_source(nllb, "x", None, "en", lambda t: ("vo", 0)) == ("en", False)
    assert local_engine.resolve_source(nllb, "Oi", "es", "en", portuguese) == ("es", False)
    assert local_engine.resolve_source(small, "Oi", None, "en", None) == (None, False)


def test_missing_packages_are_named_not_imported():
    model = local_catalog.get_model("m2m100-418m-int8")
    absent = lambda name: None if name in ("sentencepiece", "py3langid") else object()  # noqa: E731
    assert local_engine.missing_components(model, True, absent) == ("sentencepiece", "py3langid")
    assert local_engine.missing_components(model, False, absent) == ("sentencepiece",)


def test_a_model_not_on_disk_is_said_before_anything_loads(tmp_path):
    with pytest.raises(DescriptionError) as raised:
        local_engine.translate("small100-int8", "Oi", "en", root=str(tmp_path))
    assert raised.value.key == "ai_error_translation_model_missing"


def _install(root, model):
    folder = root / model.id
    folder.mkdir()
    for name, size in model.files:
        (folder / name).write_bytes(b"\0" * size if size < 10_000_000 else b"")
    return folder


def test_translate_batches_the_lines_and_puts_the_text_back_together(tmp_path, monkeypatch):
    model = local_catalog.get_model("small100-int8")
    monkeypatch.setattr(local_store, "is_installed", lambda entry, root=None: True)
    monkeypatch.setattr(local_engine, "missing_components", lambda *a, **k: ())
    seen = []

    class Tokenizer:
        def encode(self, text):
            return text.split()

        def decode(self, tokens):
            return " ".join(tokens).upper()

    class Translator:
        def translate_batch(self, sources, target_prefix, **options):
            seen.append((sources, target_prefix))
            return [SimpleNamespace(hypotheses=[tokens[1:-1]]) for tokens in sources]

    monkeypatch.setattr(local_engine, "_open",
                        lambda entry, directory, device: (Translator(), Tokenizer(), "cpu"))
    try:
        answer = local_engine.translate(model.id, "bom dia\n\n  até logo", "en", root=str(tmp_path))
    finally:
        local_engine.unload()
    assert answer == "BOM DIA\n\n  ATÉ LOGO"
    assert seen == [([["__en__", "bom", "dia", "</s>"], ["__en__", "até", "logo", "</s>"]], None)]


def test_a_model_that_will_not_load_is_its_own_pausing_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store, "is_installed", lambda entry, root=None: True)
    monkeypatch.setattr(local_engine, "missing_components", lambda *a, **k: ())
    attempts = []

    def refuse(*args):
        attempts.append(1)
        raise MemoryError
    monkeypatch.setattr(local_engine, "_open", refuse)
    with pytest.raises(DescriptionError) as raised:
        local_engine.translate("small100-int8", "oi", "en", root=str(tmp_path))
    assert raised.value.key == "ai_error_translation_local_load"
    assert mt.pauses_automatic(raised.value.key)
    assert mt.pauses_automatic("ai_error_translation_engine_missing")
    assert not mt.pauses_automatic("ai_error_translation_local_failed")


def test_a_missing_detector_is_an_engine_error_not_english():
    def absent(text):
        raise ImportError("py3langid")
    with pytest.raises(DescriptionError) as raised:
        local_engine.detect("Oi", absent)
    assert raised.value.key == "ai_error_translation_engine_missing"

    def odd(text):
        raise ValueError("bad")
    assert local_engine.detect("Oi", odd) is None


def test_an_idle_timer_that_was_replaced_does_not_drop_the_model(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(local_engine, "_loaded", sentinel)
    monkeypatch.setattr(local_engine, "_idle_timer", SimpleNamespace(cancel=lambda: None))
    local_engine._unload_if_idle(object())          # a stale timer
    assert local_engine._loaded is sentinel
    current = local_engine._idle_timer
    local_engine._unload_if_idle(current)
    assert local_engine._loaded is None


def test_a_cancel_between_batches_stops_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store, "is_installed", lambda entry, root=None: True)
    monkeypatch.setattr(local_engine, "missing_components", lambda *a, **k: ())
    monkeypatch.setattr(local_engine, "_open", lambda *a: (object(), object(), "cpu"))
    try:
        with pytest.raises(DescriptionError) as raised:
            local_engine.translate("small100-int8", "oi", "en", root=str(tmp_path),
                                   should_cancel=lambda: True)
    finally:
        local_engine.unload()
    assert raised.value.key == "ai_error_cancelled"


def test_the_store_reads_the_catalogue_sizes(tmp_path):
    model = local_catalog.get_model("small100-int8")
    assert local_store.state(model, str(tmp_path)) == local_store.STATE_ABSENT
    folder = tmp_path / model.id
    folder.mkdir()
    (folder / "config.json").write_bytes(b"\0" * 233)
    assert local_store.state(model, str(tmp_path)) == local_store.STATE_INCOMPLETE


def test_the_job_downloads_through_the_store(monkeypatch):
    model = local_catalog.get_model("small100-int8")
    calls = []
    monkeypatch.setattr(local_store, "download",
                        lambda entry, root, progress, should_cancel, session: calls.append(entry.id))
    job = local_store.TranslationModelJob(local_store.ACTION_DOWNLOAD, model, root="r")
    job._perform()
    assert calls == ["small100-int8"]


# ── what the settings say ────────────────────────────────────────────────────

@pytest.fixture
def english():
    strings = load_strings("en-US")
    return SimpleNamespace(t=lambda key: strings[key], language="en-US")


def test_a_model_item_says_size_licence_and_state(english):
    model = local_catalog.get_model("nllb-200-distilled-600m-int8")
    label = local_text.model_label(english, model, local_store.STATE_ABSENT)
    assert label.startswith("NLLB-200 distilled 600M (Meta, CC BY-NC 4.0): 200 languages")
    assert "MB download, not installed" in label
    assert "Hugging Face" in local_text.download_confirmation(english, model)


def test_a_failed_download_does_not_speak_of_transcription(english):
    model = local_catalog.get_model("small100-int8")
    failure = transcription_errors.TranscriptionError(transcription_errors.MODEL_DOWNLOAD_FAILED)
    text = local_text.outcome_text(english, local_store.ACTION_DOWNLOAD, model, None, failure)
    assert "translation model" in text
    done = local_text.outcome_text(english, local_store.ACTION_DOWNLOAD, model, "dir", None)
    assert "SMaLL-100" in done


# ── services ─────────────────────────────────────────────────────────────────

def test_libretranslate_takes_an_optional_key_and_a_local_address():
    request, parse = services.build_request(services.LIBRETRANSLATE, "", "Oi", "en",
                                            base_url="http://192.168.0.5:5000/")
    assert request.url == "http://192.168.0.5:5000/translate"
    assert request.json == {"q": "Oi", "source": "auto", "target": "en", "format": "text"}
    assert parse({"translatedText": "Hi"}) == "Hi"
    request, _ = services.build_request(services.LIBRETRANSLATE, "k", "Oi", "zh", "pt",
                                        base_url="https://lt.example.org")
    assert request.json["api_key"] == "k" and request.json["target"] == "zh-Hans"


@pytest.mark.parametrize("address", ["http://lt.example.org", "ftp://x", "https://u:p@x.org",
                                     "http://8.8.8.8:5000", "http://[2001:4860:4860::8888]:5000",
                                     "http://[2606:4700::1111]:5000", "http://134744072",
                                     "http://0x08080808", ""])
def test_a_key_never_crosses_the_internet_in_clear(address):
    with pytest.raises(DescriptionError):
        services.service_base(address)


@pytest.mark.parametrize("address", ["http://localhost:5000", "http://servidor:5000",
                                     "http://nas.local", "https://lt.example.org/api/",
                                     "http://[::1]:5000", "http://[fd00::5]:5000",
                                     "http://192.168.0.7:5000"])
def test_a_local_or_https_address_is_accepted(address):
    assert services.service_base(address).startswith(("http://", "https://"))


def test_deepl_picks_the_free_host_and_its_regional_targets():
    request, parse = services.build_request(services.DEEPL, "abc:fx", "Hi", "pt", "en")
    assert request.url == "https://api-free.deepl.com/v2/translate"
    assert request.headers == {"Authorization": "DeepL-Auth-Key abc:fx"}
    assert request.json == {"text": ["Hi"], "target_lang": "PT-BR", "source_lang": "EN"}
    assert parse({"translations": [{"text": "Oi"}]}) == "Oi"
    request, _ = services.build_request(services.DEEPL, "abc", "Hi", "zh-TW")
    assert request.url.startswith("https://api.deepl.com") and request.json["target_lang"] == "ZH-HANT"


def test_google_cloud_and_microsoft_requests():
    request, parse = services.build_request(services.GOOGLE_CLOUD, "g", "Hi", "zh")
    assert request.headers == {"X-goog-api-key": "g"} and request.json["target"] == "zh-CN"
    assert parse({"data": {"translations": [{"translatedText": "你好"}]}}) == "你好"
    request, parse = services.build_request(services.MICROSOFT, "m", "Hi", "pt-PT", "en",
                                            region="westeurope")
    assert request.url.endswith("&to=pt-pt&from=en")
    assert request.headers["Ocp-Apim-Subscription-Region"] == "westeurope"
    assert request.json == [{"Text": "Hi"}]
    assert parse([{"translations": [{"text": "Olá"}]}]) == "Olá"
    with pytest.raises(DescriptionError):
        services.build_request(services.MICROSOFT, "m", "Hi", "pt", region="West Europe!")


def test_a_paid_service_without_a_key_and_an_empty_answer_are_refused():
    with pytest.raises(DescriptionError) as raised:
        services.build_request(services.DEEPL, "", "Hi", "pt")
    assert raised.value.key == "ai_error_credentials"
    _, parse = services.build_request(services.DEEPL, "k", "Hi", "pt")
    with pytest.raises(DescriptionError):
        parse({"translations": [{"text": "  "}]})


def test_google_translate_reuses_the_web_endpoint_for_any_language(monkeypatch):
    calls = []

    def fake(text, target, source, cancel_check=None, any_target=False):
        calls.append((text, target, source, any_target))
        return "Oi", "en"

    monkeypatch.setattr(gtranslate, "translate_text", fake)
    token = SimpleNamespace(check=lambda: None)
    assert services.translate(services.GOOGLE_WEB, "", "Hi", "jv", token) == "Oi"
    assert calls == [("Hi", "jw", "auto", True)]


def test_the_web_endpoint_takes_any_well_formed_code_only_when_asked():
    session = SimpleNamespace(get=None)
    with pytest.raises(DescriptionError):
        gtranslate.translate_text("oi", "ceb", session_factory=lambda: session)
    with pytest.raises(DescriptionError):
        gtranslate.translate_text("oi", "ce b", session_factory=lambda: session, any_target=True)


# ── message_translation ──────────────────────────────────────────────────────

def test_saved_settings_fill_the_defaults_of_the_new_engines():
    local = mt.settings_from({"provider": LOCAL, "device": "tpu", "region": "x y"})
    assert local.model == local_catalog.DEFAULT_MODEL_ID and local.device == "cpu"
    assert local.region == ""
    assert mt.settings_from({"provider": "ollama"}).base_url == "http://localhost:11434/v1"
    assert mt.settings_from({"provider": "libretranslate"}).base_url == "http://localhost:5000"
    saved = mt.settings_from({"provider": "microsoft", "region": "eastus",
                              "source_language": "español", "device": "cuda"})
    assert (saved.region, saved.source_language, saved.device) == ("eastus", "español", "cuda")


def test_what_each_engine_needs_and_sends():
    assert not mt.uses_key(LOCAL) and not mt.uses_key("google_web")
    assert mt.needs_key("deepl") and not mt.needs_key("libretranslate")
    assert not mt.needs_key("ollama") and mt.needs_key("openai")
    assert not mt.sends_text_out(LOCAL) and mt.sends_text_out("libretranslate")
    assert not mt.is_ai(LOCAL) and not mt.is_ai("deepl") and mt.is_ai("lm_studio")
    local = TranslationSettings(LOCAL, local_catalog.DEFAULT_MODEL_ID, "English")
    assert not mt.counts_against_limit(local, "")
    assert mt.counts_against_limit(replace(local, provider="google_web"), "")
    assert mt.not_ready_key(local) == "translation_local_download_required"
    assert mt.not_ready_key(replace(local, provider="openai")) == "translation_test_required"


def test_a_service_key_has_a_vault_and_libretranslate_is_bound_to_its_address():
    for provider in ("deepl", "google_cloud", "microsoft"):
        assert TranslationCredentialStore._accepts(provider)
    config = TranslationSettings("libretranslate", "", "English", "https://a.example.org")
    identity = mt.credential_id(config)
    assert TranslationCredentialStore._accepts(identity)
    assert identity != mt.credential_id(replace(config, base_url="https://b.example.org"))


def test_the_settings_check_names_the_language_problem():
    config = TranslationSettings(LOCAL, "small100-int8", "Esperanto")
    with pytest.raises(DescriptionError) as raised:
        mt.check_request(config, "", "Hello")
    assert raised.value.key == "ai_error_translation_language"
    mt.check_request(replace(config, model="nllb-200-distilled-600m-int8"), "", "Hello")
    with pytest.raises(DescriptionError):
        mt.check_request(replace(config, language="Klingon", model="nllb-200-3.3b-int8"), "", "Hi")
    with pytest.raises(DescriptionError) as raised:
        mt.check_request(TranslationSettings("deepl", "", "English"), "", "Hello")
    assert raised.value.key == "ai_error_credentials"


def test_a_service_is_ready_and_the_offline_model_when_on_disk(monkeypatch):
    assert mt.translation_model_ready(TranslationSettings("google_web", "", "English"))
    local = TranslationSettings(LOCAL, "small100-int8", "English")
    monkeypatch.setattr(local_store, "is_installed", lambda model, root=None: False)
    assert not mt.translation_model_ready(local)
    monkeypatch.setattr(local_store, "is_installed", lambda model, root=None: model is not None)
    assert mt.translation_model_ready(local)


def test_translate_hands_each_engine_its_codes(monkeypatch):
    calls = []
    monkeypatch.setattr(local_engine, "translate",
                        lambda *args, **kwargs: calls.append(("local", args)) or "L")
    monkeypatch.setattr(services, "translate",
                        lambda *args: calls.append(("service", args)) or "S")
    token = SimpleNamespace(cancelled=SimpleNamespace(is_set=lambda: False))
    local = TranslationSettings(LOCAL, "nllb-200-distilled-600m-int8", "português (Brasil)",
                                source_language="English", device="cuda")
    assert mt.translate(local, "", "Hi", token) == "L"
    assert calls[0] == ("local", ("nllb-200-distilled-600m-int8", "Hi", "pt", "en", "cuda"))
    deepl = TranslationSettings("deepl", "", "Deutsch")
    assert mt.translate(deepl, "k", "Hi", token) == "S"
    assert calls[1][1][:5] == ("deepl", "k", "Hi", "de", token)


# ── the dialog's pure helpers ────────────────────────────────────────────────

def test_every_engine_is_offered_and_named():
    from ui.dialogs.translation_settings import ORDER, language_items, provider_label

    assert ORDER[0] == LOCAL and len(ORDER) == len(set(ORDER))
    assert set(mt.PROVIDER_NAMES) | {"custom_openai", "custom_anthropic", LOCAL} == set(ORDER)
    t = load_strings("pt-BR").get
    assert provider_label(t, LOCAL) == t("translation_provider_local")
    assert provider_label(t, "deepl") == "DeepL"
    assert "español" in language_items("deepl", None, "pt-BR")
    assert language_items("deepl", None, "pt-BR")[0] == "português (Brasil)"


# ── module boundaries and the live catalogue ─────────────────────────────────

def test_no_engine_package_is_imported_at_module_level():
    """A copy without ctranslate2, tokenizers, sentencepiece or py3langid (or
    whose DLLs will not load) must still open the conversation and settings."""
    import ast
    from pathlib import Path

    heavy = {"ctranslate2", "tokenizers", "sentencepiece", "py3langid"}
    folder = Path(__file__).resolve().parents[1] / "client" / "core" / "text_translation"
    for path in folder.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not {name.split(".")[0] for name in names} & heavy, path.name


_NETWORK_OPT_IN_ENV = "WINZAPP_RUN_NETWORK_TESTS"
_FILES = [(model, name, size) for model in local_catalog.MODELS for name, size in model.files]


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get(_NETWORK_OPT_IN_ENV, "").strip() in ("", "0", "false", "False"),
    reason=f"reaches huggingface.co - set {_NETWORK_OPT_IN_ENV}=1 to run it")
@pytest.mark.parametrize("model, name, size", _FILES,
                         ids=[f"{m.id}/{n}" for m, n, _s in _FILES])
def test_the_pinned_url_serves_exactly_the_catalogued_size(model, name, size):
    """A wrong repository, revision or file name looks fine offline; run this
    whenever the catalogue changes (TestTheCatalogueUrlsAreLive's twin)."""
    from core import tls_trust
    from core.transcription import model_store

    with tls_trust.create_session() as session:
        response = session.head(model_store.file_url(model, name), allow_redirects=True,
                                timeout=30, headers={"Accept-Encoding": "identity"})
    assert response.status_code == 200
    length = response.headers.get("x-linked-size") or response.headers.get("Content-Length")
    assert int(length) == size


def test_the_model_picker_is_refreshed_inside_freeze_and_thaw(monkeypatch):
    import inspect
    from ui.dialogs import translation_local_models as module
    from ui.dialogs.translation_settings import TranslationSettingsDialog

    events = []

    class Choice:
        Freeze = lambda self: events.append("freeze")  # noqa: E731
        Thaw = lambda self: events.append("thaw")  # noqa: E731

        def SetString(self, index, label):
            events.append(("set", index))

    button = SimpleNamespace(Enable=lambda state: None)
    stub = SimpleNamespace(
        local_model=Choice(), local_download=button, local_remove=button,
        _local_labels=lambda: ["a", "b"], _local_model=lambda: None)
    monkeypatch.setattr(module, "_state", lambda model: local_store.STATE_ABSENT)
    module.TranslationLocalModelsMixin._refresh_local(stub)
    assert events == ["freeze", ("set", 0), ("set", 1), "thaw"]
    # The dialog speaks through the window's speak_output: it is not optional.
    parameter = inspect.signature(TranslationSettingsDialog.__init__).parameters["speak_output"]
    assert parameter.default is inspect.Parameter.empty


def test_simplified_and_traditional_chinese_are_not_the_same_language():
    nllb = local_catalog.get_model("nllb-200-distilled-600m-int8")
    chinese = lambda text: ("zh", -5.0)  # noqa: E731
    assert local_engine.resolve_source(nllb, "你好", None, "zh-TW", chinese) == ("zh", False)
    assert local_engine.resolve_source(nllb, "你好", None, "zh", chinese) == ("zh", True)


def test_removing_a_model_can_stop_waiting_for_a_running_translation():
    import threading
    taken, release = threading.Event(), threading.Event()

    def hold():
        with local_engine._lock:
            taken.set()
            release.wait(5)
    worker = threading.Thread(target=hold)
    worker.start()
    taken.wait(5)
    try:
        assert local_engine.unload(lambda: True) is False
    finally:
        release.set()
        worker.join()
    assert local_engine.unload() is True
