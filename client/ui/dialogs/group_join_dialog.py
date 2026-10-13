"""Preview a pasted group invitation before the user explicitly joins."""

import threading
import wx
from core.group_invitation import connection_identity
from core.group_join import invitation_code, preview_group, join_group
from ui.accessible import split_mnemonic


def join_group_by_link(mw, parent, link=""):
    """Show the join dialog and open the group once membership is confirmed.

    *link* fills the field and starts the preview, for a link the user
    activated in a message or that Windows handed over. Nothing is joined
    until the user presses the join button. True when the group was joined.
    """
    dialog = GroupJoinDialog(parent, mw, link)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return False
        preview = dialog.result
    finally:
        dialog.Destroy()
    open_group_chat(mw, preview.jid, preview.name)
    return True


def open_group_chat(mw, jid, name):
    """Go to a group the account is in, creating its chat when it has none
    yet (a group just joined, or one with no message so far)."""
    chat = mw.chats.get(jid)
    if chat is None:
        chat = {"remoteJid": jid, "name": name or mw.i18n.t("unknown_group")}
        mw.chats[jid] = chat
        mw._schedule_set_chats()
    wx.CallAfter(mw.conversations_panel.navigate_to_conversation, chat)


def open_join_dialog(owner, event=None):
    """New conversation's "Join group by link": that dialog closes once the
    group is joined."""
    if join_group_by_link(owner._mw, owner):
        owner.EndModal(wx.ID_OK)


class GroupJoinDialog(wx.Dialog):
    def __init__(self, parent, main_window, link=""):
        self._mw = main_window
        self._i18n = main_window.i18n
        self._connection = connection_identity(main_window)
        self._closed = threading.Event()
        self._preview = None
        self._code = ""
        self._pending = ""
        self._attempted = False
        self.result = None
        super().__init__(parent, title=self._i18n.t("group_join_title"),
                         size=(520, 500), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=self._i18n.t("group_invitation_link")), 0, wx.ALL, 8)
        self._input = wx.TextCtrl(self)
        self._input.SetName(split_mnemonic(self._i18n.t("group_invitation_link"))[0])
        self._input.Bind(wx.EVT_TEXT, self._on_change)
        sizer.Add(self._input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self._read = wx.Button(self, label=self._i18n.t("group_join_preview"))
        self._read.Bind(wx.EVT_BUTTON, self._on_preview)
        sizer.Add(self._read, 0, wx.ALL, 8)
        self._details = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self._details.SetName(split_mnemonic(self._i18n.t("group_join_preview"))[0])
        sizer.Add(self._details, 1, wx.EXPAND | wx.ALL, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self._join = wx.Button(self, label=self._i18n.t("group_join_action"))
        self._join.Bind(wx.EVT_BUTTON, self._on_join)
        self._join.Enable(False)
        buttons.Add(self._join, 0, wx.ALL, 8)
        close = wx.Button(self, wx.ID_CANCEL, label=self._i18n.t("close"))
        close.Bind(wx.EVT_BUTTON, self._on_close)
        buttons.Add(close, 0, wx.ALL, 8)
        sizer.Add(buttons, 0, wx.EXPAND)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._input.SetFocus()
        self.CentreOnParent()
        if link:
            # ChangeValue: the field is being filled for the user, not edited
            # by them, so it must not clear the preview it is about to ask for.
            self._input.ChangeValue(link)
            wx.CallAfter(self._on_preview, None)

    def _on_close(self, event):
        self._closed.set()
        self.EndModal(wx.ID_CANCEL)

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self._closed.set()
        event.Skip()

    def _current(self):
        return (not self._closed.is_set() and not getattr(self._mw, "_shutting_down", False)
                and connection_identity(self._mw) == self._connection)

    def _controls(self):
        editable = not self._pending and not self._attempted and self._current()
        self._input.Enable(editable)
        self._read.Enable(editable)
        self._join.Enable(editable and self._preview is not None)

    def _announce(self, key):
        text = self._i18n.t(key)
        self._details.ChangeValue(text)
        self._mw.speak_output.output(text)

    def _on_change(self, event):
        self._preview = None
        self._code = ""
        self._details.ChangeValue("")
        self._join.Enable(False)

    def _on_preview(self, event):
        if self._pending or self._attempted or not self._current():
            return
        self._preview = None
        self._code = invitation_code(self._input.GetValue())
        if not self._code:
            self._controls()
            self._announce("group_join_invalid_link")
            self._input.SetFocus()
            return
        self._start("preview")

    def _on_join(self, event):
        if self._pending or self._attempted or not self._preview or not self._current():
            return
        if self._code != invitation_code(self._input.GetValue()):
            self._on_change(None)
            return
        # Even a lost response may mean membership or an approval request was
        # created. Keep this dialog terminal after one join; no automatic retry.
        self._attempted = True
        self._start("join")

    def _start(self, operation):
        self._pending = operation
        self._controls()
        self._details.ChangeValue(self._i18n.t("loading"))
        code, preview = self._code, self._preview
        def run():
            try:
                result = (preview_group(self._connection, code) if operation == "preview"
                          else join_group(self._connection, code, preview.jid))
            except Exception:
                result = None
            if not self._closed.is_set():
                wx.CallAfter(self._finish, operation, result)
        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            self._finish(operation, None)

    def _finish(self, operation, result):
        if self._closed.is_set():
            return
        self._pending = ""
        if not self._current():
            self._preview = None
            self._controls()
            self._details.ChangeValue(self._i18n.t("group_join_preview_failed"))
            return
        if operation == "preview":
            self._preview = result
            self._controls()
            if result is None:
                self._announce("group_join_preview_failed")
                return
            approval = {True: "group_join_approval_required", False: "group_join_approval_not_required",
                        None: "group_join_approval_unknown"}[result.approval]
            lines = [result.name or self._i18n.t("unknown_group"), result.description]
            if result.size is not None:
                lines.append(self._i18n.t("group_join_member_count").format(count=result.size))
            lines.append(self._i18n.t(approval))
            self._details.ChangeValue("\n\n".join(line for line in lines if line))
            self._mw.speak_output.output(self._i18n.t("group_join_preview_ready"))
            self._details.SetFocus()
        else:
            self._controls()
            if result is None:
                self._announce("group_join_unconfirmed")
            elif result is True:
                self._announce("group_join_pending")
            else:
                self.result = self._preview
                self._mw.speak_output.output(self._i18n.t("group_join_success"))
                self._closed.set()
                self.EndModal(wx.ID_OK)
