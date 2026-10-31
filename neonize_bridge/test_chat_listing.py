"""Chat reading routes: the pure functions and the routes against a real
store (no neonize and no connection)."""
import asyncio
from types import SimpleNamespace as N

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import chat_listing as cl
import msgmap
from store import Store


def wpp_id(jid):
    user, _, server = msgmap.jid_wpp(jid).partition("@")
    return {"server": server, "user": user, "_serialized": f"{user}@{server}"}


def row(jid, **fields):
    base = {"jid": jid, "name": "", "ts": 0, "unread": 0, "archived": 0,
            "pinned": 0, "muted": 0, "locked": 0}
    return {**base, **fields}


def msg(mid, from_me=False, t=1):
    return {"id": {"id": mid, "fromMe": from_me}, "fromMe": from_me, "t": t}


def test_boolean():
    assert cl.boolean(None, True) is True
    assert cl.boolean("", False) is False
    assert cl.boolean("true", False) is True
    assert cl.boolean("False", True) is False
    assert cl.boolean("0", True) is False


def test_chat_jid():
    assert cl.chat_jid("5511@c.us") == "5511@s.whatsapp.net"
    assert cl.chat_jid("5511") == "5511@s.whatsapp.net"
    assert cl.chat_jid("1203", group=True) == "1203@g.us"
    assert cl.chat_jid("1203@g.us", group=True) == "1203@g.us"
    assert cl.chat_jid("9@lid") == "9@lid"


def test_chat_to_wpp_follows_the_list_chats_format():
    c = row("5511@s.whatsapp.net", unread=2, ts=50, archived=1, pinned=7, muted=99)
    output = cl.chat_to_wpp(c, {"5511@c.us": "Ana"}, wpp_id)
    assert output == {
        "id": wpp_id("5511@s.whatsapp.net"), "name": "Ana", "isGroup": False,
        "unreadCount": 2, "t": 50, "archive": True, "pin": 7,
        "muteExpiration": 99, "isLocked": False, "msgs": None}
    assert cl.chat_to_wpp(row("1@g.us", name="Group"), {}, wpp_id)["isGroup"] is True


def test_the_address_book_name_beats_the_phones_conversation_name_and_the_nickname():
    chat = row("99@lid", name="old name on the phone")
    shown = cl.chat_to_wpp(chat, {"99@lid": "Ana Silva"}, wpp_id, nicknames={"99@lid": "aninha"})
    assert shown["name"] == "Ana Silva"
    assert shown["contact"] == {"pushname": "aninha"}


def test_without_an_address_book_name_the_conversation_name_stays_and_the_nickname_is_no_name():
    chat = row("99@lid", name="")
    shown = cl.chat_to_wpp(chat, {}, wpp_id, nicknames={"99@lid": "aninha"})
    assert shown["name"] is None                  # a nickname is not filed as the saved name
    assert shown["contact"] == {"pushname": "aninha"}
    assert cl.chat_to_wpp(row("99@lid", name="Ana (phone)"), {}, wpp_id)["name"] == "Ana (phone)"
    assert "contact" not in cl.chat_to_wpp(chat, {}, wpp_id)


def test_contact_names_hold_only_the_address_book():
    stored = [{"jid": "5511@s.whatsapp.net", "name": "Ana", "pushname": "a"},
              {"jid": "99@lid", "name": "", "pushname": "nick"}]
    assert cl.contact_names(stored) == {"5511@c.us": "Ana"}


def test_listing_identities_follow_an_lid_to_its_number():
    contact_rows = [{"jid": "99@lid", "name": "", "pushname": "aninha"},
                    {"jid": "5511999990000@s.whatsapp.net", "name": "Ana Silva", "pushname": ""},
                    {"jid": "77@lid", "name": "", "pushname": "so apelido"}]
    chat_rows = [row("99@lid"), row("77@lid"), row("5511999990000@s.whatsapp.net"),
                 row("1203@g.us"), row("status@broadcast")]
    asked = []

    def phone_of(lid):
        asked.append(lid)
        return "5511999990000@s.whatsapp.net" if lid == "99@lid" else ""

    names, nicknames = cl.listing_identities(chat_rows, contact_rows, phone_of)

    assert names == {"99@lid": "Ana Silva", "5511999990000@c.us": "Ana Silva"}
    assert nicknames == {"99@lid": "aninha", "77@lid": "so apelido"}
    assert asked == ["99@lid", "77@lid"]          # only an LID needs the number looked up


def test_a_chat_under_a_number_finds_the_name_saved_under_its_twin_form():
    contact_rows = [{"jid": "5511999990000@s.whatsapp.net", "name": "Bia Lima", "pushname": ""}]
    chat_rows = [row("551199990000@s.whatsapp.net")]

    names, _ = cl.listing_identities(chat_rows, contact_rows, lambda lid: "")

    assert names == {"551199990000@c.us": "Bia Lima"}


