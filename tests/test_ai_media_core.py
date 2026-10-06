"""Credentials, preferences, media preparation, sessions and transport, with synthetic data and no GUI/network."""
import base64
from io import BytesIO
import json
import threading

from cryptography.fernet import Fernet
from PIL import Image
import pytest
import requests

from app_settings import AppSettings
from core.ai_credentials import CredentialStore, CredentialError
from core.ai_media import config, image_input, service
from core.ai_media.errors import DescriptionError
from core.ai_media.image_input import prepare_image
from core.ai_media.payload import Media
from core.ai_media.session import MediaSession


def photo(size=(20, 30), format="PNG", exif=None):
    stream = BytesIO()
    Image.new("RGB", size, "red").save(stream, format=format, **({"exif": exif} if exif else {}))
    return stream.getvalue()


def test_credential_file_never_contains_plain_keys(tmp_path):
    store = CredentialStore(tmp_path)
    store.set("openai", "test-openai-secret")
    store.set("gemini", "test-gemini-secret")
    assert store.get("openai") == "test-openai-secret"
    assert store.get("gemini") == "test-gemini-secret"
    assert b"test-openai-secret" not in store.data_path.read_bytes()
    assert b"test-gemini-secret" not in store.data_path.read_bytes()
    store.set("openai", "")
    assert store.get("openai") == "test-openai-secret"
    store.delete("openai")
    assert not store.get("openai")
    assert store.get("gemini") == "test-gemini-secret"


def test_invalid_second_draft_cannot_partially_update_keys(tmp_path):
    store = CredentialStore(tmp_path)
    store.set("openai", "original")
    with pytest.raises(CredentialError):
        store.apply({"openai": "replacement", "gemini": "invalid key"})
    assert store.get("openai") == "original"
    assert not store.get("gemini")


@pytest.mark.parametrize("damage", ["missing_key", "bad_key", "bad_data"])
def test_corrupt_store_fails_closed_without_overwriting(tmp_path, damage):
    store = CredentialStore(tmp_path)
    store.set("openai", "secret")
    if damage == "missing_key":
        store.key_path.unlink()
    elif damage == "bad_key":
        store.key_path.write_bytes(Fernet.generate_key())
    else:
        store.data_path.write_bytes(b"broken")
    before = store.data_path.read_bytes()
    with pytest.raises(CredentialError):
        store.get("openai")
    with pytest.raises(CredentialError):
        store.set("gemini", "new-secret")
    assert store.data_path.read_bytes() == before
    store.reset()
    store.set("gemini", "replacement")
    assert store.get("gemini") == "replacement"
    assert not store.get("openai")


def test_failed_atomic_replace_keeps_old_ciphertext(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path)
    store.set("openai", "old")
    import core.ai_credentials as module
    monkeypatch.setattr(module.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("failure")))
    with pytest.raises(CredentialError):
        store.set("openai", "new")
    assert store.get("openai") == "old"
    assert not list(tmp_path.glob("*.tmp"))


def test_cross_instance_writes_preserve_other_provider(tmp_path):
    errors = []
    def save(provider):
        try:
            CredentialStore(tmp_path).set(provider, provider + "-secret")
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=save, args=(p,)) for p in config.PROVIDERS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert not errors
    store = CredentialStore(tmp_path)
    for provider in config.PROVIDERS:
        assert store.get(provider) == provider + "-secret"


def test_every_provider_has_a_slot_and_unknown_ones_are_refused(tmp_path):
    store = CredentialStore(tmp_path)
    for provider in config.PROVIDERS:
        store.set(provider, "secret-" + provider)
    assert [bool(store.get(p)) for p in config.PROVIDERS] == [True] * len(config.PROVIDERS)
    for blob in (b"secret-claude", b"secret-groq", b"secret-openrouter"):
        assert blob not in store.data_path.read_bytes()
    with pytest.raises(CredentialError):
        store.set("unknown", "secret")


