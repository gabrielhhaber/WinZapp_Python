"""Profile and privacy routes: the pure functions that build what the client reads."""
from types import SimpleNamespace as N

import profile_privacy


def test_profile_jid_accepts_the_id_forms():
    assert profile_privacy.profile_jid("5511999") == "5511999@s.whatsapp.net"
    assert profile_privacy.profile_jid("5511999@c.us") == "5511999@s.whatsapp.net"
    assert profile_privacy.profile_jid("333@lid") == "333@lid"
    assert profile_privacy.profile_jid("1203-4455", True) == "1203-4455@g.us"
    assert profile_privacy.profile_jid("1203-4455@g.us", True) == "1203-4455@g.us"


def test_only_a_person_is_valid_for_last_seen():
    for good in ("5511999", "5511999@c.us", "5511999@s.whatsapp.net", "333@lid"):
        assert profile_privacy.valid_person_id(good)
    for bad in ("", "1203-4455@g.us", "x@broadcast", "5511999@c.us/../x", "abc"):
        assert not profile_privacy.valid_person_id(bad)


def test_valid_setting_only_accepts_what_whatsapp_accepts():
    assert profile_privacy.valid_setting("lastSeen", "contacts")
    assert profile_privacy.valid_setting("online", "match_last_seen")
    assert profile_privacy.valid_setting("readReceipts", "none")
    assert not profile_privacy.valid_setting("online", "none")
    assert not profile_privacy.valid_setting("groupAdd", "none")
    assert not profile_privacy.valid_setting("unknown", "all")
    assert not profile_privacy.valid_setting("lastSeen", None)
    assert not profile_privacy.valid_setting(None, "all")


def test_every_client_setting_has_a_neonize_type():
    # the six settings that WinZapp's privacy dialog knows
    assert set(profile_privacy.SETTINGS) == {"lastSeen", "online", "about", "profilePicture",
                                     "readReceipts", "groupAdd"}
    assert set(profile_privacy.PRIVACY_FIELDS.values()) == set(profile_privacy.SETTINGS)


def test_settings_to_wpp_translates_the_enum_and_leaves_none_what_is_not_known():
    p = N(LastSeen=3, Online=5, Status=7, Profile=2, ReadReceipts=2, GroupAdd=1)
    assert profile_privacy.settings_to_wpp(p) == {
        "lastSeen": "contacts", "online": "match_last_seen", "about": "none",
        "profilePicture": "all", "readReceipts": "all", "groupAdd": None}
    assert profile_privacy.settings_to_wpp(N(LastSeen=4))["lastSeen"] == "contact_blacklist"
    assert profile_privacy.settings_to_wpp(N())["lastSeen"] is None


def test_no_photo_is_a_definitive_answer_and_another_error_is_not():
    assert profile_privacy.no_photo(Exception("that user or group does not have a profile picture"))
    assert profile_privacy.no_photo(Exception("the user has hidden their profile picture from you"))
    assert profile_privacy.no_photo(Exception("iq error: not-authorized"))
    assert not profile_privacy.no_photo(Exception("timed out"))


def test_photo_to_wpp_carries_the_keys_the_client_tries():
    info = N(URL="https://pps.whatsapp.net/a.jpg", ID="123")
    assert profile_privacy.photo_to_wpp(info, "5511@c.us") == {
        "eurl": info.URL, "img": info.URL, "imgFull": info.URL, "id": "5511@c.us", "tag": "123"}
    assert profile_privacy.photo_to_wpp(N(URL="", ID=""), "x") is None
    assert profile_privacy.photo_to_wpp(None, "x") is None


def test_about_is_the_first_persons_that_has_one():
    infos = [N(UserInfo=N(Status="")), N(UserInfo=N(Status="Busy"))]
    assert profile_privacy.about_of(infos) == "Busy"
    assert profile_privacy.about_of([]) == ""
    assert profile_privacy.about_of(None) == ""


def test_only_jpeg_goes_as_a_profile_photo():
    assert profile_privacy.is_jpeg(b"\xff\xd8\xff\xe0abc")
    assert not profile_privacy.is_jpeg(b"\x89PNG\r\n\x1a\n")
    assert not profile_privacy.is_jpeg(b"")


def _image(format_):
    import io

    from PIL import Image
    output = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(output, format=format_)
    return output.getvalue()


