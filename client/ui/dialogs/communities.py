"""The Communities panel (Alt+8): the account's communities, their groups
and admins, and the way into them.

A panel of the main window, like Status and Calls (it was a window of its
own until 2026-11): shown by main_window/content_panels.py, read again each
time it is shown, and opening a group leaves it for the conversation. The
admin actions live in community_admin.py; this module lists, describes and
opens.
"""

import dataclasses
import logging
import wx
from core.communities import (CANCELLED, JOINED, REQUEST_PENDING, REQUESTED, SUBGROUP_FILTERS,
                              cancel_join_request, empty_groups_key, filter_subgroups,
                              join_action,
                              join_subgroup, list_communities, most_recent_group,
                              refresh_community)
from core.participant_names import person_name
from ui.dialogs.labelled_control import add_labelled
from ui.dialogs.background_work import BackgroundWork
from ui.dialogs.community_admin import CommunityAdminActions
from ui.dialogs.group_join_dialog import open_group_chat
from ui.panel_labels import TranslatedLabels


def open_communities(owner, event=None):
    """New conversation's "Communities": that dialog closes and the
    Communities panel comes up."""
    from main_window.content_panels import show_communities
    owner.EndModal(wx.ID_CANCEL)
    wx.CallAfter(show_communities, owner._mw)


#: The join button's label for each join_action().
_JOIN_LABELS = {"join": "communities_join_group", "request": "communities_request_join",
                "cancel": "communities_cancel_join_request"}

#: What each join or cancel outcome says, and whether a request is pending after it.
_JOIN_OUTCOMES = {
    REQUESTED: ("communities_join_requested", True),
    REQUEST_PENDING: ("communities_join_request_pending", True),
    CANCELLED: ("communities_join_cancelled", False),
}

#: A refusal WhatsApp gave a reason for; anything else is a plain failure.
_JOIN_REFUSALS = {"hidden_group": "communities_join_hidden",
                  "group_unavailable": "communities_join_unavailable"}


def community_label(community, i18n):
    name = community.name or i18n.t("communities_unnamed")
    return i18n.t("communities_row_admin").format(name=name) if community.i_am_admin else name


def subgroup_label(group, i18n):
    name = group.name or i18n.t("unknown_group")
    if group.announcement:
        label = i18n.t("communities_group_announcements").format(name=name)
    else:
        label = i18n.t("communities_group_general").format(name=name) if group.general else name
    if group.joined:
        return label
    key = "communities_group_not_joined_approval" if group.approval else "communities_group_not_joined"
    return i18n.t(key).format(label=label)


