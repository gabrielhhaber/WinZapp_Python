"""Native event-loop regression: choosing/opening/closing never starts a request."""
from types import SimpleNamespace
import pytest
import wx
from app_settings import AppSettings
from core.ai_media import config as ai_config
from core.i18n import I18n
from tests.conftest import hidden_frame, destroy_now
from ui.dialogs import ai_writing_dialog as module
from ui.dialogs import ai_text_result_dialog as base

pytestmark = pytest.mark.wxgui


@pytest.mark.parametrize('exit_action', ['close', 'cancel', 'escape'])
def test_open_choose_and_exit_without_any_provider_or_google_call(wx_app, tmp_path, monkeypatch, exit_action):
    frame = hidden_frame()
    frame.settings = {'general': {'language': 'es-ES'}}
    frame.i18n = I18n(frame)
    app = AppSettings(str(tmp_path))
    monkeypatch.setattr(module, 'CredentialStore', lambda _: SimpleNamespace(saved=lambda: ['gemini']))
    monkeypatch.setattr(module, 'global_dir', lambda: str(tmp_path))
    monkeypatch.setattr(module, 'active_account_id', lambda: 'synthetic-account')
    monkeypatch.setattr(base, 'active_account_id', lambda: 'synthetic-account')
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('unexpected work')
        raise AssertionError('no work before an explicit Run action')
    monkeypatch.setattr(base, 'submit', forbidden)
    monkeypatch.setattr(module, 'run_chain', forbidden)
    monkeypatch.setattr(module.gtranslate, 'translate_text', forbidden)
    monkeypatch.setattr(module.WritingHelpDialog, '_consent', forbidden)
    panel = wx.Panel(frame)
    panel.main_window = frame
    panel.conversation = {'remoteJid': 'synthetic-chat'}
    panel.message_field = wx.TextCtrl(panel, value='synthetic draft')
    config = ai_config.preferences(app)
    config.update(consented_writing=['gemini'], consented_translate=True)
    dialog = module.WritingHelpDialog(panel, 'synthetic draft', False, config, app)
    try:
        dialog.Show()
        wx.Yield()
        assert not calls
        assert dialog.regenerate.GetLabel() == frame.i18n.t('writing_run_selected')
        for selection in range(dialog.action.GetCount()):
            dialog.action.SetSelection(selection)
            event = wx.CommandEvent(wx.EVT_CHOICE.typeId, dialog.action.GetId())
            event.SetEventObject(dialog.action)
            dialog.action.GetEventHandler().ProcessEvent(event)
            wx.Yield()
            assert not calls
        if exit_action == 'escape':
            dialog._key(SimpleNamespace(GetKeyCode=lambda: wx.WXK_ESCAPE))
        elif exit_action == 'cancel':
            dialog._cancel()
        else:
            dialog._close()
        wx.Yield()
        assert not calls
        assert panel.message_field.GetValue() == 'synthetic draft'
    finally:
        dialog._dispose()
        dialog.Destroy()
        destroy_now(frame)


@pytest.mark.parametrize('selection', [0, 4])
def test_only_run_button_submits_the_chosen_action_once(wx_app, tmp_path, monkeypatch, selection):
    frame = hidden_frame()
    frame.settings = {'general': {'language': 'es-ES'}}
    frame.i18n = I18n(frame)
    app = AppSettings(str(tmp_path))
    monkeypatch.setattr(module, 'CredentialStore', lambda _: SimpleNamespace(saved=lambda: ['gemini']))
    monkeypatch.setattr(module, 'global_dir', lambda: str(tmp_path))
    monkeypatch.setattr(module, 'active_account_id', lambda: 'synthetic-account')
    monkeypatch.setattr(base, 'active_account_id', lambda: 'synthetic-account')
    calls = []
    monkeypatch.setattr(module.WritingHelpDialog, '_consent', lambda d: calls.append('consent') or True)
    def worker(d, operation):
        calls.append(('worker', d._selected()))
        return lambda: ('synthetic result', 'mocked provider')
    monkeypatch.setattr(module.WritingHelpDialog, '_worker', worker)
    monkeypatch.setattr(base, 'submit', lambda work, callback: calls.append(('submit', work())))
    panel = wx.Panel(frame)
    panel.main_window = frame
    panel.conversation = {'remoteJid': 'synthetic-chat'}
    panel.message_field = wx.TextCtrl(panel, value='synthetic draft')
    dialog = module.WritingHelpDialog(panel, 'synthetic draft', False, ai_config.preferences(app), app)
    try:
        dialog.Show()
        wx.Yield()
        assert calls == []
        dialog.action.SetSelection(selection)
        dialog._action_changed(SimpleNamespace(Skip=lambda: None))
        wx.Yield()
        assert calls == []
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, dialog.regenerate.GetId())
        event.SetEventObject(dialog.regenerate)
        dialog.regenerate.GetEventHandler().ProcessEvent(event)
        wx.Yield()
        expected = 'check' if selection == 0 else 'translate'
        assert calls == ['consent', ('worker', expected), ('submit', ('synthetic result', 'mocked provider'))]
        assert panel.message_field.GetValue() == 'synthetic draft'
    finally:
        dialog._dispose()
        dialog.Destroy()
        destroy_now(frame)
