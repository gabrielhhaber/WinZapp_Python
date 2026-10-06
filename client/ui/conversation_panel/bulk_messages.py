"""BulkMessagesMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import os
import pyperclip
import threading
import wx
from ui.conversation_panel.media_paths import _SAVEABLE_MESSAGE_TYPES
from core.message_copy_format import format_copied_message
from core.call_log import is_call_log
from core.save_location import resolve_save_dialog_folder


class BulkMessagesMixin:
    """Bulk actions on selected messages.
    """

    def _on_mass_copy_messages(self, event):
        """Copy every selected plain-text message to the clipboard as one
        WhatsApp-export-style block of text, one line per message formatted
        "<date> <time> - <sender>: <text>" — the date/time pattern follows
        the active app language's datetime_fmt through
        core.message_copy_format, independently of the Windows regional
        format used by timestamps displayed elsewhere in the interface.
        Other message types (media, location, contact cards, ...) are
        silently skipped, same as how _on_menu_copy_message only ever
        handles "conversation"/"extendedTextMessage". Order follows
        _sorted_messages, not set iteration order, same as the other mass
        message actions."""
        if not self.selected_messages: return
        i18n = self.main_window.i18n
        _TEXT_TYPES = ("conversation", "extendedTextMessage")
        lines = []
        for m in self._sorted_messages:
            if self._is_separator(m) or m.get("key", {}).get("id") not in self.selected_messages:
                continue
            msg_type = m.get("messageType", "")
            if msg_type not in _TEXT_TYPES:
                continue
            text = self._message_text_with_names(m)
            if not text:
                continue
            sender = self._sender_label(m)
            ts = self._extract_timestamp(m)
            lines.append(format_copied_message(
                ts, sender, text, i18n.t("datetime_fmt")))

        if not lines:
            self.main_window.output(i18n.t("copy_selected_nothing_to_copy"), interrupt=True)
            return

        try:
            pyperclip.copy("\n".join(lines))
        except Exception:
            self.main_window.output(i18n.t("msg_copy_error"), interrupt=True)
            return

        copied_ids = list(self.selected_messages)
        self.selected_messages.clear()
        self._refresh_message_rows_by_ids(copied_ids)
        self.main_window.output(i18n.t("messages_copied_bulk"), interrupt=True)

    def _mass_message_targets(self, flag: str) -> "tuple[list, list]":
        """(messages to act on, all selected ids) for a mass message action.

        Targets are real messages in the current selection that don't already
        carry *flag* — system events are filtered here rather than left to
        each single-message handler's own _reject_system_event_action guard,
        so a mixed selection doesn't announce "unavailable" once per system
        event. Order follows _sorted_messages, not set iteration order, same
        as every other mass message action.
        """
        targets = [
            m for m in self._sorted_messages
            if not self._is_separator(m)
            and not self._is_system_event(m)
            and m.get("key", {}).get("id") in self.selected_messages
            and not m.get(flag)
        ]
        return targets, list(self.selected_messages)

    def _on_mass_star_messages(self, event):
        """Star selected unstarred messages in one sequential, verified job."""
        if not self.selected_messages: return
        i18n = self.main_window.i18n
        if getattr(self.main_window, "_star_sync_job", None) is not None:
            self.main_window.output(i18n.t("star_sync_running"), interrupt=True)
            return
        to_star, ids = self._mass_message_targets("starred")
        self.selected_messages.clear()
        if not to_star:
            # Everything selected was already starred (or was a system event).
            # Announcing success here told screen-reader users the action had
            # been applied when nothing happened at all.
            self._refresh_message_rows_by_ids(ids)
            self.main_window.output(i18n.t("mass_nothing_to_do"), interrupt=True)
            return

        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        # The whole selection, not just to_star: clearing selected_messages
        # above dropped the " selecionado" marker from every row in it.
        self._repaint_or_repopulate(ids)
        self._sync_message_stars(jid, to_star, True)

    def _on_mass_pin_messages(self, event):
        """Pin every not-yet-pinned selected message via WhatsApp's own
        message-pin feature (visible to everyone in the chat, unlike star).

        Applies the optimistic update to the whole batch and repaints once.
        The server calls additionally run on
        ONE background thread, sequentially, and their failures are collected
        into a single rollback + a single dialog reporting the count —
        _on_menu_pin_message() starts a thread per message and pops its own
        blocking wx.MessageBox per rejection, so pinning a selection the
        server refuses used to fire N concurrent requests at the local
        WPPConnect server and then stack N modal dialogs, one per message.
        (Same failure mode c518cce fixed for posting several files as status.)
        """
        if not self.selected_messages: return
        i18n = self.main_window.i18n
        to_pin, ids = self._mass_message_targets("pinInChat")
        self.selected_messages.clear()
        if not to_pin:
            self._refresh_message_rows_by_ids(ids)
            self.main_window.output(i18n.t("mass_nothing_to_do"), interrupt=True)
            return

        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if not jid:
            self._refresh_message_rows_by_ids(ids)
            return

        for m in to_pin:
            m["pinInChat"] = True
        self._persist_message_local_flags(jid, to_pin)
        self.main_window._schedule_save()
        self._repaint_or_repopulate(ids)   # see _on_mass_star_messages on `ids`
        self.main_window.output(i18n.t("success_pin_bulk"), interrupt=True)

        # Keys are copied now: the message dicts can be replaced underneath us
        # by a resync while the requests are still in flight.
        pending = [(m, dict(m.get("key", {}))) for m in to_pin]
        total   = len(pending)

        def _do(j=jid, items=pending, n=total):
            failed = []
            for m, k in items:
                try:
                    ok = self.main_window.pin_message(j, k, True)
                except Exception as exc:
                    logging.warning("[_on_mass_pin_messages] pin_message raised for %s: %s",
                                    k.get("id", ""), exc)
                    ok = False
                if not ok:
                    failed.append(m)
            if failed:
                wx.CallAfter(self._on_mass_pin_failed, failed, j, n)

        threading.Thread(target=_do, daemon=True).start()

    def _on_mass_pin_failed(self, failed: list, jid: str, total: int):
        """Roll back the optimistic pins the server rejected, all at once
        (main thread) — one repaint and one dialog carrying the count, rather
        than _on_pin_message_failed()'s per-message repaint + modal."""
        for m in failed:
            m["pinInChat"] = False
        self._persist_message_local_flags(jid, failed)
        self.main_window._schedule_save()
        self._repaint_or_repopulate([m.get("key", {}).get("id", "") for m in failed])
        i18n = self.main_window.i18n
        wx.MessageBox(
            f"{i18n.t('pin_message_failed')} ({len(failed)}/{total})",
            i18n.t("pin_message"),
            wx.OK | wx.ICON_WARNING,
        )

    def _on_mass_forward_messages(self, event):
        if not self.selected_messages: return
        msgs_to_forward = []
        for m in self._sorted_messages:
            if not self._is_separator(m) and m.get("key", {}).get("id") in self.selected_messages:
                msgs_to_forward.append(m)
        if msgs_to_forward:
            self._on_menu_forward(msgs_to_forward[0], msgs_list=msgs_to_forward)
        forwarded_ids = list(self.selected_messages)
        self.selected_messages.clear()
        self._refresh_message_rows_by_ids(forwarded_ids)
        self.main_window.output(self.main_window.i18n.t("unselected"), interrupt=True)

    def _on_mass_save_messages(self, event):
        if not self.selected_messages: return
        i18n = self.main_window.i18n
        msgs = []
        for msg_id in list(self.selected_messages):
            msg = next((m for m in self._sorted_messages if not self._is_separator(m) and m.get("key", {}).get("id") == msg_id), None)
            if msg and not self._is_separator(msg) and msg.get("messageType", "") in _SAVEABLE_MESSAGE_TYPES:
                msgs.append(msg)

        if not msgs:
            self.main_window.output(i18n.t("save_as_nothing_to_save_bulk"), interrupt=True)
            return

        with wx.DirDialog(
            self,
            i18n.t("select_folder_dialog_title"),
            defaultPath=resolve_save_dialog_folder(self.main_window.settings),
            style=wx.DD_DEFAULT_STYLE,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            target_dir = dlg.GetPath()
        # A folder, not a file — join a name so dirname() lands on the folder
        # the user actually picked rather than on its parent.
        self.main_window.remember_save_folder(os.path.join(target_dir, "x"))

        # Resolve filenames up front and dedupe within this batch so two
        # messages that would otherwise collide (e.g. same original name)
        # don't clobber each other on disk.
        used_names = set()
        for msg in msgs:
            default_file = self._resolve_media_filename(msg)
            base, ext = os.path.splitext(default_file)
            candidate = default_file
            n = 1
            while candidate.lower() in used_names or os.path.isfile(os.path.join(target_dir, candidate)):
                candidate = f"{base}_{n}{ext}"
                n += 1
            used_names.add(candidate.lower())
            save_path = os.path.join(target_dir, candidate)
            threading.Thread(target=self._save_message_media, args=(msg, save_path), daemon=True).start()

        saved_ids = list(self.selected_messages)
        self.selected_messages.clear()
        self._refresh_message_rows_by_ids(saved_ids)

    def _group_admin_delete_override(self) -> bool:
        """True when the user is an admin of the currently open group — same
        check _on_menu_delete_message() does for a single message, pulled
        out so the bulk delete dialog can compute it once instead of once
        per selected message (it doesn't depend on which message: an admin
        can revoke ANY message in their own group, not just their own)."""
        if not self.conversation:
            return False
        conv_jid = self.conversation.get("remoteJid", "")
        if not conv_jid.endswith("@g.us"):
            return False
        group_meta = self.conversation.get("groupMetadata", {})
        participants = group_meta.get("participants") or self.conversation.get("participants") or []

        def _phone_part(j: str) -> str:
            return j.rsplit("@", 1)[0].split(":")[0] if isinstance(j, str) else ""

        mw = self.main_window
        my_phone = _phone_part(getattr(mw, "my_jid", ""))
        my_lid   = _phone_part(getattr(mw, "my_lid", ""))
        for p in participants:
            if not isinstance(p, dict):
                continue
            p_id = p.get("id", "")
            if isinstance(p_id, dict):
                p_id = p_id.get("_serialized", "")
            p_digits = _phone_part(p_id)
            if not p_digits:
                continue
            is_me = (my_phone and mw._phone_digits_equivalent(p_digits, my_phone)) or (my_lid and p_digits == my_lid)
            if is_me:
                return bool(p.get("admin") or p.get("isAdmin"))
        return False

    def _on_mass_delete_messages(self, event):
        """Same delete-scope dialog _on_menu_delete_message() shows for a
        single message — radio buttons for "delete for me"/"delete for
        everyone" plus Apagar/Cancelar — applied to every selected message
        instead of the old plain Yes/No "apagar N mensagens?" confirmation.
        In the "Me" chat that scope dialog is replaced by a plain Delete/
        Cancel confirmation, since "for everyone" is a no-op there."""
        i18n = self.main_window.i18n
        if not self.selected_messages: return

        msgs_to_delete = []
        skipped_calls = 0
        for msg_id in self.selected_messages:
            msg = next((m for m in self._sorted_messages if not self._is_separator(m) and m.get("key", {}).get("id") == msg_id), None)
            # A selected call record takes no part in a mass action (it can
            # still be deleted on its own from its context menu); the delete
            # dialog below counts only what it will actually remove.
            if msg and is_call_log(msg):
                skipped_calls += 1
            elif msg:
                msgs_to_delete.append(msg)
        if not msgs_to_delete:
            self.selected_messages.clear()
            if skipped_calls:
                # Never a silent no-op for a screen-reader user.
                self.main_window.output(i18n.t("call_log_action_unavailable"), interrupt=True)
            return

        conv_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        is_self_chat = bool(conv_jid) and self.main_window._is_self_jid(conv_jid)

        admin_override = self._group_admin_delete_override()

        def _can_delete_for_all(msg):
            if self._is_system_event(msg):
                return False
            return admin_override or msg.get("key", {}).get("fromMe", False)

        # The "Me" chat has only one participant, so "delete for everyone" is
        # a no-op there for every message in the selection — same reasoning
        # _on_menu_delete_message() applies to a single message (issue #73).
        # Skip the for-me/for-everyone dialog entirely and go straight to a
        # plain Delete/Cancel confirmation (issue #95). Only the scope choice
        # is skipped: the delete itself goes through the same worker and the
        # same local-removal tail as every other bulk delete.
        if is_self_chat:
            if not self._confirm_local_only_delete(len(msgs_to_delete)):
                return
            for_everyone = False
        else:
            any_eligible = any(_can_delete_for_all(m) for m in msgs_to_delete)

            dlg = wx.Dialog(
                self,
                title=i18n.t("delete_messages_bulk_title"),
                style=wx.DEFAULT_DIALOG_STYLE,
            )
            panel = wx.Panel(dlg)
            sizer = wx.BoxSizer(wx.VERTICAL)

            rb_me = wx.RadioButton(panel, label=i18n.t("delete_for_me"), style=wx.RB_GROUP)
            rb_me.SetValue(True)
            sizer.Add(rb_me, 0, wx.ALL, 8)

            rb_all = None
            if any_eligible:
                rb_all = wx.RadioButton(panel, label=i18n.t("delete_for_everyone"))
                sizer.Add(rb_all, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

            btn_sizer  = wx.StdDialogButtonSizer()
            ok_btn     = wx.Button(panel, wx.ID_OK,     label=i18n.t("delete_messages_bulk_title"))
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

        # Only messages whose effective scope is "for me" disappear from
        # WinZapp. A successful revoke-for-everyone must keep its row so the
        # live protocolMessage can repaint it as "message deleted" in place.
        local_delete_ids = {
            msg.get("key", {}).get("id", "")
            for msg in msgs_to_delete
            if not (for_everyone and _can_delete_for_all(msg))
        }
        local_delete_ids.discard("")

        def _delete_bg():
            failed = 0
            for msg in msgs_to_delete:
                msg_key = dict(msg.get("key", {}))
                jid = self._delete_target_jid(msg_key)
                if not jid:
                    continue
                # Per message, never once for the batch: a mixed selection
                # can contain items that cannot be revoked for everyone. Those
                # still use the local-only API and are the only rows removed.
                if for_everyone and _can_delete_for_all(msg):
                    ok = self.main_window.delete_message_for_everyone(jid, msg_key)
                    if ok:
                        wx.CallAfter(self._apply_confirmed_revoke, msg, jid)
                    else:
                        failed += 1
                else:
                    self.main_window.delete_message_for_me(jid, msg_key)
            wx.CallAfter(self._on_bulk_delete_for_everyone_done, failed)

        threading.Thread(target=_delete_bg, daemon=True).start()

        if local_delete_ids:
            self.remove_messages_by_id(local_delete_ids, focus_previous=True)
        # The whole selection, not just the rows that go away: the ", selected"
        # suffix lives in the ROW TEXT (append_selected_marker()), and clearing
        # selected_messages below does not rewrite it. Only rows whose revoke
        # succeeded get repainted, by the protocolMessage coming back through
        # _apply_confirmed_revoke(); a row whose revoke FAILED stayed on screen
        # reading ", selected" forever while selected_messages was empty -- so
        # a screen reader announced it as selected and every mass-action
        # shortcut answered "nothing selected". Same reasoning, and the same
        # fix, as the note in _on_mass_star_messages().
        still_marked = list(self.selected_messages)
        self.selected_messages.clear()
        if still_marked:
            self._refresh_message_rows_by_ids(still_marked)

    def _on_bulk_delete_for_everyone_done(self, failed_count: int):
        """Report the batch's real outcome instead of an unconditional
        "success" (issue: a screen-reader user was told a delete succeeded
        while one or more messages silently stayed on everyone else's copy).
        Rows removed locally ("delete for me") already reflect their own
        outcome; this only covers "delete for everyone" revokes.
        """
        i18n = self.main_window.i18n
        if failed_count:
            wx.MessageBox(
                i18n.t("delete_for_everyone_bulk_failed").format(count=failed_count),
                i18n.t("delete_message"),
                wx.OK | wx.ICON_WARNING,
            )
        else:
            self.main_window.output(i18n.t("success_delete"), interrupt=True)
