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
def test_where_whatsapp_web_says_use_the_phone_only_home_asks(end):
    """WhatsApp Web stops at 2 and 3 (issue #220: the phone often answers
    nothing, "Sync paused"); the user's Home may ask, the backfill never."""
    assert older_history.verdict(end, True) == {
        "requested": False, "primaryHasMore": True, "phoneOnly": True}
    assert older_history.verdict(end, True, user_asked=True) is None
    assert older_history.marks_on_send(end) is True


@pytest.mark.parametrize("end", [2, 3])
def test_an_anchor_asked_where_the_rest_is_on_the_phone_says_so(end):
    # the top of the chat says "on your phone", not "no older messages"
    assert older_history.verdict(end, True, exhausted=True, user_asked=True) == {
        "requested": False, "primaryHasMore": True, "phoneOnly": True}


def test_an_anchor_the_phone_answered_with_nothing_is_not_asked_again():
    answer = older_history.verdict(None, True, exhausted=True, user_asked=True)
    assert answer == {"requested": False, "primaryHasMore": False, "phoneOnly": False}
    assert older_history.marks_on_send(None) is False


def test_only_the_clients_flag_says_the_user_asked():
    assert older_history.user_asked_of({"userAsked": True}) is True
    for body in ({}, {"userAsked": "true"}, [True], None):
        assert older_history.user_asked_of(body) is False


def test_an_answer_with_only_stored_messages_brings_nothing_new():
    rows = [("c", {"id": {"id": "A"}}), ("c", {"id": {"id": "B"}})]
    assert older_history.brought_nothing_new(rows, lambda mid: True) is True
    assert older_history.brought_nothing_new(rows, lambda mid: mid == "A") is False
    assert older_history.brought_nothing_new([], lambda mid: False) is True


@pytest.mark.parametrize("end", [None, 0])
def test_a_chat_with_more_on_the_phone_may_be_asked(end):
    assert older_history.verdict(end, True) is None


def test_the_same_anchor_is_not_asked_twice_within_the_window():
    gate = older_history.AnchorGate(window=120)
    assert gate.take(("c", "A"), now=0) is True
    assert gate.take(("c", "A"), now=60) is False
    assert gate.take(("c", "B"), now=60) is True           # a new anchor: a new page
    assert gate.take(("c", "A"), now=121) is True


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
        s.db.chat(LID, history_end=1)
        s.db.message(LID, _msg("M1", 100, LID))
        body = await (await http.get(f"/api/{token}/older-history-state/{LID}")).json()
        assert body["response"]["primaryHasMore"] is False
        assert s.client.history_requests == []
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_the_state_route_says_phone_only_where_whatsapp_web_does(tmp_path, sessions):
    http, token, s = await _open(tmp_path, sessions)
    try:
        s.db.chat(LID, history_end=2)
        s.db.message(LID, _msg("M1", 100, LID))
        body = await (await http.get(f"/api/{token}/older-history-state/{LID}")).json()
        assert body["response"]["phoneOnly"] is True
        # and the backfill (no userAsked) is refused the same way
        body = await (await http.post(f"/api/{token}/request-older-messages/{LID}")).json()
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


@pytest.mark.asyncio
async def test_two_accounts_holding_the_same_anchor_are_each_asked(tmp_path, sessions):
    http, token, s = await _open(tmp_path, sessions)
    try:
        secret = (tmp_path / auth.SECRET_FILE).read_text(encoding="utf-8")
        other_token = f"account2:{auth.session_token(secret, 'account2')}"
        await http.get(f"/api/{other_token}/status-session")
        other = sessions[1]
        other.client, other.status, other.me = PhoneFake(), "CONNECTED", ME
        for account in (s, other):
            account.db.chat(LID)
            account.db.message(LID, _msg("M1", 100, LID))
        await http.post(f"/api/{token}/request-older-messages/{LID}")
        await http.post(f"/api/{other_token}/request-older-messages/{LID}")
        assert len(s.client.history_requests) == 1
        assert len(other.client.history_requests) == 1
    finally:
        await http.close()


class FailingPhone(PhoneFake):
    def request_history_sync(self, *args):
        super().request_history_sync(*args)
        raise TimeoutError("no ack")


