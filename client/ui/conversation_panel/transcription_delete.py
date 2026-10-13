"""TranscriptionDeleteMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Deleting saved local transcriptions from the keyboard. Until now the only way
was the message's context menu ("Delete transcription"), one message at a time.

* **Alt+Shift+Delete** deletes the saved transcription of the focused message
  (the same question and the same answer as the menu item). With messages
  selected and "Substituir atalhos por ações em massa" on, it acts on the
  selection, like every other single-message key.
* **Ctrl+Alt+Shift+Delete** and the "Ações em massa" item delete the saved
  transcriptions of every selected message, after one question.

Deleting is MainWindow.delete_message_transcription() in both cases: the
database first, memory after, a dated tombstone rather than a removed key. A
message with nothing saved is never touched and never counted.

Methods run with ``self`` bound to the ConversationsPanel instance.
"""

import wx

from core.transcription import stored as stored_transcription


class TranscriptionDeleteMixin:
    """The keys and the bulk action that delete saved transcriptions."""

    @staticmethod
    def _holds_transcription(msg) -> bool:
        """Whether a saved transcription, of any model, is on the message."""
        if not isinstance(msg, dict):
            return False
        return bool(stored_transcription.results_of(
            msg.get(stored_transcription.TRANSCRIPTION_KEY)))

    def _on_accel_delete_transcription(self, event):
        """Alt+Shift+Delete on the focused message — or, with messages selected
        and "Substituir atalhos por ações em massa" on, on every selected one."""
        if self._bulk_shortcuts_enabled() and self.selected_messages:
            self._on_mass_delete_transcriptions(event)
            return
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        if not self._holds_transcription(msg):
            # Never a silent no-op: the key was pressed and nothing changed.
            self.main_window.output(
                self.main_window.i18n.t("transcription_none_to_delete"), interrupt=True)
            return
        self._on_menu_delete_transcription(msg)

    def _on_accel_bulk_delete_transcriptions(self, event):
        """Ctrl+Alt+Shift+Delete: the saved transcriptions of the selection."""
        self._run_bulk_message_action(self._on_mass_delete_transcriptions, event)

    def _on_mass_delete_transcriptions(self, event=None):
        """Delete the saved transcription of every selected message that has
        one, after one question that says how many."""
        i18n = self.main_window.i18n
        selected = set(self.selected_messages)
        targets = [
            msg for msg in self._sorted_messages
            if not self._is_separator(msg)
            and (msg.get("key") or {}).get("id") in selected
            and self._holds_transcription(msg)
        ]
        if not targets:
            self.main_window.output(i18n.t("transcription_bulk_delete_none"), interrupt=True)
            return
        dlg = wx.MessageDialog(
            self.main_window,
            i18n.t("transcription_bulk_delete_question").format(count=len(targets)),
            i18n.t("transcription_progress_title"),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        try:
            answer = dlg.ShowModal()
        finally:
            dlg.Destroy()
        if answer != wx.ID_YES:
            return
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        ids = [(msg.get("key") or {}).get("id", "") for msg in targets]
        tally = {"left": len(ids), "deleted": 0}

        def _one_done(ok):
            # On the wx thread, once per message, in whatever order the
            # database answers.
            tally["left"] -= 1
            if ok:
                tally["deleted"] += 1
            if tally["left"] == 0:
                self._finish_bulk_transcription_delete(jid, ids, tally["deleted"])

        for msg_id in ids:
            self.main_window.delete_message_transcription(jid, msg_id, _one_done)

    def _finish_bulk_transcription_delete(self, jid, ids, deleted):
        """Redraw the rows that carried a text and say how it went."""
        i18n = self.main_window.i18n
        conversation = self.conversation if isinstance(self.conversation, dict) else {}
        if conversation.get("remoteJid", "") == jid:
            # Only in the conversation the messages belong to: an answer that
            # arrives after the user moved on must not rebuild what they read.
            repaint = getattr(self, "_repaint_or_repopulate", None)
            if repaint is not None:
                repaint(ids)
        failed = len(ids) - deleted
        if failed:
            self.main_window.output(i18n.t("transcription_bulk_delete_partial").format(
                count=deleted, failed=failed), interrupt=True)
        else:
            self.main_window.output(
                i18n.t("transcription_bulk_deleted").format(count=deleted), interrupt=True)
