"""Older messages from the phone (older_history.py): when a request goes out,
what it is anchored on, and how the phone's answer is stored."""
from types import SimpleNamespace as N

import pytest
from aiohttp.test_utils import TestClient, TestServer
from neonize.proto.waHistorySync.WAWebProtobufsHistorySync_pb2 import (
    Conversation, HistorySync, HistorySyncMsg)

import authentication as auth
import older_history
import server
from store import Store
from test_chats_server import LID, ME, PN, Fake, _msg, sessions  # noqa: F401 (fixture)


# ---------------------------------------------------------------- pure rules
def test_with_no_stored_message_only_the_phone_has_the_chat():
    assert older_history.verdict(None, False) == {
        "requested": False, "primaryHasMore": True, "phoneOnly": True}


def test_a_chat_whose_history_ends_here_is_not_asked():
    assert older_history.verdict(1, True)["primaryHasMore"] is False


@pytest.mark.parametrize("end", [2, 3])
def test_a_chat_this_device_may_not_ask_more_of_is_phone_only(end):
    assert older_history.verdict(end, True)["phoneOnly"] is True


@pytest.mark.parametrize("end", [None, 0])
def test_a_chat_with_more_on_the_phone_may_be_asked(end):
    assert older_history.verdict(end, True) is None


def test_the_same_anchor_is_not_asked_twice_within_the_window():
    gate = older_history.AnchorGate(window=120)
    assert gate.take(("c", "A"), now=0) is True
    assert gate.take(("c", "A"), now=60) is False
    assert gate.take(("c", "B"), now=60) is True           # a new anchor: a new page
    assert gate.take(("c", "A"), now=121) is True


def test_a_forgotten_anchor_may_be_asked_again():
    gate = older_history.AnchorGate()
    gate.take(("c", "A"), now=0)
    gate.forget(("c", "A"))
    assert gate.take(("c", "A"), now=1) is True


def test_only_an_on_demand_block_is_one():
    assert older_history.is_on_demand(HistorySync(syncType=HistorySync.ON_DEMAND))
    assert not older_history.is_on_demand(HistorySync(syncType=HistorySync.RECENT))
    assert not older_history.is_on_demand(object())


# ---------------------------------------------------------------- store
def test_the_anchor_is_the_oldest_real_message_of_both_addresses(tmp_path):
    db = Store(str(tmp_path / "bridge.db"))
    db.message(LID, _msg("NEW", 300, LID))
    db.message(PN, _msg("OLD", 100, PN, from_me=True))
    db.message(LID, {**_msg("NOTICE", 50, LID), "type": "gp2"})     # made up by the bridge
    assert db.oldest_real([LID, PN]) == {"chat": PN, "id": "OLD", "fromMe": True, "ts": 100}
    assert db.oldest_real([]) is None
    assert db.oldest_real(["nobody@lid"]) is None


def test_the_history_end_is_read_from_any_address(tmp_path):
    db = Store(str(tmp_path / "bridge.db"))
    db.chat(LID)
    assert db.history_end([LID]) is None
    db.chat(PN, history_end=2)
    assert db.history_end([LID, PN]) == 2


# ---------------------------------------------------------------- routes
class PhoneFake(Fake):
    def __init__(self):
        super().__init__()
        self.history_requests = []

    def request_history_sync(self, chat, message_id, from_me, timestamp, count):
        self.history_requests.append((f"{chat.User}@{chat.Server}", message_id, from_me,
                                      timestamp, count))
        return N(ID="PEER1")


async def _open(tmp_path, sessions):
    http = TestClient(TestServer(server.create_app(str(tmp_path))))
    await http.start_server()
    secret = (tmp_path / auth.SECRET_FILE).read_text(encoding="utf-8")
    token = f"account1:{auth.session_token(secret, 'account1')}"
    await http.get(f"/api/{token}/status-session")
    s = sessions[0]
    s.client, s.status, s.me = PhoneFake(), "CONNECTED", ME
    return http, token, s


@pytest.mark.asyncio
async def test_a_request_is_anchored_on_the_oldest_message_and_sent_once(tmp_path, sessions):
    http, token, s = await _open(tmp_path, sessions)
    try:
        s.db.chat(LID)
        s.db.message(LID, _msg("M2", 200, LID))
        s.db.message(LID, _msg("M1", 100, LID, from_me=True))
        url = f"/api/{token}/request-older-messages/5511999990000@c.us"
        first = await (await http.post(url)).json()
        again = await (await http.post(url)).json()
        assert first["response"] == {"requested": True, "primaryHasMore": True, "phoneOnly": False}
        assert again["response"]["requested"] is True
        assert s.client.history_requests == [(LID, "M1", True, 100, older_history.PAGE)]
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_a_chat_with_nothing_to_anchor_on_sends_nothing(tmp_path, sessions):
    http, token, s = await _open(tmp_path, sessions)
    try:
        body = await (await http.post(f"/api/{token}/request-older-messages/{LID}")).json()
        assert body["response"]["phoneOnly"] is True
        assert s.client.history_requests == []
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_the_state_route_never_sends(tmp_path, sessions):
    http, token, s = await _open(tmp_path, sessions)
    try:
        s.db.chat(LID, history_end=2)
        s.db.message(LID, _msg("M1", 100, LID))
        body = await (await http.get(f"/api/{token}/older-history-state/{LID}")).json()
        assert body["response"]["phoneOnly"] is True
        assert s.client.history_requests == []
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_the_phones_answer_brings_old_messages_and_leaves_the_chat_alone(
        tmp_path, sessions):
    http, token, s = await _open(tmp_path, sessions)
    try:
        s.db.chat(LID, ts=500, unread=3, archived=1)
        s.db.message(LID, _msg("M5", 500, LID))
        old = HistorySyncMsg()
        old.message.key.ID, old.message.key.remoteJID = "M0", LID
        old.message.messageTimestamp = 50
        old.message.message.conversation = "antiga"
        answer = HistorySync(syncType=HistorySync.ON_DEMAND, conversations=[Conversation(
            ID=LID, unreadCount=0, archived=False, conversationTimestamp=50,
            endOfHistoryTransferType=Conversation.COMPLETE_ON_DEMAND_SYNC_BUT_MORE_MSG_REMAIN_ON_PRIMARY,
            messages=[old])])
        s._store_history(answer)

        row = next(c for c in s.db.chats() if c["jid"] == LID)
        assert (row["ts"], row["unread"], row["archived"]) == (500, 3, 1)
        assert s.db.history_end([LID]) == 2
        body = await (await http.get(f"/api/{token}/get-messages/{LID}?count=10")).json()
        assert [m["id"]["id"] for m in body["response"]] == ["M0", "M5"]
    finally:
        await http.close()
