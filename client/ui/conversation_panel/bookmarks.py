"""BookmarksMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import wx

from core.bookmark_settings import remove_after_use


class BookmarksMixin:
    """Message bookmarks and temporary bookmarks.
    """

    def _select_bookmarked_message(self, digit: int, jid: str, msg_id: str, i18n,
                                    other_conversation: bool):
        """Focus/select *msg_id* in the (already-open) conversation's message
        list and announce the jump. Shared by the same-conversation and
        just-navigated-to-a-different-conversation cases below — the only
        difference is which text explains where the message ended up."""
        idx = self._find_index_by_msg_id(msg_id)
        if idx < 0:
            self._msg_bookmarks.pop(digit, None)
            self.main_window.output(
                i18n.t("bookmark_not_found").format(digit=digit), interrupt=True
            )
            return
        self._drop_bookmark_after_use(digit, (jid, msg_id), temporary=False)
        # Only in the same conversation: having just navigated to another one,
        # the user did move, even if the row index happens to coincide with
        # the one the newly opened conversation focused on its own.
        if not other_conversation and self._already_on_message_row(idx):
            self.main_window.output(
                i18n.t("bookmark_already_there").format(position=idx + 1, digit=digit),
                interrupt=True,
            )
            return
        self._focus_message_row(idx)
        if not other_conversation:
            self.main_window.output(
                i18n.t("bookmark_jumped").format(position=idx + 1, digit=digit),
                interrupt=True,
            )
            return
        conv_position = self._conversation_position(jid)
        conv_name = self.conversation_name or jid
        if conv_position:
            text = i18n.t("bookmark_jumped_other_conversation").format(
                position=idx + 1, digit=digit, conv_position=conv_position, conv_name=conv_name,
            )
        else:
            text = i18n.t("bookmark_jumped_other_conversation_no_position").format(
                position=idx + 1, digit=digit, conv_name=conv_name,
            )
        self.main_window.output(text, interrupt=True)

    def _drop_bookmark_after_use(self, digit: int, used, temporary: bool):
        """Remove the bookmark a jump just used, when Settings > Messages asks
        for that kind to go away after its first use, so the same key sets a
        new one next time (core/bookmark_settings.py). Only if the digit still
        holds the bookmark that was used: the permanent jump into another
        conversation lands a moment later, through wx.CallAfter."""
        if not remove_after_use(self.main_window.settings, temporary):
            return
        store = self._msg_temp_bookmarks if temporary else self._msg_bookmarks
        if store.get(digit) == used:
            del store[digit]

    def _on_bookmark_set_or_jump(self, digit: int):
        """Ctrl+<digit>: bookmark the focused message, or — if <digit> already
        has a bookmark — move focus/selection to it instead, navigating to
        the bookmark's own conversation first if it's not the one currently
        open.

        Bookmarks store (conversation JID, message key.id) rather than a raw
        list index/position, so a bookmark still finds the right message
        even if either list was rebuilt/reordered (a new message arriving,
        pagination, etc.) between setting it and jumping to it.
        """
        i18n = self.main_window.i18n
        existing = self._msg_bookmarks.get(digit)
        if existing is not None:
            bm_jid, existing_id = existing
            current_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
            if bm_jid == current_jid:
                self._select_bookmarked_message(digit, bm_jid, existing_id, i18n, other_conversation=False)
                return
            target_chat = self.main_window.chats.get(bm_jid)
            if target_chat is None:
                # The whole conversation is gone (chat deleted) — nothing
                # left to jump to.
                del self._msg_bookmarks[digit]
                self.main_window.output(
                    i18n.t("bookmark_not_found").format(digit=digit), interrupt=True
                )
                return
            self.navigate_to_conversation(target_chat)
            # navigate_to_conversation() queues its own focus (message field
            # or messages list, per the "focus_on_open" setting) via
            # wx.CallAfter — ours must be queued AFTER that call returns so
            # it runs last and wins, same ordering this file already relies
            # on elsewhere (see navigate_to_conversation()'s own comment
            # about focus-CallAfter ordering).
            wx.CallAfter(
                self._select_bookmarked_message, digit, bm_jid, existing_id, i18n, True
            )
            return

        idx = self.messages_list.GetFocusedItem()
        if idx < 0 or idx >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[idx]
        if self._is_separator(msg):
            return
        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id:
            return
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if not jid:
            return
        self._msg_bookmarks[digit] = (jid, msg_id)
        conv_position = self._conversation_position(jid)
        if conv_position:
            text = i18n.t("bookmark_set").format(
                digit=digit, position=idx + 1, text=self.messages_list.GetItemText(idx),
                conv_position=conv_position,
            )
        else:
            text = i18n.t("bookmark_set_no_position").format(
                digit=digit, position=idx + 1, text=self.messages_list.GetItemText(idx),
            )
        self.main_window.output(text, interrupt=True)

    def _on_bookmark_remove(self, digit: int):
        """Ctrl+Shift+<digit>: remove the bookmark at that digit, if any."""
        i18n = self.main_window.i18n
        existing = self._msg_bookmarks.pop(digit, None)
        if existing is None:
            self.main_window.output(
                i18n.t("bookmark_not_found").format(digit=digit), interrupt=True
            )
            return
        bm_jid, existing_id = existing
        current_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if bm_jid != current_jid:
            # Can't cheaply confirm a bookmark set in another (possibly
            # unloaded) conversation still points at a real message without
            # loading that conversation's messages just to check — trust it
            # and confirm the removal without a position.
            self.main_window.output(
                i18n.t("bookmark_removed_other_conversation").format(digit=digit), interrupt=True
            )
            return
        idx = self._find_index_by_msg_id(existing_id)
        if idx < 0:
            self.main_window.output(
                i18n.t("bookmark_removed_stale").format(digit=digit), interrupt=True
            )
            return
        conv_position = self._conversation_position(bm_jid)
        if conv_position:
            text = i18n.t("bookmark_removed").format(
                digit=digit, position=idx + 1, conv_position=conv_position,
            )
        else:
            text = i18n.t("bookmark_removed_no_position").format(
                digit=digit, position=idx + 1,
            )
        self.main_window.output(text, interrupt=True)

    # ── Alt+Shift+0..9 / Ctrl+Alt+Shift+0..9: temporary bookmarks ──────────
    # The scratch counterpart to the ten bookmarks above: scoped to the open
    # conversation and cleared on leaving it (see _msg_temp_bookmarks'
    # declaration in __init__ for why both kinds exist). No cross-conversation
    # case to handle here, which is why these are far shorter than their
    # permanent equivalents — a temporary bookmark can only ever point into
    # the conversation that is already open.

    def _on_temp_bookmark_set_or_jump(self, digit: int):
        """Alt+Shift+<digit>: bookmark the focused message temporarily, or —
        if <digit> already holds one — move focus/selection to it instead."""
        i18n = self.main_window.i18n
        existing = self._msg_temp_bookmarks.get(digit)
        if existing is not None:
            idx = self._find_index_by_msg_id(existing)
            if idx < 0:
                # The message left the list (deleted, or trimmed out by a
                # rebuild) — drop the marker rather than keep pointing nowhere.
                del self._msg_temp_bookmarks[digit]
                self.main_window.output(
                    i18n.t("temp_bookmark_not_found").format(digit=digit), interrupt=True
                )
                return
            self._drop_bookmark_after_use(digit, existing, temporary=True)
            if self._already_on_message_row(idx):
                self.main_window.output(
                    i18n.t("temp_bookmark_already_there").format(
                        position=idx + 1, digit=digit
                    ),
                    interrupt=True,
                )
                return
            self._focus_message_row(idx)
            self.main_window.output(
                i18n.t("temp_bookmark_jumped").format(position=idx + 1, digit=digit),
                interrupt=True,
            )
            return

        if self.conversation is None:
            return
        idx = self.messages_list.GetFocusedItem()
        if idx < 0 or idx >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[idx]
        if self._is_separator(msg):
            return
        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id:
            return
        self._msg_temp_bookmarks[digit] = msg_id
        self.main_window.output(
            i18n.t("temp_bookmark_set").format(
                digit=digit, position=idx + 1, text=self.messages_list.GetItemText(idx),
            ),
            interrupt=True,
        )

    def _on_temp_bookmark_remove(self, digit: int):
        """Ctrl+Alt+Shift+<digit>: remove the temporary bookmark at that digit."""
        i18n = self.main_window.i18n
        existing = self._msg_temp_bookmarks.pop(digit, None)
        if existing is None:
            self.main_window.output(
                i18n.t("temp_bookmark_not_found").format(digit=digit), interrupt=True
            )
            return
        idx = self._find_index_by_msg_id(existing)
        if idx < 0:
            self.main_window.output(
                i18n.t("temp_bookmark_removed_stale").format(digit=digit), interrupt=True
            )
            return
        self.main_window.output(
            i18n.t("temp_bookmark_removed").format(digit=digit, position=idx + 1),
            interrupt=True,
        )
