from types import SimpleNamespace as N

from neonize.proto.waSyncAction.WAWebProtobufSyncAction_pb2 import ContactAction
from contact_state_events import address_book, profile


def session():
    stored, seen = {}, []
    s = N(name="s", db=N(set_contact_name=lambda jid,name:stored.update({jid:name}),
                         contact=lambda jid,**fields:stored.update({jid:fields})))
    s.emit = lambda event,payload:seen.append((event,payload,dict(stored)))
    return s,stored,seen


def test_address_book_events_follow_storage_and_preserve_unknown_phone_sync_flag():
    s,stored,seen=session()
    for name in ("First", "Renamed", ""):
        action=ContactAction(fullName=name)
        assert address_book(s,N(Action=action,FromFullSync=False),name,"1@s.whatsapp.net")
        assert seen[-1][2]["1@s.whatsapp.net"]==name
        assert seen[-1][1]["saved"]==bool(name)
    assert "syncedToPhone" not in seen[0][1]
    assert seen[-1][1]["syncedToPhone"] is False


def test_full_sync_is_silent_and_absent_names_are_not_removal():
    s,stored,seen=session()
    action=ContactAction(fullName="Name",saveOnPrimaryAddressbook=True)
    address_book(s,N(Action=action,FromFullSync=True),"Name","1@s.whatsapp.net")
    assert stored["1@s.whatsapp.net"]=="Name" and seen==[]
    assert not address_book(s,N(Action=ContactAction(),FromFullSync=False),"","1@s.whatsapp.net")
    assert stored["1@s.whatsapp.net"]=="Name"


def test_profile_event_does_not_create_a_saved_address_book_name():
    s,stored,seen=session()
    profile(s,"901@lid","Public profile")
    assert stored["901@lid"]=={"pushname":"Public profile"}
    assert seen[-1][1]=={"session":"s","contactId":"901@lid","kind":"profile","name":"Public profile"}