def test_profile_photo_converts_png_and_lets_jpeg_through():
    jpeg = _image("JPEG")
    assert profile_privacy.profile_photo(jpeg) is jpeg
    assert profile_privacy.is_jpeg(profile_privacy.profile_photo(_image("PNG")))


def test_profile_photo_refuses_what_is_not_an_image():
    import pytest
    with pytest.raises(Exception):
        profile_privacy.profile_photo(b"any text")


def test_own_profile_marks_none_what_was_not_read():
    assert profile_privacy.own_profile(" Ana ", "Busy", True) == {
        "name": "Ana", "status": "Busy", "photo": True}
    assert profile_privacy.own_profile(None, "", None) == {
        "name": None, "status": None, "photo": None}
    assert profile_privacy.own_profile("Ana", None, False)["photo"] is False


def _app_with_routes():
    from aiohttp import web
    app = web.Application()
    session = N(client=None, status="CLOSED", me="")
    profile_privacy.register_routes(app.router, session=lambda req: session,
                            ok=lambda **kw: web.json_response(kw),
                            jid_from_text=lambda t: t, jid_text=lambda j: str(j))
    return app


def test_last_seen_answers_unsupported_the_way_the_client_understands():
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        async with TestClient(TestServer(_app_with_routes())) as c:
            good = await c.get("/api/tok/last-seen/5511999@c.us")
            bad = await c.get("/api/tok/last-seen/1203-4455@g.us")
            return good.status, await good.json(), bad.status
    status, body, bad_status = asyncio.run(scenario())
    assert status == 200 and body["presence"] == {"status": "unsupported"}
    assert bad_status == 400


def test_without_a_connection_the_privacy_routes_say_privacy_unavailable():
    import asyncio
    from aiohttp.test_utils import TestClient, TestServer

    async def scenario():
        async with TestClient(TestServer(_app_with_routes())) as c:
            read = await c.post("/api/tok/privacy", json={})
            invalid = await c.post("/api/tok/privacy/set",
                                   json={"setting": "online", "value": "none"})
            return read.status, await read.json(), invalid.status, await invalid.json()
    read, body, invalid, invalid_body = asyncio.run(scenario())
    assert read == 503 and body["code"] == "privacy_unavailable"
    assert invalid == 400 and invalid_body["code"] == "invalid_privacy_setting"


def test_setting_applied_compares_what_whatsapp_reports():
    settings = N(LastSeen=3, ReadReceipts=7)
    assert profile_privacy.setting_applied(settings, "lastSeen", "contacts")
    assert profile_privacy.setting_applied(settings, "readReceipts", "none")
    assert not profile_privacy.setting_applied(settings, "lastSeen", "none")
    assert not profile_privacy.setting_applied(None, "lastSeen", "contacts")


def _privacy_set_with(set_error, settings_after):
    """POST /privacy/set lastSeen=contacts against a connected fake client whose
    set raises *set_error* (None: succeeds) and whose read answers *settings_after*."""
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    def set_privacy_setting(name, value):
        if set_error:
            raise set_error
    client = N(set_privacy_setting=set_privacy_setting,
               get_privacy_settings=lambda: settings_after)
    session = N(client=client, status="CONNECTED", me="")
    app = web.Application()
    profile_privacy.register_routes(app.router, session=lambda req: session,
                                    ok=lambda **kw: web.json_response(kw),
                                    jid_from_text=lambda t: t, jid_text=lambda j: str(j))

    async def scenario():
        async with TestClient(TestServer(app)) as c:
            answer = await c.post("/api/tok/privacy/set",
                                  json={"setting": "lastSeen", "value": "contacts"})
            return answer.status, await answer.json()
    return asyncio.run(scenario())


class SetPrivacySettingError(Exception):
    pass


def test_a_change_that_raised_but_is_applied_is_a_success():
    # neonize before patch 0025 raised on every change WhatsApp applied (report
    # of 2026-11-06: "Visto por último: privacy_setting_failed", yet the phone
    # showed the new value).
    status, body = _privacy_set_with(SetPrivacySettingError("x"), N(LastSeen=3))
    assert status == 200 and body["status"] == "success"


def test_a_change_that_raised_and_is_not_applied_still_fails():
    status, body = _privacy_set_with(SetPrivacySettingError("x"), N(LastSeen=2))
    assert status == 502 and body["code"] == "privacy_setting_failed"
    assert body["kind"] == "SetPrivacySettingError"


def test_a_change_that_did_not_raise_is_a_success():
    assert _privacy_set_with(None, None)[0] == 200
