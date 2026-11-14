from types import SimpleNamespace as N
from neonize.proto.Neonize_pb2 import PrivacySettings
import privacy_events
from test_group_receipts import session


def test_only_explicitly_changed_known_fields_are_emitted_and_receipts_follow():
    seen=[]
    s=N(name="s",_read_receipts="all",emit=lambda *args:seen.append(args))
    settings=PrivacySettings(ReadReceipts=7,Online=2,Profile=1)
    privacy_events.changed(s,N(NewSettings=settings,ReadReceiptsChanged=True,ProfileChanged=True))
    assert s._read_receipts=="none"
    assert seen==[("privacy-settings-changed",{"session":"s","settings":{"readReceipts":"none"}})]
    privacy_events.changed(s,N(NewSettings=settings))
    assert len(seen)==1


def test_registered_handler_receives_without_starting_neonize(session):
    import neonize.events as nev
    session.handlers[nev.PrivacySettingsEv](session.client,N(NewSettings=PrivacySettings(LastSeen=3),LastSeenChanged=True))
    assert session.emitted[-1]==("privacy-settings-changed",{"session":"account1","settings":{"lastSeen":"contacts"}})
