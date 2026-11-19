"""Own reactions from linked devices must not be mistaken for WinZapp echoes.

A reaction sent from WinZapp leaves a marker; only the echo matching it is
dropped. The marker carries the chat: a message id is only unique within its
chat, so (id, emoji) alone swallowed a reaction made on the phone to another
chat's message with the same id and emoji (core/reaction_echo.py).
"""

import threading
import time
import types

import pytest

from core.reaction_echo import (
    ECHO_WINDOW_SECONDS, prune_expired, reaction_echo_keys, take_matching_send,
)
from core.websocket_client import WebSocketClient

ANA = "5511999990000@s.whatsapp.net"
ANA_SHORT = "551199990000@s.whatsapp.net"
ANA_LID = "123456789012345@lid"
BIA = "5511888880000@s.whatsapp.net"
LID_TO_PHONE = {ANA_LID: ANA}
PHONE_TO_LID = {ANA: ANA_LID}


class _MainWindow:
    def __init__(self, sends=()):
        self._pending_own_reactions = {}
        self._pending_own_reactions_lock = threading.Lock()
        self._lid_to_phone = dict(LID_TO_PHONE)
        self._phone_to_lid = dict(PHONE_TO_LID)
        for chat, target_id, emoji, age in sends:
            self._pending_own_reactions[object()] = (
                time.monotonic() - age,
                reaction_echo_keys((chat,), target_id, emoji,
                                   self._lid_to_phone, self._phone_to_lid))


def _client(*sends):
    client = WebSocketClient.__new__(WebSocketClient)
    client.main_window = _MainWindow(sends)
    return client


def _reaction(target_id="message-1", emoji="👍", chat=ANA, reaction_id="reaction-1"):
    return {
        "key": {"fromMe": True, "id": reaction_id, "remoteJid": chat},
        "messageType": "reactionMessage",
        "message": {
            "reactionMessage": {
                "key": {"id": target_id, "remoteJid": chat},
                "text": emoji,
            }
        },
    }


def test_reaction_from_phone_is_not_suppressed_without_a_local_send():
    client = _client()

    assert client._consume_own_reaction_echo(_reaction()) is False


@pytest.mark.parametrize("wrapped", [False, True])
def test_wpp_msgkeys_and_legacy_bridge_strings_deliver_the_same_group_reaction(monkeypatch, wrapped):
    from core import websocket_client as wc
    received = []
    monkeypatch.setattr(wc.wx, "CallAfter", lambda fn, *args: fn(*args))
    group = "120363000000000001@g.us"
    def key(own, mid, author):
        return {"fromMe": own, "remote": group, "id": mid, "participant": author,
                "_serialized": f"{'true' if own else 'false'}_{group}_{mid}_{author}"}
    reaction_key, target_key = key(False, "REACTION", ANA_LID), key(True, "TARGET", "901@lid")
    data = {"id": reaction_key, "msgId": target_key,
            "reactionText": "", "timestamp": 1_700_000_000}
    if wrapped:
        data = {"response": {**data, "id": reaction_key["_serialized"],
                             "msgId": target_key["_serialized"]}}
    client = types.SimpleNamespace(
        _belongs_to_this_session=lambda d: True,
        _consume_own_reaction_echo=lambda msg: False,
        main_window=types.SimpleNamespace(on_new_message=received.append))
    client._clean_jid = types.MethodType(WebSocketClient._clean_jid, client)
    WebSocketClient.on_wpp_reaction(client, data)
    assert len(received) == 1
    result = received[0]
    assert result["key"] == {"remoteJid": group, "fromMe": False,
                             "id": "REACTION", "participant": ANA_LID}
    assert result["message"]["reactionMessage"]["key"]["id"] == "TARGET"
    assert result["message"]["reactionMessage"]["text"] == ""
    assert result["messageTimestamp"] == 1_700_000_000


def test_matching_local_reaction_echo_is_suppressed_once():
    client = _client((ANA, "message-1", "👍", 0))

    assert client._consume_own_reaction_echo(_reaction()) is True
    # The same reaction may arrive through both received-message and the
    # dedicated onreactionmessage event; both copies must stay suppressed.
    assert client._consume_own_reaction_echo(_reaction()) is True


def test_different_phone_reaction_is_not_suppressed():
    client = _client((ANA, "message-1", "👍", 0))

    assert client._consume_own_reaction_echo(_reaction(emoji="😂")) is False


def test_expired_local_marker_does_not_hide_a_phone_reaction():
    client = _client((ANA, "message-1", "👍", ECHO_WINDOW_SECONDS + 1))

    assert client._consume_own_reaction_echo(_reaction()) is False
    assert client.main_window._pending_own_reactions == {}


def test_the_same_message_id_in_another_chat_is_a_phone_reaction():
    """The reported collision: WinZapp reacted 👍 in Ana's chat, and within
    the window the phone reacted 👍 to a message with the same id in Bia's."""
    client = _client((ANA, "message-1", "👍", 0))

    assert client._consume_own_reaction_echo(_reaction(chat=BIA, reaction_id="phone")) is False
    # WinZapp's own marker is still there for its real echo.
    assert client._consume_own_reaction_echo(_reaction(chat=ANA)) is True


