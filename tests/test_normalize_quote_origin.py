"""Keep status origins through normalization without guessing from quoted text."""

import pytest

from core.websocket_client import WebSocketClient


class Normalizer:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


@pytest.mark.parametrize("quote_fields, expected_origin", [
    ({"quotedRemoteJid": "status@broadcast"}, "status@broadcast"),
    ({"contextInfo": {"remoteJid": "status@broadcast"}}, "status@broadcast"),
    ({"quotedStanzaID": "false_status@broadcast_ORIGINAL"}, "status@broadcast"),
    ({"quotedMsgId": "false_status@broadcast_ORIGINAL"}, "status@broadcast"),
    ({"quotedRemoteJid": "123@g.us"}, "123@g.us"),
    ({}, None),
])
def test_quote_origin_survives_normalization(quote_fields, expected_origin):
    raw = {
        "id": "false_123@g.us_REPLY",
        "from": "123@g.us",
        "to": "456@c.us",
        "fromMe": False,
        "timestamp": 1700000000,
        "type": "chat",
        "body": "reply",
        "quotedStanzaID": "ORIGINAL",
        "quotedParticipant": "789@c.us",
        "quotedMsg": {"body": "old message", "type": "chat"},
        **quote_fields,
    }

    normalized = Normalizer()._normalize_wpp_message(raw)
    ctx = normalized["message"]["extendedTextMessage"]["contextInfo"]

    assert ctx["stanzaId"] == "ORIGINAL"
    assert ctx.get("remoteJid") == expected_origin
    assert ctx["quotedMessage"]["conversation"] == "old message"
