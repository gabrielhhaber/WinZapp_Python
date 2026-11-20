"""The "Starred messages in this chat" button of an open conversation.

Next to "Pinned messages": it opens the starred messages window (Alt+0,
ui/dialogs/starred_messages.py) with only the open chat's messages, read from
the local database like the window of every chat. Choosing one moves the
focus to it in the conversation; "Remove star" works as it does there.

Tested in test_conversation_starred.py.
"""

import wx


class ConversationStarredMixin:
    def _init_conversation_starred_button(self, sizer):
        self._conversation_starred_btn = wx.Button(
            self.conversation_panel,
            label=self.main_window.i18n.t("conversation_starred_button"),
        )
        self._conversation_starred_btn.Bind(wx.EVT_BUTTON, self._on_show_conversation_starred)
        sizer.Add(self._conversation_starred_btn, 0, wx.LEFT | wx.TOP | wx.BOTTOM, 5)

    def _refresh_conversation_starred_label(self):
        button = getattr(self, "_conversation_starred_btn", None)
        if button is not None:
            button.SetLabel(self.main_window.i18n.t("conversation_starred_button"))

    def _on_show_conversation_starred(self, event=None):
        """The open chat's starred messages; nothing when no chat is open."""
        jid = (self.conversation or {}).get("remoteJid", "")
        if not jid:
            return False
        from ui.dialogs.starred_messages import show_starred_messages
        show_starred_messages(self.main_window, self.main_window, jid)
        return True
