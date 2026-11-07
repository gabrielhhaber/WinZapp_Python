"""MessageAccelsMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import time

import pyperclip
import wx
from core.utils import format_number
from main_window.message_rules import quote_is_of_my_message
from core.message_review import is_repeated_review, recent_message_index
from core.transcription import message_audio
from ui.conversation_panel.copy_target import copy_method_for_message


def jump_to_older_message(panel, indices, found_key):
    """Focus the closest of *indices* above the focused row (an older message,
    going backwards), wrapping to the newest after the oldest, and announce
    *found_key*. Shared by the "previous mention" and "previous reply" keys."""
    curr_idx = panel.messages_list.GetFocusedItem()
    target_idx = -1

    for idx in reversed(indices):
        if curr_idx < 0 or idx < curr_idx:
            target_idx = idx
            break

    # If we reached the oldest one, or started below the newest, wrap around to newest
    if target_idx == -1:
        target_idx = indices[-1]

    panel.messages_list.Focus(target_idx)
    panel.messages_list.Select(target_idx, True)
    panel.messages_list.EnsureVisible(target_idx)
    panel.main_window.output(panel.main_window.i18n.t(found_key), interrupt=True)


def pick_newer_index(indices, current):
    """The closest of *indices* below the row *current* (a newer message, going
    forwards), wrapping to the oldest after the newest. -1 when there are none.

    The mirror of what jump_to_older_message() does going backwards. With no
    row focused (*current* < 0) it starts from the oldest. Pure, so the wrap is
    tested without a list control.
    """
    if not indices:
        return -1
    for idx in indices:
        if idx > current:
            return idx
    return indices[0]


def jump_to_newer_message(panel, indices, found_key):
    """Focus the closest of *indices* below the focused row (a newer message),
    wrapping to the oldest after the newest, and announce *found_key*. Shared by
    the "next mention" and "next reply" keys, the forward halves of Alt+Shift+M
    and Alt+Shift+P."""
    target_idx = pick_newer_index(indices, panel.messages_list.GetFocusedItem())
    panel.messages_list.Focus(target_idx)
    panel.messages_list.Select(target_idx, True)
    panel.messages_list.EnsureVisible(target_idx)
    panel.main_window.output(panel.main_window.i18n.t(found_key), interrupt=True)


def mention_indices(panel):
    """Rows of *panel*'s messages that mention me, oldest first."""
    mentions = []
    for i, msg in enumerate(panel._sorted_messages):
        if panel._is_separator(msg): continue

        # Check if mentioned
        msg_inner = msg.get("message", {})
        if isinstance(msg_inner, str):
            import json
            try:
                msg_inner = json.loads(msg_inner)
            except:
                msg_inner = {}

        mentioned_jids = []
        for msg_type_dict in msg_inner.values():
            if isinstance(msg_type_dict, dict):
                ctx = msg_type_dict.get("contextInfo", {})
                if "mentionedJid" in ctx:
                    mentioned_jids = ctx["mentionedJid"]
                    break

        if any(panel.main_window._is_self_jid(j) for j in mentioned_jids):
            mentions.append(i)
    return mentions


def reply_indices(panel):
    """Rows of *panel*'s messages that reply to me, oldest first."""
    return [
        i for i, msg in enumerate(panel._sorted_messages)
        if not panel._is_separator(msg) and panel._is_reply_to_me(msg)
    ]


