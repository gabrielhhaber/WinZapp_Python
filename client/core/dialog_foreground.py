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
"""

import ctypes
import logging

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
    really is the foreground window afterwards."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    attached = False
    fg_thread = cur_thread = 0
    try:
        fg_hwnd = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
        cur_thread = kernel32.GetCurrentThreadId()
        if fg_thread and fg_thread != cur_thread:
            attached = bool(user32.AttachThreadInput(fg_thread, cur_thread, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(fg_thread, cur_thread, False)
    return user32.GetForegroundWindow() == hwnd


def bring_to_front_if_hidden(main_window, dialog, announce=None) -> None:
    """Call right before ``dialog.ShowModal()``. With the main window visible it
    does nothing (normal ownership already behaves). With it hidden, brings the
    dialog to the front shortly after it shows; when Windows still refuses the
    foreground, calls *announce* so the user hears that something is waiting."""
    if not parent_is_hidden(main_window):
        return

    def _front():
        try:
            if not dialog.IsShown():
                return  # answered or closed before the timer fired
            dialog.Raise()
            in_front = force_foreground(dialog.GetHandle())
        except RuntimeError:
            return  # the C++ dialog is already gone
        except Exception:
            logging.exception("[dialog_foreground] bringing the dialog to the front failed")
            in_front = False
        if not in_front and announce is not None:
            try:
                announce()
            except Exception:
                logging.exception("[dialog_foreground] announcing the dialog failed")

    wx.CallLater(_FRONT_DELAY_MS, _front)


def message_box(main_window, message, title, style, announce=None):
    """``wx.MessageBox`` that comes to the front when the main window is hidden.

    With the main window visible it is exactly ``wx.MessageBox``; hidden, the
    same box is built as a ``wx.MessageDialog`` so there is a handle to bring
    forward before it blocks.
    """
    if not parent_is_hidden(main_window):
        return wx.MessageBox(message, title, style, main_window)
    dlg = wx.MessageDialog(main_window, message, title, style)
    try:
        bring_to_front_if_hidden(main_window, dlg, announce)
        return dlg.ShowModal()
    finally:
        dlg.Destroy()
