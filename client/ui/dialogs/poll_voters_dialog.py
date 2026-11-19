"""
WinZapp – Poll Votes Dialog
===========================
Opened with "View votes" (Alt+V) in the poll window: one row per person who
voted, ``"<person>: <option>"`` (several options joined with commas), in a plain
read-only list whose first row is focused and selected. The votes are asked of
WhatsApp Web in the background, so the window opens at once; the rows are
built by core.polls.voter_rows().
"""

import threading

import wx

from core.polls import hides_voter_names, poll_options, poll_content, voter_rows


class PollVotersDialog(wx.Dialog):
    def __init__(self, parent, main_window, msg: dict, name_of=None):
        self._mw = main_window
        self._name_of = name_of or main_window.chat_display_name
        self._msg = msg
        i18n = main_window.i18n
        super().__init__(
            parent, title=i18n.t("poll_voters_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(panel, label=i18n.t("poll_voters_list_label")),
                  0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._list = wx.ListCtrl(
            panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_NO_HEADER
        )
        self._list.InsertColumn(0, "", width=360)
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)
        self._status = wx.StaticText(panel, label=i18n.t("poll_voters_loading"))
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT, 8)
        sizer.Add(wx.Button(panel, wx.ID_CANCEL, label=i18n.t("close")),
                  0, wx.ALIGN_CENTER | wx.ALL, 8)
        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.SetMinSize((380, 320))
        self.Fit()
        self.CentreOnParent()
        self._list.SetFocus()
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            data = self._mw.fetch_poll_voters(self._msg)
        except Exception:
            data = {}
        wx.CallAfter(self._show, data)

    def _show(self, data: dict) -> None:
        if not self:  # closed while loading
            return
        i18n = self._mw.i18n
        if not data:
            self._status.SetLabel(i18n.t("poll_voters_failed"))
            self._mw.speak_output.output(i18n.t("poll_voters_failed"))
            return
        options = data.get("options") or poll_options(poll_content(self._msg.get("message") or {}))
        poll = poll_content(self._msg.get("message") or {})
        hidden = hides_voter_names(poll) or data.get("hideVoterNames") is True
        rows = voter_rows(i18n, data.get("voters") or [], options, self._name_of, hide_names=hidden)
        self._list.Freeze()
        try:
            self._list.DeleteAllItems()
            for row in rows:
                self._list.InsertItem(self._list.GetItemCount(), row)
        finally:
            self._list.Thaw()
        if rows:
            self._list.Focus(0)
            self._list.Select(0)
            self._status.SetLabel("")
        else:
            self._status.SetLabel(i18n.t("poll_voters_empty"))
            self._mw.speak_output.output(i18n.t("poll_voters_empty"))
