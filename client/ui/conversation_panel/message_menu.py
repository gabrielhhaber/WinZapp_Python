"""MessageMenuMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import os
import pyperclip
import tempfile
import threading
import wx
from core.quote_recovery import RECOVERED_FROM_QUOTE
from ui.conversation_panel.media_paths import (
    _SAVEABLE_MESSAGE_TYPES,
    cached_media_path,
)
from app_paths import data_path
from core.utils import (
    decrypt_bytes,
    format_number,
)
from core.message_edit import (
    edit_kind,
    edit_window_open,
)
from core.call_log import (
    is_call_log,
    is_returnable_missed_call,
)
from core.transcription import (
    message_audio,
    stored as stored_transcription,
)
from core.wrapped_text import (
    original_range,
    selection_offsets,
    word_wrap,
)


class MessageMenuMixin:
    """The message context menu and its read-only actions: data, copy, reply, go
    to quoted, text popup.
    """

    def on_messages_context_menu(self, event):
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        if self._is_separator(self._sorted_messages[index]):
            return  # no context menu for separator
        msg      = self._sorted_messages[index]
        msg_type = msg.get("messageType", "")
        msg_id   = msg.get("key", {}).get("id", "")
        i18n     = self.main_window.i18n
        # A call record takes none of the per-message actions WhatsApp refuses
        # for it (reply, react, forward, star, pin); the handlers refuse them
        # too (_reject_system_event_action), so the accelerators stay safe.
        is_call = is_call_log(msg)

        menu = wx.Menu()

        if is_call and is_returnable_missed_call(
                msg, str((self.conversation or {}).get("remoteJid") or "")):
            return_item = menu.Append(
                wx.ID_ANY, f"{i18n.t('return_call_button')}\tCtrl+Shift+R")
            self.Bind(wx.EVT_MENU, lambda e, m=msg: self._return_call(m), return_item)
            menu.AppendSeparator()

        if getattr(self, "selected_messages", None):
            mass_menu = wx.Menu()

            # Each entry carries its own dedicated shortcut (see
            # create_accel_conversation's ID_BULK_*) — those work whatever
            # "Substituir atalhos por ações em massa..." is set to, unlike the
            # single-message shortcuts this submenu's actions used to be
            # reachable through only when that setting was on.
            copy_selected_item = mass_menu.Append(
                wx.ID_ANY, f"{i18n.t('copy_selected')}\tCtrl+Alt+Shift+C")
            self.Bind(wx.EVT_MENU, self._on_mass_copy_messages, copy_selected_item)

            fwd_item = mass_menu.Append(
                wx.ID_ANY, f"{i18n.t('forward_selected')}\tCtrl+Alt+Shift+E")
            self.Bind(wx.EVT_MENU, self._on_mass_forward_messages, fwd_item)

            star_item = mass_menu.Append(
                wx.ID_ANY, f"{i18n.t('star_selected')}\tCtrl+Alt+Shift+F")
            self.Bind(wx.EVT_MENU, self._on_mass_star_messages, star_item)

            pin_item = mass_menu.Append(
                wx.ID_ANY, f"{i18n.t('pin_selected')}\tCtrl+Alt+Shift+X")
            self.Bind(wx.EVT_MENU, self._on_mass_pin_messages, pin_item)

            save_item = mass_menu.Append(
                wx.ID_ANY, f"{i18n.t('save_selected')}\tCtrl+Alt+Shift+S")
            self.Bind(wx.EVT_MENU, self._on_mass_save_messages, save_item)

            delete_item = mass_menu.Append(
                wx.ID_ANY, f"{i18n.t('delete_selected')}\tCtrl+Shift+Delete")
            self.Bind(wx.EVT_MENU, self._on_mass_delete_messages, delete_item)

            menu.AppendSubMenu(mass_menu, i18n.t("mass_actions"))
            menu.AppendSeparator()

        # ── "Ir para a mensagem citada" (only for reply messages) ─────────────
        ctx_reply = self._get_context_info(msg)
        if ctx_reply:
            goto_item = menu.Append(
                wx.ID_ANY,
                f"{i18n.t('goto_quoted')}\tAlt+Shift+Q",
            )
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg, c=ctx_reply: self._on_menu_goto_quoted(m, c),
                goto_item,
            )
            menu.AppendSeparator()

        # ── Most-used reactions submenu (if this conversation has reactions) ──
        if self._reaction_map and not is_call:
            all_emojis: dict = {}
            for msg_reactions in self._reaction_map.values():
                for em in msg_reactions.values():
                    all_emojis[em] = all_emojis.get(em, 0) + 1
            if all_emojis:
                # issue #67: mark whichever of these I already sent to THIS
                # message as checked, and toggling that one off removes it
                # instead of resending the same emoji — there was previously
                # no way to remove a reaction from the UI at all.
                current_emoji = (self._reaction_map.get(msg_id) or {}).get(self._SELF_REACTOR_KEY, "")
                top_emojis = sorted(all_emojis.items(), key=lambda x: x[1], reverse=True)[:5]
                most_used_sub = wx.Menu()
                for em, _cnt in top_emojis:
                    sub_item = most_used_sub.AppendCheckItem(wx.ID_ANY, em)
                    is_current = em == current_emoji
                    sub_item.Check(is_current)
                    self.Bind(
                        wx.EVT_MENU,
                        lambda e, m=msg, em=em, cur=is_current: self._send_reaction(m, "" if cur else em),
                        sub_item,
                    )
                menu.AppendSubMenu(most_used_sub, i18n.t("most_used_reactions"))
                menu.AppendSeparator()

        # Message info (Alt+Shift+D)
        data_item = menu.Append(wx.ID_ANY, f"{i18n.t('message_data')}\tAlt+Shift+D")
        self.Bind(
            wx.EVT_MENU,
            lambda e, m=msg: self._on_menu_message_data(m),
            data_item,
        )

        menu.AppendSeparator()

        # Copy text (only for text messages)
        _TEXT_TYPES = ("conversation", "extendedTextMessage")
        if msg_type in _TEXT_TYPES:
            copy_item = menu.Append(wx.ID_ANY, f"{i18n.t('copy_message_text')}\tCtrl+C")
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_menu_copy_message(m),
                copy_item,
            )

        # Copy file (for image, video, document, and audio/voice messages)
        _MEDIA_TYPES = ("imageMessage", "videoMessage", "documentMessage", "audioMessage")
        if msg_type in _MEDIA_TYPES:
            copy_file_item = menu.Append(wx.ID_ANY, f"{i18n.t('copy_file')}\tCtrl+C")
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_menu_copy_file(m),
                copy_file_item,
            )

        # ── Contact card actions (issue #84) ─────────────────────────────
        # These used to exist only as two Tab-reachable buttons next to the
        # message list (Conversar / Salvar contato), which a user navigating
        # the messages with the arrow keys never meets, and there was no way
        # at all to see or copy the number. The buttons stay where they are;
        # this is the same set plus the two missing actions, in the place a
        # screen-reader user actually looks for per-message actions.
        if msg_type == "contactMessage":
            details_item = menu.Append(wx.ID_ANY, i18n.t("contact_view_details"))
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_contact_view_details(m),
                details_item,
            )
            copy_num_item = menu.Append(
                wx.ID_ANY, f"{i18n.t('contact_copy_number')}\tCtrl+C"
            )
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_contact_copy_number(m),
                copy_num_item,
            )
            _card_jid = self._jid_from_vcard(
                ((msg.get("message") or {}).get("contactMessage") or {}).get("vcard", "")
            )
            if _card_jid:
                converse_card_item = menu.Append(wx.ID_ANY, i18n.t("converse"))
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, j=_card_jid: self._on_contact_converse(None, jid=j),
                    converse_card_item,
                )
            save_card_item = menu.Append(
                wx.ID_ANY, f"{i18n.t('save_contact')}\tCtrl+Shift+S"
            )
            self.Bind(
                wx.EVT_MENU,
                lambda e: self._on_save_contact_message(None),
                save_card_item,
            )

        # Copy caption (photo/video/document messages that have one) — a
        # separate shortcut from Ctrl+C, which for these types already
        # copies the file itself (see _on_accel_copy_message).
        _has_caption = bool(self._get_message_caption(msg))
        if _has_caption:
            copy_caption_item = menu.Append(wx.ID_ANY, f"{i18n.t('copy_caption')}\tCtrl+Shift+C")
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_menu_copy_caption(m),
                copy_caption_item,
            )

        # Reply (Alt+R)
        if not is_call:
            reply_item = menu.Append(wx.ID_ANY, f"{i18n.t('reply_message')}\tAlt+R")
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_menu_reply(m),
                reply_item,
            )

        # ── Group-only: Reply privately / Converse with participant ────────────
        _conv_jid    = self.conversation.get("remoteJid", "") if self.conversation else ""
        _is_group    = _conv_jid.endswith("@g.us")
        _is_from_me  = msg.get("key", {}).get("fromMe", False)
        if _is_group and not _is_from_me and not is_call:
            _participant_jid = (
                msg.get("key", {}).get("participant", "")
                or msg.get("participant", "")
            )
            if _participant_jid:
                private_reply_item = menu.Append(
                    wx.ID_ANY,
                    f"{i18n.t('reply_private')}\tAlt+Shift+R",
                )
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, m=msg, pj=_participant_jid: self._on_menu_reply_private(m, pj),
                    private_reply_item,
                )
                _pname = self._get_participant_name(_participant_jid, msg)
                converse_item = menu.Append(
                    wx.ID_ANY,
                    f"{i18n.t('converse_with').format(name=_pname)}\tAlt+Shift+V",
                )
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, pj=_participant_jid, pn=_pname: self._on_menu_converse_private(pj, pn),
                    converse_item,
                )

        if not is_call:
            # React (opens emoji picker) — Ctrl+Shift+R
            react_item = menu.Append(wx.ID_ANY, f"{i18n.t('react_to_message')}\tCtrl+Shift+R")
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_menu_react(m),
                react_item,
            )

            # Show text popup (text messages, or a photo/video/document that has a caption)
            if msg_type in _TEXT_TYPES or _has_caption:
                show_text_item = menu.Append(wx.ID_ANY, f"{i18n.t('show_msg_text')}\tAlt+C")
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, m=msg: self._show_message_text_popup(m),
                    show_text_item,
                )

            # Forward (Ctrl+Shift+E)
            fwd_item = menu.Append(wx.ID_ANY, f"{i18n.t('forward_message')}\tCtrl+Shift+E")
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_menu_forward(m),
                fwd_item,
            )

            # Star / Unstar (Ctrl+Shift+O)
            is_starred = bool(msg.get("starred"))
            star_label = i18n.t("unstar_message") if is_starred else i18n.t("star_message")
            star_item = menu.Append(wx.ID_ANY, f"{star_label}\tCtrl+Shift+O")
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_menu_star(m),
                star_item,
            )

            # Pin / Unpin in chat (Ctrl+Shift+P) — the real WhatsApp message-pin
            # feature, visible to every participant, unlike the private star
            # above. Shares its accelerator with the recording pause/resume
            # shortcut (_on_ctrl_shift_p): only one is ever applicable at a time
            # (pause/resume only does anything while actively recording audio).
            is_pinned = bool(msg.get("pinInChat"))
            pin_msg_label = i18n.t("unpin_message") if is_pinned else i18n.t("pin_message")
            pin_msg_item = menu.Append(wx.ID_ANY, f"{pin_msg_label}\tCtrl+Shift+P")
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_menu_pin_message(m),
                pin_msg_item,
            )

        # Save As (media only, only when the file is already cached locally).
        # Audio is excluded here because it has its own branch below (separate
        # cache, its own label) — the set itself stays shared with
        # _on_action_save_as() so the menu and the shortcut can never again
        # disagree about what is saveable.
        _SAVEABLE = _SAVEABLE_MESSAGE_TYPES - {"audioMessage"}
        clean_msg_id = msg_id
        if "_" in msg_id:
            parts = msg_id.split("_")
            clean_msg_id = parts[2] if len(parts) > 2 else parts[-1]
        media_actions_added = False
        if msg_type in _SAVEABLE and os.path.isfile(
            data_path("media", f"{clean_msg_id}.wzmedia")
        ):
            menu.AppendSeparator()
            media_actions_added = True
            save_item = menu.Append(
                wx.ID_ANY, f"{i18n.t('save_as')}\tCtrl+Shift+S"
            )
            self.Bind(wx.EVT_MENU, self._on_action_save_as, save_item)
        elif msg_type == "audioMessage":
            # Voice messages are cached separately (voice_messages/*.msv) and
            # can be saved even while a download is still pending — the save
            # flow downloads it first if needed, same as the other media types.
            menu.AppendSeparator()
            media_actions_added = True
            save_audio_item = menu.Append(
                wx.ID_ANY, f"{i18n.t('save_audio_as')}\tCtrl+Shift+S"
            )
            self.Bind(wx.EVT_MENU, self._on_action_save_as, save_audio_item)

        # AI transcription / description (ui/conversation_panel/ai_actions.py).
        # Offered only while the feature is on and a provider with a key takes
        # this kind of media. Ctrl+Shift+I is the accelerator of the same action;
        # it sits with the media actions, right after Save as.
        ai_label = self._ai_menu_label(msg, i18n)
        if ai_label:
            if not media_actions_added:
                menu.AppendSeparator()
                media_actions_added = True
            ai_item = menu.Append(wx.ID_ANY, f"{ai_label}\tCtrl+Shift+I")
            self.Bind(
                wx.EVT_MENU, lambda e, m=msg: self._on_ai_action(message=m), ai_item
            )

        if self._saved_media_path(msg):
            if not media_actions_added:
                menu.AppendSeparator()
            show_in_folder_item = menu.Append(
                wx.ID_ANY, f"{i18n.t('show_in_folder')}\tCtrl+Enter"
            )
            self.Bind(
                wx.EVT_MENU, self._on_action_show_in_folder, show_in_folder_item
            )

        # Transcribe (Alt+Shift+T) — next to the audio's own Save As, for the
        # same messages message_audio.is_transcribable() accepts: voice notes,
        # audio files, and documents whose mimetype is audio/*.
        # A message whose transcription is stored offers it instead of the
        # wait: Alt+Shift+T moves to `transcription_view` (the shortcut opens the
        # stored one too), and running it again or deleting it are items of
        # their own. The row itself says nothing about it: a marker there
        # would be read on every pass over every transcribed note, for good,
        # and the one action it would inform — Alt+Shift+T — already does the
        # right thing either way, opening the stored text or starting a run.
        if message_audio.is_transcribable(msg):
            if stored_transcription.saved_transcription(msg) is not None:
                view_item = menu.Append(
                    wx.ID_ANY, f"{i18n.t('transcription_view')}\tAlt+Shift+T"
                )
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, m=msg: self._on_menu_transcribe(m),
                    view_item,
                )
                again_item = menu.Append(wx.ID_ANY, i18n.t("transcription_transcribe_again"))
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, m=msg: self._on_menu_transcribe_again(m),
                    again_item,
                )
                delete_item = menu.Append(wx.ID_ANY, i18n.t("transcription_delete"))
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, m=msg: self._on_menu_delete_transcription(m),
                    delete_item,
                )
            else:
                transcribe_item = menu.Append(
                    wx.ID_ANY, f"{i18n.t('transcribe_message')}\tAlt+Shift+T"
                )
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, m=msg: self._on_menu_transcribe(m),
                    transcribe_item,
                )

        # Edit (own text messages within WhatsApp's edit window — see
        # core.message_edit.EDIT_UI_WINDOW_SECONDS for how it was measured)
        _is_own      = msg.get("key", {}).get("fromMe", False)
        _is_text     = msg_type in ("conversation", "extendedTextMessage")
        _can_edit    = edit_window_open(msg.get("messageTimestamp"))
        # Text, or the caption an own image/video/document already has —
        # see core.message_edit.edit_kind().
        if _is_own and edit_kind(msg) is not None and _can_edit:
            edit_item = menu.Append(wx.ID_ANY, f"{i18n.t('edit_message')}\tAlt+E")
            self.Bind(
                wx.EVT_MENU,
                lambda e, i=index, m=msg: self._on_menu_edit_message(i, m),
                edit_item,
            )

        # Resend (text messages WinZapp itself never confirmed — see
        # _mark_message_unconfirmed's docstring). Deleting one already works
        # today (treated as nothing-to-revoke, see _on_menu_delete_message);
        # this is the other half — a way to try again instead of only being
        # able to give up on it.
        if _is_text and msg.get("_send_unconfirmed"):
            resend_item = menu.Append(wx.ID_ANY, i18n.t("resend_message"))
            self.Bind(
                wx.EVT_MENU,
                lambda e, m=msg: self._on_menu_resend_message(m),
                resend_item,
            )

        menu.AppendSeparator()

        # Select / Unselect message (Ctrl+Space) — mirrors the label to
        # whether msg is already in self.selected_messages, same toggle
        # _toggle_message_selection() applies for the Ctrl+Space shortcut.
        is_selected = msg_id in self.selected_messages
        select_label = i18n.t("unselect_message") if is_selected else i18n.t("select_message")
        select_item = menu.Append(wx.ID_ANY, f"{select_label}\tCtrl+Space")
        self.Bind(
            wx.EVT_MENU,
            lambda e, m=msg: self._toggle_message_selection(m),
            select_item,
        )

        # Delete message — Delete key
        del_item = menu.Append(wx.ID_ANY, f"{i18n.t('delete_message')}\tDelete")
        self.Bind(
            wx.EVT_MENU,
            lambda e, i=index: self._on_menu_delete_message(i),
            del_item,
        )

        self.PopupMenu(menu)
        menu.Destroy()

    # ── Message context menu handlers ────────────────────────────────────────

    def _on_menu_message_data(self, msg: dict):
        i18n     = self.main_window.i18n
        ts       = self._extract_timestamp(msg)
        time_str = self._format_date(ts) if ts else ""
        sender   = self._sender_label(msg)
        content  = self._get_message_content(msg)

        lines = [f"{sender}: {content}"]
        if time_str:
            lines.append(time_str)

        history = self._status_history_lines(msg)
        if history:
            lines.extend(history)
        else:
            status = self._map_status(msg)
            if status:
                lines.append(f"{i18n.t('message_data_status_label')}: {status}")

        dlg = wx.Dialog(
            self.main_window, title=i18n.t("message_data"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=(420, 280),
        )
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        info_ctrl = wx.TextCtrl(
            panel, value="\n".join(lines),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        sizer.Add(info_ctrl, 1, wx.EXPAND | wx.ALL, 8)
        close_btn = wx.Button(panel, wx.ID_OK, label=i18n.t("close"))
        sizer.Add(close_btn, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        panel.SetSizer(sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(panel, 1, wx.EXPAND)
        dlg.SetSizer(dlg_sizer)
        info_ctrl.SetFocus()
        dlg.ShowModal()
        dlg.Destroy()

    # Media types WhatsApp allows a caption on (audio/sticker never do).
    _CAPTIONABLE_TYPES = ("imageMessage", "videoMessage", "documentMessage")

    def _get_message_caption(self, msg: dict) -> str:
        """Caption text for a photo/video/document message, or "" if none
        (either the type doesn't support captions, or this one has none)."""
        msg_type = msg.get("messageType", "")
        if msg_type not in self._CAPTIONABLE_TYPES:
            return ""
        msg_obj = msg.get("message") or {}
        inner = msg_obj.get(msg_type)
        if not isinstance(inner, dict):
            return ""
        return (inner.get("caption") or "").strip()

    def _on_menu_copy_message(self, msg: dict):
        text = self._message_text_with_names(msg)
        if text:
            try:
                pyperclip.copy(text)
                self.main_window.output(self.main_window.i18n.t("msg_copied"))
            except Exception:
                self.main_window.output(self.main_window.i18n.t("msg_copy_error"))
        else:
            self.main_window.output(self.main_window.i18n.t("msg_copy_error"))

    def _on_menu_copy_caption(self, msg: dict):
        """Copy a photo/video/document message's caption text — kept
        separate from _on_menu_copy_message()/Ctrl+C, which for these
        types already copies the actual file to the clipboard."""
        text = self._get_message_caption(msg)
        if text:
            try:
                pyperclip.copy(text)
                self.main_window.output(self.main_window.i18n.t("msg_copied"))
            except Exception:
                self.main_window.output(self.main_window.i18n.t("msg_copy_error"))
        else:
            self.main_window.output(self.main_window.i18n.t("msg_copy_error"))

    def _on_menu_copy_file(self, msg: dict):
        """Decrypt media file and place it on the clipboard as a file object with original filename."""
        msg_type = msg.get("messageType", "")
        msg_obj  = msg.get("message") or {}
        msg_id   = msg.get("key", {}).get("id", "")
        if not msg_id:
            return

        # Text messages store the payload as a plain string under the
        # messageType key (e.g. {"conversation": "..."}), not a dict — guard
        # before calling .get() on it.
        inner = msg_obj.get(msg_type)
        if not isinstance(inner, dict):
            inner = {}
        media_data = msg.get("mediaData") or {}
        is_ptt = bool(inner.get("ptt", False) or inner.get("isPtt", False) or media_data.get("ptt", False))

        if msg_type not in ("documentMessage", "imageMessage", "videoMessage", "audioMessage"):
            return

        default_file = self._resolve_media_filename(msg)
        # Through the shared resolver: a voice note is cached under
        # voice_messages/<id>.msv, and hardcoding the media/ path here is what
        # made Ctrl+C on an already-downloaded audio announce "could not
        # download this media file, the link may have expired".
        media_path = cached_media_path(msg_type, msg_id)

        def _run():
            if not self._ensure_media_on_disk(msg, media_path):
                return
            try:
                with open(media_path, "rb") as fh:
                    content = decrypt_bytes(fh.read(), self.main_window.key)

                # Write decrypted content to a temp file with original filename
                tmp_dir = tempfile.mkdtemp(prefix="wz_copy_")
                target_file = os.path.join(tmp_dir, default_file)
                with open(target_file, "wb") as fh:
                    fh.write(content)
                
                # Copy the temporary file to clipboard (must run on the main thread)
                def _to_clipboard(path=target_file):
                    try:
                        if wx.TheClipboard.Open():
                            file_data = wx.FileDataObject()
                            file_data.AddFile(path)
                            wx.TheClipboard.SetData(file_data)
                            wx.TheClipboard.Close()
                            self.main_window.output(self.main_window.i18n.t("msg_copied"))
                        else:
                            self.main_window.output(self.main_window.i18n.t("msg_copy_error"))
                    except Exception as e:
                        print(f"[_to_clipboard] Clipboard error: {e}")
                        self.main_window.output(self.main_window.i18n.t("msg_copy_error"))

                wx.CallAfter(_to_clipboard)
            except Exception as exc:
                print(f"[_on_menu_copy_file] Error copying file: {exc}")
                wx.CallAfter(self.main_window.output, self.main_window.i18n.t("msg_copy_error"))

        threading.Thread(target=_run, daemon=True).start()

    def _on_accel_focus_field(self, event):
        """Alt+<message-label mnemonic>: focus the message field.

        See create_accel_conversation()'s comment on ID_ALT_FOCUS_FIELD —
        this exists because message_label's own "&" mnemonic stopped
        redirecting focus once its text changed to the reply-mode label.
        """
        if hasattr(self, "message_field"):
            self.message_field.SetFocus()

    def _on_accel_focus_list(self, event):
        """Alt+<messages-label mnemonic>: focus the messages list.

        See create_accel_conversation()'s comment on ID_ALT_FOCUS_LIST —
        messages_label's own "&" mnemonic stops redirecting focus to
        messages_list while the in-conversation search panel is shown.
        """
        if self._no_conversation_open_announced():
            return
        if hasattr(self, "messages_list"):
            self.messages_list.SetFocus()

    def _on_menu_reply(self, msg: dict):
        """Enter reply mode: change field label, store quoted message, focus field."""
        if is_call_log(msg):
            self._reject_system_event_action(msg)
            return
        if self._is_system_event(msg):
            # System events ("Fulano tornou Sicrano administrador do grupo",
            # joins/leaves, revokes) carry no quotable content — WhatsApp
            # rejects a quoted reply to them (HTTP 500), so entering reply
            # mode then watching the quote fall back on send is confusing.
            # Announce the same message the send path already uses for a
            # lost quote and stay out of reply mode entirely.
            self.main_window.output(self.main_window.i18n.t("reply_quote_lost"))
            return
        self._quoted_message = msg
        i18n      = self.main_window.i18n
        sender    = self._sender_label(msg)
        jid       = self.conversation.get("remoteJid", "") if self.conversation else ""
        is_group  = jid.endswith("@g.us")

        if is_group and not msg.get("key", {}).get("fromMe", False):
            group_name = self.conversation_name
            label = i18n.t("reply_to_group").format(name=sender, group=group_name)
        else:
            label = i18n.t("reply_to").format(name=sender)

        self.message_label.SetLabel(label)
        self._remove_quote_btn.Show()
        self.conversation_panel.Layout()
        self.message_field.SetFocus()

    def _on_menu_reply_private(self, msg: dict, participant_jid: str):
        """Open a private conversation with the group participant and cite their message."""
        if is_call_log(msg):
            self._reject_system_event_action(msg)
            return
        if self._is_system_event(msg):
            # Same guard as _on_menu_reply: a system event has no quotable
            # content, and navigating away from the group to a private chat
            # to quote it would leave the user stranded on the wrong chat.
            self.main_window.output(self.main_window.i18n.t("reply_quote_lost"))
            return
        mw = self.main_window
        chat = mw.get_chat(participant_jid)
        if chat is None:
            pname = self._get_participant_name(participant_jid, msg)
            chat = {"remoteJid": participant_jid, "pushName": pname}
        self.navigate_to_conversation(chat)
        # Set up reply quoting the group message
        self._quoted_message = msg
        self._on_menu_reply(msg)

    def _on_menu_converse_private(self, participant_jid: str, participant_name: str):
        """Open a private conversation with the group participant (no citation)."""
        mw = self.main_window
        chat = mw.get_chat(participant_jid)
        if chat is None:
            chat = {"remoteJid": participant_jid, "pushName": participant_name}
        self.navigate_to_conversation(chat)

    def _on_menu_goto_quoted(self, msg: dict, ctx: dict):
        """Move focus in the messages list to the quoted message — or, if
        the quote is actually a reply to a STATUS (never present in this
        chat's own message list at all), open the status viewer instead."""
        quoted_id = ctx.get("stanzaId") or ""
        if not quoted_id:
            self._show_quoted_not_found_error()
            return
        for i, m in enumerate(self._sorted_messages):
            if not self._is_separator(m) and m.get("key", {}).get("id") == quoted_id:
                self.messages_list.Focus(i)
                self.messages_list.Select(i, True)
                self.messages_list.EnsureVisible(i)
                self.messages_list.SetFocus()
                return
        # The target may be older than the rendered page but still be present
        # in the local database.  The old code incorrectly reported an error.
        jid = (self.conversation or {}).get("remoteJid", "")
        try:
            quoted = self.main_window.db.get_message(jid, quoted_id)
        except Exception:
            logging.exception("[goto quoted] Database lookup failed")
            quoted = None
        if quoted:
            records = (
                (self.conversation.get("messages") or {}).get("messages") or {}
            ).get("records") or []
            records = self._deduplicate_messages(list(records) + [quoted])
            records.sort(key=self._extract_timestamp)
            self.conversation.setdefault("messages", {}).setdefault(
                "messages", {}
            )["records"] = records
            self.populate_messages(preserve_focus=True)
            for i, candidate in enumerate(self._sorted_messages):
                if (
                    not self._is_separator(candidate)
                    and candidate.get("key", {}).get("id") == quoted_id
                ):
                    self.messages_list.Focus(i)
                    self.messages_list.Select(i, True)
                    self.messages_list.EnsureVisible(i)
                    self.messages_list.SetFocus()
                    return
            # Pagination keeps the newest configured page.  An older quoted
            # target can therefore still fall just outside it; expose that one
            # row at the top without starting a server-side history request.
            self._all_sorted_messages.insert(0, quoted)
            self._sorted_messages.insert(0, quoted)
            self.messages_list.InsertItem(0, self._render_message_line(quoted))
            self.messages_list.Focus(0)
            self.messages_list.Select(0, True)
            self.messages_list.EnsureVisible(0)
            self.messages_list.SetFocus()
            return
        if self._goto_quoted_status(quoted_id, ctx):
            return
        self._show_quoted_not_found_error()

    def _goto_quoted_status(self, quoted_id: str, ctx: dict) -> bool:
        """Best-effort: the id wasn't found in this chat's own messages —
        check whether it's a status reply instead. A status still tracked
        in main_window._status_updates opens in the real viewer; one old
        enough to have aged out of there is rebuilt from the quoted
        content WhatsApp still ships inline on the reply itself (same
        shape a real status dict has) so it opens the same way rather than
        falling back to a bare text popup. Returns True if it opened
        something.
        """
        mw = self.main_window
        if not hasattr(mw, "status_panel"):
            return False
        sp = mw.status_panel
        updates = getattr(mw, "_status_updates", {})
        items = [m for msgs in updates.values() for m in msgs]
        my_statuses, contacts = sp._parse_statuses(items, mw.i18n)

        for idx, st in enumerate(my_statuses):
            if st.get("key", {}).get("id") == quoted_id:
                self._open_my_status_dialog_at(my_statuses, idx)
                return True

        for entry in contacts:
            for s_idx, st in enumerate(entry.get("statuses", [])):
                if st.get("key", {}).get("id") == quoted_id:
                    self._open_status_panel_at(sp, my_statuses, contacts, entry.get("jid", ""), s_idx)
                    return True

        quoted_msg = ctx.get("quotedMessage") or {}
        if not quoted_msg:
            return False
        # Missing local history is not evidence that a quote was a status.
        # Only rebuild an expired status when its origin explicitly says so.
        # stanzaId reaches here already stripped of its status@broadcast prefix,
        # so the origin is only ever in contextInfo.remoteJid.
        if ctx.get("remoteJid") != "status@broadcast":
            return False
        poster_jid = ctx.get("participant", "") or ""
        msg_type = ""
        for key in ("videoMessage", "imageMessage", "audioMessage", "documentMessage",
                    "extendedTextMessage", "conversation"):
            if key in quoted_msg:
                msg_type = key
                break
        if not msg_type:
            return False
        is_mine = bool(poster_jid) and mw._is_self_jid(poster_jid)
        dummy_status = {
            "key": {
                "id": quoted_id,
                "remoteJid": "status@broadcast",
                "fromMe": is_mine,
                "participant": poster_jid,
            },
            "message": quoted_msg,
            "messageType": msg_type,
            "messageTimestamp": 0,
        }
        if is_mine:
            self._open_my_status_dialog_at([dummy_status], 0)
        else:
            name = (mw._resolve_contact_name({"remoteJid": poster_jid}) or format_number(poster_jid)
                    if poster_jid else mw.i18n.t("unknown_contact"))
            fake_entry = {"name": name, "jid": poster_jid, "statuses": [dummy_status]}
            self._open_status_panel_at(sp, [], [fake_entry], poster_jid, 0)
        return True

    def _open_my_status_dialog_at(self, my_statuses: list, idx: int):
        mw = self.main_window
        sp = getattr(mw, "status_panel", None)
        if sp is not None and sp._video_player.is_playing:
            sp._video_player.stop()
        from status_panel import MyStatusDialog
        dlg = MyStatusDialog(mw, my_statuses)
        if idx:
            dlg._current = idx
            dlg._update_content()
        dlg.ShowModal()
        dlg.Destroy()

    def _open_status_panel_at(self, sp, my_statuses: list, contacts: list, target_jid: str, s_idx: int):
        mw = self.main_window
        mw.on_alt_5(None)
        sp._populate_list(my_statuses, contacts)
        c_idx = next((i for i, e in enumerate(sp._status_contacts) if e.get("jid") == target_jid), None)
        if c_idx is None:
            return
        # _populate_list() now interleaves "--- Recentes/Vistos ---" header
        # rows, so a contact's row in the list is no longer just c_idx + 1
        # — look it up via the same row->contact-index map the list's own
        # selection handlers use.
        row = next(
            (r for r, ci in sp._status_row_contact.items() if ci == c_idx),
            c_idx + 1,
        )
        sp._status_list.Select(row)
        sp._status_list.Focus(row)
        sp._status_list.EnsureVisible(row)
        sp._selected_contact_idx = c_idx
        total = len(sp._status_contacts[c_idx].get("statuses", []))
        sp._current_status_idx = max(0, min(s_idx, total - 1)) if total else 0
        sp._show_current_status()

    def _show_quoted_not_found_error(self):
        wx.MessageBox(
            self.main_window.i18n.t("goto_quoted_error"),
            self.main_window.i18n.t("app_name"),
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

    def _show_message_text_popup(self, msg: dict):
        """Open a read-only dialog showing the full message text (or, for a
        photo/video/document message, its caption)."""
        msg_type = msg.get("messageType", "")
        if msg_type in ("conversation", "extendedTextMessage"):
            text = self._message_text_with_names(msg)
        else:
            text = self._get_message_caption(msg)
        if not text:
            return
        if msg.get(RECOVERED_FROM_QUOTE):
            # Same mark as the row (see _get_message_content): this is where a
            # long recovered text is read in full.
            text = f"{self.main_window.i18n.t('message_recovered_from_quote_label')}\n{text}"

        i18n = self.main_window.i18n

        # Use wx.Frame with parent=None so the window is completely independent:
        # it appears in the taskbar, stays visible when Alt+Tab switches away from
        # WinZapp, and never blocks the main window's input focus.
        dlg = wx.Frame(
            None,
            title=i18n.t("msg_text_title"),
            style=wx.DEFAULT_FRAME_STYLE | wx.FRAME_NO_TASKBAR,
            size=(480, 320),
        )
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        text_ctrl = wx.TextCtrl(
            panel, value=word_wrap(text),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        sizer.Add(text_ctrl, 1, wx.EXPAND | wx.ALL, 8)
        # The wrap above is only for line-by-line reading. Copying hands out
        # the message as written: the same range of the original, with its
        # spaces where the wrap put breaks. Ctrl+C / Ctrl+Insert are consumed
        # here; the native context menu's Copy arrives as EVT_TEXT_COPY.
        # Neither handler calls Skip(), and that is what keeps the native
        # control from copying the wrapped text on top of ours.
        def _copy_original(event=None):
            offsets = selection_offsets(
                text_ctrl.GetRange, text_ctrl.GetStringSelection,
                *text_ctrl.GetSelection(),
            )
            if not offsets:
                return
            copied = original_range(text, *offsets)
            if not copied:
                return
            # pyperclip, like the other text copies: wx.TheClipboard without
            # Flush() loses the text when WinZapp exits.
            try:
                pyperclip.copy(copied)
            except Exception:
                self.main_window.output(i18n.t("msg_copy_error"))

        def _on_text_key(event):
            key = event.GetKeyCode()
            if event.GetModifiers() == wx.MOD_CONTROL and key in (ord("C"), wx.WXK_INSERT):
                _copy_original()
                return
            event.Skip()

        text_ctrl.Bind(wx.EVT_KEY_DOWN, _on_text_key)
        text_ctrl.Bind(wx.EVT_TEXT_COPY, _copy_original)
        close_btn = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("close"))
        sizer.Add(close_btn, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)
        panel.SetSizer(sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(panel, 1, wx.EXPAND)
        dlg.SetSizer(dlg_sizer)
        close_btn.Bind(wx.EVT_BUTTON, lambda e: dlg.Destroy())
        dlg.Bind(wx.EVT_CLOSE, lambda e: dlg.Destroy())
        dlg.Bind(
            wx.EVT_CHAR_HOOK,
            lambda e: dlg.Destroy() if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip(),
        )
        text_ctrl.SetFocus()
        dlg.CentreOnScreen()
        dlg.Show()
