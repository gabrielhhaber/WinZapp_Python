from types import SimpleNamespace as N

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, PinInChatMessage
from test_group_receipts import session, GROUP
import msgmap


def test_pin_and_unpin_notice_target_identity_and_duration(session):
    for state in (PinInChatMessage.PIN_FOR_ALL, PinInChatMessage.UNPIN_FOR_ALL):
        message = Message()
        message.pinInChatMessage.key.ID = "TARGET"
        message.pinInChatMessage.type = state
        message.pinInChatMessage.senderTimestampMS = 2000000000000
        if state == PinInChatMessage.PIN_FOR_ALL:
            message.messageContextInfo.messageAddOnDurationInSecs = 86400
        assert session._pin_received(GROUP, N(Message=message, Info=N(Timestamp=2000000000)))
        assert session.emitted[-1] == ("message-pin-changed", {"session": "account1", "chatId": GROUP,
            "messageId": "TARGET", "pinned": state == PinInChatMessage.PIN_FOR_ALL,
            "timestamp": 2000000000000, "duration": 86400 if state == PinInChatMessage.PIN_FOR_ALL else 0})


def test_unknown_pin_type_is_not_an_unpin():
    message = Message()
    message.pinInChatMessage.key.ID = "TARGET"
    assert msgmap.pin(message) is None


def test_unknown_pin_type_is_consumed_without_storing_or_emitting(session):
    message = Message()
    message.pinInChatMessage.key.ID = "TARGET"
    calls = []
    session.db.pin_message = lambda *args: calls.append(args)
    before = list(session.emitted)
    assert session._pin_received(GROUP, N(Message=message, Info=N(Timestamp=2000000000))) is True
    assert calls == [] and session.emitted == before
    # Any other message is left to the regular mapping.
    plain = Message(conversation="hi")
    assert session._pin_received(GROUP, N(Message=plain, Info=N(Timestamp=2000000000))) is False
