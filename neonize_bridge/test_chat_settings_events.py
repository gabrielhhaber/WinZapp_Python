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


@pytest.mark.parametrize("locked,full", [(True, False), (False, False), (True, True)])
def test_lock_notice_updates_storage_before_notifying_and_suppresses_history(session, locked, full):
    import neonize.events as nev
    from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import LockChatAction
    seen = []
    session.emit = lambda name, payload: seen.append((payload, next(c for c in session.db.chats() if c["jid"] == GROUP)["locked"]))
    session.handlers[nev.ChatLockEv](session.client, N(JID=jid_n(GROUP), FromFullSync=full,
        Action=LockChatAction(locked=locked)))
    assert seen == ([] if full else [({"session": "account1", "chatId": GROUP,
                                     "setting": "lock", "value": locked}, int(locked))])


def test_explicit_timer_notice_is_stored_before_notification(session):
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message, ProtocolMessage
    seen = []
    session.emit = lambda name, payload: seen.append((name, payload, session.db.chat_timer(GROUP)))
    for duration in (86400, 0):
        session.observe_timer(GROUP, Message(protocolMessage=ProtocolMessage(
            type=3, ephemeralExpiration=duration, ephemeralSettingTimestamp=100)))
    assert seen == [("chat-timer-changed", {"session": "account1", "chatId": GROUP,
        "duration": d, "settingTimestamp": 100}, (d, 100)) for d in (86400, 0)]


@pytest.mark.parametrize("is_ephemeral,expected", [(True, 86400), (False, 0)])
def test_group_settings_timer_is_stored_before_its_notice(session, monkeypatch, is_ephemeral, expected):
    """GroupInfoEv.Ephemeral: turning the timer off in the group settings is 0, not the old timer."""
    import neonize.events as nev
    import group_events
    monkeypatch.setattr(group_events, "group_changed", lambda c, s, e: None)
    seen = []
    session.emit = lambda name, payload: seen.append(
        (name, payload, next(c for c in session.db.chats() if c["jid"] == GROUP)))
    event = N(JID=jid_n(GROUP), Name=None, HasField=lambda field: field == "Ephemeral",
              Ephemeral=N(IsEphemeral=is_ephemeral, DisappearingTimer=86400))
    session.handlers[nev.GroupInfoEv](session.client, event)
    name, payload, chat = seen[-1]
    assert name == "chat-timer-changed"
    assert payload == {"session": "account1", "chatId": GROUP, "duration": expected, "settingTimestamp": 0}
    assert chat["ephemeral"] == expected
