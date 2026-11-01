"""The bridge.log line of a media send."""
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
    AudioMessage, DocumentMessage, Message)

import send_log


def test_a_voice_note_line_has_what_differs_between_sends():
    m = Message(audioMessage=AudioMessage(fileLength=126874, mimetype="audio/ogg; codecs=opus",
                                          seconds=3, PTT=True, waveform=b"\x05" * 64))
    line = send_log.describe(m, "3EB0AA", "lid")
    assert line == ("media sent: id=3EB0AA to=lid type=audioMessage bytes=126874 "
                    "mime='audio/ogg; codecs=opus' ptt=True seconds=3 waveform=64")


def test_a_document_line_has_no_audio_fields():
    m = Message(documentMessage=DocumentMessage(fileLength=10, mimetype="application/pdf"))
    line = send_log.describe(m, "3EB0BB", "g.us")
    assert "type=documentMessage" in line and "ptt" not in line


def test_a_message_without_media_is_said_so():
    assert send_log.describe(Message(conversation="oi"), "X", "s.whatsapp.net") \
        == "message sent: id=X to=s.whatsapp.net (not media)"


def test_the_line_never_carries_a_jid_or_phone():
    m = Message(audioMessage=AudioMessage(fileLength=1, PTT=True))
    line = send_log.describe(m, "3EB0CC", "lid")
    assert "@" not in line
