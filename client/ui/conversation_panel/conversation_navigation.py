"""ConversationNavigationMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import threading
import wx
from core.conversation_view import ARCHIVED, LOCKED
from core.utils import (
    db_fetch_limit,
    effective_unread_count,
    is_phone_like,
)


class ConversationNavigationMixin:
    """Opening, focusing, closing and restoring a conversation; composer
    permissions and the chat-list search filter.
    """

    # ── Conversations list events ───────────────────────────────────────────

    def _on_conversation_focused(self, event):
        idx = event.GetIndex()
        if 0 <= idx < len(self.chats_list):
            chat = self.chats_list[idx]
            jid = chat.get("remoteJid", "")
            # Onde o usuario parou na lista, que nao e a mesma coisa que qual
            # conversa esta aberta: dava para mover o foco ate a Conversa 2 sem
            # abri-la, ir para as mensagens da Conversa 1 com Alt+2 e voltar com
            # Alt+1 na Conversa 1, porque _restore_conversation_selection() so
            # sabia de _last_open_jid. Vale tambem para o Esc/Ctrl+W.
            if jid:
                self._last_list_focus_jid = jid
            if jid and jid in self.selected_chats:
                self.selection_sound.play()

    def on_conversation_selected(self, event):
        self.on_conversation_selected_by_index(event.GetIndex())

    def on_conversation_selected_by_index(self, index):
        try:
            self.navigate_to_conversation(self.chats_list[index])
        except Exception:
            return

    def _stop_typing_for_current_conversation(self):
        """Stop typing/recording status for the currently open conversation, if active."""
        if self._is_typing and self.conversation is not None:
            jid = self.conversation.get("remoteJid", "")
            if jid and not jid.endswith("@newsletter"):
                self.main_window.send_typing_status(jid, False, jid.endswith("@g.us"))
            self._is_typing = False
        if self._is_recording and self.conversation is not None:
            jid = self.conversation.get("remoteJid", "")
            if jid and not jid.endswith("@newsletter"):
                self.main_window.send_recording_status(jid, False, jid.endswith("@g.us"))

    def _conversation_note_text(self, name: str, is_group: bool) -> str:
        """Subtitle for the conversation-data button. A one-to-one chat whose
        name is still just a phone number gets it labelled as such, so the
        screen reader announces "Telefone: <number>" rather than reading bare
        digits as if they were a contact name."""
        if not is_group and is_phone_like(name):
            return f"{self.main_window.i18n.t('phone_label')}: {name}"
        return name

    def _message_label_text(self, jid: str, conversation: dict, name: str) -> str:
        """What the composer's label reads: either why the field cannot be
        written to, or who the message is going to.

        Shared by navigate_to_conversation() (opening a chat) and
        update_conversation_name() (a rename landing on the open one) so the
        two cannot drift — they used to carry separate copies of this switch.
        """
        i18n = self.main_window.i18n
        if jid.endswith("@newsletter"):
            return i18n.t("channel_read_only")
        is_group = jid.endswith("@g.us")
        if is_group and self.main_window._is_group_send_restricted(conversation):
            return i18n.t("group_admins_only")
        return f"{i18n.t('type_message_group') if is_group else i18n.t('type_message')} {name}"

    def _apply_composer_permissions(self, jid: str, conversation: dict):
        """Enable/disable the composer controls according to what *jid* allows.

        Three cases: a channel (nothing can be posted at all), a group with
        "only admins can send messages" on where the user isn't an admin, and
        everything else.  Kept out of navigate_to_conversation() so it can be
        tested without a live wx panel — the emoji button used to be the one
        control this switch forgot, staying clickable in a group the user
        cannot post in and inserting text into a read-only field.
        """
        is_channel = jid.endswith("@newsletter")
        admins_only_group = (
            jid.endswith("@g.us")
            and self.main_window._is_group_send_restricted(conversation)
        )
        if is_channel:
            self.message_field.Enable()
            self.message_field.SetEditable(True)
            self.message_field.Disable()
            self.send_message_btn.Disable()
            self.record_voice_message_btn.Disable()
            self._record_voice_alt_btn.Disable()
            if hasattr(self, "_record_voice_system_btn"):
                self._record_voice_system_btn.Disable()
            self._add_attachment_btn.Disable()
            self._emoji_btn.Disable()
        elif admins_only_group:
            # Keep the field enabled/focusable (unlike the channel case
            # above) so it stays reachable via Tab/the Alt+D accelerator and
            # NVDA can announce its read-only state — only actual editing is
            # blocked. Sending/attaching/recording would just be rejected by
            # WhatsApp Web anyway, so those stay disabled like the channel case.
            # Disable() here instead of SetEditable(False) drops the field out
            # of the tab order entirely, which leaves a screen-reader user in
            # a group they cannot post in with nothing announcing why.
            self.message_field.Enable()
            self.message_field.SetEditable(False)
            self.send_message_btn.Disable()
            self.record_voice_message_btn.Disable()
            self._record_voice_alt_btn.Disable()
            if hasattr(self, "_record_voice_system_btn"):
                self._record_voice_system_btn.Disable()
            self._add_attachment_btn.Disable()
            self._emoji_btn.Disable()
        else:
            self.message_field.Enable()
            self.message_field.SetEditable(True)
            self.send_message_btn.Enable()
            self.record_voice_message_btn.Enable()
            self._record_voice_alt_btn.Enable()
            if hasattr(self, "_record_voice_system_btn"):
                self._record_voice_system_btn.Enable()
            self._add_attachment_btn.Enable()
            self._emoji_btn.Enable()

    def refresh_composer_permissions(self, jid: str, transition: bool = True):
        if not self.conversation or self.conversation.get("remoteJid") != jid:
            return

        conversation = self.main_window.chats.get(jid) or self.conversation
        was_editable = self.message_field.IsEditable()
        self._apply_composer_permissions(jid, conversation)
        # Deliberately not re-syncing the call button here: whether a chat can
        # be called depends only on its JID kind, which cannot change while the
        # conversation stays open. The two places that DO open a conversation
        # sync it; a live permission refresh only has to touch the composer.
        self.message_label.SetLabel(
            self._message_label_text(jid, conversation, self.conversation_name)
        )
        self.conversation_panel.Layout()
        if was_editable and not self.message_field.IsEditable():
            self.main_window.output(self.main_window.i18n.t(
                "group_send_restricted_now" if transition else "group_send_restricted"
            ))

    def update_conversation_name(self, jid: str, new_name: str):
        """Apply a group rename to the conversation currently on screen.

        A no-op unless *jid* is the open conversation: a rename anywhere else
        only has to reach the chat list, which main.py already schedules
        separately (_schedule_set_chats). Called via wx.CallAfter from
        MainWindow's two group-rename paths, so this runs on the UI thread.
        """
        if not self.conversation or self.conversation.get("remoteJid") != jid:
            return

        self.conversation_name = new_name
        is_group = jid.endswith("@g.us")
        self._conv_data_btn.SetNote(self._conversation_note_text(new_name, is_group))
        self.message_label.SetLabel(
            self._message_label_text(jid, self.main_window.chats.get(jid, {}), new_name)
        )
        self.conversation_panel.Layout()

    def _open_focus_target(self) -> str:
        """Where opening a conversation puts keyboard focus: "messages_list" or
        "message_field". Settings > User Interface > "focus_on_open" — the
        messages list when it is "unread_or_last", and also whenever the
        message field cannot take input (a read-only group, say). One rule for
        a conversation being opened and for one that was already open."""
        setting = self.main_window.settings.get("user_interface", {}).get(
            "focus_on_open", "message_field")
        if setting == "unread_or_last" or not self.message_field.IsEnabled():
            return "messages_list"
        return "message_field"

    def _focus_already_open_conversation(self):
        """Apply "focus_on_open" to a conversation that is already on screen:
        the message field, or the messages list on the unread separator when
        there is one and on the last message otherwise — the row a fresh open
        would have selected."""
        if self._open_focus_target() == "message_field":
            self.message_field.SetFocus()
            return
        count = self.messages_list.GetItemCount()
        if count > 0:
            sep = self._unread_sep_idx
            # A separator the user already moved past stays on screen but no
            # longer anchors anything: a fresh open would find nothing unread.
            if getattr(self, "_sep_anchors_read_position", False):
                sep = -1
            target = sep if 0 <= sep < count else count - 1
            self.messages_list.Focus(target)
            self.messages_list.Select(target)
            self.messages_list.EnsureVisible(target)
        self.messages_list.SetFocus()

    def navigate_to_conversation(self, conversation, *, origin=None,
                                 take_focus=True):
        """Open `conversation`. `origin` is the panel it belongs to (MAIN,
        ARCHIVED or LOCKED); left out, it follows resolve_origin()."""
        self._begin_conversation_visit(conversation, origin)
        if self.conversation is not None and self.conversation.get("remoteJid") == conversation.get("remoteJid"):
            self.conversation = conversation
            self._sync_voice_call_button(conversation.get("remoteJid", ""))
            # It may have been hidden behind another panel and is being opened
            # from this one now.
            self.conversation_panel.Show()
            self.conversation_panel.Layout()
            self.Layout()
            # Conversation already open: nothing to reload, but choosing it in
            # the list is still "opening" it, so the same "focar ao abrir"
            # setting decides where focus goes (it used to be the message
            # field unconditionally, and ignored take_focus).
            if take_focus:
                wx.CallAfter(self._focus_already_open_conversation)
            return
        # Record that the user actually looked at this conversation. It is the
        # gate on asking the *phone* for its older history: every such request
        # notifies the phone, so it is spent on chats the user opens rather
        # than on every chat in the account. See _note_conversation_opened().
        try:
            self.main_window._note_conversation_opened(
                conversation.get("remoteJid") or "")
        except Exception:
            logging.exception("[conversations] could not record the open (non-fatal)")
        self._stop_typing_for_current_conversation()
        if hasattr(self, "close_ai_media"):
            self.close_ai_media()
        self._cancel_active_recording()
        # Leaving the conversation invalidates any pending auto-chain timers —
        # they captured a target_msg from THIS conversation's list and would
        # otherwise start stale audio (possibly in the wrong chat) later.
        self._cancel_pending_chain_timers()
        # Audio keeps playing across conversation switches.  Save the current
        # position so it can be restored if the same message is played again
        # after a different audio has taken over and closed the stream.
        if self._current_audio_id is not None and self._audio_stream is not None:
            try:
                _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
                pos   = _ctrl.get_position()
                total = _ctrl.get_length()
                if 0 < pos < total:
                    self._audio_positions[self._current_audio_id] = pos
            except Exception:
                pass
        self._hide_audio_controls()
        self._hide_all_media_controls()
        self._hide_media_transfer_gauge()
        self._hide_attachment_panel()
        self._unread_sep_idx = -1  # reset separator for new conversation
        self._sep_anchors_read_position = False
        # _msg_bookmarks is intentionally NOT reset here — bookmarks now span
        # conversations (see the declaration in __init__). _msg_temp_bookmarks
        # is the opposite: scoped to one conversation, so switching away from
        # it is exactly when it must go.
        self._msg_temp_bookmarks.clear()
        # Same for the message selection: activating another chat's row goes
        # straight here without ever closing, so without this the selection
        # leaks into the conversation being opened (issue #99).
        self.selected_messages.clear()
        self._first_unread_msg_id = None
        self._first_unread_count = 0
        self._unread_sep_marked_read = False
        self._quoted_message = None
        self._reaction_map   = {}
        self._is_loading_more = False
        self._reset_expanded_window()
        # Reset mention state for the new conversation
        self._pending_mentions.clear()
        self._pending_mention_display_names.clear()
        self._group_participants_cache = []
        self._hide_mention_suggestions()
        if hasattr(self, "_pending_mentions_panel"):
            self._rebuild_mention_pills()
        # Reset search state
        self._search_results    = []
        self._search_result_idx = -1
        if hasattr(self, "_search_panel") and self._search_panel.IsShown():
            self._search_panel.Hide()
            self._search_open_btn.Show()
            self._search_field.SetValue("")
        self.conversation = conversation
        
        # Load up to 200 messages from local DB when opening conversation to support fast startup
        try:
            _conv_jid = conversation.get("remoteJid", "")
            if _conv_jid:
                configured_limit = int(self.main_window.settings.get("user_interface", {}).get("messages_page_size", 200))
                unread_count = int(conversation.get("unreadCount") or 0)
                limit = db_fetch_limit(configured_limit, unread_count)
                db_msgs = self.main_window.db.get_messages(_conv_jid, limit=limit)
                db_msgs.reverse()
                if "messages" not in conversation:
                    conversation["messages"] = {}
                conversation["messages"]["messages"] = {
                    "total": self.main_window.db.get_message_count(_conv_jid),
                    "pages": 1,
                    "currentPage": 1,
                    "records": db_msgs
                }
        except Exception as e:
            logging.error(f"[navigate_to_conversation] Failed to load messages from DB: {e}")

        pending_rows = [
            row for row in self._outgoing_virtual_messages.values()
            if row.get("key", {}).get("remoteJid") == conversation.get("remoteJid", "")
            and row.get("_local_pending")
        ]
        records = conversation.setdefault("messages", {}).setdefault("messages", {}).setdefault("records", [])
        known_local_ids = {row.get("_local_id") for row in records}
        records.extend(row for row in pending_rows if row.get("_local_id") not in known_local_ids)

        _conv_jid = conversation.get("remoteJid", "")
        self._last_open_jid = _conv_jid
        self.conversation_name = self.main_window.chat_display_name(conversation)
        jid      = conversation.get("remoteJid", "")
        is_group = jid.endswith("@g.us")
        i18n     = self.main_window.i18n

        # Update conversation-data button
        self._conv_data_btn.SetLabel(
            i18n.t("group_data") if is_group else i18n.t("conversation_data")
        )
        self._conv_data_btn.SetNote(
            self._conversation_note_text(self.conversation_name, is_group)
        )

        self._apply_composer_permissions(jid, conversation)
        self._sync_voice_call_button(jid)
        self.message_label.SetLabel(
            self._message_label_text(jid, conversation, self.conversation_name)
        )
            
        if hasattr(self, "_remove_quote_btn"):
            self._remove_quote_btn.Hide()
        self.conversation_panel.Show()
        self.Layout()
        # Snapshot before the background thread zeros unreadCount on the same dict
        self._pending_open_unread = effective_unread_count(conversation)
        # mark_conversation_as_read() finishes its synchronous part (zero the
        # count, wx.CallAfter the chat-list row's text update) almost
        # instantly — starting the thread here raced against the focus
        # CallAfter scheduled at the bottom of this method and routinely won,
        # so NVDA announced the chat-list row's text changing to "read"
        # before announcing the newly focused messages list/message field
        # from opening the conversation. Starting it from the SAME
        # wx.CallAfter queue as the focus change, scheduled further down,
        # guarantees FIFO order instead of leaving it to thread-timing luck.
        # Background: fetch profile/last-seen and update button note
        threading.Thread(
            target=self._fetch_and_update_profile,
            args=(conversation,),
            daemon=True,
        ).start()
        # Subscribe to presence events for this contact so last-seen and typing
        # indicators arrive via onpresencechanged Socket.IO events.
        self.main_window.subscribe_presence(jid)
        # Background: cache group participants for @mention suggestions
        if is_group:
            threading.Thread(
                target=self._fetch_group_participants,
                args=(jid,),
                daemon=True,
            ).start()
        self._clear_chat_search_on_open()
        self.populate_messages()
        self._sync_pending_document_gauge()
        self._backfill_reactions_for_open_conversation()

        # Re-show audio controls only if the playing audio message is focused.
        if (self._current_audio_id is not None
                and self._audio_conv_jid == jid
                and self._audio_stream is not None
                and self._focused_msg_id() == self._current_audio_id):
            self._show_audio_controls()
            self.audio_speed_btn.SetLabel(
                self._format_speed(self._audio_speed_steps[self._audio_speed_index])
            )


        # Move keyboard focus based on user preference.
        # Deferred via wx.CallAfter so this is the last item in the event
        # queue — prevents add_chats_to_ui (which may have been scheduled
        # earlier by restore_window on a notification click) from scheduling
        # its own lst.SetFocus and stealing focus away from the conversation.
        focus_setting = self.main_window.settings.get("user_interface", {}).get("focus_on_open", "message_field")
        logging.info(
            "[navigate_to_conversation] scheduling focus: setting=%r jid=%s",
            focus_setting, jid,
        )

        def _do_focus_messages_list():
            try:
                ok = self.messages_list.SetFocus()
                logging.info(
                    "[navigate_to_conversation] messages_list.SetFocus() ran, "
                    "FindFocus()=%r messages_list=%r",
                    wx.Window.FindFocus(), self.messages_list,
                )
            except Exception:
                logging.exception("[navigate_to_conversation] messages_list.SetFocus() raised")

        def _do_focus_message_field():
            try:
                self.message_field.SetFocus()
                logging.info(
                    "[navigate_to_conversation] message_field.SetFocus() ran, "
                    "FindFocus()=%r message_field=%r",
                    wx.Window.FindFocus(), self.message_field,
                )
            except Exception:
                logging.exception("[navigate_to_conversation] message_field.SetFocus() raised")

        if not take_focus:
            pass
        elif self._open_focus_target() == "messages_list":
            wx.CallAfter(_do_focus_messages_list)
        else:
            wx.CallAfter(_do_focus_message_field)

        # Queued after the focus CallAfter above so it always runs later on
        # the event loop — see this method's comment where the thread start
        # used to live, right after _pending_open_unread was snapshotted.
        def _start_mark_as_read():
            threading.Thread(
                target=self.main_window.mark_conversation_as_read,
                args=(jid,),
                daemon=True,
            ).start()
        wx.CallAfter(_start_mark_as_read)

    def _clear_chat_search_on_open(self):
        """Opening a conversation ends the chat search, unless the user asked
        to keep it (Settings > User Interface, off by default).

        Kept, the list stays filtered behind the open conversation, so Esc
        lands back on the results — _restore_conversation_selection() finds
        the chat in whatever the list holds — instead of on the full list,
        where someone working through several results had to type the search
        again for each one. The archived list has always kept its search.
        """
        if not self.search_field.GetValue().strip():
            return
        if self.main_window.settings.get("user_interface", {}).get(
                "keep_search_after_open", False):
            return
        self.search_field.Clear()

    def on_search_query_changed(self, event):
        # Route through add_chats_to_ui so the active filter and proper sort
        # order are both respected (add_chats_to_ui reads search_field itself).
        self.main_window.add_chats_to_ui()

    def _on_filter_changed(self, event):
        """Update the active conversation filter and rebuild the list."""
        _filter_map = ['all', 'unread', 'groups', 'individual']
        sel = self._filter_radio.GetSelection()
        self._conv_filter = _filter_map[sel] if 0 <= sel < len(_filter_map) else 'all'
        self.main_window.add_chats_to_ui()
        # Selecting a filter option leaves keyboard focus on the radio box —
        # the list itself gets rebuilt but nothing ever moves focus/selection
        # there, so the user had no way to tell what (if anything) the new
        # filter actually matched without tabbing over manually. Only the
        # list's own item focus/selection is updated here, NOT keyboard focus
        # (no SetFocus()) — moving keyboard focus away from the radio box cut
        # off NVDA mid-announcement of the option that was just selected.
        lst = self.conversations_list
        if self.chats_list:
            lst.Focus(0)
            lst.Select(0)
            lst.EnsureVisible(0)

    def on_ctrl_f(self, event):
        self.search_field.SetFocus()

    def _on_search_field_key_down(self, event):
        """Down arrow in the search field moves focus to the first conversation."""
        if event.GetKeyCode() == wx.WXK_DOWN:
            lst = self.conversations_list
            if lst.GetItemCount() > 0:
                lst.SetFocus()
                lst.Focus(0)
                lst.Select(0)
            return
        event.Skip()

    def _on_search_field_enter(self, event):
        """Reveal the hidden locked-chats entry when its search code matches."""
        if self.main_window.try_reveal_locked_chats(self.search_field.GetValue()):
            return
        # Preserve the TextCtrl's ordinary Enter behaviour for every normal
        # conversation search; only a matching secret code is consumed.
        event.Skip()

    def _close_conversation_core(self) -> "tuple[bool, str]":
        """Stop typing/recording indicators and clear the open-conversation
        state. Returns (closed, closed_jid): closed is False when the
        mention-suggestions popup was showing (that press is handled by
        just dismissing the popup — the conversation itself is left open).

        Shared by close_conversation() (Esc — also restores focus to
        whichever list the conversation was opened from) and
        close_conversation_for_panel_switch() (used when the user
        navigates to an entirely different top-level panel, e.g. Status —
        that panel sets its own focus right after, so it must NOT queue
        close_conversation()'s focus-restoration CallAfters, which would
        otherwise steal focus back to the conversations list a moment
        later).
        """
        if hasattr(self, "_mention_panel") and self._mention_panel.IsShown():
            self._hide_mention_suggestions()
            self.message_field.SetFocus()
            return False, ""
        if hasattr(self, "close_ai_media"):
            self.close_ai_media()
        self._stop_typing_for_current_conversation()
        self._cancel_active_recording()
        self._hide_audio_controls()
        self._hide_all_media_controls()
        self._hide_media_transfer_gauge()
        self._hide_attachment_panel()
        # Clear any active edit state
        if self._editing_message_id is not None:
            self._on_cancel_edit()
        if self._quoted_message is not None:
            self._on_cancel_reply()
        # Clear search state
        self._search_results    = []
        self._search_result_idx = -1
        if hasattr(self, "_search_panel") and self._search_panel.IsShown():
            self._search_panel.Hide()
            self._search_open_btn.Show()
            self._search_field.SetValue("")
        # _msg_bookmarks is intentionally NOT reset here — see __init__.
        # _msg_temp_bookmarks is, for the same reason spelled out there.
        self._msg_temp_bookmarks.clear()
        # The message selection is scoped to the conversation that was open:
        # leaving it behind made a reopened conversation come back with rows
        # still selected (issue #99). Unconditional — a user who unticked the
        # Esc option would otherwise still hit that.
        self.selected_messages.clear()
        self._reset_expanded_window()
        closed_jid = self._last_open_jid
        self.conversation = None
        self._conversation_origin = None
        self._voice_call_btn.Hide()
        self.conversation_panel.Hide()
        self.Layout()
        return True, closed_jid

    def _on_escape_conversation(self, event):
        """First Esc clears an active message selection instead of closing
        (issue #99); the next one closes as it always did.

        Deliberately a separate handler rather than a check inside
        close_conversation(): that method is also called programmatically and
        from other commands (Ctrl+W, the panel switch), and teaching it to
        refuse to close would break every one of them. Scoped to the message
        selection only — a chat-list selection must not change what Esc does
        inside a conversation.
        """
        # The mention-suggestions popup keeps winning Esc: it is an overlay
        # right in front of the user, and _close_conversation_core() already
        # answers that press by just dismissing it (and leaving the
        # conversation open).
        mention_open = (hasattr(self, "_mention_panel")
                        and self._mention_panel.IsShown())
        if not mention_open and self._escape_clears_selection_enabled() and self.selected_messages:
            cleared = list(self.selected_messages)
            self.selected_messages.clear()
            # Pass the ids just cleared: their rows still carry the
            # " selecionado" marker and are what needs re-rendering.
            self._refresh_message_rows_by_ids(cleared)
            self.main_window.output(
                self._selection_mode_announcement(
                    self.main_window.i18n.t("all_unselected"), True, False),
                interrupt=True)
            return
        self.close_conversation(event)

    def close_conversation(self, event=None):
        origin = getattr(self, "_conversation_origin", None)
        closed, closed_jid = self._close_conversation_core()
        if not closed:
            return  # _close_conversation_core() only handled the mention popup
        mw = self.main_window
        # Esc returns to the list the conversation was opened from, whatever
        # the chat is (an archived chat found through the main search box
        # belongs to the main list). The archived and locked lists sit
        # hidden behind this panel while their conversation is open.
        if (origin == LOCKED and closed_jid
                and getattr(mw, "_chat_lock_unlocked", False)
                and hasattr(mw, "locked_conversations_panel")):
            wx.CallAfter(self._restore_to_locked_list, closed_jid)
        elif (origin == ARCHIVED and closed_jid
                and hasattr(mw, "archived_conversations_panel")):
            wx.CallAfter(self._restore_to_archived_list, closed_jid)
        else:
            # Defer focus restoration so it runs after the accelerator event is
            # fully processed — calling SetFocus() synchronously inside an EVT_MENU
            # handler can be overridden by wx's post-event focus management on Win32.
            wx.CallAfter(self._restore_conversation_selection)

    def close_conversation_for_panel_switch(self):
        """Same cleanup as close_conversation() but without its focus-
        restoration side effects — see _close_conversation_core()."""
        self._close_conversation_core()

    def _restore_conversation_selection(self):
        """Select, focus and give keyboard focus back to where the user last
        was in the chat list.

        That position is ``_last_list_focus_jid`` — the row the focus actually
        sat on — and only falls back to ``_last_open_jid`` when that row is
        gone from the list (deleted, archived, filtered out) or was never
        recorded. Restoring the OPEN conversation instead was the whole of
        issue #91: moving to another chat without opening it, going to the
        messages with Alt+2 and coming back with Alt+1 (or closing the
        conversation with Esc/Ctrl+W) dropped the user back on the open chat
        every time, losing wherever they had navigated to.
        """
        lst = self.conversations_list
        target = 0
        for candidate in (self._last_list_focus_jid, self._last_open_jid):
            if not candidate:
                continue
            found = -1
            for i, chat in enumerate(self.chats_list):
                if chat.get("remoteJid") == candidate:
                    found = i
                    break
            if found >= 0:
                target = found
                break
        if self.chats_list:
            lst.Focus(target)
            lst.Select(target)
            lst.EnsureVisible(target)
        lst.SetFocus()

    def _restore_to_archived_list(self, jid: str):
        """Switch back to the archived conversations list and re-select `jid`."""
        mw = self.main_window
        # Undo the conversations_list/label Hide() from
        # ArchivedConversationsPanel.on_conversation_selected() so this panel
        # is back to its normal split-view state the next time it is shown
        # from the regular "Conversations" nav item.
        self.conversations_label.Show()
        self.conversations_list.Show()
        self.Hide()
        mw.archived_conversations_panel.Show()
        mw.content_panel.Layout()
        arch = mw.archived_conversations_panel
        lst = arch.conversations_list
        target = 0
        for i, chat in enumerate(arch.chats_list):
            if chat.get("remoteJid") == jid:
                target = i
                break
        if arch.chats_list:
            lst.Focus(target)
            lst.Select(target)
            lst.EnsureVisible(target)
        lst.SetFocus()

    def _restore_to_locked_list(self, jid: str):
        """Switch back to the unlocked vault list and re-select ``jid``."""
        mw = self.main_window
        self.conversations_label.Show()
        self.conversations_list.Show()
        self.Hide()
        panel = mw.locked_conversations_panel
        chats, names = getattr(mw, "_locked_chat_rows", ([], []))
        panel.set_all_chats(chats, names)
        panel.Show()
        mw.content_panel.Layout()
        target = next((
            index for index, chat in enumerate(panel.chats_list)
            if chat.get("remoteJid", "") == jid
        ), 0)
        if panel.chats_list:
            panel.conversations_list.Focus(target)
            panel.conversations_list.Select(target)
            panel.conversations_list.EnsureVisible(target)
        panel.conversations_list.SetFocus()
