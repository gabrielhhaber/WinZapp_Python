"""AI summary of a conversation's text messages for ConversationsPanel.

Like WhatsApp's "Summarize unread messages": the conversations-list menu
summarizes a chat's unread messages without opening it or marking it as read,
and the message menu of an open chat offers its unread messages, the messages
from the focused one to the newest, and the selected ones. The provider, model
and key are the ones set up for message translation (core/message_summary.py).
"""
import wx

from app_paths import global_dir
from core.ai_credentials import CredentialError, CredentialStore, store_for_translation
from core.ai_media.errors import DescriptionError
from core.ai_media.service import RequestToken, submit
from core.message_copy_format import format_copied_message
from core.message_summary import (SUMMARY_SECONDS, fit_transcript, summarize, unread_fetch_limit,
                                  unread_messages)
from core.message_translation import credential_id, message_text, translation_model_ready
from ui.dialogs.translation_settings import LABELS


class SummaryActionsMixin:
    # ── Menu items ───────────────────────────────────────────────────────

    def _append_summary_items(self, menu, msg):
        """Message-menu items of the open chat: unread, from here to the end, selection."""
        t = self.main_window.i18n.t
        if self._summary_open_unread_messages():
            item = menu.Append(wx.ID_ANY, t("summary_unread_menu"))
            self.Bind(wx.EVT_MENU, lambda event: self._on_summarize_messages(
                self._summary_open_unread_messages()), item)
        if self._summary_messages_from(msg):
            item = menu.Append(wx.ID_ANY, t("summary_from_here_menu"))
            self.Bind(wx.EVT_MENU, lambda event: self._on_summarize_messages(
                self._summary_messages_from(msg)), item)
        if self.selected_messages:
            item = menu.Append(wx.ID_ANY, t("summary_selected_menu"))
            self.Bind(wx.EVT_MENU, lambda event: self._on_summarize_messages(
                self._summary_selected_messages()), item)

    def _append_unread_summary_item(self, menu, chat):
        """Conversations-list item for a chat with unread messages. It reads the
        stored messages only: the chat is neither opened nor marked as read."""
        if int(chat.get("unreadCount") or 0) <= 0 or not chat.get("remoteJid"):
            return
        item = menu.Append(wx.ID_ANY, self.main_window.i18n.t("summary_unread_menu"))
        self.Bind(wx.EVT_MENU, lambda event: self._on_summarize_unread_chat(chat), item)

    # ── Which messages ───────────────────────────────────────────────────

    def _summary_messages_from(self, msg):
        """Text messages from ``msg`` to the newest one, in list order."""
        key = (msg.get("key") or {}).get("id")
        rows = self._sorted_messages
        start = next((n for n, row in enumerate(rows)
                      if not self._is_separator(row) and (row.get("key") or {}).get("id") == key), None)
        if start is None:
            return []
        return [row for row in rows[start:] if not self._is_separator(row) and message_text(row)]

    def _summary_selected_messages(self):
        return [row for row in self._sorted_messages
                if not self._is_separator(row) and message_text(row)
                and (row.get("key") or {}).get("id") in self.selected_messages]

    def _summary_open_unread_messages(self):
        """Text messages below the open chat's unread separator, while it is there."""
        index = getattr(self, "_unread_sep_idx", -1)
        rows = self._sorted_messages
        if not 0 <= index < len(rows) or not self._is_separator(rows[index]):
            return []
        return [row for row in rows[index + 1:] if not self._is_separator(row) and message_text(row)]

    def _on_summarize_unread_chat(self, chat):
        """Summarize a chat's unread messages from the database, without opening it."""
        mw = self.main_window
        jid = chat.get("remoteJid", "")
        if not jid or self._translation_chat_hidden(jid):
            return
        unread = int(chat.get("unreadCount") or 0)
        try:
            stored = mw.db.get_messages(jid, limit=unread_fetch_limit(unread))
        except Exception:
            stored = []
        stored.reverse()  # newest-first from the database; oldest first here
        rows = [row for row in unread_messages(stored, unread) if message_text(row)]
        self._on_summarize_messages(rows, jid=jid, chat=chat)

    def _summary_sender(self, row, chat):
        """Who sent ``row``. For a private chat summarized from the list, the
        chat's own name: _sender_label falls back to the OPEN chat's name."""
        if (chat is not None and not row["key"].get("fromMe")
                and not row["key"].get("participant")
                and not chat.get("remoteJid", "").endswith("@g.us")):
            mw = self.main_window
            name = mw._resolve_contact_name(chat) or chat.get("name") or chat.get("pushName")
            if name:
                return name
        return self._sender_label(row)

    def _summary_lines(self, messages, chat=None):
        """One "<date> <time> - <sender>: <text>" line per message, as Copy selected writes them."""
        fmt = self.main_window.i18n.t("datetime_fmt")
        lines = []
        for row in messages:
            text = self._message_text_with_names(row) or message_text(row)
            if text:
                lines.append(format_copied_message(
                    self._extract_timestamp(row), self._summary_sender(row, chat), text, fmt))
        return lines

    # ── Request and result ───────────────────────────────────────────────

    def _on_summarize_messages(self, messages, *, jid=None, chat=None):
        """Summarize ``messages`` of the chat ``jid`` (the open chat when None)."""
        mw = self.main_window
        t = mw.i18n.t
        config = self._translation_settings()
        if not config.provider:
            mw.output(t("summary_not_configured"))
            return
        if not translation_model_ready(config):
            mw.output(t("translation_test_required"))
            return
        follows_open_chat = jid is None
        jid = (self.conversation or {}).get("remoteJid", "") if follows_open_chat else jid
        if not jid or self._translation_chat_hidden(jid):
            return
        transcript, kept, left_out = fit_transcript(self._summary_lines(messages, chat))
        if not kept:
            mw.output(t("summary_nothing"))
            return
        if getattr(self, "_summary_request", None) is not None:
            mw.output(t("ai_error_busy"))
            return
        provider = LABELS.get(config.provider, config.provider)
        # A locked chat's text leaves WhatsApp only with fresh consent, asked
        # each time, as translation does (translation_locked_confirm).
        if mw.is_chat_locked(jid):
            answer = wx.MessageBox(
                t("summary_locked_confirm").format(provider=provider), t("summary_title"),
                wx.YES_NO | wx.NO_DEFAULT, self)
            if answer != wx.YES or self._translation_chat_hidden(jid) or (
                    follows_open_chat and (self.conversation or {}).get("remoteJid") != jid):
                return
        token = self._summary_request = RequestToken(SUMMARY_SECONDS)
        language = mw.i18n.language

        def work():
            try:
                identity = credential_id(config)
                key = store_for_translation(CredentialStore(global_dir()), identity).get(identity)
            except CredentialError:
                raise DescriptionError("credentials") from None
            return summarize(config, key, transcript, language, token)

        def complete(answer, error):
            self._summary_request = None
            if (getattr(mw, "_shutting_down", False) or self._translation_chat_hidden(jid)
                    or (follows_open_chat and (self.conversation or {}).get("remoteJid") != jid)):
                return
            if error:
                mw.output(t(error))
                return
            note = t("summary_left_out").format(count=kept, left_out=left_out) if left_out else ""
            self._show_summary(answer, note=note, jid=jid)

        try:
            submit(work, lambda answer, error: wx.CallAfter(complete, answer, error))
        except DescriptionError as error:
            complete(None, error.key)
        else:
            mw.output(t("summary_sending").format(provider=provider, count=kept))

    def _show_summary(self, answer, *, note, jid):
        """Read-only window: the summary, a Copy button and OK."""
        t = self.main_window.i18n.t
        title = t("summary_title")
        dialog = wx.Dialog(self, title=title, size=(560, 380))
        text = wx.TextCtrl(dialog, value=f"{note}\n\n{answer}" if note else answer, name=title,
                           style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(text, 1, wx.EXPAND | wx.ALL, 8)
        copy_button = wx.Button(dialog, label=t("transcription_result_copy"))
        copy_button.Bind(wx.EVT_BUTTON, lambda event: self._copy_translation(answer))
        sizer.Add(copy_button, 0, wx.ALL, 8)
        sizer.Add(dialog.CreateSeparatedButtonSizer(wx.OK), 0, wx.EXPAND | wx.ALL, 8)
        dialog.SetSizer(sizer)
        text.SetFocus()
        self._summary_dialog = (dialog, jid)
        try:
            dialog.ShowModal()
        finally:
            self._summary_dialog = None
            dialog.Destroy()

    def close_locked_summary(self):
        """The vault closed: a summary of a locked chat leaves the screen."""
        dialog = getattr(self, "_summary_dialog", None)
        if dialog is not None and self.main_window.is_chat_locked(dialog[1]):
            dialog[0].EndModal(wx.ID_CANCEL)
