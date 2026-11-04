"""A channel's admins, opened from the Channels window (Alt+9) for a channel
this account administers: how many admins it has and, for its owner,
inviting a contact to be one (WhatsApp sends the invite to their chat, where
they accept it) or taking an invite back. WhatsApp Web has nothing more to
do with a channel's admins.

The requests are core/channels.py's; every one runs off the wx thread and a
late answer never touches a closed window (BackgroundWork).
"""

import logging

import wx

from core import channels as ch
from ui.dialogs.attach_contact_dialog import AttachContactDialog
from ui.dialogs.background_work import BackgroundWork


def contact_name(entry, i18n) -> str:
    """The picked contact as the window says it, never their JID."""
    name = str((entry or {}).get("name") or (entry or {}).get("pushName") or "").strip()
    return name or i18n.t("unknown_contact")


class ChannelAdminsDialog(BackgroundWork, wx.Dialog):
    def __init__(self, parent, main_window, channel):
        self._init_background(main_window)
        self._channel = channel
        i18n = self._i18n
        super().__init__(parent, title=i18n.t("channel_admins_title").format(
            name=channel.name or i18n.t("channels_unnamed")),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._status = wx.StaticText(self, label=i18n.t("loading"))
        sizer.Add(self._status, 0, wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)

        def button(key, handler, button_id=wx.ID_ANY):
            control = wx.Button(self, button_id, label=i18n.t(key))
            control.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(control, 0, wx.ALL, 4)
            return control

        # Only the owner invites and uninvites (WhatsApp Web refuses an admin).
        self._invite = button("channel_admin_invite", self._on_invite)
        self._revoke = button("channel_admin_revoke", self._on_revoke)
        self._invite.Enable(channel.owner)
        self._revoke.Enable(channel.owner)
        button("close", self._on_close, wx.ID_CANCEL)
        sizer.Add(buttons, 0, wx.ALL, 4)
        self.SetSizer(sizer)
        self.Fit()
        self.SetMinSize((420, -1))
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        (self._invite if channel.owner else self.FindWindowById(wx.ID_CANCEL)).SetFocus()
        self.CentreOnParent()
        wx.CallAfter(self._load_count)

    def _load_count(self):
        if not self._current():
            return
        connection, jid = self._connection, self._channel.jid
        self._run_async(lambda: ch.admin_count(connection, jid), self._finish_count)

    def _finish_count(self, count):
        i18n = self._i18n
        if count is None:
            logging.warning("[channel-admins] count unavailable")
            self._status.SetLabel(i18n.t("channel_admins_unavailable"))
        else:
            self._status.SetLabel(i18n.t("channel_admins_count").format(count=count))
        self.Layout()

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

    def _on_invite(self, event=None):
        if self._busy or not self._current() or not self._channel.owner:
            return
        i18n = self._i18n
        entry = self._pick("channel_admin_invite_pick", "channel_admin_invite_button")
        user = self._target(entry)
        if not user:
            return
        name = contact_name(entry, i18n)
        with wx.TextEntryDialog(self, i18n.t("channel_admin_invite_message"),
                                i18n.t("channel_admin_invite")) as ask:
            ask.SetMaxLength(ch.INVITE_MESSAGE_LIMIT)
            if ask.ShowModal() != wx.ID_OK:
                return
            message = ask.GetValue()
        self._send(lambda connection, jid: ch.invite_admin(connection, jid, user,
                                                           self._channel.name, message),
                   i18n.t("channel_admin_invited").format(name=name),
                   i18n.t("channel_admin_invite_failed"))

    def _on_revoke(self, event=None):
        if self._busy or not self._current() or not self._channel.owner:
            return
        i18n = self._i18n
        entry = self._pick("channel_admin_revoke_pick", "channel_admin_revoke_button")
        user = self._target(entry)
        if not user:
            return
        name = contact_name(entry, i18n)
        if wx.MessageBox(i18n.t("channel_admin_revoke_confirm").format(name=name),
                         i18n.t("channel_admin_revoke"), wx.YES_NO | wx.NO_DEFAULT, self) != wx.YES:
            return
        self._send(lambda connection, jid: ch.revoke_admin_invite(connection, jid, user),
                   i18n.t("channel_admin_revoked").format(name=name),
                   i18n.t("channel_admin_revoke_failed"))

    def _send(self, work, done_text, failed_text):
        self._busy = True
        self._status.SetLabel(self._i18n.t("loading"))
        connection, jid = self._connection, self._channel.jid

        def run():
            work(connection, jid)
            return True

        def finish(outcome):
            self._busy = False
            if not outcome:
                logging.warning("[channel-admins] action refused")
            self._announce(done_text if outcome else failed_text)
            self.Layout()

        self._run_async(run, finish)
