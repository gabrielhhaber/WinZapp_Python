"""Pending requests to join a group: the admin approves or rejects them."""

import logging
from datetime import datetime
import wx
from core.group_membership_requests import NotAdmin, list_requests, settle
from core.locale_format import get_datetime_format
from core.participant_names import person_name
from core.utils import is_phone_like
from ui.accessible import split_mnemonic
from ui.dialogs.background_work import BackgroundWork

_METHODS = {"InviteLink": "group_requests_method_link",
            "LinkedGroupJoin": "group_requests_method_community"}


def add_membership_requests_button(owner, page, sizer):
    """The group data dialog's button. Hidden until the account is known to
    be an admin: nobody else can read the requests."""
    button = wx.Button(page, label=owner._i18n.t("group_requests_button"))
    button.Hide()

    def open_dialog(event):
        dialog = GroupMembershipRequestsDialog(owner, owner._mw, owner._jid)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
    button.Bind(wx.EVT_BUTTON, open_dialog)
    sizer.Add(button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
    return button


def requester_name(mw, request):
    """Who is asking, in the most telling form known: the saved name, else
    the WhatsApp profile name with the number, else the number."""
    unnamed = mw.i18n.t("unnamed_participant")
    known = [person_name(mw, jid) for jid in (request.jid, request.phone) if jid]
    known = [name for name in known if name != unnamed]
    saved = [name for name in known if not is_phone_like(name)]
    if saved:
        return saved[0]
    number = known[0] if known else ""
    if request.name:
        return f"{request.name} ({number})" if number else request.name
    return number or unnamed


def request_label(mw, request, datetime_format):
    i18n = mw.i18n
    if request.method == "NonAdminAdd" and request.added_by and request.added_by != request.jid:
        how = i18n.t("group_requests_method_added").format(name=person_name(mw, request.added_by))
    else:
        how = i18n.t(_METHODS.get(request.method, "group_requests_method_unknown"))
    parts = [requester_name(mw, request), how]
    if request.time:
        parts.append(datetime.fromtimestamp(request.time).strftime(datetime_format))
    return ", ".join(parts)


class GroupMembershipRequestsDialog(BackgroundWork, wx.Dialog):
    def __init__(self, parent, main_window, group):
        self._init_background(main_window)
        i18n = self._i18n
        self._group = group
        self._requests = ()
        name = (main_window.chats.get(group) or {}).get("name") or i18n.t("unknown_group")
        super().__init__(parent, title=i18n.t("group_requests_title").format(name=name),
                         size=(560, 460), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=i18n.t("group_requests_list")), 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._list = wx.ListBox(self, style=wx.LB_SINGLE)
        self._list.SetName(split_mnemonic(i18n.t("group_requests_list"))[0])
        self._list.Bind(wx.EVT_LISTBOX, lambda event: self._update_buttons())
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        buttons = wx.WrapSizer(wx.HORIZONTAL)

        def button(key, handler, button_id=wx.ID_ANY):
            control = wx.Button(self, button_id, label=i18n.t(key))
            control.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(control, 0, wx.ALL, 4)
            return control

        self._approve = button("group_requests_approve", lambda event: self._answer(True))
        self._reject = button("group_requests_reject", lambda event: self._answer(False))
        self._refresh = button("group_requests_refresh", self._reload)
        button("close", self._on_close, wx.ID_CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._update_buttons()
        self._list.SetFocus()
        self.CentreOnParent()
        wx.CallAfter(self._reload)

    def _selected(self):
        index = self._list.GetSelection()
        return self._requests[index] if 0 <= index < len(self._requests) else None

    def _set_busy(self, busy):
        self._busy = busy
        self._update_buttons()

    def _update_buttons(self):
        free = not self._busy and self._current()
        chosen = free and self._selected() is not None
        self._refresh.Enable(free)
        self._approve.Enable(chosen)
        self._reject.Enable(chosen)

    def _reload(self, event=None):
        if self._busy or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))
        connection, group = self._connection, self._group

        def read():
            try:
                return list_requests(connection, group)
            except NotAdmin:
                return "not_admin"
        self._run_async(read, self._finish_reload)

    def _finish_reload(self, pending):
        self._set_busy(False)
        i18n = self._i18n
        if pending == "not_admin":
            self._show(())
            self._announce(i18n.t("group_requests_not_admin"))
            return
        if pending is None or not self._current():
            logging.warning("[group_requests] read_failed")
            self._announce(i18n.t("group_requests_unavailable"))
            return
        self._show(pending.requests)
        self._announce(self._summary(pending))

    def _summary(self, pending):
        i18n = self._i18n
        text = (i18n.t("group_requests_count").format(count=len(pending.requests))
                if pending.requests else i18n.t("group_requests_empty"))
        return text if pending.refreshed else f"{text} {i18n.t('group_requests_stale')}"

    def _show(self, requests):
        """Replace the list, oldest request first, keeping the selected person."""
        kept = self._selected()
        requests = tuple(sorted(requests, key=lambda request: (request.time or 0, request.jid)))
        self._requests = requests
        pattern = get_datetime_format(self._i18n.t("datetime_fmt"))
        self._list.Freeze()
        try:
            self._list.Set([request_label(self._mw, request, pattern) for request in requests])
            if requests:
                self._list.SetSelection(next(
                    (position for position, request in enumerate(requests)
                     if kept is not None and request.jid == kept.jid), 0))
        finally:
            self._list.Thaw()
        self._update_buttons()

    def _answer(self, approve):
        request = self._selected()
        if self._busy or request is None or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))
        connection, group = self._connection, self._group
        self._run_async(lambda: settle(connection, group, [request.jid], approve),
                        lambda outcome: self._finish_answer(outcome, approve))

    def _finish_answer(self, outcome, approve):
        self._set_busy(False)
        state, pending = outcome if outcome else ("failed", None)
        if pending is not None and self._current():
            self._show(pending.requests)
        if state != "done":
            logging.warning("[group_requests] answer state=%s", state)
        self._announce(self._i18n.t({
            "done": "group_requests_approved" if approve else "group_requests_rejected",
            "unconfirmed": "group_requests_unconfirmed", "failed": "group_requests_failed"}[state]))