def test_select_archived_broadcast_and_muted():
    everything = [row("a@s.whatsapp.net", archived=1), row("b@s.whatsapp.net", muted=500),
                  row("c@s.whatsapp.net", muted=5), row("list@broadcast"),
                  row("status@broadcast", archived=1)]
    assert [c["jid"] for c in cl.select(everything, "all")] == [
        "a@s.whatsapp.net", "b@s.whatsapp.net", "c@s.whatsapp.net", "list@broadcast"]
    assert [c["jid"] for c in cl.select(everything, "archived")] == ["a@s.whatsapp.net"]
    assert [c["jid"] for c in cl.select(everything, "broadcast")] == ["list@broadcast"]
    assert [c["jid"] for c in cl.select(everything, "muted", now=100)] == ["b@s.whatsapp.net"]


def test_of_kind_filters_groups_and_people():
    cs = [row("1@g.us"), row("2@s.whatsapp.net"), row("3@lid")]
    assert len(cl.of_kind(cs, "all")) == 3
    assert [c["jid"] for c in cl.of_kind(cs, "group")] == ["1@g.us"]
    assert [c["jid"] for c in cl.of_kind(cs, "user")] == ["2@s.whatsapp.net", "3@lid"]
    assert cl.of_kind(cs, "community") == []


def test_received_unread_takes_the_last_ones_that_are_not_mine():
    items = [msg("1"), msg("2", from_me=True), msg("3"), msg("4")]
    assert [m["id"]["id"] for m in cl.received_unread(items, 2)] == ["3", "4"]
    assert cl.received_unread(items, 0) == []
    assert [m["id"]["id"] for m in cl.without_mine(items, False)] == ["1", "3", "4"]
    assert len(cl.without_mine(items, True)) == 4


def ask(tmp_path, method, path, prepare):
    """Starts only the reading routes with a real store and makes a request."""
    db = Store(str(tmp_path / "x.db"))
    prepare(db)
    s = N(db=db, votes_wpp=lambda serial: None)

    async def run():
        app = web.Application()
        cl.register_routes(
            app.router, session=lambda req: s,
            ok=lambda **kw: web.json_response(kw),
            error=lambda m, status=500: web.json_response(
                {"status": "error", "message": str(m)}, status=status),
            wpp_id=wpp_id, now=lambda: 100)
        async with TestClient(TestServer(app)) as client:
            response = await client.request(method, path)
            return response.status, await response.json()
    return asyncio.run(run())


def populate(db):
    db.chat("a@s.whatsapp.net", name="Ana", ts=10, unread=1, archived=0)
    db.chat("b@s.whatsapp.net", name="Bia", ts=20, archived=1)
    db.message("a@s.whatsapp.net", msg("m1", t=10))
    db.message("a@s.whatsapp.net", msg("m2", from_me=True, t=11))


def test_route_all_chats_archived(tmp_path):
    status, body = ask(tmp_path, "GET", "/api/t/all-chats-archived", populate)
    assert status == 200
    assert [c["name"] for c in body["response"]] == ["Bia"]


def test_route_all_chats_with_messages_only_with_messages(tmp_path):
    status, body = ask(tmp_path, "GET", "/api/t/all-chats-with-messages", populate)
    assert [c["name"] for c in body["response"]] == ["Ana"]
    assert len(body["response"][0]["msgs"]) == 1
    status, body = ask(tmp_path, "GET",
                       "/api/t/all-chats-with-messages?withNewMessageOnly=true", populate)
    assert [c["name"] for c in body["response"]] == ["Ana"]


def test_route_chat_by_id_finds_or_404(tmp_path):
    status, body = ask(tmp_path, "GET", "/api/t/chat-by-id/a@c.us", populate)
    assert status == 200 and body["response"]["name"] == "Ana"
    status, body = ask(tmp_path, "GET", "/api/t/chat-by-id/zzz@c.us", populate)
    assert status == 404 and body["status"] == "error"


def test_route_all_messages_in_chat_respects_include_me(tmp_path):
    status, body = ask(tmp_path, "GET", "/api/t/all-messages-in-chat/a@c.us", populate)
    assert [m["id"]["id"] for m in body["response"]] == ["m1", "m2"]
    status, body = ask(tmp_path, "GET",
                       "/api/t/load-messages-in-chat/a@c.us?includeMe=false", populate)
    assert [m["id"]["id"] for m in body["response"]] == ["m1"]


def test_route_unread_messages(tmp_path):
    status, body = ask(tmp_path, "GET", "/api/t/all-unread-messages", populate)
    assert [m["id"]["id"] for m in body["response"]] == ["m1"]
    status, body = ask(tmp_path, "GET", "/api/t/unread-messages", populate)
    assert body["response"][0]["name"] == "Ana"
    assert [m["id"]["id"] for m in body["response"][0]["messages"]] == ["m1"]


def test_route_list_mutes_and_votes(tmp_path):
    def with_mute(db):
        populate(db)
        db.chat("b@s.whatsapp.net", muted=2**31 - 1)
    status, body = ask(tmp_path, "GET", "/api/t/list-mutes/all", with_mute)
    assert [c["name"] for c in body["response"]] == ["Bia"]
    status, body = ask(tmp_path, "GET", "/api/t/votes/true_a@c.us_m9", populate)
    assert status == 404


def test_stored_channel_chats_stay_out_of_every_listing():
    rows = [{"jid": "120363@newsletter", "archived": 0, "muted": 0},
            {"jid": "5551@s.whatsapp.net", "archived": 0, "muted": 0}]
    assert [c["jid"] for c in cl.select(rows, "all")] == ["5551@s.whatsapp.net"]
