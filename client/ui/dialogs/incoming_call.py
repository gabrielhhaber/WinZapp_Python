"""Accessible modeless dialog for an incoming WhatsApp call."""

import ctypes
import sys
from ctypes import wintypes

import wx


class IncomingCallDialog(wx.Dialog):
    """Expose answer/reject/silence actions without blocking call events."""

    def __init__(
        self,
        parent,
        message: str,
        on_answer,
        on_reject,
        on_stop,
        on_closed,
        *,
        can_answer: bool = True,
    ):
        i18n = parent.i18n
        super().__init__(
            parent,
            title=i18n.t("incoming_call_popup_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP,
        )
        self._on_answer_callback = on_answer
        self._on_reject_callback = on_reject
        self._on_stop_callback = on_stop
        self._on_closed_callback = on_closed
        self._closing = False

        panel = wx.Panel(self)
        content = wx.BoxSizer(wx.VERTICAL)

        self._message = wx.StaticText(panel, label=message)
        self._message.SetName(message)
        content.Add(self._message, 0, wx.EXPAND | wx.ALL, 12)

        buttons = wx.StdDialogButtonSizer()
        self._answer_button = wx.Button(
            panel, wx.ID_OK, label=i18n.t("incoming_call_answer_button")
        )
        self._reject_button = wx.Button(
            panel, wx.ID_ANY, label=i18n.t("incoming_call_reject_button")
        )
        self._silence_button = wx.Button(
            panel, wx.ID_ANY, label=i18n.t("incoming_call_silence_button")
        )
        self._close_button = wx.Button(
            panel, wx.ID_CANCEL, label=i18n.t("incoming_call_close_button")
        )
        for button in (
            self._answer_button,
            self._reject_button,
            self._silence_button,
            self._close_button,
        ):
            buttons.AddButton(button)
        buttons.Realize()
        content.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        panel.SetSizer(content)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)
        self.SetMinSize((440, -1))
        self.CentreOnParent()

        self._answer_button.Enable(bool(can_answer))
        default_button = self._answer_button if can_answer else self._reject_button
        default_button.SetDefault()
        self._default_focus = default_button
        self._answer_button.Bind(wx.EVT_BUTTON, self._on_answer)
        self._reject_button.Bind(wx.EVT_BUTTON, self._on_reject)
        self._silence_button.Bind(wx.EVT_BUTTON, self._on_stop)
        self._close_button.Bind(wx.EVT_BUTTON, self._on_close)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def show_accessibly(self):
        """Display over the current app and focus the primary call action."""
        self.Show()
        self._force_foreground()
        self._foreground_retry = wx.CallLater(150, self._force_foreground)

    def _force_foreground(self):
        """Use Win32 focus attachment when ordinary Raise() is insufficient."""
        try:
            self.Raise()
            if sys.platform == "win32":
                user32 = ctypes.windll.user32
                user32.GetForegroundWindow.restype = wintypes.HWND
                user32.GetWindowThreadProcessId.argtypes = [
                    wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
                ]
                user32.GetWindowThreadProcessId.restype = wintypes.DWORD
                user32.AttachThreadInput.argtypes = [
                    wintypes.DWORD, wintypes.DWORD, wintypes.BOOL
                ]
                user32.SetWindowPos.argtypes = [
                    wintypes.HWND,
                    wintypes.HWND,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.UINT,
                ]
                user32.BringWindowToTop.argtypes = [wintypes.HWND]
                user32.SetForegroundWindow.argtypes = [wintypes.HWND]
                hwnd = wintypes.HWND(int(self.GetHandle()))
                foreground = user32.GetForegroundWindow()
                current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
                foreground_thread = (
                    user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
                )
                attached = bool(
                    foreground_thread
                    and foreground_thread != current_thread
                    and user32.AttachThreadInput(current_thread, foreground_thread, True)
                )
                try:
                    user32.SetWindowPos(
                        hwnd,
                        wintypes.HWND(-1),
                        0,
                        0,
                        0,
                        0,
                        0x0001 | 0x0002 | 0x0040,
                    )
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                finally:
                    if attached:
                        user32.AttachThreadInput(current_thread, foreground_thread, False)
            self._default_focus.SetFocus()
        except Exception:
            try:
                self.Raise()
                self._default_focus.SetFocus()
            except Exception:
                pass

    def close_from_call_lifecycle(self):
        """Close without reporting a user dismissal back to the owner."""
        if self._closing:
            return
        self._closing = True
        self.Destroy()

    def _finish_with(self, callback):
        if self._closing:
            return
        self._closing = True
        callback()
        self.Destroy()

    def _on_answer(self, _event):
        self._finish_with(self._on_answer_callback)

    def _on_reject(self, _event):
        self._finish_with(self._on_reject_callback)

    def _on_stop(self, _event):
        self._finish_with(self._on_stop_callback)

    def _on_close(self, _event):
        self._finish_with(self._on_closed_callback)
