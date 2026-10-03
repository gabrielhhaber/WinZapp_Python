"""Bring a modal dialog in front when its parent window is hidden in the tray.

A dialog owned by a hidden top-level window (autostart with --background, or a
window closed to the tray) opens without anything telling Windows to activate
it: the process is not the foreground one, so foreground-stealing prevention
turns the activation into a flashing taskbar button at best, and the dialog
sits unseen behind other windows with no keyboard focus. For a blind user that
is the same as no dialog. The update prompts hit exactly that.

``restore_window`` has the working recipe (attach our input queue to the
foreground thread's, then SetForegroundWindow); it is reused here for a dialog
instead of the main window, which stays hidden.

The prompt is spoken BEFORE the raise, whatever the raise achieves: whether a
dialog really came to the front cannot be known from here, and a blind user must
never be left with a modal nobody announced. (A screen reader that then reads the
focused dialog as well repeats it once; silence is the worse failure.)

Message boxes are real ``wx.Dialog``s in the hidden case, not
``wx.MessageDialog``: on Windows that one is the native message box, which has
no wx handle to raise and reports ``IsShown()`` False.
"""

import ctypes
import logging
import sys
from ctypes import wintypes

import wx

# Long enough for ShowModal() to have shown the dialog (the timer is armed
# before ShowModal() and fires inside its event loop), short enough that the
# user does not sit in silence.
_FRONT_DELAY_MS = 200


def parent_is_hidden(main_window) -> bool:
    """True when the main window is not on screen: tray-hidden or never shown."""
    return bool(getattr(main_window, "_window_hidden", False)
                or getattr(main_window, "background_mode", False))


def force_foreground(hwnd) -> bool:
    """SetForegroundWindow that survives the foreground lock; True if *hwnd*
    really is the foreground window afterwards. False for a null handle and off
    Windows."""
    if not hwnd or sys.platform != "win32":
        return False
    # A private WinDLL: argtypes/restype set on ctypes.windll.user32 would
    # change every other caller in the process (restore_window uses defaults).
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    for name in ("BringWindowToTop", "SetForegroundWindow", "SetActiveWindow"):
        func = getattr(user32, name)
        func.argtypes = [wintypes.HWND]
        func.restype = wintypes.BOOL if name != "SetActiveWindow" else wintypes.HWND
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    target = wintypes.HWND(hwnd)
    attached = False
    fg_thread = cur_thread = 0
    try:
        fg_hwnd = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
        cur_thread = kernel32.GetCurrentThreadId()
        if fg_thread and fg_thread != cur_thread:
            attached = bool(user32.AttachThreadInput(fg_thread, cur_thread, True))
        user32.BringWindowToTop(target)
        user32.SetForegroundWindow(target)
        user32.SetActiveWindow(target)
    finally:
        if attached:
            user32.AttachThreadInput(fg_thread, cur_thread, False)
    return user32.GetForegroundWindow() == hwnd


def _speak(announce) -> None:
    if announce is None:
        return
    try:
        announce()
    except Exception:
        logging.exception("[dialog_foreground] announcing the dialog failed")


def bring_to_front_if_hidden(main_window, dialog, announce=None) -> None:
    """Call right before ``dialog.ShowModal()``. With the main window visible it
    does nothing (normal ownership already behaves). With it hidden, calls
    *announce* now and brings the dialog to the front shortly after it shows."""
    if not parent_is_hidden(main_window):
        return
    _speak(announce)

    def _front():
        try:
            if not dialog.IsShown():
                return  # answered or closed before the timer fired
            dialog.Raise()
            if not force_foreground(dialog.GetHandle()):
                logging.info("[dialog_foreground] Windows kept the foreground; the "
                             "dialog is open but was only announced")
        except RuntimeError:
            return  # the C++ dialog is already gone
        except Exception:
            logging.exception("[dialog_foreground] bringing the dialog to the front failed")

    wx.CallLater(_FRONT_DELAY_MS, _front)


def message_box_buttons(style: int):
    """(button ids, default id, escape id) for a wx.MessageBox style: the
    buttons and the default the native box would have had."""
    if style & wx.YES_NO:
        default = wx.ID_NO if style & wx.NO_DEFAULT else wx.ID_YES
        return [wx.ID_YES, wx.ID_NO], default, wx.ID_NO
    return [wx.ID_OK], wx.ID_OK, wx.ID_OK


#: What wx.MessageBox answers, which callers compare against.
_ANSWER = {wx.ID_YES: wx.YES, wx.ID_NO: wx.NO, wx.ID_OK: wx.OK}


class _HiddenParentMessage(wx.Dialog):
    """Plain message with stock buttons: the same labels, default and Escape
    behaviour as the message box it stands in for."""

    def __init__(self, parent, message, title, style):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE)
        ids, default, escape = message_box_buttons(style)
        sizer = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(self, label=message)
        label.Wrap(420)
        sizer.Add(label, 0, wx.ALL, 12)
        row = wx.BoxSizer(wx.HORIZONTAL)
        buttons = {}
        for ident in ids:
            btn = wx.Button(self, ident)
            btn.Bind(wx.EVT_BUTTON, lambda event, i=ident: self.EndModal(i))
            row.Add(btn, 0, wx.RIGHT, 4)
            buttons[ident] = btn
        sizer.Add(row, 0, wx.ALIGN_CENTER | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        self.SetSizerAndFit(sizer)
        self.SetEscapeId(escape)
        buttons[default].SetDefault()
        buttons[default].SetFocus()
        self.CentreOnScreen()


def message_box(main_window, message, title, style, announce=None):
    """``wx.MessageBox`` that comes to the front when the main window is hidden.

    With the main window visible it is exactly ``wx.MessageBox``; hidden, it is
    a ``_HiddenParentMessage`` so there is a handle to bring forward.
    """
    if not parent_is_hidden(main_window):
        return wx.MessageBox(message, title, style, main_window)
    dlg = _HiddenParentMessage(main_window, message, title, style)
    try:
        bring_to_front_if_hidden(main_window, dlg, announce)
        return _ANSWER.get(dlg.ShowModal(), wx.NO)
    finally:
        dlg.Destroy()
