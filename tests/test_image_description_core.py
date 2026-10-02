"""Photo processing and REST contracts, with synthetic images and no GUI/network."""
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
from core.image_description import config, image_input, service
from core.image_description.errors import DescriptionError
from core.image_description.image_input import ImageInput, prepare_image
from core.image_description.prompts import instructions
from core.image_description.providers import build_request, parse_answer
from core.image_description.session import PhotoSession


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
    threads = [threading.Thread(target=save, args=(p,)) for p in ("openai", "gemini")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert not errors
    store = CredentialStore(tmp_path)
    assert store.get("openai") == "openai-secret"
    assert store.get("gemini") == "gemini-secret"


@pytest.mark.parametrize("value", ["bad key", "x" * 4097, "line\nbreak"])
def test_invalid_key_is_not_written(tmp_path, value):
    with pytest.raises(CredentialError):
        CredentialStore(tmp_path).set("openai", value)
    assert not (tmp_path / "ai_credentials.enc").exists()


def test_global_preferences_are_opt_in_and_provider_scoped(tmp_path):
    app = AppSettings(str(tmp_path))
    assert not config.preferences(app)["enabled"]
    app.set("image_description", {"enabled": True, "provider": "gemini", "consented": ["openai"],
                                  "models": {"openai": "gpt-4.1-mini"}})
    p = config.preferences(app)
    assert p["provider"] == "gemini" and not p["consented"]
    assert p["model"] == config.PROVIDERS["gemini"].model
    app.update("image_description", lambda old: {**old, "consented": ["openai", "gemini"]})
    assert config.preferences(AppSettings(str(tmp_path)))["consented"]


@pytest.mark.parametrize("raw", [None, [], "broken", {"provider": [], "profile": [], "models": 7}])
def test_corrupt_preferences_fall_back_safely(tmp_path, raw):
    app = AppSettings(str(tmp_path))
    app.set("image_description", raw)
    assert config.preferences(app)["provider"] == "openai"


def test_automatic_reading_defaults_on_without_overriding_a_saved_choice(tmp_path):
    app = AppSettings(str(tmp_path))
    assert config.preferences(app)["read_answers"] is True
    app.set("image_description", {"read_answers": False})
    assert config.preferences(app)["read_answers"] is False
    app.set("image_description", {"read_answers": True})
    assert config.preferences(app)["read_answers"] is True


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
    with pytest.raises(DescriptionError, match="ai_error_image_size"):
        prepare_image(photo())
    monkeypatch.setattr(image_input, "MAX_SOURCE_BYTES", 1)
    with pytest.raises(DescriptionError, match="ai_error_image_size"):
        prepare_image(b"too big")


def test_animation_is_not_silently_described_as_a_photo():
    data = BytesIO()
    first = Image.new("RGB", (10, 10), "red")
    first.save(data, format="PNG", save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")])
    with pytest.raises(DescriptionError, match="ai_error_image_format"):
        prepare_image(data.getvalue())


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_each_followup_resends_inline_photo_without_chat_context(provider):
    image = ImageInput(b"photo", "image/jpeg", 20, 30)
    url, headers, body = build_request(provider, config.PROVIDERS[provider].model, "private-key", image,
                                       (("user", "describe"), ("assistant", "red car")),
                                       "Read the sign", instructions("tr-TR", "balanced"), "balanced")
    assert "private-key" not in url and "private-key" not in json.dumps(body)
    assert base64.b64encode(image.data).decode() in json.dumps(body)
    assert "Read the sign" in json.dumps(body)
    if provider == "openai":
        assert body["store"] is False
        assert headers == {"Authorization": "Bearer private-key"}
        assert "previous_response_id" not in body and "tools" not in body
    else:
        assert headers == {"x-goog-api-key": "private-key"}
        assert body["contents"][1]["role"] == "model"


@pytest.mark.parametrize("model", ["../escape", "model?key=secret", "https://evil.test", "", "x y"])
def test_model_id_cannot_change_the_endpoint(model):
    with pytest.raises(DescriptionError):
        build_request("gemini", model, "secret", ImageInput(b"x", "image/jpeg", 1, 1), (), "x", "x", "fast")


def test_response_parsing_filters_thoughts_and_refusals():
    assert parse_answer("openai", {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "A red car"}]}]}) == "A red car"
    assert parse_answer("gemini", {"candidates": [{"content": {"parts": [
        {"text": "thinking", "thought": True}, {"text": "A red car"}]}}]}) == "A red car"
    with pytest.raises(DescriptionError, match="ai_error_refusal"):
        parse_answer("openai", {"output": [{"type": "message", "content": [{"type": "refusal"}]}]})
    with pytest.raises(DescriptionError, match="ai_error_refusal"):
        parse_answer("gemini", {"promptFeedback": {"blockReason": "SAFETY"}})


