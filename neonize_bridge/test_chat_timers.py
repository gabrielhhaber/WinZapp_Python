"""Temporary messages on what the bridge sends (chat_timers.py).

Reported: a message sent from WinZapp into a chat with temporary messages on
showed on the phone as permanent, "the sender may be using an incompatible
device". whatsmeow never adds the chat's timer, so the bridge does."""
from types import SimpleNamespace

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import (
    ContextInfo, ExtendedTextMessage, FutureProofMessage, ImageMessage, Message,
    ProtocolMessage, ReactionMessage)

import chat_timers as ct
from store import Store

DAY, WEEK = 24 * 3600, 7 * 24 * 3600


# ── what a received message says ─────────────────────────────────────────────

def test_the_timer_notice_sets_and_clears_the_timer():
    on = Message(protocolMessage=ProtocolMessage(
        type=ct.EPHEMERAL_SETTING, ephemeralExpiration=WEEK))
    off = Message(protocolMessage=ProtocolMessage(
        type=ct.EPHEMERAL_SETTING, ephemeralExpiration=0))
    assert ct.timer_in(on)[0] == WEEK
    assert ct.timer_in(off)[0] == 0


def test_another_protocol_message_says_nothing():
    revoke = Message(protocolMessage=ProtocolMessage(type=0))
    assert ct.timer_in(revoke) is None


def test_a_message_carrying_its_expiration_states_the_timer():
    message = Message(extendedTextMessage=ExtendedTextMessage(
        text="hi", contextInfo=ContextInfo(expiration=DAY, ephemeralSettingTimestamp=5)))
    assert ct.timer_in(message) == (DAY, 5)


def test_the_ephemeral_wrapper_is_looked_through():
    inner = Message(imageMessage=ImageMessage(contextInfo=ContextInfo(expiration=WEEK)))
    wrapped = Message(ephemeralMessage=FutureProofMessage(message=inner))
    assert ct.timer_in(wrapped)[0] == WEEK


def test_no_expiration_is_not_off():
    """An old device sends none: only the notice turns the timer off."""
    assert ct.timer_in(Message(conversation="hi")) is None
    assert ct.timer_in(Message(extendedTextMessage=ExtendedTextMessage(
        text="hi", contextInfo=ContextInfo()))) is None
    assert ct.timer_in(None) is None


# ── what the bridge sends ────────────────────────────────────────────────────

def test_plain_text_becomes_an_extended_text_with_the_timer():
    message = Message(conversation="hello")
    out = ct.stamped(message, WEEK, 9)
    assert not out.HasField("conversation")
    assert out.extendedTextMessage.text == "hello"
    assert out.extendedTextMessage.contextInfo.expiration == WEEK
    assert out.extendedTextMessage.contextInfo.ephemeralSettingTimestamp == 9
    assert message.conversation == "hello"     # the original is left alone


def test_media_and_replies_keep_their_context_and_gain_the_timer():
    message = Message(imageMessage=ImageMessage(
        caption="x", contextInfo=ContextInfo(stanzaID="Q1")))
    out = ct.stamped(message, DAY)
    assert out.imageMessage.contextInfo.expiration == DAY
    assert out.imageMessage.contextInfo.stanzaID == "Q1"


def test_reactions_and_chats_without_a_timer_go_as_they_were():
    reaction = Message(reactionMessage=ReactionMessage(text="👍"))
    assert ct.stamped(reaction, WEEK) is reaction
    text = Message(conversation="hi")
    assert ct.stamped(text, 0) is text


class _Client:
    def __init__(self):
        self.sent = []
        self.send_message = lambda to, message, *a, **kw: self.sent.append((to, message)) or "ok"

    def _parse_group_mention(self, text):
        return []

    def _parse_mention(self, text, are_lids):
        return []


def _jid(text):
    user, _, server = text.partition("@")
    return SimpleNamespace(User=user, Server=server)


