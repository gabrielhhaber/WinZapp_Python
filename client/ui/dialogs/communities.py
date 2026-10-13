"""The account's communities: their groups and admins, and the way into them.

The admin actions live in community_admin.py; this module lists, describes
and opens.
"""

import logging
import threading
import wx
from core.communities import list_communities
from core.group_invitation import connection_identity
from ui.accessible import split_mnemonic
from ui.dialogs.community_admin import CommunityAdminActions, person_name
from ui.dialogs.group_join_dialog import open_group_chat


def show_communities(mw, parent):
    """Show the dialog and open the group the user chose, if any. True when
    one was opened."""
    dialog = CommunitiesDialog(parent, mw)
    try:
        if dialog.ShowModal() != wx.ID_OK or dialog.result is None:
            return False
        group = dialog.result
    finally:
        dialog.Destroy()
    open_group_chat(mw, group.jid, group.name)
    return True


def open_communities(owner, event=None):
    """New conversation's "Communities": that dialog closes once a group is
    opened from here."""
    if show_communities(owner._mw, owner):
        owner.EndModal(wx.ID_OK)


def community_label(community, i18n):
    name = community.name or i18n.t("communities_unnamed")
    return i18n.t("communities_row_admin").format(name=name) if community.i_am_admin else name


def subgroup_label(group, i18n):
    name = group.name or i18n.t("unknown_group")
    if group.announcement:
        return i18n.t("communities_group_announcements").format(name=name)
    return i18n.t("communities_group_general").format(name=name) if group.general else name


