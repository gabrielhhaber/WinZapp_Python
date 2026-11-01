"""Chat and message routes on the real server (create_app) against a fake neonize
client: the format of the answers the WinZapp client reads, without WhatsApp.

Nothing here talks to a real account: the fake only keeps what the bridge asked of it."""
import json
from types import SimpleNamespace as N

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

import authentication as auth
import server

PN = "5511999990000@s.whatsapp.net"
LID = "111222333@lid"
ME = "5500000000@s.whatsapp.net"


class _Jid:
    def __init__(self, user, server):
        self.User, self.Server = user, server


class Fake:
    """What the bridge asks of neonize, recorded."""

    def __init__(self):
        self.sent, self.presences, self.revoked, self.edited = [], [], [], []
        self.read, self.send_error = [], None

    def send_message(self, destination, message):
        if self.send_error:
            raise RuntimeError(self.send_error)
        self.sent.append((f"{destination.User}@{destination.Server}", message))
        if isinstance(message, str):
            message = Message(conversation=message)
        return N(ID="NEW1", Timestamp=1_700_000_000, Message=message)

    def get_lid_from_pn(self, jid):
        return _Jid("111222333", "lid") if jid.User == "5511999990000" else _Jid("", "")

    def get_pn_from_lid(self, jid):
        return _Jid("5511999990000", "s.whatsapp.net") if jid.User == "111222333" else _Jid("", "")

    def send_chat_presence(self, jid, presence, media):
        self.presences.append((f"{jid.User}@{jid.Server}", presence.name, media.name))

    def mark_read(self, *ids, chat, sender, receipt):
        if self.read_error:
            raise RuntimeError("failed")
        self.read.append((ids, f"{chat.User}@{chat.Server}"))

    read_error = False

    def revoke_message(self, chat, sender, mid):
        self.revoked.append((f"{chat.User}@{chat.Server}", f"{sender.User}@{sender.Server}", mid))

    def edit_message(self, chat, mid, new):
        self.edited.append((f"{chat.User}@{chat.Server}", mid))


@pytest.fixture
def sessions(monkeypatch):
    created = []

    class Registered(server.Session):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            created.append(self)
    monkeypatch.setattr(server, "Session", Registered)
    return created


class Bridge:
    def __init__(self, http, token, s, fake):
        self.http, self.token, self.s, self.fake = http, token, s, fake

    def url(self, route):
        return f"/api/{self.token}/{route}"

    async def post(self, route, body=None, **kw):
        response = await self.http.post(self.url(route), json=body or {}, **kw)
        return response.status, await response.json()

    async def get(self, route):
        response = await self.http.get(self.url(route))
        return response.status, await response.json()


async def _open(tmp_path, sessions, connected=True):
    http = TestClient(TestServer(server.create_app(str(tmp_path))))
    await http.start_server()
    secret = (tmp_path / auth.SECRET_FILE).read_text(encoding="utf-8")
    token = f"account1:{auth.session_token(secret, 'account1')}"
    await http.get(f"/api/{token}/status-session")          # creates the Session
    s = sessions[0]
    fake = Fake()
    if connected:
        s.client, s.status, s.me = fake, "CONNECTED", ME
    return Bridge(http, token, s, fake)


def _msg(mid, ts, chat, from_me=False, **extra):
    w = {"id": {"id": mid, "fromMe": from_me, "remote": chat,
                "_serialized": f"{'true' if from_me else 'false'}_{chat}_{mid}"},
         "fromMe": from_me, "t": ts, "timestamp": ts, "type": "chat", "body": mid,
         "from": "" if from_me else chat, "to": chat}
    w.update(extra)
    return w