@pytest.mark.parametrize("provider,body", [
    ("openai", {"status": "incomplete", "output": []}),
    ("gemini", {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]}),
])
def test_incomplete_answer_is_not_announced_as_ready(provider, body):
    with pytest.raises(DescriptionError, match="ai_error_response"):
        parse_answer(provider, body)


@pytest.mark.parametrize("body", [None, {}, {"output": None}, {"output": [3]}])
def test_malformed_response_is_a_safe_error(body):
    with pytest.raises(DescriptionError, match="ai_error_response"):
        parse_answer("openai", body)


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
    return service.request_answer("openai", "gpt-4.1-mini", "secret", ImageInput(b"photo", "image/jpeg", 1, 1),
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
        request(FakeHTTP(FakeResponse(data=b"x" * (256 * 1024 + 1))))


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_connection_test_sends_no_photo_and_no_generation(provider):
    http = FakeHTTP()
    assert service.probe_connection(provider, config.PROVIDERS[provider].model, "secret",
                                    service.RequestToken(), session_factory=lambda: http)
    assert "models/" in http.calls[0][0]
    assert "secret" not in http.calls[0][0]
    assert "json" not in http.calls[0][1]


def test_session_rejects_duplicate_and_stale_completions_and_clears_data():
    s = PhotoSession("account", "chat", "message", locked=True)
    generation, token = s.begin("question")
    with pytest.raises(DescriptionError, match="ai_error_busy"):
        s.begin("duplicate")
    s.cancel()
    assert token.cancelled.is_set()
    assert not s.accept(generation, "question", "late")
    new, _ = s.begin("second")
    assert s.accept(new, "second", "answer")
    s.image = ImageInput(b"private", "image/jpeg", 1, 1)
    s.close()
    assert s.image is None and s.history == []
    assert not s.accept(new, "second", "late")


def test_session_limits_cannot_be_bypassed_by_cancelling():
    s = PhotoSession("a", "c", "m")
    for _ in range(config.MAX_REQUESTS):
        s.begin("question")
        s.cancel()
    with pytest.raises(DescriptionError, match="ai_error_limit"):
        s.begin("question")


def test_private_results_do_not_mix_between_accounts():
    first = PhotoSession("first", "chat", "message")
    second = PhotoSession("second", "chat", "message")
    generation, _ = first.begin("question")
    first.accept(generation, "question", "private answer")
    assert not second.history and first.identity != second.identity


@pytest.mark.parametrize("kind,view_once,expected", [("imageMessage", False, True),
                                                   ("imageMessage", True, False),
                                                   ("videoMessage", False, False),
                                                   ("viewOnceUnavailableMessage", False, False)])
def test_only_ordinary_photos_are_eligible(kind, view_once, expected):
    assert config.eligible_photo({"messageType": kind, "key": {"id": "message"},
                                 "message": {"imageMessage": {"viewOnce": view_once}}}) is expected


@pytest.mark.parametrize("msg_id", ["../outside", "dir\\file", "\x00bad", 123, None])
def test_photo_identity_cannot_escape_media_cache(msg_id):
    assert not config.eligible_photo({"messageType": "imageMessage", "key": {"id": msg_id},
                                      "message": {"imageMessage": {}}})


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
    from core.image_description.media_input import fetch_bounded_media
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
    from core.image_description.media_input import fetch_bounded_media
    response = FakeResponse(data=b"oversized")
    response.headers = {"Content-Type": "image/jpeg", "Content-Length": "1"}
    with pytest.raises(DescriptionError, match="ai_error_image_size"):
        fetch_bounded_media("url", {}, {}, 3, None, 15, lambda *a, **kw: response)
    assert response.closed


def test_cancelled_media_does_not_download():
    from core.image_description.media_input import fetch_bounded_media
    token = service.RequestToken()
    token.cancel()
    calls = []
    with pytest.raises(DescriptionError, match="ai_error_cancelled"):
        fetch_bounded_media("url", {}, {}, 100, token.check, 15, lambda *a, **kw: calls.append(True))
    assert not calls
