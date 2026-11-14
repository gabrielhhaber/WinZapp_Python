"""Muted state is stored before account notification; history never replays."""
from types import SimpleNamespace as N

import pytest
from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import MuteAction

from test_group_receipts import session, GROUP, jid_n


@pytest.mark.parametrize("muted,end,expected,stored", [(True, 2000000000, 2000000000, 2000000000),
    (True, 0, -1, 2**31 - 1), (False, 2000000000, 0, 0)])
def test_mute_notice_follows_database_update(session, muted, end, expected, stored):
    import neonize.events as nev
    seen = []
    session.emit = lambda name, payload: seen.append((name, payload, next(c for c in session.db.chats() if c["jid"] == GROUP)))
    session.handlers[nev.ChatMuteEv](session.client, N(JID=jid_n(GROUP), FromFullSync=False,
        Action=MuteAction(muted=muted, muteEndTimestamp=end)))
    assert len(seen) == 1
    name, payload, chat = seen[0]
    assert name == "chat-settings-changed"
    assert payload == {"session": "account1", "chatId": GROUP, "setting": "mute", "value": expected}
    assert chat["muted"] == stored


def test_full_sync_updates_storage_without_announcing_historical_mute(session):
    import neonize.events as nev
    session.handlers[nev.ChatMuteEv](session.client, N(JID=jid_n(GROUP), FromFullSync=True,
        Action=MuteAction(muted=True, muteEndTimestamp=2000000000)))
    assert next(c for c in session.db.chats() if c["jid"] == GROUP)["muted"] == 2000000000
    assert session.emitted == []


def test_absent_mute_field_is_unknown_not_unmute(session):
    import neonize.events as nev
    session.handlers[nev.ChatMuteEv](session.client, N(JID=jid_n(GROUP), FromFullSync=False, Action=MuteAction()))
    assert session.emitted == []
