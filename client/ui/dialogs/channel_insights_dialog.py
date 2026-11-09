"""The statistics of a channel the account administers (core/channel_insights.py).

"Statistics..." in the Channels window: one read-only text, one fact per
line (reach, followers, net follows, follows and unfollows over 30 days, reach
by followers and non-followers, by country), read off the main thread.
"""
import datetime

import wx

from core import channels as ch
from core.channel_insights import MIN_FOLLOWERS, fetch_insights, insight_lines
from ui.dialogs.background_work import BackgroundWork
from ui.dialogs.labelled_control import add_labelled


def _date(seconds):
    try:
        return datetime.datetime.fromtimestamp(int(seconds)).strftime("%x %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


class ChannelInsightsDialog(BackgroundWork, wx.Dialog):
    def __init__(self, parent, main_window, channel):
        self._init_background(main_window)
        self._channel = channel
        i18n = self._i18n
        super().__init__(parent, title=i18n.t("channel_insights_title").format(
            name=channel.name or i18n.t("channels_unnamed")), size=(520, 440),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._text = add_labelled(self, sizer, i18n.t("channel_insights_text"), lambda: wx.TextCtrl(
            self, value=i18n.t("loading"), style=wx.TE_MULTILINE | wx.TE_READONLY), 1)
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT, 8)
        close = wx.Button(self, wx.ID_CANCEL, label=i18n.t("close"))
        close.Bind(wx.EVT_BUTTON, self._on_close)
        sizer.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._text.SetFocus()
        self.CentreOnParent()
        wx.CallAfter(self._load)

    def _load(self):
        if not self._current():
            return
        connection, jid = self._connection, self._channel.jid
        self._run_async(lambda: fetch_insights(ch._post, connection, jid), self._finish)

    def _finish(self, insights):
        if not self._current():
            return
        if insights is None:
            followers = self._channel.followers
            key = ("channel_insights_too_small" if followers is not None
                   and followers < MIN_FOLLOWERS else "channel_insights_unavailable")
            text = self._i18n.t(key)
        else:
            text = "\n".join(insight_lines(self._i18n, insights, _date))
        self._text.ChangeValue(text)
        self._text.SetInsertionPoint(0)
        self._announce(text.splitlines()[0] if text else "")


def show_channel_insights(mw, parent, channel):
    dialog = ChannelInsightsDialog(parent, mw, channel)
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()
