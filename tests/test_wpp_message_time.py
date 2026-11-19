"""A message get-messages returns without a time must not become "now".

Measured live (2026-11-19): a reply quoting a group message made WhatsApp Web
build a stand-in of the quoted message (`fromQuotedMsg`) with no `t`,
`timestamp` or `ack`, but with `clientReceivedTsMillis`. On a restart the
sync stamped it with the current time, and the message jumped below the
replies that quote it.
"""

import time

from core.websocket_client import WebSocketClient
from core.wpp_message_time import (
    GUESSED_TIMESTAMP,
    carry_over_known_timestamps,
    wpp_message_seconds,
)

GROUP = "120363000000000001@g.us"
AUTHOR = "11111111111111@lid"
RECEIVED_MS = 1795095053648


class _Normalizer:
    _normalize_wpp_message = WebSocketClient._normalize_wpp_message
    _clean_jid = WebSocketClient._clean_jid


def _stand_in(**extra):
    """The serialized quote stand-in, with the fields measured live."""
    payload = {
        "id": f"false_{GROUP}_3EB0STANDIN000000000001_{AUTHOR}",
        "type": "chat", "body": "quoted text",
        "from": GROUP, "to": "5511999999999@c.us", "author": AUTHOR,
        "fromMe": False, "isGroupMsg": True, "chatId": GROUP,
        "clientReceivedTsMillis": RECEIVED_MS,
        "serverStoreTimeMicros": 1795095053587100,
    }
    payload.update(extra)
    return payload


def test_whatsapp_time_wins_over_the_receive_time():
    assert wpp_message_seconds({"t": 1795095053, "clientReceivedTsMillis": 1}) == 1795095053
    assert wpp_message_seconds({"timestamp": 1795095053000}) == 1795095053


def test_a_stand_in_without_t_takes_its_receive_time():
    assert wpp_message_seconds(_stand_in()) == RECEIVED_MS // 1000


def test_no_time_at_all_is_unknown_not_now():
    assert wpp_message_seconds({"t": None, "timestamp": "x"}) is None
    assert wpp_message_seconds({"t": True}) is None
    assert wpp_message_seconds(None) is None


def test_normalizer_dates_the_stand_in_by_its_receive_time():
    normalized = _Normalizer()._normalize_wpp_message(_stand_in())
    assert normalized["messageTimestamp"] == RECEIVED_MS // 1000
    assert GUESSED_TIMESTAMP not in normalized


def test_normalizer_flags_a_guessed_time():
    payload = _stand_in()
    del payload["clientReceivedTsMillis"]
    before = int(time.time())
    normalized = _Normalizer()._normalize_wpp_message(payload)
    assert normalized["messageTimestamp"] >= before
    assert normalized[GUESSED_TIMESTAMP] is True


def test_normalizer_keeps_a_real_time_unflagged():
    normalized = _Normalizer()._normalize_wpp_message(_stand_in(t=1795095053))
    assert normalized["messageTimestamp"] == 1795095053
    assert GUESSED_TIMESTAMP not in normalized


def _record(message_id, ts, **extra):
    return {"key": {"id": message_id}, "messageTimestamp": ts, **extra}


def test_a_guess_takes_the_stored_time():
    fetched = [_record("A", 1795098191, **{GUESSED_TIMESTAMP: True}), _record("B", 1795096249)]
    stored = [_record("A", 1795095053), _record("B", 1)]
    assert carry_over_known_timestamps(fetched, stored) == 1
    assert fetched[0] == _record("A", 1795095053)
    # A real time from the server is never replaced.
    assert fetched[1]["messageTimestamp"] == 1795096249


def test_a_stored_guess_is_not_a_real_time():
    fetched = [_record("A", 1795099000, **{GUESSED_TIMESTAMP: True})]
    stored = [_record("A", 1795098191, **{GUESSED_TIMESTAMP: True})]
    assert carry_over_known_timestamps(fetched, stored) == 0
    assert fetched[0]["messageTimestamp"] == 1795099000
    assert fetched[0][GUESSED_TIMESTAMP] is True


def test_a_guess_with_no_stored_copy_stays_a_guess():
    fetched = [_record("A", 1795099000, **{GUESSED_TIMESTAMP: True})]
    assert carry_over_known_timestamps(fetched, [_record("B", 1)]) == 0
    assert carry_over_known_timestamps(fetched, None) == 0
    assert fetched[0][GUESSED_TIMESTAMP] is True
