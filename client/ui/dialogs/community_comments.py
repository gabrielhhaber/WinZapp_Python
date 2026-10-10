"""Native announcement thread, with explicit refresh and a single-attempt composer."""

import copy
import logging
from datetime import datetime

import wx

from core.community_comments import CommunityCommentsError, comment_author_label
from core.locale_format import get_datetime_format


class CommunityCommentsDialog(wx.Dialog):
    def __init__(self, parent, main_window, jid, message, name, on_count=None):
        i18n = main_window.i18n
        title = i18n.t("community_comments_window").format(name=name)
        super().__init__(parent, title=title, size=(760, 540),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._mw, self._jid = main_window, jid
        self._on_count = on_count
        self._message = copy.deepcopy(message)
        self._token = main_window.token
        self._closed = self._busy = self._loaded = self._uncertain = False
        self._rows = []
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=i18n.t("community_comments_list")), 0, wx.ALL, 8)
        self._list = wx.ListBox(self)
        self._list.SetName(i18n.t("community_comments_list").replace("&", ""))
        sizer.Add(self._list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self._status = wx.StaticText(self, label=i18n.t("loading"))
        sizer.Add(self._status, 0, wx.ALL, 8)
        sizer.Add(wx.StaticText(self, label=i18n.t("community_comments_write")), 0, wx.LEFT, 8)
        self._text = wx.TextCtrl(self, style=wx.TE_MULTILINE)
        self._text.SetName(i18n.t("community_comments_write").replace("&", ""))
        self._text.Bind(wx.EVT_TEXT, self._on_text)
        self._text.Bind(wx.EVT_KEY_DOWN, self._on_text_key)
        sizer.Add(self._text, 0, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._send = wx.Button(self, label=i18n.t("community_comments_send"))
        self._reload = wx.Button(self, label=i18n.t("community_comments_refresh"))
        close = wx.Button(self, wx.ID_CANCEL, i18n.t("close"))
        self._send.Bind(wx.EVT_BUTTON, self._on_send)
        self._reload.Bind(wx.EVT_BUTTON, self._on_refresh)
        close.Bind(wx.EVT_BUTTON, self._on_close)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        for button in (self._send, self._reload, close):
            buttons.Add(button, 0, wx.RIGHT, 8)
        sizer.Add(buttons, 0, wx.ALL, 8)
        self.SetSizer(sizer)
        self._update_buttons()
        self._list.SetFocus()
        wx.CallAfter(self._on_refresh, None)

    def close_comments(self):
        self._closed = True

    def _on_close(self, event):
        self.close_comments()
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self.Destroy()

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self.close_comments()
        event.Skip()

    def _active(self):
        return (not self._closed and not getattr(self._mw, "_shutting_down", False)
                and self._token == self._mw.token)

    def _update_buttons(self):
        active = self._active() and not self._busy
        # Keep a focused Refresh button enabled during a read; its handler
        # rejects overlap without Windows moving focus to another control.
        self._reload.Enable(self._active())
        self._send.Enable(active and self._loaded and not self._uncertain
                          and bool(self._text.GetValue().strip()))
        self._text.SetEditable(active)

    def _on_text(self, event):
        self._update_buttons()
        event.Skip()

    def _on_text_key(self, event):
        if event.ControlDown() and event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._on_send(None)
        else:
            event.Skip()

    def _on_refresh(self, event):
        if not self._active() or self._busy:
            return
        self._busy = True
        self._status.SetLabel(self._mw.i18n.t("loading"))
        self._update_buttons()

        def read():
            try:
                rows = self._mw.get_announcement_comments(self._jid, self._message)
                error = None
            except CommunityCommentsError as exc:
                rows, error = None, exc.code
            except Exception:
                rows, error = None, "unavailable"
            wx.CallAfter(self._finish_refresh, rows, error)

        try:
            self._mw._msg_bg_executor.submit(read)
        except Exception:
            self._finish_refresh(None, "unavailable")

    def _row_label(self, row):
        i18n = self._mw.i18n
        author = comment_author_label(row, self._mw, self._jid)
        try:
            stamp = datetime.fromtimestamp(row["timestamp"]).strftime(
                get_datetime_format(i18n.t("datetime_fmt")))
        except (ValueError, OSError, OverflowError):
            stamp = ""
        body = (i18n.t("community_comments_deleted") if row["type"] == "revoked" else
                i18n.t("community_comments_encrypted") if row["type"] == "ciphertext" else row["body"])
        return f"{author}, {stamp}: {body}".replace("\r", " ").replace("\n", " ")

    def _finish_refresh(self, rows, error):
        if not self._active():
            return
        self._busy = False
        i18n = self._mw.i18n
        if rows is None:
            # Never log the exception, parent id, author or message content.
            code = "not_announcement" if error == "not_announcement" else "unavailable"
            logging.warning("[community_comments] read_failed code=%s", code)
            if error == "not_announcement":
                self._loaded = False
            text = (i18n.t("community_comments_not_announcement") if error == "not_announcement"
                    else i18n.t("community_comments_failed"))
        else:
            old = self._list.GetSelection()
            selected = self._rows[old]["id"] if 0 <= old < len(self._rows) else None
            labels = [self._row_label(row) for row in rows]
            if labels != list(self._list.GetItems()) or [r["id"] for r in rows] != [r["id"] for r in self._rows]:
                self._list.Freeze()
                try:
                    self._list.SetItems(labels)
                    ids = [row["id"] for row in rows]
                    self._list.SetSelection(ids.index(selected) if selected in ids else
                                            (min(max(old, 0), len(ids) - 1) if ids else wx.NOT_FOUND))
                finally:
                    self._list.Thaw()
            self._rows, self._loaded, self._uncertain = rows, True, False
            if getattr(self, "_on_count", None):
                self._on_count(len(rows))
            text = (i18n.t("community_comments_count").format(count=len(rows)) if rows else
                    i18n.t("community_comments_empty"))
        self._status.SetLabel(text)
        self._update_buttons()
        self._mw.speak_output.output(text)

    def _on_send(self, event):
        if not self._active() or self._busy or not self._loaded or self._uncertain:
            return
        text = self._text.GetValue()
        if not text.strip():
            return
        self._busy = True
        self._update_buttons()
        self._status.SetLabel(self._mw.i18n.t("community_comments_sending"))

        def send():
            try:
                confirmed = self._mw.send_announcement_comment(self._jid, self._message, text) is True
            except Exception:
                confirmed = False
            wx.CallAfter(self._finish_send, confirmed)

        try:
            self._mw._msg_bg_executor.submit(send)
        except Exception:
            self._finish_send(False)

    def _finish_send(self, confirmed):
        if not self._active():
            return
        self._busy = False
        self._uncertain = not confirmed
        if not confirmed:
            logging.warning("[community_comments] send_failed code=unconfirmed")
        if confirmed:
            self._text.ChangeValue("")
        text = self._mw.i18n.t("community_comments_sent") if confirmed else self._mw.i18n.t("community_comments_unconfirmed")
        self._status.SetLabel(text)
        self._update_buttons()
        self._mw.speak_output.output(text)
        self._text.SetFocus()
        if confirmed:
            self._on_refresh(None)
