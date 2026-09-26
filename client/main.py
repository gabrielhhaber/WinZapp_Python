import io
import os
import sys
import time

# Add lib/ directory to Windows DLL search path so BASS and its plugins
# (bass.dll, bassopus.dll) can find each other regardless of the process's
# working directory. Voice-message recording no longer needs a standalone
# libopus DLL here — encoding now goes through the bundled ffmpeg binary
# (see _convert_wav_to_ogg), which has libopus compiled in.
if sys.platform == 'win32':
    _lib_path = ""
    if getattr(sys, 'frozen', False):
        _lib_path = os.path.join(os.path.dirname(sys.executable), 'lib')
    else:
        _lib_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib')
    if os.path.isdir(_lib_path):
        if hasattr(os, 'add_dll_directory'):
            try:
                os.add_dll_directory(_lib_path)
            except Exception:
                pass
        # Fallback: add to PATH environment variable
        os.environ['PATH'] = _lib_path + os.pathsep + os.environ.get('PATH', '')

import shutil
import socket as _socket

import subprocess
import tempfile
import threading
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import base64
import socketio
import atexit
import ctypes
import ctypes.wintypes
import uuid
from urllib.parse import quote as _url_quote
from accessible_output2 import outputs
from core.accessible_speech import AccessibleSpeechOutput
from core.sound_system import (
    SoundSystem, Sound, load_sound, SOUND_EVENTS,
    alert_tone_choice_keys, resolve_alert_tone_path,
    discover_sound_packs, resolve_sound_event_path, DEFAULT_PACK_ID,
)
from core.audio_devices import (
    find_input_device_index,
    test_input_device,
    enumerate_input_devices,
    repair_stored_input_device_names,
)
from core.bulk_read_state import run_bulk_read_state
from core.call_matching import call_event_matches_active
from core.conversation_resync import deletions_to_apply, stale_ids_in_fetched_window
from core.voice_stereo import opus_encode_args
from core.quote_recovery import (
    RECOVERED_FROM_QUOTE,
    UNDECRYPTED_PLACEHOLDER_TYPES,
    awaits_real_copy,
    carry_over_recovered_quotes,
    fill_placeholders_from_replies,
    reply_context,
)
from core.message_edit import (
    apply_caption_edit,
    carry_over_edited_marker,
    connection_refused,
    is_edit_event,
    response_not_sent,
)
from core.i18n import I18n
from core.remote_deletions import (
    comparable_local_ids,
    comparable_local_records,
    message_timestamp_seconds,
)
from core.sync_contracts import observe_payload
from core.remote_reconcile import (
    deletions_within_remote_window as _deletions_within_remote_window,
    observe_deletions as _observe_deletions,
    older_than_window as _older_than_window,
    oldest_anchor as _oldest_anchor,
    MAX_MIRRORED_DELETIONS,
    split_deletions as _split_deletions,
    add_rollback_gap as _add_rollback_gap,
    normalize_rollback_gaps as _normalize_rollback_gaps,
    outside_rollback_gaps as _outside_rollback_gaps,
)
from core.incremental_sync import (
    chat_activity_floor as _chat_activity_floor,
    timestamp_seconds as _timestamp_seconds,
    chat_message_records as _chat_message_records,
    chat_sync_marker as _chat_sync_marker,
    classify_chat_sync as _classify_chat_sync,
    select_stale_rechecks as _select_stale_rechecks,
    messages_overlap as _messages_overlap,
    next_incremental_limit as _next_incremental_limit,
)
from core.websocket_client import WebSocketClient
from core.api_client import api_get, api_post, redact_credentials
from core.pii_redaction import redact_phone
from core.send_contract import accepted_message_id, send_failure_is_ambiguous
from core.meta_ai import (
    STATE_ACCEPTED,
    STATE_NOT_ACCEPTED,
    is_meta_ai_jid,
    terms_state,
)
from core.wpp_runtime import (
    read_homologated_wpp_version, wppconnect_library_drift, WPPCONNECT_PACKAGE,
)
from core.utils import reaction_targets_status, encrypt, decrypt, encrypt_json, decrypt_json, generate_and_save_key, retrieve_key, format_number, is_phone_like, looks_like_binary_blob, prune_message_record, prune_chats_messages, effective_unread_count, mute_response_accepted, normalize_for_search, search_normalization_mode, parse_bool_flag as _parse_bool_flag, group_setting_notif_value, DEFAULT_SETTINGS, append_selected_marker, is_message_forwarded, plan_row_updates, display_page_fetch_limit, carry_over_video_durations, video_seconds, MEASURED_SECONDS_KEY, is_voice_message, backfill_missing_defaults, auto_download_allows, migrate_voice_messages_media_types, migrate_voice_message_mode_default, migrate_spell_check_mode, migrate_call_exclusive_mode_split
from core.utils import clear_chat_applied, clear_chat_keep_starred_echo
from ui.dialogs.checkbox_confirm import confirm_with_checkbox
from core.settings_transfer import connection_runtime as _connection_runtime
from core.profile_backup import (
    close_snapshot_max_age as _close_snapshot_max_age,
    live_snapshot_due as _live_snapshot_due,
    live_snapshot_policy as _live_snapshot_policy,
)
from core.locale_format import get_date_format, get_time_format, get_datetime_format
from core.quiet_hours import is_quiet_hours_active
from core.call_logic import (
    active_call_label_key,
    incoming_call_can_answer,
)
from core.call_log import (
    CALL_LOG_MESSAGE_TYPE,
    LEGACY_CALL_LOG_TYPE,
    call_log_candidate_ids,
    call_log_label,
    call_log_refresh_delays,
    call_log_supersedes,
    is_call_log,
    is_call_log_pending,
    refile_call_log,
)
from core import browser_payload
from core.database_bridge import DatabaseBridge
from core.chat_lock_vault import (
    ChatLockVault,
    VaultStateError,
    jid_fingerprint,
    validate_pin,
    validate_reveal_code,
)
from core import token_vault
from app_paths import resource_path, data_path, accounts_root
from core.message_queue import MessageQueue, PendingMessage, MessageCancelled
import wx
import wx.adv
if sys.platform == "win32":
    from core.tray_manager import TrayIcon
from core.notification_manager import NotificationManager
from ui.dialogs.connect import Connect
from ui.navigation import NavigationPanel
from ui.conversations import (
    ConversationsPanel, ArchivedConversationsPanel, probe_media_duration,
)
from ui.chat_lock import (
    ID_FORGOT_PIN,
    ChatLockChangePinDialog,
    ChatLockRecoveryDialog,
    ChatLockRevealDialog,
    ChatLockSetupDialog,
    ChatLockUnlockDialog,
    LockedConversationsPanel,
    RecoveryKeyDialog,
)
from status_panel import StatusPanel
from calls_panel import CallsPanel
from ui.accessible import (
    AccessibleCallEndButton,
    AccessibleCallPromoteVideoButton,
    AccessibleCallMuteButton,
    AccessibleCallSettingsButton,
    AccessibleCallVideoToggleButton,
)
from version import __version__
from window_title import format_window_title
import json
from traceback import format_exc, format_exception
import pyperclip
import logging

# MainWindow is assembled from the mixins in main_window/ (one module per
# responsibility — see main_window/__init__.py for the map). The helper
# functions are re-exported here because tests and older call sites reach
# them as main.<name>; new code should import them from their module.
from main_window.http_pool import (  # noqa: F401
    _http_session,
    _orig_get,
    _orig_post,
    _patched_get,
    _patched_post,
)
from main_window.identity_rules import (  # noqa: F401
    _MIN_COMPARABLE_PHONE_DIGITS,
    linked_phone_digits,
    linked_number_differs,
    record_linked_phone_if_unknown,
    participant_digits,
    group_participant_is_me,
    set_group_participant_admin,
    group_participant_admin_flag,
    group_send_permission_from_metadata,
    unexpired_group_send_verdict,
)
from main_window.log_files import (  # noqa: F401
    _LazyLogFile,
    _consolidate_legacy_log_dir,
)
from main_window.message_rules import (  # noqa: F401
    MediaExpiredError,
    _MAX_RESIDENT_MESSAGES_PER_CHAT,
    _PREVIEW_ONLY_MESSAGE_TYPES,
    _MEDIA_ASSUMED_BYTES_PER_SECOND,
    _MEDIA_FETCH_TIMEOUT_CEILING,
    media_fetch_timeout,
    is_countable_message,
    _discount_non_countable_unread,
    _media_not_in_store_lock,
    _media_not_in_store,
    _MEDIA_MISSING_LOG_EVERY,
    _MAX_EMPTY_DELTA_RETRIES,
    _MAX_ABSENT_CHAT_RETRIES,
    _report_media_fetch_failure,
    media_not_in_store_count,
    reset_media_not_in_store_count,
    describe_history_sync_health,
    _UNREAD_UNDISCOUNTED,
    note_unread_discount_state,
    records_cover_snapshot,
    apply_history_sync_unread_correction,
    _message_ts,
    own_message_marks_chat_read,
    unread_after_history_sync,
    reconcile_open_chat_unread,
    _log_refused_read_receipt,
    _unread_seconds,
    reconcile_snapshot_unread,
    history_gap_detected,
    history_gap_closed,
)
from main_window.runtime_setup import (  # noqa: F401
    LEGACY_API_STATE_MARKER,
    LEGACY_API_STATE_DIRS,
    shorten_windows_path,
    migrate_legacy_api_state,
    NPM_HEALTH_MARKER_NAME,
    npm_health_recorded,
    record_npm_health,
    pick_restore_generation,
    node_runtime_needs_download,
    _looks_like_json_response,
)
from main_window.win32_helpers import (  # noqa: F401
    _is_elevated,
    _Win32Proc,
    _HotkeyManager,
    _vk_mod_to_str,
    _get_short_path_name,
    _spawn_delevated,
    BOOKMARK_ZERO_HOTKEY_ID,
)
from main_window.window_chrome import WindowChromeMixin
from main_window.connection import ConnectionMixin
from main_window.sync import SyncMixin
from main_window.updates import UpdatesMixin
from main_window.window_lifecycle import WindowLifecycleMixin
from main_window.chat_list import ChatListMixin
from main_window.calls import CallsMixin
from main_window.identity import IdentityMixin
from main_window.message_events import MessageEventsMixin
from main_window.wpp_server import WppServerMixin
from main_window.sending import SendingMixin
from main_window.session_lifecycle import SessionLifecycleMixin
from main_window.shortcuts import ShortcutsMixin
from main_window.settings import SettingsMixin
from main_window.session_tokens import SessionTokensMixin
from main_window.chat_lock import ChatLockMixin
from main_window.account_link import AccountLinkMixin
from main_window.chats_store import ChatsStoreMixin
from main_window.groups import GroupsMixin
from main_window.contacts import ContactsMixin
from main_window.backfill import BackfillMixin
from main_window.conversation_sync import ConversationSyncMixin
from main_window.media import MediaMixin
from main_window.chat_events import ChatEventsMixin
from main_window.history import HistoryMixin
from main_window.read_state import ReadStateMixin
from main_window.chat_actions import ChatActionsMixin
from main_window.message_actions import MessageActionsMixin


