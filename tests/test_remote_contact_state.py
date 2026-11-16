from types import SimpleNamespace as N

import pytest
from core import remote_contact_state as state


def window():
    writes, refresh = [], []
    mw = N(contacts={"1@s.whatsapp.net": {"remoteJid":"1@s.whatsapp.net","name":"Old book name","pushName":"Profile","syncedToPhone":True}},
        _lid_to_phone={"901@lid":"1@s.whatsapp.net"}, _phone_to_lid={"1@s.whatsapp.net":"901@lid"},
        db=N(upsert_contacts_batch=lambda rows:writes.append(rows)),
        _refresh_views_after_contact_change=lambda *args:refresh.append(args), _is_bad_contact_name=lambda name:name.isdigit())
    return mw,writes,refresh


def test_save_rename_remove_keep_profile_and_aliases_without_deleting_contact_history():
    mw,writes,refresh=window()
    state.apply_contact_state(mw,"901@lid","address-book","New book name",True,True)
    for jid in ("1@s.whatsapp.net","901@lid"):
        assert mw.contacts[jid]["name"]=="New book name" and mw.contacts[jid]["syncedToPhone"] is True
    state.apply_contact_state(mw,"901@lid","address-book","",False,False)
    assert mw.contacts["1@s.whatsapp.net"]["name"]==""
    assert mw.contacts["1@s.whatsapp.net"]["pushName"]=="Profile"
    assert mw.contacts["1@s.whatsapp.net"]["syncedToPhone"] is False
    assert len(writes)==2 and refresh[-1]==("1@s.whatsapp.net","901@lid")


def test_local_user_names_win_across_aliases_but_profile_and_sync_state_are_updated():
    mw,_,_=window()
    mw.contacts["1@s.whatsapp.net"].update(isSaved=True,name="Local choice")
    state.apply_contact_state(mw,"901@lid","address-book","Remote choice",True,True)
    assert all(row["name"]=="Local choice" for row in mw.contacts.values())
    state.apply_contact_state(mw,"901@lid","profile","New profile",None,None)
    assert mw.contacts["1@s.whatsapp.net"]["name"]=="Local choice"
    assert mw.contacts["1@s.whatsapp.net"]["pushName"]=="New profile"


def test_brazilian_variant_uses_existing_record_and_unknown_removal_creates_nothing():
    mw,writes,_=window()
    mw.contacts={"5511912345678@s.whatsapp.net":{"name":"Old"}}
    state.apply_contact_state(mw,"551112345678@s.whatsapp.net","address-book","New",True,None)
    assert len(mw.contacts)==1 and mw.contacts["5511912345678@s.whatsapp.net"]["name"]=="New"
    state.apply_contact_state(mw,"8@s.whatsapp.net","address-book","",False,False)
    assert "8@s.whatsapp.net" not in mw.contacts and len(writes)==1


@pytest.mark.parametrize("data",[None,{}, {"contactId":"7@g.us","kind":"profile","name":"Name"},
 {"contactId":"901@lid","kind":"address-book","name":"Name","saved":"true"},
 {"contactId":"901@lid","kind":"address-book","name":"","saved":True}])
def test_invalid_or_unknown_states_are_not_deletions(data):
    assert state.contact_state_target(data) is None


def test_account_and_shutdown_guards(monkeypatch):
    calls=[]
    monkeypatch.setattr(state.wx,"CallAfter",lambda fn,*args:calls.append(args))
    client=N(main_window="mw",_belongs_to_this_session=lambda d:d.get("session")=="ours")
    for session in ("other","ours"):
        state.on_contact_state(client,{"session":session,"contactId":"1@c.us","kind":"profile","name":"Name"})
    assert calls==[("mw","1@s.whatsapp.net","profile","Name",None,None)]
    mw,writes,_=window();mw._shutting_down=True
    state.apply_contact_state(mw,"1@s.whatsapp.net","profile","Name",None,None)
    assert writes==[]
