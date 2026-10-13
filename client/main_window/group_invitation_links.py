"""GroupInvitationLinksMixin — part of MainWindow (see main_window/__init__.py).

A group invitation link that reaches WinZapp from outside its own windows:
handed over by Windows when WinZapp is registered for ``whatsapp:`` links
(core/whatsapp_link_association.py), either on this process's command line or
passed on by a second launch over IPC. It ends in the same join dialog a link
in a message opens: preview first, nothing joined until the user confirms.

Also the Settings switch that turns that registration on and off.

Methods run with ``self`` bound to the MainWindow instance.
"""

import logging
import time

import wx

from core import whatsapp_link_association
from core.group_invitation_link import invitation_code


class GroupInvitationLinksMixin:
    """Group invitation links arriving from Windows."""

    #: How long a link waits for WhatsApp to be connected before giving up.
    #: A link that starts WinZapp arrives well before the session is up.
    _INVITATION_LINK_WAIT_SECONDS = 180
    _INVITATION_LINK_POLL_MS = 1000

    def open_group_invitation_link(self, link: str, _deadline: float = None):
        """Show the join dialog for *link*, once WhatsApp is connected.

        Main thread only. Anything that is not a group invitation link is
        ignored: this is reached with text that came from outside the process.
        """
        if not invitation_code(link):
            return
        if getattr(self, "_shutting_down", False):
            return
        if getattr(self, "_invitation_link_dialog_open", False):
            return  # one join dialog at a time; a second link would stack on it
        if not getattr(self, "_wa_connected", False):
            now = time.monotonic()
            if _deadline is None:
                _deadline = now + self._INVITATION_LINK_WAIT_SECONDS
                self.output(self.i18n.t("group_join_link_waiting"))
            elif now >= _deadline:
                self.output(self.i18n.t("group_join_link_not_connected"))
                return
            wx.CallLater(self._INVITATION_LINK_POLL_MS,
                         self.open_group_invitation_link, link, _deadline)
            return
        from ui.dialogs.group_join_dialog import join_group_by_link
        self._invitation_link_dialog_open = True
        try:
            # The window may be in the tray: the dialog needs a visible parent.
            self.restore_window()
            join_group_by_link(self, self, link)
        except Exception:
            logging.exception("[invitation-link] could not show the join dialog")
        finally:
            self._invitation_link_dialog_open = False

    def _apply_whatsapp_link_association(self, enable: bool) -> bool:
        """Register or unregister WinZapp for whatsapp: links (Settings).

        Returns whether the registry now matches *enable*. Says what happened:
        on success, that Windows may still ask which app to use, because the
        choice of default is Windows' and the user's, never an app's.
        """
        try:
            if enable:
                whatsapp_link_association.register()
            else:
                whatsapp_link_association.unregister()
        except Exception as exc:
            logging.warning("[invitation-link] association change failed: %s",
                            type(exc).__name__)
            wx.MessageBox(
                self.i18n.t("whatsapp_links_error_message"),
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_ERROR,
            )
            return False
        if enable:
            wx.MessageBox(
                self.i18n.t("whatsapp_links_enabled_message"),
                self.app_name,
                wx.OK | wx.ICON_INFORMATION,
            )
        return True
