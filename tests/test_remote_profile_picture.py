from types import SimpleNamespace as N

from core import remote_profile_picture as pictures


def test_picture_change_clears_both_aliases_without_changing_names_or_history():
    writes, saves = [], []
    mw = N(contacts={"1@s.whatsapp.net": {"name":"Local name","profilePicUrl":"https://old/p"},
        "901@lid":{"pushName":"Public name","profilePicUrl":"https://old/p"}},
        chats={"7@g.us":{"profilePicThumbObj":{"eurl":"https://old/g"},"messages":{"kept":True}}},
        _lid_to_phone={"901@lid":"1@s.whatsapp.net"}, _phone_to_lid={},
        db=N(upsert_contacts_batch=lambda rows:writes.append(rows)), _schedule_save=lambda **kw:saves.append(kw))
    pictures.apply_picture_changed(mw,"901@lid")
    assert all(row["profilePicUrl"]=="" for row in mw.contacts.values())
    assert mw.contacts["1@s.whatsapp.net"]["name"]=="Local name" and len(writes)==1
    pictures.apply_picture_changed(mw,"7@g.us")
    assert "profilePicThumbObj" not in mw.chats["7@g.us"] and mw.chats["7@g.us"]["messages"]=={"kept":True}
    pictures.apply_picture_changed(mw,"8@g.us")
    assert "8@g.us" not in mw.contacts and "8@g.us" not in mw.chats


def test_picture_account_validation_and_shutdown(monkeypatch):
    calls=[]
    monkeypatch.setattr(pictures.wx,"CallAfter",lambda fn,*a:calls.append(a))
    mw=N(token="current")
    client=N(main_window=mw,_belongs_to_this_session=lambda d:d.get("session")=="ours")
    for session,jid in [("other","7@g.us"),("ours","status@broadcast"),("ours","7@g.us")]:
        pictures.on_picture_changed(client,{"session":session,"chatId":jid})
    assert len(calls)==1 and calls[0][:2]==(mw,"7@g.us")
    mw.token="other"
    pictures.apply_picture_changed(*calls[0])  # no contacts: a stale notice must exit before touching them
    pictures.apply_picture_changed(N(_shutting_down=True),"7@g.us")
