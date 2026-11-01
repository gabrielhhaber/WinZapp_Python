"""One line in bridge.log for each media message sent.

bridge.log used to say nothing about a send: when a voice note stayed on one
grey tick (issue #664) there was no way to compare the messages that arrived
with the ones that did not. The line carries what differs between media sends
(kind, size, mimetype, duration, waveform) and the id the receipts log lists,
never a JID or a phone number.

Tested in test_send_log.py.
"""
MEDIA_FIELDS = ("audioMessage", "videoMessage", "imageMessage", "documentMessage",
                "stickerMessage")


def describe(message, message_id, server):
    """The log text for `message` (a waE2E Message) sent as `message_id` to a JID
    of `server` ("lid", "s.whatsapp.net", "g.us"...)."""
    for field in MEDIA_FIELDS:
        if message.HasField(field):
            media = getattr(message, field)
            break
    else:
        return f"message sent: id={message_id} to={server} (not media)"
    parts = [f"id={message_id}", f"to={server}", f"type={field}",
             f"bytes={getattr(media, 'fileLength', 0)}",
             f"mime={getattr(media, 'mimetype', '')!r}"]
    if field == "audioMessage":
        parts += [f"ptt={media.PTT}", f"seconds={media.seconds}",
                  f"waveform={len(media.waveform)}"]
    elif field == "videoMessage":
        parts.append(f"seconds={media.seconds}")
    return "media sent: " + " ".join(parts)
