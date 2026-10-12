"""Group contracts and UI callbacks on plain stubs; never construct wx objects."""

from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from core.group_operations import created_group, created_group_jid, member_outcomes, group_permissions
from core.group_invitation import connection_identity
from main_window.groups import GroupsMixin
from ui.group_actions import finish_leave_group
from ui.dialogs.new_group import NewGroupDialog
from ui.dialogs.add_member_dialog import AddMemberDialog, SelectGroupDialog


JID = '123-456@g.us'
PN = '111@c.us'


class API(GroupsMixin):
    wpp_server, wpp_port, token = 'http://localhost', 6300, 'synthetic-token'
    i18n = SimpleNamespace(t=lambda key: key)


def response(body, status=201):
    return SimpleNamespace(status_code=status, json=lambda: body, text='')


@pytest.mark.parametrize('gid', ['123-456',JID,{'_serialized':JID},{'user':'123-456'}])
def test_creation_reads_real_server_group_info_and_legacy_gid(gid):
    assert created_group_jid({'status':'success','response':{'groupInfo':[{'id':gid}]}}) == JID
    assert created_group_jid({'response':{'gid':gid}}) == JID


@pytest.mark.parametrize('body', [None,{}, {'response':{}}, {'response':{'groupInfo':[]}},
    {'response':{'gid':'123@lid'}}, {'status':'error','response':{'gid':JID}},
    {'response':{'groupInfo':[{'id':JID},{'id':'2@g.us'}]}}])
def test_creation_never_confirms_missing_invalid_or_multiple_group_ids(body):
    assert created_group_jid(body) == ''


@pytest.mark.parametrize('code,confirmed', [(200,True),(409,True),(421,False),(403,False),(None,False)])
def test_created_group_keeps_partial_membership_separate_from_creation(code, confirmed):
    result = created_group({'response':{'groupInfo':[{'id':JID,'participants':{PN:{'code':code}}}]}}, [PN])
    assert result.jid == JID
    assert result.members_confirmed is confirmed


def test_create_wrapper_normalizes_phone_and_returns_real_group(monkeypatch):
    post=Mock(return_value=response({'status':'success','response':{'groupInfo':[
        {'id':'123-456','participants':{PN:{'code':200}}}]}}))
    monkeypatch.setattr('main_window.groups.api_post',post)
    ok,result=API().create_group('Group',['111@s.whatsapp.net'])
    assert ok and result.jid == JID and result.members_confirmed
    assert post.call_args.kwargs['json']['participants'] == [PN]


def test_create_timeout_never_retries_or_reports_success(monkeypatch):
    post=Mock(side_effect=TimeoutError('private native error'))
    monkeypatch.setattr('main_window.groups.api_post',post)
    assert API().create_group('Group',[PN]) == (False,'group_create_unconfirmed')
    post.assert_called_once()


@pytest.mark.parametrize('status,body,ok', [(200,{'status':'success'},True),
    (500,{'status':'success'},False),(200,{'status':'error'},False),(200,{},False),(200,None,False)])
def test_leave_checks_http_and_explicit_confirmation(monkeypatch,status,body,ok):
    monkeypatch.setattr('main_window.groups.api_post',Mock(return_value=response(body,status)))
    api=API(); api.archive_chat=Mock()
    assert api.leave_group(JID)[0] is ok
    api.archive_chat.assert_not_called()


def test_leave_uses_captured_connection_even_if_current_token_changes(monkeypatch):
    post=Mock(side_effect=TimeoutError('private token'))
    monkeypatch.setattr('main_window.groups.api_post',post)
    api=API(); connection=connection_identity(api); api.token='replacement'
    assert api.leave_group(JID,connection)[0] is False
    assert '/synthetic-token/leave-group' in post.call_args.args[0]
    post.assert_called_once()


