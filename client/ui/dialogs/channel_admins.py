"""A channel's people, opened from the Channels window (Alt+9) for a channel
this account administers, as WhatsApp Web's channel info shows them: the
owner, the admins, the pending admin invites and the followers, each with
their role.

What the selected person allows (core/channels.member_actions): the owner
invites a contact or a follower to be an admin (WhatsApp sends the invite to
their chat, where they accept it), cancels a pending invite, removes an
admin, or hands the channel to an admin; an admin may stop being one.

The requests are core/channels.py's; every one runs off the wx thread and a
late answer never touches a closed window (BackgroundWork). Rows say names,
never JIDs.
"""

import dataclasses
import datetime
import logging

import wx

from core import channels as ch
from ui.dialogs.attach_contact_dialog import AttachContactDialog
from ui.dialogs.background_work import BackgroundWork
from ui.dialogs.labelled_control import add_labelled


def contact_name(entry, i18n) -> str:
    """The picked contact as the window says it, never their JID."""
    name = str((entry or {}).get("name") or (entry or {}).get("pushName") or "").strip()
    return name or i18n.t("unknown_contact")


def read_people(connection, channel):
    """(admin count or None, followers or None, pending invites or None) of
    *channel*; a part that fails is None, the others still come."""
    def attempt(work):
        try:
            return work()
        except Exception:
            return None
    count = attempt(lambda: ch.admin_count(connection, channel.jid))
    found = attempt(lambda: ch.followers(connection, channel.jid))
    pending = attempt(lambda: ch.pending_admins(connection, channel.jid)) if channel.owner else []
    return count, found, pending


