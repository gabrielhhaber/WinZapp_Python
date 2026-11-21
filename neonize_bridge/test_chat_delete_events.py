from types import SimpleNamespace as N

import pytest
from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import DeleteChatAction
from test_group_receipts import session, GROUP, jid_n


@pytest.mark.parametrize("full,last,expected", [(False, 90, True), (True, 90, False), (False, 101, False)])
def test_chat_deletion_is_emitted_only_after_eligible_storage_removal(session, full, last, expected, monkeypatch):
    import neonize.events as nev
    import server, sys
    def fail_on_exception(*args, **kwargs):
        raise sys.exception()
    monkeypatch.setattr(server.log, "exception", fail_on_exception)
    session.db.delete_chat(GROUP)
    session.db.chat(GROUP)
    session.db.message(GROUP, {"id": {"id": "M"}, "t": last})
    assert session.db.raw_last(GROUP)["t"] == last
    action = DeleteChatAction()
    action.messageRange.lastMessageTimestamp = 100
    seen = []
    session.emit = lambda name, payload: seen.append((name, payload, session.db.has_chat(GROUP)))
    session.handlers[nev.ChatDeleteEv](session.client, N(JID=jid_n(GROUP), Timestamp=200000,
        Action=action, FromFullSync=full))
    assert seen == ([("chat-deleted", {"session": "account1", "chatId": GROUP, "until": 100}, False)] if expected else [])


def test_deletion_without_a_message_range_is_applied_but_not_announced(session, monkeypatch):
    """Like the WPPConnect observer, only a deletion stating its range is announced."""
    import neonize.events as nev
    import server, sys
    def fail_on_exception(*args, **kwargs):
        raise sys.exception()
    monkeypatch.setattr(server.log, "exception", fail_on_exception)
    session.db.delete_chat(GROUP)
    session.db.chat(GROUP)
    session.db.message(GROUP, {"id": {"id": "M"}, "t": 90})
    seen = []
    session.emit = lambda name, payload: seen.append(name)
    session.handlers[nev.ChatDeleteEv](session.client, N(JID=jid_n(GROUP), Timestamp=200000,
        Action=DeleteChatAction(), FromFullSync=False))
    assert not session.db.has_chat(GROUP) and seen == []
