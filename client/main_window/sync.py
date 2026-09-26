"""SyncMixin — part of MainWindow (see main_window/__init__.py).

Moved verbatim out of main.py. Methods run with ``self`` bound to the
MainWindow instance, so every attribute set in MainWindow.__init__ is
available here.
"""

import logging
import os
import threading
import time
import wx
from core.database_bridge import DatabaseBridge
from core.remote_reconcile import (
    MAX_MIRRORED_DELETIONS,
    outside_rollback_gaps as _outside_rollback_gaps,
)
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)
from core.incremental_sync import (
    chat_message_records as _chat_message_records,
    chat_sync_marker as _chat_sync_marker,
    classify_chat_sync as _classify_chat_sync,
    select_stale_rechecks as _select_stale_rechecks,
)
from core.api_client import api_post
from ui.dialogs.checkbox_confirm import confirm_with_checkbox
from app_paths import data_path
from core.conversation_resync import (
    deletions_to_apply,
    stale_ids_in_fetched_window,
)
from main_window.message_rules import is_countable_message
from core.utils import prune_chats_messages


class SyncMixin:
    """Full account sync: prepare_sync, _run_sync, start/trigger gates, the resync
    menu actions and the per-chat sync planning (see docs/traps/sync-
    completion.md).
    """

    def _on_menu_toggle_offline(self, event=None):
        """Sincronização menu / Ctrl+Alt+Shift+O: toggle offline mode."""
        self.toggle_offline_mode()

    def _on_menu_sync_media(self, event=None):
        """Sincronização menu: Baixar mídias manualmente em segundo plano."""
        if getattr(self, "_media_sync_running", False):
            if not self.background_mode:
                self.output(self.i18n.t("sync_media_already_running"))
            return

        def _worker():
            if self._should_abort_sync_for_offline():
                logging.info("[_on_menu_sync_media] Aborting media sync: offline mode active.")
                return
            wx.CallAfter(self._set_status, self.i18n.t("downloading_media"))
            if not self.background_mode and self._announce_sync_events_enabled():
                wx.CallAfter(self.output, self.i18n.t("sync_media_started"))
            self._media_sync_running = True
            try:
                self.sync_media_for_all_chats()
                if not self.background_mode and self._announce_sync_events_enabled():
                    wx.CallAfter(self.output, self.i18n.t("sync_media_completed"))
            except Exception as exc:
                logging.exception("[_on_menu_sync_media] Erro ao baixar mídias: %s", exc)
                if not self.background_mode and self._announce_sync_events_enabled():
                    wx.CallAfter(self.output, self.i18n.t("sync_media_failed"))
            finally:
                self._media_sync_running = False
                wx.CallAfter(self._set_status, "")
                wx.CallAfter(self.set_chats)

        threading.Thread(target=_worker, daemon=True, name="menu-media-sync").start()

    def _on_menu_resync_all(self, event=None):
        """Sincronização menu / F5: wipe all local chat/message state and
        force a full resync, exactly as if pairing for the first time."""
        if getattr(self, "_initial_sync_running", False):
            # Avoid corrupting state with two syncs writing to self.chats/db
            # at the same time.
            return
        if getattr(self, "_resyncing_conversations", None):
            # A Shift+F5 still running would write its chat back after the
            # wipe; Shift+F5 refuses while F5 runs for the same reason.
            self.output(self.i18n.t("resync_conversation_busy"), interrupt=True)
            return
        # Ensure we are connected before wiping local data
        self.check_wa_connection_http()
        if not getattr(self, "_wa_connected", False):
            self.error_sound.play()
            wx.MessageBox(
                self.i18n.t("resync_failed_offline"),
                self.i18n.t("app_name"),
                wx.OK | wx.ICON_WARNING,
                self
            )
            return

        if not self._confirm_resync("confirm_resync_all", "resync_all_confirm",
                                    "menu_resync_all"):
            return

        self.output(self.i18n.t("resyncing_all_announcement"), interrupt=True)
        threading.Thread(target=self._resync_all_worker, daemon=True).start()

    def _confirm_resync(self, setting_key: str, message_key: str, title_key: str) -> bool:
        """Ask before F5 / Shift+F5, unless the user turned that off.

        Same contract as the mark-all-read confirmation: the default button is
        No, so a stray keystroke cannot start a resync; "don't show again"
        only counts together with Yes; and user_interface.<setting_key>
        (Settings > Interface) is both what it clears and the way back.
        """
        if not self.settings.get("user_interface", {}).get(setting_key, True):
            return True
        t = self.i18n.t
        confirmed, dont_ask_again = confirm_with_checkbox(
            self,
            t(message_key),
            t(title_key).replace("&", ""),
            t("mark_all_read_dont_show_again"),
            yes_label=t("yes_button"),
            no_label=t("no_button"),
            checked=False,
            default_yes=False,
        )
        if not confirmed:
            return False
        if dont_ask_again:
            self.settings.setdefault("user_interface", {})[setting_key] = False
            self.save_settings()
        return True

    def _on_menu_resync_conversation(self, event=None):
        """Sincronização menu / Shift+F5: F5 for the open conversation only.

        See core/conversation_resync.py for why this fetches first and then
        removes only what the server contradicts, instead of wiping the
        conversation the way F5 wipes everything.
        """
        cp = getattr(self, "conversations_panel", None)
        conversation = getattr(cp, "conversation", None) if cp is not None else None
        if not conversation:
            self.output(self.i18n.t("resync_conversation_none_open"), interrupt=True)
            return
        remote_jid = self._normalize_jid(conversation.get("remoteJid", ""))
        resyncing = getattr(self, "_resyncing_conversations", None)
        if resyncing is None:
            resyncing = self._resyncing_conversations = set()
        if getattr(self, "_initial_sync_running", False) or remote_jid in resyncing:
            self.output(self.i18n.t("resync_conversation_busy"), interrupt=True)
            return
        self.check_wa_connection_http()
        if not getattr(self, "_wa_connected", False):
            self.error_sound.play()
            wx.MessageBox(
                self.i18n.t("resync_failed_offline"),
                self.i18n.t("app_name"),
                wx.OK | wx.ICON_WARNING,
                self
            )
            return
        if not self._confirm_resync("confirm_resync_conversation",
                                    "resync_conversation_confirm",
                                    "menu_resync_conversation"):
            return
        resyncing.add(remote_jid)
        self.output(self.i18n.t("resyncing_conversation_announcement"), interrupt=True)
        threading.Thread(target=self._resync_conversation_worker, args=(remote_jid,),
                         daemon=True, name="resync-conversation").start()

    def _resync_conversation_worker(self, remote_jid: str):
        """Background worker for _on_menu_resync_conversation()."""
        try:
            chat = self.chats.get(remote_jid)
            fetched_ids = set()
            ok = bool(chat) and bool(self.sync_chat_messages(
                chat, sync_mode="full", fetched_ids_out=fetched_ids))
            if not ok or not fetched_ids:
                logging.info("[resync-conversation] %s: nothing fetched (ok=%s)",
                             remote_jid, ok)
                wx.CallAfter(self.output, self.i18n.t("resync_conversation_failed"), True)
                return
            chat = self.chats.get(remote_jid) or chat
            records = _chat_message_records(chat)
            if getattr(self, "_remote_deletions_untrusted", False):
                # A profile restore rolled WhatsApp Web's store back behind our
                # database: its silence about a message proves nothing.
                stale = []
            else:
                # Same rules as the open-chat deletion mirror, including the
                # periods a profile restore left a hole in (core/conversation_resync.py).
                judged = _outside_rollback_gaps(records, self._rollback_gaps())
                stale = stale_ids_in_fetched_window(judged, fetched_ids, is_countable_message)
                apparent = len(stale)
                stale = deletions_to_apply(stale)
                if apparent and not stale:
                    logging.warning(
                        "[resync-conversation] %s: %d apparent deletions exceed the "
                        "cap of %d; none removed", remote_jid, apparent,
                        MAX_MIRRORED_DELETIONS)
            if stale:
                stale_set = set(stale)
                cp = getattr(self, "conversations_panel", None)
                open_jid = (self._normalize_jid(cp.conversation.get("remoteJid", ""))
                            if cp is not None and cp.conversation is not None else "")
                if open_jid == remote_jid:
                    # Through the panel, like a deletion made on the phone: it
                    # also stops a removed audio and drops removed ids from the
                    # selection, which a bare record filter would leave behind.
                    wx.CallAfter(self._mirror_remote_deletions, remote_jid, stale_set)
                else:
                    records[:] = [r for r in records
                                  if ((r.get("key") or {}).get("id") or "") not in stale_set]
                    inner = (chat.get("messages") or {}).get("messages") or {}
                    if isinstance(inner, dict) and "total" in inner:
                        inner["total"] = len(records)
                    for message_id in stale:
                        self.db.delete_message(remote_jid, message_id)
            logging.info("[resync-conversation] %s: %d fetched, %d stale removed",
                         remote_jid, len(fetched_ids), len(stale))
            self._refresh_open_conversation_after_sync(remote_jid, chat)
            self._schedule_set_chats()
            wx.CallAfter(self.output, self.i18n.t("resync_conversation_done"), True)
        except Exception:
            logging.exception("[resync-conversation] %s: failed", remote_jid)
            wx.CallAfter(self.output, self.i18n.t("resync_conversation_failed"), True)
        finally:
            self._resyncing_conversations.discard(remote_jid)

    def _teardown_conversation_ui(self):
        """Empty the conversation panels before the data under them is wiped.

        Shared by the two wipes that run with the UI already up — F5
        (_resync_all_worker()) and the account switch
        (_apply_another_number_wipe()) — which had a verbatim copy of this
        each. None of it is cosmetic: the list keeps rendering rows whose
        chats are about to stop existing, Enter on one of them opens a
        conversation that is gone, and a voice note still playing holds its
        .msv open, so clear_local_data()'s os.unlink raises PermissionError
        on it.

        Marshalled to the main thread and waited on, because both callers run
        on a background thread. The event is set in a finally, so a panel in a
        state this does not expect costs the visible cleanup only — never the
        wipe behind it, and never a thread parked on a wait nobody will set.
        """
        ui_ready = threading.Event()

        def _prepare_ui():
            try:
                panel = self.conversations_panel
                panel._stop_audio()
                panel.close_conversation()
                panel.chats_list = []
                panel.chat_names = []
                panel._all_chats_list = []
                panel._all_chat_names = []
                panel._displayed_jids = None
                panel.conversations_list.DeleteAllItems()
                if hasattr(self, "archived_conversations_panel"):
                    ap = self.archived_conversations_panel
                    ap.chats_list = []
                    ap.chat_names = []
                    ap._all_chats_list = []
                    ap._all_chat_names = []
                    ap._displayed_jids = None
                    ap.conversations_list.DeleteAllItems()
            finally:
                ui_ready.set()

        wx.CallAfter(_prepare_ui)
        ui_ready.wait(timeout=5)

    def _resync_all_worker(self):
        """Background worker for _on_menu_resync_all(). See that method."""
        # Claim this immediately, before clear_local_data() runs — not just
        # inside start_sync() further down. clear_local_data() can take a
        # noticeable moment (wipes the whole DB), and the connection health
        # checker calls trigger_sync_if_needed() every 30s independently; that
        # method's own guard checks this same flag, but only start_sync()
        # itself used to set it, leaving a window where the health checker
        # could see _sync_completed already False (set below) and
        # _initial_sync_running still False, and spawn its own concurrent
        # start_sync() — two overlapping syncs racing on self.chats and on
        # which one's status/sound/speech calls land last.
        self._initial_sync_running = True
        try:
            self._teardown_conversation_ui()

            # Wipe the local database and downloaded media/voice-message caches.
            # F5 is the explicit escape hatch from the incremental strategy: the
            # user asked to reconstruct every chat from WhatsApp, so keep full
            # mode latched across retries until one complete round succeeds.
            self._force_full_sync = True
            self._persist_full_sync_pending("manual-resync")
            self._sync_completed = False
            # A user-requested resync gets a fresh automatic-retry budget, even if
            # earlier syncs this session already burned through it.
            self._sync_retry_count = 0
            # F5 resyncs the chat/message set from WhatsApp again — it must
            # not also discard cleared/deleted/archived/muted/blocked-contact
            # state the user set locally on top of them (see
            # clear_local_data()'s own docstring).
            self.clear_local_data(wipe_metadata=False)
            self._forget_history_exhaustion()
            self._forget_media_failures()

            # Resync from scratch, exactly like a fresh pairing. start_sync()
            # takes over _initial_sync_running from here (it sets it True
            # again itself and clears it in its own finally on any exit path).
            # _sync_completed was just reset to False above, so the shared
            # guard in _try_start_sync_thread() won't treat this as a no-op.
            self._try_start_sync_thread()
        except Exception:
            # Something above raised before start_sync() could take over
            # ownership of _initial_sync_running — clear it ourselves so a
            # crash here doesn't permanently block every future sync attempt.
            logging.exception("[_resync_all_worker] Unhandled error before sync could start")
            self._initial_sync_running = False

    def prepare_sync(self):
        # Diagnostic breadcrumbs: prepare_sync() runs synchronously on the
        # main thread before init_UI()/self.Show()/app.MainLoop() — a hang
        # anywhere in here (or between here and init_UI()) leaves no window,
        # no tray icon, and no way for any wx.CallAfter-based error recovery
        # to ever run, since no event loop exists yet to process it. Reported
        # live as "connected sound plays, then nothing — no window, no
        # error, forever" with no exception ever reaching the crash-log
        # handler either, which rules out a raised-and-caught error and
        # points at a genuine block. These log lines (flushed to disk
        # immediately by the logging handler, unlike anything that needs a
        # window to be shown) exist so the LAST one printed pinpoints exactly
        # which line is stuck, next time this happens.
        logging.info("[prepare_sync] start")
        os.makedirs(data_path(), exist_ok=True)
        self._media_failed_lock = threading.Lock()
        self._media_failed_ids  = self._load_media_failed_ids()
        self.generate_secret_key()
        self.key = self.retrieve_secret_key()
        self.create_basic_files()
        logging.info("[prepare_sync] basic files ready — opening DatabaseBridge")

        # Initialise DatabaseBridge (async→sync bridge)
        self.db = DatabaseBridge(data_path("messages.db"), self.key)
        logging.info("[prepare_sync] DatabaseBridge open — loading metadata")
        self._load_chat_lock_vault()
        # Load persistent metadata from database with fallback/bootstrap from settings.json
        settings_dirty = False
        
        # 1. presence_pushname_map
        if self.db.get_metadata("presence_pushname_map") is None and "presence_pushname_map" in self.settings:
            self._presence_pushname_map = dict(self.settings.pop("presence_pushname_map", {}))
            self.db.set_metadata_json("presence_pushname_map", self._presence_pushname_map)
            settings_dirty = True
        else:
            self._presence_pushname_map = dict(self.db.get_metadata_json("presence_pushname_map", {}))
            
        # 2. deleted_chats
        if self.db.get_metadata("deleted_chats") is None and "deleted_chats" in self.settings:
            self._deleted_chats = set(self.settings.pop("deleted_chats", []))
            self.db.set_metadata_json("deleted_chats", list(self._deleted_chats))
            settings_dirty = True
        else:
            self._deleted_chats = set(self.db.get_metadata_json("deleted_chats", []))
            
        # 3. archived_chats
        if self.db.get_metadata("archived_chats") is None and "archived_chats" in self.settings:
            self._archived_chats = set(self.settings.pop("archived_chats", []))
            self.db.set_metadata_json("archived_chats", list(self._archived_chats))
            settings_dirty = True
        else:
            self._archived_chats = set(self.db.get_metadata_json("archived_chats", []))
            
        # 4. pinned_chats
        if self.db.get_metadata("pinned_chats") is None and "pinned_chats" in self.settings:
            self._pinned_chats = set(self.settings.pop("pinned_chats", []))
            self.db.set_metadata_json("pinned_chats", list(self._pinned_chats))
            settings_dirty = True
        else:
            self._pinned_chats = set(self.db.get_metadata_json("pinned_chats", []))
            
        # 5. muted_chats
        if self.db.get_metadata("muted_chats") is None and "muted_chats" in self.settings:
            self._muted_chats = dict(self.settings.pop("muted_chats", {}))
            self.db.set_metadata_json("muted_chats", self._muted_chats)
            settings_dirty = True
        else:
            self._muted_chats = dict(self.db.get_metadata_json("muted_chats", {}))

        # 6. blocked_contacts — stored as bare phone-digit strings, matching
        # what WPPConnect's /blocklist endpoint returns (see get_block_list()).
        self._blocked_contacts = set(self.db.get_metadata_json("blocked_contacts", []))

        # 6b. exhausted_chats — conversations WhatsApp Web has no older history
        # for. It used to live only in memory, which was fine while the only
        # consumer was a user scrolling up in one conversation: worst case it
        # asked once more per session. The deep backfill walks every chat back
        # to its beginning, so an in-memory-only set would make every relaunch
        # re-walk an account that is already complete — hundreds of pointless
        # round-trips through the one Puppeteer page, for ever.
        self._exhausted_chats = set(self.db.get_metadata_json("exhausted_chats", []))
        # …and the timestamps of the older-history requests that justify those
        # entries. Persisted for the reason _OLDER_REQUEST_GRACE explains: a
        # durable "this chat is finished" may only be written once the phone has
        # had longer than its reply window to answer, and nothing in a single
        # session can establish that. Tolerates the legacy list form (the set
        # this used to be) by treating those entries as asked long ago, which is
        # true — they were written by an earlier run.
        _asked = self.db.get_metadata_json("older_history_requested", {})
        if isinstance(_asked, dict):
            self._older_requested_chats = {
                jid: float(ts) for jid, ts in _asked.items()
                if isinstance(ts, (int, float))
            }
        elif isinstance(_asked, list):
            self._older_requested_chats = {jid: 0.0 for jid in _asked}
        else:
            self._older_requested_chats = {}

        # Conversations the user has actually opened — the gate on asking the
        # phone for older history. Persisted: a chat opened last week is still
        # a chat whose history the user cares about.
        _opened = self.db.get_metadata_json("opened_conversations_v1", [])
        self._opened_conversations = set(_opened) if isinstance(_opened, list) else set()

        # When get-messages last actually ran for each chat, for the staleness
        # net in _plan_message_sync(). Persisted on purpose: a chat last
        # fetched before a restart is exactly as stale afterwards, and starting
        # empty would re-check the whole account on every launch.
        _verified_at = self.db.get_metadata_json("chat_verified_at_v1", {})
        self._chat_verified_at = {
            str(jid): int(ts) for jid, ts in _verified_at.items()
            if isinstance(ts, (int, float))
        } if isinstance(_verified_at, dict) else {}
        self._chat_verified_at_dirty = False

        # How many times the backfill has asked the phone about each chat this
        # session. Deliberately in memory and not persisted, for the same reason
        # _note_verified_activity() is: the bound exists to stop one run asking
        # the same chat forever, and one confirming look per launch is cheap
        # next to permanently writing off a chat that really does have history.
        self._older_request_attempts: dict[str, int] = {}

        # Short/provisional history is also durable. An incremental startup
        # must remember that a chat still owed us history in the previous
        # session; otherwise a restart could turn an unfinished backfill into
        # an apparently healthy unchanged chat and never query it again.
        _pending_backfill = self.db.get_metadata_json("backfill_pending_v1", {})
        if isinstance(_pending_backfill, dict):
            restored_counts = {
                str(jid): max(0, int(count or 0))
                for jid, count in _pending_backfill.items()
                if jid
            }
            self._chats_awaiting_messages = set(restored_counts)
            self._partial_history_counts = restored_counts
            if restored_counts:
                logging.info(
                    "[prepare_sync] restored %d pending history-repair chat(s)",
                    len(restored_counts),
                )

        _message_retry = self.db.get_metadata_json("message_retry_jids_v1", [])
        self._message_retry_jids = (
            {self._normalize_jid(str(jid)) for jid in _message_retry if jid}
            if isinstance(_message_retry, list) else set()
        )
        _history_gaps = self.db.get_metadata_json("history_gap_jids_v1", [])
        self._history_gap_jids = (
            {self._normalize_jid(str(jid)) for jid in _history_gaps if jid}
            if isinstance(_history_gaps, list) else set()
        )
        if self._message_retry_jids or self._history_gap_jids:
            logging.info(
                "[prepare_sync] restored repair state: %d failed-message retry, %d history gap(s)",
                len(self._message_retry_jids), len(self._history_gap_jids),
            )

        # 7. my_jid / my_lid — previously only ever set at runtime by an
        # online host-device/self-LID lookup (check_wa_connection_http(),
        # resolve_self_lid()), never persisted or restored. _is_self_jid()
        # (and therefore the "Eu" self-chat label) silently returned False
        # until that lookup completed, so a cold offline launch showed the
        # self-chat under its raw phone number/pushName until the first
        # successful online sync relabeled it. Restoring the last known
        # values here makes the label correct immediately, offline included.
        self.my_jid = self.db.get_metadata("my_jid") or ""
        self.my_lid = self.db.get_metadata("my_lid") or ""

        # 8. locally_read_at — {jid: chat["t"] at the moment WE marked it read}.
        # mark_conversation_as_read() records it so on_chat_unread_update() and
        # get_remote_chats() can tell a genuinely new unread count apart from
        # WhatsApp Web's own server-side read receipt simply not having caught
        # up with our /send-seen yet. It used to live only in memory, so every
        # restart threw the guard away while the stale server-side count did
        # NOT go away — the next list-chats snapshot then reported the pre-read
        # total against a local 0, which is higher, so it was accepted and
        # already-read messages came back as unread. Reported live as
        # "algumas mensagens ja lidas voltam a aparecer como nao lidas".
        self._locally_read_at = {
            k: int(v or 0)
            for k, v in dict(self.db.get_metadata_json("locally_read_at", {})).items()
        }

        # Durable diagnostic checkpoint for the last successful server-backed
        # refresh. The chat/message records themselves are the incremental
        # baseline; this small record explains in logs/UI what the last round
        # did without duplicating per-chat state into a second database model.
        sync_state = self.db.get_metadata_json("sync_state_v1", {})
        self._last_sync_state = dict(sync_state) if isinstance(sync_state, dict) else {}
        if self._last_sync_state.get("force_full_pending"):
            self._force_full_sync = True
            logging.info("[prepare_sync] restoring pending full-sync recovery latch")

        self._group_send_perms = {
            k: v
            for k, v in dict(self.db.get_metadata_json("group_send_perms", {})).items()
            if isinstance(v, dict)
        }

        if settings_dirty:
            self.save_settings()

        logging.info("[prepare_sync] metadata loaded — loading local chats (bulk DB call, up to 120s)")
        #Get Local Chats
        self.chats = self.get_chats()
        self._reconstruct_last_reactions_from_records()
        logging.info("[prepare_sync] local chats loaded (%d) — loading LID cache", len(self.chats))
        self._load_local_lid_cache()

        def _db_maintenance():
            self._prune_expired_status_updates()
            # Deliberately delayed and sequenced after pruning: VACUUM
            # rewrites the whole database file, so it must never race the
            # startup sync for the single SQLite connection, and the
            # 7-day throttle inside _maybe_vacuum_database() means it is a
            # no-op on most launches anyway.
            time.sleep(120)
            self._maybe_vacuum_database()
        threading.Thread(target=_db_maintenance, daemon=True).start()

        # Build cache first so deduplicate_chats() can use it as a fallback
        # for @lid chats whose messages carry no remoteJidAlt bridge field.
        self._build_lid_to_phone_cache()
        self.chats = self.deduplicate_chats(self.chats)
        self.chats = self.normalize_chats(self.chats)
        self.contacts = self.get_contacts()
        self._clean_contacts_cached()
        # One-time cleanup: slim bloated quoted-message payloads left behind by
        # older versions (full thumbnails / mediaKeys / URLs), which made
        # conversations with many replies slow to open. Runs now that chats,
        # contacts and the LID caches are all loaded, so the debounced save
        # persists the complete record set.
        if prune_chats_messages(self.chats):
            logging.info("[startup] pruned bloated quoted-message data")
            self._schedule_save()
        # Replies already on disk that quote an "Aguardando mensagem" stored
        # before quote recovery existed.
        # Guarded as a whole: this runs inside __init__, where anything that
        # escapes stops WinZapp from starting at all -- this pass already did
        # that once. A stored chat can hold "messages": None, which the
        # chained .get() read raised on.
        try:
            for jid, chat in list(self.chats.items()):
                records = _chat_message_records(chat)
                if records:
                    self._recover_placeholders_from_replies(jid, records)
        except Exception:
            logging.exception("[quote-recovery] startup pass failed")
        self.scan_all_cached_messages_for_mentions()
        # NOTE: the "connected" sound is deliberately NOT played here. Reaching
        # this point only proves the *local* WPPConnect API answered — with no
        # internet the app would happily announce itself connected and then
        # fail every WhatsApp call with 404/Disconnected. _set_wa_connected()
        # plays it once the connection to WhatsApp is actually confirmed.
        # Reset per-session sync guard so on_messages_set() can start a fresh
        # sync.  Without this, _sync_completed stays True from the previous
        # session and messages.set never triggers start_sync() again.
        self._sync_completed = False
        # NOTE: self._status_updates is NOT reset here. It was already loaded
        # from the database by _load_local_lid_cache() a few lines above
        # (keys are sender JIDs, values are lists of normalized message
        # dicts) — resetting it to {} at this point used to silently discard
        # every locally-cached story on every single restart, so the Status
        # tab always came up empty until new stories arrived over the socket.
        # Reset so the 60-s fallback and on_messages_set() can fire.
        # The flag persisted as True across restarts, blocking re-sync on
        # reconnection when the WPPConnect doesn't re-send messages.set.
        self.messages_set_completed = False
        self.wait_messages_set()
        self.start_connection_health_checker()
        logging.info("[prepare_sync] done")

    # Minimum gap between two sync attempts.  The health checker calls
    # trigger_sync_if_needed() every 30 s so an interrupted sync always resumes
    # on its own, but a sync that keeps failing for some other reason must not
    # turn that into a request storm against the Puppeteer session.
    _SYNC_RETRY_COOLDOWN = 120

    # Minimum gap between two "failed to save data" error dialogs (see
    # save_data()) — a sustained DB failure used to pop one per call with no
    # limit at all, often one per incoming message during a sync.
    _SAVE_ERROR_DIALOG_COOLDOWN = 30

    def _try_start_sync_thread(self) -> bool:
        """Atomically start self.sync_thread unless one is already running or
        a sync already completed this session. Returns True if a sync thread
        is now running — either just started by this call, or already
        running/completed from before.

        Every caller that wants to kick off a sync used to do its own
        check-then-create-then-start of self.sync_thread with no lock
        between the check and the start. Several independent triggers can
        fire within milliseconds of each other right after pairing — the
        session-logged WebSocket event (WebSocketClient.on_messages_set)
        and wait_messages_set()'s HTTP-probe fallback (main.py) in
        particular — and each one's plain "existing.is_alive()" check could
        see "not running yet" at the same instant, so both created and
        started their own thread. Reported live: "sincronizando conversas"
        announced to NVDA twice in a row, and — far worse — two sync
        threads hammering the single DatabaseBridge connection with
        concurrent writes hard enough that save_data() started genuinely
        failing, flooding the screen with a error dialog per failure.
        """
        with self._sync_start_lock:
            if getattr(self, "_sync_completed", False):
                return True
            existing = getattr(self, "sync_thread", None)
            if existing is not None and existing.is_alive():
                return True
            self.sync_thread = threading.Thread(target=self.start_sync, daemon=True)
            self.sync_thread.start()
            return True

    def trigger_sync_if_needed(self):
        # Trigger sync only if it hasn't completed, isn't already running, and we are connected.
        if not getattr(self, "_wa_connected", False):
            return
        if getattr(self, "_sync_completed", False) or getattr(self, "_initial_sync_running", False):
            return
        existing = getattr(self, "sync_thread", None)
        if existing is not None and existing.is_alive():
            return
        # Back off further after each failed round (the usual cause is the
        # WhatsApp Web page still busy with its own history sync), but never
        # stop retrying: capped at 10 minutes.
        cooldown = min(
            self._SYNC_RETRY_COOLDOWN * max(1, getattr(self, "_sync_retry_count", 0)),
            600,
        )
        since = time.time() - getattr(self, "_last_sync_attempt_ts", 0)
        if since < cooldown:
            return
        logging.info("[trigger_sync_if_needed] WhatsApp connected and sync is incomplete. Triggering sync thread...")
        self._try_start_sync_thread()

    def start_sync(self):
        # Block until init_UI() completes.  This prevents wx.CallAfter calls
        # below from referencing panels that don't exist yet (which happens when
        # the websocket failed and ShowModal() is still blocking init_UI()).
        if not self._ui_ready_event.wait(timeout=120):
            return  # UI never initialized; bail out silently

        self._initial_sync_running = True
        # Identifies this particular sync run.  _backfill_empty_chats() captures
        # it and stops as soon as it changes, i.e. only when a genuinely *newer*
        # sync has taken over — it must not stop for the sync that spawned it,
        # which keeps running (media phase) long after the backfill starts.
        run_id = getattr(self, "_sync_run_id", 0) + 1
        self._sync_run_id = run_id
        # Latch before _run_sync(), not after: it can bail out early (no
        # WhatsApp connection yet), and in exactly that case there is no sync
        # coming to re-fetch anything, so live events are the only source of
        # new data there is — dropping them would be strictly worse.
        self._sync_ever_started = True
        self._last_sync_attempt_ts = time.time()
        try:
            # Handed down rather than re-read inside (issue #198): a
            # clear_local_data() landing between the assignment above and that
            # re-read would be adopted as this round's own id, and the round
            # would run on as current over the data it was just wiped for.
            # The read-then-write above is still not atomic — which is why the
            # exit wait in _restart_sync_after_another_number_wipe() bumps on
            # every slice rather than once.
            self._run_sync(run_id)
        except Exception:
            logging.exception("[start_sync] Unhandled error during sync")
        finally:
            # Always clear the tray/status text and the running flag, even if
            # something above raised — otherwise an error mid-sync leaves the
            # tray stuck on "preparing to sync" / "synchronizing" forever,
            # since none of those status calls are inside a try/finally and a
            # thread that dies mid-sync never reaches its own clear-status line.
            self._initial_sync_running = False
            # Re-stamp on the way out, not only on the way in. The stamp made
            # at the top of this method is what trigger_sync_if_needed()
            # measures its cooldown from, so a round that takes longer than
            # the cooldown has already exhausted it by the time it finishes —
            # the backoff was structurally dead for exactly the large accounts
            # it exists to protect. Measured on a 937-chat session: 17 seconds
            # between the end of one full round and the start of the next,
            # against a nominal 120 s. Keeping the entry stamp as well means a
            # round that dies immediately still cannot spin.
            self._last_sync_attempt_ts = time.time()
            wx.CallAfter(self._set_status, "")

    @staticmethod
    def _attempts_needed_to_confirm(attempt: int, max_attempts: int,
                                    confirm_attempts: int) -> int:
        """New list-chats attempt budget after the first non-zero answer.

        Returns ``max_attempts`` unchanged when at least ``confirm_attempts``
        attempts already remain after ``attempt`` (0-based), otherwise the
        smallest budget that leaves exactly that many. Never shrinks the budget.

        Pulled out of _run_sync()'s retry loop purely so it can be tested —
        see tests/test_chat_list_settled.py for the failure it prevents.
        """
        remaining = max_attempts - attempt - 1
        if remaining >= confirm_attempts:
            return max_attempts
        return attempt + 1 + confirm_attempts

    # ── WhatsApp Web's in-memory store going missing ────────────────────────
    # list-chats is WPP.chat.list(), which is literally
    # ChatStore.getModelsArray().slice() inside the page — so it answering
    # with an empty list means the in-memory ChatStore is empty, not that the
    # account is. Measured on a real 937-chat session: after the very first
    # successful call (935 chats), *99 consecutive* list-chats attempts across
    # four full sync rounds and 37 minutes answered HTTP 200 with `[]`, while
    # get-messages on the same page kept returning up to 1000 messages per
    # chat (3843 successful calls) — get-messages reads WhatsApp Web's
    # IndexedDB via msgFindBefore(), never the in-memory stores. The page was
    # healthy throughout: state MAIN (NORMAL), no navigation, no detached
    # frame, no "WAPI is not defined". WPPConnect's own log named the same
    # failure from the other side, verbatim: `TypeError: Cannot read
    # properties of undefined (reading 'map')` out of getAllContacts, whose
    # body is `WPP.whatsapp.ContactStore.map(...)` — that store was not empty
    # but *undefined*.
    #
    # /history-sync-status reports storeCounts.chat from the IndexedDB side,
    # and it said 937 on every single check in both captured sessions,
    # including eight seconds before the app accepted 36 chats as a whole
    # account. That number was already being fetched and logged and used for
    # nothing; it is what tells "this account is empty" apart from "the store
    # this call reads is broken", which no chat count on its own can do.
    #
    # A ratio rather than equality because list-chats legitimately answers
    # with fewer chats than the store holds: the visibleChats filter drops
    # activity-less one-to-one entries. Measured healthy ratios: 935/937 and
    # 32/33. Half is far below anything a working page produces.
    _STORE_PLAUSIBLE_RATIO = 0.5
    # Consecutive implausible *and non-growing* answers before the store is
    # declared broken. Both halves are load-bearing, and the growth half is
    # the one that is easy to get wrong: evidence_count includes the local
    # cache, so on a returning account every early answer of a genuinely cold
    # store looks like a regression against it (931 cached vs. an answer of
    # 0, then 100, then 400). Only a count that has stopped moving is
    # evidence of anything. Three rather than two because a fresh pairing can
    # legitimately sit at zero for a moment while the in-memory store hydrates
    # behind the IndexedDB side that storeCounts reads.
    _BROKEN_STORE_CONFIRM = 3
    # Kept, unused by the sync path, and deliberately not deleted: it is the
    # number this codebase used to rebuild the page on, and a reader who finds
    # the round counter needs to be able to find out what it used to mean.
    # Nothing schedules a rebuild from a broken store any more — see the
    # store_broken branch in start_sync().
    _BROKEN_STORE_REPAIR_ROUNDS = 2

    # A list-chats snapshot does not have to equal storeCounts.chat exactly:
    # WA-JS deliberately hides a few activity-less one-to-one entries.  What
    # matters here is that both views describe the same account.  Keep a small
    # one-entry allowance for small accounts and a one-percent allowance for
    # large ones, plus a 95% floor so a one-entry difference cannot mask half
    # of a two-chat account; measured healthy snapshots were 680/682 and
    # 935/937.
    _STORE_SNAPSHOT_TOLERANCE_RATIO = 0.01
    _STORE_SNAPSHOT_TOLERANCE_MIN = 1
    _STORE_SNAPSHOT_MIN_RATIO = 0.95

    @classmethod
    def snapshot_matches_page_store(cls, snapshot_count: int,
                                    wa_web_count: "int | None") -> bool:
        """Whether a non-zero list-chats snapshot agrees with the page store.

        This is intentionally much stricter than ``count_contradicts_page``.
        The latter only proves that an answer is obviously broken; this helper
        proves that a *previous, current-round* answer is credible enough to
        keep when a later WA-JS call collapses to zero.  It never consults a
        prior round's high-water mark, because a count without its matching
        chat payload cannot safely complete the current sync.
        """
        if snapshot_count <= 0 or wa_web_count is None or wa_web_count <= 0:
            return False
        tolerance = max(
            cls._STORE_SNAPSHOT_TOLERANCE_MIN,
            int(wa_web_count * cls._STORE_SNAPSHOT_TOLERANCE_RATIO + 0.999999),
        )
        return (snapshot_count >= wa_web_count * cls._STORE_SNAPSHOT_MIN_RATIO
                and abs(wa_web_count - snapshot_count) <= tolerance)

    @classmethod
    def count_contradicts_page(cls, server_count: int,
                               wa_web_count: "int | None") -> bool:
        """True when list-chats answers with far fewer chats than WhatsApp Web
        itself reports holding.

        A ratio rather than equality because list-chats legitimately answers
        with fewer chats than the store holds — the visibleChats filter drops
        activity-less one-to-one entries. Measured healthy ratios on real
        sessions: 935/937 and 32/33.

        ``wa_web_count`` of None (no endpoint, no answer) or 0 (the page
        agrees there is nothing) is never a contradiction: the first decides
        nothing, and the second is the genuinely-empty account.
        """
        if wa_web_count is None or wa_web_count <= 0:
            return False
        return server_count < wa_web_count * cls._STORE_PLAUSIBLE_RATIO

    @classmethod
    def store_looks_broken(cls, server_count: int, wa_web_count: "int | None",
                           evidence_count: int) -> bool:
        """True when list-chats' answer contradicts the page's own store count.

        ``evidence_count`` is the largest number of chats we have positive
        evidence for — the biggest count this same round already saw, or the
        size of the local cache. It is what keeps a store that is merely still
        loading from being called broken: a loading store only ever grows, so
        an answer below something we already had is a regression, and a
        regression is not loading.

        All three conditions have to hold, and each rules out a specific case
        that must NOT be treated as broken:

        * ``wa_web_count`` unknown (no endpoint, no answer) — decide nothing
          and leave the existing heuristics in charge.
        * The page itself reports no chats — then an empty list-chats agrees
          with it. This is the genuinely-empty account, and also the account
          whose conversations were all deleted from another device: the case
          _settle_deadline_decision()'s docstring calls unresolvable is
          resolved here, correctly, because both sources say zero.
        * No evidence of content above the answer — a first pairing filling in
          0 -> 4 -> 7 -> 11 never regresses, so it never lands here.
        """
        if evidence_count <= server_count:
            return False
        return cls.count_contradicts_page(server_count, wa_web_count)

    def _wa_web_chat_count(self) -> "int | None":
        """How many chats WhatsApp Web itself says it is holding, or None.

        Reads storeCounts.chat from the same /history-sync-status payload
        log_history_sync_status() already prints. Kept separate from
        refresh_history_still_landing() because that one deliberately reads
        only the two queue fields and discards the rest.

        Short timeout on purpose: this is a corroborating probe inside the
        retry loop, not the sync itself. The default 30 s would let a hung
        endpoint add a minute and a half to a round that is already the slow
        path, and an unanswered probe costs nothing — None simply leaves the
        existing heuristics in charge.
        """
        status = self.fetch_history_sync_status(timeout=10)
        if not isinstance(status, dict):
            return None
        counts = status.get("storeCounts")
        if not isinstance(counts, dict):
            return None
        try:
            count = int(counts.get("chat"))
        except (TypeError, ValueError):
            return None
        return count if count >= 0 else None

    # Ceiling for the deadline extensions below. _CHAT_RETRIES (6) x
    # _CHAT_DELAY (5 s) gives the settle loop about 30 seconds, which is not
    # enough for an account whose chat list WhatsApp Web is still streaming in
    # from the phone — the loop ran out, the sync was declared incomplete, and
    # the health checker started it over. Reported live as "it finished
    # syncing, then a while later said it was syncing again", with no
    # connection drop involved. 30 attempts x 5 s caps the wait at ~2.5
    # minutes, past which something is wrong that waiting will not fix.
    _CHAT_ABSOLUTE_MAX_ATTEMPTS = 30
    # Attempts granted per extension: small enough that a list which settles
    # right after the deadline costs one short extra wait, not a full round.
    _CHAT_ATTEMPT_EXTENSION = 2
    @classmethod
    def _settle_deadline_decision(cls, server_count: int, prev_server_count: int,
                                  max_attempts: int, local_chat_count: int = 0) -> str:
        """What to do when the settle loop reaches its last attempt without a
        stable count: ``"extend"``, ``"accept"`` or ``"incomplete"``.

        Two different failures meet at this deadline and they need opposite
        answers, which is why the local cache is consulted rather than the
        snapshot's size alone:

        * A small account really is small. Four conversations is a complete
          sync for someone who has four conversations, and answering
          "incomplete" there is what made the health checker restart the sync
          every time it came round — the user heard "sincronizando" for ever.
          Any non-zero count is therefore accepted once the budget is spent.
        * A server still answering 0 when we hold chats locally has not
          finished loading its store. Accepting that would mark a sync
          complete that plainly is not, and — because only an incomplete sync
          gets retried — would leave the session stuck there with nothing left
          to fix it.

        A bare count cannot tell "the store is still cold" from "this account
        is genuinely empty": both answer 0. `local_chat_count` breaks that tie,
        and ONLY that one. It is deliberately not used to reject a non-zero
        count for merely falling short of the cache: chats absolutely can
        disappear from the server legitimately, because the user deleted them
        from another device, and treating every shortfall as "the server is
        behind" would loop on exactly that. Zero is the only count that needs
        the cache at all, since the loop's own two-equal exit requires
        server_count > 0 to fire — any stable non-zero answer, short of the
        cache or not, settles there long before this deadline, and an unstable
        one is genuinely still moving.

        With no cache to contradict it — a first pairing — a persistent 0 is
        taken at face value as an empty account once the ceiling is reached,
        so a brand-new number does not re-sync for ever either.

        The one case left ambiguous is deleting every conversation from
        another device: that reports 0 against a populated cache, exactly like
        a store that never loaded, and no count can separate the two. It stays
        "incomplete" because retrying is the recoverable failure of the pair —
        the local cache still shows the chats meanwhile, and the retry is
        paced by the health checker rather than being a tight loop, whereas
        accepting a broken store strands the session for good.

        Pulled out of _run_sync()'s retry loop purely so it can be tested —
        see tests/test_chat_list_settled.py.
        """
        # Nothing at all yet, or a list visibly still arriving: more time is
        # the only thing that helps, and only while the ceiling allows it.
        still_arriving = server_count > prev_server_count or server_count == 0
        if still_arriving and max_attempts < cls._CHAT_ABSOLUTE_MAX_ATTEMPTS:
            return "extend"
        if server_count == 0 and local_chat_count > 0:
            return "incomplete"
        return "accept"

    def _should_abort_sync_for_offline(self) -> bool:
        """True once offline mode (manual or auto-detected) has come on while
        this sync is still incomplete.

        Observed live: disconnecting mid-sync flipped auto-offline on but left
        the sync thread running against a dead connection — it eventually
        "finished" (all its HTTP calls failing) around the same time the
        connection came back, and the stale "conversations synchronized"
        sound/announcement fired while the UI was still titled "modo
        offline". Checking this at each phase boundary lets a sync in
        progress stop cleanly instead of racing the offline/online state.
        """
        return bool(getattr(self, "offline_mode", False)) and not getattr(self, "_sync_completed", False)

    def _run_sync(self, run_id=None):
        # Identifies which sync_thread this particular call belongs to — see
        # the guard right before "Mark sync as done" far below, added for
        # issue #199/#198: a round superseded mid-flight by
        # _wipe_local_data_if_another_number_linked() (clear_local_data()
        # bumps _sync_run_id) used to keep running to its own natural end
        # regardless, and could then commit _sync_completed=True over data
        # that belongs to the account it was just wiped for switching away
        # from — silently undoing the wipe's own _sync_completed=False.
        def _current_run_id():
            # The test stubs that bind this method answer any unknown
            # attribute with a fresh lambda each time (see the
            # _verified_activity guard elsewhere in this file for the same
            # hazard) — normalize rather than let two getattr() calls on an
            # unset attribute compare unequal to each other.
            value = getattr(self, "_sync_run_id", 0)
            return value if isinstance(value, int) else 0
        # start_sync() passes the id it just assigned; the test harnesses call
        # this bare, and then it is read here instead.
        my_run_id = run_id if isinstance(run_id, int) else _current_run_id()

        # The early-exit half of the same guard (issue #198). A superseded
        # round used to run on for minutes after the wipe, and every
        # self.chats assignment and set_chats() in it put the PREVIOUS
        # account's conversations back on screen right after the user was
        # told they had been deleted. Checked right before each of those
        # writes and after each long I/O step, not on every write site: the
        # writes that make the list visible are a handful, and between them
        # the round only holds locals.
        #
        # A superseded round returns WITHOUT writing _sync_completed or
        # _sync_retry_count either way — unlike the offline aborts, which
        # own the round and set False. The newer run owns those now, and
        # the corrective sync after an another-number wipe depends on its
        # own False surviving. start_sync()'s finally still clears
        # _initial_sync_running on the way out, which is what the join loop
        # in _restart_sync_after_another_number_wipe() waits for.
        #
        # start_sync() assigns _sync_run_id and hands the same value down, so
        # a round can never see its own start as a supersession — and a bump
        # landing between the two is seen as one.
        def _superseded_at(stage):
            current = _current_run_id()
            if current == my_run_id:
                return False
            logging.info(
                "[start_sync] Abandoning sync run %s %s: a newer run (%s) "
                "took over (clear_local_data or a new start_sync). Writing "
                "nothing further, not even _sync_completed.",
                my_run_id, stage, current,
            )
            return True

        logging.info("[start_sync] Checking WhatsApp connection status...")
        self.check_wa_connection_http()
        for _ in range(25):
            if getattr(self, "_wa_connected", False):
                break
            time.sleep(0.2)
            self.check_wa_connection_http()
        if not getattr(self, "_wa_connected", False):
            # Do NOT sync without a connection.  Every WPPConnect route answers
            # 404 "Disconnected" in this state, so the old behaviour was to
            # announce "synchronizing", spend minutes timing out and then pop a
            # modal error over the screen reader — for something as ordinary as
            # the Wi-Fi being off.  Bail out silently; the connection health
            # checker calls trigger_sync_if_needed() every 30 s and starts this
            # again the moment WhatsApp is reachable.
            logging.warning("[start_sync] No WhatsApp connection — skipping sync until it returns.")
            self._sync_completed = False
            return
        # Give WPPConnect/WA-JS internal stores 1s to settle if needed
        logging.info("[start_sync] WhatsApp connected. Proceeding to sync...")
        time.sleep(1)
        # Before anything this round says or latches. A wipe landing during the
        # connection wait above would otherwise find self.chats empty, latch
        # full mode under the wrong reason ("empty-local-cache") and speak
        # "synchronization_started" with interrupt=True — over the very
        # announcement that the previous number's conversations were deleted.
        if _superseded_at("before announcing the round"):
            return

        # Capture the warm local cache BEFORE list-chats updates its timestamps
        # and lastReceivedKey fields. Ordinary startup/reconnect rounds compare
        # the server snapshot against this baseline and query messages only for
        # chats that changed. A genuinely empty cache (first pairing/F5) latches
        # full mode until a complete round succeeds.
        has_local_chats = len(self.chats) > 0
        if not has_local_chats:
            self._force_full_sync = True
            self._persist_full_sync_pending("empty-local-cache")
        force_full = bool(getattr(self, "_force_full_sync", False))
        sync_baseline = self._capture_chat_sync_baseline()
        sync_mode = "full" if force_full else "incremental"
        full_target_count = 0
        incremental_target_count = 0
        skipped_target_count = 0
        media_scope_jids = None if force_full else set()
        logging.info(
            "[start_sync] mode=%s local_chats=%d last_success=%s",
            sync_mode, len(self.chats),
            getattr(self, "_last_sync_state", {}).get("completed_at", "never"),
        )

        # A heavy full rebuild keeps the historical "Sincronizando" sound and
        # announcement. Warm-cache rounds are intentionally quieter and use a
        # distinct status: the user can tell the client is refreshing state
        # without being told a full synchronization started on every launch.
        def _announce_sync_stage():
            if force_full:
                self._set_status(self.i18n.t("synchronizing"))
                if self._announce_sync_events_enabled():
                    self.synchronizing_sound.play()
                    if not self.background_mode:
                        self.output(self.i18n.t("synchronization_started"), interrupt=True)
                return
            self._set_status(self.i18n.t("updating_conversations"))
            if self._announce_sync_events_enabled():
                self.synchronizing_sound.play()
                if not self.background_mode:
                    announce_start_tts = bool(
                        self.settings.get("speech_content", {}).get(
                            "announce_conversations_update_start", True
                        )
                    )
                    if announce_start_tts:
                        self.output(self.i18n.t("conversations_update_started"), interrupt=False)
        wx.CallAfter(_announce_sync_stage)

        # After first pairing the API may need a few seconds to populate chats.
        # Retry only when starting cold (no local cache); if we already have
        # local chats just refresh once and move on — the API is ready.
        _CHAT_RETRIES  = 6
        _CHAT_DELAY    = 5  # seconds between retries
        # A fully failed call already burns ~6 min internally (5 escalating
        # attempts), so cap how many *failures* we sit through here; the
        # post-sync retry scheduled further down picks it up again later
        # without blocking the UI.
        _CHAT_MAX_FAILURES = 2
        local_chat_count = len(self.chats)
        chat_list_ok = False   # did list-chats ever actually answer?
        chat_list_settled = False  # …and did it answer with a stable snapshot?
        failures     = 0
        chat_list_error = None
        disconnected = False
        # Number of chats the *server* returned on the previous successful
        # attempt.  WhatsApp Web fills its chat store progressively after a
        # (re)connection, so the first answer can legitimately be "4 chats"
        # while the account has hundreds — accepting it is what left users with
        # three or four synced conversations and a sync that then restarted
        # itself over and over.  Waiting for two consecutive answers of the
        # same size means we only ever sync a settled snapshot.
        prev_server_count = -1
        # Extra attempts granted once, the first time the server answers with a
        # non-zero count, so that answer always gets a chance to be confirmed by
        # a second one.  Without this the "two consecutive equal counts" rule
        # above is unsatisfiable in the single most common startup shape: a cold
        # WhatsApp Web store answers 0 chats over and over and only fills in on
        # the very last attempt (observed live: attempts 1-5 → 0, attempt 6 →
        # 498).  The loop then ran out, chat_list_settled stayed False, and a
        # sync that had in fact fetched every chat and every message correctly
        # declared itself incomplete — permanently, for that run.
        #
        # This grants time, it does not weaken the rule: a genuinely still-growing
        # store (4 → 7 → 11) never becomes settled here, which is what keeps the
        # "user left with three or four synced conversations" failure fixed.
        _CHAT_CONFIRM_ATTEMPTS = 2
        max_attempts = _CHAT_RETRIES
        saw_nonzero  = False
        attempt      = -1
        # See store_looks_broken(): the largest chat count we have positive
        # evidence for this round, and the largest count WhatsApp Web itself
        # has claimed. Both only ever grow — a store that is loading does not
        # shrink — so keeping the maximum rather than the latest reading is
        # what makes a regression detectable at all.
        wa_web_count    = None
        broken_readings = 0
        store_broken    = False
        # Best list-chats payload actually received in *this* round.  A later
        # empty response must not erase it when the page's independent store
        # count confirms that the earlier snapshot represented the account.
        # self.chats already holds the merged payload; the count is the proof
        # that lets us finish this round without starting the whole sync over.
        best_server_count = 0
        # The previous attempt's count, whatever it was — distinct from
        # prev_server_count, which the settle rules only advance on the paths
        # that reach the bottom of the loop. A count that is still growing is
        # a store still loading, and must never be counted as broken however
        # far below the page's own total it currently is.
        last_count      = -1
        while True:
            attempt += 1
            if attempt >= max_attempts:
                break
            if self._should_abort_sync_for_offline():
                logging.info("[start_sync] Aborting sync: offline mode activated mid-sync.")
                self._sync_completed = False
                return
            # persist_full=False: the full clear-and-reimport writes every
            # message of every chat, and running it once per attempt meant a
            # 931-chat account rewrote its whole message store up to 30 times
            # per round (measured at ~4 s a time, ~8 minutes of redundant
            # writes across the captured session). Nothing needs it here —
            # sync_chat_messages() persists each chat incrementally a few
            # lines below, and the mute/pin/archive metadata this call learns
            # is written by its own set_metadata_json() regardless of the
            # flag. prune_stale keeps the one thing persist_full also gated,
            # the phantom-chat sweep, which is a plain in-memory dict scan.
            result   = self.get_remote_chats(dict(self.chats), persist_full=False,
                                             prune_stale=True, notify_errors=False,
                                             defer_chat_save=True)
            # After the fetch, not before it: `result` is merged over the
            # dict(self.chats) taken before the call, so a clear_local_data()
            # that ran while list-chats was in flight would be undone by the
            # assignment just below.
            if _superseded_at("after list-chats"):
                return
            if result is None:
                if getattr(self, "_last_chat_fetch_disconnected", False):
                    # WhatsApp went down mid-sync: stop immediately, stay
                    # incomplete, and let the health checker restart us.
                    disconnected = True
                    chat_list_error = getattr(self, "_last_chat_fetch_error", None)
                    logging.warning("[start_sync] Chat list unavailable — WhatsApp disconnected.")
                    break
                # The call itself failed (timeout/HTTP error). This must never
                # be mistaken for "the API is ready": self.chats keeps growing
                # in parallel through the WebSocket (on_new_message), so the
                # "new chats appeared" exit condition below would break out of
                # the loop and leave the sync running on nothing but the handful
                # of chats WhatsApp happened to push over the socket.
                failures += 1
                chat_list_error = getattr(self, "_last_chat_fetch_error", None)
                logging.warning(
                    "[start_sync] Chat list fetch failed (%d/%d): %s",
                    failures, _CHAT_MAX_FAILURES, chat_list_error,
                )
                if failures >= _CHAT_MAX_FAILURES:
                    break
                # Status intentionally stays "synchronizing" through this
                # retry — regressing it back to "preparing_to_sync" here
                # (the previous behaviour) broke the stage ordering the UI
                # promises the user (conectando -> preparando-se para
                # sincronizar -> sincronizando -> baixando mídias -> pronto):
                # "preparando" is the state *before* the sincronizando
                # announcement fires, never after.
                time.sleep(_CHAT_DELAY)
                continue
            self.chats   = result
            chat_list_ok = True
            server_count = getattr(self, "_last_chat_fetch_count", 0)
            best_server_count = max(best_server_count, server_count)
            logging.info(
                "[start_sync] list-chats attempt %d returned %d chats (previous: %d, local cache: %d)",
                attempt + 1, server_count, prev_server_count, local_chat_count,
            )

            # ── Is this answer even possible? ────────────────────────────
            # Checked before any of the settle rules below, because every one
            # of them takes the count at face value: a broken store answering
            # 0 is read as "still arriving" and extended 30 times, and a
            # broken store answering 36 against no local cache is *accepted*
            # as the whole account. Both were captured live on the same
            # 937-chat session. See store_looks_broken() for why the three
            # conditions there are the ones that avoid false positives.
            # Evidence is the high-water mark of what list-chats has itself
            # answered *this session*, and deliberately NOT the local cache.
            # The cache would make every early answer of a genuinely cold
            # store look like a regression on a returning account (931 cached
            # against 0, then 100, then 400) — the commonest startup shape
            # there is, and one the code has a documented live capture of
            # (attempts 1-5 -> 0, attempt 6 -> 498). A count this same session
            # already produced cannot be explained by the store still warming
            # up, which is what makes it evidence at all.
            evidence_count = getattr(self, "_chat_list_high_water", 0)
            if evidence_count > server_count:
                # Re-read rather than caching one reading for the round: the
                # count can still be filling in, and only a later, higher
                # answer can turn a wrongly-innocent verdict into the right
                # one. At most _BROKEN_STORE_CONFIRM + 1 calls per round,
                # since a confirmed break exits the loop.
                latest = self._wa_web_chat_count()
                if latest is not None:
                    wa_web_count = max(wa_web_count or 0, latest)

            # WA-JS can answer correctly once and then collapse to [] while
            # IndexedDB and the rest of the page remain healthy.  The captured
            # large-account failure was 680 -> 0 with storeCounts.chat=682.
            # Keep that real, current-round snapshot instead of declaring the
            # store broken, recreating the session and re-running message/media
            # sync forever.  A partial 36/937 snapshot deliberately fails this
            # strict check and remains incomplete.
            if (server_count < best_server_count
                    and self.snapshot_matches_page_store(
                        best_server_count, wa_web_count)):
                logging.warning(
                    "[start_sync] list-chats regressed from a credible %d-chat "
                    "snapshot to %d while WhatsApp Web reports %s. Keeping the "
                    "earlier current-round snapshot and treating the chat list "
                    "as settled; message history will continue per chat.",
                    best_server_count, server_count, wa_web_count,
                )
                chat_list_settled = True
                break
            still_growing = server_count > last_count
            last_count = server_count
            if (not still_growing
                    and self.store_looks_broken(server_count, wa_web_count, evidence_count)):
                broken_readings += 1
                logging.warning(
                    "[start_sync] list-chats answered %d chat(s) (the page's "
                    "in-memory ChatStore) while %s chat(s) sit in WhatsApp Web's "
                    "IndexedDB and we have evidence for %d (reading %d/%d) — two "
                    "different stores, and the in-memory one is the empty side.",
                    server_count, wa_web_count, evidence_count,
                    broken_readings, self._BROKEN_STORE_CONFIRM,
                )
                if broken_readings >= self._BROKEN_STORE_CONFIRM:
                    store_broken = True
                    break
                time.sleep(_CHAT_DELAY)
                continue
            broken_readings = 0
            self._chat_list_high_water = max(evidence_count, server_count)

            # First non-zero answer: make sure at least _CHAT_CONFIRM_ATTEMPTS
            # attempts remain so it can be confirmed by a second, equal one.
            # See _CHAT_CONFIRM_ATTEMPTS above for why the loop cannot be left
            # to run out here.
            if server_count > 0 and not saw_nonzero:
                saw_nonzero = True
                extended = self._attempts_needed_to_confirm(
                    attempt, max_attempts, _CHAT_CONFIRM_ATTEMPTS
                )
                if extended != max_attempts:
                    logging.info(
                        "[start_sync] First non-zero chat count (%d) arrived on attempt %d "
                        "with only %d attempt(s) left — extending to %d so it can be confirmed.",
                        server_count, attempt + 1, max_attempts - attempt - 1, extended,
                    )
                    max_attempts = extended
            # Exit the retry loop as soon as either:
            #  (a) the server returned the same number of chats twice in a row
            #      (its store has settled — this is the normal exit), or
            #  (b) the server already accounts for everything we had cached
            #      locally, which on a reconnection means it is fully warmed up, or
            #  (c) we've exhausted retries.
            settled = server_count > 0 and server_count == prev_server_count
            covers_cache = has_local_chats and local_chat_count > 0 and server_count >= local_chat_count
            if settled or covers_cache:
                chat_list_settled = True
                break
            if attempt == max_attempts - 1:
                # Out of attempts without two equal counts. See
                # _settle_deadline_decision() for what each outcome means and
                # why the thresholds are what they are.
                decision = self._settle_deadline_decision(
                    server_count, prev_server_count, max_attempts, local_chat_count
                )
                if decision == "extend":
                    max_attempts += self._CHAT_ATTEMPT_EXTENSION
                    logging.info(
                        "[start_sync] Chat list is still arriving (%d -> %d), extending to %d attempts...",
                        prev_server_count, server_count, max_attempts,
                    )
                    prev_server_count = server_count
                    time.sleep(_CHAT_DELAY)
                    continue
                if decision == "accept":
                    # Last check before a snapshot becomes "the whole account
                    # for this session": does the page agree there is nothing
                    # more? This is the failure that amputated a real account
                    # — a first pairing with no cache to contradict it took
                    # 36 chats as the complete list of 937 and never retried,
                    # eight seconds before the same session logged
                    # wa_web_chats=937. Only reachable once the full attempt
                    # budget is spent (~2.5 minutes), so unlike the early
                    # break above it cannot fire on a store that is merely
                    # slow: one that has not filled in by now is not filling in.
                    #
                    # Deliberately count_contradicts_page() and not
                    # store_looks_broken(): the regression evidence the latter
                    # demands is what keeps the *early* break off a cold
                    # store, and there is nothing left to be early about here.
                    if wa_web_count is None:
                        wa_web_count = self._wa_web_chat_count()
                    if self.count_contradicts_page(server_count, wa_web_count):
                        logging.error(
                            "[start_sync] Refusing to accept %d chat(s) as the whole "
                            "account: WhatsApp Web reports %s in its own store. Marking "
                            "the store as not answering instead of syncing an amputated "
                            "account.", server_count, wa_web_count,
                        )
                        store_broken = True
                        break
                    logging.warning(
                        "[start_sync] Chat list never settled (last count: %d) but the extension "
                        "budget is spent — accepting this snapshot rather than declaring the sync "
                        "incomplete and having the health checker start it over.", server_count,
                    )
                    chat_list_settled = True
                    break
                logging.warning(
                    "[start_sync] Chat list still empty after every attempt while %d chat(s) are "
                    "cached locally — treating the server store as not loaded, so this sync stays "
                    "incomplete and the health checker will retry it. (If every conversation was "
                    "genuinely deleted from another device this retries until the cache is "
                    "rebuilt — see _settle_deadline_decision.)",
                    local_chat_count,
                )
                break
            prev_server_count = server_count
            # See the comment on the other retry branch above — status stays
            # "synchronizing" here too.
            time.sleep(_CHAT_DELAY)
        if disconnected:
            # Not an error the user has to acknowledge — just no connection.
            # Leave the sync marked incomplete and stop here so nothing else
            # hammers the API; the health checker resumes it automatically.
            logging.info("[start_sync] Aborting sync: WhatsApp is disconnected.")
            self._sync_completed = False
            return

        if store_broken:
            # Deliberately NOT settled and NOT accepted: this snapshot is
            # known-wrong, so it must neither be announced as a finished sync
            # nor be allowed to shrink the chat list. self.chats keeps
            # whatever it already held — get_remote_chats() only merges, and
            # its one pruning pass is keyed on JIDs present in the response,
            # which is empty here.
            self._broken_store_rounds = getattr(self, "_broken_store_rounds", 0) + 1
            # This used to recreate the WPPConnect session after two such
            # rounds, on the reasoning that "nothing short of rebuilding the
            # page recovers a store in this state". A field log falsified it
            # directly, and the escalation is gone rather than retuned.
            #
            # Measured: the rebuild ran exactly as designed — browserClose, a
            # fresh browser, the pinned document served again, a new session
            # reaching inChat, all inside seven seconds — and list-chats
            # answered 0 again THREE SECONDS LATER, against the same 938 chats
            # in IndexedDB it had been answering 0 against before. Four more
            # rounds followed, each detecting the same thing.
            #
            # And rebuilding is not merely useless here, it is destructive:
            # WPP.chat.list() reads the page's in-memory ChatStore, which a new
            # document starts empty and fills from IndexedDB. Tearing the page
            # down throws away whatever progress it had made and starts that
            # over — which is the most plausible reading of why the ONE
            # non-zero answer in the whole session (37 chats, five seconds
            # after the session came up) was never built on.
            #
            # What is left is what the rest of this branch already did: refuse
            # the known-wrong snapshot, keep the sync incomplete, and let the
            # health checker come back. That is strictly better than a rebuild
            # for the case above, and no worse for the case the escalation was
            # written for — where re-asking did not help either, over 37
            # minutes, WITHOUT anyone having rebuilt anything.
            logging.error(
                "[start_sync] WhatsApp Web's in-memory chat store answered %d "
                "while IndexedDB holds far more (round %d). Not rebuilding the "
                "page: a rebuild empties that store and restarts the load from "
                "scratch, which is measurably what this state does not need. "
                "Messages are unaffected — get-messages reads IndexedDB — so "
                "this sync continues with the chats already known locally and "
                "the health checker retries.",
                server_count, self._broken_store_rounds,
            )
        else:
            # A plausible answer clears the tally, which is now purely a
            # diagnostic: it says how many consecutive rounds saw the store
            # empty, and nothing acts on it.
            self._broken_store_rounds = 0
        if not chat_list_ok:
            # Report once, after every attempt is exhausted, instead of one
            # modal dialog per attempt interrupting the screen reader.
            wx.CallAfter(self.error_sound.play)
            wx.CallAfter(
                wx.MessageBox,
                f"{self.i18n.t('chat_retrieval_failed')} {chat_list_error}",
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_ERROR,
            )
        # Computed, then checked, then assigned: normalize_chats() reads
        # self.chats, and a wipe landing between that read and the assignment
        # would be undone by it.
        normalized = self.normalize_chats(self.chats)
        if _superseded_at("after normalizing the chat list"):
            return
        self.chats = normalized

        # Quick initial contacts fetch — may be incomplete on first QR pairing
        # because WhatsApp delivers contacts to the WPPConnect concurrently
        # with messages.  We'll do a second, definitive fetch after messages are
        # synced (by then the API has received all contacts from WhatsApp).
        self.get_remote_contacts()

        # Show the contact list immediately from get_remote_chats() metadata
        # (name, pushName, unreadCount) so the user is not staring at a blank
        # screen while the per-chat message sync runs below.
        # No need to rebuild the LID cache here: prepare_sync() already built
        # it from the local cache before this point, and nothing since then
        # (get_remote_chats() only touches chat-list metadata, not per-message
        # data) could have added anything new for it to find — a full rescan
        # of every message in every chat would just reproduce the same cache.
        if _superseded_at("before showing the chat list"):
            return
        wx.CallAfter(self.set_chats)

        # ── Phase 1: sync only chats that need message I/O ────────────────
        if self._should_abort_sync_for_offline():
            logging.info("[start_sync] Aborting sync before phase 1: offline mode activated mid-sync.")
            self._sync_completed = False
            return
        # Before a single get-messages call goes out: unjam WhatsApp Web's
        # history-sync queue if an earlier run deadlocked it (the notifications
        # live in its IndexedDB and survive restarts), and record whether it
        # still has chunks to decode. Both have to happen *here*, not after the
        # sync: get-messages can only ever return what WhatsApp Web has already
        # decoded, and _note_backfill_state() uses the pending-chunk flag to
        # decide whether a chat that came back short is worth re-querying while
        # the rest of its history is still landing.
        if force_full:
            unblock_result = self.unblock_history_sync()
            if self._recent_history_needs_wait(unblock_result):
                # should_stop lets the wait itself end on a supersession instead
                # of sitting out its ten-minute budget — the one step here long
                # enough to hold a wiped round alive on its own. It compares
                # the id without logging, since the wait polls every 2 s.
                waited = self.wait_for_restarted_history_sync(
                    should_stop=lambda: _current_run_id() != my_run_id)
                # Before the session_gone branch writes _sync_completed, and
                # before the list-chats refresh below spends another request.
                if _superseded_at("after the RECENT history wait"):
                    return
                if not waited:
                    if getattr(self, "_history_wait_outcome", "") == "session_gone":
                        logging.warning(
                            "[start_sync] The session went away during the RECENT "
                            "history wait; deferring the message phase — there is "
                            "nothing left to query against."
                        )
                        self._sync_completed = False
                        return
                    # Budget exhausted while the phone is still pushing history.
                    #
                    # This used to `return` here too, and that is the whole of the
                    # "it syncs nothing and keeps re-announcing Sincronizando"
                    # report: a first pairing on a large account does not finish
                    # its RECENT pass inside the wait, so every round waited the
                    # full budget, threw the round away, and the health check
                    # started another one — with sync_remote_chats() never running
                    # once. Nothing was fetched, and _resolve_missing_group_names()
                    # (which lives past the message phase) never ran either, so
                    # groups fetched with ignoreGroupMetadata stayed unnamed.
                    #
                    # Proceed instead, which is what this code did before the wait
                    # existed at all: get-messages returns whatever WhatsApp Web
                    # has decoded SO FAR, and _history_still_landing tells
                    # _note_backfill_state() those short answers are provisional,
                    # so each chat is re-queried as the rest of its history lands.
                    # A partial sync that fills in beats an empty one that repeats.
                    logging.warning(
                        "[start_sync] RECENT history did not finish inside the wait — "
                        "running the message phase anyway against what has landed so "
                        "far; short chats will be re-queried as the rest arrives."
                    )
                    self._history_still_landing = True
                # list-chats was captured before the phone finished its transfer.
                # Refresh it now so chats delivered during the wait are part of
                # this same sync instead of waiting for a later health-check round.
                #
                # Reached on both outcomes that go on to sync — the RECENT pass
                # completing, and the budget running out while it is still
                # arriving — since chats can have landed during the wait either
                # way.
                #
                # get_remote_chats() takes the dict to merge into and *returns*
                # the merged result; it does not mutate self.chats. Calling it
                # bare raised TypeError on every single run that got this far,
                # which _run_sync()'s own `except Exception` then swallowed into
                # one "[start_sync] Unhandled error during sync" line — back when
                # this was the wait's only non-deferring exit, that meant
                # sync_remote_chats() could not run at all on any account whose
                # RECENT pass was still incomplete at sync time.
                # None means "the chat list is unknown", never "there are no
                # chats" — keep what we already had in that case.
                refreshed = self.get_remote_chats(dict(self.chats), persist_full=False,
                                                  notify_errors=False,
                                                  defer_chat_save=True)
                if refreshed is not None:
                    refreshed = self.normalize_chats(refreshed)
                # The refresh is a request of its own: same reason as the check
                # after the settle loop's fetch.
                if _superseded_at("after the post-wait list-chats refresh"):
                    return
                if refreshed is not None:
                    self.chats = refreshed
                    wx.CallAfter(self.set_chats)
        else:
            logging.info(
                "[start_sync] Warm-cache incremental round: skipping the RECENT "
                "history restart and wait; changed chats will widen on demand."
            )
            # Fallback for the read below, warm path only.
            # refresh_history_still_landing() answers an unreadable status by
            # preserving the previous value and defaults to True when there is
            # none — and on an installation whose client/api/ predates the
            # /history-sync-status route (it is one of WinZapp's own patches)
            # the status is unreadable on every call, so that True would latch
            # for the whole session: phase 2 media auto-download deferred
            # permanently, since _start_deferred_media_sync() only ever fires
            # from the backfill loop once a later read comes back False.
            #
            # False here is not a new guess. It is exactly what this branch
            # asserted before it started reading the status at all, so an
            # install without the route keeps the behaviour it already had, and
            # it matches what every *reader* of this flag already assumes when
            # it is missing (each one is a getattr(..., False)).
            #
            # hasattr, and deliberately not a plain assignment: a value already
            # read this session — by a cold round, or by an earlier warm one
            # while the route was answering — is real knowledge, and a blind
            # False every round would discard it precisely when the status goes
            # briefly unreadable, which is the moment carrying it over is worth
            # most. (hasattr answers the real question here only because
            # MainWindow defines no __getattr__: every attribute it does not
            # hold is genuinely absent. The test stub does define one, and has
            # to opt out of it to reproduce this branch at all.)
            #
            # And inside the else, not in front of the read below: seeding it
            # on both paths — or in __init__/prepare_sync() — is the tidier
            # shape and the wrong trade, because it would also decide the COLD
            # path. That path's True default is conservative on purpose
            # ("assume history is still arriving"), and on a build without the
            # route it is the only thing keeping a first pairing from retiring
            # every short chat minutes later while the phone is still decoding.
            if not hasattr(self, "_history_still_landing"):
                self._history_still_landing = False
        # Read on BOTH paths, and never asserted. The warm branch used to set
        # this False outright, which is a claim it had no evidence for and got
        # wrong in one specific, reachable case: a chat restored from
        # backfill_pending_v1 has `previous is not None`, so if it answers short
        # and did not grow, every term of _note_backfill_state()'s short-page
        # rule (still_landing or grew or first_short_page) is False and the chat
        # leaves the repair queue — while WhatsApp Web is still decoding the
        # rest of its history. Nothing puts it back: _backfill_empty_chats()
        # only re-reads the queue. Getting there takes an ordinary restart — a
        # cold round finishes with chats queued (the queue deliberately does not
        # block completion), the user reopens the app minutes later, and the
        # first warm round retires them.
        #
        # This does NOT put the RECENT restart/wait back on the warm path. That
        # skip is the whole point of the warm round and it stands:
        # unblock_history_sync() *restarts* the phone's RECENT pass and
        # wait_for_restarted_history_sync() blocks for up to ten minutes, and
        # neither runs here. This is one GET on /history-sync-status — the
        # thing that tells a short chat that is early from one that is short.
        #
        # The context differs per path on purpose: [history-sync] lines are the
        # first thing read when a user reports missing history, and a log that
        # cannot tell a warm round from a cold one costs a diagnosis.
        #
        # What reading this costs, stated plainly, because it is not free.
        # refresh_history_still_landing() ORs three signals, and the third is
        # `recentCompleted is False` — which our own Node patch documents as
        # diagnostic only: "a session whose recent sync was interrupted leaves
        # this false forever, so nothing schedules work off it"
        # (api_patches/src/controller/deviceController.ts, getHistorySyncStatus).
        # This read now does schedule work off it. In that state every warm
        # round sees landing=True, so _note_backfill_state() keeps every short
        # chat queued, phase 1 never drains the queue, it is persisted to
        # backfill_pending_v1, and the next launch pays a FULL page per restored
        # chat before draining — the incremental round's whole saving, spent.
        #
        # It does terminate, but not here and not in this process: the stuck
        # flag is repaired in-page by request_older_messages() (deviceController
        # patches WhatsApp's own getHistorySyncStatus getter once the
        # notification table proves no chunk can be overtaken), which runs from
        # _backfill_empty_chats() AFTER this phase and does not survive a page
        # reload. So the cost above is real, bounded by that repair, and paid
        # only by sessions whose RECENT pass was interrupted.
        #
        # Narrowing the third signal here (`... is False and queued`) was
        # considered and refused: refresh_history_still_landing()'s own
        # docstring says that signal exists precisely for the case where the
        # decoder queue is empty right now, so `and queued` would make it
        # redundant with the first signal and delete the reason it is there.
        # The function is also shared with the cold path, and rewriting it is
        # the scope creep this change has already declined twice.
        self.refresh_history_still_landing(
            context="before message sync" if force_full
            else "before warm message sync")

        # Decide message I/O only after the final chat-list snapshot (and, for
        # a fresh pairing, after the RECENT wait/refresh) so chats that arrived
        # during setup are included in this same round. A broken ChatStore is
        # a recovery condition: get-messages still works, so query every known
        # chat rather than trusting absent activity markers.
        effective_full = bool(force_full or store_broken)
        full_targets, incremental_targets, skipped_target_count, target_reasons = (
            self._plan_message_sync(sync_baseline, force_full=effective_full)
        )
        full_target_count = len(full_targets)
        incremental_target_count = len(incremental_targets)
        if not effective_full:
            media_scope_jids = {
                self._normalize_jid(chat.get("remoteJid", ""))
                for chat in full_targets + incremental_targets
                if isinstance(chat, dict) and chat.get("remoteJid")
            }
        else:
            media_scope_jids = None

        reason_counts = {}
        for reason in target_reasons.values():
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        logging.info(
            "[start_sync] Message plan: full=%d incremental=%d unchanged=%d reasons=%s",
            full_target_count, incremental_target_count, skipped_target_count,
            reason_counts,
        )

        _sync_phase1_started = time.time()
        message_failures = set()
        # expected_run_id makes the phase itself stop, not just this method
        # after it: phase 1 is where a round spends its minutes, and every
        # chat it finishes lands in self.chats. See sync_remote_chats() for
        # why a superseded phase reports no failures at all.
        if full_targets:
            message_failures.update(
                self.sync_remote_chats(full_targets, incremental=False,
                                       expected_run_id=my_run_id) or set()
            )
        if incremental_targets:
            message_failures.update(
                self.sync_remote_chats(incremental_targets, incremental=True,
                                       expected_run_id=my_run_id) or set()
            )
        message_sync_ok = not message_failures
        logging.info(
            "[start_sync] message phase finished in %.1fs (%d full, %d incremental, "
            "%d skipped, %d failed)",
            time.time() - _sync_phase1_started, full_target_count,
            incremental_target_count, skipped_target_count, len(message_failures),
        )

        # After messages are loaded, remoteJidAlt bridge fields are available
        # so @lid ↔ @s.whatsapp.net duplicates (introduced because the API
        # returned both JID formats before messages were fetched) can now be
        # fully resolved and merged.
        #
        # Checked after deduplicate_chats() and before the assignment, for the
        # reason given at normalize_chats() above — and this is also the check
        # that ends a round superseded during the message phase, before its
        # message_sync_ok can decide anything (CLAUDE.md, "Sync completion —
        # the trap that keeps being rediscovered") and before the three
        # requests below.
        deduplicated = self.deduplicate_chats(self.chats)
        if _superseded_at("after the message phase"):
            return
        self.chats = deduplicated

        # Re-resolve group names that were still empty right after pairing.
        # WPPConnect doesn't have every group's metadata (subject) cached
        # immediately after a fresh pairing — group-info lookups made during
        # the initial fast chat-list fetch can come back empty. By now the
        # (much slower) per-chat message sync above has given WPPConnect time
        # to receive that metadata from WhatsApp, so retry once here before
        # the chat list is shown, instead of leaving the group stuck on the
        # generic "unknown group" placeholder for the rest of the session.
        self._resolve_missing_group_names()

        # Re-fetch contacts now that sync_remote_chats() has finished.  The
        # message sync takes long enough that by this point the WPPConnect
        # has received all contacts from WhatsApp — solving the first-pairing
        # issue where names were missing because the initial fetch was too early.
        self.get_remote_contacts()
        self.get_block_list()

        # ── Refresh chat-level state now that messages are indexed ───────────
        # The unreadCount/pin/archive values used so far came from the very
        # first list-chats call, made before WhatsApp Web had finished syncing
        # its chat store — so conversations that really do have unread messages
        # showed up as read, and stayed that way until the 5-minute background
        # poll happened to correct them (which users experienced as "it takes
        # forever to notice the conversation is unread").  One extra call here
        # settles it right after the message sync, while messages_set_completed
        # is already True so server-reported counts are accepted as truth.
        refreshed = self.get_remote_chats(dict(self.chats), persist_full=False,
                                          notify_errors=False, defer_chat_save=True)
        # Same reason as the check after the settle loop's fetch; nothing
        # between here and the set_chats() below does I/O.
        if _superseded_at("after the chat-state refresh"):
            return
        if refreshed is not None:
            self.chats = refreshed

        # Leave unresolved @lid JIDs to the background name backfill. Resolving
        # even one chunk here is serial and keeps the client in "synchronizing"
        # after every conversation and its messages are already available.
        unresolved_lids = [
            jid for jid in self.chats.keys() 
            if jid.endswith("@lid") and jid not in getattr(self, "_lid_to_phone", {})
        ]
        if unresolved_lids:
            logging.info(
                "[Sync] Deferring %d unresolved @lid chat(s) to background name backfill.",
                len(unresolved_lids),
            )
            # …and actually hand them over. This said "deferring" and then did
            # nothing: the only producers for that queue are per-message (a
            # group message's sender, and @lid mentions), so a chat whose own
            # JID is an @lid was bridged only if the same person happened to
            # turn up as a sender or a mention somewhere. Measured on a real
            # install: 132 of 159 chats are @lid and 244 of 487 contacts are,
            # while _lid_to_phone held 42 entries.
            #
            # Everything downstream of that gap follows from it, because
            # contact_dedup_key() collapses the two forms of one person only
            # once the bridge holds the pair. Reported as duplicated contacts
            # in the "new conversation" picker — 208 names appearing twice on
            # that install, each once as @lid and once as the phone — and the
            # same unbridged @lid is why those chats need a name backfill at
            # all.
            #
            # Deferring is still honoured: _queue_lid_resolutions()' drain loop
            # sleeps until _sync_completed, so this costs the sync nothing. It
            # is a set, so re-queuing an already-pending JID on a later round
            # is free, and resolve_lid_jids_via_api() skips whatever the bridge
            # or _unresolvable_lids already answers for.
            self._queue_lid_resolutions(unresolved_lids)

        # Conversations are fully sorted as soon as messages are synced.
        # Sort, display, play sync-complete sound, and announce to the user
        # NOW — before the slower media-download phase begins.
        # Rebuild the LID cache first so the chat list shows correct names.
        self._build_lid_to_phone_cache()
        wx.CallAfter(self.set_chats)
        wx.CallAfter(self.preselect_conversations)

        # Same bundling as the "synchronizing" announcement above — status,
        # sound and speech for this stage transition all happen in one
        # wx.CallAfter so they land together instead of the sound/speech
        # (previously fired directly on this background thread) visibly
        # outrunning the queued status-text clear.
        def _announce_messages_synced():
            self._set_status("")
            # Only announce completion when the chat list really came from the
            # server — otherwise this is a partial sync that is about to be
            # retried, and saying "conversations synchronized" over the few
            # chats the socket delivered is exactly what makes the failure
            # invisible to the user. …and only when that list had settled:
            # announcing "conversations synchronized" over a chat list the
            # server was still filling in is precisely what made the
            # 3-or-4-conversations failure look like a success, right before
            # the sync restarted itself.
            # Re-checked at the moment this actually runs (wx.CallAfter,
            # so possibly well after the round below decided anything): a
            # round superseded by an another-number wipe (or an F5/logout
            # racing the same round — see _current_run_id() above) must not
            # speak "conversations synchronized" for data that isn't the
            # current account any more, even though nothing here writes
            # _sync_completed and so nothing is actually lost by it.
            if (chat_list_ok and chat_list_settled and message_sync_ok
                    and _current_run_id() == my_run_id
                    and self._announce_sync_events_enabled()):
                self.sync_complete_sound.play()
                if effective_full:
                    if not self.background_mode:
                        self.output(self.i18n.t("sync_complete"))
                elif not self.background_mode:
                    announce_tts = bool(
                        self.settings.get("speech_content", {}).get(
                            "announce_conversations_update_complete", True
                        )
                    )
                    if announce_tts:
                        self.output(self.i18n.t("conversations_update_complete"), interrupt=False)
        wx.CallAfter(_announce_messages_synced)

        # Mark sync as done for this session so late-arriving messages.set
        # events (WPPConnect sends them in batches) don't restart the full
        # sync process after it already completed successfully.
        # Mark sync as done for this session ONLY if we actually had an active
        # WhatsApp connection to query new messages. If we synced while disconnected,
        # we only loaded the local cache, so keep _sync_completed = False so we can
        # trigger a real sync once WhatsApp connects.
        # `chat_list_ok` is required too: when every list-chats attempt failed,
        # self.chats holds only what the WebSocket pushed in the meantime (a
        # fraction of the account), so marking the sync completed would leave
        # the session permanently half-synced — trigger_sync_if_needed() checks
        # that same flag and would never run a real sync again.
        # `chat_list_settled` is required for the same reason: a snapshot the
        # server was still growing is a partial account, not a finished sync.
        #
        # A general safety net, not a special case for one caller: anything
        # that bumps _sync_run_id (start_sync() itself, F5's
        # _resync_all_worker(), a confirmed logout, and — the case this was
        # written for — _wipe_local_data_if_another_number_linked() via
        # clear_local_data()) out from under a round already running, rather
        # than cancelling that round outright, leaves exactly this gap open.
        # The another-number wipe is the sharpest instance: it cannot wait
        # minutes for a sync to notice (see
        # _restart_sync_after_another_number_wipe()'s own docstring — issue
        # #198/#199), so it always races a round already in flight. Without
        # this check, a round that captured the PREVIOUS account's chats
        # would reach here after the wipe, find its own non-empty self.chats
        # and a settled/successful round, and commit _sync_completed=True
        # over the account it was just wiped for switching away from —
        # silently undoing the wipe's own _sync_completed=False and leaving
        # trigger_sync_if_needed() with no reason left to ever start the
        # corrective full sync. The earlier _superseded_at() checks make
        # reaching here superseded rare (a bump in the few local steps since
        # the last one), but this is the one write that must never slip
        # through. What none of those checks can stop — a request already
        # under way when the bump lands — is listed in
        # _restart_sync_after_another_number_wipe()'s docstring, together
        # with the clear_local_data() callers (a confirmed logout among them)
        # that have no second wipe to empty it afterwards.
        #
        # It returns rather than falling through: everything below belongs
        # to the round that owns the account. _backfill_empty_chats() in
        # particular captures _sync_run_id when it STARTS, so a backfill
        # spawned from here would adopt the newer run's id and keep writing
        # the previous account's history as if it were current.
        current_run_id = _current_run_id()
        if current_run_id != my_run_id:
            logging.info(
                "[start_sync] A newer sync run (%s) started while this one "
                "(%s) was still finishing — not committing its outcome "
                "either way; the newer round owns _sync_completed now.",
                current_run_id, my_run_id,
            )
            return
        elif (len(self.chats) > 0 and getattr(self, "_wa_connected", False)
                and chat_list_ok and chat_list_settled and message_sync_ok):
            self._sync_completed = True
            self._sync_retry_count = 0
            self._force_full_sync = False
            self._persist_successful_sync_state(
                "full" if effective_full else "incremental",
                full_target_count, incremental_target_count, skipped_target_count,
            )
            # list-chats was deliberately kept memory-only until every selected
            # message delta succeeded. Commit the settled snapshot now; a crash
            # before this point therefore leaves the previous on-disk activity
            # marker intact and the next launch will select the chat again.
            self._schedule_save()
        else:
            self._sync_completed = False
            self._sync_retry_count = getattr(self, "_sync_retry_count", 0) + 1
            # No bespoke retry thread and no "give up for this session" cap any
            # more.  Both were bugs in practice: the retry thread returned
            # immediately when _wa_connected was False (i.e. exactly when the
            # cause was a dropped connection), so a sync interrupted by an
            # internet outage was never resumed even after the connection came
            # back.  The connection health checker now owns retrying — it runs
            # every 30 s for the whole session and calls trigger_sync_if_needed(),
            # which only fires while connected and honours a growing cooldown.
            logging.info(
                "[start_sync] Sync incomplete (chat_list_ok=%s, settled=%s, "
                "message_sync_ok=%s, chats=%d) — the health checker will retry it "
                "(attempt %d so far).",
                chat_list_ok, chat_list_settled, message_sync_ok,
                len(self.chats), self._sync_retry_count,
            )

        # Start the background chat/contact poller before the media phase, not
        # after it: media downloads can run for many minutes, and until this
        # loop existed nothing refreshed unread badges in the meantime.
        self.start_periodic_contacts_sync()

        # Before the backfill even starts, record whether WhatsApp Web is in a
        # state where *any* amount of retrying could help. See
        # log_history_sync_status() — a dead worker bridge makes the backfill,
        # the media phase and every future sync equally pointless, and until
        # this line existed there was no way to tell that from the log.
        self.log_history_sync_status(context="after initial sync")

        # Chats whose history WhatsApp Web had not loaded into its store yet get
        # retried on their own thread — see _backfill_empty_chats(). It runs in
        # parallel with the media phase below because both are silent, best-effort
        # and can take minutes; the backfill's first pass is 30 s away, and the
        # user should not have to wait out the media downloads to get history.
        # A freshly paired session is the case with nothing pending *yet* and
        # everything still to come: the phone delivers its history over the
        # following minutes, and nothing pushes it to WinZapp when it lands. So
        # the loop starts on either signal — chats to re-query, or history still
        # on its way — and it is the loop that decides when to stop.
        pending = len(self._collapse_and_list_backfill_pending())
        still_landing = self.refresh_history_still_landing(context="after initial sync")
        # Unresolved names are a third reason to run, and until the inline
        # pass above was capped there was never a case where they were the
        # ONLY reason — it resolved every one of them before getting here, at
        # the cost of blocking the sync for minutes. Now that it hands the
        # remainder over, leaving this condition on messages alone would strand
        # them: a chat list where every chat holds a full page but hundreds
        # still show a raw @lid, with nothing left to fix it this session.
        unnamed = len(self._pending_name_resolution())
        # Chats that already hold a full page but whose older history has never
        # been walked. This was the one reason to run that the condition below
        # did not ask about, and it is the reason that outlives the others: the
        # ordinary backfill queue drains, names get resolved, history stops
        # landing — and a deep walk that may still owe thousands of pages was
        # never started, because the thread it lives in was never created.
        #
        # It bites exactly when the account is in its steady state. Every chat
        # holds its 200 messages, every name resolves, so `pending`,
        # `unnamed` and `still_landing` are all zero and the sync ends
        # announcing success with the older history untouched. Worse, that is
        # also the state a restart lands in, so the walk had no way to resume
        # either: the SQLite anchor survives, but nothing ever came back for it.
        deep_pending = len(self._chats_needing_deep_history())
        if pending or still_landing or unnamed or deep_pending:
            logging.info(
                "[backfill] Scheduling backfill: %d chat(s) short of a full page, "
                "%d still unnamed, %d awaiting a deeper walk, history still "
                "landing=%s.",
                pending, unnamed, deep_pending, still_landing)
            existing = getattr(self, "_backfill_thread", None)
            if existing is None or not existing.is_alive():
                self._backfill_thread = threading.Thread(
                    target=self._backfill_empty_chats, daemon=True, name="chat-backfill")
                self._backfill_thread.start()

        # ── Phase 2: download media ──────────────────────────────────────────
        # Opt-out via Settings > Armazenamento > "Baixar mídias automaticamente
        # ao sincronizar" (on by default). Runs on this same background sync
        # thread — the window is already open and responsive by this point
        # (UI init finished long before _run_sync), so this only delays when
        # "sync complete" fires, not startup itself. sync_if_media() still
        # applies the day/size caps from the same settings tab per message.
        if not self.settings.get("storage", {}).get("auto_download_media", True):
            logging.info("[start_sync] Phase 2 media auto-download skipped (disabled in settings).")
        elif media_scope_jids is not None and not media_scope_jids:
            logging.info(
                "[start_sync] Phase 2 media auto-download skipped — warm refresh "
                "found no chats with message changes."
            )
        elif getattr(self, "_history_still_landing", False):
            logging.info(
                "[start_sync] Phase 2 media auto-download deferred — "
                "RECENT history is still landing.")
            self._media_sync_deferred = True
        elif not getattr(self, "_sync_completed", False):
            # An incomplete chat-list round will be retried by the health
            # checker. Scanning every cached media record on each such round
            # is pure duplicate work (8k+ candidates on the captured account)
            # and makes the user hear/see a media phase that downloaded zero.
            # Message history has its own durable per-chat backfill and does
            # not depend on this media scan.
            logging.info(
                "[start_sync] Phase 2 media auto-download skipped — the chat-list "
                "round is incomplete and will be retried."
            )
        elif not getattr(self, "_wa_connected", False) or getattr(self, "offline_mode", False):
            # Nothing can be downloaded while offline: every sync_if_media()
            # call returns at its first line. Running the phase anyway used to
            # announce "download de mídias iniciado em segundo plano" and,
            # 71 ms later, "download de mídias concluído" — a full start/finish
            # pair spoken over a phase that fetched nothing at all. Skip it
            # outright; the health checker's next sync retry runs it for real
            # once the connection is back.
            logging.info("[start_sync] Phase 2 media auto-download skipped — not connected.")
        else:
            logging.info("[start_sync] Phase 2 media auto-download starting.")
            self._media_sync_running = True
            announced = False
            try:
                # Verifica se existem mídias a serem baixadas antes de anunciar
                _MEDIA_TYPES = {"audioMessage", "documentMessage", "imageMessage",
                                "stickerMessage", "videoMessage",
                                "audio", "ptt", "document", "doc", "image", "sticker", "video"}
                media_chats = (
                    list(self.chats.values())
                    if media_scope_jids is None
                    else [
                        chat for key, chat in self.chats.items()
                        if self._normalize_jid(chat.get("remoteJid") or key) in media_scope_jids
                    ]
                )
                has_media = any(
                    msg.get("messageType") in _MEDIA_TYPES or msg.get("type") in _MEDIA_TYPES
                    for chat in media_chats
                    for msg in chat.get("messages", {}).get("messages", {}).get("records", [])
                )
                if has_media:
                    wx.CallAfter(self._set_status, self.i18n.t("downloading_media"))
                    if not self.background_mode and self._announce_sync_events_enabled():
                        announced = True
                        wx.CallAfter(self.output, self.i18n.t("sync_media_started"))

                # Only a round that committed as current gets here, but a wipe,
                # F5 or logout can still land during a phase that runs for
                # minutes, and every file fetched after that lands in media/
                # with nothing on disk referring to it any more.
                count = self.sync_media_for_all_chats(
                    media_scope_jids,
                    should_stop=lambda: _current_run_id() != my_run_id)
                logging.info("[start_sync] Phase 2 downloaded %d media file(s).", count)
                if _superseded_at("during the media phase"):
                    # Neither "concluído" nor "falhou": the start was spoken
                    # for data that is gone now, and whatever superseded this
                    # round speaks for itself (F5 and the corrective sync
                    # announce their own start). The finally below still
                    # clears the status text.
                    return
                # Announce the outcome iff the start was announced, so the two
                # always come in pairs — a screen-reader user left with a
                # "iniciado" and no ending has no way to tell a finished phase
                # from a hung one. What the ending *says* depends on whether
                # the connection survived: dropping mid-phase makes every
                # remaining download a no-op, and calling that "concluído"
                # is the same lie this whole block exists to stop telling.
                if announced:
                    if getattr(self, "_wa_connected", False) and not getattr(self, "offline_mode", False):
                        wx.CallAfter(self.output, self.i18n.t("sync_media_completed"))
                    else:
                        logging.warning(
                            "[start_sync] Phase 2 lost the connection mid-download "
                            "(%d file(s) fetched before it dropped).", count)
                        wx.CallAfter(self.output, self.i18n.t("sync_media_failed"))
            except Exception:
                logging.exception("[start_sync] Phase 2 media auto-download failed")
                if announced:
                    wx.CallAfter(self.output, self.i18n.t("sync_media_failed"))
            finally:
                self._media_sync_running = False
                wx.CallAfter(self._set_status, "")
            logging.info("[start_sync] Phase 2 media auto-download finished.")
        # Final refresh so any media-resolved previews appear in the list.
        wx.CallAfter(self.set_chats)
        # _initial_sync_running is reset by start_sync()'s finally block.

    def _probe_chats_and_start_sync(self) -> bool:
        def _already_syncing() -> bool:
            if self.messages_set_completed:
                return True
            existing = getattr(self, "sync_thread", None)
            if existing and existing.is_alive():
                return True
            return getattr(self, "_sync_completed", False)

        if _already_syncing():
            if getattr(self, "_sync_completed", False):
                wx.CallAfter(self._set_status, "")
            return True
        try:
            url = (
                f"{self.wpp_server}:{self.wpp_port}"
                f"/api/{self.token}/list-chats"
            )
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
            r = api_post(
                url,
                json={"ignoreGroupMetadata": True},
                headers=headers,
                timeout=5,
            )
            if r.ok and isinstance(r.json(), list):
                self.messages_set_completed = True
                self._try_start_sync_thread()
                return True
            if self._check_wa_connection_closed(r):
                logging.warning(
                    "[wait_messages_set] list-chats answered Disconnected (HTTP %s) — "
                    "ending the probe instead of syncing against a dead session; "
                    "the health checker retries once WhatsApp is reachable.",
                    r.status_code,
                )
                return True
        except Exception:
            pass
        return False

    def wait_messages_set(self):
        # _set_wa_connected() is what sets "preparing_to_sync" now, exactly
        # when the connected sound plays — not here. This function runs
        # (from prepare_sync(), synchronously during __init__, before the
        # window/tray even exist) well before the connection is actually
        # confirmed; setting the status here made the title claim progress
        # the app hadn't made yet, decoupled from the sound that's supposed
        # to mark the same milestone.
        # Fallback: WPPConnect does not emit a messages.set WebSocket event.
        # Poll the API every 5 s for up to 60 s and start sync as soon as it
        # responds.  If the API never responds within the window, start sync
        # unconditionally so the program never stays stuck on "preparing to sync".
        def _fallback():
            # Probe immediately — when the server is already connected (no
            # session-logged event fires), this avoids an unnecessary 5-second wait.
            if self._probe_chats_and_start_sync():
                return

            for _ in range(12):   # 12 × 5 s = 60 s maximum
                time.sleep(5)
                if self._probe_chats_and_start_sync():
                    return

            # 60 s elapsed and sync still hasn't started — start it unconditionally
            # so the program never stays stuck on "preparando para sincronizar".
            self.messages_set_completed = True
            self._try_start_sync_thread()
        threading.Thread(target=_fallback, daemon=True).start()

    _INCREMENTAL_MESSAGE_WINDOW = 50

    def _capture_chat_sync_baseline(self) -> dict:
        """Snapshot local chat activity before list-chats mutates metadata.

        The database-backed chat dict is already our durable per-chat sync
        state. Copy only the tiny fields needed to decide whether querying
        get-messages can possibly add anything this round. Address aliases are
        indexed to the same marker so a @lid <-> phone mapping discovered by
        the fresh chat list does not make an old chat look brand new.
        """
        baseline = {}
        for key, chat in list(getattr(self, "chats", {}).items()):
            if not isinstance(chat, dict):
                continue
            jid = self._normalize_jid(chat.get("remoteJid") or key or "")
            if not jid:
                continue
            marker = _chat_sync_marker(chat)
            forms = {jid}
            try:
                forms.update(f for f in self._jid_address_forms(jid) if f)
            except Exception:
                pass
            for form in forms:
                baseline[self._normalize_jid(form)] = marker
        return baseline

    def _baseline_marker_for_jid(self, jid: str, baseline: dict) -> dict:
        jid = self._normalize_jid(jid or "")
        if jid in baseline:
            return baseline[jid]
        try:
            for form in self._jid_address_forms(jid):
                normalized = self._normalize_jid(form or "")
                if normalized in baseline:
                    return baseline[normalized]
        except Exception:
            pass
        return {}

    def _note_verified_activity(self, remote_jid: str, chat: dict) -> None:
        """Record that this chat's messages were fetched up to its current `t`.

        Deliberately per-session and in memory only: it exists to keep
        local_history_behind_server() from re-querying, on every single round,
        a chat whose newest server-side event never becomes a stored message
        (see _MAX_EMPTY_DELTA_RETRIES below). Persisting it would also carry
        over the one case it must never suppress — an activity marker that
        reached the database without its message — so every launch is allowed
        to confirm such a chat again from scratch. That costs at most
        _MAX_EMPTY_DELTA_RETRIES get-messages per chat per launch, not one:
        an empty delta parks the chat on _message_retry_jids, which re-selects
        it on the next round until the retry budget runs out and this gets
        written.
        """
        # Bound in __init__ so the six parallel sync workers share one dict;
        # the getattr is only for the test stubs, which carry no __init__.
        synced = getattr(self, "_verified_activity", None)
        if not isinstance(synced, dict):
            synced = self._verified_activity = {}
        try:
            activity = int((chat or {}).get("t", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return
        jid = self._normalize_jid(remote_jid or "")
        if jid and activity > int(synced.get(jid, 0) or 0):
            synced[jid] = activity

    #: How long a chat may go without an actual get-messages before it is
    #: re-checked whatever the chat-list markers say, and how many such
    #: re-checks one planning round may add.
    #:
    #: Every signal classify_chat_sync() reads is chat-list metadata, so a
    #: chat whose metadata goes stale is skipped on every round forever while
    #: get-messages for it would have returned newer messages the whole time.
    #: Issue #181 is exactly that: two chats stuck at 200 messages ending at
    #: 08:35 and 07:34, which F5 advanced to 17:12 and 17:11 — 34 and 7
    #: messages *newer* than anything stored. Nothing but a forced full
    #: rebuild of all 295 chats could reach them.
    #:
    #: Five per round against a 60 s poll is 300 chats an hour, so an account
    #: the size of that report is fully re-verified in about the hour this
    #: bounds staleness to — at a fixed cost per round however large the
    #: account grows, which is the property a full sweep does not have.
    _STALE_RECHECK_AFTER = 60 * 60
    _STALE_RECHECK_PER_ROUND = 5

    def _note_chat_verified_now(self, remote_jid: str) -> None:
        """Record that get-messages actually ran for this chat, just now.

        Separate from _note_verified_activity(), which records *which activity
        value* was covered and is deliberately per-session. This one answers
        "when did we last really look?", which is what the staleness net needs,
        and it is persisted: a chat last fetched before a restart is exactly as
        stale afterwards, and forgetting that on every launch would re-check
        the whole account each time WinZapp opens.
        """
        seen = getattr(self, "_chat_verified_at", None)
        if not isinstance(seen, dict):
            seen = self._chat_verified_at = {}
        jid = self._normalize_jid(remote_jid or "")
        if not jid:
            return
        seen[jid] = int(time.time())
        self._chat_verified_at_dirty = True

    def _persist_chat_verified_at(self) -> None:
        """Best effort, like every other sync-state persist: losing it costs
        one extra round of re-checks, never a message."""
        if not getattr(self, "_chat_verified_at_dirty", False):
            return
        try:
            if getattr(self, "db", None) is not None:
                self.db.set_metadata_json(
                    "chat_verified_at_v1",
                    dict(getattr(self, "_chat_verified_at", {})))
                self._chat_verified_at_dirty = False
        except Exception as exc:
            logging.warning("[sync] failed to persist chat_verified_at: %s", exc)

    def _plan_message_sync(self, baseline: dict, force_full: bool = False,
                           include_repairs: bool = True):
        """Return (full_targets, incremental_targets, skipped_count, reasons).

        Full targets are chats with no trustworthy local page or a known gap.
        Incremental targets already have history and only changed since the
        captured baseline. Everything else needs no get-messages request at
        all; list-chats has already refreshed unread/pin/archive/mute metadata.
        """
        full_targets = []
        incremental_targets = []
        skipped_chats = []
        reasons = {}
        gap_jids = set(getattr(self, "_history_gap_jids", set()) or set())
        pending_jids = set(getattr(self, "_chats_awaiting_messages", set()) or set())
        failed_jids = set(getattr(self, "_message_retry_jids", set()) or set())
        # getattr-guarded like every other lazily-present sync attribute here:
        # the test stubs that bind this method carry only what the path under
        # test touches, and answer anything else with a lambda — hence the
        # type check rather than a bare `or {}`. See _note_verified_activity().
        verified = getattr(self, "_verified_activity", None)
        if not isinstance(verified, dict):
            verified = {}

        for key, chat in list(getattr(self, "chats", {}).items()):
            if not isinstance(chat, dict):
                continue
            jid = self._normalize_jid(chat.get("remoteJid") or key or "")
            user_part = jid.split("@", 1)[0] if jid else ""
            if not user_part or user_part == "0" or len(user_part) < 5:
                continue

            marker = self._baseline_marker_for_jid(jid, baseline)
            forms = {jid}
            try:
                forms.update(self._normalize_jid(f) for f in self._jid_address_forms(jid) if f)
            except Exception:
                pass
            retry_failed = bool(forms & failed_jids)
            if retry_failed:
                # The list-chat marker may already contain the activity that
                # triggered the failed request, so marker comparison alone can
                # no longer see it. Keep retrying until get-messages actually
                # succeeds; a warm cache can use the bounded adaptive window.
                if int(marker.get("record_count", 0) or 0) > 0:
                    incremental_targets.append(chat)
                    reasons[jid] = "retry-failed"
                else:
                    full_targets.append(chat)
                    reasons[jid] = "retry-empty-cache"
                continue

            repair_needed = bool(
                include_repairs and forms & (gap_jids | pending_jids)
            )

            mode, reason = _classify_chat_sync(
                chat, marker, force_full=force_full, repair_needed=repair_needed,
                server_claims_content=self._server_claims_content(chat),
                verified_activity=max(
                    (int(verified.get(form, 0) or 0) for form in forms),
                    default=0,
                ),
            )
            if mode == "full":
                full_targets.append(chat)
                reasons[jid] = reason
            elif mode == "incremental":
                incremental_targets.append(chat)
                reasons[jid] = reason
            else:
                skipped_chats.append((jid, chat))

        # The staleness net. Everything above reads chat-list metadata, so a
        # chat whose metadata stops moving is skipped on every round forever —
        # see _STALE_RECHECK_AFTER and issue #181. Never on a forced full
        # sync, where every chat is already a target.
        skipped = len(skipped_chats)
        if not force_full and skipped_chats:
            verified_at = getattr(self, "_chat_verified_at", None)
            due = set(_select_stale_rechecks(
                [jid for jid, _ in skipped_chats],
                verified_at if isinstance(verified_at, dict) else {},
                int(time.time()),
                # Off the class, not the instance: these are constants, and
                # the test stubs that bind this method answer any unknown
                # attribute with a lambda — see the _verified_activity guard
                # above for the same hazard.
                SyncMixin._STALE_RECHECK_PER_ROUND,
                SyncMixin._STALE_RECHECK_AFTER,
            ))
            for jid, chat in skipped_chats:
                if jid in due:
                    incremental_targets.append(chat)
                    reasons[jid] = "stale-recheck"
                    skipped -= 1

        # Diagnostic only — no behavior here, just what "Message plan:
        # ...unchanged=N" (logged by the caller) cannot show on its own: the
        # actual markers behind the "nothing changed" verdict, for a report
        # of "sync said done, but F5 found more" to be read directly off the
        # jid/activity/newest-stored numbers instead of re-deriving them from
        # this function on a future occurrence. Bounded (first 15) so a large
        # account's ordinary skip list — most rounds, most chats — cannot
        # turn this into noise; the chats that stayed skipped after the
        # staleness net above already had every other chance to be excluded.
        if not force_full and skipped_chats:
            still_skipped = [
                (jid, chat) for jid, chat in skipped_chats if reasons.get(jid) != "stale-recheck"
            ]
            if still_skipped:
                sample = []
                for jid, chat in still_skipped[:15]:
                    marker = _chat_sync_marker(chat)
                    sample.append(
                        f"{jid}(activity={marker['activity']},"
                        f"newest_local={marker['newest_local_ts']})"
                    )
                logging.info(
                    "[start_sync] %d chat(s) classified unchanged this round "
                    "(showing up to 15): %s",
                    len(still_skipped), ", ".join(sample),
                )

        return full_targets, incremental_targets, skipped, reasons

    def _persist_full_sync_pending(self, reason: str) -> None:
        state = dict(getattr(self, "_last_sync_state", {}) or {})
        state["force_full_pending"] = True
        state["full_pending_reason"] = reason
        state["full_pending_since"] = int(time.time())
        self._last_sync_state = state
        try:
            if getattr(self, "db", None) is not None:
                self.db.set_metadata_json("sync_state_v1", state)
        except Exception as exc:
            logging.warning("[sync] failed to persist full-sync latch: %s", exc)

    def _persist_successful_sync_state(self, mode: str, full_count: int,
                                       incremental_count: int, skipped_count: int) -> None:
        state = {
            "completed_at": int(time.time()),
            "mode": mode,
            "chat_count": len(getattr(self, "chats", {})),
            "full_targets": int(full_count),
            "incremental_targets": int(incremental_count),
            "skipped_unchanged": int(skipped_count),
            "force_full_pending": False,
        }
        self._last_sync_state = state
        try:
            if getattr(self, "db", None) is not None:
                self.db.set_metadata_json("sync_state_v1", state)
        except Exception as exc:
            logging.warning("[sync] failed to persist sync_state_v1: %s", exc)
        # Guarded on its own: sync_state_v1 is the latch that decides whether
        # the next round is another full sync, and a fault in the staleness
        # net's bookkeeping must not be able to leave it unwritten.
        try:
            self._persist_chat_verified_at()
        except Exception as exc:
            logging.warning("[sync] failed to persist chat_verified_at: %s", exc)

    def sync_remote_chats(self, target_chats=None, incremental: bool = False,
                          expected_run_id=None):
        # Also deliberately NOT gated on an active voice call: this returns the
        # set of chats that FAILED, so an early empty set is read by the caller
        # as "every chat succeeded" — see the comment at the top of
        # sync_chat_messages() for what that costs.
        # expected_run_id is passed only by _run_sync() (issue #198); the
        # periodic poll leaves it None and behaves exactly as before.
        def _superseded():
            if expected_run_id is None:
                return False
            # Normalized the way _run_sync()'s _current_run_id() is, for the
            # same test-stub hazard.
            value = getattr(self, "_sync_run_id", 0)
            return (value if isinstance(value, int) else 0) != expected_run_id

        if _superseded():
            logging.info(
                "[sync_remote_chats] Sync run %s was superseded before this "
                "pass started — skipping it.", expected_run_id)
            return set()
        chats = list(target_chats) if target_chats is not None else list(self.chats.values())
        if not chats:
            return set()

        # Filter out invalid JIDs (like '0' or empty entries) to prevent API errors.
        valid_chats = []
        for chat in chats:
            jid = chat.get("remoteJid", "")
            user_part = jid.split("@")[0] if "@" in jid else jid
            if user_part and user_part != "0" and len(user_part) >= 5:
                valid_chats.append(chat)
            else:
                logging.warning("[sync_remote_chats] Skipping invalid JID from sync: %s", jid)

        if not valid_chats:
            return set()

        try:
            valid_chats = sorted(
                valid_chats, key=lambda chat: chat.get("t", 0) or 0, reverse=True
            )
        except Exception:
            pass

        # RECENT decoding and message queries share one Puppeteer page. Keep
        # the initial sweep gentle until the phone finishes feeding history.
        worker_cap = (
            2 if getattr(self, "_history_still_landing", False)
            else 3 if incremental
            else 6
        )
        max_workers = min(worker_cap, len(valid_chats))
        mode = "incremental" if incremental else "full"
        normalize_jid = getattr(self, "_normalize_jid", lambda jid: jid)
        failed_jids = set()
        successful_jids = set()
        logging.info("[sync_remote_chats] %s pass: %d target chat(s).", mode, len(valid_chats))

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            # Handing sync_chat_messages() the run id is what makes the phase
            # stop within seconds: every chat still queued returns at its first
            # line. Only the few already fetching finish their write.
            if incremental:
                futures = {
                    pool.submit(self.sync_chat_messages, chat, expected_run_id, "incremental"): chat
                    for chat in valid_chats
                }
            else:
                futures = {
                    pool.submit(self.sync_chat_messages, chat, expected_run_id): chat
                    for chat in valid_chats
                }

            for future in as_completed(futures):
                chat = futures[future]
                raw_jid = chat.get("remoteJid", "")
                jid = normalize_jid(raw_jid) or raw_jid
                try:
                    result = future.result()
                    if result is False:
                        failed_jids.add(jid)
                    else:
                        successful_jids.add(jid)
                except Exception as exc:
                    failed_jids.add(jid)
                    logging.warning("[sync_remote_chats] failed for %s: %s", jid, exc)

        if _superseded():
            # Every chat skipped by the stale check above came back False, which
            # the latch below would record as a failed fetch and persist — a
            # durable retry list of the previous account's chats, or for F5 of
            # this one's, holding the next round "not synced". Nothing a
            # superseded pass saw is evidence about the account now, so it
            # reports nothing. The in-flight workers' _sync_failed_chats entries
            # go too, or the next round would read them as its own failures.
            # The persist calls are skipped for the same reason; the run that
            # superseded this one already persisted its own state.
            lock = getattr(self, "_sync_failures_lock", None)
            if lock is not None:
                with lock:
                    target_ids = {
                        normalize_jid(chat.get("remoteJid", "")) or chat.get("remoteJid", "")
                        for chat in valid_chats
                    }
                    self._sync_failed_chats = {
                        jid for jid in (getattr(self, "_sync_failed_chats", set()) or set())
                        if (normalize_jid(jid) or jid) not in target_ids
                    }
            logging.info(
                "[sync_remote_chats] Sync run %s was superseded during the %s "
                "pass — discarding its outcome (%d of %d chat(s) reported "
                "failed, none recorded).",
                expected_run_id, mode, len(failed_jids), len(valid_chats))
            return set()

        # Keep a durable retry latch for message I/O failures. list-chats may
        # already have advanced `t`/lastReceivedKey before this query failed;
        # without an independent latch the next incremental round could see the
        # new marker as its baseline and never retry the missing message.
        lock = getattr(self, "_sync_failures_lock", None)
        if lock is not None:
            with lock:
                legacy_failed = set(getattr(self, "_sync_failed_chats", set()) or set())
                self._sync_failed_chats = set()
                target_ids = {
                    normalize_jid(chat.get("remoteJid", "")) or chat.get("remoteJid", "")
                    for chat in valid_chats
                }
                normalized_legacy_failed = {
                    normalize_jid(jid) or jid for jid in legacy_failed if jid
                }
                failed_jids.update(jid for jid in normalized_legacy_failed if jid in target_ids)
                successful_jids.difference_update(failed_jids)
                # Chats whose delta was empty this round: worth one more look,
                # but the server answered, so they are not failures and must
                # not reach the caller's message_sync_ok. Folded into the
                # durable retry list only — see sync_chat_messages().
                unsatisfied = {
                    normalize_jid(jid) or jid
                    for jid in (getattr(self, "_delta_unsatisfied_chats", set()) or set())
                }
                unsatisfied = {jid for jid in unsatisfied if jid in target_ids}
                successful_jids.difference_update(unsatisfied)
                # Chats the store answered chat_not_found for: also an answer
                # and not an I/O failure, so they must not reach
                # message_sync_ok either — one phantom @lid from an
                # e2e_notification used to hold the whole sync incomplete for
                # the rest of the session. Bounded in sync_chat_messages() by
                # _MAX_ABSENT_CHAT_RETRIES.
                absent = {
                    normalize_jid(jid) or jid
                    for jid in (getattr(self, "_absent_chats", set()) or set())
                }
                absent = {jid for jid in absent if jid in target_ids}
                successful_jids.difference_update(absent)
                retry_jids = getattr(self, "_message_retry_jids", None)
                if retry_jids is None:
                    retry_jids = self._message_retry_jids = set()
                retry_jids.difference_update(successful_jids)
                retry_jids.update(failed_jids)
                retry_jids.update(unsatisfied)
                retry_jids.update(absent)

        persist_retries = getattr(self, "_persist_message_retry_jids", None)
        if callable(persist_retries):
            persist_retries()
        persist_pending = getattr(self, "_persist_backfill_pending_state", None)
        if callable(persist_pending):
            persist_pending()
        persist_gaps = getattr(self, "_persist_history_gap_jids", None)
        if callable(persist_gaps):
            persist_gaps()

        if failed_jids:
            sample = sorted(failed_jids)
            logging.warning(
                "[sync_remote_chats] done: %d chats, %d message fetch(es) failed "
                "and remain queued for retry: %s%s",
                len(valid_chats), len(failed_jids), ", ".join(sample[:5]),
                " ..." if len(sample) > 5 else "",
            )
        else:
            logging.info(
                "[sync_remote_chats] done: %d chats, all fetched.",
                len(valid_chats),
            )
        return failed_jids
