"""The profile-picture fetch behind the chat/group data dialog's button."""

import os

from core import profile_picture as pp

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _Resp:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


class TestPictureUrlFromResponse:
    def test_prefers_the_full_size_picture(self):
        body = {"status": "success", "response": {
            "eurl": "https://pps.whatsapp.net/e", "img": "https://pps.whatsapp.net/t",
            "imgFull": "https://pps.whatsapp.net/full"}}
        assert pp.picture_url_from_response(body) == "https://pps.whatsapp.net/full"

    def test_falls_back_to_the_thumbnail_then_eurl(self):
        assert pp.picture_url_from_response({"img": "https://x/t"}) == "https://x/t"
        assert pp.picture_url_from_response({"eurl": "https://x/e"}) == "https://x/e"

    def test_a_bare_url_string_is_accepted(self):
        assert pp.picture_url_from_response({"response": "https://x/p"}) == "https://x/p"

    def test_hidden_or_absent_picture_gives_nothing(self):
        # What WA-JS answers when there is no picture or privacy hides it.
        assert pp.picture_url_from_response({"status": "success", "response": None}) == ""
        assert pp.picture_url_from_response({"response": {"id": "x", "tag": "0"}}) == ""
        assert pp.picture_url_from_response({"response": {"eurl": None}}) == ""
        assert pp.picture_url_from_response("not a url") == ""
        assert pp.picture_url_from_response([]) == ""


class TestPictureExtension:
    def test_known_formats(self):
        assert pp.picture_extension(JPEG) == ".jpg"
        assert pp.picture_extension(PNG) == ".png"
        assert pp.picture_extension(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp"

    def test_anything_else_is_not_a_picture(self):
        assert pp.picture_extension(b"<html>error</html>") == ""
        assert pp.picture_extension(b"") == ""


class TestApiUrl:
    def test_contact(self):
        assert (pp.profile_pic_api_url("http://127.0.0.1:6300", "tok", "5511999990000@s.whatsapp.net")
                == "http://127.0.0.1:6300/api/tok/profile-pic/5511999990000@s.whatsapp.net")

    def test_group_asks_with_is_group(self):
        assert pp.profile_pic_api_url("http://h:1", "tok", "120363@g.us").endswith(
            "/profile-pic/120363@g.us?isGroup=true")


class TestQueryJid:
    def test_lid_with_a_known_phone_uses_the_phone(self):
        assert pp.query_jid("123@lid", {"123@lid": "55119@s.whatsapp.net"}) == "55119@s.whatsapp.net"

    def test_unknown_lid_and_other_jids_pass_through(self):
        assert pp.query_jid("123@lid", {}) == "123@lid"
        assert pp.query_jid("123@lid", None) == "123@lid"
        assert pp.query_jid("1@g.us", {"1@g.us": "x"}) == "1@g.us"


class TestStoredPictureUrl:
    def test_reads_the_contact_entry(self):
        contacts = {"a@s.whatsapp.net": {"profilePicUrl": "https://x/p"}}
        assert pp.stored_picture_url(contacts, "a@s.whatsapp.net") == "https://x/p"

    def test_missing_entries(self):
        assert pp.stored_picture_url(None, "a") == ""
        assert pp.stored_picture_url({"a": {}}, "a") == ""
        assert pp.stored_picture_url({"a": None}, "a") == ""


class TestFetch:
    def _api(self, resp, calls=None):
        def api_get(url, **kwargs):
            if calls is not None:
                calls.append(url)
            if isinstance(resp, Exception):
                raise resp
            return resp
        return api_get

    def test_downloads_the_url_the_server_names(self):
        seen = []
        api = self._api(_Resp(200, {"response": {"imgFull": "https://cdn/p"}}))

        def http_get(url, **kwargs):
            seen.append(url)
            return _Resp(200, content=JPEG)

        assert pp.fetch_profile_picture("http://h:1", "t", "a@s.whatsapp.net", api, http_get) == JPEG
        assert seen == ["https://cdn/p"]

    def test_no_picture_is_none(self):
        api = self._api(_Resp(200, {"response": None}))

        def http_get(url, **kwargs):
            raise AssertionError("nothing to download")

        assert pp.fetch_profile_picture("http://h:1", "t", "a", api, http_get) is None
        assert pp.fetch_profile_picture("http://h:1", "t", "a", api, http_get,
                                        fallback_url="https://cdn/stale") is None

    def test_server_error_falls_back_to_the_stored_url(self):
        api = self._api(_Resp(500, {}))
        got = pp.fetch_profile_picture(
            "http://h:1", "t", "a", api, lambda url, **k: _Resp(200, content=PNG),
            fallback_url="https://cdn/stored")
        assert got == PNG

    def test_expired_url_and_non_image_body_are_none(self):
        api = self._api(_Resp(200, {"response": {"img": "https://cdn/p"}}))
        assert pp.fetch_profile_picture(
            "http://h:1", "t", "a", api, lambda url, **k: _Resp(403)) is None
        assert pp.fetch_profile_picture(
            "http://h:1", "t", "a", api, lambda url, **k: _Resp(200, content=b"<html>")) is None

    def test_network_failures_are_none_not_raised(self):
        api = self._api(ConnectionError("down"))

        def http_get(url, **kwargs):
            raise ConnectionError("down")

        assert pp.fetch_profile_picture(
            "http://h:1", "t", "a", api, http_get, fallback_url="https://cdn/s") is None

    def test_the_stored_url_is_not_downloaded_twice(self):
        seen = []
        api = self._api(_Resp(200, {"response": {"img": "https://cdn/p"}}))

        def http_get(url, **kwargs):
            seen.append(url)
            return _Resp(404)

        pp.fetch_profile_picture("http://h:1", "t", "a", api, http_get, fallback_url="https://cdn/p")
        assert seen == ["https://cdn/p"]


class TestSavePicture:
    def test_file_name_does_not_carry_the_number(self, tmp_path):
        path = pp.save_picture(str(tmp_path / "pics"), "5511999990000@s.whatsapp.net", JPEG)
        assert os.path.basename(path).endswith(".jpg")
        assert "5511999990000" not in path
        with open(path, "rb") as fh:
            assert fh.read() == JPEG

    def test_same_chat_overwrites_its_file(self, tmp_path):
        a = pp.save_picture(str(tmp_path), "x@g.us", JPEG)
        b = pp.save_picture(str(tmp_path), "x@g.us", JPEG)
        assert a == b

    def test_a_copy_held_open_elsewhere_gets_a_new_name(self, tmp_path, monkeypatch):
        held = pp.save_picture(str(tmp_path), "x@g.us", JPEG)
        real_open = open

        def fake_open(path, mode="r", *a, **k):
            if path == held and "w" in mode:
                raise PermissionError("locked by the viewer")
            return real_open(path, mode, *a, **k)
        monkeypatch.setattr("builtins.open", fake_open)
        other = pp.save_picture(str(tmp_path), "x@g.us", PNG)
        assert other != held
        with real_open(other, "rb") as fh:
            assert fh.read() == PNG
