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
