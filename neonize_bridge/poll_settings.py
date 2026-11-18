"""Hide voter names and time limit of a new poll (Neonize engine).

WhatsApp Web, read over CDP on 2026-11-18 (WAWebPollsGeneratePollCreation
MessageProto, wa-js 4.8.0): a poll with an end time or with the voters' names
hidden goes out as ``pollCreationMessageV6`` whose ``endTime`` is an absolute
time in milliseconds and ``hideParticipantName`` a flag; any other poll keeps
its usual wrapper. neonize builds the poll through whatsmeow in the old
wrapper, so this moves it into V6 and sets the two fields. The message secret
that encrypts the votes lives outside the poll (``messageContextInfo``) and is
left where it is.

A channel's poll is built by channel_media.py and never goes through here.
Tested in test_poll_settings.py.
"""

#: every wrapper a poll creation can arrive in, from neonize's build
POLL_FIELDS = ("pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3",
               "pollCreationMessageV4", "pollCreationMessageV5")


def wanted(options):
    """``(hide_voter_names, duration_seconds)`` asked in the ``options`` object
    of /send-poll-message; ``(False, 0)`` when it asked for neither."""
    options = options if isinstance(options, dict) else {}
    try:
        seconds = int(options.get("durationSeconds") or 0)
    except (TypeError, ValueError):
        seconds = 0
    return options.get("hideVoterNames") is True, max(0, seconds)


def apply(message, hide_voter_names, duration_seconds, now_ms):
    """*message* (a neonize ``Message``) with the poll in V6 carrying what was
    asked; returned unchanged when neither was."""
    if not hide_voter_names and duration_seconds <= 0:
        return message
    for field in POLL_FIELDS:
        if message.HasField(field):
            break
    else:
        return message
    poll = type(getattr(message, field))()
    poll.CopyFrom(getattr(message, field))
    message.ClearField(field)
    if hide_voter_names:
        poll.hideParticipantName = True
    if duration_seconds > 0:
        poll.endTime = int(now_ms) + int(duration_seconds) * 1000
    message.pollCreationMessageV6.CopyFrom(poll)
    return message
