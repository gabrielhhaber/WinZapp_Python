"""GroupInvitationLinksMixin — part of MainWindow (see main_window/__init__.py).

A WhatsApp link that reaches WinZapp from outside its own windows: handed over
by Windows when WinZapp is registered for ``whatsapp:`` links
(core/whatsapp_link_association.py), either on this process's command line or
passed on by a second launch over IPC.

* A group invitation ends in the same join dialog a link in a message opens:
  preview first, nothing joined until the user confirms.
* A chat link (``whatsapp://send?phone=...&text=...``, what wa.me and
  api.whatsapp.com pages open) opens the chat with that number, even when it is
  not a contact, puts the text in the message field and focuses it. Nothing is
  sent: the user reviews the text and sends it.

Also the Settings switch that turns that registration on and off.

Methods run with ``self`` bound to the MainWindow instance.
"""

import logging
import sys
import threading
import time

import wx

from core import whatsapp_link_association
from core.group_invitation_link import invitation_code
from core.whatsapp_chat_link import chat_target


class GroupInvitationLinksMixin:
    """Group invitation links arriving from Windows."""

    #: How long a link waits for WhatsApp to be connected before giving up.
    #: A link that starts WinZapp arrives well before the session is up.
    _INVITATION_LINK_WAIT_SECONDS = 180
    _INVITATION_LINK_POLL_MS = 1000

    def open_whatsapp_link(self, link: str):
        """Open a link handed over from outside: a group invitation or a chat.

        Main thread only. Anything else is ignored."""
        if invitation_code(link):
            self.open_group_invitation_link(link)
        elif chat_target(link) is not None:
            self.open_chat_link(link)

    def _wait_for_whatsapp(self, retry, link: str, deadline, waiting_key: str,
                           timeout_key: str):
        """True when WhatsApp is connected. Otherwise schedules *retry* (and
        says so once) and returns False; gives up with *timeout_key* after the
        wait."""
        if getattr(self, "_wa_connected", False):
            return True
        now = time.monotonic()
        if deadline is None:
            deadline = now + self._INVITATION_LINK_WAIT_SECONDS
            self.output(self.i18n.t(waiting_key))
        elif now >= deadline:
            self.output(self.i18n.t(timeout_key))
            return False
        wx.CallLater(self._INVITATION_LINK_POLL_MS, retry, link, deadline)
        return False

    def open_chat_link(self, link: str, _deadline: float = None):
        """Open the chat of a chat link and leave its text ready to send, once
        WhatsApp is connected. Main thread only."""
        target = chat_target(link)
        if target is None or getattr(self, "_shutting_down", False):
            return
        if not self._wait_for_whatsapp(self.open_chat_link, link, _deadline,
                                       "chat_link_waiting", "chat_link_not_connected"):
            return
        phone, text = target
        try:
            self.restore_window()
        except Exception:
            logging.exception("[chat-link] could not restore the window")

        def lookup():
            # A network call: off the GUI thread. When the engine cannot say,
            # the typed number is used, as in the New conversation dialog.
            from core.number_lookup import resolve_typed_number
            jid = phone + "@s.whatsapp.net"
            try:
                jid = resolve_typed_number(self, phone) or jid
            except Exception:
                logging.info("[chat-link] number lookup failed")
            wx.CallAfter(self._finish_chat_link, jid, text)

        threading.Thread(target=lookup, daemon=True, name="chat-link-lookup").start()

    def _finish_chat_link(self, jid: str, text: str):
        """Navigate to the chat of *jid* (created when new) and fill the
        message field with *text*, focused, unsent."""
        if getattr(self, "_shutting_down", False):
            return
        try:
            from core.utils import format_number
            from ui.dialogs.new_conversation import NewConversationDialog
            norm_jid, chat = NewConversationDialog._find_existing_chat(self, jid)
            if chat is None:
                chat = {"remoteJid": norm_jid, "pushName": format_number(norm_jid)}
                self.chats[norm_jid] = chat
                self._schedule_set_chats()
            panel = self.conversations_panel
            panel.navigate_to_conversation(chat)
            # After the navigation settles: opening a chat resets the field.
            wx.CallAfter(self._fill_message_field, text)
        except Exception:
            logging.exception("[chat-link] could not open the chat")

    def _fill_message_field(self, text: str):
        field = self.conversations_panel.message_field
        if text:
            current = field.GetValue()
            field.SetValue(f"{current} {text}" if current.strip() else text)
            field.SetInsertionPointEnd()
        field.SetFocus()

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
        if not self._wait_for_whatsapp(self.open_group_invitation_link, link,
                                       _deadline, "group_join_link_waiting",
                                       "group_join_link_not_connected"):
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

    def _register_whatsapp_links_for_install(self) -> None:
        """Once, on an installed build: register WinZapp for whatsapp: links so
        it is listed in Windows' Default apps. The choice of default stays
        Windows' and the user's; the Settings check box undoes it, and it is
        never re-applied after that."""
        if sys.platform != "win32":
            return
        try:
            if not whatsapp_link_association.is_installed_build():
                return
            from app_settings import AppSettings
            settings = AppSettings(self.global_dir)
            if settings.get("whatsapp_links_auto_registered"):
                return
            settings.set("whatsapp_links_auto_registered", True)
            whatsapp_link_association.register()
        except Exception as exc:
            logging.warning("[invitation-link] automatic registration failed: %s",
                            type(exc).__name__)

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
