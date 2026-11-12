"""A native, refreshing participant list; late replies never repaint a closed dialog."""
import logging
import threading

import wx

from core.call_log import list_update_plan
from core.call_participants import connected_only, parse_participants, participant_rows
from core.call_roster import left_of, note_seen, user_of
from ui.shortcut_bindings import make_shortcut_table


class ParticipantListMixin:
    def _refresh_participants(self, _event=None, *, announce=False):
        if not self._alive:
            return
        if self._inflight is not None:
            # A press during the timed refresh rides on it: its result is
            # spoken, or the press would be answered by silence.
            if announce or _event is not None:
                self._announce_pending = "all" if _event is not None else True
            return
        if self._timer is not None:
            self._timer.Stop()
            self._timer = None
        token = object()
        # The in-flight guard above is what refuses a second request. Never
        # disable the Refresh button: the timed refresh would take focus off
        # it, and Windows hands focus to Close, so the next Space closes.
        self._inflight = token
        # F5 says the result whatever it is (the only way to refresh now that
        # there is no Refresh button); opening the window says only a failure
        # or an empty list, the rows themselves being read with the focus
        announce = "all" if _event is not None else announce
        if not self._allowed():
            self._finish_participants(token, None, True, announce)
            return

        def worker():
            data, failed = None, False
            try:
                response = self._mw._raise_for_call_response(self._mw._post_call_control(
                    "participants", {"callId": self._call_id}, timeout=20), "participants")
                data = parse_participants(response.json(), self._call_id)
            except Exception:
                failed = True
                logging.warning("[call] participant list unavailable")
            wx.CallAfter(self._finish_participants, token, data, failed, announce)

        threading.Thread(target=worker, daemon=True, name="call-participants").start()

    def _finish_participants(self, token, data, failed, announce=False):
        if not self._alive or self._inflight is not token or getattr(self._mw, "_shutting_down", False):
            return
        self._inflight = None
        pending = getattr(self, "_announce_pending", False)
        announce = "all" if "all" in (announce, pending) else (announce or pending)
        self._announce_pending = False
        if not self._allowed():
            data, failed = None, True
        left = []
        if data is not None and not failed:
            # who was here and left stays listed, as the phone lists them
            seen = self._mw.__dict__.setdefault("_call_seen", {})
            mine = {user_of(j) for j in (getattr(self._mw, "my_jid", ""),
                                         getattr(self._mw, "my_lid", ""))} - {""}
            note_seen(seen, self._call_id,
                      [(p["jid"], p["name"]) for p in data if not p["isSelf"]], mine)
            left = left_of(seen, self._call_id, [p["jid"] for p in connected_only(data)])
        rows = [] if data is None or failed else participant_rows(data, self._mw.i18n,
            lambda jid, hint: self._mw.chat_display_name({"remoteJid": jid, "name": hint}), left)
        self._apply_participant_rows(rows)
        i18n = self._mw.i18n
        if failed:
            status = i18n.t("call_participants_failed")
        elif data is None:
            status = i18n.t("call_participants_ended")
        elif not rows:
            status = i18n.t("call_participants_empty")
        elif len(rows) > len(connected_only(data)):
            connected = len(connected_only(data))
            status = i18n.t("call_participants_count_with_left").format(
                count=connected, left=len(rows) - connected)
        else:
            status = i18n.t("call_participants_count").format(count=len(rows))
        if self._status.GetLabel() != status:
            self._status.SetLabel(status)
        if announce == "all" or (announce and (failed or not rows)):
            self._mw.output(status)
        if data is not None or failed:
            self._timer = wx.CallLater(10_000, self._refresh_participants)

    def _apply_participant_rows(self, rows):
        action, changes = list_update_plan(self._shown, rows)
        if action == "none":
            return
        index = self._list.GetFocusedItem()
        keep = self._shown[index][0] if 0 <= index < len(self._shown) else ""
        self._shown = list(rows)
        self._list.Freeze()
        try:
            if action == "set":
                for index, text in changes:
                    self._list.SetItemText(index, text)
            else:
                self._list.DeleteAllItems()
                for _key, text in rows:
                    self._list.Append((text,))
                if rows:
                    target = next((i for i, (key, _) in enumerate(rows) if key == keep), 0)
                    self._list.Focus(target)
                    self._list.Select(target)
        finally:
            self._list.Thaw()

    def _dispose_participants(self):
        self._alive = False
        if self._timer is not None:
            self._timer.Stop()
            self._timer = None


class CallParticipantsDialog(ParticipantListMixin, wx.Dialog):
    def __init__(self, parent, main_window, call_id, name, allowed, invite_chat=""):
        self._mw, self._call_id, self._allowed = main_window, call_id, allowed
        self._invite_chat = invite_chat
        self._alive, self._inflight, self._timer, self._shown = True, None, None, []
        i18n = main_window.i18n
        super().__init__(parent, title=i18n.t("call_participants_title").format(name=name),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        label = i18n.t("call_participants_list")
        sizer.Add(wx.StaticText(panel, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_NO_HEADER)
        self._list.InsertColumn(0, label, width=400)
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)
        self._status = wx.StaticText(panel, label=i18n.t("loading"))
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        if invite_chat:
            # Only for the call this device is in (show_call_participants)
            add = wx.Button(panel, label=i18n.t("call_participants_add"))
            add.Bind(wx.EVT_BUTTON, self._add_participants)
            buttons.Add(add, 0, wx.ALL, 8)
        close = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("close"))
        close.Bind(wx.EVT_BUTTON, self._close_participants)
        buttons.Add(close, 0, wx.ALL, 8)
        sizer.Add(buttons, 0, wx.ALIGN_CENTER)
        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        # No Refresh button: the list follows who joins and leaves
        # (MainWindow.on_call_participants); F5 (Settings > Shortcuts,
        # "call_participants.ID_F5") reads it again and says how many there are.
        self.ID_F5 = wx.NewIdRef()
        self.SetAcceleratorTable(make_shortcut_table(self, "call_participants", [
            (wx.ACCEL_NORMAL, wx.WXK_F5, self.ID_F5)]))
        self.Bind(wx.EVT_MENU, lambda event: self._refresh_participants(event), id=self.ID_F5)
        self.SetMinSize((450, 330))
        self.Fit()
        self.CentreOnParent()
        self.Bind(wx.EVT_CLOSE, self._close_participants)
        self._list.SetFocus()
        self._refresh_participants(announce=True)

    def _add_participants(self, _event=None):
        from ui.dialogs.call_invite import CallInviteDialog
        dialog = CallInviteDialog(self, self._mw, self._call_id, self._invite_chat, self._allowed)
        try:
            dialog.ShowModal()
        finally:
            if dialog:
                dialog.Destroy()
        if self._alive:
            self._refresh_participants()

    def _close_participants(self, _event):
        self._dispose_participants()
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self.Destroy()
