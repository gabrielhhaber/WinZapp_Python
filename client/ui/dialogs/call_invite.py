"""Add people to the call in progress: a group call offers the group's members
who are not in it, a private call the saved contacts. A second list offers
every one-to-one conversation, saved contact or not, and a field takes a phone
number to call. All of them go through `call/add-participants`, the route both
engines answer (core/call_invites.py).

Opened from the call's participant list (CallParticipantsDialog) while this
device is in the call. The logic lives in core/call_invites.py; this is
the plain-controls window around it. Every request runs off the wx thread and
a late answer never touches a closed dialog.
"""
import logging
import re
import threading

import wx

from core.call_invites import (calling_line, candidates, conversation_jids, offered_lists,
                                parse_results, picked_people, request_timeout, room_left,
                                saved_contact_jids, saved_name_of, summary)
from core.call_participants import CONNECTED, parse_participants
from countries import get_countries, get_default_country_index
from core.number_lookup import resolve_typed_number
from ui.dialogs.add_member_dialog import typed_number_to_jid
from ui.dialogs.call_invite_marks import CallInviteMarksMixin


def self_test(mw, own_in_call):
    """Whether a JID is this account: what the session knows, and the
    addresses the call itself marks as this device's (isSelf). The session
    may not know its own LID yet; the account was then offered as someone to
    add, and inviting it rang its own phone (reported 2026-11-12)."""
    own = [jid for jid in own_in_call if jid]

    def is_self(jid):
        return mw._is_self_jid(jid) or any(mw._chat_jids_equivalent(jid, o) for o in own)
    return is_self


