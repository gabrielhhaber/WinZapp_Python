"""The quick audio-device list (Ctrl+Alt+Shift+H / Ctrl+Alt+Shift+G).

A plain wx.ListBox, so the screen reader reads each device as focus moves and
announces the one in use on opening. A digit picks its device at once (top row
or numeric keypad); arrows and Enter pick any row, the unnumbered ones and the
system default included; Esc leaves everything as it was. The rows come from
core.quick_audio_devices.quick_device_rows().
"""

from ui.shortcut_bindings import command_key_event
import wx

from core.quick_audio_devices import slot_for_digit


def typed_digit(key_code: int) -> str | None:
    """The digit a key produces, top row or keypad, or None."""
    if ord("0") <= key_code <= ord("9"):
        return chr(key_code)
    if wx.WXK_NUMPAD0 <= key_code <= wx.WXK_NUMPAD9:
        return str(key_code - wx.WXK_NUMPAD0)
    return None


class QuickAudioDeviceDialog(wx.Dialog):
    def __init__(self, parent, title: str, rows, focus: int):
        super().__init__(parent, title=title)
        self._rows = list(rows)
        self.chosen = None  # device name ("" = system default) once picked

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.list = wx.ListBox(self, choices=[label for label, _ in self._rows],
                               style=wx.LB_SINGLE)
        self.list.SetName(title)
        sizer.Add(self.list, 1, wx.EXPAND | wx.ALL, 8)
        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        if buttons:
            sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizerAndFit(sizer)
        self.SetMinSize((420, 260))

        if self._rows:
            self.list.SetSelection(max(0, min(focus, len(self._rows) - 1)))
        self.list.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self._pick(self.list.GetSelection()))
        self.Bind(wx.EVT_BUTTON, lambda e: self._pick(self.list.GetSelection()), id=wx.ID_OK)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.list.SetFocus()

    def _on_char_hook(self, event):
        event = command_key_event(self, 'device', event)
        if not event.HasAnyModifiers():
            digit = typed_digit(event.GetKeyCode())
            slot = slot_for_digit(digit) if digit is not None else None
            # Only the numbered rows answer to a digit; the default row and any
            # device past the tenth sit after them and have no digit.
            if slot is not None and slot < len(self._rows) - 1 and self._rows[slot][0].startswith(f"{digit}."):
                self._pick(slot)
                return
            if event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                self._pick(self.list.GetSelection())
                return
        event.Skip()

    def _pick(self, index: int):
        if 0 <= index < len(self._rows):
            self.chosen = self._rows[index][1]
            self.EndModal(wx.ID_OK)
