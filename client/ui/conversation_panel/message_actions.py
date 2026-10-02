"""MessageActionsMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import threading
import time
import uuid
import wx
from core.message_edit import (
    EDIT_UI_WINDOW_SECONDS,
    edit_kind,
    edit_window_open,
)
from core.message_queue import PendingMessage
from app_paths import data_path
from ui.conversation_panel.media_paths import discard_local_media_cache
from core.utils import to_editor_line_endings


class MessageActionsMixin:
    """Message actions that change state: star, pin, delete, cancel, edit and
    resend.
    """

    def _persist_message_local_flag(self, jid: str, msg: dict):
        """Persist a message-level, locally-mutated field (e.g. "starred",
        "pinInChat") to that message's own row in the database.

        _schedule_save() only ever calls db.upsert_chat() — it writes chat
        metadata (name, unreadCount, last message preview, ...), never an
        individual row in the messages table. Meanwhile navigate_to_conversation()
        unconditionally reloads a conversation's messages fresh from the
        database every time it's opened. Without this, a flag toggled here
        lived only in the in-memory dict — correct until the user left and
        reopened the conversation (or any resync replaced the in-memory
        records), at which point it silently reverted, e.g. a starred
        message's context-menu item going back to "Favoritar" instead of
        staying "Desfavoritar".

        Runs on a background thread — db.insert_message() blocks the caller
        until the write completes (see DatabaseBridge), and this is always
        called from a UI event handler.
        """
        self._persist_message_local_flags(jid, [msg])

    def _persist_message_local_flags(self, jid: str, msgs: list):
        """Bulk form of _persist_message_local_flag() — persists several
        messages' locally-mutated flags on ONE background thread.

        The single-message version delegates here so both share one code
        path. It exists because the mass actions apply a flag to an entire
        selection at once: doing that through the single-message helper spun
        up one thread per message, and every one of them then blocked on the
        same serialized DatabaseBridge connection anyway (see its docstring —
        writes go through a single connection with a per-write asyncio.Lock),
        so the threads bought nothing and only multiplied.
        """
        db = getattr(self.main_window, "db", None)
        if db is None or not msgs:
            return
        def _do(j=jid, records=[dict(m) for m in msgs]):
            for record in records:
                try:
                    db.insert_message(j, record)
                except Exception as exc:
                    logging.warning("[_persist_message_local_flag] failed for %s: %s", j, exc)
        threading.Thread(target=_do, daemon=True).start()

    def _on_menu_star(self, msg: dict):
        if self._reject_system_event_action(msg):
            return
        msg["starred"] = not msg.get("starred")
        jid = self.conversation.get("remoteJid", "")
        if jid:
            self.main_window._schedule_save()
            self._persist_message_local_flag(jid, msg)
            self._repaint_or_repopulate([msg.get("key", {}).get("id", "")])

    def _on_menu_pin_message(self, msg: dict):
        """Pin/unpin a message via WhatsApp's own message-pin feature.

        Unlike _on_menu_star (a local-only flag), this is visible to every
        other participant in the chat, so it goes through the WPPConnect API
        — applied optimistically like conversation pin/unpin
        (_sync_pin_to_server), and rolled back if the server rejects it.
        """
        if self._reject_system_event_action(msg):
            return
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if not jid:
            return
        pin = not bool(msg.get("pinInChat"))
        msg["pinInChat"] = pin
        self.main_window._schedule_save()
        self._persist_message_local_flag(jid, msg)
        self._repaint_or_repopulate([msg.get("key", {}).get("id", "")])

        msg_key = dict(msg.get("key", {}))

        def _do(m=msg, k=msg_key, j=jid, p=pin):
            ok = self.main_window.pin_message(j, k, p)
            if not ok:
                wx.CallAfter(self._on_pin_message_failed, m, p)

        threading.Thread(target=_do, daemon=True).start()

    def _on_pin_message_failed(self, msg: dict, attempted_pin: bool):
        """Roll back an optimistic pin/unpin the server rejected (main thread)."""
        msg["pinInChat"] = not attempted_pin
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if jid:
            self._persist_message_local_flag(jid, msg)
        self.main_window._schedule_save()
        self._repaint_or_repopulate([msg.get("key", {}).get("id", "")])
        i18n = self.main_window.i18n
        wx.MessageBox(
            i18n.t("pin_message_failed" if attempted_pin else "unpin_message_failed"),
            i18n.t("pin_message"),
            wx.OK | wx.ICON_WARNING,
        )

    def _confirm_local_only_delete(self, count: int) -> bool:
        """Plain Delete/Cancel confirmation for a delete whose scope is
        already fixed to "for me only" — the "Me" chat's only real option
        (issue #73: "for everyone" is a no-op there). There is no scope left
        to choose, only the delete itself to confirm (issue #95). Shared by
        the single-message self-chat branch of _on_menu_delete_message() and
        the bulk self-chat branch of _on_mass_delete_messages().

        OK/Cancel rather than Yes/No, for two reasons that both matter to a
        keyboard-only user. wxMSW only sets the task dialog's
        "allow cancellation" flag when wxCANCEL is present, so a wxYES_NO
        prompt cannot be dismissed with Escape — this one is reached by a
        keystroke on a focused message, and would have been the single
        dialog in the app that swallows Escape. And wxCANCEL_DEFAULT keeps
        the destructive button off the default: Enter must not carry
        straight through from the message list into the delete, the same
        reasoning tests/test_update_dialog_default_button.py pins for the
        updater's own dialog.

        Both labels carry a mnemonic (delete_msg_confirm_yes is the
        Alt-accelerated form of delete_message): giving Cancelar an
        accelerator the other button lacks would leave the two buttons
        reachable in different ways."""
        i18n = self.main_window.i18n
        title = i18n.t("delete_message") if count == 1 else i18n.t("delete_messages_bulk_title")
        prompt = (
            i18n.t("delete_msg_confirm") if count == 1
            else i18n.t("delete_msg_confirm_bulk").format(count=count)
        )
        dlg = wx.MessageDialog(
            self, prompt, title,
            wx.OK | wx.CANCEL | wx.CANCEL_DEFAULT | wx.ICON_QUESTION,
        )
        dlg.SetOKCancelLabels(i18n.t("delete_msg_confirm_yes"), i18n.t("cancel"))
        result = dlg.ShowModal()
        dlg.Destroy()
        return result == wx.ID_OK

    def _on_menu_delete_message(self, index: int):
        """Show delete-scope dialog and delete locally or for everyone.

        The self-chat ("Me") skips the dialog entirely and always deletes
        locally only — see the is_self_chat check below (issue #73)."""
        if index < 0 or index >= len(self._sorted_messages):
            return
        if self._is_separator(self._sorted_messages[index]):
            return
        msg    = self._sorted_messages[index]
        msg_id = msg.get("key", {}).get("id", "")
        i18n   = self.main_window.i18n

        msg_key = msg.get("key", {})
        from_me = msg_key.get("fromMe", False)
        # Deleting a group notice locally is legitimate (it just hides the row),
        # so this is the one action system events keep. "For everyone" is not:
        # WhatsApp has no revoke for its own notices, so the request would only
        # fail after the row already looked deleted. Both the fromMe path and
        # the group-admin path below are excluded.
        is_system = self._is_system_event(msg)
        # The "Me" chat (messages to yourself) has only one participant —
        # WhatsApp's own revoke is a no-op there: the message disappears
        # locally, the API call returns success, but the message is still on
        # every other linked device, and reappears in WinZapp itself after
        # the next resync. Offering "delete for everyone" here just misleads
        # the user into thinking it worked (issue #73) — go straight to a
        # plain local delete instead, same as this chat's only real option.
        conv_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        is_self_chat = bool(conv_jid) and self.main_window._is_self_jid(conv_jid)
        can_delete_for_all = from_me and not is_system and not is_self_chat

        # There is only one scope possible here ("for me"), so there is
        # nothing to choose — but a delete is still a delete, and used to fire
        # with zero confirmation of any kind (issue #95). A plain Delete/
        # Cancel prompt replaces the for-me/for-everyone dialog below, which
        # would be misleading anyway (see the comment above).
        if is_self_chat and not is_system:
            if self._confirm_local_only_delete(1):
                self._delete_message_for_me_only(msg, msg_id, index)
            return

        if not can_delete_for_all and not is_system and self.conversation:
            if conv_jid.endswith("@g.us"):
                group_meta = self.conversation.get("groupMetadata", {})
                participants = group_meta.get("participants") or self.conversation.get("participants") or []

                def _phone_part(j: str) -> str:
                    return j.rsplit("@", 1)[0].split(":")[0] if isinstance(j, str) else ""

                my_phone = _phone_part(getattr(self.main_window, "my_jid", ""))
                my_lid   = _phone_part(getattr(self.main_window, "my_lid", ""))

                for p in participants:
                    if isinstance(p, dict):
                        p_id = p.get("id", "")
                        if isinstance(p_id, dict):
                            p_id = p_id.get("_serialized", "")
                        p_digits = _phone_part(p_id)
                        if p_digits:
                            is_me = (my_phone and self.main_window._phone_digits_equivalent(p_digits, my_phone)) or (my_lid and p_digits == my_lid)
                            if is_me:
                                if p.get("admin") or p.get("isAdmin"):
                                    can_delete_for_all = True
                                break

        # ── Ask the user: delete for me only, or for everyone ─────────────────
        dlg = wx.Dialog(
            self,
            title=i18n.t("delete_message"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        panel  = wx.Panel(dlg)
        sizer  = wx.BoxSizer(wx.VERTICAL)

        rb_me  = wx.RadioButton(panel, label=i18n.t("delete_for_me"), style=wx.RB_GROUP)
        rb_me.SetValue(True)
        sizer.Add(rb_me, 0, wx.ALL, 8)

        rb_all = None
        if can_delete_for_all:
            rb_all = wx.RadioButton(panel, label=i18n.t("delete_for_everyone"))
            sizer.Add(rb_all, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn     = wx.Button(panel, wx.ID_OK,     label=i18n.t("delete_message"))
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(panel, 1, wx.EXPAND)
        dlg.SetSizer(dlg_sizer)
        dlg.Fit()
        dlg.CentreOnParent()

        result       = dlg.ShowModal()
        for_everyone = rb_all.GetValue() if rb_all else False
        dlg.Destroy()

        if result != wx.ID_OK:
            return

        # Re-read the id: the dialog ran its own nested event loop, which is
        # where the worker's wx.CallAfter(_on_message_sent) gets dispatched — a
        # message that was still pending when the menu opened can have swapped
        # its local UUID for its real WhatsApp id by now, and removing the row
        # by the stale one matches nothing, leaving a just-revoked message
        # visible in the conversation.
        msg_id = msg.get("key", {}).get("id", "")
        msg_key = msg.get("key", {})
        jid = msg_key.get("remoteJid", "") or (
            self.conversation.get("remoteJid", "") if self.conversation else ""
        )

        pending_local_id = str(msg.get("_local_id") or "")
        # An unconfirmed send (see _mark_message_unconfirmed's docstring) has
        # no real WhatsApp id any more than a still-queued/in-flight one
        # does — WinZapp just never learned whether it actually went out —
        # so it belongs in the same "nothing to revoke, local delete only"
        # bucket as a cancelled pending send, not the fromMe/for-everyone
        # path below (which would build a revoke request around the local
        # UUID this row's key.id still holds and could only fail).
        cancelled_pending = bool(
            pending_local_id and (msg.get("_local_pending") or msg.get("_send_unconfirmed"))
        )
        if cancelled_pending:
            # An unconfirmed send shares the "nothing to revoke, local delete
            # only" path, but NOT the wait for an echo: its send already
            # finished and reported. Saying so explicitly matters because
            # cancel() answers False for both "a worker owns it" and "it is not
            # in the queue any more", and only the first justifies holding the
            # record.
            self._cancel_pending_message(
                msg, pending_local_id,
                hold_for_echo=bool(msg.get("_local_pending")),
            )
        elif for_everyone:
            # Do NOT remove the row locally. WhatsApp represents a successful
            # revoke with a protocolMessage tombstone under the same message id;
            # the live revoke path updates this record in place. Removing it
            # here caused a visible disappear/reappear cycle after sync.
            self._delete_message_for_everyone_keep_row(msg, jid)
        else:
            self._delete_message_for_me_only(msg, msg_id, index)

    def _cancel_pending_message(self, msg: dict, pending_local_id: str,
                                hold_for_echo: bool = True):
        """Delete a message that is still pending — the delete-while-sending path.

        ``hold_for_echo=False`` is for a send that is already OVER: an
        unconfirmed one (_send_unconfirmed), where the worker finished and
        reported long ago. cancel() returns False for it — not because a worker
        still owns the message, but because it is no longer in the queue at all
        — so without this flag it would take the hold-for-echo tail below and
        be stashed waiting for an outcome report that has already happened and
        will never come again. The record would sit in the chat forever:
        invisible (_is_displayable_message and _counts_as_last_message both
        refuse _cancelled_awaiting_id), re-persisted on every save, and holding
        a slot in _cancelled_pending_messages. Nor can the echo matcher claim
        it, since that only considers _local_pending records and an unconfirmed
        one has that False.

        There is no WhatsApp message ID to revoke yet, so whichever scope the
        delete dialog had selected, this cancels the queued/in-flight send and
        applies a local deletion; asking the API for "everyone" here can only
        fail.

        When cancel() reports the send was stopped for good that is the whole
        story. When it does not — a worker already owns the message — the row
        still goes, but the *record* deliberately stays behind, marked
        _cancelled_awaiting_id and still _local_pending. That record is what
        on_new_message()'s by-type echo matching binds the echo to: the echo
        carries no correlation ID, so with this message's record gone the
        matcher would hand its WhatsApp ID to the next unrelated pending send of
        the same type, and no amount of registering IDs afterwards fixes that —
        the echo can (and routinely does) arrive before the send call has even
        returned. _counts_as_last_message() ignores the marker, so the chat list
        does not show a message the user just deleted, and the record is dropped
        or resolved for real the moment the queue reports the outcome (see
        discard_cancelled_message()/complete_cancelled_message_delivery()).
        """
        stopped = self.main_window.message_queue.cancel(pending_local_id)
        tracked = self._outgoing_virtual_messages.pop(pending_local_id, None)
        self._media_upload_progress.pop(pending_local_id, None)
        self._upload_stages_seen.pop(pending_local_id, None)
        self._media_transfer_started.discard(pending_local_id)
        self._hide_media_transfer_gauge()
        record = tracked or msg
        chat = self.main_window.get_chat(record.get("key", {}).get("remoteJid", ""))
        position = self._record_position(chat, pending_local_id)
        # cancel() only stops the queue from ever sending it — the pending
        # bubble itself (key.id == pending_local_id for a virtual message)
        # stays in the list until removed here, same as the other two
        # branches in _on_menu_delete_message() do for their own message.
        self.remove_messages_by_id({pending_local_id}, focus_previous=True)
        if stopped:
            # Nothing was sent and nothing will be: the pre-cached copies
            # (voice_messages/<local_id>.msv, media/<local_id>.wzmedia) belong to
            # a message that no longer exists anywhere, and no later rename can
            # ever claim them.
            discard_local_media_cache(
                data_path("voice_messages"), data_path("media"), pending_local_id
            )
            return
        if not hold_for_echo:
            # The send already ran to completion and its outcome was already
            # reported; there is nothing left to wait for. Same disposal as the
            # stopped-for-good branch above.
            discard_local_media_cache(
                data_path("voice_messages"), data_path("media"), pending_local_id
            )
            return
        logging.info(
            "[conversations] %s was already being sent when it was cancelled — "
            "holding its record until the queue reports the outcome",
            pending_local_id,
        )
        record["_cancelled_awaiting_id"] = True
        self._remember_cancelled_pending(pending_local_id, record)
        if chat is None or position < 0:
            return
        # Back into the chat's records, at the position it had: on_new_message()
        # matches an echo to the FIRST pending record of its type, and records
        # are in send order, so appending this one at the end would hand its echo
        # to a message sent after it — the very swap this record exists to
        # prevent. Deliberately NOT back into the DB: remove_messages_by_id()
        # just deleted the stored copy, and leaving it deleted is the safer of
        # the two states to be caught in if the app dies inside this window — a
        # message that is gone rather than one stuck "sending" forever.
        #
        # The window is not fully closable in memory-only terms, and that is
        # accepted: if the echo claims this record before the outcome is known,
        # on_new_message() persists it as an ordinary sent message — marker and
        # all — so an app killed between that echo and the end of the revoke
        # leaves one stored record that both _counts_as_last_message() and
        # _is_displayable_message() refuse, i.e. invisible, with the chat
        # preview falling back to an older message. Both outcomes below fix the
        # stored copy (deleted on a successful revoke, rewritten clean on a
        # failed one); only being killed inside those few seconds does not.
        records = (
            chat.setdefault("messages", {})
                .setdefault("messages", {})
                .setdefault("records", [])
        )
        if not any(r.get("_local_id") == pending_local_id for r in records):
            records.insert(min(position, len(records)), record)

    @staticmethod
    def _record_position(chat: dict, local_id: str) -> int:
        """Index of a pending message inside its chat's records, or -1."""
        if not chat:
            return -1
        records = chat.get("messages", {}).get("messages", {}).get("records", [])
        for i, r in enumerate(records):
            if r.get("_local_id") == local_id:
                return i
        return -1

    def _delete_target_jid(self, msg_key: dict) -> str:
        """The chat a delete has to be addressed at.

        Normally that is the message's own key.remoteJid, and the open
        conversation is only a fallback for a record that somehow has none.
        The "Me" chat is the exception, for the same reason
        _receipts_are_meaningless() reads the chat rather than the key: it
        holds records whose key still carries the raw self-chat artifact JID
        ("<my digits>@g.us"), because _redirect_self_chat_artifact() files
        such a message under my_jid and deduplicate_chats()'s Pass 0a merges
        an already-stored phantom chat's records into it, and neither
        rewrites the key. Handing that JID to delete_message_for_me() builds
        phone="<my digits>@g.us", isGroup=True — a chat that does not exist
        server-side — so the delete silently does nothing and the message
        comes back on the next resync, which is issue #73's original symptom
        in the one chat this whole path exists for.

        Deliberately narrow: only the self-chat overrides the key, so a
        group or 1:1 delete resolves exactly as it always did.
        """
        conv_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if conv_jid and self.main_window._is_self_jid(conv_jid):
            return conv_jid
        return msg_key.get("remoteJid", "") or conv_jid

    def _apply_confirmed_revoke(self, msg: dict, jid: str):
        """Apply the revoke tombstone after our own API request succeeds.

        WhatsApp normally echoes an onRevokedMessage event, but that echo is not
        guaranteed to reach this client. The HTTP 200 is already authoritative
        for the user-initiated revoke, so synthesize the same protocolMessage
        MainWindow._apply_remote_revoke() handles for a live remote event.
        A later real echo is harmless because that method is idempotent.
        """
        msg_id = (msg.get("key") or {}).get("id", "")
        if not msg_id:
            return
        incoming = {
            "key": dict(msg.get("key") or {}),
            "messageType": "protocolMessage",
            "message": {"protocolMessage": {"type": 3, "key": msg_id}},
        }
        self.main_window._apply_remote_revoke(msg, incoming, jid)

    def _delete_message_for_everyone_keep_row(self, msg: dict, jid: str):
        """Revoke remotely and turn the existing row into "message deleted".

        Keep the row itself: a delete-for-everyone is represented by a
        protocolMessage tombstone, not by removing the message from history.
        Prefer WhatsApp's live revoke event, but when our own delete request is
        confirmed first, apply the same tombstone locally immediately instead
        of leaving stale content visible while waiting for an echo that may
        never arrive.
        """
        i18n = self.main_window.i18n

        def _revoke(record=msg, j=jid):
            k = dict(record.get("key") or {})
            ok = self.main_window.delete_message_for_everyone(j, k)
            if ok:
                wx.CallAfter(self._apply_confirmed_revoke, record, j)
            else:
                wx.CallAfter(
                    wx.MessageBox,
                    i18n.t("delete_for_everyone_failed"),
                    i18n.t("delete_message"),
                    wx.OK | wx.ICON_WARNING,
                )

        threading.Thread(target=_revoke, daemon=True).start()

    def _delete_message_for_me_only(self, msg: dict, msg_id: str, index: int):
        """Delete a message for this account only (delete_message_for_me),
        then remove it locally — the plain "delete for me" path, shared by
        the dialog's own choice and the self-chat shortcut in
        _on_menu_delete_message() that skips the dialog entirely (issue #73:
        "delete for everyone" is a no-op there, since the "Me" chat has no
        one else to delete it for)."""
        msg_key = msg.get("key", {})
        jid = self._delete_target_jid(msg_key)

        def _delete_for_me(k=dict(msg_key), j=jid):
            self.main_window.delete_message_for_me(j, k)
        threading.Thread(target=_delete_for_me, daemon=True).start()

        if msg_id:
            self.remove_messages_by_id({msg_id}, focus_previous=True)
        else:
            self._sorted_messages.pop(index)
            self.messages_list.DeleteItem(index)

    def remove_messages_by_id(self, msg_ids: set, focus_previous: bool = False):
        """Remove every row whose key.id is in msg_ids from messages_list,
        _sorted_messages, _all_sorted_messages and self.conversation's
        records (plus the DB copy) — keeping the unread-separator index and
        pagination offset in sync with whatever just disappeared.

        Shared by _on_menu_delete_message() (single message, user-initiated)
        and MainWindow._mirror_remote_deletions() (a batch mirrored in from
        a phone-side deletion detected by the periodic poll).

        focus_previous=True re-focuses whatever row the user was actually on,
        adjusted for the rows that just disappeared, once done — WITHOUT
        calling messages_list.SetFocus(), so a background-triggered removal
        never steals keyboard focus from wherever the user actually is right
        now (e.g. the message field). Only when the row that was focused is
        itself one of the removed ones does this fall back to landing just
        before the earliest removed row (or row 0 if the removal started at
        the top) — the correct behaviour for the user-initiated single-delete
        path, where the deleted message IS what was focused. Without this
        distinction, _mirror_remote_deletions()'s 60s periodic poll yanked the
        user's focus to wherever the earliest of THAT PASS's removed messages
        happened to sit — often nowhere near what the user was actually
        reading — every time it mirrored so much as one stale message.
        """
        if not msg_ids:
            return
        if hasattr(self, "close_image_description"):
            self.close_image_description(message_ids=msg_ids)
        # Stop playback before touching the list — a currently-playing audio
        # message may not even be in _sorted_messages any more (pagination
        # can scroll it out while it keeps playing in the background), so
        # this must not be gated on the row actually being found below.
        self._stop_playback_for_removed_messages(msg_ids)
        # Drop the removed ids from the selection too. The selection mode
        # (issue #99) is *derived* from this set being non-empty, so an id left
        # behind by a message that no longer exists keeps plain Space silently
        # in selecting mode on a conversation where nothing is selected and
        # nothing ever announced the mode turning on — reachable without any
        # user action at all, via _mirror_remote_deletions()'s 60s poll.
        self.selected_messages.difference_update(msg_ids)
        indices = sorted(
            i for i, m in enumerate(self._sorted_messages)
            if isinstance(m, dict) and m.get("key", {}).get("id") in msg_ids
        )
        # An id with no row on screen still has to leave `records` and the DB.
        # This used to `return` here, and that turned _mirror_remote_deletions()
        # into a permanent no-op loop: the ids it mirrors are the ones the phone
        # no longer has, and those are routinely NOT rendered rows — a reaction
        # or other non-displayable record (_is_displayable_message()), or a
        # message paginated out of the current window. Nothing was removed, so
        # the next poll found exactly the same ids missing, and the next, and
        # the next. Measured on a real session: the same 21 ids re-reported
        # every 60s, sixty times in one log, each round paying a
        # get-messages?count=200 round trip and printing a line claiming a
        # removal that never happened.
        #
        # Only the row-level work below is conditional now. Everything from
        # `if self.conversation:` on is unconditional, because it is what makes
        # the removal stick.
        earliest = indices[0] if indices else -1
        _preserved_msg_id = self._focused_msg_id() if focus_previous else ""
        _preserved_idx = self.messages_list.GetFocusedItem() if focus_previous else -1
        _preserved_was_separator = (
            focus_previous and self._unread_sep_idx >= 0
            and _preserved_idx == self._unread_sep_idx
        )
        # Keep the unread-separator index and the full (unpaginated) message
        # list in sync with the rows that just disappeared. Without this,
        # every later consumer of _unread_sep_idx (focus handling, the
        # dismiss timer, on_incoming_message's separator relocation) kept
        # operating on pre-delete rows — off by one for every message
        # deleted above the separator — and _load_more_messages()/
        # _load_older_messages() could re-introduce a just-deleted message
        # from the still-stale _all_sorted_messages on the next scroll-to-top.
        for idx in reversed(indices):
            self._sorted_messages.pop(idx)
            self.messages_list.DeleteItem(idx)
            if self._unread_sep_idx >= 0 and idx < self._unread_sep_idx:
                self._unread_sep_idx -= 1
        for i in range(len(self._all_sorted_messages) - 1, -1, -1):
            m = self._all_sorted_messages[i]
            if isinstance(m, dict) and m.get("key", {}).get("id") in msg_ids:
                self._all_sorted_messages.pop(i)
                if i < self._messages_offset:
                    self._messages_offset -= 1
        if self.conversation:
            records = (
                self.conversation.get("messages", {})
                .get("messages", {})
                .get("records", [])
            )
            self.conversation["messages"]["messages"]["records"] = [
                m for m in records
                if m.get("key", {}).get("id") not in msg_ids
            ]
            for mid in msg_ids:
                try:
                    self.main_window.db.delete_message(
                        self.conversation.get("remoteJid", ""), mid
                    )
                except Exception:
                    logging.exception("[conversations] delete_message failed for %s", mid)

            # The chat list's preview text and sort position both fall back to
            # chat["lastMessage"]/["t"] — without recomputing them here, a
            # deleted message kept showing as the preview and kept the chat
            # pinned at its old (now stale) position until the next full sync.
            jid = self.conversation.get("remoteJid", "")
            if jid:
                self.main_window._recompute_chat_last_message(jid)
                self.main_window._schedule_set_chats()

        if focus_previous and indices:
            # `and indices`: with no row removed there is nothing to adjust
            # focus for, and calling Focus()/Select() on the row the user is
            # already sitting on fires EVT_LIST_ITEM_FOCUSED for a move that
            # did not happen — the screen reader re-announces the row and the
            # selection sound fires again. Reached whenever the ids being
            # removed are all off-screen, which is the ordinary case for
            # _mirror_remote_deletions().
            count = self.messages_list.GetItemCount()
            if count > 0:
                new_focus = -1
                if _preserved_msg_id and _preserved_msg_id not in msg_ids:
                    for idx, m in enumerate(self._sorted_messages):
                        if isinstance(m, dict) and m.get("key", {}).get("id") == _preserved_msg_id:
                            new_focus = idx
                            break
                elif _preserved_was_separator and self._unread_sep_idx >= 0:
                    new_focus = self._unread_sep_idx
                if new_focus < 0:
                    # The row that was focused is itself among the removed
                    # ones (or nothing usable was focused) — land just before
                    # the earliest removed row, same as before this fix.
                    new_focus = min(max(earliest - 1, 0), count - 1)
                self.messages_list.Focus(new_focus)
                self.messages_list.Select(new_focus, True)
                self.messages_list.EnsureVisible(new_focus)

    def _on_accel_edit_message(self, event):
        """Alt+E: enter edit mode for the focused own text message."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        if not msg.get("key", {}).get("fromMe", False):
            return
        if edit_kind(msg) is None:
            return
        if not edit_window_open(msg.get("messageTimestamp")):
            # Said out loud: a shortcut that silently does nothing reads as a
            # broken shortcut to a screen-reader user, and this is by far the
            # most common reason an own text message cannot be edited.
            self.main_window.output(
                self.main_window.i18n.t("edit_window_expired").format(
                    minutes=EDIT_UI_WINDOW_SECONDS // 60),
                interrupt=True,
            )
            return
        self._on_menu_edit_message(index, msg)

    def _on_menu_edit_message(self, index: int, msg: dict):
        """Enter edit mode: pre-fill message field with message text."""
        if edit_kind(msg) == "caption":
            # The raw caption, not _get_message_content(): for media that is
            # the row as the list reads it (type, file name, size), none of
            # which is part of what gets edited.
            body = msg.get("message") or {}
            content = next(
                ((body.get(k) or {}).get("caption") or ""
                 for k in ("imageMessage", "videoMessage", "documentMessage")
                 if isinstance(body.get(k), dict)),
                "",
            )
        else:
            content = self._get_message_content(msg) or ""
            # Strip any leading quote block (from a previous reply prefix)
            if content.startswith("> ") and "\n" in content:
                content = content[content.index("\n") + 1:]

        self._editing_message_id    = msg.get("key", {}).get("id", "")
        self._editing_message_index = index
        # Captured now, from the message the user chose: by the time the edit
        # is saved a sync may have paginated the row out, and the caption rule
        # (no mentions — see _apply_message_edit) must hold regardless.
        self._editing_is_caption    = edit_kind(msg) == "caption"

        # Seed the pending-mention state from the message being edited. The
        # pre-filled text shows mentions as "@DisplayName" (that is what
        # _get_message_content renders), and _build_mention_payload maps those
        # back to "@phone" on save — but only for JIDs it knows about. Without
        # this, changing a single word in a message that mentioned someone
        # stripped every mention from it. Uses the raw list rather than
        # _extract_mentions() so an @todos message keeps all its participants.
        self._pending_mentions.clear()
        self._pending_mention_display_names.clear()
        for jid in self._raw_mentioned_jids(msg):
            if not jid or jid in self._pending_mentions:
                continue
            self._pending_mentions.append(jid)
            self._pending_mention_display_names[jid] = self._get_participant_name(jid)
        self._rebuild_mention_pills()

        # Stored text uses bare newlines; the field wants CRLF so the screen
        # reader can arrow through a multi-line message being edited.
        self.message_field.SetValue(to_editor_line_endings(content))
        self.message_field.SetInsertionPointEnd()
        self.message_field.SetFocus()

        # Show cancel button so the user knows they're in edit mode
        self._cancel_edit_btn.Show()
        self.conversation_panel.Layout()

    def _on_menu_resend_message(self, msg: dict):
        """Manually re-send a text message WinZapp itself never confirmed —
        the other recovery option besides dismissing it outright (see
        _on_menu_delete_message's cancelled_pending branch, which already
        treats this state as nothing-to-revoke).

        Deliberately does not attempt to preserve a quote or @mentions the
        original had: rebuilding those faithfully from contextInfo is more
        machinery than a rare manual recovery action warrants, and this
        must not read the composer's own current _quoted_message/
        _pending_mentions state either — those describe whatever the user
        is composing right now, unrelated to the row being resent. A resend
        goes out as plain text; if the quote mattered, the user can reply
        again themselves.
        """
        remote_jid = msg.get("key", {}).get("remoteJid", "") or (
            self.conversation.get("remoteJid", "") if self.conversation else ""
        )
        if not remote_jid:
            return

        # Read the WIRE text, never _get_message_content() — that one returns
        # what the LIST shows, which is not what was sent:
        #   * link_preview_text() PREPENDS the preview WhatsApp resolved for
        #     the URL, as "<title>. <description>. <text>". Resending that
        #     would deliver WhatsApp's own preview card to the recipient as
        #     literal characters in the message body.
        #   * _resolve_mentions_in_text() turns the stored "@554899..." back
        #     into "@João" for display. Resending that sends a literal
        #     "@João" — no mention, and a name string WhatsApp never saw.
        # The raw body has neither, so the "> " strip the edit path needs is
        # not needed here either (nothing in the send path ever writes that
        # prefix into message.conversation) — and doing it would silently
        # truncate a message from a user who legitimately types quote-style
        # lines.
        body = msg.get("message") or {}
        content = (
            body.get("conversation")
            or (body.get("extendedTextMessage") or {}).get("text")
            or ""
        )
        if not content:
            return

        old_local_id = str(msg.get("_local_id") or "")
        if old_local_id:
            # Harmless no-op on message_queue's side — an unconfirmed send
            # has already left its queue by definition — but still clears
            # this row's own tracking entries the same way a dismiss would.
            self.main_window.message_queue.cancel(old_local_id)
            self._outgoing_virtual_messages.pop(old_local_id, None)
            self.remove_messages_by_id({old_local_id}, focus_previous=True)

        local_id = str(uuid.uuid4())
        virtual_msg = {
            "_local_pending": True,
            "_local_id":      local_id,
            "key": {
                "id":       local_id,
                "fromMe":   True,
                "remoteJid": remote_jid,
            },
            "messageType":      "conversation",
            "message":          {"conversation": content},
            "messageTimestamp": int(time.time()),
            "pushName":         "",
        }
        self._clear_empty_placeholder()
        self._sorted_messages.append(virtual_msg)
        self.messages_list.Append((self._render_message_line(virtual_msg),))
        last = self.messages_list.GetItemCount() - 1
        if last >= 0:
            self.messages_list.EnsureVisible(last)

        self.main_window.message_queue.enqueue(
            PendingMessage(local_id, remote_jid, text=content)
        )

        self._register_virtual_msg(virtual_msg)
        self.main_window._schedule_set_chats()

    def _on_cancel_edit(self, event=None):
        """Leave edit mode without saving."""
        self._editing_message_id    = None
        self._editing_message_index = -1
        self._editing_is_caption    = False
        # Edit mode seeds these from the message being edited (see
        # _on_menu_edit_message) — drop them again, or the next ordinary message
        # typed into the field would inherit the edited message's mentions.
        self._pending_mentions.clear()
        self._pending_mention_display_names.clear()
        self._hide_mention_suggestions()
        self._rebuild_mention_pills()
        self.message_field.SetValue("")
        self._cancel_edit_btn.Hide()
        self.conversation_panel.Layout()
        self.message_field.SetFocus()

    def _on_cancel_reply(self, event=None):
        """Leave reply mode without sending."""
        self._quoted_message = None
        i18n     = self.main_window.i18n
        jid      = self.conversation.get("remoteJid", "") if self.conversation else ""
        is_group = jid.endswith("@g.us")
        label = (
            i18n.t("type_message_group") if is_group else i18n.t("type_message")
        )
        if self.conversation_name:
            label = f"{label} {self.conversation_name}"
        self.message_label.SetLabel(label)
        self._remove_quote_btn.Hide()
        self.conversation_panel.Layout()
        self.message_field.SetFocus()
