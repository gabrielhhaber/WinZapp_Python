"""Native, keyboard-reachable shortcut capture with explicit confirmation."""
import wx
from ui.shortcut_names import shortcut_name
from ui.accessible import AccessibleShortcutCapture
from core.keyboard_shortcuts import CTRL, ALT


def capture_error(mod, key, global_hotkey=False):
    if global_hotkey and not mod & (CTRL | ALT):
        return 'shortcut_capture_modifier_required'
    if key in (wx.WXK_CONTROL, wx.WXK_ALT, wx.WXK_SHIFT, wx.WXK_WINDOWS_LEFT,
               wx.WXK_WINDOWS_RIGHT, wx.WXK_NONE):
        return 'shortcut_capture_wait'
    if (not mod & (CTRL | ALT) and 33 <= key < wx.WXK_START
            and key != wx.WXK_DELETE):
        return 'shortcut_capture_modifier_required'
    if (mod, key) in ((ALT, wx.WXK_TAB), (ALT, wx.WXK_F4),
                     (CTRL, wx.WXK_ESCAPE), (CTRL | ALT, wx.WXK_DELETE),
                     (CTRL | 4, wx.WXK_ESCAPE)):
        return 'shortcut_capture_reserved'
    return ''


class ShortcutCaptureDialog(wx.Dialog):
    def __init__(self, parent, action, binding, global_hotkey=False):
        self.main_window = parent.main_window
        self.i18n = self.main_window.i18n
        self.binding = binding
        self.vk = 0
        self.global_hotkey = global_hotkey
        super().__init__(parent, title=self.i18n.t('shortcut_change_title'))
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=action), 0, wx.ALL, 8)
        label = self.i18n.t('shortcut_capture_prompt')
        sizer.Add(wx.StaticText(self, label=label), 0, wx.ALL, 8)
        self.field = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.field.SetName(label)
        self.field.SetAccessible(AccessibleShortcutCapture(self.field, label))
        self.field.ChangeValue(shortcut_name(binding, self.i18n))
        self.field.Bind(wx.EVT_CHAR, self._char)
        sizer.Add(self.field, 0, wx.EXPAND | wx.ALL, 8)
        self.hint = wx.StaticText(self, label=self.i18n.t('shortcut_capture_hint'))
        self.hint.Wrap(450)
        sizer.Add(self.hint, 0, wx.EXPAND | wx.ALL, 8)
        buttons = wx.StdDialogButtonSizer()
        self.ok = wx.Button(self, wx.ID_OK, self.i18n.t('ok'))
        self.ok.Enable(False)
        buttons.AddButton(self.ok)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, self.i18n.t('cancel')))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALIGN_CENTER | wx.ALL, 8)
        self.SetSizerAndFit(sizer)
        self.CenterOnParent()
        self.Bind(wx.EVT_CHAR_HOOK, self._key)
        self.field.SetFocus()

    def _char(self, event):
        if event.GetKeyCode() == wx.WXK_TAB:
            event.Skip()

    def _key(self, event):
        if wx.Window.FindFocus() is not self.field:
            event.Skip()
            return
        from ui.shortcut_bindings import event_chord
        mod, key = event_chord(event)
        if (key == wx.WXK_TAB and mod in (0, 4)) or (key == wx.WXK_ESCAPE and not mod):
            event.Skip()
            return
        error = capture_error(mod, key, self.global_hotkey)
        if mod & 8:  # event_chord marks Windows/Command gestures with 8
            error = 'shortcut_capture_reserved'
        if error:
            self.hint.SetLabel(self.i18n.t(error))
            self.main_window.speak_output.output(self.i18n.t(error))
            self.ok.Enable(False)
            return
        self.binding = mod, key
        self.vk = event.GetRawKeyCode()
        self.field.ChangeValue(shortcut_name(self.binding, self.i18n))
        self.field.SelectAll()
        self.hint.SetLabel(self.i18n.t('shortcut_capture_hint'))
        self.ok.Enable(True)
