"""Group invite contracts and asynchronous UI decisions without creating wx windows."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import wx
from core.group_join import GroupPreview, invitation_code, preview_group, join_group
from ui.dialogs.group_join_dialog import GroupJoinDialog, open_join_dialog
from main_window.chats_store import ChatsStoreMixin

CODE = 'AbCdEfGhIjKlMnOpQrStUv'
LINK = 'https://chat.whatsapp.com/' + CODE
CONNECTION = ('http://localhost', 6300, 'synthetic-token')
PREVIEW = GroupPreview('123-456@g.us', 'Test group', 'Description', 7, True)


@pytest.mark.parametrize('link', [LINK, ' ' + LINK + '\n', 'https://chat.whatsapp.com/invite/' + CODE])
def test_invitation_link_is_parsed_locally(link):
    assert invitation_code(link) == CODE


@pytest.mark.parametrize('link', [
    # What WhatsApp itself hands out today: seen on real messages through the
    # page's own store, 2 of the 11 invitation links loaded carried "?mode=".
    LINK + '?mode=ems_copy_t', LINK + '/', LINK + '#anchor', LINK + '?x=1&y=2',
    # What the web page's "Join chat" button asks Windows to open.
    'whatsapp://chat?code=' + CODE, 'whatsapp://chat/?code=' + CODE,
    'whatsapp://chat?foo=1&code=' + CODE + '&bar=2'])
def test_the_forms_whatsapp_really_uses_are_accepted(link):
    assert invitation_code(link) == CODE


@pytest.mark.parametrize('link', [None, CODE, 'http://chat.whatsapp.com/' + CODE,
    LINK + '/extra', 'https://chat.whatsapp.com.evil/' + CODE, 'file:///private',
    'https://evil.example/?next=' + LINK, 'whatsapp://send?phone=5511999999999',
    'whatsapp://chat?code=short', 'whatsapp://chat?mycode=' + CODE,
    LINK + ' and more text'])
def test_other_urls_and_bare_codes_do_not_start_a_preview(link):
    assert invitation_code(link) == ''


def transport(monkeypatch, data, status=200, body_status='success'):
    request = Mock(return_value=SimpleNamespace(status_code=status,
        json=lambda: {'status':body_status, 'response':data}))
    monkeypatch.setattr('core.group_join.api_post', request)
    return request


def test_preview_is_read_only_and_uses_existing_lowercase_contract(monkeypatch):
    request = transport(monkeypatch, {'id':PREVIEW.jid,'subject':PREVIEW.name,'desc':PREVIEW.description,
        'size':7,'membershipApprovalMode':True})
    assert preview_group(CONNECTION, CODE) == PREVIEW
    request.assert_called_once_with('http://localhost:6300/api/synthetic-token/group-info-from-invite-link',
        json={'invitecode':CODE}, headers={'Authorization':'Bearer synthetic-token'}, timeout=20)


@pytest.mark.parametrize('size,approval', [(True,1), ('7','false'), (-1,None), (None,None)])
def test_unknown_preview_fields_are_not_invented(monkeypatch,size,approval):
    transport(monkeypatch, {'id':PREVIEW.jid,'subject':'Group','size':size,'membershipApprovalMode':approval})
    result = preview_group(CONNECTION,CODE)
    assert result.size is None and result.approval is None


@pytest.mark.parametrize('name', ['123-456@g.us', 'Group 111@lid'])
def test_raw_jid_does_not_become_the_group_name(monkeypatch,name):
    transport(monkeypatch, {'id':PREVIEW.jid,'subject':name})
    assert preview_group(CONNECTION,CODE).name == ''


@pytest.mark.parametrize('data', [None, {}, {'id':'123@lid','subject':'Group'}, {'id':PREVIEW.jid}])
def test_missing_group_identity_or_name_blocks_preview(monkeypatch,data):
    request = transport(monkeypatch,data)
    with pytest.raises(ValueError): preview_group(CONNECTION,CODE)
    request.assert_called_once()


@pytest.mark.parametrize('pending',[True,False])
def test_join_preserves_approval_and_never_retries(monkeypatch,pending):
    request = transport(monkeypatch, {'id':PREVIEW.jid,'pendingApproval':pending},201)
    assert join_group(CONNECTION,CODE,PREVIEW.jid) is pending
    request.assert_called_once_with('http://localhost:6300/api/synthetic-token/join-code',
        json={'inviteCode':CODE}, headers={'Authorization':'Bearer synthetic-token'},timeout=20)


@pytest.mark.parametrize('data', [None, {}, {'id':PREVIEW.jid}, {'id':PREVIEW.jid,'pendingApproval':'false'},
    {'id':'999@g.us','pendingApproval':False}])
def test_ambiguous_or_changed_group_join_is_terminal(monkeypatch,data):
    request = transport(monkeypatch,data,201)
    with pytest.raises(ValueError): join_group(CONNECTION,CODE,PREVIEW.jid)
    request.assert_called_once()


@pytest.mark.parametrize('status,body_status', [(500,'success'),(200,'error')])
def test_http_or_body_failure_cannot_confirm_membership(monkeypatch,status,body_status):
    request = transport(monkeypatch, {'id':PREVIEW.jid,'pendingApproval':False},status,body_status)
    with pytest.raises(ValueError): join_group(CONNECTION,CODE,PREVIEW.jid)
    request.assert_called_once()


def stub():
    mw=SimpleNamespace(wpp_server=CONNECTION[0],wpp_port=CONNECTION[1],token=CONNECTION[2],
        speak_output=SimpleNamespace(output=Mock()))
    dialog=SimpleNamespace(_mw=mw,_i18n=SimpleNamespace(t=lambda key:key),_connection=CONNECTION,
        _closed=threading.Event(),_preview=PREVIEW,_code=CODE,_pending='',_attempted=False,
        _input=Mock(),_read=Mock(),_join=Mock(),_details=Mock(),EndModal=Mock(),result=None)
    dialog._input.GetValue.return_value=LINK
    for name in ('_current','_controls','_announce','_on_change'):
        setattr(dialog,name,lambda *args,_name=name:getattr(GroupJoinDialog,_name)(dialog,*args))
    dialog._start=Mock()
    return dialog


def test_reading_or_editing_does_not_join_and_invalid_link_does_not_request():
    dialog=stub()
    GroupJoinDialog._on_change(dialog,None)
    assert dialog._preview is None
    dialog._start.assert_not_called()
    dialog._input.GetValue.return_value='https://example.com/not-an-invite'
    GroupJoinDialog._on_preview(dialog,None)
    dialog._start.assert_not_called()
    dialog._details.ChangeValue.assert_called_with('group_join_invalid_link')


def test_changed_link_requires_a_new_preview_before_join():
    dialog=stub(); dialog._input.GetValue.return_value=LINK[:-1]+'Z'
    GroupJoinDialog._on_join(dialog,None)
    dialog._start.assert_not_called()
    assert dialog._preview is None


def test_join_requires_preview_and_cannot_be_double_clicked():
    dialog=stub(); dialog._preview=None
    GroupJoinDialog._on_join(dialog,None); dialog._start.assert_not_called()
    dialog._preview=PREVIEW
    GroupJoinDialog._on_join(dialog,None)
    GroupJoinDialog._on_join(dialog,None)
    dialog._start.assert_called_once_with('join')
    assert dialog._attempted


@pytest.mark.parametrize('result,key', [(None,'group_join_unconfirmed'),(True,'group_join_pending')])
def test_uncertain_or_pending_join_does_not_open_chat_or_allow_retry(result,key):
    dialog=stub(); dialog._attempted=True; dialog._pending='join'
    GroupJoinDialog._finish(dialog,'join',result)
    dialog.EndModal.assert_not_called()
    assert dialog.result is None
    dialog._details.ChangeValue.assert_called_with(key)
    dialog._join.Enable.assert_called_with(False)
    GroupJoinDialog._on_join(dialog,None); dialog._start.assert_not_called()


def test_confirmed_membership_is_the_only_successful_dialog_result():
    dialog=stub(); dialog._attempted=True; dialog._pending='join'
    GroupJoinDialog._finish(dialog,'join',False)
    assert dialog.result == PREVIEW and dialog._closed.is_set()
    dialog.EndModal.assert_called_once_with(wx.ID_OK)


@pytest.mark.parametrize('state',['closed','shutdown','connection'])
def test_late_join_or_preview_cannot_update_another_context(state):
    for operation,result in [('preview',PREVIEW),('join',False)]:
        dialog=stub()
        if state=='closed': dialog._closed.set()
        elif state=='shutdown': dialog._mw._shutting_down=True
        else: dialog._mw.token='different'
        GroupJoinDialog._finish(dialog,operation,result)
        dialog.EndModal.assert_not_called()
        assert dialog.result is None
        dialog._mw.speak_output.output.assert_not_called()


def test_preview_shows_unknown_approval_without_claiming_immediate_membership():
    dialog=stub(); dialog._pending='preview'
    result=GroupPreview(PREVIEW.jid,'', '',None,None)
    GroupJoinDialog._finish(dialog,'preview',result)
    dialog._details.ChangeValue.assert_called_with('unknown_group\n\ngroup_join_approval_unknown')
    dialog._join.Enable.assert_called_with(True)
    dialog._details.SetFocus.assert_called_once()


def test_worker_failure_is_reported_without_leaving_controls_stuck(monkeypatch):
    dialog=stub(); dialog._finish=lambda op,result:GroupJoinDialog._finish(dialog,op,result)
    thread=Mock(); thread.return_value.start.side_effect=RuntimeError('synthetic')
    monkeypatch.setattr('ui.dialogs.group_join_dialog.threading.Thread',thread)
    GroupJoinDialog._start(dialog,'preview')
    assert dialog._pending == '' and dialog._preview is None
    dialog._read.Enable.assert_called_with(True)


@pytest.mark.parametrize('modal',[wx.ID_CANCEL,wx.ID_OK])
def test_entry_point_only_inserts_and_opens_a_confirmed_group(monkeypatch,modal):
    dialog=Mock(result=PREVIEW); dialog.ShowModal.return_value=modal
    monkeypatch.setattr('ui.dialogs.group_join_dialog.GroupJoinDialog',Mock(return_value=dialog))
    after=Mock(); monkeypatch.setattr(wx,'CallAfter',after)
    mw=SimpleNamespace(chats={},_schedule_set_chats=Mock(),conversations_panel=SimpleNamespace(navigate_to_conversation=Mock()))
    owner=SimpleNamespace(_mw=mw,EndModal=Mock())
    open_join_dialog(owner)
    dialog.Destroy.assert_called_once()
    if modal==wx.ID_CANCEL:
        assert mw.chats=={}
        owner.EndModal.assert_not_called(); after.assert_not_called()
    else:
        display_stub=SimpleNamespace(_resolve_contact_name=lambda chat:'',find_name_through_messages=lambda chat:'')
        assert ChatsStoreMixin.chat_display_name(display_stub,mw.chats[PREVIEW.jid]) == PREVIEW.name
        from ui.dialogs.group_join_dialog import _go_to_group
        after.assert_called_once_with(_go_to_group,mw,PREVIEW.jid,PREVIEW.name)
