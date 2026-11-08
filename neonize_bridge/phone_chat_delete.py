"""A chat deleted on the phone (neonize's ChatDeleteEv, app state).

The bridge forgets the chat, unless the mutation is a replay or a newer message
has arrived since: WhatsApp keeps deleting chats that people write in again.
"""


def applies(from_full_sync, until, last_t):
    """Whether the bridge should delete the chat. `until` is when the phone
    deleted it (seconds), `last_t` the time of the newest stored message (or
    None). A full sync replays every old mutation, and a message stored after
    the deletion (five seconds of slack) belongs to a chat that came back."""
    if from_full_sync:
        return False
    if until and last_t is not None and last_t > until + 5:
        return False
    return True
