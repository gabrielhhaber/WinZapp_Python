"""MessageRenderingMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import threading
from core.community_comments import comment_count
from core.view_once import VIEW_ONCE_UNAVAILABLE_TYPE
from core.call_log import (
    CALL_LOG_MESSAGE_TYPE,
    LEGACY_CALL_LOG_TYPE,
    call_log_label,
    is_call_log,
)
from core.quote_recovery import RECOVERED_FROM_QUOTE
from core.message_sender_labels import (
    hide_unnamed_sender_numbers_enabled, message_sender_label, quoted_sender_label,
)
from core.utils import (
    parse_bool_flag as _parse_bool_flag,
    append_selected_marker,
    format_number,
    is_message_forwarded,
    is_phone_like,
    is_voice_message,
    link_preview_text,
    looks_like_binary_blob,
    reaction_targets_status,
    video_seconds,
)
from ui.conversation_panel.own_sender import row_lead, should_hide_sender


class MessageRenderingMixin:
    """Turning a message record into its list row text: content, status, sender,
    quotes and participant names.
    """

    @staticmethod
    def _message_mentioned_jids(msg: dict) -> list:
        """The JIDs a text message @mentions.

        mentionedJid may live at the top-level contextInfo (the WPPConnect API
        normalises it there), in message.contextInfo, or inside
        extendedTextMessage.contextInfo.
        """
        msg_obj = msg.get("message") or {}
        ext = msg_obj.get("extendedTextMessage") or {}
        ctx_top = msg.get("contextInfo") or {}
        ctx_msg = msg_obj.get("contextInfo") or {}
        ctx_ext = (ext.get("contextInfo") or {}) if isinstance(ext, dict) else {}
        return (
            ctx_top.get("mentionedJid") or ctx_top.get("mentionedJidList")
            or ctx_msg.get("mentionedJid") or ctx_msg.get("mentionedJidList")
            or ctx_ext.get("mentionedJid") or ctx_ext.get("mentionedJidList")
            or []
        )

    def _message_text_with_names(self, msg: dict) -> str:
        """A text message's body as the list row shows it: @mentions as names.

        What Ctrl+C, Alt+C and the bulk copy hand over. They used to read the
        raw body, so a mention the row read as "@Maria" was copied and shown
        as "@5511999999999" -- or as the @lid digits, which are not even a
        phone number. "" for anything that is not a text message.
        """
        msg_obj = msg.get("message") or {}
        msg_type = msg.get("messageType", "")
        if msg_type == "conversation":
            return msg_obj.get("conversation", "") or ""
        if msg_type == "extendedTextMessage":
            text = (msg_obj.get("extendedTextMessage") or {}).get("text", "") or ""
            mentioned = self._message_mentioned_jids(msg)
            return self._resolve_mentions_in_text(text, mentioned) if mentioned else text
        return ""

    def _resolve_mentions_in_text(self, text: str, mentioned: list) -> str:
        """Replace @{number}/@{lid} placeholders in *text* with display names.

        Shared by both the main message renderer and the quoted-message
        preview renderer, so a quoted message that itself contains a mention
        gets the same @lid → contact-name resolution as a normal message
        instead of showing the raw @<lid digits>.
        """
        for jid in mentioned or []:
            mw_ref = self.main_window
            if mw_ref._is_self_jid(jid):
                name = mw_ref.self_reference_label()
            else:
                name = self._get_participant_name(jid)

            # Check what pattern (LID local part or phone number) is used in the text
            lid_local = jid.rsplit("@", 1)[0]
            _lid_map = getattr(mw_ref, "_lid_to_phone", {})
            phone_jid = _lid_map.get(jid, "") if jid.endswith("@lid") else ""
            phone = phone_jid.split("@")[0] if phone_jid else jid.split("@")[0]

            placeholder = None
            if f"@{lid_local}" in text:
                placeholder = lid_local
            elif phone and f"@{phone}" in text:
                placeholder = phone

            if not placeholder:
                continue

            if name and name != placeholder and name != jid:
                text = text.replace(f"@{placeholder}", f"@{name}", 1)
        return text

    def _get_message_content(self, msg) -> str:
        """
        Return the human-readable text for a message item in the list.
        Field names match the WPPConnect API v2 / Baileys proto definitions.
        """
        msg_type = msg.get("messageType", "conversation")
        msg_obj  = msg.get("message") or {}
        i18n     = self.main_window.i18n

        # A reaction to one of our statuses is a row of its own (see
        # _is_displayable_message): there is no message here for it to
        # decorate, because the status it points at lives in the Status tab.
        if reaction_targets_status(msg):
            emoji = ((msg_obj.get("reactionMessage") or {}).get("text") or "").strip()
            return i18n.t("status_reaction_received").format(emoji=emoji)

        if not isinstance(msg_obj, dict):
            return i18n.t("unsupported_message").format(
                app_name=self.main_window.app_name
            )

        # A view-once message: WhatsApp keeps it on the phone, nothing is on
        # its way here (core/view_once.py).
        if msg_type == VIEW_ONCE_UNAVAILABLE_TYPE:
            return i18n.t("view_once_message")

        # Received but not decrypted by WhatsApp Web yet; the real copy
        # replaces this record when it arrives (MainWindow._fill_stored_placeholder).
        if msg_type == "ciphertext":
            return i18n.t("message_awaiting_decryption")

        # ── Call ─────────────────────────────────────────────────────────────
        if is_call_log(msg):
            return call_log_label(msg, i18n, self._format_duration)

        # Text taken from a reply's quote (core/quote_recovery.py) is the
        # replier's word, not the author's: said so on the row, so a quote a
        # modified client made up is never read as the author's own message.
        # First, not last: a classic list row is cut at 511 characters, and a
        # long quote would push a trailing mark out of it.
        if msg.get(RECOVERED_FROM_QUOTE):
            unmarked = {k: v for k, v in msg.items() if k != RECOVERED_FROM_QUOTE}
            return (f"{i18n.t('message_recovered_from_quote_label')} "
                    f"{self._get_message_content(unmarked)}")

        # ── Text ────────────────────────────────────────────────────────────
        if msg_type == "conversation":
            text = msg_obj.get("conversation", "")
            if looks_like_binary_blob(text):
                # Some senders — the official WhatsApp updates account
                # ("0@s.whatsapp.net") observed live — deliver a message
                # whose "conversation" text field is itself a raw base64
                # image blob rather than real text.
                return i18n.t("unsupported_message").format(
                    app_name=self.main_window.app_name
                )
            return text

        if msg_type == "extendedTextMessage":
            # extendedTextMessage.text holds the body; .description is link preview
            ext  = msg_obj.get("extendedTextMessage") or {}
            text = ext.get("text", "") or ""
            if looks_like_binary_blob(text):
                return i18n.t("unsupported_message").format(
                    app_name=self.main_window.app_name
                )
            # Resolve @mentions: replace @{number} with @{display_name}.
            text = self._resolve_mentions_in_text(text, self._message_mentioned_jids(msg))

            # Link preview (title/description WhatsApp itself generated for
            # the URL — see websocket_client.py's _has_link_preview). Shared
            # with the notification toast through link_preview_text() so the
            # two can't drift; see that function for the ordering and the
            # settings gate.
            return link_preview_text(ext, text, self.main_window)

        # ── Audio ────────────────────────────────────────────────────────────
        if msg_type in ("audioMessage", "audio", "ptt"):
            audio = (msg_obj.get("audioMessage") or {}) if isinstance(msg_obj, dict) else {}
            if not audio and isinstance(msg.get("audioMessage"), dict):
                audio = msg.get("audioMessage") or {}
            dur   = self._format_duration(audio.get("seconds"))
            is_ptt = is_voice_message(msg)
            vm_mode = (self.main_window.settings.get("user_interface", {}) if hasattr(self, "main_window") and self.main_window and hasattr(self.main_window, "settings") else {}).get("voice_message_mode", "voice_message")
            lbl = i18n.t("message_type_voice_message") if (vm_mode == "voice_message" and is_ptt) else i18n.t("message_type_audio")
            if not dur:
                # Unknown duration (e.g. a non-.wav file sent via the
                # attachment picker — see _probe_audio_duration()): omit the
                # clause entirely rather than read "duração: " with nothing
                # after the colon.
                return lbl
            return f"{lbl}, {i18n.t('duration')}: {dur}"

        # ── Document ─────────────────────────────────────────────────────────
        if msg_type == "documentMessage":
            doc      = msg_obj.get("documentMessage") or {}
            filename = doc.get("fileName") or doc.get("title") or i18n.t("document")
            size_str = self._format_filesize(doc.get("fileLength"))
            msg_id   = msg.get("key", {}).get("id", "")
            progress = self._download_progress.get(msg_id)
            if progress is not None and progress < 1.0:
                prog_str = self._download_progress_text(
                    progress, doc.get("fileLength"))
                return f"{i18n.t('document')}, {filename}, {prog_str}"
            parts = [i18n.t("document"), filename]
            if size_str:
                parts.append(size_str)
            caption = (doc.get("caption") or "").strip()
            if caption:
                parts.append(caption)
            return ", ".join(parts)

        # ── Image ────────────────────────────────────────────────────────────
        if msg_type == "imageMessage":
            img     = msg_obj.get("imageMessage") or {}
            caption = (img.get("caption") or "").strip()
            if caption:
                return f"{i18n.t('photo')}, {caption}"
            return i18n.t("photo_no_caption")

        # ── Sticker ──────────────────────────────────────────────────────────
        if msg_type == "stickerMessage":
            return i18n.t("sticker")

        # ── Video / GIF ──────────────────────────────────────────────────────
        if msg_type == "videoMessage":
            video = msg_obj.get("videoMessage") or {}
            if video.get("gifPlayback"):
                # Animated GIF — treat identically to sticker
                return i18n.t("sticker")
            dur = self._format_duration(video_seconds(video))
            # Same as the audio branch: an unknown length omits the clause
            # instead of reading "duração: " with nothing after the colon.
            # For video, "unknown" includes a stated 0 — see video_seconds().
            base = f"{i18n.t('video')}, {i18n.t('duration')}: {dur}" if dur else i18n.t("video")
            caption = (video.get("caption") or "").strip()
            return f"{base}, {caption}" if caption else base

        # ── Interactive buttons ───────────────────────────────────────────────
        if msg_type == "buttonsMessage":
            btns_msg = msg_obj.get("buttonsMessage") or {}
            # contentText = message body; text = header when headerType=TEXT
            content  = (btns_msg.get("contentText") or btns_msg.get("text") or "").strip()
            buttons  = btns_msg.get("buttons") or []
            labels   = [
                (b.get("buttonText") or {}).get("displayText", "")
                for b in buttons
                if isinstance(b, dict)
            ]
            opts = ", ".join(l for l in labels if l)
            if opts:
                return f"{content} {i18n.t('options')}: {opts}"
            return content

        # ── List message ─────────────────────────────────────────────────────
        if msg_type == "listMessage":
            list_msg = msg_obj.get("listMessage") or {}
            # title = header; description = body
            title    = (list_msg.get("title") or list_msg.get("description") or "").strip()
            sections = list_msg.get("sections") or []
            all_opts = [
                row.get("title", "")
                for sec in sections if isinstance(sec, dict)
                for row in (sec.get("rows") or []) if isinstance(row, dict)
            ]
            opts = ", ".join(o for o in all_opts if o)
            if opts:
                return f"{title} {i18n.t('options')}: {opts}"
            return title

        # ── Contact ──────────────────────────────────────────────────────────
        if msg_type == "contactMessage":
            return i18n.t("contact_message").format(name=self._contact_display_name(msg))

        if msg_type == "contactsArrayMessage":
            arr = msg_obj.get("contactsArrayMessage") or {}
            contacts = arr.get("contacts") or []
            return i18n.t("contacts_count").format(count=len(contacts))

        # ── Poll ─────────────────────────────────────────────────────────────
        if msg_type in ("pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3", "pollUpdateMessage"):
            poll = msg_obj.get("pollCreationMessage") or msg_obj.get("pollCreationMessageV2") or msg_obj.get("pollCreationMessageV3") or {}
            name = poll.get("name") or ""
            return i18n.t("notif_poll").format(name=name) if name else i18n.t("notif_poll_no_name")

        # ── Location ─────────────────────────────────────────────────────────
        if msg_type in ("locationMessage", "liveLocationMessage"):
            return i18n.t("notif_location")

        # ── Template ─────────────────────────────────────────────────────────
        if msg_type == "templateMessage":
            return i18n.t("notif_template")

        # ── Revoked / Protocol Message ───────────────────────────────────────
        if msg_type == "protocolMessage":
            protocol = msg_obj.get("protocolMessage") or {}
            p_type = protocol.get("type")
            if p_type in (3, "REVOKE", "revoke"):
                return i18n.t("notif_deleted")
            return i18n.t("notif_system_message")

        # ── Interactive / Button reply ───────────────────────────────────────
        if msg_type == "buttonsResponseMessage":
            btn = msg_obj.get("buttonsResponseMessage") or {}
            text = btn.get("selectedDisplayText") or ""
            return text or i18n.t("interactive_reply")

        if msg_type == "listResponseMessage":
            lst = msg_obj.get("listResponseMessage") or {}
            title = lst.get("title", "")
            reply = (lst.get("singleSelectReply") or {}).get("selectedRowId", "")
            return title or reply or i18n.t("list_reply")

        if msg_type == "interactiveMessage":
            inter = msg_obj.get("interactiveMessage") or {}
            body = (inter.get("body") or {}).get("text", "")
            return body or i18n.t("interactive_message")

        # ── Group participant/settings notifications (join, leave, …) ──────────
        if msg_type == "groupNotification":
            notif = msg_obj.get("groupNotification") or {}
            subtype = (notif.get("subtype") or "").lower()

            def _as_jid_str(j) -> str:
                # Normally already a plain string by the time it gets here
                # (see WebSocketClient._normalize_wpp_message's "gp2" branch),
                # but records saved to disk by an older build before that fix
                # may still have a raw WPPConnect Wid dict here — guard so a
                # stale cached message can't crash rendering.
                if isinstance(j, dict):
                    return j.get("_serialized") or j.get("id") or ""
                return j if isinstance(j, str) else ""

            author_jid = _as_jid_str(notif.get("author"))
            recipient_jids = [
                rj for rj in (_as_jid_str(r) for r in (notif.get("recipients") or [])) if rj
            ]

            def _name(j: str) -> str:
                # Our own JID gets the self label ("Eu") rather than a phone
                # number, exactly as in a normal message line. Uses
                # _get_participant_name() rather than _sender_label(): the
                # latter can legitimately return "" when a @lid can't be
                # resolved to a phone number and no contact/chat name is
                # known for it (the exact case a "so-and-so left the group"
                # notification hits for a participant nobody has chatted
                # with directly) — which rendered as a blank name with the
                # rest of the sentence still attached (" saiu do grupo").
                # _get_participant_name() is the resolver already used for
                # group participants elsewhere (reply-privately/converse-with
                # labels) and always falls back to *something* concrete
                # (formatted phone number, or the @lid's own digits) instead
                # of an empty string.
                if self.main_window._is_self_jid(j):
                    return self.main_window.self_reference_label()
                return self._get_participant_name(j, notif) or self.main_window.i18n.t("unknown_contact")

            author_name = _name(author_jid) if author_jid else ""
            names = ", ".join(_name(j) for j in recipient_jids) if recipient_jids else author_name

            if subtype == "invite":
                return i18n.t("group_notif_invited").format(names=names)
            if subtype == "add":
                if author_jid and recipient_jids and author_jid not in recipient_jids:
                    return i18n.t("group_notif_added").format(author=author_name, names=names)
                return i18n.t("group_notif_joined").format(names=names)
            if subtype == "remove":
                return i18n.t("group_notif_removed").format(author=author_name, names=names)
            if subtype == "leave":
                return i18n.t("group_notif_left").format(names=names)
            if subtype in ("promote", "promotion"):
                return i18n.t("group_notif_promoted").format(author=author_name, names=names)
            if subtype in ("demote", "demotion"):
                return i18n.t("group_notif_demoted").format(author=author_name, names=names)
            # WhatsApp sends the new group name / description text in the body
            # of the notification; showing it turns a vague "X alterou o nome do
            # grupo" into something that actually says what changed.
            detail = (notif.get("body") or "").strip()
            if subtype == "subject":
                if detail:
                    return i18n.t("group_notif_subject_changed_to").format(
                        author=author_name, subject=detail)
                return i18n.t("group_notif_subject_changed").format(author=author_name)
            if subtype == "description":
                if detail:
                    return i18n.t("group_notif_description_changed_to").format(
                        author=author_name, description=detail)
                return i18n.t("group_notif_description_changed").format(author=author_name)
            if subtype == "picture":
                return i18n.t("group_notif_picture_changed").format(author=author_name)
            if subtype == "create":
                return i18n.t("group_notif_created").format(author=author_name)
            # Group settings changes. WPPConnect reports the new value in
            # "body"/"value" as "on"/"off" (or true/false) depending on version.
            def _on_off(default=True) -> bool:
                raw = notif.get("value")
                if raw is None:
                    raw = detail
                if isinstance(raw, str):
                    low = raw.strip().lower()
                    if low in ("on", "true", "1", "yes", "announcement", "locked"):
                        return True
                    if low in ("off", "false", "0", "no", "unlocked"):
                        return False
                parsed = _parse_bool_flag(raw)
                return default if parsed is None else parsed

            if subtype in ("announce", "announcement", "restrict_messages"):
                key = "group_notif_announce_on" if _on_off() else "group_notif_announce_off"
                return i18n.t(key).format(author=author_name)
            if subtype in ("restrict", "locked", "settings"):
                key = "group_notif_restrict_on" if _on_off() else "group_notif_restrict_off"
                return i18n.t(key).format(author=author_name)
            if subtype in ("ephemeral", "disappearing_mode"):
                key = "group_notif_ephemeral_on" if _on_off() else "group_notif_ephemeral_off"
                return i18n.t(key).format(author=author_name)
            if subtype in ("revoke_invite", "link_revoke"):
                return i18n.t("group_notif_link_revoked").format(author=author_name)
            if subtype in ("membership_approval_mode", "membership_approval_request"):
                return i18n.t("group_notif_approval_mode").format(author=author_name)
            if subtype in ("sub_group_link", "linked_group", "community_link"):
                # WhatsApp Communities: this group was linked as a sub-group
                # of a community (or unlinked — WPPConnect does not appear to
                # distinguish the two directions on this subtype).
                return i18n.t("group_notif_linked_to_community").format(author=author_name)
            # Unknown subtype: still say who did it and what WhatsApp called it,
            # instead of an anonymous "Atualização do grupo" that tells the user
            # nothing about what actually happened. WhatsApp's raw subtype codes
            # are internal snake_case identifiers never meant for display — a
            # screen reader spelling out the underscores is worse than useless,
            # so turn them into plain words. `detail` (from the notification
            # body) is real WhatsApp-provided text and is left as-is.
            label = detail or (subtype.replace("_", " ") if subtype else "")
            if author_name and label:
                return i18n.t("group_notif_generic_detail").format(
                    author=author_name, detail=label)
            if label:
                return f"{i18n.t('group_notif_generic')}: {label}"
            if author_name:
                return i18n.t("group_notif_generic_author").format(author=author_name)
            return i18n.t("group_notif_generic")

        # ── Fallback ─────────────────────────────────────────────────────────
        # Logged so a future report of a raw, untranslated messageType
        # showing up in the message list (e.g. a view-once/ephemeral audio
        # wrapper, which arrives under a DIFFERENT outer messageType than
        # "audioMessage" itself and isn't unwrapped by any branch above)
        # can be traced to the exact type instead of only reproducing "some
        # message looks wrong".
        logging.info("[_get_message_content] unhandled messageType=%r", msg_type)
        return i18n.t("unsupported_message").format(
            app_name=self.main_window.app_name
        )

    def _is_displayable_message(self, m) -> bool:
        if not isinstance(m, dict):
            return False
        # The user deleted this row while it was still sending; its record only
        # survives as the anchor the WebSocket echo binds to (see
        # _cancel_pending_message()). populate_messages() rebuilds the list
        # straight from the records, so without this the deleted message comes
        # back as "sending" the moment the conversation is reopened — for the
        # whole length of an upload, no race needed. Same rule
        # MainWindow._counts_as_last_message() applies to the chat list.
        if m.get("_cancelled_awaiting_id"):
            return False
        msg_type = m.get("messageType", "")

        # A reaction normally decorates the message it points at and is never a
        # row of its own — which is why reactionMessage is absent from the
        # whitelist below. A reaction to one of OUR statuses is the exception:
        # the status it points at lives in the Status tab, so there is no row
        # here to decorate and the reaction had nowhere to go at all. Reported
        # live as replies to a status that appeared and then were gone the
        # moment the conversation was opened.
        if reaction_targets_status(m):
            return True

        # Whitelist of user-visible/displayable message types
        allowed_types = (
            "conversation",
            "extendedTextMessage",
            "imageMessage",
            "videoMessage",
            "audioMessage",
            "documentMessage",
            "stickerMessage",
            "contactMessage",
            "locationMessage",
            "liveLocationMessage",
            "pollCreationMessage",
            "pollCreationMessageV2",
            "pollCreationMessageV3",
            "pollUpdateMessage",
            "buttonsMessage",
            "listMessage",
            "templateMessage",
            "interactiveMessage",
            "buttonsResponseMessage",
            "listResponseMessage",
            "protocolMessage",
            "groupNotification",
            # WhatsApp Web's "Aguardando mensagem": a message it received but
            # has not decrypted. The live funnel drops the brief ones; what a
            # sync stores is the lasting kind, and hiding it made a reply that
            # quotes it look like WinZapp had lost the original. Never
            # countable nor a chat preview (is_countable_message(),
            # MainWindow._PREVIEW_MESSAGE_TYPES).
            "ciphertext",
            # A view-once message, kept on the phone (core/view_once.py).
            VIEW_ONCE_UNAVAILABLE_TYPE,
            # A voice/video call (core/call_log.py); the legacy type is what
            # builds before it stored, with no call data.
            CALL_LOG_MESSAGE_TYPE,
            LEGACY_CALL_LOG_TYPE,
        )

        if msg_type not in allowed_types:
            return False

        if msg_type == "protocolMessage":
            # Only display if it's a revoke/delete message
            protocol = (m.get("message") or {}).get("protocolMessage") or {}
            p_type = protocol.get("type")
            return p_type in (3, "REVOKE", "revoke")
        if msg_type == "groupNotification":
            # Pure protocol/device-resync housekeeping WhatsApp exchanges
            # between clients to keep a group's participant hash in sync —
            # not something any participant did, and never shown by the
            # official client either. Showing it as "Atualização do grupo:
            # initial phash mismatch" told the user nothing and looked like
            # a bug report leaking into the chat.
            notif = (m.get("message") or {}).get("groupNotification") or {}
            subtype = (notif.get("subtype") or "").lower()
            return subtype not in ("initial_phash_mismatch", "phash_mismatch")
        return True

    def _receipts_are_meaningless(self, chat_jid: "str | None" = None) -> bool:
        """True when the chat being rendered is the "Me" chat, where Sent/
        Delivered/Read/Played are never a real receipt (issue #95): there is
        no second participant to deliver to, read or play anything, so the
        ack WPPConnect still reports there is stale at best and misleading at
        worst. Pending/failed are deliberately not covered — they describe
        whether the send itself worked, not who received it.

        Deliberately keyed on the *chat* the message is being rendered in,
        never on msg["key"]["remoteJid"]: the "Me" chat legitimately holds
        records whose key still carries the raw self-chat artifact JID —
        _redirect_self_chat_artifact() (main.py) files such a message under
        my_jid and deduplicate_chats()'s Pass 0a merges an already-stored
        phantom chat's records into it, but neither rewrites the key. Those
        keys end in "@g.us", for which _is_self_jid() returns False by
        design, so reading the key would leave receipts showing on exactly
        the self-chat messages that needed the artifact machinery.

        *chat_jid* must be passed by any caller rendering a chat other than
        the open conversation (the conversations list's preview line reuses
        this panel for every row — see MainWindow._last_msg_preview()).
        """
        if chat_jid is None:
            conv = getattr(self, "conversation", None)
            chat_jid = conv.get("remoteJid", "") if isinstance(conv, dict) else ""
        return bool(chat_jid) and self.main_window._is_self_jid(chat_jid)

    def _map_status(self, msg, chat_jid: "str | None" = None) -> str:
        i18n = self.main_window.i18n
        # Locally-queued messages have their own pending status.
        if msg.get("_local_pending"):
            return i18n.t("status_pending")
        if msg.get("_send_failed"):
            return i18n.t("status_failed")
        # Send timed out: we never learned whether WhatsApp accepted it, and it
        # is deliberately not retried (retrying an ambiguous send is what used to
        # deliver dozens of duplicates at once). Saying "sent" here would be a
        # guess, and the wrong one often enough to matter.
        if msg.get("_send_unconfirmed"):
            return i18n.t("status_unconfirmed")

        statuses = []
        latest = ""          # newest entry of MessageUpdate — the current verdict
        updates = msg.get("MessageUpdate")
        if isinstance(updates, list) and updates:
            for u in updates:
                if isinstance(u, dict):
                    st = str(u.get("status") or "").upper()
                    statuses.append(st)
                    if st:
                        latest = st

        # Fallback: check status directly on the message (2=sent, 3=delivered, 4=read, 5=played)
        root_status = msg.get("status")
        if root_status is not None:
            statuses.append(str(root_status).upper())
            
        # Fallback: check ack directly on the message (WPPConnect format: 1=sent, 2=delivered, 3=read, 4=played)
        root_ack = msg.get("ack")
        if root_ack is not None:
            status_map = {1: 2, 2: 3, 3: 4, 4: 5}
            mapped_ack = status_map.get(root_ack, root_ack)
            statuses.append(str(mapped_ack).upper())

        from_me = msg.get("key", {}).get("fromMe", False)

        # The "Me" chat has only one participant — there is no one else to
        # deliver to, read or play the message for, so "Enviada"/"Entregue"/
        # "Lida"/"Reproduzida" are never a real receipt there, only a stale/
        # misleading ack WPPConnect still happens to report (issue #95).
        # Pending/failed below are left untouched: they are not receipts,
        # they describe whether the send itself worked.
        is_self_chat = self._receipts_are_meaningless(chat_jid)

        if not is_self_chat:
            for s in statuses:
                if "PLAYED" in s or s == "5":
                    return i18n.t("status_played")

        if not from_me:
            # Received messages only show status if they were played
            return ""

        # A negative status is WhatsApp telling us the send failed (ACK.FAILED
        # and the more specific -2..-7 variants). Only the newest verdict counts:
        # a message can legitimately be acked as sent and *then* reported as
        # failed, and the checks below would otherwise still call it "sent"
        # because they scan for any positive status anywhere in the history.
        if latest.startswith("-") or str(msg.get("status", "")).startswith("-"):
            return i18n.t("status_failed")

        if is_self_chat:
            return ""

        for s in statuses:
            if "READ" in s or s == "4":
                return i18n.t("status_read")
        for s in statuses:
            if "DELIVERED" in s or "DELIVERY_ACK" in s or s == "3":
                return i18n.t("status_delivered")
        for s in statuses:
            if "SENT" in s or "ACK" in s or s == "2":
                return i18n.t("status_sent")
        return ""

    def _classify_status_entry(self, raw) -> str:
        """Classify one raw MessageUpdate status value into a single stage
        name, using the same string/numeric matching _map_status() applies
        to the aggregate status. Returns "" when unrecognised."""
        s = str(raw or "").upper()
        if not s:
            return ""
        if "PLAYED" in s or s == "5":
            return "played"
        if s.startswith("-"):
            return "failed"
        if "READ" in s or s == "4":
            return "read"
        if "DELIVERED" in s or "DELIVERY_ACK" in s or s == "3":
            return "delivered"
        if "SENT" in s or "ACK" in s or s == "2":
            return "sent"
        return ""

    def _status_history_lines(self, msg, chat_jid: "str | None" = None,
                              full_dates: bool = False) -> list:
        """Per-stage delivery/read/played timeline for a sent message, one
        line per stage actually reached ("Enviada: 14:29", "Entregue: 14:30",
        "Lida: 14:32", …), mirroring the official WhatsApp message-info
        screen. Only stages carrying a real timestamp (recorded from live
        messages.update events onward, see MainWindow.on_message_status_update)
        are shown — messages whose status was only ever seen as a single
        aggregate value (e.g. loaded from history sync) fall back to the
        caller's plain "Status: X" line instead, since no per-stage time
        exists for them.

        full_dates=True writes every stage with its full date and time (the
        message-data window); the default keeps the short "today" form."""
        i18n = self.main_window.i18n
        fmt = self._format_full_datetime if full_dates else self._format_date
        from_me = msg.get("key", {}).get("fromMe", False)
        updates = msg.get("MessageUpdate")
        if not isinstance(updates, list):
            return []
        # Same "Me" chat exception as _map_status(): sent/delivered/read/
        # played are never a real receipt when the only participant is
        # yourself, so only a genuine failure (below) can appear there.
        # Checked *before* the not-from_me case, never after: a self-chat
        # record can legitimately carry fromMe=False (the artifact shapes
        # _redirect_self_chat_artifact() handles arrive that way, and
        # on_new_message() only corrects its own local variable, never
        # msg["key"]["fromMe"]), and mark_audio_message_played() records a
        # timestamped "played" for exactly those messages — so ordering this
        # the other way round left the message-data dialog printing
        # "Reproduzida: 14:31" for a message whose row _map_status() had
        # already, correctly, blanked.
        if self._receipts_are_meaningless(chat_jid):
            stage_order = []
        elif not from_me:
            stage_order = ["played"]
        else:
            stage_order = ["sent", "delivered", "read", "played"]
        label_keys = {
            "sent": "status_sent", "delivered": "status_delivered",
            "read": "status_read", "played": "status_played",
        }
        first_ts = {}
        failed_ts = None
        for u in updates:
            if not isinstance(u, dict):
                continue
            ts = u.get("ts")
            if ts is None:
                continue
            stage = self._classify_status_entry(u.get("status"))
            if stage == "failed":
                if from_me:
                    failed_ts = ts
                continue
            if stage in stage_order:
                first_ts.setdefault(stage, ts)
        lines = []
        for stage in stage_order:
            ts = first_ts.get(stage)
            if ts is not None:
                lines.append(f"{i18n.t(label_keys[stage])}: {fmt(ts)}")
        if failed_ts is not None:
            lines.append(f"{i18n.t('status_failed')}: {fmt(failed_ts)}")
        return lines

    def _saved_contact_name(self, lj: str) -> str:
        """The saved or known display name of a JID, or "" when none is good.

        Tries every JID format the contact may be filed under (@s.whatsapp.net,
        @c.us, @lid), strips Baileys device suffixes, prefers the address-book
        entry over a chat name over a presence-learned push name. It is what
        _sender_label() always did, moved here unchanged so the message-data
        window can name a participant the same way a message row names its sender."""
        mw = self.main_window
        lid_to_phone = getattr(mw, "_lid_to_phone", {})

        def _strip_device(j: str) -> str:
            """Remove Baileys device suffix (':N') from a JID, e.g.
            '5511:5@s.whatsapp.net' → '5511@s.whatsapp.net'."""
            if ":" in j and "@" in j:
                local, domain = j.rsplit("@", 1)
                return f"{local.split(':')[0]}@{domain}"
            return j

        def _contact_name(lj: str) -> str:
            """Return saved contact name for lj, trying all three JID formats
            (@s.whatsapp.net, @c.us, @lid), stripping Baileys device suffixes."""
            lj_clean = _strip_device(lj)
            # Normalise @c.us → @s.whatsapp.net so we always start from the modern format
            if lj_clean.endswith("@c.us"):
                lj_clean = lj_clean[:-5] + "@s.whatsapp.net"
            candidates = [lj_clean]
            if lj_clean != lj:
                candidates.append(lj)  # also try original pre-normalisation form
            if lj_clean.endswith("@lid"):
                phone = lid_to_phone.get(lj_clean, "")
                if phone:
                    # Phone first: it is the address-book entry, and the
                    # parallel @lid record can keep an older WhatsApp name —
                    # a local contact added for this person showed the old
                    # name on every row until restart. Same order as
                    # MainWindow._resolve_contact_name(). contacts may also be
                    # indexed under the @c.us legacy format.
                    candidates[:0] = [phone, phone.rsplit("@", 1)[0] + "@c.us"]
            elif lj_clean.endswith("@s.whatsapp.net"):
                # Also try @c.us — contacts dict may still hold the legacy format
                candidates.append(lj_clean.rsplit("@", 1)[0] + "@c.us")
                # O(1) reverse lookup for @lid equivalent
                lid = getattr(mw, "_phone_to_lid", {}).get(lj_clean, "")
                if lid:
                    candidates.append(lid)

            # mw._is_bad_contact_name() instead of hand-rolling a second,
            # independently-maintained copy of the same "sem nome"/"unknown"
            # placeholder check: this copy only exact-matched "unknown",
            # missing WhatsApp's newer "Unknown User" username-feature
            # placeholder that _is_bad_contact_name() already catches
            # (substring match) — a real, demonstrated way two "is this name
            # any good" checks in this codebase silently disagreed.
            ppm = getattr(mw, "_presence_pushname_map", {})
            # Every contact record before any chat name, as
            # _resolve_contact_name() does: get_chat(phone) falls back through
            # _phone_to_lid, so interleaving them let a stale chat name answer
            # before the @lid contact record was ever read.
            contact_lookup = getattr(mw, "_get_contact_tolerant", None) or mw.contacts.get
            for cjid in candidates:
                c = contact_lookup(cjid)
                if c:
                    n = (c.get("name") or c.get("pushName") or "").strip()
                    if n and not mw._is_bad_contact_name(n):
                        return n
            for cjid in candidates:
                chat_obj = mw.get_chat(cjid)
                if chat_obj:
                    cn = (chat_obj.get("name") or "").strip()
                    if cn and not mw._is_bad_contact_name(cn):
                        return cn
            # Fallback: presence-learned pushName map
            for cjid in candidates:
                pname = (ppm.get(cjid) or "").strip()
                if pname and not mw._is_bad_contact_name(pname):
                    return pname
            return ""

        return _contact_name(lj)

    def _sender_label(self, msg) -> str:
        if msg.get("key", {}).get("fromMe"):
            return self.main_window.self_reference_label()
        key         = msg.get("key", {})
        participant = key.get("participant", "")
        jid         = key.get("remoteJid", "")
        lookup_jid  = participant or jid
        mw = self.main_window
        lid_to_phone = getattr(mw, "_lid_to_phone", {})

        # Don't use the group JID (@g.us) itself as a sender lookup — when
        # key.participant is absent, lookup_jid falls back to the remoteJid of
        # the group, and _contact_name would return the group name for every
        # message, making all messages appear to be from the same sender.
        if lookup_jid and not lookup_jid.endswith("@g.us"):
            n = self._saved_contact_name(lookup_jid)
            if n:
                return n

        # For private chats the contact resolution above may have missed the
        # name when the message JID and chat storage key differ (e.g. @lid vs
        # @s.whatsapp.net).  Use the same resolution chain as the chat list so
        # the sender name stays consistent with what is shown there.
        if not participant:
            conv = self.conversation
            if conv and not conv.get("remoteJid", "").endswith("@g.us"):
                n = (
                    mw._resolve_contact_name(conv)
                    or mw.find_name_through_messages(conv)
                    or conv.get("name", "")
                    or conv.get("pushName", "")
                )
                if n:
                    return n

        push = msg.get("pushName", "")
        if push and not is_phone_like(push):
            return push

        # Last resort: format the phone number
        alt = key.get("remoteJidAlt", "")
        if alt and alt.endswith("@s.whatsapp.net"):
            return format_number(alt)
        phone_jid = participant or jid
        if phone_jid.endswith("@lid"):
            phone_jid = lid_to_phone.get(phone_jid, "")
        # Never use the group JID itself as a display name for a message sender.
        if phone_jid and not phone_jid.endswith("@lid") and not phone_jid.endswith("@g.us"):
            return format_number(phone_jid)
        return ""

    def _get_quoted_preview(self, quoted_msg: dict) -> str:
        """Return a short preview string for the content of a quoted message."""
        i18n = self.main_window.i18n
        if not quoted_msg or not isinstance(quoted_msg, dict):
            return ""
        if "conversation" in quoted_msg:
            # The common case: _slim_quoted_message() stores a slimmed quoted
            # message as plain {"conversation": text, "mentionedJid": [...]}
            # (see core/utils.py) — the flat key mentions live under here, not
            # nested in a contextInfo the slimming step deliberately drops.
            text = quoted_msg.get("conversation") or ""
            mentioned = quoted_msg.get("mentionedJid") or quoted_msg.get("mentionedJidList") or []
            if mentioned:
                text = self._resolve_mentions_in_text(text, mentioned)
            return text
        if "extendedTextMessage" in quoted_msg:
            ext = quoted_msg.get("extendedTextMessage") or {}
            text = ext.get("text") or ""
            # The quoted message may itself contain @mentions; resolve them
            # the same way the main message renderer does, instead of
            # leaving the raw @<lid digits> placeholder in the preview.
            ctx_top = quoted_msg.get("contextInfo") or {}
            ctx_ext = ext.get("contextInfo") or {}
            mentioned = (
                ctx_top.get("mentionedJid") or ctx_top.get("mentionedJidList")
                or ctx_ext.get("mentionedJid") or ctx_ext.get("mentionedJidList")
                or []
            )
            if mentioned:
                text = self._resolve_mentions_in_text(text, mentioned)
            return text

        # Support raw WPPConnect types and body/text keys
        vm_mode = (self.main_window.settings.get("user_interface", {}) if hasattr(self, "main_window") and self.main_window and hasattr(self.main_window, "settings") else {}).get("voice_message_mode", "voice_message")
        use_voice_msg = (vm_mode == "voice_message")
        msg_type_raw = quoted_msg.get("type")
        if msg_type_raw:
            _wpp_type_map = {
                "audio": "message_type_voice_message" if (use_voice_msg and is_voice_message(quoted_msg)) else "message_type_audio",
                "ptt": "message_type_voice_message" if use_voice_msg else "message_type_audio",
                "image": "photo",
                "video": "video",
                "document": "document",
                "sticker": "sticker",
                "contact": "contact_label",
            }
            if msg_type_raw in _wpp_type_map:
                cap = quoted_msg.get("caption") or quoted_msg.get("body") or ""
                # Avoid displaying base64 thumbnails
                if cap and not cap.startswith("data:") and not cap.startswith("/9j/"):
                    label = i18n.t(_wpp_type_map[msg_type_raw])
                    return f"{label[0].upper() + label[1:] if label else ''}: {cap}"
                label = i18n.t(_wpp_type_map[msg_type_raw])
                return label[0].upper() + label[1:] if label else ""

        if "body" in quoted_msg:
            body_val = quoted_msg.get("body") or ""
            if not body_val.startswith("data:") and not body_val.startswith("/9j/"):
                return body_val
        if "text" in quoted_msg:
            return (quoted_msg.get("text") or "")

        # Non-text types: return the localized type label (first letter upper)
        _type_map = [
            ("audioMessage",    "message_type_voice_message" if (use_voice_msg and is_voice_message(quoted_msg)) else "message_type_audio"),
            ("imageMessage",    "photo"),
            ("videoMessage",    "video"),
            ("documentMessage", "document"),
            ("stickerMessage",  "sticker"),
            ("contactMessage",  "contact_label"),
        ]
        for key, i18n_key in _type_map:
            if key in quoted_msg:
                label = i18n.t(i18n_key)
                return label[0].upper() + label[1:] if label else ""
        return ""

    def _get_context_info(self, msg) -> "dict | None":
        """Extract contextInfo from wherever it sits in the message hierarchy.

        WPPConnect API's prepareMessage() merges extendedTextMessage.contextInfo
        into the top-level 'contextInfo' field before erasing the sub-object,
        so we check there first.  For audio/image/video replies the contextInfo
        stays inside the respective sub-message type.
        """
        # Top-level contextInfo (WPPConnect API normalised text replies)
        top_ctx = msg.get("contextInfo")
        if isinstance(top_ctx, dict) and ("quotedMessage" in top_ctx or top_ctx.get("stanzaId")):
            return top_ctx

        msg_obj = msg.get("message") or {}
        if not isinstance(msg_obj, dict):
            return None
        for sub_key in (
            "extendedTextMessage", "audioMessage", "imageMessage",
            "videoMessage", "documentMessage", "stickerMessage",
            "locationMessage", "contactMessage", "buttonsMessage",
            "listMessage",
        ):
            sub = msg_obj.get(sub_key)
            if isinstance(sub, dict):
                ctx = sub.get("contextInfo")
                if isinstance(ctx, dict) and ("quotedMessage" in ctx or ctx.get("stanzaId")):
                    return ctx
        return None

    def _is_message_forwarded(self, msg) -> bool:
        """True when contextInfo.isForwarded is set — a real WhatsApp
        protocol field present on any forwarded message, from anyone, not
        only ones this app itself forwarded (WebSocketClient._normalize_wpp_message
        threads it through from WPPConnect's own Message.isForwarded).
        Deliberately does not reuse _get_context_info(): that helper only
        ever returns contextInfo when it also carries a quote, and a
        forwarded message is very often neither a reply nor a mention.

        Thin wrapper around core.utils.is_message_forwarded() — shared with
        main.py's on_new_message(), which needs the exact same check.
        """
        return is_message_forwarded(msg)

    def _get_quoted_sender(self, ctx: dict, msg: dict) -> str:
        """Resolve the display name of the quoted message sender from contextInfo."""
        mw   = self.main_window
        i18n = mw.i18n

        def _strip_dev(j: str) -> str:
            if ":" in j and "@" in j:
                local, domain = j.rsplit("@", 1)
                return f"{local.split(':')[0]}@{domain}"
            return j

        def _phone_part(j: str) -> str:
            return j.rsplit("@", 1)[0].split(":")[0]

        participant = ctx.get("participant", "")
        conv = self.conversation or {}
        is_group = conv.get("remoteJid", "").endswith("@g.us")

        if not participant:
            # Fast path: use local hint set when building virtual reply message.
            if "_quotedFromMe" in ctx:
                return mw.self_reference_label() if ctx["_quotedFromMe"] else (
                    mw._resolve_contact_name(self.conversation or {})
                    or (self.conversation or {}).get("pushName", "")
                    or ""
                )
            # Baileys leaves participant empty for 1:1 replies — there the
            # quote is unambiguously either "me" or "the other party in this
            # chat", both resolvable from the conversation itself. A GROUP
            # reply with no participant (seen live from the WPPConnect API's
            # own contextInfo normalization — see _get_context_info()'s
            # docstring for the same layer's history of dropping/mangling
            # this data) carries no such guarantee: guessing "the reply must
            # be to me" here is exactly how a reply to a THIRD member of the
            # group rendered live as "respondendo a Eu" — confirmed wrong by
            # "go to quoted message" landing on that third member's message,
            # not the user's own. So a group reply always resolves the
            # quoted message's OWN recorded sender instead of guessing.
            stanza_id = ctx.get("stanzaId", "")
            if stanza_id:
                for m in self._sorted_messages:
                    if m.get("key", {}).get("id") == stanza_id:
                        if m.get("key", {}).get("fromMe", False):
                            return mw.self_reference_label()
                        if is_group:
                            m_participant = m.get("key", {}).get("participant") or m.get("participant") or ""
                            if m_participant:
                                return self._get_participant_name(m_participant, m)
                            break  # no sender on record either — fall through to "unknown"
                        # 1:1: not fromMe → the other party in the conversation
                        remote = conv.get("remoteJid", "")
                        return (
                            mw._resolve_contact_name(conv)
                            or conv.get("pushName", "")
                            or (format_number(remote) if remote and not remote.endswith(("@g.us", "@lid")) else "")
                        )
            if is_group:
                # No participant in contextInfo AND the quoted message isn't
                # loaded locally to look its sender up — genuinely unknown,
                # so say so rather than defaulting to "Eu" or the group name.
                return i18n.t("unnamed_participant")
            # 1:1 fallback when the quoted message is not in local _sorted_messages:
            # if I sent this reply, I am replying to the other party.
            # If the other party sent this reply, they are replying to me ("você").
            from_me = msg.get("key", {}).get("fromMe", False)
            if from_me:
                remote = conv.get("remoteJid", "")
                return (
                    mw._resolve_contact_name(conv)
                    or conv.get("pushName", "")
                    or (format_number(remote) if remote and not remote.endswith(("@g.us", "@lid")) else "")
                )
            else:
                return mw.self_reference_label()

        # Strip Baileys device suffix before contact lookup
        clean_p = _strip_dev(participant)

        # Bridge @lid → phone
        if clean_p.endswith("@lid"):
            clean_p = getattr(mw, "_lid_to_phone", {}).get(clean_p, clean_p)

        # Private (1:1) chat fallback: resolve to the other participant or "me"
        # without contact lookup to handle unresolved @lid JIDs and digit-only pushNames.
        conv = self.conversation or {}
        remote = conv.get("remoteJid", "")
        if remote and not remote.endswith("@g.us"):
            p_phone = _phone_part(clean_p)
            
            remote_to_compare = remote
            if remote_to_compare.endswith("@lid"):
                remote_to_compare = getattr(mw, "_lid_to_phone", {}).get(remote_to_compare, remote_to_compare)
            r_phone = _phone_part(remote_to_compare)
            
            my_jid = getattr(mw, "my_jid", "")
            my_phone = _phone_part(my_jid) if my_jid else ""
            if my_phone and p_phone == my_phone:
                return mw.self_reference_label()
            elif p_phone == r_phone:
                return (
                    mw._resolve_contact_name(conv)
                    or conv.get("pushName", "")
                    or (format_number(remote) if not remote.endswith("@lid") else "")
                )

        # Check if the quoted sender is "me" — strip device suffix from both sides
        my_jid = getattr(mw, "my_jid", "")
        if my_jid and _phone_part(clean_p) == _phone_part(my_jid):
            return mw.self_reference_label()

        return self._get_participant_name(clean_p)

    @staticmethod
    def _is_system_event(msg) -> bool:
        """True for messages WhatsApp itself generated, not sent by a person.

        These render as a complete sentence that already contains the name of
        whoever triggered them, so they must not be prefixed with a sender.
        """
        if not isinstance(msg, dict):
            return False
        # A call record is WhatsApp's too: it cannot be replied to, reacted
        # to, forwarded, starred or pinned. Unlike a group notice its sentence
        # does not say who called, so it keeps its sender prefix (see
        # _render_message_line()).
        return msg.get("messageType") == "groupNotification" or is_call_log(msg)

    def _reject_system_event_action(self, msg) -> bool:
        """Announce and refuse a message action that cannot apply to a system event.

        WhatsApp's own group notices (joins/leaves, promotes, name/settings
        changes) are not addressable messages: the server resolves their id
        only sometimes, so forwarding, pinning or reacting to one either
        fails outright or acts on nothing. The purely local actions (star)
        are harmless but equally pointless, and offering them anyway makes a
        screen reader announce a state change that no other client will ever
        show — so everything is refused uniformly.

        The guard lives here, in the _on_menu_* handlers, rather than in the
        context menu: the accelerators (Ctrl+Shift+E/R/O/P, Delete) call
        those handlers directly and would bypass a menu-only gate. That is
        exactly how Ctrl+Shift+S once opened a Save As dialog for a text
        message the menu had already hidden the item for.

        Returns True when the caller must not proceed.
        """
        if not self._is_system_event(msg):
            return False
        key = ("call_log_action_unavailable" if is_call_log(msg)
               else "system_event_action_unavailable")
        self.main_window.output(self.main_window.i18n.t(key))
        return True

    def _render_message_line(self, msg, index: int | None = None, total: int | None = None,
                             include_quoted_preview: bool = True) -> str:
        """Produce the full display string for a single message row.

        include_quoted_preview=False drops the ", mensagem citada: ..." clause.
        Only link detection passes it — see _message_own_links().
        """
        if isinstance(msg, dict) and msg.get("_type") == "empty_placeholder":
            return self.main_window.i18n.t("no_messages_in_conversation")
        # Unread separator sentinel
        if self._is_separator(msg):
            line = self._render_separator(msg.get("count", 1))
            show_count = False
            if hasattr(self, "main_window") and hasattr(self.main_window, "settings"):
                show_count = self.main_window.settings.get("user_interface", {}).get(
                    "show_listbox_item_count", False
                )
            if show_count and getattr(self, "_message_list_mode", "classic") == "listbox":
                if index is None and hasattr(self, "_sorted_messages"):
                    try:
                        index = self._sorted_messages.index(msg)
                    except ValueError:
                        index = None
                if total is None and hasattr(self, "_sorted_messages"):
                    total = len(self._sorted_messages)
                if index is not None and total is not None and total > 0:
                    i18n = self.main_window.i18n
                    line += f", {index + 1} {i18n.t('of')} {total}"
            return line
        ts       = self._extract_timestamp(msg)
        time_str = self._format_date(ts) if ts else ""
        body     = (self._get_message_content(msg) or "")
        sender   = message_sender_label(
            self._sender_label(msg), msg, self.main_window, self.main_window.i18n
        )
        status   = self._map_status(msg)
        i18n     = self.main_window.i18n

        # Check for quoted/reply context
        ctx           = self._get_context_info(msg)
        quoted_sender = self._get_quoted_sender(ctx, msg) if ctx else ""
        if ctx:
            quoted_sender = quoted_sender_label(quoted_sender, ctx, msg, self)

        # A call record is the exception: "Ligação de voz efetuada" alone left
        # the user unsure who had called whom (reported on the test build), so
        # it reads "Eu: Ligação de voz efetuada" like any other message.
        if self._is_system_event(msg) and not is_call_log(msg):
            # System events ("Carlos saiu do grupo", "Ana alterou o nome do
            # grupo") already name whoever acted, inside the sentence. Prefixing
            # them with the sender produced "Carlos: Carlos saiu do grupo",
            # which a screen reader reads out twice.
            pieces = [body]
        else:
            replying_to = i18n.t('replying_to').format(name=quoted_sender) if quoted_sender else ""
            pieces = [row_lead(sender, replying_to, body,
                               should_hide_sender(msg, self.main_window.settings)
                               or (not sender and hide_unnamed_sender_numbers_enabled(
                                   self.main_window.settings)))]
        is_forwarded = not self._is_system_event(msg) and self._is_message_forwarded(msg)
        if msg.get("starred"):
            pieces[0] = f"★ {pieces[0]}"
        if msg.get("pinInChat"):
            pieces[0] = f"📌 {i18n.t('message_pinned')}, {pieces[0]}"
        # Settings > Interface do usuário > "Anunciar 'Encaminhada' no início
        # da mensagem" (default off). Off keeps the long-standing behavior of
        # a trailing ", Encaminhada" clause below, same position as "Editada"
        # and the delivery status. On moves it to the very front — ahead of
        # the sender, and even ahead of the star/pin markers above — so a
        # screen reader user arrowing quickly through a busy forwarded chat
        # (a forward chain, a viral message) hears it's forwarded before
        # anything else, instead of only after the sender and full body.
        if is_forwarded and self.main_window.settings.get("user_interface", {}).get(
            "forwarded_prefix_enabled", False
        ):
            pieces[0] = f"{i18n.t('status_forwarded')}, {pieces[0]}"
            is_forwarded = False  # already announced; don't also append the suffix below
        replies = comment_count(msg.get("replyCount"))
        if replies:
            pieces[0] += f", {i18n.t('community_comments_count').format(count=replies)}"
        if time_str:
            pieces.append(f", {time_str}")
        if status:
            pieces[-1] += f", {status}"
        if msg.get("_edited") and not self._is_system_event(msg):
            pieces[-1] += f", {i18n.t('status_edited')}"
        if is_forwarded:
            pieces[-1] += f", {i18n.t('status_forwarded')}"

        # Append quoted message preview (if this is a reply)
        if ctx and include_quoted_preview:
            quoted_msg_obj = ctx.get("quotedMessage") or {}
            quoted_preview = self._get_quoted_preview(quoted_msg_obj)
            if quoted_preview:
                pieces.append(
                    f", {i18n.t('quoted_message_label')}: {quoted_preview}"
                )

        # Append reactions if any
        msg_id    = msg.get("key", {}).get("id", "")
        reactions = self._reaction_counts(msg_id)
        if reactions:
            r_parts = []
            for emoji, count in reactions.items():
                r_parts.append(f"{emoji}, {count} {i18n.t('total_label')}")
            pieces.append(f". {i18n.t('reactions_label')} {', '.join(r_parts)}.")

        # In listbox mode, native Win32 LISTBOX controls don't announce item position
        # (e.g. "1 de 200") to screen readers. Append position info ONLY when
        # enabled in User Interface settings ("show_listbox_item_count", default False).
        show_count = False
        if hasattr(self, "main_window") and hasattr(self.main_window, "settings"):
            show_count = self.main_window.settings.get("user_interface", {}).get(
                "show_listbox_item_count", False
            )

        if show_count and getattr(self, "_message_list_mode", "classic") == "listbox":
            if index is None and hasattr(self, "_sorted_messages"):
                try:
                    index = self._sorted_messages.index(msg)
                except ValueError:
                    index = None
            if total is None and hasattr(self, "_sorted_messages"):
                total = len(self._sorted_messages)

            if index is not None and total is not None and total > 0:
                pieces.append(f", {index + 1} {i18n.t('of')} {total}")

        local_id = str(msg.get("_local_id") or "")
        if local_id and (msg.get("_local_pending") or msg.get("_awaiting_sent_ack")):
            pct = max(0, min(100, round(
                self._media_upload_progress.get(local_id, 0.0) * 100
            )))
            pieces.append(
                f", {i18n.t('uploading_progress').format(pct=pct)}"
            )

        line = " ".join(pieces)
        is_selected = bool(msg_id) and msg_id in getattr(self, "selected_messages", ())
        position = self.main_window.settings.get("user_interface", {}).get(
            "selected_announcement_position", "end"
        )
        return append_selected_marker(line, i18n.t("selected_suffix"), position, is_selected)

    def _get_participant_name(
        self,
        participant_jid: str,
        msg: dict | None = None,
        *,
        resolve_missing: bool = True,
    ) -> str:
        """Return a display name for a group participant."""
        mw = self.main_window
        if mw._is_self_jid(participant_jid):
            return mw.self_reference_label()
        lid_to_phone = getattr(mw, "_lid_to_phone", {})
        ppm = getattr(mw, "_presence_pushname_map", {})

        # Build candidates covering all three JID formats for the same person.
        # Address-book name (contact["name"]) always takes priority over pushName.
        local = participant_jid.rsplit("@", 1)[0]
        candidates = [participant_jid]
        if participant_jid.endswith("@lid"):
            phone = lid_to_phone.get(participant_jid, "")
            if phone:
                # The phone record is the address-book entry; the parallel
                # @lid record can keep an older WhatsApp name and hid a local
                # contact until restart (same order as _resolve_contact_name).
                candidates[:0] = [phone, phone.rsplit("@", 1)[0] + "@c.us"]
        elif participant_jid.endswith("@s.whatsapp.net"):
            candidates.append(local + "@c.us")
            lid = getattr(mw, "_phone_to_lid", {}).get(participant_jid, "")
            if lid:
                candidates.append(lid)
        elif participant_jid.endswith("@c.us"):
            candidates.append(local + "@s.whatsapp.net")

        # not mw._is_bad_contact_name(x) everywhere below instead of each
        # candidate hand-rolling its own "x.isdigit() or is_phone_like(x)"
        # check: those two conditions alone let through anything else
        # _is_bad_contact_name() also rejects (binary blobs, and — the
        # concrete gap this closes — the literal "Contato sem nome"/
        # "Unknown User"-style placeholders main.py itself assigns to
        # contact["name"] in some code paths, which would otherwise get
        # returned here as if they were a real saved name instead of
        # falling through to the phone-number fallback below).
        # Contact records first, then chat names, and the 8/9-digit tolerant
        # lookup: see _sender_label()'s _contact_name().
        contact_lookup = getattr(mw, "_get_contact_tolerant", None) or mw.contacts.get
        for cjid in candidates:
            contact = contact_lookup(cjid)
            if contact:
                name = (contact.get("name") or contact.get("pushName") or "").strip()
                if name and not mw._is_bad_contact_name(name):
                    return name
        for cjid in candidates:
            chat_obj = mw.get_chat(cjid)
            if chat_obj:
                cn = (chat_obj.get("name") or "").strip()
                if cn and not mw._is_bad_contact_name(cn):
                    return cn
        if msg is not None:
            for key_candidate in ("pushName", "pushname", "name", "displayName"):
                push = msg.get(key_candidate, "")
                if push and not mw._is_bad_contact_name(push):
                    return push
        # Fallback: presence-learned pushName map
        for cjid in candidates:
            pname = (ppm.get(cjid) or "").strip()
            if pname and not mw._is_bad_contact_name(pname):
                return pname
        # Fallback 2: scan sorted messages in the current conversation
        for m in getattr(self, "_sorted_messages", []):
            if not isinstance(m, dict):
                continue
            m_part = m.get("key", {}).get("participant") or m.get("participant")
            if m_part:
                m_part = mw._normalize_jid(m_part)
                if m_part in candidates:
                    push = m.get("pushName", "")
                    if push and not mw._is_bad_contact_name(push):
                        return push
        # Fallback 3: check self._group_participants_cache
        for pname, p_jid in getattr(self, "_group_participants_cache", []):
            if p_jid in candidates:
                if pname and not mw._is_bad_contact_name(pname):
                    return pname
        if not participant_jid.endswith("@lid"):
            return format_number(participant_jid) or participant_jid
        phone = lid_to_phone.get(participant_jid, "")
        if not phone and isinstance(msg, dict):
            pn = msg.get("phoneNumber") or msg.get("pnJid")
            if pn:
                if isinstance(pn, dict):
                    phone = pn.get("_serialized") or pn.get("id") or ""
                else:
                    phone = str(pn)
                if phone:
                    phone = mw._normalize_jid(phone)
                    mw.register_jid_mapping(participant_jid, phone)
        if phone:
            return format_number(phone)
        # No phone mapping for this @lid yet. Unlike a group opened via
        # ConversationDataDialog (which proactively resolves every unmapped
        # participant's @lid before showing the list), a participant
        # mentioned only in a group notification (join/leave/promote/...)
        # may never have gone through that path — e.g. someone who left
        # right after being added, with no other message ever attributed to
        # them. Kick off a background resolution (resolve_lid_jids_via_api
        # makes a synchronous HTTP call and must never run on this — the UI
        # — thread; it already dedupes concurrent/repeat requests for the
        # same JID internally) so a LATER render of this same notification
        # (conversation reopened, history resynced, ...) shows the real
        # formatted phone number instead of the raw LID digits forever.
        if resolve_missing:
            threading.Thread(
                target=mw.resolve_lid_jids_via_api,
                args=([participant_jid],),
                daemon=True,
            ).start()
        # No phone mapping for this @lid — return just the local part (strip "@lid")
        # so the display shows the raw identifier without the domain suffix.
        return participant_jid.rsplit("@", 1)[0]


    def _row_jids(self, msg, participant_by_id=None) -> set:
        """Every JID whose newly-resolved name can change this row's text.

        _render_message_line() resolves a name from five different places, and
        all five have to be collected here or the row silently stops being
        repainted:

        * the sender (_sender_label);
        * the quoted message's sender (_get_quoted_sender), by contextInfo
          participant or, failing that, by stanzaId;
        * every mention in the body and in the quoted preview
          (_resolve_mentions_in_text);
        * a group notification's author and recipients, which
          _get_message_content() runs through _get_participant_name() to build
          the "X added Y" sentence.

        Each is expanded through the @lid <-> phone bridge: the row can carry
        the @lid while the resolution loop reported the phone JID, and
        comparing the raw strings would leave it stale.

        *participant_by_id* maps message id -> sender JID for the rows
        currently loaded. _get_quoted_sender() falls back to the quoted
        message's own recorded sender when contextInfo carries a stanzaId but
        no participant, so without that map such a reply would be missed.
        """
        if not isinstance(msg, dict):
            return set()

        def _as_jid_str(j) -> str:
            # Same tolerance _get_message_content()'s own _as_jid_str() has:
            # records written to disk by an older build can still carry a raw
            # WPPConnect Wid dict here.
            if isinstance(j, dict):
                return j.get("_serialized") or j.get("id") or ""
            return j if isinstance(j, str) else ""

        key = msg.get("key") or {}
        raw = [
            key.get("participant") or "",
            key.get("remoteJid") or "",
            key.get("remoteJidAlt") or "",
            msg.get("participant") or "",
        ]
        # O JID da CONVERSA, e não só os que estão na mensagem: em 1:1
        # _sender_label() e os dois ramos 1:1 de _get_quoted_sender() resolvem
        # o nome a partir de self.conversation quando a linha não tem
        # participant, então uma linha cujo key.remoteJid está sob outra forma
        # (@lid) e ainda sem bridge não cruzaria com o JID de telefone que o
        # laço de resolução reportou — e ficaria anunciando o número cru.
        # Excluído em grupo: lá o nome nunca vem da conversa, e nenhum laço de
        # resolução reporta um @g.us, então incluí-lo não pegaria nada.
        conv_jid = (self.conversation or {}).get("remoteJid", "") or ""
        if conv_jid and not conv_jid.endswith("@g.us"):
            raw.append(conv_jid)
        raw.extend(self._raw_mentioned_jids(msg) or [])
        msg_obj = msg.get("message") or {}
        ext = msg_obj.get("extendedTextMessage") or {} if isinstance(msg_obj, dict) else {}
        # _raw_mentioned_jids() only reads mentionedJid; _get_message_content()
        # also accepts the mentionedJidList spelling, so both are collected.
        for ctx_candidate in (
            msg.get("contextInfo"),
            msg_obj.get("contextInfo") if isinstance(msg_obj, dict) else None,
            ext.get("contextInfo") if isinstance(ext, dict) else None,
        ):
            if isinstance(ctx_candidate, dict):
                raw.extend(ctx_candidate.get("mentionedJidList") or [])
        # A group notification names its author and everyone it acted on, and
        # the recipients live nowhere else in the message: only key.participant
        # happens to mirror the author (WebSocketClient copies it there). A row
        # left out here is the worst case this whole path has — the "X entrou
        # no grupo" line falls back to raw @lid digits, _get_participant_name()
        # itself kicks off the resolution meant to fix that very line, and the
        # scoped repaint it schedules would then skip it.
        notif = msg_obj.get("groupNotification") or {} if isinstance(msg_obj, dict) else {}
        if isinstance(notif, dict) and notif:
            raw.append(_as_jid_str(notif.get("author")))
            raw.extend(_as_jid_str(r) for r in (notif.get("recipients") or []))
        ctx = self._get_context_info(msg)
        if ctx:
            quoted_participant = ctx.get("participant") or ""
            raw.append(quoted_participant)
            if not quoted_participant and participant_by_id:
                raw.append(participant_by_id.get(ctx.get("stanzaId") or "", ""))
            quoted = ctx.get("quotedMessage") or {}
            if isinstance(quoted, dict):
                q_ext = quoted.get("extendedTextMessage") or {}
                for holder in (
                    quoted,
                    quoted.get("contextInfo"),
                    q_ext.get("contextInfo") if isinstance(q_ext, dict) else None,
                ):
                    if isinstance(holder, dict):
                        raw.extend(holder.get("mentionedJid") or [])
                        raw.extend(holder.get("mentionedJidList") or [])
        mw = self.main_window
        forms = set()
        for jid in raw:
            if not isinstance(jid, str) or not jid:
                continue
            normalized = mw._normalize_jid(jid)
            if normalized:
                forms.update(mw._jid_address_forms(normalized) or (normalized,))
        return forms

    def _message_ids_touching_jids(self, jids):
        """Ids of the loaded messages whose rendered text depends on *jids*,
        or None when the selective repaint cannot be trusted.

        None means "repaint everything": a row that matches but carries no
        key.id cannot be addressed by _set_message_row_texts(), and leaving it
        behind is exactly the failure this path must never cause — the row
        keeps announcing raw @lid/phone digits to the screen reader, which is
        worse than the slowness the selective repaint buys back.
        """
        mw = self.main_window
        targets = set()
        for jid in jids or ():
            if not isinstance(jid, str) or not jid:
                continue
            normalized = mw._normalize_jid(jid)
            if normalized:
                targets.update(mw._jid_address_forms(normalized) or (normalized,))
        if not targets:
            return None
        # Built in its own pass so the quoted-sender fallback is a dict lookup
        # rather than a scan of _sorted_messages per reply row.
        participant_by_id = {}
        for m in self._sorted_messages:
            if not isinstance(m, dict) or self._is_separator(m):
                continue
            m_key = m.get("key") or {}
            m_id = m_key.get("id") or ""
            if m_id:
                participant_by_id[m_id] = (
                    m_key.get("participant") or m.get("participant")
                    or m_key.get("remoteJid") or ""
                )
        ids = set()
        for m in self._sorted_messages:
            if not isinstance(m, dict) or self._is_separator(m):
                continue
            if not (self._row_jids(m, participant_by_id) & targets):
                continue
            m_id = (m.get("key") or {}).get("id") or ""
            if not m_id:
                return None
            ids.add(m_id)
        return ids