class ChannelAdminsDialog(BackgroundWork, wx.Dialog):
    def __init__(self, parent, main_window, channel):
        self._init_background(main_window)
        self._channel = channel
        self._rows = []
        self.changed = False            # this account's role changed here
        i18n = self._i18n
        super().__init__(parent, title=i18n.t("channel_people_title").format(
            name=channel.name or i18n.t("channels_unnamed")),
            size=(520, 480), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._status = wx.StaticText(self, label=i18n.t("loading"))
        sizer.Add(self._status, 0, wx.ALL, 8)
        self._list = add_labelled(self, sizer, i18n.t("channel_people_list"),
                                  lambda: wx.ListBox(self, style=wx.LB_SINGLE), 1)
        self._list.Bind(wx.EVT_LISTBOX, lambda event: self._update_buttons())
        buttons = wx.WrapSizer(wx.HORIZONTAL)

        def button(key, handler, button_id=wx.ID_ANY):
            control = wx.Button(self, button_id, label=i18n.t(key))
            control.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(control, 0, wx.ALL, 4)
            return control

        self._invite = button("channel_admin_invite", self._on_invite)
        self._make_admin = button("channel_admin_make", self._on_make_admin)
        self._revoke = button("channel_admin_revoke", self._on_revoke)
        self._demote = button("channel_admin_demote", self._on_demote)
        self._make_owner = button("channel_owner_make", self._on_make_owner)
        self._leave = button("channel_admin_leave", self._on_leave)
        self._refresh = button("channels_refresh", self._load)
        button("close", self._on_close, wx.ID_CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._update_buttons()
        self._list.SetFocus()
        self.CentreOnParent()
        wx.CallAfter(self._load)

    # ── state ─────────────────────────────────────────────────────────────

    def _me(self):
        mw = self._mw
        lid, phone = str(getattr(mw, "my_lid", "") or ""), str(getattr(mw, "my_jid", "") or "")
        return ch.Member(lid or phone, phone if lid else "", "", self._channel.role)

    def _is_me(self, member):
        me = self._me()
        return member is not None and bool(
            {member.jid, member.phone} & {me.jid, me.phone} - {""})

    def _selected(self):
        index = self._list.GetSelection()
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def _update_buttons(self):
        # Not disabled while busy: the handlers refuse a second press (channels.py).
        member = self._selected()
        actions = ch.member_actions(member, self._channel.role, self._is_me(member))
        self._invite.Enable(self._channel.owner)
        self._make_admin.Enable("make_admin" in actions)
        self._revoke.Enable("revoke" in actions)
        self._demote.Enable("demote" in actions)
        self._make_owner.Enable("make_owner" in actions)
        self._leave.Enable("leave" in actions)

    def _name(self, member):
        i18n = self._i18n
        if self._is_me(member):
            return i18n.t("channel_member_you")
        known = self._mw.chat_display_name({"remoteJid": member.phone or member.jid})
        return ch.member_name(known, member.name, i18n.t("unknown_contact"))

    def _row_text(self, member):
        i18n = self._i18n
        role = {"owner": i18n.t("channel_role_owner"), "admin": i18n.t("channel_role_admin"),
                "pending": i18n.t("channel_role_pending")}.get(member.role,
                                                                i18n.t("channel_role_follower"))
        return f"{self._name(member)}, {role}"

    # ── reading ───────────────────────────────────────────────────────────

    def _load(self, event=None, quiet=False):
        if self._refused_while_busy() or not self._current():
            return
        self._busy = True
        self._status.SetLabel(self._i18n.t("loading"))
        connection, channel = self._connection, self._channel
        self._run_async(lambda: read_people(connection, channel),
                        lambda found: self._finish_load(found, quiet))

    def _finish_load(self, found, quiet=False):
        self._busy = False
        i18n = self._i18n
        count, people, pending = found if found else (None, None, None)
        if people is None:
            logging.warning("[channel-admins] followers unavailable")
        keep = self._selected()
        self._rows = ch.members(people or [], pending or [], self._me())
        self._list.Freeze()
        try:
            self._list.Set([self._row_text(m) for m in self._rows])
            index = next((i for i, m in enumerate(self._rows) if keep and m.jid == keep.jid), 0)
            if self._rows:
                self._list.SetSelection(index)
        finally:
            self._list.Thaw()
        self._update_buttons()
        if quiet:
            return
        if people is None:
            text = i18n.t("channel_people_unavailable")
        else:
            admins = count if count is not None else sum(m.role in ("owner", "admin") for m in self._rows)
            text = i18n.t("channel_people_count").format(admins=admins, count=len(self._rows))
        if not self._channel.owner:
            text = f"{text} {i18n.t('channel_admins_owner_only')}"
        self._announce(text)
        self.Layout()

    def _refused_while_busy(self) -> bool:
        if not self._busy:
            return False
        self._announce(self._i18n.t("loading"))
        return True

    # ── actions ───────────────────────────────────────────────────────────

    def _pick(self, title_key, ok_key):
        """The contact the user picked, or None."""
        i18n = self._i18n
        dialog = AttachContactDialog(self._mw, parent=self, title=i18n.t(title_key),
                                     ok_label=i18n.t(ok_key))
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return None
            return dialog.selected_contact
        finally:
            dialog.Destroy()

    def _target(self, entry):
        return ch.admin_target((entry or {}).get("remoteJid"),
                               getattr(self._mw, "_phone_to_lid", {}))

    def _confirm(self, key, name):
        i18n = self._i18n
        return wx.MessageBox(i18n.t(key).format(name=name), self.GetTitle(),
                             wx.YES_NO | wx.NO_DEFAULT, self) == wx.YES

    def _on_invite(self, event=None):
        if self._refused_while_busy() or not self._current() or not self._channel.owner:
            return
        entry = self._pick("channel_admin_invite_pick", "channel_admin_invite_button")
        user = self._target(entry)
        if user:
            self._invite_person(user, contact_name(entry, self._i18n))

    def _on_make_admin(self, event=None):
        member = self._selected()
        if (self._refused_while_busy() or not self._current()
                or "make_admin" not in ch.member_actions(member, self._channel.role)):
            return
        self._invite_person(member.jid, self._name(member))

    def _invite_person(self, user, name):
        i18n = self._i18n
        with wx.TextEntryDialog(self, i18n.t("channel_admin_invite_message"),
                                i18n.t("channel_admin_invite")) as ask:
            ask.SetMaxLength(ch.INVITE_MESSAGE_LIMIT)
            if ask.ShowModal() != wx.ID_OK:
                return
            message = ask.GetValue()

        def said(expiration):
            if not expiration:
                return i18n.t("channel_admin_invited").format(name=name)
            date = datetime.datetime.fromtimestamp(expiration).strftime("%x %X")
            return i18n.t("channel_admin_invited_until").format(name=name, date=date)

        self._send(lambda connection, jid: ch.invite_admin(connection, jid, user,
                                                           self._channel.name, message),
                   said, i18n.t("channel_admin_invite_failed"))

    def _member_action(self, action, confirm_key, work, done_key, failed_key, my_role=""):
        member = self._selected()
        if (self._refused_while_busy() or not self._current()
                or action not in ch.member_actions(member, self._channel.role, self._is_me(member))):
            return
        name = self._name(member)
        if not self._confirm(confirm_key, name):
            return
        i18n = self._i18n
        def done(_result):
            if my_role:
                self._set_my_role(my_role)
            return i18n.t(done_key).format(name=name)

        self._send(lambda connection, jid: work(connection, jid, member.jid), done,
                   i18n.t(failed_key))

    def _set_my_role(self, role):
        """This account's role after handing the channel over or leaving it as
        an admin: the window, the composer and the menu follow it."""
        self._channel = dataclasses.replace(self._channel, role=role)
        self.changed = True
        roles = self._mw.__dict__.setdefault("_channel_roles", {})
        roles[self._channel.jid] = role

    def _on_revoke(self, event=None):
        self._member_action("revoke", "channel_admin_revoke_confirm", ch.revoke_admin_invite,
                            "channel_admin_revoked", "channel_admin_revoke_failed")

    def _on_demote(self, event=None):
        self._member_action("demote", "channel_admin_demote_confirm", ch.demote_admin,
                            "channel_admin_demoted", "channel_admin_demote_failed")

    def _on_make_owner(self, event=None):
        self._member_action("make_owner", "channel_owner_make_confirm", ch.change_owner,
                            "channel_owner_made", "channel_owner_change_failed", my_role="admin")

    def _on_leave(self, event=None):
        self._member_action("leave", "channel_admin_leave_confirm", ch.demote_admin,
                            "channel_admin_left", "channel_admin_demote_failed", my_role="subscriber")

    def _send(self, work, done, failed_text):
        """*work(connection, channel jid)* off the wx thread; *done(result)*
        gives what is said when it worked. The list is read again after."""
        self._busy = True
        self._status.SetLabel(self._i18n.t("loading"))
        connection, jid = self._connection, self._channel.jid

        def run():
            return ("ok", work(connection, jid))

        def finish(outcome):
            self._busy = False
            if not outcome:
                logging.warning("[channel-admins] action refused")
                self._announce(failed_text)
                return
            self._announce(done(outcome[1]))
            # A role changed: read the people again, quietly (the result was said).
            self._load(quiet=True)

        self._run_async(run, finish)
