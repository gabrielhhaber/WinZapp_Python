"""Reaction payloads measured with a real group add/remove over CDP.

The live WPP payload has two complete MsgKeys and a second-resolution timestamp;
the bridge used to send two strings inside a response envelope.
"""
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

import msgmap
import socket_events
from test_group_receipts import session, GROUP, MY_LID, jid_n
from test_socket_events import event, receive


def original(s):
    record = msgmap.build(chat=GROUP, from_me=True, id="TARGET",
                          sender=MY_LID, ts=1, message=Message(conversation="test"))
    s.db.message(GROUP, record)
    return record


def reaction(own=False, text="x"):
    msg = Message()
    msg.reactionMessage.key.ID = "TARGET"
    msg.reactionMessage.key.fromMe = own
    msg.reactionMessage.key.participant = MY_LID
    msg.reactionMessage.text = text
    msg.reactionMessage.senderTimestampMS = 1_700_000_004_987
    e = event(msg, "REACTION")
    e.Info.MessageSource.IsFromMe = own
    if own:
        e.Info.MessageSource.Sender = jid_n(MY_LID)
    return e


def test_incoming_reaction_preserves_stored_target_identity_and_its_own_key(session):
    record = original(session)
    receive(session, reaction())
    name, payload = session.emitted[-1]
    assert name == "onreactionmessage" and "response" not in payload
    assert payload == {
        "session": "account1", "id": msgmap.message_key(GROUP, False, "REACTION", "901@lid"),
        "msgId": record["id"], "reactionText": "x", "read": False,
        "sender": "901@lid", "orphan": 0, "orphanReason": None,
        "timestamp": 1_700_000_004}
    assert session.db.reactions_of("TARGET") == [("901@lid", "x")]


def test_own_reaction_and_removal_keep_full_keys_and_do_not_become_chat_messages(session):
    record = original(session)
    receive(session, reaction(own=True))
    assert session.emitted[-1][1]["read"] is True
    assert session.emitted[-1][1]["id"]["participant"] == MY_LID
    assert session.db.reactions_of("TARGET") == [("me", "x")]
    receive(session, reaction(own=True, text=""))
    payload = session.emitted[-1][1]
    assert payload["reactionText"] == "" and payload["msgId"] == record["id"]
    assert session.db.reactions_of("TARGET") == []
    assert all(name == "onreactionmessage" for name, _ in session.emitted)


def test_unknown_target_retains_fallback_identity_but_is_marked_locally_orphaned(session):
    receive(session, reaction())
    payload = session.emitted[-1][1]
    assert payload["orphan"] == 1
    assert payload["msgId"]["id"] == "TARGET"
    assert payload["msgId"]["participant"] == MY_LID


def test_old_build_timestamp_falls_back_to_message_info_seconds(session):
    original(session)
    e = reaction()
    e.Message.reactionMessage.ClearField("senderTimestampMS")
    receive(session, e)
    assert session.emitted[-1][1]["timestamp"] == 1_700_000_000


def test_private_reaction_has_no_invented_group_participant():
    data = socket_events.reaction_message("s", "1@s.whatsapp.net", "R", False,
        "1:2@s.whatsapp.net", "true_1@c.us_TARGET", "x", 1_700_000_000)
    assert "participant" not in data["id"] and "participant" not in data["msgId"]
    assert data["sender"] == "1@c.us"
