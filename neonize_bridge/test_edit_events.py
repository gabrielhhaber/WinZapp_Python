"""Editing keeps the message key and records a distinct complete edit key.

Measured over CDP: chat.msg_edited carries the original serialized id; its
message's latestEditMsgKey names a different protocol message, with the author
in groups. The live latestEditSenderTimestampMs field held seconds despite its name.
"""
from types import SimpleNamespace as N

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, ProtocolMessage

import msgmap
from test_group_receipts import session, GROUP, MY_LID
from test_socket_events import event, receive


def original(s, own=False, author="901@lid"):
    record = msgmap.build(chat=GROUP, from_me=own, id="O" * 22,
        sender=MY_LID if own else author, ts=1, message=Message(conversation="before"))
    record["quotedStanzaID"] = "QUOTE"
    s.db.message(GROUP, record)
    return record


def test_received_edit_emits_the_original_message_with_a_distinct_complete_edit_key(session):
    record = original(session)
    msg = Message()
    msg.protocolMessage.type = ProtocolMessage.MESSAGE_EDIT
    msg.protocolMessage.key.ID = record["id"]["id"]
    msg.protocolMessage.editedMessage.conversation = "after"
    msg.protocolMessage.timestampMS = 1_700_000_005_567
    e = event(msg, "E" * 32)
    receive(session, e)
    assert [name for name, _ in session.emitted] == ["received-message"]
    edited = session.emitted[0][1]["response"]
    assert edited["id"] == record["id"] and edited["body"] == "after"
    assert edited["t"] == 1 and edited["quotedStanzaID"] == "QUOTE"
    assert edited["latestEditMsgKey"] == msgmap.message_key(GROUP, False, "E" * 32, "901@lid")
    assert edited["latestEditSenderTimestampMs"] == 1_700_000_005
    assert session.db.wpp_message("E" * 32) is None


def test_successful_local_edit_uses_the_actual_send_response_id(session):
    record = original(session, own=True)
    sent = []
    def send(chat, mid, message):
        sent.append((mid, message.conversation))
        return N(ID="EDIT_NOTICE", Timestamp=1_700_000_007)
    session.client.edit_message = send
    assert session.edit(record["id"]["_serialized"], "after") is True
    edited = session.db.wpp_message(record["id"]["id"])
    assert sent == [(record["id"]["id"], "after")]
    assert edited["id"] == record["id"] and edited["t"] == 1
    assert edited["latestEditMsgKey"] == msgmap.message_key(GROUP, True, "EDIT_NOTICE", MY_LID)
    assert edited["latestEditSenderTimestampMs"] == 1_700_000_007
    assert session.db.wpp_message("EDIT_NOTICE") is None


def test_group_edit_of_an_old_stored_record_recovers_its_explicit_participant(session):
    record = original(session, own=True)
    record["id"].pop("participant")
    session.db.message(GROUP, record)
    edited = session.mark_edited(record["id"]["id"], GROUP, "EDIT", True,
                                 edit_timestamp=1_700_000_000, body="after")
    assert edited["latestEditMsgKey"]["participant"] == MY_LID


def test_private_edit_does_not_add_a_group_participant(session):
    chat = "901@lid"
    record = msgmap.build(chat=chat, from_me=False, id="P", sender=chat,
                         ts=1, message=Message(conversation="before"))
    session.db.message(chat, record)
    edited = session.mark_edited("P", chat, "EDIT", False,
        edit_timestamp=1_700_000_000, edit_participant=chat, body="after")
    assert "participant" not in edited["latestEditMsgKey"]
    assert edited["latestEditMsgKey"]["_serialized"] == "false_901@lid_EDIT"


def test_failed_local_edit_keeps_the_original_body_and_edit_marker(session):
    record = original(session, own=True)
    def fail(*args):
        raise RuntimeError("send failed")
    session.client.edit_message = fail
    assert session.edit(record["id"]["_serialized"], "after") is False
    assert session.db.wpp_message(record["id"]["id"]) == record
