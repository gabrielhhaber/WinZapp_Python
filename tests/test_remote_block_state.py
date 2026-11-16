from types import SimpleNamespace as N

from core import remote_block_state as blocks
from main_window.chat_actions import ChatActionsMixin


def window():
    writes=[]
    mw=N(_blocked_contacts=set(),_lid_to_phone={"901@lid":"5511912345678@s.whatsapp.net"},
        _bare_phone_digits=ChatActionsMixin._bare_phone_digits,
        db=N(set_metadata_json=lambda key,data:writes.append(set(data))),_schedule_set_chats=lambda:None)
    mw._apply_block_state=lambda jid,value:ChatActionsMixin._apply_block_state(mw,jid,value)
    return mw,writes


def test_block_and_unblock_resolve_known_lid_and_brazilian_variants():
    mw,writes=window()
    blocks.apply_block_changed(mw,"901@lid",True)
    assert mw._blocked_contacts=={"5511912345678"}
    assert ChatActionsMixin.is_contact_blocked(mw,"901@lid") is True
    mw._blocked_contacts.add("551112345678")
    blocks.apply_block_changed(mw,"901@lid",False)
    assert mw._blocked_contacts==set() and writes[-1]==set()


def test_unknown_lid_is_never_treated_as_phone_digits_and_shutdown_is_quiet():
    mw,writes=window()
    mw._blocked_contacts.add("777")
    assert ChatActionsMixin.is_contact_blocked(mw,"777@lid") is False
    blocks.apply_block_changed(mw,"777@lid",True)
    assert writes==[] and mw._blocked_contacts=={"777"}
    mw._shutting_down=True
    blocks.apply_block_changed(mw,"901@lid",True)
    assert writes==[]


def test_refreshes_coalesce_and_one_notice_during_fetch_gets_a_followup(monkeypatch):
    jobs,reads=[],[]
    monkeypatch.setattr(blocks.wx,"CallAfter",lambda fn,*a:fn(*a))
    mw=N(_msg_bg_executor=N(submit=lambda fn:jobs.append(fn)),
        get_block_list=lambda **kw:reads.append(kw))
    blocks.refresh_block_list(mw)
    blocks.refresh_block_list(mw)
    blocks.refresh_block_list(mw)
    assert len(jobs)==1
    jobs.pop(0)()
    assert len(jobs)==1 and len(reads)==1
    jobs.pop(0)()
    assert len(reads)==2 and not mw._block_event_refreshing


def test_account_scope_and_explicit_boolean_validation(monkeypatch):
    calls=[]
    monkeypatch.setattr(blocks.wx,"CallAfter",lambda fn,*a:calls.append(a))
    client=N(main_window="mw",_belongs_to_this_session=lambda d:d.get("session")=="ours")
    good={"session":"ours","kind":"contact","contactId":"1@c.us","blocked":False}
    for data in [{**good,"session":"other"},{**good,"blocked":"false"},good,
                 {"session":"ours","kind":"refresh"}]:
        blocks.on_block_state(client,data)
    assert calls==[("mw","1@s.whatsapp.net",False),("mw",)]


def test_old_poll_cannot_undo_a_block_notice_received_during_the_request(monkeypatch):
    from main_window import chat_actions
    mw,writes=window()
    mw.wpp_server,mw.wpp_port,mw.token="http://local",6300,"test"
    def get(*args,**kwargs):
        blocks.apply_block_changed(mw,"901@lid",True)
        return N(status_code=200,json=lambda:{"response":[]})
    monkeypatch.setattr(chat_actions,"api_get",get)
    ChatActionsMixin.get_block_list(mw)
    assert mw._blocked_contacts=={"5511912345678"} and len(writes)==1


def test_refresh_captures_account_before_worker_runs(monkeypatch):
    jobs,reads=[],[]
    monkeypatch.setattr(blocks.wx,"CallAfter",lambda fn,*a:fn(*a))
    mw=N(token="first",_msg_bg_executor=N(submit=lambda fn:jobs.append(fn)),
        get_block_list=lambda **kw:reads.append(kw["expected_context"]))
    blocks.refresh_block_list(mw)
    mw.token="second"
    jobs.pop()()
    assert reads[0].token=="first"
