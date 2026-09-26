"""MessageActionsMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import json
import logging
import os
import requests
import threading
import time
from core.api_client import api_post
from core.message_edit import (
    connection_refused,
    response_not_sent,
)
from app_paths import data_path


class MessageActionsMixin:
    """Message actions: edit, delete for everyone/me, forward, resend with caption
    and mark-played.
    """

    # ── Message edit / delete-for-everyone ────────────────────────────────────

    def edit_message(self, remote_jid: str, message_id: str, new_text: str,
                     mentioned_jids=None):
        """Send an edited message via POST /api/session/edit-message.

        *mentioned_jids* mirrors send_text_message(): WhatsApp only renders a
        mention when the body carries @<phone> AND the message declares the
        mentioned JIDs, so an edit that adds (or removes) an @mention has to
        restate the list. Without it, editing a message to mention someone
        produced plain text that merely looked like a mention.
        """
        lid_jid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
        if lid_jid:
            remote_jid = lid_jid

        # Find the message key in records (_serialize_msg_id falls back to
        # our own JID as the group participant when it's missing here).
        msg_key = {"id": message_id, "fromMe": True}
        chat = self.chats.get(remote_jid)
        if chat:
            records = chat.get("messages", {}).get("messages", {}).get("records", [])
            for r in records:
                if r.get("key", {}).get("id") == message_id:
                    msg_key = r.get("key", {})
                    break

        full_id = self._serialize_msg_id(remote_jid, msg_key)
        url = (
            f"{self.wpp_server}:{self.wpp_port}"
            f"/api/{self.token}/edit-message"
        )
        payload = {
            "id":      full_id,
            "newText": new_text,
        }
        if mentioned_jids:
            # wa-js's editMessage() takes the same SendMessageOptions as a normal
            # send; `mentionedJidList` is the field it reads (see
            # @wppconnect/wa-js/dist/chat/functions/editMessage.d.ts).
            mentioned = self._canonical_mention_jids(mentioned_jids)
            payload["options"] = {
                "mentionedJidList": [
                    m.replace("@s.whatsapp.net", "@c.us")
                    if m.endswith("@s.whatsapp.net") else m
                    for m in mentioned
                ],
                "linkPreview": False,
            }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        # Three answers, not two. The caller rolls the optimistic edit back only
        # on False, so False must mean "WhatsApp did not take it" — never "we
        # do not know". An edit that did go through reaches us first through
        # onMessageEdit (wa-js fires chat.msg_edited inside
        # addAndSendMessageEdit, before the HTTP response is released), finds
        # the same text already on the row and is consumed; rolling back after
        # that leaves the row wrong with nothing left to re-apply it.
        try:
            r = api_post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return True
            logging.error("[edit_message] HTTP %s for %s: %s",
                          r.status_code, full_id, r.text[:300])
            # Refusals proven to happen before anything is sent: WhatsApp
            # Web's canEditMsg() said no (measured: a 59-minute-old message),
            # or the session was not connected and the middleware answered
            # before any controller ran. Any other error can come after the
            # edit was dispatched — wppconnect compares the stored body with
            # newText once the edit is out, and wa-js trims it — so it stays
            # unknown.
            if "Cannot edit this message" in (r.text or ""):
                return False
            if response_not_sent(r.text):
                return False
            return None
        except requests.exceptions.ConnectTimeout as exc:
            logging.error("[edit_message] could not reach WPPConnect for %s: %s", full_id, exc)
            return False
        except requests.exceptions.ReadTimeout as exc:
            logging.error("[edit_message] timed out for %s (outcome unknown): %s", full_id, exc)
            return None
        except requests.exceptions.ConnectionError as exc:
            # Only a refused connection proves nothing was sent; the same class
            # also carries a connection dropped after the request went out.
            refused = connection_refused(exc)
            logging.error("[edit_message] connection error for %s (%s): %s", full_id,
                          "refused" if refused else "outcome unknown", exc)
            return False if refused else None
        except Exception as exc:
            logging.error("[edit_message] exception for %s (outcome unknown): %s", full_id, exc)
            return None

    def delete_message_for_everyone(self, remote_jid: str, msg_key: dict) -> bool:
        """Revoke a message for everyone via POST /api/session/delete-message.

        Returns True only when the server confirms the revoke. WPP.chat.delete-
        Message resolves the target through getMessageById, which needs the FULL
        serialized id (`<fromMe>_<chatId>_<id>[_<participant>]`) — a hardcoded
        `true_` prefix made it fail to find (and therefore not revoke) messages
        that weren't your own, and revoke only fires when the message is yours or
        you are a group admin.
        """
        lid_jid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
        if lid_jid:
            remote_jid = lid_jid
        url = (
            f"{self.wpp_server}:{self.wpp_port}"
            f"/api/{self.token}/delete-message"
        )
        # WhatsApp chat ids use @c.us, not @s.whatsapp.net. Both the chat id
        # embedded in the serialized message id AND the `phone` field must use
        # the same normalized form, otherwise WPP.chat.deleteMessage cannot
        # resolve the chat and the revoke silently no-ops.
        chat_jid = remote_jid.replace("@s.whatsapp.net", "@c.us")
        full_id = self._serialize_msg_id(chat_jid, msg_key)

        payload = {
            "phone":     chat_jid,
            "isGroup":   chat_jid.endswith("@g.us"),
            "messageId": full_id,
            "onlyLocal": False,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        try:
            r = api_post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return True
            logging.error("[delete_for_everyone] HTTP %s for %s: %s",
                          r.status_code, full_id, r.text[:300])
            return False
        except Exception as exc:
            logging.error("[delete_for_everyone] exception for %s: %s", full_id, exc)
            return False

    def delete_message_for_me(self, remote_jid: str, msg_key: dict) -> bool:
        """Delete a message for the current account only, via the same
        POST /api/session/delete-message endpoint delete_message_for_everyone()
        uses, with onlyLocal=True.
        """
        lid_jid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
        if lid_jid:
            remote_jid = lid_jid
        url = (
            f"{self.wpp_server}:{self.wpp_port}"
            f"/api/{self.token}/delete-message"
        )
        chat_jid = remote_jid.replace("@s.whatsapp.net", "@c.us")
        full_id = self._serialize_msg_id(chat_jid, msg_key)

        payload = {
            "phone":     chat_jid,
            "isGroup":   chat_jid.endswith("@g.us"),
            "messageId": full_id,
            "onlyLocal": True,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        try:
            r = api_post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return True
            logging.error("[delete_for_me] HTTP %s for %s: %s",
                          r.status_code, full_id, r.text[:300])
            return False
        except Exception as exc:
            logging.error("[delete_for_me] exception for %s: %s", full_id, exc)
            return False

    # Media duration the forwarded copy arrives without, waiting to be grafted
    # on. Keyed by (destination chat, kind of media) — see
    # _expect_forwarded_duration() for why not by message id.
    _MAX_FORWARDED_DURATIONS = 64

    @staticmethod
    def media_kind_of(msg: dict):
        """"audioMessage"/"videoMessage" for a message that has a duration."""
        if not isinstance(msg, dict):
            return None
        body = msg.get("message") or {}
        for key in ("audioMessage", "videoMessage"):
            if isinstance(body.get(key), dict):
                return key
        return None

    @staticmethod
    def media_duration_of(msg: dict):
        """Length in seconds of an audio/video message, or None if unknown.

        0 is a real answer, not a missing one: a voice note under a second
        reports 0 and WhatsApp shows "0:00" for it. Only an absent or
        unparseable value is None, so forwarding one of those very short
        notes carries its 0 across instead of leaving the copy blank.
        """
        if not isinstance(msg, dict):
            return None
        body = msg.get("message") or {}
        for key in ("audioMessage", "videoMessage"):
            part = body.get(key)
            if isinstance(part, dict):
                raw = part.get("seconds")
                if raw is None or raw == "":
                    return None
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return None
        return None

    def _expect_forwarded_duration(self, target_jid: str, msg: dict):
        """Record, BEFORE the forward is requested, the length its copy will
        arrive without. Returns a token to hand to _forget_forwarded_duration.

        WhatsApp's forward happens server-side, so the copy shows up in the
        destination chat as a plain live message — and that live payload
        carries no duration (issue #43). The length is right here in the
        source message, so it is put aside now and grafted onto the copy when
        it arrives.

        Registered before the HTTP call, not after it, because the socket
        echo routinely beats the HTTP response back: WhatsApp Web creates
        (and announces) the message before WPPConnect answers our POST.
        Keying this on the id the response reports therefore lost the race
        and the duration never got applied — which is exactly how the first
        attempt at this fix failed in testing.

        That means matching on (chat, kind of media) instead of an id.
        Entries are queued per key and consumed in order, so forwarding two
        audios to the same chat keeps their lengths in the order they were
        sent, and each entry is used at most once.
        """
        seconds = self.media_duration_of(msg)
        kind = self.media_kind_of(msg)
        if seconds is None or not kind or not target_jid:
            return None
        store = getattr(self, "_forwarded_media_seconds", None)
        if store is None:
            store = self._forwarded_media_seconds = {}
        key = (self._normalize_jid(target_jid), kind)
        store.setdefault(key, []).append(seconds)
        # Bounded: a forward whose echo never arrives (send failed, app closed
        # mid-flight) must not pin memory for the rest of the session.
        total = sum(len(q) for q in store.values())
        while total > self._MAX_FORWARDED_DURATIONS:
            oldest = next(iter(store))
            store[oldest].pop(0)
            if not store[oldest]:
                store.pop(oldest)
            total -= 1
        return key

    def _forget_forwarded_duration(self, token):
        """Drop an expectation whose forward turned out to fail."""
        store = getattr(self, "_forwarded_media_seconds", None)
        if not store or token not in store:
            return
        store[token].pop() if store[token] else None
        if not store[token]:
            store.pop(token, None)

    def apply_forwarded_duration(self, msg: dict) -> bool:
        """Fill in a forwarded media message's missing duration. True if applied."""
        store = getattr(self, "_forwarded_media_seconds", None)
        if not store or not isinstance(msg, dict):
            return False
        if not (msg.get("key") or {}).get("fromMe"):
            return False        # only our own forwards are being waited on
        kind = self.media_kind_of(msg)
        part = (msg.get("message") or {}).get(kind) if kind else None
        if not isinstance(part, dict) or part.get("seconds") is not None:
            return False        # nothing to fill, or the echo brought its own
        jid = self._normalize_jid((msg.get("key") or {}).get("remoteJid", ""))
        queue = store.get((jid, kind))
        if not queue:
            return False
        part["seconds"] = queue.pop(0)
        if not queue:
            store.pop((jid, kind), None)
        return True

    def forward_message(self, source_jid: str, msg_key: dict, target_jid: str,
                        source_msg: dict = None) -> bool:
        """Forward a message of any type (text, media, document, …) via
        POST /api/session/forward-messages, which wraps WPP.chat.forwardMessagesV2
        — the real WhatsApp forward, so it carries over media/captions/etc.
        without WinZapp having to re-extract and re-send content itself.
        """
        lid_jid = getattr(self, "_phone_to_lid", {}).get(source_jid, "")
        if lid_jid:
            source_jid = lid_jid
        chat_jid = source_jid.replace("@s.whatsapp.net", "@c.us")
        full_id = self._serialize_msg_id(chat_jid, msg_key)

        target_lid = getattr(self, "_phone_to_lid", {}).get(target_jid, "")
        if target_lid:
            target_jid = target_lid
        target_phone = target_jid.replace("@s.whatsapp.net", "@c.us")

        url = (
            f"{self.wpp_server}:{self.wpp_port}"
            f"/api/{self.token}/forward-messages"
        )
        payload = {
            "phone":     [target_phone],
            "isGroup":   target_phone.endswith("@g.us"),
            "messageId": [full_id],
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        # Put the source's duration aside BEFORE asking for the forward: the
        # copy's socket echo regularly arrives before this POST answers, and
        # it is the echo that gets stored and rendered (see
        # _expect_forwarded_duration).
        duration_token = self._expect_forwarded_duration(target_jid, source_msg)

        # forwardMessagesV2 drives WhatsApp Web's own Puppeteer-side Store the
        # same way a live user forward does. Fired back-to-back for several
        # messages selected at once (mass forward), the very next call can
        # land before the Store has settled from the previous one and the
        # server answers with a transient error even though nothing is
        # actually wrong — reported live as forwarding several messages at
        # once failing where forwarding one at a time never does. One retry
        # after a short pause clears this in practice; a failure that
        # survives the retry is treated as real.
        for attempt in range(2):
            try:
                r = api_post(url, json=payload, headers=headers, timeout=20)
                if r.status_code in (200, 201):
                    logging.info("[forward_message] forwarded %s -> %s (duration expected: %s)%s",
                                 full_id, target_phone, duration_token is not None,
                                 " (after retry)" if attempt else "")
                    return True
                logging.error("[forward_message] HTTP %s for %s -> %s: %s",
                              r.status_code, full_id, target_phone, r.text[:300])
            except Exception as exc:
                logging.error("[forward_message] exception for %s -> %s: %s", full_id, target_phone, exc)
            if attempt == 0:
                time.sleep(0.6)
        self._forget_forwarded_duration(duration_token)
        return False

    def resend_media_message_with_caption(self, msg: dict, target_jid: str) -> bool:
        """Forward a media message by re-sending it as a new file upload,
        in order to preserve its caption which WPPConnect's native forward drops.
        """
        import os
        import json
        from core.utils import decrypt_bytes

        msg_key = msg.get("key", {}) or {}
        msg_id = msg_key.get("id", "")
        if not msg_id:
            return False

        if "_" in msg_id:
            parts = msg_id.split("_")
            msg_id = parts[2] if len(parts) > 2 else parts[-1]

        msg_inner = msg.get("message", {})
        if isinstance(msg_inner, str):
            try:
                msg_inner = json.loads(msg_inner)
            except Exception:
                msg_inner = {}

        original_caption = ""
        media_type = ""
        for key in ["imageMessage", "videoMessage", "documentMessage"]:
            if key in msg_inner and isinstance(msg_inner[key], dict):
                original_caption = msg_inner[key].get("caption", "").strip()
                if key == "imageMessage": media_type = "image"
                elif key == "videoMessage": media_type = "video"
                elif key == "documentMessage": media_type = "document"
                break

        if not media_type:
            return self.forward_message(msg_key.get("remoteJid", ""), msg_key, target_jid)

        media_path = data_path("media", f"{msg_id}.wzmedia")
        if not os.path.isfile(media_path):
            self.output(self.i18n.t("downloading_media"))
            success = self.handle_media_message(msg)
            if not success and not os.path.isfile(media_path):
                logging.error(f"[resend_media] Failed to download media for {msg_id}")
                return False

        try:
            with open(media_path, "rb") as f:
                encrypted_data = f.read()
            decrypted_data = decrypt_bytes(encrypted_data, self.key)

            import mimetypes as _mimetypes
            ext = ".bin"
            custom_filename = ""
            if media_type == "document":
                doc_dict = msg_inner.get("documentMessage", {}) if isinstance(msg_inner.get("documentMessage"), dict) else {}
                fname = (
                    doc_dict.get("fileName") or doc_dict.get("filename") or doc_dict.get("title")
                    or msg.get("fileName") or msg.get("filename") or msg.get("title")
                    or (msg.get("mediaData") if isinstance(msg.get("mediaData"), dict) else {}).get("filename")
                    or (msg.get("mediaData") if isinstance(msg.get("mediaData"), dict) else {}).get("fileName")
                    or ""
                )
                if fname:
                    custom_filename = fname
                    _, ext2 = os.path.splitext(fname)
                    if ext2:
                        ext = ext2
            else:
                mime = msg_inner.get(f"{media_type}Message", {}).get("mimetype", "")
                guessed = _mimetypes.guess_extension(mime.split(";")[0].strip()) if mime else None
                if guessed:
                    ext = guessed
                elif media_type == "image":
                    ext = ".jpg"
                elif media_type == "video":
                    ext = ".mp4"

            temp_path = data_path("media", f"temp_{msg_id}{ext}")
            with open(temp_path, "wb") as f:
                f.write(decrypted_data)

            success = self.send_media_attachment(
                target_jid, temp_path, media_type,
                caption=original_caption,
                custom_filename=custom_filename,
            )

            try:
                os.remove(temp_path)
            except Exception:
                pass

            return success
        except Exception as exc:
            logging.error(f"[resend_media] Error decrypting or sending {msg_id}: {exc}")
            return False

    def mark_audio_message_played(self, msg: dict, skip_panel_refresh: bool = False):
        """Mark a received voice message as played, both locally (the
        status icon in the message list, same as a "played" receipt
        arriving over the WebSocket) and for real, via a played receipt
        sent to WhatsApp so the sender's own client shows it too.

        Called once, right when in-app playback of a received audioMessage
        reaches the end (ConversationsPanel.on_audio_timer(), the same
        moment the playback controls get hidden) — never for a message we
        sent ourselves: "played" only ever legitimately reflects the
        RECIPIENT's own playback, which for our own sends already arrives
        the normal way via on_message_status_update()/messages.update.

        skip_panel_refresh: passed straight through to
        on_message_status_update() — see its docstring. The caller sets this
        whenever this same voice note is about to auto-chain into the next
        one: the "now played" row refresh (which NVDA announces as a change
        on the still-focused finished row) must not land just before the
        chain moves focus onto the next message, so instead of firing it
        here, the caller (ConversationsPanel._auto_chain_next_audio()) fires
        it itself right after that focus move actually happens.
        """
        key = msg.get("key", {}) or {}
        if key.get("fromMe", False):
            return
        msg_id = key.get("id", "")
        remote_jid = key.get("remoteJid", "")
        if not remote_jid and hasattr(self, "conversations_panel"):
            conv = self.conversations_panel.conversation
            remote_jid = conv.get("remoteJid", "") if conv else ""
        if not msg_id or not remote_jid:
            return

        # Configuracoes > Reproducao de audio > "Mudar status dos audios para
        # reproduzidos nas conversas...". Off skips the LOCAL half only: the
        # row is never rewritten, so nothing announces a change on a voice note
        # the user has usually already moved off. The played receipt below
        # still goes to WhatsApp — the sender is entitled to know their message
        # was heard, and that is not what this setting is about.
        if self.settings.get("audio_playback", {}).get(
                "mark_audio_played_in_list", True):
            # Local status icon — reuses the exact same path a real
            # messages.update WebSocket event drives (find the cached record,
            # append MessageUpdate, persist to DB, refresh the visible row).
            self.on_message_status_update({
                "key": {"id": msg_id, "remoteJid": remote_jid},
                "status": "5",
            }, skip_panel_refresh=skip_panel_refresh)
        threading.Thread(
            target=self._send_mark_played_request,
            args=(remote_jid, dict(key)),
            daemon=True,
        ).start()

    def _send_mark_played_request(self, remote_jid: str, msg_key: dict):
        """Background: POST the played receipt to WPPConnect's mark-played
        endpoint (client/api_patches/src/controller/messageController.ts —
        no equivalent route existed anywhere in WPPConnect Server)."""
        full_id = self._serialize_msg_id(remote_jid, msg_key)
        if not full_id:
            return
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/mark-played"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        try:
            r = api_post(url, json={"messageId": full_id}, headers=headers, timeout=15)
            if r.status_code not in (200, 201):
                logging.warning(
                    "[mark_audio_played] HTTP %s for %s: %s",
                    r.status_code, full_id, r.text[:200],
                )
        except Exception as exc:
            logging.warning("[mark_audio_played] request failed for %s: %s", full_id, exc)