@pytest.mark.parametrize("sent_to,echoed_as", [
    (ANA, ANA_LID),                        # send_reaction() swapped in the @lid
    (ANA_LID, ANA),
    (ANA, "5511999990000@c.us"),           # legacy form
    (ANA, "5511999990000:12@s.whatsapp.net"),  # device suffix
    (ANA, ANA_SHORT),                      # Brazilian optional ninth digit
    (ANA_SHORT, ANA),
    (ANA_SHORT, ANA_LID),                   # bridge stored under the longer phone
    (ANA_LID, ANA_SHORT),
])
def test_an_echo_under_another_spelling_of_the_chat_is_still_the_echo(sent_to, echoed_as):
    client = _client((sent_to, "message-1", "👍", 0))

    assert client._consume_own_reaction_echo(_reaction(chat=echoed_as)) is True


def test_a_consumed_send_leaves_no_spelling_behind():
    """Every alias goes with the send: one left behind would swallow a later
    phone reaction to the same message with the same emoji."""
    client = _client((ANA, "message-1", "👍", 0))

    assert client._consume_own_reaction_echo(_reaction(chat=ANA_LID)) is True
    assert client._consume_own_reaction_echo(_reaction(chat=ANA, reaction_id="phone")) is False


@pytest.mark.parametrize("sent_to,echoed_as", [(ANA, ANA_LID), (ANA_LID, ANA)])
def test_bridge_stored_under_the_short_phone_spelling(sent_to, echoed_as):
    window = _MainWindow()
    window._lid_to_phone = {ANA_LID: ANA_SHORT}
    window._phone_to_lid = {ANA_SHORT: ANA_LID}
    window._pending_own_reactions[object()] = (
        time.monotonic(), reaction_echo_keys(
            (sent_to,), "message-1", "👍", window._lid_to_phone, window._phone_to_lid))
    client = WebSocketClient.__new__(WebSocketClient)
    client.main_window = window

    assert client._consume_own_reaction_echo(_reaction(chat=echoed_as)) is True


@pytest.mark.parametrize("domain", ["lid", "g.us", "broadcast", "newsletter"])
def test_optional_digit_does_not_make_non_phone_chats_equivalent(domain):
    sent = reaction_echo_keys((f"5511999990000@{domain}",), "m", "👍")
    echo = reaction_echo_keys((f"551199990000@{domain}",), "m", "👍")

    assert not sent & echo


class TestKeys:
    def test_a_chat_is_known_by_its_lid_and_phone_forms(self):
        keys = reaction_echo_keys(("5511999990000@c.us",), "m", " 👍 ", LID_TO_PHONE, PHONE_TO_LID)
        assert keys == {(ANA, "m", "👍"), (ANA_SHORT, "m", "👍"), (ANA_LID, "m", "👍")}

    def test_a_group_is_only_itself(self):
        group = "120363000000000000@g.us"
        assert reaction_echo_keys((group, group), "m", "👍") == {(group, "m", "👍")}

    def test_no_message_id_matches_nothing(self):
        assert reaction_echo_keys((ANA,), "", "👍") == frozenset()

    def test_take_prunes_and_matches_whole_sends(self):
        now = 1000.0
        old, fresh = object(), object()
        pending = {
            old: (now - ECHO_WINDOW_SECONDS - 1, reaction_echo_keys((ANA,), "m", "👍")),
            fresh: (now, reaction_echo_keys((BIA,), "m", "👍")),
        }
        assert take_matching_send(pending, reaction_echo_keys((ANA,), "m", "👍"), now) is None
        assert take_matching_send(pending, reaction_echo_keys((BIA,), "m", "👍"), now) is fresh
        assert pending == {}

    def test_prune_keeps_what_is_still_in_the_window(self):
        token = object()
        pending = {token: (100.0, frozenset())}
        prune_expired(pending, 100.0 + ECHO_WINDOW_SECONDS)
        assert token in pending


class TestSendReaction:
    """MainWindow.send_reaction() registers the chat it was asked for and the
    one it sent to, and withdraws the marker when the send fails."""

    def _window(self, monkeypatch, status=200):
        import main_window.sending as module
        from main_window.sending import SendingMixin
        posted = []
        monkeypatch.setattr(module, "api_post", lambda url, **kw: posted.append(kw["json"])
                            or types.SimpleNamespace(status_code=status, text=""))
        window = _MainWindow()
        window.wpp_server, window.wpp_port, window.token = "http://127.0.0.1", 6300, "s:t"
        window._serialize_msg_id = lambda chat, key: f"false_{chat}_{key['id']}"
        window.send_reaction = types.MethodType(SendingMixin.send_reaction, window)
        return window, posted

    def test_the_marker_knows_the_chat_under_both_forms(self, monkeypatch):
        window, posted = self._window(monkeypatch)

        assert window.send_reaction(ANA, {"id": "message-1"}, "👍") is True

        assert posted[0]["msgId"] == f"false_{ANA_LID}_message-1"
        (created_at, keys), = window._pending_own_reactions.values()
        assert keys == {(ANA, "message-1", "👍"), (ANA_SHORT, "message-1", "👍"),
                        (ANA_LID, "message-1", "👍")}

    def test_a_failed_send_leaves_no_marker(self, monkeypatch):
        window, _ = self._window(monkeypatch, status=500)

        assert window.send_reaction(ANA, {"id": "message-1"}, "👍") is False

        assert window._pending_own_reactions == {}