@pytest.mark.parametrize("value", ["bad key", "x" * 4097, "line\nbreak"])
def test_invalid_key_is_not_written(tmp_path, value):
    with pytest.raises(CredentialError):
        CredentialStore(tmp_path).set("openai", value)
    assert not (tmp_path / "ai_credentials.enc").exists()


def test_global_preferences_are_opt_in_and_defaults_are_complete(tmp_path):
    p = config.preferences(AppSettings(str(tmp_path)))
    assert p["enabled"] is False
    assert p["order"] == list(config.PROVIDERS) and p["disabled"] == []
    assert p["consented"] == [] and all(p["kinds"].values())
    assert p["models"] == {name: spec.model for name, spec in config.PROVIDERS.items()}
    assert p["auto_models"] == sorted(config.PROVIDERS)  # no model saved: all automatic


def test_order_disabled_and_consent_are_validated_and_persisted(tmp_path):
    app = AppSettings(str(tmp_path))
    app.set("ai_media", {"enabled": True, "order": ["groq", "bogus", "gemini", "groq"],
                         "disabled": ["openai", "bogus"], "consented": ["openai", "bogus"],
                         "kinds": {"audio": False, "video": "yes"},
                         "models": {"openai": "gpt-4o", "groq": "../escape"}})
    p = config.preferences(app)
    assert p["order"][:2] == ["groq", "gemini"]  # unknown and repeated ids dropped
    assert set(p["order"]) == set(config.PROVIDERS)  # newcomers appended
    assert p["disabled"] == ["openai"] and p["consented"] == ["openai"]
    assert p["kinds"]["audio"] is False and p["kinds"]["video"] is True
    assert p["models"]["openai"] == "gpt-4o"
    assert p["models"]["groq"] == config.PROVIDERS["groq"].model
    assert "openai" not in p["auto_models"] and "groq" in p["auto_models"]
    app.update("ai_media", lambda old: {**old, "consented": ["openai", "gemini"]})
    assert config.preferences(AppSettings(str(tmp_path)))["consented"] == ["gemini", "openai"]


@pytest.mark.parametrize("raw", [None, [], "broken", {"order": 3, "disabled": "x", "models": 7, "kinds": []}])
def test_corrupt_preferences_fall_back_safely(tmp_path, raw):
    app = AppSettings(str(tmp_path))
    app.set("ai_media", raw)
    p = config.preferences(app)
    assert p["enabled"] is False and p["order"] == list(config.PROVIDERS)


def test_automatic_reading_defaults_on_without_overriding_a_saved_choice(tmp_path):
    app = AppSettings(str(tmp_path))
    assert config.preferences(app)["read_answers"] is True
    app.set("ai_media", {"read_answers": False})
    assert config.preferences(app)["read_answers"] is False
    app.set("ai_media", {"read_answers": True})
    assert config.preferences(app)["read_answers"] is True


def test_the_chain_follows_the_order_skips_what_cannot_serve_and_prefers_the_first_answerer():
    saved = {"order": ["groq", "claude", "gemini", "openai"], "disabled": ["claude"]}
    p = config.preferences(type("App", (), {"get": staticmethod(lambda key: saved)})())
    has_key = lambda provider: provider in ("groq", "gemini", "claude")  # a key is saved for these only
    assert config.chain(p, "image", has_key) == ["groq", "gemini"]
    assert config.chain(p, "audio", has_key) == ["groq", "gemini"]  # claude cannot hear anyway
    assert config.chain(p, "video", has_key) == ["gemini"]  # only Gemini takes video
    assert config.chain(p, "pdf", has_key) == ["gemini"]  # Groq takes no PDF
    assert config.chain(p, "image", has_key, prefer="gemini") == ["gemini", "groq"]
    assert config.chain(p, "image", lambda provider: False) == []


def test_every_kind_has_a_provider_and_only_pictures_and_videos_take_questions():
    for kind in config.KINDS:
        assert any(config.supports(name, kind) for name in config.PROVIDERS)
    assert [k for k in config.KINDS if config.asks_questions(k)] == ["image", "sticker", "video"]


