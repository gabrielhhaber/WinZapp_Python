"""What a community admin can do from the communities dialog.

Every action goes through _perform(): sent once, then believed only when a
fresh reading of the communities shows it (core.communities.run_action).
The questions asked of the user are small methods of their own, so a test
answers them without a window.
"""

import logging
import re
import wx
from core import communities as api
from core.utils import contact_dedup_key
from ui.dialogs.community_create import CommunityCreateDialog

_PERSON = re.compile(r"\d+(?::\d+)?@(?:c\.us|lid|s\.whatsapp\.net)")
_OUTCOMES = {"done": "communities_action_done", "unconfirmed": "communities_action_unconfirmed",
             "failed": "communities_action_failed"}


def person_name(mw, jid):
    """A name to read aloud for a participant: never a raw identifier."""
    norm = mw._normalize_jid(jid)
    if mw._is_self_jid(norm):
        return mw.self_reference_label()
    name = mw._resolve_jid_name(norm, resolve_missing=False)
    if (not isinstance(name, str) or not name.strip()
            or name.endswith(("@lid", "@c.us", "@s.whatsapp.net", "@g.us"))):
        return mw.i18n.t("unnamed_participant")
    return name.strip()


class CommunityAdminActions:
    # ── questions to the user ─────────────────────────────────────────────

    def _choose_many(self, prompt, names):
        dialog = wx.MultiChoiceDialog(self, prompt, self._i18n.t("communities_title"), names)
        try:
            return list(dialog.GetSelections()) if dialog.ShowModal() == wx.ID_OK else []
        finally:
            dialog.Destroy()

    def _choose_one(self, prompt, names):
        dialog = wx.SingleChoiceDialog(self, prompt, self._i18n.t("communities_title"), names)
        try:
            return dialog.GetSelection() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()

    def _confirm(self, text):
        # No is the default, so a stray Enter changes nothing.
        dialog = wx.MessageDialog(self, text, self._i18n.t("communities_title"),
                                  wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
        try:
            return dialog.ShowModal() == wx.ID_YES
        finally:
            dialog.Destroy()

    def _ask_new_community(self, groups):
        dialog = CommunityCreateDialog(self, self._mw, groups)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return None
            return dialog.name, dialog.description, dialog.group_jids
        finally:
            dialog.Destroy()

    # ── the one way an action runs ────────────────────────────────────────

    def _admin_community(self):
        community = self._selected()
        if self._busy or not self._current() or community is None or not community.i_am_admin:
            return None
        return community

    def _person_key(self, jid):
        return contact_dedup_key(self._mw, jid)

    def _name_of(self, community):
        return community.name or self._i18n.t("communities_unnamed")

    def _perform(self, action, achieved):
        if self._busy or not self._current():
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))
        connection = self._connection
        self._run_async(lambda: api.run_action(connection, action, achieved), self._finish_action)

    def _finish_action(self, outcome):
        self._set_busy(False)
        state, communities = outcome if outcome else ("failed", None)
        if state != "done":
            logging.warning("[communities] action state=%s", state)
        if communities is not None and self._current():
            self._show(communities)
        self._announce(self._i18n.t(_OUTCOMES[state]))

    # ── groups ────────────────────────────────────────────────────────────

    def _on_add_groups(self, event):
        community = self._admin_community()
        if community is None:
            return
        i18n = self._i18n
        rows = api.linkable_groups(self._mw.chats, self._communities)
        if not rows:
            self._announce(i18n.t("communities_no_linkable_groups"))
            return
        indices = self._choose_many(
            i18n.t("communities_pick_groups_add").format(name=self._name_of(community)),
            [name for _jid, name in rows])
        if not indices:
            return
        jids = [rows[index][0] for index in indices]
        connection = self._connection
        self._perform(lambda: api.link_groups(connection, community.jid, jids),
                      api.linked(community.jid, jids))

    def _on_remove_group(self, event):
        community = self._admin_community()
        group = self._selected_group()
        if community is None or group is None or group.announcement:
            return
        i18n = self._i18n
        if not self._confirm(i18n.t("communities_confirm_remove_group").format(
                group=group.name or i18n.t("unknown_group"), name=self._name_of(community))):
            return
        connection = self._connection
        self._perform(lambda: api.unlink_groups(connection, community.jid, [group.jid]),
                      api.linked(community.jid, [group.jid], present=False))

    # ── admins ────────────────────────────────────────────────────────────

    def _on_demote(self, event):
        community = self._admin_community()
        if community is None:
            return
        i18n = self._i18n
        # The creator cannot be demoted, and nobody demotes themselves here.
        rows = sorted(
            ((member.jid, person_name(self._mw, member.jid)) for member in community.admins
             if not member.creator and not self._mw._is_self_jid(self._mw._normalize_jid(member.jid))),
            key=lambda row: row[1].casefold())
        if not rows:
            self._announce(i18n.t("communities_no_candidates"))
            return
        index = self._choose_one(
            i18n.t("communities_pick_demote").format(name=self._name_of(community)),
            [name for _jid, name in rows])
        if index is None:
            return
        person = rows[index][0]
        connection, key = self._connection, self._person_key
        self._perform(lambda: api.demote_admin(connection, community.jid, person),
                      api.admin(community.jid, key(person), key, present=False))

    def _on_promote(self, event):
        community = self._admin_community()
        if community is None or community.announcement_group is None:
            return
        self._set_busy(True)
        self._status.SetLabel(self._i18n.t("loading"))
        self._run_async(lambda: self._promotion_candidates(community),
                        lambda rows: self._pick_promotion(community, rows))

    def _promotion_candidates(self, community):
        """(jid, name) of who can be promoted, read off the main thread.
        Everyone in a community is in its announcement group, so that group's
        participants are the community's members."""
        data = self._mw.get_group_info_recent(community.announcement_group.jid)
        participants = data.get("participants") if isinstance(data, dict) else None
        if not isinstance(participants, list) or not participants:
            raise ValueError("participants_unavailable")
        key = self._person_key
        taken = {key(member.jid) for member in community.admins}
        rows = []
        for participant in participants:
            jid = participant.get("id") if isinstance(participant, dict) else None
            if isinstance(jid, dict):
                jid = jid.get("_serialized")
            if not isinstance(jid, str) or not _PERSON.fullmatch(jid):
                continue
            person = key(jid)
            if person in taken or self._mw._is_self_jid(self._mw._normalize_jid(jid)):
                continue
            taken.add(person)
            rows.append((jid, person_name(self._mw, jid)))
        return sorted(rows, key=lambda row: row[1].casefold())

    def _pick_promotion(self, community, rows):
        self._set_busy(False)
        i18n = self._i18n
        if rows is None or not self._current():
            self._announce(i18n.t("communities_participants_unavailable"))
            return
        if not rows:
            self._announce(i18n.t("communities_no_candidates"))
            return
        index = self._choose_one(
            i18n.t("communities_pick_promote").format(name=self._name_of(community)),
            [name for _jid, name in rows])
        if index is None:
            return
        person = rows[index][0]
        connection, key = self._connection, self._person_key
        self._perform(lambda: api.promote_admin(connection, community.jid, person),
                      api.admin(community.jid, key(person), key))

    # ── the community itself ──────────────────────────────────────────────

    def _on_deactivate(self, event):
        community = self._admin_community()
        if community is None:
            return
        if not self._confirm(self._i18n.t("communities_confirm_deactivate").format(
                name=self._name_of(community))):
            return
        connection = self._connection
        self._perform(lambda: api.deactivate_community(connection, community.jid),
                      api.gone(community.jid))

    def _on_new(self, event):
        # Only over a list that was read: "created" means "was not there".
        if self._busy or not self._loaded or not self._current():
            return
        answer = self._ask_new_community(api.linkable_groups(self._mw.chats, self._communities))
        if answer is None:
            return
        name, description, jids = answer
        known = frozenset(community.jid for community in self._communities)
        connection = self._connection
        self._perform(lambda: api.create_community(connection, name, description, jids),
                      api.created(known, name))
