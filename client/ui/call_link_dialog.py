"""Calls (Alt+6) > "Create call link": WhatsApp's "New call link".

Choose voice or video, create the link, then copy it, send it to a
conversation or join its call -- the same three things WhatsApp Web's dialog
offers. The link is created only when asked (a link is a lasting invitation:
it is not made just by opening the window), and choosing the other kind after
creating one makes a new link on the next "Create".
"""

import logging
import threading

import pyperclip
import wx

from core.call_links import parse_call_link


def send_targets(main_window) -> tuple:
    """(chats, names) a link can be sent to: the forward dialog's targets
    (archived chats and saved contacts included, locked chats left out)."""
    from ui.conversation_panel.forwarding import ForwardingMixin
    chats, names = ForwardingMixin._forward_target_chats(main_window)
    rows = [(chat, name) for chat, name in zip(chats, names)
            if chat.get("remoteJid") and not str(chat["remoteJid"]).endswith(("@broadcast", "@newsletter"))]
    return [chat for chat, _ in rows], [name for _, name in rows]


def sent_ok(result) -> bool:
    """send_text_message() answers the new message's id, or a dict whose
    "ok" says how it went."""
    if isinstance(result, dict):
        return result.get("ok") is True
    return isinstance(result, str) and bool(result)


class CallLinkDialog(wx.Dialog):
    def __init__(self, parent, main_window):
        self.main_window = main_window
        t = main_window.i18n.t
        super().__init__(parent, title=t("call_link_title"), style=wx.DEFAULT_DIALOG_STYLE)
        self._link = ""
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.kind = wx.RadioBox(panel, label=t("call_link_kind_label"),
                                choices=[t("call_link_kind_voice"), t("call_link_kind_video")],
                                majorDimension=1, style=wx.RA_SPECIFY_ROWS)
        sizer.Add(self.kind, 0, wx.EXPAND | wx.ALL, 8)
        self.create_button = wx.Button(panel, label=t("call_link_create_button"))
        self.create_button.Bind(wx.EVT_BUTTON, self._on_create)
        sizer.Add(self.create_button, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        sizer.Add(wx.StaticText(panel, label=t("call_link_field_label")), 0, wx.LEFT | wx.RIGHT, 8)
        self.link_field = wx.TextCtrl(panel, style=wx.TE_READONLY, size=(420, -1))
        sizer.Add(self.link_field, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(wx.StaticText(panel, label=t("call_link_explanation")), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.copy_button = wx.Button(panel, label=t("call_link_copy_button"))
        self.send_button = wx.Button(panel, label=t("call_link_send_button"))
        self.join_button = wx.Button(panel, label=t("call_link_join_button"))
        close_button = wx.Button(panel, wx.ID_CANCEL, label=t("close"))
        for button in (self.copy_button, self.send_button, self.join_button, close_button):
            buttons.Add(button, 0, wx.ALL, 4)
        self.copy_button.Bind(wx.EVT_BUTTON, self._on_copy)
        self.send_button.Bind(wx.EVT_BUTTON, self._on_send)
        self.join_button.Bind(wx.EVT_BUTTON, self._on_join)
        sizer.Add(buttons, 0, wx.ALL, 4)
        panel.SetSizer(sizer)
        sizer.Fit(self)
        self.SetEscapeId(wx.ID_CANCEL)
        self._set_link("")
        self.kind.SetFocus()

    def _set_link(self, link: str):
        self._link = link
        self.link_field.SetValue(link)
        for button in (self.copy_button, self.send_button, self.join_button):
            button.Enable(bool(link))

    def _is_video(self) -> bool:
        return self.kind.GetSelection() == 1

    def _on_create(self, _event=None):
        mw = self.main_window
        self.create_button.Disable()
        mw.output(mw.i18n.t("call_link_creating"), interrupt=True)

        def done(link, error):
            if not self:
                return
            self.create_button.Enable()
            if not link:
                mw.output(mw.i18n.t("call_link_create_failed").format(error=error), interrupt=True)
                return
            self._set_link(link)
            is_video, _ = parse_call_link(link)
            mw.output(mw.i18n.t("call_link_created_video" if is_video else "call_link_created_voice"),
                      interrupt=True)
            self.link_field.SetFocus()

        mw.create_call_link(self._is_video(), done)

    def _on_copy(self, _event=None):
        mw = self.main_window
        try:
            pyperclip.copy(self._link)
        except Exception:
            mw.output(mw.i18n.t("msg_copy_error"), interrupt=True)
            return
        mw.output(mw.i18n.t("call_link_copied"), interrupt=True)

    def _on_send(self, _event=None):
        mw = self.main_window
        t = mw.i18n.t
        chats, names = send_targets(mw)
        if not chats:
            mw.output(t("call_link_no_conversations"), interrupt=True)
            return
        with wx.SingleChoiceDialog(self, t("call_link_send_prompt"), t("call_link_send_title"), names) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            index = dialog.GetSelection()
        if not 0 <= index < len(chats):
            return
        jid, name, link = chats[index]["remoteJid"], names[index], self._link

        def work():
            ok = False
            try:
                ok = sent_ok(mw.send_text_message(jid, link))
            except Exception:
                logging.exception("[call_link] sending the link failed")
            wx.CallAfter(mw.output, t("call_link_sent" if ok else "call_link_send_failed").format(name=name), True)

        threading.Thread(target=work, daemon=True, name="call-link-send").start()

    def _on_join(self, _event=None):
        link = self._link
        self.EndModal(wx.ID_OK)
        self.main_window.join_call_link(link)
