"""App state events update the bridge before notifying the client."""
from types import SimpleNamespace as N

from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import StarAction

from test_group_receipts import session, GROUP, jid_n
import msgmap
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message


def star(s, value=True, full=False, chat=GROUP, action=None):
    import neonize.events as nev
    s.handlers[nev.MessageStarEv](s.client, N(ChatJID=jid_n(chat), MessageID="M",
        Action=action if action is not None else StarAction(starred=value), FromFullSync=full))


def test_star_and_unstar_are_announced_after_the_stored_state_changes(session):
    session.db.message(GROUP, msgmap.build(chat=GROUP, from_me=False, id="M",
        sender="901@lid", ts=1, message=Message(conversation="keep")))
    seen = []
    session.emit = lambda name, data: seen.append((name, data, session.db.wpp_message("M")["star"]))
    star(session)
    star(session, False)
    assert seen == [("message-star-changed", {"session": "account1", "chatId": GROUP,
        "messageId": "M", "starred": value}, value) for value in (True, False)]


def test_full_sync_updates_the_store_without_announcing_old_changes(session):
    session.db.message(GROUP, {"id": {"id": "M"}, "t": 1, "body": "keep"})
    star(session, full=True)
    assert session.db.wpp_message("M")["star"] is True and session.emitted == []


def test_missing_star_field_is_unknown_not_an_unstar(session):
    star(session, action=StarAction())
    assert session.emitted == []


def test_unloaded_message_is_still_announced_with_its_chat_identity(session):
    star(session, chat="901@lid")
    assert session.emitted[-1][1]["chatId"] == "901@lid"
    assert session.db.wpp_message("M") is None


def test_delete_is_announced_after_the_bridge_row_is_removed(session):
    import neonize.events as nev
    session.db.message(GROUP, {"id": {"id": "M"}, "t": 1, "body": "keep"})
    seen = []
    session.emit = lambda name, payload: seen.append((name, payload, session.db.wpp_message("M")))
    session.handlers[nev.MessageDeleteForMeEv](session.client,
        N(ChatJID=jid_n(GROUP), MessageID="M", FromFullSync=False))
    assert seen == [("message-deleted-for-me", {"session": "account1", "chatId": GROUP,
                                              "messageId": "M"}, None)]


def test_full_sync_delete_does_not_remove_history_or_emit_a_notice(session):
    import neonize.events as nev
    session.db.message(GROUP, {"id": {"id": "M"}, "t": 1, "body": "keep"})
    session.handlers[nev.MessageDeleteForMeEv](session.client,
        N(ChatJID=jid_n(GROUP), MessageID="M", FromFullSync=True))
    assert session.db.wpp_message("M") is not None and session.emitted == []
