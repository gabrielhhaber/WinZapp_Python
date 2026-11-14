"""Real wx event dispatch from composer menu into writing-help modal.

Synthetic draft, no provider requests and no NVDA claim.
"""
import os
from pathlib import Path
from types import SimpleNamespace
import wx
import pytest
from app_settings import AppSettings
from core.ai_media import config as ai_config
from core.i18n import I18n
from tests.conftest import hidden_frame, destroy_now
from ui.conversation_panel.composer import ComposerMixin
from ui.conversation_panel.ai_writing import AIWritingMixin
from ui.dialogs import ai_writing_dialog as module

pytestmark = pytest.mark.wxgui


@pytest.mark.parametrize("entry", ["context_menu", "visible_button"])
def test_context_menu_choice_opens_real_modal_once(wx_app, tmp_path, monkeypatch, entry):
    frame = hidden_frame()
    frame.settings = {'general': {'language': 'es-ES'}}
    frame.i18n = I18n(frame)
    frame._app_settings = AppSettings(str(tmp_path))
    frame.is_chat_locked = lambda chat: False
    frame._shutting_down = False

    class Panel(ComposerMixin, AIWritingMixin, wx.Panel):
        pass

    panel = Panel(frame)
    panel.main_window = frame
    panel.conversation = {'remoteJid': 'synthetic-chat'}
    panel.message_field = wx.TextCtrl(panel, value='hola como estas', style=wx.TE_MULTILINE)
    panel._spell_checker = None
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(panel.message_field, 0, wx.EXPAND | wx.ALL, 8)
    emoji = wx.Button(panel, label=frame.i18n.t('emoji_button'))
    layout.Add(emoji, 0, wx.LEFT | wx.BOTTOM, 8)
    panel._writing_help_btn = panel._create_writing_help_button(panel)
    layout.Add(panel._writing_help_btn, 0, wx.LEFT | wx.BOTTOM, 8)
    panel.SetSizer(layout)
    frame.SetClientSize((680, 270))
    frame.Show()
    frame.Layout()
    wx.Yield()
    assert panel._writing_help_btn.GetName() == frame.i18n.t('writing_menu')
    assert panel._writing_help_btn.IsEnabled()
    panel.message_field.ChangeValue('')
    panel._sync_writing_help_button()
    assert not panel._writing_help_btn.IsEnabled()
    panel.message_field.ChangeValue('hola como estas')
    panel._sync_writing_help_button()
    assert panel._writing_help_btn.IsEnabled()
    panel.message_field.SetEditable(False)
    panel._sync_writing_help_button()
    assert not panel._writing_help_btn.IsEnabled()
    panel.message_field.SetEditable(True)
    panel._sync_writing_help_button()

    monkeypatch.setattr(module, 'CredentialStore', lambda _: SimpleNamespace(saved=lambda: ['gemini']))
    monkeypatch.setattr(module, 'global_dir', lambda: str(tmp_path))
    monkeypatch.setattr(module, 'active_account_id', lambda: 'synthetic-account')
    monkeypatch.setattr(panel, '_ai_prefs', lambda: (frame._app_settings, ai_config.preferences(frame._app_settings)))
    monkeypatch.setattr(module.WritingHelpDialog, '_regenerate', lambda *a: None)
    opened = []
    original_show = module.WritingHelpDialog.ShowModal

    def show(dialog):
        opened.append(dialog)
        def finish():
            capture(dialog, 'writing-from-' + entry)
            dialog.EndModal(wx.ID_CANCEL)
        wx.CallLater(100, finish)
        return original_show(dialog)

    monkeypatch.setattr(module.WritingHelpDialog, 'ShowModal', show)

    def select_writing(menu, *args):
        item = next(item for item in menu.GetMenuItems()
                    if item.GetItemLabel() == frame.i18n.t('writing_menu'))
        command = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
        command.SetEventObject(menu)
        assert menu.ProcessEvent(command)

    monkeypatch.setattr(panel.message_field, 'PopupMenu', select_writing)
    panel.message_field.Bind(wx.EVT_CONTEXT_MENU, panel._on_message_field_context_menu)
    try:
        capture(frame, 'composer-visible-button')
        if entry == 'context_menu':
            event = wx.ContextMenuEvent(wx.EVT_CONTEXT_MENU.typeId,
                                       panel.message_field.GetId(), wx.DefaultPosition)
            event.SetEventObject(panel.message_field)
            assert panel.message_field.GetEventHandler().ProcessEvent(event)
        else:
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, panel._writing_help_btn.GetId())
            event.SetEventObject(panel._writing_help_btn)
            assert panel._writing_help_btn.GetEventHandler().ProcessEvent(event)
        assert len(opened) == 1
        assert panel._writing_dialog is None
        assert panel.message_field.GetValue() == 'hola como estas'
    finally:
        destroy_now(frame)


def capture(window, name):
    out = os.environ.get('WINZAPP_VISUAL_OUTPUT')
    if not out or wx.Platform != '__WXMSW__':
        return
    import ctypes, win32gui, win32ui
    from PIL import Image
    folder = Path(out)
    folder.mkdir(exist_ok=True, parents=True)
    hwnd = window.GetHandle()
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    source = win32ui.CreateDCFromHandle(win32gui.GetWindowDC(hwnd))
    target = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source, right-left, bottom-top)
    target.SelectObject(bitmap)
    try:
        assert ctypes.windll.user32.PrintWindow(hwnd, target.GetSafeHdc(), 3)
        info = bitmap.GetInfo()
        image = Image.frombuffer('RGB', (info['bmWidth'], info['bmHeight']),
                                 bitmap.GetBitmapBits(True), 'raw', 'BGRX', 0, 1)
        image.save(folder / (name + '.png'))
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        target.DeleteDC()
        source.DeleteDC()
