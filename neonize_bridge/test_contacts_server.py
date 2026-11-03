"""Contact, profile and presence routes on the real server (create_app), with a
fake neonize client: what the WinZapp client reads (contact/pn-lid,
blocklist, all-contacts, contact/:phone, subscribe-presence, set-online-presence,
set-profile-pic, join-code). No WhatsApp; nothing here was proved against a real account."""
import io
from types import SimpleNamespace as N

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import authentication as auth
import server

PHONE = "5511999990000"
LID = "123456789012345"


def jid(user, server="s.whatsapp.net"):
    return N(User=user, Server=server)


class FakeClient:
    """Only what the routes touch; each call is kept in `calls`."""

    def __init__(self):
        self.calls = []
        self.blocked = []
        self.phone_of_lid = {}         # LID user -> number user
        self.lid_of_phone = {}
        self.businesses = {}          # number user -> BusinessName
        self.failure = None           # exception the next call raises
        self.contact = N(get_contact=self._get_contact)

    def _maybe_fail(self):
        if self.failure is not None:
            raise self.failure

    def get_blocklist(self):
        self._maybe_fail()
        return N(JIDs=self.blocked)

    def get_pn_from_lid(self, j):
        if j.User not in self.phone_of_lid:
            raise RuntimeError("no map")
        return jid(self.phone_of_lid[j.User])

    def get_lid_from_pn(self, j):
        if j.User not in self.lid_of_phone:
            raise RuntimeError("no map")
        return jid(self.lid_of_phone[j.User], "lid")

    def _get_contact(self, j):
        return N(BusinessName=self.businesses.get(j.User, ""))

    def subscribe_presence(self, j):
        self.calls.append(("subscribe", j.User, j.Server))
        self._maybe_fail()

    def send_presence(self, presence):
        self.calls.append(("presence", presence.value))
        self._maybe_fail()

    def set_profile_photo(self, data):
        self.calls.append(("photo", bytes(data[:3])))
        self._maybe_fail()

    def get_group_info_from_link(self, code):
        return N(JID=jid("1203", "g.us"))

    def join_group_with_link(self, code):
        self._maybe_fail()


@pytest.fixture
def sessions(monkeypatch):
    created = []

    class Registered(server.Session):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            created.append(self)
    monkeypatch.setattr(server, "Session", Registered)
    return created


async def _bridge(folder, sessions, connected=True):
    """(http client, API prefix, session, fake neonize client)."""
    http = TestClient(TestServer(server.create_app(str(folder))))
    await http.start_server()
    secret = (folder / auth.SECRET_FILE).read_text(encoding="utf-8")
    token = f"account:{auth.session_token(secret, 'account')}"
    assert (await http.get(f"/api/{token}/status-session")).status == 200
    s = sessions[0]
    fake = FakeClient()
    s.client = fake
    s.status = "CONNECTED" if connected else "CLOSED"
    s.me, s.lid = "5511888880000@s.whatsapp.net", "999888777666555@lid"
    return http, f"/api/{token}", s, fake