requests.get = _patched_get
requests.post = _patched_post

# Tell Windows to use "WinZapp" as the App User Model ID so notifications
# show the correct name instead of the executable filename.
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WinZapp")
except Exception:
    pass


class MainWindow(
    WindowChromeMixin,
    ConnectionMixin,
    SyncMixin,
    UpdatesMixin,
    WindowLifecycleMixin,
    ChatListMixin,
    CallsMixin,
    IdentityMixin,
    MessageEventsMixin,
    WppServerMixin,
    SendingMixin,
    SessionLifecycleMixin,
    ShortcutsMixin,
    SettingsMixin,
    SessionTokensMixin,
    ChatLockMixin,
    AccountLinkMixin,
    ChatsStoreMixin,
    GroupsMixin,
    ContactsMixin,
    BackfillMixin,
    ConversationSyncMixin,
    MediaMixin,
    ChatEventsMixin,
    HistoryMixin,
    ReadStateMixin,
    ChatActionsMixin,
    MessageActionsMixin,
    wx.Frame,
):
    def __init__(self, account_id=None, account_name=None, startup_source="user",
                 resume_pending=False, registry=None, global_dir=None):
        import time as _time
        self._t_app_start = _time.perf_counter()
        is_post_update = "--post-update" in sys.argv
        logging.info("[STARTUP] ==================================================")
        logging.info("[STARTUP] WinZapp Application Opened Successfully!")
        if is_post_update:
            logging.info("[POST_UPDATE_SUCCESS] *** SUCCESS: APPLICATION SUCCESSFULLY RESTARTED AFTER AUTO-UPDATE! ***")
        logging.info("[STARTUP_TIMING] T+0.000s — MainWindow __init__ started")

        # Check for update marker file from previous update attempt
        try:
            from updater import _outer_exe_dir
            marker_file = os.path.join(_outer_exe_dir(), "update_failed.marker")
            if os.path.exists(marker_file):
                with open(marker_file, "r", encoding="utf-8", errors="ignore") as _mf:
                    marker_content = _mf.read().strip()
                logging.error("[UPDATER_STATUS] WARNING: Found update_failed.marker from previous update: %s", marker_content)
                # The batch installer leaves this marker precisely so the
                # user can be told; logging it and deleting it told nobody.
                # Reported live as "it updates and nothing changes": two
                # failed xcopy runs, both logged here, both silent.
                self._previous_update_failed = True
                # Clean up old marker file on successful application startup
                try:
                    os.remove(marker_file)
                    logging.info("[UPDATER_STATUS] Cleaned up old update_failed.marker after successful launch.")
                except OSError:
                    pass
            else:
                logging.info("[UPDATER_STATUS] Application started cleanly without update_failed.marker.")
        except Exception as _me:
            logging.warning("[UPDATER_STATUS] Error checking update marker: %s", _me)
        logging.info("[STARTUP] ==================================================")
        super().__init__(None)
        # Multi-account context (plan Zad 2.2). account_id is None only in
        # legacy/single-account fallback; new startup always passes it.
        self.account_id = account_id
        self.account_name = account_name or "WinZapp"
        self.startup_source = startup_source
        self.resume_pending = resume_pending
        self.registry = registry
        self.global_dir = global_dir
        # Locks and saving state (initialized early to prevent AttributeErrors on early saves/migrations)
        self._save_lock = threading.Lock()
        self._save_timer = None
        self._save_timer_lock = threading.Lock()
        # Guards the _shutting_down check-and-set in _perform_shutdown() and
        # _on_end_session(): an unlocked check-then-set there is a real
        # TOCTOU race — a local quit, a WM_ENDSESSION, and an IPC "quit" from
        # another account can each observe _shutting_down still False and
        # all proceed into _stop_wpp_server() concurrently, racing on the
        # same wpp_process/taskkill target.
        self._teardown_started_lock = threading.Lock()
        # Set once whichever path actually owns teardown has genuinely
        # finished it — distinct from _shutting_down, which only means
        # teardown has STARTED somewhere. A caller that lost the lock race
        # must wait on this before self-terminating, or it could os._exit()
        # while the winning call is still mid _stop_wpp_server().
        self._teardown_complete_event = threading.Event()
        # Guards self.sync_thread creation — see _try_start_sync_thread().
        self._sync_start_lock = threading.Lock()
        # Serializes wake-from-suspend recovery so the two independent triggers
        # (EVT_POWER_RESUME thread and the health-check clock-gap detector)
        # never run _recover_from_suspend concurrently — overlapping runs made
        # the second kill-orphan sweep murder the fresh Chrome the first run's
        # start-session had just spawned, hanging the session in INITIALIZING or
        # bouncing it to QRCODE. See _recover_from_suspend.
        self._resume_recovery_lock = threading.Lock()
        self._resume_recovery_active = False
        # True only during the ACTIVE close/kill/start restart sequence (not the
        # whole passive observation window), so the health loop's auto-start
        # yields to it without being suppressed for the full observation (GPT r5 #3).
        self._recovery_restart_active = False
        # True only while _recover_suspect_profile()'s restore thread owns the
        # profile (from before it starts to its finally). Narrower than
        # _recovery_restart_active, which the power-resume restart also sets
        # and which covers a session being STARTED; this one only ever covers
        # a session being closed and a profile being put back.
        # _handle_unattended_qr() (websocket_client.py) reads it: see there.
        self._profile_restore_in_flight = False
        self._profile_restore_started_at = 0.0
        self._unresolvable_lids = set()
        self._unresolvable_names = set()
        self._resolving_lids = set()
        self._lid_resolution_lock = threading.Lock()
        self._media_sync_running = False
        self._update_checker = None
        self._wpp_update_checker = None
        self._notification_sound_cache = {}

        self.app_name = "WinZapp"
        # Per-account window title so each account is distinguishable to the
        # screen reader and to IPC/activation (plan Zad 2.2 / 4.2).
        self.SetTitle(self._format_title())

        # Detect no-UI background mode (started via --background flag by Windows
        # autostart).  When True: no dialogs, no sounds, no visible window.
        self.background_mode = "--background" in sys.argv
        logging.info("MainWindow: background_mode=%s", self.background_mode)

        #Initialize screen reader/sapi output
        logging.info("MainWindow: Initializing screen reader output...")
        # Wrapped in AccessibleSpeechOutput so both Settings > Acessibilidade
        # toggles (extended_sr_compat_enabled, sapi_fallback_enabled) apply to
        # every call site at once, not just MainWindow.output() — see that
        # module's docstring. The settings dict isn't loaded yet at this point
        # in __init__ (load_settings() runs right below), so the getter reads
        # self.settings live rather than capturing today's (empty) value.
        self.speak_output = AccessibleSpeechOutput(
            outputs.auto.Auto(), lambda: self.settings, self._voice_recording_silence_active
        )

        # Settings must exist before the sound system loads, since
        # load_sounds()/get_active_sound_pack() read self.settings to resolve
        # the active soundpack and per-event overrides.
        self.settings = {}
        logging.info("MainWindow: Loading settings...")
        self.load_settings()

        #Initialize sound system
        logging.info("MainWindow: Initializing sound system...")
        self.sound_system = SoundSystem(self, sound_dir=resource_path("sounds"))
        self.sound_system.start()

        # Switch to the configured output device (Settings > Audio Devices)
        # BEFORE loading any UI sound — Output.set_device() frees and
        # reinitializes the whole BASS session (BASS_Free()/BASS_Init()),
        # which invalidates every stream already created against it. Doing
        # this after load_sounds() left every loaded Sound (startup.ogg
        # included) pointing at a stream BASS had already freed out from
        # under it, so nothing played. warn_on_failure is deferred to
        # _apply_configured_audio_devices() below since i18n isn't ready yet.
        self.sound_system.apply_output_device(
            self.settings.get("audio_devices", {}).get("output_device_name", "")
        )
        self.sound_system.apply_effects_device(
            self.settings.get("audio_devices", {}).get("effects_output_device_name", "")
        )

        self.refresh_sound_packs()
        self.load_sounds()

        # Synchronize registry key with the autostart setting on Windows
        self._sync_autostart_registry()


        # ── Language selection on first launch ─────────────────────────────────
        # Show before everything else so the user can pick their language
        # before any module installation or connection dialogs appear.
        if not self.background_mode:
            logging.info("MainWindow: Ensuring language selected...")
            self._ensure_language_selected()

        #Initialize helper classes
        logging.info("MainWindow: Initializing Connect/I18n helpers...")
        self.token = ""
        self.connect = Connect(self)
        self.i18n = I18n(self)
        self.i18n.get_language()
        # Published as soon as i18n exists, not at the end of __init__: if
        # anything further down this constructor raises (issue #104 — a
        # missing constructor argument deep in a sub-panel's init_UI()), the
        # `frame = MainWindow(...)` assignment in __main__ never completes,
        # so __main__'s own except-block has no `frame` to read a language
        # from — that's the reason its crash dialog was always hardcoded to
        # Portuguese, even though the user may have picked another language.
        # This partial (but already-i18n-ready) self is what lets that
        # except-block translate the message anyway. See _write_crash_log()'s
        # caller for how it's used, and the module-level docstring near
        # _last_partial_frame's definition for why it can't just be `frame`.
        global _last_partial_frame
        _last_partial_frame = self

        # Apply the configured output/input audio devices (Settings > Audio
        # Devices). A device that fails to open here falls back to the
        # Windows default and warns — settings.json itself is left untouched
        # so the same device is retried on the next launch.
        self._apply_configured_audio_devices()

        # ── Auto-updater ──────────────────────────────────────────────────────
        # Schedule the update checker on the event loop early (but after i18n
        # is initialized) so it can run even if modal dialogs block __init__.
        if not self.background_mode:
            wx.CallLater(15000, self._start_update_checker)
            if getattr(self, "_previous_update_failed", False):
                # Same delay as the checker: past the startup sound and the
                # first sync announcements, before the checker offers the
                # very same release again.
                wx.CallLater(15000, self._announce_previous_update_failure)
            # Separate, independent check for the WPPConnect Server itself —
            # it breaks between WinZapp releases too, and until now the only
            # fix was a user manually wiping client/api/ and node_modules.
            # Given a much longer delay: unlike the WinZapp checker (which
            # only shows a dialog), accepting this one stops and restarts the
            # live API session, so it must never fire while pairing/the
            # initial sync is still settling in.
            wx.CallLater(90000, self._start_wpp_update_checker)

        # Terms of service – show once before anything else happens
        if not self.background_mode:
            logging.info("MainWindow: Checking terms acceptance...")
            self._check_terms_acceptance()

        #bind exception global handler for unexpected errors
        sys.excepthook = self.exception_handler

        self.ws = None

        conn = self.settings.get("connection", {})
        self.wpp_server    = conn.get("wpp_server",    "http://127.0.0.1")
        self.wpp_ws_server = conn.get("wpp_ws_server", "ws://127.0.0.1")
        self.wpp_api_key   = conn.get("wpp_api_key",   "wz-local-api-key")
        self.wpp_custom_api = conn.get("wpp_custom_api", False)
        # Per-account Node port (revised multi-account architecture): each
        # account runs its OWN WPPConnect Node on its OWN port so sessions never
        # share a server and can't race/kill each other. The port is resolved
        # once here, persisted, and stable across launches. A user-configured
        # custom API keeps whatever port they set (we don't manage their server).
        self.wpp_port = self._resolve_wpp_port(conn)
        logging.info("MainWindow: WPPConnect config - server=%s, port=%s, custom_api=%s", self.wpp_server, self.wpp_port, self.wpp_custom_api)

        #Set basic variables
        self.chats = {}
        self.chat_names = []
        # Incremented every time a chat-list rebuild is kicked off (set_chats
        # / _do_scheduled_set_chats). Each background computation captures its
        # own generation number and _apply_chat_lists_if_current() discards
        # the result if a newer rebuild has since started — wx.CallAfter only
        # preserves the order calls were *registered* in, not the order the
        # background threads that produced their arguments actually finished,
        # so without this a slower-finishing older rebuild could overwrite
        # the UI with stale chat order/unread badges after a newer one had
        # already applied fresher data.
        self._chat_list_generation = 0
        # Latched True by start_sync() the first time a sync thread actually
        # begins, and never reset for the life of the process — _live_events_ready()
        # uses it to tell "no sync has run yet, drop live events, one is coming"
        # apart from "a sync has already run", which is a state no other flag
        # expresses: _sync_completed goes back to False on an incomplete sync
        # and _initial_sync_running is cleared as soon as the thread exits.
        self._sync_ever_started = False
        # Explicit full-sync latch. Ordinary startup/reconnect rounds use the
        # local database as their baseline and fetch messages only for chats
        # whose server activity changed. F5/repair paths set this True and it
        # remains latched until a complete server-backed round succeeds.
        self._force_full_sync = False
        self._last_sync_state = {}
        # High-water mark of what list-chats has answered with this session,
        # across sync rounds. Session-scoped on purpose — see the evidence
        # comment in _run_sync()'s retry loop: it is the one number that
        # cannot be explained by WhatsApp Web's store still warming up, which
        # is what lets a store that has gone missing be told apart from one
        # that is merely cold. Never reset while the process lives; a chat
        # count that was real once does not stop having been real.
        self._chat_list_high_water = 0
        # Consecutive sync rounds that found the store not answering. Purely a
        # diagnostic since the session-rebuild escalation was removed — see the
        # store_broken branch in start_sync() for the field log that killed it.
        self._broken_store_rounds = 0
        # Message-sync workers discover incomplete chats concurrently. Keep the
        # queue and its growth counters behind one lock so a LID and its phone
        # JID cannot be inserted or retired in conflicting states.
        self._backfill_state_lock = threading.RLock()
        # Chats whose message fetch exhausted its retries during the current
        # sync. Reported at the end of sync_remote_chats() so a partial sync
        # says so out loud instead of logging the same line as a clean one.
        self._sync_failures_lock = threading.Lock()
        self._sync_failed_chats = set()
        # Chats whose latest-message query failed remain durable retry targets.
        # This is separate from short-history backfill: a warm chat can already
        # hold hundreds of local messages and still miss the newest one if the
        # one delta request that should fetch it times out.
        self._message_retry_jids = set()
        # Chats whose incremental delta came back 200-but-empty this round.
        # Deliberately NOT the same thing as a failed fetch: the server
        # answered, so there is no I/O problem to report and the run is still a
        # clean one — see sync_chat_messages() for why an empty delta is worth
        # one more look anyway, and why that look is bounded.
        self._delta_unsatisfied_chats = set()
        self._delta_unsatisfied_attempts = {}
        # Chats the store answered chat_not_found for. Same reasoning as the
        # empty delta above — an answer, not a failure — bounded by
        # _MAX_ABSENT_CHAT_RETRIES so a phantom JID minted by an
        # e2e_notification does not cost one request per round forever.
        self._absent_chats = set()
        self._absent_chat_attempts = {}
        # Per-session record of the activity each chat's get-messages actually
        # completed on — see _note_verified_activity(). Bound here rather than
        # lazily, because up to six sync_chat_messages() workers write it in
        # parallel and two of them racing the lazy `= {}` would drop one
        # chat's entry (one wasted refetch, invisible in a log).
        self._verified_activity = {}
        self._chats_awaiting_messages = set()
        self._partial_history_counts = {}
        self._history_gap_jids = set()
        self.contacts = {}
        # Presence cache: maps JID → {lastKnownPresence, lastSeen}. Must be
        # initialized here (not lazily in _build_lid_to_phone_cache, which only
        # runs after the initial chat sync) because a presence.update WebSocket
        # event can arrive and call on_presence_update() before that sync
        # completes, depending on how fast WPPConnect emits it.
        self._presence_cache = {}
        # Maps chat JID → {participant_jid: "composing"|"recording"}
        self._composing_chats = {}
        # Maps (chat_jid, participant_jid) → wx.CallLater for 10-second auto-clear
        self._presence_timers = {}
        # Persistent pushName map: phone@s.whatsapp.net → real pushName, learned
        # from presence.update events. Loaded from DB on prepare_sync() and saved whenever updated.
        self._presence_pushname_map = {}
        self._contact_resolution_lock = threading.Lock()
        self._contact_resolution_inflight = set()
        self._lid_resolution_queue_lock = threading.Lock()
        self._lid_resolution_queue = set()
        self._lid_resolution_queue_running = False
        # Incoming call IDs currently ringing. The sound is one shared looping
        # stream, so it stops only after the final simultaneous call ends.
        self._active_incoming_calls = {}
        self._incoming_call_details = {}
        self._incoming_call_watchdogs = {}
        self._call_action_lock = threading.Lock()
        self._call_audio_session = None
        self._active_voice_call = None
        # Set while an outgoing offer is in flight; end_active_call() marks it
        # cancelled so an offer that lands afterwards hangs itself up.
        self._outgoing_call_attempt = None
        self._voice_call_last_announced_state = ""
        # Diagnostic-only: distinguish "never reached this method", "reached
        # it but is_video gated it out" and "gate passed, frame rendered" from
        # a live call's log without spamming a line per dropped frame.
        self._call_remote_video_gate_blocked = 0
        self._call_remote_video_rendered = False
        # Modeless call dialogs, keyed by the same call identity as the active
        # lifecycle maps.  Keeping ownership here lets terminal socket events
        # close a popup that is no longer relevant.
        self._incoming_call_dialogs = {}
        # List of deleted, archived, pinned, and muted chats, loaded from DB on prepare_sync()
        self._deleted_chats = set()
        self._archived_chats = set()
        self._pinned_chats = set()
        self._muted_chats = {}
        # Set by init_UI() when all wx widgets are ready.  start_sync() waits
        # on this before making any wx.CallAfter calls so it never touches
        # widgets that don't exist yet (e.g. when ShowModal() is blocking init_UI).
        self._ui_ready_event = threading.Event()

        # Check if we should ask the user to choose between local and custom/remote API (first run)
        self._check_api_type_first_run()

        # First-run dialogs: autostart and global hotkey (normal mode only, once ever).
        # These must run BEFORE the WPPConnect API is started so the user never
        # sees a "starting WPPConnect" dialog stacked on top of setup prompts —
        # the API only starts once all setup steps are confirmed.
        self.wpp_process = None
        if not self.background_mode:
            self._check_first_run()
            self._check_hotkey_first_run()

        # Handle API execution configuration
        if self.wpp_custom_api:
            logging.info("MainWindow: Custom API enabled — preserving all local API files, cache, and remote session state.")
        else:
            # Check API modules and start WPPConnect Server synchronously BEFORE
            # init_UI so the startup dialog shows first before opening the main
            # conversation list.
            #
            # SYNCHRONOUS IN BACKGROUND MODE TOO. This used to hand the same
            # three calls to a daemon thread when started with --background,
            # on the reasoning that a boot-time launch has no dialog to show
            # and should not hold anything up. What it actually did was let
            # __init__ run on to init_UI() and post_ui_init() while Node was
            # still booting, so the whole connect sequence ran against a port
            # nobody was listening on. Measured on a real boot (2026-09-10
            # 09:39:52, background_mode=True):
            #
            #   T+1.1s   tray icon up, post_ui_init reaches STEP 5
            #   T+1.2s   check_wa_connection_http -> WinError 10061 (refused)
            #   T+2.0s   connect_websocket attempt 1/6 -> Connection error
            #   ...      attempts 2 and 3 fail the same way
            #   T+15.0s  Node finally answers; session CLOSED, then
            #            INITIALIZING, disconnectedMobile, QRCODE
            #
            # i.e. a tray icon claiming to be offline, a WebSocket ladder burnt
            # on a dead port, and a session driven from a cold start by the
            # health checker instead of by the launch. The foreground path has
            # always waited for the port before any of that, and the background
            # path has exactly the same reason to: what background mode should
            # skip is the DIALOG, not the wait. ensure_wpp_running() already
            # knows the difference — its own background branch polls the port
            # for up to 300s and never constructs ApiStartupDialog — so calling
            # it here is enough, and nothing appears on screen while it works.
            try:
                import time as _time
                _t_start = getattr(self, "_t_app_start", _time.perf_counter())
                logging.info("[STARTUP_TIMING] T+%.3fs — Checking/installing API modules...", _time.perf_counter() - _t_start)
                self.ensure_api_modules_installed()
                logging.info("[STARTUP_TIMING] T+%.3fs — Checking WPPConnect Server version...", _time.perf_counter() - _t_start)
                self.ensure_wpp_version()
                logging.info("[STARTUP_TIMING] T+%.3fs — Ensuring WPPConnect Server process is running...", _time.perf_counter() - _t_start)
                self.ensure_wpp_running()
                logging.info("[STARTUP_TIMING] T+%.3fs — WPPConnect Server process ready!", _time.perf_counter() - _t_start)
            except Exception as exc:
                logging.error("[STARTUP_TIMING] Error in API initialization: %s", exc)

        # Effective offline state = user-toggled OR auto-detected (no WhatsApp
        # connection).  Kept as a single attribute because everything else in
        # the app (MessageQueue, media sync, title bar) just asks "are we
        # offline?"; the two sources are tracked separately so the automatic
        # one can be cleared the moment connectivity returns without wiping a
        # deliberate user choice.
        self.offline_mode = False
        self._user_offline = False
        self._auto_offline = False
        # True only while _update_wpp_server() is stopping/reinstalling/
        # restarting the local WPPConnect Server (Help > forced reinstall or
        # the background WppUpdateChecker). The health checker below polls
        # status-session every 30s regardless of what else is happening, so
        # without this flag it would catch the server mid-restart, get a
        # connection error, and declare "offline/disconnected" — even though
        # nothing about the actual WhatsApp session changed.
        self._wpp_updating = False
        # True while WhatsApp Web itself is reachable (verified against the
        # WPPConnect /check-connection-session endpoint, which runs the very
        # same isConnected() test the server uses to answer 404/Disconnected
        # on every other route). False means "the local API is up but WhatsApp
        # is not connected" — the state the app used to mistake for online.
        self._wa_connected = False
        # Timestamp of the most recent WPPConnect Socket.IO event that could
        # only have been emitted by a genuinely live WhatsApp session (a new
        # message, an ack, a chats/presence update — see
        # _note_live_wpp_event() and check_whatsapp_reachable()). WinZapp's
        # Socket.IO client talks to the LOCAL WPPConnect server over
        # loopback, which stays up regardless of the machine's own internet
        # route — so these events keep flowing even while an unrelated
        # network-path issue (a switched Wi-Fi/mobile connection, a stale
        # negative DNS cache entry) makes the app's own outbound reachability
        # probe fail. Reported live: messages kept arriving in an open group
        # (sound and all) for several minutes while the app insisted it was
        # offline, because check_whatsapp_reachable() trusted only that
        # separate probe and never considered the traffic it was watching
        # arrive in real time as proof of connectivity.
        self._last_live_wpp_event_ts = 0.0
        # Set once the first real WhatsApp connection of this session is
        # confirmed, so the "connected" sound plays on connection to WhatsApp
        # and not merely on connection to the local API.
        self._wa_connect_announced = False
        # Whether /send-capabilities has already given a verdict this session.
        # Its own latch rather than _wa_connect_announced's: the probe has to
        # be able to ask again when it could not be answered at all.
        self._send_capabilities_checked = False
        # IDs of messages sent by WinZapp itself (via MessageQueue).  Used by
        # WebSocketClient.on_messages_upsert to distinguish "echo of our own
        # send" (skip — already in UI) from "sent on another device" (show).
        # Populated from the MessageQueue worker thread immediately after the
        # API returns the real message ID, so it is always populated before the
        # corresponding WebSocket echo event can be processed.
        self._own_sent_ids: set = set()
        self._own_sent_ids_lock = threading.Lock()
        # Guards _lid_to_phone/_phone_to_lid — the @lid<->phone bridging
        # state. (_extract_lid_mapping() also touches _message_pushname_cache
        # and _chats_without_alt_jid inside the same section because it is
        # already holding the lock there; their other writers, in
        # find_name_through_messages() and _find_alt_jid_from_messages(), do
        # a single atomic set/dict store each and nothing anywhere iterates
        # them, so they do not need it.)
        # _extract_lid_mapping() mutates these directly on the Socket.IO
        # callback thread (see its own docstring for why it bypasses the
        # usual wx.CallAfter dispatch), while the wx main thread does the
        # same through on_new_message()'s own call into it and through
        # _build_lid_to_phone_cache()'s full rebuilds from the sync thread.
        # Every other writer of those two dicts takes it too —
        # register_jid_mapping() (the sync thread's real writer, via
        # _backfill_names -> resolve_lid_jids_via_api), resolve_self_lid()'s
        # own thread, get_contact_profile(), get_remote_chats(),
        # _load_local_lid_cache() and clear_local_data(). A writer left out
        # is not merely a lost update: the two comprehensions in
        # resolve_self_lid() iterate the live dicts, and one concurrent
        # insert there raises "dictionary changed size during iteration".
        # Blocking I/O (self.db goes through DatabaseBridge, which waits on
        # the DB thread; HTTP calls; wx.CallAfter) must stay OUTSIDE the
        # critical section — holding this while waiting on SQLite would
        # stall the Socket.IO thread behind it.
        # Reentrant because _extract_lid_mapping() can, in principle, be
        # re-entered by code it itself triggers while still holding the lock.
        self._lid_mapping_lock = threading.RLock()
        # Reactions made by WinZapp are rendered optimistically. Keep their
        # target/emoji briefly so the WebSocket client suppresses only that
        # echo, not a fromMe reaction made on the phone or another device.
        self._pending_own_reactions: dict = {}
        self._pending_own_reactions_lock = threading.Lock()
        # Guards the unlink/logout strike counters (_logout_strikes,
        # _resume_fail_strikes, _last_strike_ts, _still_linked_vetoes,
        # _logout_handled) and the decision built from them in
        # _act_on_unlink_decision(). check_wa_connection_http() runs from
        # several independent threads (the health-check loop, _run_sync's
        # tight poll, wx.CallAfter callbacks), so a bare check-then-set on
        # _logout_handled is a real race: two callers can both read it False
        # before either sets it True and both fire _on_disconnect() — a real
        # incident this codebase already hit once (two wipes logged inside
        # the same second). Held across the whole count-then-decide section
        # including _act_on_unlink_decision(), with one deliberate exception:
        # that call can block for up to 10s on _still_linked_on_server()'s
        # HTTP probe when a destructive decision is actually being confirmed
        # — rare, and correctness there matters more than another caller
        # waiting briefly. wx.CallAfter itself is non-blocking either way.
        self._unlink_decision_lock = threading.Lock()
        # Consecutive failed network probes (see check_whatsapp_reachable),
        # and when the current run of them started — the widened during-sync
        # budget is capped in wall-clock time, not in readings (see
        # connection_state.probe_strike_budget).
        self._offline_probe_strikes = 0
        self._offline_probe_first_strike_ts = 0.0
        # Consecutive not-yet-connected results from _set_wa_connected() this
        # session, and when the session started — together these give the
        # first connection attempt a grace period before the UI is allowed to
        # say "offline". Without it, the very first status-session check
        # (fired seconds into startup, often before the local WPPConnect/
        # Chrome process has even finished booting) looked identical to a real
        # outage and immediately flipped the title/tray to "desconectado" —
        # scaring the user over something that resolves itself in a few
        # seconds.
        self._wa_offline_strikes = 0
        self._wa_startup_time = time.time()
        self._reset_startup_probe()
        # (Locks initialized early at the top of __init__)
        # Status text shown in the title bar and tray tooltip (e.g. "sincronizando").
        # Starts as "connecting" rather than blank/offline — the connection
        # state genuinely isn't known yet at this point in startup.
        self._tray_status = self.i18n.t("tray_connecting")

        # True from the moment a deliberate app shutdown starts (real_exit())
        # until the process actually exits. _stop_wpp_server() closes the
        # WPPConnect session itself (POST /close-session) before killing the
        # Node/Chrome processes — while our own WebSocket is still connected,
        # so it receives that as an ordinary "connection.update state=close"
        # event, indistinguishable at that layer from WhatsApp genuinely
        # dropping the connection. Without this flag, _set_wa_connected()
        # read that as a real disconnect and announced "modo offline
        # ativado" (sound + speech) in the second or two before the process
        # actually exits — reported live as the app seeming to announce an
        # error on every quit. Checked at the top of _set_wa_connected(),
        # the single entry point for every connection-state transition, so
        # every path into it (the live event above, and the periodic
        # health-checker) is covered by one guard.
        self._shutting_down = False

        # Track whether the user went through the pairing flow this session
        self._just_paired = False

        # True from the moment a pairing attempt starts (Connect.on_continue)
        # until WPPConnect actually delivers real chat data (messages.set) —
        # much narrower than _just_paired, and also covers re-pairing after a
        # mid-session logout, which _just_paired never does. Used by
        # WebSocketClient.on_connection_update to tell "WhatsApp opened the
        # connection then closed it again before pairing genuinely finished"
        # (reported live: WinZapp played the connected sound and then just
        # sat there forever with no window, no error, no way back to
        # pairing, while the phone eventually showed "could not connect the
        # device") apart from an ordinary transient drop on an
        # already-established, already-synced account — which must NOT be
        # treated as a failed pairing and log the user out over a network
        # blip.
        self._pairing_in_progress = False

        #Check for what window should be shown (skipped in background mode)
        if not self.background_mode:
            logging.info("MainWindow: Checking WhatsApp connection status...")
            if not self.connect.check_connection_status():
                # This account is unpaired (session lost, or pairing never
                # finished). If OTHER paired accounts exist, do NOT trap the
                # user in this dead account's pairing dialog with no way to
                # reach a working account or the menu (reported live: after an
                # overnight session loss, launch showed only the connect dialog
                # of the logged-out account — no way to switch to the healthy
                # one). Offer connect-this / switch-to-other / quit first.
                if self._offer_switch_when_unpaired():
                    return  # switching away; this process is shutting down
                logging.info("MainWindow: WhatsApp connection not paired. Showing connection dialog...")
                self.connect.show_connection_dial()
                if not self.connect.check_connection_status():
                    logging.info("Connection dialog closed without pairing. Exiting application.")
                    sys.exit()
                # Do NOT disconnect self.ws here — see the "Initialize
                # websocket" block below for why this used to cause the
                # pairing session to crash.
                self._just_paired = True
                # Multi-account: pairing succeeded → promote this account from
                # pending to paired in the registry, so it appears in the
                # switcher/autostart (plan Zad 3.2/GPT r7 #1). last_foreground is
                # set later, only after the window is ready and for source=user.
                if getattr(self, "account_id", None) and getattr(self, "registry", None):
                    try:
                        self.registry.set_state(self.account_id, "paired")
                        self.resume_pending = False
                    except Exception:
                        logging.exception("[accounts] pending→paired transition failed")

        self._startup_token_tail_done = False

        logging.info("MainWindow: Retrieving token...")
        self.retrieve_token()
        if not self.token:
            logging.error("No token retrieved. Exiting application.")
            sys.exit()
        #Initialize websocket
        logging.info("MainWindow: Initializing WebSocketClient...")
        # A pairing that just succeeded (_just_paired) already leaves self.ws
        # connected and authenticated — Connect._bg_pairing_flow() created it
        # and used it to receive the phoneCode/session-logged events that
        # just completed pairing. Disconnecting it here (unconditionally,
        # until this fix) and reconnecting from scratch a moment later raced
        # WPPConnect's own session lifecycle: reported live, disconnecting
        # the socket mere milliseconds after WPPConnect logged the session as
        # "Started" reliably closed the WhatsApp Web page/browser
        # server-side (wppconnect.log showed the socket's "saiu" entry
        # immediately followed by "Page Closed" / "browserClose") — leaving
        # WinZapp connected to a server with a dead WhatsApp session inside
        # it, forever, with no window ever shown, no sync, and no further
        # event arriving to explain why. Reuse the live connection instead.
        reuse_existing_ws = (
            self._just_paired
            and getattr(self, "ws", None) is not None
            and getattr(self.ws.sio, "connected", False)
        )
        if reuse_existing_ws:
            logging.info("MainWindow: Reusing the live WebSocketClient established during pairing.")
        else:
            if hasattr(self, 'ws') and self.ws:
                try:
                    self.ws.sio.disconnect()
                except Exception:
                    pass
                self.ws = None
            self.ws = WebSocketClient(self, self.connect, self.token)

        logging.info("MainWindow: Preparing sync...")
        self.prepare_sync()
        if self._just_paired:
            # A pairing that just happened through the dialog above may have
            # linked a phone this account's history does not belong to (see
            # the method's own docstring). Here, and not at the dialog's own
            # end, because that runs before prepare_sync() opens the database
            # — a wipe there would clear media/ and voice_messages/ and leave
            # messages.db to be loaded back in a few lines later. Still before
            # the first sync, which is the merge this prevents.
            self._wipe_local_data_if_another_number_linked()
        # Initialise outgoing-message queue (must exist before init_UI so the
        # ConversationsPanel can call self.main_window.message_queue.enqueue).
        self.message_queue = MessageQueue(self)
        # Bounded pool for per-message background work spawned from
        # on_new_message (DB inserts, LID resolution, media downloads).
        self._msg_bg_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="msg-bg"
        )
        # jid -> Future of the most recent message insert submitted for an
        # @lid chat, so _merge_lid_into_phone() can wait for it before moving
        # that chat's rows to the phone JID (see its own comment). Only @lid
        # chats are tracked: they are the only ones a merge can rename, and
        # tracking every chat would keep a dict of completed futures around
        # for nothing.
        self._pending_lid_inserts: dict = {}
        # Cache of resolved background-notification Sound objects
        self._notification_sound_cache: dict = {}

        logging.info("MainWindow: Initializing User Interface immediately...")

        # Define _post_ui_init BEFORE calling init_UI(), because init_UI() ends
        # with app.MainLoop() which blocks forever — any code placed after
        # init_UI() is unreachable dead code and will never execute.
        # The thread is started from inside init_UI(), right before MainLoop().
        def _post_ui_init():
            logging.info("[post_ui_init] *** THREAD STARTED ***")
            try:
                logging.info("[post_ui_init] STEP 1 — checking background_mode=%s", self.background_mode)
                if not self.background_mode:
                    logging.info("[post_ui_init] STEP 1a — calling check_connection_status()...")
                    _connected = self.connect.check_connection_status()
                    logging.info("[post_ui_init] STEP 1a — check_connection_status() returned: %s", _connected)
                    if not _connected:
                        logging.info("[post_ui_init] STEP 1b — not paired, showing connection dialog...")
                        self.connect.show_connection_dial()
                        logging.info("[post_ui_init] STEP 1b — dialog closed. Re-checking connection...")
                        if not self.connect.check_connection_status():
                            logging.info("[post_ui_init] STEP 1b — still not paired after dialog. Exiting.")
                            sys.exit()
                        logging.info("[post_ui_init] STEP 1b — pairing completed via dialog.")
                        self._just_paired = True

                logging.info("[post_ui_init] STEP 2 — retrieving token...")
                self.retrieve_token()
                logging.info("[post_ui_init] STEP 2 — token retrieved: %s", bool(self.token))
                if not self.token:
                    logging.error("[post_ui_init] STEP 2 — NO TOKEN. Exiting application.")
                    sys.exit()

                logging.info("[post_ui_init] STEP 3 — initializing WebSocketClient (just_paired=%s)...", self._just_paired)
                reuse_existing_ws = (
                    self._just_paired
                    and getattr(self, "ws", None) is not None
                    and getattr(self.ws.sio, "connected", False)
                )
                logging.info("[post_ui_init] STEP 3 — reuse_existing_ws=%s", reuse_existing_ws)
                if reuse_existing_ws:
                    logging.info("[post_ui_init] STEP 3 — Reusing the live WebSocketClient established during pairing.")
                else:
                    if hasattr(self, 'ws') and self.ws:
                        logging.info("[post_ui_init] STEP 3 — Disconnecting existing ws before creating new one...")
                        try:
                            self.ws.sio.disconnect()
                        except Exception:
                            pass
                        self.ws = None
                    logging.info("[post_ui_init] STEP 3 — Creating new WebSocketClient instance...")
                    self.ws = WebSocketClient(self, self.connect, self.token)
                    logging.info("[post_ui_init] STEP 3 — WebSocketClient created OK.")

                logging.info("[post_ui_init] STEP 4 — prepare_sync already performed during window initialization.")

                logging.info("[post_ui_init] STEP 5 — calling check_wa_connection_http()...")
                self.check_wa_connection_http()
                logging.info("[post_ui_init] STEP 5 — check_wa_connection_http() done.")

                if reuse_existing_ws:
                    logging.info("[post_ui_init] STEP 6 — Skipping WebSocket reconnect — already connected from pairing.")
                    return

                logging.info("[post_ui_init] STEP 6 — connecting WebSocket...")
                ws_connected = False
                saw_invalid_namespace = False
                for attempt in range(1, 7):
                    try:
                        self.connect_websocket()
                        ws_connected = True
                        logging.info("[post_ui_init] STEP 6 — WebSocket connected successfully on attempt %d.", attempt)
                        break
                    except Exception as e:
                        error_str = str(e)
                        # Accumulated, never reassigned: a plain assignment
                        # here made this mean "the LAST attempt was an invalid
                        # namespace", so five namespace failures followed by
                        # one ECONNREFUSED landed in the generic
                        # websocket_failed_reconnect dialog instead of the
                        # pairing one the namespace failures called for.
                        invalid_namespace = (
                            "Invalid namespace" in error_str
                            or "namespaces failed to connect" in error_str
                        )
                        saw_invalid_namespace = saw_invalid_namespace or invalid_namespace
                        # "Invalid namespace" means the server has no
                        # Socket.IO namespace for our session at all — which
                        # can mean the session was really deleted
                        # server-side, but early in startup can just as
                        # easily mean Node has not finished registering it
                        # yet. Retrying like any other failure instead of
                        # giving up on the first sighting gives that race a
                        # chance to resolve before anything is decided.
                        last_attempt = attempt == 6
                        logging.warning(
                            "[post_ui_init] STEP 6 — WebSocket connect attempt %d/6 "
                            "failed (%s)%s.%s",
                            attempt, e,
                            " — invalid namespace" if invalid_namespace else "",
                            " Giving up." if last_attempt else " Retrying in 3s...",
                        )
                        if not last_attempt:
                            # No point sleeping after the sixth failure: the
                            # loop is over, and the 3s only delayed the dialog
                            # the user is waiting on by another cycle.
                            time.sleep(3.0)

                if not ws_connected and saw_invalid_namespace:
                    # We have not connected even once this run (reuse_existing_ws
                    # above would have returned early otherwise), so per the same
                    # rule the rest of the app follows, an invalid namespace seen
                    # at any point during the retries is not proof of an unlink —
                    # show the pairing dialog but keep the local data.
                    logging.info(
                        "[post_ui_init] STEP 6 — WebSocket never connected and at "
                        "least one attempt failed on an invalid namespace — "
                        "showing pairing dialog WITHOUT wiping."
                    )
                    def _gui_logout():
                        wx.MessageBox(
                            self.i18n.t("device_logged_out"),
                            self.i18n.t("error").format(app_name=self.app_name),
                            wx.OK | wx.ICON_ERROR,
                        )
                        self._on_disconnect(wipe=False)
                    wx.CallAfter(_gui_logout)
                elif not ws_connected:
                    self.error_sound.play()
                    def _gui_failed():
                        wx.MessageBox(
                            self.i18n.t("websocket_failed_reconnect"),
                            self.i18n.t("connection_error"),
                            wx.OK | wx.ICON_WARNING,
                        )
                        self.connect.show_connection_dial()
                    wx.CallAfter(_gui_failed)
                    wx.CallAfter(self._set_status, self.i18n.t("tray_wa_disconnected"))
                    self._just_paired = True

                logging.info("[post_ui_init] *** ALL STEPS COMPLETED SUCCESSFULLY ***")
            except SystemExit:
                logging.info("[post_ui_init] SystemExit caught — propagating.")
                raise
            except Exception:
                logging.exception("[post_ui_init] *** UNEXPECTED EXCEPTION — see traceback above ***")
                # Ensure the status is never left stuck at "conectando" on any crash.
                wx.CallAfter(self._set_status, self.i18n.t("tray_wa_disconnected"))

        # Store the function so init_UI() can start it right before MainLoop().
        self._post_ui_init_fn = _post_ui_init

        self.init_UI()


    def init_UI(self):
        logging.info("[init_UI] start")
        self.SetMinSize((400, 300))

        # This in-window call surface is the non-intrusive counterpart to the
        # always-on-top popup. It stays hidden until a call arrives with the
        # popup option disabled, then becomes the first control the user meets
        # after returning to WinZapp with Alt+Tab.
        self.incoming_call_bar = wx.Panel(self)
        incoming_call_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.incoming_call_label = wx.StaticText(self.incoming_call_bar, label="")
        self.incoming_call_answer_button = wx.Button(
            self.incoming_call_bar,
            label=self.i18n.t("incoming_call_answer_button"),
        )
        self.incoming_call_answer_button.Bind(
            wx.EVT_BUTTON, self._on_answer_incoming_call_bar
        )
        self.incoming_call_reject_button = wx.Button(
            self.incoming_call_bar,
            label=self.i18n.t("incoming_call_reject_button"),
        )
        self.incoming_call_reject_button.Bind(
            wx.EVT_BUTTON, self._on_reject_incoming_call_bar
        )
        self.incoming_call_stop_button = wx.Button(
            self.incoming_call_bar,
            label=self.i18n.t("incoming_call_silence_button"),
        )
        self.incoming_call_stop_button.Bind(
            wx.EVT_BUTTON, self._on_stop_incoming_call_bar
        )
        incoming_call_sizer.Add(
            self.incoming_call_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 8
        )
        incoming_call_sizer.Add(
            self.incoming_call_answer_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 8
        )
        incoming_call_sizer.Add(
            self.incoming_call_reject_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 8
        )
        incoming_call_sizer.Add(
            self.incoming_call_stop_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 8
        )
        self.incoming_call_bar.SetSizer(incoming_call_sizer)
        self.incoming_call_bar.Hide()

        # Calls live in their own modeless window so changing focus back to
        # the conversation never leaves call controls stranded in the main UI.
        # There is deliberately only this one set of call controls — see
        # _sync_voice_call_bar() for why the in-frame copy was removed.
        #
        # Parent is deliberately None, not self. A wx.Frame given another
        # top-level frame as its parent becomes a Win32 OWNED window, and an
        # owned window is unconditionally kept above its owner in the
        # Z-order by Windows itself — clicking, Alt-Tabbing to, or otherwise
        # activating MainWindow would still leave this window pinned on top
        # of it, no matter what focus code does on either side. Reported
        # live as the call window always ending up over WinZapp's own
        # window even while trying to switch back to it. An unparented
        # frame is a fully independent top-level window, so the two can be
        # freely interleaved like any other two windows. This process only
        # ever ends via os._exit() (see _perform_shutdown()/real_exit()),
        # never a clean wx destroy cascade, so there is no lifetime cost to
        # this window outliving MainWindow in wx's own bookkeeping.
        self.voice_call_window = wx.Frame(
            None, title=self.i18n.t("voice_call_window_title"), size=(700, 520),
            style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX),
        )
        call_panel = wx.Panel(self.voice_call_window)
        call_sizer = wx.BoxSizer(wx.VERTICAL)
        self.call_video_image = wx.StaticBitmap(call_panel, bitmap=wx.Bitmap(640, 360))
        call_sizer.Add(self.call_video_image, 1, wx.EXPAND | wx.ALL, 8)
        self.call_video_image.Hide()
        controls = wx.BoxSizer(wx.HORIZONTAL)
        self.voice_call_window_label = wx.StaticText(call_panel, label="")
        self.voice_call_window_end_button = wx.Button(call_panel, label=self.i18n.t("voice_call_end_button"))
        self.voice_call_window_settings_button = wx.Button(call_panel, label=self.i18n.t("voice_call_settings_button"))
        self.voice_call_window_mute_button = wx.Button(call_panel, label=self.i18n.t("voice_call_mute_button"))
        # Only while the call is voice: it turns this call into a video call
        # without hanging up (WhatsApp's own "switch to video").
        self.voice_call_window_promote_button = wx.Button(
            call_panel, label=self.i18n.t("voice_call_promote_video_button"))
        self.voice_call_window_video_button = wx.Button(call_panel, label=self.i18n.t("voice_call_video_off_button"))
        self.voice_call_window_video_button.Hide()
        self.voice_call_window_end_button.SetAccessible(AccessibleCallEndButton())
        self.voice_call_window_settings_button.SetAccessible(AccessibleCallSettingsButton())
        self.voice_call_window_mute_button.SetAccessible(AccessibleCallMuteButton())
        self.voice_call_window_promote_button.SetAccessible(AccessibleCallPromoteVideoButton())
        self.voice_call_window_video_button.SetAccessible(AccessibleCallVideoToggleButton())
        self.voice_call_window_end_button.Bind(wx.EVT_BUTTON, self.end_active_call)
        self.voice_call_window_settings_button.Bind(wx.EVT_BUTTON, self._open_active_call_settings)
        self.voice_call_window_mute_button.Bind(wx.EVT_BUTTON, self.toggle_call_microphone)
        self.voice_call_window_promote_button.Bind(wx.EVT_BUTTON, self.promote_call_to_video)
        self.voice_call_window_video_button.Bind(wx.EVT_BUTTON, self.toggle_call_video)
        # The label gets a row of its own. Sharing one fixed-width row with four
        # buttons, "Video call: <name>." pushed the last button -- "turn video
        # off" -- past the window's right edge: still reachable with Tab, but
        # off-screen (confirmed with a sighted-assistance description, which
        # listed three buttons). Longer locales and longer names only made it
        # worse, which is also why the window is fitted to its content in
        # _sync_voice_call_bar() rather than given fixed sizes.
        call_sizer.Add(self.voice_call_window_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)
        controls.Add(self.voice_call_window_end_button, 0, wx.ALL, 8)
        controls.Add(self.voice_call_window_settings_button, 0, wx.ALL, 8)
        controls.Add(self.voice_call_window_mute_button, 0, wx.ALL, 8)
        controls.Add(self.voice_call_window_promote_button, 0, wx.ALL, 8)
        controls.Add(self.voice_call_window_video_button, 0, wx.ALL, 8)
        call_sizer.Add(controls, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)
        call_panel.SetSizer(call_sizer)
        self._call_window_sizer = call_sizer
        self.voice_call_window.Bind(wx.EVT_CLOSE, self._on_voice_call_window_close)

        # Dedicated Ctrl-based shortcuts for the four call controls, reported
        # to screen readers by the AccessibleCall* classes above — this is a
        # standalone top-level window (see the note on parenting above), so
        # its own accelerator table can't collide with MainWindow's or
        # ConversationsPanel's.
        self.ID_CALL_END      = wx.NewIdRef()  # end call            (Ctrl+Shift+Q)
        self.ID_CALL_MUTE     = wx.NewIdRef()  # mute/unmute mic     (Ctrl+M)
        self.ID_CALL_SETTINGS = wx.NewIdRef()  # call settings       (Ctrl+C)
        self.ID_CALL_VIDEO    = wx.NewIdRef()  # toggle video        (Ctrl+V)
        self.ID_CALL_PROMOTE  = wx.NewIdRef()  # voice -> video      (Ctrl+P)
        call_accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("Q"), self.ID_CALL_END),
            (wx.ACCEL_CTRL,                  ord("M"), self.ID_CALL_MUTE),
            (wx.ACCEL_CTRL,                  ord("C"), self.ID_CALL_SETTINGS),
            (wx.ACCEL_CTRL,                  ord("V"), self.ID_CALL_VIDEO),
            (wx.ACCEL_CTRL,                  ord("P"), self.ID_CALL_PROMOTE),
        ])
        self.voice_call_window.SetAcceleratorTable(call_accel_tbl)
        self.voice_call_window.Bind(wx.EVT_MENU, self.end_active_call,          id=self.ID_CALL_END)
        self.voice_call_window.Bind(wx.EVT_MENU, self.toggle_call_microphone,   id=self.ID_CALL_MUTE)
        self.voice_call_window.Bind(wx.EVT_MENU, self._open_active_call_settings, id=self.ID_CALL_SETTINGS)
        self.voice_call_window.Bind(wx.EVT_MENU, self.toggle_call_video,        id=self.ID_CALL_VIDEO)
        self.voice_call_window.Bind(wx.EVT_MENU, self.promote_call_to_video,    id=self.ID_CALL_PROMOTE)
        self.voice_call_window.Hide()

        self.main_panel = wx.Panel(self)

        self.navigation_panel = NavigationPanel(self, self.main_panel)
        self.content_panel = wx.Panel(self.main_panel)
        self.conversations_panel = ConversationsPanel(self, self.content_panel)
        self.archived_conversations_panel = ArchivedConversationsPanel(
            self, self.content_panel
        )
        self.archived_conversations_panel.Hide()
        self.locked_conversations_panel = LockedConversationsPanel(
            self, self.content_panel
        )
        self.locked_conversations_panel.Hide()
        self.status_panel = StatusPanel(self, self.content_panel)
        self.status_panel.Hide()
        self.calls_panel = CallsPanel(self, self.content_panel)
        self.calls_panel.Hide()

        # Content panel: all panels fill it; only one is shown at a time
        content_sizer = wx.BoxSizer(wx.VERTICAL)
        content_sizer.Add(self.conversations_panel, 1, wx.EXPAND)
        content_sizer.Add(self.archived_conversations_panel, 1, wx.EXPAND)
        content_sizer.Add(self.locked_conversations_panel, 1, wx.EXPAND)
        content_sizer.Add(self.status_panel, 1, wx.EXPAND)
        content_sizer.Add(self.calls_panel, 1, wx.EXPAND)
        self.content_panel.SetSizer(content_sizer)

        # Main panel: nav sidebar on left, content on right
        main_sizer = wx.BoxSizer(wx.HORIZONTAL)
        main_sizer.Add(self.navigation_panel, 0, wx.EXPAND | wx.ALL, 5)
        main_sizer.Add(self.content_panel, 1, wx.EXPAND | wx.ALL, 5)
        self.main_panel.SetSizer(main_sizer)

        # Frame sizer
        frame_sizer = wx.BoxSizer(wx.VERTICAL)
        frame_sizer.Add(self.incoming_call_bar, 0, wx.EXPAND)
        frame_sizer.Add(self.main_panel, 1, wx.EXPAND)
        self.SetSizer(frame_sizer)

        self.create_accelerator_table()
        logging.info("[init_UI] panels built — building menu bar")

        # ── Menu bar ──────────────────────────────────────────────────────────
        self._update_checker = None
        self._wpp_update_checker = None
        self._build_menubar()

        # ── Online presence (sendPresence) ────────────────────────────────────
        # Sends "available" while the window is focused; "unavailable" otherwise.
        self._presence_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER,    self._on_presence_timer,   self._presence_timer)
        self.Bind(wx.EVT_ACTIVATE, self._on_window_activate)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_chat_lock_char_hook)
        self._presence_debounce_timer = None

        # ── System tray icon ──────────────────────────────────────────────────
        self.tray_icon = None
        # True while the window is physically hidden to tray (set in _on_close,
        # cleared in restore_window).  Used to suppress tray-tooltip redraws
        # while the window is visible — prevents NVDA focus disruption.
        self._window_hidden = self.background_mode
        logging.info("[init_UI] menu bar built — initializing tray icon")
        self._init_tray()
        logging.info("[init_UI] tray icon initialized")

        # ── Notification manager ──────────────────────────────────────────────
        from core.notification_manager import NotificationManager
        self.notification_manager = NotificationManager(self)

        # ── Global hotkey ─────────────────────────────────────────────────────
        self._hotkey_manager = None
        self._apply_global_hotkey()
        logging.info("[init_UI] global hotkey applied — showing window")

        # Intercept window-close: hide to tray instead of quitting (when tray active)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_ICONIZE, self._on_iconize)

        # Windows shutdown/restart/logoff. Without these, Windows simply
        # terminates the process, and node.exe + its Chrome child die with it —
        # Chrome's profile (which holds the WhatsApp Web linked-device
        # credentials in an IndexedDB/LevelDB store) is torn mid-write and can
        # come back corrupted, which looks exactly like being unlinked even
        # though the phone still shows the session. Ask Windows to hold the
        # shutdown while we close WPPConnect properly.
        #
        # **On the wx.App, never on this frame.** wxMSW routes both messages to
        # wxTheApp and to nothing else — `wxWindowMSW::HandleQueryEndSession()`
        # and `HandleEndSession()` build the wxCloseEvent and dispatch it with
        # `wxTheApp->SafelyProcessEvent(event)` — and a wxCloseEvent is not a
        # command event, so it never propagates to a frame. Bound here, these
        # two handlers could not run, and did not: a shutdown_audit.log
        # covering 159 launches carries seventeen runs that ended with no
        # teardown at all, eleven of them overnight, and not one line from
        # either handler. Confirmed live by sending WM_QUERYENDSESSION to the
        # running app's own window: delivered, and no audit line.
        #
        # What ran instead is wxApp's own static table entry, and it is the
        # rest of the bug (src/msw/app.cpp):
        #
        #     void wxApp::OnQueryEndSession(wxCloseEvent& event)
        #     {
        #         if (GetTopWindow())
        #             if (!GetTopWindow()->Close(!event.CanVeto()))
        #                 event.Veto(true);
        #     }
        #
        # `Close()` on this frame fires EVT_CLOSE, which is _on_close() — and
        # _on_close() hides to the tray and **vetoes**, because that is the
        # right answer when a human clicks the X. So WinZapp answered "no, you
        # may not shut down" to Windows on every single shutdown. Measured on
        # the live app: 0, a veto. Windows then puts up the blocking-apps
        # screen and, on the way past it, terminates the process outright —
        # which is precisely the STARTUP-with-no-_stop_wpp_server pattern that
        # comes back as a profile WhatsApp Web refuses.
        #
        # A dynamic Bind is searched before the class's static event table, so
        # binding here replaces that default rather than adding to it. Which is
        # also why neither handler may call event.Skip(): skipping resumes the
        # search, reaches wxApp::OnQueryEndSession, and restores the veto.
        _app = wx.GetApp()
        if _app is not None:
            _app.Bind(wx.EVT_QUERY_END_SESSION, self._on_query_end_session)
            _app.Bind(wx.EVT_END_SESSION, self._on_end_session)
        else:
            logging.error("[init_UI] no wx.App to bind the Windows shutdown "
                          "handlers to — WPPConnect will not be closed cleanly "
                          "on a Windows shutdown.")

        # System sleep/resume: the socket.io client, its underlying TCP
        # connection, and the local Puppeteer/Chrome session all go stale the
        # instant Windows suspends, but nothing tells any of them that until
        # the 30s health-check loop happens to run again on its own — which,
        # observed live, routinely never resolves it on its own and leaves
        # the app "offline" forever after a resume until the user quits it
        # from the tray and reopens it. Windows fires WM_POWERBROADCAST
        # (wx's EVT_POWER_SUSPENDED/EVT_POWER_RESUME) reliably around both
        # edges — react to resume by forcing an immediate reconnect instead
        # of waiting for the next poll cycle.
        self.Bind(wx.EVT_POWER_SUSPENDED, self._on_power_suspended)
        self.Bind(wx.EVT_POWER_RESUME, self._on_power_resume)

        # In background mode the window is intentionally hidden; it can be
        # restored later by a second instance or a future tray-icon action.
        if not self.background_mode:
            # Reported by several users: the window used to open at wx's
            # computed "best size" (small, based on the sizer contents)
            # instead of maximized, which is especially awkward for the
            # QR-pairing flow launched right after this and for screen-reader
            # users navigating a cramped layout. Maximize before Show() so it
            # appears already full-size instead of visibly resizing.
            self.Maximize(True)
            self.Show()
            import time as _time
            _t_show = _time.perf_counter() - getattr(self, "_t_app_start", _time.perf_counter())
            logging.info("[STARTUP_TIMING] T+%.3fs — Window physically SHOWN on screen", _t_show)
            # Play startup sound only after the window is physically shown on screen (if not played already)
            self.play_startup_sound()
        import time as _time
        logging.info("[STARTUP_TIMING] T+%.3fs — [init_UI] populating initial chat list", _time.perf_counter() - getattr(self, "_t_app_start", _time.perf_counter()))
        #Set offline chats for the first time
        self.set_chats()
        logging.info("[STARTUP_TIMING] T+%.3fs — [init_UI] chat list populated, UI fully ready", _time.perf_counter() - getattr(self, "_t_app_start", _time.perf_counter()))
        # All widgets exist and the initial chat list is painted — unblock any
        # sync thread that was waiting for the UI to be ready.
        self._ui_ready_event.set()

        # ── Multi-account: window is ready → start IPC + flush queued requests,
        # and record this as the foreground account for a conscious user start
        # (plan Zad 2.0/4.1; GPT r5 #2: autostart-boot does NOT move it).
        self._window_ready = True
        self._ipc_released = False
        self._start_ipc_listener()
        try:
            if getattr(self, "_ipc_listener", None) is not None:
                self._ipc_listener.flush_queue()
        except Exception:
            pass
        if (getattr(self, "account_id", None) and getattr(self, "registry", None)
                and getattr(self, "startup_source", "user") == "user"
                and not self.background_mode):
            try:
                self.registry.set_last_foreground(self.account_id)
            except Exception:
                logging.exception("[accounts] set_last_foreground failed (non-fatal)")

        # ── Quick tip after first pairing ─────────────────────────────────────
        if not self.background_mode and self._just_paired:
            wx.CallAfter(self._check_quick_tip)

        # Auto-updater already scheduled early in constructor

        # ── CRITICAL: Start the post-UI background thread HERE, right before
        # app.MainLoop() — this is the ONLY place this can run.
        # app.MainLoop() blocks forever; any code placed after it in __init__
        # is dead code that will never execute.  The _post_ui_init_fn closure
        # was stored on self by __init__ so we can start it from here.
        _fn = getattr(self, "_post_ui_init_fn", None)
        if _fn is not None:
            logging.info("[init_UI] STARTING post_ui_init background thread — about to enter MainLoop()")
            threading.Thread(target=_fn, daemon=True, name="post_ui_init").start()
        else:
            logging.error("[init_UI] _post_ui_init_fn is MISSING — connection/sync will NOT start! This is a bug.")

        self.start_ui_watchdog()
        logging.info("[init_UI] Entering app.MainLoop() — main thread will block here until app exits")
        try:
            app.MainLoop()
        except KeyboardInterrupt:
            # Ctrl+C on the console: python-socketio installs its own signal
            # handler that raises KeyboardInterrupt here instead of letting
            # wx close the window normally. Do a graceful teardown instead of
            # dumping an ugly traceback.
            logging.info("[init_UI] KeyboardInterrupt (Ctrl+C) received — shutting down gracefully")
            try:
                self._perform_shutdown()
                self._terminate_process()
            except Exception:
                logging.exception("[init_UI] Error during Ctrl+C shutdown")
                os._exit(0)

    def generate_secret_key(self):
        key_file = data_path("secret.key")
        if not os.path.isfile(key_file):
            generate_and_save_key(key_file)

    def retrieve_secret_key(self):
        self.generate_secret_key()
        return retrieve_key(data_path("secret.key"))

    def exception_handler(self, exc_type, exc_value, exc_traceback):
        """Global exception handler for unexpected errors."""
        # Format the full traceback
        error_text = ''.join(format_exception(exc_type, exc_value, exc_traceback))
        try:
            logging.error("Unhandled global exception:\n%s", error_text)
        except Exception:
            pass

        if not wx.IsMainThread():
            wx.CallAfter(lambda: self.exception_handler(exc_type, exc_value, exc_traceback))
            return

        # Play error sound. Guarded because this handler is the last line of
        # defence: a raise here lands in sys.excepthook itself, which Python
        # reports as "Error in sys.excepthook" and then re-prints the original
        # exception — so a broken audio device turned every unhandled error
        # into two tracebacks and hid the one that mattered. Seen live with a
        # stale BASS handle after a device reinit.
        try:
            self.error_sound.play()
        except Exception:
            logging.warning("[error-dialog] could not play the error sound", exc_info=True)

        # Create error dialog
        dialog = wx.Dialog(None, title=self.i18n.t("error").format(app_name=self.app_name), size=(600, 400), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)

        panel = wx.Panel(dialog)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Error message
        message_text = wx.StaticText(panel, label=self.i18n.t("unexpected_error_message").format(app_name=self.app_name))
        sizer.Add(message_text, 0, wx.ALL, 10)

        #Error details label
        details_label = wx.StaticText(panel, label=self.i18n.t("error_details"))
        sizer.Add(details_label, 0, wx.LEFT | wx.TOP, 10)

        # Error details text control (read-only, multiline)
        error_ctrl = wx.TextCtrl(panel, value=error_text, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        sizer.Add(error_ctrl, 1, wx.ALL | wx.EXPAND, 10)

        # Buttons
        button_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # Copy button
        copy_btn = wx.Button(panel, label=self.i18n.t("copy_error_text"))
        copy_btn.Bind(wx.EVT_BUTTON, lambda evt: self.on_copy_error(error_text))
        button_sizer.Add(copy_btn, 0, wx.ALL, 5)

        # Close button
        close_btn = wx.Button(panel, id=wx.ID_CANCEL, label=self.i18n.t("close"))
        button_sizer.Add(close_btn, 0, wx.ALL, 5)

        sizer.Add(button_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        panel.SetSizer(sizer)

        # Show dialog
        dialog.ShowModal()
        dialog.Destroy()

    def on_copy_error(self, error_text):
        """Copy error text to clipboard."""
        try:
            pyperclip.copy(error_text)
            self.output(self.i18n.t("error_copied"), interrupt=True)
        except Exception:
            pass


# Set by MainWindow.__init__ the moment self.i18n exists (see that call
# site's own comment) — this module-level name, not the `frame` local in
# __main__, is what survives a constructor crash: `frame = MainWindow(...)`
# never completes assigning `frame` when the constructor itself raises, but
# the partially-built instance already ran that assignment before failing
# further down. Only ever read by _startup_critical_error_text() below, and
# only for its title/message strings — never assumed fully initialized.
_last_partial_frame = None


def _startup_critical_error_text(crash_path: str, tb: str) -> tuple[str, str]:
    """(title, message) for the native MessageBoxW shown when __main__'s
    top-level except-block catches an error during startup — translated
    into the user's selected language when possible.

    Falls back to the original hardcoded Portuguese only when no usable
    i18n is available at all (a crash before MainWindow even constructs
    self.i18n, or i18n.t() itself raising) — this dialog is the one thing
    standing between the user and a silent exit, so it must never crash
    trying to be helpful.
    """
    frame = _last_partial_frame
    if frame is not None and getattr(frame, "i18n", None) is not None:
        try:
            title = frame.i18n.t("startup_critical_title")
            message = frame.i18n.t("startup_critical_message").format(
                path=crash_path, details=tb[:800]
            )
            return title, message
        except Exception:
            pass
    return (
        "WinZapp — Erro de inicialização",
        f"O WinZapp encontrou um erro crítico ao iniciar e não pôde continuar.\n\n"
        f"Detalhes foram salvos em:\n{crash_path}\n\n{tb[:800]}",
    )


def _write_crash_log(tb: str) -> str:
    """Write a traceback to crash.log next to the exe and return the path."""
    from app_paths import _outer_exe_dir
    crash_path = os.path.join(_outer_exe_dir(), "crash.log")
    try:
        with open(crash_path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(tb)
    except Exception:
        pass
    return crash_path


class LoggerWriter:
    def __init__(self, original_stream, level):
        self.original_stream = original_stream
        self.level = level

    def write(self, message):
        if self.original_stream:
            self.original_stream.write(message)
        msg = message.rstrip()
        if msg:
            logging.log(self.level, msg)

    def flush(self):
        if self.original_stream:
            self.original_stream.flush()


class _PiiRedactingFormatter(logging.Formatter):
    """Masks WhatsApp phone numbers, LIDs and group ids in every log line.

    Well over a hundred `logging.*()` call sites across this file log a JID
    directly — a JID's leading digits ARE the contact's phone number.
    Editing every one of them is both impossible to keep complete and
    reopens the leak on the next new call site; this masks the fully
    rendered message once instead, so it covers all of them (past and
    future) by construction, regardless of whether the call used %-style
    args or an f-string — by `format()` time both are already merged into
    plain text. See `client/core/pii_redaction.py` for the actual pattern
    and `client/api_patches/src/util/logger.ts` for the Node-side mirror.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_phone(super().format(record))


def setup_logging():
    import logging.handlers
    from app_paths import log_path
    try:
        os.makedirs(log_path(), exist_ok=True)
        log_file = log_path("log.log")

        # Remove the log.log.1/.2/.3 backups a previous RotatingFileHandler
        # left behind. There is deliberately only ONE log file now, holding
        # only the current run: when diagnosing a startup/pairing problem,
        # having to work out where the last launch begins inside a 10 MB file
        # (or which of four files it landed in) is pure friction.
        for _n in range(1, 10):
            try:
                os.remove(f"{log_file}.{_n}")
            except OSError:
                pass

        # mode="w" truncates on open, so each launch starts from a clean file.
        # Safe because __main__ only calls setup_logging() after the
        # single-instance mutex is acquired — otherwise a second launch would
        # wipe the log of the instance that is actually running.
        handler = logging.FileHandler(
            log_file,
            mode="w",
            encoding="utf-8",
        )
        handler.setFormatter(_PiiRedactingFormatter(
            "%(asctime)s [%(levelname)s] (%(filename)s:%(lineno)d) - %(message)s"
        ))

        root = logging.getLogger()
        # Remove any handler added by a prior basicConfig call
        for h in root.handlers[:]:
            root.removeHandler(h)
        root.addHandler(handler)
        # Set logging level to INFO to expose auto-updater, settings validation,
        # and startup logs. Noisy dependencies are silenced at ERROR level below.
        root.setLevel(logging.INFO)

        # Silence very noisy third-party libraries
        for _lib in ("urllib3", "requests", "socketio", "engineio",
                     "charset_normalizer", "websocket", "PIL"):
            logging.getLogger(_lib).setLevel(logging.ERROR)

        logging.warning("WinZapp client starting up...")

        # Only redirect stderr (uncaught exceptions / tracebacks) to the log.
        # Redirecting stdout would write every print() call to the file.
        sys.stderr = LoggerWriter(sys.stderr, logging.ERROR)
    except Exception as e:
        sys.stderr.write(f"Failed to setup logging: {e}\n")


if __name__ == "__main__":
    try:
        import signal

        # Ctrl+C on the console must close WinZapp gracefully no matter which
        # wx handler happens to be running when the interrupt lands. python-
        # socketio installs its own SIGINT handler that re-raises
        # KeyboardInterrupt on the main thread; wx then surfaces that inside
        # whatever handler is dispatching (observed live: _on_window_activate),
        # far away from MainLoop()'s own try/except, producing an ugly
        # traceback and an unclean exit. Installing OUR handler BEFORE the
        # socketio client is created makes engineio save it as the "original"
        # handler and call it after it has disconnected its clients — so a
        # single top-level handler covers every code path. It schedules the
        # graceful teardown on the wx main thread (thread-safe via
        # wx.CallAfter) instead of raising, and the __main__ KeyboardInterrupt
        # guard below is kept as a fallback for any interrupt that bypasses it.
        _main_frame_ref = []

        def _sigint_graceful(sig, frame):
            logging.info("[main] SIGINT (Ctrl+C) received — closing WinZapp gracefully")
            try:
                if _main_frame_ref:
                    import wx as _wx
                    _wx.CallAfter(_main_frame_ref[0]._perform_shutdown)
                    _wx.CallAfter(_main_frame_ref[0]._terminate_process)
            except Exception:
                pass
            os._exit(0)

        signal.signal(signal.SIGINT, _sigint_graceful)
        signal.signal(signal.SIGTERM, _sigint_graceful)

        import app_paths
        from account_bootstrap import resolve_startup, parse_startup_source
        from account_migration import migrate_if_needed
        from accounts import AccountRegistry
        import update_coord

        background = "--background" in sys.argv
        startup_source = parse_startup_source(sys.argv)
        gd = app_paths.global_dir()

        # 1) Updater coordination FIRST (before migration): if an install is in
        #    progress, don't start into a file-swap; otherwise claim a runtime
        #    lease so the updater won't swap files under us (plan Zad 2.2/2.-1).
        os.makedirs(gd, exist_ok=True)
        if update_coord.is_update_in_progress(gd):
            sys.exit(0)
        _runtime_lease = update_coord.try_create_runtime_lease(gd)
        if _runtime_lease is None:
            sys.exit(0)  # an update slipped in between the check and the claim
        atexit.register(update_coord.release_runtime_lease, gd, _runtime_lease)

        # 2) One-time legacy flat-data migration, then resolve which account.
        migrate_if_needed(gd)
        _registry = AccountRegistry(gd)
        _startup = resolve_startup(sys.argv, _registry)

        _mode = _startup["mode"]
        if _mode == "error":
            ctypes.windll.user32.MessageBoxW(
                0, f"WinZapp: {_startup.get('reason', 'nieprawidłowe konto')}",
                "WinZapp", 0x10)
            sys.exit(2)
        elif _mode == "manager":
            # Global manager mode: no account/data_path, no Node (plan sekcja F).
            # TODO(Zad 4.5/4.6): show the account manager. For now, inform+exit.
            ctypes.windll.user32.MessageBoxW(
                0, "WinZapp: brak kont do uruchomienia (menedżer kont w budowie).",
                "WinZapp", 0x40)
            sys.exit(0)
        elif _mode == "first_run":
            _acc = _registry.add("default", state="pending")
            _account_id = _acc["id"]
            _resume_pending = True
        elif _mode == "autostart_boot":
            # Boot launcher: open foreground here, spawn the rest in background.
            from account_launcher import build_launch_command
            _account_id = _startup["foreground"]
            for _bg_id in _startup.get("background", []):
                try:
                    subprocess.Popen(build_launch_command(
                        sys.argv[0], sys.executable, _bg_id,
                        getattr(sys, "frozen", False),
                        startup_source="autostart", background=True))
                except Exception:
                    logging.exception("[autostart-boot] failed to spawn %s", _bg_id)
            startup_source = "autostart"
            _resume_pending = (_registry.get(_account_id) or {}).get("state") == "pending"
        else:  # "account"
            _account_id = _startup["account_id"]
            _resume_pending = _startup.get("resume_pending", False)

        # 3) Bind this process to its account BEFORE mutex/AppUserModelID/window.
        app_paths.set_active_account(_account_id)
        _account = _registry.get(_account_id) or {}
        _account_name = _account.get("name", "WinZapp")

        # Per-account AUMID so Windows groups toasts per account (GPT r4 #8).
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"WinZapp.{_account_id}")
        except Exception:
            pass

        from autostart import acquire_single_instance_mutex
        first_instance = acquire_single_instance_mutex()  # keyed on per-account data_path()
        if not first_instance:
            # Another process already runs THIS account: ask it to come to the
            # foreground via account-scoped IPC (not a title match), then exit.
            if not background:
                try:
                    import ipc
                    ipc.request_activate(gd, _account_id, source=startup_source)
                except Exception:
                    pass
            sys.exit(0)

        setup_logging()
        logging.info("Instance lock acquired for account %s (%s).", _account_id, _account_name)
        logging.info("Creating wx.App...")
        app = wx.App()
        frame = MainWindow(account_id=_account_id, account_name=_account_name,
                           startup_source=startup_source, resume_pending=_resume_pending,
                           registry=_registry, global_dir=gd)
        _main_frame_ref.append(frame)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        # Ctrl+C on the console: python-socketio's signal handler re-raises
        # KeyboardInterrupt on the main thread, which wx may surface inside
        # any dispatched handler (e.g. _on_window_activate) instead of inside
        # MainLoop()'s own try/except — producing an ugly traceback and an
        # unclean exit. Catch it at the top level and shut down gracefully,
        # exactly like closing the window would.
        logging.info("[main] KeyboardInterrupt (Ctrl+C) received — shutting down gracefully")
        try:
            frame.real_exit()
        except Exception:
            try:
                frame._terminate_process()
            except Exception:
                os._exit(0)
    except Exception:
        tb = format_exc()
        try:
            logging.error("Critical initialization error:\n%s", tb)
        except Exception:
            pass
        crash_path = _write_crash_log(tb)
        # Try to show a native Windows error box (works even without wx).
        try:
            title, message = _startup_critical_error_text(crash_path, tb)
            ctypes.windll.user32.MessageBoxW(
                0,
                message,
                title,
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass
        sys.exit(1)