class MessageAccelsMixin:
    """Accelerator handlers for the open conversation's messages.
    """

    # ── Accelerator shims ─────────────────────────────────────────────────────

    def _on_accel_message_data(self, event):
        index = self.messages_list.GetFirstSelected()
        if 0 <= index < len(self._sorted_messages):
            self._on_menu_message_data(self._sorted_messages[index])

    def _on_accel_reply(self, event):
        index = self.messages_list.GetFirstSelected()
        if 0 <= index < len(self._sorted_messages):
            self._on_menu_reply(self._sorted_messages[index])

    def _on_accel_forward(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_messages:
            self._on_mass_forward_messages(event)
            return
        index = self.messages_list.GetFirstSelected()
        if 0 <= index < len(self._sorted_messages):
            self._on_menu_forward(self._sorted_messages[index])

    def _on_accel_delete_message(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_messages:
            self._on_mass_delete_messages(event)
            return
        index = self.messages_list.GetFirstSelected()
        if index >= 0:
            self._on_menu_delete_message(index)

    # ── Mass-action accelerators ─────────────────────────────────────────────
    # Their own shortcuts for every entry of the context menu's "Ações em
    # massa" submenu, so the submenu is no longer the only way to reach them
    # when Settings > Interface do usuário > "Substituir atalhos por ações em
    # massa ao selecionar conversas e mensagens" is off (that setting stays
    # exactly as it was: with it on, the single-message shortcuts act on the
    # whole selection — see _bulk_shortcuts_enabled).

    def _run_bulk_message_action(self, handler, event):
        """Shared body of the dedicated mass-action shortcuts below: they are
        inert without a selection, mirroring how the "Ações em massa" submenu
        isn't built at all until messages are selected.

        Inert, not silent: a shortcut that does nothing at all reads as
        broken to a screen-reader user, the same reason
        _on_action_save_as() announces save_as_nothing_to_save instead of
        just returning."""
        if not self.selected_messages:
            self.main_window.output(
                self.main_window.i18n.t("bulk_no_message_selection"), interrupt=True
            )
            return
        handler(event)

    def _on_accel_bulk_copy(self, event):
        """Ctrl+Alt+Shift+C: copy every selected message."""
        self._run_bulk_message_action(self._on_mass_copy_messages, event)

    def _on_accel_bulk_forward(self, event):
        """Ctrl+Alt+Shift+E: forward every selected message."""
        self._run_bulk_message_action(self._on_mass_forward_messages, event)

    def _on_accel_bulk_star(self, event):
        """Ctrl+Alt+Shift+F: star every selected message."""
        self._run_bulk_message_action(self._on_mass_star_messages, event)

    def _on_accel_bulk_pin(self, event):
        """Ctrl+Alt+Shift+X: pin every selected message in the chat."""
        self._run_bulk_message_action(self._on_mass_pin_messages, event)

    def _on_accel_bulk_save(self, event):
        """Ctrl+Alt+Shift+S: save every selected message's media."""
        self._run_bulk_message_action(self._on_mass_save_messages, event)

    def _on_accel_bulk_delete(self, event):
        """Ctrl+Shift+Delete: delete every selected message."""
        self._run_bulk_message_action(self._on_mass_delete_messages, event)

    def _on_accel_block(self, event):
        """Ctrl+Shift+B: block/unblock the current contact."""
        if self.conversation is None:
            return
        jid = self.conversation.get("remoteJid", "")
        if not jid or jid.endswith("@g.us"):
            return
        if self.main_window._is_self_jid(jid):
            return  # cannot block yourself
        self._on_menu_block(self.conversation, jid, self.main_window.is_contact_blocked(jid))

    def _on_accel_toggle_read(self, event):
        """Ctrl+Shift+M: mark conversation as read if it has unreads, else unread."""
        if self.conversation is None:
            return
        jid = self.conversation.get("remoteJid", "")
        if not jid:
            return
        if int(self.conversation.get("unreadCount") or 0) > 0:
            self.main_window.mark_conversation_as_read(jid)
        else:
            self.main_window.mark_conversation_as_unread(jid)

    def _on_accel_clear(self, event):
        """Ctrl+Shift+L: clear all local messages from the current conversation."""
        if self.conversation is None:
            return
        jid = self.conversation.get("remoteJid", "")
        if jid:
            self._on_menu_clear_chat(jid)

    def _on_accel_react(self, event):
        """Ctrl+Shift+R: open the reaction picker for the focused message, or
        return the call when that message is a missed call (a call record
        cannot be reacted to, so the shortcut is free for it there)."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        if self._return_call(msg):
            return
        self._on_menu_react(msg)

    def _on_accel_star(self, event):
        """Ctrl+Shift+O: star/favourite the focused message."""
        index = self.messages_list.GetFirstSelected()
        if 0 <= index < len(self._sorted_messages):
            msg = self._sorted_messages[index]
            if not self._is_separator(msg):
                self._on_menu_star(msg)

    def _on_accel_delete_conv(self, event):
        """Delete (in chat list): delete the focused conversation, or every
        selected conversation when a bulk selection exists (see
        _bulk_shortcuts_enabled)."""
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            self._on_mass_delete_chats(event)
            return
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid:
                self._on_menu_delete_chat(jid)

    def _on_accel_copy_message(self, event):
        """Ctrl+C: copy focused message text or media file (with original
        filename) to clipboard — or, with a bulk selection and Settings >
        Interface do usuário > "Substituir atalhos por ações em massa..."
        on, copy every selected plain-text message instead (see
        _on_mass_copy_messages).

        Before any of that: a link control with focus takes the shortcut for
        itself. Tab reaches the links of the focused message (a HyperlinkCtrl
        for one, a list for several), and Ctrl+C there copied the *message*,
        which is what the shortcut means everywhere else and nothing anyone
        wants while standing on a link. The link controls have handlers of
        their own, but this one runs first and unconditionally — wxMSW
        translates accelerators before the focused control sees a key event —
        so the check has to live here to have any effect at all.

        Ahead of the bulk-selection branch too, and deliberately: a selection
        can be left behind in a conversation the user has since gone on
        reading, while focus sitting on a link is a statement about right
        now."""
        url = self._focused_link_url()
        if url:
            self._copy_focused_link(url)
            return
        if self._bulk_shortcuts_enabled() and self.selected_messages:
            self._on_mass_copy_messages(event)
            return
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        # Ctrl+C on a contact card copies the phone number, not the row's
        # rendered text (issue #84); media copies the file.
        getattr(self, copy_method_for_message(msg))(msg)

    def _on_accel_copy_caption(self, event):
        """Ctrl+Shift+C: copy the caption of the focused photo/video/document
        message. Kept on a separate shortcut from Ctrl+C, which already
        copies the file itself for these message types."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        self._on_menu_copy_caption(msg)

    # ── Alt+Shift+T: transcribe a voice message ─────────────────────────────
    # Only the wiring lives here; the flow is ui/transcription_flow.py and the
    # slow part core/transcription/message_run.py.

    def _on_accel_transcribe(self, event):
        """Alt+Shift+T on the focused message — or, with messages selected and
        "Substituir atalhos por ações em massa" on, on every selected one."""
        if self._bulk_shortcuts_enabled() and self.selected_messages:
            self._on_mass_transcribe_messages(event)
            return
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        self._on_menu_transcribe(msg)

    def _on_menu_transcribe(self, msg: dict):
        # Imported here, like SettingsDialog in main_window/settings.py: the
        # flow pulls in the model store and the CUDA runtime (requests, TLS
        # setup), and nobody should pay for that at startup before
        # transcribing anything.
        from ui.transcription_flow import open_or_transcribe

        # A message with no audio answers with one sentence and opens nothing,
        # and one with a stored transcription opens it instead of running —
        # the flow checks both, so the shortcut and the menu cannot disagree.
        open_or_transcribe(self, msg)

    # ── Review a recent message by position (issue #462) ─────────────────────
    # Ten commands with no default key (the Shortcuts tab gives them one): the
    # digit is the position counting back from the newest, 1 the newest and 0
    # the tenth. The key reads that message aloud without moving the focus;
    # the same key again, quickly, copies it.

    def _on_accel_review_message(self, digit):
        if not self.conversation:
            return
        position = 10 if digit == 0 else digit
        index = recent_message_index(self._sorted_messages, position, self._is_separator)
        if index < 0:
            self.main_window.output(self.main_window.i18n.t("review_no_message"), interrupt=True)
            return
        msg = self._sorted_messages[index]
        msg_id = (msg.get("key") or {}).get("id", "")
        now = time.monotonic()
        if is_repeated_review(getattr(self, "_review_last_press", None), digit, msg_id, now):
            self._review_last_press = None
            self._on_menu_copy_message(msg)
            return
        self._review_last_press = (digit, now, msg_id)
        self.main_window.output(
            self._render_message_line(
                msg, index=index, total=len(self._sorted_messages)),
            interrupt=True,
        )

    # ── Alt+Shift+N: the other half of "Enter transcribes" ──────────────────
    # One key that always does the action Enter and Space do not: with the
    # setting off (Enter plays) it transcribes the voice message into its line
    # of the list; with it on (Enter transcribes) it plays or pauses it.

    def _on_accel_transcribe_inline(self, event):
        """Alt+Shift+N on the focused message — or, while it transcribes (Enter
        plays), with messages selected and "Substituir atalhos por ações em
        massa" on, on every selected one."""
        if (self._bulk_shortcuts_enabled() and self.selected_messages
                and not self._enter_transcribes_voice()):
            self._on_mass_transcribe_inline_messages(event)
            return
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        self._on_menu_toggle_voice_action(msg)

    def _on_menu_toggle_voice_action(self, msg: dict):
        """The action the setting leaves for Alt+Shift+N and the menu item."""
        if self._enter_transcribes_voice():
            if msg.get("messageType") == "audioMessage":
                self._toggle_audio_message_playback(msg)
                return
            if msg.get("messageType") == "videoMessage":
                self._video_default_action(msg)
                return
            if msg.get("messageType") == "documentMessage" and message_audio.is_transcribable(msg):
                # An audio or video sent as a file: Enter transcribes it, so
                # this key opens it. Any other document is not swapped: Enter
                # already opens it, and this key says it has no audio.
                self.open_media_message(msg)
                return
        self._on_menu_transcribe_inline(msg)

    def _on_menu_transcribe_inline(self, msg: dict):
        """Transcribe into the message's line — the stored one when it exists."""
        from ui.transcription_flow import transcribe_inline

        transcribe_inline(self, msg)

    def _on_menu_transcribe_again(self, msg: dict):
        from ui.transcription_flow import transcribe_message

        transcribe_message(self, msg)

    def _on_menu_delete_transcription(self, msg: dict):
        from ui.transcription_flow import delete_transcription

        delete_transcription(self, msg)

    def _on_accel_show_text_popup(self, event):
        """Alt+C: show focused message text in a popup dialog."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        self._show_message_text_popup(msg)

    # ── Alt+Shift+L / Alt+Shift+K: announce message status / date-time ────

    def _on_accel_msg_status(self, event):
        """Alt+Shift+L: speak the focused message's current status."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        i18n   = self.main_window.i18n
        status = self._map_status(msg)
        self.main_window.output(status or i18n.t("msg_status_none"), interrupt=True)

    def _on_accel_msg_datetime(self, event):
        """Alt+Shift+K: speak the focused message's date/time, as shown in the list."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        ts       = self._extract_timestamp(msg)
        date_str = self._format_date(ts) if ts else ""
        i18n     = self.main_window.i18n
        self.main_window.output(
            date_str or i18n.t("msg_datetime_none"), interrupt=True
        )

    # ── Alt+Shift+R: reply privately ────────────────────────────────────────

    def _on_accel_reply_private(self, event):
        """Alt+Shift+R: reply privately to the focused group message."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        jid      = self.conversation.get("remoteJid", "") if self.conversation else ""
        from_me  = msg.get("key", {}).get("fromMe", False)
        if not jid.endswith("@g.us") or from_me:
            return
        participant_jid = (
            msg.get("key", {}).get("participant", "")
            or msg.get("participant", "")
        )
        if participant_jid:
            self._on_menu_reply_private(msg, participant_jid)

    # ── Alt+Shift+C: copy phone number + speak ──────────────────────────────

    def _copy_and_speak_jid(self, jid: str):
        """Internal: copy formatted phone number for jid to clipboard and speak it."""
        if not jid or jid.endswith("@g.us"):
            return
        number = format_number(jid)
        if not number:
            return
        try:
            pyperclip.copy(number)
        except Exception:
            pass
        self.main_window.speak_output.output(number)

    def _on_accel_copy_number_speak(self, event):
        """Alt+Shift+C (conversation panel): copy current conversation's phone number."""
        if self.conversation is None:
            return
        self._copy_and_speak_jid(self.conversation.get("remoteJid", ""))

    def _on_accel_copy_number_list(self, event):
        """Alt+Shift+C (chat list): copy selected conversation's phone number."""
        idx = self.conversations_list.GetFirstSelected()
        if idx < 0 or idx >= len(self.chats_list):
            # Fall back to the currently open conversation if nothing selected
            if self.conversation:
                self._copy_and_speak_jid(self.conversation.get("remoteJid", ""))
            return
        self._copy_and_speak_jid(self.chats_list[idx].get("remoteJid", ""))

    # ── Alt+Shift+V: converse with participant ───────────────────────────────

    def _on_accel_alt_shift_v(self, event):
        """Alt+Shift+V: open a private chat with the focused group message's author."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        jid     = self.conversation.get("remoteJid", "") if self.conversation else ""
        from_me = msg.get("key", {}).get("fromMe", False)
        if jid.endswith("@g.us") and not from_me:
            participant_jid = (
                msg.get("key", {}).get("participant", "")
                or msg.get("participant", "")
            )
            if participant_jid:
                pname = self._get_participant_name(participant_jid, msg)
                self._on_menu_converse_private(participant_jid, pname)

    # ── Alt+Shift+Q: goto quoted message ────────────────────────────────────────

    def _on_accel_goto_quoted(self, event):
        """Alt+Shift+Q: navigate to the quoted message of the focused message."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        ctx = self._get_context_info(msg)
        if ctx:
            self._on_menu_goto_quoted(msg, ctx)

    # ── Alt+Shift+S: mute / unmute conversation ──────────────────────────────

    def _on_accel_mute(self, event):
        """Alt+Shift+S: open the mute-duration menu (or unmute if already muted)."""
        if self.conversation is None:
            return
        jid = self.conversation.get("remoteJid", "")
        if not jid:
            return
        self._popup_mute_menu(jid, wx.Window.FindFocus() or self.messages_list)

    # ── Ctrl+N: nova conversa ─────────────────────────────────────────────────

    def _on_new_conversation(self, event=None):
        """Ctrl+N / Nova conversa button: open the New Conversation dialog."""
        from ui.dialogs.new_conversation import NewConversationDialog
        dlg = NewConversationDialog(self.main_window)
        dlg.ShowModal()
        dlg.Destroy()

    def _on_accel_recent_reactions(self, event):
        if not self.conversation:
            return
        # Show recent reactions in the current conversation
        recent = []
        for msg_id, reactions in self._reaction_map.items():
            for jid, emoji in reactions.items():
                recent.append((msg_id, jid, emoji))

        if not recent:
            self.main_window.output(self.main_window.i18n.t("no_reactions_found"), interrupt=True)
            return

        recent.reverse() # show newest first
        recent = recent[:10] # limit to 10

        # Our own reactions are stored under the _SELF_REACTOR_KEY sentinel,
        # not a JID, so they can't go through the participant lookup — and the
        # word for them is the user's "Como se referir a mim?" choice
        # (Eu/Você/custom), not a hardcoded one. Same resolution the reactions
        # dialog does for the very same map; _get_participant_name() covers
        # the other half, where our own reaction did arrive under a real JID.
        msg_parts = []
        for msg_id, jid, emoji in recent:
            name = (
                self.main_window.self_reference_label()
                if jid == self._SELF_REACTOR_KEY
                else self._get_participant_name(jid)
            )
            msg_parts.append(f"{name}: {emoji}")

        text = self.main_window.i18n.t("recent_reactions") + " " + ", ".join(msg_parts)
        self.main_window.output(text, interrupt=True)

    def _on_accel_mentions(self, event):
        """Alt+Shift+M: jump to the previous message that mentions me."""
        if not self.conversation:
            return
        mentions = mention_indices(self)
        if not mentions:
            self.main_window.output(self.main_window.i18n.t("no_mentions_found"), interrupt=True)
            return

        jump_to_older_message(self, mentions, "jumped_to_mention")

    def _on_accel_mentions_next(self, event):
        """Alt+Shift+J: jump to the next message that mentions me — the forward
        half of Alt+Shift+M."""
        if not self.conversation:
            return
        mentions = mention_indices(self)
        if not mentions:
            self.main_window.output(self.main_window.i18n.t("no_mentions_found"), interrupt=True)
            return
        jump_to_newer_message(self, mentions, "jumped_to_mention")

    def _is_reply_to_me(self, msg) -> bool:
        """Whether *msg*, written by someone else, quotes one of MY messages.

        The rule is message_rules.quote_is_of_my_message(), shared with the
        notification side. Where it cannot tell (no participant on the quote
        and the quoted message not loaded) this reads it as the message list
        does (_get_quoted_sender()): a 1:1 reply from the other party is to
        me, while a group reply carries no such guarantee and does not count.
        """
        if not isinstance(msg, dict) or (msg.get("key") or {}).get("fromMe"):
            return False
        ctx = self._get_context_info(msg)
        if not ctx:
            return False

        def _quoted_from_me(stanza_id):
            for other in self._sorted_messages:
                if isinstance(other, dict) and (other.get("key") or {}).get("id") == stanza_id:
                    return bool(other["key"].get("fromMe"))
            return None

        answer = quote_is_of_my_message(ctx, self.main_window._is_self_jid, _quoted_from_me)
        if answer is None:
            return not (self.conversation or {}).get("remoteJid", "").endswith("@g.us")
        return answer

    def _on_accel_replies(self, event):
        """Alt+Shift+P: jump to the previous message that replies to me."""
        if not self.conversation:
            return
        replies = reply_indices(self)
        if not replies:
            self.main_window.output(self.main_window.i18n.t("no_replies_found"), interrupt=True)
            return
        jump_to_older_message(self, replies, "jumped_to_reply")

    def _on_accel_replies_next(self, event):
        """Alt+Shift+O: jump to the next message that replies to me — the
        forward half of Alt+Shift+P."""
        if not self.conversation:
            return
        replies = reply_indices(self)
        if not replies:
            self.main_window.output(self.main_window.i18n.t("no_replies_found"), interrupt=True)
            return
        jump_to_newer_message(self, replies, "jumped_to_reply")
