"""MessageEventsMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import threading
import time
import wx
from core.quote_recovery import (
    RECOVERED_FROM_QUOTE,
    UNDECRYPTED_PLACEHOLDER_TYPES,
    awaits_real_copy,
    fill_placeholders_from_replies,
    reply_context,
)
from main_window.message_rules import (
    _MAX_RESIDENT_MESSAGES_PER_CHAT,
    is_countable_message,
    own_message_marks_chat_read,
)
from core.message_edit import (
    apply_caption_edit,
    is_edit_event,
)
from core.call_log import (
    call_log_supersedes,
    is_call_log,
)
from core.utils import (
    effective_unread_count,
    is_message_forwarded,
    is_phone_like,
    prune_message_record,
    reaction_targets_status,
)


class MessageEventsMixin:
    """Live and historical message ingestion (on_new_message /
    on_historical_message), revokes, edits, undecrypted placeholders and
    reaction notifications.
    """

    def _apply_remote_revoke(self, existing: dict, incoming: dict, remote_jid: str) -> bool:
        """Detect a "delete for everyone" (protocolMessage type 3/REVOKE)
        re-delivered under the same key.id as an already-stored message, and
        mark the original revoked in place — instead of leaving its
        original content (playable audio/video included) on screen until
        the next periodic _mirror_remote_deletions() poll, which only
        removes the row outright rather than marking it deleted, and can
        take a while to even run. The official client reflects a remote
        delete instantly; this is the live-event equivalent of that.

        Returns True if `incoming` was a revoke (handled or already
        applied), so the caller's edit-detection logic is skipped either way.
        """
        if incoming.get("messageType") != "protocolMessage":
            return False
        protocol = (incoming.get("message") or {}).get("protocolMessage") or {}
        if protocol.get("type") not in (3, "REVOKE", "revoke"):
            return False
        if existing.get("messageType") == "protocolMessage":
            return True  # already applied (e.g. re-delivered echo) — nothing to do

        msg_id = existing.get("key", {}).get("id", "")
        existing["message"]     = incoming.get("message")
        existing["messageType"] = "protocolMessage"
        existing.pop("_edited", None)

        def _bg_persist():
            try:
                self.db.insert_message(remote_jid, existing)
            except Exception as e:
                logging.error(f"[_apply_remote_revoke] Failed to persist revoked message: {e}")
        self._msg_bg_executor.submit(_bg_persist)

        if hasattr(self, "conversations_panel"):
            wx.CallAfter(self.conversations_panel.on_message_revoked, msg_id)
        self._schedule_set_chats()
        return True

    #: WhatsApp Web's "arrived, not decrypted yet" placeholder. It is followed
    #: by the real message under the same key.id.
    _UNDECRYPTED_PLACEHOLDER_TYPES = UNDECRYPTED_PLACEHOLDER_TYPES

    @staticmethod
    def _is_undecrypted_placeholder(msg: dict) -> bool:
        """Whether this event is a placeholder rather than a message."""
        return (((msg or {}).get("messageType") or "")
                in MessageEventsMixin._UNDECRYPTED_PLACEHOLDER_TYPES)

    @staticmethod
    def _resolved_placeholder_is_fresh(records: list, index: int, incoming: dict,
                                       connected_at: float) -> bool:
        """Whether the decrypted copy of a STORED placeholder is a new arrival.

        The live funnel never stores a placeholder, but a get-messages sync
        does: it stores whatever WhatsApp Web's store holds, and a message that
        device could not decrypt yet ("Aguardando mensagem") is held as a
        `ciphertext`. When the real copy arrives later it is either
        - still the newest message, and recent: a sync raced the 2-4 s the
          decryption normally takes, and the copy must get the full new-message
          path (badge, sound, announcement), or
        - an older one, decrypted minutes or hours later (the sender's phone
          answering WhatsApp's retry): it is filled in where it already sits,
          silently, since appending it would put it after newer messages.
        Same 60 s cutoff the notification path uses.
        """
        if index != len(records) - 1:
            return False
        try:
            return int(incoming.get("messageTimestamp") or 0) >= connected_at - 60
        except (TypeError, ValueError):
            return False

    def _recover_placeholders_from_replies(self, remote_jid: str, records: list,
                                           replies=None) -> int:
        """Fill placeholders in *records* from the quotes of *replies*; persist them.

        *replies* defaults to *records* itself. The database is written per
        message rather than through the debounced chat save, so the text
        survives closing and reopening the conversation. Never raises: it runs
        inside the live funnel and at startup.
        """
        try:
            filled = fill_placeholders_from_replies(
                records, self._chat_jids_equivalent, replies)
        except Exception:
            logging.exception("[quote-recovery] %s: failed", remote_jid)
            return 0
        if not filled:
            return 0
        logging.info("[quote-recovery] %s: %d message(s) recovered from reply quotes",
                     remote_jid, len(filled))

        def _bg_persist():
            for record in filled:
                try:
                    self.db.insert_message(remote_jid, record)
                except Exception as e:
                    logging.error(f"[quote-recovery] Failed to persist message: {e}")
        self._run_quote_recovery_write(_bg_persist)
        if hasattr(self, "conversations_panel"):
            wx.CallAfter(self.conversations_panel.refresh_messages_if_changed)
        return len(filled)

    def _recover_quoted_placeholder(self, remote_jid: str, records: list, reply: dict) -> None:
        """Let a live reply fill the placeholder it quotes, in memory or on disk.

        Only the newest messages of a chat are resident, so a reply to an older
        "Aguardando mensagem" finds it through the database, by id, off the
        main thread; reopening the conversation then reads the filled row.
        """
        ctx = reply_context(reply)
        if ctx is None:
            return
        quoted_id = ctx.get("stanzaId")
        if any((r.get("key") or {}).get("id") == quoted_id for r in records):
            self._recover_placeholders_from_replies(remote_jid, records, [reply])
            return

        def _from_database():
            try:
                stored = self.db.get_message_by_id(remote_jid, quoted_id)
                if not stored:
                    return
                if fill_placeholders_from_replies(
                        [stored], self._chat_jids_equivalent, [reply]):
                    # The decrypted copy may have landed since the read above
                    # (the executor runs several writes at once): never write
                    # a reply's quote over the real message.
                    current = self.db.get_message_by_id(remote_jid, quoted_id)
                    if current and not awaits_real_copy(current):
                        return
                    self.db.insert_message(remote_jid, stored)
                    logging.info("[quote-recovery] %s: stored message %s recovered "
                                 "from a reply quote", remote_jid, str(quoted_id)[:22])
            except Exception:
                logging.exception("[quote-recovery] %s: database lookup failed", remote_jid)
        self._run_quote_recovery_write(_from_database)

    def _run_quote_recovery_write(self, work) -> None:
        """Run a quote-recovery database write off the main thread when possible.

        The startup pass runs from prepare_sync(), which __init__ calls BEFORE
        it creates _msg_bg_executor: submitting there raised AttributeError
        out of __init__ and WinZapp could not start at all (measured
        2026-09-23, on the first launch with pairs to recover). The database is
        already open at that point, so without the executor the write simply
        runs now. Never raises -- neither caller may be taken down by it.
        """
        try:
            executor = getattr(self, "_msg_bg_executor", None)
            if executor is None:
                work()
            else:
                executor.submit(work)
        except Exception:
            logging.exception("[quote-recovery] could not run the database write")

    @staticmethod
    def _adopt_decrypted_copy(existing: dict, incoming: dict) -> dict:
        """Turn the stored placeholder record itself into its decrypted copy.

        In place, and returned: the open conversation's rows are these same
        dict objects, so replacing the record with a new dict left the list
        rendering the old one -- a review caught the row still reading
        "Aguardando mensagem" after a rebuild. Replaced, not merged: nothing the
        placeholder carried outlives it except local-only (`_`) fields, and a
        text recovered from a reply's quote is superseded.
        """
        for field in [f for f in existing if not str(f).startswith("_")]:
            if field not in incoming:
                del existing[field]
        for field, value in incoming.items():
            existing[field] = value
        existing.pop(RECOVERED_FROM_QUOTE, None)
        return existing

    def _fill_stored_placeholder(self, existing: dict, incoming: dict, remote_jid: str,
                                 what: str = "decrypted copy of stored placeholder") -> None:
        """Replace a stored placeholder with its decrypted copy, in place.

        Also how a call record's newer state replaces the stored one (*what*
        names which, for the log; core/call_log.py), from either funnel.

        _apply_possible_edit() cannot do it: a placeholder has no text, so it
        is never "an edit" there, and the decrypted copy was discarded as a
        duplicate. The row stayed hidden for good (a ciphertext is not a
        displayable type) and a reply quoting it pointed at nothing — measured
        2026-09-23: 165 such rows on one install, 20 of them from that morning.
        Local-only fields (`_`-prefixed) on the record are kept.
        """
        prune_message_record(incoming)
        MessageEventsMixin._adopt_decrypted_copy(existing, incoming)
        logging.info("[stored record] %s: %s %s filled in (%s).", remote_jid, what,
                     ((existing.get("key") or {}).get("id") or "")[:22],
                     existing.get("messageType"))

        def _bg_persist():
            try:
                self.db.insert_message(remote_jid, existing)
            except Exception as e:
                logging.error(f"[_fill_stored_placeholder] Failed to persist message: {e}")
        self._msg_bg_executor.submit(_bg_persist)
        # The row changes from "Aguardando mensagem" to the real message;
        # refresh_messages_if_changed() repaints or rebuilds while keeping focus.
        if hasattr(self, "conversations_panel"):
            wx.CallAfter(self.conversations_panel.refresh_messages_if_changed)
        self._schedule_save(dirty_jid=remote_jid)
        self._schedule_set_chats()

    def _drop_protocol_edit(self, remote_jid: str, msg: dict) -> bool:
        """True when *msg* is an edit protocol message, which is then dropped.

        Not applied: the same edit also arrives under the original id through
        onMessageEdit, carrying the full current message, and
        _apply_possible_edit() handles that one (core/message_edit.py). A row
        this event already left behind under its own id — every install that
        met the bug has one per edit — is purged on the way.
        """
        if not is_edit_event(msg):
            return False
        event_id = (msg.get("key") or {}).get("id") or ""
        target_id = ((msg.get("message") or {}).get("protocolMessage") or {}).get("key") or ""
        logging.info("[edit] dropping edit event %s (edits %s) in %s",
                     event_id[:22], str(target_id)[:22], remote_jid)
        if event_id:
            self._remember_dropped_edit_events({event_id})
            self._purge_materialized_edit_rows(remote_jid, {event_id})
        return True

    def _remember_dropped_edit_events(self, event_ids) -> None:
        """Record edit-event ids so sync_chat_messages()' merge never keeps a
        local copy of one.

        That merge preserves every stored record whose id the fetched page
        lacks — and once the page stops returning the event as a row, the copy
        stored before the fix is exactly such a record. Without this it would
        be merged straight back into `records` on every sync, racing (and
        undoing) _purge_materialized_edit_rows(). A bare set is enough: ids are
        unique, set.add is atomic under the GIL, and it grows by one per edit.
        """
        ids = {i for i in (event_ids or ()) if i}
        if not ids:
            return
        if not hasattr(self, "_dropped_edit_event_ids"):
            self._dropped_edit_event_ids = set()
        self._dropped_edit_event_ids.update(ids)

    def _purge_materialized_edit_rows(self, remote_jid: str, event_ids) -> int:
        """Remove rows an edit event was stored as, under the event's own id.

        Main thread. Such an id can only ever belong to that bogus copy — a
        protocol message is never a real message — so removing it cannot touch
        anything genuine. Returns how many records were removed.
        """
        ids = {i for i in (event_ids or ()) if i}
        if not ids:
            return 0
        candidates = [remote_jid, self._normalize_jid(remote_jid),
                      getattr(self, "_lid_to_phone", {}).get(remote_jid, ""),
                      getattr(self, "_phone_to_lid", {}).get(self._normalize_jid(remote_jid), "")]
        removed = 0
        for jid in dict.fromkeys(j for j in candidates if j):
            chat = self.chats.get(jid) or {}
            records = chat.get("messages", {}).get("messages", {}).get("records", [])
            present = {(r.get("key") or {}).get("id") for r in records
                       if isinstance(r, dict)} & ids
            if present:
                logging.info("[edit] removing %d row(s) stored from an edit event in %s",
                             len(present), jid)
                cp = getattr(self, "conversations_panel", None)
                if (cp is not None and cp.conversation is not None
                        and self._normalize_jid(cp.conversation.get("remoteJid", ""))
                        == self._normalize_jid(jid)):
                    # Also takes them out of records and the DB.
                    cp.remove_messages_by_id(present, focus_previous=True)
                else:
                    records[:] = [r for r in records
                                  if not (isinstance(r, dict)
                                          and (r.get("key") or {}).get("id") in present)]
                removed += len(present)
                self._recompute_chat_last_message(jid)
            # Unconditionally, not only for what is resident: records are
            # capped and store-only history fetches never load what they
            # write, so a copy can sit on disk with nothing in memory. A
            # DELETE of a row that is not there is harmless.
            for event_id in ids:
                try:
                    self.db.delete_message(jid, event_id)
                except Exception as e:
                    logging.error("[edit] failed to delete stored edit event %s: %s",
                                  event_id, e)
        if removed:
            self._schedule_set_chats()
        return removed

    def _persist_and_repaint_edit(self, existing: dict, remote_jid: str) -> None:
        """Save an edited record and refresh what shows it."""
        def _bg_persist():
            try:
                self.db.insert_message(remote_jid, existing)
            except Exception as e:
                logging.error(f"[_apply_possible_edit] Failed to persist edited message: {e}")
        self._msg_bg_executor.submit(_bg_persist)
        if hasattr(self, "conversations_panel"):
            wx.CallAfter(self.conversations_panel.refresh_active_conversation_messages)
        self._schedule_set_chats()

    def _apply_possible_edit(self, existing: dict, incoming: dict, remote_jid: str):
        """Detect and apply an edit re-delivered under the same key.id.

        WhatsApp reuses the original message's ID when a message is edited
        (own edits via edit_message(), or an edit made by anyone else from any
        device) — the edited copy arrives back through the exact same
        live-message channel as any other message, just with a duplicate
        key.id. Without this, the dedup check right above ("already stored")
        silently discarded it, so edits from other people never appeared at
        all, and our own edits only showed locally because conversations.py
        already updates them optimistically when sent.

        Text is compared directly. An image, video or document can have its
        caption edited too (WhatsApp's getMsgEditType maps those to
        CaptionEdit); that goes through core.message_edit.apply_caption_edit(),
        which acts only on a copy WhatsApp itself marks as edited and changes
        nothing but the caption.
        """
        if self._apply_remote_revoke(existing, incoming, remote_jid):
            return

        caption_result = apply_caption_edit(existing, incoming)
        if caption_result is not None:
            logging.info("[edit] caption edit %s for %s in %s", caption_result,
                         ((existing.get("key") or {}).get("id") or "")[:22], remote_jid)
            self._persist_and_repaint_edit(existing, remote_jid)
            return

        def _text_of(m):
            mo = m.get("message") or {}
            if not isinstance(mo, dict):
                return None
            if "conversation" in mo:
                return mo.get("conversation") or ""
            if "extendedTextMessage" in mo:
                return (mo.get("extendedTextMessage") or {}).get("text") or ""
            return None  # not a text message — never treat as an edit

        # Meta AI streams its answer: a reply stored before its words arrived
        # is an empty "rich_response" (websocket_client.py normalizes only one
        # that already has text), and the filled copy comes back under the same
        # id. That is the message finishing, not an edit -- take it whole and
        # do not mark it "edited".
        if (existing.get("messageType") == "rich_response"
                and not (existing.get("message") or {})
                and _text_of(incoming)):
            existing["message"] = incoming.get("message")
            existing["messageType"] = incoming.get("messageType", "conversation")
            self._persist_and_repaint_edit(existing, remote_jid)
            return

        old_text = _text_of(existing)
        new_text = _text_of(incoming)
        if old_text is not None and old_text == new_text:
            # Same text, but WhatsApp now says it was edited: a sync applied the
            # new text before the marker existed (every install that synced
            # before server_marks_edited() was read), and this echo is the
            # only thing that will say so until the next sync.
            if incoming.get("_edited") and not existing.get("_edited"):
                existing["_edited"] = True
                self._persist_and_repaint_edit(existing, remote_jid)
            return
        if old_text is None or new_text is None:
            return

        existing["message"]     = incoming.get("message")
        existing["messageType"] = incoming.get("messageType", existing.get("messageType"))
        existing["_edited"]     = True

        def _bg_persist():
            try:
                self.db.insert_message(remote_jid, existing)
            except Exception as e:
                logging.error(f"[_apply_possible_edit] Failed to persist edited message: {e}")
        self._msg_bg_executor.submit(_bg_persist)

        if hasattr(self, "conversations_panel"):
            wx.CallAfter(self.conversations_panel.refresh_active_conversation_messages)
        self._schedule_set_chats()

    def _live_events_ready(self) -> bool:
        """True once it is safe to let a live WebSocket event mutate self.chats.

        Two separate conditions, both required:

        1. The UI must exist (_ui_ready_event) — see on_new_message()'s old
           comment: a reused pairing socket can deliver events via
           wx.CallAfter before MainWindow.__init__ has finished creating
           self.db/self.chats, which used to crash deep inside save_data().

        2. A sync must have actually begun at least once this session
           (_sync_ever_started, latched True by start_sync() and never reset).
           Between "window shown" and "sync thread actually started" —
           the "preparando para sincronizar" window — the conversation list
           is expected to show only what was already on disk (or be empty
           for a fresh pairing). Accepting live messages during that gap let
           them sneak into the list ahead of the sync that is about to fetch
           the complete, authoritative state anyway — redundant at best, and
           a source of duplicate/out-of-order entries at worst. Once the
           sync thread is actually running, live events are safe to accept
           again: sync_chat_messages() already merges them with whatever the
           REST fetch returns instead of one silently overwriting the other.

           This deliberately does NOT check _sync_completed/_initial_sync_running,
           which is what it used to do.  Both are False in the very common case
           of a sync that ran to the end and still marked itself incomplete
           (see chat_list_settled in _run_sync: a cold WhatsApp Web store that
           only fills in on the last list-chats attempt produces exactly that,
           on every fresh pairing).  start_sync()'s finally clause then clears
           _initial_sync_running, and the app went completely deaf to live
           events — no new messages in the list, no reordering, no pushName
           learned for group participants ("Participante sem nome" coming
           back), no LID mappings — until the health checker's next retry,
           up to 10 minutes later.  The gap this condition exists to close is
           the one *before* the first sync; once one has started, dropping
           events buys nothing, because there is no longer a pending fetch
           guaranteed to re-deliver them.
        """
        if not self._ui_ready_event.is_set():
            return False
        return getattr(self, "_sync_ever_started", False)

    def _redirect_self_chat_artifact(self, remote_jid: str, key: dict, from_me: bool):
        """Detect and redirect a WPPConnect/Baileys self-chat sync artifact.

        WPPConnect/Baileys occasionally reports one of our own sends (seen
        with self-chat text, audio and documents) tagged with an identity
        that isn't our real phone JID, in one of two shapes:

          (a) "participant" (the actual sender/author, per wa-js semantics)
              has the same digits as "remoteJid" (the chat). For a real
              GROUP, remoteJid is the group's own independently-allocated
              ID, never equal to any participant's JID — so this overlap
              alone proves it's not a real group, regardless of whatever
              fromMe flag WPPConnect attached to the event (observed: it
              can arrive as fromMe=False, producing a bogus "new message
              from an unnamed participant" notification). For a bare,
              not-yet-resolved @lid or @s.whatsapp.net remoteJid, the same
              overlap is only unambiguous when fromMe is already True —
              for a real 1:1 chat, an incoming (fromMe=False) message's
              participant legitimately mirrors remoteJid (the sender IS
              the chat), so that combination must NOT be redirected.

          (b) remoteJid is suffixed "@g.us" but its digits are simply our
              own phone number (with the Brazilian 9th-digit variant) — no
              real group JID is ever shaped like a plain phone number.

        Either shape otherwise spawns an unnamed phantom "group"/duplicate
        chat that (1) duplicates a message already stored under "Eu" and
        (2) can't be cleanly identified/deleted afterwards.

        Returns (remote_jid, from_me), redirected/forced True when either
        shape matched, unchanged otherwise.

        Shared by on_new_message() (the live path) and
        on_historical_message() (history sync) — the latter used to have no
        guard at all, so a fake self-chat arriving through a history-sync
        batch sat in the chat list unfiltered until the next full
        deduplicate_chats() pass happened to run. deduplicate_chats() keeps
        its own equivalent guard pass for chats that slipped through before
        either of these ran (e.g. a session resumed from a store that
        already had one saved) — all three must keep agreeing on what
        counts as a fake self-chat.

        That third one doesn't call this method — it needs a verdict about a
        whole chat's stored records, not about one arriving message — so it
        restates the same condition instead. Keeping the two in step matters
        in both directions: a shape that pass accepts but these funnels
        don't is a chat wiped on every launch and recreated by live traffic
        in between, and a shape these reject but that pass doesn't is one
        already saved in messages.db that nothing ever cleans up.
        """
        participant_raw = key.get("participant") or ""
        remote_digits = remote_jid.split("@", 1)[0]
        part_digits = participant_raw.split("@", 1)[0] if participant_raw else ""
        my_jid = self._normalize_jid(getattr(self, "my_jid", ""))
        my_lid = getattr(self, "my_lid", "")
        is_group_jid = remote_jid.endswith("@g.us")

        digits_self_referential = bool(part_digits and remote_digits == part_digits)
        is_self_referential = digits_self_referential and (
            is_group_jid or (from_me and my_jid and self._phone_digits_equivalent(remote_digits, my_jid.split("@", 1)[0]))
        )
        is_self_phone_group = bool(
            is_group_jid and my_jid
            and self._phone_digits_equivalent(remote_digits, my_jid.split("@", 1)[0])
        )

        if is_self_referential or is_self_phone_group:
            from_me = True
            if my_jid:
                remote_jid = my_jid
            elif my_lid:
                remote_jid = my_lid
            else:
                remote_jid = participant_raw or remote_jid
        elif (
            my_jid and remote_jid != my_jid
            and remote_jid.endswith("@s.whatsapp.net")
            and self._is_self_jid(remote_jid)
        ):
            remote_jid = my_jid

        return remote_jid, from_me

    def on_new_message(self, msg: dict):
        """
        Called on the main thread (via wx.CallAfter) when a new message
        arrives via the messages.upsert WebSocket event.
        Adds the message to local storage, updates the UI, and sends a
        notification if appropriate.
        """
        # Popped here, not at the notification point: a message that never
        # notifies (our own echo, a muted chat, a system event) would otherwise
        # carry this straight into the stored record.
        arrived_at = msg.pop("_arrived_at", None)
        # See _live_events_ready() for why this must be checked before
        # touching self.chats/self.db at all. Safe to drop unconditionally:
        # the sync that is either about to start or already running fetches
        # the complete current chat/message state regardless, so nothing
        # arriving this early is ever actually lost.
        if not self._live_events_ready():
            return
        key        = msg.get("key", {})
        from_me    = key.get("fromMe", False)
        remote_jid = self._normalize_jid(key.get("remoteJid", ""))
        msg_id     = key.get("id", "")

        # If the message is from ourselves, ensure from_me is True
        sender = key.get("participant") or key.get("remoteJid") or ""
        if sender and self._is_self_jid(sender):
            from_me = True

        if not remote_jid:
            return

        # ── Guard against self-chat multi-device-sync artifacts ─────────────
        # See _redirect_self_chat_artifact()'s own docstring for the two
        # shapes this catches — shared with on_historical_message() so both
        # the live and history-sync paths agree on what counts as fake.
        # Opportunistically learn my_lid from case (a) first, so later
        # messages resolve immediately via _is_self_jid() without waiting on
        # resolve_self_lid()'s async API round-trip.
        participant_raw = key.get("participant") or ""
        # Normalize before using as a redirect target below — my_jid can be
        # in raw "@c.us" form early in a session (set directly from the
        # host-device API response, before resolve_self_lid() gets a chance
        # to normalize it), and redirecting to it as-is created yet another
        # duplicate "Eu" chat under @c.us instead of the canonical @s.whatsapp.net one.
        my_jid = self._normalize_jid(getattr(self, "my_jid", ""))
        my_lid = getattr(self, "my_lid", "")

        # A fromMe message's own "participant" field always identifies us —
        # wa-js only populates it to tag the sender within a group, and the
        # sender of our own outgoing message is always us. Learn my_lid from
        # this far more common signal (any ordinary group message we send),
        # not just the rarer self-referential artifacts checked below, so
        # _is_self_jid()/self_reference_label() resolve correctly (e.g. for
        # quoted-reply headers) from the first group message sent this
        # session — without waiting on resolve_self_lid()'s async API call,
        # which otherwise left _get_participant_name() falling through to a
        # saved contact name (e.g. a self-addressed contact literally named
        # "Eu") instead of honouring the "Como se referir a mim?" setting.
        # A forwarded copy is the exception to the comment above: its own
        # participant/key fields can carry residual provenance about
        # whoever originally sent the message being forwarded, not about
        # the forward action itself (which is always us, on a fromMe
        # message). Learning my_lid from that residual value here corrupted
        # self-identity globally — reported live as a forwarded contact's
        # OWN later messages, in a completely different chat, rendering as
        # "Eu" everywhere (last-message preview, quoted-reply header, ...)
        # the instant one of their messages got forwarded to "Mensagens
        # para mim". Recognizable via the same contextInfo.isForwarded flag
        # _is_message_forwarded() already checks for other purposes.
        if (from_me and participant_raw.endswith("@lid") and not getattr(self, "my_lid", "")
                and my_lid != participant_raw and not is_message_forwarded(msg)):
            self.my_lid = my_lid = participant_raw
            if my_jid:
                self.register_jid_mapping(participant_raw, my_jid)

        remote_jid, from_me = self._redirect_self_chat_artifact(remote_jid, key, from_me)

        # Learn/update presence pushName map from incoming message
        if not from_me and self._learn_sender_name(msg):
            self._schedule_save(contacts_dirty=True)

        # Extract mapping and mentions from incoming messages
        self._extract_lid_mapping(msg)

        # A `ciphertext` is not a message — it is WhatsApp Web saying "something
        # arrived and I have not decrypted it yet". The real one follows under
        # the SAME key.id (2.5 s and 4.2 s in the two occurrences measured on a
        # live install), and storing the placeholder is what makes that second
        # delivery look like a duplicate.
        #
        # The damage is the whole notification, not a cosmetic one. Measured:
        #
        #   18:30:49 on_messages_upsert id=ACBF…B49F type=ciphertext
        #   18:30:49 [unread] chats-update in: …936700@g.us unread=1 previous=0
        #   18:30:49 [unread] …936700@g.us: no change after discounting
        #                     non-countable messages (already 0, previous=0)
        #   18:30:51 on_messages_upsert id=ACBF…B49F type=audioMessage
        #
        # WhatsApp said unread=1; the placeholder is not countable (correctly —
        # it has no content), so the badge was discounted back to zero, and the
        # real message 2.5 s later hit the same-id dedup and was routed to
        # _apply_possible_edit(), which never announces anything. The user got
        # a voice message with no sound, no badge and no screen-reader
        # announcement, and the row read "Mensagem incompatível" until a later
        # poll rewrote it.
        #
        # Dropping it costs nothing that is not already lost: the placeholder
        # renders as "Mensagem incompatível", and on the path where the
        # decrypted copy never arrives at all the 60 s poll is what recovers
        # the message today either way. _extract_lid_mapping() above still runs
        # first, since the envelope's addressing is real even when its content
        # is not.
        if MessageEventsMixin._is_undecrypted_placeholder(msg):
            logging.info(
                "[on_new_message] %s: ignoring the ciphertext placeholder for %s "
                "— waiting for the decrypted copy under the same id.",
                remote_jid, (key or {}).get("id", "")[:22])
            return

        # An edit's protocol message carries a NEW id, so every id-based check
        # below would miss it and store it as its own row. Dropped here, before
        # anything can create or restore a chat on its behalf — see
        # core/message_edit.py.
        if self._drop_protocol_edit(remote_jid, msg):
            return

        # Statuses (stories) arrive as messages on status@broadcast; they are
        # stored in _status_updates for the Status tab, not in a conversation.
        # Newsletter (channels) are read-only and also ignored.
        if remote_jid.endswith("@broadcast"):
            self._store_status_update(msg)
            return
        if remote_jid.endswith("@newsletter"):
            return

        # Reaction messages only update the live display of an existing message;
        # they must not be added to records or unread counts. They DO, however,
        # trigger a notification when someone reacts to one of *your* messages.
        if msg.get("messageType") == "reactionMessage":
            if reaction_targets_status(msg):
                # A reaction to one of OUR statuses has no message in this
                # conversation to attach itself to — the status lives in
                # _status_updates and the Status tab, never in a chat. The
                # transient handling below is right for every other reaction
                # and is exactly what made these vanish: the notification fired
                # and the chat-list preview updated, so the user saw something
                # arrive, and then opening the conversation showed nothing at
                # all because no record was ever stored. Reported three times,
                # in the same words: "respondeu meu status com um emoji, a
                # mensagem apareceu, quando fui abrir desapareceu."
                #
                # Keep the notification and the preview, then fall through to
                # normal storage so it survives as its own timeline entry —
                # which is also how WhatsApp itself shows it. Deliberately NOT
                # calling conversations_panel.on_incoming_message() here: the
                # normal path below calls it once the record exists, and
                # calling it twice showed the reaction twice.
                self._maybe_notify_reaction(remote_jid, msg)
                self._track_last_reaction(remote_jid, msg)
            else:
                if hasattr(self, "conversations_panel"):
                    self.conversations_panel.on_incoming_message(remote_jid, msg)
                self._maybe_notify_reaction(remote_jid, msg)
                self._track_last_reaction(remote_jid, msg)
            # Own reactions refresh the chat list explicitly from
            # _on_own_reaction_sent() right after sending. A reaction from
            # someone else only ever arrives here, so without this the chat
            # list's last-message preview never picked up
            # chat["_last_reaction"] until some unrelated event happened to
            # trigger a refresh.
            self._schedule_set_chats()
            if not reaction_targets_status(msg):
                return

        # ── Resolve canonical JID, merging @lid duplicates ───────────────────
        # Handles both API key formats and all combinations of which entries exist:
        #   OLD format: remoteJid=@lid,  remoteJidAlt=@s.whatsapp.net
        #   NEW format: remoteJid=phone, remoteJidAlt=@lid
        #   Cache-only: no remoteJidAlt, but @lid known from prior messages
        alt_jid = self._normalize_jid(key.get("remoteJidAlt", ""))

        if remote_jid.endswith("@lid"):
            # OLD format — redirect to canonical phone JID
            phone_jid = (
                alt_jid if alt_jid.endswith("@s.whatsapp.net")
                else getattr(self, "_lid_to_phone", {}).get(remote_jid, "")
            )
            if phone_jid:
                self._merge_lid_into_phone(remote_jid, phone_jid)
                remote_jid = phone_jid
            else:
                self._queue_lid_resolutions([remote_jid])
        elif alt_jid.endswith("@lid"):
            # NEW format — merge the @lid side into the phone chat
            self._merge_lid_into_phone(alt_jid, remote_jid)
        elif remote_jid.endswith("@s.whatsapp.net"):
            # No remoteJidAlt — consult cache for any @lid counterpart
            lid_jid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
            if lid_jid:
                self._merge_lid_into_phone(lid_jid, remote_jid)

        # A conversation the user deleted comes back the moment it receives a
        # new message — that is what WhatsApp itself does.  Without lifting the
        # deleted flag here the chat would be re-created in self.chats but
        # filtered out of every list, so the message would arrive invisibly.
        if remote_jid in self._deleted_chats:
            self._deleted_chats.discard(remote_jid)
            alt = (getattr(self, "_phone_to_lid", {}).get(remote_jid)
                   or getattr(self, "_lid_to_phone", {}).get(remote_jid))
            if alt:
                self._deleted_chats.discard(alt)
            if hasattr(self, "db") and self.db is not None:
                self.db.set_metadata_json("deleted_chats", list(self._deleted_chats))
            logging.info("[on_new_message] %s was deleted locally — restored by a new message.",
                         remote_jid)

        # ── Ensure the chat record exists ─────────────────────────────────────
        if remote_jid not in self.chats:
            push_name = "" if remote_jid.endswith("@g.us") else msg.get("pushName", "")
            self.chats[remote_jid] = {
                "remoteJid":   remote_jid,
                "unreadCount": 0,
                "pushName":    push_name,
                "messages":    {"messages": {
                    "records":     [],
                    "total":       0,
                    "pages":       1,
                    "currentPage": 1,
                }},
                # This chat is brand new to WinZapp — "unreadCount": 0 above is
                # an assumption, not a fact: the phone may have had a real
                # backlog for it (e.g. a chat that already had 230 unread
                # before this session even started) that get_remote_chats()
                # simply hasn't fetched yet. Live increments below build on top
                # of that assumed 0, so a toast fired before the real count
                # lands would announce "1 unread"/"2 unread" while the true
                # total is far higher. Cleared once a real chat-list sync
                # merges its own unreadCount for this jid (see get_remote_chats())
                # — until then, the notification code omits the numeric badge
                # rather than show a number that's actively misleading.
                "_unread_count_unsynced": True,
            }
            if remote_jid.endswith("@g.us"):
                # Unlike chats created by get_remote_chats() at sync time, a
                # group first seen via a live socket event has no name yet —
                # without this it stays "unnamed" until the next full sync.
                self._resolve_group_name_async(remote_jid)

        chat = self.chats[remote_jid]
        # live=True: this is the funnel for events happening now, the only one
        # worth spending a /group-info request on when the notification itself
        # arrives without the new name (on_historical_message deliberately
        # does not — see _apply_group_subject_change).
        self._apply_group_subject_change(remote_jid, chat, msg, live=True)
        self._refresh_mention_cache_on_membership_change(remote_jid, msg)
        self._apply_group_settings_change(remote_jid, chat, msg)

        msg_ts = int(msg.get("messageTimestamp", 0) or msg.get("t", 0) or time.time())
        if msg_ts > 1_000_000_000_000:
            msg_ts //= 1000
        # System events (group join/leave, settings changes, revokes, ...)
        # must not bump the chat's sort timestamp — see is_countable_message().
        # Without this an old, already-read conversation jumped back to the
        # top of the list purely because a group's metadata changed.
        if is_countable_message(msg) and msg_ts > int(chat.get("t", 0) or 0):
            chat["t"] = msg_ts

        # ── Avoid duplicate insertions or resolve pending ones ────────────────
        records = (
            chat.setdefault("messages", {})
                .setdefault("messages", {})
                .setdefault("records", [])
        )
        if from_me:
            # Match the echo to the pending virtual message it actually
            # confirms, not just "whichever pending message we saw first".
            # When two sends are in flight at once (e.g. a text message
            # still awaiting its HTTP response while a voice message is
            # fired off right after), the previous "first pending" pick
            # would happily hand a text message the real ID of an unrelated
            # audio message (and vice versa) — corrupting both: the text
            # message freezes with no status updates (WhatsApp's status
            # events for its real ID never find a matching record), the
            # audio message's real ID collides with another entry's, its
            # sent sound fires for the wrong message, and the recording
            # file gets renamed onto the wrong ID so playback later loads
            # someone else's audio. Restrict candidates to pending messages
            # of the same type so unrelated messages can no longer swap IDs.
            incoming_type = msg.get("messageType", "")
            _text_types = ("conversation", "extendedTextMessage")
            pending_msg = None
            # A message whose id some record already carries was matched to
            # its pending row earlier. Running the type search again for a
            # redelivery of that same echo — or a delayed one that lands after
            # a *second* same-type message started sending in the meantime —
            # would pick the first still-pending record of that type and hand
            # it an id belonging to a different message entirely. Skipping the
            # search lets it fall through to the exact id/edit check below,
            # which is unambiguous.
            #
            # The question asked is deliberately "has a record already claimed
            # this id?", not "is this id in _own_sent_ids?". The latter races
            # the very notification it is meant to follow: MessageQueue calls
            # _remember_own_sent_id(real_id) one line BEFORE the wx.CallAfter
            # that eventually stamps the id onto the record (see that method's
            # docstring — "the echo routinely arrives first"). In that window
            # the id is in the set but no record carries it, so the type search
            # would be skipped, the exact-id check below would find nothing,
            # and the echo would be appended as a brand new record — leaving
            # two records with the same key.id, which is the duplicate this
            # guard exists to prevent.
            already_resolved = bool(msg_id) and any(
                (r.get("key") or {}).get("id") == msg_id for r in records
            )
            if not already_resolved:
                for r in records:
                    if not r.get("_local_pending"):
                        continue
                    r_type = r.get("messageType", "")
                    if incoming_type in _text_types:
                        if r_type not in _text_types:
                            continue
                    elif r_type != incoming_type:
                        continue
                    pending_msg = r
                    break
            if pending_msg:
                # Found the corresponding pending message: update it and skip appending a duplicate
                pending_msg["_local_pending"] = False
                local_id = pending_msg.get("_local_id")
                pending_msg["key"]["id"] = msg_id
                pending_msg["messageTimestamp"] = msg.get("messageTimestamp", pending_msg["messageTimestamp"])
                # The virtual message built before sending never carries a
                # "participant" (it doesn't know its own WhatsApp identity),
                # but our own group messages are indexed in WPPConnect's
                # store under participant=our own JID (see _serialize_msg_id).
                # Without backfilling it from the real echo here, replying-
                # to/quoting a message sent seconds ago falls back to a
                # guessed participant (my_jid) that doesn't match what the
                # live store actually indexed it under whenever the account
                # is on @lid — "Message ... not found" — until a later full
                # resync overwrites this record with the API's copy anyway.
                if key.get("participant"):
                    pending_msg["key"]["participant"] = key.get("participant")
                
                # Remove any existing record with the same real ID (e.g. from API
                # sync) *including* pending_msg itself (its key was just updated
                # to msg_id), then re-append it at the end.  The old filter kept
                # pending_msg via `r is pending_msg`, which left it in the list
                # AND then appended it again — creating a duplicate entry.
                if msg_id:
                    records[:] = [r for r in records
                                  if r.get("key", {}).get("id") != msg_id]
                records.append(pending_msg)
                
                def _bg_insert_pending():
                    try:
                        self.db.insert_message(remote_jid, pending_msg)
                    except Exception as e:
                        logging.error(f"[on_new_message] Failed to insert pending message to DB: {e}")
                self._msg_bg_executor.submit(_bg_insert_pending)
                
                with self._own_sent_ids_lock:
                    self._own_sent_ids.add(msg_id)
                    if len(self._own_sent_ids) > 500:
                        self._own_sent_ids.discard(next(iter(self._own_sent_ids)))
                
                if hasattr(self, "conversations_panel"):
                    wx.CallAfter(self.conversations_panel._mark_message_sent, local_id, real_id=msg_id)
                
                self._schedule_save(dirty_jid=remote_jid)
                self._schedule_set_chats()
                return

        if msg_id:
            for index, existing in enumerate(records):
                if existing.get("key", {}).get("id") == msg_id:
                    # A call record is rewritten when the call ends (Ongoing ->
                    # its outcome and duration, core/call_log.py); the newer
                    # state replaces the stored one in place, silently.
                    if call_log_supersedes(existing, msg):
                        self._fill_stored_placeholder(existing, msg, remote_jid,
                                                      what="newer state of call record")
                        self._watch_pending_call_log(remote_jid, existing)
                        self._refresh_calls_tab()
                        return
                    # A text recovered from a reply's quote is the replier's
                    # claim, so the real copy replaces it like a placeholder
                    # rather than being compared as an edit (core/quote_recovery.py).
                    if not awaits_real_copy(existing):
                        self._apply_possible_edit(existing, msg, remote_jid)
                        return  # already stored (edited in place if content changed)
                    # A sync stored WhatsApp Web's placeholder for this id and
                    # this is its decrypted copy (see _resolved_placeholder_is_fresh).
                    ws = getattr(self, "ws", None)
                    connected_at = getattr(ws, "_connect_time", None) or time.time()
                    if not MessageEventsMixin._resolved_placeholder_is_fresh(
                            records, index, msg, connected_at):
                        self._fill_stored_placeholder(existing, msg, remote_jid)
                        return
                    # Fresh: the copy continues as the new message it is (badge,
                    # sound, announcement), but AS the placeholder's own record:
                    # the open list's row is that same object, and its dedup
                    # refuses a second record under this id. Taken out here and
                    # appended again below; its DB insert replaces the row.
                    msg = MessageEventsMixin._adopt_decrypted_copy(existing, msg)
                    del records[index]
                    if hasattr(self, "conversations_panel"):
                        wx.CallAfter(self.conversations_panel.refresh_messages_if_changed)
                    break



        # Ignore stale re-deliveries of messages the user already cleared.
        if self._is_cleared_message(remote_jid, msg):
            return

        # A message we just forwarded arrives here with no duration on it —
        # graft back the length taken from the source before it is stored or
        # rendered (see _expect_forwarded_duration).
        self.apply_forwarded_duration(msg)

        # Slim any bloated quoted-message payload before persisting.
        prune_message_record(msg)
        records.append(msg)
        if len(records) > _MAX_RESIDENT_MESSAGES_PER_CHAT:
            del records[:len(records) - _MAX_RESIDENT_MESSAGES_PER_CHAT]

        def _bg_insert_msg():
            try:
                self.db.insert_message(remote_jid, msg)
            except Exception as e:
                logging.error(f"[on_new_message] Failed to insert message to DB: {e}")
        _insert_fut = self._msg_bg_executor.submit(_bg_insert_msg)
        if remote_jid.endswith("@lid"):
            # This message is being filed under a JID that a later merge will
            # rename (the @lid is only resolved to a phone JID once
            # /contact/pn-lid answers). _merge_lid_into_phone() waits on this
            # future so it never moves the chat out from under an insert that
            # hasn't landed yet — that leaves the message under a JID nothing
            # queries again, which is exactly how a first message from a
            # contact could vanish on opening the conversation.
            self._pending_lid_inserts[remote_jid] = _insert_fut

        # A reply carries the text it quotes: an "Aguardando mensagem" it
        # answers can show that text now (core/quote_recovery.py).
        self._recover_quoted_placeholder(remote_jid, records, msg)
        if is_call_log(msg):
            self._watch_pending_call_log(remote_jid, msg)
            self._refresh_calls_tab()

        # ── Update unread count (only for messages we received) ───────────────
        # System events never count as unread — see is_countable_message().
        if not from_me and is_countable_message(msg):
            # Don't increment unread for the conversation already open — it is
            # immediately visible to the user and will be marked as read.
            _cp   = getattr(self, "conversations_panel", None)
            _open = (
                _cp is not None
                and _cp.conversation is not None
                and _cp.conversation.get("remoteJid") == remote_jid
            )
            _visible = (
                not getattr(self, "_window_hidden", False)
                and self.IsShown()
                and not self.IsIconized()
            )
            if not (_open and _visible):
                chat["unreadCount"] = int(chat.get("unreadCount") or 0) + 1
                # Track how many messages have genuinely arrived since the
                # last local mark-as-read, so on_chat_unread_update() can
                # tell a real new-unread total apart from a stale server
                # count that still includes messages we already read locally
                # but the server hasn't acknowledged as read yet.
                #
                # Note what this line does NOT establish: it creates the entry
                # for a chat that has never been read here either, where the
                # number means "arrivals since this process started" and is no
                # ceiling for anything. Only mark_conversation_as_read() makes
                # it a count since a read — which is why the clamp in
                # on_chat_unread_update() asks _unread_anchored_to_local_read()
                # rather than trusting a nonzero entry on its own.
                if not hasattr(self, "_new_since_read"):
                    self._new_since_read = {}
                self._new_since_read[remote_jid] = self._new_since_read.get(remote_jid, 0) + 1
        elif from_me and int(chat.get("unreadCount") or 0) > 0:
            # Our own message, and it matched no pending virtual message —
            # a local send never reaches this far (see the from_me echo
            # matching above, which returns). So this was sent from the
            # phone or another linked device, which means the chat was read
            # there: clear the badge, exactly as WhatsApp itself does.
            # mark_conversation_as_read() is reused rather than assigning 0
            # here so the read also gets the /send-seen call and the
            # _locally_read_at bookkeeping that stops the 60s list-chats
            # poll from resurrecting the count a minute later.
            if own_message_marks_chat_read(records, msg):
                logging.info(
                    "[on_new_message] Own message from another device in %s — "
                    "clearing unread (%s).", remote_jid, chat.get("unreadCount"),
                )
                # Called directly, like every other call site: this runs on
                # the main thread already (on_new_message is always invoked
                # via wx.CallAfter), and mark_conversation_as_read() already
                # backgrounds its own /send-seen HTTP call internally. Wrapping
                # the whole call in an extra thread bought nothing and mutated
                # self.chats/self._locally_read_at off the main thread instead,
                # racing with the very same dicts this handler mutates.
                self.mark_conversation_as_read(remote_jid, True)

        # ── Persist in background — debounced so rapid bursts produce one write ─
        self._schedule_save(dirty_jid=remote_jid)

        # ── Update conversation list UI ───────────────────────────────────────
        # A new message moves this chat to the top of its group and changes its
        # preview — and nothing else in the list changes. move_chat_row_to_top()
        # does exactly that, comparing this chat's sort key against the current
        # top of its group so the claim is checked rather than assumed. The
        # debounced full recompute stays as the fallback: it re-resolves the
        # display name and rebuilds the row text of EVERY chat, which on an
        # account with several hundred of them is what made a new message take
        # seconds to show up in the list.
        if not self.move_chat_row_to_top(remote_jid):
            self._schedule_set_chats()

        # ── Add message to the open conversation panel (if visible) ──────────
        if hasattr(self, "conversations_panel"):
            self.conversations_panel.on_incoming_message(remote_jid, msg)

        # ── Download media in background ──────────────────────────────────────
        media_types = {"audioMessage", "imageMessage", "videoMessage",
                       "documentMessage", "stickerMessage"}
        if msg.get("messageType") in media_types:
            self._msg_bg_executor.submit(self.sync_if_media, msg)

        # ── Send notification ─────────────────────────────────────────────────
        if from_me:
            return
        # System events never trigger a sound/toast/AO2 announcement either —
        # see is_countable_message().
        if not is_countable_message(msg):
            return

        # Guard: do not play sound or show notification for messages older than 60 seconds
        ts = msg.get("messageTimestamp")
        if ts:
            try:
                conn_time = getattr(self.ws, "_connect_time", time.time()) if self.ws else time.time()
                cutoff = conn_time - 60
                if int(ts) < cutoff:
                    return
            except (TypeError, ValueError):
                pass

        # A reply to one of my own messages or an explicit @-mention always
        # breaks through a muted chat's suppression, everywhere (background
        # toast or foreground sound/speech) — the user still needs to know
        # about those regardless of the mute. Ignoring the mute entirely for
        # everything else is either always-on ("keep_muted_chats_silent_
        # when_open", default True — the original behavior) or only lifted
        # while that exact chat is the one currently open and the window is
        # focused (setting off) — see the two mute checks below.
        muted = self.is_chat_muted(remote_jid)
        locked = self.is_chat_locked(remote_jid)
        # A locked chat may also retain WhatsApp's archive flag, but the vault
        # has its own privacy-preserving notification policy and takes
        # precedence over archived-chat silence.
        archived = self.is_chat_archived(remote_jid) and not locked
        priority = muted and self._is_reply_or_mention_of_me(msg, remote_jid)
        if muted and not priority and self.settings.get("general", {}).get(
            "keep_muted_chats_silent_when_open", True
        ):
            return

        from core.notification_manager import (
            format_notification_title, format_notification_body,
            format_foreground_sender, format_locked_notification,
        )

        body  = format_notification_body(msg, self, self.i18n)

        # Check if the WinZapp window is currently active/focused
        window_active = (
            not getattr(self, "_window_hidden", False)
            and self.IsShown()
            and not self.IsIconized()
            and self.IsActive()
        )

        if window_active:
            speech = self.settings.get("speech_content", {})
            # Determine if the incoming message is for the currently-open conversation
            cp = getattr(self, "conversations_panel", None)
            current_jid = (
                cp.conversation.get("remoteJid", "")
                if cp is not None and cp.conversation is not None
                else ""
            )
            is_current_conv = (
                cp._matches_open_conversation(remote_jid)
                if cp is not None and hasattr(cp, "_matches_open_conversation") and cp.conversation is not None
                else (current_jid == remote_jid and bool(current_jid))
            )

            # Muted + not the open conversation: stay silent even with the
            # window active (the "keep silent when open" setting only ever
            # exempts the chat that is actually open right now) — unless
            # it's a reply/mention, which always gets through.
            if muted and not priority and not is_current_conv:
                return

            # Archived + not the open conversation: stay silent even with the
            # window active (archived chats only play sound / speak when the
            # user currently has that exact conversation open and focused).
            if archived and not is_current_conv:
                return

            if locked and not is_current_conv:
                self.message_foreground_sound.play()
                if speech.get("speak_other_conv_messages", True):
                    private_title, private_body = format_locked_notification(
                        effective_unread_count(chat), self.i18n, self.app_name
                    )
                    self.output(f"{private_title}: {private_body}")
                return

            if is_current_conv:
                # Scenario 1: message in the ACTIVE conversation
                # Play current-conversation sound (always), speak "Sender: body"
                # via AO2 only when speak_active_conv_messages is on. Neither of
                # these is the background toast this function's general.
                # notifications_enabled setting is meant to gate — see below.
                self.message_current_sound.play()
                if speech.get("speak_active_conv_messages", True):
                    sender = format_foreground_sender(msg, self, self.i18n)
                    self.output(f"{sender}: {body}")
                # Mark the active conversation as read immediately, but only if the
                # window has been focused for at least 5 seconds (to prevent marking
                # startup/offline messages as read automatically).
                last_act = getattr(self, "_last_activation_time", 0)
                if time.time() - last_act >= 5.0:
                    threading.Thread(
                        target=self.mark_conversation_as_read,
                        args=(remote_jid, True),
                        daemon=True,
                    ).start()
            else:
                # Scenario 2: message in a DIFFERENT conversation (window active)
                # Play foreground sound (always), speak "Nova mensagem de X: body"
                # via AO2 only when speak_other_conv_messages is on.
                self.message_foreground_sound.play()
                if speech.get("speak_other_conv_messages", True):
                    title = format_notification_title(msg, self, self.i18n)
                    spoken = self.i18n.t("fg_new_msg").format(name=title) + f": {body}"
                    self.output(spoken)
            return  # never send system toast when window is active

        # Window is not focused: a muted chat is never "open" from here, so
        # this is where "keep silent when open" (setting off) actually stops
        # exempting it — the chat being open only ever matters while the
        # window is active. A reply/mention still gets through, same as above.
        if muted and not priority:
            return

        # An archived chat in the background never sends a toast or sound/speech.
        if archived:
            return

        # Send system toast notification. general.notifications_enabled is
        # the ONLY thing this controls — it used to gate the whole function
        # above this point too, silently killing the foreground sounds/
        # announcements above (and, when a burst of messages included one in
        # a muted/archived chat before this point, nothing else) whenever the
        # user turned it off.
        if not self.settings.get("general", {}).get("notifications_enabled", True):
            return
        if locked:
            title, body = format_locked_notification(
                effective_unread_count(chat), self.i18n, self.app_name
            )
        else:
            title = format_notification_title(msg, self, self.i18n)

        # The toast is the ONLY announcement a backgrounded message gets.
        # Speaking it through AO2 here as well used to make every background
        # message arrive twice: once as "Nova mensagem de X: ..." straight
        # from accessible_output2, and again as the screen reader read the
        # toast banner Windows had just put on screen. That AO2 call was
        # added when the banner could silently never render at all (an
        # unregistered AUMID in dev mode — see _setup_toaster()); now that
        # the toast reliably shows in both source and frozen builds, the
        # unconditional announcement is pure duplication.
        #
        # The safety net stays, just moved to where the outcome is actually
        # known: NotificationManager._dispatch() speaks the message itself
        # when — and only when — it produced no banner, and
        # should_speak_background_message() covers the cases where no toast
        # is even attempted — otherwise a user with toasts off would be left
        # with nothing but the sound cue.
        from core.notification_manager import (
            announce_background_message, should_speak_background_message,
        )

        if should_speak_background_message(
            self.settings, hasattr(self, "notification_manager")
        ):
            announce_background_message(self, self.i18n, title, body)
            return

        # The unread suffix is deliberately NOT baked in here — see
        # NotificationManager._dispatch(), which appends it fresh right
        # before the toast is actually shown. A burst of messages queues
        # one notification per message, but _coalesce_pending() only
        # ever displays the newest one; that toast's body (formatted
        # here, at enqueue time) could still be showing the unreadCount
        # from the FIRST message of the burst — e.g. "1" — while several
        # more arrived (and incremented the real count) before the
        # worker thread got around to actually dispatching it. Reported
        # live as the toast's "✉️ N não lidas" line reading much lower
        # than what Alt+3 announced moments later in the same chat.
        if arrived_at is not None:
            # Everything WinZapp does between the socket event and handing the
            # toast to Windows. Anything beyond this is the OS notification
            # pipeline and the screen reader's own queue, which is exactly the
            # split the "demora 3 segundos pra ler" report needed and nobody
            # could make: no timestamp existed anywhere on this path.
            logging.info(
                "[notif-timing] %s: %.0fms from arrival to enqueue.",
                remote_jid, (time.monotonic() - arrived_at) * 1000,
            )
        self.notification_manager.send(title, body, remote_jid, msg_key=msg.get("key"))

    def _learn_sender_name(self, msg: dict) -> bool:
        """Remember the pushName a message carries for its sender JID.

        In a group, ``key.participant`` is very often a bare ``@lid`` that maps
        to no phone number we know: the participant is not in the address book,
        and group messages carry no ``remoteJidAlt`` bridge field the way 1:1
        chats do.  When that happens every name lookup fails and the sender
        shows up as "Participante sem nome".

        The message itself is the one place the name *is* available — WhatsApp
        ships the sender's pushName on it.  Recording it against the JID makes
        that name available to every later lookup (chat-list previews,
        notifications, the message list), including for messages of types that
        arrive with no pushName of their own.

        Returns True when something new was learned, so callers can persist.
        """
        key = msg.get("key") or {}
        if key.get("fromMe"):
            return False
        sender_jid = key.get("participant") or msg.get("participant") or key.get("remoteJid", "")
        push = (msg.get("pushName") or "").strip()
        if not sender_jid or not push:
            return False
        if push.isdigit() or is_phone_like(push):
            return False
        sender_jid = self._normalize_jid(sender_jid)
        # Never attribute a name to the group itself.
        if not sender_jid or sender_jid.endswith(("@g.us", "@broadcast", "@newsletter")):
            return False

        ppm = self._presence_pushname_map
        changed = False
        targets = [sender_jid]
        # Index both JID forms when the bridge is known, so a lookup by either
        # one finds the name.
        if sender_jid.endswith("@lid"):
            phone = getattr(self, "_lid_to_phone", {}).get(sender_jid, "")
            if phone:
                targets.append(phone)
        else:
            lid = getattr(self, "_phone_to_lid", {}).get(sender_jid, "")
            if lid:
                targets.append(lid)
        for target in targets:
            if ppm.get(target) != push:
                ppm[target] = push
                changed = True
        if changed:
            unresolvable = getattr(self, "_unresolvable_names", None)
            for target in targets:
                if not unresolvable or target not in unresolvable:
                    continue
                unresolvable.discard(target)
                try:
                    self.db.delete_unresolvable_name(target)
                except Exception as exc:
                    logging.warning("[_learn_sender_name] Failed to clear unresolvable name %s: %s",
                                    target, exc)
        return changed

    def _needs_sender_resolution(self, jid: str) -> bool:
        """True when `jid` is an @lid we still have no display name for.

        Used to feed resolve_lid_jids_via_api() with group participants. A JID
        already bridged to a phone number, present in contacts, or covered by a
        learned pushName resolves fine without an API round-trip.
        """
        if not isinstance(jid, str) or not jid.endswith("@lid"):
            return False
        if jid in getattr(self, "_lid_to_phone", {}):
            return False
        if jid in getattr(self, "_unresolvable_lids", set()):
            return False
        contact = self.contacts.get(jid) or {}
        if (contact.get("name") or contact.get("pushName") or "").strip():
            return False
        if (self._presence_pushname_map.get(jid) or "").strip():
            return False
        return True

    def _learn_sender_names_bulk(self, messages) -> bool:
        """Run _learn_sender_name over a batch of freshly-synced messages.

        The live WebSocket path already learned names message by message, but
        everything fetched through get-messages during a sync bypassed it — so
        after a fresh pairing whole group histories had no resolvable sender
        until each participant happened to send a new message.
        """
        changed = False
        for m in messages or ():
            if isinstance(m, dict) and self._learn_sender_name(m):
                changed = True
        return changed

    def on_historical_message(self, msg: dict):
        """
        Processes historical/sync messages (isMdHistoryMsg=True) received via WebSocket.
        Saves them to local storage, sorts records, and updates the lastMessage/t
        of the chat if the incoming message is newer. Does not trigger notifications or sounds.
        """
        # See _live_events_ready() — same reasoning as on_new_message().
        # Nothing is lost by dropping it here: the sync that is either about
        # to start or already running re-fetches all history regardless.
        if not self._live_events_ready():
            return
        key        = msg.get("key", {})
        remote_jid = self._normalize_jid(key.get("remoteJid", ""))
        msg_id     = key.get("id", "")

        if not remote_jid or not msg_id:
            return

        # Guard against the same self-chat multi-device-sync artifacts
        # on_new_message() redirects on the live path — see
        # _redirect_self_chat_artifact()'s own docstring. Without this, a
        # fake self-chat arriving through a history-sync batch (rather than
        # a live event) sat in the chat list unfiltered — an unnamed
        # phantom "group"/duplicate of "Eu" that couldn't be cleanly
        # identified or deleted afterwards — until the next full
        # deduplicate_chats() pass happened to run.
        #
        # from_me is discarded on purpose: unlike on_new_message(), this funnel
        # never reads it again — the history path files by JID alone.
        remote_jid, _ = self._redirect_self_chat_artifact(
            remote_jid, key, key.get("fromMe", False)
        )

        # Statuses (stories) or channels ignored
        if remote_jid.endswith("@broadcast") or remote_jid.endswith("@newsletter"):
            return

        # Normalize Alt JID mapping if present
        self._extract_lid_mapping(msg)

        # Same as on_new_message(): never a row, and never a reason to create
        # a chat (core/message_edit.py).
        if self._drop_protocol_edit(remote_jid, msg):
            return
        alt_jid = self._normalize_jid(key.get("remoteJidAlt", ""))
        if alt_jid:
            self._extract_lid_mapping(msg)

        # History messages carry the sender's pushName too — the only source of
        # a display name for group participants we cannot resolve otherwise.
        if self._learn_sender_name(msg):
            self._schedule_save(contacts_dirty=True)

        # Retrieve/create local chat object
        chat = self.chats.get(remote_jid)
        if not chat:
            chat = {
                "remoteJid": remote_jid,
                "unreadCount": 0,
                "pushName": msg.get("pushName", "") or "",
                "name": "",
                "messages": {"messages": {"records": []}},
                "lastMessage": None,
                "t": 0,
                "archived": False,
                "archive": False,
                "type": "group" if remote_jid.endswith("@g.us") else "chat",
            }
            if remote_jid.endswith("@g.us"):
                chat["name"] = self._fill_group_name(remote_jid)
            self.chats[remote_jid] = chat

        self._apply_group_subject_change(remote_jid, chat, msg)

        # The reactionMessage record still gets appended to `records` below
        # (needed so ConversationsPanel can rebuild the in-conversation
        # reaction display on reopen — see populate_messages()'s
        # _reaction_map scan), but the chat-LIST preview relies on
        # chat["_last_reaction"] (_track_last_reaction()) to show "you
        # reacted with X to Y" instead of falling through to formatting the
        # raw reactionMessage record, which has no case in
        # _last_msg_preview() and renders as "Mensagem incompatível". The
        # live path (on_new_message()) and the own-reaction path
        # (_on_own_reaction_sent()) already call this; history-sync
        # redelivering a reaction after an app restart or an F5 resync
        # never did, so _last_reaction stayed empty (it's in-memory only)
        # and the preview fell through to the raw record instead.
        if msg.get("messageType") == "reactionMessage":
            self._track_last_reaction(remote_jid, msg)

        records_wrapper = chat.setdefault("messages", {})
        if not isinstance(records_wrapper, dict):
            records_wrapper = chat["messages"] = {}
        inner_wrapper = records_wrapper.setdefault("messages", {})
        if not isinstance(inner_wrapper, dict):
            inner_wrapper = records_wrapper["messages"] = {}
        records = inner_wrapper.setdefault("records", [])
        if not isinstance(records, list):
            records = inner_wrapper["records"] = []

        # Check if already present in memory records
        existing = next((r for r in records if r.get("key", {}).get("id") == msg_id), None)
        if existing is not None:
            # A stored placeholder (or a text recovered from a quote) is
            # replaced by its decrypted copy, as on_new_message() does; history
            # never announces, so it is always filled in silently.
            if awaits_real_copy(existing) and not self._is_undecrypted_placeholder(msg):
                self._fill_stored_placeholder(existing, msg, remote_jid)
            elif call_log_supersedes(existing, msg):
                self._fill_stored_placeholder(existing, msg, remote_jid,
                                              what="newer state of call record")
                self._watch_pending_call_log(remote_jid, existing)
                self._refresh_calls_tab()
            return

        # Ignore stale re-deliveries of cleared messages
        if self._is_cleared_message(remote_jid, msg):
            return

        # Slim the payload
        prune_message_record(msg)
        records.append(msg)

        # Sort the records chronologically
        try:
            records.sort(key=lambda m: int(m.get("messageTimestamp") or m.get("timestamp") or 0))
        except Exception as sort_err:
            logging.error(f"[on_historical_message] Failed to sort records: {sort_err}")

        # Trim oldest-first now that the list is actually chronological —
        # doing this before the sort could drop a just-arrived message that
        # happened to land at the front of the (still unsorted) list.
        if len(records) > _MAX_RESIDENT_MESSAGES_PER_CHAT:
            del records[:len(records) - _MAX_RESIDENT_MESSAGES_PER_CHAT]

        # Update lastMessage and 't' (timestamp) if this message is newer.
        # System events never count — see is_countable_message() — otherwise
        # a group-metadata change arriving via history sync could still bump
        # an old, already-read conversation back to the top of the list.
        msg_ts = int(msg.get("messageTimestamp") or msg.get("timestamp") or 0)
        current_lm = chat.get("lastMessage")
        lm_ts = 0
        if isinstance(current_lm, dict):
            lm_ts = int(current_lm.get("messageTimestamp") or current_lm.get("timestamp") or 0)
        if is_countable_message(msg) and msg_ts >= lm_ts:
            chat["lastMessage"] = msg
            chat["t"] = msg_ts
            # Save updated chat to DB
            def _bg_upsert_chat():
                try:
                    self.db.upsert_chat(remote_jid, chat)
                except Exception as db_err:
                    logging.error(f"[on_historical_message] Failed to upsert chat to DB: {db_err}")
            self._msg_bg_executor.submit(_bg_upsert_chat)

        # Insert message to DB in background
        def _bg_insert_msg():
            try:
                self.db.insert_message(remote_jid, msg)
            except Exception as e:
                logging.error(f"[on_historical_message] Failed to insert message to DB: {e}")
        self._msg_bg_executor.submit(_bg_insert_msg)

        # History arrives in any order: a reply may quote an "Aguardando
        # mensagem" already stored, or a placeholder may land after a reply
        # that quotes it (core/quote_recovery.py).
        if MessageEventsMixin._is_undecrypted_placeholder(msg):
            self._recover_placeholders_from_replies(remote_jid, records)
        else:
            self._recover_quoted_placeholder(remote_jid, records, msg)
        if is_call_log(msg):
            self._watch_pending_call_log(remote_jid, msg)
            self._refresh_calls_tab()

        # Debounced UI update
        self._schedule_save(dirty_jid=remote_jid)
        self._schedule_set_chats()

        # Add message to the open conversation panel if it's currently selected
        cp = getattr(self, "conversations_panel", None)
        if cp and cp.conversation and cp.conversation.get("remoteJid") == remote_jid:
            # History backfill delivers messages one at a time, most of them
            # already on screen. refresh_messages_if_changed() collapses the
            # no-op ones into nothing instead of rebuilding (and re-placing
            # focus in) the whole list once per backfilled message — but only
            # the no-op ones: a message that genuinely changes the conversation
            # rebuilds in full, and during a backfill that is most of them.
            # Debounced for the same reason _schedule_set_chats() below is; see
            # _schedule_refresh_messages() for the cost that made this freeze
            # the UI outright on a large group.
            self._schedule_refresh_messages()

    def _reacted_message_preview(self, remote_jid: str, orig_id: str) -> str:
        """Return a short text preview of the original message a reaction targets."""
        if not orig_id:
            return ""
        from core.notification_manager import format_notification_body
        candidates = [remote_jid, self._normalize_jid(remote_jid)]
        lid = getattr(self, "_phone_to_lid", {}).get(remote_jid)
        phone = getattr(self, "_lid_to_phone", {}).get(remote_jid)
        if lid:
            candidates.append(lid)
        if phone:
            candidates.append(phone)
        seen = set()
        for cj in candidates:
            if not cj or cj in seen:
                continue
            seen.add(cj)
            chat = self.chats.get(cj)
            if not chat:
                continue
            for r in list(chat.get("messages", {}).get("messages", {}).get("records", [])):
                if r.get("key", {}).get("id") == orig_id:
                    try:
                        return (format_notification_body(r, self, self.i18n) or "")[:120]
                    except Exception:
                        return ""
        return ""

    def _track_last_reaction(self, remote_jid: str, msg: dict):
        """Remember the most recent reaction in this chat so
        _last_msg_preview() can show it in place of the last real message
        when it is genuinely the newest event. reactionMessage records are
        deliberately never added to a chat's regular records list (see
        on_new_message() — keeping them out of the message list/unread
        counts is intentional), so without a side-channel like this the
        chat-list preview always fell back to the last real message even
        when a reaction to it arrived afterwards.
        """
        chat = self.chats.get(remote_jid)
        if chat is None:
            return
        reaction = (msg.get("message") or {}).get("reactionMessage") or {}
        emoji = (reaction.get("text") or "").strip()
        if not emoji:
            # Empty emoji = the reaction was removed. Clear any stored
            # reaction for this same target message so the preview falls
            # back to the last real message instead of showing a reaction
            # that no longer exists.
            target_id = (reaction.get("key") or {}).get("id", "")
            if chat.get("_last_reaction", {}).get("target_id") == target_id:
                chat.pop("_last_reaction", None)
            return
        ts = msg.get("messageTimestamp")
        try:
            ts_val = int(ts) if ts else 0
        except (TypeError, ValueError):
            ts_val = 0
        if ts_val > 1_000_000_000_000:
            ts_val //= 1000
        key = msg.get("key", {})
        chat["_last_reaction"] = {
            "emoji": emoji,
            "target_id": (reaction.get("key") or {}).get("id", ""),
            "from_me": bool(key.get("fromMe")),
            "participant": key.get("participant") or key.get("remoteJid") or "",
            "push_name": msg.get("pushName", ""),
            "timestamp": ts_val,
        }

    def _reconstruct_last_reactions_from_records(self):
        """Rebuild chat["_last_reaction"] (in-memory only — see
        _track_last_reaction()) from persisted reactionMessage records right
        after self.chats is loaded from the DB at startup.

        _track_last_reaction() is already called by on_new_message() (live
        send/receive), _on_own_reaction_sent() (own reaction) and
        on_historical_message() (history-sync redelivery of the same
        reaction) — but none of those run at cold start before an actual
        resync happens, so a chat whose newest activity was a reaction kept
        showing its previous real message as the chat-list preview after
        every restart, until the next resync (or a brand-new live reaction)
        happened to repopulate it. The reactionMessage record itself is
        already persisted (see on_historical_message() — kept there so
        ConversationsPanel can rebuild the in-conversation reaction display
        on reopen), so replaying it here needs no extra DB round-trip.
        """
        for jid, chat in self.chats.items():
            if not isinstance(chat, dict):
                continue
            records = (
                chat.get("messages", {}).get("messages", {}).get("records", [])
            )
            if not isinstance(records, list) or not records:
                continue
            reaction_records = [
                r for r in records
                if isinstance(r, dict) and r.get("messageType") == "reactionMessage"
            ]
            if not reaction_records:
                continue

            def _ts(m):
                val = int(m.get("messageTimestamp") or m.get("timestamp") or 0)
                return val // 1000 if val > 1_000_000_000_000 else val

            # Replay oldest → newest so the final state matches whatever a
            # live sequence of the same reactions/removals would have left
            # behind — _track_last_reaction() always overwrites
            # unconditionally, it never compares timestamps itself.
            for r in sorted(reaction_records, key=_ts):
                self._track_last_reaction(jid, r)

    def _maybe_notify_reaction(self, remote_jid: str, msg: dict):
        """
        Notify when someone reacts to one of *your* messages.

        Only fires for reactions by other people to messages you sent — never for
        your own reactions, nor for reactions to other people's messages. Mirrors
        the guards (age, mute, archive, master toggle) used for normal messages.
        """
        try:
            reaction = (msg.get("message") or {}).get("reactionMessage") or {}
            emoji = (reaction.get("text") or "").strip()
            if not emoji:
                return  # empty emoji = reaction removed
            key = msg.get("key", {})
            if key.get("fromMe"):
                return  # I reacted — don't notify myself
            target_key = reaction.get("key") or {}
            if not target_key.get("fromMe"):
                return  # reaction to someone else's message — ignore

            ts = msg.get("messageTimestamp")
            if ts:
                try:
                    conn_time = getattr(self.ws, "_connect_time", time.time()) if self.ws else time.time()
                    if int(ts) < conn_time - 60:
                        return
                except (TypeError, ValueError):
                    pass

            muted = self.is_chat_muted(remote_jid)
            locked = bool(
                getattr(self, "is_chat_locked", lambda _jid: False)(remote_jid)
            )
            archived = self.is_chat_archived(remote_jid) and not locked

            if muted and self.settings.get("general", {}).get(
                "keep_muted_chats_silent_when_open", True
            ):
                return

            from core.notification_manager import format_notification_title

            orig_text = self._reacted_message_preview(remote_jid, target_key.get("id", ""))
            if orig_text:
                body = self.i18n.t("notif_reaction_to_own").format(emoji=emoji, text=orig_text)
            else:
                body = self.i18n.t("notif_reaction").format(emoji=emoji)
            title = format_notification_title(msg, self, self.i18n)

            window_active = (
                not getattr(self, "_window_hidden", False)
                and self.IsShown()
                and not self.IsIconized()
                and self.IsActive()
            )
            if window_active:
                cp = getattr(self, "conversations_panel", None)
                current_jid = (
                    cp.conversation.get("remoteJid", "")
                    if cp is not None and cp.conversation is not None
                    else ""
                )
                is_current_conv = (
                    cp._matches_open_conversation(remote_jid)
                    if cp is not None and hasattr(cp, "_matches_open_conversation") and cp.conversation is not None
                    else (current_jid == remote_jid and bool(current_jid))
                )
                if muted and not is_current_conv:
                    return
                if archived and not is_current_conv:
                    return
                # Reactions do not increment the unread-message count. A
                # count-only locked-chat notification would therefore be
                # misleading, while speaking the normal title/body would leak
                # the sender and reacted text. Only announce it when that
                # locked conversation is already open after PIN entry.
                if locked and not is_current_conv:
                    return
                if is_current_conv:
                    self.message_current_sound.play()
                else:
                    self.message_foreground_sound.play()
                self.output(f"{title}: {body}")
                return

            if muted or archived or locked:
                return
            # general.notifications_enabled only ever gates the background
            # toast below — see the matching comment in on_new_message().
            if not self.settings.get("general", {}).get("notifications_enabled", True):
                return
            if not self.settings.get("general", {}).get("show_tray_icon", True):
                return
            if hasattr(self, "notification_manager"):
                self.notification_manager.send(title, body, remote_jid)
        except Exception:
            logging.exception("[_maybe_notify_reaction] failed")
