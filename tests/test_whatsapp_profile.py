"""File > WhatsApp settings > Profile: what the page sends and what counts as
failed (core/whatsapp_profile.py) and the server calls
(main_window/whatsapp_profile.py). No window is opened.
"""

import pytest

from core import whatsapp_profile as profile
from main_window import whatsapp_profile as mixin_module
from main_window.whatsapp_profile import WhatsAppProfileMixin


# ── What the page sends ──────────────────────────────────────────────────────

class TestProfileChanges:
    def test_text_is_stripped_and_a_blank_field_is_left_alone(self):
        changes = profile.profile_changes("  Ana  ", "   ", "")
        assert changes == profile.ProfileChanges(name="Ana", status="", photo="")
        assert changes.fields() == [("name", "Ana")]

    def test_nothing_filled_in_is_empty(self):
        assert profile.profile_changes("", " ", None).empty
        assert profile.profile_changes(None, None, "").empty
        assert not profile.profile_changes("", "Busy", "").empty
        assert not profile.profile_changes("", "", r"C:\fotos\eu.png").empty

    def test_fields_come_in_the_pages_order(self):
        changes = profile.profile_changes("Ana", "Busy", r"C:\fotos\eu.jpg")
        assert [field for field, _value in changes.fields()] == ["name", "status", "photo"]


class TestPhotoType:
    @pytest.mark.parametrize("path,expected", [
        (r"C:\fotos\eu.jpg", "image/jpeg"),
        (r"C:\fotos\eu.JPEG", "image/jpeg"),
        (r"C:\fotos\eu.png", "image/png"),
        (r"C:\fotos\eu.PNG", "image/png"),
        (r"C:\fotos\eu.gif", ""),
        (r"C:\fotos\eu.webp", ""),
        (r"C:\fotos\eu", ""),
        ("", ""),
        (None, ""),
    ])
    def test_only_photos_the_server_takes_have_a_type(self, path, expected):
        assert profile.photo_content_type(path) == expected


class TestFailureLines:
    LABELS = {"name": "Display name:", "status": "About:", "photo": "Profile photo:"}

    def test_each_failed_field_is_named_in_the_pages_order(self):
        errors = {"photo": "HTTP 500", "name": "setPushname is not a function"}
        assert profile.failure_lines(errors, self.LABELS) == [
            "Display name:: setPushname is not a function",
            "Profile photo:: HTTP 500",
        ]

    def test_a_field_without_a_label_is_named_by_its_key(self):
        assert profile.failure_lines({"status": "HTTP 500"}, {}) == ["status: HTTP 500"]

    def test_no_failures_is_no_lines(self):
        assert profile.failure_lines({}, self.LABELS) == []


# ── Server calls ─────────────────────────────────────────────────────────────

class _Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class _Stub(WhatsAppProfileMixin):
    wpp_server = "http://127.0.0.1"
    wpp_port = 6300
    token = "SESSION:TOKEN"


@pytest.fixture
def posts(monkeypatch):
    calls = []
    answers = []

    def fake_post(url, json=None, files=None, headers=None, timeout=None):
        route = url.rsplit("/api/SESSION:TOKEN/", 1)[1]
        calls.append({"route": route, "json": json, "files": files,
                      "headers": headers, "timeout": timeout})
        return answers.pop(0)

    monkeypatch.setattr(mixin_module, "api_post", fake_post)
    return calls, answers