@pytest.mark.asyncio
async def test_an_unconfirmed_send_is_ambiguous_and_never_resent_at_once(tmp_path, sessions):
    http, token, s = await _open(tmp_path, sessions)
    try:
        s.client = FailingPhone()
        s.db.chat(LID)
        s.db.message(LID, _msg("M1", 100, LID))
        url = f"/api/{token}/request-older-messages/{LID}"
        first = (await (await http.post(url)).json())["response"]
        again = (await (await http.post(url)).json())["response"]
        assert "requested" not in first            # backfill.py: ambiguous, keep the grace
        assert again["requested"] is True           # the anchor stays taken
        assert len(s.client.history_requests) == 1
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_a_build_without_the_request_refuses_before_sending(tmp_path, sessions):
    http, token, s = await _open(tmp_path, sessions)
    try:
        s.client = Fake()
        s.db.chat(LID)
        s.db.message(LID, _msg("M1", 100, LID))
        body = (await (await http.post(f"/api/{token}/request-older-messages/{LID}")).json())
        assert body["response"]["requested"] is False
        assert "missing from" in body["response"]["error"]
    finally:
        await http.close()


def test_an_on_demand_block_leaves_the_pairing_history_progress_alone(tmp_path):
    import history_progress
    s = N(db=Store(str(tmp_path / "bridge.db")), existing_chat=lambda jid: "",
          _history_progress=None, _store_history_block=lambda data: 100)
    server.Session._store_history(s, HistorySync(syncType=HistorySync.ON_DEMAND))
    assert s._history_progress is None
    server.Session._store_history(s, HistorySync(syncType=HistorySync.RECENT))
    assert history_progress.progress_of(s, 0).percent == 100


@pytest.mark.asyncio
async def test_an_answer_under_the_other_address_lands_in_the_stored_chat(tmp_path, sessions):
    http, token, s = await _open(tmp_path, sessions)
    try:
        s.db.chat(LID, ts=500)
        s.db.message(LID, _msg("M5", 500, LID))
        old = HistorySyncMsg()
        old.message.key.ID, old.message.key.remoteJID = "M0", PN
        old.message.messageTimestamp = 50
        old.message.message.conversation = "antiga"
        s._store_history(HistorySync(syncType=HistorySync.ON_DEMAND, conversations=[Conversation(
            ID=PN, endOfHistoryTransferType=2, messages=[old])]))
        assert [c["jid"] for c in s.db.chats()] == [LID]
        assert s.db.history_end([LID]) == 2
        body = await (await http.get(f"/api/{token}/get-messages/{LID}?count=10")).json()
        assert [m["id"]["id"] for m in body["response"]] == ["M0", "M5"]
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_home_asks_the_phone_once_per_anchor_while_it_brings_more(
        tmp_path, sessions, monkeypatch):
    """Where WhatsApp Web says "use the phone" (type 2), Home asks once per
    anchor: a silent phone is never asked again for it, even after the two
    minutes the gate forgets; a batch that does come gives a new anchor."""
    http, token, s = await _open(tmp_path, sessions)
    home = {"userAsked": True}
    clock = [1000.0]
    monkeypatch.setattr(older_history.time, "monotonic", lambda: clock[0])
    try:
        s.db.chat(LID, ts=500, history_end=2)
        s.db.message(LID, _msg("M5", 500, LID))
        url = f"/api/{token}/request-older-messages/{LID}"
        assert (await (await http.post(url, json=home)).json())["response"]["requested"] is True
        assert len(s.client.history_requests) == 1
        # the phone stays silent; Home again, minutes later: not asked again
        clock[0] += 3 * older_history.SAME_ANCHOR_WINDOW
        body = await (await http.post(url, json=home)).json()
        assert body["response"] == {"requested": False, "primaryHasMore": True, "phoneOnly": True}
        assert len(s.client.history_requests) == 1
        # an older batch arrives (late, or by another route): a new anchor
        s.db.message(LID, _msg("M1", 100, LID))
        await http.post(url, json=home)
        assert len(s.client.history_requests) == 2
        # the backfill never asks in this state
        s.db.message(LID, _msg("M0", 50, LID))
        await http.post(url)
        assert len(s.client.history_requests) == 2
    finally:
        await http.close()


@pytest.mark.asyncio
async def test_home_goes_on_until_the_phone_sends_nothing_new(tmp_path, sessions):
    """A chat with no "use the phone" verdict: an empty answer marks the anchor."""
    http, token, s = await _open(tmp_path, sessions)
    try:
        s.db.chat(LID, ts=500)
        s.db.message(LID, _msg("M5", 500, LID))
        await http.post(f"/api/{token}/request-older-messages/{LID}")
        assert len(s.client.history_requests) == 1
        s._store_history(HistorySync(syncType=HistorySync.ON_DEMAND, conversations=[
            Conversation(ID=LID)]))
        body = await (await http.post(f"/api/{token}/request-older-messages/{LID}")).json()
        assert body["response"]["primaryHasMore"] is False
        assert len(s.client.history_requests) == 1
        s.db.message(LID, _msg("M1", 100, LID))
        await http.post(f"/api/{token}/request-older-messages/{LID}")
        assert len(s.client.history_requests) == 2
    finally:
        await http.close()