def typed_number_jid(text, dial_code="") -> str:
    """'digits@s.whatsapp.net' of a number typed in the field, '' when it is
    not one. A number without a leading + or 00 is local to the country picked:
    its code is added, or "51 99999-0000" was invited at once as a Peruvian
    number. One with + or 00 already carries its country code."""
    text = str(text or "").strip()
    if not text or re.search(r"[^\d\s()+.\-]", text):
        return ""
    if text.startswith("+") or text.startswith("00"):
        dial_code = ""
        text = text[1:] if text.startswith("+") else text[2:]
    return typed_number_to_jid(text, dial_code).replace("@c.us", "@s.whatsapp.net")


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
        # One pane per list; the marks mixin acts on the one in use
        # (call_invite_marks.py), and one set of marks serves both.
        self._panes = []
        for title in (i18n.t("call_invite_list") if self._group else i18n.t("call_invite_list_contacts"),
                      i18n.t("call_invite_list_conversations")):
            sizer.Add(wx.StaticText(panel, label=title), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            listbox = wx.ListBox(panel, choices=[], style=wx.LB_SINGLE, name=title)
            sizer.Add(listbox, 1, wx.EXPAND | wx.ALL, 8)
            self._panes.append({"list": listbox, "rows": [], "labels": []})
        self._list, self._rows = self._panes[0]["list"], self._panes[0]["rows"]
        sizer.Add(wx.StaticText(panel, label=i18n.t("add_member_custom_number_label")),
                  0, wx.LEFT | wx.RIGHT, 8)
        # The country of a number typed without its code, as in the add-member
        # dialog; a number typed with + or 00 ignores it.
        sizer.Add(wx.StaticText(panel, label=i18n.t("add_member_country_label")),
                  0, wx.LEFT | wx.RIGHT, 8)
        self._countries = get_countries(i18n.language)
        self._country = wx.ComboBox(panel, choices=[c[0] for c in self._countries],
                                    style=wx.CB_READONLY)
        self._country.SetSelection(get_default_country_index(self._countries, i18n.language))
        sizer.Add(self._country, 0, wx.EXPAND | wx.ALL, 8)
        # Enter in the field adds, as the Add button does.
        self._number = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self._number.Bind(wx.EVT_TEXT_ENTER, self._on_add)
        sizer.Add(self._number, 0, wx.EXPAND | wx.ALL, 8)
        self._status = wx.StaticText(panel, label=i18n.t("loading"))
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._add_btn = wx.Button(panel, wx.ID_OK, label=i18n.t("call_invite_add_button"))
        self._add_btn.Bind(wx.EVT_BUTTON, self._on_add)
        buttons.Add(self._add_btn, 0, wx.ALL, 8)
        cancel = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
        cancel.Bind(wx.EVT_BUTTON, self._close)
        buttons.Add(cancel, 0, wx.ALL, 8)
        sizer.Add(buttons, 0, wx.ALIGN_CENTER)
        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.SetMinSize((450, 520))
        self.Fit()
        self.CentreOnParent()
        self.Bind(wx.EVT_CLOSE, self._close)
        self._init_invite_marks()
        self._list.SetFocus()
        threading.Thread(target=self._load, daemon=True, name="call-invite-load").start()

    # ------------------------------------------------------------- loading
    def _load(self):
        mw = self._mw
        lists, connected, failed = [[], []], 0, False
        try:
            contacts = dict(getattr(mw, "contacts", None) or {})
            if self._group:
                info = mw.get_group_info_recent(self._chat) or {}
                members = [p.get("id") for p in info.get("participants") or () if isinstance(p, dict)]
            else:
                members = saved_contact_jids(contacts)
            chats = conversation_jids(
                list(getattr(mw, "chats", None) or {}),
                lambda jid: mw.is_chat_locked(jid) or mw.is_chat_deleted(jid))
            response = mw._raise_for_call_response(mw._post_call_control(
                "participants", {"callId": self._call_id}, timeout=20), "participants")
            listed = parse_participants(response.json(), self._call_id) or []
            connected = sum(1 for p in listed if p["state"] == CONNECTED)
            # someone who left, declined or missed it may be added again
            present = [p["jid"] for p in listed
                       if p["state"] in (CONNECTED, "ringing") and not p["isSelf"]]
            is_self = self_test(mw, [p["jid"] for p in listed if p["isSelf"]])
            sources = offered_lists([members, chats], dict(getattr(mw, "_lid_to_phone", None) or {}),
                                    saved_name_of(contacts))
            lists = [candidates(source, present, is_self, mw._chat_jids_equivalent,
                                lambda jid: mw.chat_display_name({"remoteJid": jid}))
                     for source in sources]
        except Exception:
            failed = True
            logging.warning("[call] members to add unavailable")
        wx.CallAfter(self._show_rows, lists, connected, failed)

    def _show_rows(self, lists, connected, failed):
        if not self._alive:
            return
        if not self._allowed():
            lists, failed = [[], []], True
        i18n = self._mw.i18n
        self._connected = connected
        # a mark of someone no longer offered is forgotten
        self._marks.intersection_update(jid for rows in lists for jid, _name in rows)
        for pane, rows in zip(self._panes, lists):
            pane["rows"] = rows
            pane["labels"] = self._invite_labels(rows)
            listbox = pane["list"]
            listbox.Freeze()
            try:
                listbox.Set(pane["labels"])
                if rows:
                    listbox.SetSelection(0)
            finally:
                listbox.Thaw()
        self._use_invite_pane(self._list)
        rows = lists[0]
        if failed:
            status = i18n.t("call_invite_failed")
        elif not rows:
            status = i18n.t("call_invite_none") if self._group else i18n.t("call_invite_none_contacts")
        else:
            status = i18n.t("call_invite_count").format(count=len(rows))
        self._status.SetLabel(status)
        if not rows:
            self._mw.output(status)

    # ------------------------------------------------------------- adding
    def _on_add(self, _event=None):
        if self._busy or not self._alive:
            return
        i18n = self._mw.i18n
        if not self._allowed():
            self._mw.output(i18n.t("call_invite_failed"), interrupt=True)
            return
        picked = picked_people([row for pane in self._panes for row in pane["rows"]], self._marks)
        typed = self._number.GetValue().strip()
        index = self._country.GetSelection()
        number = typed_number_jid(
            typed, self._countries[index][1] if 0 <= index < len(self._countries) else "")
        if typed and not number:
            self._mw.output(i18n.t("add_member_invalid_number"), interrupt=True)
            self._number.SetFocus()
            return
        if number and not any(self._mw._chat_jids_equivalent(number, jid) for jid, _ in picked):
            # a known person is named; anyone else is read as the number
            picked.append((number, self._mw.chat_display_name({"remoteJid": number})))
        else:
            number = ""
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
        jids = [jid for jid, _name in picked]
        names = {jid: name for jid, name in picked}
        # "Calling ..." now; who was invited is only said when the server
        # confirmed it (summary), never because the request left
        sending = calling_line([name for _jid, name in picked], i18n)
        self._status.SetLabel(sending)
        self._mw.output(sending, interrupt=True)

        def worker():
            spoken, error, invited = None, None, False
            try:
                if number:
                    # the address WhatsApp knows the typed number by (the
                    # Brazilian 9th digit), as a new conversation asks it
                    found = resolve_typed_number(self._mw, number.split("@")[0])
                    if found and found != number and found not in names:
                        jids[jids.index(number)] = found
                        names[found] = names.pop(number)
                response = self._mw._raise_for_call_response(self._mw._post_call_control(
                    "add-participants", {"callId": self._call_id, "participants": jids},
                    timeout=request_timeout(len(jids))), "add-participants")
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
