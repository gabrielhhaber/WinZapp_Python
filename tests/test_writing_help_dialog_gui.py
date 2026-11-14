"""Native Windows controls with synthetic text, no account/key/network/NVDA."""
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
import wx
from app_settings import AppSettings
from core.ai_media import config as ai_config
from core.i18n import I18n
from tests.conftest import hidden_frame, destroy_now
from ui.dialogs import ai_writing_dialog as module

pytestmark = pytest.mark.wxgui


@pytest.mark.parametrize('language', ['es-ES', 'en-US', 'pt-BR', 'pt-PT', 'ar', 'it', 'pl', 'ro', 'tr-TR'])
def test_native_writing_dialog_layout_and_actions(wx_app, tmp_path, monkeypatch, language):
    frame = hidden_frame()
    frame.settings = {'general': {'language': language}}
    frame.i18n = I18n(frame)
    frame._app_settings = AppSettings(str(tmp_path))
    monkeypatch.setattr(module, 'CredentialStore', lambda _: SimpleNamespace(saved=lambda: ['gemini']))
    monkeypatch.setattr(module, 'global_dir', lambda: str(tmp_path))
    monkeypatch.setattr(module.WritingHelpDialog, '_regenerate', lambda *a: None)
    monkeypatch.setattr(module, 'active_account_id', lambda: 'synthetic-account')
    panel = wx.Panel(frame)
    panel.main_window = frame
    panel.conversation = {'remoteJid': 'synthetic-chat'}
    panel.message_field = wx.TextCtrl(panel, value='hola como estas')
    config = ai_config.preferences(frame._app_settings)
    dialog = module.WritingHelpDialog(panel, 'hola como estas', False, config, frame._app_settings)
    try:
        dialog.Show()
        wx.Yield()
        assert dialog.GetTitle() == frame.i18n.t('writing_title')
        assert dialog.result.GetWindowStyleFlag() & wx.TE_READONLY
        assert dialog.result.GetName() == frame.i18n.t('ai_result')
        assert dialog.action.GetCount() == 5
        assert dialog.target.GetCount() == 6
        assert not dialog.target.IsEnabled()
        assert not dialog.replace.IsEnabled()
        dialog.action.SetSelection(4)
        dialog._sync_target()
        assert dialog.target.IsEnabled()
        dialog._latest = 'Hola, ¿cómo estás?'
        dialog.result.ChangeValue(dialog._latest)
        dialog._busy(False)
        assert dialog.replace.IsEnabled()
        dialog._busy(True)
        assert not dialog.replace.IsEnabled()
        dialog._busy(False)
        out = os.environ.get('WINZAPP_VISUAL_OUTPUT')
        if out:
            folder = Path(out); folder.mkdir(parents=True, exist_ok=True)
            from tests.test_writing_menu_gui import capture
            capture(dialog, 'writing-' + language)
    finally:
        dialog._dispose()
        dialog.Destroy()
        destroy_now(frame)