class CommunitiesPanel(TranslatedLabels, CommunityAdminActions, BackgroundWork, wx.Panel):
    def __init__(self, main_window, parent):
        self._init_background(main_window)
        self._init_labels()
        i18n = self._i18n
        self._loaded = False
        self._communities = []
        # Which of the community's groups the list shows (SUBGROUP_FILTERS),
        # and the communities whose other groups were already asked for.
        self._filter = SUBGROUP_FILTERS[0]
        self._refreshed = set()
        super().__init__(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        def labelled(key, make, proportion):
            return self._keep_labelled(add_labelled(self, sizer, i18n.t(key), make, proportion), key)

        self._list = labelled("communities_list", lambda: wx.ListBox(self, style=wx.LB_SINGLE), 2)
        self._list.Bind(wx.EVT_LISTBOX, self._on_select)
        # Straight after the list in the Tab order: the quick way into a
        # community is its group with the newest message. Enter on the list
        # moves to the community's groups instead (_on_enter).
        self._open_recent = wx.Button(self, label=i18n.t("communities_open_recent_group"))
        self._open_recent.Bind(wx.EVT_BUTTON, self._on_open_recent)
        sizer.Add(self._open_recent, 0, wx.LEFT | wx.RIGHT, 8)
        # The same kind of control, in the same place before its list, as
        # the chat list's filters.
        self._filter_radio = wx.RadioBox(
            self, label=i18n.t("communities_groups_filter"),
            choices=[i18n.t("communities_filter_all"), i18n.t("communities_filter_joined"),
                     i18n.t("communities_filter_not_joined")],
            majorDimension=1, style=wx.RA_SPECIFY_ROWS)
        self._keep_label(self._filter_radio, "communities_groups_filter")
        self._keep_items(self._filter_radio, ("communities_filter_all", "communities_filter_joined",
                                              "communities_filter_not_joined"))
        self._filter_radio.Bind(wx.EVT_RADIOBOX, self._on_filter)
        sizer.Add(self._filter_radio, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._groups = labelled("communities_groups_list", lambda: wx.ListBox(self, style=wx.LB_SINGLE), 2)
        self._groups.Bind(wx.EVT_LISTBOX, lambda event: self._update_buttons())
        self._groups.Bind(wx.EVT_LISTBOX_DCLICK, self._on_open)
        self._details = labelled("communities_details", lambda: wx.TextCtrl(
            self, style=wx.TE_READONLY | wx.TE_MULTILINE), 1)
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        buttons = wx.WrapSizer(wx.HORIZONTAL)

        def button(key, handler):
            control = wx.Button(self, label=i18n.t(key))
            control.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(control, 0, wx.ALL, 4)
            return self._keep_label(control, key)

        self._open = button("communities_open_group", self._on_open)
        # Join, ask to join, or withdraw that request: whichever WhatsApp's
        # own button would offer on the selected group (join_action()).
        self._join = button("communities_join_group", self._on_join)
        self._add_groups = button("communities_add_groups", self._on_add_groups)
        self._remove_group = button("communities_remove_group", self._on_remove_group)
        self._promote = button("communities_promote", self._on_promote)
        self._demote = button("communities_demote", self._on_demote)
        self._deactivate = button("communities_deactivate", self._on_deactivate)
        self._new = button("communities_new", self._on_new)
        self._refresh = button("communities_refresh", lambda event: self._reload())
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        # No default button: Enter means something different on each list.
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._update_buttons()

    # ── shown and translated ──────────────────────────────────────────────

    def on_show(self):
        """Alt+8 / the navigation list: the list gets the focus and the
        communities are read again; their count is said the first time, or
        after the account's connection changed (a new pairing)."""
        if self._renew_connection():
            self._loaded, self._refreshed = False, set()
        self._list.SetFocus()
        self._reload(quiet=self._loaded)

    def refresh_labels(self):
        """MainWindow switched language: every label, and the rows."""
        self._relabel()
        if self._communities:
            self._show(self._communities)
        self._update_buttons()

    # ── state ─────────────────────────────────────────────────────────────

    def _selected(self):
        index = self._list.GetSelection()
        return self._communities[index] if 0 <= index < len(self._communities) else None

    def _shown_groups(self):
        """The selected community's groups the filter lets through, in the
        list's order."""
        return filter_subgroups(self._selected(), self._filter)

    def _selected_group(self):
        groups = self._shown_groups()
        index = self._groups.GetSelection()
        return groups[index] if 0 <= index < len(groups) else None

    def _recent_group(self):
        """The selected community's group with the newest message, by the
        same timestamp the conversations list sorts by. A locked group is
        never offered, as in every other picker."""
        mw = self._mw

        def last_activity(jid):
            chat = mw.chats.get(jid)
            if not isinstance(chat, dict) or mw.is_chat_locked(jid):
                return 0
            return mw._chat_last_ts(chat)

        return most_recent_group(self._selected(), last_activity)

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
        self._open.Enable(group is not None and group.joined)
        action = join_action(group) if free else None
        self._join.Enable(action is not None)
        self._join.SetLabel(self._i18n.t(_JOIN_LABELS.get(action, "communities_join_group")))
        recent = self._recent_group() if free else None
        self._open_recent.Enable(recent is not None)
        self._open_recent.SetLabel(
            self._i18n.t("communities_open_recent_group_named").format(
                name=subgroup_label(recent, self._i18n).replace("&", "&&"))
            if recent else self._i18n.t("communities_open_recent_group"))
        self._add_groups.Enable(admin)
        # The announcement group is the community itself: it is not unlinked.
        self._remove_group.Enable(admin and group is not None and not group.announcement)
        self._promote.Enable(admin and community.announcement_group is not None)
        self._demote.Enable(admin)
        self._deactivate.Enable(admin)

    # ── reading ───────────────────────────────────────────────────────────

    def _reload(self, event=None, quiet=False):
        if self._busy or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))
        connection = self._connection
        self._run_async(lambda: list_communities(connection),
                        lambda communities: self._finish_reload(communities, quiet))

    def _finish_reload(self, communities, quiet=False):
        self._set_busy(False)
        if communities is None or not self._current():
            logging.warning("[communities] read_failed")
            self._announce_if_shown(self._i18n.t("communities_unavailable"))
            return
        self._loaded = True
        self._show(communities)
        text = (self._i18n.t("communities_count").format(count=len(communities))
                if communities else self._i18n.t("communities_empty"))
        if quiet:       # shown again: the list is what it was, said only on screen
            self._status.SetLabel(text)
        else:
            self._announce_if_shown(text)

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
        groups = self._shown_groups()
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
        self._ask_for_other_groups(community)

    def _on_filter(self, event):
        index = self._filter_radio.GetSelection()
        self._filter = SUBGROUP_FILTERS[index] if 0 <= index < len(SUBGROUP_FILTERS) else SUBGROUP_FILTERS[0]
        kept = self._selected_group()
        self._show_selected(kept.jid if kept else "")

    def _ask_for_other_groups(self, community):
        """Ask WhatsApp, once per community while the panel lives, for the
        community's groups the account is not in: WhatsApp only knows them
        after being asked, as when a community is opened in WhatsApp Web.
        Quiet: the groups already listed stay usable meanwhile, and a failure
        leaves the list as it was, to be asked again the next time."""
        if community is None or community.jid in self._refreshed or not self._current():
            return
        jid = community.jid
        self._refreshed.add(jid)
        connection = self._connection
        self._run_async(lambda: refresh_community(connection, jid),
                        lambda refreshed: self._finish_other_groups(jid, refreshed))

    def _finish_other_groups(self, jid, refreshed):
        if refreshed is None or self._busy or not self._current():
            # Not now: an action is reading the list itself, or this failed.
            self._refreshed.discard(jid)
            return
        current = next((c for c in self._communities if c.jid == jid), None)
        if current is None or current.subgroups == refreshed.subgroups:
            return
        self._show([refreshed if c.jid == jid else c for c in self._communities])

    def _describe(self, community):
        i18n = self._i18n
        lines = [community.description]
        if community.admins:
            names = sorted({person_name(self._mw, member.jid) for member in community.admins},
                           key=str.casefold)
            lines.append(i18n.t("communities_admins_line").format(names=", ".join(names)))
        if not any(group.joined for group in community.subgroups):
            lines.append(i18n.t("communities_no_groups"))
        if not community.i_am_admin:
            lines.append(i18n.t("communities_view_only"))
        return "\n\n".join(line for line in lines if line)

    # ── keyboard ──────────────────────────────────────────────────────────

    def _on_char_hook(self, event):
        if (event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
                and not event.HasAnyModifiers() and self._on_enter(wx.Window.FindFocus())):
            return
        event.Skip()

    def _on_enter(self, focus):
        """Enter on a community shows its groups (focus moves to them); Enter
        on a group opens it. True when Enter was handled here."""
        if focus is self._list:
            if self._groups.GetCount():
                self._groups.SetFocus()
            elif self._selected() is not None:
                self._announce(self._i18n.t(empty_groups_key(self._selected(), self._filter)))
            return True
        if focus is self._groups:
            self._on_open(None)
            return True
        return False

    # ── opening ───────────────────────────────────────────────────────────

    def _on_open(self, event):
        self._open_group(self._selected_group())

    def _on_open_recent(self, event):
        self._open_group(self._recent_group())

    # ── joining ───────────────────────────────────────────────────────────

    def _on_join(self, event):
        community, group = self._selected(), self._selected_group()
        action = join_action(group)
        if self._busy or community is None or action is None or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))
        connection, community_jid, group_jid = self._connection, community.jid, group.jid
        call = cancel_join_request if action == "cancel" else join_subgroup

        def work():
            try:
                return "state", call(connection, community_jid, group_jid)
            except ValueError as exc:
                return "error", str(exc)

        self._run_async(work, lambda result: self._finish_join(community_jid, group, action, result))

    def _finish_join(self, community_jid, group, action, result):
        self._set_busy(False)
        if not self._current():
            return
        kind, value = result if isinstance(result, tuple) else ("error", "unavailable")
        if kind == "error":
            # A static code only: never the group, never the account.
            logging.warning("[communities] join_failed action=%s code=%s", action, value)
            fallback = "communities_join_cancel_failed" if action == "cancel" else "communities_join_failed"
            self._announce(self._i18n.t(_JOIN_REFUSALS.get(value, fallback)))
            return
        if value == JOINED:
            self._announce(self._i18n.t("communities_joined"))
            self._open_group(dataclasses.replace(group, joined=True, requested=False))
            return
        key, requested = _JOIN_OUTCOMES[value]
        self._replace_group(community_jid, dataclasses.replace(group, requested=requested))
        self._announce(self._i18n.t(key))

    def _replace_group(self, community_jid, group):
        """Show *group* as it now is, in place, keeping the selection."""
        communities = []
        for community in self._communities:
            if community.jid == community_jid:
                community = dataclasses.replace(community, subgroups=tuple(
                    group if existing.jid == group.jid else existing
                    for existing in community.subgroups))
            communities.append(community)
        self._show(communities)

    def _open_group(self, group):
        if self._busy or group is None:
            return
        if not group.joined:
            # Not a conversation of this account: there is nothing to open.
            self._announce(self._i18n.t("communities_group_not_joined_open"))
            return
        # The conversation takes the screen: navigate_to_conversation_jid()
        # hides this panel (main_window/content_panels.py).
        open_group_chat(self._mw, group.jid, group.name)
