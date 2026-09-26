"""IdentityMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import requests
import threading
import time
import wx
from core.api_client import (
    api_get,
    api_post,
)
from core.utils import (
    format_number,
    is_phone_like,
)


class IdentityMixin:
    """JID identity: normalization, @lid <-> phone bridging, LID mapping caches,
    contact-name resolution and remote profile lookups.
    """

    @staticmethod
    def _normalize_jid(jid: str) -> str:
        """Normalize WhatsApp JID: strip device suffix (e.g. :1, :60) and replace legacy @c.us with @s.whatsapp.net.
        @g.us (groups) and @lid (linked-device IDs) are left unchanged."""
        if not jid:
            return jid
        # Strip companion device suffix if present (e.g. "5511919177719:60@c.us" -> "5511919177719@c.us")
        if ":" in jid and "@" in jid:
            parts = jid.split("@", 1)
            base = parts[0].split(":", 1)[0]
            jid = f"{base}@{parts[1]}"
        if jid.endswith("@c.us"):
            return jid[:-5] + "@s.whatsapp.net"
        return jid

    def _merge_lid_into_phone(self, lid_jid: str, phone_jid: str):
        """Merge a @lid chat entry into the canonical phone (@s.whatsapp.net) entry.

        If only @lid exists, renames it.
        If both exist, copies @lid messages into phone_jid (dedup by ID), then
        removes the @lid entry.
        """
        if lid_jid not in self.chats:
            return
        if phone_jid in self.chats:
            dst_records = (
                self.chats[phone_jid]
                .setdefault("messages", {})
                .setdefault("messages", {})
                .setdefault("records", [])
            )
            src_records = (
                self.chats[lid_jid]
                .get("messages", {})
                .get("messages", {})
                .get("records", [])
            )
            dst_ids = {r.get("key", {}).get("id") for r in dst_records}
            for r in src_records:
                if r.get("key", {}).get("id") not in dst_ids:
                    dst_records.append(r)
        else:
            lid_chat = self.chats.pop(lid_jid)
            lid_chat["remoteJid"] = phone_jid
            self.chats[phone_jid] = lid_chat
        self.chats.pop(lid_jid, None)

        # The in-memory merge above is only half the job: navigate_to_conversation()
        # reloads a conversation's messages straight from the database by its
        # JID, and get_messages()'s _jid_variants() knows @c.us <-> @s.whatsapp.net
        # but nothing about @lid. So any message already filed under lid_jid has
        # to be MOVED to phone_jid, not dropped — this used to call
        # db.delete_chat(lid_jid), which deletes that chat's message rows
        # outright. A first message from a contact whose @lid wasn't cached yet
        # (on_new_message stores it under the raw @lid until /contact/pn-lid
        # answers) was therefore deleted from the database moments after
        # arriving: the chat-list preview still showed it, because the merged
        # in-memory records are what the preview reads, and opening the
        # conversation replaced those records with an empty DB read. Reported
        # as a message from a long-quiet contact that appeared in the list and
        # was gone by the time the conversation opened.
        #
        # merge_or_rename_chat() is the same operation deduplicate_chats()
        # already uses for the sync path: it moves every row it safely can and
        # only deletes an old_jid row once an equivalent survives under new_jid.
        pending_insert = self._pending_lid_inserts.pop(lid_jid, None)

        def _bg_merge_chat(fut=pending_insert):
            # A just-arrived message for this @lid may still be queued for the
            # messages table (on_new_message hands the insert to a pool
            # thread). Renaming the chat before that insert lands would file it
            # under a JID nothing queries again, so wait for it first. Waiting
            # on a dedicated thread rather than inside _msg_bg_executor: a
            # bounded pool whose workers block on other tasks from the same
            # pool can deadlock.
            if fut is not None:
                try:
                    fut.result(timeout=30)
                except Exception as e:
                    logging.warning(
                        "[merge_lid] insert still pending for %s after waiting: %s",
                        lid_jid, e,
                    )
            try:
                self.db.merge_or_rename_chat(lid_jid, phone_jid)
                logging.info("[merge_lid] moved %s -> %s in the database", lid_jid, phone_jid)
            except Exception as e:
                logging.error(f"[merge_lid] Failed to merge LID chat {lid_jid} into {phone_jid}: {e}")
        threading.Thread(target=_bg_merge_chat, daemon=True).start()


        # Redirect active conversation if it was the merged LID chat, or refresh if it is the destination phone chat
        if hasattr(self, "conversations_panel") and self.conversations_panel.conversation:
            active_jid = self.conversations_panel.conversation.get("remoteJid", "")
            if active_jid == lid_jid:
                self.conversations_panel.conversation = self.chats[phone_jid]
                wx.CallAfter(self.conversations_panel.refresh_messages_if_changed)
            elif active_jid == phone_jid:
                wx.CallAfter(self.conversations_panel.refresh_messages_if_changed)

    def _build_lid_to_phone_cache(self):
        """
        Build self._lid_to_phone: a dict mapping @lid JIDs to @s.whatsapp.net
        JIDs by scanning remoteJidAlt fields across all loaded chat messages.

        WPPConnect v2 normalises the key before emitting the WebSocket event:
          OLD format: remoteJid=@lid,          remoteJidAlt=@s.whatsapp.net
          NEW format: remoteJid=@s.whatsapp.net, remoteJidAlt=@lid  (after swap)
        Both formats are handled here so the cache is populated regardless of
        which version of the API produced the stored messages.
        """
        # The scan runs outside the lock: it is proportional to the total
        # message count (unlike _extract_lid_mapping()'s per-message work),
        # so holding the lock across it would stall the Socket.IO thread for
        # seconds on a large account. It therefore builds its own dict and
        # merges it in at the end — see below for why it must not replace.
        cache = {}
        for chat in list(self.chats.values()):
            for msg in list(chat.get("messages", {}).get("messages", {}).get("records", [])):
                key    = msg.get("key", {})
                remote = key.get("remoteJid", "")
                alt    = key.get("remoteJidAlt", "")

                # Normalise @c.us → @s.whatsapp.net so the cache is always keyed
                # under the modern format regardless of which API version wrote
                # the message.
                if alt and alt.endswith("@c.us"):
                    alt = alt[:-5] + "@s.whatsapp.net"
                if remote and remote.endswith("@c.us"):
                    remote = remote[:-5] + "@s.whatsapp.net"

                if alt and alt.endswith("@s.whatsapp.net"):
                    # OLD format: remoteJid=@lid, remoteJidAlt=phone
                    if remote.endswith("@lid"):
                        cache[remote] = alt
                    participant = key.get("participant", "")
                    if participant.endswith("@lid"):
                        cache[participant] = alt

                elif alt and alt.endswith("@lid") and remote.endswith("@s.whatsapp.net"):
                    # NEW format (post-swap): remoteJid=phone, remoteJidAlt=lid
                    cache[alt] = remote

        # Merge, never replace. A pair learned by _extract_lid_mapping() on
        # the Socket.IO thread while the scan above was running exists only
        # in the live dict — assigning the scan result over it would drop it,
        # and since the pair is already saved in SQLite nothing would put it
        # back until the next restart: that chat shows a raw @lid (or
        # "Participante sem nome") for the rest of the session. The reverse
        # map is rebuilt from the live dict, not from `cache`, for the same
        # reason.
        with self._lid_mapping_lock:
            if not hasattr(self, "_lid_to_phone"):
                self._lid_to_phone = {}
            self._lid_to_phone.update(cache)
            self._phone_to_lid  = {v: k for k, v in self._lid_to_phone.items()}

    def _extract_lid_mapping(self, msg):
        """Extract JID mapping from a message object and update cache & persist if new."""
        # WebSocketClient.on_messages_upsert() (core/websocket_client.py) calls
        # this directly on the socket.io callback thread — not via
        # wx.CallAfter like on_new_message()/on_historical_message() — so it
        # is not protected by either of their guards. A reused pairing socket
        # can start delivering messages.upsert events the instant pairing
        # succeeds, before MainWindow.__init__ has finished creating self.db
        # in prepare_sync(), which crashed here via the self.db.set_lid_mapping()/
        # upsert_contacts_batch() calls below (and their save_data() fallback)
        # with "'MainWindow' object has no attribute 'db'".
        #
        # _ui_ready_event is the whole guard this needs, and deliberately not
        # _live_events_ready(): that one additionally waits for a sync to have
        # started, which is meaningless here.  This method touches no chat and
        # no message — only self.db and the _lid_to_phone/_phone_to_lid/
        # _message_pushname_cache dictionaries — so there is no chat-list state
        # for an early event to corrupt or to arrive "out of order" in, and a
        # mapping learned early is a mapping the sync does not have to spend an
        # API round-trip resolving later.  Gating it on sync state is what let
        # a whole session's worth of @lid participants stay unresolved and show
        # up as "Participante sem nome".
        if not self._ui_ready_event.is_set():
            return
        if not isinstance(msg, dict):
            return
        key = msg.get("key")
        if not isinstance(key, dict):
            return
        remote = key.get("remoteJid", "")
        alt = key.get("remoteJidAlt", "")
        participant = key.get("participant", "")

        # Guard against corrupt self-mappings: if any JID is ours, block cross-mapping with others
        if self._is_self_jid(remote) or self._is_self_jid(alt) or self._is_self_jid(participant):
            if alt and (self._is_self_jid(remote) != self._is_self_jid(alt)):
                alt = ""
            if participant and (self._is_self_jid(remote) != self._is_self_jid(participant)):
                participant = ""

        updated = False
        # Pairs actually changed by *this* call — the only ones that need a
        # DB write below. Previously the save step looped over the entire
        # _lid_to_phone cache and wrote every mapping back to SQLite on every
        # single new mapping learned, so an account with hundreds of resolved
        # LIDs did hundreds of synchronous writes on the wx main thread (this
        # runs off on_new_message, via wx.CallAfter) for one new pair.
        updated_pairs = []
        contacts_to_update = {}

        # Everything below that touches _lid_to_phone/_phone_to_lid/
        # _message_pushname_cache/_chats_without_alt_jid is one critical
        # section: this method runs unprotected on the Socket.IO callback
        # thread (see the docstring above) while the wx main thread reaches
        # the same dictionaries through on_new_message()'s own call into this
        # method and through _build_lid_to_phone_cache()'s full rebuilds from
        # the sync thread. Without a lock, two threads racing a
        # check-then-set on the same key can lose one thread's update, and a
        # concurrent discard()/rebuild during another thread's iteration can
        # raise "set/dict changed size during iteration" outright.
        with self._lid_mapping_lock:
            # Invalidate the negative cache since a new message is added to this chat
            if remote and hasattr(self, "_chats_without_alt_jid"):
                self._chats_without_alt_jid.discard(remote)

            # Cache pushName if present in the message
            push_name = msg.get("pushName")
            if push_name and remote and not remote.endswith("@g.us") and not is_phone_like(push_name):
                if not hasattr(self, "_message_pushname_cache"):
                    self._message_pushname_cache = {}
                self._message_pushname_cache[remote] = push_name

            # Initialize dictionary if not present
            if not hasattr(self, "_lid_to_phone"):
                self._lid_to_phone = {}
            if not hasattr(self, "_phone_to_lid"):
                self._phone_to_lid = {}

            if alt and alt.endswith("@s.whatsapp.net"):
                if remote.endswith("@lid") and self._lid_to_phone.get(remote) != alt:
                    self._lid_to_phone[remote] = alt
                    self._phone_to_lid[alt] = remote
                    updated = True
                    updated_pairs.append((remote, alt))
                    logging.info(f"[LID Mapping] Extracted mapping from message key: {remote} <-> {alt}")
            elif alt and alt.endswith("@lid") and remote.endswith("@s.whatsapp.net"):
                if self._lid_to_phone.get(alt) != remote:
                    self._lid_to_phone[alt] = remote
                    self._phone_to_lid[remote] = alt
                    updated = True
                    updated_pairs.append((alt, remote))
                    logging.info(f"[LID Mapping] Extracted mapping from message key (alt): {alt} <-> {remote}")

            # Direct mapping between remote (LID) and participant (phone) for 1:1 chats
            # ONLY if the message is NOT fromMe (if fromMe is True, participant is the user, and remote is the contact!)
            if not key.get("fromMe", False):
                if remote.endswith("@lid") and participant.endswith("@s.whatsapp.net"):
                    if self._lid_to_phone.get(remote) != participant:
                        self._lid_to_phone[remote] = participant
                        self._phone_to_lid[participant] = remote
                        updated = True
                        updated_pairs.append((remote, participant))
                        logging.info(f"[LID Mapping] Extracted mapping from 1:1 chat key: {remote} <-> {participant}")
                elif remote.endswith("@s.whatsapp.net") and participant.endswith("@lid"):
                    if self._lid_to_phone.get(participant) != remote:
                        self._lid_to_phone[participant] = remote
                        self._phone_to_lid[remote] = participant
                        updated = True
                        updated_pairs.append((participant, remote))
                        logging.info(f"[LID Mapping] Extracted mapping from 1:1 chat key (reversed): {participant} <-> {remote}")

            if updated:
                # Propagate contact details from phone contact to LID contact
                # to make it immediately available — still under the lock
                # since this iterates _lid_to_phone itself.
                for lid, phone in list(self._lid_to_phone.items()):
                    if phone in self.contacts and self.contacts[phone]:
                        if lid not in self.contacts or self.contacts[lid].get("name") in (None, "", "Contato sem nome"):
                            self.contacts[lid] = self.contacts[phone].copy()
                            self.contacts[lid]["id"] = lid
                            self.contacts[lid]["remoteJid"] = lid
                            contacts_to_update[lid] = self.contacts[lid]

        if updated:
            # Save only the mapping(s) this call actually changed.
            try:
                for lid, phone in updated_pairs:
                    self.db.set_lid_mapping(lid, phone)
                if contacts_to_update:
                    self.db.upsert_contacts_batch(contacts_to_update)
            except Exception as e:
                logging.error(f"[LID Mapping] Incremental save in _extract_lid_mapping failed: {e}")
                self.save_data(self.chats, self.contacts)

            wx.CallAfter(self._schedule_set_chats)

        # Extract mentions and resolve in background if they are not in mapping/contacts
        msg_obj = msg.get("message") or {}
        ext = msg_obj.get("extendedTextMessage") or {}
        mentioned = (
            (msg.get("contextInfo") or {}).get("mentionedJid")
            or (msg_obj.get("contextInfo") or {}).get("mentionedJid")
            or ext.get("contextInfo", {}).get("mentionedJid")
            or []
        )
        lids_to_resolve = []
        phone_jids_to_resolve = []

        # The sender of a group message needs resolving just as much as anyone
        # it mentions: its @lid rarely comes with a bridge to a phone number,
        # and until it is resolved the participant has no name to show.
        sender_jid = (msg.get("key") or {}).get("participant") or msg.get("participant") or ""
        if not (msg.get("key") or {}).get("fromMe") and self._needs_sender_resolution(sender_jid):
            lids_to_resolve.append(sender_jid)

        if isinstance(mentioned, list):
            for jid in mentioned:
                if not isinstance(jid, str):
                    continue
                if jid.endswith("@lid"):
                    if jid not in getattr(self, "_lid_to_phone", {}):
                        lids_to_resolve.append(jid)
                elif jid.endswith("@s.whatsapp.net") or jid.endswith("@c.us"):
                    normalized = self._normalize_jid(jid)
                    contact = self.contacts.get(normalized)
                    name = ""
                    if contact:
                        name = (contact.get("name") or contact.get("pushName") or "").strip()
                    if not name or name == "Contato sem nome" or is_phone_like(name):
                        phone_jids_to_resolve.append(jid)

        # Outside the `mentioned` branch on purpose: the sender collected above
        # must still be resolved for a message that mentions nobody.
        if lids_to_resolve:
            self._queue_lid_resolutions(lids_to_resolve)

        if phone_jids_to_resolve:
            unique_phone_jids = list(dict.fromkeys(phone_jids_to_resolve))
            with self._contact_resolution_lock:
                claimed_phone_jids = []
                for p_jid in unique_phone_jids:
                    normalized = self._normalize_jid(p_jid)
                    if normalized in self._contact_resolution_inflight:
                        continue
                    self._contact_resolution_inflight.add(normalized)
                    claimed_phone_jids.append(p_jid)
            if not claimed_phone_jids:
                return
            logging.info(
                "[Contact Resolution] Resolving mentioned phone JIDs: %s",
                claimed_phone_jids)
            def resolve_phones_in_bg():
                updated_contacts = {}
                for p_jid in claimed_phone_jids:
                    try:
                        res = self.get_contact_profile(p_jid)
                        if res:
                            res_data = res.get("response", {})
                            if isinstance(res_data, dict):
                                name = res_data.get("name") or res_data.get("pushname") or res_data.get("pushName") or res_data.get("displayName")
                                if name and name != "Contato sem nome" and not is_phone_like(name):
                                    normalized = self._normalize_jid(p_jid)
                                    if normalized not in self.contacts:
                                        self.contacts[normalized] = {}
                                    self.contacts[normalized]["name"] = name
                                    self.contacts[normalized]["pushName"] = name
                                    self._presence_pushname_map[normalized] = name
                                    updated_contacts[normalized] = self.contacts[normalized]
                    except Exception as e:
                        logging.error(f"[Contact Resolution] Error resolving {p_jid}: {e}")
                    finally:
                        normalized = self._normalize_jid(p_jid)
                        with self._contact_resolution_lock:
                            self._contact_resolution_inflight.discard(normalized)
                if updated_contacts:
                    try:
                        self.db.upsert_contacts_batch(updated_contacts)
                    except Exception as e:
                        logging.error(f"[Contact Resolution] Error saving contacts incrementally: {e}")
                        self.save_data(self.chats, self.contacts)
                    wx.CallAfter(self._schedule_set_chats)
                    # Only the contacts this batch actually named can change a
                    # rendered row — every other row already reads the same.
                    wx.CallAfter(self._schedule_refresh_active_messages,
                                 set(updated_contacts))
            threading.Thread(target=resolve_phones_in_bg, daemon=True).start()

    def _queue_lid_resolutions(self, jids) -> None:
        """Deduplicate LID lookups and drain them through one background worker.

        Historical-message normalization calls this once per message. Creating
        a thread for every call produced hundreds of simultaneous Puppeteer
        evaluations, starving get-messages and even the connection heartbeat.
        One serialized worker is enough: names are optional metadata, while
        chat/message synchronization is user-visible and keeps API priority.
        """
        unique = {
            jid for jid in jids
            if isinstance(jid, str) and jid.endswith("@lid")
            and jid not in getattr(self, "_lid_to_phone", {})
        }
        if not unique:
            return
        lock = getattr(self, "_lid_resolution_queue_lock", None)
        if lock is None:
            lock = self._lid_resolution_queue_lock = threading.Lock()
        with lock:
            pending = getattr(self, "_lid_resolution_queue", None)
            if pending is None:
                pending = self._lid_resolution_queue = set()
            pending.update(unique)
            if getattr(self, "_lid_resolution_queue_running", False):
                return
            self._lid_resolution_queue_running = True

        def _drain():
            try:
                while not getattr(self, "_shutting_down", False):
                    # Never let optional names race the initial chat/message
                    # sync. The queue remains in memory and resumes afterward.
                    if (getattr(self, "_initial_sync_running", False)
                            or not getattr(self, "_sync_completed", True)):
                        time.sleep(1)
                        continue
                    with lock:
                        pending = self._lid_resolution_queue
                        if not pending:
                            return
                        batch = []
                        while pending and len(batch) < 20:
                            batch.append(pending.pop())
                    self.resolve_lid_jids_via_api(batch)
            finally:
                with lock:
                    self._lid_resolution_queue_running = False
                    # A producer can append after the empty check but before
                    # the flag is cleared. Restart once to close that race.
                    restart = bool(self._lid_resolution_queue)
                if restart and not getattr(self, "_shutting_down", False):
                    self._queue_lid_resolutions(list(self._lid_resolution_queue))

        threading.Thread(
            target=_drain, daemon=True, name="lid-resolution-queue").start()
        logging.info(
            "[LID Resolution] Queued %d unique LID(s); one serialized worker active.",
            len(unique))

    def scan_all_cached_messages_for_mentions(self):
        """Scan all cached messages in self.chats, find all unresolved LIDs/phones, and resolve them."""
        with self._contact_resolution_lock:
            if getattr(self, "_mentions_scan_running", False):
                return
            self._mentions_scan_running = True

        def _scan():
            # prepare_sync() starts this worker before start_sync() is launched.
            # Do not let optional contact-name lookups compete with list-chats,
            # get-messages and the history worker on the single Puppeteer page.
            while (getattr(self, "_initial_sync_running", False)
                   or not getattr(self, "_sync_completed", True)):
                if getattr(self, "_shutting_down", False):
                    return
                time.sleep(1)
            time.sleep(3)
            logging.info("[Mentions Scan] Starting scan of all cached messages...")
            
            lids_to_resolve = set()
            phones_to_resolve = set()
            # Mappings learned by this scan, so the single end-of-scan refresh
            # below fires even when no contact record happened to change.
            mapped = 0
            # Both sides of each mapping learned above: the open conversation
            # may render the participant under either form, so the selective
            # repaint has to be told about both.
            mapped_jids = set()
            # _learn_sender_names_bulk() writes names for arbitrary participant
            # JIDs and reports none of them, so a scan that learned any is a
            # scan that cannot say which rows changed — it asks for the full
            # repaint. This scan runs once, not at 1 Hz, so that costs nothing;
            # scoping it to mapped_jids instead left a 4000-row group showing
            # formatted numbers for 40 participants until it was reopened.
            learned_names = False
            # Senders are collected separately and capped: a busy account can
            # hold thousands of distinct group participants, and every one of
            # them would otherwise become an API round-trip through the single
            # Puppeteer session at startup, starving sends and media downloads.
            # Most participants never need this anyway — _learn_sender_name()
            # resolves them for free from the pushName on their messages.
            sender_lids = set()
            _MAX_SENDER_LOOKUPS = 150

            # 1. Collect JID mappings and mentions
            chats_snapshot = list(self.chats.values())
            for chat in chats_snapshot:
                records = chat.get("messages", {}).get("messages", {}).get("records", [])
                # Learn every sender name the stored history already carries.
                # This is local and cheap, and it is what keeps the API lookups
                # below down to the handful that genuinely need them.
                if self._learn_sender_names_bulk(records):
                    learned_names = True
                for msg in list(records):
                    if not isinstance(msg, dict):
                        continue
                    # First, see if we can extract immediate JID mappings from key/alt
                    key = msg.get("key") or {}
                    remote = key.get("remoteJid", "")
                    alt = key.get("remoteJidAlt", "")
                    participant = key.get("participant", "")

                    # defer_ui: this walks every message of every chat, so a
                    # refresh per mapping is the same rebuild storm
                    # resolve_lid_jids_via_api() had. One refresh at the end.
                    if alt and alt.endswith("@s.whatsapp.net"):
                        if remote.endswith("@lid") and self._lid_to_phone.get(remote) != alt:
                            self.register_jid_mapping(remote, alt, defer_ui=True)
                            mapped += 1
                            mapped_jids.update((remote, alt))
                    elif alt and alt.endswith("@lid") and remote.endswith("@s.whatsapp.net"):
                        if self._lid_to_phone.get(alt) != remote:
                            self.register_jid_mapping(alt, remote, defer_ui=True)
                            mapped += 1
                            mapped_jids.update((alt, remote))

                    # Sender of a group message. Only mentioned JIDs used to be
                    # collected here, so a participant whose @lid we cannot
                    # bridge — the common case in groups, since group messages
                    # carry no remoteJidAlt — was never sent to the resolver and
                    # stayed "Participante sem nome" for the life of the chat.
                    if self._needs_sender_resolution(participant):
                        sender_lids.add(participant)

                    # Now collect mentions
                    msg_obj = msg.get("message") or {}
                    ext = msg_obj.get("extendedTextMessage") or {}
                    mentioned = (
                        (msg.get("contextInfo") or {}).get("mentionedJid")
                        or (msg_obj.get("contextInfo") or {}).get("mentionedJid")
                        or ext.get("contextInfo", {}).get("mentionedJid")
                        or []
                    )
                    if isinstance(mentioned, list):
                        for jid in mentioned:
                            if not isinstance(jid, str):
                                continue
                            if jid.endswith("@lid"):
                                if jid not in getattr(self, "_lid_to_phone", {}):
                                    lids_to_resolve.add(jid)
                            elif jid.endswith("@s.whatsapp.net") or jid.endswith("@c.us"):
                                normalized = self._normalize_jid(jid)
                                contact = self.contacts.get(normalized)
                                name = ""
                                if contact:
                                    name = (contact.get("name") or contact.get("pushName") or "").strip()
                                if not name or name == "Contato sem nome" or is_phone_like(name):
                                    phones_to_resolve.add(jid)

            # Add the group senders that the pushName pass above could not
            # name, up to the cap, so mentions never lose their slot to them.
            sender_lids = {j for j in sender_lids if self._needs_sender_resolution(j)}
            if sender_lids:
                capped = sorted(sender_lids)[:_MAX_SENDER_LOOKUPS]
                logging.info(
                    "[Mentions Scan] %d group senders still unnamed; queueing %d for resolution.",
                    len(sender_lids), len(capped),
                )
                lids_to_resolve.update(capped)

            # 2. Resolve in controlled batches
            if lids_to_resolve:
                logging.info(f"[Mentions Scan] Found {len(lids_to_resolve)} unresolved LIDs.")
                self.resolve_lid_jids_via_api(list(lids_to_resolve))
                
            # Declared out here, not inside the `if` below: the end-of-scan
            # refresh reads it, and that refresh has to run whether or not there
            # were any mentioned phone JIDs to resolve.
            updated_contacts = {}
            if phones_to_resolve:
                logging.info(f"[Mentions Scan] Found {len(phones_to_resolve)} unresolved mentioned phone JIDs.")
                for p_jid in list(phones_to_resolve):
                    try:
                         res = self.get_contact_profile(p_jid)
                         if res:
                             res_data = res.get("response", {})
                             if isinstance(res_data, dict):
                                 name = res_data.get("name") or res_data.get("pushname") or res_data.get("pushName") or res_data.get("displayName")
                                 if name and name != "Contato sem nome" and not is_phone_like(name):
                                     normalized = self._normalize_jid(p_jid)
                                     if normalized not in self.contacts:
                                         self.contacts[normalized] = {}
                                     self.contacts[normalized]["name"] = name
                                     self.contacts[normalized]["pushName"] = name
                                     if not hasattr(self, "_presence_pushname_map"):
                                         self._presence_pushname_map = {}
                                     self._presence_pushname_map[normalized] = name
                                     updated_contacts[normalized] = self.contacts[normalized]
                         time.sleep(0.1)  # Rate limiting
                    except Exception as e:
                         logging.error(f"[Mentions Scan] Error resolving phone {p_jid}: {e}")
                if updated_contacts:
                     try:
                         self.db.upsert_contacts_batch(updated_contacts)
                     except Exception as e:
                         logging.error(f"[Mentions Scan] Error saving contacts incrementally: {e}")
                         self.save_data(self.chats, self.contacts)
            # Outside `if phones_to_resolve:` — it was written inside it, which
            # made it unreachable in precisely the case `mapped` exists for.
            # The two are fed by unrelated passes: `mapped` counts @lid <-> phone
            # pairs learned from remoteJidAlt while walking the messages, while
            # phones_to_resolve holds *mentioned* phone JIDs that still need a
            # name. A scan that learns mappings, needs no @lid lookup (so
            # resolve_lid_jids_via_api()'s own unconditional refresh never fires)
            # and finds no unnamed mention refreshed nothing at all — with the
            # per-mapping refreshes deferred, the list kept showing raw @lid
            # until something unrelated happened to rebuild it.
            if updated_contacts or mapped or learned_names:
                wx.CallAfter(self._schedule_set_chats)
                # resolve_lid_jids_via_api() above schedules its own repaint
                # for the LIDs it resolved; this one only owns what this scan
                # itself learned — the named mentions, the @lid <-> phone pairs
                # read off remoteJidAlt, and (unscopable) the bulk pushName
                # pass, which forces the full repaint.
                wx.CallAfter(
                    self._schedule_refresh_active_messages,
                    None if learned_names else (set(updated_contacts) | mapped_jids),
                )
            
            logging.info("[Mentions Scan] Scan and resolution of cached messages completed.")

        def _run_once():
            try:
                _scan()
            finally:
                with self._contact_resolution_lock:
                    self._mentions_scan_running = False

        threading.Thread(target=_run_once, daemon=True, name="mentions-scan").start()

    def _find_alt_jid_from_messages(self, chat):
        """
        Find the canonical @s.whatsapp.net phone JID for a chat by scanning its
        message keys.  Handles both WPPConnect v2 key formats and normalises
        any @c.us JIDs encountered to @s.whatsapp.net on the fly:

          OLD: remoteJid=@lid,   remoteJidAlt=@s.whatsapp.net|@c.us → return alt (normalised)
          NEW: remoteJid=phone,  remoteJidAlt=@lid                  → return remoteJid
        Returns the phone JID (@s.whatsapp.net) string, or None if not found.
        """
        jid = chat.get("remoteJid", "")
        if not jid:
            return None

        if not hasattr(self, "_chats_without_alt_jid"):
            self._chats_without_alt_jid = set()

        if jid in self._chats_without_alt_jid:
            return None

        def _norm(j: str) -> str:
            if not j:
                return j
            if j.endswith("@c.us"):
                j = j[:-5] + "@s.whatsapp.net"
            if ":" in j:
                parts = j.split("@")
                if len(parts) == 2:
                    j = parts[0].split(":")[0] + "@" + parts[1]
            return j

        # Copy records list to avoid RuntimeError due to concurrent modifications
        records_copy = list(chat.get("messages", {}).get("messages", {}).get("records", []))
        for msg in records_copy:
            key    = msg.get("key", {})
            remote = _norm(key.get("remoteJid", ""))
            alt    = _norm(key.get("remoteJidAlt", ""))
            # alt is the phone JID, remote is @lid (OLD format)
            if alt and alt.endswith("@s.whatsapp.net"):
                self.register_jid_mapping(jid, alt)
                return alt
            # remote is the phone JID, alt is @lid (NEW post-swap format)
            if remote and remote.endswith("@s.whatsapp.net") and alt and alt.endswith("@lid"):
                self.register_jid_mapping(alt, remote)
                return remote

        self._chats_without_alt_jid.add(jid)
        return None

    def _format_jid_for_display(self, jid: str) -> str:
        """
        Format a JID as a phone number for display, resolving @lid to its mapped
        phone number when known. A raw @lid (an internal 15+ digit identifier)
        must NEVER be shown as a phone number, so when no mapping exists this
        returns "" and the caller falls back to a generic placeholder.
        """
        if not jid:
            return ""
        if jid.endswith("@lid"):
            phone = getattr(self, "_lid_to_phone", {}).get(jid, "")
            return format_number(phone) if phone else ""
        if jid.endswith("@g.us"):
            return ""
        return format_number(jid)

    def _resolve_contact_name(self, chat):
        """
        Return the saved contact name (contact.pushName) for a private chat, or None.

        Tries all three JID formats (@s.whatsapp.net, @c.us, @lid) and returns
        the first valid pushName found.  Groups are skipped (always return None).
        Falls back to the presence-learned pushName map for @lid contacts.
        """
        remoteJid = chat.get("remoteJid", "")
        if not remoteJid or remoteJid.endswith("@g.us"):
            return None  # groups don't have address-book entries

        # "0@s.whatsapp.net" is WhatsApp's own official system/updates
        # account (occasionally messages about new app features) — it has
        # no real contact record, so every fallback below eventually landed
        # on the bare JID local part "0", formatted as "+0". Special-case it
        # to the name WhatsApp's own clients use instead.
        if remoteJid.split("@", 1)[0] == "0":
            return "WhatsApp"

        def _name_from_contact(c):
            # Prefer the address-book name ('name') over the WhatsApp profile
            # name ('pushName'). Both fields may be absent, a bare phone
            # number, binary garbage, or a placeholder like "Contato sem
            # nome" — reject all of those in either case.
            for field in ("name", "pushName"):
                val = c.get(field)
                if val and isinstance(val, str) and not self._is_bad_contact_name(val):
                    return val.strip()
            return None

        ppm = getattr(self, "_presence_pushname_map", {})

        def _try(jid: str) -> str:
            if not jid:
                return ""
            c = self._get_contact_tolerant(jid)
            if c:
                return _name_from_contact(c) or ""
            return ""

        def _ppm(jid: str) -> str:
            val = (ppm.get(jid) or "").strip()
            return val if val and not val.isdigit() and not is_phone_like(val) else ""

        local = remoteJid.rsplit("@", 1)[0]
        resolved = ""
        if remoteJid.endswith("@s.whatsapp.net"):
            resolved = (
                _try(remoteJid)
                or _try(local + "@c.us")
                or _try(getattr(self, "_phone_to_lid", {}).get(remoteJid, ""))
                or _ppm(remoteJid)
            )
        elif remoteJid.endswith("@c.us"):
            phone_net = local + "@s.whatsapp.net"
            resolved = (
                _try(remoteJid)
                or _try(phone_net)
                or _try(getattr(self, "_phone_to_lid", {}).get(phone_net, ""))
                or _ppm(remoteJid)
                or _ppm(phone_net)
            )
        elif remoteJid.endswith("@lid"):
            phone = (
                getattr(self, "_lid_to_phone", {}).get(remoteJid, "")
                or self._find_alt_jid_from_messages(chat)
                or ""
            )
            # A phone-number contact is the canonical address-book entry.
            # The parallel LID record may retain an older WhatsApp/profile name,
            # so consulting it first can hide a freshly renamed phone contact.
            resolved = (
                (phone and (_try(phone) or _try(phone.rsplit("@", 1)[0] + "@c.us")))
                or _try(remoteJid)
                or (phone and _ppm(phone))
                or _ppm(remoteJid)
            )
        else:
            resolved = _try(remoteJid)

        if resolved:
            return resolved

        # Fall back to the chat's own 'name' field
        chat_name = chat.get("name", "")
        if chat_name and isinstance(chat_name, str) and not self._is_bad_contact_name(chat_name):
            return chat_name.strip()

        return None

    def find_name_through_messages(self, chat):
        jid = chat.get("remoteJid", "")
        if not jid or jid.endswith("@g.us"):
            return None

        if not hasattr(self, "_message_pushname_cache"):
            self._message_pushname_cache = {}

        if jid in self._message_pushname_cache:
            return self._message_pushname_cache[jid]

        messages_obj = chat.get("messages") or {}
        for message in messages_obj.get("messages", {}).get("records", []):
            if message.get("key", {}).get("fromMe"):
                continue
            push = message.get("pushName", "")
            if push and not is_phone_like(push):
                self._message_pushname_cache[jid] = push
                return push
        return None

    def find_jid_through_messages(self, chat):
        messages_obj = chat.get("messages") or {}
        for message in messages_obj.get("messages", {}).get("records", []):
            if not message.get("key", {}).get("fromMe"):
                key = message.get("key", {})
                alt = key.get("remoteJidAlt", "")
                if alt and alt.endswith("@s.whatsapp.net"):
                    return format_number(alt)
                jid = key.get("remoteJid", "")
                if jid and not jid.endswith("@lid") and not jid.endswith("@g.us"):
                    return format_number(jid)
        return None

    def preselect_conversations(self):
        #Checks if window is still open
        if self.IsShown():
            lst = self.conversations_panel.conversations_list
            if lst.GetItemCount() > 0:
                # Only preselect if there is no current selection/focus
                if lst.GetFocusedItem() == -1:
                    lst.Focus(0)
                    lst.Select(0)
                    lst.EnsureVisible(0)


    def _resolve_jid_name(
        self,
        jid_norm: str,
        chat_jid_norm: str = "",
        *,
        resolve_missing: bool = True,
    ) -> str:
        """Return the best display name for a participant JID (contact lookup + fallback).

        chat_jid_norm: the group this participant belongs to, when known —
        lets the @lid fallback below check that group's own already-loaded
        messages for a pushName, the same source _get_participant_name()
        (ui/conversations.py, used once the message itself has arrived)
        checks. Without it, a presence event for someone whose @lid isn't in
        _lid_to_phone yet had nothing left to try and fell straight to the
        generic placeholder — reported live as "participante sem nome está
        digitando" for a group member whose name rendered correctly moments
        later once their actual message came in and went through that
        richer resolution path instead of this one.
        """
        ppm = getattr(self, "_presence_pushname_map", {})

        # Build candidate list covering all three JID formats for the same person.
        candidates = [jid_norm]
        local = jid_norm.rsplit("@", 1)[0]
        if jid_norm.endswith("@s.whatsapp.net"):
            candidates.append(local + "@c.us")
            lid = getattr(self, "_phone_to_lid", {}).get(jid_norm, "")
            if lid:
                candidates.append(lid)
        elif jid_norm.endswith("@c.us"):
            candidates.append(local + "@s.whatsapp.net")
        elif jid_norm.endswith("@lid"):
            phone = getattr(self, "_lid_to_phone", {}).get(jid_norm, "")
            if phone:
                candidates.append(phone)
                candidates.append(phone.rsplit("@", 1)[0] + "@c.us")

        for cjid in candidates:
            # A participant JID is never a group JID — some WPPConnect/Baileys
            # presence payloads key a group-wide event by the group's own
            # @g.us id instead of a specific participant (seen after deleting
            # and re-pairing a session). Falling through to self.chats.get()
            # below would then resolve to the GROUP's own chat entry, showing
            # "<group name> está digitando..." instead of a person's name.
            if cjid.endswith("@g.us"):
                continue
            contact = self._get_contact_tolerant(cjid)
            if contact:
                name = (contact.get("name") or contact.get("pushName") or "").strip()
                if name and not name.isdigit():
                    return name
            chat = self.chats.get(cjid)
            if chat:
                name = (chat.get("name") or chat.get("pushName") or "").strip()
                if name and not name.isdigit():
                    return name
        # Fallback: check the presence-learned pushName map
        for cjid in candidates:
            pname = (ppm.get(cjid) or "").strip()
            if pname and not pname.isdigit() and not is_phone_like(pname):
                return pname
        if jid_norm.endswith("@lid"):
            phone = getattr(self, "_lid_to_phone", {}).get(jid_norm, "")
            if phone:
                return format_number(phone)
            # No phone mapping cached yet — before giving up, check whatever
            # this group's own messages already know about this @lid (an
            # earlier message from the same person, still carrying its
            # pushName) and, for the conversation currently open, the same
            # participant cache _get_participant_name() consults.
            if chat_jid_norm:
                records = (
                    self.chats.get(chat_jid_norm, {})
                        .get("messages", {})
                        .get("messages", {})
                        .get("records", [])
                )
                for m in records:
                    if not isinstance(m, dict):
                        continue
                    m_part = m.get("key", {}).get("participant") or m.get("participant")
                    if m_part and self._normalize_jid(m_part) == jid_norm:
                        push = (m.get("pushName") or "").strip()
                        if push and not is_phone_like(push):
                            return push
            cp = getattr(self, "conversations_panel", None)
            if cp is not None and cp.conversation is not None:
                if self._normalize_jid(cp.conversation.get("remoteJid", "")) == chat_jid_norm:
                    for pname, p_jid in getattr(cp, "_group_participants_cache", []):
                        if p_jid == jid_norm and pname and not is_phone_like(pname):
                            return pname
            # Still nothing — kick a background resolution so a later
            # presence event in the same typing burst (or the next time this
            # group is opened) shows the real name instead of the
            # placeholder forever. resolve_lid_jids_via_api dedupes
            # concurrent/repeat requests for the same jid internally.
            if resolve_missing:
                threading.Thread(
                    target=self.resolve_lid_jids_via_api,
                    args=([jid_norm],),
                    daemon=True,
                ).start()
            # `local` here is just the raw @lid digits, meaningless to a user
            # ("Fulano está digitando" showing a bare numeric ID instead of a
            # name/phone). A generic placeholder is far more useful than
            # exposing that internal ID.
            return self.i18n.t("unnamed_participant")
        if jid_norm.endswith("@g.us"):
            # The presence event never identified an actual participant (its
            # jid_norm is the group itself) — same reasoning as the @lid
            # branch above: raw digits are meaningless/inaccessible to read
            # aloud, so use the generic placeholder instead.
            return self.i18n.t("unnamed_participant")
        return format_number(jid_norm)

    # ── WPPConnect — profile / group info ─────────────────────────────────
    
    def resolve_self_lid(self):
        """Query WPPConnect API for own PN-LID mapping so self-mentions resolve correctly."""
        my_jid = getattr(self, "my_jid", "")
        if not my_jid:
            return

        # Avoid redundant calls if already resolved and present in cache
        my_lid = getattr(self, "my_lid", "")
        if my_lid and my_lid in getattr(self, "_lid_to_phone", {}):
            return

        def _resolve():
            try:
                url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/contact/pn-lid/{my_jid}"
                headers = {
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json"
                }
                logging.info(f"[Self LID Resolution] Querying pn-lid mapping for own JID {my_jid}...")
                response = api_get(url, headers=headers, timeout=10)
                if response.status_code in (200, 201):
                    res = response.json() or {}
                    logging.info(f"[Self LID Resolution] Response: {res}")
                    # Parse LID JID
                    lid_obj = res.get("lid") or {}
                    lid_jid = None
                    if isinstance(lid_obj, dict):
                        lid_jid = lid_obj.get("_serialized") or lid_obj.get("id")
                    elif isinstance(lid_obj, str):
                        lid_jid = lid_obj
                    if not lid_jid:
                        lid_jid = res.get("lidJid")

                    # Parse Phone JID
                    phone_obj = res.get("phone") or res.get("phoneJid") or res.get("id") or {}
                    phone_jid = None
                    if isinstance(phone_obj, dict):
                        phone_jid = phone_obj.get("_serialized") or phone_obj.get("id")
                    elif isinstance(phone_obj, str):
                        phone_jid = phone_obj

                    if lid_jid and phone_jid:
                        normalized_phone = self._normalize_jid(phone_jid)
                        normalized_lid = self._normalize_jid(lid_jid)
                        self.my_jid = normalized_phone
                        self.my_lid = normalized_lid
                        if hasattr(self, "db") and self.db is not None:
                            self.db.set_metadata("my_jid", normalized_phone)
                            self.db.set_metadata("my_lid", normalized_lid)

                        # Clean up any bad mappings where normalized_phone or normalized_lid were mapped to other contacts
                        #
                        # All four passes are one critical section, and the
                        # two comprehensions are the reason: they iterate the
                        # live dicts, which _extract_lid_mapping() writes to
                        # from the Socket.IO thread. A message arriving
                        # mid-comprehension raised "dictionary changed size
                        # during iteration", the except below swallowed it,
                        # and register_jid_mapping() at the end never ran —
                        # the user's own LID<->phone pair went unregistered
                        # for the whole session and their group messages
                        # showed up without a name.
                        #
                        # The DB deletions are collected and executed after
                        # the lock is released: DatabaseBridge blocks this
                        # thread until the coroutine returns, and holding the
                        # mapping lock across that would freeze the Socket.IO
                        # thread with it.
                        stale_mappings = []
                        with self._lid_mapping_lock:
                            if hasattr(self, "_lid_to_phone"):
                                # 1. If another LID was mapped to our phone, delete it (from memory and DB)
                                bad_lids = [k for k, v in self._lid_to_phone.items() if v == normalized_phone and k != normalized_lid]
                                for bad_lid in bad_lids:
                                    self._lid_to_phone.pop(bad_lid, None)
                                    self._phone_to_lid.pop(normalized_phone, None)
                                    stale_mappings.append(bad_lid)
                                    logging.warning(f"[Self LID Resolution] Deleted corrupt mapping: {bad_lid} was mapped to our phone {normalized_phone}")

                                # 2. If our LID JID was mapped to another phone number, delete it
                                old_phone = self._lid_to_phone.get(normalized_lid)
                                if old_phone and old_phone != normalized_phone:
                                    self._lid_to_phone.pop(normalized_lid, None)
                                    self._phone_to_lid.pop(old_phone, None)
                                    stale_mappings.append(normalized_lid)
                                    logging.warning(f"[Self LID Resolution] Cleaned corrupt mapping: {normalized_lid} was mapped to {old_phone}")

                                # 3. If another phone JID was mapped to our LID, delete it
                                bad_phones = [k for k, v in self._phone_to_lid.items() if v == normalized_lid and k != normalized_phone]
                                for bad_phone in bad_phones:
                                    self._phone_to_lid.pop(bad_phone, None)
                                    self._lid_to_phone.pop(normalized_lid, None)
                                    stale_mappings.append(normalized_lid)
                                    logging.warning(f"[Self LID Resolution] Deleted corrupt mapping: our LID {normalized_lid} was mapped to another phone {bad_phone}")

                                # 4. If our phone JID was mapped to another LID, delete it
                                old_lid = self._phone_to_lid.get(normalized_phone)
                                if old_lid and old_lid != normalized_lid:
                                    self._phone_to_lid.pop(old_lid, None)
                                    self._lid_to_phone.pop(old_lid, None)
                                    stale_mappings.append(old_lid)
                                    logging.warning(f"[Self LID Resolution] Cleaned corrupt mapping: {normalized_phone} was mapped to {old_lid}")

                        # dict.fromkeys: passes 2 and 3 can both queue
                        # normalized_lid, and every delete here is a blocking
                        # DatabaseBridge round-trip — no point paying for it twice.
                        for stale_lid in dict.fromkeys(stale_mappings):
                            try:
                                self.db.delete_lid_mapping(stale_lid)
                            except Exception as _e:
                                pass

                        self.register_jid_mapping(normalized_lid, normalized_phone)
                        # ...and persist it unconditionally, which
                        # register_jid_mapping() does not: it only writes to
                        # SQLite when the in-memory pair actually changed.
                        # Between releasing the lock above and finishing the
                        # deletes, the Socket.IO thread can have learned this
                        # very pair from an echo of one of our own messages
                        # and written both memory and DB; the delete loop then
                        # removes that just-written row, and
                        # register_jid_mapping() sees nothing changed and
                        # skips the rewrite — leaving the pair in memory but
                        # not on disk. Harmless for the session (the display
                        # is right) and self-healing on the next launch at the
                        # cost of one pn-lid round-trip, but set_lid_mapping()
                        # is an INSERT OR REPLACE, so writing every time is
                        # cheaper than the divergence.
                        try:
                            self.db.set_lid_mapping(normalized_lid, normalized_phone)
                        except Exception:
                            pass
                        logging.info(f"[Self LID Resolution] Successfully resolved and registered own JID mapping: {normalized_lid} <-> {normalized_phone}")
            except Exception as e:
                logging.error(f"[Self LID Resolution] Error resolving self LID: {e}")

        threading.Thread(target=_resolve, daemon=True).start()

    def register_jid_mapping(self, lid_jid, phone_jid, save=True, defer_ui=False):
        """Register a bidirectional mapping between @lid and @s.whatsapp.net, and persist it.

        `defer_ui` suppresses this call's own chat-list refresh, for callers
        resolving a whole batch: they schedule one refresh when the batch
        finishes instead of one per mapping. Measured on a live 935-chat
        sync, resolve_lid_jids_via_api() learned 1245 mappings in eight
        minutes and each one scheduled a rebuild — _schedule_set_chats()
        debounces at 300 ms, so that still came to roughly 380 full
        recomputations of a 935-chat list, each resolving every chat's name.
        The live message path (on_new_message) deliberately does NOT pass
        this: a single arriving message must still refresh the list at once.
        """
        if not lid_jid or not phone_jid:
            return
        if not lid_jid.endswith("@lid") or not phone_jid.endswith("@s.whatsapp.net"):
            return
            
        # Guard against corrupt self-mappings.
        # Special case: if lid_jid is definitively our own LID (my_lid), we know
        # phone_jid is also ours — phone number format differences can make
        # _is_self_jid() return False for the phone side even when they match.
        _my_lid = getattr(self, "my_lid", "")
        if _my_lid and lid_jid == _my_lid:
            # User's own LID→phone mapping: always valid, update my_jid if format differs.
            if not self._is_self_jid(phone_jid):
                logging.info(f"[LID Mapping] Updating my_jid to {phone_jid} (format differs from {getattr(self, 'my_jid', '')})")
                self.my_jid = phone_jid
        elif self._is_self_jid(lid_jid) or self._is_self_jid(phone_jid):
            if not (self._is_self_jid(lid_jid) and self._is_self_jid(phone_jid)):
                logging.warning(f"[LID Mapping] Blocked corrupt self-mapping attempt: {lid_jid} <-> {phone_jid}")
                return
            
        # This is the mapping writer the sync thread actually uses
        # (_backfill_names -> resolve_lid_jids_via_api -> here), racing
        # _extract_lid_mapping() on the Socket.IO thread over the very same
        # check-then-set. Only the in-memory update belongs in the critical
        # section: the DB write and the UI refresh below stay outside it,
        # because self.db blocks this thread until the coroutine returns —
        # exactly what must not happen while the Socket.IO thread is waiting
        # for the lock.
        changed = False
        was_unresolvable = False
        with self._lid_mapping_lock:
            if not hasattr(self, "_lid_to_phone"):
                self._lid_to_phone = {}
            if not hasattr(self, "_phone_to_lid"):
                self._phone_to_lid = {}

            current_phone = self._lid_to_phone.get(lid_jid)
            if current_phone != phone_jid:
                changed = True
                self._lid_to_phone[lid_jid] = phone_jid
                self._phone_to_lid[phone_jid] = lid_jid
                logging.info(f"[LID Mapping] Registered JID mapping: {lid_jid} <-> {phone_jid}")

                # If it was in the unresolvable set, remove it
                if hasattr(self, "_unresolvable_lids") and lid_jid in self._unresolvable_lids:
                    self._unresolvable_lids.discard(lid_jid)
                    was_unresolvable = True

                # Update the contact name display mappings in contacts if possible
                if phone_jid in self.contacts and self.contacts[phone_jid]:
                    if lid_jid not in self.contacts or self.contacts[lid_jid].get("name") in (None, "", "Contato sem nome"):
                        self.contacts[lid_jid] = self.contacts[phone_jid].copy()
                        self.contacts[lid_jid]["id"] = lid_jid
                        self.contacts[lid_jid]["remoteJid"] = lid_jid

        if changed:
            if was_unresolvable:
                try:
                    self.db.delete_unresolvable_lid(lid_jid)
                except Exception as exc:
                    logging.warning("[LID Mapping] Failed to clear unresolvable LID %s: %s", lid_jid, exc)
            if save:
                # Save the mapping to SQLite incrementally
                try:
                    self.db.set_lid_mapping(lid_jid, phone_jid)
                    if lid_jid in self.contacts:
                        self.db.upsert_contacts_batch({lid_jid: self.contacts[lid_jid]})
                except Exception as exc:
                    logging.warning("[LID Mapping] Failed to save mapping incrementally: %s", exc)
                    # Fallback to save_data if incremental save fails
                    self.save_data(self.chats, self.contacts)
            if not defer_ui:
                wx.CallAfter(self._schedule_set_chats)
                # The open conversation needs the same treatment as the chat
                # list, and only since the message repaint became scoped: the
                # non-batch mapping writers (_extract_lid_mapping() on the
                # Socket.IO thread, _get_participant_name()'s inline learn)
                # never scheduled one of their own and used to be fixed up by
                # whatever unrelated full repaint happened next. Both JIDs go
                # in, since the rows can carry either form. Terminating even
                # though rendering itself can reach here: a mapping is only
                # `changed` once, so the second pass schedules nothing.
                wx.CallAfter(self._schedule_refresh_active_messages,
                             {lid_jid, phone_jid})

    def resolve_lid_jids_via_api(self, jids):
        """Resolve a list of @lid JIDs to phone JIDs using WPPConnect contact endpoint."""
        if not jids:
            return
        if not hasattr(self, "db") or self.db is None:
            # Defense in depth: every known caller is now gated behind
            # _ui_ready_event (see _extract_lid_mapping()), but this batch
            # runs on its own background thread and can outlive that check —
            # bail rather than crash self.db.upsert_contacts_batch() below.
            return
            
        updated_contacts = {}
        # A single timeout is routine while WPPConnect is busy with the initial
        # history sync, so don't throw away the rest of the batch over one —
        # only give up when the API looks genuinely down (several in a row).
        _MAX_CONSECUTIVE_ERRORS = 3
        _REQUEST_TIMEOUT        = 10   # seconds; 4 s expired constantly during sync
        consecutive_errors      = 0
        for lid_jid in jids:
            if not lid_jid.endswith("@lid"):
                continue
                
            if not getattr(self, "_wa_connected", False):
                logging.warning("[LID Resolution] WhatsApp is not connected. Aborting loop.")
                break

            # Check caches and active resolving list under lock
            if not hasattr(self, "_lid_resolution_lock"):
                self._lid_resolution_lock = threading.Lock()
            if not hasattr(self, "_unresolvable_lids"):
                self._unresolvable_lids = set()
            if not hasattr(self, "_resolving_lids"):
                self._resolving_lids = set()
                
            if not hasattr(self, "_unresolvable_names"):
                self._unresolvable_names = set()
                
            query_pn = lid_jid not in getattr(self, "_lid_to_phone", {}) and lid_jid not in self._unresolvable_lids
            
            contact = self.contacts.get(lid_jid, {})
            has_name = contact.get("name") or contact.get("pushName")
            query_name = not has_name and lid_jid not in self._unresolvable_names
            
            if not query_pn and not query_name:
                continue
                
            with self._lid_resolution_lock:
                if lid_jid in self._resolving_lids:
                    continue
                self._resolving_lids.add(lid_jid)
                
            # Set when the API never actually answered (timeout, connection
            # reset, dead session).  Such a failure says nothing about whether
            # this LID is resolvable, so it must not feed the blacklists below.
            transient_error = False
            try:
                canonical_jid = getattr(self, "_lid_to_phone", {}).get(lid_jid)
                headers = {
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json"
                }
                
                if query_pn:
                    # First, resolve pn-lid mapping
                    url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/contact/pn-lid/{lid_jid}"
                    logging.info(f"[LID Resolution] Querying WPPConnect pn-lid mapping for {lid_jid}...")
                    response = api_get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
                    if response.status_code in (200, 201):
                        res = response.json() or {}
                        logging.info(f"[LID Resolution] pn-lid response for {lid_jid}: {res}")
                        res_data = res.get("response") if isinstance(res.get("response"), dict) else res
                        pn_obj = res_data.get("phoneNumber") or {}
                        pn_jid = None
                        if isinstance(pn_obj, dict):
                            pn_jid = pn_obj.get("_serialized") or pn_obj.get("id")
                        elif isinstance(pn_obj, str):
                            pn_jid = pn_obj
                        if not pn_jid:
                            pn_jid = res_data.get("pnJid")
                        if pn_jid:
                            canonical_jid = self._normalize_jid(pn_jid)
                            if canonical_jid and canonical_jid.endswith("@s.whatsapp.net"):
                                self.register_jid_mapping(lid_jid, canonical_jid, save=False, defer_ui=True)
                                try:
                                    self.db.set_lid_mapping(lid_jid, canonical_jid)
                                except Exception as exc:
                                    logging.warning("[LID Resolution] set_lid_mapping failed: %s", exc)
                        
                        # Try to resolve contact name/pushname directly from pn-lid mapping response
                        contact_obj = res_data.get("contact") or {}
                        res_name = contact_obj.get("name") or contact_obj.get("pushname") or contact_obj.get("pushName") or contact_obj.get("displayName")
                        if res_name and res_name != "Contato sem nome" and not is_phone_like(res_name):
                            if lid_jid not in self.contacts:
                                self.contacts[lid_jid] = {}
                            self.contacts[lid_jid]["name"] = res_name
                            self.contacts[lid_jid]["pushName"] = res_name
                            updated_contacts[lid_jid] = self.contacts[lid_jid]
                            
                            if not hasattr(self, "_presence_pushname_map"):
                                self._presence_pushname_map = {}
                            self._presence_pushname_map[lid_jid] = res_name
                            
                            if canonical_jid:
                                if canonical_jid not in self.contacts:
                                    self.contacts[canonical_jid] = {}
                                self.contacts[canonical_jid]["name"] = res_name
                                self.contacts[canonical_jid]["pushName"] = res_name
                                updated_contacts[canonical_jid] = self.contacts[canonical_jid]
                                self._presence_pushname_map[canonical_jid] = res_name
                            
                            # Resolved the name successfully, no need to query profile
                            query_name = False
                
                if query_name:
                    # Fetch profile info for name caching
                    # If we mapped it to a phone JID, fetch that. Otherwise fetch the lid JID directly.
                    target_jid = canonical_jid if canonical_jid else lid_jid
                    url_profile = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/contact/{target_jid}"
                    logging.info(f"[LID Resolution] Querying profile details for {target_jid}...")
                    resp_profile = api_get(url_profile, headers=headers, timeout=_REQUEST_TIMEOUT)
                    # Check profile response
                    if resp_profile.status_code in (200, 201):
                        res_prof = resp_profile.json() or {}
                        res_data = res_prof.get("response") if isinstance(res_prof.get("response"), dict) else res_prof
                        if not isinstance(res_data, dict):
                            res_data = {}
                            
                        # Resolve JID mapping from contact details
                        profile_pn_jid = None
                        id_obj = res_data.get("id") or {}
                        if isinstance(id_obj, dict):
                            ser_id = id_obj.get("_serialized") or ""
                            if ser_id.endswith(("@c.us", "@s.whatsapp.net")):
                                profile_pn_jid = ser_id
                        if not profile_pn_jid:
                            pn_obj = res_data.get("phoneNumber") or {}
                            if isinstance(pn_obj, dict):
                                profile_pn_jid = pn_obj.get("_serialized") or pn_obj.get("id")
                            elif isinstance(pn_obj, str):
                                profile_pn_jid = pn_obj
                        if not profile_pn_jid:
                            profile_pn_jid = res_data.get("pnJid")
                        if not profile_pn_jid:
                            profile_pn_jid = res_data.get("phone")
                            
                        if profile_pn_jid:
                            profile_canonical = self._normalize_jid(profile_pn_jid)
                            if profile_canonical and profile_canonical.endswith("@s.whatsapp.net"):
                                self.register_jid_mapping(lid_jid, profile_canonical, save=False, defer_ui=True)
                                try:
                                    self.db.set_lid_mapping(lid_jid, profile_canonical)
                                except Exception as exc:
                                    logging.warning("[LID Resolution] set_lid_mapping (profile) failed: %s", exc)
                                if not canonical_jid:
                                    canonical_jid = profile_canonical
                        formatted_name = res_data.get("formattedName")
                        if formatted_name:
                            if lid_jid not in self.contacts:
                                self.contacts[lid_jid] = {}
                            self.contacts[lid_jid]["formattedName"] = formatted_name
                            updated_contacts[lid_jid] = self.contacts[lid_jid]

                        name = res_data.get("name") or res_data.get("pushname") or res_data.get("pushName") or res_data.get("displayName")
                        if name and name != "Contato sem nome" and not is_phone_like(name):
                            if lid_jid not in self.contacts:
                                self.contacts[lid_jid] = {}
                            self.contacts[lid_jid]["name"] = name
                            self.contacts[lid_jid]["pushName"] = name
                            updated_contacts[lid_jid] = self.contacts[lid_jid]
                            
                            # Also save to presence pushname map to ensure UI functions find it
                            if not hasattr(self, "_presence_pushname_map"):
                                self._presence_pushname_map = {}
                            self._presence_pushname_map[lid_jid] = name
                            
                            # Also copy to phone contact cache if mapped
                            if canonical_jid:
                                if canonical_jid not in self.contacts:
                                    self.contacts[canonical_jid] = {}
                                self.contacts[canonical_jid]["name"] = name
                                self.contacts[canonical_jid]["pushName"] = name
                                updated_contacts[canonical_jid] = self.contacts[canonical_jid]
                                self._presence_pushname_map[canonical_jid] = name
                        else:
                            # Not the name, and not the raw response (which
                            # carries formattedName/pushname/a signed profile
                            # photo URL) — same "shape, not content" rule as
                            # get_remote_contacts(). Whether a name was present
                            # at all, and why it was rejected, is the part that
                            # was ever useful for debugging this path.
                            reason = (
                                "empty" if not name
                                else "placeholder" if name == "Contato sem nome"
                                else "phone-like"
                            )
                            res_shape = {k: type(v).__name__ for k, v in res_data.items()} if isinstance(res_data, dict) else type(res_data).__name__
                            logging.info(f"[LID Resolution] Profile name not resolved/accepted for {target_jid}. reason={reason}. Response shape: {res_shape}")
                    else:
                        logging.error(f"[LID Resolution] fetchProfile API error {resp_profile.status_code} for {target_jid}: {resp_profile.text}")
                        # If the API returns 404/500 indicating the session was closed/disconnected, stop making calls immediately
                        if resp_profile.status_code in (404, 500) or "session is not active" in resp_profile.text.lower():
                            logging.warning("[LID Resolution] Session is disconnected/not active. Aborting loop.")
                            transient_error = True
                            break
                consecutive_errors = 0
            except requests.exceptions.RequestException as e:
                transient_error     = True
                consecutive_errors += 1
                logging.warning(
                    "[LID Resolution] Network/API error resolving %s (%d/%d): %s",
                    lid_jid, consecutive_errors, _MAX_CONSECUTIVE_ERRORS, e,
                )
                # Only abort once the API looks genuinely down — a lone timeout
                # while WPPConnect is busy must not cancel the whole batch.
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    logging.error(
                        "[LID Resolution] API unresponsive after %d consecutive errors — aborting batch.",
                        consecutive_errors,
                    )
                    break
            except Exception as e:
                transient_error = True
                logging.error(f"[LID Resolution] Exception during resolution of {lid_jid}: {e}")
            finally:
                with self._lid_resolution_lock:
                    self._resolving_lids.discard(lid_jid)
                    # Only blacklist when the API actually answered.  Both sets
                    # are persisted to SQLite and consulted before every future
                    # query, so recording a LID here after a mere timeout leaves
                    # that contact stuck on "Contato sem nome" for good — across
                    # restarts — even though it was perfectly resolvable.
                    if not transient_error:
                        if query_pn and lid_jid not in getattr(self, "_lid_to_phone", {}):
                            self._unresolvable_lids.add(lid_jid)
                            try:
                                self.db.add_unresolvable_lid(lid_jid)
                            except Exception as exc:
                                logging.warning("[LID Resolution] add_unresolvable_lid failed: %s", exc)
                        if query_name:
                            contact_now = self.contacts.get(lid_jid, {})
                            has_name_now = contact_now.get("name") or contact_now.get("pushName")
                            if not has_name_now:
                                self._unresolvable_names.add(lid_jid)
                                try:
                                    self.db.add_unresolvable_name(lid_jid)
                                except Exception as exc:
                                    logging.warning("[LID Resolution] add_unresolvable_name failed: %s", exc)
                # Throttle the query loop exactly once per iteration (success
                # or failure) so Puppeteer isn't overwhelmed and can prioritize
                # message sending. This used to also sleep on the try block's
                # success path above, silently doubling the 0.5s throttle to
                # 1s and, over a large batch of unresolved LIDs, doubling how
                # long a fresh account spends with "Participante sem nome"
                # showing in groups.
                time.sleep(0.5)

        if updated_contacts:
            try:
                self.db.upsert_contacts_batch(updated_contacts)
            except Exception as e:
                logging.error(f"[LID Resolution] Error saving contacts incrementally: {e}")
                self.save_data(self.chats, self.contacts)
        wx.CallAfter(self._schedule_set_chats)
        # Every LID this batch was asked about (its name and/or its phone
        # bridge may have just been filled in) plus the phone JIDs the answers
        # named. _jid_address_forms() expands each into its counterpart, so a
        # row still carrying the @lid matches a phone JID listed here.
        wx.CallAfter(self._schedule_refresh_active_messages,
                     {j for j in jids if isinstance(j, str) and j} | set(updated_contacts))

    def get_contact_profile(self, jid: str) -> dict:
        """Fetch contact profile from WPPConnect (runs on background thread)."""
        original_jid = jid
        if jid.endswith("@lid"):
            resolved = getattr(self, "_lid_to_phone", {}).get(jid, "")
            if resolved:
                jid = resolved
            else:
                # Only query if not marked as unresolvable
                if jid not in getattr(self, "_unresolvable_lids", set()):
                    # Resolve mapping via API before querying profile
                    self.resolve_lid_jids_via_api([original_jid])
                    resolved = getattr(self, "_lid_to_phone", {}).get(original_jid, "")
                    if resolved:
                        jid = resolved
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/contact/{jid}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        try:
            r = api_get(url, headers=headers, timeout=10)
            logging.info(f"[get_contact_profile] Querying for {original_jid} (using JID: {jid}). Response status: {r.status_code}")
            if r.status_code in (200, 201):
                res = r.json() or {}
                logging.info(f"[get_contact_profile] API Response for {original_jid}: {res}")
                res_data = res.get("response", {})
                if not isinstance(res_data, dict):
                    res_data = {}
                
                # If queried directly with @lid, check if we got back a canonical @s.whatsapp.net JID
                if original_jid.endswith("@lid") and jid.endswith("@lid"):
                    canonical_jid = self._normalize_jid(res_data.get("id", {}).get("_serialized") or res_data.get("id") or "")
                    if canonical_jid and canonical_jid.endswith("@s.whatsapp.net"):
                        logging.info(f"[get_contact_profile] SUCCESS: Mapped {original_jid} to {canonical_jid} via profile query")
                        # This method runs on a background thread (see the
                        # docstring), so the write shares the mapping lock
                        # with the Socket.IO thread's own. The refresh and
                        # the DB write below stay outside it — no blocking
                        # call may run while holding this lock.
                        with self._lid_mapping_lock:
                            if not hasattr(self, "_lid_to_phone"):
                                self._lid_to_phone = {}
                            if not hasattr(self, "_phone_to_lid"):
                                self._phone_to_lid = {}
                            self._lid_to_phone[original_jid] = canonical_jid
                            self._phone_to_lid[canonical_jid] = original_jid

                        # Trigger UI refresh and save mapped JIDs
                        wx.CallAfter(self._schedule_set_chats)
                        try:
                            self.db.set_lid_mapping(original_jid, canonical_jid)
                        except Exception as e:
                            logging.error(f"[get_contact_profile] Error saving mapping incrementally: {e}")
                            self.save_data(self.chats, self.contacts)
                # The contact endpoint's top-level "status" is the API result
                # ("success"), NOT the contact's About text. Fetch the real
                # About/bio from the dedicated profile-status endpoint and expose
                # it under a clean key the dialog can read without ambiguity.
                res["aboutText"] = self.get_profile_about(jid)
                res["lastSeenTs"] = self.get_last_seen(jid)
                return res
        except Exception as e:
            logging.exception(f"[get_contact_profile] Error querying for {original_jid}: {e}")
        return {}

    def get_last_seen(self, jid: str):
        """Return a contact's last-seen Unix timestamp via /last-seen, or None.

        More reliable than waiting for a presence.update event, which only fires
        if the contact changes state after we subscribe. Returns None when the
        contact hides last-seen or it is unavailable.
        """
        if not jid or jid.endswith("@lid") or jid.endswith("@g.us"):
            return None
        phone = jid.split("@")[0]
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/last-seen/{phone}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            r = api_get(url, headers=headers, timeout=10)
            if r.status_code not in (200, 201):
                return None
            resp = (r.json() or {}).get("response")
            if isinstance(resp, dict):
                resp = resp.get("t") or resp.get("lastSeen")
            if isinstance(resp, bool) or resp in (None, 0):
                return None
            try:
                ts = int(resp)
            except (TypeError, ValueError):
                return None
            # WhatsApp sometimes returns timestamps in ms.
            if ts > 1_000_000_000_000:
                ts //= 1000
            return ts if ts > 0 else None
        except Exception:
            return None

    def get_profile_about(self, jid: str) -> str:
        """Return a contact's WhatsApp About/bio text via /profile-status, or ''."""
        if not jid or jid.endswith("@lid"):
            return ""
        phone = jid.replace("@s.whatsapp.net", "@c.us")
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/profile-status/{phone}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            r = api_get(url, headers=headers, timeout=10)
            if r.status_code not in (200, 201):
                return ""
            resp = (r.json() or {}).get("response")
            # getStatus returns either a string or {id, status: "<about>"}.
            if isinstance(resp, dict):
                about = resp.get("status") or resp.get("about") or ""
            else:
                about = resp or ""
            about = str(about).strip()
            # Guard against the endpoint echoing an API status word.
            if about.lower() in ("success", "error", "none", "null"):
                return ""
            return about
        except Exception:
            return ""

    def subscribe_presence(self, jid: str):
        """Subscribe to presence events for a contact via WPPConnect API (non-blocking)."""
        if not jid or jid.endswith("@newsletter"):
            return
        
        if not hasattr(self, "_subscribed_presence_cache"):
            self._subscribed_presence_cache = {}
            
        jids_to_subscribe = [jid]
        phone_to_lid = getattr(self, "_phone_to_lid", {})
        lid_to_phone = getattr(self, "_lid_to_phone", {})
        
        if jid in phone_to_lid:
            jids_to_subscribe.append(phone_to_lid[jid])
        elif jid in lid_to_phone:
            jids_to_subscribe.append(lid_to_phone[jid])
            
        now = time.time()
        targets = []
        for target_jid in set(jids_to_subscribe):
            last_sub = self._subscribed_presence_cache.get(target_jid, 0)
            if now - last_sub > 10.0:  # Throttle duplicate subscriptions within 10 seconds
                self._subscribed_presence_cache[target_jid] = now
                targets.append(target_jid)
                
        if not targets:
            return
            
        def _api():
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            for target_jid in targets:
                is_group = target_jid.endswith("@g.us")
                is_lid = target_jid.endswith("@lid")
                phone = target_jid.replace("@s.whatsapp.net", "@c.us")
                url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/subscribe-presence"
                logging.info("[subscribe_presence] Subscribing to: %s (isGroup=%s, isLid=%s)", phone, is_group, is_lid)
                try:
                    resp = api_post(url, json={"phone": phone, "isGroup": is_group, "isLid": is_lid}, headers=headers, timeout=10)
                    logging.info("[subscribe_presence] Response for %s: %s (body: %s)", phone, resp.status_code, resp.text[:200])
                except Exception as e:
                    logging.error("[subscribe_presence] Error subscribing to %s: %s", phone, e)
        threading.Thread(target=_api, daemon=True).start()