@pytest.mark.parametrize('ok,changed',[(False,False),(True,False),(True,True)])
def test_leave_ui_only_closes_and_archives_confirmed_current_account(monkeypatch,ok,changed):
    box=Mock(); monkeypatch.setattr('ui.group_actions.wx.MessageBox',box)
    api=API(); connection=connection_identity(api)
    api.conversations_panel=SimpleNamespace(conversation={'remoteJid':JID},close_conversation=Mock())
    api._set_archived_state=Mock()
    if changed: api.token='other'
    finish_leave_group(api,JID,ok,'unconfirmed',connection)
    assert api._set_archived_state.called is (ok and not changed)
    assert api.conversations_panel.close_conversation.called is (ok and not changed)
    assert box.called is (not ok and not changed)


@pytest.mark.parametrize('code,state',[(200,'added'),('409','added'),(421,'pending'),
    (403,'invitable'),(408,'failed'),(True,'unknown'),(None,'unknown')])
def test_member_outcomes(code,state):
    body={'response':{'result':[{PN:{'code':code,'invite_code':'synthetic'}}]}}
    assert member_outcomes(body,['111@s.whatsapp.net']) == [('111@s.whatsapp.net',state)]


def test_member_result_requires_every_requested_identity_and_no_alias_guess():
    body={'response':{'result':[{PN:{'code':200}}]}}
    assert member_outcomes(body,[PN,'222@lid']) == [(PN,'added'),('222@lid','unknown')]
    body['response']['result'].append({PN:{'code':403,'invite_code':'synthetic'}})
    assert member_outcomes(body,[PN]) == [(PN,'unknown')]


def test_pending_add_is_informational_and_not_an_added_or_sent_claim(monkeypatch):
    monkeypatch.setattr('main_window.groups.api_post',Mock(return_value=response(
        {'response':{'result':[{PN:{'code':421}}]}})))
    ok,notice=API().add_group_members(JID,[PN])
    assert ok and notice == 'group_members_pending'


@pytest.mark.parametrize('admin,member,metadata,expected',[
    (True,True,{'restrict':True,'memberAddMode':'admin_add'},(True,True)),
    (False,True,{'restrict':False,'memberAddMode':'all_member_add'},(True,True)),
    (False,True,{'restrict':True,'memberAddMode':'admin_add'},(False,False)),
    (False,True,{},(False,True)), (False,False,{'restrict':False},(False,False)),
    (False,True,{'restrict':'false'},(False,True)),
])
def test_permission_policy_separates_member_editing_and_admin_actions(admin,member,metadata,expected):
    assert group_permissions(admin,member,metadata) == expected


@pytest.mark.parametrize('dialog_class',[AddMemberDialog,SelectGroupDialog])
def test_pending_notice_reaches_both_add_dialogs_without_generic_added_text(monkeypatch,dialog_class):
    box=Mock(); monkeypatch.setattr('ui.dialogs.add_member_dialog.wx.MessageBox',box)
    stub=SimpleNamespace(_i18n=API.i18n, EndModal=Mock())
    dialog_class._finish(stub,True,'Awaiting approval')
    assert box.call_args.args[0] == 'Awaiting approval'
    stub.EndModal.assert_called_once()


def test_created_group_opens_actual_jid_and_warns_partial_membership(monkeypatch):
    box=Mock(); monkeypatch.setattr('ui.dialogs.new_group.wx.MessageBox',box)
    monkeypatch.setattr('ui.dialogs.new_group.wx.CallAfter',lambda fn,*args:fn(*args))
    api=API(); api.chats={}; api.conversations_panel=SimpleNamespace(navigate_to_conversation=Mock())
    stub=SimpleNamespace(_mw=api,EndModal=Mock())
    result=created_group({'response':{'groupInfo':[{'id':JID}]}},[PN])
    NewGroupDialog._on_create_done(stub,True,result)
    api.conversations_panel.navigate_to_conversation.assert_called_once_with({'remoteJid':JID})
    assert box.call_args.args[0] == 'group_create_members_unconfirmed'
