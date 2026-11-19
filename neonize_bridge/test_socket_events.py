"""WPP event contracts measured over CDP; fake traffic never reaches WhatsApp."""
import hashlib
from types import SimpleNamespace as N

import pytest
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

import msgmap
import socket_events
from test_group_receipts import session, GROUP, MY_LID, jid_n


def poll():
    msg = Message()
    msg.pollCreationMessage.name = "Question"
    for option in ("Yes", "No"):
        msg.pollCreationMessage.options.add(optionName=option)
    return msgmap.build(chat=GROUP, from_me=False, id="P" * 22,
                        sender="900@lid", ts=1, message=msg)


@pytest.mark.parametrize("ts", [1_700_000_000, 1_700_000_000_000])
def test_poll_event_has_sparse_option_objects_serialized_wids_and_milliseconds(ts):
    data = socket_events.poll_response("account1", poll(), [1], "901:4@lid", ts)
    assert data["selectedOptions"] == [None, {"name": "No", "localId": 1}]
    assert data["timestamp"] == 1_700_000_000_000
    assert data["sender"] == "901@lid"
    assert data["msgId"]["participant"] == "900@lid"
    assert data["chatId"].endswith("@g.us")
    assert data["msgId"]["remote"] == data["chatId"]
    assert socket_events.poll_response("s", poll(), [], "901@lid", ts)["selectedOptions"] == []


def test_old_stored_keys_keep_the_participant_when_converted_to_wire():
    key = poll()["id"]
    key.pop("participant")
    assert socket_events.wire_key(key)["participant"] == "900@lid"


def event(message, mid="VOTE"):
    return N(UnavailableRequestID="", Info=N(
        ID=mid, Timestamp=1_700_000_000, Pushname="",
        MessageSource=N(Chat=jid_n(GROUP), Sender=jid_n("901@lid"),
                        IsFromMe=False, IsGroup=True)), Message=message)


def receive(s, e):
    import neonize.events as nev
    s.handlers[nev.MessageEv](s.client, e)


def test_vote_is_stored_before_its_event_and_a_retraction_is_emitted(session):
    s, original = session, poll()
    mid = original["id"]["id"]
    s.db.message(GROUP, original)
    hashes = [hashlib.sha256(b"No").digest()]
    s.client.decrypt_poll_vote = lambda e: N(selectedOptions=hashes)
    msg = Message()
    msg.pollUpdateMessage.pollCreationMessageKey.ID = mid
    msg.pollUpdateMessage.senderTimestampMS = 1_700_000_000_123
    receive(s, event(msg))
    assert s.db.votes_of(mid) == [("901@lid", [1])]
    assert [name for name, _ in s.emitted] == ["onpollresponse"]
    assert s.emitted[0][1]["msgId"]["_serialized"] == original["id"]["_serialized"]
    assert s.emitted[0][1]["timestamp"] == 1_700_000_000_123
    hashes.clear()
    receive(s, event(msg, "RETRACT"))
    assert s.db.votes_of(mid) == []
    assert s.emitted[-1][1]["selectedOptions"] == []


def test_unreadable_or_unmatched_vote_does_not_erase_the_existing_vote(session):
    s, original = session, poll()
    mid = original["id"]["id"]
    s.db.message(GROUP, original)
    s.db.vote(mid, "901@lid", [0], 1)
    msg = Message()
    msg.pollUpdateMessage.pollCreationMessageKey.ID = mid
    s.client.decrypt_poll_vote = lambda e: N(selectedOptions=[b"unknown hash"])
    receive(s, event(msg))
    assert s.db.votes_of(mid) == [("901@lid", [0])]
    assert s.emitted == []
    def fail(e):
        raise ValueError("no secret")
    s.client.decrypt_poll_vote = fail
    receive(s, event(msg))
    assert s.db.votes_of(mid) == [("901@lid", [0])]
    assert s.emitted == []


def test_revocation_preserves_target_key_and_emits_distinct_notice_id(session):
    s = session
    original = msgmap.build(chat=GROUP, from_me=True, id="T" * 32,
                            sender=MY_LID, ts=1, message=Message(conversation="text"))
    s.db.message(GROUP, original)
    msg = Message()
    msg.protocolMessage.type = 0
    msg.protocolMessage.key.ID = original["id"]["id"]
    receive(s, event(msg, "R" * 20))
    assert [name for name, _ in s.emitted] == ["received-message", "onrevokedmessage"]
    notice, data = s.emitted[0][1]["response"], s.emitted[1][1]
    assert notice["protocolMessageKey"] == original["id"]
    assert data["refId"] == original["id"]["_serialized"]
    assert data["id"] == notice["id"]["_serialized"] != data["refId"]
    assert data["author"] == "901@lid"
    assert data["from"] == GROUP
    assert s.db.wpp_message(original["id"]["id"])["type"] == "revoked"


@pytest.mark.parametrize("length", [18, 20, 22, 32])
def test_group_and_status_keys_keep_variable_message_ids_and_lid_authors(length):
    for chat in (GROUP, "status@broadcast"):
        record = msgmap.build(chat=chat, from_me=False, id="X" * length,
                              sender="901:2@lid", ts=1, message=Message(conversation="x"))
        assert record["id"]["_serialized"] == f"false_{chat}_{'X' * length}_901@lid"
        assert record["id"]["participant"] == "901@lid"


def test_revoke_target_carries_the_explicit_participant_without_a_stored_original():
    msg = Message()
    msg.protocolMessage.key.ID = "TARGET"
    msg.protocolMessage.key.remoteJID = GROUP
    msg.protocolMessage.key.participant = "900@lid"
    record = msgmap.build(chat=GROUP, from_me=False, id="REVOKE", sender="901@lid",
                          ts=1, message=msg)
    assert record["protocolMessageKey"]["_serialized"] == f"false_{GROUP}_TARGET_900@lid"