@pytest.mark.asyncio
async def test_pn_lid_returns_what_the_client_reads(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions)
    try:
        fake.phone_of_lid[LID] = PHONE
        s.db.contact(f"{PHONE}@s.whatsapp.net", name="Ana Silva", pushname="aninha")
        response = await http.get(f"{api}/contact/pn-lid/{LID}@lid")
        body = await response.json()
        assert response.status == 200
        # identity.py::resolve_lid_jids_via_api: phoneNumber/contact; resolve_self_lid: lid/phone
        assert body["phoneNumber"] == {"_serialized": f"{PHONE}@c.us"}
        assert body["contact"] == {"name": "Ana Silva", "pushname": "aninha"}
        assert body["lid"] == {"_serialized": f"{LID}@lid"}
        assert body["phone"] == body["id"] == {"_serialized": f"{PHONE}@c.us"}
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_blocklist_with_a_failure_does_not_become_an_empty_list(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions)
    try:
        fake.failure = RuntimeError("timeout")
        response = await http.get(f"{api}/blocklist")
        assert response.status == 502
        assert (await response.json())["status"] == "error"
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_blocklist_without_a_connection_does_not_become_an_empty_list(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions, connected=False)
    try:
        response = await http.get(f"{api}/blocklist")
        assert response.status == 503
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_blocklist_brings_the_number_of_whoever_was_blocked_by_lid(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions)
    try:
        fake.phone_of_lid[LID] = PHONE
        fake.blocked = [jid(LID, "lid"), jid("5511777770000"), jid("555000000000000", "lid")]
        response = await http.get(f"{api}/blocklist")
        items = (await response.json())["response"]
        assert response.status == 200
        # the client reads `phone` as the phone: the LID becomes the number; the LID stays in `id`
        assert items[0] == {"phone": PHONE, "id": {"_serialized": f"{LID}@lid"}}
        assert items[1]["phone"] == "5511777770000"
        # LID without a map: at least the LID's own digits (they match the LID's chat)
        assert items[2]["phone"] == "555000000000000"
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_all_contacts_says_whether_it_is_in_the_address_book_and_who_i_am(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions)
    try:
        s.db.contact("5511777770000@s.whatsapp.net", name="Bia")
        s.db.contact("5511666660000@s.whatsapp.net", pushname="only a nickname")
        s.db.contact(s.me, pushname="me")
        rows = {c["id"]["_serialized"]: c for c in
                (await (await http.get(f"{api}/all-contacts")).json())["response"]}
        assert rows["5511777770000@c.us"]["syncToAddressbook"] is True
        assert rows["5511777770000@c.us"]["isMyContact"] is True
        assert rows["5511666660000@c.us"]["syncToAddressbook"] is False
        assert rows["5511666660000@c.us"]["isMe"] is False
        assert rows["5511888880000@c.us"]["isMe"] is True
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_contact_brings_the_business_account_and_the_nickname_stored_only_under_the_lid(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions)
    try:
        fake.businesses[PHONE] = "Joe's Shop"
        fake.lid_of_phone[PHONE] = LID
        s.db.contact(f"{LID}@lid", pushname="joe")
        body = (await (await http.get(f"{api}/contact/{PHONE}@c.us")).json())["response"]
        assert body["isBusiness"] is True and body["verifiedName"] == "Joe's Shop"
        assert body["pushname"] == "joe"        # it was stored only under the LID

        person = (await (await http.get(f"{api}/contact/5511555550000@c.us")).json())["response"]
        assert person["isBusiness"] is False and "verifiedName" not in person
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_contact_finds_the_name_stored_under_the_other_form_of_the_brazilian_number(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions)
    try:
        s.db.contact("551199990000@s.whatsapp.net", name="Cris")     # without the 9th digit
        body = (await (await http.get(f"{api}/contact/{PHONE}@c.us")).json())["response"]
        assert body["name"] == "Cris" and body["isMyContact"] is True
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_subscribe_presence_only_confirms_what_whatsapp_accepted(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions)
    try:
        good = await http.post(f"{api}/subscribe-presence", json={"phone": f"{PHONE}@c.us"})
        assert good.status == 200 and (await good.json())["status"] == "success"
        assert fake.calls == [("subscribe", PHONE, "s.whatsapp.net")]

        fake.failure = RuntimeError("refused")
        refused = await http.post(f"{api}/subscribe-presence", json={"phone": f"{LID}@lid"})
        assert refused.status == 502 and (await refused.json())["status"] == "error"
        invalid = await http.post(f"{api}/subscribe-presence", json={"phone": "status@broadcast"})
        assert invalid.status == 400
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_subscribe_presence_without_a_connection_does_not_confirm(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions, connected=False)
    try:
        response = await http.post(f"{api}/subscribe-presence", json={"phone": f"{PHONE}@c.us"})
        assert response.status == 503
        assert fake.calls == []
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_set_online_presence_sends_the_presence_to_whatsapp(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions)
    try:
        assert (await http.post(f"{api}/set-online-presence", json={"isOnline": True})).status == 200
        assert (await http.post(f"{api}/set-online-presence", json={"isOnline": False})).status == 200
        assert fake.calls == [("presence", b"available"), ("presence", b"unavailable")]
        fake.failure = RuntimeError("no network")
        assert (await http.post(f"{api}/set-online-presence", json={"isOnline": True})).status == 502
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_set_online_presence_without_a_connection_only_keeps_the_state(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions, connected=False)
    try:
        assert (await http.post(f"{api}/set-online-presence", json={"isOnline": True})).status == 200
        assert s.online is True and fake.calls == []
    finally:
        await http.close()


def _png():
    from PIL import Image
    output = io.BytesIO()
    Image.new("RGB", (32, 32), "red").save(output, format="PNG")
    return output.getvalue()


