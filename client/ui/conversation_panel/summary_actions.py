"""SummaryActionsMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

AI summary of chat messages, like WhatsApp's "Summarize unread messages", with
the providers the person set up under Settings > Online AI (core/ai_media) and
the accessible result window of a description (AIResultDialog, kind "text":
follow-up questions, Copy, consent of its own).

Where it is offered, all ending in ``_on_summarize_messages``:

* Ctrl+Shift+U of the open chat, and a button beside it once there are
  ``BUTTON_MIN_UNREAD`` or more: its unread messages;
* the conversations list's context menu: a chat's unread messages, read from
  the database — the chat is neither opened nor marked as read;
* the message context menu of the open chat: unread, from this message to the
  newest, and the selected ones; the last two also have a key each
  (Alt+Shift+I, Ctrl+Alt+Shift+I), shown on the item.

Only the button has a threshold; everything else is offered whenever there is
something to summarize and Online AI can take text. A locked chat can be summarized too, with a
consent asked every time and never remembered. The consent comes first, before any
voice message is transcribed (``_summary_consent``); AIResultDialog is told it was
given. Voice messages carry their transcription (summary_voice.py). Nothing here
logs the messages.

Methods run with ``self`` bound to the ConversationsPanel instance.
"""
import wx

from app_paths import active_account_id, global_dir
from core.ai_credentials import CredentialStore
from core.ai_media import config as ai_config
from core.ai_media.summary import (MAX_VOICE_TRANSCRIPTIONS, build_unread_transcript, button_offered,
                                   is_voice_message, message_text,
                                   unread_fetch_limit, unread_messages, voice_text)
from core.ai_media.summary_voice_choice import MODE_LOCAL, MODE_ONLINE
from core.message_copy_format import format_copied_message
from core.utils import format_number, is_phone_like
from ui.shortcut_bindings import popup_item_label


