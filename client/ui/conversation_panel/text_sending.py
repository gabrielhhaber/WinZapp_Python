"""TextSendingMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import copy
import logging
import threading
import time
import uuid
import wx
from core.message_queue import PendingMessage
from core.reply_failure import clear_reply_draft, is_missing_quote_failure
from ui.conversation_panel.typing_row import append_message_row, message_row_count
from app_paths import data_path
from ui.conversation_panel.media_paths import (
    discard_local_media_cache,
    promote_local_media_cache,
)
from core.message_edit import (
    edit_kind,
    edited_text_message,
    restore_edit_state,
    snapshot_edit_state,
)
from core.utils import normalize_line_separators, to_editor_line_endings


class TextSendingMixin:
    """Sending and editing text messages: virtual pending rows,
    sent/failed/unconfirmed marks and cancelled sends.
    """

    # ── Text message sending ─────────────────────────────────────────────────

    def on_send_message(self, event):
        """Send button handler: enqueue message, add to UI immediately as pending.
        If in edit mode, instead calls the edit API and updates the existing message."""
        if self.conversation is None:
            return
        text = normalize_line_separators(self.message_field.GetValue()).strip()
        if not text:
            return
        remote_jid = self.conversation.get("remoteJid", "")
        if not remote_jid:
            return

        # Guard against a single user action enqueueing the same message
        # twice — e.g. Enter's key-repeat firing EVT_TEXT_ENTER more than
        # once for what felt like one press, or a stray duplicate BUTTON/
        # TEXT_ENTER event. Each duplicate created its own pending message
        # and both went through independently, so the "sent" sound played
        # twice and the recipient got the text twice.
        now = time.monotonic()
        last = getattr(self, "_last_sent_signature", None)
        if last is not None:
            last_text, last_jid, last_time = last
            if last_text == text and last_jid == remote_jid and (now - last_time) < 1.5:
                return
        self._last_sent_signature = (text, remote_jid, now)

        # ── Edit mode: update existing message ──────────────────────────────
        if self._editing_message_id is not None:
            self._apply_message_edit(text, remote_jid)
            return

        # ── Normal send ──────────────────────────────────────────────────────
        # WhatsApp refuses Meta AI until its terms are accepted: ask first,
        # and keep the typed text if the user declines.
        if not self.main_window.ensure_meta_ai_terms(remote_jid):
            self._last_sent_signature = None
            return
        # Enter right after an emoticon never typed the boundary that would
        # have converted it in the field (emoticon_conversion.py). New sends
        # only: an edit keeps exactly the text the person corrected, so
        # saving an old message ending in ":/" does not quietly change it.
        text = self._text_with_trailing_emoticon(text)
        self._send_new_text_message(text, remote_jid)

    def _apply_message_edit(self, text: str, remote_jid: str):
        """Apply an edit to a message already sent: update it locally now and
        tell WhatsApp about it on a worker thread.

        Split out of on_send_message() so it can be tested without a live wx
        panel, and so the server call is visibly off the UI thread.
        """
        msg_id = self._editing_message_id

        # An edit goes through exactly the same @mention pipeline as a new
        # send. It used to skip it entirely: the raw "@DisplayName" text was
        # posted verbatim (so WhatsApp highlighted nothing — the mention was
        # only cosmetic) and the local record was rewritten as a plain
        # `conversation`, discarding any contextInfo it had. That is why
        # adding a mention with Alt+E never produced the hyperlinks that
        # lead to the mentioned person's chat, and why editing a message
        # that already had mentions silently dropped them.
        api_text, edit_mentions = self._build_mention_payload(text)

        # Re-locate the message by ID rather than trusting the row index
        # captured when edit mode was entered: a background sync can call
        # populate_messages() at any point while the user is typing,
        # which fully rebuilds _sorted_messages — the old index could by
        # then point at an unrelated row, silently overwriting a
        # different message's local content/cache with the edited text
        # (the server-side edit_message() call above is unaffected, since
        # it addresses the message by ID, not by index — only the local
        # display was at risk).
        idx = next(
            (i for i, m in enumerate(self._sorted_messages)
             if isinstance(m, dict) and m.get("key", {}).get("id") == msg_id),
            -1,
        )

        # Update local state
        snapshot = applied_message = None
        if 0 <= idx < len(self._sorted_messages):
            edited = self._sorted_messages[idx]
            # Taken before the optimistic rewrite, so a refusal from WhatsApp
            # can put the row back — see _rollback_message_edit().
            snapshot = snapshot_edit_state(edited)
            caption_key = next(
                (k for k in ("imageMessage", "videoMessage", "documentMessage")
                 if isinstance((edited.get("message") or {}).get(k), dict)),
                None,
            ) if edit_kind(edited) == "caption" else None
            if caption_key is not None:
                # A caption edit changes the caption and nothing else: the
                # media's URL, key, measured duration and cache stay put, and
                # the snapshot above already holds the whole body for a
                # rollback.
                #
                # Mentions are deliberately not carried on a caption edit: only
                # text rows resolve "@<phone>" back to a name
                # (_get_message_content()), so a caption stored with the
                # mention payload read the raw number out loud, and the send
                # path never attaches mentions to a caption either. The caption
                # is sent and kept exactly as typed.
                api_text, edit_mentions = text, None
                edited["message"][caption_key]["caption"] = text
                # Both places a mention list can live: top-level (local sends)
                # and inside the media body (anything normalised from sync).
                for ctx in (edited.get("contextInfo"),
                            edited["message"][caption_key].get("contextInfo")):
                    if isinstance(ctx, dict):
                        ctx.pop("mentionedJid", None)
                        ctx.pop("mentionedJidList", None)
            # edited_text_message() keeps a reply's quote when it lives inside
            # the body (every reply that came from sync or the phone), which a
            # bare rewrite to `conversation` silently dropped from the row.
            elif edit_mentions:
                # Same shape the send path builds for a mentioning message,
                # so _get_message_content() rewrites @phone → @DisplayName
                # and _extract_mentions() finds the JIDs for the hyperlinks.
                edited["message"], edited["messageType"] = edited_text_message(
                    edited, api_text, extended=True)
                ctx = edited.setdefault("contextInfo", {})
                ctx["mentionedJid"] = edit_mentions
            else:
                edited["message"], edited["messageType"] = edited_text_message(
                    edited, text)
                # An edit that removed every mention must clear the old list
                # too, or the stale hyperlinks stay on screen forever.
                ctx = edited.get("contextInfo")
                if isinstance(ctx, dict):
                    ctx.pop("mentionedJid", None)
                    ctx.pop("mentionedJidList", None)
            edited["_edited"] = True
            applied_message = copy.deepcopy(edited.get("message"))
            self.messages_list.SetItemText(
                idx, self._render_message_line(edited)
            )
            # _sorted_messages[idx] is the same dict object held in
            # main_window.chats[remote_jid]'s records (populate_messages()
            # builds it from there without copying) — persist it so the
            # "Editada" marker and new text survive a restart.
            self.main_window._schedule_save(dirty_jid=remote_jid)
            # Refresh the conversations list too — _last_msg_preview()
            # reads straight from these records, but nothing tells the
            # list widget to redraw the row on its own. Without this the
            # preview kept showing the pre-edit text until the
            # conversation was closed (which rebuilds the list from
            # scratch for an unrelated reason) — see the remote-edit
            # path (_apply_possible_edit(), main.py), which already
            # does this and never had the bug.
            self.main_window._schedule_set_chats()
            # Rebuild the links/mentions panels if the edited row is the one
            # currently focused — they are only refreshed on a focus change,
            # so without this the panels below the list keep describing the
            # message as it was before the edit.
            if self.messages_list.GetFocusedItem() == idx:
                self._update_links_panel(self._message_own_links(edited))
                self._update_mentions_panel(self._extract_mentions(edited))

        # Call WPPConnect to update the message — on a worker thread.
        # edit-message drives Puppeteer/WhatsApp Web and routinely takes a
        # second or two to come back (its own timeout is 15s); running it
        # inline here froze the whole window for that long on every edit,
        # the one server-backed message action still doing that. Started
        # after the local, optimistic update above (the same shape
        # _on_menu_pin_message() uses) only so the snapshot and the body it
        # wrote exist to hand over; a failure is reported through
        # wx.CallAfter, which cannot run before this handler returns anyway.
        if getattr(self, "_editing_is_caption", False):
            # Also when the row was not found above: a caption is never sent
            # with a mention payload, or its echo would store "@<phone>".
            api_text, edit_mentions = text, None
        threading.Thread(
            target=self._send_message_edit,
            args=(remote_jid, msg_id, api_text, edit_mentions, snapshot, applied_message),
            daemon=True,
        ).start()

        self._on_cancel_edit()

    def _send_message_edit(self, remote_jid, msg_id, api_text, edit_mentions,
                           snapshot, applied_message):
        """Worker: send the edit, and undo the optimistic update if refused.

        Only an explicit False is a refusal; None (a timeout, an error that may
        have come after the edit went out) keeps the optimistic text — see
        MainWindow.edit_message(). WhatsApp answers "Cannot edit this
        message" once the message is past its edit window, and before this the
        row kept the new text and the "Editada" marker while nobody else ever
        received the edit.
        """
        ok = self.main_window.edit_message(
            remote_jid, msg_id, api_text, mentioned_jids=edit_mentions)
        if ok is False:
            wx.CallAfter(self._rollback_message_edit, remote_jid, msg_id,
                         snapshot, applied_message)

    def _rollback_message_edit(self, remote_jid, msg_id, snapshot, applied_message):
        """Main thread: restore a refused edit's row and say it failed."""
        restored_idx = -1
        if snapshot is not None:
            candidates = [(i, m) for i, m in enumerate(self._sorted_messages)
                          if isinstance(m, dict)
                          and (m.get("key") or {}).get("id") == msg_id]
            chat = self.main_window.get_chat(remote_jid)
            records = ((chat or {}).get("messages", {}).get("messages", {})
                       .get("records", []))
            candidates += [(-1, r) for r in records
                           if isinstance(r, dict)
                           and (r.get("key") or {}).get("id") == msg_id
                           and all(r is not m for _, m in candidates)]
            for i, record in candidates:
                if restore_edit_state(record, snapshot, applied_message) and i >= 0:
                    restored_idx = i
            if restored_idx >= 0:
                restored = self._sorted_messages[restored_idx]
                self.messages_list.SetItemText(
                    restored_idx, self._render_message_line(restored))
                # Same as the apply path: the panels under the list only follow
                # focus changes, so a refused edit that added a mention would
                # otherwise keep offering it.
                if self.messages_list.GetFocusedItem() == restored_idx:
                    self._update_links_panel(self._message_own_links(restored))
                    self._update_mentions_panel(self._extract_mentions(restored))
            if candidates:
                self.main_window._schedule_save(dirty_jid=remote_jid)
                self.main_window._schedule_set_chats()
        self.main_window.output(
            self.main_window.i18n.t("edit_message_failed"), interrupt=True)

    def _send_new_text_message(self, text: str, remote_jid: str):
        """Queue a brand-new text message and show it as pending right away.

        The other half of on_send_message(), split out alongside
        _apply_message_edit() so neither branch hides inside the other.
        """
        # Build a virtual message dict that renders identically to real messages.
        local_id = str(uuid.uuid4())
        api_text, _mentioned = self._build_mention_payload(text)
        link_preview = self._pending_link_preview

        # When mentions or a resolved link preview are present, use
        # extendedTextMessage: the rendering pipeline needs it either way,
        # for @phone → @DisplayName resolution and for
        # _get_message_content()'s title/description rendering respectively.
        if _mentioned or link_preview:
            _ext = {"text": api_text}
            if link_preview:
                _ext["title"] = link_preview.get("title", "")
                _ext["description"] = link_preview.get("description", "")
                _ext["canonicalUrl"] = link_preview.get("canonicalUrl", "")
            _msg_type  = "extendedTextMessage"
            _msg_body  = {"extendedTextMessage": _ext}
        else:
            _msg_type  = "conversation"
            _msg_body  = {"conversation": text}

        virtual_msg = {
            "_local_pending": True,
            "_local_id":      local_id,
            "key": {
                "id":       local_id,
                "fromMe":   True,
                "remoteJid": remote_jid,
            },
            "messageType":      _msg_type,
            "message":          _msg_body,
            "messageTimestamp": int(time.time()),
            "pushName":         "",
        }
        if self._quoted_message:
            # Keep the composed text (including display names), not a rendered
            # quote/preview or a wire body with mention phone numbers.
            virtual_msg["_reply_draft_text"] = text
            virtual_msg["_reply_draft_mentions"] = list(self._pending_mentions)
            virtual_msg["_reply_draft_mention_names"] = dict(self._pending_mention_display_names)
            _qk = self._quoted_message.get("key", {})
            virtual_msg["contextInfo"] = {
                "stanzaId":      _qk.get("id", ""),
                "participant":   _qk.get("participant", ""),
                "quotedMessage": self._quoted_message.get("message") or {},
                "_quotedFromMe": bool(_qk.get("fromMe", False)),  # local hint for immediate render
            }
        if _mentioned:
            virtual_msg.setdefault("contextInfo", {})["mentionedJid"] = _mentioned

        # Add to sorted list and UI list immediately.
        self._clear_empty_placeholder()
        self._sorted_messages.append(virtual_msg)
        append_message_row(self, self._render_message_line(virtual_msg))
        # Scroll to the new item.
        last = message_row_count(self) - 1   # the row just sent, not the typing row
        if last >= 0:
            self.messages_list.EnsureVisible(last)

        # Clear any pending @mentions before clearing the field.
        self._pending_mentions.clear()
        self._pending_mention_display_names.clear()
        self._hide_mention_suggestions()
        self._rebuild_mention_pills()

        # Clear the text field (this also hides send btn, shows record btn).
        self.message_field.SetValue("")
        self.message_field.SetFocus()

        # Enqueue for background sending (with retry on failure).
        pm = PendingMessage(
            local_id, remote_jid, text=api_text,
            quoted=self._quoted_message,
            mentioned_jids=_mentioned,
            link_preview=link_preview,
        )
        self.main_window.message_queue.enqueue(pm)
        self._on_cancel_reply()  # clear quoted state after send
        self._link_preview_dismissed_url = ""  # fresh field, nothing dismissed yet
        self._clear_link_preview()
        # Replying is a clear signal the conversation has been read — clears
        # the unread badge/title/tray count and notifies WPPConnect, even in
        # the edge case where unreadCount is still nonzero for the chat
        # that's open right now (e.g. the window was minimized when a
        # message arrived, so the open-conversation suppression in
        # on_new_message() never applied).
        self.main_window.mark_conversation_as_read(remote_jid)

        # Register the virtual message in chat records so the conversation
        # list preview updates immediately to show the sent message.
        self._register_virtual_msg(virtual_msg)
        self.main_window._schedule_set_chats()

    def _build_mention_payload(self, text: str):
        """Turn the composed text + pending @mentions into what the API needs.

        Returns ``(api_text, mentioned_jids_or_None)``:

        * WhatsApp only highlights a mention when the message body contains
          ``@{phonenumber}``, never ``@{display_name}`` — so each inserted
          ``@DisplayName`` is swapped back to ``@phone`` here.
        * The JID list is canonicalised (``@lid`` → phone) because that is the
          form the send/edit endpoints tag against.

        Shared by the normal-send and the edit paths so an edit can never again
        end up posting a mention WhatsApp does not recognise.
        """
        raw_mentions = list(self._pending_mentions) if self._pending_mentions else []
        if not raw_mentions:
            return text, None

        mentioned = raw_mentions
        if hasattr(self.main_window, "_canonical_mention_jids"):
            mentioned = self.main_window._canonical_mention_jids(raw_mentions)

        api_text = text
        _normalize = getattr(self.main_window, "_normalize_jid", lambda j: j)
        _lid_map   = getattr(self.main_window, "_lid_to_phone", {})
        for raw_jid in raw_mentions:
            display = self._pending_mention_display_names.get(raw_jid, "")
            if not display:
                continue
            if raw_jid.endswith("@lid"):
                phone = _lid_map.get(raw_jid, raw_jid).split("@")[0]
            else:
                phone = _normalize(raw_jid).split("@")[0]
            if phone and f"@{display}" in api_text:
                api_text = api_text.replace(f"@{display}", f"@{phone}", 1)

        return api_text, (mentioned or None)

    def _register_virtual_msg(self, virtual_msg: dict):
        """
        Add a just-sent virtual message to the chat's records dict so that
        _last_msg_preview() can pick it up and set_chats() shows the correct
        preview in the conversation list.

        Because virtual_msg is the *same* Python dict object that sits in
        _sorted_messages, clearing _local_pending later (in _mark_message_sent)
        automatically updates the records entry too.
        """
        if self._unread_sep_idx >= 0:
            self._dismiss_unread_separator()
        # Fora do if de propósito: _dismiss_unread_separator() era o único
        # ponto do caminho de envio que largava a âncora, e ela deixou de ser
        # volátil. Com _unread_sep_idx == -1 e a âncora ainda gravada —
        # alcançável quando _place_unread_separator_for_rebuild() não a
        # encontra num records transitoriamente vazio, ou depois de um
        # _recompute_unread_sep_idx() que não achou a linha — o envio não
        # limpava nada e o rebuild seguinte RESSUSCITAVA o separador acima da
        # mensagem que o usuário acabou de mandar. Enviar apaga o separador,
        # sempre; é também o que mantém o Alt+2 pousando na mensagem certa.
        self._first_unread_msg_id = None
        self._first_unread_count = 0
        remote_jid = virtual_msg.get("key", {}).get("remoteJid", "")
        if not remote_jid:
            return
        chat = self.main_window.get_chat(remote_jid)
        if chat is None:
            return
        records = (
            chat.setdefault("messages", {})
                .setdefault("messages", {})
                .setdefault("records", [])
        )
        local_id = virtual_msg.get("_local_id", "")
        if local_id:
            self._outgoing_virtual_messages[local_id] = virtual_msg
        if local_id and any(r.get("_local_id") == local_id for r in records):
            return  # already registered
        records.append(virtual_msg)
        
        # Update chat timestamp (t) so the sending chat floats to the top immediately
        msg_ts = int(virtual_msg.get("messageTimestamp", 0) or time.time())
        if msg_ts > 1_000_000_000_000:
            msg_ts //= 1000
        current_t = int(chat.get("t", 0) or 0)
        if current_t > 1_000_000_000_000:
            current_t //= 1000
        if msg_ts > current_t:
            chat["t"] = msg_ts

    def _mark_message_sent(self, local_id: str, real_id: str = None, quote_lost: bool = False):
        """
        Called on the main thread when a queued message is successfully delivered.
        Clears the _local_pending flag, refreshes the list item, plays the
        message-sent sound, and refreshes the conversation list preview.
        real_id (the WhatsApp message ID returned by the API) replaces the local
        UUID in the virtual message's key so that media playback can later look
        up the message in the WPPConnect API database.
        quote_lost=True means the quoted send failed server-side and the message
        went out as a plain send (send_text_message's fallback): the virtual
        message's reply contextInfo is dropped so the row stops reading as a
        reply — the quote never actually reached the recipient.

        A missing/non-string real_id means the send itself succeeded (this is
        only ever called after one did) but its real WhatsApp id couldn't be
        parsed out of the API response. Finalising the row here regardless
        used to strand it permanently: on_new_message()'s later echo match
        only ever considers rows still marked pending, so this call was the
        one and only chance a message like that got to be linked to its real
        id — every one after it landed as a brand new, separately-stored
        duplicate instead of resolving the original. Returning without
        touching the row leaves it pending, so the echo (which always does
        carry the real id) resolves it via a second call to this same method,
        exactly as if this inconclusive one had never happened.
        """
        # The transfer itself is over even when the id is not knowable — the
        # send succeeded, only its response was unparseable. So the gauge and
        # the "this row has a transfer in progress" marker come down either
        # way; leaving them up strands a finished upload on screen, and
        # _sync_pending_document_gauge() (which keys off _media_transfer_started
        # plus _local_pending) re-shows it every time the row is selected.
        self._hide_media_transfer_gauge()
        self._media_transfer_started.discard(local_id)
        if not (real_id and isinstance(real_id, str)):
            # Pin the row at 100% rather than popping the entry: the row stays
            # pending on purpose (see above), and _render_message_line's
            # pending clause falls back to .get(local_id, 0.0) — popping would
            # make a just-finished upload announce as ", enviando 0%".
            if local_id in self._media_upload_progress:
                self._media_upload_progress[local_id] = 1.0
            return
        tracked = self._outgoing_virtual_messages.pop(local_id, None)
        if tracked is not None:
            tracked["_local_pending"] = False
            clear_reply_draft(tracked)
            if real_id and isinstance(real_id, str):
                tracked.setdefault("key", {})["id"] = real_id
        self._media_upload_progress.pop(local_id, None)
        self._upload_stages_seen.pop(local_id, None)
        # Panel-level guard: survive _sorted_messages rebuilds that replace dict
        # objects, keeping the per-dict _ui_sent flag from being seen by both callers.
        _played = getattr(self, "_played_sent_local_ids", None)
        if _played is None:
            self._played_sent_local_ids: set = set()
            _played = self._played_sent_local_ids
        if local_id in _played:
            return
        _played.add(local_id)
        if len(_played) > 500:
            _played.clear()

        for i, msg in enumerate(self._sorted_messages):
            if msg.get("_local_id") == local_id:
                if msg.get("_ui_sent"):
                    return  # Already marked sent on the UI, ignore to prevent duplicate sound and actions
                msg["_ui_sent"] = True
                msg["_local_pending"] = False
                clear_reply_draft(msg)
                # The quoted send failed and the message went out as a plain
                # send: drop the reply contextInfo so the row no longer reads
                # as "respondendo a …". The quote never reached the recipient.
                if quote_lost:
                    msg.pop("contextInfo", None)
                # Replace the local UUID with the real WhatsApp message ID so
                # get_base64_from_media can find the message in the DB later.
                if real_id and isinstance(real_id, str):
                    msg.setdefault("key", {})["id"] = real_id
                    # Rename the local audio file (voice_messages/<id>.msv) and
                    # the pre-cached attachment (media/<id>.wzmedia, written by
                    # _pre_cache_sent_media()) onto the real id, so playback and
                    # Open/Save As find them without a redundant download.
                    # Kept inside a catch-all, as the two inline blocks this
                    # replaced were: a failure to resolve the data dir here must
                    # never stop the row from being marked as sent.
                    try:
                        promote_local_media_cache(
                            data_path("voice_messages"), data_path("media"),
                            local_id, real_id,
                        )
                    except Exception:
                        logging.warning(
                            "[_mark_message_sent] failed to rename the local "
                            "copies of %s", local_id, exc_info=True,
                        )
                    if getattr(self, "_current_audio_id", None) == local_id:
                        self._current_audio_id = real_id
                    if hasattr(self, "_audio_positions") and local_id in self._audio_positions:
                        self._audio_positions[real_id] = self._audio_positions.pop(local_id)
                    # For audio messages, kick off background download now that
                    # we have the real ID the WPPConnect API can look up.
                    if msg.get("messageType") == "audioMessage":
                        import threading as _threading
                        _threading.Thread(
                            target=self.main_window.sync_if_media,
                            args=(msg,),
                            daemon=True,
                        ).start()
                self.messages_list.SetItemText(i, self._render_message_line(msg))
                # Play sent sound — fires only when the originating conversation
                # is still the active one (otherwise local_id is not found here).
                # Recorded voice messages are excluded: their sound is played by
                # _on_message_sent at API-confirmation time, guaranteeing it
                # fires even if the user navigated away during the upload. An
                # audio FILE sent via the attachment picker is also messageType
                # "audioMessage" but never goes through that recording-specific
                # path (it has no audio_path, only media_path) — excluding it
                # here too meant it never got a sent sound from anywhere.
                if hasattr(self.main_window, "message_sent_sound"):
                    if not msg.get("_is_voice_recording"):
                        self.main_window.message_sent_sound.play()
                if self.conversation:
                    self.main_window._schedule_save(dirty_jid=self.conversation.get("remoteJid"))
                break
        # Refresh conversation list so the preview reflects the sent message.
        self.main_window._schedule_set_chats()


    def _on_restore_unsent_reply(self, msg: dict):
        """Explicit recovery only: never overwrite another draft or send here."""
        if not is_missing_quote_failure(msg):
            return
        # The record keeps the address it was sent to; after an @lid chat is
        # re-keyed to its phone JID the two differ but are the same chat.
        conv_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if not conv_jid or not self.main_window._chat_jids_equivalent(
                (msg.get("key") or {}).get("remoteJid", ""), conv_jid):
            return
        if (self.message_field.GetValue() or self._quoted_message
                or self._editing_message_id is not None or self._pending_mentions):
            self.main_window.output(self.main_window.i18n.t("reply_restore_draft_busy"), interrupt=False)
            return
        text = msg.get("_reply_draft_text")
        if not isinstance(text, str) or not text:
            return
        self._pending_mention_display_names = dict(msg.get("_reply_draft_mention_names") or {})
        self._pending_mentions = list(msg.get("_reply_draft_mentions") or [])
        self._last_sent_signature = None  # This explicit retry is a new user action.
        self._rebuild_mention_pills()
        self.message_field.SetValue(to_editor_line_endings(text))
        self.message_field.SetInsertionPointEnd()
        self.message_field.SetFocus()

    def _mark_message_failed(self, local_id: str):
        """Mark a virtual pending message as permanently failed (exhausted retries)."""
        self._hide_media_transfer_gauge()
        self._outgoing_virtual_messages.pop(local_id, None)
        for i, msg in enumerate(self._sorted_messages):
            if msg.get("_local_id") == local_id:
                msg["_local_pending"] = False
                msg["_send_failed"]   = True
                self.messages_list.SetItemText(i, self._render_message_line(msg))
                if self.conversation:
                    self.main_window._schedule_save(dirty_jid=self.conversation.get("remoteJid"))
                break

    def _mark_message_unconfirmed(self, local_id: str):
        """Mark a virtual message whose send timed out with an unknown outcome.

        Deliberately not "failed": WhatsApp Web may still flush it from its own
        outbox, and if it does the WebSocket echo replaces this bubble with the
        real message. Until then the row must not claim to be sent — it was left
        as "sending" forever before, which reads as success once the spinner
        stops meaning anything.
        """
        self._hide_media_transfer_gauge()
        self._outgoing_virtual_messages.pop(local_id, None)
        for i, msg in enumerate(self._sorted_messages):
            if msg.get("_local_id") == local_id:
                msg["_local_pending"]     = False
                msg["_send_unconfirmed"]  = True
                self.messages_list.SetItemText(i, self._render_message_line(msg))
                try:
                    self.messages_list.RefreshItem(i)
                except Exception:
                    pass
                if self.conversation:
                    self.main_window._schedule_save(dirty_jid=self.conversation.get("remoteJid"))
                break

    def _remember_cancelled_pending(self, local_id: str, msg: dict):
        """Stash the virtual message of a row deleted while it was still pending."""
        if not local_id or not isinstance(msg, dict):
            return
        self._cancelled_pending_messages[local_id] = msg
        # Most cancellations really do stop the send, and then nothing ever comes
        # back to clear the entry — bound the map instead of growing it for a
        # whole session.
        while len(self._cancelled_pending_messages) > 50:
            self._cancelled_pending_messages.pop(
                next(iter(self._cancelled_pending_messages))
            )

    def _is_cancelled_pending(self, local_id: str) -> bool:
        """True while a cancelled message is still waiting to find out whether
        its send reached WhatsApp anyway.  Read by MainWindow._on_message_sent()
        to route a send that outran its own cancellation.
        """
        return bool(local_id) and local_id in self._cancelled_pending_messages

    def discard_cancelled_message(self, local_id: str):
        """Drop a cancelled message for good — the queue confirmed it never went.

        Releases the record _cancel_pending_message() was holding as the echo's
        anchor. Nothing is announced: this is simply the cancellation the user
        asked for, having worked.
        """
        msg = self._cancelled_pending_messages.pop(local_id, None)
        self._forget_cancelled_record(local_id, msg)
        discard_local_media_cache(
            data_path("voice_messages"), data_path("media"), local_id
        )

    def complete_cancelled_message_delivery(self, local_id: str, real_id: str,
                                            remote_jid: str = "",
                                            quote_lost: bool = False,
                                            ambiguous: bool = False):
        """Finish cancelling a message that reached WhatsApp before the cancel did.

        The row is already gone from the list and the message's own record is
        still standing in for it (see _cancel_pending_message), so the echo
        cannot be mistaken for anything else. What is left is to make the
        recipient's copy match what the user asked for: revoke it. A revoke that
        fails is NOT swallowed — the row is restored instead, because a
        delivered message the app pretends to have cancelled is worse than a
        cancellation that visibly failed.

        `ambiguous` is a third outcome, not a flavour of the other two: a
        timeout leaves no ID to revoke AND no promise that anything went out.
        """
        msg = self._cancelled_pending_messages.get(local_id)
        jid = remote_jid or (msg or {}).get("key", {}).get("remoteJid", "")
        if not real_id:
            # Two different things arrive here, told apart by `ambiguous`:
            #   * the send answered {"ok": True} with no ID (main.py's "ID not
            #     found in response", and the quote fallbacks) — it definitely
            #     went out, so the echo is coming and the row goes back exactly
            #     as it was, still pending, for that echo to claim;
            #   * the send timed out — it may never have gone out at all, so the
            #     row must NOT stay a pending anchor.
            # Either way there is nothing to revoke.
            logging.warning(
                "[conversations] cancelled %s was delivered without a real ID "
                "(ambiguous=%s)", local_id, ambiguous,
            )
            self._restore_cancelled_message(local_id, "", quote_lost, ambiguous)
            return

        msg_key = dict((msg or {}).get("key") or {})
        msg_key.update({"remoteJid": jid, "fromMe": True, "id": real_id})

        def _revoke(k=msg_key, j=jid, lid=local_id, rid=real_id, ql=quote_lost):
            try:
                ok = self.main_window.delete_message_for_everyone(j, k)
            except Exception:
                logging.exception(
                    "[conversations] revoking cancelled message %s raised", lid
                )
                ok = False
            wx.CallAfter(
                self._finish_cancelled_message_delivery, lid, rid, bool(ok), ql
            )
        threading.Thread(target=_revoke, daemon=True).start()

    def _finish_cancelled_message_delivery(self, local_id: str, real_id: str,
                                           revoked: bool, quote_lost: bool = False):
        """Announce how the revoke of a cancelled-but-delivered message went."""
        if not revoked:
            # A revoke is only ever attempted with a real ID, so this restore is
            # never the ambiguous one.
            self._restore_cancelled_message(local_id, real_id, quote_lost)
            return
        msg = self._cancelled_pending_messages.pop(local_id, None)
        # The echo may have already claimed the record and given it the real ID,
        # so drop both spellings of it.
        self._forget_cancelled_record(local_id, msg, real_id)
        discard_local_media_cache(
            data_path("voice_messages"), data_path("media"), local_id
        )
        self.main_window.output(
            self.main_window.i18n.t("cancelled_message_revoked"), interrupt=False
        )

    def _forget_cancelled_record(self, local_id: str, msg: dict, real_id: str = ""):
        """Remove a held cancelled record from its chat and from the DB."""
        remote_jid = (msg or {}).get("key", {}).get("remoteJid", "")
        if not remote_jid:
            return   # nothing was being held for this message
        chat = self.main_window.get_chat(remote_jid)
        if chat is not None:
            # setdefault on the way in as well as out: _cancel_pending_message()
            # returns early without ever creating these keys when the record is
            # not in the chat, and reading with .get() while writing with [] then
            # raises KeyError on a chat that has no messages block at all.
            records = (
                chat.setdefault("messages", {})
                    .setdefault("messages", {})
                    .setdefault("records", [])
            )
            chat["messages"]["messages"]["records"] = [
                r for r in records if r.get("_local_id") != local_id
            ]
        for msg_id in {local_id, real_id} - {""}:
            try:
                self.main_window.db.delete_message(remote_jid, msg_id)
            except Exception:
                logging.exception(
                    "[conversations] delete_message failed for %s", msg_id
                )
        self.main_window._recompute_chat_last_message(remote_jid)
        self.main_window._schedule_set_chats()

    def _restore_cancelled_message(self, local_id: str, real_id: str,
                                   quote_lost: bool = False,
                                   ambiguous: bool = False):
        """Put back a row whose cancellation could not be completed.

        The message is on the recipient's phone and could not be revoked, so it
        goes back into the conversation as the ordinary sent message it actually
        is — under its real ID, which is what makes a later retry of "delete for
        everyone", a quote or a delivery-status update land on it.

        With no real ID the row can come back in one of two states, and the
        difference matters more than it looks: a send that reported success
        still has an echo coming, so it stays pending for that echo to claim,
        while a send that timed out may never produce one — and a row left
        pending forever is an anchor that the NEXT message's echo matches first
        (on_new_message() takes the first pending record of the type), handing
        this message's row the next message's WhatsApp ID.
        """
        msg = self._cancelled_pending_messages.pop(local_id, None)
        if msg is None:
            # The record is gone (evicted from the stash, or the panel was
            # rebuilt): the row cannot come back, but the user must still not be
            # left believing a delivered message was cancelled.
            logging.error(
                "[conversations] cancelled %s could not be revoked, and its "
                "record is no longer available to restore", local_id,
            )
            self.main_window.output(
                self.main_window.i18n.t(
                    "cancelled_message_unconfirmed" if ambiguous
                    else "cancelled_message_still_sent"
                ),
                interrupt=False,
            )
            return
        msg.pop("_cancelled_awaiting_id", None)
        remote_jid = msg.get("key", {}).get("remoteJid", "")
        if real_id:
            msg["_local_pending"] = False
            msg.setdefault("key", {})["id"] = real_id
            clear_reply_draft(msg)  # delivered, as in _mark_message_sent()
            if quote_lost:
                # The quoted send failed server-side and it went out as a plain
                # message: the row must stop reading as a reply, exactly as
                # _mark_message_sent() does for the ordinary path.
                msg.pop("contextInfo", None)
            # The pre-cached copies still sit under the local UUID: rename them
            # so playback/Save As find them instead of downloading again.
            # Guarded like the identical call in _mark_message_sent(): this runs
            # after the stash was already popped, so letting a disk error out of
            # here would abort the restore with no row back and nothing spoken —
            # a re-download is a far smaller loss than a silent disappearance.
            try:
                promote_local_media_cache(
                    data_path("voice_messages"), data_path("media"), local_id, real_id
                )
            except Exception:
                logging.warning("[cancel] could not promote cached media for %s",
                                local_id, exc_info=True)
        elif ambiguous:
            # Exactly what _mark_message_unconfirmed() does for a send that was
            # never cancelled, and for the same reason: an unresolved ambiguous
            # send must stop being a pending anchor. The row reads "not
            # confirmed" rather than "sending", which is also the truth.
            msg["_local_pending"]    = False
            msg["_send_unconfirmed"] = True
        msg_id = msg.get("key", {}).get("id", "")

        chat = self.main_window.get_chat(remote_jid)
        if chat is not None:
            records = (
                chat.setdefault("messages", {})
                    .setdefault("messages", {})
                    .setdefault("records", [])
            )
            if not any(r.get("key", {}).get("id") == msg_id for r in records):
                records.append(msg)
        if real_id:
            try:
                self.main_window.db.insert_message(remote_jid, msg)
            except Exception:
                logging.exception(
                    "[conversations] could not re-store restored message %s", msg_id
                )
        else:
            # Deliberately not persisted without a real ID: the stored copy
            # would be keyed by a local UUID nothing can ever look up again,
            # surviving restarts with no queue left to resolve it. It is in
            # records, so it is visible and can be deleted again for as long as
            # this session lasts; the echo claiming it is what gives it an ID
            # worth storing (and on_new_message() stores it then).
            logging.info(
                "[conversations] restored %s has no real ID — not persisting it",
                local_id,
            )
        # Renders the row again when this conversation is the open one, and is a
        # no-op otherwise — the same path a message sent from a linked device
        # takes, which is exactly what this message now is.
        self.on_incoming_message(remote_jid, msg)
        self.main_window._recompute_chat_last_message(remote_jid)
        self.main_window._schedule_set_chats()
        self.main_window.output(
            self.main_window.i18n.t(
                "cancelled_message_unconfirmed" if ambiguous
                else "cancelled_message_still_sent"
            ),
            interrupt=False,
        )
