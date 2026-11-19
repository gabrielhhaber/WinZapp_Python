"""Hide voter names and time limit of a new poll: the V6 wrapper
(poll_settings.py)."""
import pytest

import poll_settings

proto = pytest.importorskip("neonize.proto.waE2E.WAWebProtobufsE2E_pb2")


def _message(field="pollCreationMessage"):
    poll = proto.PollCreationMessage(
        name="Q?", selectableOptionsCount=1,
        options=[proto.PollCreationMessage.Option(optionName="a"),
                 proto.PollCreationMessage.Option(optionName="b")])
    message = proto.Message(**{field: poll})
    message.messageContextInfo.messageSecret = b"s" * 32
    return message


def test_wanted_reads_both_settings_and_ignores_garbage():
    assert poll_settings.wanted({"hideVoterNames": True, "durationSeconds": 3600}) == (True, 3600)
    assert poll_settings.wanted({"hideVoterNames": "yes", "durationSeconds": "x"}) == (False, 0)
    assert poll_settings.wanted({"durationSeconds": -5}) == (False, 0)
    assert poll_settings.wanted(None) == (False, 0)


def test_a_plain_poll_is_left_alone():
    message = _message()
    assert poll_settings.apply(message, False, 0, 1_000) is message
    assert message.HasField("pollCreationMessage")
    assert not message.HasField("pollCreationMessageV6")


@pytest.mark.parametrize("field", ["pollCreationMessage", "pollCreationMessageV3"])
def test_settings_move_the_poll_into_v6(field):
    message = poll_settings.apply(_message(field), True, 3600, 1_000)
    assert not message.HasField(field)
    poll = message.pollCreationMessageV6
    assert poll.hideParticipantName is True
    assert poll.endTime == 1_000 + 3600 * 1000      # absolute, in milliseconds
    assert [o.optionName for o in poll.options] == ["a", "b"]
    assert poll.name == "Q?" and poll.selectableOptionsCount == 1
    assert message.messageContextInfo.messageSecret == b"s" * 32


def test_only_what_was_asked_is_set():
    only_hide = poll_settings.apply(_message(), True, 0, 5).pollCreationMessageV6
    assert only_hide.hideParticipantName is True and only_hide.endTime == 0
    only_time = poll_settings.apply(_message(), False, 60, 5).pollCreationMessageV6
    assert only_time.hideParticipantName is False and only_time.endTime == 5 + 60_000