def test_exif_orientation_is_applied_and_metadata_removed():
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private caption"
    prepared = prepare_image(photo(format="JPEG", exif=exif))
    assert (prepared.width, prepared.height) == (30, 20)
    with Image.open(BytesIO(prepared.data)) as decoded:
        assert not decoded.getexif()
        assert decoded.format == "JPEG"
    assert b"private caption" not in prepared.data


def test_alpha_is_composited_and_pixels_are_bounded():
    data = BytesIO()
    Image.new("RGBA", (3200, 20), (0, 0, 0, 0)).save(data, format="PNG")
    prepared = prepare_image(data.getvalue(), "fast")
    assert prepared.width == 2048
    with Image.open(BytesIO(prepared.data)) as decoded:
        assert decoded.getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize("data", [b"garbage", b"<svg></svg>", b""])
def test_invalid_input_is_refused(data):
    with pytest.raises(DescriptionError):
        prepare_image(data)


def test_pixel_and_byte_limits_are_checked_before_decode(monkeypatch):
    monkeypatch.setattr(image_input, "MAX_PIXELS", 100)
    with pytest.raises(DescriptionError, match="ai_error_media_size"):
        prepare_image(photo())
    monkeypatch.setattr(image_input, "MAX_SOURCE_BYTES", {"image": 1})
    with pytest.raises(DescriptionError, match="ai_error_media_size"):
        prepare_image(b"too big")