def test_install_stamps_every_message_to_a_chat_with_a_timer():
    client = _Client()
    asked = []
    ct.install(client, lambda jid: asked.append(jid) or (WEEK, 3))
    assert client.send_message(_jid("123@lid"), "oi") == "ok"
    client.send_message(_jid("123@lid"), Message(conversation="tudo bem"))
    assert asked == ["123@lid", "123@lid"]
    for _to, message in client.sent:
        assert message.extendedTextMessage.contextInfo.expiration == WEEK


def test_install_leaves_chats_without_a_timer_untouched():
    client = _Client()
    ct.install(client, lambda jid: (0, 0) if jid.startswith("1") else None)
    client.send_message(_jid("1@lid"), "a")
    client.send_message(_jid("2@g.us"), "b")
    assert [m for _t, m in client.sent] == ["a", "b"]


def test_a_failing_lookup_still_sends():
    client = _Client()

    def broken(jid):
        raise RuntimeError("store closed")

    ct.install(client, broken)
    client.send_message(_jid("1@lid"), "a")
    assert client.sent[0][1] == "a"


# ── the session keeps it under both addresses ────────────────────────────────

class _Session(ct.ChatTimers):
    def __init__(self, db, keys):
        self.db = db
        self._keys = keys

    def chat_keys(self, jid):
        return self._keys


def test_a_timer_is_kept_under_the_number_and_the_lid(tmp_path):
    db = Store(str(tmp_path / "bridge.db"))
    try:
        db.chat("55@s.whatsapp.net")
        db.chat("77@lid")
        session = _Session(db, ["77@lid", "55@s.whatsapp.net"])
        session.record_timer("77@lid", WEEK, 12)
        assert db.chat_timer("55@s.whatsapp.net") == (WEEK, 12)
        assert session.chat_timer("55@s.whatsapp.net") == (WEEK, 12)
        session.observe_timer("77@lid", Message(protocolMessage=ProtocolMessage(
            type=ct.EPHEMERAL_SETTING, ephemeralExpiration=0)))
        assert db.chat_timer("77@lid")[0] == 0
    finally:
        db.close()


def test_an_unknown_chat_has_no_timer(tmp_path):
    db = Store(str(tmp_path / "bridge.db"))
    try:
        assert _Session(db, []).chat_timer("9@lid") is None
    finally:
        db.close()


def test_a_status_has_no_timer():
    assert not ct.has_timer("status@broadcast")
    assert not ct.has_timer("123@broadcast")
    assert ct.has_timer("123@lid") and ct.has_timer("1@g.us")


def test_a_status_that_states_a_timer_does_not_give_the_statuses_one(tmp_path):
    """REGRESSION: a received status with an expiration gave status@broadcast
    a timer; every video and voice status after it went out stamped, and
    WhatsApp refused them with 400."""
    db = Store(str(tmp_path / "bridge.db"))
    try:
        db.chat("status@broadcast")
        session = _Session(db, [])
        session.observe_timer("status@broadcast", Message(imageMessage=ImageMessage(
            contextInfo=ContextInfo(expiration=DAY))))
        assert db.chat_timer("status@broadcast") == (0, 0)
    finally:
        db.close()


def test_a_status_timer_already_stored_is_not_sent(tmp_path):
    """The stores that already hold one: the status goes out without it."""
    db = Store(str(tmp_path / "bridge.db"))
    try:
        db.chat("status@broadcast", ephemeral=DAY)
        session = _Session(db, [])
        assert session.chat_timer("status@broadcast") is None
        client = _Client()
        ct.install(client, session.chat_timer)
        video = Message(imageMessage=ImageMessage(caption="x"))
        client.send_message(_jid("status@broadcast"), video)
        assert not client.sent[0][1].imageMessage.HasField("contextInfo")
    finally:
        db.close()


def test_a_client_without_send_message_is_left_alone():
    """REGRESSION: the session's start installs the wrapper on any client;
    the stand-in clients of other tests have no send_message."""
    client = SimpleNamespace()
    assert ct.install(client, lambda jid: None) is None
    assert not hasattr(client, "send_message")
