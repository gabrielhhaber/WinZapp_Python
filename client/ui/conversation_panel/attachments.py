"""AttachmentsMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import mimetypes
import os
import threading
import time
import uuid
import wx
from core.utils import (
    MEASURED_SECONDS_KEY,
    encrypt,
    format_number,
    normalize_line_separators,
)
from core.message_queue import PendingMessage
from core.message_resend import RESEND_PATH_KEY
from core.coalesced_callback import CoalescedCallback
from ui.conversation_panel.typing_row import append_message_row, message_row_count
from core.attachment_types import classify_attachment_media_type
from core.audio_transcode import exceeds_aac_channel_limit
from app_paths import data_path


class AttachmentsMixin:
    """Attaching files and contacts and sending attachments.
    """

    # ── Attachment handling ──────────────────────────────────────────────────

    def on_add_attachment(self, event=None):
        """Open a popup menu to choose the attachment type."""
        if self.conversation is None:
            return
        i18n = self.main_window.i18n
        menu = wx.Menu()
        pv_item  = menu.Append(wx.ID_ANY, i18n.t("attachment_photos_videos"))
        doc_item = menu.Append(wx.ID_ANY, i18n.t("attachment_document"))
        aud_item = menu.Append(wx.ID_ANY, i18n.t("attachment_audio_file"))
        con_item = menu.Append(wx.ID_ANY, i18n.t("attachment_contact"))
        poll_item = menu.Append(wx.ID_ANY, i18n.t("attachment_poll"))
        self.Bind(wx.EVT_MENU, self._on_attach_photo_video, pv_item)
        self.Bind(wx.EVT_MENU, self._on_attach_document,    doc_item)
        self.Bind(wx.EVT_MENU, self._on_attach_audio_file,  aud_item)
        self.Bind(wx.EVT_MENU, self._on_attach_contact,     con_item)
        self.Bind(wx.EVT_MENU, self._on_attach_poll,        poll_item)
        self.PopupMenu(menu)
        menu.Destroy()

    def _on_attach_photo_video(self, event):
        i18n = self.main_window.i18n
        wildcard = (
            f"{i18n.t('attachment_photos_videos')} "
            "(*.jpg;*.jpeg;*.png;*.gif;*.webp;*.mp4;*.avi;*.mov;*.mkv)|"
            "*.jpg;*.jpeg;*.png;*.gif;*.webp;*.mp4;*.avi;*.mov;*.mkv"
        )
        with wx.FileDialog(
            self, i18n.t("attachment_photos_videos"),
            wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            for path in dlg.GetPaths():
                mtype = classify_attachment_media_type(path)
                if mtype not in {"image", "video"}:
                    mtype = "document"
                self._staged_attachments.append({"path": path, "media_type": mtype})
        if self._staged_attachments:
            self._show_attachment_panel()

    def _on_attach_document(self, event):
        with wx.FileDialog(
            self, self.main_window.i18n.t("attachment_document"),
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            for path in dlg.GetPaths():
                self._staged_attachments.append(
                    {"path": path, "media_type": "document"}
                )
        if self._staged_attachments:
            self._show_attachment_panel()

    def _on_attach_audio_file(self, event):
        i18n     = self.main_window.i18n
        wildcard = (
            f"{i18n.t('attachment_audio_file')} "
            "(*.mp3;*.ogg;*.wav;*.m4a;*.aac;*.flac)|"
            "*.mp3;*.ogg;*.wav;*.m4a;*.aac;*.flac"
        )
        with wx.FileDialog(
            self, i18n.t("attachment_audio_file"),
            wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            for path in dlg.GetPaths():
                self._staged_attachments.append(
                    {"path": path, "media_type": "audio"}
                )
        if self._staged_attachments:
            self._show_attachment_panel()

    def _on_attach_contact(self, event):
        from ui.dialogs.attach_contact_dialog import AttachContactDialog
        dlg = AttachContactDialog(self.main_window)
        if dlg.ShowModal() != wx.ID_OK or dlg.selected_contact is None:
            dlg.Destroy()
            return
        contact    = dlg.selected_contact
        dlg.Destroy()
        remote_jid = self.conversation.get("remoteJid", "")
        if not remote_jid:
            return
        local_id = str(uuid.uuid4())
        name = (
            contact.get("pushName")
            or format_number(contact.get("remoteJid", ""))
        )
        virtual_msg = {
            "_local_pending": True,
            "_local_id":      local_id,
            "key": {"id": local_id, "fromMe": True, "remoteJid": remote_jid},
            "messageType": "contactMessage",
            "message": {
                "contactMessage": {
                    "displayName": name,
                    "vcard": "",
                }
            },
            "messageTimestamp": int(time.time()),
            "pushName": "",
        }
        if self._quoted_message:
            _qk = self._quoted_message.get("key", {})
            virtual_msg["contextInfo"] = {
                "stanzaId":      _qk.get("id", ""),
                "participant":   _qk.get("participant", ""),
                "quotedMessage": self._quoted_message.get("message") or {},
            }
        
        self._clear_empty_placeholder()
        self._sorted_messages.append(virtual_msg)
        append_message_row(self, self._render_message_line(virtual_msg))
        last = message_row_count(self) - 1   # the row just sent, not the typing row
        if last >= 0:
            self.messages_list.EnsureVisible(last)
        pm = PendingMessage(local_id, remote_jid, contact_info=contact,
                            quoted=self._quoted_message)
        self.main_window.message_queue.enqueue(pm)
        self._on_cancel_reply()  # clear quoted state after send
        self.main_window.mark_conversation_as_read(remote_jid)

        self._register_virtual_msg(virtual_msg)
        self.main_window._schedule_set_chats()

    def _pre_cache_sent_media(self, local_id: str, path: str, media_type: str):
        """Copy a just-sent attachment straight into the local media cache,
        keyed by its local_id the same way a downloaded copy is keyed by
        message id.

        We're the sender, so the exact bytes already sit on disk at *path* —
        there's no reason to require a redundant round-trip download through
        WPPConnect just to unlock the Open/Save As buttons. This mirrors the
        existing "rename the local audio file so we don't have to download
        it" trick _mark_message_sent() already does for recorded voice
        messages, extended to files sent via the attachment picker
        (document/image/video/audio). _mark_message_sent() renames the cache
        entry from local_id to the real WhatsApp id once the echo confirms it.
        """
        try:
            with open(path, "rb") as fh:
                content = fh.read()
            encrypted = encrypt(content, self.main_window.key)
            if media_type == "audio":
                cache_path = data_path("voice_messages", f"{local_id}.msv")
            else:
                cache_path = data_path("media", f"{local_id}.wzmedia")
            with open(cache_path, "wb") as fh:
                fh.write(encrypted)
        except Exception as e:
            logging.error(f"[_pre_cache_sent_media] failed to pre-cache {path}: {e}")

    def _show_attachment_panel(self):
        self._rebuild_attachment_list()
        self.message_label.Hide()
        self.message_field.Hide()
        if hasattr(self, "_emoji_btn"):
            self._emoji_btn.Hide()
        self.send_message_btn.Hide()
        self.record_voice_message_btn.Hide()
        if hasattr(self, "_record_voice_system_btn"):
            self._record_voice_system_btn.Hide()
        self._add_attachment_btn.Hide()
        self._attachment_panel.Show()
        self.conversation_panel.Layout()
        self._apply_typed_text_as_caption()
        self._caption_field.SetFocus()

    def _apply_typed_text_as_caption(self):
        """Move whatever was already typed in message_field into the
        attachment caption field, matching the official WhatsApp client.

        Only fires when the caption is still empty (never clobbers a caption
        the user already typed for a previous batch of staged attachments)
        and the setting is enabled. The text is moved, not copied — left in
        message_field it would still be sitting there, ready to be sent as a
        separate message, once the attachment panel closes.
        """
        preserve = self.main_window.settings.get("user_interface", {}).get(
            "preserve_typed_text_as_attachment_caption", True
        )
        if not preserve or self._caption_field.GetValue():
            return
        typed = normalize_line_separators(self.message_field.GetValue()).strip()
        if not typed:
            return
        self._caption_field.SetValue(typed)
        self.message_field.SetValue("")

    def _rebuild_attachment_list(self):
        """Rebuild the per-file remove-buttons to match _staged_attachments."""
        i18n  = self.main_window.i18n
        panel = self._attachments_list_panel
        sizer = self._attachments_list_sizer
        for child in list(panel.GetChildren()):
            child.Destroy()
        sizer.Clear()
        for idx, att in enumerate(self._staged_attachments):
            filename = os.path.basename(att["path"])
            btn = wx.Button(
                panel,
                label=f"{i18n.t('remove_attachment')} {filename}",
            )
            # Bind by index, not path: the same file can legitimately be
            # staged twice (attached in two separate picks), and removing by
            # path used to delete every entry sharing it instead of just the
            # one the user clicked remove on.
            btn.Bind(
                wx.EVT_BUTTON,
                lambda evt, i=idx: self._on_remove_attachment(i),
            )
            sizer.Add(btn, 0, wx.BOTTOM, 3)
        panel.Layout()
        if self._attachment_panel.IsShown():
            self._attachment_panel.Layout()
            self.conversation_panel.Layout()

    def _on_remove_attachment(self, index: int):
        """Remove one staged file and rebuild the list (or close the panel)."""
        if 0 <= index < len(self._staged_attachments):
            del self._staged_attachments[index]
        if not self._staged_attachments:
            self._hide_attachment_panel()
        else:
            self._rebuild_attachment_list()

    def _hide_attachment_panel(self):
        self._staged_attachments = []
        self._attachment_panel.Hide()
        if hasattr(self, "message_label"):
            self.message_label.Show()
            self.message_field.Show()
            if hasattr(self, "_emoji_btn"):
                self._emoji_btn.Show()
            if self.message_field.GetValue().strip():
                self.send_message_btn.Show()
            else:
                self.record_voice_message_btn.Show()
                if hasattr(self, "_record_voice_system_btn"):
                    self._record_voice_system_btn.Show()
            self._add_attachment_btn.Show()
        if hasattr(self, "conversation_panel") and self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

    def _on_add_more_files(self, event):
        """Re-open the file picker to add more files to the staging list."""
        self.on_add_attachment(event)

    def _on_send_attachment(self, event=None):
        """Enqueue all staged attachments as outgoing messages."""
        if not self._staged_attachments or self.conversation is None:
            return
        remote_jid = self.conversation.get("remoteJid", "")
        if not remote_jid:
            return
        caption = self._consume_attachment_caption()

        # Capture quoted state before looping (cleared after all enqueued)
        quoted = self._quoted_message
        for attachment in list(self._staged_attachments):
            self._send_attachment_file(remote_jid, attachment["path"],
                                       attachment.get("media_type", "document"), caption, quoted)

        self._on_cancel_reply()  # clear quoted state after send
        self.main_window.mark_conversation_as_read(remote_jid)
        self._hide_attachment_panel()
        # Attachment-panel teardown performs its own layout pass. Reassert the
        # transfer UI afterwards so that pass cannot swallow the new gauge.
        self._sync_pending_document_gauge()
        self.main_window._schedule_set_chats()
        self.message_field.SetFocus()

        # Refresh conversation list preview to show the last sent attachment.
        self.main_window._schedule_set_chats()

    # ── Contact message helpers ──────────────────────────────────────────────

    def _send_attachment_file(self, remote_jid, path, media_type, caption, quoted) -> bool:
        """Send one file as a message of *remote_jid*: its pending row, its
        cache entry and its queue item. True when it was queued, False when it
        was refused (too large). The composer is not touched: the attachment
        panel sends through here, and so does Resend (message_actions.py) for a
        file whose send failed or was never confirmed."""
        _VTYPE = {
            "image":    "imageMessage",
            "video":    "videoMessage",
            "audio":    "audioMessage",
            "document": "documentMessage",
        }

        # WhatsApp's own ceiling is 2 GB for documents and 1 GB for photos,
        # videos and audio — WinZapp used to cap documents at 1 GB as well,
        # for no reason other than sharing one constant with the other types.
        # WinZapp's WPPConnect patch transfers large files to Chromium in
        # bounded chunks, avoiding the single oversized CDP argument that
        # previously killed the session — that used to be document-only but now
        # covers image/video/audio too (see
        # core/wppconnect_sender_layer_patch.py), so size alone is no longer
        # what limits this; the ceilings below are WhatsApp's, not ours.
        #
        # These are only the FIRST of four gates a large file passes, and all
        # four have to agree or a document between 1 and 2 GB is refused
        # somewhere the user cannot see: this pre-check, send_media_attachment()
        # in main.py, the maxFileSize WPPConnect is told about in
        # core/websocket_client.py, and WhatsApp Web's own MediaGatingUtils
        # ceiling raised by the sender-layer patch.
        _MAX_DOCUMENT_BYTES = 2 * 1024 * 1024 * 1024
        _MAX_DOCUMENT_MB    = 2048
        _MAX_MEDIA_BYTES    = 1 * 1024 * 1024 * 1024
        _MAX_MEDIA_MB       = 1024
        i18n = self.main_window.i18n
        if media_type == "audio" and exceeds_aac_channel_limit(path):
            # More channels than AAC carries: converting would downmix,
            # and an attachment never loses a channel, so the original
            # goes untouched as a document. Decided here and not inside
            # send_media_attachment(): the pending row built below has to
            # be a documentMessage too, or on_new_message()'s by-type
            # echo matching never binds the document echo to it.
            logging.info(
                "[attachments] audio has more than 8 channels; sending "
                "the original untouched as a document"
            )
            media_type = "document"

        vtype      = _VTYPE.get(media_type, "documentMessage")
        is_document = vtype == "documentMessage"
        max_bytes = _MAX_DOCUMENT_BYTES if is_document else _MAX_MEDIA_BYTES
        max_mb    = _MAX_DOCUMENT_MB if is_document else _MAX_MEDIA_MB

        try:
            file_size = os.path.getsize(path)
        except OSError:
            # Unreadable size is not a reason to refuse the send — the
            # limit check below simply can't run, exactly as before.
            file_size = None
        if file_size is not None and file_size > max_bytes:
            wx.MessageBox(
                i18n.t("media_too_large").format(max_mb=max_mb),
                i18n.t("app_name"),
                wx.OK | wx.ICON_ERROR,
                self,
            )
            return False

        local_id   = str(uuid.uuid4())
        _body = {
            "caption":  caption,
            "fileName": os.path.basename(path),
            "mimetype": mimetypes.guess_type(path)[0]
                        or "application/octet-stream",
        }
        if vtype == "documentMessage" and file_size is not None:
            # Issue #96: a document we send showed no size, while the same
            # document received from someone else did. The rendering is
            # shared and keys only on fileLength — the field just never
            # reached it. WPPConnect's echo of our own send DOES carry the
            # size, but on_new_message() merges an echo into the pending
            # virtual message by copying id/timestamp/participant onto it,
            # keeping this body, so the echo's copy is discarded and the
            # record persisted to the DB never has it. (It reappeared only
            # after a resync re-fetched the message from the server through
            # _normalize_wpp_message.)
            #
            # Filling it here rather than from the echo is deliberate: the
            # line is complete the moment it appears, instead of being
            # rewritten once the echo lands — and rewriting a list row is
            # what makes a screen reader read the whole row out again (see
            # _release_chain_held_repaints()).
            #
            # Only documents: theirs is the one type whose rendered line
            # shows a size, and fileLength is also read by
            # MainWindow.sync_if_media() as an auto-download size gate, so
            # populating it for our own images/videos would change that
            # decision for something this issue never asked about.
            _body["fileLength"] = file_size
        if media_type == "audio":
            _dur = self._probe_audio_duration(path)
            if _dur is not None:
                _body["seconds"] = _dur
        elif media_type == "video":
            # Unlike audio, video_seconds() doesn't trust a plain "seconds"
            # of 0 (WhatsApp itself sends that to mean "not stated" — see
            # that function's own docstring), so a video we're sending
            # needs its length under _measured_seconds instead, same key
            # _learn_video_duration() fills in for a received video once
            # it's played. Without this, a video sent as a WinZapp
            # attachment showed no duration in the list until the sender
            # opened it themselves at least once.
            _dur = self._probe_audio_duration(path)
            if _dur is not None and _dur >= 0:
                _body[MEASURED_SECONDS_KEY] = _dur
        virtual_msg = {
            "_local_pending": True,
            "_local_id":      local_id,
            "key": {"id": local_id, "fromMe": True, "remoteJid": remote_jid},
            "messageType": vtype,
            "message": {vtype: _body},
            "messageTimestamp": int(time.time()),
            "pushName": "",
            # What Try sending again sends, should this fail or go unconfirmed
            # (core/message_resend.py).
            RESEND_PATH_KEY: path,
        }
        if quoted:
            _qk = quoted.get("key", {})
            virtual_msg["contextInfo"] = {
                "stanzaId":      _qk.get("id", ""),
                "participant":   _qk.get("participant", ""),
                "quotedMessage": quoted.get("message") or {},
            }

        self._clear_empty_placeholder()
        self._sorted_messages.append(virtual_msg)
        append_message_row(self, self._render_message_line(virtual_msg))
        # The row just added — not the typing row that may sit below it.
        last = message_row_count(self) - 1
        if last >= 0:
            self.messages_list.Select(last, True)
            self.messages_list.EnsureVisible(last)
        def _update_upload_progress(
            progress, local_id=local_id,
            notify=CoalescedCallback(wx.CallAfter, self.update_media_upload_progress),
        ):
            notify(local_id, progress)

        pm = PendingMessage(
            local_id, remote_jid,
            media_path=path, media_type=media_type, caption=caption,
            quoted=quoted, progress_callback=_update_upload_progress,
        )
        self._register_virtual_msg(virtual_msg)

        # Pre-cache the file under local_id BEFORE enqueueing the actual
        # send: _mark_message_sent() renames the cache entry from
        # local_id to the real WhatsApp id as soon as the send is
        # confirmed, so the file must already exist under local_id by
        # then, or that rename silently no-ops and the cache is never
        # found under the real id afterwards.
        def _cache_then_enqueue(pm=pm, local_id=local_id, path=path, media_type=media_type):
            self._pre_cache_sent_media(local_id, path, media_type)
            self.main_window.message_queue.enqueue(pm)

        threading.Thread(target=_cache_then_enqueue, daemon=True).start()

        self._show_media_transfer_gauge()
        return True

    def _on_caption_field_key_down(self, event):
        """Shift+Enter and Ctrl+Enter add a line to the caption; plain Enter
        still sends (EVT_TEXT_ENTER), like the message field. WriteText()
        keeps the caret right on Windows, where the control stores \\r\\n."""
        if (event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
                and (event.ShiftDown() or event.ControlDown())):
            self._caption_field.WriteText("\n")
            return
        event.Skip()

    def _consume_attachment_caption(self) -> str:
        """Return the staged caption and clear it for the next attachment."""
        caption = normalize_line_separators(self._caption_field.GetValue()).strip()
        self._caption_field.Clear()
        return caption