class SummaryActionsMixin:
    # ── Is it offered ────────────────────────────────────────────────────

    def _summary_ready(self, config) -> bool:
        """Online AI is on and a provider with a saved key takes text."""
        if not config.get("enabled"):
            return False
        saved = CredentialStore(global_dir()).saved()
        return bool(ai_config.chain(config, "text", lambda p: p in saved))

    def _summary_unread_messages(self) -> list:
        """The still-unread messages of the open conversation, oldest first.

        Not the conversation dict's unreadCount: mark_conversation_as_read()
        zeroes it as soon as the chat opens. The panel's unread separator keeps
        the real answer: it is placed from the count snapshotted at open and
        moved as messages arrive. Once focus stepped past it
        (``_sep_anchors_read_position``) those messages were read: nothing is
        unread then, though the row stays as a marker.
        """
        if not self.conversation or getattr(self, "_sep_anchors_read_position", False):
            return []
        messages = getattr(self, "_sorted_messages", None) or []
        index = getattr(self, "_unread_sep_idx", -1)
        if 0 <= index < len(messages) and self._is_separator(messages[index]):
            start = index + 1
        else:
            # No separator row at the recorded index (mid-rebuild): fall back
            # to the anchor id. The anchor may be the first loaded row, whose
            # valid start index 0 must not be taken for a missing separator.
            start = None
            anchor = getattr(self, "_first_unread_msg_id", None)
            if anchor:
                for idx, msg in enumerate(messages):
                    if (isinstance(msg, dict) and not self._is_separator(msg)
                            and (msg.get("key") or {}).get("id") == anchor):
                        start = idx
                        break
            if start is None:
                return []
        return [row for row in messages[start:]
                if isinstance(row, dict) and not self._is_separator(row) and message_text(row)]

    def _summary_offered(self, config=None) -> bool:
        """The button and Ctrl+Shift+U share this rule: a conversation open and
        showing, unread messages to summarize, and Online AI able to take text.
        Cheapest first, because every incoming message re-evaluates the
        button: the unread count (memory), then the settings, and the
        credential store last."""
        if not self.conversation or not self._summary_unread_messages():
            return False
        if self.main_window.is_chat_hidden_by_vault(self.conversation.get("remoteJid", "")):
            return False
        if config is None:
            _app, config = self._ai_settings()
        return self._summary_ready(config)

    def _update_summary_button(self):
        button = getattr(self, "_summary_btn", None)
        if button is None:
            return
        try:
            offered = (self._summary_offered()
                       and button_offered(len(self._summary_unread_messages())))
        except Exception:
            offered = False
        if offered:
            # Kept in sync here (not only at construction) so a language
            # change with a chat open renames the button too.
            button.SetLabel(self.main_window.i18n.t("ai_summary_button"))
            button.Show()
        else:
            button.Hide()
        # The slot's visibility is decided by _sync_media_action_slot_visibility()
        # (the summary button is one of its controls); a bare Layout() here
        # left the slot hidden when the summary was the only action offered.
        sync = getattr(self, "_sync_media_action_slot_visibility", None)
        if sync is not None:
            sync()

    # ── Menu items ───────────────────────────────────────────────────────

    def _append_summary_items(self, menu, msg):
        """Message-menu items of the open chat: unread, from here to the end, selection."""
        unread = self._summary_unread_messages()
        from_here = self._summary_messages_from(msg)
        selected = self._summary_selected_messages()
        if not (unread or from_here or selected):
            return
        _app, config = self._ai_settings()
        if not self._summary_ready(config):
            return
        t = self.main_window.i18n.t
        for rows, key, again, shortcut_id in (
                (unread, "ai_summary_unread_menu", self._summary_unread_messages,
                 "messages.ID_SUMMARIZE_UNREAD"),
                (from_here, "ai_summary_from_here_menu", lambda: self._summary_messages_from(msg),
                 "messages.ID_SUMMARIZE_FROM_HERE"),
                (selected, "ai_summary_selected_menu", self._summary_selected_messages,
                 "messages.ID_SUMMARIZE_SELECTED")):
            if rows:
                item = menu.Append(wx.ID_ANY, popup_item_label(self, t(key), shortcut_id))
                # Read again when chosen: the list moves while a menu is open.
                self.Bind(wx.EVT_MENU, lambda event, again=again: self._on_summarize_messages(
                    again(), follows_open_chat=True), item)

    def _on_summary_from_here_shortcut(self, event=None):
        """Alt+Shift+I: the "from here to the end" menu item, for the focused
        message. Nothing focused (or only the unread separator) says so, as the
        menu item would not have been offered."""
        index = self.messages_list.GetFirstSelected()
        rows = self._sorted_messages
        msg = rows[index] if 0 <= index < len(rows) and not self._is_separator(rows[index]) else None
        self._on_summarize_messages(self._summary_messages_from(msg) if msg else [],
                                    follows_open_chat=True)

    def _on_summary_selected_shortcut(self, event=None):
        """Ctrl+Alt+Shift+I: the "selected messages" menu item. Without a
        selection _on_summarize_messages speaks ai_summary_nothing."""
        self._on_summarize_messages(self._summary_selected_messages(), follows_open_chat=True)

    def _append_unread_summary_item(self, menu, chat):
        """Conversations-list item for a chat with unread messages. It reads the
        stored messages only: the chat is neither opened nor marked as read."""
        jid = chat.get("remoteJid")
        if int(chat.get("unreadCount") or 0) <= 0 or not jid:
            return
        if self.main_window.is_chat_hidden_by_vault(jid):
            return
        _app, config = self._ai_settings()
        if not self._summary_ready(config):
            return
        item = menu.Append(wx.ID_ANY, self.main_window.i18n.t("ai_summary_unread_menu"))
        self.Bind(wx.EVT_MENU, lambda event: self._on_summarize_unread_chat(chat), item)

    # ── Which messages ───────────────────────────────────────────────────

    def _summary_messages_from(self, msg):
        """Usable messages from ``msg`` to the newest one, in list order."""
        key = (msg.get("key") or {}).get("id")
        rows = self._sorted_messages
        start = next((n for n, row in enumerate(rows)
                      if not self._is_separator(row) and (row.get("key") or {}).get("id") == key), None)
        if start is None:
            return []
        return [row for row in rows[start:] if not self._is_separator(row) and message_text(row)]

    def _summary_selected_messages(self):
        if not self.selected_messages:
            return []
        return [row for row in self._sorted_messages
                if not self._is_separator(row) and message_text(row)
                and (row.get("key") or {}).get("id") in self.selected_messages]

    def _on_summarize_unread_chat(self, chat):
        """Summarize a chat's unread messages from the database, without opening it."""
        mw = self.main_window
        jid = chat.get("remoteJid", "")
        if not jid or mw.is_chat_hidden_by_vault(jid):
            return
        unread = int(chat.get("unreadCount") or 0)
        try:
            stored = mw.db.get_messages(jid, limit=unread_fetch_limit(unread))
        except Exception:
            stored = []
        stored.reverse()  # newest-first from the database; oldest first here
        rows = [row for row in unread_messages(stored, unread) if message_text(row)]
        self._on_summarize_messages(rows, jid=jid, chat=chat)

    # ── The lines ────────────────────────────────────────────────────────

    def _summary_sender(self, row, chat):
        """Who sent ``row``. For a private chat summarized from the list, never
        _sender_label: without a participant it falls back to the OPEN chat's
        name, and the summary would put words in another person's mouth."""
        key = row.get("key") or {}
        if (chat is None or key.get("fromMe") or key.get("participant")
                or chat.get("remoteJid", "").endswith("@g.us")):
            return self._sender_label(row)
        mw = self.main_window
        name = (mw._resolve_contact_name(chat) or chat.get("name") or chat.get("pushName")
                or mw.find_name_through_messages(chat))
        if name:
            return name
        push = row.get("pushName") or ""
        if push and not is_phone_like(push):
            return push
        jid = chat.get("remoteJid", "")
        if jid.endswith("@lid"):
            jid = getattr(mw, "_lid_to_phone", {}).get(jid, "")
        return format_number(jid) if jid and not jid.endswith("@lid") else ""

    def _summary_text_of(self, row, voice_texts):
        """What ``row`` says: a voice message's transcription (or its marker),
        a text with @mentions as names, else the media placeholder/caption."""
        if is_voice_message(row):
            return voice_text(voice_texts.get((row.get("key") or {}).get("id", "")))
        named = " ".join((self._message_text_with_names(row) or "").split())
        return named or message_text(row)

    # ── Request and result ───────────────────────────────────────────────

    def _on_summary_action(self, event=None):
        """Button and Ctrl+Shift+U: summarize the unread messages of this chat,
        or offer to cancel the voice transcription under way."""
        mw = self.main_window
        if self._summary_voice_running():
            return  # Its progress window is open; Cancel is there.
        if not self._summary_offered():
            mw.output(mw.i18n.t("ai_summary_not_available"), interrupt=True)
            return
        self._on_summarize_messages(self._summary_unread_messages(), follows_open_chat=True)

    def _on_summarize_messages(self, messages, *, jid=None, chat=None, follows_open_chat=None):
        """Summarize ``messages`` of the chat ``jid`` (the open chat when None;
        ``chat`` is its list entry when summarized from the conversations list)."""
        mw = self.main_window
        t = mw.i18n.t
        if self._summary_voice_running():
            return
        if follows_open_chat is None:
            follows_open_chat = jid is None
        jid = (self.conversation or {}).get("remoteJid", "") if jid is None else jid
        if not jid or mw.is_chat_hidden_by_vault(jid):
            return
        existing = getattr(self, "_ai_dialog", None)
        if existing is not None:
            existing.Raise()
            return  # No second window or duplicate billed request.
        app, config = self._ai_settings()
        if not self._summary_ready(config):
            mw.output(t("ai_summary_not_available"))
            return
        messages = [row for row in messages if message_text(row)]
        if not messages:
            mw.output(t("ai_summary_nothing"))
            return
        _outcome, to_run = self._summary_voice_plan(messages)
        choice = None
        if to_run:
            # How the voice messages are handled, before anything is asked
            # about sending: the dialog sends and transcribes nothing.
            choice = self._summary_voice_decide(app, config, jid, len(to_run))
            if choice is None:
                if not (getattr(mw, "_shutting_down", False) or mw.is_chat_hidden_by_vault(jid)):
                    mw.output(t("ai_summary_cancelled"), interrupt=True)
                return
            # Another local transcription running refuses before any consent
            # question: asking for a summary that cannot start would be noise.
            if choice.mode == MODE_LOCAL and self._summary_voice_blocked(to_run):
                return
        # Consent first, before any voice message is transcribed: declining
        # leaves nothing transcribed and nothing sent.
        if not self._summary_consent(app, config, jid) or (
                choice is not None and choice.mode == MODE_ONLINE
                and not self._summary_voice_online_consent(app, config, jid, choice, len(to_run))):
            if not (getattr(mw, "_shutting_down", False) or mw.is_chat_hidden_by_vault(jid)):
                mw.output(t("ai_summary_cancelled"), interrupt=True)  # the person said no
            return
        self._summary_voice_step(
            messages, jid,
            lambda voice: self._open_summary(messages, jid, chat, voice, follows_open_chat),
            follows_open_chat, choice=choice)

    def _summary_consent(self, app, config, jid) -> bool:
        """Ask to send the messages' text to the providers that would take it
        (once, remembered, for a common chat; every time for a locked one);
        whether the person agreed. The result window does not ask again."""
        mw = self.main_window
        saved = CredentialStore(global_dir()).saved()
        providers = ai_config.chain(config, "text", lambda p: p in saved)
        from ui.dialogs.ai_result_dialog import ask_consent
        def track(dialog):
            self._summary_consent_dialog = dialog
            self._summary_consent_jid = jid
        return ask_consent(
            self, mw.i18n, app, config, providers, "text", mw.is_chat_locked(jid),
            valid=lambda: not getattr(mw, "_shutting_down", False) and not mw.is_chat_hidden_by_vault(jid),
            track=track)

    def close_summary_consent(self, *, locked_only=False):
        """The vault locked or the app is closing: dismiss the consent window
        of a summary still waiting for its answer. ``locked_only`` closes it
        only for a locked chat. The answer, if one was already queued, is
        dropped by the ``valid`` check above."""
        dialog = getattr(self, "_summary_consent_dialog", None)
        if not dialog:
            return
        if locked_only and not self.main_window.is_chat_locked(getattr(self, "_summary_consent_jid", "")):
            return
        if dialog.IsModal():
            dialog.EndModal(wx.ID_CANCEL)

    def _open_summary(self, messages, jid, chat, voice, follows_open_chat):
        """The transcript is built and the result window opens, once the voice
        messages have their words (or stay markers)."""
        mw = self.main_window
        t = mw.i18n.t
        if (getattr(mw, "_shutting_down", False) or mw.is_chat_hidden_by_vault(jid)
                or (follows_open_chat and (self.conversation or {}).get("remoteJid") != jid)):
            return  # The vault closed or the chat changed while voice was transcribed.
        if getattr(self, "_ai_dialog", None) is not None:
            return
        app, config = self._ai_settings()
        if not self._summary_ready(config):
            mw.output(t("ai_summary_not_available"))
            return
        fmt = t("datetime_fmt")
        built = build_unread_transcript(
            messages,
            lambda row, text: format_copied_message(
                self._extract_timestamp(row), self._summary_sender(row, chat), text, fmt),
            lambda row: self._summary_text_of(row, voice.texts))
        if not built.text:
            mw.output(t("ai_summary_nothing"))
            return
        notices = []
        if built.left_out:
            notices.append(t("ai_summary_left_out").format(count=built.included, left_out=built.left_out))
        if built.voice_markers:
            notices.append(t("ai_summary_voice_missing").format(count=built.voice_markers))
        if getattr(voice, "failed", 0):
            notices.append(t("ai_summary_voice_failed").format(count=voice.failed))
        if getattr(voice, "over_limit", 0):
            notices.append(t("ai_summary_voice_over_limit").format(
                count=voice.over_limit, limit=MAX_VOICE_TRANSCRIPTIONS))
        data = ai_config.utf8_bytes(built.text)
        identity = (active_account_id(), jid, "summary")
        locked = mw.is_chat_locked(jid)
        from ui.dialogs.ai_result_dialog import AIResultDialog
        dialog = AIResultDialog(self, identity, "text", locked, config, app,
                                lambda operation: data, "text/plain", notice="\n".join(notices),
                                preconsented=True)
        self._ai_dialog = dialog
        try:
            dialog.ShowModal()
        finally:
            dialog._dispose()
            self._ai_dialog = None
            dialog.Destroy()
            if follows_open_chat:
                self._restore_focus_after_ai(identity, locked)
