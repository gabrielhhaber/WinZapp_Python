"""ContactsMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import threading
import time
import wx
from core.api_client import api_get
from traceback import format_exc
from core.utils import (
    is_phone_like,
    looks_like_binary_blob,
)


class ContactsMixin:
    """Local and remote contacts, self-reference detection and chat list
    computation inputs.
    """

    _UNRESOLVABLE_MAX_AGE_SECONDS = 7 * 24 * 3600

    def _load_local_lid_cache(self):
        try:
            # The DB read stays outside the lock — DatabaseBridge blocks this
            # thread until the coroutine returns, and the Socket.IO thread
            # must never wait on SQLite to record a mapping.
            mappings = self.db.get_lid_mappings()
            with self._lid_mapping_lock:
                self._lid_to_phone = mappings
                self._phone_to_lid = {v: k for k, v in mappings.items()}
            try:
                cutoff = int(time.time()) - self._UNRESOLVABLE_MAX_AGE_SECONDS
                expired = self.db.delete_expired_unresolvable(cutoff)
                if expired:
                    logging.info("[LID Cache] %d expired unresolvable entr%s purged — they will be retried.",
                                 expired, "y" if expired == 1 else "ies")
            except Exception as exc:
                logging.warning("[LID Cache] Failed to purge expired unresolvable entries: %s", exc)
            lids, names = self.db.get_unresolvable_lids()
            self._unresolvable_lids = lids
            self._unresolvable_names = names
            self._status_updates = self.db.get_status_updates()
            logging.info(f"[LID Cache] Loaded {len(self._lid_to_phone)} JID mappings, {len(self._unresolvable_lids)} LIDs, {len(self._unresolvable_names)} names, and status updates for {len(self._status_updates)} participants.")
            return
        except Exception as e:
            logging.error(f"[LID Cache] Error loading JID mappings from database: {e}")
        with self._lid_mapping_lock:
            self._lid_to_phone = {}
            self._phone_to_lid = {}
        self._unresolvable_lids = set()
        self._unresolvable_names = set()

    def get_contacts(self):
        try:
            return self.db.get_contacts()
        except Exception as e:
            self.error_sound.play()
            wx.MessageBox(f"{self.i18n.t('contact_load_failed')} {format_exc()}", self.i18n.t("error").format(app_name=self.app_name), wx.OK | wx.ICON_ERROR)
            return {}

    def save_local_contact(self, jid: str, entry: dict) -> str:
        """Store a local (WinZapp-only) contact and make every view use it now.

        The name used to land only under the phone JID, while the chat and
        its messages are often keyed by the person's @lid, whose parallel
        contact record still carried the WhatsApp name: the message list kept
        showing the old name, even after reopening the conversation, until a
        restart rebuilt the @lid record from the phone one
        (register_jid_mapping()). So the @lid record gets the same entry,
        both are persisted, and the chat list and the open conversation are
        redrawn. Returns the normalized phone JID.
        """
        jid = self._normalize_jid(jid)
        self.contacts[jid] = entry
        changed = {jid: entry}
        lid = self._lid_for_local_contact(jid)
        if lid:
            mirrored = {**(self.contacts.get(lid) or {}), **entry,
                        "id": lid, "remoteJid": lid}
            self.contacts[lid] = mirrored
            changed[lid] = mirrored
        try:
            self.db.upsert_contacts_batch(changed)
        except Exception:
            logging.exception("[save_local_contact] Failed to persist contact")
        self._refresh_views_after_contact_change(jid, lid)
        return jid

    def remove_local_contact(self, jid: str) -> None:
        """Delete a local contact and the @lid copy save_local_contact() made.

        The @lid record is removed only while it still carries the local
        contact's name: if WhatsApp has since written its own name there, that
        name is WhatsApp's, not ours to delete.
        """
        jid = self._normalize_jid(jid)
        removed = self.contacts.pop(jid, None) or {}
        jids = [jid]
        lid = self._lid_for_local_contact(jid)
        name = (removed.get("name") or "").strip()
        lid_record = self.contacts.get(lid) if lid else None
        if lid_record is not None and name and (lid_record.get("name") or "").strip() == name:
            self.contacts.pop(lid, None)
            jids.append(lid)
        elif lid_record is not None and lid_record.pop("isSaved", None) is not None:
            # WhatsApp's name is back on it; it only stops counting as saved.
            try:
                self.db.upsert_contacts_batch({lid: lid_record})
            except Exception:
                logging.exception("[remove_local_contact] Failed to persist contact")
        for contact_jid in jids:
            try:
                self.db.delete_contact(contact_jid)
            except Exception:
                logging.exception("[remove_local_contact] Failed to delete contact")
        self._refresh_views_after_contact_change(jid, lid)

    def _lid_for_local_contact(self, jid: str) -> str:
        """The @lid of the person a typed phone JID names, or "".

        The number is whatever the user typed, and a Brazilian number may carry
        the 9th digit the lid mapping was learned without (or the reverse), so
        an exact lookup is tried first and the digit-equivalent one after it.
        """
        phone_to_lid = getattr(self, "_phone_to_lid", {}) or {}
        lid = phone_to_lid.get(jid, "")
        if lid:
            return lid
        digits = jid.split("@", 1)[0]
        for phone, candidate in list(phone_to_lid.items()):
            if (phone.endswith("@s.whatsapp.net")
                    and self._phone_digits_equivalent(digits, phone.split("@", 1)[0])):
                return candidate
        return ""

    def _refresh_views_after_contact_change(self, jid: str, lid: str = "") -> None:
        """Redraw what shows this person's name: the chat list, and the open
        conversation's rows that touch them — through
        _schedule_refresh_active_messages(), which repaints only those rows,
        inside Freeze()/Thaw(), debounced, and never rebuilds the list."""
        self._schedule_set_chats()
        wx.CallAfter(self._schedule_refresh_active_messages, {j for j in (jid, lid) if j})

    @staticmethod
    def _is_bad_contact_name(name: str) -> bool:
        if not name or not isinstance(name, str):
            return True
        name = name.strip()
        if not name or name.isdigit() or is_phone_like(name) or looks_like_binary_blob(name):
            return True
        val_lower = name.lower()
        # "unknown" as a substring (not just an exact match) so WhatsApp's
        # username-feature placeholder — observed as "Unknown User" — is
        # caught too, not just the older bare "Unknown"/"unknown" contacts
        # used to arrive as before that feature existed.
        return (
            "sem nome" in val_lower
            or "unnamed" in val_lower
            or "unknown" in val_lower
            or val_lower in ("no name", "desconhecido")
        )

    def _clean_contacts_cached(self):
        changed = False
        for jid, contact in list(self.contacts.items()):
            for field in ("name", "pushName"):
                val = contact.get(field)
                if self._is_bad_contact_name(val):
                    if field in contact:
                        del contact[field]
                        changed = True
            if not contact.get("name") and not contact.get("pushName"):
                contact["name"] = ""
        if changed and hasattr(self, "db"):
            self.db.upsert_contacts_batch(self.contacts)

    def get_remote_contacts(self):
        try:
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/all-contacts"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"
            }
            
            response_data = []
            for attempt in range(5):
                try:
                    response = api_get(url, headers=headers, timeout=90)
                    if response.status_code not in (200, 201):
                        logging.error(f"[get_remote_contacts] API error {response.status_code}: {response.text[:200]}")
                        response_data = []
                        if response.status_code >= 500:
                            # Server is overloaded (e.g. syncing a huge contact list). Don't blindly retry 5 times and block startup.
                            break
                    else:
                        try:
                            body = response.json()
                        except Exception as json_err:
                            logging.error(f"[get_remote_contacts] Failed to parse JSON response: {json_err}. Response body: {response.text[:200]}")
                            body = {}
                        response_data = body.get("response", []) if isinstance(body, dict) else []

                    if isinstance(response_data, list) and len(response_data) > 0:
                        break
                    else:
                        logging.info(f"[get_remote_contacts] Got 0 contacts from API, waiting for WPPConnect initialization... (attempt {attempt+1}/5)")
                        import time
                        time.sleep(4)
                except Exception as e:
                    logging.error(f"[get_remote_contacts] Request failed: {e}")
                    import time
                    time.sleep(4)

            if not isinstance(response_data, list):
                response_data = []

            # Traduzir id._serialized para remoteJid e definir type = contact
            for contact in response_data:
                if not isinstance(contact, dict):
                    continue
                wpp_id = contact.get("id")
                jid_str = wpp_id.get("_serialized") if isinstance(wpp_id, dict) else wpp_id
                if jid_str:
                    contact["remoteJid"] = jid_str.replace("@c.us", "@s.whatsapp.net")
                contact["type"] = "contact"
            logging.info(f"[get_remote_contacts] Downloaded {len(response_data)} contacts from WPPConnect API.")
            active_jids = set(self.chats.keys())
            # NOTE: this used to also require c.get("type") == "contact" — but
            # every entry was unconditionally stamped type="contact" a few
            # lines above, *before* this filter ever ran, so that clause could
            # never be false and never actually excluded anything. Removed
            # rather than "fixed" with a guessed replacement value: the
            # WPPConnect all-contacts response's real (pre-stamp) type field
            # isn't documented anywhere in this codebase, and filtering on a
            # wrong guess risks silently dropping legitimate contacts, which
            # is worse than the current (redundant but harmless) no-op. The
            # three checks below already do the real filtering work.
            filtered_contacts = [
                c for c in response_data
                if isinstance(c, dict) and (
                    c.get("isMyContact") is True
                    or c.get("isMe") is True
                    or self._normalize_jid(c.get("remoteJid") or c.get("id", "")) in active_jids
                )
            ]
            names_with_values = [c.get("name") or c.get("pushName") for c in filtered_contacts if c.get("name") or c.get("pushName")]
            logging.info(f"[get_remote_contacts] Total filtered contacts (phonebook): {len(filtered_contacts)} (with valid names: {len(names_with_values)})")
            if filtered_contacts:
                # Shape, not content — the same rule get_remote_chats() already
                # follows (see tests/test_chat_log_has_no_pii.py). This used to
                # log the first contact's raw dict (name, pushName, JID, signed
                # profile-photo URL) AND the actual text of up to 50 contact
                # names, at INFO, every sync — meaning every log.log anyone
                # shared for diagnosis carried a slice of their address book.
                # Key names and value TYPES are what was ever useful here.
                shape = {k: type(v).__name__ for k, v in filtered_contacts[0].items()}
                logging.info(f"[get_remote_contacts] First contact shape: {shape}")
            
            contacts = {}
            for contact in filtered_contacts:
                jid = self._normalize_jid(contact.get("remoteJid") or contact.get("id", ""))
                if jid and not jid.endswith("@g.us") and not jid.endswith("@broadcast"):
                    name = contact.get("name") or contact.get("pushName") or ""
                    if not name or name == "Contato sem nome" or is_phone_like(name):
                        name = ""
                    contact = dict(contact)
                    contact["remoteJid"] = jid
                    contact["name"] = name
                    contact["pushName"] = name
                    
                    if jid not in self.contacts:
                        # Not the name: free text has no pattern the logging
                        # formatter's phone/JID masking can catch, so it needs
                        # to just never be interpolated into a log line.
                        logging.debug(f"[get_remote_contacts] Adding contact: {jid}")
                        self.contacts[jid] = contact
                    else:
                        updated_fields = []
                        for k, v in contact.items():
                            if v is not None and v != "":
                                if self.contacts[jid].get(k) != v:
                                    self.contacts[jid][k] = v
                                    updated_fields.append(k)
                        if updated_fields:
                            logging.debug(f"[get_remote_contacts] Updated fields {updated_fields} for contact: {jid}")
                    contacts[jid] = self.contacts[jid]
            self._schedule_save(contacts_dirty=True)
            return contacts
        except Exception as e:
            self.error_sound.play()
            logging.exception("Exception in get_remote_contacts")
            wx.MessageBox(f"{self.i18n.t('contact_retrieval_failed')} {format_exc()}", self.i18n.t("error").format(app_name=self.app_name), wx.OK | wx.ICON_ERROR, self)

    def start_periodic_contacts_sync(self):
        if hasattr(self, "_contacts_sync_thread_started") and self._contacts_sync_thread_started:
            return
        self._contacts_sync_thread_started = True

        # Chat state (unread badges, pin, archive, mute) is polled far more
        # often than the contact list: WPPConnect Server never relays a
        # "chats-update" socket event for changes made on the phone or another
        # linked device, so this poll is the *only* way those reach WinZapp —
        # at the old 5-minute cadence a conversation could sit there looking
        # read for minutes after the phone said otherwise.  Contacts change
        # rarely and the fetch is heavy, so it stays on the 5-minute schedule.
        _CHAT_POLL_SECONDS    = 60
        _CONTACT_POLL_SECONDS = 300

        def _loop():
            elapsed = 0
            while True:
                time.sleep(_CHAT_POLL_SECONDS)
                elapsed += _CHAT_POLL_SECONDS
                try:
                    if not getattr(self, "_wa_connected", False):
                        continue
                    if getattr(self, "_initial_sync_running", False):
                        # Don't fight the initial sync for the same dict.
                        continue
                    if self._voice_call_in_progress():
                        # The only safe place to stand the message sync down
                        # during a call: nothing has been attempted yet, so
                        # skipping this cycle cannot be mistaken for a round
                        # that succeeded. See sync_chat_messages()' own note.
                        logging.info(
                            "[periodic_contacts_sync] skipped during active voice call"
                        )
                        continue
                    if elapsed >= _CONTACT_POLL_SECONDS:
                        elapsed = 0
                        self.get_remote_contacts()
                        self.get_block_list()
                    baseline = self._capture_chat_sync_baseline()
                    result = self.get_remote_chats(dict(self.chats), persist_full=False,
                                                   notify_errors=False, defer_chat_save=True)
                    if result is not None:
                        self.chats = result
                        full_targets, incremental_targets, skipped, reasons = (
                            self._plan_message_sync(
                                baseline, force_full=False, include_repairs=False
                            )
                        )
                        message_failures = set()
                        if full_targets or incremental_targets:
                            # The reason histogram, same as [start_sync]'s.
                            # This loop runs once a minute for the whole
                            # session, so it is the only place a planner that
                            # has started selecting every chat every round
                            # (one over-eager signal is enough) is visible at
                            # all — the counts alone cannot say whether 40
                            # incremental targets are 40 real changes or one
                            # bad comparison.
                            reason_counts = {}
                            for _reason in reasons.values():
                                reason_counts[_reason] = reason_counts.get(_reason, 0) + 1
                            logging.info(
                                "[periodic_contacts_sync] message delta: %d full/new, "
                                "%d incremental, %d unchanged. reasons=%s",
                                len(full_targets), len(incremental_targets), skipped,
                                reason_counts,
                            )
                            if full_targets:
                                message_failures.update(
                                    self.sync_remote_chats(full_targets, incremental=False) or set()
                                )
                            if incremental_targets:
                                message_failures.update(
                                    self.sync_remote_chats(incremental_targets, incremental=True) or set()
                                )

                            # A missed WebSocket media message recovered by this
                            # safety poll should behave like a live one. Keep the
                            # scan scoped to the chats we just changed and silent
                            # so the 60-second fallback never turns into a global
                            # media rescan or another visible synchronization.
                            changed_jids = {
                                self._normalize_jid(chat.get("remoteJid", ""))
                                for chat in full_targets + incremental_targets
                                if isinstance(chat, dict) and chat.get("remoteJid")
                            }
                            changed_jids.difference_update(message_failures)
                            if (changed_jids
                                    and self.settings.get("storage", {}).get(
                                        "auto_download_media", True)
                                    and not getattr(self, "_media_sync_running", False)
                                    and not getattr(self, "_history_still_landing", False)):
                                self.sync_media_for_all_chats(changed_jids)

                        if not message_failures:
                            # Persist unread/pin/archive/activity metadata only
                            # after every required message delta succeeded. If a
                            # target failed, keeping the old DB marker guarantees
                            # the next process can rediscover the change even if it
                            # dies before the in-memory retry latch is written.
                            self._schedule_save()
                        else:
                            logging.warning(
                                "[periodic_contacts_sync] Deferring chat-list metadata save; "
                                "%d message delta(s) still need retry.",
                                len(message_failures),
                            )
                    wx.CallAfter(self._schedule_set_chats)
                    # Phone-side clears/deletions — active conversation only,
                    # one extra cheap GET per cycle, nothing at all when no
                    # conversation is open. See the method's own docstring
                    # for why this stays scoped to just the open chat.
                    self._reconcile_active_conversation_with_remote()
                except Exception as e:
                    logging.warning(f"[periodic_contacts_sync] error: {e}")
                # Settings > Cópia de segurança, when on. Outside the try above
                # so a failed chat poll never skips it, and inside its own so
                # it can never break the poll.
                try:
                    if getattr(self, "_wa_connected", False):
                        self._maybe_refresh_profile_snapshot_live()
                except Exception as e:
                    logging.warning(f"[periodic_contacts_sync] profile backup check failed: {e}")

        threading.Thread(target=_loop, daemon=True).start()

    @staticmethod
    def _phone_digits_equivalent(a: str, b: str) -> bool:
        """Compare two bare digit strings, tolerating the Brazilian 9th-digit
        variant (55DDD9XXXXXXXX vs 55DDDXXXXXXXX) so a self/contact match
        isn't missed just because one side carries the extra digit.
        """
        if a == b:
            return True
        if a.startswith("55") and b.startswith("55"):
            if len(a) == 13 and len(b) == 12 and a[4] == "9":
                return a[:4] + a[5:] == b
            if len(b) == 13 and len(a) == 12 and b[4] == "9":
                return b[:4] + b[5:] == a
        return False

    def _get_contact_tolerant(self, jid: str) -> "dict | None":
        """Look up ``self.contacts`` by *jid*, tolerating two things a plain
        ``dict.get()`` misses: a Baileys per-device suffix (``:N``) on the
        local part, and the Brazilian mobile 8/9-digit interchangeability
        (``5511999999999`` vs ``551199999999``) — a contact can legitimately
        be saved under either digit count depending on when/how it was added.
        Was reimplemented as an identical local closure in three different
        methods; consolidated here so a future fix to this logic doesn't need
        to be repeated three times (and re-drift, as two of the three already
        had — one was missing the device-suffix strip the others had).
        """
        if not jid:
            return None
        if ":" in jid:
            parts = jid.split("@")
            if len(parts) == 2:
                jid = parts[0].split(":")[0] + "@" + parts[1]
        c = self.contacts.get(jid)
        if c:
            return c
        if jid.endswith("@s.whatsapp.net"):
            phone = jid.split("@")[0]
            if phone.startswith("55"):
                if len(phone) == 13 and phone[4] == "9":
                    # e.g., 5511999999999 -> try 551199999999
                    alt = phone[:4] + phone[5:] + "@s.whatsapp.net"
                    return self.contacts.get(alt)
                elif len(phone) == 12:
                    # e.g., 551199999999 -> try 5511999999999
                    alt = phone[:4] + "9" + phone[4:] + "@s.whatsapp.net"
                    return self.contacts.get(alt)
        return None

    def self_reference_label(self) -> str:
        """Return the word used for the user's own messages/replies in the
        messages list ("Eu"/"Você"/a custom word), per the "Como se referir
        a mim?" setting. Does not affect the self-chat's own name (still
        always self_chat_name, "Eu (mensagens para mim)") — only the sender
        label shown next to your own messages and quoted-reply headers.
        """
        ui = self.settings.get("user_interface", {})
        mode = ui.get("self_reference_mode", "eu")
        if mode == "voce":
            return self.i18n.t("ui_self_reference_voce")
        if mode == "custom":
            word = (ui.get("self_reference_custom_word") or "").strip()
            if word:
                return word
        # "eu" (first-person) mode. Was self.i18n.t("sender_you") — a key
        # meant for "You: ..." message-sender labels elsewhere, whose
        # natural translation is second-person in every language (pt-BR/
        # pt-PT "Eu" and es-ES "Yo" only ever happened to already BE
        # first-person by coincidence; en-US's is "You", making "eu" and
        # "voce" mode produce the exact same word — the setting had no
        # effect at all for English users, and the settings dialog itself
        # showed "You"/"You" as its two options for this same reason.
        return self.i18n.t("ui_self_reference_eu")

    def _is_self_jid(self, jid: str) -> bool:
        """Return True if jid refers to the user's own WhatsApp account.
        Bridges @lid JIDs via cache and strips Baileys device suffixes (':N')
        so self-chats stored under any JID variant are correctly detected.
        """
        if not jid or jid.endswith("@g.us"):
            return False
        my_jid = getattr(self, "my_jid", "")
        if not my_jid:
            return False
        compare = jid
        if jid.endswith("@lid"):
            compare = getattr(self, "_lid_to_phone", {}).get(jid, jid)
        def _phone_part(j: str) -> str:
            return j.rsplit("@", 1)[0].split(":")[0]
        if self._phone_digits_equivalent(_phone_part(compare), _phone_part(my_jid)):
            return True
        my_lid = getattr(self, "my_lid", "")
        if my_lid and _phone_part(compare) == _phone_part(my_lid):
            return True
        return False

    def _is_reply_or_mention_of_me(self, msg: dict, remote_jid: str) -> bool:
        """True when *msg* @-mentions me or replies to one of my own
        messages. Used by on_incoming_message() to let these through a
        muted chat's notification suppression — a reply/mention is
        something the user needs to know about regardless of the mute,
        everywhere (background toast or foreground sound/speech).
        """
        msg_obj = msg.get("message") or {}
        if not isinstance(msg_obj, dict):
            msg_obj = {}

        ctx_candidates = []
        top_ctx = msg.get("contextInfo")
        if isinstance(top_ctx, dict):
            ctx_candidates.append(top_ctx)
        for sub in msg_obj.values():
            if isinstance(sub, dict) and isinstance(sub.get("contextInfo"), dict):
                ctx_candidates.append(sub["contextInfo"])

        for ctx in ctx_candidates:
            mentioned = ctx.get("mentionedJid") or []
            if isinstance(mentioned, list) and any(
                isinstance(j, str) and self._is_self_jid(j) for j in mentioned
            ):
                return True

        for ctx in ctx_candidates:
            if "quotedMessage" not in ctx and not ctx.get("stanzaId"):
                continue
            participant = ctx.get("participant", "")
            if participant:
                if self._is_self_jid(participant):
                    return True
                continue
            # No participant on the quote (typical for 1:1 chats) — resolve
            # via the quoted message's own fromMe flag, if it's still in our
            # local history for this chat.
            stanza_id = ctx.get("stanzaId", "")
            if not stanza_id:
                continue
            chat = self.chats.get(remote_jid) or {}
            container = chat.get("messages")
            records = []
            if isinstance(container, dict):
                inner = container.get("messages")
                if isinstance(inner, dict) and isinstance(inner.get("records"), list):
                    records = inner["records"]
            for m in records:
                if isinstance(m, dict) and m.get("key", {}).get("id") == stanza_id:
                    if m.get("key", {}).get("fromMe", False):
                        return True
                    break
        return False
