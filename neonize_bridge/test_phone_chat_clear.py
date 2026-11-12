"""A chat cleared on the phone reaches the bridge's store and the client
(phone_chat_clear.py). Users reported that clearing on the phone left the
chat full in WinZapp."""
from types import SimpleNamespace as N

from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import (
    ClearChatAction, SyncActionMessageRange, SyncActionValue)

import phone_chat_clear as pcc
from store import Store


def action(last=1790000100, ts_ms=1790000200000):
    return SyncActionValue(timestamp=ts_ms, clearChatAction=ClearChatAction(
        messageRange=SyncActionMessageRange(lastMessageTimestamp=last)))


def test_a_clear_says_its_chat_range_and_starred_choice():
    assert pcc.cleared(["clearChat", "1@lid", "0", "0"], False, False, action()) == (
        "1@lid", 1790000100, True)
    assert pcc.cleared(["clearChat", "1@lid", "1", "0"], False, False, action())[2] is False


def test_without_a_range_the_mutation_time_is_the_limit():
    bare = SyncActionValue(timestamp=1790000200000, clearChatAction=ClearChatAction())
    assert pcc.cleared(["clearChat", "1@g.us", "0", "0"], False, False, bare)[1] == 1790000200


def test_a_replay_a_removal_or_another_mutation_is_not_news():
    assert pcc.cleared(["clearChat", "1@lid", "0", "0"], False, True, action()) is None
    assert pcc.cleared(["clearChat", "1@lid", "0", "0"], True, False, action()) is None
    assert pcc.cleared(["deleteChat", "1@lid", "0"], False, False, action()) is None
    assert pcc.cleared(["clearChat"], False, False, action()) is None
    assert pcc.cleared(["clearChat", "1@lid"], False, False, SyncActionValue()) is None


def test_the_client_gets_the_number_when_the_store_has_it():
    assert pcc.shown_chat("1@lid", ["1@lid", "55119@s.whatsapp.net"]) == "55119@s.whatsapp.net"
    assert pcc.shown_chat("1@lid", []) == "1@lid"
    assert pcc.payload("acc", "55119@s.whatsapp.net", 5, False) == {
        "session": "acc", "chatId": "55119@c.us", "until": 5, "keepStarred": False}


def _msg(mid, t, star=False):
    return {"id": {"id": mid}, "t": t, "star": star}


def test_the_store_loses_what_the_clear_covered(tmp_path):
    db = Store(str(tmp_path / "bridge.db"))
    chat = "55119@s.whatsapp.net"
    db.messages_in_batch([(chat, _msg("old", 100)), (chat, _msg("fav", 100, True)),
                          (chat, _msg("new", 300))])
    emitted = []
    s = N(db=db, name="acc", chat_keys=lambda jid: [chat],
          emit=lambda event, data: emitted.append((event, data)))
    value = action(last=200)
    assert pcc.handle(s, ["clearChat", "1@lid", "0", "0"], False, False, value)
    left = {m["id"]["id"] for m in db.messages([chat], 10)}
    assert left == {"fav", "new"}
    assert emitted == [("chat-cleared", {"session": "acc", "chatId": "55119@c.us",
                                         "until": 200, "keepStarred": True})]
    # the phone cleared the starred ones too
    assert pcc.handle(s, ["clearChat", "1@lid", "1", "0"], False, False, action(last=250))
    assert {m["id"]["id"] for m in db.messages([chat], 10)} == {"new"}


def test_a_replay_touches_nothing(tmp_path):
    db = Store(str(tmp_path / "bridge.db"))
    chat = "55119@s.whatsapp.net"
    db.messages_in_batch([(chat, _msg("old", 100))])
    s = N(db=db, name="acc", chat_keys=lambda jid: [chat],
          emit=lambda *a: (_ for _ in ()).throw(AssertionError("emitted")))
    assert not pcc.handle(s, ["clearChat", chat, "0", "0"], False, True, action(last=200))
    assert len(db.messages([chat], 10)) == 1
