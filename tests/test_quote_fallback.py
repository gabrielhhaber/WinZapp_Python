"""A reply carries a description of the message it quotes (core/quote_fallback.py).

Reported 2026-11-13: right after switching to Neonize, replying to a group
message refused with "the quoted message was not found". The message had
arrived while the account was on WPPConnect; the bridge only got it about 4
minutes later, from the queue the server kept for that device, and group
messages it cannot decrypt never reach its store. WinZapp has the message, so
the reply describes it and the bridge builds the quote from that when its own
store misses (neonize_bridge/quote_fallback.py).
"""

from core.quote_fallback import MAX_TEXT, quote_fallback
from tests.test_reply_with_mention_keeps_quote import (
    GROUP, MENTION, QUOTED_ID, _OK, _Response, _Stub, _answer)

IGOR = "153442135879887@lid"
TEXT = {"key": {"id": "3EB0AAA", "remoteJid": GROUP, "fromMe": False, "participant": IGOR},
        "message": {"conversation": "vamos testar a build"}}


def test_a_text_message_is_described_with_its_author():
    assert quote_fallback(TEXT) == {"type": "chat", "body": "vamos testar a build",
                                    "fromMe": False, "author": IGOR}
    extended = {"key": {"id": "X", "remoteJid": "5511@s.whatsapp.net", "fromMe": True},
                "message": {"extendedTextMessage": {"text": "a" * 5000}}}
    described = quote_fallback(extended)
    assert described["fromMe"] is True and len(described["body"]) == MAX_TEXT
    assert described["author"] == "5511@s.whatsapp.net"   # a private chat's own jid


def test_media_is_described_by_its_kind_caption_and_length():
    voice = {"key": {"id": "V", "remoteJid": GROUP, "participant": IGOR},
             "message": {"audioMessage": {"ptt": True, "seconds": 12, "mimetype": "audio/ogg"}}}
    assert quote_fallback(voice)["type"] == "ptt" and quote_fallback(voice)["duration"] == 12
    wrapped = {"key": {"id": "D", "remoteJid": GROUP, "participant": IGOR},
               "message": {"documentWithCaptionMessage": {"message": {"documentMessage": {
                   "fileName": "log.zip", "caption": "logs", "mimetype": "application/zip"}}}}}
    assert quote_fallback(wrapped) == {"type": "document", "caption": "logs",
                                       "mimetype": "application/zip", "filename": "log.zip",
                                       "fromMe": False, "author": IGOR}


def test_what_cannot_be_described_is_left_out():
    assert quote_fallback(None) is None
    assert quote_fallback({"key": {"id": "R"}, "message": {"reactionMessage": {}}}) is None
    assert quote_fallback({"key": {"id": "R"}}) is None


def test_the_reply_and_the_mention_routes_send_the_description(monkeypatch):
    calls = _answer(monkeypatch, _Response(201, _OK))
    _Stub().send_text_message(GROUP, "oi", quoted=TEXT)
    _Stub().send_text_message(GROUP, "oi @x", quoted=TEXT, mentioned_jids=[MENTION])
    for route, payload in calls:
        assert route in ("send-reply", "send-mentioned")
        assert payload["messageId"] == QUOTED_ID
        assert payload["quotedMessage"]["body"] == "vamos testar a build"
