"""MessageListMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

from ui.shortcut_bindings import command_key_event
import base64 as _b64
import logging
import os
import threading
import wx
from ui.conversation_panel.media_paths import _SAVEABLE_MESSAGE_TYPES
from core.call_log import (
    call_log_is_video,
    is_returnable_missed_call,
)
from app_paths import data_path


class MessageListMixin:
    """Moving around the message list: selection, activation, focus, paging,
    quick-reply buttons and jumps.
    """

    def _no_conversation_open_announced(self) -> bool:
        """Say "no chat is open" and return True when there is none.

        The shortcuts that only make sense inside a conversation (Alt+2, Alt+3,
        Ctrl+W) used to be completely silent with nothing open. For a
        screen-reader user silence is indistinguishable from the shortcut being
        broken — the same reasoning as _run_bulk_chat_action()'s
        "bulk_no_chat_selection" and save_media_message()'s
        "save_as_nothing_to_save".
        """
        if self.conversation is not None:
            return False
        self.main_window.output(
            self.main_window.i18n.t("no_chat_open"), interrupt=True
        )
        return True

    # ── Messages list events ────────────────────────────────────────────────

    def on_message_selected(self, event):
        """Show / hide action controls when the selection changes in the messages list."""
        if getattr(self, "_suppress_selection_side_effects", False):
            return
        index = event.GetIndex()
        self._hide_all_media_controls()   # also clears links panel
        if index < 0 or index >= len(self._sorted_messages):
            return
        if self._is_separator(self._sorted_messages[index]):
            return  # separator row — no action controls
        msg     = self._sorted_messages[index]
        msg_type = msg.get("messageType", "")
        msg_obj  = msg.get("message") or {}
        msg_id   = msg.get("key", {}).get("id", "")
        clean_msg_id = msg_id
        if "_" in msg_id:
            parts = msg_id.split("_")
            clean_msg_id = parts[2] if len(parts) > 2 else parts[-1]
        media_path = data_path("media", f"{clean_msg_id}.wzmedia")
        is_downloaded = os.path.isfile(media_path)

        if msg_type == "documentMessage":
            if is_downloaded:
                self._action_open_btn.SetLabel(self.main_window.i18n.t("open"))
                self._action_open_btn.Show()
                self._action_save_as_btn.Show()
            else:
                self._action_download_btn.Show()
            self.conversation_panel.Layout()

        elif msg_type == "imageMessage":
            jpeg = (msg_obj.get("imageMessage") or {}).get("jpegThumbnail", "")
            self._try_show_thumbnail(jpeg)
            self._action_open_btn.SetLabel(self.main_window.i18n.t("open_image"))
            self._action_open_btn.Show()
            self._action_save_as_btn.Show()
            self.conversation_panel.Layout()

        elif msg_type == "stickerMessage":
            jpeg = (msg_obj.get("stickerMessage") or {}).get("jpegThumbnail", "")
            self._try_show_thumbnail(jpeg)
            # No action buttons for stickers

        elif msg_type == "videoMessage":
            video = msg_obj.get("videoMessage") or {}
            jpeg = video.get("jpegThumbnail", "")
            self._try_show_thumbnail(jpeg)
            if not video.get("gifPlayback"):
                if is_downloaded:
                    self._action_open_btn.SetLabel(self.main_window.i18n.t("open"))
                    self._action_open_btn.Show()
                    self._action_save_as_btn.Show()
                else:
                    self._action_download_btn.Show()
            self.conversation_panel.Layout()

        elif msg_type == "buttonsMessage":
            buttons = (msg_obj.get("buttonsMessage") or {}).get("buttons", [])
            remote_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
            self._show_reply_buttons(buttons, remote_jid)

        elif msg_type == "listMessage":
            sections = (msg_obj.get("listMessage") or {}).get("sections", [])
            rows: list = []
            for sec in sections:
                rows.extend(sec.get("rows", []) if isinstance(sec, dict) else [])
            remote_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
            self._show_list_rows(rows, remote_jid)

        elif msg_type == "contactMessage":
            contact = msg_obj.get("contactMessage") or {}
            vcard = contact.get("vcard", "")
            self._contact_msg_jid = self._jid_from_vcard(vcard)
            if self._contact_msg_jid:
                self._contact_converse_btn.Show()
                self._contact_save_btn.Show()
                self.conversation_panel.Layout()

        elif msg_type in ("locationMessage", "liveLocationMessage"):
            if self._location_maps_url(msg):
                self._action_open_btn.SetLabel(self.main_window.i18n.t("open_location"))
                self._action_open_btn.Show()
                self.conversation_panel.Layout()

        self._update_ai_describe_button(msg)

        if self._saved_media_path(msg):
            self._action_show_in_folder_btn.Show()

        # ── Link detection ────────────────────────────────────────────────
        # Always check the rendered text for URLs (regardless of msg_type).
        # Must go through _message_own_links(), which renders the full,
        # untruncated text — not messages_list.GetItemText(index): SysListView32 (the native
        # control wx.ListCtrl wraps) truncates each row's accessible name at
        # _LIST_CTRL_TEXT_LIMIT characters, so a link further into a long
        # message was silently invisible to link detection and never became
        # Tab-focusable, even though the message itself displayed fine (via
        # the "Ler mais" remainder).
        self._update_links_panel(self._message_own_links(msg))

        # ── Mention detection ─────────────────────────────────────────────
        self._update_mentions_panel(self._extract_mentions(msg))

        self._sync_media_action_slot_visibility()

    def on_message_activated(self, event):
        """Enter / double-click on a message item."""
        idx = self.messages_list.GetFocusedItem()
        if 0 <= idx < len(self._sorted_messages):
            # Native list controls can emit ITEM_ACTIVATED for Ctrl+Enter even
            # when EVT_KEY_DOWN consumed the key.  Route it here as well so it
            # can never fall through to normal Enter (video/audio playback).
            if wx.GetKeyState(wx.WXK_CONTROL):
                msg = self._sorted_messages[idx]
                if (
                    not self._is_separator(msg)
                    and msg.get("messageType", "") in _SAVEABLE_MESSAGE_TYPES
                ):
                    self.show_message_in_folder(msg)
                return
            self._do_activate_message(idx)

    def _do_activate_message(self, index: int):
        """Core activation logic shared by Enter and double-click.

        Space does not come through here. It reaches playback directly via
        _space_toggles_playback() — which covers only the audio/video half of
        what follows, because Space stays strictly narrower than Enter — and
        only while no selection exists, since a selection turns it back into a
        selecting key (see _on_messages_list_key_down).
        """
        if index < 0 or index >= len(self._sorted_messages):
            return
        if self._is_separator(self._sorted_messages[index]):
            return  # separator row — no action
        self.activate_message(self._sorted_messages[index], index=index)

    def activate_message(self, msg: dict, index=None):
        """Activate a message (what Enter does), given the message itself.

        Split out of _do_activate_message() because every branch below already
        worked from the message; the index was only ever used to look it up,
        plus restoring list focus after the media viewer closes. That made the
        whole activation path unreachable for a caller holding a message that
        is not in this panel's list — the data dialogs' Media tab, which reads
        the conversation's entire history from the database while this panel
        keeps roughly the last 200 messages in memory.

        Reported live as: some audios in the Media tab simply do not play, with
        no error, no announcement, and no visible change. They were not a codec
        problem — the activation never reached playback at all, because the
        index lookup returned -1 and the caller skipped the action in silence.
        Documents, images, videos and links in that tab were affected the same
        way; audio is just the type Enter is the natural gesture for.

        index is optional and only used to put keyboard focus back on the row
        the media viewer was opened from.
        """
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("messageType", "")
        msg_obj  = msg.get("message") or {}
        msg_id   = msg.get("key", {}).get("id", "")

        # For text-based messages: open the first link if one is present,
        # otherwise show the full message text popup (same as Alt+C).
        if msg_type in ("conversation", "extendedTextMessage", ""):
            # Full untruncated text — see the matching comment in
            # _on_message_focused() for why GetItemText(index) is wrong here.
            links = self._message_own_links(msg)
            if links:
                try:
                    os.startfile(links[0])
                except Exception:
                    wx.LaunchDefaultBrowser(links[0])
                return
            self._show_message_text_popup(msg)
            return

        if msg_type == "audioMessage":
            self._toggle_audio_message_playback(msg)

        elif msg_type == "videoMessage" and not self._use_conversation_video_media_viewer_dialog():
            # Classic mode (Settings > Interface do usuário > "Mostrar vídeos
            # nas conversas em player separado" unchecked): play in-app via
            # BASS/ffmpeg instead of the dialog, exactly like before that
            # dialog existed — see _play_toggle_video_message().
            video = msg_obj.get("videoMessage") or {}
            if video.get("gifPlayback"):
                return  # GIFs have no audio track to play
            self._play_toggle_video_message(msg)

        elif msg_type in ("imageMessage", "videoMessage"):
            # Media opens in the same accessible, maximized viewer used by
            # statuses. This avoids wx.StaticBitmap clipping and gives video
            # proper seek/volume/speed controls.
            self.open_media_viewer_for_message(msg, restore_index=index)

        elif msg_type in ("documentMessage", "locationMessage", "liveLocationMessage"):
            # Documents and locations keep their existing system-open behaviour.
            # open_media_message() is _on_action_open()'s message-based half and
            # covers both, including the download-failure reporting.
            self.open_media_message(msg)

        elif msg_type == "contactMessage":
            # Enter on a contact message → open a conversation with that
            # contact, same as clicking the "Conversar" button. Space
            # deliberately does NOT do this — see _space_toggles_playback().
            contact = msg_obj.get("contactMessage") or {}
            jid = self._jid_from_vcard(contact.get("vcard", ""))
            self._on_contact_converse(None, jid=jid)

    def _toggle_audio_message_playback(self, msg: dict) -> None:
        """Start/pause playback of an audioMessage row. Split out of
        activate_message() so Enter and plain Space share one copy of the
        clean_msg_id derivation and the _toggle_playback() call."""
        msg_id   = msg.get("key", {}).get("id", "")
        msg_obj  = msg.get("message") or {}
        duration = (msg_obj.get("audioMessage") or {}).get("seconds", 0) or 0
        clean_msg_id = msg_id
        if "_" in msg_id:
            parts = msg_id.split("_")
            clean_msg_id = parts[2] if len(parts) > 2 else parts[-1]
        logging.info(f"[UI Audio Activation] msg_id={msg_id}, clean_msg_id={clean_msg_id}, duration={duration}, file={data_path('voice_messages', f'{clean_msg_id}.msv')}")
        self._toggle_playback(
            msg_id, duration, msg,
            file_path=data_path("voice_messages", f"{clean_msg_id}.msv"),
            audio_ext=".ogg",
        )

    def _space_toggles_playback(self, msg: dict) -> bool:
        """Play/pause *msg* if plain Space can, and return whether it did.

        Space stays strictly narrower than Enter (issue #99): it never opens a
        link, a text popup, the media viewer, a document or a contact's
        conversation. Those all put a window on screen *as their purpose*, and
        a blind user reasonably expects that only from an explicit Enter — so
        Space only ever starts or pauses playback, and returns False (the
        caller then skips the key) for everything else. It is not a promise
        that nothing can appear: _play_toggle_video_message()'s background
        worker still reports a decode/IO failure in a wx.MessageBox, which is
        an error and not an activation.
        """
        if not isinstance(msg, dict) or self._is_separator(msg):
            return False
        msg_type = msg.get("messageType", "")
        if msg_type == "audioMessage":
            self._toggle_audio_message_playback(msg)
            return True
        if msg_type == "videoMessage" and not self._use_conversation_video_media_viewer_dialog():
            video = (msg.get("message") or {}).get("videoMessage") or {}
            if video.get("gifPlayback"):
                return False  # GIFs have no audio track to play
            self._play_toggle_video_message(msg)
            return True
        return False

    # ── Lazy-loading: load older messages when the user focuses item 0 ─────────

    def _focused_msg_id(self) -> str:
        """Return the message ID of the currently focused list item, or ''."""
        idx = self.messages_list.GetFocusedItem()
        if idx < 0 or idx >= len(self._sorted_messages):
            return ""
        m = self._sorted_messages[idx]
        if self._is_separator(m):
            return ""
        return m.get("key", {}).get("id", "")

    def _on_message_focused(self, event):
        # Set while another surface (the group data dialog's Media tab) drives
        # this list's selection purely to hand an index to a handler that reads
        # it. Everything below reacts to a HUMAN moving through the list -
        # playing the selection sound, marking the conversation read once the
        # unread separator is passed, and page-loading more history at index 0 -
        # and none of it should fire for a selection the user never made. The
        # history load is the dangerous one: it rebuilds _sorted_messages
        # synchronously, so the very index being handed over stops being valid.
        if getattr(self, "_suppress_selection_side_effects", False):
            return
        idx = event.GetIndex()

        if 0 <= idx < len(self._sorted_messages):
            msg = self._sorted_messages[idx]
            if not self._is_separator(msg):
                msg_id = msg.get("key", {}).get("id", "")
                if msg_id and msg_id in self.selected_messages:
                    self.selection_sound.play()

        # Unread-separator logic:
        # - Focus reaching the separator (or anything below it) marks the
        #   conversation as read, once.
        # - Focus moving PAST the separator dismisses the row itself. Merely
        #   landing on it does not — see _should_dismiss_unread_separator().
        # `idx < len(...)`: the typing row (typing_row.py) sits past the last
        # message and is not one — landing on it reads nothing, so it must not
        # mark the conversation read.
        if self._unread_sep_idx >= 0 and idx < len(self._sorted_messages):
            if idx >= self._unread_sep_idx:
                # Mark as read immediately (first time focus arrives) — but
                # not while populate_messages() is still running
                # (_populating_messages): that method's OWN default-placement
                # Focus() call (landing exactly on the separator/last row when
                # a conversation is freshly opened) fires this same
                # EVT_LIST_ITEM_FOCUSED synchronously, well before the
                # deferred wx.CallAfter in navigate_to_conversation() ever
                # moves real keyboard focus off the conversations-list row
                # the user just pressed Enter on. Starting the mark-as-read
                # thread here raced ahead of that CallAfter and could get its
                # own wx.CallAfter(_refresh_chat_row_in_list) queued (and
                # executed) first — updating the still-focused conversations
                # list row's text (removing the unread badge) before focus
                # had actually moved away from it, so NVDA re-announced the
                # row's new text instead of the newly focused messages
                # list/message field. navigate_to_conversation() already
                # starts its own mark-as-read thread (deferred until after
                # that focus change) for the "just opened this conversation"
                # case, so nothing is lost by skipping it here.
                if (not self._unread_sep_marked_read
                        and not getattr(self, "_populating_messages", False)):
                    self._unread_sep_marked_read = True
                    if self.conversation is not None:
                        jid = self.conversation.get("remoteJid", "")
                        if jid:
                            threading.Thread(
                                target=self.main_window.mark_conversation_as_read,
                                args=(jid,),
                                daemon=True,
                            ).start()
                # Focus has now genuinely stepped PAST the separator, into the
                # unread messages themselves, so it anchors an already-read
                # position. The row stays on screen showing the old count (the
                # official WhatsApp behaviour the user asked for: a marker of
                # where you had stopped reading), but the next live message
                # must replace it entirely (fresh separator, count reset to 1)
                # rather than bumping its count, or the count would keep
                # accumulating on top of messages already read. This is the
                # ONLY situation that justifies that reset — a separator placed
                # on conversation open still sits above genuinely unread
                # messages, so there a new message just adds to it.
                #
                # Strictly PAST, via the same predicate that decides the
                # dismissal, and not the `idx >= sep_idx` of the mark-as-read
                # above: merely landing on the separator row is where
                # populate_messages() itself parks focus on open, and Alt+3 /
                # Alt+U land there on purpose. Under the old semantics setting
                # the flag there was harmless (every rebuild set it anyway);
                # now it is the single thing choosing between "add" and "move
                # and restart at 1", so a user pressing Alt+3 just to get their
                # bearings would have watched a separator reading "3" be
                # replaced by a "1" on the next message.
                if self._should_dismiss_unread_separator(idx, self._unread_sep_idx):
                    self._sep_anchors_read_position = True

            # Keep the unread separator visible throughout navigation while the
            # conversation is open, allowing the user to jump back to it at any time
            # using Alt+U or Alt+3. It is reset only when opening/closing conversations.
            pass

        # Show audio controls only when the focused item IS the playing audio.
        if self._current_audio_id is not None and self._audio_stream is not None:
            if 0 <= idx < len(self._sorted_messages):
                m = self._sorted_messages[idx]
                if (not self._is_separator(m)
                        and m.get("key", {}).get("id") == self._current_audio_id):
                    self._show_audio_controls()
                else:
                    self._hide_audio_controls()

        # Lazy-loading: whenever focus lands on the very first row, pull in
        # the previous page. This used to be handled only in the raw
        # WXK_UP/PAGEUP/HOME key-down handler below, which only fires when the
        # user presses Up again while *already* sitting on row 0 — pressing
        # Home/PageUp/Ctrl+Home from further down jumps straight to row 0
        # without ever going through that handler, and so did a mouse click or
        # a screen reader's object-navigation landing there. Hooking the focus
        # event instead catches every way of reaching the first message, not
        # just one specific key combo pressed twice.
        if (idx == 0 and not self._is_loading_more and self._sorted_messages
                and not getattr(self, "_populating_messages", False)):
            if self._messages_offset > 0:
                self._load_more_messages()
            else:
                self._load_older_messages()

        self._update_return_call_button(idx)
        self._update_read_more_button(idx)
        self._update_reactions_button(idx)
        event.Skip()

    def _update_return_call_button(self, idx: int):
        """Show "Retornar ligação" only while a returnable missed call is focused."""
        msg = None
        if 0 <= idx < len(self._sorted_messages):
            candidate = self._sorted_messages[idx]
            chat_jid = str((self.conversation or {}).get("remoteJid") or "")
            if (not self._is_separator(candidate)
                    and is_returnable_missed_call(candidate, chat_jid)):
                msg = candidate
        self._return_call_msg = msg
        self._return_call_btn.Show(msg is not None)
        self.conversation_panel.Layout()

    def _return_call(self, msg) -> bool:
        """Call back the caller of the missed call *msg*, voice or video like
        the call it returns. False when *msg* is not a returnable missed call."""
        if not self.conversation:
            return False
        jid = str(self.conversation.get("remoteJid") or "")
        if not is_returnable_missed_call(msg, jid):
            return False
        name = self.conversation_name or self.conversation.get("name") or ""
        if call_log_is_video(msg):
            self.main_window.start_video_call(jid, name)
        else:
            self.main_window.start_voice_call(jid, name)
        return True

    def _on_return_call(self, _event=None):
        msg = getattr(self, "_return_call_msg", None)
        if msg is not None:
            self._return_call(msg)

    def _update_reactions_button(self, idx: int):
        """Show/hide the reactions-list button for the focused message row.

        Only visible when the focused message actually has reactions —
        label states the emoji breakdown so a screen-reader user knows what
        the button does and what they'll find without opening it, e.g.
        "Reações 👍, 1 no total. 😂, 2 no total.".
        """
        msg_id = ""
        counts = {}
        if 0 <= idx < len(self._sorted_messages):
            msg = self._sorted_messages[idx]
            if not self._is_separator(msg):
                msg_id = msg.get("key", {}).get("id", "")
                if msg_id:
                    counts = self._reaction_counts(msg_id)
        if counts:
            i18n = self.main_window.i18n
            parts = [
                f"{emoji}, {count} {i18n.t('total_label')}"
                for emoji, count in counts.items()
            ]
            self._reactions_btn.SetLabel(f"{i18n.t('reactions_label')} {'. '.join(parts)}.")
            self._reactions_btn.Show()
            self._reactions_focused_msg_id = msg_id
        else:
            self._reactions_btn.Hide()
            self._reactions_focused_msg_id = ""
        self.conversation_panel.Layout()

    def _on_show_reactions(self, event):
        """Open the reactions-list dialog for the currently focused message."""
        msg_id = getattr(self, "_reactions_focused_msg_id", "")
        if not msg_id:
            return
        per_msg = self._reaction_map.get(msg_id) or {}
        if not per_msg:
            return
        from ui.dialogs.reactions_dialog import ReactionsDialog
        dlg = ReactionsDialog(self.main_window, self, per_msg)
        dlg.ShowModal()
        dlg.Destroy()

    def _update_read_more_button(self, idx: int):
        """Show/hide the "Ler mais" button for a truncated text message row.

        Only meaningful in classic wx.ListCtrl mode — SysListView32 truncates
        the accessible name of each row at _LIST_CTRL_TEXT_LIMIT characters;
        CompatListBoxMessagesCtrl exposes the full text and has no such limit.
        """
        if getattr(self, "_message_list_mode", "classic") == "listbox":
            return
        show = False
        if 0 <= idx < len(self._sorted_messages):
            msg = self._sorted_messages[idx]
            if not self._is_separator(msg):
                msg_type = msg.get("messageType", "")
                if msg_type in ("conversation", "extendedTextMessage", ""):
                    rendered = self._render_message_line(msg)
                    if len(rendered) > self._LIST_CTRL_TEXT_LIMIT:
                        self._read_more_remainder = rendered[self._LIST_CTRL_TEXT_LIMIT:]
                        show = True
        if show:
            self._read_more_btn.Show()
        else:
            self._read_more_btn.Hide()
            self._read_more_remainder = ""
        self.conversation_panel.Layout()

    def _on_read_more(self, event):
        """Alt+L / button click: speak only the text cut off by the list-view limit."""
        remainder = getattr(self, "_read_more_remainder", "")
        if remainder:
            self.main_window.output(remainder, interrupt=True)

    # ── Keyboard Space-as-activate helpers ──────────────────────────────────

    # Default for how many rows Page Up/Page Down jump in messages_list/
    # conversations_list, when Settings > User Interface hasn't set one yet.
    # Both are plain wx.ListCtrl (native SysListView32 on Windows), whose
    # default Page Up/Down handling only moved focus by a single row instead
    # of paging — reported as making them functionally indistinguishable
    # from Up/Down. 15 mirrors the default messages_page_size fetched per
    # sync page (see client/data/settings_default.json's user_interface
    # section) as a reasonable "one screenful" default.
    _DEFAULT_PAGE_JUMP_SIZE = 15

    def _page_jump_size(self) -> int:
        """User-configurable Page Up/Page Down jump size (Settings > User
        Interface). Falls back to the default for a missing/invalid value —
        settings_dialog.py validates on save, but settings.json can still be
        hand-edited or predate this option."""
        raw = self.main_window.settings.get("user_interface", {}).get(
            "page_jump_size", self._DEFAULT_PAGE_JUMP_SIZE
        )
        try:
            size = int(raw)
        except (TypeError, ValueError):
            return self._DEFAULT_PAGE_JUMP_SIZE
        return size if size >= 1 else self._DEFAULT_PAGE_JUMP_SIZE

    @staticmethod
    def _page_jump_target(count: int, focused_idx: int, delta: int) -> int:
        """Clamped index for a Page Up/Down jump, or -1 when the list is empty."""
        if count <= 0:
            return -1
        idx = focused_idx if focused_idx >= 0 else 0
        return max(0, min(count - 1, idx + delta))

    def _jump_list_by(self, list_ctrl: wx.ListCtrl, delta: int) -> None:
        target = self._page_jump_target(list_ctrl.GetItemCount(), list_ctrl.GetFocusedItem(), delta)
        if target < 0:
            return
        list_ctrl.Focus(target)
        list_ctrl.Select(target, True)
        list_ctrl.EnsureVisible(target)

    # ── Selection helpers (messages list) ───────────────────────────────────

    def _toggle_message_selection(self, msg: dict) -> bool:
        """Toggle *msg*'s membership in self.selected_messages, refresh its
        row, and play/announce the change. Shared by Ctrl+Space
        (_on_messages_list_key_down) and the "Selecionar mensagem"/
        "Desselecionar mensagem" context menu item.

        Returns whether anything was actually toggled, so plain Space can hand
        the key back to the control (event.Skip()) instead of swallowing it in
        silence on a row this refuses — the unread separator, or a message with
        no id. The context-menu caller ignores it: the item is only built for a
        row that has one.
        """
        if self._is_separator(msg):
            return False
        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id:
            return False
        was_active = bool(self.selected_messages)
        if msg_id in self.selected_messages:
            self.selected_messages.remove(msg_id)
            self._refresh_message_rows_by_ids([msg_id])
            self.main_window.output(
                self._selection_mode_announcement(
                    self.main_window.i18n.t("unselected"),
                    was_active, bool(self.selected_messages)),
                interrupt=True)
        else:
            self.selected_messages.add(msg_id)
            self._refresh_message_rows_by_ids([msg_id])
            self.selection_sound.play()
            self.main_window.output(
                self._selection_mode_announcement(
                    self.main_window.i18n.t("selected"),
                    was_active, bool(self.selected_messages)),
                interrupt=True)
        return True

    def _select_message_at(self, idx: int) -> bool:
        """Add the message at *idx* to self.selected_messages, if it's a real
        (non-separator) message with an id. Returns whether it was added."""
        if not (0 <= idx < len(self._sorted_messages)):
            return False
        msg = self._sorted_messages[idx]
        if self._is_separator(msg):
            return False
        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id or msg_id in self.selected_messages:
            return False
        self.selected_messages.add(msg_id)
        return True

    def _all_selectable_message_ids(self) -> list:
        return [
            msg_id
            for m in self._sorted_messages
            if not self._is_separator(m)
            for msg_id in [m.get("key", {}).get("id", "")]
            if msg_id
        ]

    def _refresh_message_rows_by_ids(self, msg_ids) -> None:
        """Re-render specific rows (by message id) so the " selecionado"
        marker _render_message_line() adds stays in sync after a selection
        change — SetItemText only, no list rebuild/focus disruption."""
        if not msg_ids:
            return
        self._set_message_row_texts(set(msg_ids))

    def _set_message_row_texts(self, ids: set) -> set:
        """Re-render every rendered row whose message id is in *ids*, and
        return the ids that were actually found on screen.

        The bare mechanism, shared by _refresh_message_rows_by_ids() (selection
        markers, which never care whether a row is missing) and
        _repaint_message_rows() (local flag changes, which fall back to a full
        rebuild when one is).
        """
        found = set()
        total = len(self._sorted_messages)
        for i, m in enumerate(self._sorted_messages):
            if self._is_separator(m):
                continue
            mid = m.get("key", {}).get("id", "")
            if mid in ids:
                self.messages_list.SetItemText(i, self._render_message_line(m, index=i, total=total))
                found.add(mid)
        return found

    def _on_messages_list_key_down(self, event):
        """Ctrl+Space toggles the focused row's membership in
        self.selected_messages (the mass actions act on that set). Plain Space
        plays/pauses the focused audio or video message — except while a
        selection already exists, where it goes on selecting instead
        ("selection mode", issue #99, off via
        user_interface.space_selects_in_selection_mode).
        Shift+Down/Shift+Up extend the selection to the
        next/previous row; Shift+Home/Shift+End select every row above/below the focused one and
        move focus to the first/last row (falling back to their previous
        meaning — seeking the active playback to its start/end — whenever
        something actually is playing); Ctrl+Shift+Space selects every
        message, or clears the selection if everything is already selected.
        Activation stayed on Enter / double-click — see _do_activate_message.
        Page Up / Page Down jump by a configurable number of messages (page_up_down_step setting).
        Trigger loading older messages on Arrow Up / Page Up when at the top (index 0)."""
        event = command_key_event(self, 'message_list', event)
        key = event.GetKeyCode()
        ctrl = event.ControlDown()
        shift = event.ShiftDown()
        idx = self.messages_list.GetFocusedItem()
        total = self.messages_list.GetItemCount()
        logging.info(f"[_on_messages_list_key_down] Key down: {key}, idx: {idx}, is_loading_more: {self._is_loading_more}, offset: {self._messages_offset}")

        if ctrl and not shift and key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if 0 <= idx < len(self._sorted_messages):
                msg = self._sorted_messages[idx]
                if (
                    not self._is_separator(msg)
                    and msg.get("messageType", "") in _SAVEABLE_MESSAGE_TYPES
                ):
                    self.show_message_in_folder(msg)
            return

        if ctrl and not shift and key == ord("V"):
            if self._paste_from_messages_list():
                return

        ui_cfg = self.main_window.settings.get("user_interface", {})
        raw_step = ui_cfg.get("page_jump_size", ui_cfg.get("page_up_down_step", 15))
        try:
            step = max(1, int(raw_step))
        except (ValueError, TypeError):
            step = 15

        # Shift+arrow/PageUp/PageDown seek the currently playing voice
        # message or video instead of moving list focus (issue #17). Checked
        # before the plain (unmodified) equivalents below, since PageUp/
        # PageDown already have their own unmodified meaning here (jump N
        # messages / load older history).
        if shift and key in (
            wx.WXK_LEFT, wx.WXK_NUMPAD_LEFT, wx.WXK_RIGHT, wx.WXK_NUMPAD_RIGHT,
            wx.WXK_PAGEUP, wx.WXK_NUMPAD_PAGEUP, wx.WXK_PAGEDOWN, wx.WXK_NUMPAD_PAGEDOWN,
        ):
            if key in (wx.WXK_LEFT, wx.WXK_NUMPAD_LEFT):
                self.seek_active_playback_by(-5)
            elif key in (wx.WXK_RIGHT, wx.WXK_NUMPAD_RIGHT):
                self.seek_active_playback_by(5)
            elif key in (wx.WXK_PAGEUP, wx.WXK_NUMPAD_PAGEUP):
                self.seek_active_playback_by(-60)
            elif key in (wx.WXK_PAGEDOWN, wx.WXK_NUMPAD_PAGEDOWN):
                self.seek_active_playback_by(60)
            return

        # Shift+Home/Shift+End: seek to the very start/end of the active
        # playback when something is actually playing (issue #17) — otherwise
        # select every message above/below the focused row and move focus to
        # the first/last row.
        if shift and key in (wx.WXK_HOME, wx.WXK_NUMPAD_HOME, wx.WXK_END, wx.WXK_NUMPAD_END):
            to_end = key in (wx.WXK_END, wx.WXK_NUMPAD_END)
            if self.seek_active_playback_to_edge(to_end=to_end):
                return
            if total > 0:
                idx0 = idx if idx >= 0 else 0
                lo, hi = (idx0, total - 1) if to_end else (0, idx0)
                was_active = bool(self.selected_messages)
                newly_selected = []
                for i in range(lo, hi + 1):
                    if self._select_message_at(i):
                        newly_selected.append(self._sorted_messages[i].get("key", {}).get("id", ""))
                target = total - 1 if to_end else 0
                self.messages_list.Focus(target)
                self.messages_list.Select(target, True)
                self.messages_list.EnsureVisible(target)
                if newly_selected:
                    self._refresh_message_rows_by_ids(newly_selected)
                    self.selection_sound.play()
                    self.main_window.output(
                        self._selection_mode_announcement(
                            self.main_window.i18n.t("selected"),
                            was_active, bool(self.selected_messages)),
                        interrupt=True)
            return

        # Shift+Down: extend the selection to the next row and move focus to it.
        if shift and key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN):
            target = (idx + 1) if idx >= 0 else 0
            if target < total:
                was_active = bool(self.selected_messages)
                self.messages_list.Focus(target)
                self.messages_list.Select(target, True)
                self.messages_list.EnsureVisible(target)
                if self._select_message_at(target):
                    self._refresh_message_rows_by_ids([self._sorted_messages[target].get("key", {}).get("id", "")])
                    self.selection_sound.play()
                    self.main_window.output(
                        self._selection_mode_announcement(
                            self.main_window.i18n.t("selected"),
                            was_active, bool(self.selected_messages)),
                        interrupt=True)
            return

        # Shift+Up: extend the selection to the previous row and move focus to
        # it — the upward mirror of Shift+Down above. Without this, Shift+Up
        # fell through to the plain WXK_UP branch further down (which ignores
        # `shift` and calls event.Skip()), leaving the native ListCtrl
        # selection to extend on its own. That never touches
        # self.selected_messages (what the mass actions actually act on) and
        # doesn't know the unread-separator row isn't a selectable message,
        # so selecting upward past it went out of sync with the visible
        # highlight.
        if shift and key in (wx.WXK_UP, wx.WXK_NUMPAD_UP):
            target = (idx - 1) if idx >= 0 else 0
            if target >= 0:
                was_active = bool(self.selected_messages)
                self.messages_list.Focus(target)
                self.messages_list.Select(target, True)
                self.messages_list.EnsureVisible(target)
                if self._select_message_at(target):
                    self._refresh_message_rows_by_ids([self._sorted_messages[target].get("key", {}).get("id", "")])
                    self.selection_sound.play()
                    self.main_window.output(
                        self._selection_mode_announcement(
                            self.main_window.i18n.t("selected"),
                            was_active, bool(self.selected_messages)),
                        interrupt=True)
            return

        # Ctrl+Shift+Space: select every message, or clear the selection if
        # everything selectable is already selected.
        if ctrl and shift and key == wx.WXK_SPACE:
            all_ids = self._all_selectable_message_ids()
            was_active = bool(self.selected_messages)
            if all_ids and all(mid in self.selected_messages for mid in all_ids):
                self.selected_messages.clear()
                self._refresh_message_rows_by_ids(all_ids)
                self.main_window.output(
                    self._selection_mode_announcement(
                        self.main_window.i18n.t("all_unselected"),
                        was_active, bool(self.selected_messages)),
                    interrupt=True)
            elif all_ids:
                self.selected_messages.update(all_ids)
                self._refresh_message_rows_by_ids(all_ids)
                self.selection_sound.play()
                self.main_window.output(
                    self._selection_mode_announcement(
                        self.main_window.i18n.t("all_selected"),
                        was_active, bool(self.selected_messages)),
                    interrupt=True)
            return

        # Plain Space: once a selection exists it keeps selecting ("selection
        # mode", issue #99) — otherwise it plays/pauses the focused audio or
        # video, which is what a095fca5 freed it for but never implemented.
        if key == wx.WXK_SPACE and not ctrl and not shift:
            if 0 <= idx < len(self._sorted_messages):
                msg = self._sorted_messages[idx]
                # Both branches fall through to Skip() when they did nothing —
                # the focused row can be the unread separator or carry no id,
                # and consuming Space there leaves the one key people press by
                # accident dead: no sound, no speech, no effect.
                if self._selection_mode_enabled() and self.selected_messages:
                    if self._toggle_message_selection(msg):
                        return
                elif self._space_toggles_playback(msg):
                    return
            event.Skip()
            return

        if ctrl and not shift and key == wx.WXK_SPACE:
            if idx >= 0 and idx < len(self._sorted_messages):
                self._toggle_message_selection(self._sorted_messages[idx])
        elif key in (wx.WXK_PAGEUP, wx.WXK_NUMPAD_PAGEUP):
            if idx <= 0 and not self._is_loading_more:
                if self._messages_offset > 0:
                    self._load_more_messages()
                else:
                    self._load_older_messages()
            elif total > 0 and idx > 0:
                target_idx = max(0, idx - step)
                self.messages_list.Focus(target_idx)
                self.messages_list.Select(target_idx, True)
                self.messages_list.EnsureVisible(target_idx)
        elif key in (wx.WXK_PAGEDOWN, wx.WXK_NUMPAD_PAGEDOWN):
            if total > 0 and idx >= 0:
                target_idx = min(total - 1, idx + step)
                self.messages_list.Focus(target_idx)
                self.messages_list.Select(target_idx, True)
                self.messages_list.EnsureVisible(target_idx)
        elif key in (wx.WXK_UP, wx.WXK_NUMPAD_UP, wx.WXK_HOME):
            if idx <= 0 and not self._is_loading_more:
                if self._messages_offset > 0:
                    self._load_more_messages()
                else:
                    self._load_older_messages()
            else:
                event.Skip()
        else:
            event.Skip()

    def _try_show_thumbnail(self, jpeg_b64: str):
        """Decode and display an inline JPEG thumbnail (base64-encoded)."""
        if not jpeg_b64:
            return
        try:
            jpeg_data = _b64.b64decode(jpeg_b64)
            stream    = wx.MemoryInputStream(jpeg_data)
            image     = wx.Image(stream, wx.BITMAP_TYPE_JPEG)
            if not image.IsOk():
                return
            w, h = image.GetWidth(), image.GetHeight()
            max_side = 200
            if w > max_side or h > max_side:
                ratio = min(max_side / w, max_side / h)
                image = image.Scale(
                    int(w * ratio), int(h * ratio), wx.IMAGE_QUALITY_HIGH
                )
            self._media_bitmap.SetMinSize((-1, -1))
            self._media_bitmap.SetBitmap(wx.Bitmap(image))
            self._media_bitmap.Show()
            self.conversation_panel.Layout()
        except Exception:
            pass

    def _show_reply_buttons(self, buttons: list, remote_jid: str):
        """Render interactive message buttons (buttonsMessage) in the container."""
        self._buttons_container.DestroyChildren()
        sizer = wx.WrapSizer(wx.HORIZONTAL)
        for btn_data in buttons:
            if not isinstance(btn_data, dict):
                continue
            label = (btn_data.get("buttonText") or {}).get("displayText", "").strip()
            if not label:
                continue
            btn = wx.Button(self._buttons_container, label=label)
            btn.Bind(
                wx.EVT_BUTTON,
                lambda e, d=btn_data, jid=remote_jid: self._on_reply_button(d, jid),
            )
            sizer.Add(btn, 0, wx.ALL, 4)
        self._buttons_container.SetSizer(sizer, True)
        self._buttons_container.Layout()
        self._buttons_container.Show()
        self.conversation_panel.Layout()

    def _show_list_rows(self, rows: list, remote_jid: str):
        """Render list-message rows as reply buttons."""
        self._buttons_container.DestroyChildren()
        sizer = wx.WrapSizer(wx.HORIZONTAL)
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = row.get("title", "").strip()
            if not label:
                continue
            btn = wx.Button(self._buttons_container, label=label)
            btn.Bind(
                wx.EVT_BUTTON,
                lambda e, r=row, jid=remote_jid: self._on_list_row_selected(r, jid),
            )
            sizer.Add(btn, 0, wx.ALL, 4)
        self._buttons_container.SetSizer(sizer, True)
        self._buttons_container.Layout()
        self._buttons_container.Show()
        self.conversation_panel.Layout()

    def _on_reply_button(self, btn_data: dict, remote_jid: str):
        label = (btn_data.get("buttonText") or {}).get("displayText", "").strip()
        if not label or not remote_jid:
            return
        threading.Thread(
            target=self.main_window.send_text_message,
            args=(remote_jid, label),
            daemon=True,
        ).start()

    def _on_list_row_selected(self, row: dict, remote_jid: str):
        label = row.get("title", "").strip()
        if not label or not remote_jid:
            return
        threading.Thread(
            target=self.main_window.send_text_message,
            args=(remote_jid, label),
            daemon=True,
        ).start()

    # ── Alt+2: jump to last message ────────────────────────────────────────

    def _on_accel_jump_last(self, event):
        """Alt+2: move focus to the last REAL message in the current
        conversation — never a sentinel row (unread separator/placeholder).
        The bottom row is a sentinel whenever the unread separator gets
        (re)placed at the very end of the list, e.g. right after
        on_incoming_message() creates a fresh one for a message that just
        arrived; skip backwards over any such rows instead of focusing them
        directly, or Alt+2 would land on the separator (or, before that,
        potentially on a stale earlier row) instead of the newest message.
        """
        if self._no_conversation_open_announced():
            return
        last = len(self._sorted_messages) - 1
        while last >= 0 and self._is_separator(self._sorted_messages[last]):
            last -= 1
        if last < 0:
            # Nothing but sentinel rows: the conversation is open but empty, so
            # the loop above walked off the top of the list. Alt+2 is defined as
            # "focus the last real message", and the empty-list placeholder is
            # not one (no more than the unread separator is) — so say the chat is
            # empty instead of focusing the placeholder, which is what issue #87
            # settled on. Silence here read as a dead shortcut.
            self.main_window.output(
                self.main_window.i18n.t("chat_is_empty"), interrupt=True
            )
            return
        if last >= 0:
            # Focus() (not just Select()) is what actually moves the
            # keyboard-focus/screen-reader cursor to the row — every other
            # "jump to a specific row" handler in this file calls both
            # together (see _on_accel_jump_unread() just below, and
            # populate_messages()'s own default-tail-selection block).
            # This one only ever called Select(), which on its own can
            # leave the previous row's focus rectangle in place or just
            # clear the old selection without moving anything — reported
            # live as Alt+2 "either staying put or just deselecting the
            # current message without really moving focus".
            self.messages_list.Focus(last)
            self.messages_list.Select(last, True)
            self.messages_list.EnsureVisible(last)
            self.messages_list.SetFocus()

    # ── Alt+3: jump to unread separator ────────────────────────────────────

    def _on_accel_jump_unread(self, event):
        i18n = self.main_window.i18n
        if self._no_conversation_open_announced():
            return
        if self._unread_sep_idx < 0 or self._unread_sep_idx >= self.messages_list.GetItemCount():
            self.main_window.output(i18n.t("no_unread_in_conv"), interrupt=True)
            return
        self.messages_list.Focus(self._unread_sep_idx)
        self.messages_list.Select(self._unread_sep_idx, True)
        self.messages_list.EnsureVisible(self._unread_sep_idx)
        self.messages_list.SetFocus()
        self.main_window.output(
            self.messages_list.GetItemText(self._unread_sep_idx),
            interrupt=True,
        )
        # mark_conversation_as_read is triggered by _on_message_focused which
        # fires when Focus() is called above — no need to call it here again.

    # ── Ctrl+0..9 / Ctrl+Shift+0..9: message bookmarks ──────────────────────
    # Bookmarks span conversations (see _msg_bookmarks' declaration in
    # __init__): jumping to one set in a conversation other than the one
    # currently open navigates there first, then focuses the bookmarked
    # message — never cleared by closing/switching conversations.

    def _find_index_by_msg_id(self, msg_id: str) -> int:
        """Return the current _sorted_messages index for a message ID, or -1."""
        if not msg_id:
            return -1
        for i, m in enumerate(self._sorted_messages):
            if (isinstance(m, dict) and not self._is_separator(m)
                    and m.get("key", {}).get("id") == msg_id):
                return i
        return -1

    def _conversation_position(self, jid: str) -> int:
        """1-based position of *jid* in the currently displayed (non-archived)
        conversation list, or 0 if it isn't currently shown there (archived,
        filtered out by search/the active filter, etc.). Chats reorder
        whenever new messages arrive, so this is always looked up live —
        never cached alongside a bookmark."""
        for i, chat in enumerate(self.chats_list):
            if chat.get("remoteJid", "") == jid:
                return i + 1
        return 0

    def _already_on_message_row(self, idx: int) -> bool:
        """True when the messages list already holds the keyboard focus, on
        exactly this row — i.e. jumping here would move nothing.

        Both conditions matter. The row cursor alone is not enough: it survives
        the user tabbing away to the message field, and in that state a jump
        does have work to do (bring focus back into the list), so announcing
        "you are already there" and stopping would strand the focus where it
        was. Only when the list itself is focused *and* sitting on the row is
        the jump genuinely a no-op.
        """
        try:
            return self.messages_list.GetFocusedItem() == idx and self.messages_list.HasFocus()
        except Exception:
            return False

    def _focus_message_row(self, idx: int):
        """Move focus + selection to a message row and scroll it into view.

        Focus() alone moves the screen-reader cursor without selecting, and
        Select() alone selects a row the keyboard cursor isn't on — both
        bookmark kinds want the row to become the one and only current
        message, which takes all four calls together.
        """
        self.messages_list.Focus(idx)
        self.messages_list.Select(idx, True)
        self.messages_list.EnsureVisible(idx)
        self.messages_list.SetFocus()