def _animation():
    data = BytesIO()
    first = Image.new("RGB", (10, 10), "red")
    first.save(data, format="PNG", save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")])
    return data.getvalue()


def test_animation_is_not_silently_described_as_a_photo():
    with pytest.raises(DescriptionError, match="ai_error_media_format"):
        prepare_image(_animation())


def test_a_sticker_is_described_from_its_first_frame():
    prepared = prepare_image(_animation(), first_frame=True)
    assert (prepared.width, prepared.height) == (10, 10)


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self.data = data or b'{"output":[{"type":"message","content":[{"type":"output_text","text":"answer"}]}]}'
        self.closed = False
        self.headers = {}
        self.raw = self
        self.position = 0

    def read1(self, amount, decode_content=True):
        chunk = self.data[self.position:self.position + amount]
        self.position += len(chunk)
        return chunk

    def shutdown(self):
        self.closed = True

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        yield self.data


class FakeHTTP:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response

    get = post


def request(http, token=None):
    return service.request_answer("openai", "gpt-4.1-mini", "secret", Media("image", b"photo", "image/jpeg", "image.jpg"),
                                  (), "question", "instructions", "balanced", token or service.RequestToken(),
                                  session_factory=lambda: http)


def test_transport_disables_redirects_and_closes_response():
    http = FakeHTTP()
    assert request(http) == "answer"
    assert http.response.closed
    assert http.calls[0][1]["allow_redirects"] is False
    assert http.calls[0][1]["stream"] is True
    timeout = http.calls[0][1]["timeout"]
    assert timeout.connect_timeout == 8
    assert 59 <= timeout.total <= 60


@pytest.mark.parametrize("status,category", [(401, "authentication"), (403, "authentication"),
                                            (429, "quota"), (503, "server"), (302, "request"), (400, "request")])
def test_http_errors_are_safe_and_not_retried(status, category):
    http = FakeHTTP(FakeResponse(status=status, data=b"private provider body with key"))
    with pytest.raises(DescriptionError, match=f"^ai_error_{category}$"):
        request(http)
    assert len(http.calls) == 1 and http.response.closed


@pytest.mark.parametrize("error,category", [(requests.Timeout("private"), "timeout"),
                                           (requests.ConnectionError("private"), "network")])
def test_network_errors_do_not_disclose_messages_or_retry(error, category):
    http = FakeHTTP(error=error)
    with pytest.raises(DescriptionError, match=f"^ai_error_{category}$"):
        request(http)
    assert len(http.calls) == 1


def test_cancel_before_send_makes_no_network_call():
    token = service.RequestToken()
    token.cancel()
    http = FakeHTTP()
    with pytest.raises(DescriptionError, match="ai_error_cancelled"):
        request(http, token)
    assert not http.calls


def test_deadline_during_read_discards_response():
    token = service.RequestToken(clock=lambda: 0)
    class Late(FakeResponse):
        def read1(self, amount, decode_content=True):
            token.clock = lambda: 61
            return super().read1(amount, decode_content)
    http = FakeHTTP(Late())
    with pytest.raises(DescriptionError, match="ai_error_timeout"):
        request(http, token)
    assert http.response.closed


def test_oversized_response_is_bounded():
    with pytest.raises(DescriptionError, match="ai_error_response"):
        request(FakeHTTP(FakeResponse(data=b"x" * (config.MAX_RESPONSE_BYTES + 1))))


@pytest.mark.parametrize("provider", list(config.PROVIDERS))
def test_connection_test_sends_no_media_and_no_generation(provider):
    http = FakeHTTP()
    assert service.probe_connection(provider, config.PROVIDERS[provider].model, "secret",
                                    service.RequestToken(), session_factory=lambda: http)
    url, kwargs = http.calls[0]
    assert "secret" not in url and "json" not in kwargs and "data" not in kwargs


def test_connection_test_refuses_a_missing_key_and_a_model_that_is_an_endpoint():
    for model, key in (("../x", "secret"), (config.PROVIDERS["openai"].model, "")):
        with pytest.raises(DescriptionError):
            service.probe_connection("openai", model, key, service.RequestToken(),
                                     session_factory=lambda: FakeHTTP())


def test_session_rejects_duplicate_and_stale_completions_and_clears_data():
    s = MediaSession("account", "chat", "message", "image", locked=True)
    generation, token = s.begin("question")
    with pytest.raises(DescriptionError, match="ai_error_busy"):
        s.begin("duplicate")
    s.cancel()
    assert token.cancelled.is_set()
    assert not s.accept(generation, "question", "late")
    new, _ = s.begin("second")
    assert s.accept(new, "second", "answer")
    s.media = Media("image", b"private", "image/jpeg", "image.jpg")
    s.close()
    assert s.media is None and s.history == []
    assert not s.accept(new, "second", "late")


def test_session_limits_cannot_be_bypassed_by_cancelling():
    s = MediaSession("a", "c", "m", "image")
    for _ in range(config.MAX_REQUESTS):
        s.begin("question")
        s.cancel()
    with pytest.raises(DescriptionError, match="ai_error_limit"):
        s.begin("question")


def test_private_results_do_not_mix_between_accounts():
    first = MediaSession("first", "chat", "message", "image")
    second = MediaSession("second", "chat", "message", "image")
    generation, _ = first.begin("question")
    first.accept(generation, "question", "private answer")
    assert not second.history and first.identity != second.identity


def message(kind, **inner):
    return {"messageType": kind, "key": {"id": "message"}, "message": {kind: inner}}


@pytest.mark.parametrize("kind,expected", [("imageMessage", "image"), ("stickerMessage", "sticker"),
                                           ("videoMessage", "video"), ("audioMessage", "audio"),
                                           ("viewOnceUnavailableMessage", None), ("conversation", None)])
def test_each_message_type_maps_to_one_kind(kind, expected):
    assert config.eligible_kind(message(kind)) == expected


@pytest.mark.parametrize("kind", ["imageMessage", "videoMessage", "audioMessage"])
def test_view_once_media_is_never_offered(kind):
    assert config.eligible_kind(message(kind, viewOnce=True)) is None
    assert config.eligible_kind(message(kind, isViewOnce=True)) is None


@pytest.mark.parametrize("mime,expected", [("application/pdf", "pdf"), ("application/pdf; x=1", "pdf"),
                                           ("APPLICATION/PDF", "pdf"), ("application/zip", None), ("", None)])
def test_only_pdf_documents_are_offered(mime, expected):
    assert config.eligible_kind(message("documentMessage", mimetype=mime)) == expected


@pytest.mark.parametrize("msg_id", ["../outside", "dir\\file", "\x00bad", 123, None, ""])
def test_media_identity_cannot_escape_media_cache(msg_id):
    msg = message("imageMessage")
    msg["key"]["id"] = msg_id
    assert config.eligible_kind(msg) is None


def test_worker_queue_is_bounded_and_slot_is_released_on_completion(monkeypatch):
    pending, completed = [], []
    class Executor:
        def submit(self, work):
            pending.append(work)
    monkeypatch.setattr(service, "_executor", Executor())
    monkeypatch.setattr(service, "_slots", threading.BoundedSemaphore(2))
    service.submit(lambda: "first", lambda *args: completed.append(args))
    service.submit(lambda: "second", lambda *args: completed.append(args))
    with pytest.raises(DescriptionError, match="ai_error_busy"):
        service.submit(lambda: "third", lambda *args: None)
    pending.pop(0)()
    assert completed == [("first", None)]
    service.submit(lambda: "third", lambda *args: None)
    for work in pending:
        work()


def test_cancel_shuts_down_socket_off_caller_thread():
    token = service.RequestToken()
    called = threading.Event()
    caller = threading.get_ident()
    threads = []
    class Raw:
        def shutdown(self):
            threads.append(threading.get_ident())
            called.set()
    from types import SimpleNamespace
    token.attach(SimpleNamespace(raw=Raw()))
    token.cancel()
    assert called.wait(timeout=2)
    assert threads and threads[0] != caller


@pytest.mark.parametrize("json_body", [False, True])
def test_bounded_media_works_with_both_server_versions(json_body):
    from core.ai_media.media_input import fetch_bounded_media
    body = json.dumps({"base64": base64.b64encode(b"photo").decode()}).encode() if json_body else b"photo"
    response = FakeResponse(data=body)
    response.headers["Content-Type"] = "application/json" if json_body else "application/octet-stream"
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs)
        return response
    assert fetch_bounded_media("url", {}, {}, 100, None, 15, post) == b"photo"
    assert response.closed and len(calls) == 1
    assert calls[0]["stream"] and calls[0]["allow_redirects"] is False


