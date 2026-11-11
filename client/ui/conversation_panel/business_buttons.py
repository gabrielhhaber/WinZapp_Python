"""BusinessButtonsMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

The buttons of a business's message (core/business_buttons.py): one real
button each, under the message list, in the order of Tab. A reply is sent the
way WhatsApp Web sends it, a link opens, a code is copied and a number is
spoken.
"""

import threading

import wx

from core.business_buttons import (CALL, COPY, REPLY, URL, ButtonReplyError,
                                   business_button_label, reply_to_button)
from core.group_invitation import connection_identity
from core.business_message_content import message_business_buttons


class BusinessButtonsMixin:
    def _show_business_buttons(self, msg: dict):
        """One button for each button of the selected message, in the
        container the legacy reply buttons use."""
        buttons = message_business_buttons(msg)
        context = (self.conversation, connection_identity(self.main_window), object())
        self._business_context = context
        self._buttons_container.DestroyChildren()
        sizer = wx.WrapSizer(wx.HORIZONTAL)
        for button in buttons:
            label = business_button_label(msg, button).replace("&", "&&")
            control = wx.Button(self._buttons_container, label=label)
            control.Bind(wx.EVT_BUTTON, lambda e, b=button, m=msg, c=context: self._on_business_button(b, m, c))
            sizer.Add(control, 0, wx.ALL, 4)
        self._buttons_container.SetSizer(sizer, True)
        self._buttons_container.Layout()
        self._buttons_container.Show()
        self.conversation_panel.Layout()

    def _on_business_button(self, button: dict, msg: dict, context=None):
        mw = self.main_window
        if context is not None and (context is not getattr(self, "_business_context", None)
                or self.conversation is not context[0] or connection_identity(mw) != context[1]):
            return
        i18n = mw.i18n
        kind = button.get("kind")
        if kind == URL:
            self._activate_link(button["value"])
        elif kind == COPY:
            self._copy_business_text(button["value"])
        elif kind == CALL:
            mw.output(i18n.t("business_button_call").format(number=button["value"]), interrupt=True)
        elif kind == REPLY:
            self._send_business_reply(button, msg)

    def _copy_business_text(self, text: str):
        mw = self.main_window
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(text))
                wx.TheClipboard.Flush()
            finally:
                wx.TheClipboard.Close()
            mw.output(mw.i18n.t("business_button_copied"), interrupt=True)
        else:
            mw.output(mw.i18n.t("msg_copy_error"), interrupt=True)

    def _send_business_reply(self, button: dict, msg: dict):
        """Once per tap: a second tap while the first is on its way is
        ignored, and a failure is never retried by itself."""
        if getattr(self, "_business_sending", False):
            return
        mw = self.main_window
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        message_id = mw._serialize_msg_id(jid, msg.get("key") or {}, msg)
        connection = connection_identity(mw)
        self._business_sending = True
        pending = object()
        self._business_pending = pending
        context = getattr(self, "_business_context", None)

        def run():
            try:
                reply_to_button(connection, message_id, button)
                sent = True
            except ButtonReplyError:
                sent = False
            wx.CallAfter(self._business_reply_done, sent, pending, connection, context)
        threading.Thread(target=run, daemon=True).start()

    def _business_reply_done(self, sent: bool, pending=None, connection=None, context=None):
        if pending is not None and pending is not getattr(self, "_business_pending", None):
            return
        self._business_sending = False
        mw = self.main_window
        if connection is not None and (connection_identity(mw) != connection
                or context is not getattr(self, "_business_context", None)
                or context is not None and self.conversation is not context[0]):
            return
        mw.output(mw.i18n.t("business_button_sent" if sent else "business_button_failed"), interrupt=True)
