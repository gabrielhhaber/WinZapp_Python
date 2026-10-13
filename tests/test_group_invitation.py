"""Invitation HTTP and late callbacks on stubs, plus isolated Node controller tests."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from core.group_invitation import read_invitation, connection_identity
from core.group_operations import invitation_link
from ui.dialogs.group_invitation_dialog import GroupInvitationDialog
from ui.dialogs.group_invitation_dialog import add_invitation_button
from ui.dialogs.conversation_data_dialog import ConversationDataDialog

LINK='https://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv'
PNG=b'\x89PNG\r\n\x1a\nsynthetic'
CONNECTION=('http://localhost',6300,'synthetic-token')


def test_invitation_waits_for_metadata_before_snapshotting_admin_permission(monkeypatch):
    button = Mock()
    button.Enable.side_effect = lambda enabled: setattr(button, 'enabled', enabled)
    button.IsEnabled.side_effect = lambda: button.enabled
    monkeypatch.setattr('ui.dialogs.group_invitation_dialog.wx.Button', Mock(return_value=button))
    dialog = Mock()
    dialog_factory = Mock(return_value=dialog)
    monkeypatch.setattr('ui.dialogs.group_invitation_dialog.GroupInvitationDialog', dialog_factory)
    part = Mock()
    part.GetItemCount.return_value = 0
    owner = SimpleNamespace(_i18n=SimpleNamespace(t=lambda key:key), _jid='123@g.us',
        _name='Group', _user_is_admin=False, _overview_ctrl=Mock(), _part_list=part,
        _apply_subject_to_title=Mock(), _participant_row=lambda jid,flag:('Member','','Member',jid),
        _mw=SimpleNamespace(my_jid='111@c.us',my_lid='',_phone_digits_equivalent=lambda a,b:a==b),
        _requests_btn=Mock(),
        _add_members_btn=Mock(),_add_members_btn_overview=Mock(),_edit_name_btn=Mock(),_edit_desc_btn=Mock())
    owner._invitation_btn = add_invitation_button(owner, Mock(), Mock())
    click = button.Bind.call_args.args[1]
    click(None)
    dialog_factory.assert_not_called()
    ConversationDataDialog._populate_group_unsafe(owner, {
        'participants':[{'id':'111@c.us','isAdmin':True}], 'restrict':True, 'memberAddMode':'admin_add'})
    click(None)
    dialog_factory.assert_called_once_with(owner, owner._mw, '123@g.us', True)
    dialog.ShowModal.assert_called_once()
    dialog.Destroy.assert_called_once()


def payload():
    return {'status':'success','response':{'link':LINK,'qr':'data:image/png;base64,'+base64.b64encode(PNG).decode()}}


@pytest.mark.parametrize('reset',[False,True])
def test_invitation_uses_get_for_read_and_post_for_explicit_reset(monkeypatch,reset):
    get,post=Mock(),Mock()
    for request in (get,post): request.return_value=SimpleNamespace(status_code=200,json=payload)
    monkeypatch.setattr('core.group_invitation.api_get',get)
    monkeypatch.setattr('core.group_invitation.api_post',post)
    assert read_invitation(CONNECTION,'123@g.us',reset) == (LINK,PNG)
    request=post if reset else get
    request.assert_called_once()
    assert request.call_args.args[0].endswith('/reset' if reset else '/123@g.us')
    (get if reset else post).assert_not_called()


@pytest.mark.parametrize('link',[None,'http://chat.whatsapp.com/AbCdEfGhIjKlMnOpQrStUv',
    LINK+'?secret=yes',LINK+'#bad','https://chat.whatsapp.com.evil/test', 'file:///private'])
def test_link_validation(link):
    assert invitation_link(link) == ''


@pytest.mark.parametrize('body',[None,{}, {'status':'error','response':{'link':LINK}},
    {'status':'success','response':{'link':LINK,'qr':'data:image/png;base64,not-base64'}},
    {'status':'success','response':{'link':LINK,'qr':'data:image/png;base64,aGVsbG8='}}])
def test_malformed_results_do_not_enable_share_or_save(monkeypatch,body):
    request=Mock(return_value=SimpleNamespace(status_code=200,json=lambda:body))
    monkeypatch.setattr('core.group_invitation.api_get',request)
    with pytest.raises(ValueError): read_invitation(CONNECTION,'123@g.us')
    request.assert_called_once()


def dialog_stub():
    mw=SimpleNamespace(wpp_server=CONNECTION[0],wpp_port=CONNECTION[1],token=CONNECTION[2],
                       speak_output=SimpleNamespace(output=Mock()))
    stub=SimpleNamespace(_mw=mw,_connection=connection_identity(mw),_closed=threading.Event(),
        _i18n=SimpleNamespace(t=lambda key:key),_link=LINK,_png=PNG,_link_ctrl=Mock(),_qr=Mock(),
        _status=Mock(),_copy=Mock(),_save=Mock(),_refresh=Mock(),_reset=Mock(),_is_admin=True,
        _busy_loading=False,Layout=Mock())
    stub._clear=lambda:GroupInvitationDialog._clear(stub)
    stub._busy=lambda value:GroupInvitationDialog._busy(stub,value)
    return stub


def test_uncertain_reset_clears_stale_link_and_does_not_announce_success():
    stub=dialog_stub(); stub._clear()
    GroupInvitationDialog._finish(stub,None,True)
    assert stub._link == '' and stub._png == b''
    stub._copy.Enable.assert_called_with(False)
    stub._save.Enable.assert_called_with(False)
    stub._mw.speak_output.output.assert_called_with('group_invitation_reset_unconfirmed')


def test_image_decode_exception_does_not_escape_callback_or_enable_sharing(monkeypatch):
    stub=dialog_stub(); stub._clear()
    monkeypatch.setattr('ui.dialogs.group_invitation_dialog.wx.Image',Mock(side_effect=ValueError('bad PNG')))
    GroupInvitationDialog._finish(stub,(LINK,PNG),False)
    assert stub._link == '' and stub._png == b''
    stub._copy.Enable.assert_called_with(False)
    stub._save.Enable.assert_called_with(False)
    stub._status.ChangeValue.assert_called_with('group_invitation_unavailable')


@pytest.mark.parametrize('closed',[False,True])
def test_late_result_does_not_expose_invite_after_close_or_account_change(closed):
    stub=dialog_stub()
    if closed: stub._closed.set()
    else: stub._mw.token='changed'
    GroupInvitationDialog._finish(stub,(LINK,PNG),False)
    stub._qr.SetBitmap.assert_not_called() if closed else None
    stub._link_ctrl.ChangeValue.assert_not_called() if closed else stub._link_ctrl.ChangeValue.assert_called_with('')
    stub._mw.speak_output.output.assert_not_called()


def test_reset_requires_confirmation_and_clears_old_data_before_request(monkeypatch):
    import wx
    stub=dialog_stub(); stub._load=Mock()
    confirm=Mock(return_value=wx.NO); monkeypatch.setattr(wx,'MessageBox',confirm)
    GroupInvitationDialog._on_reset(stub,None); stub._load.assert_not_called()
    confirm.return_value=wx.YES
    GroupInvitationDialog._on_reset(stub,None); stub._load.assert_called_once_with(reset=True)
    stub._is_admin=False; stub._load.reset_mock()
    GroupInvitationDialog._on_reset(stub,None); stub._load.assert_not_called()


def test_load_clears_link_before_worker_and_blocks_overlap(monkeypatch):
    stub=dialog_stub(); stub._jid='123@g.us'; stub._finish=Mock()
    worker=Mock(); monkeypatch.setattr('ui.dialogs.group_invitation_dialog.threading.Thread',worker)
    GroupInvitationDialog._load(stub,True)
    assert stub._link == '' and stub._png == b'' and stub._busy_loading
    worker.assert_called_once()
    GroupInvitationDialog._load(stub,True); worker.assert_called_once()


def test_native_group_controller_contracts(tmp_path):
    root=Path(__file__).resolve().parents[1]
    modules=Path(os.environ.get('WINZAPP_TEST_NODE_MODULES',root/'client/api/node_modules'))
    node=shutil.which('node')
    if not node or not (modules/'ts-jest').is_dir():
        pytest.skip('Existing Node test dependencies unavailable; no install permitted')
    config={'rootDir':str(root/'client/api_patches'), 'testEnvironment':'node',
        'cacheDirectory':str(tmp_path/'jest-cache'), 'moduleDirectories':[str(modules),'node_modules'],
        'transform':{'^.+\\.tsx?$':[str(modules/'ts-jest'),{'diagnostics':False,'tsconfig':{
            'target':'ES2020','module':'commonjs','esModuleInterop':True}}]}}
    completed=subprocess.run([node,str(modules/'jest/bin/jest.js'),'--runInBand','--config',json.dumps(config),
        '--runTestsByPath',str(root/'client/api_patches/src/tests/controller/groupOperationsController.test.ts')],
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=60,
        env={**os.environ,'NODE_PATH':str(modules),'TEMP':str(tmp_path),'TMP':str(tmp_path),
             'TMPDIR':str(tmp_path)})
    assert completed.returncode == 0, completed.stdout+completed.stderr
