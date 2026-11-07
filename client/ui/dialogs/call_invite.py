"""Add people to the call in progress: a group call offers the group's members
who are not in it, a private call the saved contacts.

Opened from the call's participant list (CallParticipantsDialog) while this
device is in the call. The logic lives in core/call_invites.py; this is
the plain-controls window around it. Every request runs off the wx thread and
a late answer never touches a closed dialog.
"""
import logging
import threading

import wx

from core.call_invites import (candidates, one_per_person, parse_results, room_left,
                                saved_contact_jids, summary)
from core.call_participants import parse_participants
from ui.dialogs.call_invite_marks import CallInviteMarksMixin


def failure_line(i18n, reason) -> str:
    """The line spoken when the request itself failed: what went wrong."""
    return i18n.t("call_invite_failed_reason").format(reason=reason)


class CallInviteDialog(CallInviteMarksMixin, wx.Dialog):
    def __init__(self, parent, main_window, call_id, chat_jid, allowed=lambda: True):
        self._mw, self._call_id, self._chat = main_window, call_id, chat_jid
        # the participants dialog's check: a chat locked meanwhile shows nobody
        self._allowed = allowed
        self._group = chat_jid.endswith("@g.us")
        self._alive, self._busy, self._rows, self._connected = True, False, [], 0
        i18n = main_window.i18n
        super().__init__(parent, title=i18n.t("call_invite_title"),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        title = i18n.t("call_invite_list") if self._group else i18n.t("call_invite_list_contacts")
        label = wx.StaticText(panel, label=title)
        sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._list = wx.ListBox(panel, choices=[], style=wx.LB_SINGLE, name=title)
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)
        self._status = wx.StaticText(panel, label=i18n.t("loading"))
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._add_btn = wx.Button(panel, wx.ID_OK, label=i18n.t("call_invite_add_button"))
        self._add_btn.Bind(wx.EVT_BUTTON, self._on_add)
        self._add_btn.Disable()
        buttons.Add(self._add_btn, 0, wx.ALL, 8)
        cancel = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
        cancel.Bind(wx.EVT_BUTTON, self._close)
        buttons.Add(cancel, 0, wx.ALL, 8)
        sizer.Add(buttons, 0, wx.ALIGN_CENTER)
        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.SetMinSize((450, 360))
        self.Fit()
        self.CentreOnParent()
        self.Bind(wx.EVT_CLOSE, self._close)
        self._init_invite_marks()
        self._list.SetFocus()
        threading.Thread(target=self._load, daemon=True, name="call-invite-load").start()

    # ------------------------------------------------------------- loading
    def _load(self):
        mw = self._mw
        rows, connected, failed = [], 0, False
        try:
            if self._group:
                info = mw.get_group_info_recent(self._chat) or {}
                members = [p.get("id") for p in info.get("participants") or () if isinstance(p, dict)]
            else:
                members = saved_contact_jids(dict(getattr(mw, "contacts", None) or {}))
            response = mw._raise_for_call_response(mw._post_call_control(
                "participants", {"callId": self._call_id}, timeout=20), "participants")
            present = parse_participants(response.json(), self._call_id) or []
            connected = len(present)
            members = one_per_person(members, dict(getattr(mw, "_lid_to_phone", None) or {}))
            rows = candidates(
                members, [p["jid"] for p in present if not p["isSelf"]], mw._is_self_jid,
                mw._chat_jids_equivalent,
                lambda jid: mw.chat_display_name({"remoteJid": jid}))
        except Exception:
            failed = True
            logging.warning("[call] members to add unavailable")
        wx.CallAfter(self._show_rows, rows, connected, failed)

    def _show_rows(self, rows, connected, failed):
        if not self._alive:
            return
        if not self._allowed():
            rows, failed = [], True
        i18n = self._mw.i18n
        self._rows, self._connected = rows, connected
        # a mark of someone no longer offered is forgotten
        self._marks.intersection_update(jid for jid, _name in rows)
        self._labels = self._invite_labels()
        self._list.Freeze()
        try:
            self._list.Set(self._labels)
        finally:
            self._list.Thaw()
        if failed:
            status = i18n.t("call_invite_failed")
        elif not rows:
            status = i18n.t("call_invite_none") if self._group else i18n.t("call_invite_none_contacts")
        else:
            status = i18n.t("call_invite_count").format(count=len(rows))
        self._status.SetLabel(status)
        self._add_btn.Enable(bool(rows))
        if rows:
            self._list.SetSelection(0)
        else:
            self._mw.output(status)

    # ------------------------------------------------------------- adding
    def _on_add(self, _event=None):
        if self._busy or not self._alive:
            return
        i18n = self._mw.i18n
        if not self._allowed():
            self._mw.output(i18n.t("call_invite_failed"), interrupt=True)
            return
        picked = [row for row in self._rows if row[0] in self._marks]
        if not picked:
            self._mw.output(i18n.t("call_invite_nothing_selected"), interrupt=True)
            return
        room = room_left(self._connected)
        if len(picked) > room:
            self._mw.output(i18n.t("call_invite_too_many").format(count=room), interrupt=True)
            return
        # The busy flag refuses a second press; the button stays enabled, or
        # Windows would move the focus to Cancel and the next Space would
        # close the window (call_participants.py does the same).
        self._busy = True
        sending = i18n.t("call_invite_sending")
        self._status.SetLabel(sending)
        self._mw.output(sending, interrupt=True)
        jids = [jid for jid, _name in picked]
        names = {jid: name for jid, name in picked}

        def worker():
            spoken, error, invited = None, None, False
            try:
                response = self._mw._raise_for_call_response(self._mw._post_call_control(
                    "add-participants", {"callId": self._call_id, "participants": jids},
                    timeout=60), "add-participants")
                results = parse_results(response.json(), self._call_id, jids)
                invited = any(status in ("invited", "rang") for status in results.values())
                spoken = summary(results, names, i18n)
            except Exception as exc:
                error = exc
                logging.warning("[call] adding participants failed")
            wx.CallAfter(self._finish_add, spoken, error, invited)

        threading.Thread(target=worker, daemon=True, name="call-invite-send").start()

    def _finish_add(self, spoken, error=None, invited=True):
        if getattr(self._mw, "_shutting_down", False):
            return
        i18n = self._mw.i18n
        if spoken is None:
            # What went wrong, not only that it did: the screen reader user has
            # no other way to tell a refusal from a call that ended meanwhile.
            spoken = failure_line(i18n, self._mw._call_error_text(error))
        if not self._alive:
            self._mw.output(spoken, interrupt=True)
            return
        self._busy = False
        if not invited:
            # nobody got through: stay, so the choice can be changed and retried
            self._status.SetLabel(spoken)
            self._mw.output(spoken, interrupt=True)
            return
        self._status.SetLabel(spoken)
        self._close()
        # After the dialog is gone: closing moves the focus back to the
        # participants window, and the reader's focus announcement would cut
        # the result off if it were spoken first.
        wx.CallLater(400, self._mw.output, spoken, True)

    def _close(self, _event=None):
        self._alive = False
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)
        else:
            self.Destroy()
