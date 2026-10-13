"""Native, keyboard-accessible group invitation link and QR dialog."""

import io
import threading
import wx
from core.group_invitation import connection_identity, read_invitation
from ui.accessible import split_mnemonic


def add_invitation_button(owner, page, sizer):
    button = wx.Button(page, label=owner._i18n.t("group_invitation_title"))
    # Metadata determines whether the child dialog exposes link reset.
    # Do not snapshot the initial (unknown) admin state before it arrives.
    button.Enable(False)
    def open_dialog(event):
        if not button.IsEnabled():
            return
        dialog = GroupInvitationDialog(owner, owner._mw, owner._jid, owner._user_is_admin)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
    button.Bind(wx.EVT_BUTTON, open_dialog)
    sizer.Add(button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
    return button


class GroupInvitationDialog(wx.Dialog):
    def __init__(self, parent, main_window, jid, is_admin=False):
        self._mw = main_window
        self._i18n = main_window.i18n
        self._jid = jid
        self._connection = connection_identity(main_window)
        self._closed = threading.Event()
        self._link = ""
        self._png = b""
        self._is_admin = is_admin
        # The same words as the button that opens this dialog, whose label
        # carries a mnemonic marker a title would show as a literal "&".
        super().__init__(parent, title=split_mnemonic(self._i18n.t("group_invitation_title"))[0],
                         size=(520, 640), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        outer = wx.BoxSizer(wx.VERTICAL)
        self._status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self._status.SetName(self._i18n.t("group_invitation_status"))
        self._status.SetMinSize((-1, 72))
        outer.Add(self._status, 0, wx.EXPAND | wx.ALL, 8)
        outer.Add(wx.StaticText(self, label=self._i18n.t("group_invitation_link")), 0, wx.LEFT, 8)
        self._link_ctrl = wx.TextCtrl(self, style=wx.TE_READONLY)
        outer.Add(self._link_ctrl, 0, wx.EXPAND | wx.ALL, 8)
        self._qr = wx.StaticBitmap(self)
        self._qr.SetName(self._i18n.t("group_invitation_qr"))
        outer.Add(self._qr, 1, wx.ALIGN_CENTER | wx.ALL, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self._copy = wx.Button(self, label=self._i18n.t("group_invitation_copy_link"))
        self._save = wx.Button(self, label=self._i18n.t("group_invitation_save_qr"))
        self._refresh = wx.Button(self, label=self._i18n.t("group_invitation_refresh"))
        self._reset = wx.Button(self, label=self._i18n.t("group_invitation_reset"))
        self._reset.Show(is_admin)
        for button, handler in ((self._copy, self._on_copy), (self._save, self._on_save),
                                (self._refresh, self._on_refresh), (self._reset, self._on_reset)):
            buttons.Add(button, 0, wx.ALL, 4)
            button.Bind(wx.EVT_BUTTON, handler)
        outer.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        outer.Add(self.CreateButtonSizer(wx.CLOSE), 0, wx.EXPAND | wx.ALL, 8)
        # The stock button says "Close" in English whatever the app's language.
        close = self.FindWindowById(wx.ID_CLOSE)
        if close is not None:
            close.SetLabel(self._i18n.t("close"))
        self.SetSizer(outer)
        self.Bind(wx.EVT_BUTTON, self._on_close, id=wx.ID_CLOSE)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._load()
        self._link_ctrl.SetFocus()

    def _on_close(self, event):
        self._closed.set()
        self.EndModal(wx.ID_CLOSE)

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self._closed.set()
        event.Skip()

    def _clear(self):
        self._link, self._png = "", b""
        self._link_ctrl.ChangeValue("")
        self._qr.SetBitmap(wx.NullBitmap)

    def _busy(self, busy):
        self._busy_loading = busy
        self._copy.Enable(not busy and bool(self._link))
        self._save.Enable(not busy and bool(self._png))
        self._refresh.Enable(not busy)
        self._reset.Enable(not busy and self._is_admin)

    def _load(self, reset=False):
        if getattr(self, "_busy_loading", False) or self._closed.is_set():
            return
        self._clear()
        self._busy(True)
        self._status.ChangeValue(self._i18n.t("loading"))
        def run():
            try:
                result = read_invitation(self._connection, self._jid, reset)
            except Exception:
                result = None
            if not self._closed.is_set():
                wx.CallAfter(self._finish, result, reset)
        threading.Thread(target=run, daemon=True).start()

    def _finish(self, result, reset):
        if self._closed.is_set():
            return
        if getattr(self._mw, "_shutting_down", False) or connection_identity(self._mw) != self._connection:
            self._clear()
            self._status.ChangeValue(self._i18n.t("group_invitation_unavailable"))
            return
        if result:
            link, png = result
            try:
                image = wx.Image(io.BytesIO(png), wx.BITMAP_TYPE_PNG)
                if not image.IsOk() or image.GetWidth() > 1024 or image.GetHeight() > 1024:
                    raise ValueError("invalid_qr")
                bitmap = wx.Bitmap(image)
            except Exception:
                result = None
            else:
                self._link, self._png = link, png
                self._link_ctrl.ChangeValue(link)
                self._qr.SetBitmap(bitmap)
        key = "group_invitation_ready" if result else (
            "group_invitation_reset_unconfirmed" if reset else "group_invitation_unavailable")
        self._status.ChangeValue(self._i18n.t(key))
        self._busy(False)
        self.Layout()
        self._mw.speak_output.output(self._i18n.t(key))

    def _on_refresh(self, event):
        self._load()

    def _on_reset(self, event):
        if self._busy_loading or not self._is_admin:
            return
        if wx.MessageBox(self._i18n.t("group_invitation_reset_confirm"),
                         split_mnemonic(self._i18n.t("group_invitation_reset"))[0],
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) == wx.YES:
            self._load(reset=True)

    def _on_copy(self, event):
        if self._link and wx.TheClipboard.Open():
            try:
                if wx.TheClipboard.SetData(wx.TextDataObject(self._link)):
                    self._mw.speak_output.output(self._i18n.t("group_invitation_copied"))
            finally:
                wx.TheClipboard.Close()

    def _on_save(self, event):
        if not self._png:
            return
        dialog = wx.FileDialog(self, self._i18n.t("save_as"), defaultFile="group-invitation.png",
                               wildcard=self._i18n.t("group_invitation_png"),
                               style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                with open(dialog.GetPath(), "wb") as target:
                    target.write(self._png)
        except OSError:
            wx.MessageBox(self._i18n.t("group_invitation_save_failed"), self._i18n.t("error"),
                          wx.OK | wx.ICON_ERROR, self)
        finally:
            dialog.Destroy()