class TestTextFields:
    def test_the_name_goes_to_change_username(self, posts):
        calls, answers = posts
        answers.append(_Response(200, {"status": "success"}))
        assert _Stub().set_profile_name("Ana") is None
        assert calls[0]["route"] == "change-username"
        assert calls[0]["json"] == {"name": "Ana"}
        assert calls[0]["headers"]["Content-Type"] == "application/json"

    def test_the_about_text_goes_to_profile_status(self, posts):
        calls, answers = posts
        answers.append(_Response(201, {"status": "success"}))
        assert _Stub().set_profile_status("Busy") is None
        assert calls[0]["route"] == "profile-status"
        assert calls[0]["json"] == {"status": "Busy"}

    def test_the_servers_own_reason_is_reported_not_just_the_generic_message(self, posts):
        _calls, answers = posts
        answers.append(_Response(500, {
            "status": "error", "message": "Error on set profile name.",
            "error": "n.functions.setPushname is not a function"}))
        assert _Stub().set_profile_name("Ana") == (
            "Error on set profile name. (n.functions.setPushname is not a function)")

    def test_an_error_object_is_written_out(self, posts):
        _calls, answers = posts
        answers.append(_Response(500, {"message": "Error", "error": {"code": 7}}))
        assert _Stub().set_profile_status("Busy") == 'Error ({"code": 7})'

    def test_an_answer_that_is_not_json_is_reported_by_status(self, posts):
        _calls, answers = posts
        answers.append(_Response(502))
        assert _Stub().set_profile_name("Ana") == "HTTP 502"

    def test_an_exception_is_an_error_string_not_a_crash(self, monkeypatch):
        def boom(*args, **kwargs):
            raise ConnectionError("refused")

        monkeypatch.setattr(mixin_module, "api_post", boom)
        assert _Stub().set_profile_name("Ana") == "ConnectionError"
        assert _Stub().set_profile_status("Busy") == "ConnectionError"

    def test_what_was_typed_never_reaches_the_log(self, posts, caplog):
        _calls, answers = posts
        answers.append(_Response(500, {"message": "Error", "error": "boom"}))
        with caplog.at_level("DEBUG"):
            _Stub().set_profile_name("Segredo Pessoal")
        assert "Segredo Pessoal" not in caplog.text


class TestPhoto:
    def test_a_png_is_uploaded_as_png_without_a_json_content_type(self, posts, tmp_path):
        calls, answers = posts
        answers.append(_Response(200, {"status": "success"}))
        photo = tmp_path / "eu.png"
        photo.write_bytes(b"\x89PNG")
        assert _Stub().set_profile_pic(str(photo)) is None
        call = calls[0]
        assert call["route"] == "set-profile-pic"
        name, _handle, content_type = call["files"]["file"]
        assert (name, content_type) == ("eu.png", "image/png")
        # Multipart sets its own Content-Type, with the boundary.
        assert "Content-Type" not in call["headers"]
        assert call["timeout"] == _Stub._PROFILE_PHOTO_TIMEOUT

    def test_a_jpg_is_uploaded_as_jpeg(self, posts, tmp_path):
        calls, answers = posts
        answers.append(_Response(200, {"status": "success"}))
        photo = tmp_path / "eu.jpg"
        photo.write_bytes(b"\xff\xd8")
        assert _Stub().set_profile_pic(str(photo)) is None
        assert calls[0]["files"]["file"][2] == "image/jpeg"

    def test_an_unsupported_type_is_refused_without_calling_the_server(self, posts, tmp_path):
        calls, _answers = posts
        photo = tmp_path / "eu.gif"
        photo.write_bytes(b"GIF8")
        assert _Stub().set_profile_pic(str(photo)) == "unsupported photo type"
        assert calls == []

    def test_a_missing_file_is_an_error_string_not_a_crash(self, posts, tmp_path):
        calls, _answers = posts
        assert _Stub().set_profile_pic(str(tmp_path / "nao-existe.png")) == "FileNotFoundError"
        assert calls == []

    def test_a_refused_photo_reports_the_servers_reason(self, posts, tmp_path):
        _calls, answers = posts
        answers.append(_Response(500, {
            "message": "Error changing profile photo", "error": "invalid image"}))
        photo = tmp_path / "eu.jpg"
        photo.write_bytes(b"\xff\xd8")
        assert _Stub().set_profile_pic(str(photo)) == "Error changing profile photo (invalid image)"


class TestApplyProfileChanges:
    def test_only_what_was_filled_in_is_sent(self, posts):
        calls, answers = posts
        answers.append(_Response(200, {}))
        changes = profile.profile_changes("", "Busy", "")
        assert _Stub().apply_profile_changes(changes) == {}
        assert [call["route"] for call in calls] == ["profile-status"]

    def test_a_refused_field_does_not_stop_the_others(self, posts, tmp_path):
        calls, answers = posts
        answers.append(_Response(500, {"message": "Error on set profile name.", "error": "no"}))
        answers.append(_Response(200, {}))
        answers.append(_Response(200, {}))
        photo = tmp_path / "eu.png"
        photo.write_bytes(b"\x89PNG")
        changes = profile.profile_changes("Ana", "Busy", str(photo))
        errors = _Stub().apply_profile_changes(changes)
        assert errors == {"name": "Error on set profile name. (no)"}
        assert [call["route"] for call in calls] == [
            "change-username", "profile-status", "set-profile-pic"]