def test_media_limit_applies_even_when_metadata_and_headers_understate_size():
    from core.ai_media.media_input import fetch_bounded_media
    response = FakeResponse(data=b"oversized")
    response.headers = {"Content-Type": "image/jpeg", "Content-Length": "1"}
    with pytest.raises(DescriptionError, match="ai_error_media_size"):
        fetch_bounded_media("url", {}, {}, 3, None, 15, lambda *a, **kw: response)
    assert response.closed


def test_cancelled_media_does_not_download():
    from core.ai_media.media_input import fetch_bounded_media
    token = service.RequestToken()
    token.cancel()
    calls = []
    with pytest.raises(DescriptionError, match="ai_error_cancelled"):
        fetch_bounded_media("url", {}, {}, 100, token.check, 15, lambda *a, **kw: calls.append(True))
    assert not calls


def test_an_unreadable_store_never_blocks_a_save_that_stages_nothing(tmp_path):
    store = CredentialStore(tmp_path)
    store.set("openai", "x")
    store.key_path.unlink()
    store.apply({})  # no change, no reset: not even read
    with pytest.raises(CredentialError):
        store.apply({"gemini": "new"})


def test_groq_skips_a_photo_over_its_documented_size_without_stopping_the_chain():
    from core.ai_media.providers import GROQ_IMAGE_BASE64_LIMIT, build_request
    big = Media("image", b"x" * (GROQ_IMAGE_BASE64_LIMIT * 3 // 4 + 10), "image/jpeg", "image.jpg")
    with pytest.raises(DescriptionError) as raised:
        build_request("groq", config.PROVIDERS["groq"].model, "k", big, (), "q", "i", "balanced")
    assert raised.value.category == "request"  # a category the chain moves past
    assert build_request("openrouter", config.PROVIDERS["openrouter"].model, "k", big, (), "q", "i", "balanced")
