"""ChatsStoreMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import os
import shutil
import sys
import threading
import time
import wx
from main_window.log_files import (
    _LazyLogFile,
    _consolidate_legacy_log_dir,
)
from core.incremental_sync import (
    chat_activity_floor as _chat_activity_floor,
    timestamp_seconds as _timestamp_seconds,
)
from main_window.message_rules import (
    _discount_non_countable_unread,
    _log_refused_read_receipt,
    note_unread_discount_state,
    reconcile_open_chat_unread,
    reconcile_snapshot_unread,
    records_cover_snapshot,
)
from core.utils import (
    parse_bool_flag as _parse_bool_flag,
    looks_like_binary_blob,
)
from core.api_client import api_post
from app_paths import data_path
from traceback import format_exc
from core.sync_contracts import observe_payload
from main_window.chat_list import ChatListMixin


class ChatsStoreMixin:
    """Local chat storage: clearing local data, chat getters, remote chat fetch,
    deduplication, status updates and saving.
    """

    def _store_status_update(self, msg: dict):
        """Store an incoming status/story message in _status_updates and refresh the Status tab."""
        key = msg.get("key", {})
        participant = (
            key.get("participant")
            or msg.get("participant")
            or (key.get("fromMe") and getattr(self, "my_jid", ""))
            or ""
        )
        if not participant:
            return
        if not hasattr(self, "_status_updates"):
            self._status_updates = {}
        bucket = self._status_updates.setdefault(participant, [])
        msg_id = key.get("id", "")
        if msg_id and any(m.get("key", {}).get("id") == msg_id for m in bucket):
            return  # deduplicate
        bucket.append(msg)
        # Persist the status update directly instead of going through _schedule_save()
        # (which would fall back to writing all chats when no dirty_jid is set).
        def _save_status():
            try:
                self.db.upsert_status_update(participant, msg)
            except Exception as exc:
                logging.warning("[_store_status_update] DB write failed: %s", exc)
        threading.Thread(target=_save_status, daemon=True).start()
        # Refresh the Status tab if it is currently visible
        try:
            if hasattr(self, "navigation_panel"):
                sp = getattr(self.navigation_panel, "status_panel", None)
                if sp and sp.IsShown():
                    wx.CallAfter(lambda: threading.Thread(target=sp._load_statuses, daemon=True).start())
        except Exception:
            pass

    def remove_failed_status_update(self, message_id: str, refresh: bool = True) -> None:
        """Remove a rejected or reconciled own status from memory and SQLite."""
        if not message_id:
            return
        removed = False
        for participant in list(getattr(self, "_status_updates", {}).keys()):
            bucket = self._status_updates.get(participant) or []
            kept = [m for m in bucket if m.get("key", {}).get("id") != message_id]
            if len(kept) != len(bucket):
                removed = True
                if kept:
                    self._status_updates[participant] = kept
                else:
                    self._status_updates.pop(participant, None)
        try:
            self.db.delete_status_update(message_id)
        except Exception:
            logging.exception("[status] Failed to delete cached status %s", message_id)
        if removed and refresh:
            sp = getattr(getattr(self, "navigation_panel", None), "status_panel", None)
            if sp:
                threading.Thread(target=sp._load_statuses, daemon=True).start()

    # How many individual "could not delete this file" lines the media sweep
    # below may write per folder before it falls back to the count alone.
    _MAX_MEDIA_DELETE_ERRORS_LOGGED = 5

    def clear_local_data(self, wipe_metadata: bool = True) -> bool:
        """Wipe all cached chats, contacts, messages, media, and mapping caches.

        wipe_metadata=True (default, used for a confirmed logout/account
        switch via _on_disconnect) also wipes system_metadata — cleared_chats,
        deleted_chats, archived_chats, pinned_chats, muted_chats,
        blocked_contacts and more — so nothing leaks into whatever account
        pairs next. wipe_metadata=False (used by _resync_all_worker(), F5)
        keeps that table intact: the point of a resync is only to refetch
        chats/messages from WhatsApp again, not to also discard every local
        action (a cleared/deleted/archived/muted/blocked chat) the user took
        on top of them — resyncing used to silently undo all of those too.

        Two things outside system_metadata go with it, for the same reason and
        under the same flag: data/media_failed.json (ids of the previous
        account's messages) and privateinfo["WA_phone_number_linked"] (the
        number this data belonged to). Both describe data that no longer
        exists once this returns.

        Returns whether the database really was emptied. That is one of the
        two conditions the WA_phone_number_linked drop below is gated on, not
        the same one: the drop also needs wipe_metadata, so F5
        (wipe_metadata=False) empties the message tables, is reported here as
        True and still drops nothing — the flag decides what is emptied, never
        whether emptying it is reported. Only _apply_another_number_wipe()
        reads the answer, and it has to: it records the newly linked number the
        moment this returns, and doing that after a wipe that emptied nothing
        would leave the key naming the new account while the old account's
        messages are still in messages.db, which reads as "no divergence" from
        then on. Every other caller ignores it.
        """
        logging.info("[clear_local_data] Clearing all local caches, media, and database...")
        # Invalidate every background job before touching shared chat state.
        # A backfill captures this generation and must not keep querying or
        # writing after F5/logout has emptied the database underneath it.
        self._sync_run_id = getattr(self, "_sync_run_id", 0) + 1
        self._backfill_thread = None
        with self._backfill_state_guard():
            self._chats_awaiting_messages.clear()
            self._partial_history_counts.clear()
            self._history_gap_jids.clear()
        with self._sync_failures_lock:
            self._message_retry_jids.clear()
            self._sync_failed_chats.clear()
            self._delta_unsatisfied_chats.clear()
            self._delta_unsatisfied_attempts.clear()
            self._absent_chats.clear()
            self._absent_chat_attempts.clear()
        self._persist_backfill_pending_state()
        self._persist_history_gap_jids()
        self._persist_message_retry_jids()
        self.chats = {}
        self.contacts = {}
        self._status_updates = {}
        # Under the mapping lock: an in-flight Socket.IO event can be inside
        # _extract_lid_mapping() right now, and clearing a dict it is
        # iterating raises "dictionary changed size during iteration".
        with self._lid_mapping_lock:
            if hasattr(self, "_lid_to_phone"):
                self._lid_to_phone.clear()
            else:
                self._lid_to_phone = {}
            if hasattr(self, "_phone_to_lid"):
                self._phone_to_lid.clear()
            else:
                self._phone_to_lid = {}
        if hasattr(self, "_unresolvable_lids"):
            self._unresolvable_lids.clear()
        else:
            self._unresolvable_lids = set()
        if hasattr(self, "_unresolvable_names"):
            self._unresolvable_names.clear()
        else:
            self._unresolvable_names = set()
        if hasattr(self, "_resolving_lids"):
            self._resolving_lids.clear()
        else:
            self._resolving_lids = set()
            
        if wipe_metadata:
            # The in-memory half of the system_metadata wipe below. prepare_sync()
            # reads every one of these OUT of that table into RAM at startup, and
            # _wipe_local_data_if_another_number_linked() is the first caller that
            # runs AFTER that load — so clearing only the table left account A's
            # deleted/archived/pinned/muted sets, its block list, its push names
            # and its own JID live in this process, and account B's very first
            # sync wrote all of them straight back into B's database
            # (get_remote_chats() persists muted/pinned/archived, the deleted set
            # is persisted from the chat-list build, _resolve_self_referential_jid()
            # reads my_jid). A conversation of B's that A had deleted then never
            # appeared in B's list at all — on disk, permanently.
            #
            # Only under wipe_metadata: F5/resync must keep every one of them,
            # which is the whole point of the flag (see the docstring above).
            self._deleted_chats = set()
            self._archived_chats = set()
            self._pinned_chats = set()
            self._muted_chats = {}
            self._blocked_contacts = set()
            self._presence_pushname_map = {}
            self._locally_read_at = {}
            # The read anchors and the arrivals counter they qualify. A group
            # JID is the same string in both accounts, so an anchor left over
            # from A authorises the clamp in on_chat_unread_update() for the
            # same group in B — where the chat has never been read and the
            # counter measures only arrivals since the switch. That is the
            # collapse this whole mechanism exists to prevent, reintroduced
            # through the back door.
            self._unread_read_anchors = set()
            self._new_since_read = {}
            # Which groups the previous account could post in — i.e. which
            # groups it was a member of at all. _persist_group_send_perms()
            # writes it back out of RAM, so the emptied table filled up again
            # with the other account's group list.
            self._group_send_perms = {}
            # The last round's diagnostic checkpoint, including the
            # force_full_pending latch prepare_sync() restores _force_full_sync
            # from. Persisted again by _persist_successful_sync_state() /
            # _persist_full_sync_pending().
            self._last_sync_state = {}
            # Left as the empty string rather than deleted: _is_self_jid() and
            # the "Eu" label read them unconditionally, and the next
            # host-device/self-LID lookup rewrites them.
            self.my_jid = ""
            self.my_lid = ""
            # "This chat has no older history" and the requests that concluded
            # it — one line, because F5 already needed exactly this and its
            # docstring already describes the damage of keeping them.
            self._forget_history_exhaustion()
            # When get-messages last really ran for each chat, keyed by JID and
            # persisted. Same family as everything above and reached the same
            # way: prepare_sync() loads it out of chat_verified_at_v1 into RAM,
            # so emptying the table left account A's timestamps live here and
            # _persist_chat_verified_at() wrote the whole dict — A's JIDs
            # included — straight back into B's freshly emptied entry. For a
            # contact both accounts have, that stale timestamp then keeps B's
            # chat out of select_stale_rechecks() for a full
            # _STALE_RECHECK_AFTER.
            self._chat_verified_at = {}
            # Which conversations the user opened — the gate on asking the
            # PHONE for older history, and the one collection here whose
            # leftovers the user of the new account can see, on their own
            # device. Reached exactly like the two above: prepare_sync() loads
            # opened_conversations_v1 into RAM, so emptying the table left
            # account A's JIDs live here, and _backfill_empty_chats() read
            # _user_has_opened() as True for a contact both accounts have and
            # sent request_older_messages() for a conversation B's user never
            # opened — the lock-screen "Synchronizing WhatsApp with Google
            # Chrome (Windows)…" followed by "Sync paused", which is issue
            # #108 all over again. Worse, _note_conversation_opened() writes
            # the whole set back on the first conversation B opens, so A's
            # JIDs become durable on B's disk; and _forget_history_exhaustion()
            # above hands B the full _MAX_PHONE_HISTORY_REQUESTS budget to
            # spend on them.
            self._opened_conversations = set()
            # Media whose CDN URL answered 403/410, keyed by message id. Same
            # family as everything above and the last member of it: the ids
            # belong to the previous account's messages, and the file outlives
            # the account switch entirely, so a fresh install of account B
            # started life refusing to download media it had never tried.
            # F5 calls the same helper itself (it passes wipe_metadata=False
            # and keeps nothing else here either), so this line touches only
            # the account switch.
            self._forget_media_failures()
            # Same family by origin as everything above (fed by
            # _note_verified_activity(), consulted by
            # local_history_behind_server() as a floor), inert here today
            # only because it is deliberately never persisted — clearing it
            # anyway keeps it out of the same leak class the moment that
            # changes, rather than relying on that being true forever.
            self._verified_activity = {}

        db_emptied = False
        try:
            if hasattr(self, "db") and self.db is not None:
                self.db.save_full_state({"chats": {}, "contacts": {}}, clear_metadata=wipe_metadata)
                db_emptied = True
                logging.info("[clear_local_data] Database cleared successfully.")
        except Exception as e:
            logging.error(f"[clear_local_data] Failed to clear database: {e}")

        # Clear local downloaded media files to prevent cross-account leakage.
        # Swept here — after the database is emptied above, but BEFORE the
        # WA_phone_number_linked key is dropped below — deliberately, not
        # left in its previous position after the key drop (issue #200). A
        # process killed anywhere in the account-switch window is routine: 17
        # of the 159 launches in one field shutdown_audit.log ended with no
        # _stop_wpp_server line at all. Swept after the key drop, a kill
        # between the two left the key already gone with the previous
        # account's media still on disk — the next launch's "learn this
        # number, delete nothing" branch (see below) is exactly the one that
        # never comes back to clean orphaned media up, since a database with
        # nothing in it never trips the divergence check again. Swept first,
        # as here, a kill in the same spot instead leaves the key still
        # naming the previous account, so the next launch detects the
        # divergence and repeats this whole method — re-sweeping an
        # already-empty media/voice_messages (a per-file no-op, each entry
        # already missing) before reaching the key drop again. Nothing is
        # ever orphaned; at worst one redundant pass runs on the next launch.
        for subdir in ("media", "voice_messages"):
            path = data_path(subdir)
            if not os.path.exists(path):
                continue
            try:
                entries = os.listdir(path)
            except Exception as e:
                logging.error(f"[clear_local_data] Failed to list {subdir} folder: {e}")
                continue
            failed = 0
            for filename in entries:
                file_path = os.path.join(path, filename)
                # Per file, not per folder. A single entry Windows refuses to
                # delete — a voice note BASS still has open is the measured one
                # — used to abort the sweep of the whole directory from its
                # first failure onwards, leaving the rest of the previous
                # account's media on disk.
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    failed += 1
                    # Only the first few, with the count reported below. A
                    # folder an antivirus (or a crashed BASS handle) has locked
                    # fails on every single entry, and a media/ directory holds
                    # thousands of them — in log.log, which is truncated every
                    # launch and is the one file a user pastes into a bug
                    # report, that buries the whole rest of the run.
                    if failed <= self._MAX_MEDIA_DELETE_ERRORS_LOGGED:
                        logging.error(
                            f"[clear_local_data] Failed to delete {file_path}: {e}")
            if failed:
                logging.error(
                    f"[clear_local_data] Cleared folder {subdir} except {failed} entries")
            else:
                logging.info(f"[clear_local_data] Cleared folder: {subdir}")

        if wipe_metadata and db_emptied:
            # The number this data belonged to. The invariant the divergence
            # check is written around is that the key describes what is on
            # disk, and six call sites in connect.py wipe through here without
            # ever having heard of it — leaving the key naming an account whose
            # database no longer exists, which reads as "no divergence" the
            # next time somebody else's phone pairs. _on_disconnect(wipe=False)
            # deliberately does not come through here, so the armed case stays
            # armed. _wipe_local_data_if_another_number_linked() rewrites it
            # immediately after its own call.
            #
            # Dropped down here, after both the database has actually been
            # emptied AND the previous account's media has been swept above
            # (issue #200), rather than with the rest of the in-memory wipe
            # higher up. A process killed between the two is routine — 17 of
            # the 159 launches in one field shutdown_audit.log ended with no
            # _stop_wpp_server line at all — and killed with the key already
            # gone while the messages were still on disk, the next launch has
            # nothing to compare against, takes the "learn this number,
            # delete nothing" branch, and lets account B merge onto account
            # A: the merge this key exists to prevent, disarmed by its own
            # cleanup. The other order costs nothing — a key naming a
            # database that is already empty (and media that is already
            # swept) is read as a divergence and wipes both a second time,
            # harmlessly.
            #
            # And only when the database really was emptied, which is why
            # db_emptied is a variable and not just the `if` above. self.db
            # exists from prepare_sync() onwards, and all six connect.py call
            # sites run before that, inside __init__'s connection dialog: there
            # the write is skipped entirely and every message of the previous
            # account stays in messages.db (the divergence check's own docstring
            # says so). save_full_state() can also raise DatabaseBridgeTimeout/
            # Closed, which is swallowed right above. Either way the key is
            # still describing what is on disk, so it has to stay armed for the
            # check after prepare_sync() to act on — dropping it there is the
            # kill window above without the kill. The account-switch path is
            # unaffected: _apply_another_number_wipe() rewrites the key
            # immediately after its own call.
            privateinfo = getattr(self, "settings", {}).get("privateinfo")
            if (isinstance(privateinfo, dict)
                    and privateinfo.pop("WA_phone_number_linked", None) is not None):
                # Saved here rather than left to the caller: most of those six
                # call sites never save at all, and a key that survives in
                # settings.json is exactly as wrong as one that survives in
                # memory — the next launch reads it straight back.
                try:
                    self.save_settings()
                except Exception:
                    logging.exception(
                        "[clear_local_data] Could not persist dropping the "
                        "recorded linked number.")

        # Returned after both the database clear and the media sweep (the
        # sweep is deliberately not gated on db_emptied: media/ and
        # voice_messages/ are cleared either way). A False answer means
        # self.db didn't exist yet or save_full_state() raised — the key
        # above was therefore deliberately left in place (see its own
        # comment), so the previous account's rows are still in messages.db
        # even though its media and voice notes are already gone from disk
        # and, on the account-switch path, the user already told out loud
        # that those conversations were deleted. The list therefore comes back
        # holding chats whose attachments no longer resolve locally and cannot
        # be fetched again either, since the session now belongs to the other
        # account. Nothing here can undo that half — the files are gone — which
        # is precisely why the other half is reported rather than assumed: the
        # caller keeps the key naming the account those rows belong to, so the
        # next pass finishes the wipe instead of merging on top of it.
        return db_emptied

    def create_basic_files(self):
        data_dir = data_path("")
        os.makedirs(data_dir, exist_ok=True)

        #Create media/voice message directories
        os.makedirs(data_path("media"), exist_ok=True)
        os.makedirs(data_path("voice_messages"), exist_ok=True)

        # Create stderr/stdout log files.
        #
        # These live in logs/ with everything else. They used to go to a
        # separate data/log/ — one letter away from data/logs/, which holds
        # log.log, shutdown_audit.log and wppconnect.log. Two sibling
        # directories whose names differ by an "s" is a trap when asking a
        # user to send their logs, and there was never a reason for the split:
        # log_path() is the account's log directory and this was the only
        # writer that did not use it.
        from app_paths import log_path
        _consolidate_legacy_log_dir()
        log_dir = log_path()
        os.makedirs(log_dir, exist_ok=True)
        stderr_log = os.path.join(log_dir, "stderr.log")
        stdout_log = os.path.join(log_dir, "stdout.log")
        # Neither file is created here. They are crash/traceback sinks and a
        # healthy run writes nothing to them, so creating them up front left
        # two permanently empty files in logs/ that read as if they ought to
        # contain something. _LazyLogFile creates its file on the first byte
        # actually written; a run with nothing to report leaves no file at all,
        # which makes "is there anything in the logs?" answerable at a glance.
        for leftover in (stderr_log, stdout_log):
            try:
                if os.path.isfile(leftover) and os.path.getsize(leftover) == 0:
                    os.remove(leftover)
            except OSError:
                pass
        #Set stderr and stdout
        sys.stderr = _LazyLogFile(stderr_log)
        sys.stdout = _LazyLogFile(stdout_log)

    def get_chat(self, jid: str) -> dict | None:
        """Get a chat from self.chats by JID, with fallback to mapped JID (LID/phone)."""
        if not jid:
            return None
        chat = self.chats.get(jid)
        if chat is not None:
            return chat
        # Fallback to mapped JID
        alt_jid = ""
        if jid.endswith("@lid"):
            alt_jid = getattr(self, "_lid_to_phone", {}).get(jid, "")
        else:
            alt_jid = getattr(self, "_phone_to_lid", {}).get(jid, "")
        if alt_jid:
            return self.chats.get(alt_jid)
        return None

    def chat_display_name(self, chat_or_jid) -> str:
        """The name a conversation is shown under: its title when opened, and
        who a call was with on the Calls tab. Never a raw JID."""
        if isinstance(chat_or_jid, dict):
            chat = chat_or_jid
        else:
            jid = str(chat_or_jid or "")
            chat = self.get_chat(jid) or {"remoteJid": jid}
        jid = chat.get("remoteJid", "")
        is_group = jid.endswith("@g.us")
        return (
            self._resolve_contact_name(chat)
            or self.find_name_through_messages(chat)
            or chat.get("name", "")
            or ("" if is_group else chat.get("pushName", ""))
            or self.find_jid_through_messages(chat)
            or self._format_jid_for_display(jid)
            or (self.i18n.t("unknown_group") if is_group else self.i18n.t("unknown_contact"))
        )

    def get_chats(self, limit: int = 200):
        try:
            return self.db.get_chats(limit=limit)
        except Exception as e:
            self.error_sound.play()
            wx.MessageBox(f"{self.i18n.t('chat_load_failed')} {format_exc()}", self.i18n.t("error").format(app_name=self.app_name), wx.OK | wx.ICON_ERROR)
            return {}

    @staticmethod
    def _lift_contact_identity(chat: dict) -> None:
        """Copy list-chats' nested `contact` block into flat name/pushName keys.

        WPPConnect ships every individual chat with a `contact` sub-object
        carrying name/shortName/pushname, and nothing read it — so individual
        chats were stored and rendered nameless (measured on a real account:
        124 of 263 chats had a usable name here, 0 of 263 reached the DB with
        one).  That is not merely cosmetic: _compute_chat_lists() decides
        whether a chat is worth showing from exactly these two keys, so a
        chat with no name, no messages yet (list-chats returns `msgs: null`,
        so lastMessage is empty for all of them) and no unread count was
        dropped from the conversation list entirely.

        Never overwrites a value the chat already carries — the top-level keys
        win when both are present. Mutates `chat` in place.
        """
        contact_obj = chat.get("contact")
        if not isinstance(contact_obj, dict):
            return
        if not (chat.get("name") or "").strip():
            cname = (contact_obj.get("name") or contact_obj.get("shortName") or "").strip()
            if cname:
                chat["name"] = cname
        if not (chat.get("pushName") or "").strip():
            cpush = (contact_obj.get("pushname") or contact_obj.get("pushName") or "").strip()
            if cpush:
                chat["pushName"] = cpush

    @staticmethod
    def _last_received_jid(chat: dict) -> str:
        """Extract the JID a chat's `lastReceivedKey` belongs to, or ''.

        list-chats serialises every chat with msgs:null (no lastMessage), but
        the raw ChatModel still carries lastReceivedKey — the key of the last
        message received in that chat. Its `remote` (or the serialized
        '<fromMe>_<chatId>_<id>' string) tells us which chat that message
        actually belongs to, which is how a non-group chat that is really a
        phantom group-participant entry can be told apart from a genuine 1:1.
        """
        if not isinstance(chat, dict):
            return ""
        lrc = chat.get("lastReceivedKey")
        if not isinstance(lrc, dict):
            return ""
        lrc_remote = lrc.get("remote")
        if isinstance(lrc_remote, dict):
            jid = lrc_remote.get("_serialized") or ""
            if jid:
                return jid
        ser = lrc.get("_serialized") or ""
        parts = ser.split("_") if ser else []
        return parts[1] if len(parts) > 1 else ""

    def get_remote_chats(self, chats, persist_full: bool = True, notify_errors: bool = True,
                         prune_stale: "bool | None" = None, defer_chat_save: bool = False):
        """Fetch/merge the remote chat list into `chats`.

        Returns the merged dict on success and **None** when every attempt
        failed — callers must treat None as "the chat list is unknown", never
        as "there are no chats", since `self.chats` may still be growing in
        parallel from WebSocket events.  The last error is also left in
        `self._last_chat_fetch_error` for the caller to report.

        `notify_errors` controls whether a modal error dialog is shown when all
        attempts fail.  The initial sync retries this call itself and reports
        once at the end, so it passes False to avoid stacking one dialog per
        attempt.

        `persist_full` controls whether the result is written via the
        expensive full clear-and-reimport `save_data()` path (appropriate
        right after a real sync) or left to the existing lightweight
        debounced per-chat save (appropriate for the periodic background
        refresh, which otherwise re-clears and re-encrypts the *entire*
        chats+contacts DB every few minutes just to notice a pin/mute
        change on one chat).

        `prune_stale` controls the retroactive phantom-chat sweep, which used
        to ride along on `persist_full` for no reason other than both being
        "right after a real sync" work. They have completely different costs —
        the sweep is an in-memory dict scan, the save rewrites every message
        in the database — so the initial sync's retry loop needs the first
        without paying for the second. Defaults to `persist_full`, preserving
        the previous behaviour for every caller that does not pass it.

        `defer_chat_save` keeps a fresh list-chats snapshot in memory until its
        corresponding message delta has succeeded. This matters for incremental
        sync: persisting a newer `t`/lastReceivedKey before get-messages succeeds
        creates a crash window where the next launch sees the new marker as its
        baseline and can incorrectly skip the missing message. Startup and the
        periodic delta poll use this mode, then persist the snapshot only after
        the message phase is known-good.
        """
        if prune_stale is None:
            prune_stale = persist_full
        # Use the modern `list-chats` endpoint (WPP.chat.list) instead of the
        # deprecated `all-chats` (legacy WAPI.getAllChats). The legacy call omits
        # some chats — notably muted or pinned groups — so those never got
        # collected on pairing. A body with no filters returns every chat.
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/list-chats"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        # `ignoreGroupMetadata` is not a filter — it only skips the group-metadata
        # prefetch WPP.chat.list() does at the end of every call: a *serial*
        # `await GroupMetadataStore.find(id)` per group chat, one network
        # round-trip each.  Right after pairing, while WhatsApp Web is still
        # running its initial sync, that loop routinely runs longer than
        # Puppeteer's protocolTimeout, so list-chats never answers at all and
        # every attempt here dies with "Read timed out" — leaving the user with
        # an empty chat list.  Worse, an HTTP timeout on our side does not
        # cancel the evaluate inside the page, so each retry stacks *another*
        # metadata loop onto the same JS thread and the call gets slower, not
        # faster.
        #
        # Skipping the prefetch means a group whose metadata WhatsApp Web hasn't
        # cached yet arrives without groupMetadata.subject, i.e. unnamed.  That
        # case is already handled — and handled better — downstream:
        # _resolve_missing_group_names() re-fetches exactly those groups through
        # /group-info, six at a time and with a 10 s timeout each, instead of
        # one at a time with no timeout at all.  A named group is unaffected
        # either way: its subject is already in the store and still serialises.
        payload = {"ignoreGroupMetadata": True, "count": 5000}

        # Escalating per-request timeouts instead of a flat 120 s × 3.
        #
        # list-chats runs WPP.chat.list() inside the Puppeteer page, so it only
        # answers once WhatsApp Web's single JS thread is free — right after a
        # pairing that can take minutes.  A flat 120 s meant just 3 chances
        # spread over 6 min, each an opaque 2-minute block.  A flat *short*
        # timeout is worse: the attempt that actually succeeded in the field
        # took ~35 s, so 30 s everywhere would have killed the good response
        # and restarted the (expensive) evaluate for nothing.
        #
        # Starting short and growing gives more chances inside the same overall
        # budget (~6 min): the healthy case answers in seconds, a warming-up
        # server gets caught by the early cheap attempts, and the later patient
        # ones still cover a page that stays busy for minutes.
        _RETRY_SLEEP = 5   # seconds between retries
        _TIMEOUTS    = (30, 45, 60, 90, 120)  # seconds per request, per attempt
        _ATTEMPTS    = len(_TIMEOUTS)
        last_error = None
        self._last_chat_fetch_count = 0
        self._last_chat_fetch_disconnected = False
        for attempt, _timeout in enumerate(_TIMEOUTS):
            try:
                response = api_post(url, json=payload, headers=headers, timeout=_timeout)
                if response.status_code not in (200, 201):
                    logging.error(
                        "[get_remote_chats] API error %s (attempt %d/%d): %s",
                        response.status_code, attempt + 1, _ATTEMPTS, response.text[:200],
                    )
                    last_error = f"HTTP {response.status_code}"
                    # HTTP 404 {"status": "Disconnected"} is WPPConnect saying
                    # WhatsApp itself is unreachable (typically: the machine has
                    # no internet).  Retrying it five times with growing
                    # timeouts just burns ~6 minutes and ends in a modal error
                    # dialog; bail out at once, flag the connection as down and
                    # let the health checker restart the sync when it returns.
                    if self._check_wa_connection_closed(response):
                        self._last_chat_fetch_disconnected = True
                        self._last_chat_fetch_error = last_error
                        return None
                    if attempt < _ATTEMPTS - 1:
                        logging.info("[get_remote_chats] Retrying in %ds...", _RETRY_SLEEP)
                        time.sleep(_RETRY_SLEEP)
                        continue
                    break
                try:
                    resp_text = response.text.strip() if response.text else ""
                    if not resp_text or resp_text == "undefined" or resp_text == "null":
                        logging.warning("[get_remote_chats] Server returned empty or undefined response.")
                        body = []
                    else:
                        body = response.json()
                except Exception as json_err:
                    logging.error(
                        "[get_remote_chats] Failed to parse JSON (attempt %d/%d): %s. Body: %s",
                        attempt + 1, _ATTEMPTS, json_err, response.text[:200],
                    )
                    last_error = json_err
                    if attempt < _ATTEMPTS - 1:
                        logging.info("[get_remote_chats] Retrying in %ds...", _RETRY_SLEEP)
                        time.sleep(_RETRY_SLEEP)
                        continue
                    break

                # list-chats returns the array directly; tolerate the legacy
                # {"response": [...]} envelope too in case of a mixed deployment.
                if isinstance(body, list):
                    response_data = body
                elif isinstance(body, dict):
                    response_data = body.get("response", [])
                else:
                    response_data = []
                if not isinstance(response_data, list):
                    response_data = []
                # Names the field in the log when the chat shape changes,
                # instead of leaving it to surface later as a wrong badge or a
                # chat with no title. Returns the list untouched — see
                # core/sync_contracts.py.
                observe_payload(response_data, "list-chats")

                # How many chats the *server* returned this time.  Callers use
                # it to tell "the API is warmed up and gave us the whole
                # account" from "the API answered early with a handful of
                # chats" — len(self.chats) cannot do that, since it also grows
                # from WebSocket traffic while the sync runs.
                self._last_chat_fetch_count = len(response_data)

                # Traduzir as chaves do WPPConnect (remoteJid)
                for chat in response_data:
                    if not isinstance(chat, dict):
                        continue
                    wpp_id = chat.get("id")
                    jid_str = wpp_id.get("_serialized") if isinstance(wpp_id, dict) else wpp_id
                    if jid_str:
                        chat["remoteJid"] = jid_str.replace("@c.us", "@s.whatsapp.net")

                    self._lift_contact_identity(chat)

                lid_chats = [c for c in response_data if isinstance(c, dict) and c.get("remoteJid", "").endswith("@lid")]
                if lid_chats:
                    shape = {k: type(v).__name__ for k, v in lid_chats[0].items()}
                    logging.info("[get_remote_chats] @lid chat shape: %s", shape)

                # The deleted-chat list lives in DB metadata (self._deleted_chats)
                # since 0.17 — prepare_sync() pops it out of settings.json on
                # first run.  Reading settings here therefore returned an empty
                # set on every modern install, which is why chats deleted by the
                # user came straight back on the next sync/restart.
                deleted = set(self._deleted_chats)
                cleared = self.settings.get("cleared_chats", {})

                for chat in response_data:
                    if not isinstance(chat, dict):
                        continue
                    jid = self._normalize_jid(chat.get("remoteJid", ""))

                    # Try to extract JID mapping from lastMessage if present
                    last_msg = chat.get("lastMessage")
                    if isinstance(last_msg, dict):
                        key = last_msg.get("key")
                        if isinstance(key, dict):
                            remote = key.get("remoteJid", "")
                            alt = key.get("remoteJidAlt", "")
                            if remote and alt:
                                # This runs on the sync thread while
                                # _extract_lid_mapping() writes the same two
                                # dicts from the Socket.IO one — same
                                # check-then-set, same lock.
                                with self._lid_mapping_lock:
                                    if not hasattr(self, "_lid_to_phone"):
                                        self._lid_to_phone = {}
                                    if not hasattr(self, "_phone_to_lid"):
                                        self._phone_to_lid = {}
                                    if remote.endswith("@lid") and alt.endswith("@s.whatsapp.net"):
                                        if self._lid_to_phone.get(remote) != alt:
                                            self._lid_to_phone[remote] = alt
                                            self._phone_to_lid[alt] = remote
                                            logging.info(f"[LID Mapping] Extracted mapping from lastMessage in get_remote_chats: {remote} <-> {alt}")
                                    elif alt.endswith("@lid") and remote.endswith("@s.whatsapp.net"):
                                        if self._lid_to_phone.get(alt) != remote:
                                            self._lid_to_phone[alt] = remote
                                            self._phone_to_lid[remote] = alt
                                            logging.info(f"[LID Mapping] Extracted mapping from lastMessage in get_remote_chats (alt): {alt} <-> {remote}")

                    # Skip status@broadcast — statuses are shown in the Status tab
                    if not jid or jid.endswith("@broadcast"):
                        continue

                    # Populate/update self.contacts from chat name metadata
                    if jid and not jid.endswith("@g.us"):
                        name = chat.get("name")
                        pushName = chat.get("pushName")
                        if looks_like_binary_blob(name):
                            name = None
                        if looks_like_binary_blob(pushName):
                            pushName = None
                        if jid not in self.contacts:
                            self.contacts[jid] = {"id": jid, "remoteJid": jid}
                        if name:
                            self.contacts[jid]["name"] = name
                        if pushName:
                            self.contacts[jid]["pushName"] = pushName

                        phone_jid = getattr(self, "_lid_to_phone", {}).get(jid)
                        if phone_jid:
                            if phone_jid not in self.contacts:
                                self.contacts[phone_jid] = {"id": phone_jid, "remoteJid": phone_jid}
                            if name:
                                self.contacts[phone_jid]["name"] = name
                            if pushName:
                                self.contacts[phone_jid]["pushName"] = pushName

                        lid_jid = getattr(self, "_phone_to_lid", {}).get(jid)
                        if lid_jid:
                            if lid_jid not in self.contacts:
                                self.contacts[lid_jid] = {"id": lid_jid, "remoteJid": lid_jid}
                            if name:
                                self.contacts[lid_jid]["name"] = name
                            if pushName:
                                self.contacts[lid_jid]["pushName"] = pushName

                    if jid.endswith("@lid"):
                        phone_jid = getattr(self, "_lid_to_phone", {}).get(jid)
                        if phone_jid and phone_jid in chats:
                            continue
                    if jid in deleted:
                        continue
                    if jid.endswith("@lid"):
                        phone_jid = getattr(self, "_lid_to_phone", {}).get(jid)
                        if phone_jid and phone_jid in deleted:
                            continue
                    if not jid.endswith("@lid"):
                        lid_jid = getattr(self, "_phone_to_lid", {}).get(jid)
                        if lid_jid and lid_jid in deleted:
                            continue
                    cleared_cutoff = cleared.get(jid)
                    if cleared_cutoff:
                        # A "clear chat" only wipes messages, it must not make the
                        # conversation disappear from the list (that's delete's job).
                        # Keep the chat entry but strip any last-message/unread state
                        # that predates the clear so the conversation shows as empty
                        # instead of resurrecting the pre-clear preview.
                        last_msg = chat.get("lastMessage")
                        if isinstance(last_msg, dict):
                            try:
                                lm_ts = int(last_msg.get("messageTimestamp", 0) or 0)
                            except (ValueError, TypeError):
                                lm_ts = 0
                            if not lm_ts or lm_ts < cleared_cutoff:
                                chat["lastMessage"] = None
                        if not chat.get("lastMessage"):
                            chat["unreadCount"] = 0
                    # WPPConnect's list-chats returns every entry in WhatsApp's
                    # internal ChatStore, which includes 1:1 "phantom" chats the
                    # user never actually messaged (e.g. address-book contacts
                    # WhatsApp matched but no conversation ever started with) as
                    # well as orphaned/ghost group entries from other sessions.
                    # Real chats always carry a last-activity timestamp ("t"),
                    # a last message, or unread messages; entries with none of
                    # these are not real conversations and would otherwise
                    # pollute the chat list and the forward-message picker.
                    if jid not in chats:
                        has_activity = (
                            bool(chat.get("t"))
                            or bool(chat.get("lastMessage"))
                            or bool(chat.get("unreadCount"))
                        )
                        if not has_activity:
                            if not jid.endswith("@g.us"):
                                continue
                            # For @g.us, also skip ghost groups that have 0 messages, no activity, and no valid name
                            g_name = self._group_name_from_chat_dict(chat) or getattr(self, "_group_name_cache", {}).get(jid, "")
                            if not g_name or g_name.strip() in ("", "Grupo sem nome", "Grupo"):
                                logging.info(f"[get_remote_chats] Skipping ghost/inactive group without name: {jid}")
                                continue
                    # A non-group chat whose last received message actually
                    # belongs to a GROUP is a phantom entry WhatsApp Web creates
                    # in its store for a group participant: the participant's
                    # @lid/@c.us surfaces as a "chat" because of messages they
                    # wrote in groups we are in — never a real 1:1
                    # conversation. list-chats carries that last message under
                    # `lastReceivedKey` (it serialises msgs:null, so
                    # lastMessage is always empty here); real 1:1 chats always
                    # have a lastReceivedKey whose remote is the chat's own
                    # @lid/@s.whatsapp.net.
                    if not jid.endswith("@g.us"):
                        _lrc_jid = self._last_received_jid(chat)
                        if _lrc_jid.endswith("@g.us"):
                            logging.info(
                                f"[get_remote_chats] Skipping phantom group-participant chat: {jid} "
                                f"(lastReceivedKey from {_lrc_jid})")
                            continue
                    if jid not in chats:
                        if "messages" not in chat:
                            chat["messages"] = {"messages": {"records": []}}
                        chat["remoteJid"] = jid
                        if jid.endswith("@g.us"):
                            name = self._group_name_from_chat_dict(chat)
                            if not name:
                                name = getattr(self, "_group_name_cache", {}).get(jid, "")
                                if not name:
                                    name = self._fill_group_name(jid)
                            chat["name"] = name
                        # A snapshot can legitimately lower the count after the
                        # chat was read on another device. Preserve local only
                        # when its activity is newer than this snapshot.
                        if hasattr(self, "chats") and jid in self.chats:
                            local_unread = int(self.chats[jid].get("unreadCount") or 0)
                            server_unread = int(chat.get("unreadCount") or 0)
                            chat["unreadCount"] = reconcile_snapshot_unread(
                                server_unread,
                                local_unread,
                                chat.get("t", 0),
                                self.chats[jid].get("t", 0),
                                bool(self.chats[jid].get("_unread_count_unsynced")),
                            )
                            _log_refused_read_receipt(
                                jid, server_unread, local_unread,
                                chat.get("t", 0), self.chats[jid].get("t", 0),
                                chat["unreadCount"],
                            )
                        # list-chats carries no messages, so this count is raw.
                        note_unread_discount_state(
                            chat,
                            bool(((chat.get("messages") or {}).get("messages") or {})
                                 .get("records")),
                        )
                        chats[jid] = chat
                    else:
                        local_activity_t = int(chats[jid].get("t", 0) or 0)
                        # A snapshot can be BEHIND the messages we already
                        # hold: a restored browser profile comes back with every
                        # chat's `t` from its snapshot, up to a day old. Copying
                        # that down lowered the local marker, and the next round
                        # reconcile_snapshot_unread() saw the snapshot as current
                        # and took its unread counts — putting the list back to
                        # whatever the snapshot had (near zero, for one taken
                        # after a mass mark-as-read). `t` is never lowered below
                        # the newest stored message that counts as the chat's
                        # last one, the same rule sync_chat_messages() applies
                        # when it raises `t` itself. See chat_activity_floor().
                        activity_floor = _chat_activity_floor(
                            chats[jid], ChatListMixin._counts_as_last_message,
                            now=int(time.time()))
                        for k, v in chat.items():
                            if k in ("messages", "remoteJid"):
                                continue
                            if k == "lastMessage" and not v:
                                continue
                            if (k == "t" and activity_floor
                                    and _timestamp_seconds(v) < activity_floor):
                                v = activity_floor
                            if k == "pushName" and jid.endswith("@g.us"):
                                continue
                            if k == "name" and jid.endswith("@g.us"):
                                # Always prefer the freshly-computed name
                                # (groupMetadata.subject when present, see
                                # _group_name_from_chat_dict) over the raw
                                # flat field this loop is iterating — a
                                # renamed group's flat "name" can be a
                                # non-empty but STALE cache, and gating this
                                # on "only when v is blank" is exactly what
                                # let a rename never reach chats[jid] even
                                # after a full resync.
                                fresh = self._group_name_from_chat_dict(chat)
                                if fresh:
                                    v = fresh
                            # Don't let the server overwrite a positive local
                            # unreadCount with a lower one — the local counter
                            # may have incremented since the server snapshot
                            # was taken. But always accept a server-reported
                            # value HIGHER than the local one (e.g. a message
                            # read/sent from another device this session
                            # doesn't know about) so newly-arrived unread
                            # chats still show up.
                            if k == "unreadCount":
                                server_val = int(v or 0)
                                local_val = int(chats[jid].get("unreadCount") or 0)
                                # The server counts system events (promote/join/leave
                                # group notifications, protocolMessage revokes) and
                                # sometimes own (fromMe) sends toward unread. Discount
                                # the same tail is_countable_message() excludes in
                                # on_new_message(), so a snapshot that reports e.g.
                                # "1 unread" for a chat whose only new record is a
                                # group promote doesn't mint a phantom badge here
                                # either. Same correction as on_chat_unread_update().
                                _discount_records = (
                                    (chats[jid].get("messages") or {})
                                    .get("messages", {})
                                    .get("records", [])
                                )
                                # A tail older than the snapshot is not what the
                                # server counted: leave the count raw and let the
                                # post-fetch correction discount it, once.
                                if not records_cover_snapshot(_discount_records,
                                                              chat.get("t", 0)):
                                    _discount_records = []
                                server_val = _discount_non_countable_unread(
                                    _discount_records, server_val,
                                )
                                # An open conversation is reconciled, not zeroed.
                                # This used to be a bare `v = 0` under a comment
                                # claiming parity with on_chat_unread_update() —
                                # there was none. on_new_message() counts messages
                                # that land in the open chat while the window is
                                # hidden (Alt+F4 only hides to tray), so this
                                # 60s-polled merge was wiping a real backlog: badge,
                                # title counter and toast count all reset, and the
                                # next arrival announcing "1 mensagem não lida" for a
                                # conversation holding six. Both surfaces now share
                                # reconcile_open_chat_unread(), so the parity the
                                # comment asserts is a fact rather than a claim.
                                # remote_read_confirmed is False here because
                                # list-chats carries no previousUnreadCount — see the
                                # helper for what that costs.
                                _cp = getattr(self, "conversations_panel", None)
                                # Gated on the read anchor exactly like the live
                                # handler's own _open_now — an open chat whose
                                # read was undone (Ctrl+Shift+M, or a restored
                                # backlog after /send-seen failed) is open but
                                # not read, and answering 0 for it here erases
                                # that state a minute later. See the comment on
                                # _open_now in on_chat_unread_update().
                                open_now = (
                                    _cp is not None
                                    and _cp.conversation is not None
                                    and self._normalize_jid(
                                        _cp.conversation.get("remoteJid", "")
                                    ) == jid
                                    and self._unread_anchored_to_local_read(jid)
                                )
                                if open_now:
                                    _local_new = getattr(
                                        self, "_new_since_read", {}
                                    ).get(jid, 0)
                                    v, _clear_new = reconcile_open_chat_unread(
                                        server_val, _local_new
                                    )
                                    if _clear_new:
                                        getattr(self, "_new_since_read", {}).pop(jid, None)
                                elif server_val < local_val:
                                    # WPPConnect's list-chats snapshot lagging behind
                                    # live on_new_message increments isn't just a
                                    # startup-sync artifact — this periodic resync
                                    # (get_remote_chats(), polled every 60s) can under-
                                    # report the same way well after startup too.
                                    # Silently resetting the count here meant the very
                                    # next live message's toast notification announced
                                    # "1 unread" right after the reset, even though
                                    # several messages had already piled up before it.
                                    merged = reconcile_snapshot_unread(
                                        server_val,
                                        local_val,
                                        chat.get("t", 0),
                                        local_activity_t,
                                        bool(chats[jid].get("_unread_count_unsynced")),
                                    )
                                    if merged == local_val:
                                        continue
                                    v = merged
                                    if merged == 0:
                                        removed_ack = getattr(
                                            self, "_locally_read_at", {}
                                        ).pop(jid, None)
                                        getattr(self, "_new_since_read", {}).pop(jid, None)
                                        if removed_ack is not None:
                                            self._persist_locally_read_at()
                                else:
                                    # A snapshot taken shortly after
                                    # mark_conversation_as_read() cleared this chat
                                    # (the user opened it and left) can still report
                                    # the pre-read unreadCount — WhatsApp Web's own
                                    # server-side read receipt lags behind our local
                                    # /send-seen call. Accepting it here resurrected
                                    # already-read messages into the badge/separator,
                                    # and any live message that then arrived stacked
                                    # its own +1 on top of that resurrected total
                                    # (e.g. 2 already-read + 1 genuinely new = "3
                                    # unread"). Trust the server value again only
                                    # once this chat's own last-activity timestamp
                                    # has actually moved past what it was when we
                                    # marked it read — i.e. a genuinely new message
                                    # has since arrived server-side.
                                    read_at_t = getattr(self, "_locally_read_at", {}).get(jid)
                                    if read_at_t is not None:
                                        incoming_t = int(chat.get("t", 0) or 0)
                                        if incoming_t <= read_at_t:
                                            continue
                                        self._locally_read_at.pop(jid, None)
                                        self._persist_locally_read_at()
                                    # Store the DISCOUNTED count, not the raw
                                    # `v` this loop is iterating. The other two
                                    # branches above already assign `v` from
                                    # server_val (directly, or through
                                    # reconcile_*); this one used to fall
                                    # through and write the raw snapshot value,
                                    # so the discount computed above decided
                                    # the branch and was then thrown away.
                                    #
                                    # That single omission is self-sustaining,
                                    # not cosmetic. The stored count stays at
                                    # the server's number (say 31) while
                                    # sync_chat_messages()'s
                                    # apply_history_sync_unread_correction()
                                    # immediately discounts the same chat back
                                    # down (30). _capture_chat_sync_baseline()
                                    # then snapshots 30, the next 60s round
                                    # merges 31 again, and
                                    # chat_sync_marker_changed() reads 31 != 30
                                    # as "this chat changed" — every round,
                                    # forever, for a chat where nothing
                                    # happened. Confirmed live: 39 consecutive
                                    # "[unread] <jid>: 31 -> 30 after history
                                    # sync" lines, one per minute, same numbers
                                    # every time, and a periodic delta that
                                    # never once reported 0 chats to sync.
                                    #
                                    # The cost lands on the person reading:
                                    # re-syncing the OPEN conversation runs
                                    # _refresh_open_conversation_after_sync()
                                    # -> refresh_messages_if_changed(), whose
                                    # signature legitimately differs, so
                                    # populate_messages() rebuilds the native
                                    # list with DeleteAllItems() + Append().
                                    # A screen reader is handed a brand-new
                                    # list once a minute, mid-sentence, and
                                    # the unread separator and focus go with
                                    # it — which is exactly the symptom
                                    # refresh_messages_if_changed()'s own
                                    # docstring warns about.
                                    v = server_val
                                # A real chat-list snapshot now backs this
                                # chat's unreadCount, whichever way the guards
                                # above resolved it — the notification code can
                                # trust the number again.
                                chats[jid].pop("_unread_count_unsynced", None)
                                note_unread_discount_state(chats[jid], bool(_discount_records))
                            chats[jid][k] = v
                        # The incoming chat dict may carry the group's real
                        # name only under groupMetadata.subject (see
                        # _group_name_from_chat_dict) and may not even have a
                        # "name" key at all, in which case the loop above
                        # never touched chats[jid]["name"] — re-derive from
                        # the raw incoming `chat` (not chats[jid]) so this
                        # still catches it. Not gated on the stored name
                        # being empty: a rename this round has to be able to
                        # replace an already-set (now stale) name too, same
                        # reasoning as the per-key loop above.
                        if jid.endswith("@g.us"):
                            subj = self._group_name_from_chat_dict(chat)
                            if subj and subj != chats[jid].get("name"):
                                chats[jid]["name"] = subj
                                self._group_name_cache = getattr(self, "_group_name_cache", {})
                                self._group_name_cache[jid] = subj

                # Sync mute, pin and archive state from server into DB metadata
                now = int(time.time())
                db_changed = False
                archive_changed = False
                for chat in response_data:
                    if not isinstance(chat, dict):
                        continue
                    raw_jid = chat.get("remoteJid", "")
                    if not raw_jid:
                        continue
                    jid = self._normalize_jid(raw_jid)
                    if "muteExpiration" in chat:
                        mute_expiry = chat["muteExpiration"]
                        mute_jids = self._mute_state_jids(jid)
                        if mute_expiry == -1 or (isinstance(mute_expiry, (int, float)) and mute_expiry > now):
                            for mute_jid in mute_jids:
                                if self._muted_chats.get(mute_jid) != int(mute_expiry):
                                    self._muted_chats[mute_jid] = int(mute_expiry)
                                    db_changed = True
                        else:
                            for mute_jid in mute_jids:
                                if mute_jid in self._muted_chats:
                                    del self._muted_chats[mute_jid]
                                    db_changed = True

                    # ── Archive state: two-way sync ──────────────────────────
                    # This used to be add-only (normalize_chats() could put a
                    # JID into _archived_chats but nothing ever took it out),
                    # so a single spurious/stale "archived" value pinned the
                    # conversation to the Archived tab forever — including
                    # conversations the user had never archived on WhatsApp.
                    # Whenever the server states the archive flag, it wins.
                    raw_archive = chat.get("archive")
                    if raw_archive is None:
                        raw_archive = chat.get("archived")
                    server_archived = _parse_bool_flag(raw_archive)
                    if server_archived is not None:
                        chat["archive"] = server_archived
                        chat["archived"] = server_archived
                        if jid in chats:
                            chats[jid]["archive"] = server_archived
                            chats[jid]["archived"] = server_archived

                        alt_jid = ""
                        if jid.endswith("@lid"):
                            alt_jid = getattr(self, "_lid_to_phone", {}).get(jid, "")
                        else:
                            alt_jid = getattr(self, "_phone_to_lid", {}).get(jid, "")
                        if alt_jid:
                            alt_jid = self._normalize_jid(alt_jid)
                            if alt_jid in chats:
                                chats[alt_jid]["archive"] = server_archived
                                chats[alt_jid]["archived"] = server_archived

                        if server_archived:
                            if jid not in self._archived_chats:
                                self._archived_chats.add(jid)
                                archive_changed = True
                            if alt_jid and alt_jid not in self._archived_chats:
                                self._archived_chats.add(alt_jid)
                                archive_changed = True
                        else:
                            if jid in self._archived_chats:
                                self._archived_chats.discard(jid)
                                archive_changed = True
                            if alt_jid and alt_jid in self._archived_chats:
                                self._archived_chats.discard(alt_jid)
                                archive_changed = True

                    # Check if the JID starts with "0@" (official WhatsApp/system account)
                    is_system = jid.startswith("0@")
                    
                    # Parse and clean pin values to prevent bool() truthiness bug on non-standard fields
                    pin_val = chat.get("pin")
                    if isinstance(pin_val, str):
                        if pin_val.lower() == "true":
                            pin_val = True
                        elif pin_val.lower() == "false":
                            pin_val = False
                        else:
                            try:
                                pin_val = float(pin_val)
                            except ValueError:
                                pin_val = None

                    is_pinned = False
                    if not is_system:
                        if isinstance(pin_val, bool):
                            is_pinned = pin_val
                        elif isinstance(pin_val, (int, float)):
                            is_pinned = pin_val > 1000000
                        


                    if is_pinned:
                        if jid not in self._pinned_chats:
                            self._pinned_chats.add(jid)
                            db_changed = True
                        # Also pin the alternate JID if present
                        if jid.endswith("@lid"):
                            alt = getattr(self, "_lid_to_phone", {}).get(jid, "")
                            if alt:
                                alt_norm = self._normalize_jid(alt)
                                if alt_norm not in self._pinned_chats:
                                    self._pinned_chats.add(alt_norm)
                                    db_changed = True
                        else:
                            alt = getattr(self, "_phone_to_lid", {}).get(jid, "")
                            if alt:
                                if alt not in self._pinned_chats:
                                    self._pinned_chats.add(alt)
                                    db_changed = True
                    elif jid in self._pinned_chats:
                        self._pinned_chats.remove(jid)
                        db_changed = True
                        # Also remove pin from the alternate JID if present
                        if jid.endswith("@lid"):
                            alt = getattr(self, "_lid_to_phone", {}).get(jid, "")
                            if alt:
                                alt_norm = self._normalize_jid(alt)
                                if alt_norm in self._pinned_chats:
                                    self._pinned_chats.remove(alt_norm)
                                    db_changed = True
                        else:
                            alt = getattr(self, "_phone_to_lid", {}).get(jid, "")
                            if alt:
                                if alt in self._pinned_chats:
                                    self._pinned_chats.remove(alt)
                                    db_changed = True

                if db_changed and hasattr(self, "db") and self.db is not None:
                    self.db.set_metadata_json("muted_chats", self._muted_chats)
                    self.db.set_metadata_json("pinned_chats", list(self._pinned_chats))
                if archive_changed and hasattr(self, "db") and self.db is not None:
                    self.db.set_metadata_json("archived_chats", list(self._archived_chats))

                perms_changed = False
                groups_total = groups_with_metadata = 0
                groups_with_announce = groups_with_verdict = 0
                for chat in response_data:
                    if not isinstance(chat, dict):
                        continue
                    jid = self._normalize_jid(chat.get("remoteJid", ""))
                    if not jid.endswith("@g.us"):
                        continue
                    groups_total += 1
                    group_meta = chat.get("groupMetadata")
                    if isinstance(group_meta, dict) and group_meta:
                        groups_with_metadata += 1
                    else:
                        group_meta = {}
                    if (_parse_bool_flag(group_meta.get("announce")) is not None
                            or _parse_bool_flag(chat.get("announce")) is not None):
                        groups_with_announce += 1
                    else:
                        continue
                    previous = dict(getattr(self, "_group_send_perms", {}).get(jid) or {})
                    current = self._record_group_send_perms(jid, chat)
                    if current is None:
                        continue
                    groups_with_verdict += 1
                    if (previous.get("announce") == current["announce"]
                            and previous.get("am_admin") == current["am_admin"]
                            and previous.get("t") == current.get("t")):
                        continue
                    perms_changed = True
                    was_restricted = (bool(previous.get("announce"))
                                      and not bool(previous.get("am_admin")))
                    now_restricted = (bool(current.get("announce"))
                                      and not bool(current.get("am_admin")))
                    if now_restricted != was_restricted:
                        cp = getattr(self, "conversations_panel", None)
                        if cp is not None:
                            wx.CallAfter(cp.refresh_composer_permissions, jid,
                                         bool(previous))
                if perms_changed:
                    self._persist_group_send_perms()
                if groups_total:
                    logging.info(
                        "[get_remote_chats] group metadata shape: %d groups, %d with groupMetadata, "
                        "%d stating announce, %d yielding a usable send-permission verdict",
                        groups_total, groups_with_metadata, groups_with_announce, groups_with_verdict,
                    )

                # Retroactively prune 1:1 phantom chats that slipped into the
                # local cache before this filter existed: no local messages,
                # no server-reported activity, and not deliberately pinned or
                # muted by the user. Only worth the full-dict scan right after
                # a real sync (prune_stale) — the periodic background
                # refresh just wants pin/mute state, so skip it there.
                if prune_stale:
                    response_jids = {
                        self._normalize_jid(c.get("remoteJid", ""))
                        for c in response_data if isinstance(c, dict)
                    }
                    for stale_jid in list(chats.keys()):
                        if stale_jid.endswith("@g.us"):
                            continue
                        if stale_jid not in response_jids:
                            continue
                        if stale_jid in self._pinned_chats or stale_jid in self._muted_chats:
                            continue
                        if stale_jid in cleared:
                            # A conversation the user cleared legitimately has
                            # no messages and no last message — pruning it here
                            # is what made "clear chat" behave like "delete
                            # chat" and drop the conversation off the list.
                            continue
                        stale_chat = chats[stale_jid]
                        has_messages = bool(
                            stale_chat.get("messages", {}).get("messages", {}).get("records")
                        )
                        if has_messages:
                            continue
                        has_activity = (
                            bool(stale_chat.get("t"))
                            or bool(stale_chat.get("lastMessage"))
                            or bool(stale_chat.get("unreadCount"))
                        )
                        if not has_activity:
                            del chats[stale_jid]

                if persist_full:
                    self.save_data(chats, self.contacts)
                elif not defer_chat_save:
                    self._schedule_save()
                return chats
            except Exception as e:
                last_error = e
                logging.warning(
                    "[get_remote_chats] Attempt %d/%d (timeout=%ds) failed: %s",
                    attempt + 1, _ATTEMPTS, _timeout, e,
                )
                if attempt < _ATTEMPTS - 1:
                    time.sleep(_RETRY_SLEEP)
                    continue
            else:
                break

        self._last_chat_fetch_error = last_error
        if last_error and notify_errors:
            wx.CallAfter(self.error_sound.play)
            wx.CallAfter(
                wx.MessageBox,
                f"{self.i18n.t('chat_retrieval_failed')} {last_error}",
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_ERROR,
            )

    def normalize_chats(self, chats):
        db_changed = False
        normalized = {}
        for key, chat in chats.items():
            if key.endswith("@newsletter") or chat.get("remoteJid", "").endswith("@newsletter"):
                continue
            if chat.get("unreadCount") is None:
                chat["unreadCount"] = 0
            raw_arch = chat.get("archive")
            if raw_arch is None:
                raw_arch = chat.get("archived")
            is_arch = _parse_bool_flag(raw_arch)
            if is_arch is True:
                if key not in self._archived_chats:
                    self._archived_chats.add(key)
                    db_changed = True
            elif is_arch is False and key in self._archived_chats:
                # Explicitly not archived — drop the stale membership instead of
                # keeping the conversation stuck in the Archived tab forever.
                self._archived_chats.discard(key)
                db_changed = True
            normalized[key] = chat
        if db_changed and hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("archived_chats", list(self._archived_chats))
        return normalized

    def deduplicate_chats(self, chats: dict) -> dict:
        """
        Merge duplicate chat entries that refer to the same contact but use
        different JID formats:

          1. @c.us (legacy) vs @s.whatsapp.net (modern) for the same phone number.
             Both formats identify the same conversation; we keep @s.whatsapp.net
             and merge any messages from the @c.us entry into it.

          2. @lid (Linked-Device ID) vs @s.whatsapp.net when the @lid chat's
             messages contain a key.remoteJidAlt bridge field that maps
             back to a phone-number JID already present in the chats dict.
             We merge the @lid messages into the @s.whatsapp.net entry and drop
             the @lid duplicate.

        New keys are normalised to @s.whatsapp.net during the merge so that
        subsequent lookups always hit the canonical entry.
        """
        def _merge_records(dst_records: list, src_records: list):
            """Append src messages that are not already in dst (dedup by msg ID)."""
            if not src_records:
                return
            dst_ids = {r.get("key", {}).get("id") for r in dst_records}
            for r in src_records:
                if r.get("key", {}).get("id") not in dst_ids:
                    dst_records.append(r)

        # ── Pass 0: merge phantom "self-referential" chats ────────────────────
        # WPPConnect/Baileys occasionally reports a self-chat send (seen with
        # text, audio and documents) tagged with an identity that isn't our
        # real phone JID — either a group whose JID is built from a
        # participant's own @lid number with "@g.us" swapped in for "@lid",
        # a group JID that's simply our own phone number, or the bare,
        # not-yet-resolved @lid itself left over from before
        # resolve_self_lid() completed (see on_new_message() for the same
        # detection applied to live traffic). No real WhatsApp group JID is
        # ever numerically identical to one of its own participants' JIDs or
        # to a plain phone number — group IDs come from an entirely
        # different, longer ID space — so either digit overlap alone
        # identifies the artifact. Merge its records into the real
        # self-chat and drop it, instead of leaving an unnamed phantom
        # chat that duplicates messages already stored under "Eu" and
        # can't be reliably deleted (any other chat cleared out by the same
        # buggy delete would be a side effect of this same bogus entry, not
        # a separate bug).
        my_jid = getattr(self, "my_jid", "")
        if my_jid:
            my_jid_digits = my_jid.split("@", 1)[0]
            candidate_jids = [
                j for j in list(chats.keys())
                if j.endswith("@g.us") or j.endswith("@lid")
            ]
            for cand_jid in candidate_jids:
                cand_chat = chats.get(cand_jid)
                if cand_chat is None:
                    continue
                cand_digits = cand_jid.split("@", 1)[0]
                records = cand_chat.get("messages", {}).get("messages", {}).get("records", [])
                # Same shape as _redirect_self_chat_artifact()'s own test, and
                # deliberately so — the two must agree or a chat one of them
                # rejects is a chat the other keeps resurrecting. Note fromMe
                # is required only for the non-@g.us case: the group-suffixed
                # artifact can arrive with fromMe=False, so demanding it here
                # left every such chat already saved in messages.db
                # untouchable — the funnels only stop new ones from being
                # created, and this pass is what deals with the old ones.
                #
                # Keeping the relaxation scoped to @g.us is what makes it
                # safe, and candidate_jids also holds @lid: a @lid chat whose
                # digits mirror its participant is the ordinary shape of a
                # legitimate 1:1, where the sender IS the chat. Letting the
                # relaxation reach those would swallow real conversations
                # into "Eu" wholesale.
                is_self_referential = any(
                    r.get("key", {}).get("participant", "").split("@", 1)[0] == cand_digits
                    and (
                        cand_jid.endswith("@g.us")
                        or (r.get("key", {}).get("fromMe")
                            and self._phone_digits_equivalent(cand_digits, my_jid_digits))
                    )
                    for r in records
                    if r.get("key", {}).get("participant")
                )
                is_self_phone_group = (
                    cand_jid.endswith("@g.us")
                    and self._phone_digits_equivalent(cand_digits, my_jid_digits)
                )
                if not (is_self_referential or is_self_phone_group):
                    continue
                # Persist it, like Pass 1 and Pass 2 already do for their own
                # re-keys. Without this the pass only ever filtered in memory:
                # _do_save() writes the chats table through upsert_chat and
                # never deletes, so the phantom's rows — the chat and every
                # message stored under its JID — survived in messages.db and
                # were filtered out again from scratch on every single launch.
                # The user stopped seeing it, which is why this went unnoticed,
                # but nothing ever removed it and its messages never moved to
                # the self-chat they belong to.
                if hasattr(self, "db") and self.db is not None:
                    try:
                        self.db.merge_or_rename_chat(cand_jid, my_jid)
                    except Exception as db_err:
                        logging.error(
                            "[deduplicate_chats] Failed to merge/rename self-chat artifact in DB (%s)",
                            type(db_err).__name__,
                        )
                if my_jid in chats:
                    dst_records = (
                        chats[my_jid]
                        .setdefault("messages", {})
                        .setdefault("messages", {})
                        .setdefault("records", [])
                    )
                    _merge_records(dst_records, records)
                else:
                    cand_chat["remoteJid"] = my_jid
                    chats[my_jid] = cand_chat
                del chats[cand_jid]

        # ── Pass 0b: merge duplicate self-chat digit variants ────────────────
        # Even without any group/participant artifact, WhatsApp sometimes
        # reports our own self-chat messages under the "other" Brazilian
        # 9th-digit variant of our number for a given event (the matching
        # normalisation for live traffic is in on_new_message()) — e.g.
        # sending a photo to yourself as a document created one "Eu" chat
        # for the real document echo and a second "Eu" chat, under the
        # other digit variant, for a sync-artifact echo of the same send.
        # Merge any such leftover duplicate into the canonical my_jid entry.
        if my_jid:
            for other_jid in [
                j for j in list(chats.keys())
                if j != my_jid and j.endswith("@s.whatsapp.net") and self._is_self_jid(j)
            ]:
                other_chat = chats.get(other_jid)
                if other_chat is None:
                    continue
                records = other_chat.get("messages", {}).get("messages", {}).get("records", [])
                if my_jid in chats:
                    dst_records = (
                        chats[my_jid]
                        .setdefault("messages", {})
                        .setdefault("messages", {})
                        .setdefault("records", [])
                    )
                    _merge_records(dst_records, records)
                else:
                    other_chat["remoteJid"] = my_jid
                    chats[my_jid] = other_chat
                del chats[other_jid]

        # ── Pass 1: normalise @c.us → @s.whatsapp.net ────────────────────────
        cus_jids = [j for j in list(chats.keys()) if j.endswith("@c.us")]
        for cus_jid in cus_jids:
            if cus_jid not in chats:
                continue
            normalized = self._normalize_jid(cus_jid)
            cus_chat   = chats.pop(cus_jid)
            cus_chat["remoteJid"] = normalized

            if hasattr(self, "db") and self.db is not None:
                try:
                    self.db.merge_or_rename_chat(cus_jid, normalized)
                except Exception as db_err:
                    logging.error(f"[deduplicate_chats] Failed to merge/rename {cus_jid} to {normalized} in DB: {db_err}")

            if normalized in chats:
                # Both exist — merge messages into the @s.whatsapp.net entry
                dst_records = (
                    chats[normalized]
                    .setdefault("messages", {})
                    .setdefault("messages", {})
                    .setdefault("records", [])
                )
                src_records = (
                    cus_chat.get("messages", {})
                    .get("messages", {})
                    .get("records", [])
                )
                _merge_records(dst_records, src_records)
            else:
                # Only the @c.us version existed — rename it
                chats[normalized] = cus_chat

        # ── Pass 2: merge or rename @lid to its @s.whatsapp.net equivalent ───
        temp_cache = {}
        for jid_key, chat_obj in chats.items():
            for msg in chat_obj.get("messages", {}).get("messages", {}).get("records", []):
                key    = msg.get("key", {})
                remote = key.get("remoteJid", "")
                alt    = key.get("remoteJidAlt", "")
                if alt and alt.endswith("@s.whatsapp.net"):
                    if remote.endswith("@lid"):
                        temp_cache[remote] = alt
                    participant = key.get("participant", "")
                    if participant.endswith("@lid"):
                        temp_cache[participant] = alt
                elif alt and alt.endswith("@lid") and remote.endswith("@s.whatsapp.net"):
                    temp_cache[alt] = remote

        lid_jids = [j for j in list(chats.keys()) if j.endswith("@lid")]
        for lid_jid in lid_jids:
            if lid_jid not in chats:
                continue
            lid_chat = chats[lid_jid]
            alt_jid  = self._find_alt_jid_from_messages(lid_chat) or temp_cache.get(lid_jid)
            if not alt_jid:
                # Fallback: consult the pre-built _lid_to_phone cache
                alt_jid = getattr(self, "_lid_to_phone", {}).get(lid_jid, "")
            if not alt_jid:
                continue  # no phone-number JID found anywhere — keep @lid as-is

            if hasattr(self, "db") and self.db is not None:
                try:
                    self.db.merge_or_rename_chat(lid_jid, alt_jid)
                except Exception as db_err:
                    logging.error(f"[deduplicate_chats] Failed to merge/rename {lid_jid} to {alt_jid} in DB: {db_err}")

            src_records = (
                lid_chat.get("messages", {})
                .get("messages", {})
                .get("records", [])
            )
            if alt_jid in chats:
                # Both exist — merge @lid messages into the @s.whatsapp.net entry
                dst_records = (
                    chats[alt_jid]
                    .setdefault("messages", {})
                    .setdefault("messages", {})
                    .setdefault("records", [])
                )
                _merge_records(dst_records, src_records)
                
                # Merge unread counts (take max, not sum, to avoid duplicating counts across LID and phone entries)
                unread_dst = int(chats[alt_jid].get("unreadCount") or 0)
                unread_src = int(lid_chat.get("unreadCount") or 0)
                chats[alt_jid]["unreadCount"] = max(unread_dst, unread_src)
            else:
                # Only the @lid version exists — rename it to @s.whatsapp.net
                lid_chat["remoteJid"] = alt_jid
                chats[alt_jid] = lid_chat
            del chats[lid_jid]
            
            # Redirect active conversation if it was the merged LID chat
            if hasattr(self, "conversations_panel") and self.conversations_panel.conversation:
                active_jid = self.conversations_panel.conversation.get("remoteJid", "")
                if active_jid == lid_jid:
                    self.conversations_panel.conversation = chats[alt_jid]
                    wx.CallAfter(self.conversations_panel.refresh_messages_if_changed)

        # ── Pass 3: re-key any entry whose dict key drifted from its own
        # remoteJid field ──────────────────────────────────────────────────
        # Every pass above keys strictly off dict keys (self.chats.keys()),
        # so a chat whose stored chat["remoteJid"] no longer matches the key
        # it's actually sitting under — left over from before an earlier
        # merge/rename ran, or written once under a slightly different JID
        # serialization from an older WPPConnect payload shape — slips
        # through every one of them untouched. _compute_chat_lists()'s
        # render-time dedupe only catches two *different* dict keys sharing
        # the exact same remoteJid string; it has no way to notice a key
        # that disagrees with its own entry's remoteJid, and neither did any
        # pass above until now. Left alone, sync_remote_chats() (keyed off
        # these same stale dict keys) keeps re-fetching and re-inserting
        # both, which is how a single real group ended up rendered two or
        # three times — reported live as groups (archived and not)
        # duplicating for some accounts.
        for stale_key in list(chats.keys()):
            chat_obj = chats.get(stale_key)
            if chat_obj is None:
                continue
            canonical = self._normalize_jid(chat_obj.get("remoteJid", "") or stale_key)
            if not canonical or canonical == stale_key:
                continue
            del chats[stale_key]
            if canonical in chats:
                dst_records = (
                    chats[canonical]
                    .setdefault("messages", {})
                    .setdefault("messages", {})
                    .setdefault("records", [])
                )
                src_records = (
                    chat_obj.get("messages", {})
                    .get("messages", {})
                    .get("records", [])
                )
                _merge_records(dst_records, src_records)
            else:
                chat_obj["remoteJid"] = canonical
                chats[canonical] = chat_obj

        return chats

    @staticmethod
    def _group_name_from_chat_dict(chat: dict) -> str:
        """Best-effort group display name from a *raw* WPPConnect chat
        object (list-chats / chats-update shape) — NOT the already-flat
        /group-info response, which puts "subject"/"name" at the top level
        itself and doesn't need this.

        WPPConnect's chat serializer (WAPI._serializeChatObj, confirmed by
        reading the vendored wppconnect library and the group-info
        controller's own `chat?.groupMetadata?.subject` access) nests a
        group's real name under groupMetadata.subject — there is no flat
        "subject" key on a raw chat object. Every call site here that
        checked chat.get("subject") directly was reading a field that
        essentially never exists on this data source, so a group whose name
        hadn't propagated into WhatsApp Web's own metadata cache yet
        (routine right after a fresh pairing, before it finishes lazily
        hydrating group metadata for every group — WhatsApp Web's own
        internal timing, outside this app's control) never picked itself
        back up on a later periodic refresh even once WhatsApp Web did
        catch up, because the fallback was looking in the wrong place.

        groupMetadata.subject is checked FIRST, not last: it is fetched
        fresh on every list-chats round (see the "group metadata shape" log
        line in get_remote_chats()), while the flat "name"/"subject" fields
        are whatever WhatsApp Web's own chat-store cache happened to hold
        and can lag a real rename by an unknown amount. Checking the flat
        fields first meant a renamed group's OLD name (non-empty, so every
        "fall back only when blank" guard downstream skipped right past it)
        permanently won over the correct one already sitting in
        groupMetadata on that very same sync — reported live as group names
        never updating even after a full F5 resync. A brand-new group with
        no groupMetadata yet still falls through to the flat fields exactly
        as before, so first-time naming is unaffected.
        """
        group_meta = chat.get("groupMetadata")
        if isinstance(group_meta, dict):
            gm_subject = (group_meta.get("subject") or "").strip()
            if gm_subject:
                return gm_subject
        name = (chat.get("name") or "").strip()
        if name:
            return name
        subject = (chat.get("subject") or "").strip()
        if subject:
            return subject
        return ""

    def save_data(self, chats, contacts):
        """Write chat+contact data to SQLite via DatabaseBridge.

        Protected by _save_lock so concurrent callers never write at the
        same time.  Replaces the old messages.dat blob with a transactional
        full-state import.
        """
        # Root-level safety net: save_data() is reachable from several
        # background-thread call paths (see _extract_lid_mapping(),
        # resolve_lid_jids_via_api()) that can fire before prepare_sync() has
        # created self.db. Those paths now guard against firing this early,
        # but this no-op keeps a stray/future call from popping the
        # "data_save_failed" error dialog instead of just skipping the write
        # — the next debounced/full save retries once self.db exists.
        if not hasattr(self, "db") or self.db is None:
            logging.warning("[save_data] Called before self.db exists — skipping.")
            return
        with self._save_lock:
            try:
                lid_to_phone = getattr(self, "_lid_to_phone", {})
                unresolvable_lids = list(getattr(self, "_unresolvable_lids", set()))
                unresolvable_names = list(getattr(self, "_unresolvable_names", set()))
                # Incremental upsert — never clear the DB during normal saves.
                # Full-clear is only used by clear_local_data() for account reset.
                self.db.save_full_state({
                    "chats": dict(chats),
                    "contacts": dict(contacts),
                    "lid_to_phone": dict(lid_to_phone),
                    "unresolvable_lids": unresolvable_lids,
                    "unresolvable_names": unresolvable_names,
                    "status_updates": {
                        k: list(v) for k, v in
                        getattr(self, "_status_updates", {}).items()
                    }
                }, clear_first=False)
            except Exception:
                logging.exception("[save_data] Failed to save chat/contact data")
                self.error_sound.play()
                # save_data() is called from many places, often once per
                # incoming message during a sync — a genuinely failing DB
                # (e.g. sustained write contention) used to pop one blocking
                # MessageBox per call with no limit, flooding the screen and
                # making the whole PC sluggish while they piled up. Report it
                # at most once per cooldown window; every failure is still
                # logged above regardless.
                now = time.time()
                last = getattr(self, "_last_save_error_dialog_ts", 0)
                if now - last >= self._SAVE_ERROR_DIALOG_COOLDOWN:
                    self._last_save_error_dialog_ts = now
                    wx.CallAfter(
                        wx.MessageBox,
                        f"{self.i18n.t('data_save_failed')} {format_exc()}",
                        self.i18n.t("error").format(app_name=self.app_name),
                        wx.OK | wx.ICON_ERROR,
                    )

    def _do_save(self):
        """Timer callback: incrementally persist dirty chats and contacts.

        Uses targeted upsert_chat() / upsert_contacts_batch() instead of the
        old save_data() full dump.  This avoids re-encrypting every message
        blob on every save — the dominant source of idle CPU usage.
        """
        # ── 1. Dirty chats ────────────────────────────────────────────────────
        dirty_chats: set[str] = set(getattr(self, "_dirty_jids_for_save", None) or set())
        self._dirty_jids_for_save = set()

        # Only save explicitly dirty chats.  The old "save all as fallback"
        # behaviour wrote every chat on every unrelated event (e.g. mark-as-read,
        # status updates), causing 1-second DB writes even during idle operation.
        for jid in dirty_chats:
            chat = self.chats.get(jid)
            if not chat:
                continue
            try:
                self.db.upsert_chat(jid, chat)
            except Exception as exc:
                logging.warning("[_do_save] upsert_chat %s: %s", jid, exc)

        # ── 2. Contacts (only when explicitly marked dirty) ───────────────────
        if getattr(self, "_contacts_dirty_for_save", False):
            self._contacts_dirty_for_save = False
            try:
                self.db.upsert_contacts_batch(dict(self.contacts))
            except Exception as exc:
                logging.warning("[_do_save] upsert_contacts_batch: %s", exc)

    def _schedule_save(
        self,
        dirty_jid: "str | None" = None,
        contacts_dirty: bool = False,
    ) -> None:
        """Debounce DB saves into one write per burst.

        Parameters
        ----------
        dirty_jid :
            JID of the specific chat that changed.  When supplied, only that
            chat is written to the DB (fast).  When omitted, all chat metadata
            is saved (slower but still far cheaper than a full import_from_dict).
        contacts_dirty :
            Set to True to also flush self.contacts to the contacts table.
        """
        if not hasattr(self, "_dirty_jids_for_save"):
            self._dirty_jids_for_save = set()
        if dirty_jid:
            self._dirty_jids_for_save.add(dirty_jid)
        else:
            # No specific JID given — the caller wants the full chat set
            # persisted (see docstring). _do_save() only writes JIDs found
            # in _dirty_jids_for_save, so without this the "save everything"
            # call was silently a no-op (e.g. resolved group names and
            # mark-as-unread never reached disk).
            self._dirty_jids_for_save.update(self.chats.keys())
        if contacts_dirty:
            self._contacts_dirty_for_save = True
        with self._save_timer_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            t = threading.Timer(0.15, self._do_save)
            t.daemon = True
            self._save_timer = t
            t.start()

    # Stories/status updates are only ever valid for 24h on WhatsApp's own
    # side; nothing pruned this table before, so it grew forever — every
    # status ever received or viewed (payload included) stayed in the
    # database indefinitely. The extra 2h beyond the real 24h lifetime is
    # just slack for clock skew / late delivery, not a feature.
    _STATUS_UPDATE_MAX_AGE_SECONDS = 26 * 3600
    # How often vacuum() is allowed to run at most. VACUUM rewrites the whole
    # file, so it belongs nowhere near "every startup" — this is purely to
    # reclaim space after large deletes (clearing chats, the status pruning
    # above) accumulate over time.
    _VACUUM_MIN_INTERVAL_SECONDS = 7 * 24 * 3600

    def _prune_expired_status_updates(self):
        """Delete stories older than 24h from the DB and the in-memory cache.

        Runs on a background thread — called once at startup, well after the
        cutoff for it to block anything the user is waiting on.
        """
        try:
            cutoff = int(time.time()) - self._STATUS_UPDATE_MAX_AGE_SECONDS
            deleted = self.db.delete_expired_status_updates(cutoff)
            if deleted:
                logging.info("[status_updates] pruned %d expired stories from the database", deleted)
            pruned_memory = 0
            for participant in list(self._status_updates.keys()):
                bucket = self._status_updates.get(participant) or []
                kept = []
                for m in bucket:
                    ts = int(m.get("messageTimestamp", 0) or m.get("timestamp", 0) or 0)
                    if ts > 1_000_000_000_000:
                        ts //= 1000
                    if ts and ts < cutoff:
                        pruned_memory += 1
                    else:
                        kept.append(m)
                if kept:
                    self._status_updates[participant] = kept
                else:
                    self._status_updates.pop(participant, None)
            if pruned_memory and hasattr(self, "navigation_panel"):
                sp = getattr(self.navigation_panel, "status_panel", None)
                if sp and sp.IsShown():
                    wx.CallAfter(lambda: threading.Thread(target=sp._load_statuses, daemon=True).start())
        except Exception as exc:
            logging.warning("[status_updates] failed to prune expired stories: %s", exc)

    def _maybe_vacuum_database(self):
        """Reclaim disk space, throttled to at most once every 7 days.

        Runs on a background thread, well after startup — VACUUM rewrites the
        entire database file, so it must never compete with the app's own
        startup reads/writes for the single SQLite connection.
        """
        try:
            last_run = int(self.db.get_metadata("last_vacuum_ts", "0") or "0")
        except (TypeError, ValueError):
            last_run = 0
        now = int(time.time())
        if now - last_run < self._VACUUM_MIN_INTERVAL_SECONDS:
            return
        try:
            logging.info("[maintenance] Running database VACUUM...")
            self.db.vacuum()
            self.db.set_metadata("last_vacuum_ts", str(now))
            logging.info("[maintenance] Database VACUUM complete.")
        except Exception as exc:
            logging.warning("[maintenance] VACUUM failed: %s", exc)