async def _send_photo(http, api, data, kind):
    form = FormData()
    form.add_field("file", data, filename="photo", content_type=kind)
    return await http.post(f"{api}/set-profile-pic", data=form)


@pytest.mark.asyncio
async def test_set_profile_pic_converts_png_to_jpeg(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions)
    try:
        response = await _send_photo(http, api, _png(), "image/png")
        assert response.status == 200
        assert fake.calls == [("photo", b"\xff\xd8\xff")]
        assert (await _send_photo(http, api, b"not an image", "image/png")).status == 415
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_join_code_only_says_pending_request_when_the_error_says_so(tmp_path, sessions):
    http, api, s, fake = await _bridge(tmp_path, sessions)
    try:
        body = {"inviteCode": "ABCDEFGHIJ1234"}
        assert (await (await http.post(f"{api}/join-code", json=body)).json())[
            "response"]["pendingApproval"] is False

        fake.failure = RuntimeError("info query returned status 400: bad-request")
        failed = await http.post(f"{api}/join-code", json=body)
        assert failed.status == 500 and (await failed.json())["status"] == "error"

        fake.failure = RuntimeError("membership_approval_request")
        pending = await http.post(f"{api}/join-code", json=body)
        assert pending.status == 200
        assert (await pending.json())["response"]["pendingApproval"] is True

        fake.failure = RuntimeError("info query returned status 409: conflict")
        assert (await http.post(f"{api}/join-code", json=body)).status == 409
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_saved_names_synced_after_the_connection_reach_all_contacts(tmp_path, sessions):
    """Right after pairing, neonize's store has no saved names yet: they come
    with the app state later. Read once at connect, every contact stayed
    unsaved for good; a later reload brings them."""
    http, api, s, fake = await _bridge(tmp_path, sessions)
    try:
        store = []
        fake.contact = N(get_all_contacts=lambda: list(store))
        phone = jid("5511777770000")
        store.append(N(JID=phone, Info=N(FullName="", FirstName="", PushName="bia")))
        assert s.reload_names() == 0                       # at connect: nickname only

        async def bia():
            rows = (await (await http.get(f"{api}/all-contacts")).json())["response"]
            return {c["id"]["_serialized"]: c for c in rows}["5511777770000@c.us"]
        assert (await bia())["isMyContact"] is False

        store[0] = N(JID=phone, Info=N(FullName="Bia Lima", FirstName="Bia", PushName="bia"))
        assert s.reload_names() == 1                       # the app state arrived
        assert s.reload_names() == 0                       # nothing new: nothing written
        bia = await bia()
        assert bia["isMyContact"] is True and bia["name"] == "Bia Lima"
        assert bia["pushname"] == "bia"
    finally:
        await http.close()


class SavingClient(FakeClient):
    """save-contact's calls, in the order they reach WhatsApp."""

    def __init__(self):
        super().__init__()
        self.contact = N(get_contact=self._get_contact, put_contact_name=lambda *a: None)

    def is_on_whatsapp(self, *numbers):
        return [N(IsIn=True, JID=jid(PHONE), HasField=lambda field: False)]

    def send_iq(self, query):
        self.calls.append(("usync", query))
        return {"node": {"tag": "iq"}}

    def send_app_state(self, patch):
        self.calls.append(("app state", patch))


@pytest.mark.asyncio
async def test_save_contact_tells_the_server_before_writing_the_address_book(
        tmp_path, sessions, monkeypatch):
    import raw_queries
    monkeypatch.setattr(raw_queries, "available", lambda c: hasattr(c, "send_iq"))
    http, api, s, _fake = await _bridge(tmp_path, sessions)
    fake = s.client = SavingClient()
    try:
        response = await http.post(f"{api}/save-contact",
                                   json={"phone": PHONE, "name": "Ana", "lastName": "Silva"})
        assert response.status == 200, await response.text()
        assert [kind for kind, _ in fake.calls] == ["usync", "app state"]
        usync = fake.calls[0][1]["content"][0]
        assert usync["attrs"]["mode"] == "delta"
        assert usync["content"][1]["content"] == [
            {"tag": "user", "content": [{"tag": "contact", "content": PHONE}]}]
        (mutation,) = fake.calls[1][1].Mutations
        # the index WhatsApp Web writes (toString({legacy: true}) of a number)
        assert list(mutation.Index) == ["contact", f"{PHONE}@s.whatsapp.net"]
    finally:
        await http.close()
