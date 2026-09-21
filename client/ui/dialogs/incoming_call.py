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
        is_video: bool = False,
        on_answer_without_video=None,
    ):
        i18n = parent.i18n
        # `parent` supplies i18n and owns the callbacks, but is deliberately
        # NOT the wx parent, and the style carries no wx.STAY_ON_TOP. Reported
        # live: while a call rang, Alt+Tab to WinZapp's main window always
        # landed back on this popup, so the user could not move between them
        # -- the same defect Gabriel fixed for the call window in 7f50df41, from
        # the same causes. A wx.Dialog with a top-level frame as its parent is
        # a Win32 OWNED window, which Windows keeps above its owner no matter
        # what focus code does; and STAY_ON_TOP (plus the HWND_TOPMOST that
        # _force_foreground() used to leave set) pinned it above every window
        # on the desktop for as long as the call rang. The popup still has to
        # appear over whatever app the user is in when the call arrives --
        # that is how a blind user learns of it -- so _force_foreground()
        # brings it to the top ONCE and then drops topmost, leaving an
        # ordinary window the user can Alt+Tab away from.
        #
        # Passing None is not enough on its own: wxWidgets gives a dialog
        # created with a NULL parent the application's top-level window as
        # parent anyway (on MSW, as the owner of its HWND), unless the style
        # says DIALOG_NO_PARENT. The popup is modeless, which is what that
        # style is meant for.
        super().__init__(
            None,
            title=i18n.t("incoming_call_popup_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.DIALOG_NO_PARENT,
        )
        self._on_answer_callback = on_answer
        self._on_reject_callback = on_reject
        self._on_stop_callback = on_stop
        self._on_closed_callback = on_closed
        self._on_answer_without_video_callback = on_answer_without_video
        self._is_video = is_video
        self._closing = False
        self._i18n = i18n

        panel = wx.Panel(self)
        content = wx.BoxSizer(wx.VERTICAL)

        self._message = wx.StaticText(panel, label=message)
        self._message.SetName(message)
        content.Add(self._message, 0, wx.EXPAND | wx.ALL, 12)

        buttons = wx.StdDialogButtonSizer()
        answer_key = (
            "incoming_call_answer_with_video_button"
            if is_video
            else "incoming_call_answer_button"
        )
        self._answer_button = wx.Button(panel, wx.ID_OK, label=i18n.t(answer_key))
        if is_video:
            self._answer_without_video_button = wx.Button(
                panel,
                wx.ID_ANY,
                label=i18n.t("incoming_call_answer_without_video_button"),
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
        button_order = [self._answer_button]
        if is_video:
            button_order.append(self._answer_without_video_button)
        button_order += [self._reject_button, self._silence_button, self._close_button]
        for button in button_order:
            buttons.AddButton(button)
        buttons.Realize()
        content.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        panel.SetSizer(content)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)
        self.SetMinSize((440, -1))
        self.CentreOnScreen()  # it has no parent (see __init__)

        self._answer_button.Enable(bool(can_answer))
        if is_video:
            self._answer_without_video_button.Enable(bool(can_answer))
        default_button = self._answer_button if can_answer else self._reject_button
        default_button.SetDefault()
        self._default_focus = default_button
        self._answer_button.Bind(wx.EVT_BUTTON, self._on_answer)
        if is_video:
            self._answer_without_video_button.Bind(
                wx.EVT_BUTTON, self._on_answer_without_video
            )
        self._reject_button.Bind(wx.EVT_BUTTON, self._on_reject)
        self._silence_button.Bind(wx.EVT_BUTTON, self._on_stop)
        self._close_button.Bind(wx.EVT_BUTTON, self._on_close)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    def refresh_labels(self, message: str | None = None):
        """Re-translate this modeless popup without closing the ringing call."""
        i18n = self._i18n
        self.SetTitle(i18n.t("incoming_call_popup_title"))
        if message is not None:
            self._message.SetLabel(message)
            self._message.SetName(message)
        self._answer_button.SetLabel(i18n.t("incoming_call_answer_button"))
        if self._is_video:
            self._answer_button.SetLabel(i18n.t("incoming_call_answer_with_video_button"))
        if self._is_video and hasattr(self, "_answer_without_video_button"):
            self._answer_without_video_button.SetLabel(
                i18n.t("incoming_call_answer_without_video_button")
            )
        self._reject_button.SetLabel(i18n.t("incoming_call_reject_button"))
        self._silence_button.SetLabel(i18n.t("incoming_call_silence_button"))
        self._close_button.SetLabel(i18n.t("incoming_call_close_button"))
        self.Layout()
        self.Fit()
        self.SetMinSize((440, -1))

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
                    # HWND_TOPMOST gets it above whatever app is in front right
                    # now; HWND_NOTOPMOST straight after keeps it at the top of
                    # the ordinary Z-order without pinning it there, so Alt+Tab
                    # to another window -- WinZapp's own included -- works.
                    swp_flags = 0x0001 | 0x0002 | 0x0040  # NOSIZE|NOMOVE|SHOWWINDOW
                    user32.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, swp_flags)
                    user32.SetWindowPos(hwnd, wintypes.HWND(-2), 0, 0, 0, 0, swp_flags)
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

    def _on_answer_without_video(self, _event):
        self._finish_with(self._on_answer_without_video_callback)

    def _on_reject(self, _event):
        self._finish_with(self._on_reject_callback)

    def _on_stop(self, _event):
        self._finish_with(self._on_stop_callback)

    def _on_close(self, _event):
        self._finish_with(self._on_closed_callback)