class CommunitiesDialog(CommunityAdminActions, wx.Dialog):
    def __init__(self, parent, main_window):
        self._mw = main_window
        self._i18n = i18n = main_window.i18n
        self._connection = connection_identity(main_window)
        self._closed = threading.Event()
        self._busy = False
        self._loaded = False
        self._communities = []
        self.result = None
        super().__init__(parent, title=i18n.t("communities_title"), size=(620, 620),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)

        def labelled(key, control, proportion):
            sizer.Add(wx.StaticText(self, label=i18n.t(key)), 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
            control.SetName(split_mnemonic(i18n.t(key))[0])
            sizer.Add(control, proportion, wx.EXPAND | wx.ALL, 8)
            return control

        self._list = labelled("communities_list", wx.ListBox(self, style=wx.LB_SINGLE), 2)
        self._list.Bind(wx.EVT_LISTBOX, self._on_select)
        self._groups = labelled("communities_groups_list", wx.ListBox(self, style=wx.LB_SINGLE), 2)
        self._groups.Bind(wx.EVT_LISTBOX, lambda event: self._update_buttons())
        self._groups.Bind(wx.EVT_LISTBOX_DCLICK, self._on_open)
        self._details = labelled("communities_details", wx.TextCtrl(
            self, style=wx.TE_READONLY | wx.TE_MULTILINE), 1)
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        buttons = wx.WrapSizer(wx.HORIZONTAL)

        def button(key, handler, button_id=wx.ID_ANY):
            control = wx.Button(self, button_id, label=i18n.t(key))
            control.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(control, 0, wx.ALL, 4)
            return control

        self._open = button("communities_open_group", self._on_open)
        self._add_groups = button("communities_add_groups", self._on_add_groups)
        self._remove_group = button("communities_remove_group", self._on_remove_group)
        self._promote = button("communities_promote", self._on_promote)
        self._demote = button("communities_demote", self._on_demote)
        self._deactivate = button("communities_deactivate", self._on_deactivate)
        self._new = button("communities_new", self._on_new)
        self._refresh = button("communities_refresh", self._reload)
        button("close", self._on_close, wx.ID_CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        # Enter anywhere but on a button opens the selected group.
        self._open.SetDefault()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._update_buttons()
        self._list.SetFocus()
        self.CentreOnParent()
        wx.CallAfter(self._reload)

    # ── lifetime ──────────────────────────────────────────────────────────

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

    def _run_async(self, work, done):
        """*work* off the main thread, *done(result)* back on it; a dialog
        closed meanwhile hears nothing."""
        def run():
            try:
                result = work()
            except Exception:
                result = None
            if not self._closed.is_set():
                wx.CallAfter(self._deliver, done, result)
        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception:
            self._deliver(done, None)

    def _deliver(self, done, result):
        if not self._closed.is_set():
            done(result)

    # ── state ─────────────────────────────────────────────────────────────

    def _selected(self):
        index = self._list.GetSelection()
        return self._communities[index] if 0 <= index < len(self._communities) else None

    def _selected_group(self):
        community = self._selected()
        index = self._groups.GetSelection()
        return community.subgroups[index] if community and 0 <= index < len(community.subgroups) else None

    def _set_busy(self, busy):
        self._busy = busy
        self._update_buttons()

    def _update_buttons(self):
        free = not self._busy and self._current()
        community = self._selected() if free else None
        group = self._selected_group() if free else None
        admin = bool(community and community.i_am_admin)
        self._refresh.Enable(free)
        self._new.Enable(free and self._loaded)
        self._open.Enable(group is not None)
        self._add_groups.Enable(admin)
        # The announcement group is the community itself: it is not unlinked.
        self._remove_group.Enable(admin and group is not None and not group.announcement)
        self._promote.Enable(admin and community.announcement_group is not None)
        self._demote.Enable(admin)
        self._deactivate.Enable(admin)

    def _announce(self, text):
        self._status.SetLabel(text)
        self._mw.speak_output.output(text)

    # ── reading ───────────────────────────────────────────────────────────

    def _reload(self, event=None):
        if self._busy or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))
        connection = self._connection
        self._run_async(lambda: list_communities(connection), self._finish_reload)

    def _finish_reload(self, communities):
        self._set_busy(False)
        if communities is None or not self._current():
            logging.warning("[communities] read_failed")
            self._announce(self._i18n.t("communities_unavailable"))
            return
        self._loaded = True
        self._show(communities)
        self._announce(self._i18n.t("communities_count").format(count=len(communities))
                       if communities else self._i18n.t("communities_empty"))

    def _show(self, communities):
        """Replace the list, keeping the selected community and group."""
        i18n = self._i18n
        kept = self._selected()
        kept_group = self._selected_group()
        communities = sorted(communities, key=lambda community: (
            (community.name or i18n.t("communities_unnamed")).casefold(), community.jid))
        self._communities = communities
        index = next((position for position, community in enumerate(communities)
                      if kept is not None and community.jid == kept.jid), 0)
        self._list.Freeze()
        try:
            self._list.Set([community_label(community, i18n) for community in communities])
            if communities:
                self._list.SetSelection(index)
        finally:
            self._list.Thaw()
        self._show_selected(kept_group.jid if kept_group else "")

    def _on_select(self, event):
        self._show_selected("")

    def _show_selected(self, group_jid):
        i18n = self._i18n
        community = self._selected()
        groups = community.subgroups if community else ()
        self._groups.Freeze()
        try:
            self._groups.Set([subgroup_label(group, i18n) for group in groups])
            if groups:
                self._groups.SetSelection(next(
                    (position for position, group in enumerate(groups) if group.jid == group_jid), 0))
        finally:
            self._groups.Thaw()
        self._details.ChangeValue(self._describe(community) if community else "")
        self._update_buttons()

    def _describe(self, community):
        i18n = self._i18n
        lines = [community.description]
        if community.admins:
            names = sorted({person_name(self._mw, member.jid) for member in community.admins},
                           key=str.casefold)
            lines.append(i18n.t("communities_admins_line").format(names=", ".join(names)))
        if not community.subgroups:
            lines.append(i18n.t("communities_no_groups"))
        if not community.i_am_admin:
            lines.append(i18n.t("communities_view_only"))
        return "\n\n".join(line for line in lines if line)

    def _on_open(self, event):
        group = self._selected_group()
        if self._busy or group is None:
            return
        self.result = group
        self._closed.set()
        self.EndModal(wx.ID_OK)
