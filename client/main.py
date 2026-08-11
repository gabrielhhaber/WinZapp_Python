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
import threading
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import base64
import socketio
import atexit
import ctypes
import ctypes.wintypes
from accessible_output2 import outputs
from core.sound_system import (
    SoundSystem, Sound, load_sound, SOUND_EVENTS,
    alert_tone_choice_keys, resolve_alert_tone_path,
    discover_sound_packs, resolve_sound_event_path, DEFAULT_PACK_ID,
)
from core.audio_devices import find_input_device_index, test_input_device
from core.i18n import I18n
from core.websocket_client import WebSocketClient
from core.utils import encrypt, decrypt, encrypt_json, decrypt_json, generate_and_save_key, retrieve_key, format_number, is_phone_like, looks_like_binary_blob, prune_message_record, prune_chats_messages, effective_unread_count, mute_response_accepted, parse_bool_flag as _parse_bool_flag, DEFAULT_SETTINGS
from core.locale_format import get_date_format, get_time_format, get_datetime_format
from core.database_bridge import DatabaseBridge
from core import token_vault
from app_paths import resource_path, data_path
from core.message_queue import MessageQueue, PendingMessage
import wx
import wx.adv
if sys.platform == "win32":
    from core.tray_manager import TrayIcon
from core.notification_manager import NotificationManager
from ui.dialogs.connect import Connect
from ui.navigation import NavigationPanel
from ui.conversations import ConversationsPanel, ArchivedConversationsPanel
from status_panel import StatusPanel
from version import __version__
import json
from traceback import format_exc, format_exception
import pyperclip
import logging

# Enable global HTTP connection pooling (Keep-Alive) to optimize remote API request latency
_http_session = requests.Session()
_orig_get = requests.get
_orig_post = requests.post

def _patched_get(*args, **kwargs):
    return _http_session.get(*args, **kwargs)

def _patched_post(*args, **kwargs):
    return _http_session.post(*args, **kwargs)

requests.get = _patched_get
requests.post = _patched_post

# Tell Windows to use "WinZapp" as the App User Model ID so notifications
# show the correct name instead of the executable filename.
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WinZapp")
except Exception:
    pass


def _is_elevated() -> bool:
    """Return True when the current process holds an elevated (admin) token."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class _Win32Proc:
    """Minimal Popen-compatible wrapper around a Win32 process handle returned by
    CreateProcessWithTokenW (used when de-elevating the Node.js child process)."""

    __slots__ = ("_h", "pid")

    def __init__(self, h_process, pid: int):
        self._h  = h_process
        self.pid = pid

    def poll(self):
        ec = ctypes.wintypes.DWORD(0)
        ctypes.windll.kernel32.GetExitCodeProcess(self._h, ctypes.byref(ec))
        return None if ec.value == 259 else int(ec.value)  # 259 = STILL_ACTIVE

    def terminate(self):
        try:
            ctypes.windll.kernel32.TerminateProcess(self._h, 1)
        except Exception:
            pass
        finally:
            try:
                ctypes.windll.kernel32.CloseHandle(self._h)
            except Exception:
                pass


class _HotkeyManager:
    """
    Registers a Windows global hotkey (RegisterHotKey) and calls a callback
    on the wx main thread when the hotkey is pressed from any application.

    A background thread owns the Win32 message loop (GetMessageW) so WM_HOTKEY
    is received even when WinZapp is minimised or in the background.

    RegisterHotKey ties the registration to the CALLING THREAD's message
    queue, not to the process — reported live by multiple users as "the
    hotkey just stops working out of nowhere, with the key combo passing
    straight through to whatever window is active, and the app never
    notices". Two real gaps made that possible and invisible:
      1. RegisterHotKey failing (e.g. another app transiently holds the same
         combo right at boot) was only ever tried once, and logged via a
         bare print() — which goes to stdout, not stderr, so it never even
         reached log.log in a windowed/frozen build (setup_logging() only
         redirects stderr). Retried below, and now logged with logging.error.
      2. Nothing ever re-affirmed the registration was still alive after
         that. A periodic refresh (unregister + re-register every few
         minutes) below is a low-cost way to self-heal from a registration
         silently dropped by Windows (e.g. around a display/power state
         change) without waiting for the user to notice and restart the app.
    """

    _WM_HOTKEY = 0x0312
    _HOTKEY_ID = 1
    # How often to unregister + re-register as a self-healing refresh, and
    # the retry cadence used only while the very first registration attempt
    # is still failing (e.g. another app transiently holds the combo).
    _REFRESH_INTERVAL_SECONDS = 5 * 60
    _RETRY_INTERVAL_SECONDS = 15

    def __init__(self, vk: int, mod: int, callback):
        self._vk       = vk
        self._mod      = mod
        self._callback = callback
        self._stop     = threading.Event()
        self._thread   = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _register(self) -> bool:
        user32 = ctypes.windll.user32
        # MOD_NOREPEAT (0x4000) suppresses the flood of WM_HOTKEY messages that
        # holding the key down would otherwise generate.
        _MOD_NOREPEAT = 0x4000
        if user32.RegisterHotKey(None, self._HOTKEY_ID, self._mod | _MOD_NOREPEAT, self._vk):
            return True
        # Some keyboard layouts / older Windows builds reject MOD_NOREPEAT;
        # fall back to a plain registration so the hotkey still works.
        return bool(user32.RegisterHotKey(None, self._HOTKEY_ID, self._mod, self._vk))

    def _run(self):
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        class _POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.wintypes.LONG), ("y", ctypes.wintypes.LONG)]

        class _MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd",    ctypes.wintypes.HWND),
                ("message", ctypes.wintypes.UINT),
                ("wParam",  ctypes.wintypes.WPARAM),
                ("lParam",  ctypes.wintypes.LPARAM),
                ("time",    ctypes.wintypes.DWORD),
                ("pt",      _POINT),
            ]

        registered = self._register()
        if not registered:
            logging.error(
                "[HotkeyManager] RegisterHotKey failed (error=%s) — will keep "
                "retrying every %ds instead of giving up silently.",
                kernel32.GetLastError(), self._RETRY_INTERVAL_SECONDS,
            )

        msg = _MSG()
        last_refresh = time.time()
        last_retry = time.time()
        while not self._stop.is_set():
            # MsgWaitForMultipleObjects with a 200 ms timeout so we can check _stop.
            # 0x0088 = QS_HOTKEY | QS_POSTMESSAGE — wake up immediately when a
            # WM_HOTKEY (posted message) arrives instead of waiting for the timeout.
            try:
                ctypes.windll.user32.MsgWaitForMultipleObjects(
                    0, None, False, 200, 0x0088  # QS_HOTKEY | QS_POSTMESSAGE
                )
                if self._stop.is_set():
                    break
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                    if msg.message == self._WM_HOTKEY:
                        wx.CallAfter(self._callback)
            except Exception:
                # Never let a single bad iteration silently kill this thread —
                # that used to mean the hotkey stopped working forever with no
                # trace anywhere, since Windows auto-unregisters a hotkey tied
                # to a thread that's gone.
                logging.exception("[HotkeyManager] Error in hotkey message loop")

            now = time.time()
            if not registered and now - last_retry >= self._RETRY_INTERVAL_SECONDS:
                last_retry = now
                registered = self._register()
                if registered:
                    logging.info("[HotkeyManager] RegisterHotKey succeeded on retry.")
                    last_refresh = now
            elif registered and now - last_refresh >= self._REFRESH_INTERVAL_SECONDS:
                # Self-healing refresh: re-affirm the registration is still
                # alive even though nothing told us it wasn't. Cheap, and the
                # only way to recover from a silent drop without a restart.
                last_refresh = now
                try:
                    user32.UnregisterHotKey(None, self._HOTKEY_ID)
                except Exception:
                    pass
                if not self._register():
                    registered = False
                    logging.warning(
                        "[HotkeyManager] Periodic refresh failed to re-register "
                        "the hotkey (error=%s) — will keep retrying.",
                        kernel32.GetLastError(),
                    )

        if registered:
            user32.UnregisterHotKey(None, self._HOTKEY_ID)

    def stop(self):
        self._stop.set()


def _vk_mod_to_str(vk: int, mod: int) -> str:
    """Convert a (vk, mod) pair to a human-readable string like 'Ctrl+Shift+A'."""
    parts = []
    if mod & 0x0002: parts.append("Ctrl")   # MOD_CONTROL
    if mod & 0x0001: parts.append("Alt")    # MOD_ALT
    if mod & 0x0004: parts.append("Shift")  # MOD_SHIFT
    if mod & 0x0008: parts.append("Win")    # MOD_WIN
    vk_names = {
        0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc",
        0x20: "Space", 0x21: "PgUp", 0x22: "PgDn", 0x23: "End",
        0x24: "Home", 0x25: "Left", 0x26: "Up", 0x27: "Right",
        0x28: "Down", 0x2D: "Ins", 0x2E: "Del", 0x70: "F1",
        0x71: "F2", 0x72: "F3", 0x73: "F4", 0x74: "F5", 0x75: "F6",
        0x76: "F7", 0x77: "F8", 0x78: "F9", 0x79: "F10",
        0x7A: "F11", 0x7B: "F12",
    }
    if vk in vk_names:
        parts.append(vk_names[vk])
    elif 0x30 <= vk <= 0x39:
        parts.append(chr(vk))
    elif 0x41 <= vk <= 0x5A:
        parts.append(chr(vk))
    else:
        parts.append(f"#{vk:02X}")
    return "+".join(parts)


def _get_short_path_name(long_path: str) -> str:
    """Return Windows short (8.3) path to avoid PostgreSQL initdb failures
    when the install path contains accented characters (e.g. 'Área de Trabalho')."""
    try:
        buf_size = ctypes.windll.kernel32.GetShortPathNameW(long_path, None, 0)
        if buf_size:
            buf = ctypes.create_unicode_buffer(buf_size)
            if ctypes.windll.kernel32.GetShortPathNameW(long_path, buf, buf_size):
                return buf.value
    except Exception:
        pass
    return long_path


def _spawn_delevated(cmd: list, cwd: str, log_fh, main_window) -> bool:
    """
    Launch *cmd* as a restricted (non-admin) process using the Windows Safer API.

    SaferCreateLevel(SAFER_LEVELID_NORMALUSER) produces a token where the
    Administrators SID is marked DENY_ONLY, so PostgreSQL's pgwin32_is_admin()
    / CheckTokenMembership() returns FALSE even when the parent holds an
    elevated token, allowing initdb to proceed.

    Returns True and sets main_window.wpp_process on success (de-elevated launch).
    Returns False when de-elevation is impossible or the API call fails.
    """
    import msvcrt

    SAFER_SCOPEID_USER        = 1
    SAFER_LEVELID_NORMALUSER  = 0x20000
    SAFER_LEVEL_OPEN          = 1
    SAFER_TOKEN_NULL_IF_EQUAL = 4
    LOGON_WITH_PROFILE        = 0x00000001
    CREATE_NO_WINDOW          = 0x08000000
    STARTF_USESHOWWINDOW      = 0x00000001
    STARTF_USESTDHANDLES      = 0x00000100
    SW_HIDE                   = 0
    DUPLICATE_SAME_ACCESS     = 0x00000002

    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb",              ctypes.wintypes.DWORD),
            ("lpReserved",      ctypes.wintypes.LPWSTR),
            ("lpDesktop",       ctypes.wintypes.LPWSTR),
            ("lpTitle",         ctypes.wintypes.LPWSTR),
            ("dwX",             ctypes.wintypes.DWORD),
            ("dwY",             ctypes.wintypes.DWORD),
            ("dwXSize",         ctypes.wintypes.DWORD),
            ("dwYSize",         ctypes.wintypes.DWORD),
            ("dwXCountChars",   ctypes.wintypes.DWORD),
            ("dwYCountChars",   ctypes.wintypes.DWORD),
            ("dwFillAttribute", ctypes.wintypes.DWORD),
            ("dwFlags",         ctypes.wintypes.DWORD),
            ("wShowWindow",     ctypes.wintypes.WORD),
            ("cbReserved2",     ctypes.wintypes.WORD),
            ("lpReserved2",     ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput",       ctypes.wintypes.HANDLE),
            ("hStdOutput",      ctypes.wintypes.HANDLE),
            ("hStdError",       ctypes.wintypes.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess",    ctypes.wintypes.HANDLE),
            ("hThread",     ctypes.wintypes.HANDLE),
            ("dwProcessId", ctypes.wintypes.DWORD),
            ("dwThreadId",  ctypes.wintypes.DWORD),
        ]

    try:
        # ── Step 1: create a SAFER level for a normal (non-admin) user ───────
        h_level = ctypes.wintypes.HANDLE(0)
        if not advapi32.SaferCreateLevel(
            SAFER_SCOPEID_USER,
            SAFER_LEVELID_NORMALUSER,
            SAFER_LEVEL_OPEN,
            ctypes.byref(h_level),
            None,
        ):
            print(f"[_spawn_delevated] SaferCreateLevel failed: {kernel32.GetLastError()}")
            return False

        # ── Step 2: compute a restricted token from the current process token ─
        # NULL input token = use the calling thread's primary token (elevated).
        # The result has the Administrators SID as DENY_ONLY so
        # CheckTokenMembership(adminSID) returns FALSE inside node/PostgreSQL.
        h_restricted = ctypes.wintypes.HANDLE(0)
        ok = advapi32.SaferComputeTokenFromLevel(
            h_level, None, ctypes.byref(h_restricted),
            SAFER_TOKEN_NULL_IF_EQUAL, None,
        )
        advapi32.SaferCloseLevel(h_level)

        if not ok or not h_restricted:
            print(f"[_spawn_delevated] SaferComputeTokenFromLevel failed: {kernel32.GetLastError()}")
            return False

        # ── Step 3: duplicate the log file handle for child inheritance ───────
        h_proc    = kernel32.GetCurrentProcess()
        h_log     = msvcrt.get_osfhandle(log_fh.fileno())
        h_log_dup = ctypes.wintypes.HANDLE(0)
        kernel32.DuplicateHandle(
            h_proc, ctypes.wintypes.HANDLE(h_log), h_proc,
            ctypes.byref(h_log_dup), 0, True, DUPLICATE_SAME_ACCESS,
        )

        si             = _STARTUPINFOW()
        si.cb          = ctypes.sizeof(_STARTUPINFOW)
        si.dwFlags     = STARTF_USESHOWWINDOW | STARTF_USESTDHANDLES
        si.wShowWindow = SW_HIDE
        si.hStdOutput  = h_log_dup
        si.hStdError   = h_log_dup
        si.hStdInput   = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE

        # ── Step 4: launch node.exe under the restricted token ────────────────
        pi      = _PROCESS_INFORMATION()
        cmd_str = subprocess.list2cmdline(cmd)
        ok = advapi32.CreateProcessWithTokenW(
            h_restricted, LOGON_WITH_PROFILE, None,
            ctypes.create_unicode_buffer(cmd_str),
            CREATE_NO_WINDOW, None,
            ctypes.create_unicode_buffer(cwd),
            ctypes.byref(si), ctypes.byref(pi),
        )

        kernel32.CloseHandle(h_restricted)
        kernel32.CloseHandle(h_log_dup)

        if not ok:
            print(f"[_spawn_delevated] CreateProcessWithTokenW failed: {kernel32.GetLastError()}")
            return False

        kernel32.CloseHandle(pi.hThread)
        main_window.wpp_process = _Win32Proc(pi.hProcess, int(pi.dwProcessId))
        print("[_spawn_delevated] node.exe launched de-elevated via Safer API")
        return True

    except Exception as e:
        print(f"[_spawn_delevated] failed: {e}")
        return False


class MediaExpiredError(Exception):
    """CDN URL for this media has expired (HTTP 403 or 410 from WhatsApp)."""


# Hard cap on how many messages stay resident in a chat's in-memory records
# list. Without this, a chat that stays active across a long-running session
# (this is a tray app — restarts are rare) grows records forever since
# on_new_message()/on_historical_message() only ever append. The initial
# sync already bounds itself to messages_page_size (default 200) per chat,
# so this only kicks in for chats that keep receiving messages well past
# that after sync — trimming the oldest ones out of RAM (they're still on
# disk in SQLite, just not resident).
_MAX_RESIDENT_MESSAGES_PER_CHAT = 1000

# Message types that are WhatsApp/system-generated rather than something a
# person actually sent, even though they ARE worth showing as the chat-list
# preview text once you're already looking at the list (a revoke reads
# "Mensagem apagada"; a join/leave reads "Fulano entrou no grupo") — see
# MainWindow._PREVIEW_MESSAGE_TYPES/_counts_as_last_message(), the allowlist
# this function builds on. is_countable_message() is strictly narrower than
# that allowlist: these two must ALSO never bump the chat-list sort
# timestamp, inflate the unread badge, or fire a notification, purely
# because a group's metadata changed or someone's own revoke arrived weeks
# after everyone stopped talking in that chat.
_PREVIEW_ONLY_MESSAGE_TYPES = frozenset({"protocolMessage", "groupNotification"})


def is_countable_message(msg: dict) -> bool:
    """True for a message type that should count as real conversation
    activity (unread badge, chat-list sort order, notifications).

    Deliberately built ON TOP of MainWindow._counts_as_last_message()
    (a real-content ALLOWLIST, not a blocklist of known-bad types) rather
    than keeping a second, separately-maintained list: WPPConnect/Baileys
    keep surfacing new WhatsApp-internal system message types
    (e2e_notification, notification_template, "unknown", ...) that carry no
    real content, and two independently-maintained lists are exactly how
    one of them silently missed one — is_countable_message() used to keep
    its own short blocklist, which only excluded groupNotification/
    protocolMessage, so an e2e_notification arriving for a chat nobody had
    messaged in months still bumped it to the top of the list with a
    phantom "1 unread" and nothing to show when opened, and fired a toast
    reading "Nova mensagem de <raw @lid digits>: Mensagem incompatível" —
    a type format_notification_body() had no way to describe. Deriving from
    the same allowlist the preview/sort code already trusts means a type
    only has to be taught to one place to be handled correctly everywhere.
    """
    if not MainWindow._counts_as_last_message(msg):
        return False
    return msg.get("messageType") not in _PREVIEW_ONLY_MESSAGE_TYPES


def _message_ts(msg: dict) -> int:
    """The timestamp of a message record, whichever key it arrived under."""
    if not isinstance(msg, dict):
        return 0
    try:
        return int(msg.get("messageTimestamp") or msg.get("timestamp") or msg.get("t") or 0)
    except (TypeError, ValueError):
        return 0


def history_gap_detected(fetched: list, local_records: list, page_size: int) -> bool:
    """True when a freshly fetched page cannot be joined onto stored history.

    sync_chat_messages() asks get-messages for the newest `page_size`
    messages and nothing else. Whenever a chat received more than that while
    WinZapp was closed, the window lands entirely *after* the newest message
    on disk and the messages in between are never requested by anything —
    reported live on an active group: 1391 messages stored with a 14.5-hour
    hole (08/08 01:56 -> 16:28) and exactly 201 messages on the newer side,
    i.e. one saturated 200-page. WhatsApp's own unread count still counted
    the missing ones, so the chat announced 433 unread and produced ~200.

    Three conditions, because each alone gives a false positive:

    * The page must be **full**. A short page is everything the store had, so
      nothing can be hiding behind it and a wider request would not help.
    * There must be stored messages **older** than the page. With nothing
      older there is no second block to be disjoint from — that is a first
      sync, not a hole.
    * The page must be **mostly new to us**. This is what separates a hole
      from the ordinary case of re-syncing a chat we already hold: a routine
      re-sync returns the same newest messages we already stored, so the
      overlap is near total, while a page on the far side of a hole shares
      almost nothing with what is on disk. Comparing timestamps instead
      cannot tell the two apart — a single live message arriving mid-sync
      drags the newest stored timestamp past the fetched window and masks
      the hole permanently.
    """
    if not fetched or not local_records or page_size <= 0:
        return False
    if len(fetched) < page_size:
        return False

    oldest_fetched = min(_message_ts(m) for m in fetched)
    if not oldest_fetched:
        return False
    if not any(0 < _message_ts(r) < oldest_fetched for r in local_records):
        return False

    fetched_ids = {m.get("key", {}).get("id") for m in fetched if isinstance(m, dict)}
    fetched_ids.discard(None)
    fetched_ids.discard("")
    known = sum(
        1 for r in local_records
        if isinstance(r, dict) and r.get("key", {}).get("id") in fetched_ids
    )
    # Half is deliberately loose: the point is to separate "we already had
    # this window" (overlap near 100%) from "this window is new" (near 0),
    # not to pin down a precise ratio.
    return known * 2 < len(fetched)


def history_gap_closed(fetched: list, local_records: list, hole_top_ts: int) -> bool:
    """True when a widened fetch has reached back into the history we held.

    Deliberately not "history_gap_detected() is now False". That ratio asks
    whether *most* of a page is new, and it misreads the widened case: `known`
    is counted over local_records — the 200 records get_chats() keeps in
    memory — while the widened page holds 800 or 2000. Whenever the widened
    window lands *inside* that snapshot instead of reaching past it, some
    stored records are still older than the page, and `known * 2 <
    len(fetched)` is then true however complete the overlap is — so a hole
    that was in fact reached reads as still open and burns another
    escalation.

    The right question is narrower and has an exact answer: did the widened
    window come back down far enough to touch a message we already had from
    *below* the hole? `hole_top_ts` is the oldest timestamp of the original
    narrow page — the hole's ceiling — so anything stored below it is history
    on the far side. Restricting the comparison that way also makes it immune
    to a live message landing mid-sync: those sit above the ceiling and are
    excluded from the reference set entirely.
    """
    if not fetched or not local_records or not hole_top_ts:
        return False
    below = {
        r.get("key", {}).get("id") for r in local_records
        if isinstance(r, dict) and 0 < _message_ts(r) < hole_top_ts
    }
    below.discard(None)
    below.discard("")
    if not below:
        # Nothing stored below the hole, so there is no far side to reach.
        return False
    got = {m.get("key", {}).get("id") for m in fetched if isinstance(m, dict)}
    return bool(below & got)


class MainWindow(wx.Frame):
    def __init__(self):
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
                if is_post_update:
                    # Clean up old marker on successful post-update start
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
        # Locks and saving state (initialized early to prevent AttributeErrors on early saves/migrations)
        self._save_lock = threading.Lock()
        self._save_timer = None
        self._save_timer_lock = threading.Lock()
        # Guards self.sync_thread creation — see _try_start_sync_thread().
        self._sync_start_lock = threading.Lock()
        self._unresolvable_lids = set()
        self._unresolvable_names = set()
        self._resolving_lids = set()
        self._lid_resolution_lock = threading.Lock()
        self._media_sync_running = False
        self._update_checker = None
        self._wpp_update_checker = None
        self._notification_sound_cache = {}

        self.app_name = "WinZapp"
        self.SetTitle(self.app_name)

        # Detect no-UI background mode (started via --background flag by Windows
        # autostart).  When True: no dialogs, no sounds, no visible window.
        self.background_mode = "--background" in sys.argv
        logging.info("MainWindow: background_mode=%s", self.background_mode)

        #Initialize screen reader/sapi output
        logging.info("MainWindow: Initializing screen reader output...")
        self.speak_output = outputs.auto.Auto()

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
        self.wpp_port      = conn.get("wpp_port",      6300)
        if self.wpp_port == 3417:
            self.wpp_port = 6300
        self.wpp_ws_server = conn.get("wpp_ws_server", "ws://127.0.0.1")
        self.wpp_api_key   = conn.get("wpp_api_key",   "wz-local-api-key")
        self.wpp_custom_api = conn.get("wpp_custom_api", False)
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
            # Check API modules and start WPPConnect Server synchronously BEFORE init_UI
            # so the startup dialog shows first before opening the main conversation list.
            if not self.background_mode:
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
            else:
                def _async_api_init():
                    try:
                        self.ensure_api_modules_installed()
                        self.ensure_wpp_version()
                        self.ensure_wpp_running()
                    except Exception as exc:
                        logging.error("Error in background API init: %s", exc)
                threading.Thread(target=_async_api_init, daemon=True, name="wpp-api-init").start()

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
        # Set once the first real WhatsApp connection of this session is
        # confirmed, so the "connected" sound plays on connection to WhatsApp
        # and not merely on connection to the local API.
        self._wa_connect_announced = False
        # IDs of messages sent by WinZapp itself (via MessageQueue).  Used by
        # WebSocketClient.on_messages_upsert to distinguish "echo of our own
        # send" (skip — already in UI) from "sent on another device" (show).
        # Populated from the MessageQueue worker thread immediately after the
        # API returns the real message ID, so it is always populated before the
        # corresponding WebSocket echo event can be processed.
        self._own_sent_ids: set = set()
        self._own_sent_ids_lock = threading.Lock()
        # Consecutive failed network probes (see check_whatsapp_reachable).
        self._offline_probe_strikes = 0
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

        # Initialise outgoing-message queue (must exist before init_UI so the
        # ConversationsPanel can call self.main_window.message_queue.enqueue).
        self.message_queue = MessageQueue(self)
        # Bounded pool for per-message background work spawned from
        # on_new_message (DB inserts, LID resolution, media downloads).
        self._msg_bg_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="msg-bg"
        )
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

                logging.info("[post_ui_init] STEP 4 — calling prepare_sync()...")
                self.prepare_sync()
                logging.info("[post_ui_init] STEP 4 — prepare_sync() done. Calling wx.CallAfter(set_chats)...")
                # Refresh the chat list on the GUI thread now that the DB is populated.
                wx.CallAfter(self.set_chats)
                logging.info("[post_ui_init] STEP 4 — set_chats scheduled on GUI thread.")

                logging.info("[post_ui_init] STEP 5 — calling check_wa_connection_http()...")
                self.check_wa_connection_http()
                logging.info("[post_ui_init] STEP 5 — check_wa_connection_http() done.")

                if reuse_existing_ws:
                    logging.info("[post_ui_init] STEP 6 — Skipping WebSocket reconnect — already connected from pairing.")
                    return

                logging.info("[post_ui_init] STEP 6 — connecting WebSocket...")
                try:
                    self.connect_websocket()
                    logging.info("[post_ui_init] STEP 6 — WebSocket connected successfully.")
                except Exception as e:
                    logging.exception("[post_ui_init] STEP 6 — Exception during websocket connection")
                    self.error_sound.play()
                    error_str = str(e)
                    if "Invalid namespace" in error_str or "namespaces failed to connect" in error_str:
                        logging.info("[post_ui_init] STEP 6 — WebSocket namespace invalid (instance not on server). Triggering logout.")
                        def _gui_logout():
                            wx.MessageBox(
                                self.i18n.t("device_logged_out"),
                                self.i18n.t("error").format(self.app_name),
                                wx.OK | wx.ICON_ERROR,
                            )
                            self._on_disconnect()
                        wx.CallAfter(_gui_logout)
                    else:
                        def _gui_failed():
                            wx.MessageBox(
                                self.i18n.t("websocket_failed_reconnect"),
                                self.i18n.t("connection_error"),
                                wx.OK | wx.ICON_WARNING,
                            )
                            self.connect.show_connection_dial()
                        wx.CallAfter(_gui_failed)
                    # On websocket failure, clear the "connecting" status so the
                    # UI is never left frozen on that label.
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
        self.main_panel = wx.Panel(self)

        self.navigation_panel = NavigationPanel(self, self.main_panel)
        self.content_panel = wx.Panel(self.main_panel)
        self.conversations_panel = ConversationsPanel(self, self.content_panel)
        self.archived_conversations_panel = ArchivedConversationsPanel(
            self, self.content_panel
        )
        self.archived_conversations_panel.Hide()
        self.status_panel = StatusPanel(self, self.content_panel)
        self.status_panel.Hide()

        # Content panel: all panels fill it; only one is shown at a time
        content_sizer = wx.BoxSizer(wx.VERTICAL)
        content_sizer.Add(self.conversations_panel, 1, wx.EXPAND)
        content_sizer.Add(self.archived_conversations_panel, 1, wx.EXPAND)
        content_sizer.Add(self.status_panel, 1, wx.EXPAND)
        self.content_panel.SetSizer(content_sizer)

        # Main panel: nav sidebar on left, content on right
        main_sizer = wx.BoxSizer(wx.HORIZONTAL)
        main_sizer.Add(self.navigation_panel, 0, wx.EXPAND | wx.ALL, 5)
        main_sizer.Add(self.content_panel, 1, wx.EXPAND | wx.ALL, 5)
        self.main_panel.SetSizer(main_sizer)

        # Frame sizer
        frame_sizer = wx.BoxSizer(wx.VERTICAL)
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

        # Windows shutdown/restart/logoff. Without these, Windows simply
        # terminates the process, and node.exe + its Chrome child die with it —
        # Chrome's profile (which holds the WhatsApp Web linked-device
        # credentials in an IndexedDB/LevelDB store) is torn mid-write and can
        # come back corrupted, which looks exactly like being unlinked even
        # though the phone still shows the session. Ask Windows to hold the
        # shutdown while we close WPPConnect properly.
        self.Bind(wx.EVT_QUERY_END_SESSION, self._on_query_end_session)
        self.Bind(wx.EVT_END_SESSION, self._on_end_session)

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
        app.MainLoop()

    # ── Menu bar ─────────────────────────────────────────────────────────────

    def _build_menubar(self):
        """Create the menu bar with Arquivo, Sincronização and Ajuda menus."""
        self._ID_MARK_ALL_READ = wx.NewIdRef()
        self._ID_SETTINGS      = wx.NewIdRef()
        self._ID_DISCONNECT    = wx.NewIdRef()
        self._ID_EXIT          = wx.NewIdRef()
        self._ID_RESYNC_ALL    = wx.NewIdRef()
        self._ID_SYNC_MEDIA    = wx.NewIdRef()
        self._ID_OFFLINE_MENU  = wx.NewIdRef()
        self._ID_SHORTCUTS     = wx.NewIdRef()
        self._ID_FORCE_UPDATE  = wx.NewIdRef()
        self._ID_FORCE_REINSTALL_ZIP = wx.NewIdRef()
        self._ID_FORCE_REINSTALL_WPP = wx.NewIdRef()
        self._ID_ABOUT         = wx.NewIdRef()

        menubar = wx.MenuBar()

        # ── Arquivo ───────────────────────────────────────────────────────────
        file_menu = wx.Menu()
        file_menu.Append(
            self._ID_MARK_ALL_READ,
            f"{self.i18n.t('menu_mark_all_read')}\tCtrl+Shift+Alt+M",
        )
        file_menu.AppendSeparator()
        file_menu.Append(
            self._ID_SETTINGS,
            f"{self.i18n.t('menu_settings')}\tCtrl+,",
        )
        file_menu.AppendSeparator()
        file_menu.Append(
            self._ID_DISCONNECT,
            f"{self.i18n.t('menu_disconnect')}\tCtrl+Alt+Shift+D",
        )
        file_menu.AppendSeparator()
        file_menu.Append(
            self._ID_EXIT,
            f"{self.i18n.t('menu_exit')}\tCtrl+Alt+Shift+Q",
        )
        menubar.Append(file_menu, self.i18n.t("menu_file"))

        # ── Sincronização ─────────────────────────────────────────────────────
        sync_menu = wx.Menu()
        sync_menu.Append(
            self._ID_RESYNC_ALL,
            f"{self.i18n.t('menu_resync_all')}\tF5",
        )
        sync_menu.Append(
            self._ID_SYNC_MEDIA,
            f"{self.i18n.t('menu_sync_media')}\tCtrl+Shift+Alt+B",
        )
        self._sync_offline_menu_item = sync_menu.AppendCheckItem(
            self._ID_OFFLINE_MENU,
            f"{self.i18n.t('tray_offline_mode')}\tCtrl+Alt+Shift+O",
        )
        self._sync_offline_menu_item.Check(bool(self.offline_mode))
        menubar.Append(sync_menu, self.i18n.t("menu_sync"))

        # ── Ajuda ─────────────────────────────────────────────────────────────
        help_menu = wx.Menu()
        help_menu.Append(
            self._ID_SHORTCUTS,
            f"{self.i18n.t('menu_shortcuts')}\tF1",
        )
        help_menu.AppendSeparator()
        help_menu.Append(self._ID_FORCE_UPDATE, self.i18n.t("menu_force_update"))
        help_menu.Append(self._ID_FORCE_REINSTALL_ZIP, self.i18n.t("menu_force_reinstall_zip"))
        help_menu.Append(self._ID_FORCE_REINSTALL_WPP, self.i18n.t("menu_force_reinstall_wpp"))
        help_menu.AppendSeparator()
        help_menu.Append(self._ID_ABOUT, self.i18n.t("menu_about"))
        menubar.Append(help_menu, self.i18n.t("menu_help"))

        self.SetMenuBar(menubar)
        self.Bind(wx.EVT_MENU, self._on_mark_all_read, id=self._ID_MARK_ALL_READ)
        self.Bind(wx.EVT_MENU, self.on_ctrl_comma,     id=self._ID_SETTINGS)
        self.Bind(wx.EVT_MENU, self._on_menu_disconnect, id=self._ID_DISCONNECT)
        self.Bind(wx.EVT_MENU, lambda e: self.real_exit(), id=self._ID_EXIT)
        self.Bind(wx.EVT_MENU, self._on_menu_resync_all, id=self._ID_RESYNC_ALL)
        self.Bind(wx.EVT_MENU, self._on_menu_sync_media, id=self._ID_SYNC_MEDIA)
        self.Bind(wx.EVT_MENU, self._on_menu_toggle_offline, id=self._ID_OFFLINE_MENU)
        self.Bind(wx.EVT_MENU, self.on_f1,             id=self._ID_SHORTCUTS)
        self.Bind(wx.EVT_MENU, self._on_force_update,  id=self._ID_FORCE_UPDATE)
        self.Bind(wx.EVT_MENU, self._on_force_reinstall_zip, id=self._ID_FORCE_REINSTALL_ZIP)
        self.Bind(wx.EVT_MENU, self._on_force_reinstall_wpp, id=self._ID_FORCE_REINSTALL_WPP)
        self.Bind(wx.EVT_MENU, self._on_about,         id=self._ID_ABOUT)

    def _refresh_menubar(self):
        """Retranslate the menu bar labels after a language change."""
        mb = self.GetMenuBar()
        if mb is None:
            return
        file_menu = mb.GetMenu(0)
        mb.SetMenuLabel(0, self.i18n.t("menu_file"))
        file_menu.FindItemById(self._ID_MARK_ALL_READ).SetItemLabel(
            f"{self.i18n.t('menu_mark_all_read')}\tCtrl+Shift+Alt+M"
        )
        file_menu.FindItemById(self._ID_SETTINGS).SetItemLabel(
            f"{self.i18n.t('menu_settings')}\tCtrl+,"
        )
        file_menu.FindItemById(self._ID_DISCONNECT).SetItemLabel(
            f"{self.i18n.t('menu_disconnect')}\tCtrl+Alt+Shift+D"
        )
        file_menu.FindItemById(self._ID_EXIT).SetItemLabel(
            f"{self.i18n.t('menu_exit')}\tCtrl+Alt+Shift+Q"
        )
        mb.SetMenuLabel(1, self.i18n.t("menu_sync"))
        mb.GetMenu(1).FindItemById(self._ID_RESYNC_ALL).SetItemLabel(
            f"{self.i18n.t('menu_resync_all')}\tF5"
        )
        mb.GetMenu(1).FindItemById(self._ID_SYNC_MEDIA).SetItemLabel(
            f"{self.i18n.t('menu_sync_media')}\tCtrl+Shift+Alt+B"
        )
        mb.GetMenu(1).FindItemById(self._ID_OFFLINE_MENU).SetItemLabel(
            f"{self.i18n.t('tray_offline_mode')}\tCtrl+Alt+Shift+O"
        )
        mb.SetMenuLabel(2, self.i18n.t("menu_help"))
        mb.GetMenu(2).FindItemById(self._ID_SHORTCUTS).SetItemLabel(
            f"{self.i18n.t('menu_shortcuts')}\tF1"
        )
        mb.GetMenu(2).FindItemById(self._ID_FORCE_UPDATE).SetItemLabel(
            self.i18n.t("menu_force_update")
        )
        mb.GetMenu(2).FindItemById(self._ID_FORCE_REINSTALL_ZIP).SetItemLabel(
            self.i18n.t("menu_force_reinstall_zip")
        )
        mb.GetMenu(2).FindItemById(self._ID_FORCE_REINSTALL_WPP).SetItemLabel(
            self.i18n.t("menu_force_reinstall_wpp")
        )
        mb.GetMenu(2).FindItemById(self._ID_ABOUT).SetItemLabel(
            self.i18n.t("menu_about")
        )

    def _on_about(self, event=None):
        """Show application authorship, version and license information."""
        i18n = self.i18n
        info = "\n".join(
            textwrap.fill(line, width=100, break_long_words=False, break_on_hyphens=False)
            for line in (
                i18n.t("about_developed_by"),
                "",
                i18n.t("about_special_thanks"),
                i18n.t("about_translation_thanks"),
                i18n.t("about_main_contributors"),
                i18n.t("about_community_thanks"),
                "",
                i18n.t("about_current_version").format(version=__version__),
                i18n.t("about_license"),
            )
        )

        dialog = wx.Dialog(
            self,
            title=self.i18n.t("about_dialog_title"),
            size=(620, 260),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        panel = wx.Panel(dialog)
        sizer = wx.BoxSizer(wx.VERTICAL)
        info_ctrl = wx.TextCtrl(
            panel,
            value=info,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
        )
        sizer.Add(info_ctrl, 1, wx.EXPAND | wx.ALL, 10)
        close_btn = wx.Button(panel, id=wx.ID_OK, label=self.i18n.t("close"))
        sizer.Add(close_btn, 0, wx.ALIGN_RIGHT | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(sizer)
        dialog.ShowModal()
        dialog.Destroy()

    def _on_menu_disconnect(self, event=None):
        """Arquivo > Desconectar / Ctrl+Alt+Shift+D: confirm before disconnecting.

        This is a destructive, easy-to-trigger-by-accident action (wipes the
        paired session and local data) — unlike _on_disconnect() itself,
        which is also called from automatic/internal flows (e.g. WhatsApp
        reporting the device was logged out elsewhere) where a confirmation
        prompt would be wrong, since that already happened without the user
        asking here.
        """
        if wx.MessageBox(
            self.i18n.t("disconnect_confirm_msg"),
            self.i18n.t("disconnect_confirm_title"),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) == wx.YES:
            self._on_disconnect()

    def _on_disconnect(self, event=None):
        """Disconnect from WhatsApp: wipe credentials, stop WebSocket and show pairing dialog."""
        old_token = self._get_wa_token()
        pi = self.settings.setdefault("privateinfo", {})
        self._set_wa_token("")
        pi.pop("WA_phone_number", None)
        pi.pop("paired", None)
        self.messages_set_completed = False
        self.token = ""
        self.save_settings()
        self.clear_local_data()
        # Reset the connection state as if this were a fresh app launch, not
        # just a fresh WebSocket. Without this, _wa_connect_announced stayed
        # True from the connection that just ended, which permanently
        # disables _set_wa_connected()'s startup grace window (it only
        # applies while "never_connected_yet") — so the very first
        # not-yet-settled status check after re-pairing (WPPConnect/Chrome
        # still booting a fresh session) looked identical to a real outage
        # and immediately declared full auto-offline, seconds after a
        # successful pairing. Reported live as "reconnected, but the app
        # decided I was offline right away, which was wrong."
        self._wa_connected = False
        self._wa_connect_announced = False
        self._auto_offline = False
        self._wa_offline_strikes = 0
        self._wa_startup_time = time.time()
        self._reset_startup_probe()
        # Best-effort: close the WPPConnect session so Chrome is released.
        if old_token:
            def _close():
                try:
                    import requests as _req
                    _req.post(
                        f"{self.wpp_server}:{self.wpp_port}/api/{old_token}/close-session",
                        headers={"Authorization": f"Bearer {old_token}", "Content-Type": "application/json"},
                        timeout=5,
                    )
                except Exception:
                    pass
            threading.Thread(target=_close, daemon=True).start()
        try:
            if self.ws and self.ws.sio.connected:
                self.ws.sio.disconnect()
        except Exception:
            pass
        self.connect.show_connection_dial()

    def _on_mark_all_read(self, event=None):
        """Mark every conversation with unread messages as read."""
        def _worker():
            for jid, chat in list(self.chats.items()):
                if int(chat.get("unreadCount") or 0) > 0:
                    try:
                        self.mark_conversation_as_read(jid)
                    except Exception:
                        pass
        threading.Thread(target=_worker, daemon=True).start()

    def _apply_global_hotkey(self):
        """Register (or unregister) the global hotkey from settings."""
        if not hasattr(self, "_hotkey_manager"):
            return
        if self._hotkey_manager is not None:
            self._hotkey_manager.stop()
            self._hotkey_manager = None
        hk = self.settings.get("general", {}).get("global_hotkey")
        if not hk or not isinstance(hk, dict):
            return
        vk  = hk.get("vk", 0)
        mod = hk.get("mod", 0)
        if vk:
            self._hotkey_manager = _HotkeyManager(vk, mod, self.restore_window)

    def set_global_hotkey(self, vk: int, mod: int):
        """Save and apply a new global hotkey (vk=0 removes it)."""
        self.settings.setdefault("general", {})
        if vk:
            self.settings["general"]["global_hotkey"] = {"vk": vk, "mod": mod}
        else:
            self.settings["general"].pop("global_hotkey", None)
        self.save_settings()
        self._apply_global_hotkey()

    def _set_status(self, status: str):
        """Update window title and tray tooltip to reflect current status."""
        self._tray_status = status
        self._update_title()

    def _update_title(self):
        """
        Rebuild the frame title from the app name, the number of conversations
        with unread messages and the current status, e.g.:
          "WinZapp"
          "WinZapp (2)"
          "WinZapp (2) | modo offline"
          "WinZapp (3) | baixando mídias"
        """
        title   = self.i18n.t("app_name")
        if not getattr(self, "_initial_sync_running", False):
            deleted = self._deleted_chats
            # Archived conversations are intentionally excluded here — they
            # get their own unread indicator on the "Conversas arquivadas"
            # nav item (see NavigationPanel) instead of inflating the count
            # the user sees in the window title, which used to make the
            # title claim unread conversations that were not visible
            # anywhere in the main conversations list.
            unread_chats = sum(
                1 for jid, chat in list(self.chats.items())
                if jid not in deleted
                and not self.is_chat_archived(jid)
                and effective_unread_count(chat) > 0
            )
            if unread_chats:
                title += f" ({unread_chats})"
        if hasattr(self, "navigation_panel"):
            self.navigation_panel.refresh_archived_label()
        if self.offline_mode:
            title += f" | {self.i18n.t('tray_offline_mode')}"
        if self._tray_status:
            title += f" | {self._tray_status}"
        self.SetTitle(title)
        if getattr(self, "tray_icon", None) is not None:
            self.tray_icon.update_tooltip()

    def _allow_ui_focus_changes(self) -> bool:
        """Return True only when WinZapp is already visible and active."""
        return (
            not self.background_mode
            and not getattr(self, "_window_hidden", False)
            and self.IsShown()
            and not self.IsIconized()
            and self.IsActive()
        )

    def toggle_offline_mode(self):
        """
        Toggle the user-controlled offline mode (tray menu item / Sincronização menu).
        While offline the outgoing message queue is suspended; disabling it
        wakes the queue so pending messages are sent immediately.
        """
        self._user_offline = not self._user_offline
        self.offline_mode_sound.play()
        if self._user_offline:
            self.output(self.i18n.t("offline_mode_enabled"), interrupt=True)
        elif self._auto_offline:
            # Turning the manual switch off does not put us back online when
            # the connection itself is down — say so instead of announcing a
            # state change that did not happen.
            self.output(self.i18n.t("offline_mode_auto_enabled"), interrupt=True)
        else:
            self.output(self.i18n.t("offline_mode_disabled"), interrupt=True)
        self._apply_offline_state()

    def _apply_offline_state(self):
        """Recompute self.offline_mode from its two sources and refresh the UI.

        Safe to call from any thread — the wx work is marshalled with CallAfter.
        """
        effective = bool(self._user_offline or self._auto_offline)
        was_offline = bool(self.offline_mode)
        self.offline_mode = effective
        if was_offline and not effective:
            if getattr(self, "message_queue", None) is not None:
                # Back online: send whatever piled up while we were paused.
                self.message_queue.flush()
            # Leaving offline mode — whether the automatic detector cleared it
            # or the user flipped the tray switch off manually — must force a
            # resync the same way a reconnection does. Previously only the
            # connection health-check path (_set_wa_connected) reset
            # _sync_completed, so toggling the *manual* switch off while the
            # connection itself never actually dropped left the app "online"
            # but permanently stuck on whatever was synced before, since
            # trigger_sync_if_needed() no-ops once _sync_completed is True.
            if getattr(self, "_wa_connected", False):
                self._sync_completed = False
                self._last_sync_attempt_ts = 0
                self.trigger_sync_if_needed()

        def _ui():
            self._update_title()
            if getattr(self, "_sync_offline_menu_item", None) is not None:
                # The menu item reflects the *user* toggle only: an automatic
                # offline caused by a dead connection is not something the user
                # can uncheck, and showing it checked would make the next click
                # a no-op from their point of view.
                self._sync_offline_menu_item.Check(bool(self._user_offline))
        if wx.IsMainThread():
            _ui()
        else:
            wx.CallAfter(_ui)

    def _on_power_suspended(self, event):
        logging.info("[power] System is suspending.")
        event.Skip()

    def _on_power_resume(self, event):
        """Force a reconnection check right after the system wakes up.

        Left alone, the app relied on the 30s health-check loop to
        eventually notice the socket/session died across the suspend —
        which observed live routinely never actually recovered, leaving
        the app permanently in offline mode until manually restarted from
        the tray. Resetting the failure-strike counters and re-probing
        immediately (rather than waiting up to 30s, plus however many
        strikes are now required) gives both the HTTP health check and the
        WebSocket a clean slate to reconnect against right away.
        """
        logging.info("[power] System resumed from suspend — forcing reconnection check.")
        self._wa_http_fail_strikes = 0
        self._offline_probe_strikes = 0
        threading.Thread(target=self._recover_from_suspend, daemon=True).start()
        event.Skip()

    def _recover_from_suspend(self):
        try:
            if self.ws is not None and not getattr(self.ws.sio, "connected", False):
                self._reconnect_websocket_now()
            self.check_wa_connection_http()
            self.trigger_sync_if_needed()
        except Exception:
            logging.exception("[power] _recover_from_suspend failed")

    # How long the very first connection attempt of a session gets before the
    # UI is allowed to declare "offline". WPPConnect/Chrome routinely take
    # several seconds to finish booting, during which every status probe looks
    # identical to a real outage (connection refused, CLOSED/INITIALIZING
    # status, etc.) — without this grace window the app announced itself
    # offline within the first second or two of every single launch.
    #
    # A companion cap on the *number* of not-yet-connected readings used to sit
    # alongside this and was removed: see _set_wa_connected(), where callers
    # polling at different rates made a reading count mean different amounts of
    # time and silently shrank this window to about a second.
    #
    # The window can stay generous because it is no longer the only thing
    # separating "still booting" from "no internet" — see
    # _startup_offline_confirmed(), which ends it early once the machine is
    # shown to have no network at all.
    _WA_STARTUP_GRACE_SECONDS = 45

    # How often the network itself may be probed while the startup grace is
    # open, and how many consecutive failures confirm the machine really has no
    # connectivity. Two, because a single failure a second after login is just
    # as likely to be DNS not being warm yet as a genuine outage.
    _STARTUP_PROBE_INTERVAL = 3.0
    _STARTUP_PROBE_STRIKES = 2

    def _startup_offline_confirmed(self) -> bool:
        """True once the machine has been shown to have no internet at all
        while the startup grace is still open.

        The grace exists because a booting WPPConnect looks exactly like an
        outage from the status endpoint, so the app must not shout "offline"
        over what is really a slow launch. But the reverse case — the user
        genuinely opened WinZapp with no connection — then waits out the whole
        window before being told anything, and a screen-reader user staring at
        "conectando" for 45 seconds is badly served by that.

        Those two cases are not actually ambiguous: they only look alike
        through WPPConnect's status string. Asking the network directly tells
        them apart, so the grace can stay long for a slow boot and end almost
        immediately when there is nothing to connect to.

        Never blocks the caller. _set_wa_connected() runs on whatever thread
        happened to poll — including the 0.2 s wait loop in _run_sync() — so
        the probe is fired into the background and only its cached verdict is
        read here; the next poll picks the answer up. Rate-limited so that loop
        cannot spawn one probe every 200 ms.
        """
        if getattr(self, "_startup_probe_offline", False):
            return True
        now = time.time()
        if getattr(self, "_startup_probe_running", False):
            return False
        if now - getattr(self, "_startup_probe_at", 0.0) < self._STARTUP_PROBE_INTERVAL:
            return False
        self._startup_probe_at = now
        self._startup_probe_running = True

        def _probe():
            try:
                if self._probe_whatsapp_host():
                    self._startup_probe_fails = 0
                    return
                fails = getattr(self, "_startup_probe_fails", 0) + 1
                self._startup_probe_fails = fails
                if fails >= self._STARTUP_PROBE_STRIKES:
                    self._startup_probe_offline = True
                    logging.warning(
                        "[connection] No route to WhatsApp on %d consecutive probes "
                        "— ending the startup grace early.", fails)
            except Exception:
                logging.exception("[connection] startup network probe failed")
            finally:
                self._startup_probe_running = False

        threading.Thread(target=_probe, daemon=True, name="startup-net-probe").start()
        return False

    def _reset_startup_probe(self) -> None:
        """Forget the startup network verdict, so a later grace window (a
        re-pair, a reconnect) starts from a clean slate instead of inheriting
        a stale "this machine is offline"."""
        self._startup_probe_offline = False
        self._startup_probe_fails = 0
        self._startup_probe_at = 0.0

    def _set_wa_connected(self, connected: bool, reason: str = "", announce: bool = True,
                           confirmed: bool = False):
        """Single entry point for every WhatsApp connection-state transition.

        Keeps three things in lockstep that used to drift apart: the
        ``_wa_connected`` flag the MessageQueue and the sync gate read, the
        automatic offline mode, and the "connected" sound/announcement — which
        previously fired on startup just because the *local* WPPConnect API had
        answered, even with no internet at all.

        ``confirmed`` marks a *definite* negative signal (WhatsApp itself says
        the device is logged out / needs pairing) — those skip the startup
        grace window below and go straight to the offline UI, same as before.
        Everything else (network hiccups, the local API not answering yet,
        WPPConnect still initializing) is ambiguous during the first
        connection attempt of a session and gets the grace window instead.
        """
        if getattr(self, "_shutting_down", False):
            # The app is on its way out (real_exit()) — closing the
            # WPPConnect session ourselves as part of shutdown looks
            # identical, at this layer, to WhatsApp dropping the connection.
            # Nothing about a deliberate quit should announce an "offline"
            # transition with sound/speech in the second before the process
            # actually exits.
            return
        connected = bool(connected)
        was = bool(getattr(self, "_wa_connected", False))
        self._wa_connected = connected
        # Nothing to do only when the flag *and* the derived offline state are
        # both already consistent with `connected`.
        if connected == was and self._auto_offline == (not connected):
            return

        if connected:
            # True exactly when the connection just came back from being down
            # (auto-offline or the app never having connected this session) —
            # NOT when this call merely re-confirms an already-known-good
            # connection (health check ticks fire this constantly while
            # online; only a real transition should force anything below).
            was_offline = not was
            self._wa_offline_strikes = 0
            self._reset_startup_probe()
            self._dead_browser_strikes = 0
            self._auto_repair_dialog_shown = False
            self._auto_offline = False
            self._apply_offline_state()
            logging.info("[connection] WhatsApp connection is up (%s)", reason or "checked")
            if not self._wa_connect_announced:
                self._wa_connect_announced = True
                if not self.background_mode:
                    self.connected_sound.play()
            elif announce and not self.background_mode:
                self.output(self.i18n.t("connection_restored"), interrupt=False)
            # Clear only the transient "connecting"/"disconnected" text — a
            # sync running in parallel owns the status line otherwise
            # ("sincronizando", "baixando mídias").
            if self._tray_status in (self.i18n.t("tray_wa_disconnected"), self.i18n.t("tray_connecting")):
                wx.CallAfter(self._set_status, "")
            self._sync_retry_count = 0
            if was_offline:
                # Every offline→online transition forces a fresh sync, not
                # just the first one. trigger_sync_if_needed() alone only
                # acts while _sync_completed is False — once a session has
                # synced successfully and then loses connectivity for a
                # while, that flag stays True, so reconnecting silently did
                # nothing: messages.upsert events that WhatsApp never had a
                # live channel to deliver over were never picked up any other
                # way, and the conversation just quietly stayed stale until
                # the user noticed and pressed F5.
                self._sync_completed = False
                # Also clear the retry cooldown timestamp — without this, a
                # sync attempt made shortly before the connection dropped
                # could still be inside its own backoff window and silently
                # swallow this forced resync for up to another 10 minutes.
                self._last_sync_attempt_ts = 0
                # The WebSocket client auto-reconnects on its own (unlimited
                # retries), but its backoff can be up to 60s between
                # attempts — after a longer outage that means "online" could
                # sit for the better part of a minute with the live message
                # channel still down. Nudge it explicitly instead of waiting.
                if self.ws is not None and not getattr(self.ws.sio, "connected", False):
                    threading.Thread(target=self._reconnect_websocket_now, daemon=True).start()
            self.trigger_sync_if_needed()
            return

        self._wa_offline_strikes += 1
        if not confirmed:
            never_connected_yet = not self._wa_connect_announced
            # Bounded by elapsed time alone, deliberately. This used to also
            # cap the number of not-yet-connected readings, and the two bounds
            # were measured in incompatible units: the window was sized for the
            # health checker's 30 s cadence (6 readings would span 3 minutes,
            # so the 45 s clock always ran out first), but _run_sync() polls
            # check_wa_connection_http() every 0.2 s while waiting for the
            # connection. Under that loop the same 6 readings were spent in
            # 1.2 s, collapsing a 45-second grace into barely one second.
            # Observed live: six "INITIALIZING" readings 265 ms apart, then a
            # full "modo offline" announcement with sound and speech — and the
            # session reported itself connected 54 ms later. A reading count is
            # a proxy for elapsed time that breaks the moment anything polls at
            # a different rate; the clock is the thing actually meant here.
            within_grace = (
                never_connected_yet
                and (time.time() - self._wa_startup_time) < self._WA_STARTUP_GRACE_SECONDS
            )
            if within_grace and self._startup_offline_confirmed():
                # Not a slow boot — there is no network to boot onto. Say so
                # now instead of holding "conectando" for the rest of the
                # window.
                logging.warning(
                    "[connection] Startup grace cut short (%s): the machine cannot "
                    "reach WhatsApp at all.", reason or "checked")
                within_grace = False
            if within_grace:
                logging.info(
                    "[connection] Not connected yet during startup grace "
                    "(%s, strike %d) — showing 'connecting' instead of 'offline'.",
                    reason or "checked", self._wa_offline_strikes,
                )
                wx.CallAfter(self._set_status, self.i18n.t("tray_connecting"))
                return

        self._auto_offline = True
        self._apply_offline_state()
        logging.warning("[connection] WhatsApp connection is down (%s)", reason or "checked")
        wx.CallAfter(self._set_status, self.i18n.t("tray_wa_disconnected"))
        # Sound + speech on every genuine transition INTO offline — including
        # the very first one, e.g. starting the app with no internet at all.
        # This used to require `was` (a prior *confirmed* online connection)
        # before announcing anything, so the common "opened with no internet"
        # case went auto-offline in total silence: no sound, no speech in any
        # of the four languages, nothing telling the user why. The early-return
        # guard above (`connected == was and _auto_offline == (not connected)`)
        # already keeps this from repeating on every failed health-check retry
        # — it only reaches here on an actual state change — so this is safe
        # to fire unconditionally, matching the manual toggle's own behaviour
        # (offline_mode_sound + speech) exactly.
        if announce and not self.background_mode:
            self.offline_mode_sound.play()
            self.output(self.i18n.t("offline_mode_auto_enabled"), interrupt=False)

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
            if not self.background_mode:
                wx.CallAfter(self.output, self.i18n.t("sync_media_started"))
            self._media_sync_running = True
            try:
                self.sync_media_for_all_chats()
                if not self.background_mode:
                    wx.CallAfter(self.output, self.i18n.t("sync_media_completed"))
            except Exception as exc:
                logging.exception("[_on_menu_sync_media] Erro ao baixar mídias: %s", exc)
                if not self.background_mode:
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

        self.output(self.i18n.t("resyncing_all_announcement"), interrupt=True)
        threading.Thread(target=self._resync_all_worker, daemon=True).start()

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

            # Wipe the local database and downloaded media/voice-message caches.
            self._sync_completed = False
            # A user-requested resync gets a fresh automatic-retry budget, even if
            # earlier syncs this session already burned through it.
            self._sync_retry_count = 0
            self.clear_local_data()
            try:
                media_failed_path = data_path("media_failed.json")
                if os.path.isfile(media_failed_path):
                    os.remove(media_failed_path)
            except Exception as exc:
                logging.warning("[resync_all] failed to remove media_failed.json: %s", exc)
            self._media_failed_ids = {}

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

    def _on_force_update(self, event):
        if self._update_checker is None:
            self._start_update_checker(force=True)
        else:
            self._update_checker.force_check()

    def _on_force_reinstall_zip(self, event):
        """
        Help > Force Reinstall from ZIP: always downloads and reinstalls the
        latest GitHub release's ZIP, regardless of whether it's actually
        newer than the running version — unlike _on_force_update(), which
        only checks and installs when a newer version exists.
        """
        if self._update_checker is None:
            from updater import UpdateChecker
            self._update_checker = UpdateChecker(self)
        self._update_checker.force_reinstall()

    # ── Auto-updater ──────────────────────────────────────────────────────────

    def _start_update_checker(self, force: bool = False):
        updates_enabled = self.settings.get("general", {}).get("updates_enabled", True)
        if not updates_enabled and not force:
            return
        from updater import UpdateChecker
        self._update_checker = UpdateChecker(self)
        if force:
            self._update_checker.force_check()
        else:
            self._update_checker.start()

    # ── WPPConnect Server updater ───────────────────────────────────────────────
    # Independent of WinZapp's own auto-updater above: the WPPConnect Server
    # WinZapp bundles breaks upstream between WinZapp releases too, and the
    # only fix used to be a user manually deleting client/api/ and node_modules
    # and letting WinZapp reinstall from scratch. This checks the actual
    # wppconnect-team/wppconnect-server GitHub releases directly.

    def _start_wpp_update_checker(self, force: bool = False):
        if self.background_mode:
            return
        updates_enabled = self.settings.get("general", {}).get("updates_enabled", True)
        if not updates_enabled and not force:
            return
        from updater import WppUpdateChecker
        self._wpp_update_checker = WppUpdateChecker(self)
        if force:
            self._wpp_update_checker.force_check()
        else:
            self._wpp_update_checker.start()



    def _on_force_reinstall_wpp(self, event):
        """
        Ajuda > Forçar reinstalação da WPPConnect: always fetches whatever is
        currently the latest wppconnect-server release and replaces the
        installed one with it, regardless of version — the same
        stop → reinstall → restart flow the periodic checker uses, just
        without the "is it actually newer" comparison first. Meant to recover
        a broken/corrupted API install without waiting for a real version
        bump upstream.
        """
        if self._wpp_update_checker is None:
            from updater import WppUpdateChecker
            self._wpp_update_checker = WppUpdateChecker(self)
        self._wpp_update_checker.force_reinstall()

    def _update_wpp_server(self, target_tag: str):
        """
        Stop the running WPPConnect Server, reinstall it at *target_tag* and
        restart it. Must run on the wx main thread (creates modal dialogs);
        both WppUpdateChecker call sites already dispatch here via
        wx.CallAfter, so this can call ShowModal() directly.
        """
        logging.info("[wpp_update] Stopping WPPConnect Server before update to %s...", target_tag)
        if not self.background_mode:
            self.output(self.i18n.t("wpp_update_in_progress"), interrupt=True)
        # Set before stopping the server and only cleared in `finally` below —
        # the health checker (running on its own thread every 30s) would
        # otherwise catch the server mid-stop/reinstall/restart, fail its
        # status-session probe, and declare the app offline/disconnected even
        # though the actual WhatsApp session never dropped.
        self._wpp_updating = True
        try:
            self._stop_wpp_server()
            self.wpp_process = None

            from ui.dialogs.api_setup import ApiSetupDialog
            dlg = ApiSetupDialog(
                self,
                title_override=self.i18n.t("api_update_dialog_title"),
                forced_tag=target_tag,
            )
            result = dlg.ShowModal()
            dlg.Destroy()

            if result != wx.ID_OK:
                logging.warning("[wpp_update] Update to %s was cancelled or failed.", target_tag)
                self.error_sound.play()
                wx.MessageBox(
                    self.i18n.t("wpp_update_failed_msg"),
                    self.i18n.t("update_error_title"),
                    wx.OK | wx.ICON_ERROR,
                    self,
                )
                # Whatever is left on disk (the previous install if the failure
                # happened before ApiSetupDialog's cleanup step, nothing at all if
                # it happened after) — ensure_wpp_running() already knows how to
                # handle both: start what's there, or silently do nothing if the
                # required files are missing.
                self.ensure_wpp_running()
                return

            logging.info("[wpp_update] WPPConnect Server updated to %s — restarting...", target_tag)
            self.ensure_wpp_running()

            # ensure_wpp_running() only confirms the new WPPConnect HTTP API
            # answers — killing the old node.exe process to update it also
            # dropped the Socket.IO connection this app uses for live
            # messages/presence, and nothing else here re-establishes it or
            # tells the new process to resume the WhatsApp session. Left
            # alone, python-socketio's own auto-reconnect (see
            # WebSocketClient.__init__) eventually notices and retries on its
            # own, but with up to a 60 s backoff and no guarantee the
            # WhatsApp session itself gets told to restart — reported live as
            # the app sitting in offline/disconnected mode until the whole
            # program was restarted. Force it explicitly instead of waiting
            # on the passive health checker to get around to it; a
            # successful reconnect's on_connect() handler already re-checks
            # HTTP status and retriggers a sync on its own — but check and
            # trigger a sync explicitly here too, the same recovery sequence
            # _recover_from_suspend() uses, as a fallback in case the socket
            # was already connected (so on_connect() never fires again) or
            # the reconnect itself is slow.
            def _recover_after_update():
                try:
                    self._reconnect_websocket_now()
                    self.check_wa_connection_http()
                    self.trigger_sync_if_needed()
                except Exception:
                    logging.exception("[wpp_update] Post-update reconnection failed")
            threading.Thread(target=_recover_after_update, daemon=True).start()

            # If the window was hidden (minimized to tray) when the update
            # was requested, bring it back — the update can run for minutes
            # and finishes with the API restarting, which is exactly the
            # kind of state change the user needs to see.
            #
            # _window_hidden is only set once __init__ reaches its own
            # "window lifecycle" setup — but WppUpdateChecker's first check
            # is scheduled via wx.CallLater(90000, ...) very early in
            # __init__, well before that point, and its own check can take
            # longer still. If the initial pairing dialog is still on
            # screen 90+ seconds after launch (completely normal — that's a
            # human reading/scanning a QR code) and the user accepts an
            # update from it, _update_wpp_server() runs before
            # self._window_hidden exists at all — getattr() instead of a
            # bare attribute access is what keeps that a no-op instead of
            # an AttributeError crash right after the very first pairing.
            if getattr(self, "_window_hidden", False) and not self.background_mode:
                wx.CallAfter(self.restore_window)

            if not self.background_mode:
                self.output(self.i18n.t("wpp_update_complete"), interrupt=True)
        finally:
            self._wpp_updating = False

    # ── Tray / window lifecycle ───────────────────────────────────────────────

    # ── Online presence ───────────────────────────────────────────────────────

    def _on_window_activate(self, event):
        """
        Fired by wxPython when the main window gains or loses OS focus.
        - Gained focus  → send "available" immediately, then every 20 s
        - Lost focus    → stop the timer, send "unavailable" once

        Debounced: internal modal dialogs (pairing, settings, etc.) can toggle
        the main frame's activation state several times within milliseconds
        as focus moves to/from them, and each toggle used to fire its own
        presence POST. Only the state that's still current after a short
        quiet window gets sent, collapsing that flicker into a single request.
        """
        if self.background_mode:
            event.Skip()
            return
        token = getattr(self, "token", None)
        if not token:
            event.Skip()
            return
        active = event.GetActive()
        if self._presence_debounce_timer is not None and self._presence_debounce_timer.IsRunning():
            self._presence_debounce_timer.Stop()
        self._presence_debounce_timer = wx.CallLater(300, self._apply_window_activate, active)
        event.Skip()

    def _apply_window_activate(self, active: bool):
        """Actually send the presence update after `_on_window_activate`'s debounce settles."""
        if active:
            self._last_activation_time = time.time()
            threading.Thread(
                target=self._send_presence, args=("available",), daemon=True
            ).start()
            if not self._presence_timer.IsRunning():
                self._presence_timer.Start(20_000)   # refresh every 20 s
        else:
            self._presence_timer.Stop()
            threading.Thread(
                target=self._send_presence, args=("unavailable",), daemon=True
            ).start()

    def _on_presence_timer(self, event):
        """Periodic keep-alive: resend 'available' while window is focused."""
        token = getattr(self, "token", None)
        if token:
            threading.Thread(
                target=self._send_presence, args=("available",), daemon=True
            ).start()

    def _send_presence(self, presence: str):
        """
        POST /api/{session}/set-online-presence
        Body: {"isOnline": true | false}

        Always runs on a background thread — never blocks the UI.
        """
        token = getattr(self, "token", None)
        if not token:
            return
        url = f"{self.wpp_server}:{self.wpp_port}/api/{token}/set-online-presence"
        is_online = presence == "available"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            requests.post(url, json={"isOnline": is_online}, headers=headers, timeout=5)
        except Exception:
            pass

    def _init_tray(self):
        """Create the system-tray icon if the setting is enabled."""
        show = self.settings.get("general", {}).get("show_tray_icon", True)
        if show:
            from core.tray_manager import TrayIcon
            self.tray_icon = TrayIcon(self)

    def _on_close(self, event):
        """
        Intercept the window-close button.
        If the tray icon is active, hide the window instead of exiting.

        Uses Win32 ShowWindow(SW_HIDE) directly so that the window is
        physically hidden even when wx's internal IsShown() state has drifted
        out of sync (e.g. after another process showed the window via Win32
        without going through wx's Show() path).
        """
        if self.tray_icon is not None:
            try:
                import ctypes
                ctypes.windll.user32.ShowWindow(self.GetHandle(), 0)  # SW_HIDE = 0
            except Exception:
                self.Hide()
            self._window_hidden = True
            # One authoritative tray update now that the window is hidden.
            self.tray_icon.update_tooltip()
            event.Veto()
        else:
            self.real_exit()

    def restore_window(self):
        """Bring the WinZapp window to the foreground.

        Uses Win32 ShowWindow + SetForegroundWindow directly to avoid wx
        state-drift: _on_close hides the window via SW_HIDE which bypasses
        wx's internal visibility tracking, so wx-level Show()/Raise() calls
        may silently no-op. SW_SHOWMAXIMIZED both un-hides/un-minimizes the
        window and (unlike SW_RESTORE, which reopens at whatever size it had
        before being hidden) always brings it back maximized — the size the
        app is meant to run at, and the one restore-from-tray is expected to
        return to.
        Also refreshes the chat list in case sync updates happened while the
        window was hidden.
        """
        import ctypes
        hwnd = self.GetHandle()
        SW_SHOWMAXIMIZED = 3
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
        # SetForegroundWindow() alone silently fails when another application
        # holds the foreground lock (Win32 foreground-stealing prevention). When
        # that happened the window stayed hidden/behind and the global hotkey
        # appeared "dead" until the app was restarted. Briefly attaching our
        # input queue to the current foreground thread lifts the lock so the
        # restore is reliable.
        try:
            kernel32 = ctypes.windll.kernel32
            fg_hwnd = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
            cur_thread = kernel32.GetCurrentThreadId()
            attached = False
            if fg_thread and fg_thread != cur_thread:
                attached = bool(user32.AttachThreadInput(fg_thread, cur_thread, True))
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
            if attached:
                user32.AttachThreadInput(fg_thread, cur_thread, False)
        except Exception:
            user32.SetForegroundWindow(hwnd)
        self._window_hidden = False
        # When started via --background the window was never shown; clear the
        # flag so _allow_ui_focus_changes(), _on_window_activate() and the
        # notification window_active check all work correctly from now on.
        self.background_mode = False
        # ShowWindow via Win32 does NOT update wx's internal m_isShown flag, so
        # IsShown() returns False even though the window is physically visible.
        # Calling Show(True) syncs the flag without causing flicker (the window
        # is already visible to Win32 so SW_SHOW is a no-op at the OS level).
        if not self.IsShown():
            self.Show(True)
        if hasattr(self, "conversations_panel"):
            wx.CallAfter(self.add_chats_to_ui)

    def real_exit(self):
        """Completely close WinZapp, removing the tray icon and stopping all threads.

        _stop_wpp_server() waits for WPPConnect to close Chrome gracefully
        (see its own docstring for why that wait matters) and used to run
        right here, synchronously, on the wx main thread — which means the
        message loop stopped pumping for however long that took. Windows
        marks any window whose message loop goes quiet for a few seconds as
        "Not Responding", so quitting looked exactly like a hang, every time,
        for as long as the graceful-stop budget.
        """
        # Set FIRST, before anything else: _stop_wpp_server() below closes
        # the WPPConnect session itself, which — while our WebSocket is
        # still connected — arrives as an ordinary "connection closed" event
        # indistinguishable from a real disconnect. _set_wa_connected()
        # checks this flag and skips entirely, so quitting never announces
        # "modo offline ativado" in the moment before the process exits.
        self._shutting_down = True
        # Stop the presence keep-alive timer before tearing down
        if hasattr(self, "_presence_timer") and self._presence_timer.IsRunning():
            self._presence_timer.Stop()
        # Also cancel a pending debounced activate/deactivate — otherwise it
        # can fire mid-shutdown and spawn a presence POST + restart the timer
        # just stopped above.
        if getattr(self, "_presence_debounce_timer", None) is not None and self._presence_debounce_timer.IsRunning():
            self._presence_debounce_timer.Stop()
        if getattr(self, "tray_icon", None) is not None:
            try:
                self.tray_icon.RemoveIcon()
                self.tray_icon.Destroy()
            except Exception:
                pass
            self.tray_icon = None
        if hasattr(self, "message_queue"):
            self.message_queue.stop()
        if getattr(self, "_update_checker", None) is not None:
            self._update_checker.stop()
        if getattr(self, "_wpp_update_checker", None) is not None:
            self._wpp_update_checker.stop()

        # Hide immediately so quitting still feels instant to the user — the
        # actual shutdown work below (which can legitimately take a few
        # seconds) now happens off the main thread instead, so there is no
        # window left for Windows to watch go quiet.
        try:
            self.Hide()
        except Exception:
            pass

        def _finish_exit():
            # Neither of these touches any wx object — DatabaseBridge.close()
            # is plain threading/asyncio, and _stop_wpp_server() is HTTP +
            # subprocess — so both are safe to run off the main thread.
            self._stop_wpp_server()
            if hasattr(self, "db") and self.db is not None:
                try:
                    self.db.close()
                except Exception:
                    pass
            try:
                wx.GetApp().ExitMainLoop()
            except Exception:
                pass
            import os
            os._exit(0)

        threading.Thread(target=_finish_exit, daemon=True).start()

    # ── Navigate to conversation by JID ──────────────────────────────────────

    def navigate_to_conversation_jid(self, jid: str):
        """Bring the window to front and open the conversation matching jid.

        Only calls restore_window() when the window is actually hidden; if it
        is already visible the caller (e.g. _do_open) has already restored it
        and a second SetForegroundWindow call would steal focus at an unexpected
        moment (e.g. the user has already moved to another app after clicking
        the toast).
        """
        if self._window_hidden:
            self.restore_window()
        if hasattr(self, "conversations_panel"):
            self.conversations_panel.navigate_to_jid(jid)

    # ── Incoming real-time messages ───────────────────────────────────────────

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
        
        def _bg_delete_chat():
            try:
                self.db.delete_chat(lid_jid)
            except Exception as e:
                logging.error(f"[merge_lid] Failed to delete merged LID chat {lid_jid}: {e}")
        threading.Thread(target=_bg_delete_chat, daemon=True).start()

        
        # Redirect active conversation if it was the merged LID chat, or refresh if it is the destination phone chat
        if hasattr(self, "conversations_panel") and self.conversations_panel.conversation:
            active_jid = self.conversations_panel.conversation.get("remoteJid", "")
            if active_jid == lid_jid:
                self.conversations_panel.conversation = self.chats[phone_jid]
                wx.CallAfter(self.conversations_panel.refresh_messages_if_changed)
            elif active_jid == phone_jid:
                wx.CallAfter(self.conversations_panel.refresh_messages_if_changed)

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

    def _apply_possible_edit(self, existing: dict, incoming: dict, remote_jid: str):
        """Detect and apply a text-message edit re-delivered under the same key.id.

        WhatsApp reuses the original message's ID when a text message is
        edited (own edits via edit_message(), or an edit made by anyone else
        from any device) — the edited copy arrives back through the exact
        same live-message channel as any other message, just with a
        duplicate key.id. Without this, the dedup check right above ("already
        stored") silently discarded it, so edits from other people never
        appeared at all, and our own edits only showed locally because
        conversations.py already updates them optimistically when sent.
        Only text messages can be edited (WhatsApp's own edit window never
        applies to media/audio/etc.), so comparing "conversation"/
        "extendedTextMessage" text is a reliable, format-agnostic signal —
        it never fires for a plain re-sync of an unrelated message type.
        """
        if self._apply_remote_revoke(existing, incoming, remote_jid):
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

        old_text = _text_of(existing)
        new_text = _text_of(incoming)
        if old_text is None or new_text is None or old_text == new_text:
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

    def on_new_message(self, msg: dict):
        """
        Called on the main thread (via wx.CallAfter) when a new message
        arrives via the messages.upsert WebSocket event.
        Adds the message to local storage, updates the UI, and sends a
        notification if appropriate.
        """
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
        # WPPConnect/Baileys occasionally reports one of our own sends (seen
        # with self-chat text, audio and documents) tagged with an identity
        # that isn't our real phone JID, in one of two shapes:
        #
        #  (a) "participant" (the actual sender/author, per wa-js semantics)
        #      has the same digits as "remoteJid" (the chat). For a real
        #      GROUP, remoteJid is the group's own independently-allocated
        #      ID, never equal to any participant's JID — so this overlap
        #      alone proves it's not a real group, regardless of whatever
        #      fromMe flag WPPConnect attached to the sync echo (observed:
        #      it can arrive as fromMe=False, producing a bogus "new message
        #      from an unnamed participant" notification). For a bare,
        #      not-yet-resolved @lid or @s.whatsapp.net remoteJid, the same
        #      overlap is only unambiguous when fromMe is already True —
        #      for a real 1:1 chat, an incoming (fromMe=False) message's
        #      participant legitimately mirrors remoteJid (the sender IS
        #      the chat), so that combination must NOT be redirected.
        #
        #  (b) remoteJid is suffixed "@g.us" but its digits are simply our
        #      own phone number (with the Brazilian 9th-digit variant) — no
        #      real group JID is ever shaped like a plain phone number.
        #
        # Either shape otherwise spawns an unnamed phantom "group"/duplicate
        # chat that (1) duplicates a message already stored under "Eu" and
        # (2) can't be cleanly identified/deleted afterwards. Redirect to
        # the real self-chat, and opportunistically learn my_lid from case
        # (a) so later messages resolve immediately via _is_self_jid()
        # without waiting on resolve_self_lid()'s async API round-trip.
        participant_raw = key.get("participant") or ""
        remote_digits = remote_jid.split("@", 1)[0]
        part_digits = participant_raw.split("@", 1)[0] if participant_raw else ""
        # Normalize before using as a redirect target below — my_jid can be
        # in raw "@c.us" form early in a session (set directly from the
        # host-device API response, before resolve_self_lid() gets a chance
        # to normalize it), and redirecting to it as-is created yet another
        # duplicate "Eu" chat under @c.us instead of the canonical @s.whatsapp.net one.
        my_jid = self._normalize_jid(getattr(self, "my_jid", ""))
        my_lid = getattr(self, "my_lid", "")
        is_group_jid = remote_jid.endswith("@g.us")

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
        if from_me and participant_raw.endswith("@lid") and not getattr(self, "my_lid", "") and my_lid != participant_raw:
            self.my_lid = my_lid = participant_raw
            if my_jid:
                self.register_jid_mapping(participant_raw, my_jid)

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
            # Plain self-chat message, no group/participant artifact involved
            # — just WhatsApp reporting our own number in the "other" digit
            # variant for this particular event (with vs. without the
            # Brazilian 9th digit). _is_self_jid() already tolerates that
            # when deciding it's self, but without canonicalizing remote_jid
            # here too, each variant kept its own separate chat entry —
            # e.g. sending a photo to yourself as a document created one
            # "Eu" chat for the (9-digit) document echo and a second "Eu"
            # chat for the (8-digit) sync-artifact echo of the same send.
            remote_jid = my_jid

        # Learn/update presence pushName map from incoming message
        if not from_me and self._learn_sender_name(msg):
            self._schedule_save(contacts_dirty=True)

        # Extract mapping and mentions from incoming messages
        self._extract_lid_mapping(msg)

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
                # We don't have the mapping for this new @lid JID!
                # Start a background thread to resolve it, merge it, and update the UI
                def _bg_resolve_new_lid(lid_jid, message_obj):
                    try:
                        pn_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/contact/pn-lid/{lid_jid}"
                        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
                        pn_resp = requests.get(pn_url, headers=headers, timeout=5)
                        if pn_resp.ok:
                            pn_data = pn_resp.json()
                            phone_obj = pn_data.get("phoneNumber") or {}
                            phone_val = phone_obj.get("_serialized") or phone_obj.get("id") or ""
                            if phone_val:
                                if not phone_val.endswith("@s.whatsapp.net") and not phone_val.endswith("@c.us"):
                                    phone_val = f"{phone_val}@s.whatsapp.net"
                                phone_val = self._normalize_jid(phone_val)
                                
                                # Register mapping and merge on the main thread
                                def _main_thread_merge():
                                    self.register_jid_mapping(lid_jid, phone_val)
                                    self._merge_lid_into_phone(lid_jid, phone_val)
                                    self._schedule_set_chats()
                                wx.CallAfter(_main_thread_merge)
                    except Exception as e:
                        logging.warning("[on_new_message] Failed to resolve new LID %s in background: %s", lid_jid, e)
                self._msg_bg_executor.submit(_bg_resolve_new_lid, remote_jid, msg)
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
        self._apply_group_subject_change(remote_jid, chat, msg)

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
            for existing in records:
                if existing.get("key", {}).get("id") == msg_id:
                    self._apply_possible_edit(existing, msg, remote_jid)
                    return  # already stored (edited in place if content changed)



        # Ignore stale re-deliveries of messages the user already cleared.
        if self._is_cleared_message(remote_jid, msg):
            return

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
        self._msg_bg_executor.submit(_bg_insert_msg)

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
                if not hasattr(self, "_new_since_read"):
                    self._new_since_read = {}
                self._new_since_read[remote_jid] = self._new_since_read.get(remote_jid, 0) + 1

        # ── Persist in background — debounced so rapid bursts produce one write ─
        self._schedule_save(dirty_jid=remote_jid)

        # ── Update conversation list UI (debounced to avoid rapid rebuilds) ───
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

        if self.is_chat_muted(remote_jid):
            return
        if self.is_chat_archived(remote_jid):
            return
        if not self.settings.get("general", {}).get("notifications_enabled", True):
            return

        from core.notification_manager import (
            format_notification_title, format_notification_body,
            format_foreground_sender,
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
            # Determine if the incoming message is for the currently-open conversation
            cp = getattr(self, "conversations_panel", None)
            current_jid = (
                cp.conversation.get("remoteJid", "")
                if cp is not None and cp.conversation is not None
                else ""
            )
            is_current_conv = (current_jid == remote_jid)

            if is_current_conv:
                # Scenario 1: message in the ACTIVE conversation
                # Play current-conversation sound, speak "Sender: body" via AO2
                self.message_current_sound.play()
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
                # Play foreground sound, speak "Nova mensagem de X: body" via AO2
                self.message_foreground_sound.play()
                title = format_notification_title(msg, self, self.i18n)
                spoken = self.i18n.t("fg_new_msg").format(name=title) + f": {body}"
                self.output(spoken)
            return  # never send system toast when window is active

        # Window is not focused → send system toast notification
        if not self.settings.get("general", {}).get("show_tray_icon", True):
            return
        title = format_notification_title(msg, self, self.i18n)
        if hasattr(self, "notification_manager"):
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

        # Statuses (stories) or channels ignored
        if remote_jid.endswith("@broadcast") or remote_jid.endswith("@newsletter"):
            return

        # Normalize Alt JID mapping if present
        self._extract_lid_mapping(msg)
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
        if any(r.get("key", {}).get("id") == msg_id for r in records):
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

            if self.is_chat_muted(remote_jid) or self.is_chat_archived(remote_jid):
                return
            if not self.settings.get("general", {}).get("notifications_enabled", True):
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
                self.message_foreground_sound.play()
                self.output(f"{title}: {body}")
                return
            if not self.settings.get("general", {}).get("show_tray_icon", True):
                return
            if hasattr(self, "notification_manager"):
                self.notification_manager.send(title, body, remote_jid)
        except Exception:
            logging.exception("[_maybe_notify_reaction] failed")

    def connect_websocket(self):
        """Connect to the WPPConnect Server WebSocket.

        Connects to both the session namespace and root namespace so that
        global events (qrCode, phoneCode, session-logged) are received.
        Retries up to 6 times with a 2-second delay to handle the brief
        window after session creation where the namespace isn't ready yet.
        """
        import time
        max_attempts = 6
        delay = 2
        last_exc = None
        for attempt in range(1, max_attempts + 1):
            try:
                logging.info("connect_websocket: Attempting connection %d/%d...", attempt, max_attempts)
                if self.ws.sio.connected:
                    self.ws.sio.disconnect()
                # WPPConnect Server only uses the root Socket.IO namespace.
                # All events (qrCode, phoneCode, received-message, etc.) are
                # emitted via req.io.emit() on root "/".
                self.ws.sio.connect(
                    f"{self.wpp_ws_server}:{self.wpp_port}/",
                    socketio_path="socket.io",
                    headers={"apikey": self.token},
                    namespaces=["/"],
                    transports=["websocket"],
                )
                logging.info("connect_websocket: Connected successfully on attempt %d.", attempt)
                return
            except Exception as exc:
                logging.warning("connect_websocket: Attempt %d failed: %s", attempt, exc)
                last_exc = exc
                if attempt < max_attempts:
                    time.sleep(delay)
        raise last_exc

    def _reconnect_websocket_now(self):
        """Force the Socket.IO client to reconnect right away.

        python-socketio already retries forever on its own (reconnection=True,
        reconnection_attempts=0), but its backoff grows up to 60s between
        tries — after an outage long enough to trip auto-offline, "online"
        could otherwise sit for the better part of a minute with the live
        message channel still down. Called on a background thread (blocks on
        the actual handshake) whenever an offline→online transition finds the
        socket not connected.
        """
        try:
            if self.ws is None:
                return
            if getattr(self.ws.sio, "connected", False):
                return
            logging.info("[connection] Forcing WebSocket reconnect after coming back online...")
            self.connect_websocket()
        except Exception as exc:
            logging.warning("[connection] Forced WebSocket reconnect failed (will keep retrying on its own): %s", exc)

    def run_on_main_thread(self, func, *args, **kwargs):
        """
        Execute a callable on the wx main thread using wx.CallAfter if invoked
        from a background thread, blocking until the callable finishes and returning its result.
        If called from the main thread, execute directly.
        """
        if wx.IsMainThread():
            return func(*args, **kwargs)

        result_container = []
        exception_container = []
        event = threading.Event()

        def _wrapper():
            try:
                res = func(*args, **kwargs)
                result_container.append(res)
            except Exception as exc:
                exception_container.append(exc)
            finally:
                event.set()

        wx.CallAfter(_wrapper)
        event.wait()

        if exception_container:
            raise exception_container[0]
        return result_container[0]

    # ── First-run module installation ──────────────────────────────────────

    # Must match start.js's HEADLESS_SHELL_NAMES — start.js searches
    # client/api/.cache for exactly these and launches what it finds.
    _HEADLESS_SHELL_NAMES = ("chrome-headless-shell.exe", "chrome-headless-shell")

    def find_headless_shell(self):
        """Path to chrome-headless-shell inside client/api/.cache, or None."""
        cache_dir = resource_path("api", ".cache")
        if not os.path.isdir(cache_dir):
            return None
        for root, _dirs, files in os.walk(cache_dir):
            for name in files:
                if name in self._HEADLESS_SHELL_NAMES:
                    return os.path.join(root, name)
        return None

    def ensure_headless_shell_installed(self) -> bool:
        """Download chrome-headless-shell if client/api/.cache has none.

        This exists because of *where* the download would otherwise happen.
        start.js has its own execSync fallback, but that runs inside the API
        process at boot — so the download eats ApiStartupDialog's 300 s budget
        while the server has not started listening yet, and a slow connection
        turns a one-off download into "WinZapp failed to start the API".
        Running it here instead puts it before that timer starts.

        Who actually hits this: not fresh installs (ApiSetupDialog step 4.5
        downloads the shell), but everyone *updating* from a build that shipped
        full Chrome. Their client/api/.cache already holds a chrome.exe, so
        every "is the API set up?" check above passes and nothing would fetch
        the shell — the browser is the one piece of the install those checks
        never looked at.

        Best-effort by design: a failure here is logged and start.js's own
        fallback still gets its turn. Returns True when a shell is present
        afterwards.
        """
        existing = self.find_headless_shell()
        if existing:
            logging.info("[headless-shell] Already installed: %s", existing)
            return True

        logging.info(
            "[headless-shell] chrome-headless-shell not found in api/.cache — "
            "downloading it now (before the API startup timer begins)."
        )
        if sys.platform == "win32":
            node_exe = resource_path("node", "node.exe")
            npm_cli = resource_path("node", "node_modules", "npm", "bin", "npm-cli.js")
            npm_cmd = [node_exe, npm_cli]
            node_dir = resource_path("node")
            path_env = node_dir + os.pathsep + os.environ.get("PATH", "")
        else:
            local_node = resource_path("node", "node")
            node_exe = local_node if os.path.isfile(local_node) else (shutil.which("node") or "node")
            local_npm = resource_path("node", "node_modules", "npm", "bin", "npm-cli.js")
            npm_cmd = [node_exe, local_npm] if os.path.isfile(local_npm) else [shutil.which("npm") or "npm"]
            node_dir = os.path.dirname(node_exe) if os.path.isabs(node_exe) else ""
            path_env = (node_dir + os.pathsep + os.environ.get("PATH", "")) if node_dir else os.environ.get("PATH", "")

        api_dir = resource_path("api")
        if not os.path.isdir(api_dir):
            logging.info("[headless-shell] api/ not present yet — skipping.")
            return False

        npm_env = {
            **os.environ,
            "PATH": path_env,
            "PUPPETEER_CACHE_DIR": resource_path("api", ".cache"),
        }
        creation_flags = 0
        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation_flags = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                npm_cmd + ["exec", "puppeteer", "browsers", "install", "chrome-headless-shell"],
                cwd=api_dir,
                env=npm_env,
                creationflags=creation_flags,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = proc.communicate(timeout=1800)
            if proc.returncode != 0:
                logging.error(
                    "[headless-shell] install failed (exit %s): %s",
                    proc.returncode,
                    (stderr_bytes or b"").decode("utf-8", errors="replace")[:500],
                )
            else:
                logging.info(
                    "[headless-shell] installed: %s",
                    (stdout_bytes or b"").decode("utf-8", errors="replace").strip()[:300],
                )
        except Exception as exc:
            logging.error("[headless-shell] install error: %s", exc)

        found = self.find_headless_shell()
        if not found:
            logging.warning(
                "[headless-shell] still missing after the install attempt — start.js "
                "will retry, and falls back to any full Chrome already in the cache."
            )
        return bool(found)

    def ensure_api_modules_installed(self):
        """Ensure the API is installed, then that a headless browser exists.

        Split in two on purpose: every check in _ensure_api_modules_installed()
        looks at api/ (dist/server.js, node_modules) and none of them look at
        the browser, so "the API is fully set up" was true for an install with
        no browser at all — see ensure_headless_shell_installed().
        """
        self._ensure_api_modules_installed()
        self.ensure_headless_shell_installed()

    def _ensure_api_modules_installed(self):
        """
        Ensure the WPPConnect is cloned, compiled, and has its node_modules.

        node/node.exe is mandatory in all scenarios — it is the portable Node.js
        runtime bundled with WinZapp that drives both npm and the API itself.
        Its absence is always a fatal error.

        Depending on what is present inside api/:

          dist/server.js absent →  API not yet cloned/compiled (or the whole
                                    api/ folder was deleted). Show
                                    ApiSetupDialog, which clones + npm installs
                                    + builds. Expected state for a fresh
                                    install or first developer run.

          dist/server.js present
          node_modules absent   →  API already cloned/built, just node_modules
                                    is missing — the normal state of every
                                    fresh WinZapp.zip extract, since
                                    node_modules isn't bundled. Still shows
                                    ApiSetupDialog (the ONE setup dialog this
                                    app has — used to be a second, separately
                                    titled ModuleInstallDialog doing
                                    practically the same thing, which was
                                    confusing and had its own bugs), which
                                    detects dist/server.js already exists and
                                    runs only the npm-install portion of its
                                    flow internally.

          Both present          →  Nothing to do.

        In background mode dialogs are never shown; if the setup is incomplete
        the process exits silently.
        """
        import sys
        import shutil
        if sys.platform == "win32":
            node_exe = resource_path("node", "node.exe")
        else:
            local_node = resource_path("node", "node")
            if os.path.isfile(local_node):
                node_exe = local_node
            else:
                node_exe = shutil.which("node") or "node"

        dist_server  = resource_path("api",  "dist", "server.js")
        node_modules = resource_path("api",  "node_modules")

        # start.js ships bundled with WinZapp itself — it is NOT fetched from
        # WPPConnect's own repo by either install flow below. Its absence
        # means this WinZapp installation itself is incomplete or corrupted
        # (e.g. a partial/interrupted ZIP extraction), not just "WPPConnect
        # hasn't been cloned yet" — attempting either install flow would not
        # fix it (ApiSetupDialog only ever downloads WPPConnect's own source)
        # and would just fail confusingly deep inside npm/WPPConnect startup
        # instead. Fail fast with a clear, actionable message instead of
        # trying anything.
        #
        # api/.env is deliberately NOT checked here: nothing reads it. The
        # WPPConnect side has its dotenv load commented out (api/src/index.ts),
        # start.js takes its settings from config.json plus the environment
        # variables _start_wpp_background() injects (AUTHENTICATION_API_KEY,
        # PORT, ...), and api/.gitignore excludes it so it never shipped in
        # the ZIP either. Requiring it only ever aborted startup on a
        # perfectly good install.
        start_js = resource_path("api", "start.js")
        if not os.path.isfile(start_js):
            logging.error(
                "[ensure_api_modules_installed] Missing required WinZapp file "
                "api/start.js — installation appears incomplete.",
            )
            if not self.background_mode:
                wx.MessageBox(
                    self.i18n.t("api_files_missing_error"),
                    self.i18n.t("error").format(app_name=self.app_name),
                    wx.OK | wx.ICON_ERROR,
                )
            sys.exit(1)

        # Node.js is mandatory — auto-download portable version if missing.
        if not os.path.isfile(node_exe):
            if self.background_mode:
                logging.error("[ensure_api_modules_installed] Node.js not found and cannot show download dialog in background mode")
                sys.exit(0)
            logging.info("[ensure_api_modules_installed] Node.js not found — downloading portable version...")
            from ui.dialogs.node_download import NodeDownloadDialog
            def _show_node_download():
                dlg = NodeDownloadDialog(self)
                res = dlg.ShowModal()
                dlg.Destroy()
                return res
            result = self.run_on_main_thread(_show_node_download)
            if result != wx.ID_OK:
                sys.exit(1)
            # Re-resolve path after download
            if sys.platform == "win32":
                node_exe = resource_path("node", "node.exe")
            # If still missing after download, abort
            if not os.path.isfile(node_exe):
                logging.error("[ensure_api_modules_installed] Node.js download failed — node.exe still missing")
                sys.exit(1)

        # Detect and clean legacy node_modules from WPPConnect to force a clean install of WPPConnect
        wpp_marker = os.path.join(node_modules, "@wppconnect-team")
        if os.path.isdir(node_modules) and not os.path.isdir(wpp_marker):
            logging.info("[ensure_api_modules_installed] Legacy node_modules detected. Cleaning for WPPConnect...")
            try:
                import shutil
                shutil.rmtree(node_modules, ignore_errors=True)
            except Exception as e:
                logging.error("[ensure_api_modules_installed] Failed to remove legacy node_modules: %s", e)

        # ── Check for new required packages in an existing node_modules ──────
        # When we add a new npm dependency (e.g. @ffmpeg-installer/ffmpeg) the
        # user's node_modules is already installed from a previous run, so the
        # normal "node_modules absent" gate never fires. We compare a list of
        # required package markers and run `npm install` silently in the
        # background if any are missing — no dialog needed.
        # Resolved rather than just probed for a directory: @ffmpeg-installer
        # unpacks its binary under a platform-specific subfolder, and WinZapp
        # also accepts a bundled lib/ copy or one on PATH — so "is the package
        # folder there?" is the wrong question, "can we actually find an
        # ffmpeg to run?" is the right one.
        ffmpeg_bin = self._find_api_ffmpeg()
        _REQUIRED_MARKERS = [
            os.path.join(node_modules, "@babel", "runtime"),
        ]
        if os.path.isfile(dist_server) and os.path.isdir(node_modules):
            missing = [m for m in _REQUIRED_MARKERS if not os.path.isdir(m)]
            # Without ffmpeg, _convert_wav_to_ogg() returns None and voice
            # messages go out as raw WAV, which WhatsApp often rejects — a core
            # accessibility feature degrading silently, with only a warning in
            # the log. node_modules existing is not evidence this specific
            # package inside it does, so it gets its own check.
            if not ffmpeg_bin:
                missing.append(os.path.join(node_modules, "@ffmpeg-installer", "ffmpeg"))
            if missing:
                logging.info(
                    "[ensure_api_modules_installed] Missing packages detected: %s — running npm install",
                    missing,
                )
                if sys.platform == "win32":
                    node_exe = resource_path("node", "node.exe")
                    npm_cli  = resource_path("node", "node_modules", "npm", "bin", "npm-cli.js")
                    npm_cmd  = [node_exe, npm_cli]
                    node_dir = resource_path("node")
                    path_env = node_dir + os.pathsep + os.environ.get("PATH", "")
                else:
                    local_node = resource_path("node", "node")
                    if os.path.isfile(local_node):
                        node_exe = local_node
                    else:
                        node_exe = shutil.which("node") or "node"
                    local_npm = resource_path("node", "node_modules", "npm", "bin", "npm-cli.js")
                    if os.path.isfile(local_npm):
                        npm_cmd = [node_exe, local_npm]
                    else:
                        npm_cmd = [shutil.which("npm") or "npm"]
                    node_dir = os.path.dirname(node_exe) if os.path.isabs(node_exe) else ""
                    path_env = (node_dir + os.pathsep + os.environ.get("PATH", "")) if node_dir else os.environ.get("PATH", "")

                npm_env  = {
                    **os.environ,
                    "PATH": path_env,
                    # client/api/.cache — the tree start.js searches for
                    # chrome-headless-shell and re-exports as PUPPETEER_CACHE_DIR.
                    # Pointing at the .cache/puppeteer subfolder instead makes
                    # this installer download into one tree while the server
                    # looks in another.
                    "PUPPETEER_CACHE_DIR": resource_path("api", ".cache"),
                }
                api_dir  = resource_path("api")
                creation_flags = 0
                if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                    creation_flags = subprocess.CREATE_NO_WINDOW

                try:
                    proc = subprocess.Popen(
                        npm_cmd + ["install", "--no-audit", "--no-fund", "--include=optional", "--legacy-peer-deps"],
                        cwd=api_dir,
                        env=npm_env,
                        creationflags=creation_flags,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                    _, stderr_bytes = proc.communicate()
                    if proc.returncode != 0:
                        logging.error(
                            "[ensure_api_modules_installed] npm install failed: %s",
                            (stderr_bytes or b"").decode("utf-8", errors="replace"),
                        )
                    else:
                        logging.info("[ensure_api_modules_installed] npm install completed OK")
                        # This npm install (unlike the main ApiSetupDialog
                        # flow) rebuilt node_modules outside that dialog, so
                        # nothing else re-applies WinZapp's node_modules-level
                        # patches (decrypt.js, host.layer.js pairing-code fix)
                        # afterwards — without this they'd silently regress
                        # every time this "missing package" repair path runs.
                        try:
                            from ui.dialogs.api_setup import ApiSetupDialog
                            ApiSetupDialog._apply_node_modules_patches(api_dir)
                        except Exception as exc:
                            logging.warning(
                                "[ensure_api_modules_installed] Failed to apply node_modules patches: %s", exc
                            )
                except Exception as exc:
                    logging.error("[ensure_api_modules_installed] npm install error: %s", exc)
            else:
                # No new package was missing, so npm install above never ran —
                # but WinZapp's own node_modules-level patches (host.layer.js
                # pairing-code fix, status.layer.js posting-result fix,
                # sender.layer.js sendFile error-detail fix, ...) can still be
                # stale on their own: a WinZapp update that only ADDS or
                # CHANGES one of these patches, without adding a new npm
                # dependency, never reruns npm install, so the patch-reapply
                # above (which only fires as a side effect of that install)
                # never fires either — an already-fully-installed node_modules
                # would otherwise only pick it up via a full API reinstall.
                # Each _patch_* method is idempotent (a cheap string search,
                # no-op if already applied), so checking on every startup is
                # safe.
                try:
                    from ui.dialogs.api_setup import ApiSetupDialog
                    api_dir = resource_path("api")
                    ApiSetupDialog._apply_node_modules_patches(api_dir)
                except Exception as exc:
                    logging.warning(
                        "[ensure_api_modules_installed] Failed to verify/reapply node_modules patches: %s", exc
                    )
            return

        # Everything already set up — nothing to do.
        if os.path.isfile(dist_server) and os.path.isdir(node_modules):
            return

        if self.background_mode:
            sys.exit(0)

        # One dialog for both cases — it detects internally whether
        # dist/server.js already exists and runs only the npm-install portion
        # of its flow when so, instead of a second dialog for that case.
        from ui.dialogs.api_setup import ApiSetupDialog
        def _show_api_setup():
            dlg = ApiSetupDialog(self)
            res = dlg.ShowModal()
            dlg.Destroy()
            return res
        result = self.run_on_main_thread(_show_api_setup)

        if result != wx.ID_OK:
            sys.exit(0)

    # ── WPPConnect version gate ───────────────────────────────────────────────

    def _read_env_value(self, key: str, default: str = "") -> str:
        """Read a value from the bundled client .env file."""
        env_path = resource_path(".env")
        try:
            with open(env_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    if k.strip() == key:
                        return v.strip()
        except Exception:
            pass
        return default

    def _get_installed_wpp_version(self) -> str:
        """Read the WPPConnect Server version from api/package.json."""
        pkg_path = resource_path("api", "package.json")
        try:
            with open(pkg_path, encoding="utf-8") as fh:
                import json as _json
                pkg = _json.load(fh)
            return pkg.get("version", "")
        except Exception:
            return ""


    @staticmethod
    def _version_is_below(installed: str, minimum: str) -> bool:
        """
        Return True when *installed* is strictly older than *minimum*.
        Handles standard semver and pre-release suffixes (e.g. "2.4.0-rc2").
        Returns False on any parsing error so the check never blocks startup
        due to an unexpected version string format.
        """
        if not installed or not minimum:
            return False
        try:
            from packaging.version import Version
            return Version(installed) < Version(minimum)
        except Exception:
            return False

    def ensure_wpp_version(self):
        """
        Compare the installed WPPConnect version against the minimum required
        by this WinZapp build (WPP_MINIMUM_VERSION in client/.env).

        If the installed version is older the user is prompted to:
          • Update now   — re-download + rebuild via ApiSetupDialog, then continue
          • Exit         — terminate WinZapp
          • Continue     — proceed without updating (not recommended)

        The check is skipped when:
          - Running in background mode (no UI)
          - api/package.json is absent (setup not done yet)
          - WPP_MINIMUM_VERSION is not defined in the .env
        """
        if self.background_mode:
            return

        dist_main = resource_path("api", "dist", "main.js")
        if not os.path.isfile(dist_main):
            return  # API not installed yet — setup dialog will handle it

        minimum  = self._read_env_value("WPP_MINIMUM_VERSION")
        if not minimum:
            return  # No minimum defined — nothing to check

        installed = self._get_installed_wpp_version()
        if not installed:
            return  # Could not determine installed version — skip silently

        if not self._version_is_below(installed, minimum):
            return  # Installed version meets (or exceeds) the minimum — all good

        # ── Installed version is older than the minimum ───────────────────────
        from ui.dialogs.api_version_check import (
            ApiVersionOutdatedDialog,
            RESULT_UPDATE, RESULT_EXIT, RESULT_CONTINUE,
        )

        def _show_outdated_dlg():
            dlg = ApiVersionOutdatedDialog(self, self.i18n, installed, minimum)
            res = dlg.ShowModal()
            dlg.Destroy()
            return res
        result = self.run_on_main_thread(_show_outdated_dlg)

        if result == RESULT_EXIT:
            sys.exit(0)

        if result == RESULT_CONTINUE:
            return  # Proceed with the outdated version — user's choice

        # RESULT_UPDATE: re-download and rebuild using the minimum-version tag
        from ui.dialogs.api_setup import ApiSetupDialog
        def _show_update_dlg():
            update_dlg = ApiSetupDialog(
                self,
                title_override=self.i18n.t("api_update_dialog_title"),
                forced_tag=minimum,
            )
            res = update_dlg.ShowModal()
            update_dlg.Destroy()
            return res
        update_result = self.run_on_main_thread(_show_update_dlg)

        if update_result != wx.ID_OK:
            # Update was cancelled or failed — exit to avoid running an
            # incompatible API version
            sys.exit(0)

    # ── WPPConnect lifecycle ─────────────────────────────────────────────────

    def _is_wpp_running(self):
        """Return True if the WPPConnect is already listening on the configured server/port."""
        import urllib.parse
        try:
            parsed = urllib.parse.urlparse(self.wpp_server)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or self.wpp_port
            with _socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    def _start_wpp_background(self):
        """
        Launch the bundled WPPConnect Server node process in the background.
        stdout and stderr are redirected to api/wppconnect.log so that startup
        errors can be shown to the user if the port never opens.
        Does nothing if the node or start.js files are not present (dev mode).

        When the current process is elevated (run as Administrator) the child
        is spawned using the non-elevated linked token via CreateProcessWithTokenW
        so that PostgreSQL's initdb can start (it refuses to run as root/admin).
        """
        import sys
        import shutil

        if sys.platform == "win32":
            node_exe = resource_path("node", "node.exe")
        else:
            local_node = resource_path("node", "node")
            if os.path.isfile(local_node):
                node_exe = local_node
            else:
                node_exe = shutil.which("node") or "node"

        start_js  = resource_path("api",  "start.js")
        if not os.path.isfile(node_exe) or not os.path.isfile(start_js):
            return  # Not bundled — developer runs WPPConnect separately
        try:
            from app_paths import log_path
            self._wpp_log_path = log_path("wppconnect.log")
            log_fh = open(self._wpp_log_path, "w",
                          encoding="utf-8", errors="replace")
            # Use the short (8.3) path so PostgreSQL's initdb doesn't choke on
            # accented characters in the install path (e.g. "Área de Trabalho").
            cwd = _get_short_path_name(resource_path("api"))
            self.wpp_process = None

            # Guarantee that the child Node process inherits the correct API key
            # regardless of whether the local start.js or .env has been preserved.
            os.environ["AUTHENTICATION_API_KEY"] = self.wpp_api_key
            os.environ["WPP_LID_MODE"] = "false"
            os.environ["PORT"] = str(self.wpp_port)
            os.environ["PUPPETEER_CACHE_DIR"] = resource_path("api", ".cache")

            # Ensure dist/config.js has useChrome:false so WPPConnect always uses
            # Puppeteer's own bundled Chrome/Chromium instead of searching for a
            # system Chrome installation. Patched here at runtime so existing users
            # with a pre-built dist/ benefit immediately without a full rebuild.
            try:
                _dist_cfg = resource_path("api", "dist", "config.js")
                if os.path.isfile(_dist_cfg):
                    with open(_dist_cfg, "r", encoding="utf-8") as _f:
                        _cfg_src = _f.read()
                    if "useChrome" not in _cfg_src:
                        _cfg_src = _cfg_src.replace(
                            "createOptions: {",
                            "createOptions: { useChrome: false,",
                            1,
                        )
                        with open(_dist_cfg, "w", encoding="utf-8") as _f:
                            _f.write(_cfg_src)
                        logging.info("[startup] Patched dist/config.js: useChrome → false")
            except Exception as _e:
                logging.warning("[startup] Could not patch dist/config.js: %s", _e)

            # WPPConnect uses Puppeteer/Chrome which already includes --no-sandbox
            # in its config (see api/src/config.ts), so Chrome runs correctly even
            # when the parent process is elevated.  De-elevation via the Safer API
            # is therefore not needed and would prevent Node.js from writing session
            # tokens/cache to the installation directory, breaking admin users.
            creation_flags = 0
            if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                creation_flags = subprocess.CREATE_NO_WINDOW

            self.wpp_process = subprocess.Popen(
                [node_exe, "--max-old-space-size=4096", start_js],
                cwd=cwd,
                creationflags=creation_flags,
                stdout=log_fh,
                stderr=log_fh,
            )
            # Start of the window in which "notLogged"/"QRCODE" readings mean
            # "still booting", not "unlinked" — see _logout_confirmed().
            self._wpp_started_at = time.time()
            # Release Python's file handle now that node.exe has inherited it.
            # This avoids a double-lock on wppconnect.log so an update extraction
            # can overwrite the file once WinZapp exits (only node.exe holds a
            # lock while it is running — we don't need it on the Python side).
            log_fh.close()
            self._wpp_log_fh = None
            atexit.register(self._stop_wpp_server)
        except Exception:
            pass

    # Emitted by WPPConnect (controllers/browser.js) when the WhatsApp Web build it
    # pins is not present in the installed @wppconnect/wa-version package. It is a
    # plain log line, not an error the API ever returns, so nothing downstream sees
    # it — yet it is the direct predecessor of a nasty silent failure: WhatsApp Web
    # then serves its newest build, which the bundled wa-js may not support, and
    # 1:1 sends start failing inside the browser (isSendFailure, ack 0) while the
    # REST call still answers 200 and groups keep working.
    _WPP_VERSION_FALLBACK_MARKER = "using latest as fallback"

    def _check_wpp_version_pin(self):
        """Warn when WPPConnect could not pin the WhatsApp Web version.

        Reads the WPPConnect log written during this startup. Best-effort: any
        problem reading it is logged and otherwise ignored, since this is a
        diagnostic, never a reason to block startup.
        """
        log_file = getattr(self, "_wpp_log_path", None)
        if not log_file or not os.path.isfile(log_file):
            return
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
                # The marker is printed during browser init, well inside the first
                # few dozen lines; reading the whole file would drag in megabytes.
                head = fh.read(200_000)
        except Exception as exc:
            logging.warning("[startup] Could not read %s to check the version pin: %s",
                            log_file, exc)
            return

        for line in head.splitlines():
            if self._WPP_VERSION_FALLBACK_MARKER in line:
                logging.error(
                    "[startup] WPPConnect could not pin the WhatsApp Web version and fell back "
                    "to the live build: %s | Sending to individual contacts may fail silently "
                    "(groups keep working). Fix: npm update @wppconnect/wa-version in client/api/.",
                    line.strip(),
                )
                wx.CallAfter(self.output, self.i18n.t("wpp_version_unpinned_warning"))
                return
        logging.info("[startup] WhatsApp Web version pin OK (no fallback reported by WPPConnect).")

    def _on_query_end_session(self, event):
        """Windows is asking whether it may shut down. Always say yes, but ask
        for extra time first so _on_end_session() can close WPPConnect cleanly
        instead of Chrome being killed mid-write (see _WPP_GRACEFUL_STOP_SECONDS)."""
        try:
            import ctypes
            ctypes.windll.user32.ShutdownBlockReasonCreate(
                self.GetHandle(),
                ctypes.c_wchar_p("Closing the WhatsApp session safely..."),
            )
        except Exception:
            pass
        event.Skip()

    def _on_end_session(self, event):
        """Windows is shutting down: stop WPPConnect gracefully before we go."""
        logging.warning("[_on_end_session] Windows is ending the session — stopping WPPConnect.")
        self._shutting_down = True
        try:
            self._stop_wpp_server()
        except Exception:
            logging.exception("[_on_end_session] Failed to stop WPPConnect cleanly")
        try:
            import ctypes
            ctypes.windll.user32.ShutdownBlockReasonDestroy(self.GetHandle())
        except Exception:
            pass
        event.Skip()

    # How long to wait for WPPConnect's /close-session request to confirm
    # Chrome closed gracefully before giving up and force-killing.
    #
    # This used to be a flat 2-second sleep, and that is very likely how a
    # perfectly valid WhatsApp Web session ended up "logged out" after a
    # restart. The linked-device credentials do NOT live in WinZapp's own
    # settings — they live in Chrome's profile (userDataDir), inside IndexedDB,
    # which is a LevelDB store. `taskkill /F /T` on the Node process kills
    # every child, Chrome included, without giving it a chance to flush and
    # close those files. A LevelDB torn mid-write comes back corrupted, and a
    # WhatsApp Web that cannot read its own key material behaves exactly like a
    # device that was unlinked — while the phone, which was never told
    # anything, keeps listing the session as active. That is precisely the
    # reported symptom.
    #
    # A first fix gave the HTTP request 25s, plus a SEPARATE poll loop after
    # it waiting up to another 25s for the Node process to exit or its port
    # to be released — 50s worst case, and since _stop_wpp_server() ran on
    # the wx main thread at the time, that made every "Sair" look and feel
    # exactly like a hang (Windows marks a quiet message loop "Not
    # Responding" after a few seconds). _stop_wpp_server() now always runs
    # off the main thread (see real_exit()), fixing the "Not Responding"
    # symptom on its own — but that poll loop turned out to be worse than
    # just slow: WPPConnect Server is a persistent multi-session host that
    # never exits or releases its port just because one session's browser
    # closed, so the condition it polled for could never come true. That
    # guaranteed every single exit burned the whole budget and then force-
    # killed the tree regardless of whether Chrome had already closed
    # cleanly in under a second — which was likely the actual, dominant
    # cause of the reported profile corruption, not a slow/hung Chrome. The
    # poll loop is gone; a 200 response from /close-session (which the
    # server only sends after `await`ing client.close()) is trusted directly
    # as proof Chrome is down, and the budget below is the request's own
    # timeout.
    _WPP_GRACEFUL_STOP_SECONDS = 10

    def _stop_wpp_server(self):
        """Terminate the WPPConnect Server process and all its children.

        This does two genuinely different things, and conflating them (as
        this used to) is what made `taskkill /F /T` — the thing that tears
        down Chrome's LevelDB-backed profile mid-write if it's still running
        — fire on essentially every single exit, even when Chrome itself had
        already closed cleanly and quickly:

        1. Ask WPPConnect to close THIS session's Chrome/Puppeteer instance
           via /close-session. Its handler `await`s `client.close()`
           (page.close() + browser.close()) before responding — a 200 here
           genuinely means Chrome is already down; that's the part that
           protects the profile (see _WPP_GRACEFUL_STOP_SECONDS' comment).
        2. Terminate the Node.js server process itself. WPPConnect Server is
           a persistent multi-session host — it never exits or releases its
           port just because one session's browser closed, by design (other
           sessions may still be using it). The old code waited for exactly
           that (proc.poll()/port release) as its "did it close gracefully"
           signal, which can never come true from step 1 alone — so it
           always burned the whole grace budget and always fell through to
           force-killing the tree, Chrome included, regardless of whether
           Chrome had already closed on its own in under a second. Chrome is
           no longer running under this process by the time we get here (if
           step 1 succeeded), so terminating the bare Node process is
           comparatively low-risk — it holds no fragile profile of its own.

        Must not be called on the wx main thread: step 1 can legitimately
        block for the whole grace budget.
        """
        proc = getattr(self, "wpp_process", None)
        token = getattr(self, "token", "")
        browser_closed_cleanly = False
        if token:
            try:
                url = (
                    f"{self.wpp_server}:{self.wpp_port}"
                    f"/api/{token}/close-session"
                )
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self._WPP_GRACEFUL_STOP_SECONDS,
                )
                if resp.status_code == 200:
                    browser_closed_cleanly = True
                    logging.info("[_stop_wpp_server] WPPConnect closed the session's browser gracefully.")
                else:
                    logging.warning(
                        "[_stop_wpp_server] close-session returned HTTP %s — "
                        "Chrome may not have closed cleanly.",
                        resp.status_code,
                    )
            except Exception as e:
                logging.warning(
                    "[_stop_wpp_server] close-session request failed or timed out (%s) — "
                    "Chrome may still be running.",
                    e,
                )

        pid = None
        if proc and proc.poll() is None:
            pid = proc.pid
        elif proc is None:
            # This session never spawned WPPConnect itself — it found the port
            # already open (e.g. a previous session was force-quit and its
            # node.exe never got killed). Locate the orphaned process by the
            # port it's listening on so it doesn't leak across restarts.
            pid = self._find_pid_listening_on_port(self.wpp_port)

        if not pid:
            return

        if not browser_closed_cleanly:
            logging.warning(
                "[_stop_wpp_server] Force-killing WPPConnect (and any Chrome still "
                "running under it) — the graceful close-session above didn't confirm "
                "success, so its profile may not have finished flushing."
            )
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                )
            elif proc is not None:
                proc.terminate()
        except Exception:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    def _find_pid_listening_on_port(self, port):
        """Return the PID of the node.exe process listening on *port* (Windows only).

        Used when this session reused an already-running WPPConnect Server
        left behind by a previous session that was force-quit, so we still
        have a way to terminate it instead of leaving it running forever.
        """
        import sys
        if sys.platform != "win32":
            return None
        no_window = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        try:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"],
                creationflags=no_window,
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return None

        port_suffix = f":{port}"
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0] != "TCP" or parts[3] != "LISTENING":
                continue
            if not parts[1].endswith(port_suffix):
                continue
            try:
                candidate_pid = int(parts[-1])
            except ValueError:
                continue
            try:
                tasklist_out = subprocess.check_output(
                    ["tasklist", "/FI", f"PID eq {candidate_pid}", "/FO", "CSV", "/NH"],
                    creationflags=no_window,
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                continue
            if "node.exe" in tasklist_out.lower():
                return candidate_pid
        return None

    def ensure_wpp_running(self):
        """
        Start the local WPPConnect Server if it is not already listening.

        Normal mode   — shows a progress dialog while waiting (up to 5 min).
        Background mode — polls silently; exits with code 1 on timeout.

        On first launch the database initialisation and migrations can take
        60-90 s; subsequent starts are much faster.  On slower machines (HDD,
        antivirus scanning, or a first-run Puppeteer/Chrome download in
        start.js) startup can take well over 2 minutes, hence the 5-minute
        budget below.
        """
        if self._is_wpp_running():
            return  # Already up (e.g. left running from a previous session)

        import sys
        import shutil

        if sys.platform == "win32":
            node_exe = resource_path("node", "node.exe")
        else:
            local_node = resource_path("node", "node")
            if os.path.isfile(local_node):
                node_exe = local_node
            else:
                node_exe = shutil.which("node") or "node"

        start_js  = resource_path("api",  "start.js")
        dist_server = resource_path("api",  "dist", "server.js")

        # All three files are required to start the bundled API.
        # If any is missing (setup incomplete or not yet run), skip silently —
        # ensure_api_modules_installed() already handled the missing node.exe
        # case; dist/server.js absence means setup was cancelled or not done yet.
        if not (os.path.isfile(node_exe)
                and os.path.isfile(start_js)
                and os.path.isfile(dist_server)):
            return

        self._wpp_log_path = None
        self._wpp_log_fh   = None

        if self.background_mode:
            self._start_wpp_background()
            deadline = time.time() + 300
            while time.time() < deadline:
                if self._is_wpp_running():
                    self._check_wpp_version_pin()
                    return
                time.sleep(1)
            sys.exit(1)

        # A WPPConnect left listening by a previous run (a crash, or an exit
        # that never got to force-kill it) is already serving this port.
        # _start_wpp_background() has no guard of its own, so without this the
        # launch below spawns a second node that cannot bind 6300 and dies —
        # and the dialog then "succeeds" against the *old* server, which may be
        # holding a long-dead Chrome. Reuse what is up, or nothing is.
        if self._is_wpp_running():
            logging.info("[ensure_wpp_running] WPPConnect already listening on %s — reusing it.",
                         self.wpp_port)
            self._check_wpp_version_pin()
            return

        from ui.dialogs.api_startup import ApiStartupDialog
        def _show_startup_dlg():
            dlg = ApiStartupDialog(self, self.wpp_port)
            # Queued rather than called inline so the dialog is painted (and
            # announced by the screen reader) before the launch work begins.
            wx.CallAfter(self._start_wpp_background)
            res = dlg.ShowModal()
            dlg.Destroy()
            return res
        result = self.run_on_main_thread(_show_startup_dlg)

        if result != wx.ID_OK:
            details = ""
            log_path = getattr(self, "_wpp_log_path", None)
            if log_path and os.path.isfile(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    details = "".join(lines[-40:]).strip()
                except Exception:
                    pass
            msg = self.i18n.t("api_startup_warning")
            if details:
                msg = f"{msg}\n\n{details}"
            self.run_on_main_thread(wx.MessageBox, msg, self.app_name, wx.OK | wx.ICON_ERROR)
            sys.exit(1)

        self._check_wpp_version_pin()

    def create_accelerator_table(self):
        #Set IDs
        self.ID_ALT_1      = wx.NewIdRef()
        self.ID_ALT_2      = wx.NewIdRef()
        self.ID_ALT_3      = wx.NewIdRef()
        self.ID_ALT_4      = wx.NewIdRef()
        self.ID_ALT_5      = wx.NewIdRef()
        self.ID_ALT_NAV    = wx.NewIdRef()
        self.ID_CTRL_COMMA = wx.NewIdRef()
        self.ID_F1         = wx.NewIdRef()

        # navigation_panel's "&Navegação principal" label mnemonic is meant
        # to redirect Alt+N to nav_list, but that native StaticText-mnemonic
        # redirect proved unreliable elsewhere in this app (see the Alt+D/
        # Alt+M fixes in ConversationsPanel.create_accel_conversation) —
        # reported live as barely ever working. An explicit global
        # accelerator, extracted from the same i18n mnemonic so it still
        # tracks the label instead of hardcoding "N", works unconditionally
        # from anywhere in the window instead of depending on that mechanism.
        nav_letter = "N"
        _nav_label = self.i18n.t("main_nav")
        _amp = _nav_label.find("&")
        if 0 <= _amp < len(_nav_label) - 1 and _nav_label[_amp + 1].isalpha():
            nav_letter = _nav_label[_amp + 1].upper()

        #create accelerator table
        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_ALT,    ord('1'),    self.ID_ALT_1),
            (wx.ACCEL_ALT,    ord('2'),    self.ID_ALT_2),
            (wx.ACCEL_ALT,    ord('3'),    self.ID_ALT_3),
            (wx.ACCEL_ALT,    ord('4'),    self.ID_ALT_4),
            (wx.ACCEL_ALT,    ord('5'),    self.ID_ALT_5),
            (wx.ACCEL_ALT,    ord(nav_letter), self.ID_ALT_NAV),
            (wx.ACCEL_CTRL,   ord(','),    self.ID_CTRL_COMMA),
            (wx.ACCEL_NORMAL, wx.WXK_F1,  self.ID_F1),
        ])
        self.SetAcceleratorTable(accel_tbl)
        self.Bind(wx.EVT_MENU, self.on_alt_1,       id=self.ID_ALT_1)
        self.Bind(wx.EVT_MENU, self._on_global_alt2, id=self.ID_ALT_2)
        self.Bind(wx.EVT_MENU, self._on_global_alt3, id=self.ID_ALT_3)
        self.Bind(wx.EVT_MENU, self.on_alt_4,       id=self.ID_ALT_4)
        self.Bind(wx.EVT_MENU, self.on_alt_5,       id=self.ID_ALT_5)
        self.Bind(wx.EVT_MENU, self._on_alt_nav,    id=self.ID_ALT_NAV)
        self.Bind(wx.EVT_MENU, self.on_ctrl_comma,  id=self.ID_CTRL_COMMA)
        self.Bind(wx.EVT_MENU, self.on_f1,          id=self.ID_F1)

    def _on_alt_nav(self, event):
        """Alt+N (or the localized equivalent): focus the main navigation list."""
        if hasattr(self, "navigation_panel"):
            self.navigation_panel.nav_list.SetFocus()

    def _ensure_conversations_panel_visible(self):
        """Bring conversations_panel to the front if some other top-level
        panel (archived/status/settings) is currently showing instead.

        _on_global_alt2/_on_global_alt3 act on whichever conversation is
        open regardless of which panel currently has focus — but a
        conversation staying "open" (self.conversations_panel.conversation
        still set) after the user switched to, say, the Status tab used to
        mean Alt+2/Alt+3 jumped focus inside a conversations_panel that
        stayed hidden: nothing visibly happened, reported live as "doesn't
        switch to the right panel" when called from anywhere but the
        conversations/archived panels.
        """
        if self.conversations_panel.IsShown():
            return
        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel.Hide()
        if hasattr(self, "status_panel"):
            self.status_panel.Hide()
        # Deliberately does NOT touch conversations_label/conversations_list
        # visibility (unlike on_alt_1(), which always returns to the LIST
        # view) — a conversation being open here means the detail pane, not
        # the list, is what should already be showing; whichever of the two
        # was visible before the user switched away stays as-is.
        self.conversations_panel.Show()
        self.content_panel.Layout()

    def _on_global_alt2(self, event):
        """Alt+2: jump to last message regardless of which panel has focus."""
        cp = getattr(self, "conversations_panel", None)
        if cp is not None and cp.conversation is not None:
            self._ensure_conversations_panel_visible()
            cp._on_accel_jump_last(event)

    def _on_global_alt3(self, event):
        """Alt+3: jump to unread separator regardless of which panel has focus."""
        cp = getattr(self, "conversations_panel", None)
        if cp is not None and cp.conversation is not None:
            self._ensure_conversations_panel_visible()
            cp._on_accel_jump_unread(event)

    def on_f1(self, event):
        from ui.dialogs.shortcuts_dialog import ShortcutsDialog
        dlg = ShortcutsDialog(self)
        dlg.ShowModal()
        dlg.Destroy()

    def on_ctrl_comma(self, event):
        self.open_settings()

    def open_settings(self):
        from ui.dialogs.settings_dialog import SettingsDialog
        dlg = SettingsDialog(self)
        dlg.ShowModal()
        dlg.Destroy()

    def apply_language_changes(self):
        """Refresh all visible translatable text after a language change."""
        if not hasattr(self, "navigation_panel"):
            return
        self.navigation_panel.refresh_labels()
        self.conversations_panel.refresh_labels()
        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel.refresh_labels()
        if hasattr(self, "status_panel"):
            self.status_panel.refresh_labels()
        # Update frame title (unread indicator + any status suffix)
        self._update_title()
        self.main_panel.Layout()
        # Refresh tray icon tooltip with new language
        if self.tray_icon is not None:
            self.tray_icon.refresh_labels()
        # Refresh menu bar labels
        self._refresh_menubar()

    def on_alt_1(self, event):
        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel.Hide()
        if hasattr(self, "status_panel"):
            self.status_panel.Hide()
        # ArchivedConversationsPanel.on_conversation_selected() hides
        # conversations_panel's own conversations_label/conversations_list
        # (leaving only the conversation detail pane visible) when an
        # archived chat is opened, since ConversationsPanel is the shared
        # widget both the normal and archived lists open messages in. Alt+1
        # showed conversations_panel itself but never undid that hide, so
        # the list _restore_conversation_selection() below focuses/selects
        # a control that stayed hidden — NVDA still announced the row
        # (Select() fires an accessibility event regardless of visibility)
        # but keyboard focus had nothing visible to actually land on,
        # reported live as "announces the first conversation but focus
        # itself is lost", reproducing only when an archived conversation
        # was open.
        self.conversations_panel.conversations_label.Show()
        self.conversations_panel.conversations_list.Show()
        self.conversations_panel.Show()
        self.content_panel.Layout()
        # Restore focus AND selection so the list never ends up empty-focused
        # when navigating back from a conversation or another panel.
        self.conversations_panel._restore_conversation_selection()

    def on_alt_4(self, event):
        self.conversations_panel.Hide()
        if hasattr(self, "status_panel"):
            self.status_panel.Hide()
        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel.Show()
            self.content_panel.Layout()
            self.archived_conversations_panel.restore_selection()

    def on_alt_5(self, event):
        # A conversation left open (archived or not) while switching to
        # Status kept sending typing/recording presence updates for it in
        # the background — the user has no way to see or act on that once
        # this tab's out of view, so close it the same way Esc would,
        # without close_conversation()'s own focus-restoration (this panel
        # sets its own focus right below).
        if self.conversations_panel.conversation is not None:
            self.conversations_panel.close_conversation_for_panel_switch()
        self.conversations_panel.Hide()
        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel.Hide()
        if hasattr(self, "status_panel"):
            self.status_panel.Show()
            self.content_panel.Layout()
            self.status_panel._add_status_btn.SetFocus()
            self.status_panel.on_show()

    def output(self, text, interrupt=False):
        self.speak_output.output(text, interrupt=interrupt)

    # ── Language selection ────────────────────────────────────────────────────

    def _ensure_language_selected(self):
        """
        Show the language-selection dialog if no language has been stored yet
        in settings.  On Cancel the application exits immediately.
        """
        lang_already_set = bool(
            self.settings.get("general", {}).get("language")
        )
        if lang_already_set:
            return

        from ui.dialogs.language_dialog import LanguageSelectionDialog
        dlg    = LanguageSelectionDialog(parent=None)
        result = dlg.ShowModal()
        lang   = dlg.selected_language
        dlg.Destroy()

        if result != wx.ID_OK:
            sys.exit(0)

        self.settings.setdefault("general", {})["language"] = lang
        self.save_settings()

    def _check_api_type_first_run(self):
        """
        Check if we need to ask the user to choose between local and custom/remote API on first launch.
        """
        if self.background_mode:
            return

        gen = self.settings.get("general", {})
        if gen.get("api_type_first_run_asked", False):
            return

        msg = self.i18n.t("api_type_ask_message")
        title = self.i18n.t("api_type_ask_title")

        result = wx.MessageBox(
            msg,
            title,
            wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION,
        )

        if result == wx.YES:
            # User wants local API (default)
            self.settings.setdefault("connection", {})["wpp_custom_api"] = False
            self.wpp_custom_api = False
            self.settings.setdefault("general", {})["api_type_first_run_asked"] = True
            self.save_settings()
        elif result == wx.NO:
            # User wants to specify a custom/remote API
            self.settings.setdefault("connection", {})["wpp_custom_api"] = True
            self.wpp_custom_api = True
            self.save_settings()

            # Open settings dialog on the Connection tab (index 3)
            from ui.dialogs.settings_dialog import SettingsDialog
            dlg = SettingsDialog(self)
            dlg._notebook.SetSelection(3)
            settings_res = dlg.ShowModal()
            dlg.Destroy()

            if settings_res == wx.ID_OK:
                # Successfully configured! Mark as asked.
                self.settings.setdefault("general", {})["api_type_first_run_asked"] = True
                self.save_settings()
            else:
                # User cancelled or closed settings dialog. Roll back and exit.
                self.settings.setdefault("connection", {})["wpp_custom_api"] = False
                self.wpp_custom_api = False
                self.save_settings()
                sys.exit(0)
        else:
            # User cancelled or closed the question box. Exit.
            sys.exit(0)

    def _check_first_run(self):
        """
        Show the autostart-offer dialog exactly once per installation.
        The ``first_run`` flag in settings is cleared immediately to prevent
        re-showing on a subsequent launch if the app crashes after this point.
        """
        if not self.settings.get("general", {}).get("first_run", True):
            return
        # Mark as done before showing the dialog
        self.settings.setdefault("general", {})["first_run"] = False
        self.save_settings()

        result = wx.MessageBox(
            self.i18n.t("autostart_ask_message"),
            self.i18n.t("autostart_ask_title"),
            wx.YES_NO | wx.ICON_QUESTION,
        )
        if result == wx.YES:
            self._apply_autostart(enable=True)
        else:
            self.settings.setdefault("general", {})["autostart"] = False
            self.save_settings()

    def _check_hotkey_first_run(self):
        """
        Show a one-time dialog offering the user a global hotkey to open WinZapp
        from any application.  Guards on ``hotkey_first_run_asked`` so it only
        shows once per installation, right after the autostart prompt.

        The chosen (vk, mod) pair is written to settings immediately; the
        _HotkeyManager is created later in init_UI via _apply_global_hotkey().
        """
        gen = self.settings.get("general", {})
        if gen.get("hotkey_first_run_asked", False):
            return
        # Already has a hotkey configured — mark done without asking again.
        if gen.get("global_hotkey"):
            self.settings.setdefault("general", {})["hotkey_first_run_asked"] = True
            self.save_settings()
            return

        self.settings.setdefault("general", {})["hotkey_first_run_asked"] = True
        self.save_settings()

        from ui.dialogs.settings_dialog import _HotkeyCapture

        dlg = wx.Dialog(
            None,
            title=self.i18n.t("hotkey_first_run_title"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)

        msg_ctrl = wx.StaticText(dlg, label=self.i18n.t("hotkey_first_run_message"))
        msg_ctrl.Wrap(480)
        sizer.Add(msg_ctrl, 0, wx.ALL, 15)

        capture = _HotkeyCapture(
            dlg,
            accessible_name=self.i18n.t("global_hotkey_label"),
        )
        capture.SetHint(self.i18n.t("global_hotkey_hint"))
        sizer.Add(capture, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 15)

        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn   = wx.Button(dlg, wx.ID_OK,     self.i18n.t("ok"))
        skip_btn = wx.Button(dlg, wx.ID_CANCEL, self.i18n.t("hotkey_first_run_skip"))
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(skip_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)

        dlg.SetSizer(sizer)
        sizer.Fit(dlg)
        dlg.CenterOnScreen()

        result = dlg.ShowModal()
        vk  = capture._vk
        mod = capture._mod
        dlg.Destroy()

        if result == wx.ID_OK and vk:
            self.settings.setdefault("general", {})["global_hotkey"] = {"vk": vk, "mod": mod}
            self.save_settings()
            wx.MessageBox(
                self.i18n.t("hotkey_first_run_success").format(hotkey=_vk_mod_to_str(vk, mod)),
                self.i18n.t("autostart_success_title"),
                wx.OK | wx.ICON_INFORMATION,
            )

    def _apply_autostart(self, enable: bool):
        """
        Enable or disable the Windows Run registry entry for WinZapp.

        On success with ``enable=True``: shows a confirmation dialog.
        On failure: shows an error dialog and stores ``autostart=False``.
        Called from ``_check_first_run()`` and from the Settings dialog.
        """
        from autostart import enable_autostart, disable_autostart
        if enable:
            try:
                enable_autostart()
                self.settings.setdefault("general", {})["autostart"] = True
                self.save_settings()
                wx.MessageBox(
                    self.i18n.t("autostart_success_message"),
                    self.i18n.t("autostart_success_title"),
                    wx.OK | wx.ICON_INFORMATION,
                )
            except Exception as exc:
                self.settings.setdefault("general", {})["autostart"] = False
                self.save_settings()
                wx.MessageBox(
                    f"{self.i18n.t('autostart_error_message')}\n\n{exc}",
                    self.i18n.t("error").format(app_name=self.app_name),
                    wx.OK | wx.ICON_ERROR,
                )
        else:
            disable_autostart()
            self.settings.setdefault("general", {})["autostart"] = False
            self.save_settings()

    def _sync_autostart_registry(self):
        """
        Synchronize the Windows Run registry key with the current settings.
        Only runs on Windows. If autostart setting is True, ensures the registry key exists.
        If autostart setting is False (and it's not the first run), ensures the key is removed.
        """
        import sys
        if sys.platform != "win32":
            return

        if self.settings.get("general", {}).get("first_run", True):
            return

        try:
            from autostart import is_autostart_enabled, enable_autostart, disable_autostart
            setting_enabled = self.settings.get("general", {}).get("autostart", False)
            registry_enabled = is_autostart_enabled()

            if setting_enabled and not registry_enabled:
                logging.info("Startup: Autostart is enabled in settings but missing in registry. Enabling...")
                enable_autostart()
            elif not setting_enabled and registry_enabled:
                logging.info("Startup: Autostart is disabled in settings but present in registry. Disabling...")
                disable_autostart()
        except Exception as e:
            logging.error("Startup: Failed to sync autostart registry key: %s", e)

    # ── Quick tip ─────────────────────────────────────────────────────────────

    def _check_quick_tip(self):
        """
        Show the "quick tip" (F1 shortcut hint) once after the user's first
        successful pairing.  Guarded by the ``quick_tip_shown`` setting so it
        never shows twice.
        """
        if self.settings.get("general", {}).get("quick_tip_shown", False):
            return
        self.settings.setdefault("general", {})["quick_tip_shown"] = True
        self.save_settings()
        wx.MessageBox(
            self.i18n.t("quick_tip_message"),
            self.i18n.t("quick_tip_title"),
            wx.OK | wx.ICON_INFORMATION,
            self,
        )

    # ── Terms of service ─────────────────────────────────────────────────────

    def _check_terms_acceptance(self):
        """
        Show the terms-of-service dialog exactly once.
        If the user declines, the application exits immediately.
        """
        if self.settings.get("general", {}).get("terms_alert_displayed", False):
            return

        dlg = wx.Dialog(
            None,
            title=self.i18n.t("terms_title"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        sizer = wx.BoxSizer(wx.VERTICAL)

        msg_ctrl = wx.StaticText(dlg, label=self.i18n.t("terms_message"))
        msg_ctrl.Wrap(480)
        sizer.Add(msg_ctrl, 0, wx.ALL, 15)

        btn_sizer = wx.StdDialogButtonSizer()
        accept_btn = wx.Button(dlg, wx.ID_OK,     self.i18n.t("terms_accept"))
        decline_btn = wx.Button(dlg, wx.ID_CANCEL, self.i18n.t("terms_decline"))
        btn_sizer.AddButton(accept_btn)
        btn_sizer.AddButton(decline_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)

        dlg.SetSizer(sizer)
        sizer.Fit(dlg)
        dlg.CenterOnScreen()

        result = dlg.ShowModal()
        dlg.Destroy()

        if result == wx.ID_OK:
            self.settings.setdefault("general", {})["terms_alert_displayed"] = True
            self.save_settings()
        else:
            sys.exit(0)

    def load_settings(self):
        settings_file = data_path("settings.json")
        default_file = resource_path("data", "settings_default.json")
        fallback_dict = DEFAULT_SETTINGS

        # Bootstrap settings.json if missing
        if not os.path.isfile(settings_file):
            os.makedirs(os.path.dirname(settings_file), exist_ok=True)
            if os.path.isfile(default_file):
                try:
                    shutil.copy2(default_file, settings_file)
                except Exception:
                    pass
            if not os.path.isfile(settings_file):
                try:
                    with open(settings_file, "w", encoding="utf-8") as f:
                        json.dump(fallback_dict, f, indent=4)
                except Exception:
                    pass

        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                self.settings = json.load(f)
        except Exception:
            # If load still fails (e.g. corrupt settings.json), reset to defaults
            # rather than crashing the app — but tell the user, since this
            # silently discards any customization (language, hotkeys, audio
            # devices, etc.) they had saved.
            logging.error("[load_settings] settings.json unreadable, resetting to defaults", exc_info=True)
            self.settings = json.loads(json.dumps(fallback_dict))
            try:
                with open(settings_file, "w", encoding="utf-8") as f:
                    json.dump(self.settings, f, indent=4)
            except Exception:
                pass
            if hasattr(self, "i18n"):
                msg   = self.i18n.t("settings_load_failed")
                title = self.i18n.t("error").format(app_name=self.app_name)
            else:
                from core.i18n import _load_translations
                _pt   = _load_translations("pt-BR")
                msg   = _pt.get("settings_load_failed",
                                "Erro ao carregar o arquivo de configuração:")
                title = _pt.get("error", "{app_name} Erro").format(app_name=self.app_name)
            if hasattr(self, "error_sound"):
                self.error_sound.play()
            if not self.background_mode:
                wx.CallAfter(wx.MessageBox, f"{msg}\n{format_exc()}", title, wx.OK | wx.ICON_WARNING)
        self._migrate_settings()

    def _migrate_settings(self):
        """Migrate settings from old section names to current ones."""
        changed = False
        # audio_default_speed: general → audio_playback
        if "audio_default_speed" in self.settings.get("general", {}):
            speed = self.settings["general"].pop("audio_default_speed")
            self.settings.setdefault("audio_playback", {})["audio_default_speed"] = speed
            changed = True
        # ui → user_interface
        if "ui" in self.settings and "user_interface" not in self.settings:
            self.settings["user_interface"] = self.settings.pop("ui")
            changed = True
        # sound_events: flat {event_key: {...}} (pre-soundpack) → nested
        # {pack_id: {event_key: {...}}}. Sound.play() only ever reads the
        # nested shape (settings["sound_events"][pack_id][event_key]); an
        # install that had "sound_events" saved before soundpacks existed
        # still has the flat shape on disk, so every lookup under a real
        # pack_id came back empty and silently defaulted "enabled" to True —
        # any event a user had disabled before that restructuring (e.g. the
        # startup sound) started playing again despite still showing as
        # "disabled" in Settings, because nothing ever migrated it into the
        # new nested shape. Detect the old shape by an unmistakable signal:
        # a top-level key that is itself a known event name (pack ids never
        # collide with those) rather than a soundpack id.
        events = self.settings.get("sound_events")
        if isinstance(events, dict):
            event_keys = {key for key, _ in SOUND_EVENTS}
            if event_keys & events.keys():
                self.settings["sound_events"] = {DEFAULT_PACK_ID: events}
                changed = True
        if changed:
            self.save_settings()

    @property
    def messages_set_completed(self) -> bool:
        """Get the messages synchronization status from SQLite metadata."""
        if not hasattr(self, "db") or self.db is None:
            return False
        return self.db.get_metadata_json("messages_set_completed", False)

    @messages_set_completed.setter
    def messages_set_completed(self, val: bool):
        """Set the messages synchronization status in SQLite metadata."""
        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("messages_set_completed", val)

    def save_settings(self):
        try:
            target = data_path("settings.json")
            # Write to a temp file in the same directory, then atomically
            # replace the real file. Writing settings.json in place used to
            # truncate it immediately on open("w") — a crash, forced
            # shutdown, or antivirus lock mid-write (this fires often, via
            # the debounced timer below, e.g. on every presence-update burst)
            # left a truncated/corrupt file. load_settings() has no recovery
            # for that: it shows an error and calls sys.exit(), so a
            # corrupted settings.json bricked the app until the user found
            # and deleted/fixed the file by hand. os.replace() is atomic on
            # both Windows and POSIX — the old file is never observably
            # partial, even if the process dies mid-write.
            tmp = f"{target}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4)
            os.replace(tmp, target)
        except Exception:
            self.error_sound.play()
            # save_settings() is called from many places, including several
            # background threads (e.g. WebSocketClient event handlers) — a
            # raw wx.MessageBox() call off the main thread is a real crash
            # risk on Windows, so always marshal it through CallAfter.
            msg   = f"{self.i18n.t('settings_save_failed')} {format_exc()}"
            title = self.i18n.t("error").format(app_name=self.app_name)
            wx.CallAfter(wx.MessageBox, msg, title, wx.OK | wx.ICON_ERROR)

    def _schedule_save_settings(self):
        """Debounce save_settings: coalesce rapid calls into one write after 2 s.

        Used when background events (e.g. presence.update bursts) update settings
        frequently — avoids hammering the disk on every event.
        """
        with self._save_timer_lock:
            existing = getattr(self, "_settings_save_timer", None)
            if existing is not None:
                existing.cancel()
            t = threading.Timer(2.0, self.save_settings)
            t.daemon = True
            self._settings_save_timer = t
            t.start()

    def refresh_sound_packs(self):
        """Re-scan client/sounds/*/ for soundpacks (a *.pack.json manifest
        folder each). Call after importing a new pack so it's immediately
        selectable without restarting the app.
        """
        self._sound_packs = discover_sound_packs(self.sound_system.sound_dir)
        self._default_sound_pack = self._sound_packs.get(DEFAULT_PACK_ID)

    def get_active_sound_pack(self) -> "dict | None":
        """The soundpack currently selected in Settings (falls back to the
        default pack if the stored choice no longer exists — e.g. its folder
        was deleted outside the app)."""
        pack_id = self.settings.get("active_sound_pack", DEFAULT_PACK_ID)
        return self._sound_packs.get(pack_id) or self._default_sound_pack

    def load_sounds(self):
        """Load every per-event UI sound from the active soundpack (Settings >
        Sound Events), falling back to the default pack — and then to
        enabled=True/no override — for anything the user hasn't customized.
        message_background is one of these events too (it's what the Alert
        Tones "Padrão" choice and the per-conversation "Padrão" override
        ultimately resolve to — see _resolve_message_background_path()).

        Safe to call again after Settings > Sound Events changes to pick up
        a new active pack / enabled / per-event override without restarting.
        """
        active_pack = self.get_active_sound_pack()
        default_pack = self._default_sound_pack
        pack_id = active_pack.get("id") if active_pack else DEFAULT_PACK_ID
        events_cfg = self.settings.get("sound_events", {}).get(pack_id, {})
        for key, _default_filename in SOUND_EVENTS:
            cfg = events_cfg.get(key) or {}
            override_path = cfg.get("path", "")
            resolved = resolve_sound_event_path(active_pack, default_pack, key, override_path)
            if resolved:
                setattr(self, f"{key}_sound", load_sound(self.sound_system, resolved, event_key=key, pack_id=pack_id))
            else:
                # Nothing resolves at all (broken install: even the default
                # pack is missing this file) — a silent no-op beats a crash.
                from core.sound_system import NullSound
                setattr(self, f"{key}_sound", NullSound())

    def _apply_configured_audio_devices(self):
        """Finish applying the Settings > Audio Devices output/input device
        choices, and warn if either failed.

        The output device itself was already switched to right after the
        sound system started (before load_sounds(), see __init__) — this
        call is deliberately a no-op if that already succeeded (Output.
        set_device() only does real work when the target device differs
        from the current one) and exists here purely to surface the failure
        message box, which needs i18n (not ready yet at that earlier point).
        A device that isn't found or fails to open falls back to the
        Windows default and warns — the stored setting is left as-is so the
        same device is retried on the next launch (see
        core.sound_system.SoundSystem and the Settings dialog's own
        validation for the other two points this same policy applies at).
        """
        audio_devices = self.settings.get("audio_devices", {})

        output_name = audio_devices.get("output_device_name", "")
        self.sound_system.apply_output_device(output_name, warn_on_failure=True)

        input_name = audio_devices.get("input_device_name", "")
        self.effective_input_device_name = ""
        if input_name:
            idx = find_input_device_index(input_name)
            if idx is not None and test_input_device(idx):
                self.effective_input_device_name = input_name
            elif not self.background_mode:
                wx.MessageBox(
                    self.i18n.t("audio_device_failed_input").format(device=input_name),
                    self.i18n.t("error").format(app_name=self.app_name),
                    wx.OK | wx.ICON_WARNING,
                )

    def _resolve_message_background_path(self) -> str:
        """Resolve the message_background Sound Events entry: '' if the user
        disabled it, else its custom path override or the active/default
        pack's own file (same fallback chain as any other Sound Events entry).
        """
        active_pack = self.get_active_sound_pack()
        default_pack = self._default_sound_pack
        pack_id = active_pack.get("id") if active_pack else DEFAULT_PACK_ID
        cfg = self.settings.get("sound_events", {}).get(pack_id, {}).get("message_background", {})
        if not cfg.get("enabled", True):
            return ""
        return resolve_sound_event_path(active_pack, default_pack, "message_background", cfg.get("path", ""))

    def _resolve_background_sound_path(self, remote_jid: str) -> str:
        """Pick the .ogg file for a background/toast notification for `remote_jid`.

        Priority: per-conversation override (Settings > conversation data
        dialog) > the private/group default from Settings > Alert Tones >
        the message_background Sound Events entry (active pack, falling back
        to the default pack). Falls through to the next tier whenever a
        chosen path doesn't resolve to an existing file, so a removed/typo'd
        custom path or an active pack missing that file never silently kills
        notification sound.
        """
        active_pack = self.get_active_sound_pack()
        default_pack = self._default_sound_pack

        conv_cfg = self.settings.get("conversation_sounds", {}).get(remote_jid) or {}
        choice = conv_cfg.get("choice", "default")
        if choice and choice != "default":
            path = resolve_alert_tone_path(active_pack, default_pack, choice, conv_cfg.get("custom_path", ""))
            if path and os.path.isfile(path):
                return path

        is_group = remote_jid.endswith("@g.us")
        tones = self.settings.get("alert_tones", {})
        type_key = "group" if is_group else "private"
        type_choice = tones.get(type_key, "default")
        type_custom = tones.get(f"{type_key}_custom_path", "")
        if type_choice and type_choice != "default":
            path = resolve_alert_tone_path(active_pack, default_pack, type_choice, type_custom)
            if path and os.path.isfile(path):
                return path

        return self._resolve_message_background_path()

    def play_background_notification_sound(self, remote_jid: str):
        """Play the resolved background/toast notification sound for `remote_jid`."""
        path = self._resolve_background_sound_path(remote_jid)
        if not path:
            return
        cache = self._notification_sound_cache
        snd = cache.get(path)
        if snd is None:
            try:
                snd = Sound(self.sound_system, path)
            except Exception:
                snd = self.message_background_sound
            cache[path] = snd
        snd.play()

    def play_startup_sound(self):
        """Play startup sound exactly once per application run."""
        if getattr(self, "_startup_sound_played", False):
            return
        self._startup_sound_played = True
        try:
            if hasattr(self, "startup_sound") and self.startup_sound:
                logging.info("[sound] Playing startup sound")
                self.startup_sound.play()
        except Exception as e:
            logging.warning("[sound] Error playing startup sound: %s", e)

    def _token_key(self) -> bytes:
        """Return the per-install Fernet key (data_path()/secret.key) that
        backs token_vault.py, loading it lazily if needed.

        retrieve_token() (and therefore _get_wa_token()/_set_wa_token()) runs
        early in __init__, before prepare_sync() normally sets self.key —
        retrieve_secret_key() is idempotent (creates the file on first call,
        otherwise just reads it), so calling it here too is harmless; it just
        means whichever of the two call sites runs first is the one that
        actually creates the key file.
        """
        if not getattr(self, "key", None):
            self.key = self.retrieve_secret_key()
        return self.key

    def _get_wa_token(self) -> str:
        """Read the WPPConnect session token, transparently migrating a
        legacy plaintext copy (settings["privateinfo"]["WA_token"]) to
        Fernet-protected storage (settings["privateinfo"]["WA_token_protected"],
        see core/token_vault.py) the first time it's read.

        A value that fails to decrypt (corrupted, or encrypted under a
        different secret.key — e.g. settings.json copied without it) is
        treated exactly like "no token saved": retrieve_token() already
        handles that by showing the pairing dialog again, never a crash.
        """
        pi = self.settings.get("privateinfo", {})
        protected = pi.get("WA_token_protected", "")
        if protected:
            token = token_vault.unprotect_token(protected, self._token_key())
            if token:
                return token
            # Falls through to the legacy field below only so a token that
            # somehow still has a plaintext copy alongside a now-unreadable
            # protected one isn't lost — normally these are mutually exclusive.
        legacy = pi.get("WA_token", "").strip()
        if legacy:
            # One-time migration: re-save protected, remove the plaintext copy.
            self._set_wa_token(legacy)
        return legacy

    def _set_wa_token(self, token: str):
        """Write the WPPConnect session token, Fernet-protected with the
        per-install secret.key (see core/token_vault.py). Falls back to
        plaintext only if encryption genuinely fails for some reason — still
        functional, just not the hardened path. token="" clears both the
        protected and legacy fields.
        """
        pi = self.settings.setdefault("privateinfo", {})
        if not token:
            pi.pop("WA_token_protected", None)
            pi["WA_token"] = ""
            self.save_settings()
            return
        try:
            pi["WA_token_protected"] = token_vault.protect_token(token, self._token_key())
            pi.pop("WA_token", None)  # never leave a plaintext copy lying around
            self.save_settings()
        except Exception as e:
            logging.warning("[_set_wa_token] Token protection failed, falling back to plaintext: %s", e)
            pi["WA_token"] = token
            self.save_settings()

    def retrieve_token(self):
        token = self._get_wa_token()
        if not token:
            # Migration: read from legacy token.tk if WA_token not yet present
            try:
                with open(data_path("token.tk"), "r") as f:
                    token = f.read().strip()
                if token:
                    self._set_wa_token(token)
            except Exception:
                pass
        if token and ":" not in token:
            try:
                url = f"{self.wpp_server}:{self.wpp_port}/api/{token}/{self.wpp_api_key}/generate-token"
                import requests
                response = requests.post(url, timeout=10)
                if response.status_code in (200, 201):
                    data = response.json()
                    hash_token = data.get("token")
                    if hash_token:
                        hash_token = hash_token.replace("/", "_").replace("+", "-")
                        token = f"{token}:{hash_token}"
                        self._set_wa_token(token)
            except Exception as e:
                logging.error("[retrieve_token] Failed to migrate WPPConnect token: %s", e)
        if not token:
            if self.background_mode:
                # No token means WhatsApp has never been paired — exit silently.
                sys.exit(0)
            self.error_sound.play()
            wx.MessageBox(f"{self.i18n.t('token_retrieval_failed')} {format_exc()}", self.i18n.t("error").format(app_name=self.app_name), wx.OK | wx.ICON_ERROR)
            sys.exit()
        self.token = token.replace("/", "_").replace("+", "-")

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

        if settings_dirty:
            self.save_settings()

        logging.info("[prepare_sync] metadata loaded — loading local chats (bulk DB call, up to 120s)")
        #Get Local Chats
        self.chats = self.get_chats()
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

    def start_connection_health_checker(self):
        """Periodically verify session health and auto-restart Puppeteer if closed."""
        def _loop():
            # Wait a bit after startup before starting checks
            time.sleep(30)
            while True:
                try:
                    # Only a *user-requested* offline pauses the checker.  When
                    # offline mode was entered automatically (connection lost)
                    # this loop is precisely what notices the connection coming
                    # back, so skipping it there would make the state permanent.
                    # A WPPConnect reinstall in progress also pauses it — the
                    # server is deliberately down for a few seconds there, and
                    # that is not a real connection loss (see _wpp_updating).
                    if not getattr(self, "_user_offline", False) and not getattr(self, "_wpp_updating", False):
                        self.check_wa_connection_http()
                        # Safety net for a sync that failed or never started
                        # while the connection was down: retry it as soon as
                        # WhatsApp is reachable again, for as long as it takes.
                        self.trigger_sync_if_needed()
                except Exception as e:
                    logging.warning(f"[health_checker] Error checking connection in background: {e}")
                time.sleep(30)

        threading.Thread(target=_loop, daemon=True).start()

    # A single failed network probe is not proof of an outage (a slow proxy, a
    # captive portal check, a momentary DNS hiccup); two in a row is.  Keeping
    # this at 2 means an outage is detected within one health-check cycle
    # (~30-60 s) while a blip never drops the app into offline mode.
    _OFFLINE_PROBE_STRIKES = 2

    # Same idea, but for the local WPPConnect API request itself (the
    # `except` branch of check_wa_connection_http() below) rather than the
    # WhatsApp-reachability probe. The local Node/Puppeteer process can be
    # briefly unresponsive to its own HTTP server under load (GC pause, a
    # heavy history-sync page load, disk I/O) without WhatsApp itself having
    # dropped at all — that used to flip straight to offline on the very
    # first missed 10s-timeout request, then back online on the next
    # successful poll 30s later, over and over. Reported live as the app
    # repeatedly flickering between online/offline for no apparent reason.
    # Requiring consecutive failures before believing it is a real outage
    # fixes the flapping and, as a side effect, gives a slower/busier machine
    # more time before offline mode kicks in at all.
    _HTTP_PROBE_STRIKES = 2

    # Consecutive notLogged/QRCODE readings required before believing the device
    # was really unlinked.  The health checker polls every ~30 s.
    _LOGOUT_CONFIRM_STRIKES = 4

    # The unlinked state must ALSO have been continuously true for at least this
    # long. Strike count alone is not a time guarantee — two callers can observe
    # the same poll cycle, and the poll interval itself is not fixed.
    _LOGOUT_CONFIRM_SECONDS = 180

    # ...and WPPConnect must have been up for at least this long. Restoring a
    # saved session means booting Node, launching Chrome, loading web.whatsapp.com
    # and replaying IndexedDB; WhatsApp Web renders its QR canvas during part of
    # that, so WPPConnect legitimately reports QRCODE/notLogged for a while on a
    # cold start — longest right after a machine reboot, which is exactly when
    # users reported being told they had been disconnected.
    _LOGOUT_STARTUP_GRACE_SECONDS = 240

    def _still_linked_on_server(self) -> bool:
        """Positive proof the device is still linked, or False if unprovable.

        Before wiping anything, ask WPPConnect for the host device: a session
        that WhatsApp genuinely unlinked cannot answer with our own phone
        number. This is the check that turns "we saw a status string we don't
        like" into "WhatsApp really did drop us" — status-session alone is a
        cached string produced by a browser that may simply still be restoring.
        """
        try:
            resp = requests.get(
                f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/host-device",
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=10,
            )
        except Exception as exc:
            logging.info("[_still_linked_on_server] host-device probe failed: %s", exc)
            return False
        if resp.status_code not in (200, 201):
            return False
        try:
            phone = (resp.json().get("response") or {}).get("phoneNumber")
        except Exception:
            return False
        if isinstance(phone, dict):
            phone = phone.get("_serialized", "")
        return bool(phone)

    def _logout_confirmed(self, status: str) -> bool:
        """True when an unlinked status has been seen often enough to act on it.

        Acting means _on_disconnect(), which drops the token AND calls
        clear_local_data() — the whole local database, irreversibly, forcing a
        full re-pairing. So this deliberately demands a lot before saying yes:

        1. ``_LOGOUT_STARTUP_GRACE_SECONDS`` must have passed since WPPConnect
           started. A cold start (worst right after a Windows reboot) spends
           minutes booting Chrome and replaying the saved session, and WhatsApp
           Web shows its QR canvas partway through — WPPConnect reports that as
           QRCODE even though nothing is wrong.
        2. ``_LOGOUT_CONFIRM_STRIKES`` consecutive unlinked readings, the tally
           being reset by any other status in check_wa_connection_http().
        3. The unlinked state must have held for ``_LOGOUT_CONFIRM_SECONDS``.
        4. ``_still_linked_on_server()`` must NOT be able to prove the device is
           still linked — a real unlink cannot return our own phone number.

        This is the "minha conta foi desconectada, mas o celular ainda mostra a
        sessão aberta" report: a session that was never unlinked at all, wiped
        on the strength of two transient status strings.

        Only consulted on the *destructive* path, i.e. when the account was
        actually paired and there is local history to lose. An account that was
        never paired has an empty database and needs the pairing dialog now, not
        a poll cycle later, so that path is never gated on this.
        """
        if status not in ("notLogged", "QRCODE"):
            self._logout_strikes = 0
            self._logout_first_seen = None
            return False

        now = time.time()
        started_at = getattr(self, "_wpp_started_at", None)
        if started_at is not None and (now - started_at) < self._LOGOUT_STARTUP_GRACE_SECONDS:
            logging.warning(
                "[check_wa_connection_http] Saw unlinked state '%s' only %.0fs after "
                "WPPConnect started — still within the %ds startup grace, ignoring.",
                status, now - started_at, self._LOGOUT_STARTUP_GRACE_SECONDS,
            )
            return False

        if getattr(self, "_logout_first_seen", None) is None:
            self._logout_first_seen = now
        self._logout_strikes = getattr(self, "_logout_strikes", 0) + 1
        held_for = now - self._logout_first_seen

        if (self._logout_strikes < self._LOGOUT_CONFIRM_STRIKES
                or held_for < self._LOGOUT_CONFIRM_SECONDS):
            logging.warning(
                "[check_wa_connection_http] Saw unlinked state '%s' (strike %d/%d, held "
                "%.0fs/%ds) — waiting for confirmation before wiping local data.",
                status, self._logout_strikes, self._LOGOUT_CONFIRM_STRIKES,
                held_for, self._LOGOUT_CONFIRM_SECONDS,
            )
            return False

        if self._still_linked_on_server():
            logging.warning(
                "[check_wa_connection_http] status-session says '%s' but host-device "
                "still reports a linked phone — NOT a logout. Resetting the tally.",
                status,
            )
            self._logout_strikes = 0
            self._logout_first_seen = None
            return False

        if getattr(self, "_logout_handled", False):
            return False
        self._logout_handled = True
        logging.warning(
            "[check_wa_connection_http] Confirmed unlinked/logged out state: %s after "
            "%d consecutive readings over %.0fs. Triggering disconnect.",
            status, self._logout_strikes, held_for,
        )
        return True

    def _probe_whatsapp_host(self) -> bool:
        """True if WhatsApp's own servers answer over the network.

        Any HTTP answer counts as reachable — we only care about whether the
        machine can talk to WhatsApp at all, not what it replies.  Only a
        transport-level failure (no DNS, no route, timeout) means offline.
        No third-party host is contacted: this is the same service the app
        already talks to through the browser session.
        """
        try:
            # Reuses the module-level pooled session (requests.head is not one
            # of the patched, pooled helpers).
            _http_session.head("https://web.whatsapp.com", timeout=6,
                               allow_redirects=False)
            return True
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logging.info("[_probe_whatsapp_host] network unreachable: %s", e)
            return False
        except Exception:
            # Anything else (odd TLS/proxy behaviour) still proves we reached
            # something — do not call that an outage.
            return True

    def _nudge_whatsapp_socket_stream(self) -> bool:
        """Ask WPPConnect to fire WPP.whatsapp.Cmd.openSocketStream() inside
        the page — the same internal trigger a real, focused browser tab
        fires on its own via visibility/focus/online DOM events after the OS
        resumes from sleep.

        This session's Chrome runs headless and is never focused, so nothing
        ever fires that trigger by itself. That alone was the first symptom
        reported: stuck offline forever after a suspend/resume cycle, even
        though WPPConnect's own cached session status keeps saying CONNECTED.

        Returns False when the request itself fails (network) or the server
        reports a non-2xx status — which turned out to mean something worse
        than "no trigger fired": WPPConnect's log showed
        ``Attempted to use detached Frame '<id>'`` from Puppeteer every time
        this endpoint was hit after a suspend/resume cycle. The Chrome tab's
        page/frame had structurally died (crashed or been replaced) during
        sleep, and Puppeteer's cached Page/Frame handle for it is now
        permanently unusable — no in-page command can ever succeed again on
        it, by definition. See _restart_wpp_session(), which the caller
        escalates to after enough consecutive failures here.
        """
        try:
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/reconnect-socket-stream"
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                logging.warning(
                    "[_nudge_whatsapp_socket_stream] HTTP %s: %s",
                    resp.status_code, resp.text[:300],
                )
                return False
            return True
        except Exception as exc:
            logging.warning("[_nudge_whatsapp_socket_stream] request failed: %s", exc)
            return False

    # How many *consecutive* failed nudges (see _nudge_whatsapp_socket_stream)
    # before assuming the browser page itself is structurally dead — rather
    # than a transient blip — and restarting the WPPConnect session.
    _DEAD_BROWSER_RESTART_STRIKES = 3
    # Minimum time between automatic session restarts, so a persistently
    # broken state can't retrigger this every health-check cycle forever.
    _WPP_SESSION_RESTART_COOLDOWN = 120

    # How long after an automatic _restart_wpp_session() the "confirmed
    # logout" path (below, in check_wa_connection_http) must stay suppressed.
    # See _restart_wpp_session()'s docstring for why this exists at all.
    _AUTO_RESTART_LOGOUT_GRACE_SECONDS = 600

    def _auto_restart_grace_active(self) -> bool:
        """True while an automatic session restart is still settling — see
        _AUTO_RESTART_LOGOUT_GRACE_SECONDS."""
        ts = getattr(self, "_auto_session_restart_ts", 0)
        return bool(ts) and (time.time() - ts) < self._AUTO_RESTART_LOGOUT_GRACE_SECONDS

    def _restart_wpp_session(self):
        """Recreate the WPPConnect Chrome session in place (close-session +
        start-session), without touching the Node process or WinZapp itself.

        Used automatically when the Puppeteer page has structurally died
        (detached frame after a suspend/resume cycle — see
        _nudge_whatsapp_socket_stream()); a dead page can never satisfy
        isConnected() again on its own, no matter how many times the health
        check retries it. start-session on a session with a stored, valid
        token silently restores the existing WhatsApp session (no new QR
        code) — exactly what already happens on every normal app restart,
        just without restarting the whole app or Node process.

        Reported live the first time this was wired up unguarded: the
        stored token had already gone bad for an unrelated reason (possibly
        the very same crash that killed the page), so start-session's
        create() came back needing a fresh QR scan instead — with nobody
        there to scan it. The pre-existing (and, on its own, entirely
        correct) "confirmed logout" detection further down
        check_wa_connection_http() then saw that QRCODE state hold for its
        normal confirm-strikes threshold and treated it exactly like a real
        phone-side unlink, calling _on_disconnect() — which wipes the
        *entire* local database via clear_local_data(). _auto_session_restart_ts
        (set below, checked via _auto_restart_grace_active()) is what now
        keeps that destructive path from firing as a side effect of this
        one: for _AUTO_RESTART_LOGOUT_GRACE_SECONDS after a restart, a
        QRCODE/notLogged reading is treated as "still settling", not "phone
        confirmed unlinked". A genuine phone-side unlink happening to
        coincide with that window is only delayed, not missed — trading a
        possible few extra minutes before a real unlink is detected for
        never again wiping local data over an artifact of our own restart.
        """
        # Sets the grace window immediately, synchronously, before the
        # cooldown/re-entrancy checks below can bail out early — a health
        # check landing on another thread between "decided to restart" and
        # this function's body actually running must not see the old,
        # expired window and treat a QRCODE reading as confirmable.
        self._auto_session_restart_ts = time.time()
        if getattr(self, "_restarting_wpp_session", False):
            return
        now = time.time()
        last = getattr(self, "_last_wpp_session_restart_ts", 0)
        if now - last < self._WPP_SESSION_RESTART_COOLDOWN:
            return
        self._restarting_wpp_session = True
        self._last_wpp_session_restart_ts = now
        try:
            logging.warning(
                "[_restart_wpp_session] Browser page appears dead (detached "
                "frame) after suspend/resume — restarting the WPPConnect "
                "session in place."
            )
            headers = {"Authorization": f"Bearer {self.token}"}
            close_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/close-session"
            try:
                requests.post(close_url, headers=headers, timeout=15)
            except Exception as exc:
                logging.warning("[_restart_wpp_session] close-session failed: %s", exc)
            time.sleep(2)
            start_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/start-session"
            try:
                requests.post(start_url, json={"waitQrCode": False}, headers=headers, timeout=15)
                logging.info("[_restart_wpp_session] start-session requested.")
            except Exception as exc:
                logging.warning("[_restart_wpp_session] start-session failed: %s", exc)
        finally:
            self._restarting_wpp_session = False

    def check_whatsapp_reachable(self) -> bool:
        """Decide whether WhatsApp traffic can actually flow right now.

        ``/status-session`` cannot answer this: it just echoes the session
        status string WPPConnect cached when the session was created, so it
        keeps saying CONNECTED with the network cable unplugged — which is why
        the app used to announce itself connected, start a sync and fire the
        send queue with no internet at all.

        Two sources are combined:

        * ``/check-connection-session``, which reports the session as
          Disconnected when WhatsApp Web itself has gone down inside the
          browser.  (It only reports a failure when the underlying call
          *throws*, so a False here is meaningful but a True is not conclusive.)
        * a direct reachability probe against WhatsApp's servers, which is what
          catches the plain "this machine has no internet" case.

        Both negatives require _OFFLINE_PROBE_STRIKES *consecutive* readings
        before they count — see the session-probe branch below for why the
        first one is not proof of anything.
        """
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/check-connection-session"
        session_down = False
        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=10,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                if not data.get("status"):
                    session_down = True
            elif resp.status_code == 404:
                session_down = True
        except Exception as e:
            logging.warning("[check_whatsapp_reachable] session probe failed: %s", e)

        if session_down:
            # This used to skip the strike tally entirely — it set
            # _offline_probe_strikes to the limit and returned False on the
            # spot, so one negative reading was enough to declare an outage.
            # But the commonest cause of a negative here is not an outage at
            # all: WhatsApp Web reloads its own page from time to time, and
            # while it does, WPPConnect's isConnected() evaluates
            # WAPI.isConnected() against a page where the injected WAPI
            # namespace no longer exists — it throws ReferenceError, and the
            # endpoint duly answers "not connected".
            #
            # Observed live: a 28-second reload window (wppconnect.log:
            # "Execution context was destroyed, most likely because of a
            # navigation", then ~700 "WAPI is not defined", then "State
            # Change CONNECTED") announced a full "modo offline" with sound
            # and speech, aborted LID resolution, marked a complete 574-chat
            # sync as incomplete and skipped the whole media phase — with the
            # machine's internet perfectly fine throughout, and with the page
            # already healthy again 4 seconds later.
            #
            # Requiring the same consecutive strikes the host probe already
            # requires rides out a reload; a genuine outage still registers
            # on the next health-check tick ~30 s later.
            self._offline_probe_strikes = getattr(self, "_offline_probe_strikes", 0) + 1
            if self._offline_probe_strikes >= self._OFFLINE_PROBE_STRIKES:
                return False
            logging.info(
                "[check_whatsapp_reachable] session probe reports disconnected "
                "(strike %d/%d) — holding the current state for one more cycle.",
                self._offline_probe_strikes, self._OFFLINE_PROBE_STRIKES,
            )
            return bool(getattr(self, "_wa_connected", False))

        if self._probe_whatsapp_host():
            self._offline_probe_strikes = 0
            return True
        self._offline_probe_strikes = getattr(self, "_offline_probe_strikes", 0) + 1
        if self._offline_probe_strikes >= self._OFFLINE_PROBE_STRIKES:
            return False
        # First strike: give it one more cycle before going offline.
        return bool(getattr(self, "_wa_connected", False))

    def _is_pairing_dialog_active(self) -> bool:
        """True while the connection/pairing dialog is on screen — i.e. the
        user has never actually paired yet (or is re-pairing) and is looking
        straight at it."""
        dial = getattr(self.connect, "connection_dial", None)
        return bool(dial) and dial.IsShown()

    def check_wa_connection_http(self):
        """Query the WPPConnect API via HTTP to check if the instance is already connected to WhatsApp."""
        if self._is_pairing_dialog_active():
            # Nothing below is meaningful yet: WPPConnect reporting
            # CLOSED/QRCODE/notLogged while the user is actively looking at
            # the pairing dialog is completely normal — that IS what
            # "not paired yet" looks like, not an outage. Every branch
            # below unconditionally called _set_wa_connected(False, ...)
            # (only the *auto-start-session* side effect was gated on the
            # dialog being open), so this ran every 30s during pairing and
            # announced "sem conexão com o WhatsApp. Modo offline ativado
            # automaticamente" — sound and speech — while the user hadn't
            # even finished scanning the QR code, let alone ever connected.
            # This whole HTTP poll exists to detect/recover from outages
            # *after* pairing; pairing's own completion is already driven by
            # WebSocket events (on_connection_update/session-logged), so
            # skipping it entirely here loses nothing.
            return
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/status-session"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        try:
            response = requests.get(url, headers=headers, timeout=10)
            # The local API answered at all — whatever it says below, the
            # request-level failure streak that would otherwise declare an
            # outage is not applicable here.
            self._wa_http_fail_strikes = 0
            if response.status_code in (401, 403):
                logging.warning("[check_wa_connection_http] Token is unauthorized (HTTP %s). Triggering logout.", response.status_code)
                def _logout_with_warning_401():
                    self.error_sound.play()
                    wx.MessageBox(
                        self.i18n.t("device_logged_out"),
                        self.i18n.t("error").format(app_name=self.app_name),
                        wx.OK | wx.ICON_ERROR,
                    )
                    self._on_disconnect()
                wx.CallAfter(_logout_with_warning_401)
                return

            if response.status_code in (200, 201):
                data = response.json()
                # WPPConnect /status-session returns {"status": "CONNECTED"} — the key is
                # "status", not "state".  Reading "state" always yields "" which incorrectly
                # triggers /start-session even when a session is already alive.
                status = (
                    data.get("status")
                    or data.get("state")
                    or data.get("response", {}).get("status")
                    or data.get("response", {}).get("state")
                    or ""
                )

                logging.info("[check_wa_connection_http] Instance status: %s", status)

                # Any status other than the two unlinked ones clears the logout
                # tally, so only *consecutive* readings can ever confirm one —
                # see _LOGOUT_CONFIRM_STRIKES.
                if status not in ("notLogged", "QRCODE"):
                    self._logout_strikes = 0
                    self._logout_first_seen = None

                # Robust check: Only call start-session if the instance is explicitly CLOSED, DESTROYED, or completely inactive.
                # WPPConnect status values include: CONNECTED, open, INITIALIZING, QRCODE, PHONECODE, notLogged, inChat, PAIRED, etc.
                if status in ("CONNECTED", "open"):
                    # "CONNECTED" only means the WPPConnect session object is
                    # alive — it is a cached string that stays put when the
                    # machine loses internet.  Confirm against the live
                    # isConnected() probe before declaring ourselves online,
                    # otherwise the app plays the "connected" sound, starts a
                    # sync and lets the send queue fire with no connectivity.
                    if not self.check_whatsapp_reachable():
                        # See _nudge_whatsapp_socket_stream(): a headless,
                        # never-focused page has no natural trigger left to
                        # reopen WhatsApp Web's own socket after a
                        # suspend/resume cycle — without this, this branch
                        # (and therefore offline mode) can persist forever,
                        # since nothing else ever pokes the page to retry.
                        if self._nudge_whatsapp_socket_stream():
                            self._dead_browser_strikes = 0
                        else:
                            # The nudge request itself failed — not just "no
                            # trigger fired", but the page/frame Puppeteer
                            # holds is gone. Escalate to a full session
                            # restart after enough consecutive failures — see
                            # _restart_wpp_session() and
                            # _auto_restart_grace_active() (checked in the
                            # confirmed-logout branch further down this same
                            # function) for why this is safe to do
                            # automatically now.
                            self._dead_browser_strikes = getattr(self, "_dead_browser_strikes", 0) + 1
                            if self._dead_browser_strikes >= self._DEAD_BROWSER_RESTART_STRIKES:
                                self._dead_browser_strikes = 0
                                threading.Thread(target=self._restart_wpp_session, daemon=True).start()
                        self._set_wa_connected(False, "status-session CONNECTED but isConnected() false")
                        return
                    self._dead_browser_strikes = 0
                    self._set_wa_connected(True, "status-session CONNECTED")
                    try:
                        dev_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/host-device"
                        dev_resp = requests.get(dev_url, headers=headers, timeout=5)
                        if dev_resp.status_code in (200, 201):
                            dev_data = dev_resp.json()
                            phoneNumberObj = dev_data.get("response", {}).get("phoneNumber", {})
                            wuid = ""
                            if isinstance(phoneNumberObj, dict):
                                wuid = phoneNumberObj.get("_serialized", "")
                            elif isinstance(phoneNumberObj, str):
                                wuid = phoneNumberObj
                            if wuid:
                                self.my_jid = wuid
                                if hasattr(self, "db") and self.db is not None:
                                    self.db.set_metadata("my_jid", wuid)
                                self.resolve_self_lid()
                                # Mark as paired on successful HTTP host check too
                                pi = self.settings.setdefault("privateinfo", {})
                                if not pi.get("paired"):
                                    pi["paired"] = True
                                    self.save_settings()
                    except Exception as e:
                        logging.error("[check_wa_connection_http] Failed to fetch host device JID: %s", e)
                elif status in ("CLOSED", "DESTROYED", ""):
                    self._set_wa_connected(False, f"status-session {status or 'unknown'}")
                    # Status is CLOSED or unknown: safe to start a new session.
                    # But skip if the connection dialog is currently open (pairing in progress)
                    # to avoid spawning a duplicate Chrome alongside the one the pairing flow manages.
                    # (Unreachable in practice now that this whole method returns early
                    # while the dialog is active — kept as a defensive fallback.)
                    if self._is_pairing_dialog_active():
                        logging.info("[check_wa_connection_http] Skipping auto-start — pairing dialog is active.")
                    else:
                        try:
                            start_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/start-session"
                            requests.post(start_url, json={"waitQrCode": False}, headers=headers, timeout=10)
                            logging.info("[check_wa_connection_http] Sent auto-start session command")
                        except Exception as e:
                            logging.error("[check_wa_connection_http] Failed to auto-start session: %s", e)
                else:
                    # Instance is in some active state (e.g. notLogged, inChat, QRCODE, INITIALIZING, etc.)
                    # We should NOT call start-session to avoid launching duplicate Puppeteer tabs.
                    # None of these states can carry WhatsApp traffic, so the
                    # app must consider itself offline while they last.
                    # notLogged/QRCODE are a definite, explained signal (the
                    # dialog below tells the user pairing is needed) — skip the
                    # startup grace for those. Anything else here (inChat,
                    # INITIALIZING, PAIRED, ...) is exactly the kind of normal
                    # mid-boot status that must NOT flip the UI to "offline".
                    self._set_wa_connected(
                        False, f"status-session {status}",
                        confirmed=status in ("notLogged", "QRCODE"),
                    )
                    logging.info(
                        "[check_wa_connection_http] Session is in active state '%s' — skipping /start-session to avoid browser conflict.",
                        status,
                    )
                    if status in ("notLogged", "QRCODE"):
                        self._wa_connected = False

                        def _logout_with_warning():
                            self.error_sound.play()
                            wx.MessageBox(
                                self.i18n.t("device_logged_out"),
                                self.i18n.t("error").format(app_name=self.app_name),
                                wx.OK | wx.ICON_ERROR,
                            )
                            self._on_disconnect()

                        if self.settings.get("privateinfo", {}).get("paired"):
                            # Destructive path: _on_disconnect() wipes the whole
                            # local database, so an unlinked reading has to be
                            # confirmed first — see _logout_confirmed().
                            #
                            # An automatic _restart_wpp_session() (dead-browser
                            # recovery) legitimately re-shows a fresh QR itself
                            # whenever the stored token turns out to already be
                            # bad — that is NOT a phone-side unlink, and must
                            # never be confirmed as one. See
                            # _auto_restart_grace_active()/_restart_wpp_session()'s
                            # docstring for the incident this guards against
                            # (a real one: it wiped a user's local database).
                            if self._auto_restart_grace_active():
                                logging.info(
                                    "[check_wa_connection_http] %s seen while an "
                                    "automatic session restart is still settling — "
                                    "not confirming a logout yet.", status,
                                )
                                self._logout_strikes = 0
                                self._logout_first_seen = None
                                return
                            if not self._logout_confirmed(status):
                                return
                            wx.CallAfter(_logout_with_warning)
                        else:
                            # Not paired: there is nothing to lose (the database
                            # is empty by definition) and _on_disconnect() is
                            # what puts the pairing dialog on screen. Delaying it
                            # behind the confirmation left the app sitting on
                            # "sem conexão com o WhatsApp / modo offline" with no
                            # way to connect — the guard was protecting data that
                            # does not exist, at the cost of the one action the
                            # user actually needed.
                            wx.CallAfter(self._on_disconnect)
        except Exception as e:
            # The local API itself did not answer — we certainly cannot reach
            # WhatsApp through it either, but only once this has happened
            # _HTTP_PROBE_STRIKES times in a row (see that constant): a lone
            # failed request is far more often a briefly-busy local Node
            # process than a real outage.
            self._wa_http_fail_strikes = getattr(self, "_wa_http_fail_strikes", 0) + 1
            logging.warning(
                "[check_wa_connection_http] Request failed (strike %d/%d): %s",
                self._wa_http_fail_strikes, self._HTTP_PROBE_STRIKES, e,
            )
            if self._wa_http_fail_strikes < self._HTTP_PROBE_STRIKES:
                return
            self._set_wa_connected(False, f"status-session request failed: {e}")
            logging.error("[check_wa_connection_http] Error checking connection state: %s", e)

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
        self._sync_run_id = getattr(self, "_sync_run_id", 0) + 1
        # Latch before _run_sync(), not after: it can bail out early (no
        # WhatsApp connection yet), and in exactly that case there is no sync
        # coming to re-fetch anything, so live events are the only source of
        # new data there is — dropping them would be strictly worse.
        self._sync_ever_started = True
        self._last_sync_attempt_ts = time.time()
        try:
            self._run_sync()
        except Exception:
            logging.exception("[start_sync] Unhandled error during sync")
        finally:
            # Always clear the tray/status text and the running flag, even if
            # something above raised — otherwise an error mid-sync leaves the
            # tray stuck on "preparing to sync" / "synchronizing" forever,
            # since none of those status calls are inside a try/finally and a
            # thread that dies mid-sync never reaches its own clear-status line.
            self._initial_sync_running = False
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

    def _run_sync(self):
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

        # Bundle the title/tray text, sound and speech for this stage into a
        # single wx.CallAfter so they can never visibly fall out of step.
        # Previously the sound and speech ran immediately on this background
        # thread while _set_status() was merely queued via its own separate
        # wx.CallAfter — the user heard "sincronizando" and the sound played
        # right away, but the title kept showing whatever the previous stage
        # was (e.g. "preparando-se para sincronizar") until wx's event loop
        # got around to draining that queued call, which could visibly lag
        # by a few seconds. Doing all three in one callback guarantees they
        # land in the same UI-thread tick.
        def _announce_synchronizing():
            self._set_status(self.i18n.t("synchronizing"))
            self.synchronizing_sound.play()
            if not self.background_mode:
                self.output(self.i18n.t("synchronization_started"), interrupt=True)
        wx.CallAfter(_announce_synchronizing)

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
        has_local_chats = len(self.chats) > 0
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
        while True:
            attempt += 1
            if attempt >= max_attempts:
                break
            if self._should_abort_sync_for_offline():
                logging.info("[start_sync] Aborting sync: offline mode activated mid-sync.")
                self._sync_completed = False
                return
            result   = self.get_remote_chats(dict(self.chats), notify_errors=False)
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
            logging.info(
                "[start_sync] list-chats attempt %d returned %d chats (previous: %d, local cache: %d)",
                attempt + 1, server_count, prev_server_count, local_chat_count,
            )
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
                # Still growing when we ran out of attempts: use what we have,
                # but do NOT call this sync complete — it gets retried below.
                logging.warning(
                    "[start_sync] Chat list never settled (last count: %d) — "
                    "treating this sync as incomplete.", server_count,
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
        self.chats = self.normalize_chats(self.chats)

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
        wx.CallAfter(self.set_chats)

        # ── Phase 1: sync all messages ────────────────────────────────────
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
        self.unblock_history_sync()
        self.refresh_history_still_landing(context="before message sync")
        _sync_phase1_started = time.time()
        self.sync_remote_chats()
        logging.info(
            "[start_sync] sync_remote_chats() finished in %.1fs",
            time.time() - _sync_phase1_started,
        )

        # After messages are loaded, remoteJidAlt bridge fields are available
        # so @lid ↔ @s.whatsapp.net duplicates (introduced because the API
        # returned both JID formats before messages were fetched) can now be
        # fully resolved and merged.
        self.chats = self.deduplicate_chats(self.chats)

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
                                          notify_errors=False)
        if refreshed is not None:
            self.chats = refreshed

        # Resolve all unresolved @lid JIDs in our chat list via WPPConnect API
        unresolved_lids = [
            jid for jid in self.chats.keys() 
            if jid.endswith("@lid") and jid not in getattr(self, "_lid_to_phone", {})
        ]
        if unresolved_lids:
            logging.info(f"[Sync] Resolving {len(unresolved_lids)} unresolved @lid chats via API...")
            self.resolve_lid_jids_via_api(unresolved_lids)
            self.chats = self.deduplicate_chats(self.chats)

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
            if chat_list_ok and chat_list_settled:
                self.sync_complete_sound.play()
                if not self.background_mode:
                    self.output(self.i18n.t("sync_complete"))
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
        if (len(self.chats) > 0 and getattr(self, "_wa_connected", False)
                and chat_list_ok and chat_list_settled):
            self._sync_completed = True
            self._sync_retry_count = 0
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
                "[start_sync] Sync incomplete (chat_list_ok=%s, settled=%s, chats=%d) — "
                "the health checker will retry it (attempt %d so far).",
                chat_list_ok, chat_list_settled, len(self.chats), self._sync_retry_count,
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
        pending = len(getattr(self, "_chats_awaiting_messages", set()))
        still_landing = self.refresh_history_still_landing(context="after initial sync")
        if pending or still_landing:
            logging.info(
                "[backfill] Scheduling backfill: %d chat(s) short of a full page, "
                "history still landing=%s.", pending, still_landing)
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
                has_media = any(
                    msg.get("messageType") in _MEDIA_TYPES or msg.get("type") in _MEDIA_TYPES
                    for chat in self.chats.values()
                    for msg in chat.get("messages", {}).get("messages", {}).get("records", [])
                )
                if has_media:
                    wx.CallAfter(self._set_status, self.i18n.t("downloading_media"))
                    if not self.background_mode:
                        announced = True
                        wx.CallAfter(self.output, self.i18n.t("sync_media_started"))

                count = self.sync_media_for_all_chats()
                logging.info("[start_sync] Phase 2 downloaded %d media file(s).", count)
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

    def wait_messages_set(self):
        self._set_status(self.i18n.t("preparing_to_sync"))
        # Fallback: WPPConnect does not emit a messages.set WebSocket event.
        # Poll the API every 5 s for up to 60 s and start sync as soon as it
        # responds.  If the API never responds within the window, start sync
        # unconditionally so the program never stays stuck on "preparing to sync".
        def _fallback():
            def _already_syncing() -> bool:
                if self.messages_set_completed:
                    return True
                existing = getattr(self, "sync_thread", None)
                if existing and existing.is_alive():
                    return True
                return getattr(self, "_sync_completed", False)

            def _probe_and_start() -> bool:
                """Probe the API for existing chats; start sync and return True if found."""
                if _already_syncing():
                    # Sync is already running or completed — clear "preparing" status
                    # if it was left visible because the sync finished before this
                    # fallback thread checked in.
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
                    # Same reason as in get_remote_chats(): without this flag the
                    # probe kicks off WPP.chat.list()'s serial per-group metadata
                    # fetch inside the page.  Our 5 s timeout doesn't cancel it,
                    # so a probe that "failed" still leaves that loop running and
                    # competing with the real chat-list fetch that follows.
                    r = requests.post(
                        url,
                        json={"ignoreGroupMetadata": True},
                        headers=headers,
                        timeout=5,
                    )
                    if r.ok and isinstance(r.json(), list):
                        self.messages_set_completed = True
                        self._try_start_sync_thread()
                        return True
                except Exception:
                    pass
                return False

            # Probe immediately — when the server is already connected (no
            # session-logged event fires), this avoids an unnecessary 5-second wait.
            if _probe_and_start():
                return

            for _ in range(12):   # 12 × 5 s = 60 s maximum
                time.sleep(5)
                if _probe_and_start():
                    return

            # 60 s elapsed and sync still hasn't started — start it unconditionally
            # so the program never stays stuck on "preparando para sincronizar".
            self.messages_set_completed = True
            self._try_start_sync_thread()
        threading.Thread(target=_fallback, daemon=True).start()

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

    def clear_local_data(self):
        """Wipe all cached chats, contacts, messages, media, and mapping caches to avoid cross-account leakage."""
        logging.info("[clear_local_data] Clearing all local caches, media, and database...")
        self.chats = {}
        self.contacts = {}
        self._status_updates = {}
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
            
        try:
            if hasattr(self, "db") and self.db is not None:
                self.db.save_full_state({"chats": {}, "contacts": {}})
                logging.info("[clear_local_data] Database cleared successfully.")
        except Exception as e:
            logging.error(f"[clear_local_data] Failed to clear database: {e}")
            
        # Clear local downloaded media files to prevent cross-account leakage
        for subdir in ("media", "voice_messages"):
            path = data_path(subdir)
            if os.path.exists(path):
                import shutil
                try:
                    for filename in os.listdir(path):
                        file_path = os.path.join(path, filename)
                        if os.path.isfile(file_path) or os.path.islink(file_path):
                            os.unlink(file_path)
                        elif os.path.isdir(file_path):
                            shutil.rmtree(file_path)
                    logging.info(f"[clear_local_data] Cleared folder: {subdir}")
                except Exception as e:
                    logging.error(f"[clear_local_data] Failed to clear {subdir} folder: {e}")

    def create_basic_files(self):
        data_dir = data_path("")
        os.makedirs(data_dir, exist_ok=True)

        #Create media/voice message directories
        os.makedirs(data_path("media"), exist_ok=True)
        os.makedirs(data_path("voice_messages"), exist_ok=True)

        #Create stderr/stdout log files
        log_dir = data_path("log")
        os.makedirs(log_dir, exist_ok=True)
        stderr_log = os.path.join(log_dir, "stderr.log")
        stdout_log = os.path.join(log_dir, "stdout.log")
        if not os.path.isfile(stderr_log):
            open(stderr_log, "w").close()
        if not os.path.isfile(stdout_log):
            open(stdout_log, "w").close()
        #Set stderr and stdout
        sys.stderr = open(stderr_log, "a")
        sys.stdout = open(stdout_log, "a")

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

    def get_remote_chats(self, chats, persist_full: bool = True, notify_errors: bool = True):
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
        """
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
                response = requests.post(url, json=payload, headers=headers, timeout=_timeout)
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

                # Diagnostic log to inspect chat keys
                lid_chats = [c for c in response_data if isinstance(c, dict) and c.get("remoteJid", "").endswith("@lid")]
                if lid_chats:
                    logging.info(f"[get_remote_chats] RAW LID CHAT KEYS: {list(lid_chats[0].keys())}")
                    logging.info(f"[get_remote_chats] RAW LID CHAT DATA: {lid_chats[0]}")

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
                                if remote.endswith("@lid") and alt.endswith("@s.whatsapp.net"):
                                    if not hasattr(self, "_lid_to_phone"):
                                        self._lid_to_phone = {}
                                    if not hasattr(self, "_phone_to_lid"):
                                        self._phone_to_lid = {}
                                    if self._lid_to_phone.get(remote) != alt:
                                        self._lid_to_phone[remote] = alt
                                        self._phone_to_lid[alt] = remote
                                        logging.info(f"[LID Mapping] Extracted mapping from lastMessage in get_remote_chats: {remote} <-> {alt}")
                                elif alt.endswith("@lid") and remote.endswith("@s.whatsapp.net"):
                                    if not hasattr(self, "_lid_to_phone"):
                                        self._lid_to_phone = {}
                                    if not hasattr(self, "_phone_to_lid"):
                                        self._phone_to_lid = {}
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
                    # WhatsApp matched but no conversation ever started with).
                    # Real chats always carry a last-activity timestamp ("t"),
                    # a last message, or unread messages; entries with none of
                    # these are not real conversations and would otherwise
                    # pollute the chat list and the forward-message picker.
                    # Groups are exempt: a freshly-joined group can legitimately
                    # have none of these yet.
                    if jid not in chats and not jid.endswith("@g.us"):
                        has_activity = (
                            bool(chat.get("t"))
                            or bool(chat.get("lastMessage"))
                            or bool(chat.get("unreadCount"))
                        )
                        if not has_activity:
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
                        # If chat exists in self.chats (passed in), preserve any higher unreadCount
                        if hasattr(self, "chats") and jid in self.chats:
                            local_unread = int(self.chats[jid].get("unreadCount") or 0)
                            server_unread = int(chat.get("unreadCount") or 0)
                            if local_unread > server_unread:
                                chat["unreadCount"] = local_unread
                        chats[jid] = chat
                    else:
                        for k, v in chat.items():
                            if k in ("messages", "remoteJid"):
                                continue
                            if k == "pushName" and jid.endswith("@g.us"):
                                continue
                            if k == "name" and jid.endswith("@g.us") and not v:
                                v = self._group_name_from_chat_dict(chat)
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
                                # Never resurrect unread count for the conversation the
                                # user has open right now — mark_conversation_as_read()
                                # already set it to 0 locally, and this snapshot can be
                                # a few seconds stale relative to that. Same guard as
                                # on_chat_unread_update()'s live-event path.
                                _cp = getattr(self, "conversations_panel", None)
                                if jid == getattr(_cp, "_last_open_jid", ""):
                                    v = 0
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
                                    continue
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
                                # A real chat-list snapshot now backs this
                                # chat's unreadCount, whichever way the guards
                                # above resolved it — the notification code can
                                # trust the number again.
                                chats[jid].pop("_unread_count_unsynced", None)
                            chats[jid][k] = v
                        # The incoming chat dict may carry the group's real
                        # name only under groupMetadata.subject (see
                        # _group_name_from_chat_dict) and may not even have a
                        # "name" key at all, in which case the loop above
                        # never touched chats[jid]["name"] — re-derive from
                        # the raw incoming `chat` (not chats[jid]) so this
                        # still catches it.
                        if jid.endswith("@g.us") and not chats[jid].get("name"):
                            subj = self._group_name_from_chat_dict(chat)
                            if subj:
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
                        if mute_expiry == -1 or (isinstance(mute_expiry, (int, float)) and mute_expiry > now):
                            if self._muted_chats.get(jid) != int(mute_expiry):
                                self._muted_chats[jid] = int(mute_expiry)
                                db_changed = True
                        elif jid in self._muted_chats:
                            del self._muted_chats[jid]
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

                # Retroactively prune 1:1 phantom chats that slipped into the
                # local cache before this filter existed: no local messages,
                # no server-reported activity, and not deliberately pinned or
                # muted by the user. Only worth the full-dict scan right after
                # a real sync (persist_full=True) — the periodic background
                # refresh just wants pin/mute state, so skip it there.
                if persist_full:
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
                else:
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
                is_self_referential = any(
                    r.get("key", {}).get("fromMe")
                    and r.get("key", {}).get("participant", "").split("@", 1)[0] == cand_digits
                    and (cand_jid.endswith("@g.us") or self._phone_digits_equivalent(cand_digits, my_jid_digits))
                    for r in records
                    if r.get("key", {}).get("participant")
                )
                is_self_phone_group = (
                    cand_jid.endswith("@g.us")
                    and self._phone_digits_equivalent(cand_digits, my_jid_digits)
                )
                if not (is_self_referential or is_self_phone_group):
                    continue
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
        """
        name = (chat.get("name") or "").strip()
        if name:
            return name
        subject = (chat.get("subject") or "").strip()
        if subject:
            return subject
        group_meta = chat.get("groupMetadata")
        if isinstance(group_meta, dict):
            gm_subject = (group_meta.get("subject") or "").strip()
            if gm_subject:
                return gm_subject
        return ""

    def _is_group_send_restricted(self, chat: dict) -> bool:
        """True when `chat` is a WhatsApp group set to "only admins can send
        messages" (Baileys/WPPConnect's groupMetadata.announce) and the
        current user isn't one of those admins.

        Only ever reads group metadata local sync already has — this must
        stay synchronous and side-effect-free since it runs from
        navigate_to_conversation() on the UI thread. If the data needed to
        decide isn't available yet (participants not hydrated in local
        metadata), this fails OPEN (returns False, message field stays
        writable): WhatsApp Web itself is the actual source of truth and
        would reject the send if this guess were wrong in that direction,
        whereas guessing wrong the other way would silently lock a user out
        of a group they can genuinely post in.
        """
        jid = chat.get("remoteJid", "")
        if not jid.endswith("@g.us"):
            return False
        group_meta = chat.get("groupMetadata")
        if not isinstance(group_meta, dict):
            group_meta = {}
        announce = _parse_bool_flag(group_meta.get("announce"))
        if announce is None:
            announce = _parse_bool_flag(chat.get("announce"))
        if not announce:
            return False

        participants = group_meta.get("participants") or chat.get("participants") or []
        if not isinstance(participants, list) or not participants:
            return False  # can't verify admin status — fail open

        def _phone_part(j) -> str:
            if not isinstance(j, str):
                return ""
            return j.rsplit("@", 1)[0].split(":")[0]

        my_phone_digits = _phone_part(getattr(self, "my_jid", ""))
        my_lid_digits   = _phone_part(getattr(self, "my_lid", ""))

        for p in participants:
            if not isinstance(p, dict):
                continue
            p_id = p.get("id") or ""
            if isinstance(p_id, dict):
                p_id = p_id.get("_serialized", "")
            p_digits = _phone_part(p_id)
            if not p_digits:
                continue
            is_me = (
                (my_phone_digits and self._phone_digits_equivalent(p_digits, my_phone_digits))
                or (my_lid_digits and p_digits == my_lid_digits)
            )
            if is_me:
                return not bool(p.get("admin") or p.get("isAdmin"))
        return False  # current user not found in participants — fail open

    def _fill_group_name(self, jid: str) -> str:
        """Fetch group info from API and cache the name.

        Called lazily when a group has no cached name. Returns the group
        name or empty string on failure.
        """
        try:
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/group-info/{jid}"
            headers = {"Authorization": f"Bearer {self.token}"}
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.ok:
                body = resp.json()
                info = body.get("response", body) if isinstance(body, dict) else {}
                name = info.get("name") or info.get("subject", "")
                if name:
                    if not hasattr(self, "_group_name_cache"):
                        self._group_name_cache = {}
                    self._group_name_cache[jid] = name
                    return name
        except Exception:
            pass
        return ""

    def _resolve_group_name_async(self, jid: str):
        """Look up a newly-seen group's name in the background.

        _fill_group_name() does a blocking HTTP request, so this must not run
        on the wx main thread (on_new_message is called via wx.CallAfter).
        """
        def _worker():
            name = self._fill_group_name(jid)
            if not name:
                return
            chat = self.chats.get(jid)
            if chat is None or self._group_name_from_chat_dict(chat):
                return
            chat["name"] = name
            self._schedule_save(dirty_jid=jid)
            wx.CallAfter(self._schedule_set_chats)
        threading.Thread(target=_worker, daemon=True).start()

    def _apply_group_subject_change(self, remote_jid: str, chat: dict, msg: dict) -> None:
        """Rename an already-known group chat when its WhatsApp subject changes.

        The "gp2"/subject-change system message (subtype "subject", new name
        in "body" — see WebSocketClient's gp2 handling) already got rendered
        as an in-chat notification, but nothing updated chat["name"] itself:
        the group kept showing its old name everywhere else (chat list,
        window title, tray tooltip, dialogs) until the next full sync
        happened to re-fetch group-info, which could be minutes/hours away
        or never for a group with no other activity. Applying it immediately
        here — from the same event that already told us the new name — keeps
        the chat list in sync instead of looking like the renamed group
        vanished.
        """
        if not remote_jid.endswith("@g.us"):
            return
        if msg.get("messageType") != "groupNotification":
            return
        notif = (msg.get("message") or {}).get("groupNotification") or {}
        if notif.get("subtype") != "subject":
            return
        new_name = (notif.get("body") or "").strip()
        if not new_name or chat.get("name") == new_name:
            return
        chat["name"] = new_name
        self._group_name_cache = getattr(self, "_group_name_cache", {})
        self._group_name_cache[remote_jid] = new_name

    def on_group_subject_updated(self, remote_jid: str, new_subject: str) -> None:
        """
        Handle a live group subject update received via the groups.update websocket event.
        """
        if remote_jid not in self.chats:
            return

        new_name = new_subject.strip()
        if not new_name:
            return

        chat = self.chats[remote_jid]
        if chat.get("name") == new_name:
            return

        logging.info(f"[on_group_subject_updated] Updating group {remote_jid} name to {new_name}")
        chat["name"] = new_name
        self._group_name_cache = getattr(self, "_group_name_cache", {})
        self._group_name_cache[remote_jid] = new_name
        
        self._schedule_save(dirty_jid=remote_jid)
        self._schedule_set_chats()

    def _resolve_missing_group_names(self):
        """Retry group-info lookups for groups still unnamed after sync.

        Runs on the background sync thread. Uses a few parallel workers so a
        large number of unresolved groups doesn't add much wall-clock time to
        the sync.
        """
        unresolved = [
            jid for jid, chat in list(self.chats.items())
            if jid.endswith("@g.us") and not self._group_name_from_chat_dict(chat)
        ]
        if not unresolved:
            return
        max_workers = min(6, len(unresolved))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(self._fill_group_name, jid): jid for jid in unresolved}
            for fut in as_completed(futs):
                jid = futs[fut]
                try:
                    name = fut.result()
                except Exception:
                    name = ""
                if name:
                    self.chats[jid]["name"] = name
                    self._schedule_save(dirty_jid=jid)

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

    def _load_local_lid_cache(self):
        try:
            self._lid_to_phone = self.db.get_lid_mappings()
            self._phone_to_lid = {v: k for k, v in self._lid_to_phone.items()}
            lids, names = self.db.get_unresolvable_lids()
            self._unresolvable_lids = lids
            self._unresolvable_names = names
            self._status_updates = self.db.get_status_updates()
            logging.info(f"[LID Cache] Loaded {len(self._lid_to_phone)} JID mappings, {len(self._unresolvable_lids)} LIDs, {len(self._unresolvable_names)} names, and status updates for {len(self._status_updates)} participants.")
            return
        except Exception as e:
            logging.error(f"[LID Cache] Error loading JID mappings from database: {e}")
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
                    response = requests.get(url, headers=headers, timeout=90)
                    if response.status_code not in (200, 201):
                        logging.error(f"[get_remote_contacts] API error {response.status_code}: {response.text[:200]}")
                        response_data = []
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
                logging.info(f"[get_remote_contacts] First contact raw keys: {list(filtered_contacts[0].keys())}")
                logging.info(f"[get_remote_contacts] First contact raw data: {filtered_contacts[0]}")
            if names_with_values:
                logging.info(f"[get_remote_contacts] First 50 named contacts: {', '.join(names_with_values[:50])}")
            else:
                logging.info("[get_remote_contacts] No filtered contacts have a name or pushName field set in the API response.")
            
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
                        logging.debug(f"[get_remote_contacts] Adding contact: {name} ({jid})")
                        self.contacts[jid] = contact
                    else:
                        updated_fields = []
                        for k, v in contact.items():
                            if v is not None and v != "":
                                if self.contacts[jid].get(k) != v:
                                    self.contacts[jid][k] = v
                                    updated_fields.append(k)
                        if updated_fields:
                            logging.debug(f"[get_remote_contacts] Updated fields {updated_fields} for contact: {name} ({jid})")
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
                    if elapsed >= _CONTACT_POLL_SECONDS:
                        elapsed = 0
                        self.get_remote_contacts()
                        self.get_block_list()
                    result = self.get_remote_chats(dict(self.chats), persist_full=False,
                                                   notify_errors=False)
                    if result is not None:
                        self.chats = result
                    wx.CallAfter(self._schedule_set_chats)
                    # Phone-side clears/deletions — active conversation only,
                    # one extra cheap GET per cycle, nothing at all when no
                    # conversation is open. See the method's own docstring
                    # for why this stays scoped to just the open chat.
                    self._reconcile_active_conversation_with_remote()
                except Exception as e:
                    logging.warning(f"[periodic_contacts_sync] error: {e}")

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

    def _compute_chat_lists(self):
        """Compute sorted/filtered chat lists. Safe to run on a background thread."""
        deleted  = self._deleted_chats
        archived = self._archived_chats
        pinned   = self._pinned_chats
        my_jid   = getattr(self, "my_jid", "")

        # Dedup: if both a @lid JID and its corresponding phone JID exist as
        # separate keys in self.chats, only render the one with more content
        # (prefer @lid since that's the active WPPConnect chat). Build a set of
        # phone JIDs that are already covered by a @lid entry so we can skip them.
        lid_to_phone = getattr(self, "_lid_to_phone", {})
        phone_to_lid = getattr(self, "_phone_to_lid", {})
        _covered_by_lid: set[str] = set()
        for lid_jid, phone_jid in lid_to_phone.items():
            if lid_jid in self.chats and phone_jid in self.chats:
                # Both exist — keep the one with more messages (usually lid).
                lid_msgs = len(self.chats[lid_jid].get("messages", {}).get("messages", {}).get("records", []))
                phone_msgs = len(self.chats[phone_jid].get("messages", {}).get("messages", {}).get("records", []))
                if lid_msgs >= phone_msgs:
                    _covered_by_lid.add(phone_jid)
                else:
                    _covered_by_lid.add(lid_jid)

        main_chats, main_names = [], []
        arch_chats, arch_names = [], []

        # Every row the UI renders is identified by ``chat["remoteJid"]``, not by
        # the ``self.chats`` key it was stored under — and the two are NOT always
        # the same string (a chat merged/renamed in place keeps the dict it had,
        # and `_merge_lid_into_phone`/`deduplicate_chats` rewrite one of them).
        # Two keys resolving to the same remoteJid therefore produced two
        # identical-looking rows, and because every lookup downstream
        # (`_displayed_jids`, focus restore, `on_conversation_selected`) matches
        # on remoteJid, activating either row could open whichever one came
        # first — reported live as archived groups appearing twice and opening
        # a different group than the one announced. Render each remoteJid once.
        _seen_render_jids: set[str] = set()

        for jid, chat in list(self.chats.items()):
            if jid in deleted:
                continue
            if jid in _covered_by_lid:
                continue  # duplicate – already shown via the other JID
            render_jid = chat.get("remoteJid") or jid
            if render_jid in _seen_render_jids:
                continue
            _seen_render_jids.add(render_jid)

    
            records_wrapper = chat.get("messages") or {}
            records = []
            if isinstance(records_wrapper, dict):
                inner_wrapper = records_wrapper.get("messages") or {}
                if isinstance(inner_wrapper, dict):
                    records = inner_wrapper.get("records") or []
            last_msg  = chat.get("lastMessage")
            unread    = int(chat.get("unreadCount", 0) or 0)
            is_pinned = jid in pinned
            # Skip chats with absolutely no content AND no identity.
            # We do NOT skip based on missing messages alone: during and just
            # after sync many valid chats have empty records but still carry a
            # name/pushName from the WPPConnect list-chats response.
            # A cleared conversation is *supposed* to be empty: it must stay in
            # the list (with no preview) instead of vanishing as if deleted.
            is_cleared   = jid in self.settings.get("cleared_chats", {})
            has_content  = bool(records or last_msg or unread > 0 or is_pinned or is_cleared)

            is_group = jid.endswith("@g.us")
            resolved_name = ""
            msg_push = ""
            # Resolve the contact name BEFORE deciding whether to drop the chat.
            # This used to happen ~30 lines below the skip, so a chat whose name
            # was perfectly resolvable through self.contacts was still judged
            # "no identity" on the raw dict alone and dropped before the lookup
            # ever ran.  On a real account that silently hid 218 of 539
            # conversations — every individual chat that WhatsApp Web had not
            # yet loaded messages for (list-chats returns `msgs: null`, so
            # lastMessage is empty for all of them) and that had no unread
            # count, even though all 263 had a matching contact record.
            if not is_group:
                resolved_name = self._resolve_contact_name(chat)

            name_hint    = (chat.get("name") or chat.get("pushName") or resolved_name or
                            self._group_name_from_chat_dict(chat)).strip()
            has_identity = bool(name_hint and not name_hint.isdigit() and len(name_hint) > 1)
            if not has_content and not has_identity:
                continue

            def get_valid_name(val):
                return "" if self._is_bad_contact_name(val) else val.strip()

            if jid.endswith("@lid"):
                phone_jid = getattr(self, "_lid_to_phone", {}).get(jid) or self._find_alt_jid_from_messages(chat)
            else:
                phone_jid = jid

            if is_group:
                # A group's real name may only be under groupMetadata.subject
                # in the raw chat dict — see _group_name_from_chat_dict().
                name = get_valid_name(self._group_name_from_chat_dict(chat))
                if not name:
                    cached = getattr(self, "_group_name_cache", {}).get(jid, "")
                    if cached:
                        name = cached
                    else:
                        fetched = self._fill_group_name(jid)
                        if fetched:
                            chat["name"] = fetched
                            name = fetched
            else:
                # Chat individual: resolved_name já foi calculado acima, antes
                # do descarte — reaproveitado aqui em vez de resolver de novo.
                chat_push = get_valid_name(chat.get("pushName", ""))
                name = resolved_name or chat_push
                if not name:
                    msg_push = self.find_name_through_messages(chat)
                    name = msg_push or get_valid_name(chat.get("name", ""))
            
            # Treat placeholders as empty to trigger phone number fallback
            if name and (self._is_bad_contact_name(name) or name == self.i18n.t("unknown_contact")):
                name = ""

            if not name or not name.strip():
                if jid.endswith("@g.us"):
                    name = self.i18n.t("unknown_group")
                else:
                    if phone_jid and not phone_jid.endswith("@lid"):
                        name = format_number(phone_jid)
                    else:
                        msg_jid_num = self.find_jid_through_messages(chat)
                        if msg_jid_num:
                            name = msg_jid_num
                        elif self._format_jid_for_display(jid):
                            name = self._format_jid_for_display(jid)
                        else:
                            # Let's check contact cache and chat metadata for a fallback (like masked number +55∙∙∙∙∙∙∙∙12)
                            c_obj = self.contacts.get(jid) or {}
                            fallback = c_obj.get("formattedName") or c_obj.get("pushName") or chat.get("formattedName") or chat.get("pushName") or ""
                            fallback_clean = fallback.strip()
                            if fallback_clean and "sem nome" not in fallback_clean.lower() and fallback_clean != self.i18n.t("unknown_contact"):
                                name = fallback_clean
                            elif jid.endswith("@lid"):
                                name = self.i18n.t("unknown_contact")
                            else:
                                numeric = jid.split("@")[0].split(":")[0]
                                if numeric.isdigit():
                                    name = format_number(numeric)
                                else:
                                    name = self.i18n.t("unknown_contact")
            
            # Detailed logging for name resolution debugging
            if jid.endswith("@lid") or name == self.i18n.t("unknown_contact"):
                logging.info(
                    f"[Name Resolution] jid={jid} phone_jid={phone_jid} "
                    f"resolved_name={resolved_name} "
                    f"msg_name={msg_push} "
                    f"chat_name={chat.get('name')} push_name={chat.get('pushName')} -> final_name='{name}'"
                )
            if my_jid and not jid.endswith("@g.us") and self._is_self_jid(jid):
                name = self.i18n.t("self_chat_name")
            raw_arch = chat.get("archive")
            if raw_arch is None:
                raw_arch = chat.get("archived")
            arch_flag = _parse_bool_flag(raw_arch)
            # An explicit flag on the chat record (server truth) wins over the
            # persisted set; the set only decides when the record says nothing.
            is_archived = arch_flag if arch_flag is not None else (jid in archived)
            if is_archived:
                arch_chats.append(chat)
                arch_names.append(name)
            else:
                main_chats.append(chat)
                main_names.append(name)

        # Pinned chats float to the top; within each group sort by most-recent
        # message timestamp descending (newest first), then alphabetically.
        #
        # Only counts is_countable_message() records — a system event (group
        # join/leave, settings change, revoke, ...) stored in this chat's
        # records must never push it back to the top of the list just
        # because its timestamp is the newest one on file. chat["t"]/
        # lastMessage are already never set from a non-countable message
        # (see on_new_message()/on_historical_message()), but this also
        # scans every raw record directly, so it needs the same filter.
        def _chat_last_ts(c):
            # Fallback to chat's own last activity timestamp (t)
            chat_ts = int(c.get("t", 0) or 0)
            if chat_ts > 1_000_000_000_000:
                chat_ts //= 1000
            ts = chat_ts

            lm = c.get("lastMessage")
            if isinstance(lm, dict) and is_countable_message(lm):
                lm_ts = int(lm.get("timestamp", 0) or lm.get("messageTimestamp", 0) or lm.get("t", 0) or 0)
                if lm_ts > 1_000_000_000_000:
                    lm_ts //= 1000
                if lm_ts > ts:
                    ts = lm_ts

            records_wrapper = c.get("messages") or {}
            if isinstance(records_wrapper, dict):
                inner_wrapper = records_wrapper.get("messages") or {}
                if isinstance(inner_wrapper, dict):
                    records_copy = list(inner_wrapper.get("records") or [])
                    if records_copy:
                        for m in records_copy:
                            # Only records the preview would show may move a chat
                            # — see _counts_as_last_message(), which is stricter
                            # than is_countable_message() and rejects non-dicts
                            # itself. Counting silent bookkeeping here (a
                            # groupNotification for someone joining) floated
                            # week-old groups above live ones while they still
                            # displayed their old preview.
                            if not self._counts_as_last_message(m):
                                continue
                            t = int(m.get("timestamp", 0) or m.get("messageTimestamp", 0) or m.get("t", 0) or 0)
                            if t > 1_000_000_000_000:
                                t //= 1000
                            if t > ts:
                                ts = t

            return ts if ts else 1

        def _sort_key(pair):
            c, n = pair
            j   = c.get("remoteJid", "")
            pin = 0 if j in pinned else 1
            return (pin, -_chat_last_ts(c), n.lower())

        pairs = sorted(zip(main_chats, main_names), key=_sort_key)
        main_chats = [c for c, _ in pairs]
        main_names = [n for _, n in pairs]

        arch_pairs = sorted(zip(arch_chats, arch_names), key=_sort_key)
        arch_chats = [c for c, _ in arch_pairs]
        arch_names = [n for _, n in arch_pairs]

        return main_chats, main_names, arch_chats, arch_names

    def _apply_chat_lists(self, main_chats, main_names, arch_chats, arch_names):
        """Apply sorted chat lists to panels and refresh UI. Must run on main thread."""
        if not hasattr(self, "conversations_panel"):
            return  # UI not yet initialized; skip silently
        self.chat_names = main_names

        # Save focused JIDs from the CURRENT (old) displayed lists BEFORE
        # overwriting chats_list.  add_chats_to_ui() maps focused_idx (from
        # the live ListCtrl) against chats_list to recover the JID — but
        # chats_list is about to be replaced with a reordered copy, so
        # focused_idx would point to the wrong chat after the assignment.
        _panel = self.conversations_panel
        _fi = _panel.conversations_list.GetFocusedItem()
        _panel._preserved_focused_jid = (
            _panel.chats_list[_fi].get("remoteJid")
            if 0 <= _fi < len(_panel.chats_list) else None
        )
        if hasattr(self, "archived_conversations_panel"):
            _ap = self.archived_conversations_panel
            _afi = _ap.conversations_list.GetFocusedItem()
            _ap._preserved_focused_jid = (
                _ap.chats_list[_afi].get("remoteJid")
                if 0 <= _afi < len(_ap.chats_list) else None
            )

        # _all_chats_list / _all_chat_names always hold the full sorted list.
        # add_chats_to_ui() reads these to apply search / filter, then writes
        # back to chats_list / chat_names so indices stay consistent.
        self.conversations_panel._all_chats_list = main_chats
        self.conversations_panel._all_chat_names = main_names
        self.conversations_panel.chats_list = main_chats
        self.conversations_panel.chat_names = main_names

        if hasattr(self, "archived_conversations_panel"):
            self.archived_conversations_panel._all_chats_list = arch_chats
            self.archived_conversations_panel._all_chat_names = arch_names
            self.archived_conversations_panel.chats_list = arch_chats
            self.archived_conversations_panel.chat_names = arch_names

        if self.IsShown():
            self.add_chats_to_ui()
        # Refresh title whenever chat list / unread counts change.
        # Tray tooltip is only refreshed while the window is hidden — when
        # visible the title already shows unread counts, and RemoveIcon/SetIcon
        # disrupts NVDA focus (see tray_manager.py update_tooltip docstring).
        self._update_title()

    def _apply_chat_lists_if_current(self, generation: int, *args):
        """Apply a chat-list rebuild only if no newer one has been kicked off
        since. See ``_chat_list_generation`` for why this matters — without
        it, two rebuilds racing (e.g. a message arriving right as sync
        finishes) could apply in the wrong order and leave the UI showing the
        older/stale one."""
        if generation != self._chat_list_generation:
            logging.debug(
                "[chat_lists] discarding stale rebuild (generation=%d, current=%d)",
                generation, self._chat_list_generation,
            )
            return
        self._apply_chat_lists(*args)

    def set_chats(self):
        # NOTE: _build_lid_to_phone_cache() is intentionally NOT called here.
        # It scans every message in every chat (O(chats × messages)) and is
        # too expensive to run on the wx main thread. The cache is built once
        # at startup (in init_chats) and then maintained incrementally by
        # _extract_lid_mapping() on each new message.
        self._chat_list_generation += 1
        generation = self._chat_list_generation
        def _bg():
            try:
                result = self._compute_chat_lists()
                wx.CallAfter(self._apply_chat_lists_if_current, generation, *result)
            except Exception:
                logging.exception("[set_chats] Unhandled error during bg set_chats")
        threading.Thread(target=_bg, daemon=True).start()

    # How often the watchdog pings the wx main loop, and how long a ping may
    # go unanswered before the UI counts as stalled. One second between pings
    # costs a single no-op CallAfter per second while everything is healthy.
    _UI_WATCHDOG_INTERVAL = 1.0
    _UI_WATCHDOG_STALL_SECONDS = 2.0

    def start_ui_watchdog(self):
        """Detect a frozen wx main loop and log *where* it is frozen.

        Every UI-freeze report so far has been diagnosed by inference, because
        the main thread writes nothing to the log while it is blocked — the
        freeze window simply contains worker-thread lines and a hole. Two
        successive theories built that way (a refresh storm, then an oversized
        pagination window) each explained the symptom and each turned out not
        to be the cause, which is a good sign the guessing should stop.

        This pings the main loop once a second with a no-op CallAfter. When a
        ping goes unanswered past _UI_WATCHDOG_STALL_SECONDS the main thread is
        by definition not draining its event queue, and sys._current_frames()
        gives its stack from *outside* it — so the log gets the exact call the
        UI is stuck in, repeated every couple of seconds for as long as the
        stall lasts, followed by how long it took to recover.

        Costs nothing while the UI is healthy and never touches UI state, so it
        stays on permanently rather than being a debug-only switch.
        """
        main_id = threading.main_thread().ident

        def _loop():
            import sys as _sys
            import traceback as _traceback
            while not getattr(self, "_shutting_down", False):
                pong = threading.Event()
                t0 = time.monotonic()
                try:
                    wx.CallAfter(pong.set)
                except Exception:
                    return          # app is going away
                stalled = False
                while not pong.wait(self._UI_WATCHDOG_STALL_SECONDS):
                    if getattr(self, "_shutting_down", False):
                        return
                    stalled = True
                    frame = _sys._current_frames().get(main_id)
                    stack = ("".join(_traceback.format_stack(frame)) if frame
                             else "<main thread frame unavailable>")
                    logging.warning(
                        "[ui-watchdog] UI thread unresponsive for %.1fs — main thread stack:\n%s",
                        time.monotonic() - t0, stack)
                if stalled:
                    logging.warning(
                        "[ui-watchdog] UI thread responsive again after %.1fs.",
                        time.monotonic() - t0)
                time.sleep(self._UI_WATCHDOG_INTERVAL)

        threading.Thread(target=_loop, daemon=True, name="ui-watchdog").start()
        logging.info("[ui-watchdog] started (ping every %.0fs, stall threshold %.0fs).",
                     self._UI_WATCHDOG_INTERVAL, self._UI_WATCHDOG_STALL_SECONDS)

    # Longer than the 300 ms used for content refreshes: this one only ever
    # repaints *names* that background resolution has just filled in, and a
    # name settling a second later is invisible to the user — while the
    # resolution loops that trigger it run in batches for minutes on end.
    _ACTIVE_REFRESH_DEBOUNCE_MS = 1000

    def _schedule_refresh_active_messages(self):
        """Debounce refresh_active_conversation_messages().

        That method re-renders every row of the open conversation
        (SetItemText + _render_message_line per message), and three separate
        background loops — [Contact Resolution], [Mentions Scan] and
        [LID Resolution] — used to wx.CallAfter it once per resolved batch.
        On an account with thousands of unresolved @lid senders those batches
        arrive continuously for minutes, so the wx event queue filled with
        full re-renders faster than the main loop could drain them.

        Confirmed, not inferred: the UI watchdog caught three stalls of 9.4 s,
        19.8 s and 40.1 s, and all 33 stack samples taken during them landed on
        the same line — conversations.py's `SetItemText(i,
        self._render_message_line(msg))`. Two earlier theories about this
        freeze (a refresh storm on the history path, then an oversized
        pagination window) were wrong; this is what the stacks actually show.
        """
        if getattr(self, "_refresh_active_pending", False):
            return
        self._refresh_active_pending = True
        wx.CallLater(self._ACTIVE_REFRESH_DEBOUNCE_MS,
                     self._do_scheduled_refresh_active_messages)

    def _do_scheduled_refresh_active_messages(self):
        """Run the coalesced name repaint. Flag is cleared first so a failure
        cannot wedge every later batch."""
        self._refresh_active_pending = False
        cp = getattr(self, "conversations_panel", None)
        if cp is None:
            return
        try:
            cp.refresh_active_conversation_messages()
        except Exception:
            logging.exception("[_do_scheduled_refresh_active_messages] repaint failed")

    def _schedule_refresh_messages(self):
        """Debounce the open conversation's message-list rebuild.

        on_historical_message() fires once per backfilled message, and for the
        conversation currently on screen every one of them used to schedule its
        own refresh_messages_if_changed() on the wx main thread. That call is
        not cheap: _messages_signature() walks *every stored record* of the chat
        — ~1400 on the group this was reported against, not just the 200 on
        screen — calling _get_message_content() on each, and whenever the
        signature differs, which it does on every genuinely new message,
        populate_messages() rebuilds the native list with DeleteAllItems() plus
        one Append() per visible row.

        During a history backfill that is hundreds of full rebuilds queued
        back-to-back on the UI thread, and wx does not coalesce CallAfter — so
        opening a large, actively-backfilling group locked the interface up for
        as long as its history kept landing. Reported live against a group
        the logs could not show it directly because
        nothing on the UI thread writes to them, but the freeze window held 95
        worker-thread lines and not one from the UI module.

        Same shape as _schedule_set_chats() below, and called from the same
        place, so a burst of N messages costs one rebuild instead of N.
        """
        if getattr(self, "_refresh_messages_pending", False):
            return
        self._refresh_messages_pending = True
        wx.CallLater(300, self._do_scheduled_refresh_messages)

    def _do_scheduled_refresh_messages(self):
        """Run the coalesced rebuild. Re-checks the panel still has a
        conversation open — the user may have closed it during the window."""
        self._refresh_messages_pending = False
        cp = getattr(self, "conversations_panel", None)
        if cp is None or getattr(cp, "conversation", None) is None:
            return
        try:
            cp.refresh_messages_if_changed()
        except Exception:
            logging.exception("[_do_scheduled_refresh_messages] refresh failed")

    def _schedule_set_chats(self):
        """Debounce set_chats() so rapid message bursts trigger only one rebuild.
        Safe to call from any thread; scheduling happens on the wx main thread."""
        if getattr(self, "_set_chats_pending", False):
            return
        self._set_chats_pending = True
        wx.CallLater(300, self._do_scheduled_set_chats)

    def _do_scheduled_set_chats(self):
        """Run heavy computation in background; apply UI changes on main thread."""
        self._set_chats_pending = False
        self._chat_list_generation += 1
        generation = self._chat_list_generation
        def _bg():
            try:
                # _build_lid_to_phone_cache() is intentionally NOT called here.
                # It scans every message in every chat (O(total_messages)) and is
                # too expensive to run on every WebSocket event.  The cache is
                # maintained incrementally by _extract_lid_mapping() on each new
                # message, and rebuilt in full only at startup (set_chats calls).
                result = self._compute_chat_lists()
                wx.CallAfter(self._apply_chat_lists_if_current, generation, *result)
            except Exception:
                logging.exception("[_do_scheduled_set_chats] Unhandled error during scheduled set_chats")
        threading.Thread(target=_bg, daemon=True).start()

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
        cache = getattr(self, "_lid_to_phone", {}).copy()
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

        self._lid_to_phone  = cache
        self._phone_to_lid  = {v: k for k, v in cache.items()}

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

        # Invalidate the negative cache since a new message is added to this chat
        if remote and hasattr(self, "_chats_without_alt_jid"):
            self._chats_without_alt_jid.discard(remote)

        # Cache pushName if present in the message
        push_name = msg.get("pushName")
        if push_name and remote and not remote.endswith("@g.us") and not is_phone_like(push_name):
            if not hasattr(self, "_message_pushname_cache"):
                self._message_pushname_cache = {}
            self._message_pushname_cache[remote] = push_name

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
            # Propagate contact details from phone contact to LID contact to make it immediately available
            contacts_to_update = {}
            for lid, phone in list(self._lid_to_phone.items()):
                if phone in self.contacts and self.contacts[phone]:
                    if lid not in self.contacts or self.contacts[lid].get("name") in (None, "", "Contato sem nome"):
                        self.contacts[lid] = self.contacts[phone].copy()
                        self.contacts[lid]["id"] = lid
                        self.contacts[lid]["remoteJid"] = lid
                        contacts_to_update[lid] = self.contacts[lid]

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
            logging.info(f"[LID Mapping] Found unresolved LIDs in message: {lids_to_resolve}")
            def resolve_in_bg():
                self.resolve_lid_jids_via_api(lids_to_resolve)
            threading.Thread(target=resolve_in_bg, daemon=True).start()

        if phone_jids_to_resolve:
            logging.info(f"[Contact Resolution] Found unresolved mentioned phone JIDs in message: {phone_jids_to_resolve}")
            def resolve_phones_in_bg():
                updated_contacts = {}
                for p_jid in phone_jids_to_resolve:
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
                if updated_contacts:
                    try:
                        self.db.upsert_contacts_batch(updated_contacts)
                    except Exception as e:
                        logging.error(f"[Contact Resolution] Error saving contacts incrementally: {e}")
                        self.save_data(self.chats, self.contacts)
                    wx.CallAfter(self._schedule_set_chats)
                    wx.CallAfter(self._schedule_refresh_active_messages)
            threading.Thread(target=resolve_phones_in_bg, daemon=True).start()

    def scan_all_cached_messages_for_mentions(self):
        """Scan all cached messages in self.chats, find all unresolved LIDs/phones, and resolve them."""
        def _scan():
            time.sleep(3)  # Wait for startup to stabilize
            logging.info("[Mentions Scan] Starting scan of all cached messages...")
            
            lids_to_resolve = set()
            phones_to_resolve = set()
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
                self._learn_sender_names_bulk(records)
                for msg in list(records):
                    if not isinstance(msg, dict):
                        continue
                    # First, see if we can extract immediate JID mappings from key/alt
                    key = msg.get("key") or {}
                    remote = key.get("remoteJid", "")
                    alt = key.get("remoteJidAlt", "")
                    participant = key.get("participant", "")

                    if alt and alt.endswith("@s.whatsapp.net"):
                        if remote.endswith("@lid") and self._lid_to_phone.get(remote) != alt:
                            self.register_jid_mapping(remote, alt)
                    elif alt and alt.endswith("@lid") and remote.endswith("@s.whatsapp.net"):
                        if self._lid_to_phone.get(alt) != remote:
                            self.register_jid_mapping(alt, remote)

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
                
            if phones_to_resolve:
                logging.info(f"[Mentions Scan] Found {len(phones_to_resolve)} unresolved mentioned phone JIDs.")
                updated_contacts = {}
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
                     wx.CallAfter(self._schedule_set_chats)
                     wx.CallAfter(self._schedule_refresh_active_messages)
            
            logging.info("[Mentions Scan] Scan and resolution of cached messages completed.")

        threading.Thread(target=_scan, daemon=True).start()

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
            resolved = (
                _try(remoteJid)
                or (phone and (_try(phone) or _try(phone.rsplit("@", 1)[0] + "@c.us")))
                or _ppm(remoteJid)
                or (phone and _ppm(phone))
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
        # Checks if window is still open
        if self.IsShown():
            lst = self.conversations_panel.conversations_list
            if lst.GetItemCount() > 0:
                # Only preselect focus if there is no current selection/focus
                if lst.GetFocusedItem() == -1:
                    lst.Focus(0)
                    lst.Select(0)
                    lst.EnsureVisible(0)


    def sync_remote_chats(self):
        chats = list(self.chats.values())
        if not chats:
            return
            
        # Filter out invalid JIDs (like '0' or empty entries) to prevent API errors
        valid_chats = []
        for c in chats:
            jid = c.get("remoteJid", "")
            user_part = jid.split("@")[0] if "@" in jid else jid
            if user_part and user_part != "0" and len(user_part) >= 5:
                valid_chats.append(c)
            else:
                logging.warning(f"[sync_remote_chats] Skipping invalid JID from sync: {jid}")
                
        if not valid_chats:
            return
            
        # Sort chats by most recent active timestamp
        try:
            valid_chats = sorted(valid_chats, key=lambda c: c.get("t", 0) or 0, reverse=True)
        except Exception:
            pass
            
        # Parallel HTTP calls dramatically reduce sync time.  WPPConnect handles
        # concurrent requests fine; cap at 6 workers to avoid overloading it.
        max_workers = min(6, len(valid_chats))
        failed_count = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(self.sync_chat_messages, c.copy()): c for c in valid_chats}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:
                    failed_count += 1
                    jid = futs[fut].get("remoteJid", "?")
                    logging.warning("[sync_remote_chats] failed for %s: %s", jid, exc)
        logging.info(
            "[sync_remote_chats] done: %d chats, %d raised an exception",
            len(valid_chats), failed_count,
        )

    # Backfill pacing.  Adaptive rather than a fixed schedule: WhatsApp Web
    # loads each chat's history into its store at its own pace, so how long the
    # whole account takes is not knowable up front.  Measured on a real 539-chat
    # account, one pass recovered 203 of 463 chats — a fixed five-pass schedule
    # would have abandoned the rest while they were still arriving.  So: retry
    # quickly while passes keep recovering chats, back off when one recovers
    # nothing, and stop at an overall budget so this can never poll forever.
    _BACKFILL_FIRST_DELAY = 30     # seconds before the first pass, and after any
                                   # pass that made progress
    _BACKFILL_MAX_DELAY   = 300    # ceiling once passes stop recovering anything
    _BACKFILL_BUDGET      = 45 * 60  # total wall-clock the backfill may run for
    # Renewal ceiling for a session whose history is still arriving — a fresh
    # pairing delivers and decodes for far longer than the ordinary budget.
    _BACKFILL_LANDING_BUDGET = 4 * 60 * 60
    # Deliberately below sync_remote_chats()'s 6 workers and capped per pass:
    # this is background history nobody is waiting on, so it must not add a
    # burst of automation traffic on top of the media phase.
    _BACKFILL_WORKERS     = 3
    _BACKFILL_CHUNK       = 60     # chats re-queried per pass

    @staticmethod
    def _server_claims_content(chat: dict) -> bool:
        """True when list-chats says this chat has history, whatever we fetched.

        `unreadCount` and `t` (last-activity timestamp) both come straight from
        WhatsApp Web's chat record and are populated even when its message store
        is not — which is exactly the state that needs a backfill.
        """
        try:
            if int(chat.get("unreadCount", 0) or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
        try:
            return int(chat.get("t", 0) or 0) > 0
        except (TypeError, ValueError):
            return False

    def history_page_target(self) -> int:
        """How many messages per chat a finished sync should leave behind.

        The same messages_page_size the conversation view pages by, so "synced"
        means "opening the conversation shows a full page without going back to
        the network".
        """
        try:
            return max(1, int(getattr(self, "settings", {})
                              .get("user_interface", {})
                              .get("messages_page_size", 200)))
        except (AttributeError, TypeError, ValueError):
            return 200

    def refresh_history_still_landing(self, context: str = "") -> bool:
        """Note whether more history is still on its way into WhatsApp Web.

        Everything about how hard the backfill should try depends on this. A
        chat that answers with 15 messages while history is still arriving is a
        chat whose history is *on its way*; the same 15 once everything has
        landed is simply a short conversation. Kept as an attribute because
        _note_backfill_state() runs on sync worker threads that must not each
        make their own status call.

        Two signals, because either alone has a blind spot:

          * unprocessedChunks > 0 — chunks delivered and waiting to be decoded.
          * initialSyncComplete is not true — a freshly paired session, where
            the phone has not even delivered the bulk of the history yet. This
            is the one that matters for a new pairing: at the moment the first
            sync runs there is often nothing in the queue *yet*, and reading
            only the chunk count would conclude that every short conversation
            is genuinely short at exactly the moment that is least true.

        Deliberately not keyed on `recentCompleted`: that flag stays false on a
        session whose recent sync was interrupted, and treating it as "still
        landing" would re-query every short chat on every launch forever.
        """
        status = self.fetch_history_sync_status()
        if status is None:
            # No endpoint / no answer. Assume settled: one wasted retry per
            # short chat is a far better failure than re-querying hundreds of
            # them for the whole backfill budget.
            self._history_still_landing = False
            return False
        unprocessed = status.get("unprocessedChunks")
        queued = isinstance(unprocessed, int) and unprocessed > 0
        first_sync = status.get("initialSyncComplete") is not True
        landing = queued or first_sync
        self._history_still_landing = landing
        logging.info(
            "[history-sync] %s: unprocessed=%s initial_complete=%s recent_complete=%s "
            "— short chats %s.",
            context or "check", unprocessed, status.get("initialSyncComplete"),
            status.get("recentCompleted"),
            "will be re-queried as history lands" if landing
            else "will get one confirming retry only",
        )
        return landing

    def _note_backfill_state(self, remote_jid: str, chat: dict, api_ok: bool) -> None:
        """Track chats that still owe us history.

        Two cases, and they used to be one. The original: get-messages reads
        WhatsApp Web's store, which right after pairing is empty for most chats,
        so it answers 200 with an empty list — indistinguishable from a
        genuinely empty conversation. Measured on a real 539-chat account, 514
        came back empty during the sync, and the sync never ran again because
        _sync_completed gates it. That left the list permanently missing
        history, unread badges clamped to zero by effective_unread_count(), and
        chats with no other identity dropped entirely.

        The second case is the same problem one step later: a chat that answers
        with *some* messages but fewer than a page. While WhatsApp Web is still
        decoding history-sync chunks, that number keeps growing for minutes
        after the sync ends — the account measured here went from 1.2k to 60k
        messages over about ten minutes — so a chat that returned 15 messages
        was not short, it was early. Those get re-queried too, until they either
        reach a full page or stop growing.

        Chats already holding a full page are done, and so is a short chat seen
        for the first time with the chunk queue already drained: get-messages
        reads the same store the chunks feed, so with nothing left to decode an
        immediate re-query returns the identical list. (Zero records is the
        exception and stays unconditional — that one really is "WhatsApp Web has
        not materialised this chat yet".) A chat that grew during the backfill
        keeps its slot for one more pass, so the ramp finishes, and is dropped
        the first pass that adds nothing.

        Called from sync_chat_messages() on its worker threads; set/dict item
        assignment is atomic under the GIL, so no lock is needed.
        """
        pending = getattr(self, "_chats_awaiting_messages", None)
        if pending is None:
            pending = self._chats_awaiting_messages = set()
        counts = getattr(self, "_partial_history_counts", None)
        if counts is None:
            counts = self._partial_history_counts = {}

        def _done():
            # Discard every address form: the chat may have been marked under
            # its @lid and re-synced under its phone JID (or the reverse), and
            # leaving the other form behind would retry it forever.
            for form in self._jid_address_forms(remote_jid):
                pending.discard(form)
                counts.pop(form, None)

        records = (chat.get("messages", {}).get("messages", {}).get("records")) or []
        if not api_ok:
            # The API never really answered — a failed call is the retry loop's
            # business, not the backfill's.
            _done()
            return
        if not records:
            if self._server_claims_content(chat):
                pending.add(remote_jid)
            else:
                _done()
            return

        count = len(records)
        # A full page used to retire the chat unconditionally, and that is the
        # one signal that most deserves a second look: the page is full
        # *because* the window saturated, which is exactly when there can be
        # more history behind it. sync_chat_messages() flags the chats where
        # that turned out to be true and its widened re-query still could not
        # reach stored history — those stay in the queue.
        gap = remote_jid in getattr(self, "_history_gap_jids", ())
        if count >= self.history_page_target() and not gap:
            _done()
            return

        forms = self._jid_address_forms(remote_jid)
        previous = next((counts[f] for f in forms if f in counts), None)
        grew = previous is not None and count > previous
        still_landing = getattr(self, "_history_still_landing", False)
        _done()
        # `gap and previous is None` queues a gap chat once; from the next pass
        # on it is kept only while it keeps growing, exactly like a short chat.
        # Without that the queue would never drain for a hole WhatsApp Web
        # cannot fill.
        if still_landing or grew or (gap and previous is None):
            counts[remote_jid] = count
            pending.add(remote_jid)

    def _jid_address_forms(self, jid: str) -> tuple:
        """The JID plus its counterpart across the @lid ↔ phone bridge.

        deduplicate_chats() re-keys self.chats from @lid to phone JIDs *after*
        sync_remote_chats() has run, so a JID recorded during the message sync
        can be absent from self.chats minutes later under a different address.
        """
        if not jid:
            return ()
        alt = (getattr(self, "_lid_to_phone", {}).get(jid)
               or getattr(self, "_phone_to_lid", {}).get(jid))
        return (jid, alt) if alt and alt != jid else (jid,)

    def _resolve_backfill_target(self, jid: str):
        """Live (key, chat) for a pending JID, or (None, None) if it is gone.

        Without this the backfill silently did nothing for individual chats:
        it recorded them under their @lid during the sync, deduplicate_chats()
        then re-keyed self.chats to phone JIDs, and every later `jid in
        self.chats` lookup missed. Only groups — which dedup never renames —
        were ever retried, which is why a real account sat at 399 visible
        conversations while pass after pass reported progress.
        """
        for form in self._jid_address_forms(jid):
            chat = self.chats.get(form)
            if chat is not None:
                return form, chat
        return None, None

    def _local_record_count(self, jid: str) -> int:
        """How many messages we currently hold for a chat, across both JID forms."""
        _key, chat = self._resolve_backfill_target(jid)
        if chat is None:
            return 0
        return len((chat.get("messages", {}).get("messages", {}).get("records")) or [])

    def _pending_name_resolution(self) -> list:
        """@lid chats still bridged to no phone number, i.e. still unnamed.

        Same rule _run_sync() uses, but callable again later. That single pass
        runs while WhatsApp Web is still warming up, so LIDs it could not map
        then stayed unmapped for the rest of the session and their chats kept
        showing a bare @lid or a raw phone number instead of a name.
        """
        resolved = getattr(self, "_lid_to_phone", {})
        unresolvable = getattr(self, "_unresolvable_lids", set())
        return [jid for jid in list(self.chats.keys())
                if jid.endswith("@lid") and jid not in resolved and jid not in unresolvable]

    def _backfill_names(self) -> int:
        """Retry name resolution for chats still lacking one. Returns how many
        got bridged this round."""
        pending = self._pending_name_resolution()
        if not pending:
            return 0
        before = len(getattr(self, "_lid_to_phone", {}))
        # Same chunk as the message backfill, and for the same reason: this all
        # funnels through the one Puppeteer page.
        logging.info("[backfill] Resolving names for %d unresolved @lid chat(s) "
                     "(%d still pending).", min(len(pending), self._BACKFILL_CHUNK), len(pending))
        self.resolve_lid_jids_via_api(pending[:self._BACKFILL_CHUNK])
        gained = len(getattr(self, "_lid_to_phone", {})) - before
        if gained > 0:
            self.chats = self.deduplicate_chats(self.chats)
            self._build_lid_to_phone_cache()
            logging.info("[backfill] Name resolution bridged %d new LID(s).", gained)
        return gained

    def _backfill_empty_chats(self):
        """Re-fetch messages for chats whose history WhatsApp Web had not loaded.

        Runs on its own daemon thread after the initial message sync. Each pass
        retries only the chats still missing history, so the work shrinks as the
        store warms up, and stops early once nothing is pending.

        On a freshly paired session this loop is also the only thing watching
        the history-sync queue. Nothing pushes decoded history to WinZapp:
        measured during a live drain, 93,000 messages entered WhatsApp Web's
        store and not one socket event came out of it — the messages exist only
        for whoever asks again. So each pass, while history is still landing,
        re-reads the queue and re-kicks it if needed.
        """
        my_run = getattr(self, "_sync_run_id", 0)
        deadline = time.monotonic() + self._BACKFILL_BUDGET
        # A fresh pairing can take longer to deliver and decode its history than
        # the ordinary budget allows, and stopping halfway leaves conversations
        # permanently short. So the deadline is renewed while history is
        # demonstrably still arriving — never past this ceiling, so a session
        # that never settles still ends.
        hard_deadline = time.monotonic() + self._BACKFILL_LANDING_BUDGET
        delay = self._BACKFILL_FIRST_DELAY
        attempt = 0
        attempted: set[str] = set()
        try:
            while time.monotonic() < deadline:
                # Sleep in slices so a shutdown or a newer sync is noticed
                # quickly instead of after the whole delay.
                for _ in range(delay):
                    if not self._ui_ready_event.is_set():
                        return
                    if getattr(self, "_sync_run_id", 0) != my_run:
                        logging.info("[backfill] A newer sync took over — stopping.")
                        return
                    time.sleep(1)

                if not getattr(self, "_wa_connected", False):
                    # Not a wasted pass: nothing was attempted, so just wait
                    # again rather than spending part of the budget on it.
                    logging.info("[backfill] Offline — waiting before retrying.")
                    continue

                attempt += 1
                # Re-read the queue once per pass (not once per chat): it is
                # what decides whether a chat that came back short is still owed
                # history or has simply told us everything it has.
                if self.refresh_history_still_landing(context=f"backfill pass {attempt}"):
                    # Two things can stall the queue, and both are silent: a
                    # chunk the processing loop will never accept parked at the
                    # head of it, and a loop that simply stopped scheduling
                    # itself (seen live at 20 of 22 chunks, with the last two
                    # sitting untouched). One call handles both, and no-ops when
                    # neither applies.
                    self.unblock_history_sync()
                    deadline = min(hard_deadline,
                                   max(deadline, time.monotonic() + self._BACKFILL_BUDGET))

                # Read the pending set *after* the queue check: a chat can join
                # it as history lands, and the sync that added it may have
                # finished while this pass was sleeping.
                pending = sorted(getattr(self, "_chats_awaiting_messages", set()))
                names_pending = self._pending_name_resolution()
                if not pending and not names_pending:
                    if getattr(self, "_history_still_landing", False):
                        # Nothing to re-query yet, but history is still arriving
                        # — keep the loop alive to notice when it does.
                        logging.info("[backfill] Nothing pending yet, but history is still "
                                     "landing — staying on watch.")
                        delay = self._BACKFILL_FIRST_DELAY
                        continue
                    logging.info("[backfill] Nothing pending — every chat has a full page "
                                 "of history (or all there is) and a name.")
                    return
                # Names get the same second chance as messages. _run_sync()
                # resolves LIDs exactly once, while WhatsApp Web is still warming
                # up, so anything it could not map then stayed a bare @lid or a
                # raw phone number for the whole session.
                named = self._backfill_names()
                if named:
                    wx.CallAfter(self._schedule_set_chats)

                if not pending:
                    # Only names were outstanding this round.
                    delay = (self._BACKFILL_FIRST_DELAY if named
                             else min(delay * 2, self._BACKFILL_MAX_DELAY))
                    continue

                before = len(pending)
                # Sweep by remembering what has been tried, not by advancing an
                # index: `pending` shrinks as chats recover, so index arithmetic
                # over it skipped entries outright — some chats were never
                # retried at all. Once every pending chat has had a turn, the
                # record clears and the next cycle begins.
                untried = [j for j in pending if j not in attempted]
                if not untried:
                    attempted.clear()
                    untried = pending
                window = untried[:self._BACKFILL_CHUNK]
                attempted.update(window)

                # Resolve through the @lid ↔ phone bridge: deduplicate_chats()
                # re-keys self.chats after the sync that recorded these JIDs.
                # The window is already capped at _BACKFILL_CHUNK — deliberately
                # gentler than sync_remote_chats(), because an unchunked pass
                # fired 463 get-messages calls in ~6 s through the one Puppeteer
                # page, on top of the media phase. None of this is urgent.
                targets, missing = [], []
                for j in window:
                    _key, chat = self._resolve_backfill_target(j)
                    if chat is None:
                        missing.append(j)
                    else:
                        targets.append(chat.copy())
                if missing:
                    # The chat is gone for good (deleted, or merged away) —
                    # stop asking about it.
                    logging.info("[backfill] Dropping %d pending JID(s) with no chat left.",
                                 len(missing))
                    for j in missing:
                        pending_set = getattr(self, "_chats_awaiting_messages", set())
                        pending_set.discard(j)
                if not targets:
                    continue
                # Chunked and deliberately gentler than sync_remote_chats().
                # An unchunked pass fired 463 get-messages calls in ~6 s through
                # the one Puppeteer page — on top of the media downloads running
                # in parallel — which is a lot of automation traffic for an
                # account WhatsApp is already watching.  Nothing here is urgent:
                # this is history the user is not looking at yet, so it costs
                # nothing to spread it out.
                logging.info("[backfill] Pass %d: retrying %d of %d chat(s) short of a "
                             "full page (%d msg).",
                             attempt, len(targets), before, self.history_page_target())
                # Snapshot what each target holds so progress can be measured in
                # messages, not just in chats that finished. A chat that went
                # from 15 to 90 messages made real progress and must not read as
                # a wasted pass — that is what backs the delay off.
                counts_before = {j: self._local_record_count(j) for j in window}
                with ThreadPoolExecutor(max_workers=self._BACKFILL_WORKERS) as pool:
                    futs = [pool.submit(self.sync_chat_messages, c) for c in targets]
                    for fut in as_completed(futs):
                        try:
                            fut.result()
                        except Exception as exc:
                            logging.warning("[backfill] chat sync failed: %s", exc)

                completed = before - len(getattr(self, "_chats_awaiting_messages", set()))
                grew = sum(1 for j, was in counts_before.items()
                           if self._local_record_count(j) > was)
                logging.info(
                    "[backfill] Pass %d: %d chat(s) gained messages, %d no longer pending "
                    "(of %d).", attempt, grew, completed, before)
                if grew > 0 or completed > 0 or named > 0:
                    # Unread badges, the "is this chat worth showing" decision and
                    # the displayed name all depend on this, so rebuild the list.
                    self._schedule_save()
                    wx.CallAfter(self._schedule_set_chats)
                    delay = self._BACKFILL_FIRST_DELAY
                else:
                    delay = min(delay * 2, self._BACKFILL_MAX_DELAY)
            still = len(getattr(self, "_chats_awaiting_messages", set()))
            unnamed = len(self._pending_name_resolution())
            if still or unnamed:
                logging.info(
                    "[backfill] Budget spent with %d chat(s) still short of a full page "
                    "and %d still unnamed — WhatsApp Web never resolved them this session.",
                    still, unnamed)
        except Exception:
            logging.exception("[backfill] Unhandled error in the backfill loop")

    # ── History-sync health ─────────────────────────────────────────────────
    # WhatsApp's multi-device design keeps older history on the phone and only
    # pushes a bounded window to a linked device; that window arrives as
    # "history sync" chunks which WhatsApp Web decodes inside a Web Worker.
    # When that worker's bridge fails to come up the chunks pile up untouched
    # and WhatsApp Web ends up holding roughly one message per chat — which
    # get-messages then reports faithfully, so from WinZapp's side the sync
    # looks like it worked and simply found short conversations. That is
    # exactly the shape of the "only 15-20 messages per group" reports.
    #
    # There is nothing WinZapp can do about it at runtime, so these two
    # methods only observe and log. What they buy is that the next report of
    # this comes with the answer already in log.log instead of needing a
    # CDP session against the live page to find.

    def fetch_history_sync_status(self, timeout: int = 30):
        """Raw /history-sync-status payload, or None when it can't be read."""
        if not getattr(self, "_wa_connected", False):
            return None
        url = (f"{self.wpp_server}:{self.wpp_port}"
               f"/api/{self.token}/history-sync-status")
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=timeout,
            )
            if response.status_code not in (200, 201):
                logging.warning("[history-sync] status endpoint returned %s: %s",
                                response.status_code, response.text[:200])
                return None
            body = response.json()
            return body.get("response") if isinstance(body, dict) else None
        except Exception as exc:
            # An older client/api/ build simply has no such route. Not worth a
            # warning on its own — the caller logs the miss once.
            logging.info("[history-sync] status endpoint unavailable: %s", exc)
            return None

    def log_history_sync_status(self, context: str = "") -> dict | None:
        """Log WhatsApp Web's history-sync health. Returns the payload."""
        status = self.fetch_history_sync_status()
        if not status:
            logging.info("[history-sync] No status available (%s).", context or "n/a")
            return None

        counts = status.get("storeCounts") or {}
        stored_msgs = counts.get("message")
        stored_chats = counts.get("chat")
        bridge_ready = status.get("backendWorkerBridgeReady")
        unprocessed = status.get("unprocessedChunks")

        logging.info(
            "[history-sync] %s: bridge_ready=%s persisted_storage=%s "
            "notification_api=%s unprocessed_chunks=%s wa_web_messages=%s "
            "wa_web_chats=%s on_demand=%s initial_complete=%s",
            context or "status", bridge_ready, status.get("persistedStorage"),
            status.get("notificationApi"), unprocessed, stored_msgs,
            stored_chats, status.get("onDemandEnabled"),
            status.get("initialSyncComplete"),
        )

        # The chunk map is the smoking gun when things are wrong: every entry
        # stuck at 'notification_stored' means nothing was ever decoded.
        chunk_status = status.get("chunkStatus")
        if isinstance(chunk_status, dict) and chunk_status:
            tally = {}
            for state in chunk_status.values():
                tally[state] = tally.get(state, 0) + 1
            logging.info("[history-sync] chunk states: %s", tally)

        if bridge_ready is False:
            logging.error(
                "[history-sync] WhatsApp Web's backend worker bridge is DOWN. "
                "History-sync chunks cannot be decoded, so WhatsApp Web itself "
                "holds almost no history (%s message(s) across %s chat(s)) and "
                "no sync, backfill or older-message request can recover it. "
                "This is an environment problem in the Chrome the API launches, "
                "not a WinZapp sync bug.", stored_msgs, stored_chats,
            )
        elif isinstance(unprocessed, int) and unprocessed > 0:
            logging.info(
                "[history-sync] %d chunk(s) still being decoded — history will "
                "keep growing for a while yet.", unprocessed,
            )
        return status

    def unblock_history_sync(self, timeout: int = 60) -> dict | None:
        """Free a history-sync queue stuck behind an unprocessable chunk.

        WhatsApp Web processes its notification queue by descending syncType,
        so an ON_DEMAND chunk always sorts ahead of every RECENT one — while
        being gated on the recent sync having *finished*. An on-demand request
        sent too early therefore parks a chunk at the head of a queue it can
        never leave, and the whole backlog behind it stops moving with no error
        anywhere. WinZapp used to create that state itself, from
        request_older_messages() during the initial sync; the API now refuses
        those, and this clears a queue already in that state.

        Safe to call unconditionally — the endpoint no-ops on a healthy queue.
        """
        if not getattr(self, "_wa_connected", False):
            return None
        url = (f"{self.wpp_server}:{self.wpp_port}"
               f"/api/{self.token}/unblock-history-sync")
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=timeout,
            )
            if response.status_code not in (200, 201):
                logging.warning("[history-sync] unblock endpoint returned %s: %s",
                                response.status_code, response.text[:200])
                return None
            body = response.json()
            payload = body.get("response") if isinstance(body, dict) else None
            if not isinstance(payload, dict):
                return None
            removed = payload.get("removed") or []
            if removed:
                logging.warning(
                    "[history-sync] Dropped %d on-demand chunk(s) that were "
                    "blocking %s pending recent chunk(s) and restarted the "
                    "processing loop. History should start filling in now.",
                    len(removed), payload.get("recentWaiting"),
                )
            else:
                logging.info(
                    "[history-sync] Queue not blocked (recent_completed=%s, "
                    "unprocessed=%s, on_demand_pending=%s).",
                    payload.get("recentCompleted"), payload.get("unprocessed"),
                    payload.get("onDemandPending"),
                )
            return payload
        except Exception as exc:
            # An older client/api/ build simply has no such route.
            logging.info("[history-sync] unblock endpoint unavailable: %s", exc)
            return None

    def request_older_messages(self, remote_jid: str, timeout: int = 60) -> bool:
        """Ask the phone for history older than what this device holds.

        Fire-and-forget by nature: the phone answers with a history-sync chunk
        minutes later, never in this response, so a True here means "the
        request went out", not "there are new messages now".

        The 60 s timeout is not generosity — a timeout here returns False, and
        the caller in fetch_older_messages() reads False as "the request never
        went out" and drops the chat into _exhausted_chats, which stops it from
        ever being re-queried this session. Timing out early therefore
        permanently writes off exactly the chats that still have history coming.
        """
        if not getattr(self, "_wa_connected", False):
            return False
        jid = self._normalize_jid(remote_jid)
        # Same @lid-preferred addressing sync_chat_messages() uses, so a chat
        # the store only knows under its @lid still resolves.
        lid = getattr(self, "_phone_to_lid", {}).get(jid, "")
        if lid:
            phone = lid
        elif jid.endswith("@s.whatsapp.net"):
            phone = jid.split("@")[0] + "@c.us"
        else:
            phone = jid

        url = (f"{self.wpp_server}:{self.wpp_port}"
               f"/api/{self.token}/request-older-messages/{phone}")
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                timeout=timeout,
            )
            body = {}
            try:
                body = response.json()
            except Exception:
                pass
            payload = body.get("response") if isinstance(body, dict) else None
            if response.status_code in (200, 201) and isinstance(payload, dict) \
                    and payload.get("requested"):
                logging.info(
                    "[history-sync] Requested older messages from the phone for "
                    "%s (primary_has_more=%s).", jid, payload.get("primaryHasMore"),
                )
                return True
            logging.info(
                "[history-sync] Older-message request for %s did not go out "
                "(status=%s, payload=%s).", jid, response.status_code,
                payload if payload else response.text[:200],
            )
            return False
        except Exception as exc:
            logging.warning(
                "[history-sync] Older-message request failed for %s: %s", jid, exc)
            return False

    def sync_media_for_all_chats(self) -> int:
        """Download every not-yet-stored media file across all chats.

        Returns the number of files **actually downloaded**, not the number of
        candidate messages considered. That distinction is the whole point: it
        used to return len(tasks), which counts every media message in the
        cache whether or not anything was fetched for it. When the app was
        offline, sync_if_media() returned at its first line for all of them —
        1579 "tasks" completed in 71 ms having downloaded nothing — and the
        caller, seeing a count above zero, announced "download de mídias
        concluído" to the user. See start_sync()'s Phase 2.
        """
        _MEDIA_TYPES = {"audioMessage", "documentMessage", "imageMessage",
                        "stickerMessage", "videoMessage",
                        "audio", "ptt", "document", "doc", "image", "sticker", "video"}
        tasks = [
            msg
            for chat in self.chats.values()
            for msg in chat.get("messages", {}).get("messages", {}).get("records", [])
            if (msg.get("messageType") in _MEDIA_TYPES or msg.get("type") in _MEDIA_TYPES)
        ]
        if not tasks:
            return 0

        downloaded = 0
        timeout = self._MEDIA_SYNC_TIMEOUT
        with ThreadPoolExecutor(max_workers=self._MEDIA_SYNC_WORKERS) as pool:
            futs = {pool.submit(self.sync_if_media, msg, timeout): msg for msg in tasks}
            for fut in as_completed(futs):
                try:
                    if fut.result():
                        downloaded += 1
                except Exception:
                    pass

        # Persist the set of expired IDs accumulated during this sync run.
        self._save_media_failed_ids()
        logging.info(
            "[sync_media_for_all_chats] Downloaded %d of %d candidate media message(s).",
            downloaded, len(tasks),
        )
        return downloaded

    def _refresh_open_conversation_after_sync(self, remote_jid: str, chat: dict) -> None:
        """Repaint the open conversation after sync_chat_messages() replaces
        its backing dict.

        sync_chat_messages() does `self.chats[remote_jid] = chat` — a NEW
        dict object, not a mutation of the old one. ConversationsPanel.
        conversation, if this chat happens to be open, is still holding that
        now-orphaned old object: its message list kept showing exactly what
        was loaded before this sync (e.g. before the app went offline) no
        matter how many messages this sync just merged in, until the user
        closed (Esc) and reopened the conversation to pick up
        self.chats[remote_jid] fresh. Point it at the new object and force a
        repaint instead — same pattern as the @lid-merge case elsewhere.

        Runs on a ThreadPoolExecutor worker thread (see sync_remote_chats()),
        so the swap-and-repaint is marshalled onto the main thread as one
        unit, re-checking the panel still has this jid open at that point
        (the user may have navigated away in the meantime).
        """
        cp = getattr(self, "conversations_panel", None)
        if cp is None or cp.conversation is None:
            return
        if cp.conversation.get("remoteJid") != remote_jid:
            return

        def _apply(new_chat=chat):
            if cp.conversation is not None and cp.conversation.get("remoteJid") == remote_jid:
                cp.conversation = new_chat
                cp.refresh_messages_if_changed()
        wx.CallAfter(_apply)

    # Multipliers applied to the page size when re-querying a chat whose newest
    # page turned out to be disjoint from stored history. The API pages
    # backwards on its own (deviceController.ts walks up to 10 extra pages
    # under one `count`), so each step here is a single request, not N.
    _HISTORY_GAP_FACTORS = (4, 10)
    # Ceiling for one gap-closing request, matching what the API can actually
    # deliver: its internal loop stops at 10 extra pages.
    _HISTORY_GAP_MAX_COUNT = 2000

    def _normalize_fetched_messages(self, raw_messages, remote_jid: str) -> list:
        """WPPConnect get-messages payload -> WinZapp's canonical message dicts."""
        out = []
        for wm in raw_messages or []:
            if isinstance(wm, dict) and self.ws:
                try:
                    normalized = self.ws._normalize_wpp_message(wm)
                    prune_message_record(normalized)
                    out.append(normalized)
                except Exception as e:
                    logging.error(f"[sync_chat_messages] Failed to normalize message in {remote_jid}: {e}")
        return out

    def _refetch_history_gap(self, remote_jid: str, phone: str, headers: dict,
                             page_size: int, local_records: list,
                             hole_top_ts: int = 0) -> list:
        """Re-query a chat with a wider window until it reaches stored history.

        Only called once history_gap_detected() has said the newest page is
        disjoint from what is on disk. Escalates `count` instead of walking
        page by page because the API already does the walking internally, and
        stops as soon as the widened window overlaps stored history — or as
        soon as the store stops yielding more, which is the honest signal that
        WhatsApp Web simply does not have the missing stretch (yet).

        Returns the widest set of messages it managed to fetch, or [] when the
        first widened request failed outright — never a *narrower* set than the
        caller already had.
        """
        widest = []
        best_len = page_size
        for factor in self._HISTORY_GAP_FACTORS:
            count = min(page_size * factor, self._HISTORY_GAP_MAX_COUNT)
            if count <= best_len:
                break
            url = (f"{self.wpp_server}:{self.wpp_port}/api/{self.token}"
                   f"/get-messages/{phone}?count={count}")
            try:
                # Deliberately longer than the 30s of the normal page: this
                # request makes WhatsApp Web walk back through up to ten pages.
                resp = requests.get(url, headers=headers, timeout=90)
            except Exception as exc:
                logging.warning("[history-gap] %s: request failed at count=%d: %s",
                                remote_jid, count, exc)
                break
            if resp.status_code not in (200, 201):
                logging.warning("[history-gap] %s: HTTP %s at count=%d",
                                remote_jid, resp.status_code, count)
                break
            try:
                body = resp.json()
            except Exception:
                break
            raw = body.get("response", []) if isinstance(body, dict) else []
            if not isinstance(raw, list) or len(raw) <= best_len:
                logging.info(
                    "[history-gap] %s: store returned %d at count=%d — no more history "
                    "available, leaving the gap for a later pass.",
                    remote_jid, len(raw) if isinstance(raw, list) else 0, count)
                break
            best_len = len(raw)
            widest = self._normalize_fetched_messages(raw, remote_jid)
            if history_gap_closed(widest, local_records, hole_top_ts):
                logging.info("[history-gap] %s: closed at count=%d (%d messages).",
                             remote_jid, count, len(widest))
                break
        return widest

    def sync_chat_messages(self, chat):
        remote_jid = self._normalize_jid(chat.get("remoteJid", ""))
        chat["remoteJid"] = remote_jid
        
        user_part = remote_jid.split("@")[0] if "@" in remote_jid else remote_jid
        if not user_part or user_part == "0" or len(user_part) < 5:
            logging.warning(f"[sync_chat_messages] Aborting sync for invalid JID: {remote_jid}")
            return
            
        # Formata o JID corretamente para o WPPConnect
        # Se houver mapeamento phone -> LID, usamos o LID.
        lid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
        if lid:
            phone = lid
        elif remote_jid.endswith("@s.whatsapp.net"):
            phone = remote_jid.split("@")[0] + "@c.us"
        else:
            phone = remote_jid

        limit = int(self.settings.get("user_interface", {}).get("messages_page_size", 200))
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-messages/{phone}?count={limit}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        # Always sync with WPPConnect API to ensure no messages are lost or missed due to stale lastMessage cache.

        all_messages = []
        api_ok = False
        # The JID form that actually answered. Starts as the one built above and
        # is corrected when the alternate-JID fallback below is what worked —
        # the history-gap re-query has to reuse the form the store recognises,
        # not the one that just 401'd.
        fetch_jid = phone
        # Skip API call entirely if session is known disconnected
        if getattr(self, "_wa_connected", False):
            max_retries = 3
            for attempt in range(max_retries):
                if not getattr(self, "_wa_connected", False):
                    logging.info(f"[sync_chat_messages] Connection lost during sync retry loop for {remote_jid}, aborting sync.")
                    break
                try:
                    logging.info(f"[sync_chat_messages] Querying URL: {url} for chat: {remote_jid} (attempt {attempt+1}/{max_retries})")
                    response = requests.get(url, headers=headers, timeout=30)
                    logging.info(f"[sync_chat_messages] URL: {url} returned status: {response.status_code}")

                    # Alternate JID query fallback (resolves 401/TypeError or Chat not found errors)
                    both_jid_forms_failed = False
                    if response.status_code not in (200, 201):
                        alternate_jid = ""
                        if remote_jid.endswith("@lid"):
                            resolved = getattr(self, "_lid_to_phone", {}).get(remote_jid, "")
                            if resolved:
                                alternate_jid = resolved.replace("@s.whatsapp.net", "@c.us")
                        else:
                            # `phone` (the JID actually just queried) is the @lid
                            # form whenever a phone->LID mapping exists — see the
                            # "usamos o LID" preference above. Re-deriving the
                            # alternate from the SAME map here reproduces that
                            # identical @lid and silently retries the exact URL
                            # that just failed (observed live: a chat whose @lid
                            # form WA-JS has no store entry for — "Chat not found
                            # for X@lid" — kept re-querying that same @lid on
                            # every retry AND on the later backfill pass, never
                            # once trying the @c.us form). Try the @c.us form
                            # first since that's guaranteed to differ from a
                            # lid-preferred primary; only fall back to a fresh
                            # phone->LID lookup when the primary wasn't the LID
                            # form to begin with (no mapping existed yet).
                            cus_form = (
                                remote_jid.split("@")[0] + "@c.us"
                                if remote_jid.endswith("@s.whatsapp.net")
                                else remote_jid
                            )
                            if cus_form != phone:
                                alternate_jid = cus_form
                            else:
                                alt_lid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
                                if alt_lid and alt_lid != phone:
                                    alternate_jid = alt_lid

                        if alternate_jid and alternate_jid != phone:
                            alt_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-messages/{alternate_jid}?count={limit}"
                            logging.info(f"[sync_chat_messages] Primary query failed. Retrying with alternate JID {alternate_jid}...")
                            try:
                                alt_response = requests.get(alt_url, headers=headers, timeout=30)
                                if alt_response.status_code in (200, 201):
                                    response = alt_response
                                    fetch_jid = alternate_jid
                                    logging.info("[sync_chat_messages] Fallback alternate JID query succeeded!")
                                else:
                                    both_jid_forms_failed = True
                            except Exception as alt_e:
                                logging.warning(f"[sync_chat_messages] Fallback alternate JID query failed: {alt_e}")
                                both_jid_forms_failed = True

                    if response.status_code in (200, 201):
                        body = response.json()
                        wpp_messages = body.get("response", []) if isinstance(body, dict) else []
                        logging.info(f"[sync_chat_messages] Fetched {len(wpp_messages)} messages from API for {remote_jid}")
                        if not isinstance(wpp_messages, list):
                            wpp_messages = []
                        all_messages.extend(
                            self._normalize_fetched_messages(wpp_messages, remote_jid))
                        api_ok = True
                        break
                    elif response.status_code in (401, 404, 500):
                        # 401 = "Error on open list" (Baileys not ready yet)
                        # 404 = session not active
                        # 500 = transient WPPConnect internal error
                        # All are retryable — wait briefly and try again. But
                        # WPPConnect flattens every internal exception (including
                        # a hard, non-transient one like "Chat not found for
                        # <jid>@lid" — the live session lost track of that chat
                        # entirely, no amount of retrying fixes it) into this
                        # same generic 401, so we can't tell hard failures apart
                        # from "session still warming up" by status code alone.
                        # If BOTH the primary and the only known alternate JID
                        # form have now failed twice, that's a strong enough
                        # signal to stop early — otherwise a handful of these
                        # permanently-broken chats can each burn a worker slot
                        # for over a minute, making the whole sync feel stuck.
                        logging.warning(f"[sync_chat_messages] Retryable error {response.status_code} for {remote_jid} (attempt {attempt+1}/{max_retries}): {response.text[:120]}")
                        if both_jid_forms_failed and attempt >= 1:
                            logging.warning(f"[sync_chat_messages] Giving up early for {remote_jid} — both JID forms failed on attempt {attempt+1}.")
                            break
                        if attempt < max_retries - 1:
                            sleep_time = min(5 * (attempt + 1), 20)
                            logging.info(f"[sync_chat_messages] Sleeping {sleep_time} seconds before attempt {attempt+2} for {remote_jid}...")
                            # Check connection repeatedly while sleeping
                            for _ in range(sleep_time):
                                if not getattr(self, "_wa_connected", False):
                                    break
                                time.sleep(1)
                        continue
                    else:
                        logging.error(f"[sync_chat_messages] API returned error status {response.status_code} for {remote_jid}: {response.text}")
                        break
                except Exception as e:
                    logging.error(f"[sync_chat_messages] failed to get messages for {remote_jid}: {e}")
                    break
        else:
            logging.info(f"[sync_chat_messages] Session disconnected, using cached messages for {remote_jid}")

        # ── History-gap repair ───────────────────────────────────────────────
        # The page above is always the newest `limit` messages and nothing
        # else, so a chat that outran that window while WinZapp was closed
        # comes back describing a stretch of time we have no messages for, and
        # nothing downstream ever asks for the middle: the merge below unions
        # two disjoint blocks, and _note_backfill_state() reads a full page as
        # proof the chat is complete. See history_gap_detected().
        gap_jids = getattr(self, "_history_gap_jids", None)
        if gap_jids is None:
            gap_jids = self._history_gap_jids = set()
        gap_jids.discard(remote_jid)
        if api_ok and all_messages:
            # Snapshot before the merge below folds the fetched page into it.
            gap_reference = (self.chats.get(remote_jid, {})
                             .get("messages", {})
                             .get("messages", {})
                             .get("records") or [])
            if history_gap_detected(all_messages, gap_reference, limit):
                # Ceiling of the hole: the oldest message the narrow page
                # reached. Everything stored below it is the far side we are
                # trying to get back down to.
                hole_top_ts = min(_message_ts(m) for m in all_messages)
                logging.warning(
                    "[history-gap] %s: newest page of %d is disjoint from %d stored "
                    "message(s) — widening the window.",
                    remote_jid, len(all_messages), len(gap_reference))
                wider = self._refetch_history_gap(
                    remote_jid, fetch_jid, headers, limit, gap_reference, hole_top_ts)
                if len(wider) > len(all_messages):
                    all_messages = wider
                if not history_gap_closed(all_messages, gap_reference, hole_top_ts):
                    # Still disjoint: WhatsApp Web has not decoded that stretch
                    # yet. Flag it so the backfill keeps the chat in its queue
                    # instead of retiring it for holding a full page.
                    gap_jids.add(remote_jid)
                    logging.warning(
                        "[history-gap] %s: gap still open after widening — queued "
                        "for the backfill.", remote_jid)

        # NOTE on "conversation cleared from the phone": there is deliberately
        # no automatic mirroring here.  The only local evidence would be
        # get-messages answering 200 with an empty list, and that is
        # indistinguishable from "WhatsApp Web has not loaded this chat's
        # history into its store yet" — which is routine right after pairing or
        # a reconnect.  Acting on it would silently destroy the user's local
        # history.  (list-chats cannot help either: WPPConnect serialises the
        # raw ChatModel with msgs:null, so it carries no last-message data at
        # all.)  A clear made on the phone therefore only reaches WinZapp when
        # the user clears the conversation here as well.

        # Drop messages the user cleared (older than the clear-chat cutoff) so a
        # cleared conversation does not silently repopulate on the next sync.
        if all_messages:
            all_messages = [m for m in all_messages
                            if not self._is_cleared_message(remote_jid, m)]

        # After fetching, update chat messages
        for msg in all_messages:
            self._extract_lid_mapping(msg)
        # Learn sender names from the synced history as well.  Without this,
        # every group message fetched by the initial sync had no resolvable
        # sender (its participant is usually a bare @lid), and only messages
        # arriving live afterwards ever got a name.
        if self._learn_sender_names_bulk(all_messages):
            self._schedule_save(contacts_dirty=True)
        # Preserve any messages received via WebSocket during this sync that
        # the API hasn't indexed yet (they arrived after the API snapshot).
        local_chat    = self.chats.get(remote_jid, {})
        local_records = (local_chat.get("messages", {})
                         .get("messages", {})
                         .get("records", []))
        if local_records:
            api_ids = {r.get("key", {}).get("id") for r in all_messages}
            extra   = [r for r in local_records
                       if r.get("key", {}).get("id") and
                          r.get("key", {}).get("id") not in api_ids
                          # Also apply the clear-chat cutoff here: local_records
                          # comes from the on-disk cache, which can still hold
                          # pre-clear messages if the app was closed before the
                          # debounced save after clear_chat_messages_local() ran.
                          # Without this check those stale records get merged
                          # right back in, making "clear chat" undone by the
                          # next sync / app restart.
                          and not self._is_cleared_message(remote_jid, r)]
            if extra:
                all_messages = all_messages + extra

        # Deduplicate: when the same message exists as both an API copy (real
        # WhatsApp ID) and a pending virtual copy (local UUID), keep the API
        # version and drop the pending one.  The hash-set approach below ensures
        # the first occurrence (API) survives, removing the pending dup.
        seen = set()
        deduped = []
        for m in all_messages:
            mid = m.get("key", {}).get("id", "")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            deduped.append(m)
        all_messages = deduped

        # Sort by timestamp so the conversation always shows the most recent
        # messages at the bottom. The user scrolls up to see older history.
        all_messages.sort(
            key=lambda m: int(
                m.get("messageTimestamp") or m.get("timestamp") or m.get("t") or 0
            )
        )

        # ── Late-arriving race-condition fix ─────────────────────────────────
        # on_historical_message() and on_new_message() run on the wx main thread
        # and may have inserted messages into self.chats[remote_jid] AFTER we
        # took the local_records snapshot above but BEFORE we write back below.
        # Do a second merge against the live chat to ensure none of those
        # messages are silently discarded by our final self.chats assignment.
        live_chat    = self.chats.get(remote_jid, {})
        live_records = (live_chat.get("messages", {})
                        .get("messages", {})
                        .get("records", []))
        if live_records:
            current_ids = {r.get("key", {}).get("id") for r in all_messages}
            late_extra  = [r for r in live_records
                           if r.get("key", {}).get("id") and
                              r.get("key", {}).get("id") not in current_ids
                              and not self._is_cleared_message(remote_jid, r)]
            if late_extra:
                all_messages = all_messages + late_extra
                # Re-sort to keep chronological order
                all_messages.sort(
                    key=lambda m: int(
                        m.get("messageTimestamp") or m.get("timestamp") or m.get("t") or 0
                    )
                )

        # Update records: accept API data only when it actually returned some
        # messages, or fall back to preserving whatever we have in memory.
        # An empty API response (200 OK with no messages) must NOT wipe the
        # cached records, otherwise conversations appear empty after sync.
        has_records = bool(chat.get("messages", {}).get("messages", {}).get("records"))
        if api_ok and all_messages:
            if "messages" not in chat:
                chat["messages"] = {}
            chat["messages"]["messages"] = {
                "total": len(all_messages),
                "pages": 1,
                "currentPage": 1,
                "records": all_messages
            }
        elif not has_records:
            if "messages" not in chat:
                chat["messages"] = {}

        self.chats[remote_jid] = chat
        self._note_backfill_state(remote_jid, chat, api_ok)

        # This replaces self.chats[remote_jid] with a NEW dict object rather
        # than mutating the old one in place — if the conversation is open
        # right now, ConversationsPanel.conversation is still holding that
        # now-orphaned old object, so its message list kept showing exactly
        # what was loaded before this sync (e.g. before the app went
        # offline) no matter how many messages this sync just merged in,
        # until the user closed (Esc) and reopened the conversation to pick
        # up self.chats[remote_jid] fresh. Point it at the new object and
        # force a repaint here, same as the @lid-merge case above.
        self._refresh_open_conversation_after_sync(remote_jid, chat)

        if not getattr(self, "_initial_sync_running", False):
            wx.CallAfter(self._schedule_set_chats)

        # Incremental DB save: write only this chat + its messages.
        # This replaces the old save_data(self.chats, ...) call which dumped the
        # ENTIRE state (O(N) writes per chat → O(N²) total during bulk sync).
        try:
            # Don't persist a chat whose message fetch failed and which has
            # no prior local records — that would write the chat-list summary
            # (including a nonzero unreadCount) with zero messages attached,
            # permanently showing "N unread" with an empty conversation on
            # every future restart. Leave it unsaved so the next full sync
            # retries it from scratch instead.
            if api_ok or has_records:
                self.db.upsert_chat(remote_jid, chat)
            if all_messages:
                self.db.insert_messages_batch(remote_jid, all_messages)
        except Exception as exc:
            logging.warning("[sync_chat_messages] incremental DB save failed for %s: %s",
                            remote_jid, exc)

    # ── Phone-side deletions/clears — active conversation only ──────────────
    # sync_chat_messages() above deliberately never removes anything: its
    # "extra"/"late_extra" merges exist specifically to protect messages that
    # arrived live but the API snapshot hasn't indexed yet, so reusing it here
    # would silently undo real deletions. Detecting a message (or a whole
    # conversation) that vanished from the phone needs its own comparison —
    # kept scoped to the conversation the user has open right now: diffing
    # every chat's messages against the server on every 60s poll would turn
    # one cheap GET into dozens/hundreds against a local API that is already
    # doing real work, for a benefit (a stale bubble the user probably
    # wouldn't notice) that doesn't justify the cost. For the open
    # conversation specifically, the cost is one extra GET per poll and the
    # payoff (not staring at a message that no longer exists, or a "cleared"
    # conversation that stays full until F5) is worth it.

    def _fetch_remote_message_ids(self, remote_jid: str) -> "set[str] | None":
        """Best-effort GET of the message IDs WPPConnect currently has for
        remote_jid. Returns None on ANY failure/ambiguity — a failed fetch
        must never be read as "the phone deleted everything". IDs are
        extracted via the same _normalize_wpp_message() sync_chat_messages()
        uses, so they compare equal to what's stored in key.id locally.
        """
        if not self.ws:
            return None
        lid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
        if lid:
            phone = lid
        elif remote_jid.endswith("@s.whatsapp.net"):
            phone = remote_jid.split("@")[0] + "@c.us"
        else:
            phone = remote_jid
        limit = int(self.settings.get("user_interface", {}).get("messages_page_size", 200))
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-messages/{phone}?count={limit}"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code not in (200, 201):
                return None
            body = response.json()
            wpp_messages = body.get("response", []) if isinstance(body, dict) else []
            if not isinstance(wpp_messages, list):
                return None
            ids = set()
            for wm in wpp_messages:
                if not isinstance(wm, dict):
                    continue
                try:
                    normalized = self.ws._normalize_wpp_message(wm)
                except Exception:
                    continue
                mid = normalized.get("key", {}).get("id", "")
                if mid:
                    ids.add(mid)
            return ids
        except Exception as e:
            logging.warning(f"[_fetch_remote_message_ids] failed for {remote_jid}: {e}")
            return None

    # Consecutive polls a conversation must look fully cleared server-side
    # (see _reconcile_active_conversation_with_remote) before it's actually
    # mirrored locally — a single valid-but-empty read is not enough.
    _REMOTE_CLEAR_CONFIRM_STRIKES = 3

    def _reconcile_active_conversation_with_remote(self):
        """Detect a phone-side clear or individual message deletions in
        whichever conversation is currently open, and mirror them locally.
        Called once per periodic-poll cycle (start_periodic_contacts_sync);
        a no-op — no HTTP call at all — whenever no conversation is open.
        """
        if not hasattr(self, "_remote_clear_strikes"):
            self._remote_clear_strikes = {}
        cp = getattr(self, "conversations_panel", None)
        if cp is None or cp.conversation is None:
            return
        remote_jid = self._normalize_jid(cp.conversation.get("remoteJid", ""))
        if not remote_jid or not getattr(self, "messages_set_completed", False):
            return
        chat = self.chats.get(remote_jid)
        if not chat:
            return
        records = chat.get("messages", {}).get("messages", {}).get("records", [])
        # _fetch_remote_message_ids() only asks WhatsApp Web for its last
        # `limit` messages (same messages_page_size setting) — comparing the
        # FULL local history against that limited remote window meant any
        # older local message, once a busy conversation pushed it past the
        # server's last-`limit` cutoff, looked "missing" and got deleted
        # locally even though it was never actually removed anywhere. This
        # was reported live as a message that had demonstrably been
        # delivered (visible to other group members) vanishing from
        # WinZapp's own local history shortly after being sent.
        limit = int(self.settings.get("user_interface", {}).get("messages_page_size", 200))
        recent_records = records[-limit:] if len(records) > limit else records

        # Also exclude anything sent/received in roughly the last two
        # minutes: WhatsApp Web's own /get-messages can lag behind a message
        # actually reaching the server by a few seconds, so a fetch that
        # hasn't caught up yet would otherwise flag a message as "missing"
        # (and delete it) purely because of that race, not a real deletion.
        _stable_cutoff = time.time() - 120

        def _is_stable(r: dict) -> bool:
            ts = r.get("messageTimestamp") or r.get("timestamp") or 0
            try:
                ts = int(ts)
            except (TypeError, ValueError):
                return False
            if ts > 1_000_000_000_000:
                ts //= 1000
            return bool(ts) and ts < _stable_cutoff

        local_ids = {
            r.get("key", {}).get("id") for r in recent_records
            if isinstance(r, dict) and not r.get("_local_pending")
            and r.get("key", {}).get("id") and _is_stable(r)
        }
        # Too little history for "the server has fewer messages" to mean
        # anything other than "this is just a short conversation".
        if len(local_ids) < 2:
            return
        remote_ids = self._fetch_remote_message_ids(remote_jid)
        if remote_ids is None:
            return
        missing_ids = local_ids - remote_ids
        if not missing_ids:
            self._remote_clear_strikes.pop(remote_jid, None)
            return
        if missing_ids == local_ids:
            # Every local message is gone server-side — a clear, not a
            # handful of individually deleted messages. Require this to hold
            # for _REMOTE_CLEAR_CONFIRM_STRIKES consecutive polls before
            # actually wiping anything: _fetch_remote_message_ids() returning
            # a valid-but-empty list (as opposed to None, which already bails
            # out above) is indistinguishable from a real clear, but can also
            # come from a transient server-side hiccup — reported live as an
            # actively-open group conversation briefly clearing to "no
            # messages available" mid-read, only to "recover" once a new
            # live message forced a repaint. A single bad read must never be
            # enough to nuke a conversation's entire visible history.
            strikes = self._remote_clear_strikes.get(remote_jid, 0) + 1
            self._remote_clear_strikes[remote_jid] = strikes
            if strikes < self._REMOTE_CLEAR_CONFIRM_STRIKES:
                logging.info(
                    "[_reconcile_active_conversation_with_remote] %s looks fully "
                    "cleared server-side (strike %d/%d) — waiting for confirmation.",
                    remote_jid, strikes, self._REMOTE_CLEAR_CONFIRM_STRIKES,
                )
                return
            self._remote_clear_strikes.pop(remote_jid, None)
            wx.CallAfter(self._mirror_remote_clear, remote_jid)
        else:
            self._remote_clear_strikes.pop(remote_jid, None)
            wx.CallAfter(self._mirror_remote_deletions, remote_jid, missing_ids)

    def _mirror_remote_clear(self, remote_jid: str):
        """Mirror a conversation cleared on the phone. Runs on the main thread."""
        cp = getattr(self, "conversations_panel", None)
        # Re-check the conversation is still the one open and still looks
        # cleared — time passed between the background fetch and this
        # CallAfter actually running (user could have switched away, or a
        # new message could have arrived in the meantime).
        if cp is None or cp.conversation is None:
            return
        if self._normalize_jid(cp.conversation.get("remoteJid", "")) != remote_jid:
            return
        logging.info("[_mirror_remote_clear] %s appears cleared on the phone — mirroring locally.", remote_jid)
        # record_cutoff=False: this isn't a cutoff WE are choosing to
        # remember, the server is already the source of truth going forward.
        self.clear_chat_messages_local(remote_jid, record_cutoff=False)
        cp.conversation = self.chats.get(remote_jid, cp.conversation)
        cp.populate_messages()
        self._schedule_set_chats()

    def _mirror_remote_deletions(self, remote_jid: str, msg_ids: set):
        """Mirror one or more messages deleted on the phone from the
        currently open conversation. Runs on the main thread."""
        cp = getattr(self, "conversations_panel", None)
        if cp is None or cp.conversation is None:
            return
        if self._normalize_jid(cp.conversation.get("remoteJid", "")) != remote_jid:
            return
        logging.info("[_mirror_remote_deletions] %d message(s) in %s no longer on the phone — removing locally.",
                     len(msg_ids), remote_jid)
        cp.remove_messages_by_id(msg_ids, focus_previous=True)

    # WhatsApp CDN URLs (mmg.whatsapp.net) expire after ~90 days.  Attempting
    # to download older media causes the WPPConnect to enter a 5-second retry
    # loop for every expired URL, which starves the API thread pool and eventually
    # breaks sends.  Never request media older than this threshold.
    _MEDIA_MAX_AGE_SECONDS = 14 * 24 * 3600  # 14 days — WhatsApp CDN typical TTL
    _MEDIA_SYNC_WORKERS    = 1               # parallel workers during bulk sync — kept
                                              # low because WPPConnect proxies every
                                              # request through a single Puppeteer/Chrome
                                              # automation session; too many concurrent
                                              # downloads were starving unrelated requests
                                              # (send-seen, contact lookups) into sporadic
                                              # "session is not active" / "chat not found"
                                              # failures even though the session was fine.
    _MEDIA_SYNC_TIMEOUT    = 60              # seconds per request during bulk sync

    # A message this large gets skipped by the automatic background sync
    # (sync_if_media) instead of being downloaded eagerly. WPPConnect base64-
    # encodes the whole file inside its Node/Puppeteer process before ever
    # handing it back over HTTP — for a ~1 GB document sent into a group this
    # was observed pushing node.exe's memory usage past 5 GB and hanging the
    # machine. The user can still explicitly open/download an oversized file
    # from the conversation view (_on_action_open/_on_action_download in
    # conversations.py) — only the unattended background pass is capped.
    _MEDIA_AUTO_DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024   # 100 MB — fallback only,
                                                          # see _media_max_download_bytes()

    def _media_max_download_days(self) -> int:
        """User-configurable cap (Settings > Armazenamento) on how old a
        message can be and still have its media auto-downloaded. 0 means
        unlimited (still subject to the hard _MEDIA_MAX_AGE_SECONDS CDN-TTL
        floor above, which is not user-configurable — downloading past that
        point fails regardless of what the user asked for)."""
        try:
            return int(self.settings.get("storage", {}).get("media_max_days", 30))
        except (TypeError, ValueError):
            return 30

    def _media_max_download_bytes(self) -> int:
        """User-configurable cap (Settings > Armazenamento) on individual
        media file size for auto-download. 0 means unlimited."""
        try:
            mb = int(self.settings.get("storage", {}).get("media_max_mb", 100))
        except (TypeError, ValueError):
            mb = 100
        return mb * 1024 * 1024 if mb > 0 else 0

    def _load_media_failed_ids(self) -> dict:
        """Load {message_id: failed_at_timestamp} for media whose CDN URL has
        previously expired (403/410) — checked by sync_if_media() to skip a
        pointless repeat download attempt.

        This was a bare set with no eviction, growing forever and persisted
        across every restart (data/media_failed.json) — for an account with
        a lot of old/expired media, a genuine unbounded-growth source. Every
        entry is provably dead weight once its message is older than
        _MEDIA_MAX_AGE_SECONDS anyway: sync_if_media()'s own age check skips
        it before ever consulting this set, so there is nothing lost by
        pruning entries past that point — they can never be looked up again.
        """
        try:
            with open(data_path("media_failed.json"), "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            return {}
        now = time.time()
        if isinstance(raw, dict):
            return {
                mid: ts for mid, ts in raw.items()
                if isinstance(ts, (int, float)) and (now - ts) <= self._MEDIA_MAX_AGE_SECONDS
            }
        if isinstance(raw, list):
            # Legacy format (plain list from before this became a dict) —
            # no timestamp to judge age by, so treat every entry as freshly
            # failed rather than either keeping stale ones forever or
            # discarding real, still-useful skip-hints outright.
            return {mid: now for mid in raw if isinstance(mid, str)}
        return {}

    def _save_media_failed_ids(self):
        """Persist the failed-media map so expired IDs are skipped on future launches."""
        with self._media_failed_lock:
            try:
                with open(data_path("media_failed.json"), "w", encoding="utf-8") as f:
                    json.dump(self._media_failed_ids, f)
            except Exception:
                pass

    def _is_conversation_open_for(self, msg) -> bool:
        """True if msg belongs to the conversation currently shown on screen."""
        cp = getattr(self, "conversations_panel", None)
        if cp is None or getattr(cp, "conversation", None) is None:
            return False
        open_jid = cp.conversation.get("remoteJid", "")
        if not open_jid:
            return False
        key = msg.get("key", {})
        msg_jid = self._normalize_jid(key.get("remoteJid", ""))
        return msg_jid == self._normalize_jid(open_jid)

    def sync_if_media(self, msg, timeout=60):
        """Download media for a single message during the background sync phase.

        Returns True only when a file was actually downloaded. Every skip
        below — offline, not a media message, past the CDN TTL, past the
        user's day/size caps, a known-expired id, already on disk — returns
        False, so sync_media_for_all_chats() can count real work rather than
        candidates.
        """
        if not getattr(self, "_wa_connected", False) or getattr(self, "offline_mode", False):
            return False
        message_type = msg.get("messageType", "")
        if not message_type and msg.get("type"):
            t = str(msg.get("type"))
            if t in ("audio", "ptt"):
                message_type = "audioMessage"
            elif t == "image":
                message_type = "imageMessage"
            elif t == "video":
                message_type = "videoMessage"
            elif t in ("document", "doc"):
                message_type = "documentMessage"
            elif t == "sticker":
                message_type = "stickerMessage"

        _MEDIA_TYPES = {"documentMessage", "imageMessage", "stickerMessage", "videoMessage"}
        if message_type not in _MEDIA_TYPES and message_type != "audioMessage":
            return False

        # Skip messages older than the CDN TTL — URLs have certainly expired.
        ts = int(msg.get("messageTimestamp", 0) or 0)
        if ts and (time.time() - ts) > self._MEDIA_MAX_AGE_SECONDS:
            return False

        # User-configurable age cap (Settings > Armazenamento > "Baixar
        # mídias de até (dias)"). 0 means unlimited — falls back to whatever
        # the CDN-TTL check above already allows.
        max_days = self._media_max_download_days()
        if ts and max_days > 0 and (time.time() - ts) > max_days * 86400:
            return False

        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id or "-" in msg_id or msg.get("_local_pending"):
            return False

        # Skip IDs that previously returned 403/410 (expired CDN URL).
        if msg_id and msg_id in self._media_failed_ids:
            return False

        # Skip oversized files during the automatic background sync — see
        # _media_max_download_bytes() (Settings > Armazenamento > "Baixar
        # mídias de até no máximo (mb)"; 0 = unlimited).
        max_bytes = self._media_max_download_bytes()
        msg_inner = msg.get("message")
        if isinstance(msg_inner, str):
            try:
                msg_inner = json.loads(msg_inner)
            except Exception:
                msg_inner = None
        inner = msg_inner.get(message_type) if isinstance(msg_inner, dict) else None
        if max_bytes and isinstance(inner, dict):
            try:
                file_length = int(inner.get("fileLength") or 0)
            except (TypeError, ValueError):
                file_length = 0
            if file_length > max_bytes:
                logging.info(
                    "[sync_if_media] Skipping auto-download of %s (%s, %.1f MB > %.0f MB limit)",
                    msg_id, message_type, file_length / (1024 * 1024),
                    max_bytes / (1024 * 1024),
                )
                return False

        try:
            if message_type == "audioMessage":
                return bool(self.handle_audio_message(msg, timeout=timeout))
            else:
                # Bulk background sync: download WITHOUT per-chunk progress
                # callbacks. Streaming 64 KB chunks across 6 workers used to fire
                # a wx.CallAfter per chunk per file — tens of thousands of UI
                # events, each doing an O(n) scan of the open conversation —
                # which froze the app while media downloaded. Only refresh the
                # row once, and only when its chat is the conversation currently
                # on screen.
                downloaded = self.handle_media_message(
                    msg, progress_callback=None, timeout=timeout)
                if msg_id and self._is_conversation_open_for(msg):
                    conv = self.conversations_panel
                    wx.CallAfter(conv.update_message_download_progress, msg_id, 1.0)
                return bool(downloaded)
        except MediaExpiredError:
            if msg_id:
                self._media_failed_ids[msg_id] = time.time()
        except Exception:
            pass
        return False

    def handle_media_message(self, msg, progress_callback=None, timeout=60):
        """Download and encrypt a document/image/sticker/video to data/media/.

        Returns True only when this call actually wrote a new file — every
        other path (no id, already on disk, not connected, empty response)
        returns False. sync_media_for_all_chats() counts those return values
        to report how much was really downloaded; see its own docstring.
        """
        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id:
            return False
        if "_" in msg_id:
            parts = msg_id.split("_")
            msg_id = parts[2] if len(parts) > 2 else parts[-1]
        media_path = data_path("media", f"{msg_id}.wzmedia")
        if os.path.isfile(media_path):
            return False
        if not getattr(self, "_wa_connected", False):
            # Covers both "confirmed offline" and "still connecting at
            # startup" (_wa_connected only flips True once the connection is
            # actually verified — see _set_wa_connected) — attempting the
            # HTTP call in either case just burns the request timeout against
            # an API that cannot possibly answer yet, and previously surfaced
            # as a generic "could not download this media file" instead of
            # something that tells the user to wait for the connection.
            logging.info("[handle_media_message] Skipping download for %s — not connected.", msg_id)
            return False
        b64 = self.get_base64_from_media(msg, progress_callback=progress_callback,
                                         timeout=timeout)
        if not b64:
            return False
        content = base64.b64decode(b64)
        encrypted = encrypt(content, self.key)
        with open(media_path, "wb") as f:
            f.write(encrypted)
        return True

    def _check_wa_connection_closed(self, response) -> bool:
        """Detect a response that means "WhatsApp is not connected".

        Two shapes matter:

        * HTTP 404 with ``{"status": "Disconnected"}`` — WPPConnect's
          statusConnection middleware answers this for *every* route (send,
          list-chats, …) whenever ``isConnected()`` is false, i.e. whenever the
          machine has no internet.  It is the single most reliable offline
          signal the API gives us.
        * a 'Connection Closed' error message from Baileys.

        Marks the connection as down (which pauses the MessageQueue and turns
        on automatic offline mode) and returns True when either is seen.
        """
        disconnected = False
        try:
            body = response.json()
        except Exception:
            body = {}
        try:
            if response.status_code == 404 and isinstance(body, dict):
                if str(body.get("status", "")).lower() == "disconnected":
                    disconnected = True
            if response.status_code in (500, 502, 503) and isinstance(body, dict):
                err_obj = body.get("error", {})
                err_name = str(err_obj.get("name", "")) if isinstance(err_obj, dict) else ""
                if "TargetCloseError" in err_name or "ProtocolError" in err_name or "TargetCloseError" in str(body):
                    disconnected = True
            if isinstance(body, dict):
                messages = body.get("response", {})
                messages = messages.get("message", []) if isinstance(messages, dict) else []
                if any("Connection Closed" in str(m) for m in messages):
                    disconnected = True
        except Exception:
            pass
        if disconnected:
            logging.warning("[send] WhatsApp reported Disconnected or TargetCloseError — pausing queue and triggering session recovery")
            self._set_wa_connected(False, "API answered Disconnected or TargetCloseError")
            # Proactively schedule connection check to auto-recover session via HTTP
            wx.CallAfter(self.check_wa_connection_http)
        return disconnected

    def _classify_send_exception(self, exc, where: str) -> dict:
        """Turn a transport-level send failure into a queue instruction.

        A read timeout or a dropped connection is **not** evidence that the
        message was not sent: WPPConnect drives WhatsApp Web, which accepts an
        outgoing message into its own outbox and flushes it as soon as the
        phone/network is back.  Retrying such a send is what produced the
        reported "30 copies of the same message arrive when the internet comes
        back" — every retry queued another genuine copy inside WhatsApp Web.

        So these are reported as *ambiguous*: the queue drops the message
        instead of resending it, and the WebSocket echo of the real send (which
        is matched against the pending virtual message) resolves the UI if and
        when WhatsApp actually delivers it.
        """
        err = str(exc)[:200]
        ambiguous = isinstance(exc, (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ))
        logging.error("[%s] request exception (ambiguous=%s): %s", where, ambiguous, err)
        if ambiguous:
            return {"ok": False, "error": err, "retry": False, "ambiguous": True}
        return {"ok": False, "error": err, "retry": True}

    def _serialize_quoted_id(self, quoted: dict, fallback_jid: str = None) -> str:
        """Serialize a quoted message key into the format expected by WPPConnect.

        Delegates to _serialize_msg_id, which keeps whatever JID variant
        (@lid or phone) the message was actually keyed under in WPPConnect's
        internal store — rewriting @lid to phone here makes the lookup miss
        and the reply fail (same root cause as the media-download bug).
        """
        if not quoted or not isinstance(quoted, dict):
            return None
        raw_key = quoted.get("key", {})
        if not isinstance(raw_key, dict) or not raw_key.get("id"):
            return None
        # key.remoteJid can be empty for own messages in local cache, fallback to current conversation JID
        remote_jid = raw_key.get("remoteJid") or fallback_jid or ""
        # Swap self-JID with fallback_jid (the other person in the 1-on-1 chat) to prevent WPPConnect lookup fail
        if self._is_self_jid(remote_jid) and fallback_jid:
            remote_jid = fallback_jid
        return self._serialize_msg_id(remote_jid, raw_key)

    def _canonical_mention_jids(self, mentioned_jids):
        """Return mention JIDs in the phone-number format Baileys/WPPConnect can tag."""
        out = []
        seen = set()
        lid_to_phone = getattr(self, "_lid_to_phone", {})
        for raw_jid in mentioned_jids or []:
            jid = self._normalize_jid(str(raw_jid or ""))
            if not jid:
                continue
            if jid.endswith("@lid"):
                jid = lid_to_phone.get(jid, jid)
            if jid not in seen:
                seen.add(jid)
                out.append(jid)
        return out

    def _resolve_jid_for_send(self, jid: str) -> str:
        """
        Destination JID for the WPPConnect *send* endpoints: @lid whenever the
        cache knows one, otherwise the @c.us phone form.

        This deliberately prefers @lid, same policy as
        _resolve_jid_for_chat_state (typing/presence) and as the message-key
        serialization in _serialize_msg_id — WhatsApp Web keys the chat, and
        every message in it, under the @lid once the account is on LID
        addressing, and reports the phone JID only as the chat's legacy
        `historyChatId`. Sending to that legacy address does NOT fail loudly:
        WhatsApp Web creates the message, hands back a real 3EB0… id (so the
        HTTP call looks like a success) and then never gets it acked by the
        server — ack stays 0/CLOCK, the message sits in the browser's outbox
        forever and reaches neither the phone nor the recipient. Groups and
        broadcast lists keep their own address, which is canonical everywhere.

        The @c.us form is still used, as a *fallback*, by every send method
        whenever the @lid destination is refused with a definite HTTP error
        (the pre-LID behaviour, which existed because a @lid chat that
        Puppeteer has not loaded yet answers 400 "o número não existe"); see
        _legacy_phone_for_send.
        """
        return self._resolve_jid_for_chat_state(jid)

    def _legacy_phone_for_send(self, jid: str) -> str:
        """Legacy @c.us address to retry a failed send on, or '' when there is none.

        Only meaningful for private chats: groups/broadcast lists have a single
        canonical address and nothing to fall back to.
        """
        if not jid or jid.endswith(("@g.us", "@broadcast")):
            return ""
        if jid.endswith("@lid"):
            phone_net = getattr(self, "_lid_to_phone", {}).get(jid, "")
            return phone_net.replace("@s.whatsapp.net", "@c.us") if phone_net else ""
        return jid.replace("@s.whatsapp.net", "@c.us")

    def _resolve_jid_for_msg_key(self, jid: str) -> str:
        """
        Phone/@c.us form of a JID, translating @lid back through the cache.

        Kept separate from _resolve_jid_for_send: fetch_older_messages uses this
        both as the /get-messages/:phone URL parameter and as the chat segment of
        the serialized message id it asks for, and that pair has to stay on the
        phone form — see the comment at its call site.
        """
        if not jid:
            return jid
        if jid.endswith(("@g.us", "@broadcast")):
            return jid
        if jid.endswith("@lid"):
            phone_net = getattr(self, "_lid_to_phone", {}).get(jid, jid)
            if phone_net:
                return phone_net.replace("@s.whatsapp.net", "@c.us")
            return jid
        if jid.endswith("@s.whatsapp.net"):
            return jid.replace("@s.whatsapp.net", "@c.us")
        return jid



    def send_text_message(self, remote_jid, text, quoted=None, mentioned_jids=None):
        """Send a plain-text message via the WPPConnect Server API."""
        # Canonical destination: @lid when known, else the @c.us phone form —
        # see _resolve_jid_for_send's docstring for why @lid has to win here.
        remote_jid = self._resolve_jid_for_send(remote_jid)
        is_lid_target = remote_jid.endswith("@lid")
        logging.info("[send_text_message] destination resolved to %s (isLid=%s)", remote_jid, is_lid_target)

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        quoted_id = None

        if mentioned_jids:
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-mentioned"
            phone_net = remote_jid
            if phone_net.endswith("@s.whatsapp.net"):
                phone_net = phone_net.replace("@s.whatsapp.net", "@c.us")
            
            mentioned = self._canonical_mention_jids(mentioned_jids)
            mentioned_clean = [m.replace("@s.whatsapp.net", "@c.us") if m.endswith("@s.whatsapp.net") else m for m in mentioned]
            
            payload = {
                "phone": [phone_net],
                "message": text,
                "mentioned": mentioned_clean,
                "isGroup": phone_net.endswith("@g.us"),
                "isLid": is_lid_target,
                "options": {
                    "linkPreview": False
                }
            }
        else:
            quoted_id = self._serialize_quoted_id(quoted, fallback_jid=remote_jid) if quoted else None
            if quoted_id:
                url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-reply"
                phone_net = remote_jid
                if phone_net.endswith("@s.whatsapp.net"):
                    phone_net = phone_net.replace("@s.whatsapp.net", "@c.us")
                payload = {
                    "phone": [phone_net],
                    "message": text,
                    "messageId": quoted_id,
                    "isGroup": phone_net.endswith("@g.us"),
                    "isLid": is_lid_target,
                    "options": {
                        "linkPreview": False
                    }
                }
                logging.debug("[send_text_message] sending quoted reply via send-reply to %s, quoted key.id=%s", phone_net, quoted_id)
            else:
                phone_net = remote_jid
                if phone_net.endswith("@s.whatsapp.net"):
                    phone_net = phone_net.replace("@s.whatsapp.net", "@c.us")
                url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-message"
                payload = {
                    "phone": [phone_net],
                    "message": text,
                    "isGroup": phone_net.endswith("@g.us"),
                    "isLid": is_lid_target,
                    "options": {
                        "linkPreview": False
                    }
                }
        try:
            # 25s (not 15s): WPPConnect can take longer to ack under load (e.g.
            # concurrent media sync). A client-side timeout here is indistinguishable
            # from a real failure to MessageQueue, which then retries — if the
            # original request actually went through server-side, that retry sends
            # a genuine duplicate message to the recipient. A more generous timeout
            # reduces how often that false-timeout/duplicate-send scenario happens.
            response = requests.post(url, json=payload, headers=headers, timeout=25)
            active_dest = phone_net
            if response.status_code not in (200, 201):
                # 1. The @lid destination was refused: fall back to the legacy
                #    @c.us address. Historically this fired only on the 400
                #    "o número não existe" that a @lid chat Puppeteer has not
                #    loaded yet answers with, but any definite 4xx/5xx on a @lid
                #    destination is worth one legacy attempt — that address is
                #    what WinZapp used before and it still works for chats
                #    WhatsApp has not moved to LID addressing.
                #    Skipped when the API reports the session as disconnected:
                #    nothing was sent, and the message must stay queued as-is
                #    instead of burning a retry (see MessageQueue).
                fb_phone = self._legacy_phone_for_send(remote_jid) if is_lid_target else ""
                if fb_phone and not self._check_wa_connection_closed(response):
                    logging.warning(
                        "[send_text_message] @lid destination %s refused (HTTP %s: %s) — retrying with legacy %s",
                        remote_jid, response.status_code, response.text[:200], fb_phone,
                    )
                    if mentioned_jids:
                        retry_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-mentioned"
                        retry_payload = {
                            "phone": [fb_phone], "message": text,
                            "mentioned": mentioned_clean,
                            "isGroup": fb_phone.endswith("@g.us"),
                            "isLid": False,
                            "options": {"linkPreview": False}
                        }
                    elif quoted_id:
                        retry_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-reply"
                        retry_payload = {
                            "phone": [fb_phone], "message": text,
                            "messageId": quoted_id, "isGroup": fb_phone.endswith("@g.us"),
                            "isLid": False,
                            "options": {"linkPreview": False}
                        }
                    else:
                        retry_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-message"
                        retry_payload = {
                            "phone": [fb_phone], "message": text,
                            "isGroup": fb_phone.endswith("@g.us"),
                            "isLid": False,
                            "options": {"linkPreview": False}
                        }
                    active_dest = fb_phone
                    response = requests.post(retry_url, json=retry_payload, headers=headers, timeout=25)
                    if response.status_code in (200, 201):
                        logging.info("[send_text_message] legacy retry with %s succeeded", fb_phone)

                # 2. If it's still failing and we had a quote, strip the quote and
                #    try a plain send to whichever address we last used.
                if response.status_code not in (200, 201) and quoted_id:
                    logging.warning("[send_text_message] Quoted send failed (HTTP %s). Retrying without quote on %s...",
                                    response.status_code, active_dest)
                    wx.CallAfter(self.output, self.i18n.t("reply_quote_lost"))
                    url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-message"
                    payload = {
                        "phone": [active_dest],
                        "message": text,
                        "isGroup": active_dest.endswith("@g.us"),
                        "isLid": active_dest.endswith("@lid"),
                        "options": {
                            "linkPreview": False
                        }
                    }
                    response = requests.post(url, json=payload, headers=headers, timeout=25)

                # 3. Final error handling if all retries failed
                if response.status_code not in (200, 201):
                    err = f"HTTP {response.status_code}: {response.text[:300]}"
                    logging.error("[send_text_message] All send attempts failed: %s for %s", err, remote_jid)
                    if self._check_wa_connection_closed(response):
                        # WhatsApp is down: the message was definitely NOT sent,
                        # so it stays queued — but never retried in a loop while
                        # the connection is out (see MessageQueue).
                        return {"ok": False, "error": err, "retry": False, "disconnected": True}
                    # If it's a transient error, mark retryable
                    is_retryable = response.status_code in (408, 429, 500, 502, 503, 504)
                    return {"ok": False, "error": err, "retry": is_retryable}


            self._set_wa_connected(True, "send succeeded")
            try:
                body = response.json()
                # WPPConnect retorna a resposta dentro de 'response'
                resp = body.get("response", {})
                if isinstance(resp, list) and len(resp) > 0:
                    resp = resp[0]
                if isinstance(resp, dict):
                    msg_id = resp.get("id")
                    if isinstance(msg_id, dict):
                        msg_id = msg_id.get("_serialized", "")
                    parts = msg_id.split("_") if msg_id else []
                    clean_id = parts[2] if len(parts) > 2 else (parts[-1] if parts else msg_id)
                    return clean_id or True
                return True
            except Exception:
                return True
        except Exception as exc:
            return self._classify_send_exception(exc, "send_text_message")

    @staticmethod
    def _find_api_ffmpeg() -> str:
        """Locate ffmpeg binary: check bundled lib/ first, then node_modules, then system PATH."""
        import glob as _glob
        import shutil
        # 1. Check bundled lib/ directory first (client/lib in dev mode, lib/ in compiled mode)
        lib_dirs = [
            resource_path("lib"),
            resource_path("client", "lib"),
            os.path.join(os.path.dirname(__file__), "lib"),
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "client", "lib"),
        ]
        for lib_dir in lib_dirs:
            for name in ["ffmpeg.exe", "ffmpeg"]:
                path = os.path.join(lib_dir, name)
                if os.path.isfile(path):
                    return path

        # 2. Bundled npm package (local API dev/run mode)
        installer_root = resource_path("api", "node_modules", "@ffmpeg-installer")
        explicit_paths = [
            os.path.join(installer_root, "win32-x64", "ffmpeg.exe"),
            os.path.join(installer_root, "win32-ia32", "ffmpeg.exe"),
            os.path.join(installer_root, "win32-arm64", "ffmpeg.exe"),
            os.path.join(installer_root, "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join(installer_root, "ffmpeg", "bin", "ffmpeg"),
        ]
        for ep in explicit_paths:
            if os.path.isfile(ep):
                return ep

        hits = _glob.glob(os.path.join(installer_root, "**", "ffmpeg.exe"), recursive=True)
        if not hits:
            hits = _glob.glob(os.path.join(installer_root, "**", "ffmpeg"), recursive=True)
        if hits:
            return hits[0]

        # 3. Fallback: ffmpeg on the system PATH (user-installed)
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            return system_ffmpeg
        return None

    def _convert_wav_to_ogg(self, wav_path: str) -> str | None:
        """
        Convert a WAV file to OGG/Opus using the bundled ffmpeg binary.
        Returns the path to the new .ogg file, or None on failure.
        """
        ffmpeg = self._find_api_ffmpeg()
        if not ffmpeg or not os.path.isfile(ffmpeg):
            logging.warning("[audio] ffmpeg not found — sending WAV (may fail). Searched: %s",
                            resource_path("api", "node_modules", "@ffmpeg-installer", "ffmpeg", "bin"))
            return None
        ogg_path = wav_path + ".ogg"
        try:
            creationflags = 0
            if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(
                [ffmpeg, "-y", "-i", wav_path,
                 "-ac", "1",
                 "-c:a", "libopus", "-b:a", "64k",
                 "-vbr", "on", "-compression_level", "10",
                 ogg_path],
                capture_output=True,
                timeout=60,
                creationflags=creationflags,
            )
            if result.returncode == 0 and os.path.isfile(ogg_path) and os.path.getsize(ogg_path) > 0:
                logging.debug("[audio] WAV→OGG conversion succeeded: %s", ogg_path)
                return ogg_path
            logging.error("[audio] ffmpeg WAV→OGG failed (rc=%s): %s",
                          result.returncode,
                          (result.stderr or b"").decode("utf-8", errors="replace")[-800:])
        except Exception as exc:
            logging.error("[audio] ffmpeg conversion exception: %s", exc)
        return None

    def send_audio_message(self, remote_jid: str, wav_path: str, quoted=None,
                           ogg_bytes: bytes = None) -> bool:
        """
        Encode a recorded WAV file to OGG Opus via FFmpeg (or pre-encoded ogg_bytes)
        and send it as a PTT voice message using /send-voice-base64.

        ogg_bytes: if provided (pre-encoded in background thread), skip the
                   disk read and OGG encoding entirely — just base64 + POST.
                   On retry (ogg_bytes=None) falls back to reading wav_path.
        """
        # Canonical destination: @lid when known, else the @c.us phone form —
        # see _resolve_jid_for_send's docstring for why @lid has to win here.
        import time as _time
        _tsend0 = _time.perf_counter()
        remote_jid = self._resolve_jid_for_send(remote_jid)
        is_lid_target = remote_jid.endswith("@lid")
        logging.info("[VOICE_TIMING] send_audio_message started for %s (isLid=%s, ogg_bytes=%s) — jid resolved in %.3fs",
                     remote_jid, is_lid_target, "yes" if ogg_bytes else "NO", _time.perf_counter() - _tsend0)

        if ogg_bytes is None:
            # Fallback path: convert WAV to OGG using ffmpeg and read the bytes
            _t_fallback = _time.perf_counter()
            logging.info("[VOICE_TIMING] ogg_bytes is None — running ffmpeg AGAIN as fallback (this should NOT happen!)")
            ogg_path = self._convert_wav_to_ogg(wav_path)
            if ogg_path and os.path.isfile(ogg_path):
                try:
                    with open(ogg_path, "rb") as fh:
                        ogg_bytes = fh.read()
                except Exception as exc:
                    logging.error("[send_audio_message] cannot read OGG file %s: %s", ogg_path, exc)
                finally:
                    try:
                        os.unlink(ogg_path)
                    except Exception:
                        pass

            if ogg_bytes is None:
                # If conversion failed, do not send raw WAV as it breaks WhatsApp (silent failure)
                err_msg = self.i18n.t("audio_convert_failed")
                logging.error("[send_audio_message] %s", err_msg)
                return {"ok": False, "error": err_msg, "retry": False}
            logging.info("[VOICE_TIMING] fallback encode+read done in %.3fs",
                         _time.perf_counter() - _t_fallback)

        audio_b64 = base64.b64encode(ogg_bytes).decode("utf-8")

        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-voice-base64"
        phone_net = remote_jid
        if phone_net.endswith("@s.whatsapp.net"):
            phone_net = phone_net.replace("@s.whatsapp.net", "@c.us")
        quoted_id = self._serialize_quoted_id(quoted, fallback_jid=phone_net) if quoted else None
        payload = {
            "phone": [phone_net],
            "base64Ptt": f"data:audio/ogg;codecs=opus;base64,{audio_b64}",
            "isGroup": phone_net.endswith("@g.us"),
            "isLid": is_lid_target,
        }
        if quoted_id:
            payload["quotedMessageId"] = quoted_id
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        _t_post = _time.perf_counter()
        logging.info("[VOICE_TIMING] POSTing to send-voice-base64 (payload size ~%d bytes b64)",
                     len(audio_b64))
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            logging.info("[VOICE_TIMING] POST returned HTTP %s in %.3fs",
                         response.status_code, _time.perf_counter() - _t_post)
            if response.status_code not in (200, 201):
                err = f"HTTP {response.status_code}: {response.text[:300]}"
                logging.error("[send_audio_message] %s for %s", err, remote_jid)

                # Fallback: the @lid destination was refused — the chat may just
                # not be loaded in Puppeteer yet (classic 400 "o número não
                # existe"), but any definite failure on a @lid destination earns
                # one attempt on the legacy @c.us address. Skipped when the API
                # reports the session as disconnected: nothing was sent.
                fb_phone = self._legacy_phone_for_send(remote_jid) if is_lid_target else ""
                disc = self._check_wa_connection_closed(response)
                if fb_phone and not disc:
                    logging.warning("[send_audio_message] @lid destination %s refused (HTTP %s) — retrying with legacy %s",
                                    remote_jid, response.status_code, fb_phone)
                    retry_payload = {
                        "phone": [fb_phone],
                        "base64Ptt": f"data:audio/ogg;codecs=opus;base64,{audio_b64}",
                        "isGroup": fb_phone.endswith("@g.us"),
                        "isLid": False,
                    }
                    if quoted_id:
                        retry_payload["quotedMessageId"] = quoted_id
                    response = requests.post(url, json=retry_payload, headers=headers, timeout=30)
                    if response.status_code in (200, 201):
                        logging.info("[send_audio_message] legacy retry with %s succeeded", fb_phone)
                        # fall through to normal response parsing below
                    else:
                        err = f"HTTP {response.status_code}: {response.text[:300]}"
                        logging.error("[send_audio_message] legacy retry also failed: %s", err)
                        if self._check_wa_connection_closed(response):
                            return {"ok": False, "error": err, "retry": False, "disconnected": True}
                        return {"ok": False, "error": err, "retry": True}
                else:
                    return {"ok": False, "error": err, "retry": False, "disconnected": disc}

            self._set_wa_connected(True, "audio send succeeded")
            try:
                body = response.json()
                resp = body.get("response", {})
                if isinstance(resp, list) and len(resp) > 0:
                    resp = resp[0]
                if isinstance(resp, dict):
                    msg_id = resp.get("id")
                    if isinstance(msg_id, dict):
                        msg_id = msg_id.get("_serialized", "")
                    parts = msg_id.split("_") if msg_id else []
                    clean_id = parts[2] if len(parts) > 2 else (parts[-1] if parts else msg_id)
                    return clean_id or True
                return True
            except Exception:
                return True
        except Exception as e:
            return self._classify_send_exception(e, "send_audio_message")


    def _serialize_msg_id(self, remote_jid: str, msg_key: dict, full_msg: dict = None) -> str:
        """
        Build the full serialized WhatsApp message ID expected by WPPConnect
        (`WPP.chat.getMessageById`).  The bare key.id is not enough — the library
        needs `<fromMe>_<chatId>_<id>` and, for group messages, a trailing
        `_<participant>` — including for our own group messages (`fromMe=True`).
        """
        def _resolve_to_lid_if_available(jid: str) -> str:
            """Resolve JID to cached @lid if available, keeping @g.us / @broadcast, and formatting to @c.us otherwise."""
            if not jid:
                return jid
            if jid.endswith(("@g.us", "@broadcast")):
                return jid
            if jid.endswith("@lid"):
                return jid
            clean = jid.replace("@c.us", "@s.whatsapp.net")
            lid = getattr(self, "_phone_to_lid", {}).get(clean, "")
            if lid:
                return lid
            return jid.replace("@s.whatsapp.net", "@c.us")

        def _format_1on1_chat(jid: str) -> str:
            if not jid:
                return jid
            if jid.endswith("@s.whatsapp.net"):
                return jid.replace("@s.whatsapp.net", "@c.us")
            return jid

        msg_id = msg_key.get("id", "")
        if not msg_id:
            return ""
        # A serialized id may already have been stored as the key id.
        if msg_id.startswith(("true_", "false_")):
            return msg_id
        from_me = bool(msg_key.get("fromMe", False))
        prefix = "true" if from_me else "false"
        
        raw_remote = remote_jid or msg_key.get("remoteJid", "") or (full_msg.get("from") if isinstance(full_msg, dict) else "") or ""
        if raw_remote.endswith("@g.us"):
            chat = raw_remote
        else:
            chat = _format_1on1_chat(raw_remote)

        # Group messages — and status updates (status@broadcast is a shared
        # "chat" the same way a group is: WPPConnect/Baileys need the actual
        # poster's JID as the trailing participant segment to look up a
        # specific status in Store, exactly like a specific group message) —
        # always carry the sender's JID in the serialized id, even for our
        # own (fromMe=True). 1-on-1 keys have no participant.
        #
        # Dropping this segment for @broadcast used to make every status
        # video/audio silently fail to play and every status "like" fail
        # with a generic server error: WPPConnect's getMessageById() (media
        # download) and its reaction endpoint both look up
        # status@broadcast messages by <chat>_<id>_<participant> — the
        # 2-segment id this produced without a participant never matched
        # anything in Store, so both requests failed on a status update that
        # was otherwise perfectly available.
        participant = ""
        if chat.endswith(("@g.us", "@broadcast")):
            if from_me:
                raw = (
                    getattr(self, "my_lid", "")
                    or getattr(self, "my_jid", "")
                    or msg_key.get("participant")
                    or (full_msg.get("participant") if isinstance(full_msg, dict) else "")
                    or (full_msg.get("author") if isinstance(full_msg, dict) else "")
                    or ""
                )
            else:
                raw = (
                    msg_key.get("participant")
                    or msg_key.get("author")
                    or (full_msg.get("participant") if isinstance(full_msg, dict) else "")
                    or (full_msg.get("author") if isinstance(full_msg, dict) else "")
                    or (full_msg.get("from") if isinstance(full_msg, dict) and not str(full_msg.get("from", "")).endswith("@g.us") else "")
                    or msg_key.get("remoteJidAlt")
                    or ""
                )
            participant = _resolve_to_lid_if_available(raw)
        if participant:
            return f"{prefix}_{chat}_{msg_id}_{participant}"
        return f"{prefix}_{chat}_{msg_id}"

    def send_reaction(self, remote_jid: str, msg_key: dict, emoji: str) -> bool:
        """Send a reaction to a message via the WPPConnect Server API."""
        # Resolve the @lid chat to its phone JID the same way deletes do, so the
        # serialized id matches the chat WPPConnect actually has loaded.
        lid_jid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
        if lid_jid:
            remote_jid = lid_jid
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/react-message"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "msgId": self._serialize_msg_id(remote_jid, msg_key),
            "reaction": emoji
        }
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=15)
            if response.status_code not in (200, 201):
                # 1500 chars (not 500) — deviceController.ts's reactMessage
                # now includes a real error message + stack trace in the
                # body (see its own comment on why a bare `error: e` used
                # to serialize down to almost nothing), which can run
                # longer than the old truncation allowed.
                logging.error("[send_reaction] HTTP %s: %s",
                              response.status_code, response.text[:1500])
                return False
            return True
        except Exception as exc:
            logging.error("[send_reaction] exception: %s", exc)
            return False

    def pin_message(self, remote_jid: str, msg_key: dict, pin: bool = True) -> bool:
        """Pin/unpin a single message in a chat via the WPPConnect Server API.

        This is WhatsApp's own message-pin feature (visible to every other
        participant) — a separate custom `/pin-message` endpoint added in
        api_patches/, since @wppconnect-team/wppconnect only wraps pinning a
        whole *chat* (see pin_chat/unpin_chat below), not an individual
        message within it.
        """
        lid_jid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
        if lid_jid:
            remote_jid = lid_jid
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/pin-message"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messageId": self._serialize_msg_id(remote_jid, msg_key),
            "pin": pin,
        }
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=15)
            if response.status_code not in (200, 201):
                logging.error("[pin_message] HTTP %s: %s",
                              response.status_code, response.text[:500])
                return False
            return True
        except Exception as exc:
            logging.error("[pin_message] exception: %s", exc)
            return False

    def _on_message_sent(self, local_id: str, audio_path: str = None, real_id: str = None, remote_jid: str = None):
        """
        Called on the main thread after a queued message is successfully sent.
        Updates the UI status label and cleans up any temporary audio file.
        real_id is the WhatsApp message ID returned by the API; it replaces the
        local UUID in the virtual message so playback can find the message in the DB.
        """
        import time as _time
        logging.info("[VOICE_TIMING] _on_message_sent — message LEFT pending state. local_id=%s real_id=%s",
                     local_id, real_id)
        if real_id and remote_jid:
            def _bg_update_db():
                try:
                    self.db.update_message_id(remote_jid, local_id, real_id)
                except Exception as e:
                    logging.error("[_on_message_sent] failed to update database message ID: %s", e)
            threading.Thread(target=_bg_update_db, daemon=True).start()

        # Save or copy the local audio copy under the real ID *before* calling _mark_message_sent
        # to prevent background media sync from downloading a file we already have.
        if audio_path and os.path.isfile(audio_path):
            if real_id and isinstance(real_id, str):
                try:
                    voice_messages_dir = data_path("voice_messages")
                    os.makedirs(voice_messages_dir, exist_ok=True)
                    local_audio_path = os.path.join(voice_messages_dir, f"{local_id}.msv")
                    real_audio_path = os.path.join(voice_messages_dir, f"{real_id}.msv")
                    
                    if os.path.isfile(local_audio_path):
                        import shutil
                        shutil.copy2(local_audio_path, real_audio_path)
                    else:
                        with open(audio_path, "rb") as f:
                            wav_data = f.read()
                        with open(real_audio_path, "wb") as f_out:
                            f_out.write(encrypt(wav_data, self.key))
                except Exception as e:
                    print(f"[_on_message_sent] error saving sent audio locally: {e}")
            try:
                os.unlink(audio_path)
            except Exception:
                pass

        # For audio messages, play the sent sound HERE — not only inside
        # _mark_message_sent — because the upload can take several seconds.
        # If the user navigated to another conversation before the API
        # confirmed the send, local_id is no longer in _sorted_messages and
        # _mark_message_sent would silently skip the sound.  Playing it here
        # guarantees the "tac" always fires the moment the API says "sent".
        if audio_path and hasattr(self, "message_sent_sound"):
            self.message_sent_sound.play()

        if hasattr(self, "conversations_panel"):
            self.conversations_panel._mark_message_sent(local_id, real_id=real_id)

    def _on_message_unconfirmed(self, local_id: str):
        """Called when a send timed out and its outcome cannot be determined.

        The message is NOT retried (that is what used to flood conversations
        with duplicates), and WhatsApp Web may still deliver it on reconnect —
        in which case the echo arriving over the WebSocket resolves this very
        bubble. Until that happens the row carries an explicit "unconfirmed"
        status: it used to be left in "sending" forever, which a user reasonably
        reads as sent, and that is exactly how a message that never left the
        browser passed for delivered.
        """
        if hasattr(self, "conversations_panel"):
            self.conversations_panel._mark_message_unconfirmed(local_id)
        if not self.background_mode:
            self.output(self.i18n.t("message_send_unconfirmed"), interrupt=False)

    def _on_message_failed(self, local_id: str, error: str = "", show_dialog: bool = False):
        """
        Called on the main thread after a queued message exhausts all retries.
        Marks the virtual message as failed in the UI and, for media attachments,
        shows an error dialog so the user knows the file was not delivered.
        """
        if hasattr(self, "conversations_panel"):
            self.conversations_panel._mark_message_failed(local_id)
        # _mark_message_failed() only updates the row inside the open
        # conversation — the chat-list preview reads the same underlying
        # record via _last_msg_preview()/_counts_as_last_message() (which
        # now excludes a failed send), but the list widget itself was never
        # told to re-render, so the stale preview sat there until the user
        # happened to reopen the conversation for an unrelated reason.
        self._schedule_set_chats()
        if show_dialog:
            self.error_sound.play()
            detail = error[:300] if error else self.i18n.t("error").format(app_name=self.app_name)
            wx.MessageBox(
                self.i18n.t("media_send_failed").format(error=detail),
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_ERROR,
            )

    def on_message_status_update(self, update: dict):
        """
        Handle a messages.update WebSocket event on the main thread.
        Updates MessageUpdate list on the cached message record and refreshes
        the status icon shown in the active conversation.
        """
        key       = update.get("key", {})
        msg_id    = key.get("id", "")
        status    = update.get("status", "") or str(update.get("update", {}).get("status", ""))
        if not msg_id or not status:
            return
        remote_jid = self._normalize_jid(key.get("remoteJid", ""))
        logging.info(f"[on_message_status_update] msg_id={msg_id} status={status} remote_jid={remote_jid}")

        # Try all known JID forms (@lid <-> @s.whatsapp.net) to find the chat
        candidates = [remote_jid]
        if remote_jid.endswith("@lid"):
            phone_jid = getattr(self, "_lid_to_phone", {}).get(remote_jid, "")
            if phone_jid:
                candidates.append(phone_jid)
        elif remote_jid.endswith("@s.whatsapp.net"):
            lid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
            if lid:
                candidates.append(lid)

        chat_jid = next((j for j in candidates if j in self.chats), None)
        found_msg = None
        found_chat_jid = None

        if chat_jid:
            records = (
                self.chats[chat_jid]
                    .get("messages", {})
                    .get("messages", {})
                    .get("records", [])
            )
            for msg in records:
                if msg.get("key", {}).get("id") == msg_id:
                    msg.setdefault("MessageUpdate", []).append({"status": status})
                    found_msg = msg
                    found_chat_jid = chat_jid
                    logging.info(f"[on_message_status_update] Updated status to {status} for msg_id={msg_id} in records of chat={chat_jid}")
                    break
            if not found_msg:
                logging.warning(f"[on_message_status_update] Message {msg_id} not found in records of chat {chat_jid}")
        else:
            logging.warning(f"[on_message_status_update] Chat not found in self.chats for candidates: {candidates}")

        # ── Fallback: scan all chats in memory when the initial candidates miss ──
        # This happens when a status event arrives with remote_jid equal to our own
        # LID (the account LID), not the recipient's JID, so none of the candidates
        # matched. The message is actually stored in the recipient's chat.
        if not found_msg:
            for jid, chat_data in self.chats.items():
                if jid in candidates:
                    continue
                recs = (
                    chat_data.get("messages", {})
                             .get("messages", {})
                             .get("records", [])
                )
                for msg in recs:
                    if msg.get("key", {}).get("id") == msg_id:
                        msg.setdefault("MessageUpdate", []).append({"status": status})
                        found_msg = msg
                        found_chat_jid = jid
                        logging.info(
                            f"[on_message_status_update] Fallback: updated status to {status} "
                            f"for msg_id={msg_id} in chat={jid}"
                        )
                        break
                if found_msg:
                    break

        # ── Persist updated message record to DB ─────────────────────────────────
        # insert_message rewrites message_json (which carries MessageUpdate, what
        # the UI reads) *and* the indexed status column, so both stay in step.
        if found_msg and found_chat_jid:
            try:
                self.db.insert_message(found_chat_jid, found_msg)
            except Exception as e:
                logging.error(f"[on_message_status_update] Failed to persist status update to DB: {e}")

        # A failed ack retires the "sending"/"unconfirmed" state a virtual message
        # may still be showing: WhatsApp has given its verdict, so stop implying
        # the send is still in flight.
        try:
            if found_msg and int(status) < 0:
                found_msg.pop("_send_unconfirmed", None)
                found_msg["_local_pending"] = False
                found_msg["_send_failed"] = True
                logging.warning("[on_message_status_update] msg_id=%s reported FAILED by WhatsApp "
                                "(status=%s) — marking as not delivered", msg_id, status)
        except (TypeError, ValueError):
            pass

        if hasattr(self, "conversations_panel"):
            self.conversations_panel.refresh_message_status(msg_id, status)


    def _resolve_jid_name(self, jid_norm: str) -> str:
        """Return the best display name for a participant JID (contact lookup + fallback)."""
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
            # No phone mapping yet for this @lid — `local` here is just the
            # raw @lid digits, meaningless to a user ("Fulano está digitando"
            # showing a bare numeric ID instead of a name/phone). A generic
            # placeholder is far more useful than exposing that internal ID.
            return self.i18n.t("unnamed_participant")
        if not jid_norm.endswith("@g.us"):
            return format_number(jid_norm)
        return local

    def _presence_label_for_chat(self, chat_jid_norm: str, is_group: bool) -> str:
        """Return the typing/recording label to append to a chat-list row, or ''."""
        active = getattr(self, "_composing_chats", {}).get(chat_jid_norm, {})
        if not active:
            return ""
        participant_jid, action = next(iter(active.items()))
        if action == "composing":
            action_label = self.i18n.t("typing_indicator")
        elif action == "recording":
            action_label = self.i18n.t("recording_indicator")
        else:
            return ""
        if is_group:
            name = self._resolve_jid_name(participant_jid)
            if name:
                return self.i18n.t("group_presence_indicator").format(
                    name=name, action=action_label
                )
        return action_label

    def _refresh_chat_row_in_list(self, chat_jid_norm: str):
        """Update only the chat-list row for chat_jid_norm via SetItem(), in
        whichever panel (main or archived) currently displays it.

        Replaces the full _schedule_set_chats() rebuild for changes that don't
        affect list membership/order (presence, unread count going to 0).
        SetItem() on a single row prevents NVDA from re-reading the entire
        list, and applies immediately instead of waiting out the 300ms
        _schedule_set_chats() debounce.
        """
        # Title/tray tooltip unread count: cheap (one pass over self.chats),
        # so recompute it here directly rather than waiting for the next
        # debounced _apply_chat_lists() rebuild.
        self._update_title()

        is_group = chat_jid_norm.endswith("@g.us")
        for panel in (
            getattr(self, "conversations_panel", None),
            getattr(self, "archived_conversations_panel", None),
        ):
            if panel is None:
                continue
            lst       = getattr(panel, "conversations_list", None)
            displayed = getattr(panel, "chats_list", [])
            names     = getattr(panel, "chat_names", [])
            if lst is None:
                continue
            for idx, chat in enumerate(displayed):
                if self._normalize_jid(chat.get("remoteJid", "")) != chat_jid_norm:
                    continue
                if idx >= lst.GetItemCount():
                    # displayed/chats_list (this panel's backing array) has
                    # drifted ahead of the ListCtrl's actual row count — e.g.
                    # a debounced full rebuild (_apply_chat_lists) is
                    # mid-flight on another callback and hasn't inserted this
                    # many rows yet. There's nothing to patch until that
                    # rebuild finishes and re-syncs both; the next presence/
                    # unread event will retry. Falls through to the crash
                    # this guard exists for otherwise ("invalid item index in
                    # SetItem").
                    break
                unread = effective_unread_count(chat)
                conv_filter = getattr(panel, '_conv_filter', 'all')
                if conv_filter == 'unread' and unread == 0:
                    # No longer belongs in the "unread" filtered view. Remove it
                    # outright (and keep the backing arrays in sync) immediately
                    # instead of waiting for the next debounced full rebuild —
                    # this used to be the only way such a row ever disappeared,
                    # and letting several of these pile up made the backing
                    # arrays' indices drift from the ListCtrl's real item count
                    # (the "Couldn't retrieve information about list control
                    # item N" crashes).
                    try:
                        lst.DeleteItem(idx)
                    except Exception:
                        break
                    del displayed[idx]
                    if idx < len(names):
                        del names[idx]
                    displayed_jids = getattr(panel, '_displayed_jids', None)
                    if displayed_jids is not None and idx < len(displayed_jids):
                        del displayed_jids[idx]
                    break
                name   = names[idx] if idx < len(names) else ""
                unread_str = (
                    f" {unread} " + (
                        self.i18n.t("unread_messages") if unread > 1
                        else self.i18n.t("unread_message")
                    )
                    if unread > 0 else ""
                )
                preview   = self._last_msg_preview(chat)
                item_text = name + unread_str
                if preview:
                    item_text += f" {preview}"
                label = self._presence_label_for_chat(chat_jid_norm, is_group)
                if label:
                    item_text += f" {label}"
                # Mirrors add_chats_to_ui()'s _build_item_text() — without
                # these, a single-row refresh (presence/unread changes, which
                # fire far more often than a full rebuild) silently dropped
                # the pinned/muted/blocked suffix from a row until the next
                # full rebuild happened to run.
                if self.is_chat_pinned(chat_jid_norm):
                    item_text += f" ({self.i18n.t('pinned_suffix')})"
                if self.is_chat_muted(chat_jid_norm):
                    item_text += f" ({self.i18n.t('muted')})"
                if self.is_contact_blocked(chat_jid_norm):
                    item_text += f" ({self.i18n.t('blocked')})"
                # Only touch the row when the visible text actually changes. Presence
                # bursts (online/offline toggles that don't alter the row) otherwise
                # rewrote the focused item's text repeatedly, making NVDA announce the
                # conversation name over and over while the user sat idle on the list.
                try:
                    if lst.GetItemText(idx, 0) != item_text:
                        lst.SetItem(idx, 0, item_text)
                except Exception:
                    # GetItemText/SetItem failing here almost always means idx
                    # is no longer valid for this ListCtrl (see the item-count
                    # guard above) — unconditionally retrying SetItem with the
                    # same idx just raised the exact same wx assertion again,
                    # uncaught this time, crashing the app instead of no-op'ing.
                    pass
                break

    def on_presence_update(self, jid: str, presences: dict):
        """Update the presence cache and composing indicators. Speaks changes
        via AO2 when the active conversation has a new composing event, and refreshes
        the data-button note for the open conversation.

        presences: {jid_str: {"lastKnownPresence": str, "lastSeen": int|None}, ...}
        """
        logging.info("[on_presence_update] jid=%s, presences=%s", jid, presences)

        if not jid or not isinstance(presences, dict):
            return

        chat_jid_norm = self._normalize_jid(jid)
        if chat_jid_norm.endswith("@lid"):
            chat_jid_norm = self._lid_to_phone.get(chat_jid_norm, chat_jid_norm)

        composing_chats = getattr(self, "_composing_chats", None)
        if composing_chats is None:
            self._composing_chats = {}
            composing_chats = self._composing_chats

        # Determine the open conversation JID (may be None)
        panel     = getattr(self, "conversations_panel", None)
        conv      = getattr(panel, "conversation", None) if panel else None
        conv_jid  = ""
        if conv is not None:
            conv_jid = self._normalize_jid(conv.get("remoteJid", ""))
            if conv_jid.endswith("@lid"):
                conv_jid = self._lid_to_phone.get(conv_jid, conv_jid)

        presence_changed = False

        # Check if this presence event belongs to the currently active conversation
        def is_active_chat(cjid, open_jid):
            if not open_jid:
                return False
            if cjid == open_jid:
                return True
            p1 = self._lid_to_phone.get(cjid, cjid)
            p2 = self._lid_to_phone.get(open_jid, open_jid)
            if p1 == p2:
                return True
            l1 = self._phone_to_lid.get(cjid, cjid)
            l2 = self._phone_to_lid.get(open_jid, open_jid)
            if l1 == l2:
                return True
            return False

        _ppm_updated = False
        for participant_jid, data in presences.items():
            if not isinstance(data, dict):
                continue
            canonical = self._normalize_jid(participant_jid)
            if canonical.endswith("@lid"):
                canonical = self._lid_to_phone.get(canonical, canonical)

            # ── Persist pushName learned from presence so @lid contacts show
            # the correct name even before they appear in _lid_to_phone. ──────
            if canonical.endswith("@s.whatsapp.net"):
                contact_entry = self.contacts.get(canonical)
                if contact_entry:
                    push = (contact_entry.get("pushName") or "").strip()
                    if push and not push.isdigit() and not is_phone_like(push):
                        if self._presence_pushname_map.get(canonical) != push:
                            self._presence_pushname_map[canonical] = push
                            _ppm_updated = True
                        # Also index the corresponding @lid if known, so callers
                        # can look up by lid_jid directly without bridging.
                        lid = getattr(self, "_phone_to_lid", {}).get(canonical, "")
                        if lid and self._presence_pushname_map.get(lid) != push:
                            self._presence_pushname_map[lid] = push
                            _ppm_updated = True

            old_lkp = self._presence_cache.get(canonical, {}).get("lastKnownPresence", "")
            new_lkp = data.get("lastKnownPresence", "unavailable")

            self._presence_cache[canonical] = {
                "lastKnownPresence": new_lkp,
                "lastSeen": data.get("lastSeen"),
            }

            if new_lkp != old_lkp:
                presence_changed = True

            # Update composing/recording index for this chat
            if chat_jid_norm not in composing_chats:
                composing_chats[chat_jid_norm] = {}
            timer_key = (chat_jid_norm, canonical)
            if new_lkp in ("composing", "recording"):
                composing_chats[chat_jid_norm][canonical] = new_lkp
                # Reset the 10-second auto-clear timer on every new event
                old_timer = self._presence_timers.pop(timer_key, None)
                if old_timer is not None:
                    try:
                        old_timer.Stop()
                    except Exception:
                        pass
                def _make_clear(cjid, part):
                    def _clear():
                        self._composing_chats.get(cjid, {}).pop(part, None)
                        self._presence_timers.pop((cjid, part), None)
                        self._refresh_chat_row_in_list(cjid)
                    return _clear
                self._presence_timers[timer_key] = wx.CallLater(
                    10_000, _make_clear(chat_jid_norm, canonical)
                )
            else:
                composing_chats[chat_jid_norm].pop(canonical, None)
                old_timer = self._presence_timers.pop(timer_key, None)
                if old_timer is not None:
                    try:
                        old_timer.Stop()
                    except Exception:
                        pass

            # Speak via AO2 only when a composing/recording event starts in the ACTIVE conversation.
            # Events from other chats are intentionally silent to avoid interrupting the user.
            if new_lkp != old_lkp and new_lkp in ("composing", "recording"):
                speech = self.settings.get("speech_content", {})
                announce_enabled = (
                    speech.get("announce_typing", True) if new_lkp == "composing"
                    else speech.get("announce_recording", True)
                )
                active_match = is_active_chat(chat_jid_norm, conv_jid)
                # Typing/recording indicators are only meaningful while the user
                # is actually looking at WinZapp — a conversation left open when
                # the window was minimized to the tray must not keep announcing.
                window_active = (
                    not getattr(self, "_window_hidden", False)
                    and self.IsShown()
                    and not self.IsIconized()
                    and self.IsActive()
                )
                logging.info("[on_presence_update] announce_enabled=%s, is_active_chat=%s, window_active=%s (chat_jid_norm=%s, conv_jid=%s)",
                             announce_enabled, active_match, window_active, chat_jid_norm, conv_jid)
                if announce_enabled and active_match and window_active:
                    if not self.is_chat_muted(chat_jid_norm) and not self.is_chat_archived(chat_jid_norm):
                        name = self._resolve_jid_name(canonical)
                        logging.info("[on_presence_update] resolved name=%s for canonical=%s", name, canonical)
                        if name:
                            try:
                                i18n_key = "typing_text" if new_lkp == "composing" else "recording_text"
                                msg_text = self.i18n.t(i18n_key).format(name=name)
                                logging.info("[on_presence_update] speaking: %s", msg_text)
                                self.speak_output.output(msg_text)
                            except Exception as e:
                                logging.error("[on_presence_update] speak error: %s", e)

        # Persist the updated pushName map to database metadata.
        if _ppm_updated and hasattr(self, "db") and self.db is not None:
            try:
                self.db.set_metadata_json("presence_pushname_map", dict(self._presence_pushname_map))
            except Exception as db_err:
                logging.warning("[on_presence_update] Failed to save presence_pushname_map: %s", db_err)

        # Update only the affected row — avoids DeleteAllItems()+Append() rebuild
        # that causes NVDA to re-read the full list and stutter during TTS echo.
        if presence_changed:
            self._refresh_chat_row_in_list(chat_jid_norm)

        # Refresh the data-button note for the open conversation
        if panel is None or conv is None:
            return
        if conv_jid in self._presence_cache:
            panel._refresh_presence_note(conv_jid)

    def on_chat_unread_update(self, jid: str, unread_count: int):
        """Handle unread-count change from chats.update (e.g. read on another device)."""
        normalized = self._normalize_jid(jid)
        chat = self.chats.get(normalized)
        if chat is None:
            return
        # During the initial sync the WPPConnect handshake can emit
        # chats-update with unreadCount=0 BEFORE get_remote_chats() has
        # fetched the real list — accepting that would wipe the locally
        # stored (never-read) badge and persist the 0 to the DB, so every
        # conversation the user hasn't actually read shows as read right
        # after opening the app. The list-chats merge in get_remote_chats()
        # is the authoritative source for the real counts; ignore live
        # chats.update while it (or the initial sync) is still running.
        if getattr(self, "_initial_sync_running", False) or not getattr(self, "_sync_completed", False):
            return
        old_count = int(chat.get("unreadCount") or 0)
        if old_count == unread_count:
            return  # no actual change — skip expensive rebuild + save
        # The server sometimes counts own (fromMe) messages as unread. Correct
        # for that by inspecting the tail of the locally-stored message list.
        if unread_count > 0:
            records = (
                (chat.get("messages") or {})
                .get("messages", {})
                .get("records", [])
            )
            if records:
                tail = records[-unread_count:] if unread_count <= len(records) else records
                own_count = sum(1 for m in tail if (m.get("key") or {}).get("fromMe"))
                unread_count = max(0, unread_count - own_count)
        old_count = int(chat.get("unreadCount") or 0)
        if old_count == unread_count:
            return
        # Never resurrect unread count for a conversation the user already read
        # locally (mark_conversation_as_read set it to 0). The server may still
        # carry a stale unread count from before the read-ack arrived.
        # NOTE: _last_open_jid lives on ConversationsPanel, not MainWindow —
        # this used to read `self._last_open_jid` directly, which never
        # existed on MainWindow and so always fell back to "", silently
        # disabling this guard entirely.
        cp = getattr(self, "conversations_panel", None)
        if normalized == getattr(cp, "_last_open_jid", ""):
            unread_count = 0
        else:
            read_at_t = getattr(self, "_locally_read_at", {}).get(normalized)
            if read_at_t is not None:
                incoming_t = int(chat.get("t", 0) or 0)
                if incoming_t <= read_at_t:
                    unread_count = 0
                else:
                    # A genuinely new message arrived after the local
                    # read-ack, so the guard above no longer applies. But the
                    # server-reported total can still be stale/inflated —
                    # e.g. it may still include messages we already read
                    # locally that WhatsApp's own servers haven't caught up
                    # with yet. Clamp to what we actually know arrived since
                    # the read-ack instead of trusting the raw server count.
                    local_new = getattr(self, "_new_since_read", {}).get(normalized)
                    if local_new:
                        unread_count = min(unread_count, local_new)
                    self._locally_read_at.pop(normalized, None)
                    if hasattr(self, "_new_since_read"):
                        self._new_since_read.pop(normalized, None)
        chat["unreadCount"] = unread_count
        self._schedule_save(dirty_jid=normalized)
        self._schedule_set_chats()

    def on_chat_archive_update(self, jid: str, archived: bool):
        """Handle archive/unarchive status change from chats.update."""
        normalized = self._normalize_jid(jid)
        chat = self.chats.get(normalized)
        if chat is None:
            return
        self._set_archived_state(normalized, archived)

    def on_chat_pin_update(self, jid: str, is_pinned: bool):
        """Handle pin/unpin status change from chats.update."""
        normalized = self._normalize_jid(jid)
        chat = self.chats.get(normalized)
        if chat is None:
            if normalized.endswith("@lid"):
                alt = getattr(self, "_lid_to_phone", {}).get(normalized, "")
                if alt: chat = self.chats.get(self._normalize_jid(alt))
            else:
                alt = getattr(self, "_phone_to_lid", {}).get(normalized, "")
                if alt: chat = self.chats.get(alt)

        if is_pinned:
            self._pinned_chats.add(normalized)
            if normalized.endswith("@lid"):
                alt_phone = getattr(self, "_lid_to_phone", {}).get(normalized, "")
                if alt_phone:
                    self._pinned_chats.add(self._normalize_jid(alt_phone))
            else:
                alt_lid = getattr(self, "_phone_to_lid", {}).get(normalized, "")
                if alt_lid:
                    self._pinned_chats.add(alt_lid)
        else:
            self._pinned_chats.discard(normalized)
            if normalized.endswith("@lid"):
                alt_phone = getattr(self, "_lid_to_phone", {}).get(normalized, "")
                if alt_phone:
                    self._pinned_chats.discard(self._normalize_jid(alt_phone))
            else:
                alt_lid = getattr(self, "_phone_to_lid", {}).get(normalized, "")
                if alt_lid:
                    self._pinned_chats.discard(alt_lid)

        if chat is not None:
            chat["pin"] = is_pinned

        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("pinned_chats", list(self._pinned_chats))
        self._schedule_set_chats()

    def handle_audio_message(self, msg, timeout=60):
        """Download and encrypt a voice message to data/voice_messages/.

        Returns True only when this call actually wrote a new file — see
        handle_media_message(), which follows the same contract.
        """
        voice_messages_dir = data_path("voice_messages")
        msg_id = msg.get('key', {}).get('id', '')
        if "_" in msg_id:
            parts = msg_id.split("_")
            msg_id = parts[2] if len(parts) > 2 else parts[-1]
        audio_file_path = os.path.join(voice_messages_dir, f"{msg_id}.msv")
        if os.path.isfile(audio_file_path):
            return False
        if not getattr(self, "_wa_connected", False):
            # See handle_media_message() — same reasoning applies to audio.
            logging.info("[handle_audio_message] Skipping download for %s — not connected.", msg_id)
            return False
        base64_audio = self.get_base64_from_media(msg, timeout=timeout)
        if not base64_audio:
            return False
        audio_content = base64.b64decode(base64_audio)
        return self.save_audio_locally(msg, audio_content)

    def get_base64_from_media(self, media, progress_callback=None, timeout=60):
        """
        Fetch encrypted media from WPPConnect and return its base64 string.

        Raises MediaExpiredError when the WhatsApp CDN URL has expired (HTTP 403/410).
        When *progress_callback* is provided the request is streamed and the
        callback is called with a float in [0, 1] as each chunk arrives.
        """
        _key = media.get("key", {})
        remote_jid = _key.get("remoteJid", "") or media.get("from", "")
        # If remote_jid is phone@c.us and we have an LID mapping for it, prefer LID JID
        if remote_jid and not remote_jid.endswith("@lid"):
            norm_phone = self._normalize_jid(remote_jid)
            alt_lid = getattr(self, "_phone_to_lid", {}).get(norm_phone, "")
            if alt_lid:
                _key = dict(_key)
                _key["remoteJid"] = alt_lid

        msg_id = self._serialize_msg_id(_key.get("remoteJid", "") or media.get("from", ""), _key, full_msg=media)
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-media-by-message/{msg_id}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        # Prepare body with media details to bypass Puppeteer cache lookups in WPPConnect Server
        body_data = dict(media)
        msg_type = media.get("messageType")
        msg_inner_obj = media.get("message")
        if isinstance(msg_inner_obj, str):
            try:
                msg_inner_obj = json.loads(msg_inner_obj)
            except Exception:
                msg_inner_obj = None

        if not msg_type and media.get("type"):
            t = str(media.get("type"))
            if t in ("audio", "ptt"):
                msg_type = "audioMessage"
            elif t == "image":
                msg_type = "imageMessage"
            elif t == "video":
                msg_type = "videoMessage"
            elif t in ("document", "doc"):
                msg_type = "documentMessage"

        # Check nested structures as well as top-level keys
        candidate_objs = []
        if isinstance(msg_inner_obj, dict):
            candidate_objs.append(msg_inner_obj)
            if msg_type and isinstance(msg_inner_obj.get(msg_type), dict):
                candidate_objs.append(msg_inner_obj.get(msg_type))
            for k in ("audioMessage", "imageMessage", "videoMessage", "documentMessage", "stickerMessage"):
                if isinstance(msg_inner_obj.get(k), dict):
                    candidate_objs.append(msg_inner_obj.get(k))
        candidate_objs.append(media)

        for obj in candidate_objs:
            if not body_data.get("mediaKey") and obj.get("mediaKey"):
                mk = obj.get("mediaKey")
                if isinstance(mk, bytes):
                    body_data["mediaKey"] = base64.b64encode(mk).decode("utf-8")
                elif isinstance(mk, dict) and "data" in mk:
                    body_data["mediaKey"] = base64.b64encode(bytes(mk["data"])).decode("utf-8")
                else:
                    body_data["mediaKey"] = str(mk)
            if not body_data.get("clientUrl") and (obj.get("url") or obj.get("clientUrl")):
                body_data["clientUrl"] = obj.get("url") or obj.get("clientUrl")
            if not body_data.get("directPath") and obj.get("directPath"):
                body_data["directPath"] = obj.get("directPath")
            if not body_data.get("mimetype") and obj.get("mimetype"):
                body_data["mimetype"] = obj.get("mimetype")

        if msg_type:
            body_data["type"] = msg_type.replace("Message", "")

        has_media_key = bool(body_data.get("mediaKey"))
        has_client_url = bool(body_data.get("clientUrl"))
        has_direct_path = bool(body_data.get("directPath"))
        media_type = body_data.get("type", "")
        logging.info(
            "[get_base64_from_media] Requesting media for msg_id=%s, url=%s, has_mediaKey=%s, has_clientUrl=%s, type=%s",
            msg_id, url, has_media_key, has_client_url, media_type
        )

        max_attempts = 3
        for attempt in range(max_attempts):
            if progress_callback is None:
                try:
                    response = requests.post(url, headers=headers, json=body_data, timeout=timeout)
                except MediaExpiredError:
                    logging.warning("[get_base64_from_media] MediaExpiredError for msg_id=%s", msg_id)
                    raise
                except Exception as exc:
                    logging.warning(
                        "[get_base64_from_media] request exception for %s (attempt %d/%d): %s",
                        msg_id, attempt + 1, max_attempts, exc,
                    )
                    if attempt < max_attempts - 1:
                        time.sleep(3)
                        continue
                    return ""
                
                resp_text = response.text or ""
                logging.info(
                    "[get_base64_from_media] WPPConnect server status=%d for msg_id=%s, body_snippet=%s",
                    response.status_code, msg_id, resp_text[:200]
                )

                if response.status_code in (403, 410):
                    logging.warning("[get_base64_from_media] HTTP %d (CDN expired) for %s", response.status_code, msg_id)
                    raise MediaExpiredError(response.status_code)
                if response.status_code in (200, 201):
                    b64 = response.json().get("base64", "")
                    logging.info("[get_base64_from_media] Success for %s — base64 len=%d", msg_id, len(b64))
                    return b64

                # Check for transient session not active errors
                if response.status_code in (400, 500) and any(x in resp_text.lower() for x in ("session is not active", "not active", "disconnected")):
                    logging.warning(
                        "[get_base64_from_media] session not active for %s, retrying in 3s (attempt %d/%d)",
                        msg_id, attempt + 1, max_attempts
                    )
                    self._set_wa_connected(False, "media fetch: session not active", announce=False)
                    if attempt < max_attempts - 1:
                        time.sleep(3)
                        continue
                logging.warning(
                     "[get_base64_from_media] HTTP %s fetching media for %s: %s",
                     response.status_code, msg_id, resp_text[:200],
                )
                return ""
            else:
                # Streaming mode so we can report per-chunk progress
                try:
                    response = requests.post(url, headers=headers, json=body_data, stream=True, timeout=timeout)
                    if response.status_code in (403, 410):
                        raise MediaExpiredError(response.status_code)
                    
                    # Check for transient session not active errors before streaming
                    if response.status_code in (400, 500):
                        # Read small error response
                        resp_text = response.text
                        if any(x in resp_text.lower() for x in ("session is not active", "not active", "disconnected")):
                            logging.warning(
                                "[get_base64_from_media] session not active for %s (stream), retrying in 3s (attempt %d/%d)",
                                msg_id, attempt + 1, max_attempts
                            )
                            self._set_wa_connected(False, "media fetch: session not active", announce=False)
                            if attempt < max_attempts - 1:
                                time.sleep(3)
                                continue
                        logging.warning(
                            "[get_base64_from_media] HTTP %s fetching media for %s: %s",
                            response.status_code, msg_id, resp_text[:200],
                        )
                        return ""

                    if response.status_code not in (200, 201):
                        logging.warning(
                            "[get_base64_from_media] HTTP %s fetching media for %s",
                            response.status_code, msg_id,
                        )
                        return ""
                    
                    total = int(response.headers.get("content-length", 0))
                    downloaded = 0
                    chunks: list = []
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            chunks.append(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                progress_callback(downloaded / total)
                    body = b"".join(chunks).decode("utf-8", errors="replace")
                    try:
                        return json.loads(body).get("base64", "")
                    except Exception:
                        # Caso o body retornado seja o base64 bruto ou binário
                        return base64.b64encode(b"".join(chunks)).decode("utf-8")
                except MediaExpiredError:
                    raise
                except Exception as exc:
                    logging.warning(
                        "[get_base64_from_media] request failed for %s (stream) (attempt %d/%d): %s",
                        msg_id, attempt + 1, max_attempts, exc,
                    )
                    if attempt < max_attempts - 1:
                        time.sleep(3)
                        continue
                    return ""
        return ""

    def fetch_older_messages(self, remote_jid, oldest_msg):
        """Fetch older messages from server starting before the oldest_msg."""
        remote_jid = self._normalize_jid(remote_jid)

        # Check if history is already marked as exhausted in-memory
        if remote_jid in getattr(self, "_exhausted_chats", set()):
            logging.info(f"[fetch_older_messages] History already marked as exhausted in-memory for {remote_jid}, skipping API query.")
            return []

        # Resolved phone/@c.us form of the chat JID — used both as the URL
        # parameter (WPPConnect has a special evaluate-bypass in
        # /get-messages/:phone for @lid JIDs) and as the chat segment of the
        # serialized message ID below, since the message ID key in
        # WPPConnect's browser store also matches the chat JID (LID if
        # available). These used to be computed twice under two different
        # names for no reason.
        # Handle group JIDs (@g.us) vs user JIDs (@c.us / @s.whatsapp.net)
        if remote_jid.endswith("@g.us"):
            phone = remote_jid
        else:
            phone = self._resolve_jid_for_msg_key(remote_jid).replace("@s.whatsapp.net", "@c.us")
        resolved_phone = phone

        _key = oldest_msg.get("key", {})
        serialized_key = _key.get("_serialized", "") or oldest_msg.get("_serialized", "")
        if serialized_key:
            serialized_id = serialized_key
        else:
            serialized_id = self._serialize_msg_id(remote_jid, _key if _key else oldest_msg)

        limit = int(self.settings.get("user_interface", {}).get("messages_page_size", 200))
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-messages/{phone}?count={limit}&direction=before&id={serialized_id}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        try:
            logging.info(f"[fetch_older_messages] Querying URL: {url}")
            response = requests.get(url, headers=headers, timeout=30)
            
            # Alternate JID query fallback (resolves 401/TypeError or Chat not found errors)
            if response.status_code not in (200, 201):
                alternate_jid = ""
                if remote_jid.endswith("@lid"):
                    # Primary query used resolved phone JID, so fallback to original LID JID
                    alternate_jid = remote_jid
                else:
                    alt_lid = getattr(self, "_phone_to_lid", {}).get(remote_jid, "")
                    if alt_lid:
                        alternate_jid = alt_lid

                if alternate_jid and alternate_jid != phone:
                    alt_serialized_id = serialized_id
                    if "_" in serialized_id:
                        parts = serialized_id.split("_")
                        if len(parts) >= 3:
                            parts[1] = alternate_jid
                            alt_serialized_id = "_".join(parts)
                    alt_url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/get-messages/{alternate_jid}?count={limit}&direction=before&id={alt_serialized_id}"
                    logging.info(f"[fetch_older_messages] Primary query failed. Retrying with alternate JID {alternate_jid}...")
                    try:
                        alt_response = requests.get(alt_url, headers=headers, timeout=30)
                        if alt_response.status_code in (200, 201):
                            response = alt_response
                            logging.info("[fetch_older_messages] Fallback alternate JID query succeeded!")
                    except Exception as alt_e:
                        logging.warning(f"[fetch_older_messages] Fallback alternate JID query failed: {alt_e}")

            if response.status_code in (200, 201):
                body = response.json()
                wpp_messages = body.get("response", []) if isinstance(body, dict) else []
                if not isinstance(wpp_messages, list):
                    wpp_messages = []
                
                # No messages left locally — but "locally" is the operative
                # word. WhatsApp only pushes a bounded window of history to a
                # linked device and keeps the rest on the phone, so running
                # out here usually means "this device has no more", not "this
                # conversation has no more". WhatsApp Web's own UI handles it
                # with the "get older messages from your phone" banner; do the
                # same thing, once per chat, before writing the chat off.
                #
                # The phone answers asynchronously (a history-sync chunk,
                # minutes later), so the request cannot help *this* call. What
                # it must not do is leave the chat in _exhausted_chats, or the
                # early-return at the top of this method would refuse to look
                # again even after the messages have landed.
                if not wpp_messages:
                    if not hasattr(self, "_exhausted_chats"):
                        self._exhausted_chats = set()
                    if not hasattr(self, "_older_requested_chats"):
                        self._older_requested_chats = set()
                    already_asked = remote_jid in self._older_requested_chats
                    requested = False
                    if not already_asked:
                        self._older_requested_chats.add(remote_jid)
                        requested = self.request_older_messages(remote_jid)
                    if requested:
                        logging.info(
                            "[fetch_older_messages] No local history left for %s — "
                            "asked the phone for older messages; leaving the chat "
                            "re-queryable so the reply can be picked up.", remote_jid,
                        )
                    else:
                        self._exhausted_chats.add(remote_jid)
                        logging.info(
                            "[fetch_older_messages] Marked history as exhausted "
                            "in-memory for %s (older-message request %s).",
                            remote_jid,
                            "already sent earlier" if already_asked else "not sent",
                        )

                fetched_messages = []
                for wm in wpp_messages:
                    if isinstance(wm, dict) and self.ws:
                        try:
                            normalized = self.ws._normalize_wpp_message(wm)
                            self._extract_lid_mapping(normalized)
                            fetched_messages.append(normalized)
                        except Exception:
                            pass
                
                if fetched_messages:
                    # Update local database/memory
                    chat = self.chats.get(remote_jid, {})
                    if chat:
                        local_records = chat.get("messages", {}).get("messages", {}).get("records", [])
                        existing_ids = {r.get("key", {}).get("id") for r in local_records}
                        new_records = [m for m in fetched_messages if m.get("key", {}).get("id") not in existing_ids]
                        if new_records:
                            all_records = new_records + local_records
                            chat.setdefault("messages", {}).setdefault("messages", {})["records"] = all_records
                            chat["messages"]["messages"]["total"] = len(all_records)
                            try:
                                self.db.upsert_chat(remote_jid, chat)
                                self.db.insert_messages_batch(remote_jid, new_records)
                            except Exception as e:
                                logging.error(f"[fetch_older_messages] Incremental save failed: {e}")
                                self.save_data(self.chats, self.contacts)
                    return fetched_messages
                else:
                    return []
            else:
                err_msg = response.text[:300]
                try:
                    body = response.json()
                    if isinstance(body, dict) and "error" in body:
                        err_obj = body["error"]
                        if isinstance(err_obj, dict) and "message" in err_obj:
                            err_msg = f"{err_obj.get('message')} - {err_obj.get('stack', '')[:200]}"
                except Exception:
                    pass
                logging.warning(
                    f"[fetch_older_messages] API returned status {response.status_code} for {remote_jid}: {err_msg}"
                )
                return None
        except Exception as e:
            logging.error(f"[fetch_older_messages] failed to get older messages for {remote_jid}: {e}")
            return None

    def save_audio_locally(self, msg, audio_content):
        """Encrypt and write a voice message to disk. Returns whether it worked."""
        voice_messages_dir = data_path("voice_messages")
        msg_id = msg.get('key', {}).get('id', '')
        if "_" in msg_id:
            parts = msg_id.split("_")
            msg_id = parts[2] if len(parts) > 2 else parts[-1]
        audio_file_path = os.path.join(voice_messages_dir, f"{msg_id}.msv")
        try:
            with open(audio_file_path, "wb") as audio_file:
                encrypted_audio = encrypt(audio_content, self.key)
                audio_file.write(encrypted_audio)
            return True
        except Exception as e:
            #Ignore audios that couldn't be saved for now
            return False

    def mark_conversation_as_read(self, remote_jid: str, force: bool = False):
        """Mark conversation as read locally and notify WPPConnect."""
        chat = self.chats.get(remote_jid)
        if chat is None:
            return

        unread = int(chat.get("unreadCount") or 0)
        chat["unreadCount"] = 0
        # Remember this chat's last-activity timestamp as of the moment we
        # cleared it locally — see the periodic list-chats merge in
        # get_remote_chats(), which uses this to tell a genuinely new
        # unreadCount apart from WhatsApp Web's own server-side read receipt
        # simply not having caught up with this mark-as-read yet.
        if not hasattr(self, "_locally_read_at"):
            self._locally_read_at = {}
        self._locally_read_at[remote_jid] = int(chat.get("t", 0) or 0)
        if not hasattr(self, "_new_since_read"):
            self._new_since_read = {}
        self._new_since_read[remote_jid] = 0
        self._schedule_save(dirty_jid=remote_jid)
        # Immediate single-row update: unlike _schedule_set_chats()/set_chats(),
        # this isn't suppressed while a media sync is running, so the badge
        # clears right away instead of only after the sync eventually finishes
        # (previously observed as a 10-20+ second — or longer — delay).
        wx.CallAfter(self._refresh_chat_row_in_list, self._normalize_jid(remote_jid))
        wx.CallAfter(self._schedule_set_chats)

        if unread == 0 and not force:
            return

        # Prefer @lid JID for WPPConnect if mapped
        target_phone = remote_jid
        if not target_phone.endswith("@lid"):
            alt_lid = getattr(self, "_phone_to_lid", {}).get(self._normalize_jid(remote_jid), "")
            if alt_lid:
                target_phone = alt_lid

        if target_phone.endswith("@s.whatsapp.net"):
            target_phone = target_phone.rsplit("@", 1)[0] + "@c.us"
        
        is_lid_target = target_phone.endswith("@lid")

        def _send_seen(phone: str, is_lid: bool) -> "requests.Response | None":
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-seen"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            payload = {"phone": phone, "isGroup": phone.endswith("@g.us")}
            if is_lid:
                payload["isLid"] = True
            return requests.post(url, json=payload, headers=headers, timeout=10)

        def _do_api():
            try:
                resp = _send_seen(target_phone, is_lid_target)
                if not resp.ok:
                    logging.warning("[mark_as_read] API response %s for %s: %s",
                                     resp.status_code, target_phone, resp.text[:200])
                    # If it failed, try the alternate format (LID <-> phone) as fallback
                    fallback_phone = remote_jid
                    if fallback_phone.endswith("@lid"):
                        phone_jid = getattr(self, "_lid_to_phone", {}).get(fallback_phone, "")
                        if phone_jid: fallback_phone = phone_jid
                    else:
                        alt_lid = getattr(self, "_phone_to_lid", {}).get(self._normalize_jid(fallback_phone), "")
                        if alt_lid: fallback_phone = alt_lid

                    if fallback_phone.endswith("@s.whatsapp.net"):
                        fallback_phone = fallback_phone.rsplit("@", 1)[0] + "@c.us"

                    if fallback_phone != target_phone:
                        logging.info("[mark_as_read] Retrying /send-seen using fallback JID: %s", fallback_phone)
                        resp2 = _send_seen(fallback_phone, fallback_phone.endswith("@lid"))
                        if not resp2.ok:
                            logging.warning("[mark_as_read] Fallback /send-seen also failed %s for %s: %s",
                                             resp2.status_code, fallback_phone, resp2.text[:200])
            except Exception as exc:
                logging.warning("[mark_as_read] Request failed for %s: %s", target_phone, exc)
        threading.Thread(target=_do_api, daemon=True).start()

    def mark_conversation_as_unread(self, remote_jid: str):
        chat = self.chats.get(remote_jid)
        if chat is not None:
            chat["unreadCount"] = 1
            self._schedule_save()
            wx.CallAfter(self.set_chats)

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
                response = requests.get(url, headers=headers, timeout=10)
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
                        if hasattr(self, "_lid_to_phone"):
                            # 1. If another LID was mapped to our phone, delete it (from memory and DB)
                            bad_lids = [k for k, v in self._lid_to_phone.items() if v == normalized_phone and k != normalized_lid]
                            for bad_lid in bad_lids:
                                self._lid_to_phone.pop(bad_lid, None)
                                self._phone_to_lid.pop(normalized_phone, None)
                                try:
                                    self.db.delete_lid_mapping(bad_lid)
                                except Exception as _e:
                                    pass
                                logging.warning(f"[Self LID Resolution] Deleted corrupt mapping: {bad_lid} was mapped to our phone {normalized_phone}")

                            # 2. If our LID JID was mapped to another phone number, delete it
                            old_phone = self._lid_to_phone.get(normalized_lid)
                            if old_phone and old_phone != normalized_phone:
                                self._lid_to_phone.pop(normalized_lid, None)
                                self._phone_to_lid.pop(old_phone, None)
                                try:
                                    self.db.delete_lid_mapping(normalized_lid)
                                except Exception as _e:
                                    pass
                                logging.warning(f"[Self LID Resolution] Cleaned corrupt mapping: {normalized_lid} was mapped to {old_phone}")
                            
                            # 3. If another phone JID was mapped to our LID, delete it
                            bad_phones = [k for k, v in self._phone_to_lid.items() if v == normalized_lid and k != normalized_phone]
                            for bad_phone in bad_phones:
                                self._phone_to_lid.pop(bad_phone, None)
                                self._lid_to_phone.pop(normalized_lid, None)
                                try:
                                    self.db.delete_lid_mapping(normalized_lid)
                                except Exception as _e:
                                    pass
                                logging.warning(f"[Self LID Resolution] Deleted corrupt mapping: our LID {normalized_lid} was mapped to another phone {bad_phone}")

                            # 4. If our phone JID was mapped to another LID, delete it
                            old_lid = self._phone_to_lid.get(normalized_phone)
                            if old_lid and old_lid != normalized_lid:
                                self._phone_to_lid.pop(old_lid, None)
                                self._lid_to_phone.pop(old_lid, None)
                                try:
                                    self.db.delete_lid_mapping(old_lid)
                                except Exception as _e:
                                    pass
                                logging.warning(f"[Self LID Resolution] Cleaned corrupt mapping: {normalized_phone} was mapped to {old_lid}")

                        self.register_jid_mapping(normalized_lid, normalized_phone)
                        logging.info(f"[Self LID Resolution] Successfully resolved and registered own JID mapping: {normalized_lid} <-> {normalized_phone}")
            except Exception as e:
                logging.error(f"[Self LID Resolution] Error resolving self LID: {e}")

        threading.Thread(target=_resolve, daemon=True).start()

    def register_jid_mapping(self, lid_jid, phone_jid, save=True):
        """Register a bidirectional mapping between @lid and @s.whatsapp.net, and persist it."""
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
            
        if not hasattr(self, "_lid_to_phone"):
            self._lid_to_phone = {}
        if not hasattr(self, "_phone_to_lid"):
            self._phone_to_lid = {}
            
        current_phone = self._lid_to_phone.get(lid_jid)
        if current_phone != phone_jid:
            self._lid_to_phone[lid_jid] = phone_jid
            self._phone_to_lid[phone_jid] = lid_jid
            logging.info(f"[LID Mapping] Registered JID mapping: {lid_jid} <-> {phone_jid}")
            
            # If it was in the unresolvable set, remove it
            if hasattr(self, "_unresolvable_lids") and lid_jid in self._unresolvable_lids:
                self._unresolvable_lids.discard(lid_jid)
            
            # Update the contact name display mappings in contacts if possible
            if phone_jid in self.contacts and self.contacts[phone_jid]:
                if lid_jid not in self.contacts or self.contacts[lid_jid].get("name") in (None, "", "Contato sem nome"):
                    self.contacts[lid_jid] = self.contacts[phone_jid].copy()
                    self.contacts[lid_jid]["id"] = lid_jid
                    self.contacts[lid_jid]["remoteJid"] = lid_jid
            
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
            wx.CallAfter(self._schedule_set_chats)

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
                    response = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
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
                                self.register_jid_mapping(lid_jid, canonical_jid, save=False)
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
                    resp_profile = requests.get(url_profile, headers=headers, timeout=_REQUEST_TIMEOUT)
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
                                self.register_jid_mapping(lid_jid, profile_canonical, save=False)
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
                            logging.info(f"[LID Resolution] Profile name not resolved/accepted for {target_jid}. Original name field: {name}. Response data: {res_data}")
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
        wx.CallAfter(self._schedule_refresh_active_messages)

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
            r = requests.get(url, headers=headers, timeout=10)
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
            r = requests.get(url, headers=headers, timeout=10)
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
            r = requests.get(url, headers=headers, timeout=10)
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
                    resp = requests.post(url, json={"phone": phone, "isGroup": is_group, "isLid": is_lid}, headers=headers, timeout=10)
                    logging.info("[subscribe_presence] Response for %s: %s (body: %s)", phone, resp.status_code, resp.text[:200])
                except Exception as e:
                    logging.error("[subscribe_presence] Error subscribing to %s: %s", phone, e)
        threading.Thread(target=_api, daemon=True).start()


    def get_group_info(self, jid: str) -> dict:
        """Fetch group metadata via GET /api/{session}/group-info/{groupId}"""
        url = (
            f"{self.wpp_server}:{self.wpp_port}"
            f"/api/{self.token}/group-info/{jid}"
        )
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        try:
            r = requests.get(url, headers=headers, timeout=10)
            logging.info(f"[get_group_info] status={r.status_code} for {jid}")
            if r.status_code in (200, 201):
                res_data = r.json() or {}
                response = res_data.get("response") or {}
                logging.info(f"[get_group_info] response type={type(response).__name__} keys={list(response.keys()) if isinstance(response, dict) else response}")
                return response if isinstance(response, dict) else {}
        except Exception as e:
            logging.error(f"[get_group_info] error: {e}")
        return {}

    # ── Block ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _bare_phone_digits(jid: str) -> str:
        """Strip the @suffix and any Baileys device suffix (':N'), leaving
        bare phone digits — the form WPPConnect's /blocklist endpoint returns
        (see get_block_list())."""
        if not jid:
            return ""
        local = jid.split("@", 1)[0]
        return local.split(":", 1)[0]

    def is_contact_blocked(self, jid: str) -> bool:
        digits = self._bare_phone_digits(jid)
        if not digits:
            return False
        if digits in self._blocked_contacts:
            return True
        # Brazilian mobile 8/9-digit interchangeable form — a contact can be
        # blocked under either digit count depending on how WhatsApp/the
        # phone reported it, same tolerance _get_contact_tolerant() applies.
        if digits.startswith("55"):
            if len(digits) == 13 and digits[4] == "9":
                if (digits[:4] + digits[5:]) in self._blocked_contacts:
                    return True
            elif len(digits) == 12:
                if (digits[:4] + "9" + digits[4:]) in self._blocked_contacts:
                    return True
        return False

    def get_block_list(self):
        """Fetch the account's blocked-contacts list from WPPConnect and sync
        it into _blocked_contacts. Block state is account-wide, not a
        per-chat field WPPConnect's list-chats response carries (unlike
        mute/pin/archive), so it needs its own endpoint — called from the
        full sync and the periodic chat/contact poll."""
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/blocklist"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code not in (200, 201):
                logging.warning("[get_block_list] HTTP %s", resp.status_code)
                return
            data = resp.json()
            entries = data.get("response", []) if isinstance(data, dict) else []
            digits_set = set()
            for entry in entries:
                if isinstance(entry, dict):
                    phone = entry.get("phone", "")
                elif isinstance(entry, str):
                    phone = entry.split("@")[0]
                else:
                    phone = ""
                if phone:
                    digits_set.add(phone)
            if digits_set != self._blocked_contacts:
                self._blocked_contacts = digits_set
                if hasattr(self, "db") and self.db is not None:
                    self.db.set_metadata_json("blocked_contacts", list(self._blocked_contacts))
                wx.CallAfter(self._schedule_set_chats)
        except Exception as e:
            logging.warning("[get_block_list] failed: %s", e)

    def _apply_block_state(self, jid: str, blocked: bool):
        """Local-only half of block/unblock: mutate _blocked_contacts,
        persist to DB metadata, and refresh the chat list. Split out so
        block_contact() can call this again to roll back the optimistic
        change if WhatsApp rejects it. Safe to call off the main thread —
        _schedule_set_chats() is documented safe from any thread."""
        digits = self._bare_phone_digits(jid)
        if not digits:
            return
        if blocked:
            self._blocked_contacts.add(digits)
        else:
            self._blocked_contacts.discard(digits)
        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("blocked_contacts", list(self._blocked_contacts))
        self._schedule_set_chats()

    def block_contact(self, jid: str, action: str = "block"):
        """action: 'block' or 'unblock'. Runs on a background thread (see
        callers in conversations.py)."""
        blocked = action == "block"
        self._apply_block_state(jid, blocked)
        endpoint = "block-contact" if blocked else "unblock-contact"
        url = (
            f"{self.wpp_server}:{self.wpp_port}"
            f"/api/{self.token}/{endpoint}"
        )
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        try:
            resp = requests.post(
                url, json={"phone": jid},
                headers=headers, timeout=10,
            )
            if not resp.ok:
                logging.warning(
                    "[block_contact] API error %s for %s (%s): %s",
                    resp.status_code, jid, action, resp.text[:200],
                )
                # This call used to be fire-and-forget with no response
                # check at all — the menu item never reflected the real
                # block state (always showed "Bloquear", never toggled to
                # "Desbloquear") and a rejected request left WinZapp
                # believing a contact was blocked when WhatsApp never
                # actually blocked it. Roll back immediately instead.
                wx.CallAfter(self._on_block_sync_rejected, jid, blocked)
        except Exception as exc:
            logging.warning("[block_contact] request failed for %s: %s", jid, exc)
            wx.CallAfter(self._on_block_sync_rejected, jid, blocked)

    def _on_block_sync_rejected(self, jid: str, attempted_blocked: bool):
        """Revert an optimistic block/unblock that WhatsApp did not actually
        accept, and tell the user (runs on the wx main thread)."""
        self._apply_block_state(jid, not attempted_blocked)
        if not self.background_mode:
            self.error_sound.play()
            key = "block_contact_failed" if attempted_blocked else "unblock_contact_failed"
            wx.MessageBox(
                self.i18n.t(key),
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_WARNING,
                self,
            )

    # ── Mute ──────────────────────────────────────────────────────────────────

    def is_chat_muted(self, jid: str) -> bool:
        expiry = self._muted_chats.get(jid)
        if expiry is None:
            return False
        if expiry == -1:
            return True  # permanent
        return time.time() < expiry

    def _apply_mute_state(self, jid: str, expiry):
        """Local-only half of mute/unmute: mutate _muted_chats, persist to
        DB metadata, and refresh the chat list. Split out from
        mute_chat()/unmute_chat() so _sync_mute_to_server() can call this
        again to roll back the optimistic change if WhatsApp rejects it."""
        if expiry is None:
            self._muted_chats.pop(jid, None)
        else:
            self._muted_chats[jid] = expiry
        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("muted_chats", self._muted_chats)
        self._schedule_set_chats()

    def mute_chat(self, jid: str, duration_secs: int):
        """duration_secs=-1 means mute permanently."""
        expiry = -1 if duration_secs == -1 else int(time.time()) + duration_secs
        self._apply_mute_state(jid, expiry)
        self._sync_mute_to_server(jid, duration_secs, rollback_expiry=None)

    def unmute_chat(self, jid: str):
        previous_expiry = self._muted_chats.get(jid)
        self._apply_mute_state(jid, None)
        self._sync_mute_to_server(jid, 0, rollback_expiry=previous_expiry)

    def _sync_mute_to_server(self, jid: str, duration_secs: int, rollback_expiry=None):
        """Send mute/unmute to WPPConnect in a background thread. duration_secs=0 = unmute.
        rollback_expiry is the _muted_chats value to restore if the request
        is rejected (None means "was not muted before this call")."""
        def _do():
            try:
                if duration_secs == 0:
                    wpp_time, wpp_type = 0, "hours"
                elif duration_secs == -1:
                    wpp_time, wpp_type = 8766, "hours"  # ~1 year (closest to permanent)
                elif duration_secs < 3600:
                    # WPPConnect's sendMute also accepts "minutes" granularity
                    # (see WAPI.sendMute's timeType switch: hours/minutes/year)
                    # — using it for sub-hour durations instead of always
                    # rounding up to a full hour matters if this is ever
                    # called with a shorter duration than the UI currently
                    # offers (today's mute presets are all >= 1h, so this
                    # branch is dormant but correct rather than silently
                    # wrong).
                    wpp_time = max(1, duration_secs // 60)
                    wpp_type = "minutes"
                else:
                    wpp_time = duration_secs // 3600
                    wpp_type = "hours"
                url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-mute"
                headers = {"Authorization": f"Bearer {self.token}"}

                def _post(dest: str):
                    payload = {
                        "phone": dest,
                        "time": wpp_time,
                        "type": wpp_type,
                        "isGroup": dest.endswith("@g.us"),
                    }
                    return requests.post(url, json=payload, headers=headers, timeout=10)

                def _accepted(resp) -> bool:
                    return mute_response_accepted(
                        bool(resp.ok), resp.text, duration_secs == 0
                    )

                # Prefer the @lid form when one is known — same preference
                # delete_message_for_everyone()/forward_message() already
                # apply. WPPConnect's legacy WAPI.sendMute resolves the
                # target by looking it up in WhatsApp Web's own in-memory
                # chat store (confirmed live: a failure here returns
                # {"erro":true,"to":"<jid>","status":404}, WAPI's own
                # "not found in store" shape) — that lookup can fail under
                # one JID form while the store genuinely has the chat keyed
                # under the other.
                lid_jid = getattr(self, "_phone_to_lid", {}).get(jid, "")
                primary = lid_jid if lid_jid else jid.replace("@s.whatsapp.net", "@c.us")
                resp = _post(primary)
                ok = _accepted(resp)
                if not ok:
                    logging.warning(
                        "[mute_chat] API error %s for %s: %s",
                        resp.status_code, primary, resp.text[:2000],
                    )
                    fallback = (jid.replace("@s.whatsapp.net", "@c.us")
                                if primary == lid_jid else "")
                    if fallback and fallback != primary:
                        logging.info(
                            "[mute_chat] Retrying %s with alternate JID form %s...",
                            jid, fallback,
                        )
                        resp = _post(fallback)
                        ok = _accepted(resp)
                        if ok:
                            logging.info("[mute_chat] Alternate JID form succeeded for %s", jid)
                        else:
                            logging.warning(
                                "[mute_chat] API error %s for %s (alternate form): %s",
                                resp.status_code, fallback, resp.text[:2000],
                            )
                if not ok:
                    # The mute/unmute call was previously fire-and-forget —
                    # nothing checked whether WPPConnect actually applied it,
                    # so a rejected request (bad JID form, session hiccup,
                    # WPPConnect error) left WinZapp showing a chat as muted
                    # that WhatsApp never muted, until the next full resync
                    # silently "corrected" it back — exactly the "mutei para
                    # sempre, funcionou, mas sumiu depois de reabrir o
                    # programa" report. Roll back immediately instead.
                    wx.CallAfter(self._on_mute_sync_rejected, jid, duration_secs != 0, rollback_expiry)
            except Exception as exc:
                logging.warning("[mute_chat] request failed for %s: %s", jid, exc)
                wx.CallAfter(self._on_mute_sync_rejected, jid, duration_secs != 0, rollback_expiry)
        threading.Thread(target=_do, daemon=True).start()

    def _on_mute_sync_rejected(self, jid: str, attempted_mute: bool, rollback_expiry=None):
        """Revert an optimistic mute/unmute that WhatsApp did not actually
        accept, and tell the user (runs on the wx main thread)."""
        self._apply_mute_state(jid, None if attempted_mute else rollback_expiry)
        if not self.background_mode:
            self.error_sound.play()
            key = "mute_chat_failed" if attempted_mute else "unmute_chat_failed"
            wx.MessageBox(
                self.i18n.t(key),
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_WARNING,
                self,
            )

    # ── Archive ───────────────────────────────────────────────────────────────

    def get_archived_unread_count(self) -> int:
        """Number of archived conversations with unread messages.

        Mirrors _update_title()'s main-list unread tally but restricted to
        archived chats — the counterpart that used to be missing entirely
        once archived chats stopped counting toward the window title.
        """
        deleted = self._deleted_chats
        return sum(
            1 for jid, chat in list(self.chats.items())
            if jid not in deleted
            and self.is_chat_archived(jid)
            and effective_unread_count(chat) > 0
        )

    def is_chat_archived(self, jid: str) -> bool:
        if not jid:
            return False
        jid_norm = self._normalize_jid(jid)
        chat = self.chats.get(jid_norm, {})
        raw = chat.get("archive")
        if raw is None:
            raw = chat.get("archived")
        flag = _parse_bool_flag(raw)
        if flag is not None:
            return flag
        if jid_norm in self._archived_chats:
            return True

        alt_jid = ""
        if jid_norm.endswith("@lid"):
            alt_jid = getattr(self, "_lid_to_phone", {}).get(jid_norm, "")
        else:
            alt_jid = getattr(self, "_phone_to_lid", {}).get(jid_norm, "")

        if alt_jid:
            alt_jid_norm = self._normalize_jid(alt_jid)
            alt_chat = self.chats.get(alt_jid_norm, {})
            alt_raw = alt_chat.get("archive")
            if alt_raw is None:
                alt_raw = alt_chat.get("archived")
            alt_flag = _parse_bool_flag(alt_raw)
            if alt_flag is not None:
                return alt_flag
            if alt_jid_norm in self._archived_chats:
                return True

        return False


    def _set_archived_state(self, jid: str, archived: bool):
        """Apply an archive decision to both the chat record and the metadata set.

        Both have to move together: the chat record is what the list builder
        and is_chat_archived() consult first (it carries the server's truth),
        while the set is what survives a restart. Supports LID <-> Phone JID mapping.
        """
        if not jid:
            return
        jid_norm = self._normalize_jid(jid)
        alt_jid = ""
        if jid_norm.endswith("@lid"):
            alt_jid = getattr(self, "_lid_to_phone", {}).get(jid_norm, "")
        else:
            alt_jid = getattr(self, "_phone_to_lid", {}).get(jid_norm, "")

        targets = [jid_norm]
        if alt_jid:
            targets.append(self._normalize_jid(alt_jid))

        for target in targets:
            if archived:
                self._archived_chats.add(target)
            else:
                self._archived_chats.discard(target)
            chat = self.chats.get(target)
            if chat is not None:
                chat["archive"] = archived
                chat["archived"] = archived

        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("archived_chats", list(self._archived_chats))
            try:
                for target in targets:
                    chat = self.chats.get(target)
                    if chat is not None:
                        self.db.upsert_chat(target, chat)
            except Exception as exc:
                logging.warning("[_set_archived_state] DB update failed for %s: %s", jid, exc)
        self._schedule_set_chats()

    def archive_chat(self, jid: str):
        self._set_archived_state(jid, True)
        self._api_archive_chat(jid, archive=True)

    def unarchive_chat(self, jid: str):
        self._set_archived_state(jid, False)
        self._api_archive_chat(jid, archive=False)

    def _api_archive_chat(self, jid: str, archive: bool):
        url = (f"{self.wpp_server}:{self.wpp_port}"
               f"/api/{self.token}/archive-chat")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        # Prefer `@lid` for API operations if mapped, as WPPConnect expects it
        api_jid = jid
        if not api_jid.endswith("@lid"):
            alt_lid = getattr(self, "_phone_to_lid", {}).get(self._normalize_jid(jid), "")
            if alt_lid:
                api_jid = alt_lid

        try:
            resp = requests.post(
                url,
                json={"phone": api_jid, "value": archive, "isGroup": jid.endswith("@g.us")},
                headers=headers,
                timeout=10,
            )
            if not resp.ok:
                print(f"[archive_chat] API error {resp.status_code} for {jid} (api_jid: {api_jid}): {resp.text[:200]}")
        except Exception as exc:
            print(f"[archive_chat] Request failed for {jid}: {exc}")

    # ── Delete / Clear ────────────────────────────────────────────────────────

    def is_chat_deleted(self, jid: str) -> bool:
        return jid in self._deleted_chats

    def delete_chat_local(self, jid: str):
        if jid not in self._deleted_chats:
            self._deleted_chats.add(jid)
        if jid.endswith("@s.whatsapp.net"):
            lid_jid = getattr(self, "_phone_to_lid", {}).get(jid)
            if lid_jid and lid_jid not in self._deleted_chats:
                self._deleted_chats.add(lid_jid)
        elif jid.endswith("@lid"):
            phone_jid = getattr(self, "_lid_to_phone", {}).get(jid)
            if phone_jid and phone_jid not in self._deleted_chats:
                self._deleted_chats.add(phone_jid)
        self.chats.pop(jid, None)
        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("deleted_chats", list(self._deleted_chats))
            # Drop the row as well, not just the "deleted" marker.  Leaving it
            # in the database meant the conversation was reloaded into
            # self.chats on the next start and only hidden by the marker — so
            # anything that lost or bypassed the marker (as get_remote_chats
            # did, reading it from the wrong place) brought the chat back.
            try:
                self.db.delete_chat(jid)
            except Exception as exc:
                logging.warning("[delete_chat_local] DB delete failed for %s: %s", jid, exc)
        self._schedule_save()
        self._schedule_set_chats()

    def clear_chat_messages_local(self, jid: str, record_cutoff: bool = True):
        """Empty a conversation locally, keeping it in the chat list.

        Clearing removes the messages and the last-message preview — it must
        NOT remove the conversation itself; that is what "delete chat" does.
        `record_cutoff` is False when we are only mirroring a clear that already
        happened on the phone (no new cutoff to remember, the server is the
        source of truth).
        """
        chat = self.chats.get(jid)
        if not chat:
            return
        chat.setdefault("messages", {}).setdefault("messages", {})["records"] = []
        chat["lastMessage"] = None
        chat["unreadCount"] = 0
        if record_cutoff:
            self.settings.setdefault("cleared_chats", {})[jid] = int(time.time())
            self.save_settings()
        self._schedule_save(dirty_jid=jid)
        if hasattr(self, "db") and self.db is not None:
            try:
                self.db.delete_chat_messages(jid)
                self.db.upsert_chat(jid, chat)
            except Exception as exc:
                logging.warning("[clear_chat_messages_local] DB clear failed for %s: %s", jid, exc)

    def delete_chat(self, jid: str):
        """Delete chat locally and sync to WPPConnect API."""
        self.delete_chat_local(jid)
        if jid.endswith("@g.us"):
            # NEVER send delete-chat for a group.  WhatsApp has no concept of
            # "delete this group conversation but stay in it": its internal
            # sendDelete on a group you are still a member of exits the group
            # first — which is how users who only meant to tidy up their chat
            # list found themselves removed from groups.  Deleting a group is
            # therefore local-only here; leaving is a separate, explicit action
            # (leave_group / the "Sair do grupo" menu item).
            logging.info("[delete_chat] %s is a group — deleting locally only "
                         "(a server-side delete would leave the group).", jid)
            return
        def _api():
            phone = jid.replace("@s.whatsapp.net", "@c.us")
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/delete-chat"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            try:
                r = requests.post(
                    url, json={"phone": [phone], "isGroup": phone.endswith("@g.us")},
                    headers=headers, timeout=10,
                )
                if not r.ok:
                    logging.warning("[delete_chat] API error %s for %s: %s", r.status_code, jid, r.text[:200])
            except Exception as exc:
                logging.warning("[delete_chat] Request failed for %s: %s", jid, exc)
        threading.Thread(target=_api, daemon=True).start()

    def clear_chat(self, jid: str):
        """Clear chat messages locally and sync to WPPConnect API."""
        self.clear_chat_messages_local(jid)
        def _api():
            phone = jid.replace("@s.whatsapp.net", "@c.us")
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/clear-chat"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            try:
                r = requests.post(
                    url, json={"phone": [phone], "isGroup": phone.endswith("@g.us")},
                    headers=headers, timeout=10,
                )
                if not r.ok:
                    logging.warning("[clear_chat] API error %s for %s: %s", r.status_code, jid, r.text[:200])
            except Exception as exc:
                logging.warning("[clear_chat] Request failed for %s: %s", jid, exc)
        threading.Thread(target=_api, daemon=True).start()

    def _resolve_jid_for_chat_state(self, jid: str) -> str:
        """Resolve to the active JID for chat state, preferring @lid if mapped."""
        if not jid:
            return jid
        if jid.endswith(("@g.us", "@broadcast")):
            return jid.replace("@s.whatsapp.net", "@c.us")
        
        normalized = jid.replace("@c.us", "@s.whatsapp.net")
        lid = getattr(self, "_phone_to_lid", {}).get(normalized, "")
        if lid:
            return lid.replace("@s.whatsapp.net", "@lid")
            
        if jid.endswith("@lid"):
            return jid
            
        return jid.replace("@s.whatsapp.net", "@c.us")

    def send_typing_status(self, jid: str, value: bool, is_group: bool = False):
        """Notify WPPConnect that the user started or stopped typing."""
        def _api():
            phone = self._resolve_jid_for_chat_state(jid)
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/typing"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            try:
                r = requests.post(
                    url,
                    json={"phone": phone, "value": value, "isGroup": is_group},
                    headers=headers,
                    timeout=10,
                )
                if not r.ok:
                    logging.warning("[send_typing_status] API error %s: %s", r.status_code, r.text)
            except Exception as exc:
                logging.warning("[send_typing_status] Request failed: %s", exc)
        threading.Thread(target=_api, daemon=True).start()

    def send_recording_status(self, jid: str, value: bool, is_group: bool = False):
        """Notify WPPConnect that the user started or stopped recording audio."""
        def _api():
            phone = self._resolve_jid_for_chat_state(jid)
            url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/recording"
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            try:
                r = requests.post(
                    url,
                    json={"phone": phone, "duration": 0, "value": value, "isGroup": is_group},
                    headers=headers,
                    timeout=10,
                )
                if not r.ok:
                    logging.warning("[send_recording_status] API error %s: %s", r.status_code, r.text)
            except Exception as exc:
                logging.warning("[send_recording_status] Request failed: %s", exc)
        threading.Thread(target=_api, daemon=True).start()

    def _is_cleared_message(self, jid: str, msg: dict) -> bool:
        """
        True if `msg` predates the user's last "clear chat" action for `jid`.

        Clearing a conversation records a cutoff timestamp in
        settings["cleared_chats"]. Without consulting it, the next history sync
        (or a WebSocket re-delivery) would simply repopulate the chat, making the
        clear appear to do nothing. Messages received after the clear have a
        newer timestamp and are kept.
        """
        cutoff = self.settings.get("cleared_chats", {}).get(jid)
        if not cutoff:
            return False
        try:
            ts = int(msg.get("messageTimestamp", 0) or 0)
        except (ValueError, TypeError):
            return False
        return bool(ts) and ts < cutoff

    # ── Pin ───────────────────────────────────────────────────────────────────

    def is_chat_pinned(self, jid: str) -> bool:
        return jid in self._pinned_chats

    def _apply_pin_state(self, jid: str, pinned: bool):
        """Local-only half of pin/unpin: mutate _pinned_chats (+ its alt-JID
        mirror), persist to DB metadata, and refresh the chat list. Split out
        from pin_chat()/unpin_chat() so _sync_pin_to_server() can call this
        again to roll back the optimistic change if WhatsApp rejects it,
        without recursing back into a server call."""
        normalized = self._normalize_jid(jid)
        if pinned:
            self._pinned_chats.add(normalized)
        else:
            self._pinned_chats.discard(normalized)
        # Also mirror onto the alternate JID form if present
        if normalized.endswith("@lid"):
            alt = getattr(self, "_lid_to_phone", {}).get(normalized, "")
            if alt:
                alt = self._normalize_jid(alt)
                self._pinned_chats.add(alt) if pinned else self._pinned_chats.discard(alt)
        else:
            alt = getattr(self, "_phone_to_lid", {}).get(normalized, "")
            if alt:
                self._pinned_chats.add(alt) if pinned else self._pinned_chats.discard(alt)

        if hasattr(self, "db") and self.db is not None:
            self.db.set_metadata_json("pinned_chats", list(self._pinned_chats))
        self._schedule_set_chats()

    def pin_chat(self, jid: str):
        self._apply_pin_state(jid, True)
        self._sync_pin_to_server(jid, pinned=True)

    def unpin_chat(self, jid: str):
        self._apply_pin_state(jid, False)
        self._sync_pin_to_server(jid, pinned=False)

    def _sync_pin_to_server(self, jid: str, pinned: bool):
        def _do():
            try:
                # Prefer `@lid` for API operations if mapped, as WPPConnect expects it
                api_jid = jid
                if not api_jid.endswith("@lid"):
                    alt_lid = getattr(self, "_phone_to_lid", {}).get(self._normalize_jid(jid), "")
                    if alt_lid:
                        api_jid = alt_lid

                if api_jid.endswith("@s.whatsapp.net"):
                    api_jid = api_jid.rsplit("@", 1)[0] + "@c.us"
                url = (f"{self.wpp_server}:{self.wpp_port}"
                       f"/api/{self.token}/pin-chat")
                payload = {
                    "phone": [api_jid],
                    "state": "true" if pinned else "false",
                    "isGroup": jid.endswith("@g.us"),
                }
                resp = requests.post(
                    url, json=payload,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=10,
                )
                if not resp.ok:
                    logging.warning("[pin_chat] API error %s for %s (api_jid: %s): %s",
                                    resp.status_code, jid, api_jid, resp.text[:200])
                    # WhatsApp rejected the change — most commonly because it
                    # only allows 3 pinned chats at once, an existing-account
                    # rule WPPConnect enforces server-side that WinZapp never
                    # checked before sending the request. The optimistic local
                    # update above (_apply_pin_state, already applied before
                    # this thread ran) was never actually accepted by
                    # WhatsApp, so left uncorrected it silently drifted out of
                    # sync — the chat looked pinned in WinZapp until the next
                    # periodic chat-list poll (up to 60s later) quietly
                    # "unpinned" it again, which is exactly the erratic
                    # pin behaviour reported. Roll it back immediately and
                    # tell the user why instead of waiting for that poll.
                    wx.CallAfter(self._on_pin_sync_rejected, jid, pinned)
            except Exception as exc:
                logging.warning("[pin_chat] request failed for %s: %s", jid, exc)
        threading.Thread(target=_do, daemon=True).start()

    def _on_pin_sync_rejected(self, jid: str, attempted_pinned: bool):
        """Revert an optimistic pin/unpin that WhatsApp did not actually
        accept, and tell the user (runs on the wx main thread)."""
        self._apply_pin_state(jid, not attempted_pinned)
        if not self.background_mode:
            self.error_sound.play()
            key = "pin_chat_failed" if attempted_pinned else "unpin_chat_failed"
            wx.MessageBox(
                self.i18n.t(key),
                self.i18n.t("error").format(app_name=self.app_name),
                wx.OK | wx.ICON_WARNING,
                self,
            )

    # ── Group ─────────────────────────────────────────────────────────────────

    def leave_group(self, jid: str):
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/leave-group"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        try:
            requests.post(url, json={"groupId": jid}, headers=headers, timeout=10)
        except Exception:
            pass
        # Archive instead of delete so the message history is preserved locally.
        self.archive_chat(jid)

    def create_group(self, name: str, participants: list) -> tuple:
        """
        Create a WhatsApp group with the given name and participant numbers.
        participants: list of phone number or JID strings (e.g. ["5511999999999@s.whatsapp.net", "63977983840477@lid"])
        Returns (True, group_jid) on success, (False, error_message) on failure.
        """
        # Normalize participant JIDs for WPPConnect
        normalized_participants = []
        for p in participants:
            if "@" in p:
                p_norm = p.replace("@s.whatsapp.net", "@c.us")
                normalized_participants.append(p_norm)
            else:
                # Default to c.us for raw typed digits (phone numbers)
                normalized_participants.append(f"{p}@c.us")

        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/create-group"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "name":         name,
            "participants": normalized_participants,
        }
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=30)
            if r.status_code in (200, 201):
                resp = r.json().get("response", {})
                gid = resp.get("gid", {})
                if isinstance(gid, dict):
                    gid = gid.get("_serialized", "")
                return True, gid or ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    def add_group_members(self, group_jid: str, participant_jids: list) -> tuple:
        """
        Add one or more participants to a group.
        Returns (True, "") on success, (False, error_message) on failure.
        """
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/add-participant-group"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        # groupController.ts's addParticipant() (upstream, unpatched) reads
        # req.body.phone — same field name remove/promote/demote already use
        # below via _group_participant_action(). This used to send
        # "participantId" instead, a field the controller never reads at
        # all, so phone came through as undefined and every add attempt
        # failed with a 500 inside contactToArray(undefined).
        payload = {
            "groupId": group_jid,
            "phone":   [j if "@" in j else f"{j}@c.us" for j in participant_jids],
        }
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    def _group_participant_action(self, endpoint: str, group_jid: str, participant_jids: list) -> tuple:
        """Shared POST for remove/promote/demote-participant-group — all
        three take the same {groupId, phone} shape."""
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {
            "groupId": group_jid,
            "phone": [j if "@" in j else f"{j}@c.us" for j in participant_jids],
        }
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    def remove_group_members(self, group_jid: str, participant_jids: list) -> tuple:
        """Remove one or more participants from a group.
        Returns (True, "") on success, (False, error_message) on failure."""
        return self._group_participant_action("remove-participant-group", group_jid, participant_jids)

    def promote_group_members(self, group_jid: str, participant_jids: list) -> tuple:
        """Promote one or more participants to group admin."""
        return self._group_participant_action("promote-participant-group", group_jid, participant_jids)

    def demote_group_members(self, group_jid: str, participant_jids: list) -> tuple:
        """Demote one or more participants from group admin."""
        return self._group_participant_action("demote-participant-group", group_jid, participant_jids)

    def set_group_subject(self, group_jid: str, title: str) -> tuple:
        """Change a group's name/subject. Returns (True, "") or (False, error)."""
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/group-subject"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {"groupId": group_jid, "title": title}
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    def set_group_description(self, group_jid: str, description: str) -> tuple:
        """Change a group's description. Returns (True, "") or (False, error)."""
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/group-description"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        payload = {"groupId": group_jid, "description": description}
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return True, ""
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    # ── Media / contact attachments ───────────────────────────────────────────

    def send_media_attachment(
        self, remote_jid: str, file_path: str,
        media_type: str, caption: str = "", quoted: dict = None
    ) -> bool:
        """
        Upload a file as a media message via multipart/form-data.
        Avoids base64 encoding so payloads stay at true file size
        (no 33 % overhead, no JSON body-size limit).
        media_type: 'image' | 'video' | 'audio' | 'document'
        """
        # Canonical destination: @lid when known, else the @c.us phone form —
        # see _resolve_jid_for_send's docstring for why @lid has to win here.
        remote_jid = self._resolve_jid_for_send(remote_jid)
        is_lid_target = remote_jid.endswith("@lid")
        logging.info("[send_media] destination resolved to %s (isLid=%s)", remote_jid, is_lid_target)
        import mimetypes
        try:
            file_size = os.path.getsize(file_path)
        except Exception as exc:
            logging.error("[send_media] failed to stat file %s: %s", file_path, exc)
            return False
        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        filename = os.path.basename(file_path)
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/send-file"
        # Authorization only — Content-Type is set automatically by requests
        # when using files= (multipart/form-data with correct boundary).
        headers = {"Authorization": f"Bearer {self.token}"}
        phone_val = remote_jid
        if phone_val.endswith("@s.whatsapp.net"):
            phone_val = phone_val.replace("@s.whatsapp.net", "@c.us")
        # Force WPPConnect to send the chosen WhatsApp message type instead of
        # its mimetype-based "auto-detect", which otherwise sends e.g. an .mp3
        # picked from the "Document" menu as a playable audio message, or a
        # .jpg/.png as a photo, regardless of what the user actually selected.
        _wpp_type = {
            "image": "image", "video": "video",
            "audio": "audio", "document": "document",
        }.get(media_type, "document")
        data = {
            "filename": filename,
            "caption":  caption,
            "type":     _wpp_type,
        }
        if quoted:
            quoted_id = self._serialize_quoted_id(quoted, fallback_jid=remote_jid)
            if quoted_id:
                data["quotedMessageId"] = quoted_id
        # Scale timeout with file size: at least 1 s per 100 KB, min 120 s, max 30 min.
        timeout = max(120, file_size // (100 * 1024))
        timeout = min(timeout, 1800)

        def _post(dest: str):
            """POST the upload to `dest`, reopening the file (a retry cannot
            reuse the already-consumed handle).

            multipart/form-data has no booleans: requests serializes False as
            the string "False", which is *truthy* in JavaScript — so a plain
            `"isGroup": False` made WPPConnect's statusConnection/contactToArray
            treat every media send as a group send and rewrite the destination
            to `<digits>@g.us`. Only send these flags when they are true.
            """
            post_data = dict(data, phone=[dest])
            if dest.endswith("@g.us"):
                post_data["isGroup"] = "true"
            if dest.endswith("@lid"):
                post_data["isLid"] = "true"
            with open(file_path, "rb") as fh:
                return requests.post(
                    url,
                    headers=headers,
                    data=post_data,
                    files={"file": (filename, fh, mime)},
                    timeout=timeout,
                )

        try:
            r = _post(phone_val)
            if r.status_code not in (200, 201):
                # Same legacy fallback as the text/audio paths: a @lid
                # destination that WhatsApp Web refuses gets one attempt on the
                # old @c.us address, unless the session is simply disconnected.
                fb_phone = self._legacy_phone_for_send(remote_jid) if is_lid_target else ""
                if fb_phone and not self._check_wa_connection_closed(r):
                    logging.warning("[send_media] @lid destination %s refused (HTTP %s) — retrying with legacy %s",
                                    remote_jid, r.status_code, fb_phone)
                    r = _post(fb_phone)
                    if r.status_code in (200, 201):
                        logging.info("[send_media] legacy retry with %s succeeded", fb_phone)
            if r.status_code in (200, 201):
                body = r.json()
                resp = body.get("response", body)
                if isinstance(resp, list) and resp:
                    resp = resp[0]
                msg_id = ""
                if isinstance(resp, dict):
                    msg_id = resp.get("id") or resp.get("key", {}).get("id") or ""
                    if isinstance(msg_id, dict):
                        msg_id = msg_id.get("_serialized", "")
                    if msg_id:
                        parts = msg_id.split("_")
                        msg_id = parts[2] if len(parts) > 2 else (parts[-1] if parts else msg_id)
                if msg_id:
                    return msg_id
                return {"ok": True, "error": "ID not found in response"}
            err = f"HTTP {r.status_code}"
            inner_error_name = ""
            try:
                body = r.json()
                inner_error = body.get("error")
                inner_message = ""
                if isinstance(inner_error, dict):
                    inner_error_name = inner_error.get("name") or ""
                    inner_message = inner_error.get("message") or ""
                elif isinstance(inner_error, str):
                    inner_message = inner_error
                detail = inner_message or (body.get("message") or "")
                if detail:
                    err = f"{err}: {detail}"
            except Exception:
                if r.text:
                    err = f"{err}: {r.text[:200]}"
            logging.error("[send_media] %s for %s (%s, %.1f MB): %s",
                          err, remote_jid, filename, file_size / (1024*1024), r.text[:300])
            # 5xx responses are transient server/puppeteer hiccups — notably the
            # WPPConnect "ProtocolError: Promise was collected" that strikes large
            # uploads under load. Retry those; treat 4xx as permanent.
            if self._check_wa_connection_closed(r):
                return {"ok": False, "error": err, "retry": False, "disconnected": True}
            # MediaUnsupportedError (observed as "video loaded with duration
            # but no dims") means WhatsApp Web's own upload pipeline rejected
            # THIS specific file as malformed/unprocessable — retrying sends
            # the exact same bytes again and fails identically every time, so
            # unlike a generic 5xx this is never transient. Skip the retry
            # loop and surface a clear reason immediately instead of the
            # generic "Erro ao enviar a mensagem" after 4 wasted attempts.
            if inner_error_name == "MediaUnsupportedError":
                return {
                    "ok": False,
                    "error": self.i18n.t("media_unsupported_error"),
                    "retry": False,
                }
            retryable = r.status_code >= 500
            return {"ok": False, "error": err, "retry": retryable}
        except Exception as exc:
            return self._classify_send_exception(exc, "send_media")

    def send_contact_attachment(self, remote_jid: str, contact_info: dict,
                                quoted: dict = None) -> bool:
        """Send a contact card as an attachment."""
        # Canonical destination: @lid when known, else the @c.us phone form —
        # see _resolve_jid_for_send's docstring for why @lid has to win here.
        remote_jid = self._resolve_jid_for_send(remote_jid)
        is_lid_target = remote_jid.endswith("@lid")
        logging.info("[send_contact_attachment] destination resolved to %s (isLid=%s)", remote_jid, is_lid_target)
        is_group = remote_jid.endswith("@g.us")
        if is_group:
            remote_jid = remote_jid.split("@")[0]
        name = contact_info.get("pushName") or ""
        jid = contact_info.get("remoteJid", "")
        phone_raw = jid.split("@")[0] if "@" in jid else jid
        url = f"{self.wpp_server}:{self.wpp_port}/api/{self.token}/contact-vcard"
        payload = {
            "phone":       [remote_jid],
            "isGroup":     is_group,
            "isLid":       is_lid_target,
            "contactsId":  [f"{phone_raw}@c.us"],
        }
        if quoted:
            quoted_id = self._serialize_quoted_id(quoted, fallback_jid=remote_jid)
            if quoted_id:
                payload["quotedMessageId"] = quoted_id
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        def _parse(r):
            try:
                resp = r.json().get("response", {})
                if isinstance(resp, list) and resp:
                    resp = resp[0]
                return (resp or {}).get("id") or True
            except Exception:
                return True

        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return _parse(r)
            # Same legacy fallback as the other send paths.
            fb_phone = self._legacy_phone_for_send(remote_jid) if is_lid_target else ""
            if fb_phone and not self._check_wa_connection_closed(r):
                logging.warning("[send_contact_attachment] @lid destination %s refused (HTTP %s) — retrying with legacy %s",
                                remote_jid, r.status_code, fb_phone)
                payload["phone"] = [fb_phone]
                payload["isLid"] = False
                r = requests.post(url, json=payload, headers=headers, timeout=15)
                if r.status_code in (200, 201):
                    logging.info("[send_contact_attachment] legacy retry with %s succeeded", fb_phone)
                    return _parse(r)
            logging.error("[send_contact_attachment] HTTP %s for %s: %s", r.status_code, remote_jid, r.text[:300])
            return None
        except Exception as exc:
            logging.error("[send_contact_attachment] exception for %s: %s", remote_jid, exc)
            return None

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
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code not in (200, 201):
                logging.error("[edit_message] HTTP %s for %s: %s",
                              r.status_code, full_id, r.text[:300])
        except Exception as exc:
            logging.error("[edit_message] exception for %s: %s", full_id, exc)

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
            r = requests.post(url, json=payload, headers=headers, timeout=15)
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
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code in (200, 201):
                return True
            logging.error("[delete_for_me] HTTP %s for %s: %s",
                          r.status_code, full_id, r.text[:300])
            return False
        except Exception as exc:
            logging.error("[delete_for_me] exception for %s: %s", full_id, exc)
            return False

    def forward_message(self, source_jid: str, msg_key: dict, target_jid: str) -> bool:
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
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=20)
            if r.status_code in (200, 201):
                return True
            logging.error("[forward_message] HTTP %s for %s -> %s: %s",
                          r.status_code, full_id, target_phone, r.text[:300])
            return False
        except Exception as exc:
            logging.error("[forward_message] exception for %s -> %s: %s", full_id, target_phone, exc)
            return False

    def mark_audio_message_played(self, msg: dict):
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
        # Local status icon — reuses the exact same path a real
        # messages.update WebSocket event drives (find the cached record,
        # append MessageUpdate, persist to DB, refresh the visible row).
        self.on_message_status_update({
            "key": {"id": msg_id, "remoteJid": remote_jid},
            "status": "5",
        })
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
            r = requests.post(url, json={"messageId": full_id}, headers=headers, timeout=15)
            if r.status_code not in (200, 201):
                logging.warning(
                    "[mark_audio_played] HTTP %s for %s: %s",
                    r.status_code, full_id, r.text[:200],
                )
        except Exception as exc:
            logging.warning("[mark_audio_played] request failed for %s: %s", full_id, exc)

    def _preview_sender_from_jid(self, jid: str) -> str:
        """
        Resolve a participant JID to a display name for chat list previews.
        Tries contacts dict (with @lid bridging), then falls back to
        format_number on the phone-number JID. Never returns a bare @lid string.
        """
        if not jid:
            return ""
        ppm = getattr(self, "_presence_pushname_map", {})
        phone_jid = ""
        contact = self._get_contact_tolerant(jid)
        if not contact and jid.endswith("@lid"):
            phone_jid = getattr(self, "_lid_to_phone", {}).get(jid, "")
            if phone_jid:
                contact = self._get_contact_tolerant(phone_jid)
        if contact:
            name = (contact.get("name") or contact.get("pushName") or "").strip()
            if name and not is_phone_like(name):
                return name
        # Fallback: presence-learned pushName map
        for lookup_jid in ([jid, phone_jid] if phone_jid else [jid]):
            pname = (ppm.get(lookup_jid) or "").strip()
            if pname and not pname.isdigit() and not is_phone_like(pname):
                return pname
        if jid.endswith("@lid"):
            if not phone_jid:
                phone_jid = getattr(self, "_lid_to_phone", {}).get(jid, "")
            return format_number(phone_jid) if phone_jid else self.i18n.t("unnamed_participant")
        if jid.endswith("@g.us"):
            return self.i18n.t("unknown_group")
        return format_number(jid)

    # Message types that count as "the conversation's last message" — the ones
    # a user would recognise as activity. Deliberately excludes the silent
    # bookkeeping WhatsApp stores alongside real messages: groupNotification
    # ("X entrou no grupo"), notification_template, and every unknown type.
    #
    # reactionMessage is ALSO deliberately excluded, even though a
    # reactionMessage record legitimately sits in a chat's `records` (see
    # on_historical_message() — needed there so ConversationsPanel can
    # rebuild the in-conversation reaction display on reopen). A reaction
    # has its own dedicated preview channel — chat["_last_reaction"], set by
    # _track_last_reaction() and consumed directly by _last_msg_preview()
    # before it ever reaches this allowlist — so letting a reactionMessage
    # record ALSO count here served no purpose and was actively harmful:
    # since _last_msg_preview() has no rendering case for messageType
    # "reactionMessage", picking one as `last` (which happened whenever
    # _last_reaction hadn't been (re)populated for it, e.g. right after an
    # app restart or an F5 resync, since _last_reaction is in-memory only)
    # rendered as literally "Mensagem incompatível". It also let a bare
    # reaction's timestamp float a chat to the top of the list via
    # _chat_last_ts(), independent of any real message ever being sent.
    _PREVIEW_MESSAGE_TYPES = frozenset({
        "conversation", "extendedTextMessage", "imageMessage", "videoMessage",
        "audioMessage", "documentMessage", "stickerMessage", "contactMessage",
        "locationMessage", "liveLocationMessage",
        "pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3",
        "pollUpdateMessage",
        "buttonsMessage", "listMessage", "templateMessage", "interactiveMessage",
        "buttonsResponseMessage", "listResponseMessage", "protocolMessage",
    })

    @classmethod
    def _counts_as_last_message(cls, m) -> bool:
        """True when a record should decide a chat's preview and its position.

        Both the preview and the sort key have to agree on this, or the list
        contradicts itself: a group where someone merely joined jumps to the top
        while still showing a week-old preview, because the join was counted for
        ordering but skipped for display. Observed live — a group whose newest
        stored record was a groupNotification at 09:52 sat above conversations
        from minutes earlier while displaying its real last message from five
        days before.
        """
        if not isinstance(m, dict):
            return False
        # A message that permanently failed to send (retries exhausted,
        # _mark_message_failed()) never reached WhatsApp — it must not be
        # treated as the conversation's real last message. Without this the
        # chat-list preview kept showing a message the recipient never got,
        # with no visible cue anything was wrong, until the user happened to
        # reopen the conversation (which rebuilds this from the same
        # records — so it "fixed itself" there for an unrelated reason, not
        # because this case was actually handled).
        if m.get("_send_failed"):
            return False
        m_type = m.get("messageType", "")
        if m_type not in cls._PREVIEW_MESSAGE_TYPES:
            return False
        if m_type == "protocolMessage":
            protocol = (m.get("message") or {}).get("protocolMessage") or {}
            return protocol.get("type") in (3, "REVOKE", "revoke")
        return True

    def _recompute_chat_last_message(self, jid: str):
        """Recompute chat["lastMessage"]/["t"] from the records still present
        after one or more messages were removed (local delete, revoke-for-
        everyone, remote-mirrored deletion). Both the preview and the sort
        key (_chat_last_ts) fall back to these fields whenever they are newer
        than anything left in records, so without this a deleted message kept
        the chat pinned at its old position with its old preview text forever."""
        chat = self.chats.get(jid)
        if not chat:
            return

        def _ts(m):
            val = int(m.get("messageTimestamp") or m.get("timestamp") or m.get("t") or 0)
            return val // 1000 if val > 1_000_000_000_000 else val

        records_wrapper = chat.get("messages") or {}
        records = []
        if isinstance(records_wrapper, dict):
            inner_wrapper = records_wrapper.get("messages") or {}
            if isinstance(inner_wrapper, dict):
                records = inner_wrapper.get("records") or []

        candidates = [m for m in records if self._counts_as_last_message(m)]
        if candidates:
            last = max(candidates, key=_ts)
            chat["lastMessage"] = last
            chat["t"] = _ts(last)
        else:
            chat["lastMessage"] = None
            chat["t"] = 0

        if hasattr(self, "db") and self.db is not None:
            try:
                self.db.upsert_chat(jid, chat)
            except Exception as exc:
                logging.warning(
                    "[_recompute_chat_last_message] DB upsert failed for %s: %s", jid, exc
                )

    def _last_msg_preview(self, chat: dict) -> str:
        """
        Build a compact last-message description for the conversations list.
        Returns "" if no messages are found.
        Format: "[você: ]{content} {timestamp}"
        """
        records_wrapper = chat.get("messages") or {}
        records = []
        if isinstance(records_wrapper, dict):
            inner_wrapper = records_wrapper.get("messages") or {}
            if isinstance(inner_wrapper, dict):
                records = list(inner_wrapper.get("records") or [])
        if not records:
            return ""

        # Shared with _chat_last_ts() so the preview and the ordering can never
        # disagree about which record is a chat's last message.
        is_displayable = self._counts_as_last_message

        def _get_ts(m):
            if not isinstance(m, dict):
                return 0
            val = int(m.get("timestamp", 0) or m.get("messageTimestamp", 0) or m.get("t", 0) or 0)
            return val // 1000 if val > 1_000_000_000_000 else val

        try:
            last = max(
                (m for m in records if is_displayable(m)),
                key=_get_ts,
                default=None,
            )
        except Exception:
            last = None

        i18n = self.i18n

        # A reaction is deliberately never added to `records` (on_new_message
        # returns early for messageType == "reactionMessage" so it can't
        # pollute the message list or unread counts) — it's tracked
        # separately in chat["_last_reaction"] instead (see
        # _track_last_reaction()). Show it here in place of the last real
        # message only when it is genuinely the most recent event in the
        # chat; a reaction to an older message must not resurrect itself as
        # the preview once newer messages have since arrived.
        last_reaction = chat.get("_last_reaction")
        if last_reaction and last_reaction.get("timestamp", 0) >= _get_ts(last):
            emoji = last_reaction.get("emoji", "")
            orig_text = ""
            target_id = last_reaction.get("target_id", "")
            if target_id:
                for m in records:
                    if isinstance(m, dict) and m.get("key", {}).get("id") == target_id:
                        orig_type = m.get("messageType", "")
                        orig_obj  = m.get("message") or {}
                        if orig_type == "conversation":
                            orig_text = (orig_obj.get("conversation") or "")
                        elif orig_type == "extendedTextMessage":
                            orig_text = ((orig_obj.get("extendedTextMessage") or {}).get("text") or "")
                        elif orig_type == "audioMessage":
                            orig_text = i18n.t("message_type_audio")
                        elif orig_type == "videoMessage":
                            orig_text = i18n.t("video")
                        elif orig_type == "imageMessage":
                            orig_text = i18n.t("photo")
                        elif orig_type == "documentMessage":
                            orig_text = i18n.t("document")
                        elif orig_type == "stickerMessage":
                            orig_text = i18n.t("sticker")
                        elif orig_type == "contactMessage":
                            orig_text = i18n.t("notif_contact")
                        elif orig_type == "locationMessage":
                            orig_text = i18n.t("notif_location")
                        else:
                            orig_text = i18n.t("notif_unsupported")
                        break
            ts_val = last_reaction.get("timestamp", 0)
            time_str = ""
            if ts_val:
                try:
                    from datetime import datetime as _dt
                    dt    = _dt.fromtimestamp(ts_val)
                    today = _dt.now().date()
                    if dt.date() == today:
                        time_str = dt.strftime(get_time_format(i18n.t("time_fmt")))
                    else:
                        time_str = dt.strftime(get_datetime_format(i18n.t("datetime_fmt")))
                except Exception:
                    pass
            if last_reaction.get("from_me"):
                label = i18n.t("reaction_preview_you").format(emoji=emoji)
            else:
                sender_jid = last_reaction.get("participant", "")
                push       = last_reaction.get("push_name", "")
                if sender_jid.endswith("@g.us") and push and push.isdigit():
                    sender_jid = f"{push}@s.whatsapp.net"
                sender_name = (
                    self._resolve_contact_name({"remoteJid": sender_jid})
                    or (push if push and not is_phone_like(push) else "")
                    or self._preview_sender_from_jid(sender_jid)
                )
                label = i18n.t("reaction_preview_them").format(name=sender_name, emoji=emoji)
            parts = [label]
            if orig_text:
                parts.append(orig_text)
            if time_str:
                parts.append(time_str)
            return " ".join(parts)

        if last is None:
            return ""

        from_me  = last.get("key", {}).get("fromMe", False)
        msg_type = last.get("messageType", "conversation")
        msg_obj  = last.get("message") or {}

        # Build compact content
        def _dur(secs):
            try:
                s = int(secs or 0)
            except Exception:
                return "0:00"
            h, m, sec = s // 3600, (s % 3600) // 60, s % 60
            return f"{h}:{m:02d}:{sec:02d}" if h > 0 else f"{m}:{sec:02d}"

        if msg_type == "conversation":
            content = msg_obj.get("conversation") or ""
            if looks_like_binary_blob(content):
                # Some senders — the official WhatsApp updates account
                # ("0@s.whatsapp.net") observed live — deliver a message
                # whose "conversation" text field is itself a raw base64
                # image blob rather than real text. Without this guard the
                # chat-list preview showed "+0: /9j/4AAQSkZJRg..." verbatim.
                content = i18n.t("notif_unsupported")
        elif msg_type == "extendedTextMessage":
            content = (msg_obj.get("extendedTextMessage") or {}).get("text", "") or ""
            if looks_like_binary_blob(content):
                content = i18n.t("notif_unsupported")
            ext = msg_obj.get("extendedTextMessage") or {}
            mentioned = (
                (last.get("contextInfo") or {}).get("mentionedJid")
                or (msg_obj.get("contextInfo") or {}).get("mentionedJid")
                or ext.get("contextInfo", {}).get("mentionedJid")
                or []
            )
            if isinstance(mentioned, list) and mentioned:
                for jid in mentioned:
                    if not isinstance(jid, str):
                        continue
                    if self._is_self_jid(jid):
                        name = "eu"
                    else:
                        if hasattr(self, "conversations_panel"):
                            name = self.conversations_panel._get_participant_name(jid)
                        else:
                            name = ""
                    
                    lid_local = jid.rsplit("@", 1)[0]
                    _lid_map = getattr(self, "_lid_to_phone", {})
                    phone_jid = _lid_map.get(jid, "") if jid.endswith("@lid") else ""
                    phone = phone_jid.split("@")[0] if phone_jid else jid.split("@")[0]
                    
                    placeholder = None
                    if f"@{lid_local}" in content:
                        placeholder = lid_local
                    elif phone and f"@{phone}" in content:
                        placeholder = phone
                        
                    if not placeholder:
                        continue
                        
                    if name and name != placeholder and name != jid:
                        content = content.replace(f"@{placeholder}", f"@{name}")
        elif msg_type == "audioMessage":
            dur     = _dur((msg_obj.get("audioMessage") or {}).get("seconds"))
            content = f"{i18n.t('message_type_audio')} {dur}"
        elif msg_type == "videoMessage":
            video = msg_obj.get("videoMessage") or {}
            dur   = _dur(video.get("seconds"))
            content = f"{i18n.t('video')} {dur}"
        elif msg_type == "imageMessage":
            img     = msg_obj.get("imageMessage") or {}
            caption = (img.get("caption") or "").strip()
            content = i18n.t("photo") + (f" {caption}" if caption else "")
        elif msg_type == "documentMessage":
            doc      = msg_obj.get("documentMessage") or {}
            filename = doc.get("fileName") or doc.get("title") or ""
            size_bytes = doc.get("fileLength")
            size_str = ""
            if size_bytes:
                try:
                    sz  = int(size_bytes)
                    sep = i18n.t("decimal_separator")
                    if sz < 1024:
                        size_str = f"{sz} b"
                    elif sz < 1024 ** 2:
                        size_str = f"{sz / 1024:.1f}".replace(".", sep) + " kb"
                    elif sz < 1024 ** 3:
                        size_str = f"{sz / 1024 ** 2:.1f}".replace(".", sep) + " mb"
                    else:
                        size_str = f"{sz / 1024 ** 3:.1f}".replace(".", sep) + " gb"
                except (ValueError, TypeError):
                    pass
            parts = [i18n.t("document")]
            if filename:
                parts.append(filename)
            if size_str:
                parts.append(size_str)
            content = ", ".join(parts)
        elif msg_type == "stickerMessage":
            content = i18n.t("sticker")
        elif msg_type == "contactMessage":
            contact = msg_obj.get("contactMessage") or {}
            content = i18n.t("contact_message").format(
                name=contact.get("displayName") or ""
            )
        elif msg_type == "locationMessage":
            content = i18n.t("notif_location")
        elif msg_type == "pollCreationMessage":
            poll = msg_obj.get("pollCreationMessage") or {}
            name = poll.get("name") or ""
            content = f"📊 Enquete: {name}" if name else "📊 Enquete"
        elif msg_type == "buttonsMessage":
            content = "🔘 Botão"
        elif msg_type == "listMessage":
            content = "📋 Lista"
        elif msg_type == "templateMessage":
            content = "📝 Modelo"
        elif msg_type == "protocolMessage":
            protocol = msg_obj.get("protocolMessage") or {}
            p_type = protocol.get("type")
            if p_type in (3, "REVOKE", "revoke"):
                content = "Mensagem apagada"
            else:
                content = "⚙️ Mensagem do sistema"
        else:
            # Logged so a future report of a raw, untranslated messageType
            # showing up in the chat-list preview (e.g. "audioMessage" wraps
            # such as view-once/ephemeral audio, which arrive under a
            # DIFFERENT outer messageType than "audioMessage" itself and
            # aren't unwrapped here) can be traced straight to the exact
            # type instead of only reproducing "some preview looks wrong".
            logging.info("[_last_msg_preview] unhandled messageType=%r for chat=%r",
                         msg_type, chat.get("remoteJid", ""))
            content = i18n.t("notif_unsupported")

        # Build time string
        ts = last.get("messageTimestamp")
        time_str = ""
        if ts:
            try:
                from datetime import datetime as _dt
                ts_val = int(ts)
                if ts_val > 1_000_000_000_000:
                    ts_val //= 1000
                dt    = _dt.fromtimestamp(ts_val)
                today = _dt.now().date()
                if dt.date() == today:
                    time_str = dt.strftime(get_time_format(i18n.t("time_fmt")))
                else:
                    time_str = dt.strftime(get_datetime_format(i18n.t("datetime_fmt")))
            except Exception:
                pass

        # For group chats add sender name before content (e.g. "João: vídeo 0:30")
        jid      = chat.get("remoteJid", "")
        is_group = jid.endswith("@g.us")
        if from_me:
            sender_prefix = self.self_reference_label() + ": "
        elif is_group:
            p_key      = last.get("key", {})
            sender_jid = last.get("participant") or p_key.get("participant") or p_key.get("remoteJid", "")
            push       = last.get("pushName", "")
            if sender_jid.endswith("@g.us") and push and push.isdigit():
                sender_jid = f"{push}@s.whatsapp.net"
            sender_name = (
                self._resolve_contact_name({"remoteJid": sender_jid})
                or (push if push and not is_phone_like(push) else "")
                or self._preview_sender_from_jid(sender_jid)
            )
            sender_prefix = f"{sender_name}: " if sender_name else ""
        else:
            sender_prefix = ""
        parts = [f"{sender_prefix}{content}"]
        if time_str:
            parts.append(time_str)
        return " ".join(parts)

    def _refresh_archived_chats_in_ui(self, arch_focused_jid: "str | None" = None):
        """Update the archived conversations list using SetItem when possible.

        Avoids DeleteAllItems() when JID order/count is unchanged so the
        archived panel's scroll position and focus are preserved.
        """
        if not hasattr(self, "archived_conversations_panel"):
            return
        panel = self.archived_conversations_panel
        arch_full_chats = list(getattr(panel, '_all_chats_list', panel.chats_list))
        arch_full_names = list(getattr(panel, '_all_chat_names', panel.chat_names))
        arch_lst = panel.conversations_list
        arch_filter = getattr(panel, '_conv_filter', 'all')

        new_arch_chats: list = []
        new_arch_names: list = []
        new_arch_texts: list = []
        for i, chat in enumerate(arch_full_chats):
            chat_jid = chat.get("remoteJid", "")
            unread_count = effective_unread_count(chat)
            if arch_filter == 'unread' and unread_count == 0:
                continue
            if arch_filter == 'groups' and not chat_jid.endswith("@g.us"):
                continue
            if arch_filter == 'individual' and chat_jid.endswith("@g.us"):
                continue
            name = arch_full_names[i] if i < len(arch_full_names) else ""
            unread = unread_count
            unread_str = (
                f" {unread} " + (self.i18n.t("unread_messages") if unread > 1 else self.i18n.t("unread_message"))
                if unread > 0 else ""
            )
            preview = self._last_msg_preview(chat)
            item_text = name + unread_str
            if item_text and preview:
                item_text += f" {preview}"
            new_arch_chats.append(chat)
            new_arch_names.append(name)
            new_arch_texts.append(item_text)

        new_arch_jids = [c.get("remoteJid", "") for c in new_arch_chats]
        _arch_displayed_jids = getattr(panel, '_displayed_jids', None)

        _arch_fast_path_ok = False
        if (
            _arch_displayed_jids is not None
            and _arch_displayed_jids == new_arch_jids
            and arch_lst.GetItemCount() == len(new_arch_jids)
        ):
            try:
                for idx, new_text in enumerate(new_arch_texts):
                    if arch_lst.GetItemText(idx, 0) != new_text:
                        arch_lst.SetItem(idx, 0, new_text)
                _arch_fast_path_ok = True
            except Exception:
                # See add_chats_to_ui(): don't retry SetItem on a stale index,
                # fall through to the full rebuild below instead.
                pass
        if _arch_fast_path_ok:
            panel.chats_list = new_arch_chats
            panel.chat_names = new_arch_names
            return

        arch_list_has_focus = (wx.Window.FindFocus() == arch_lst)
        arch_fi = arch_lst.GetFocusedItem()
        if arch_fi != -1:
            try:
                arch_lst.SetItemState(arch_fi, 0, wx.LIST_STATE_FOCUSED)
            except Exception:
                pass
        arch_lst.DeleteAllItems()
        for item_text in new_arch_texts:
            arch_lst.Append((item_text,))
        panel.chats_list = new_arch_chats
        panel.chat_names = new_arch_names
        panel._displayed_jids = new_arch_jids

        if new_arch_chats:
            target_idx = -1
            if arch_focused_jid:
                for i, chat in enumerate(new_arch_chats):
                    if chat.get("remoteJid") == arch_focused_jid:
                        target_idx = i
                        break
            if target_idx != -1:
                if arch_list_has_focus and arch_lst.GetFocusedItem() != target_idx:
                    arch_lst.Focus(target_idx)
                if not arch_lst.IsSelected(target_idx):
                    arch_lst.Select(target_idx)
                arch_lst.EnsureVisible(target_idx)
            elif not getattr(self, "_initial_sync_running", False):
                last_jid = getattr(panel, "_last_open_jid", "")
                target_idx = 0
                if last_jid:
                    for i, chat in enumerate(new_arch_chats):
                        if chat.get("remoteJid") == last_jid:
                            target_idx = i
                            break
                if arch_list_has_focus:
                    arch_lst.Focus(target_idx)
                arch_lst.Select(target_idx)
                arch_lst.EnsureVisible(target_idx)

    def add_chats_to_ui(self):
        """Rebuild the conversations list from the current chats data.

        Applies active search and conversation filter to both the wx.ListCtrl
        and the backing chats_list/chat_names arrays so that list indices are
        always consistent.  Without this sync the user would open the wrong
        conversation when a search was active.
        """
        search       = self.conversations_panel.search_field.GetValue().strip().lower()
        conv_filter  = getattr(self.conversations_panel, '_conv_filter', 'all')

        # Used below to tell "the same filtered view just lost an item" (where
        # reusing the old row position to keep focus nearby makes sense) apart
        # from "the active filter/search changed" (where the old row position
        # belongs to a different, unrelated list and must not be reused).
        _filter_or_search_changed = (
            getattr(self, "_last_conv_filter_key", None) != (conv_filter, search)
        )
        self._last_conv_filter_key = (conv_filter, search)

        # Always start from the full sorted lists saved by set_chats() so
        # that restoring the window or clearing a search shows all chats.
        full_chats = list(getattr(self.conversations_panel, '_all_chats_list',
                                  self.conversations_panel.chats_list))
        full_names = list(getattr(self.conversations_panel, '_all_chat_names',
                                  self.conversations_panel.chat_names))

        lst = self.conversations_panel.conversations_list

        # Save focused JID before any potential modification (used by both paths).
        focused_idx = lst.GetFocusedItem()
        focused_jid = getattr(self.conversations_panel, '_preserved_focused_jid', None)
        self.conversations_panel._preserved_focused_jid = None  # consume
        if focused_jid is None and focused_idx != -1 and 0 <= focused_idx < len(self.conversations_panel.chats_list):
            focused_jid = self.conversations_panel.chats_list[focused_idx].get("remoteJid")

        # Save currently focused archived chat JID if archived panel is present
        arch_focused_jid = None
        if hasattr(self, "archived_conversations_panel"):
            arch_lst = self.archived_conversations_panel.conversations_list
            arch_focused_idx = arch_lst.GetFocusedItem()
            arch_focused_jid = getattr(self.archived_conversations_panel, '_preserved_focused_jid', None)
            self.archived_conversations_panel._preserved_focused_jid = None  # consume
            if arch_focused_jid is None and arch_focused_idx != -1 and 0 <= arch_focused_idx < len(self.archived_conversations_panel.chats_list):
                arch_focused_jid = self.archived_conversations_panel.chats_list[arch_focused_idx].get("remoteJid")

        # Pre-compute the new display list (filtering + item text) so we can
        # choose between a lightweight SetItem path and a full rebuild.
        def _build_item_text(chat, name):
            chat_jid = chat.get("remoteJid", "")
            unread = effective_unread_count(chat)
            unread_str = (
                f" {unread} " + (self.i18n.t("unread_messages") if unread > 1 else self.i18n.t("unread_message"))
                if unread > 0 else ""
            )
            preview = self._last_msg_preview(chat)
            text = name + unread_str
            if preview:
                text += f" {preview}"
            chat_jid_norm = self._normalize_jid(chat_jid) if chat_jid else ""
            if chat_jid_norm:
                presence_label = self._presence_label_for_chat(chat_jid_norm, chat_jid_norm.endswith("@g.us"))
                if presence_label:
                    text += f" {presence_label}"
            if chat_jid_norm and self.is_chat_pinned(chat_jid_norm):
                text += f" ({self.i18n.t('pinned_suffix')})"
            if chat_jid_norm and self.is_chat_muted(chat_jid_norm):
                text += f" ({self.i18n.t('muted')})"
            if chat_jid_norm and self.is_contact_blocked(chat_jid_norm):
                text += f" ({self.i18n.t('blocked')})"
            return text

        displayed_chats: list = []
        displayed_names: list = []
        new_item_texts: list = []
        for i, chat in enumerate(full_chats):
            name     = full_names[i]
            chat_jid = chat.get("remoteJid", "")
            if conv_filter == 'unread' and effective_unread_count(chat) == 0:
                continue
            if conv_filter == 'groups' and not chat_jid.endswith("@g.us"):
                continue
            if conv_filter == 'individual' and chat_jid.endswith("@g.us"):
                continue
            if search and search not in name.lower():
                continue
            displayed_chats.append(chat)
            displayed_names.append(name)
            new_item_texts.append(_build_item_text(chat, name))

        # ── SetItem path: same JIDs in same order — only text may have changed ──
        # Avoids DeleteAllItems() entirely, keeping scroll position and focus intact.
        # NOTE: chats_list was already overwritten by _apply_chat_lists before this
        # function runs, so we track what's truly rendered via _displayed_jids.
        new_jids = [c.get("remoteJid", "") for c in displayed_chats]

        # Content fingerprint of exactly what this call would render. Used only
        # to skip the per-row GetItemText comparison loop below when nothing
        # changed — it deliberately does NOT short-circuit the whole method.
        #
        # It used to: an equal fingerprint returned from add_chats_to_ui()
        # immediately. But _apply_chat_lists() overwrites panel.chats_list with
        # the *unfiltered* sorted list right before calling this, and the
        # archived panel's rows were never part of the fingerprint at all — so
        # that early return left the backing chats_list and the rows actually
        # on screen describing two different lists, and skipped
        # _refresh_archived_chats_in_ui() entirely. Every lookup that maps a row
        # index back to a chat (activation, context menu, focus restore) then
        # read the wrong entry: reported live as archived groups showing up
        # twice and opening a different group than the one announced.
        _fp = (conv_filter, search, tuple(new_jids), tuple(new_item_texts))
        _fp_unchanged = (_fp == getattr(self, "_chats_ui_fp", None))
        self._chats_ui_fp = _fp

        _displayed_jids = getattr(self.conversations_panel, '_displayed_jids', None)
        _fast_path_ok = False
        if (
            _displayed_jids is not None
            and _displayed_jids == new_jids
            and lst.GetItemCount() == len(new_jids)
        ):
            if _fp_unchanged:
                _fast_path_ok = True  # rows already hold exactly this text
            else:
                try:
                    for idx, new_text in enumerate(new_item_texts):
                        if lst.GetItemText(idx, 0) != new_text:
                            lst.SetItem(idx, 0, new_text)
                    _fast_path_ok = True
                except Exception:
                    # The underlying Win32 list control rejected an index that
                    # GetItemCount() claimed was valid (observed as "Couldn't
                    # retrieve information about list control item N"). Don't
                    # retry SetItem blindly — fall through to the full rebuild
                    # below, which resyncs the control from scratch.
                    pass
        if _fast_path_ok:
            self.conversations_panel.chats_list = displayed_chats
            self.conversations_panel.chat_names = displayed_names
            # _displayed_jids stays the same (JIDs didn't change)
            # Refresh archived panel via the same SetItem logic
            if hasattr(self, "archived_conversations_panel"):
                self._refresh_archived_chats_in_ui(arch_focused_jid)
            return

        # ── Full rebuild path: JID order or count changed ────────────────────
        focus_allowed = self._allow_ui_focus_changes()
        _lst_had_focus = (wx.Window.FindFocus() is lst)
        if _lst_had_focus:
            # Set focus to parent panel temporarily to prevent OS from auto-focusing
            # item 0 during DeleteAllItems/Append when the control has focus.
            self.conversations_panel.SetFocus()
        if focused_idx != -1:
            try:
                # Clear focus state before DeleteAllItems to prevent NVDA COMError/freeze
                lst.SetItemState(focused_idx, 0, wx.LIST_STATE_FOCUSED)
            except Exception:
                pass
        if hasattr(self, "archived_conversations_panel"):
            if arch_focused_idx != -1:
                try:
                    self.archived_conversations_panel.conversations_list.SetItemState(
                        arch_focused_idx, 0, wx.LIST_STATE_FOCUSED
                    )
                except Exception:
                    pass
        lst.Freeze()
        try:
            lst.DeleteAllItems()
            for item_text in new_item_texts:
                lst.Append((item_text,))
        finally:
            lst.Thaw()

        # Keep backing lists in sync with exactly what is displayed.
        self.conversations_panel.chats_list = displayed_chats
        self.conversations_panel.chat_names = displayed_names
        self.conversations_panel._displayed_jids = new_jids

        # Restore selection / focus after DeleteAllItems() clears everything.
        # Prefer the previously focused item if it is still in the list to prevent jumping.
        panel = self.conversations_panel
        target_idx = -1
        if focused_jid:
            for i, chat in enumerate(displayed_chats):
                if chat.get("remoteJid") == focused_jid:
                    target_idx = i
                    break

        if target_idx != -1:
            if panel.conversations_list.GetFocusedItem() != target_idx:
                panel.conversations_list.Focus(target_idx)
            if _lst_had_focus:
                if not panel.conversations_list.IsSelected(target_idx):
                    panel.conversations_list.Select(target_idx)
                panel.conversations_list.EnsureVisible(target_idx)
                panel.conversations_list.SetFocus()
            elif panel.conversation is not None:
                if not panel.conversations_list.IsSelected(target_idx):
                    panel.conversations_list.Select(target_idx)
        elif (_lst_had_focus and focused_jid and displayed_chats
              and focus_allowed):
            # The previously focused chat is gone (e.g. it was just cleared and
            # filtered out). Keep keyboard focus in the list by landing on
            # whatever now occupies its slot instead of dropping focus entirely.
            # But only reuse that raw position when this is still the same
            # filtered/searched view — if the filter or search just changed,
            # the old index refers to an unrelated list and landing on row 0
            # is the only position that means anything in the new one.
            if _filter_or_search_changed:
                neighbor_idx = 0
            else:
                neighbor_idx = min(focused_idx, len(displayed_chats) - 1)
            if neighbor_idx < 0:
                neighbor_idx = 0
            panel.conversations_list.Focus(neighbor_idx)
            panel.conversations_list.Select(neighbor_idx)
            panel.conversations_list.EnsureVisible(neighbor_idx)
            panel.conversations_list.SetFocus()
        elif getattr(self, "_initial_sync_running", False):
            # Skip selection/focus restoration during active initial background sync to prevent screen readers loop
            pass
        elif panel.conversation is None and displayed_chats:
            last_jid    = getattr(panel, "_last_open_jid", "")
            target_idx  = 0
            if last_jid:
                for i, chat in enumerate(displayed_chats):
                    if chat.get("remoteJid") == last_jid:
                        target_idx = i
                        break
            if focus_allowed:
                if panel.conversations_list.GetFocusedItem() != target_idx:
                    panel.conversations_list.Focus(target_idx)
                if not panel.conversations_list.IsSelected(target_idx):
                    panel.conversations_list.Select(target_idx)
                panel.conversations_list.EnsureVisible(target_idx)
                # Restore keyboard focus to the list when no conversation is open.
                search = getattr(panel, "search_field", None)
                focused_now = wx.Window.FindFocus()
                if _lst_had_focus or focused_now is None or focused_now is lst:
                    if focused_now is not search:
                        wx.CallAfter(lst.SetFocus)
        elif panel.conversation is not None:
            open_jid = panel.conversation.get("remoteJid", "")
            target_idx = -1
            for i, chat in enumerate(displayed_chats):
                if chat.get("remoteJid") == open_jid:
                    target_idx = i
                    break
            if target_idx != -1:
                if _lst_had_focus:
                    if panel.conversations_list.GetFocusedItem() != target_idx:
                        panel.conversations_list.Focus(target_idx)
                if not panel.conversations_list.IsSelected(target_idx):
                    panel.conversations_list.Select(target_idx)
                panel.conversations_list.EnsureVisible(target_idx)

            if focus_allowed:
                focus_ctrl = getattr(panel, "message_field", None)
                if focus_ctrl and focus_ctrl.IsShownOnScreen():
                    if wx.Window.FindFocus() is None and self.IsActive():
                        wx.CallAfter(focus_ctrl.SetFocus)

        # Also refresh the archived panel if present
        if hasattr(self, "archived_conversations_panel"):
            self._refresh_archived_chats_in_ui(arch_focused_jid)

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

        #Play error sound
        self.error_sound.play()

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
        handler.setFormatter(logging.Formatter(
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
        from autostart import acquire_single_instance_mutex, activate_existing_window

        background = "--background" in sys.argv
        first_instance = acquire_single_instance_mutex()

        if not first_instance:
            # Deliberately BEFORE setup_logging(): the log file is truncated
            # on open so it only ever holds the current run, which means a
            # second launch must not touch it — the instance that owns it is
            # still running and writing to it.
            if not background:
                # A normal launch while WinZapp is already running in the background:
                # bring the existing window to the foreground and exit.
                activate_existing_window()
            # If --background and already running: nothing to do — exit silently.
            sys.exit(0)

        setup_logging()
        logging.info("Instance lock acquired.")
        logging.info("Creating wx.App...")
        app = wx.App()
        frame = MainWindow()
    except Exception:
        tb = format_exc()
        try:
            logging.error("Critical initialization error:\n%s", tb)
        except Exception:
            pass
        crash_path = _write_crash_log(tb)
        # Try to show a native Windows error box (works even without wx).
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                f"O WinZapp encontrou um erro crítico ao iniciar e não pôde continuar.\n\n"
                f"Detalhes foram salvos em:\n{crash_path}\n\n{tb[:800]}",
                "WinZapp — Erro de inicialização",
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass
        sys.exit(1)
