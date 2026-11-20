"""Encrypted reactions and comments through neonize's own calls (enc_secrets.py),
against fake clients: whatsmeow's cipher is not run here."""
from types import SimpleNamespace as N

from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, ReactionMessage

import enc_secrets
import msgmap
from test_group_receipts import session, GROUP, MY_LID, jid_n          # noqa: F401
from test_reaction_events import original
from test_socket_events import event, receive


def enc_reaction_event(own=False):
    msg = Message()
    msg.encReactionMessage.targetMessageKey.ID = "TARGET"
    msg.encReactionMessage.targetMessageKey.fromMe = own
    msg.encReactionMessage.targetMessageKey.participant = MY_LID
    e = event(msg, "ENCREACTION")
    e.Info.MessageSource.IsFromMe = own
    if own:
        e.Info.MessageSource.Sender = jid_n(MY_LID)
    return e


class Client:
    def __init__(self, **calls):
        self.__dict__.update(calls)


def test_an_encrypted_reaction_opens_with_the_target_of_the_envelope():
    client = Client(decrypt_reaction=lambda e: ReactionMessage(text="👍", senderTimestampMS=5))
    assert enc_secrets.open_reaction(client, enc_reaction_event()) == (
        "TARGET", "👍", False, MY_LID, 5)


def test_what_is_not_an_encrypted_reaction_or_cannot_be_opened_is_none():
    def refuse(e):
        raise RuntimeError("no secret")
    plain = event(Message(conversation="hi"), "M1")
    assert enc_secrets.open_reaction(Client(decrypt_reaction=lambda e: 1 / 0), plain) is None
    assert enc_secrets.open_reaction(Client(decrypt_reaction=refuse), enc_reaction_event()) is None
    assert enc_secrets.open_reaction(None, enc_reaction_event()) is None


def test_the_session_stores_and_announces_an_encrypted_reaction(session):
    record = original(session)
    session.client = Client(decrypt_reaction=lambda e: ReactionMessage(
        text="x", senderTimestampMS=1_700_000_004_987))
    receive(session, enc_reaction_event())
    name, payload = session.emitted[-1]
    assert name == "onreactionmessage" and payload["msgId"] == record["id"]
    assert payload["reactionText"] == "x" and payload["timestamp"] == 1_700_000_004
    assert session.db.reactions_of("TARGET") == [("901@lid", "x")]


def test_seal_reaction_wraps_what_neonize_returns_and_gives_up_on_error():
    info = enc_secrets.root_info(GROUP, MY_LID, True, "TARGET")
    sealed = enc_secrets.seal_reaction(
        Client(encrypt_reaction=lambda i, r: Message().encReactionMessage.__class__(
            encPayload=b"c", encIV=b"i")), info, ReactionMessage(text="x"))
    assert sealed.encReactionMessage.encPayload == b"c"

    def refuse(i, r):
        raise RuntimeError("no secret")
    assert enc_secrets.seal_reaction(Client(encrypt_reaction=refuse), info, ReactionMessage()) is None


def test_comments_through_neonize_or_none_for_an_older_build_and_for_errors():
    info = enc_secrets.root_info(GROUP, MY_LID, False, "P1")
    comment = Message(conversation="hi")
    assert enc_secrets.open_comment(Client(), object()) is None           # a build before 0.12
    assert enc_secrets.seal_comment(Client(), info, comment) is None
    assert enc_secrets.open_comment(Client(decrypt_comment=lambda e: comment), object()) is comment
    assert enc_secrets.seal_comment(Client(encrypt_comment=lambda i, c: c), info, comment) is comment

    def refuse(*a):
        raise RuntimeError("no secret")
    assert enc_secrets.open_comment(Client(decrypt_comment=refuse), object()) is None
    assert enc_secrets.seal_comment(Client(encrypt_comment=refuse), info, comment) is None


def test_root_info_carries_what_whatsmeow_reads_of_the_message():
    info = enc_secrets.root_info(GROUP, "5511999990000:3@s.whatsapp.net", False, "P1")
    assert info.ID == "P1" and info.MessageSource.IsGroup and not info.MessageSource.IsFromMe
    assert info.MessageSource.Chat.User == GROUP.split("@")[0]
    assert info.MessageSource.Sender.User == "5511999990000"       # without the device


def test_a_reaction_in_a_known_announcement_goes_encrypted_and_falls_back_plain(session):
    import time
    sent = []
    plain = Message(reactionMessage=ReactionMessage(text="x"))
    client = Client(
        build_reaction=lambda *a: plain, send_message=lambda to, m: sent.append(m),
        encrypt_reaction=lambda i, r: Message().encReactionMessage.__class__(encPayload=b"c"))
    session.client, session.status = client, "CONNECTED"
    session.__dict__["_community_announcements"] = {GROUP: (True, time.monotonic() + 60)}
    serial = f"false_{GROUP}_TARGET_{MY_LID}"
    assert session.react(serial, "x") and sent[-1].HasField("encReactionMessage")

    def refuse(i, r):
        raise RuntimeError("no secret")
    client.encrypt_reaction = refuse
    assert session.react(serial, "x") and sent[-1] is plain
    session.__dict__["_community_announcements"] = {}
    client.encrypt_reaction = lambda i, r: 1 / 0
    assert session.react(serial, "x") and sent[-1] is plain


def test_a_received_comment_is_opened_by_neonize_before_the_local_cipher():
    import comments
    s = N(client=Client(decrypt_comment=lambda e: Message(conversation="hello")),
          folder="/nowhere", lid=MY_LID, me="")
    enc = Message().encCommentMessage.__class__()
    assert comments._open(s, GROUP, enc, "5511@s.whatsapp.net", False, object()) == (
        comments.KIND_COMMENT, "hello")
    # neonize cannot open it and nothing is stored for the local cipher: unreadable
    s.client = Client(decrypt_comment=lambda e: 1 / 0)
    assert comments._open(s, GROUP, enc, "5511@s.whatsapp.net", False, object()) == (
        comments.KIND_UNREADABLE, "")


def test_an_own_message_is_tried_under_the_lid_then_the_number(session):
    import time
    seen = []

    def encrypt(info, reaction):
        seen.append(info.MessageSource.Sender.User)
        if len(seen) == 1:
            raise RuntimeError("no secret under this sender")
        return Message().encReactionMessage.__class__(encPayload=b"c")
    plain = Message(reactionMessage=ReactionMessage(text="x"))
    session.client = Client(build_reaction=lambda *a: plain, send_message=lambda to, m: None,
                            encrypt_reaction=encrypt)
    session.__dict__["_community_announcements"] = {GROUP: (True, time.monotonic() + 60)}
    sealed = session.encrypted_reaction(plain, GROUP, session.me, True, "TARGET")
    assert sealed.HasField("encReactionMessage") and len(seen) == 2
    assert seen[0] == MY_LID.partition("@")[0] and seen[0] != seen[1]