ROUTES_NEEDING_CONNECTION = [
    ("send-message", {"phone": "5511@c.us", "message": "oi"}),
    ("send-reply", {"phone": "5511@c.us", "message": "oi", "messageId": "false_5511@c.us_X"}),
    ("send-mentioned", {"phone": "5511@c.us", "message": "oi"}),
    ("send-voice-base64", {"phone": "5511@c.us", "base64Ptt": "eA=="}),
    ("send-poll-message", {"phone": "5511@c.us", "name": "p", "choices": ["a", "b"]}),
    ("vote-poll", {"messageId": "true_5511@c.us_X", "localIds": [0]}),
    ("react-message", {"msgId": "true_5511@c.us_X", "reaction": "x"}),
    ("forward-messages", {"phone": "5511@c.us", "messageId": ["true_5511@c.us_X"]}),
    ("edit-message", {"id": "true_5511@c.us_X", "newText": "new"}),
    ("delete-message", {"messageId": "true_5511@c.us_X"}),
    ("send-seen", {"phone": "5511@c.us"}),
    ("mark-played", {"messageId": "false_5511@c.us_X"}),
    ("star-message", {"messageId": "true_5511@c.us_X"}),
    ("pin-message", {"messageId": "true_5511@c.us_X"}),
    ("pin-chat", {"phone": "5511@c.us"}),
    ("archive-chat", {"phone": "5511@c.us"}),
    ("send-mute", {"phone": "5511@c.us", "time": 1, "type": "hours"}),
    ("typing", {"phone": "5511@c.us", "value": True}),
    ("recording", {"phone": "5511@c.us", "value": True}),
    ("chat-state", {"phone": "5511@c.us", "chatstate": 0}),
    ("clear-chat", {"phone": ["5511@c.us"]}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("route,body", ROUTES_NEEDING_CONNECTION)
async def test_without_a_connection_every_route_answers_the_real_servers_404_disconnected(
        tmp_path, sessions, route, body):
    bridge = await _open(tmp_path, sessions, connected=False)
    try:
        status, response = await bridge.post(route, body)
        assert status == 404, route
        assert response["status"] == "Disconnected" and response["response"] is None
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_send_file_without_a_connection_answers_disconnected(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions, connected=False)
    try:
        form = FormData()
        form.add_field("file", b"x", filename="a.bin", content_type="application/octet-stream")
        response = await bridge.http.post(bridge.url("send-file"), data=form)
        assert response.status == 404 and (await response.json())["status"] == "Disconnected"
    finally:
        await bridge.http.close()


# ------------------------------------------------------------ get-messages
@pytest.mark.asyncio
async def test_get_messages_finds_the_chat_stored_by_lid_when_asked_by_number(
        tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.chat(LID, name="Ana")
        bridge.s.db.message(LID, _msg("M1", 100, LID))
        bridge.s.db.message(LID, _msg("M2", 101, LID))
        status, body = await bridge.get("get-messages/5511999990000@c.us?count=10")
        assert status == 200
        assert [m["id"]["id"] for m in body["response"]] == ["M1", "M2"]
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_get_messages_joins_the_rows_of_both_addresses(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.chat(LID)
        bridge.s.db.chat(PN)
        bridge.s.db.message(PN, _msg("M1", 100, PN))
        bridge.s.db.message(LID, _msg("M2", 101, LID))
        _, body = await bridge.get(f"get-messages/{LID}?count=10")
        assert [m["id"]["id"] for m in body["response"]] == ["M1", "M2"]
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_get_messages_before_with_an_unknown_anchor_returns_empty(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.chat(LID)
        for n in range(1, 5):
            bridge.s.db.message(LID, _msg(f"M{n}", 100 + n, LID))
        _, body = await bridge.get(f"get-messages/{LID}?count=3&direction=before&id=false_{LID}_ZZZ")
        assert body["response"] == []
        _, body = await bridge.get(f"get-messages/{LID}?count=3&direction=before&id=false_{LID}_M3")
        assert [m["id"]["id"] for m in body["response"]] == ["M1", "M2"]
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_get_messages_carries_ack_on_mine_and_star_always(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.chat(PN)
        bridge.s.db.message(PN, _msg("M1", 100, PN, from_me=True))
        bridge.s.db.message(PN, _msg("M2", 101, PN, from_me=True))
        bridge.s.db.message(PN, _msg("M3", 102, PN))
        bridge.s.db.receipt("M1", PN, "read", 110)
        _, body = await bridge.get(f"get-messages/{PN}?count=10")
        by_id = {m["id"]["id"]: m for m in body["response"]}
        assert by_id["M1"]["ack"] == 3 and by_id["M2"]["ack"] == 1
        assert "ack" not in by_id["M3"]
        assert all(m["star"] is False for m in by_id.values())
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_message_by_id_answers_data_like_the_real_server(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.message(PN, _msg("M1", 100, PN, from_me=True))
        bridge.s.db.receipt("M1", PN, "delivered", 110)
        status, body = await bridge.get(f"message-by-id/true_{PN}_M1")
        assert status == 200
        data = body["response"]["data"]
        assert data["id"]["id"] == "M1" and data["ack"] == 2 and data["star"] is False
    finally:
        await bridge.http.close()


# --------------------------------------------------------------- send-seen
@pytest.mark.asyncio
async def test_send_seen_of_an_unknown_chat_confirms_false(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        status, body = await bridge.post("send-seen", {"phone": "9999@c.us"})
        assert status == 200 and body["response"]["data"] == [False]
        assert not bridge.s.db.chat_exists("9999@s.whatsapp.net")      # no new row
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_send_seen_finds_the_chat_by_the_other_address_and_confirms(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.chat(LID, unread=1)
        bridge.s.db.message(LID, _msg("M1", 100, LID))
        bridge.s.state_read = lambda chat, read: True
        status, body = await bridge.post("send-seen", {"phone": "5511999990000@c.us"})
        assert body["response"]["data"] == [True]
        assert bridge.s.db.unread(LID) == 0
        assert bridge.fake.read == [(("M1",), LID)]
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_send_seen_keeps_the_open_chat_and_mark_as_unread_does_not(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.chat(PN, unread=0)
        bridge.s.state_read = lambda chat, read: True
        assert bridge.s.open_chat == ""
        await bridge.post("send-seen", {"phone": PN})
        assert bridge.s.open_chat == PN
        bridge.s.open_chat = ""
        await bridge.post("send-seen", {"phone": PN, "unread": True})
        assert bridge.s.open_chat == ""
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_send_seen_with_a_receipt_that_did_not_go_out_does_not_confirm(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.chat(PN, unread=1)
        bridge.s.db.message(PN, _msg("M1", 100, PN))
        bridge.s.state_read = lambda chat, read: True
        bridge.fake.read_error = True
        _, body = await bridge.post("send-seen", {"phone": PN})
        assert body["response"]["data"] == [False]
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_send_seen_with_nothing_to_read_confirms(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.chat(PN, unread=0)
        bridge.s.state_read = lambda chat, read: True
        _, body = await bridge.post("send-seen", {"phone": PN})
        assert body["response"]["data"] == [True]
    finally:
        await bridge.http.close()


# ------------------------------------------- archive / pin / mute
@pytest.mark.asyncio
async def test_archive_chat_acts_on_the_real_row_under_the_other_address(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.chat(LID, name="Ana")
        bridge.s.send_state = lambda *a, **k: True
        status, _ = await bridge.post("archive-chat", {"phone": "5511999990000@c.us", "value": True})
        assert status == 200
        rows = {c["jid"]: c for c in bridge.s.db.chats()}
        assert set(rows) == {LID}                       # no new empty row
        assert rows[LID]["archived"] == 1
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_pin_and_mute_follow_the_two_rows_of_the_chat(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.chat(LID)
        bridge.s.db.chat(PN)
        bridge.s.send_state = lambda *a, **k: True
        assert (await bridge.post("pin-chat", {"phone": LID, "state": "true"}))[0] == 200
        assert (await bridge.post("send-mute", {"phone": LID, "time": 1, "type": "hours"}))[0] == 200
        rows = {c["jid"]: c for c in bridge.s.db.chats()}
        assert rows[LID]["pinned"] > 0 and rows[PN]["pinned"] == rows[LID]["pinned"]
        assert rows[LID]["muted"] > 0 and rows[PN]["muted"] == rows[LID]["muted"]
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_clear_chat_acts_on_the_chat_stored_by_lid(tmp_path, sessions, monkeypatch):
    import chat_actions
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.chat(LID)
        bridge.s.db.message(LID, _msg("M1", 100, LID))
        calls = []
        monkeypatch.setattr(chat_actions, "clear_on_phone",
                            lambda s, jid, keep: calls.append(jid) or True)
        status, _ = await bridge.post("clear-chat", {"phone": ["5511999990000@c.us"]})
        assert status == 200 and calls == [LID]
    finally:
        await bridge.http.close()


# ------------------------------------------------------- typing / recording
@pytest.mark.asyncio
async def test_typing_and_recording_send_the_presence_to_whatsapp(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        assert (await bridge.post("typing", {"phone": PN, "value": True, "isGroup": False}))[0] == 200
        await bridge.post("typing", {"phone": PN, "value": False})
        await bridge.post("recording", {"phone": PN, "duration": 0, "value": True})
        await bridge.post("recording", {"phone": "1203@g.us", "value": False, "isGroup": True})
        assert bridge.fake.presences == [
            (PN, "CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_TEXT"),
            (PN, "CHAT_PRESENCE_PAUSED", "CHAT_PRESENCE_MEDIA_TEXT"),
            (PN, "CHAT_PRESENCE_COMPOSING", "CHAT_PRESENCE_MEDIA_AUDIO"),
            ("1203@g.us", "CHAT_PRESENCE_PAUSED", "CHAT_PRESENCE_MEDIA_TEXT")]
    finally:
        await bridge.http.close()


# ---------------------------------------------------- history-sync-status
@pytest.mark.asyncio
async def test_history_status_of_a_new_account_says_the_history_is_still_arriving(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        _, body = await bridge.get("history-sync-status")
        r = body["response"]
        assert r["recentCompleted"] is False and r["initialSyncComplete"] is False
        bridge.s.db.chat(PN)                    # there are already stored chats
        _, body = await bridge.get("history-sync-status")
        assert body["response"]["recentCompleted"] is True
        assert body["response"]["storeCounts"]["chat"] == 1
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_history_status_follows_the_progress_of_the_chunks(tmp_path, sessions):
    import history_progress as hp
    bridge = await _open(tmp_path, sessions)
    try:
        progress = hp.progress_of(bridge.s, 0)
        progress.started(0)
        _, body = await bridge.get("history-sync-status")
        assert body["response"]["unprocessedChunks"] == 1
        assert body["response"]["recentCompleted"] is False
        progress.finished(0, percent=100)
        _, body = await bridge.post("unblock-history-sync")
        assert body["response"]["unprocessedChunks"] == 0
        assert body["response"]["recentCompleted"] is True
    finally:
        await bridge.http.close()


# -------------------------------------------------------------- edit-message
@pytest.mark.asyncio
async def test_edit_message_refused_before_sending_says_cannot_edit(tmp_path, sessions):
    import time
    bridge = await _open(tmp_path, sessions)
    try:
        now = int(time.time())
        bridge.s.db.message(PN, _msg("OLD", now - 3600, PN, from_me=True))
        bridge.s.db.message(PN, _msg("THEIRS", now, PN))
        cases = [f"true_{PN}_OLD", f"false_{PN}_THEIRS", f"true_{PN}_UNKNOWN"]
        for serial in cases:
            response = await bridge.http.post(
                bridge.url("edit-message"), json={"id": serial, "newText": "new"})
            assert response.status == 400 and "Cannot edit this message" in await response.text()
        assert bridge.fake.edited == []                 # nothing reached WhatsApp
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_edit_message_within_the_window_edits(tmp_path, sessions):
    import time
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.message(PN, _msg("FRESH", int(time.time()) - 60, PN, from_me=True))
        status, body = await bridge.post("edit-message", {"id": f"true_{PN}_FRESH", "newText": "x"})
        assert status == 200 and body["response"] == {"edited": True}
        assert bridge.fake.edited == [(PN, "FRESH")]
    finally:
        await bridge.http.close()


# ------------------------------------------------------------ missing quote
@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["send-reply", "send-mentioned"])
async def test_unknown_quote_is_the_500_refusal_the_client_understands(
        tmp_path, sessions, route):
    bridge = await _open(tmp_path, sessions)
    try:
        status, body = await bridge.post(
            route, {"phone": PN, "message": "oi", "messageId": f"false_{PN}_DOESNOTEXIST"})
        assert status == 500
        assert body == {"status": "error", "code": "quoted_message_not_found",
                        "stage": "quote_resolution", "sendAttempted": False}
        assert bridge.fake.sent == []
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_send_file_with_an_unknown_quote_does_not_send_without_quoting(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        form = FormData()
        form.add_field("phone", PN)
        form.add_field("quotedMessageId", f"false_{PN}_DOESNOTEXIST")
        form.add_field("file", b"x", filename="a.bin", content_type="application/octet-stream")
        response = await bridge.http.post(bridge.url("send-file"), data=form)
        body = await response.json()
        assert response.status == 500 and body["code"] == "quoted_message_not_found"
        assert body["sendAttempted"] is False and bridge.fake.sent == []
    finally:
        await bridge.http.close()


# ---------------------------------------------------------- link preview and errors
@pytest.mark.asyncio
async def test_send_message_carries_the_link_card_the_client_built(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        preview = {"title": "Title", "description": "Description",
                   "canonicalUrl": "https://example.org/a", "matchedText": "https://example.org/a"}
        status, _ = await bridge.post("send-message", {
            "phone": [PN], "message": "see https://example.org/a",
            "options": {"linkPreview": preview}})
        assert status == 200
        _, sent = bridge.fake.sent[0]
        e = sent.extendedTextMessage
        assert (e.text, e.title, e.description, e.matchedText) == (
            "see https://example.org/a", "Title", "Description", "https://example.org/a")
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_send_message_without_a_preview_goes_as_plain_text(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        await bridge.post("send-message", {"phone": [PN], "message": "oi",
                                           "options": {"linkPreview": False}})
        assert bridge.fake.sent[0][1] == "oi"
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_send_reply_carries_the_quote_and_the_link_card(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.message(PN, _msg("ORIG", 100, PN))
        await bridge.post("send-reply", {
            "phone": PN, "message": "https://example.org/b", "messageId": f"false_{PN}_ORIG",
            "options": {"linkPreview": {"title": "T", "canonicalUrl": "https://example.org/b"}}})
        e = bridge.fake.sent[0][1].extendedTextMessage
        assert e.contextInfo.stanzaID == "ORIG" and e.title == "T"
        assert e.matchedText == "https://example.org/b"
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("route,body", [
    ("send-message", {"phone": PN, "message": "oi"}),
    ("send-poll-message", {"phone": PN, "name": "p", "choices": ["a", "b"]}),
])
async def test_neonize_error_becomes_error_json_and_not_plain_text(
        tmp_path, sessions, route, body):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.fake.send_error = "boom"
        bridge.fake.build_poll_vote_creation = lambda *a: object()
        response = await bridge.http.post(bridge.url(route), json=body)
        assert response.status == 500
        assert response.content_type == "application/json"
        assert (await response.json())["status"] == "error"
    finally:
        await bridge.http.close()


# ------------------------------------------------------- delete for everyone
@pytest.mark.asyncio
async def test_admin_revokes_another_persons_message_in_a_group(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        group = "1203@g.us"
        bridge.s.db.message(group, _msg("THEIRS", 100, group, author="5551@c.us"))
        status, body = await bridge.post(
            "delete-message", {"messageId": f"false_{group}_THEIRS_5551@c.us"})
        assert status == 200
        assert bridge.fake.revoked == [(group, "5551@s.whatsapp.net", "THEIRS")]
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_revoking_another_persons_message_in_a_private_chat_is_still_refused(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.message(PN, _msg("THEIRS", 100, PN))
        status, _ = await bridge.post("delete-message", {"messageId": f"false_{PN}_THEIRS"})
        assert status == 500 and bridge.fake.revoked == []
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_revoking_my_own_message_is_still_signed_by_me(tmp_path, sessions):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.message(PN, _msg("MINE", 100, PN, from_me=True))
        status, _ = await bridge.post("delete-message", {"messageId": f"true_{PN}_MINE"})
        assert status == 200
        assert bridge.fake.revoked == [(PN, ME, "MINE")]
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body,delete_media", [
    ({}, True), ({"deleteMediaInDevice": True}, True), ({"deleteMediaInDevice": False}, False)])
async def test_delete_for_me_carries_the_keep_on_phone_choice(tmp_path, sessions, body, delete_media):
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.message(PN, _msg("PHOTO", 100, PN))
        sent = []
        bridge.s.send_state = lambda name, version, patch, value, *a, **k: sent.append(value) or True
        status, _ = await bridge.post("delete-message", dict(
            body, messageId=f"false_{PN}_PHOTO", onlyLocal=True))
        assert status == 200
        assert sent[0].deleteMessageForMeAction.deleteMedia is delete_media
        assert sent[0].deleteMessageForMeAction.messageTimestamp == 100
    finally:
        await bridge.http.close()


# -------------------------------------------------------------- forward
@pytest.mark.asyncio
async def test_forward_messages_returns_the_new_ids_and_stores_the_copy_in_the_destination(
        tmp_path, sessions):
    """The copy is stored in the destination chat, the ids go in the answer and
    the copy is announced through received-message (issue #656: without it the
    client only showed a forwarded file after a manual refresh)."""
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.message(PN, _msg("ORIG", 100, PN))
        destination = "5522888880000@s.whatsapp.net"
        events = []
        bridge.s.emit = lambda event, data: events.append((event, data))
        status, body = await bridge.post("forward-messages", {
            "phone": ["5522888880000@c.us"], "messageId": [f"false_{PN}_ORIG"]})
        assert status == 200
        assert body["response"] == [{"forwarded": 1, "messageIds": ["NEW1"]}]
        assert bridge.fake.sent[0][1].extendedTextMessage.contextInfo.isForwarded
        assert bridge.s.db.chat_exists(destination)
        assert bridge.s.db.wpp_message("NEW1") is not None
        assert [(e, d["response"]["id"]["id"]) for e, d in events] == [
            ("received-message", "NEW1")]
    finally:
        await bridge.http.close()


@pytest.mark.asyncio
async def test_get_messages_reads_the_store_off_the_event_loop(tmp_path, sessions):
    """A page of messages, each with its group ack, once held the loop for a
    minute on a large store and dropped the client's socket."""
    import threading
    bridge = await _open(tmp_path, sessions)
    try:
        bridge.s.db.message(PN, _msg("M1", 100, PN, from_me=True))
        loop_thread = threading.get_ident()
        seen = []
        real = bridge.s.db.for_response
        bridge.s.db.for_response = lambda *a, **k: seen.append(threading.get_ident()) or real(*a, **k)
        status, body = await bridge.get(f"get-messages/{PN}?count=20")
        assert status == 200 and [m["id"]["id"] for m in body["response"]] == ["M1"]
        assert seen and seen[0] != loop_thread
    finally:
        await bridge.http.close()
