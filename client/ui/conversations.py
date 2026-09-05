import base64 as _b64
import logging
import mimetypes
import os
import re
import tempfile
import threading
import time
import uuid
import wx
import wx.adv
import pyperclip
try:
    import pyaudio
except ImportError:
    # No wheel exists for PyAudio on Python 3.14 at the time of writing —
    # see requirements.txt's version marker. Voice recording degrades to a
    # clear "not available" message (see _start_voice_recording()) instead
    # of the whole app failing to import.
    pyaudio = None
import wave
import sound_lib.stream as sl_stream
from sound_lib.effects import Tempo
from core.audio_devices import (
    find_input_device_index, fallback_input_device_indices, RECORDING_SAMPLE_CONFIGS,
)
from core.audio_transcode import transcode_audio_to_wav
from core.attachment_types import classify_attachment_media_type
from core.sound_system import load_sound
from core.link_preview import find_first_url, fetch_link_preview
from ui.accessible import (
    AccessibleSearchConversations,
    AccessibleRecordVoiceMessage,
    AccessibleAudioSlider,
    AccessibleSaveAs,
    AccessibleConversationDataButton,
    AccessibleAddAttachmentButton,
    AccessibleEmojiButton,
    AccessibleDiscardVoiceMessage,
    AccessiblePauseResumeRecording,
    AccessibleSendVoiceMessage,
    AccessiblePlayRecordedAudio,
    AccessibleSearchInConversation,
    AccessibleSearchNextResult,
    AccessibleSearchPrevResult,
    AccessibleNewConversationButton,
    AccessibleMessagesListControl,
    AccessibleReadMoreButton,
    CompatListBoxMessagesCtrl,
)
from ui.dialogs.emoji_picker import choose_and_insert_emoji
from core.save_location import resolve_save_dialog_folder
from core.utils import history_window, reaction_targets_status, format_number, decrypt_bytes, is_phone_like, encrypt, effective_unread_count, first_unread_index, db_fetch_limit, looks_like_binary_blob, normalize_for_search, normalize_line_separators, parse_bool_flag as _parse_bool_flag, append_selected_marker, is_message_forwarded, is_voice_message, video_seconds, MEASURED_SECONDS_KEY, link_preview_text
from core.locale_format import get_date_format, get_time_format, get_datetime_format
from core.message_copy_format import format_copied_message
from core.video_player import VideoPlayer
from core.focus_cloak import cloak_focus_announcement
from ui.media_viewer import MediaViewerDialog
from app_paths import data_path
from core.message_queue import PendingMessage
from datetime import datetime, timedelta

# Compiled URL regex used for link extraction from message text
_URL_RE = re.compile(r'https?://\S+|www\.\S+')

# Message types that carry a file "Save as" can actually write to disk.
# Everything else — text, stickers, locations, contacts, system events — has
# no payload to save: _resolve_media_filename() falls back to "<id>.bin" for
# those, so the save dialog used to open on a plain text message offering a
# .bin that no download could ever produce, and saving it just errored.
# The context menu was already gating on this set; the Ctrl+Shift+S
# accelerator and the toolbar button reached _on_action_save_as() without
# passing anywhere near it, which is how the two disagreed.
_SAVEABLE_MESSAGE_TYPES = frozenset({
    "documentMessage", "imageMessage", "videoMessage", "audioMessage",
})


class _FocusedTransferGaugeAccessible(wx.Accessible):
    """Expose value changes to screen readers only while the gauge has focus."""

    def __init__(self, gauge):
        super().__init__()
        self._gauge = gauge

    def GetState(self, childId):
        state = wx.ACC_STATE_SYSTEM_FOCUSABLE
        if self._gauge.HasFocus():
            state |= wx.ACC_STATE_SYSTEM_FOCUSED
        else:
            # NVDA's native ProgressBar handler deliberately ignores value
            # changes carrying INVISIBLE/OFFSCREEN. The gauge remains visible
            # on screen; only unsolicited accessibility updates are suppressed.
            state |= wx.ACC_STATE_SYSTEM_INVISIBLE
        return (wx.ACC_OK, state)


class _FocusedTransferGauge(wx.Gauge):
    """Native gauge reachable by Tab, with focus-scoped NVDA progress output."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.SetAccessible(_FocusedTransferGaugeAccessible(self))
        self.Bind(wx.EVT_LEFT_DOWN, self._focus_from_mouse)

    def AcceptsFocus(self):
        return True

    def AcceptsFocusFromKeyboard(self):
        return True

    def _focus_from_mouse(self, event):
        self.SetFocus()
        event.Skip()


def message_caption(msg) -> str:
    """The caption carried by an image/video/document message, '' otherwise.

    Forwarding preserves captions through a different server call than a
    plain forward (resend_media_message_with_caption), so "does this message
    have a caption?" has to be answered per message — a mass forward mixes
    captioned media with plain text, and sending a text message down the
    media path is not a no-op.
    """
    if not isinstance(msg, dict):
        return ""
    inner = msg.get("message", {})
    if isinstance(inner, str):
        import json
        try:
            inner = json.loads(inner)
        except Exception:
            return ""
    if not isinstance(inner, dict):
        return ""
    for key in ("imageMessage", "videoMessage", "documentMessage"):
        media = inner.get(key)
        if isinstance(media, dict):
            return (media.get("caption") or "").strip()
    return ""


def local_media_cache_paths(voice_dir: str, media_dir: str, msg_id: str) -> list:
    """The locally pre-cached copies a sent message can own, by message id.

    voice_messages/<id>.msv is written by the voice recorder before the send is
    even enqueued, media/<id>.wzmedia by _pre_cache_sent_media() — both under
    the local UUID first, then renamed to the real WhatsApp id so Open/Save As
    and voice playback find them instead of re-downloading a file already on
    disk (see _mark_message_sent).  Module-level so the cancelled-but-delivered
    path can reach the same two names without a panel instance.

    They are also the only two names a *received* message's media can be cached
    under (handle_audio_message writes the first, handle_media_message the
    second), so the Media tab's "baixada / nao baixada" scan asks here rather
    than keeping its own idea of where a file might be.
    """
    return [
        os.path.join(voice_dir, f"{msg_id}.msv"),
        os.path.join(media_dir, f"{msg_id}.wzmedia"),
    ]


def promote_local_media_cache(voice_dir: str, media_dir: str,
                              local_id: str, real_id: str) -> None:
    """Rename a message's cached copies from its local UUID to its real id."""
    if not local_id or not real_id or local_id == real_id:
        return
    old_paths = local_media_cache_paths(voice_dir, media_dir, local_id)
    new_paths = local_media_cache_paths(voice_dir, media_dir, real_id)
    for old, new in zip(old_paths, new_paths):
        try:
            if os.path.isfile(old) and not os.path.isfile(new):
                os.rename(old, new)
        except Exception as exc:
            logging.warning("[conversations] could not promote %s: %s", old, exc)


def discard_local_media_cache(voice_dir: str, media_dir: str, local_id: str) -> None:
    """Delete a message's cached copies — the message itself is gone for good.

    Without this a cancelled-then-revoked voice message leaves its .msv behind
    under a local UUID nothing will ever look up again.
    """
    if not local_id:
        return
    for path in local_media_cache_paths(voice_dir, media_dir, local_id):
        try:
            if os.path.isfile(path):
                os.unlink(path)
        except Exception as exc:
            logging.warning("[conversations] could not discard %s: %s", path, exc)


def probe_media_duration(path: str):
    """Best-effort length in whole seconds of a media file, or None if unknown.

    Supports .mp3, .ogg, .wav, .m4a, .flac, .opus, .aac etc. — and .mp4, since
    BASS opens the container directly through the bass_aac plugin the app
    already loads at startup (that is how core/video_player.py plays a video's
    audio track), which is what lets a video with no stated duration be
    measured from the file. Uses sound_lib / BASS when available, or stdlib
    wave and header fallback parsers.

    A module-level function rather than only a ConversationsPanel method
    because MainWindow probes downloaded video from the media-download path,
    where there is no panel in reach.
    """
    if not path or not os.path.isfile(path):
        return None

    # 1. Try BASS / sound_lib stream length (supports all audio formats: mp3, ogg, wav, m4a, flac, opus, aac)
    #    A file that reads as shorter than a second returns 0, not None: the
    #    caller has to tell "measured, and it really is that short" apart from
    #    "could not measure" — see core.utils.video_seconds(). A length of
    #    exactly 0.0 is the second case (nothing decodable), so it falls
    #    through to the parsers below.
    #    decode=True matters, not just cosmetics: it's the exact stream mode
    #    every actual playback path in the app already opens the file with
    #    (VideoPlayer._start_audio, ConversationsPanel._play_audio's
    #    _open_stream) — BASS can report a slightly different get_length()
    #    for a plain (decode=False) stream vs. a decoded one on the same AAC/
    #    MP4 file, which is why a probed duration used to drift a second or
    #    two from what the player itself later showed for the same file.
    try:
        from sound_lib import stream
        s = stream.FileStream(file=path, decode=True)
        length_bytes = s.get_length()
        length_secs = s.bytes_to_seconds(length_bytes)
        s.free()
        if length_secs and 0 < length_secs < 86400:
            return int(length_secs)
    except Exception:
        pass

    # 2. Try stdlib wave module for .wav files
    if path.lower().endswith(".wav"):
        try:
            import wave
            with wave.open(path, "rb") as wf:
                frames = wf.getnframes()
                rate   = wf.getframerate()
                if frames > 0 and rate > 0:
                    sec = int(frames / rate)
                    if sec < 86400:
                        return sec
        except Exception:
            pass

    # 3. Fallback lightweight header parser for MP3 / OGG
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext == ".mp3":
            size = os.path.getsize(path)
            if size > 0:
                # Estimate based on standard 128kbps (16000 bytes/sec)
                sec = max(1, int(size / 16000))
                if 0 < sec < 86400:
                    return sec
        elif ext in (".ogg", ".opus"):
            size = os.path.getsize(path)
            if size > 0:
                sec = max(1, int(size / 6000))
                if 0 < sec < 86400:
                    return sec
    except Exception:
        pass

    return None


def _fmt_last_seen(ts, i18n) -> str:
    """Format a Unix timestamp as a localized last-seen string."""
    if not ts:
        return ""
    try:
        from datetime import datetime as _dt, timedelta as _td
        ts_val = int(ts)
        if ts_val > 1_000_000_000_000:
            ts_val //= 1000
        dt       = _dt.fromtimestamp(ts_val)
        now      = _dt.now()
        time_str = dt.strftime(get_time_format(i18n.t("time_fmt")))
        if dt.date() == now.date():
            return i18n.t("last_seen_today").format(time=time_str)
        if dt.date() == (now - _td(days=1)).date():
            return i18n.t("last_seen_yesterday").format(time=time_str)
        date_str = dt.strftime(get_date_format(i18n.t("date_fmt")))
        return i18n.t("last_seen_date").format(date=date_str, time=time_str)
    except Exception:
        return ""


class ConversationsPanel(wx.Panel):
    # Windows' native SysListView32 (the classic wx.ListCtrl) reads each item's
    # text through a 512-character buffer whose last slot holds the terminating
    # NUL — so exactly 511 characters survive.  Slicing the remainder at 512
    # skipped the 512th character, which is why "Ler mais" used to resume in the
    # middle of a word with one letter missing.
    _LIST_CTRL_TEXT_LIMIT = 511

    # Box _media_bitmap is given while an in-app video plays (released again
    # once playback stops — see _start_video_playback). Matches the fixed
    # (320, 240) StatusPanel creates its own video bitmap at, so a video
    # renders the same size whether it came from a conversation or a status.
    _VIDEO_BITMAP_SIZE = (320, 240)

    def __init__(self, main_window, parent):
        super().__init__(parent)
        self.main_window = main_window
        self.parent = parent
        self.chats_list = []
        self.chat_names = []
        self.selected_chats = set()
        self.selected_messages = set()

        # Feedback tone for the Ctrl+Space-toggled selection in both lists —
        # the dedicated "selected" cue, not a generic alert tone. load_sound()
        # returns a NullSound if the file can't be opened, so the callers
        # below never have to care whether it loaded.
        self.selection_sound = load_sound(
            self.main_window.sound_system,
            os.path.join("default", "selected.ogg"),
        )

        self.conversation = None
        self.conversation_name = ""
        self._last_open_jid = ""
        # Ultima linha da lista de conversas em que o foco pousou, aberta ou
        # nao — ver _on_conversation_focused() e
        # _restore_conversation_selection().
        self._last_list_focus_jid = ""

        # ── Audio / video player state ──────────────────────────────────────
        self._sorted_messages = []
        self._current_audio_id = None
        self._audio_stream = None
        self._audio_tempo_ctrl = None
        self._is_audio_playing = False
        self._is_in_audio_chain = False
        # Handles to the wx.CallLater timers scheduled by the auto-chain
        # (see _auto_chain_next_audio). They MUST be cancelled whenever
        # playback stops, a new audio starts manually, or the user leaves the
        # conversation — otherwise a stale timer fires up to ~1 s later and
        # starts an audio the user didn't ask for (reported live as the
        # sequence "keeping playing audios from above" after the last audio
        # finished / after jumping to a different message).
        self._chain_play_timer = None
        self._chain_start_timer = None
        self._chain_end_timer = None
        self._pending_played_refresh_id = None
        # Row repaints deferred while the audio chain is moving list focus —
        # see _release_chain_held_repaints() for why they must not be written
        # during the sequence at all.
        self._hold_status_repaints_for_chain = False
        self._chain_held_status_repaints = set()
        self._audio_stream_duration = 0
        self._audio_temp_file = None
        self._audio_speed_steps = [1.0, 1.5, 2.0]
        self._audio_tempo_map = {1.0: 0, 1.5: 50, 2.0: 100}
        # Restore the last-used speed from settings (persists across conversations/sessions)
        _saved_speed = self.main_window.settings.get("audio_playback", {}).get("audio_default_speed", 1.0)
        try:
            self._audio_speed_index = self._audio_speed_steps.index(float(_saved_speed))
        except (ValueError, TypeError):
            self._audio_speed_index = 0
        self._audio_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_audio_timer, self._audio_timer)
        # msg_id → position (samples) saved when a *different* audio starts while
        # this one is still mid-play.  Restored the next time _play_audio() is
        # called for the same message so playback resumes where it was left off.
        self._audio_positions: dict = {}
        # JID of the conversation where the current audio was started; used by
        # _auto_chain_next_audio and navigate_to_conversation to avoid operating
        # on the wrong conversation's message list.
        self._audio_conv_jid: str = ""

        # ── Typing status state ─────────────────────────────────────────────
        self._is_typing = False

        # ── Voice recording state ───────────────────────────────────────────
        self._is_recording         = False
        self._recording_paused     = False
        self._recording_frames: list = []   # list of bytes chunks from callback
        self._recording_stream     = None   # pyaudio.Stream
        self._recording_pa         = None   # pyaudio.PyAudio instance
        # Actual rate/channels are resolved at open time (stereo → mono fallback).
        self._recording_actual_rate: int = 48000
        self._recording_actual_ch:   int = 1
        # Playback of what's been recorded so far, offered only while paused
        # (see _toggle_play_recorded_audio / _stop_recorded_audio_preview).
        self._recorded_audio_sound      = None
        self._recorded_audio_temp_path  = None
        # True while a background thread is opening the PyAudio input stream
        # (pa.open() can block for seconds negotiating with the driver — it
        # must never run on the UI thread). Guards on_record_voice_message
        # against re-entry, and _recording_open_token lets a conversation
        # switch/close that happens mid-open discard the stream once it opens.
        self._recording_starting    = False
        self._recording_open_token  = 0

        # ── Attachment staging ──────────────────────────────────────────────
        # list of {"path": str, "media_type": str}
        self._staged_attachments: list = []

        # ── Contact message state ───────────────────────────────────────────
        self._contact_msg_jid: str | None = None  # JID in currently-selected contactMessage

        # ── Edit message state ──────────────────────────────────────────────
        self._editing_message_id: str | None = None    # key.id of msg being edited
        self._editing_message_index: int = -1          # list row index

        # ── Message bookmarks (Ctrl+0..9 / Ctrl+Shift+0..9) ──────────────────
        # digit (0-9) -> (conversation JID, stable message identifier). The
        # message id, not a raw list index, so a bookmark keeps pointing at
        # the same message even if the list is rebuilt/reordered (new
        # message arriving, pagination, etc.) between setting it and jumping
        # to it. Bookmarks now span conversations rather than being scoped to
        # the one they were set in: jumping to one set in a different
        # conversation than the one currently open navigates there first.
        # Not persisted across restarts.
        self._msg_bookmarks: dict = {}

        # ── Temporary bookmarks (Alt+Shift+0..9 / Ctrl+Alt+Shift+0..9) ───────
        # digit (0-9) -> stable message identifier, scoped to the conversation
        # currently open and dropped the moment it is left — which is exactly
        # how _msg_bookmarks behaved before it was widened to span
        # conversations. Both kinds are needed: the ten cross-conversation
        # bookmarks are a scarce resource a user assigns to messages that
        # matter for a long time, so spending one on "hold my place while I
        # scroll up to check something" would cost a slot they were keeping.
        # These are the scratch set for that, and being cleared on leaving the
        # conversation is the point, not a limitation — nothing accumulates.
        # Only a message id is stored (no JID): the conversation a temporary
        # bookmark belongs to is always the open one, by construction.
        self._msg_temp_bookmarks: dict = {}

        # ── Media download progress ─────────────────────────────────────────
        # msg_id -> float 0.0-1.0  (absent = not tracked / already complete)
        self._download_progress: dict = {}

        # ── Unread separator ────────────────────────────────────────────────
        # Index in _sorted_messages of the unread-separator sentinel, or -1
        self._unread_sep_idx: int = -1
        # Unread count captured before mark-as-read thread starts (avoids race)
        self._pending_open_unread: int = 0
        # True while the current separator anchors an already-read position:
        # o foco do usuário já passou por ele (ver _on_message_focused()), a
        # conversa já foi marcada como lida, e o separador continua visível só
        # para a pessoa não se perder, como no WhatsApp oficial. Só nesse caso
        # a próxima mensagem ao vivo o substitui por um separador novo
        # (contagem de volta a 1). Um separador colocado na ABERTURA da
        # conversa não entra aqui: as mensagens abaixo dele ainda são
        # genuinamente não lidas, então a mensagem nova soma nele — tratar os
        # dois casos igual é o que produzia "separador diz 1, duas mensagens
        # abaixo dele".
        self._sep_anchors_read_position: bool = False
        # Par que populate_messages() lê de volta para recriar o separador
        # depois de cada DeleteAllItems(): o id da mensagem que ele ancora (a
        # primeira abaixo dele) e a contagem exibida. Todo caminho que mexe no
        # separador tem de escrever este par, ou o rebuild seguinte desfaz o
        # trabalho — ver _update_unread_separator_for_incoming().
        self._first_unread_msg_id = None
        self._first_unread_count: int = 0
        # Latch so the mark-as-read request fires once per separator, not on
        # every focus event at or below it. This used to be inferred from the
        # dismiss timer still running, which no longer exists — see
        # _should_dismiss_unread_separator().
        self._unread_sep_marked_read: bool = False


        # ── Reaction tracking ───────────────────────────────────────────────
        # Maps original_msg_id → {emoji: count}
        self._reaction_map: dict = {}
        # Keep track of chats where we reached the start of history on the server
        self._reached_server_start: dict = {}
        # When a server page overlaps local history completely, keep walking
        # from that page's oldest message instead of repeating the same anchor.
        self._server_history_anchor: dict = {}

        # ── Reply / quoted message state ────────────────────────────────────
        # When not None, the next sent message will be a quoted reply
        self._quoted_message: dict | None = None
        self._outgoing_virtual_messages: dict = {}
        self._media_upload_progress: dict = {}
        self._media_transfer_started: set = set()
        # local_id → the virtual message dict of a row the user deleted while it
        # was still pending. Kept because cancelling an in-flight send is only
        # best effort: if it reached WhatsApp anyway the message has to be
        # revoked, and if that revoke fails this dict is what puts the row back
        # (see complete_cancelled_message_delivery()).
        self._cancelled_pending_messages: dict = {}

        # ── Outgoing link preview state ──────────────────────────────────────
        # {"title", "description", "canonicalUrl"} once a preview was
        # resolved for the URL currently in the message field, else None —
        # see core/link_preview.py and _check_link_preview_for_current_text().
        self._pending_link_preview: dict | None = None
        # The exact URL the resolved preview above was fetched for, so a
        # further edit that changes/removes that URL invalidates it.
        self._link_preview_source_url: str = ""
        # Set when the user explicitly clicks "remove preview" — that exact
        # URL is not re-fetched again until the field's URL changes away
        # from it (see _on_remove_link_preview()).
        self._link_preview_dismissed_url: str = ""
        # Bumped on every debounce tick; a fetch result is applied only if
        # this still matches the token captured when that fetch started —
        # guards against a stale, slow fetch overwriting what a later one
        # (or the user clearing the field) already resolved.
        self._link_preview_fetch_token: int = 0
        self._link_preview_debounce_timer: wx.CallLater | None = None

        # ── Search in conversation state ─────────────────────────────────────
        # Indices in _sorted_messages that match the current search query
        self._search_results: list = []
        # Current position in _search_results (-1 = no active navigation)
        self._search_result_idx: int = -1

        # ── Link extraction state ────────────────────────────────────────────
        # URLs found in the currently focused message
        self._current_links: list = []
        # @mention (display_name, jid) pairs for the currently focused message
        self._current_mentions: list = []

        # ── @mention input state ─────────────────────────────────────────────
        # Whether a mention suggestion dropdown is currently active
        self._mention_active: bool = False
        # Character position in the message field where the @ was typed
        self._mention_start_pos: int = -1
        # Text typed after the @ (the current filter query)
        self._mention_query: str = ""
        # Filtered suggestion pairs [(display_name, jid), ...]
        self._mention_suggestions: list = []
        # Participants of the current group, cached on conversation open
        self._group_participants_cache: list = []
        # JIDs confirmed for @mention to be sent with the next message
        self._pending_mentions: list = []
        # Maps JID → display_name for each pending mention (used to replace
        # @DisplayName with @phonenumber in the API payload — WhatsApp only
        # renders a mention when the text contains the bare phone number after @).
        self._pending_mention_display_names: dict = {}

        # ── Lazy-loading / pagination state ─────────────────────────────────
        # Full sorted+displayable list (never paginated)
        self._all_sorted_messages: list = []
        # How many messages from _all_sorted_messages are before _sorted_messages[0]
        self._messages_offset: int = 0
        # Guard to prevent recursive load-more triggers during list rebuild
        self._is_loading_more: bool = False
        # Quantas mensagens exibíveis a lista passou a mostrar depois que o
        # usuário puxou histórico (Home/scroll ao topo). populate_messages()
        # reconstrói a janela sempre a partir do fim, então sem isso um
        # rebuild de fundo — e há um a cada mensagem nova — descartava tudo
        # que o usuário tinha carregado e voltava ao messages_page_size.
        self._expanded_visible_count: int = 0
        # Id da mensagem mais antiga exibida naquele momento: é a âncora real
        # da janela, já que a contagem sozinha escorrega uma linha para frente
        # a cada mensagem nova. Vazio quando ela não existe mais.
        self._expanded_oldest_msg_id: str = ""

        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self.init_UI()
        self.create_accelerator_table()
        self.create_accel_conversation()

        # In-app video-message playback (audio via BASS, frames via ffmpeg —
        # see core/video_player.py). Created after init_UI() since it needs
        # _media_bitmap to already exist.
        self._video_player = VideoPlayer(
            self.main_window, self._media_bitmap, on_frame_size=self._on_video_frame_size_known
        )
        # id of the video message currently loaded in _video_player, or None
        # — lets a second Enter on the SAME video toggle pause instead of
        # restarting it, while Enter on a DIFFERENT video switches to it.
        self._current_video_msg_id = None

    # ── UI ──────────────────────────────────────────────────────────────────

    def init_UI(self):
        i18n = self.main_window.i18n
        outer_sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Search ──────────────────────────────────────────────────────────
        self.search_label = wx.StaticText(self, label=i18n.t("search_conversations"))
        outer_sizer.Add(self.search_label, 0, wx.LEFT | wx.TOP, 5)

        self.search_field = wx.TextCtrl(self, style=wx.TE_DONTWRAP)
        self.search_field.Bind(wx.EVT_TEXT, self.on_search_query_changed)
        self.search_field.Bind(wx.EVT_KEY_DOWN, self._on_search_field_key_down)
        self.search_field.SetAccessible(AccessibleSearchConversations("Ctrl+F"))
        outer_sizer.Add(self.search_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        # ── Nova conversa button ────────────────────────────────────────────
        self._new_conv_btn = wx.Button(self, label=i18n.t("new_conversation"))
        self._new_conv_btn.SetAccessible(AccessibleNewConversationButton())
        self._new_conv_btn.Bind(wx.EVT_BUTTON, self._on_new_conversation)
        outer_sizer.Add(self._new_conv_btn, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 5)

        # ── Conversation filter tabs ─────────────────────────────────────────
        # Tracks the active filter key: 'all' | 'unread' | 'groups' | 'individual'
        self._conv_filter = 'all'
        self._filter_radio = wx.RadioBox(
            self,
            label=i18n.t("conv_filter_label"),
            choices=[
                i18n.t("conv_filter_all"),
                i18n.t("conv_filter_unread"),
                i18n.t("conv_filter_groups"),
                i18n.t("conv_filter_individual"),
            ],
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
        )
        self._filter_radio.Bind(wx.EVT_RADIOBOX, self._on_filter_changed)
        outer_sizer.Add(self._filter_radio, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        # ── Conversations list ──────────────────────────────────────────────
        self.conversations_label = wx.StaticText(self, label=i18n.t("conversations"))
        outer_sizer.Add(self.conversations_label, 0, wx.LEFT, 5)

        self.conversations_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.conversations_list.InsertColumn(0, i18n.t("conversations"), width=200)
        self.conversations_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_conversation_selected)
        self.conversations_list.Bind(wx.EVT_LIST_ITEM_FOCUSED, self._on_conversation_focused)
        self.conversations_list.Bind(wx.EVT_CONTEXT_MENU, self.on_conversations_context_menu)
        self.conversations_list.Bind(wx.EVT_KEY_DOWN, self._on_conv_list_key_down)
        outer_sizer.Add(self.conversations_list, 1, wx.EXPAND | wx.ALL, 5)

        # ── Conversation panel ──────────────────────────────────────────────
        self.conversation_panel = wx.Panel(self)
        conv_sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Conversation / group data button ───────────────────────────────
        self._conv_data_btn = wx.adv.CommandLinkButton(
            self.conversation_panel,
            mainLabel=i18n.t("conversation_data"),
            note="",
        )
        self._conv_data_btn.SetAccessible(AccessibleConversationDataButton())
        self._conv_data_btn.Bind(wx.EVT_BUTTON, self._show_conversation_data)
        conv_sizer.Add(self._conv_data_btn, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        # ── Search in conversation button ───────────────────────────────────
        self._search_open_btn = wx.Button(
            self.conversation_panel, label=i18n.t("search_in_conv")
        )
        self._search_open_btn.SetAccessible(AccessibleSearchInConversation())
        self._search_open_btn.Bind(wx.EVT_BUTTON, self._on_open_search)
        conv_sizer.Add(self._search_open_btn, 0, wx.LEFT | wx.TOP | wx.BOTTOM, 5)

        # ── Search panel (hidden by default) ───────────────────────────────
        self._search_panel = wx.Panel(self.conversation_panel)
        search_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self._search_close_btn = wx.Button(self._search_panel, label=i18n.t("search_close"))
        self._search_close_btn.Bind(wx.EVT_BUTTON, self._on_close_search)
        search_sizer.Add(self._search_close_btn, 0, wx.RIGHT, 5)

        self._search_field_label = wx.StaticText(self._search_panel, label=i18n.t("search_in_conv"))
        search_sizer.Add(self._search_field_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)

        self._search_field = wx.TextCtrl(self._search_panel, style=wx.TE_DONTWRAP | wx.TE_PROCESS_ENTER)
        self._search_field.Bind(wx.EVT_TEXT, self._on_search_text_changed)
        self._search_field.Bind(wx.EVT_KEY_DOWN, self._on_search_key_down)
        search_sizer.Add(self._search_field, 1, wx.EXPAND | wx.RIGHT, 5)

        self._search_prev_btn = wx.Button(self._search_panel, label=i18n.t("search_prev_result"))
        self._search_prev_btn.SetAccessible(AccessibleSearchPrevResult())
        self._search_prev_btn.Bind(wx.EVT_BUTTON, self._on_search_prev)
        search_sizer.Add(self._search_prev_btn, 0, wx.RIGHT, 5)

        self._search_next_btn = wx.Button(self._search_panel, label=i18n.t("search_next_result"))
        self._search_next_btn.SetAccessible(AccessibleSearchNextResult())
        self._search_next_btn.Bind(wx.EVT_BUTTON, self._on_search_next)
        search_sizer.Add(self._search_next_btn, 0)

        self._search_panel.SetSizer(search_sizer)
        self._search_panel.Hide()
        conv_sizer.Add(self._search_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)

        self.messages_label = wx.StaticText(
            self.conversation_panel, label=i18n.t("messages")
        )
        conv_sizer.Add(self.messages_label, 0, wx.LEFT | wx.TOP, 5)

        # The messages list control type is configurable and can also be
        # switched live from Settings. Keep creation/bindings in one helper so
        # startup and runtime replacement always expose the same behaviour.
        message_list_mode = self.main_window.settings.get("user_interface", {}).get(
            "message_list_mode", "classic"
        )
        if message_list_mode == "dataview":
            message_list_mode = "listbox"
        self._message_list_mode = message_list_mode
        self._messages_list_accessibles = {}
        self._message_list_controls = {
            "classic": self._create_messages_list_control("classic"),
            "listbox": self._create_messages_list_control("listbox"),
        }
        self.messages_list = self._message_list_controls[message_list_mode]
        for mode, control in self._message_list_controls.items():
            conv_sizer.Add(control, 1, wx.EXPAND | wx.ALL, 5)
            control.Show(mode == message_list_mode)

        # ── "Ler mais" button (classic ListCtrl only) ─────────────────────────
        # SysListView32 truncates each row's accessible text to ~512 characters,
        # so a screen reader can't read the tail of a long text message just by
        # focusing it. This button is the first focusable control after the
        # list (created here, before any other conversation_panel child) and is
        # only shown when the focused row is a truncated text message.
        self._read_more_btn = wx.Button(
            self.conversation_panel, label=i18n.t("read_more_button")
        )
        self._read_more_btn.SetAccessible(AccessibleReadMoreButton())
        self._read_more_btn.Bind(wx.EVT_BUTTON, self._on_read_more)
        conv_sizer.Add(self._read_more_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._read_more_btn.Hide()

        # ── Link controls (shown when focused message contains URLs) ─────────
        self._links_panel = wx.Panel(self.conversation_panel)
        self._links_label = wx.StaticText(
            self._links_panel, label=i18n.t("links_section_label")
        )
        self._links_sizer = wx.BoxSizer(wx.VERTICAL)
        self._links_sizer.Add(self._links_label, 0, wx.LEFT | wx.TOP, 3)
        self._links_panel.SetSizer(self._links_sizer)
        self._links_panel.Hide()
        # The list control _update_links_panel() builds when a message has 2+
        # links (None otherwise, or before the first message with links is
        # focused) — see that method.
        self._links_list = None
        conv_sizer.Add(self._links_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)

        # ── Mention controls (shown when focused message contains @mentions) ──
        self._mentions_panel = wx.Panel(self.conversation_panel)
        self._mentions_label = wx.StaticText(
            self._mentions_panel, label=i18n.t("mentions_section_label")
        )
        self._mentions_sizer = wx.BoxSizer(wx.VERTICAL)
        self._mentions_sizer.Add(self._mentions_label, 0, wx.LEFT | wx.TOP, 3)
        self._mentions_panel.SetSizer(self._mentions_sizer)
        self._mentions_panel.Hide()
        conv_sizer.Add(self._mentions_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)

        # ── Thumbnail (image / sticker / video) ─────────────────────────────
        # Doubles as the in-app video surface (see _start_video_playback,
        # which installs _VIDEO_BITMAP_SIZE on it for the duration of a
        # playback and releases it afterwards). Same box StatusPanel's own
        # video viewer uses, so both places render video at one size.
        self._media_bitmap = wx.StaticBitmap(
            self.conversation_panel, bitmap=wx.NullBitmap
        )
        conv_sizer.Add(self._media_bitmap, 0, wx.ALIGN_LEFT | wx.LEFT | wx.BOTTOM, 5)
        self._media_bitmap.Hide()

        # Stable row shared by transfer progress and the selected media's
        # actions. This gives the native Windows gauge an already-laid-out
        # parent and puts it exactly where Open / Save As normally appear.
        self._media_action_slot = wx.Panel(self.conversation_panel)
        self._media_action_sizer = wx.BoxSizer(wx.VERTICAL)
        self._media_action_slot.SetSizer(self._media_action_sizer)
        conv_sizer.Add(
            self._media_action_slot, 0,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5,
        )

        self._media_transfer_gauge = _FocusedTransferGauge(
            self._media_action_slot,
            range=100,
            style=wx.GA_HORIZONTAL | wx.GA_SMOOTH,
        )
        self._media_transfer_gauge.SetMinSize((-1, 24))
        self._media_action_sizer.Add(self._media_transfer_gauge, 0, wx.EXPAND)
        gauge = getattr(self, "_media_transfer_gauge", None)
        if gauge:
            gauge.Hide()

        # ── Action buttons (document / image / video) ───────────────────────
        self._action_open_btn = wx.Button(
            self._media_action_slot, label=i18n.t("open")
        )
        self._action_open_btn.Bind(wx.EVT_BUTTON, self._on_action_open)
        self._media_action_sizer.Add(self._action_open_btn, 0, wx.TOP, 2)
        self._action_open_btn.Hide()

        self._action_save_as_btn = wx.Button(
            self._media_action_slot, label=i18n.t("save_as")
        )
        self._action_save_as_btn.SetAccessible(AccessibleSaveAs())
        self._action_save_as_btn.Bind(wx.EVT_BUTTON, self._on_action_save_as)
        self._media_action_sizer.Add(self._action_save_as_btn, 0, wx.TOP, 2)
        self._action_save_as_btn.Hide()

        # ── Download button (shown when media is not yet cached locally) ───
        self._action_download_btn = wx.Button(
            self._media_action_slot, label=i18n.t("download")
        )
        self._action_download_btn.Bind(wx.EVT_BUTTON, self._on_action_download)
        self._media_action_sizer.Add(self._action_download_btn, 0, wx.TOP, 2)
        self._action_download_btn.Hide()
        self._hide_media_transfer_gauge()
        self._media_action_slot.Hide()

        # ── Business reply buttons container ───────────────────────────────
        self._buttons_container = wx.Panel(self.conversation_panel)
        self._buttons_container.SetSizer(wx.WrapSizer(wx.HORIZONTAL))
        conv_sizer.Add(
            self._buttons_container, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5
        )
        self._buttons_container.Hide()

        # ── Contact message — Converse / Save contact buttons ──────────────
        self._contact_converse_btn = wx.Button(
            self.conversation_panel, label=i18n.t("converse")
        )
        self._contact_converse_btn.Bind(wx.EVT_BUTTON, self._on_contact_converse)
        conv_sizer.Add(self._contact_converse_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._contact_converse_btn.Hide()

        # Same Ctrl+Shift+S accelerator/accessible reporting as the media
        # "Save as" button (_action_save_as_btn) — _on_action_save_as()
        # dispatches to _on_save_contact_message() for a contactMessage
        # instead of the file-save dialog.
        self._contact_save_btn = wx.Button(
            self.conversation_panel, label=i18n.t("save_contact")
        )
        self._contact_save_btn.SetAccessible(AccessibleSaveAs())
        self._contact_save_btn.Bind(wx.EVT_BUTTON, self._on_save_contact_message)
        conv_sizer.Add(self._contact_save_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._contact_save_btn.Hide()

        # ── Audio / video playback controls ────────────────────────────────
        self.audio_speed_btn = wx.Button(
            self.conversation_panel,
            label=self._format_speed(self._audio_speed_steps[self._audio_speed_index]),
        )
        self.audio_speed_btn.Bind(wx.EVT_BUTTON, self.on_audio_speed_btn)
        conv_sizer.Add(self.audio_speed_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self.audio_speed_btn.Hide()

        self.audio_progress_label = wx.StaticText(
            self.conversation_panel, label=i18n.t("audio_progress_label")
        )
        conv_sizer.Add(self.audio_progress_label, 0, wx.LEFT, 5)
        self.audio_progress_label.Hide()

        self.audio_slider = wx.Slider(
            self.conversation_panel, value=0, minValue=0, maxValue=1000
        )
        self.audio_slider.SetAccessible(AccessibleAudioSlider(self))
        self.audio_slider.Bind(wx.EVT_SLIDER, self.on_audio_slider)
        conv_sizer.Add(self.audio_slider, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        self.audio_slider.Hide()

        # ── Mention suggestion list (hidden; shown when user types @ in group) ─
        self._mention_panel = wx.Panel(self.conversation_panel)
        _mention_sizer = wx.BoxSizer(wx.VERTICAL)
        self._mention_list_label = wx.StaticText(
            self._mention_panel, label=i18n.t("mention_suggestions_label")
        )
        _mention_sizer.Add(self._mention_list_label, 0, wx.LEFT | wx.TOP, 3)
        self._mention_list = wx.ListBox(self._mention_panel, style=wx.LB_SINGLE, size=(-1, 120))
        self._mention_list.Bind(wx.EVT_KEY_DOWN, self._on_mention_list_key_down)
        self._mention_list.Bind(wx.EVT_CHAR,     self._on_mention_list_char)
        self._mention_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_mention_list_selected_mouse)
        _mention_sizer.Add(self._mention_list, 0, wx.EXPAND | wx.ALL, 3)
        self._mention_panel.SetSizer(_mention_sizer)
        self._mention_panel.Hide()
        conv_sizer.Add(self._mention_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)

        # ── Reactions list button (focused message only, when it has any) ───
        self._reactions_btn = wx.Button(self.conversation_panel, label=i18n.t("reactions_label"))
        self._reactions_btn.Bind(wx.EVT_BUTTON, self._on_show_reactions)
        conv_sizer.Add(self._reactions_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._reactions_btn.Hide()

        # ── Message input ───────────────────────────────────────────────────
        self.message_label = wx.StaticText(
            self.conversation_panel, label=i18n.t("type_message")
        )
        conv_sizer.Add(self.message_label, 0, wx.LEFT | wx.TOP, 5)

        self.message_field = wx.TextCtrl(
            self.conversation_panel,
            style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER | wx.TE_DONTWRAP,
        )
        self.message_field.Bind(wx.EVT_TEXT,       self.on_change_message_field)
        self.message_field.Bind(wx.EVT_TEXT_ENTER, self.on_send_message)
        self.message_field.Bind(wx.EVT_KEY_DOWN,   self._on_message_field_key_down)
        self.message_field.Bind(wx.EVT_CHAR,       self._on_message_field_char)
        self.message_field.Bind(wx.EVT_TEXT_PASTE, self._on_text_field_paste)
        conv_sizer.Add(self.message_field, 0, wx.EXPAND | wx.ALL, 5)

        # Criado (e adicionado ao sizer) antes do botão de emojis de propósito:
        # quando há uma citação ativa, remover a citação é a ação mais imediata,
        # então ela deve ser lida primeiro pelo leitor de tela. A ordem de
        # tabulação segue a ordem de criação dos controles, não só a do sizer.
        self._remove_quote_btn = wx.Button(
            self.conversation_panel, label=i18n.t("remove_quote")
        )
        self._remove_quote_btn.Bind(wx.EVT_BUTTON, self._on_cancel_reply)
        conv_sizer.Add(self._remove_quote_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._remove_quote_btn.Hide()

        # Shown once a link preview has been resolved for a URL currently in
        # the message field (see _check_link_preview_for_current_text()) — mirrors
        # _remove_quote_btn immediately above: same idea, same placement
        # rationale (read before the emoji button, since removing an active
        # preview is the more immediate action).
        self._remove_link_preview_btn = wx.Button(
            self.conversation_panel, label=i18n.t("remove_link_preview")
        )
        self._remove_link_preview_btn.Bind(wx.EVT_BUTTON, self._on_remove_link_preview)
        conv_sizer.Add(self._remove_link_preview_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._remove_link_preview_btn.Hide()

        self._emoji_btn = wx.Button(
            self.conversation_panel, label=i18n.t("emoji_button")
        )
        self._emoji_btn.SetAccessible(AccessibleEmojiButton())
        self._emoji_btn.Bind(wx.EVT_BUTTON, self._on_open_emoji_picker)
        conv_sizer.Add(self._emoji_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._cancel_edit_btn = wx.Button(
            self.conversation_panel, label=i18n.t("cancel_edit")
        )
        self._cancel_edit_btn.Bind(wx.EVT_BUTTON, self._on_cancel_edit)
        conv_sizer.Add(self._cancel_edit_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._cancel_edit_btn.Hide()

        # ── Pending mention pills (one label + remove button per @mention) ──
        self._pending_mentions_panel = wx.Panel(self.conversation_panel)
        self._pending_mentions_sizer = wx.BoxSizer(wx.VERTICAL)
        self._pending_mentions_panel.SetSizer(self._pending_mentions_sizer)
        self._pending_mentions_panel.Hide()
        conv_sizer.Add(
            self._pending_mentions_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5
        )

        self.send_message_btn = wx.Button(
            self.conversation_panel, label=i18n.t("send_message")
        )
        self.send_message_btn.Bind(wx.EVT_BUTTON, self.on_send_message)
        conv_sizer.Add(self.send_message_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self.send_message_btn.Hide()

        # ── Add attachment button (before Record voice — see issue #68: adding
        # attachments is more frequent, and Record voice reads last, matching
        # where most other messaging apps place it) ────────────────────────
        self._add_attachment_btn = wx.Button(
            self.conversation_panel, label=i18n.t("add_attachment")
        )
        self._add_attachment_btn.SetAccessible(AccessibleAddAttachmentButton())
        self._add_attachment_btn.Bind(wx.EVT_BUTTON, self.on_add_attachment)
        conv_sizer.Add(self._add_attachment_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self.record_voice_message_btn = wx.Button(
            self.conversation_panel, label=i18n.t("record_voice_message")
        )
        self.record_voice_message_btn.SetAccessible(
            AccessibleRecordVoiceMessage("Ctrl+R")
        )
        self.record_voice_message_btn.Bind(wx.EVT_BUTTON, self.on_record_voice_message)
        conv_sizer.Add(self.record_voice_message_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        # ── Attachment staging panel (hidden until files are chosen) ─────────
        self._attachment_panel = wx.Panel(self.conversation_panel)
        attach_sizer = wx.BoxSizer(wx.VERTICAL)

        # Dynamic list of "Remover anexo <filename>" buttons, rebuilt on every change
        self._attachments_list_panel = wx.Panel(self._attachment_panel)
        self._attachments_list_sizer = wx.BoxSizer(wx.VERTICAL)
        self._attachments_list_panel.SetSizer(self._attachments_list_sizer)
        attach_sizer.Add(self._attachments_list_panel, 0, wx.EXPAND | wx.LEFT | wx.TOP, 5)

        self._add_more_btn = wx.Button(
            self._attachment_panel, label=i18n.t("add_more_files")
        )
        self._add_more_btn.Bind(wx.EVT_BUTTON, self._on_add_more_files)
        attach_sizer.Add(self._add_more_btn, 0, wx.LEFT | wx.TOP | wx.BOTTOM, 5)

        self._caption_label = wx.StaticText(
            self._attachment_panel, label=i18n.t("attachment_caption_hint")
        )
        attach_sizer.Add(self._caption_label, 0, wx.LEFT | wx.TOP, 5)

        self._caption_field = wx.TextCtrl(
            self._attachment_panel,
            style=wx.TE_DONTWRAP | wx.TE_PROCESS_ENTER,
        )
        self._caption_field.SetHint(i18n.t("attachment_caption_hint"))
        self._caption_field.Bind(wx.EVT_TEXT_PASTE, self._on_text_field_paste)
        self._caption_field.Bind(wx.EVT_TEXT_ENTER, self._on_send_attachment)
        attach_sizer.Add(self._caption_field, 0, wx.EXPAND | wx.ALL, 5)

        self._send_attachment_btn = wx.Button(
            self._attachment_panel, label=i18n.t("send_attachment")
        )
        self._send_attachment_btn.Bind(wx.EVT_BUTTON, self._on_send_attachment)
        attach_sizer.Add(self._send_attachment_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._attachment_panel.SetSizer(attach_sizer)
        self._attachment_panel.Hide()
        conv_sizer.Add(self._attachment_panel, 0, wx.EXPAND | wx.ALL, 5)

        # ── Voice recording panel (hidden until recording starts) ───────────
        self._voice_panel = wx.Panel(self.conversation_panel)
        voice_sizer = wx.BoxSizer(wx.VERTICAL)

        self._discard_voice_btn = wx.Button(
            self._voice_panel, label=i18n.t("discard_voice_message")
        )
        self._discard_voice_btn.SetAccessible(AccessibleDiscardVoiceMessage(self.main_window))
        self._discard_voice_btn.Bind(wx.EVT_BUTTON, self._discard_voice_message)
        voice_sizer.Add(self._discard_voice_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._pause_resume_btn = wx.Button(
            self._voice_panel, label=i18n.t("pause_recording")
        )
        self._pause_resume_btn.SetAccessible(AccessiblePauseResumeRecording(self.main_window))
        self._pause_resume_btn.Bind(wx.EVT_BUTTON, self._toggle_pause_recording)
        voice_sizer.Add(self._pause_resume_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        # Only shown while the recording is paused (_toggle_pause_recording) —
        # plays back everything captured so far. Timer created once here and
        # just Start()/Stop()ed on each play, rather than per-play, so it
        # never ends up with more than one wx.EVT_TIMER handler bound.
        self._play_recorded_btn = wx.Button(
            self._voice_panel, label=i18n.t("play_recorded_audio")
        )
        self._play_recorded_btn.SetAccessible(AccessiblePlayRecordedAudio())
        self._play_recorded_btn.Bind(wx.EVT_BUTTON, self._toggle_play_recorded_audio)
        voice_sizer.Add(self._play_recorded_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._play_recorded_btn.Hide()
        self._recorded_audio_timer = wx.Timer(self._play_recorded_btn)
        self._play_recorded_btn.Bind(
            wx.EVT_TIMER, self._on_recorded_audio_timer, self._recorded_audio_timer
        )

        self._send_voice_btn = wx.Button(
            self._voice_panel, label=i18n.t("send_voice_message")
        )
        self._send_voice_btn.SetAccessible(AccessibleSendVoiceMessage(self.main_window))
        self._send_voice_btn.Bind(wx.EVT_BUTTON, self._send_voice_message)
        voice_sizer.Add(self._send_voice_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._voice_panel.SetSizer(voice_sizer)
        self._voice_panel.Hide()
        conv_sizer.Add(self._voice_panel, 0, wx.LEFT | wx.BOTTOM, 5)

        self.conversation_panel.SetSizer(conv_sizer)
        self.conversation_panel.Bind(wx.EVT_CHAR_HOOK, self._on_conversation_char_hook)
        self.conversation_panel.Hide()
        outer_sizer.Add(self.conversation_panel, 1, wx.EXPAND | wx.ALL, 5)

        self.SetSizer(outer_sizer)

    def _create_messages_list_control(self, mode: str):
        """Create and fully wire one messages-list control for *mode*."""
        if mode == "listbox":
            control = CompatListBoxMessagesCtrl(self.conversation_panel)
        else:
            control = wx.ListCtrl(
                self.conversation_panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL
            )

        label = self.main_window.i18n.t("messages").replace("&", "")
        control.InsertColumn(0, label, width=360)
        accessible = AccessibleMessagesListControl(label)
        control.SetAccessible(accessible)
        if hasattr(self, "_messages_list_accessibles"):
            self._messages_list_accessibles[mode] = accessible
        control.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_message_activated)
        control.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_message_selected)
        control.Bind(wx.EVT_LIST_ITEM_FOCUSED, self._on_message_focused)
        control.Bind(wx.EVT_CONTEXT_MENU, self.on_messages_context_menu)
        control.Bind(wx.EVT_KEY_DOWN, self._on_messages_list_key_down)
        if isinstance(control, CompatListBoxMessagesCtrl):
            control.set_key_down_handler(self._on_messages_list_key_down)
        return control

    def _rerender_messages_list_rows(self):
        """Refresh row text without rebuilding pagination or changing focus."""
        total = len(getattr(self, "_sorted_messages", ()))
        count = min(total, self.messages_list.GetItemCount())
        self.messages_list.Freeze()
        try:
            for index in range(count):
                self.messages_list.SetItemText(
                    index,
                    self._render_message_line(
                        self._sorted_messages[index], index=index, total=total
                    ),
                )
        finally:
            self.messages_list.Thaw()

    def apply_message_list_mode(self, mode: str):
        """Switch the persistent ListCtrl/ListBox without restarting the app."""
        mode = "listbox" if mode in ("listbox", "dataview") else "classic"
        if mode == getattr(self, "_message_list_mode", "classic"):
            self._rerender_messages_list_rows()
            return

        old_list = self.messages_list
        focused = old_list.GetFocusedItem()
        had_focus = wx.Window.FindFocus() is old_list
        new_list = self._message_list_controls[mode]

        self.messages_list = new_list
        self._message_list_mode = mode

        total = len(getattr(self, "_sorted_messages", ())) if self.conversation is not None else 0
        new_list.Freeze()
        try:
            new_list.DeleteAllItems()
            if total:
                for index, msg in enumerate(self._sorted_messages):
                    new_list.Append((self._render_message_line(msg, index=index, total=total),))
        finally:
            new_list.Thaw()

        old_list.Hide()
        new_list.Show()
        self.conversation_panel.Layout()

        if total and focused >= 0:
            focused = min(focused, total - 1)
            new_list.Focus(focused)
            new_list.Select(focused)
            new_list.EnsureVisible(focused)

        if mode == "listbox":
            self._read_more_btn.Hide()
            self._read_more_remainder = ""
        elif total and focused >= 0:
            self._update_read_more_button(focused)

        if had_focus:
            new_list.SetFocus()

    # ── Accelerators ────────────────────────────────────────────────────────

    def create_accelerator_table(self):
        self.ID_CTRL_F              = wx.NewIdRef()
        self.ID_CTRL_N              = wx.NewIdRef()
        self.ID_DELETE_CONV         = wx.NewIdRef()
        self.ID_ALT_SHIFT_C_LIST    = wx.NewIdRef()  # copy number from chat list
        self.ID_CONV_DATA_LIST      = wx.NewIdRef()
        self.ID_TOGGLE_READ_LIST    = wx.NewIdRef()
        self.ID_MUTE_LIST           = wx.NewIdRef()
        self.ID_BLOCK_LIST          = wx.NewIdRef()
        self.ID_CLEAR_LIST          = wx.NewIdRef()
        self.ID_ARCHIVE_LIST        = wx.NewIdRef()
        self.ID_PIN_LIST            = wx.NewIdRef()
        self.ID_CLOSE_CONV_LIST     = wx.NewIdRef()
        # Alt+2 / Alt+3 exist on conversation_panel's own table, and that panel
        # is HIDDEN while no conversation is open — so with nothing open the
        # two combos reached no handler at all and the user got silence, which
        # reads as a broken shortcut (issue #86). Duplicated here, on the
        # always-present panel table, purely so there is somewhere to say
        # "no chat is open". With a conversation open and focus in the chat
        # list they simply delegate to the real handlers, which is what Alt+2
        # ("go to messages") is supposed to do from there anyway.
        self.ID_ALT_2_LIST          = wx.NewIdRef()
        self.ID_ALT_3_LIST          = wx.NewIdRef()
        # ── Mass actions (only act while conversations are selected) ─────────
        # One shortcut per entry of the chat list's "Ações em massa" submenu,
        # for the same reason the messages list has its own set (see
        # create_accel_conversation's ID_BULK_* block): with Settings >
        # Interface do usuário > "Substituir atalhos por ações em massa..."
        # off, that submenu used to be the only way to reach them.
        # Letters mirror the single-chat shortcut where Ctrl+Alt+Shift+<letter>
        # is free — L(impar/clear) — and fall back to a mnemonic where it is
        # already an app-wide shortcut: archive is Ctrl+Shift+Q but
        # Ctrl+Alt+Shift+Q exits WinZapp, and read/unread share Ctrl+Shift+M
        # but Ctrl+Alt+Shift+M marks every chat as read — so archive uses A
        # and the two read states get one shortcut each (R/U) instead of a
        # toggle, matching the submenu, which offers them separately.
        # Delete keeps the Delete key it already has, plus Ctrl+Shift — the
        # same combo the messages list uses for its own bulk delete, which
        # never collides: conversation_panel's table wins while a conversation
        # is open, this one applies otherwise (same split as plain Delete).
        self.ID_BULK_CLEAR_CHATS    = wx.NewIdRef()  # clear selected    (Ctrl+Alt+Shift+L)
        self.ID_BULK_DELETE_CHATS   = wx.NewIdRef()  # delete selected   (Ctrl+Shift+Delete)
        self.ID_BULK_ARCHIVE_CHATS  = wx.NewIdRef()  # archive selected  (Ctrl+Alt+Shift+A)
        self.ID_BULK_READ_CHATS     = wx.NewIdRef()  # mark read         (Ctrl+Alt+Shift+R)
        self.ID_BULK_UNREAD_CHATS   = wx.NewIdRef()  # mark unread       (Ctrl+Alt+Shift+U)
        CS = wx.ACCEL_CTRL | wx.ACCEL_SHIFT
        AS = wx.ACCEL_ALT | wx.ACCEL_SHIFT
        CAS = wx.ACCEL_CTRL | wx.ACCEL_ALT | wx.ACCEL_SHIFT
        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_CTRL,   ord("F"),        self.ID_CTRL_F),
            (wx.ACCEL_CTRL,   ord("N"),        self.ID_CTRL_N),
            (wx.ACCEL_NORMAL, wx.WXK_DELETE,   self.ID_DELETE_CONV),
            (AS,              ord("C"),         self.ID_ALT_SHIFT_C_LIST),
            (CS,              ord("D"),         self.ID_CONV_DATA_LIST),
            (CS,              ord("M"),         self.ID_TOGGLE_READ_LIST),
            (AS,              ord("S"),         self.ID_MUTE_LIST),
            (CS,              ord("B"),         self.ID_BLOCK_LIST),
            (CS,              ord("L"),         self.ID_CLEAR_LIST),
            # Ctrl+Shift+Q, not plain Ctrl+Q: archiving is destructive-ish
            # (drops the conversation out of the main list) and Ctrl+Q sits
            # right next to other single-Ctrl combos a user can easily
            # fat-finger while just trying to navigate the list.
            (CS,              ord("Q"),         self.ID_ARCHIVE_LIST),
            (wx.ACCEL_CTRL,   ord("P"),         self.ID_PIN_LIST),
            (wx.ACCEL_CTRL,   ord("W"),         self.ID_CLOSE_CONV_LIST),
            (wx.ACCEL_ALT,    ord("2"),         self.ID_ALT_2_LIST),
            (wx.ACCEL_ALT,    ord("3"),         self.ID_ALT_3_LIST),
            (CAS,             ord("L"),         self.ID_BULK_CLEAR_CHATS),
            (CS,              wx.WXK_DELETE,    self.ID_BULK_DELETE_CHATS),
            (CAS,             ord("A"),         self.ID_BULK_ARCHIVE_CHATS),
            (CAS,             ord("R"),         self.ID_BULK_READ_CHATS),
            (CAS,             ord("U"),         self.ID_BULK_UNREAD_CHATS),
        ])
        self.SetAcceleratorTable(accel_tbl)
        self.Bind(wx.EVT_MENU, self.on_ctrl_f,                    id=self.ID_CTRL_F)
        self.Bind(wx.EVT_MENU, self._on_new_conversation,         id=self.ID_CTRL_N)
        self.Bind(wx.EVT_MENU, self._on_accel_delete_conv,        id=self.ID_DELETE_CONV)
        self.Bind(wx.EVT_MENU, self._on_accel_copy_number_list,   id=self.ID_ALT_SHIFT_C_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_conversation_data_list, id=self.ID_CONV_DATA_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_toggle_read_list,    id=self.ID_TOGGLE_READ_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_mute_list,           id=self.ID_MUTE_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_block_list,          id=self.ID_BLOCK_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_clear_list,          id=self.ID_CLEAR_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_archive_list,        id=self.ID_ARCHIVE_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_pin_list,            id=self.ID_PIN_LIST)
        self.Bind(wx.EVT_MENU, self.on_context_menu_close,         id=self.ID_CLOSE_CONV_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_jump_last,           id=self.ID_ALT_2_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_jump_unread,         id=self.ID_ALT_3_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_clear_chats,    id=self.ID_BULK_CLEAR_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_delete_chats,   id=self.ID_BULK_DELETE_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_archive_chats,  id=self.ID_BULK_ARCHIVE_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_read_chats,     id=self.ID_BULK_READ_CHATS)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_unread_chats,   id=self.ID_BULK_UNREAD_CHATS)

    def create_accel_conversation(self):
        # ── Navigation / recording ──────────────────────────────────────────
        self.ID_CTRL_R          = wx.NewIdRef()  # record voice            (Ctrl+R)
        self.ID_ALT_2           = wx.NewIdRef()  # jump to last message    (Alt+2)
        self.ID_ESC             = wx.NewIdRef()  # close conversation      (Esc)
        self.CTRL_W             = wx.NewIdRef()  # close conversation      (Ctrl+W)
        self.ID_CTRL_SHIFT_D    = wx.NewIdRef()  # conv data / discard     (Ctrl+Shift+D)
        # ── Attachment / media ───────────────────────────────────────────────
        self.ID_CTRL_SHIFT_A    = wx.NewIdRef()  # add attachment          (Ctrl+Shift+A)
        self.ID_CTRL_SHIFT_B    = wx.NewIdRef()  # block contact           (Ctrl+Shift+B)
        # ── Message-level ────────────────────────────────────────────────────
        self.ID_ALT_FOCUS_FIELD = wx.NewIdRef()  # focus message field     (Alt+<msg-label mnemonic>)
        self.ID_ALT_FOCUS_LIST  = wx.NewIdRef()  # focus messages list     (Alt+<list-label mnemonic>)
        self.ID_ALT_R           = wx.NewIdRef()  # reply                   (Alt+R)
        self.ID_ALT_SHIFT_D     = wx.NewIdRef()  # message data            (Alt+Shift+D)
        self.ID_CTRL_SHIFT_E    = wx.NewIdRef()  # forward                 (Ctrl+Shift+E)
        self.ID_CTRL_SHIFT_P    = wx.NewIdRef()  # pause/resume recording  (Ctrl+Shift+P)
        self.ID_CTRL_SHIFT_R    = wx.NewIdRef()  # react to message        (Ctrl+Shift+R)
        self.ID_DELETE_MSG      = wx.NewIdRef()  # delete focused message  (Delete)
        self.ID_CTRL_C          = wx.NewIdRef()  # copy message            (Ctrl+C)
        self.ID_CTRL_SHIFT_C    = wx.NewIdRef()  # copy caption (photo/video/doc) (Ctrl+Shift+C)
        self.ID_ALT_C           = wx.NewIdRef()  # show text popup         (Alt+C)
        self.ID_ALT_E           = wx.NewIdRef()  # edit message            (Alt+E)
        self.ID_ALT_L           = wx.NewIdRef()  # read-more (truncated)   (Alt+L)
        self.ID_ALT_SHIFT_L     = wx.NewIdRef()  # announce message status (Alt+Shift+L)
        self.ID_ALT_SHIFT_K     = wx.NewIdRef()  # announce message date   (Alt+Shift+K)
        # ── Conversation-level ───────────────────────────────────────────────
        self.ID_CTRL_SHIFT_S    = wx.NewIdRef()  # save as / download      (Ctrl+Shift+S)
        self.ID_CTRL_SHIFT_M    = wx.NewIdRef()  # toggle read / unread    (Ctrl+Shift+M)
        self.ID_CTRL_SHIFT_L    = wx.NewIdRef()  # clear conversation      (Ctrl+Shift+L)
        # ── Search / unread jump ─────────────────────────────────────────────
        self.ID_CTRL_SHIFT_F    = wx.NewIdRef()  # open search panel       (Ctrl+Shift+F)
        self.ID_ALT_3           = wx.NewIdRef()  # jump to unread sep      (Alt+3)
        self.ID_ALT_U           = wx.NewIdRef()  # jump to unread sep      (Alt+U)
        # ── Message bookmarks ────────────────────────────────────────────────
        self.ID_BOOKMARK        = [wx.NewIdRef() for _ in range(10)]  # set/jump (Ctrl+0..9)
        self.ID_BOOKMARK_REMOVE = [wx.NewIdRef() for _ in range(10)]  # remove   (Ctrl+Shift+0..9)
        # ── Temporary (this-conversation-only) bookmarks ─────────────────────
        self.ID_TEMP_BOOKMARK        = [wx.NewIdRef() for _ in range(10)]  # set/jump (Alt+Shift+0..9)
        self.ID_TEMP_BOOKMARK_REMOVE = [wx.NewIdRef() for _ in range(10)]  # remove   (Ctrl+Alt+Shift+0..9)
        # ── Group actions ────────────────────────────────────────────────────
        self.ID_ALT_SHIFT_R     = wx.NewIdRef()  # reply privately         (Alt+Shift+R)
        self.ID_ALT_SHIFT_E     = wx.NewIdRef()  # recent reactions        (Alt+Shift+E)
        self.ID_ALT_SHIFT_M     = wx.NewIdRef()  # mentions                (Alt+Shift+M)
        self.ID_ALT_SHIFT_C     = wx.NewIdRef()  # copy phone number       (Alt+Shift+C)
        self.ID_ALT_SHIFT_V     = wx.NewIdRef()  # converse with           (Alt+Shift+V)
        self.ID_ALT_SHIFT_Q     = wx.NewIdRef()  # goto quoted message     (Alt+Shift+Q)
        self.ID_ALT_SHIFT_S     = wx.NewIdRef()  # mute / unmute           (Alt+Shift+S)
        # ── Message star ─────────────────────────────────────────────────────
        self.ID_CTRL_SHIFT_O    = wx.NewIdRef()  # star message            (Ctrl+Shift+O)
        # ── Mass actions (only act while messages are selected) ──────────────
        # One shortcut per entry of the context menu's "Ações em massa"
        # submenu. Deliberately a family of their own instead of relying on
        # the single-message shortcuts being remapped by Settings > Interface
        # do usuário > "Substituir atalhos por ações em massa...": that
        # setting is exactly what a user turns OFF to keep acting on the
        # focused message while a selection exists, and with it off the
        # submenu used to be the only way to reach these at all.
        # Letters follow the single-message shortcut where that letter is
        # free — C(opy), E (forward, Ctrl+Shift+E), S(ave) — and fall back to
        # a mnemonic where Ctrl+Alt+Shift+<letter> is already an app-wide
        # shortcut: star is Ctrl+Shift+O but Ctrl+Alt+Shift+O toggles offline
        # mode, and pin is Ctrl+Shift+P but Ctrl+Alt+Shift+P is the global
        # audio play/pause, so those two use F (favoritar) and X (fixar).
        # Delete keeps the Delete key it already has, plus Ctrl+Shift.
        self.ID_BULK_COPY       = wx.NewIdRef()  # copy selected           (Ctrl+Alt+Shift+C)
        self.ID_BULK_FORWARD    = wx.NewIdRef()  # forward selected        (Ctrl+Alt+Shift+E)
        self.ID_BULK_STAR       = wx.NewIdRef()  # star selected           (Ctrl+Alt+Shift+F)
        self.ID_BULK_PIN        = wx.NewIdRef()  # pin selected            (Ctrl+Alt+Shift+X)
        self.ID_BULK_SAVE       = wx.NewIdRef()  # save selected           (Ctrl+Alt+Shift+S)
        self.ID_BULK_DELETE     = wx.NewIdRef()  # delete selected         (Ctrl+Shift+Delete)
        # ── Audio speed ──────────────────────────────────────────────────────
        self.ID_ALT_COMMA       = wx.NewIdRef()  # decrease audio speed    (Alt+,)
        self.ID_ALT_PERIOD      = wx.NewIdRef()  # increase audio speed    (Alt+.)
        self.ID_CTRL_PERIOD     = wx.NewIdRef()  # insert emoji            (Ctrl+.)

        CS  = wx.ACCEL_CTRL | wx.ACCEL_SHIFT
        AS  = wx.ACCEL_ALT  | wx.ACCEL_SHIFT
        CAS = wx.ACCEL_CTRL | wx.ACCEL_ALT | wx.ACCEL_SHIFT

        # message_label's own native mnemonic ("&" in "type_message"/
        # "reply_to"/"reply_to_group", all deliberately kept on the same
        # letter across translations) is supposed to redirect Alt+<letter>
        # focus to whichever control follows it — but that relies entirely
        # on wx/Windows re-scanning sibling controls at key-press time, and
        # showing _remove_quote_btn when entering reply mode (see
        # _on_menu_reply) was observed to break that redirect: Alt+<letter>
        # stopped moving focus to message_field once "Responder a Fulano"
        # replaced the default label. Binding the same letter as an
        # explicit accelerator that unconditionally focuses message_field
        # makes it work the same way in every state, independent of that
        # native mnemonic mechanism.
        def _mnemonic_letter(i18n_key: str, default: str) -> str:
            label = self.main_window.i18n.t(i18n_key)
            amp = label.find("&")
            if 0 <= amp < len(label) - 1 and label[amp + 1].isalpha():
                return label[amp + 1].upper()
            return default

        focus_field_letter = _mnemonic_letter("type_message", "D")
        # Same reasoning as message_label's mnemonic above, for the
        # "&Mensagens" label over messages_list: showing _search_panel (see
        # on_ctrl_f) was observed to break that native redirect the same
        # way, leaving Alt+M unable to move focus into the messages list
        # while the in-conversation search bar was open.
        focus_list_letter = _mnemonic_letter("messages", "M")

        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_ALT,     ord(focus_field_letter), self.ID_ALT_FOCUS_FIELD),
            (wx.ACCEL_ALT,     ord(focus_list_letter),  self.ID_ALT_FOCUS_LIST),
            (wx.ACCEL_CTRL,    ord("R"),         self.ID_CTRL_R),
            (wx.ACCEL_ALT,     ord("2"),         self.ID_ALT_2),
            (wx.ACCEL_NORMAL,  wx.WXK_ESCAPE,    self.ID_ESC),
            (wx.ACCEL_CTRL,    ord("W"),          self.CTRL_W),
            (CS,               ord("D"),          self.ID_CTRL_SHIFT_D),
            (CS,               ord("A"),          self.ID_CTRL_SHIFT_A),
            (CS,               ord("B"),          self.ID_CTRL_SHIFT_B),
            (wx.ACCEL_ALT,     ord("R"),          self.ID_ALT_R),
            (AS,               ord("D"),          self.ID_ALT_SHIFT_D),
            (CS,               ord("E"),          self.ID_CTRL_SHIFT_E),
            (CS,               ord("P"),          self.ID_CTRL_SHIFT_P),
            (CS,               ord("R"),          self.ID_CTRL_SHIFT_R),
            (wx.ACCEL_NORMAL,  wx.WXK_DELETE,     self.ID_DELETE_MSG),
            (wx.ACCEL_CTRL,    ord("C"),          self.ID_CTRL_C),
            (CS,               ord("C"),          self.ID_CTRL_SHIFT_C),
            (wx.ACCEL_ALT,     ord("C"),          self.ID_ALT_C),
            (wx.ACCEL_ALT,     ord("E"),          self.ID_ALT_E),
            (wx.ACCEL_ALT,     ord("L"),          self.ID_ALT_L),
            (AS,               ord("L"),          self.ID_ALT_SHIFT_L),
            (AS,               ord("K"),          self.ID_ALT_SHIFT_K),
            (CS,               ord("S"),          self.ID_CTRL_SHIFT_S),
            (CS,               ord("M"),          self.ID_CTRL_SHIFT_M),
            (CS,               ord("L"),          self.ID_CTRL_SHIFT_L),
            (CS,               ord("F"),          self.ID_CTRL_SHIFT_F),
            (wx.ACCEL_ALT,     ord("3"),          self.ID_ALT_3),
            (wx.ACCEL_ALT,     ord("U"),          self.ID_ALT_U),
            (wx.ACCEL_ALT,     ord("u"),          self.ID_ALT_U),
            (wx.ACCEL_CTRL,    ord("L"),          self.ID_ALT_U),
            (wx.ACCEL_CTRL,    ord("l"),          self.ID_ALT_U),
            (AS,               ord("R"),          self.ID_ALT_SHIFT_R),
            (AS,               ord("E"),          self.ID_ALT_SHIFT_E),
            (AS,               ord("M"),          self.ID_ALT_SHIFT_M),
            (AS,               ord("C"),          self.ID_ALT_SHIFT_C),
            (AS,               ord("V"),          self.ID_ALT_SHIFT_V),
            (AS,               ord("Q"),          self.ID_ALT_SHIFT_Q),
            (AS,               ord("S"),          self.ID_ALT_SHIFT_S),
            (CS,               ord("O"),           self.ID_CTRL_SHIFT_O),
            (wx.ACCEL_ALT,     ord(","),           self.ID_ALT_COMMA),
            (wx.ACCEL_ALT,     ord("."),           self.ID_ALT_PERIOD),
            (wx.ACCEL_CTRL,    ord("."),           self.ID_CTRL_PERIOD),
            (CAS,              ord("C"),           self.ID_BULK_COPY),
            (CAS,              ord("E"),           self.ID_BULK_FORWARD),
            (CAS,              ord("F"),           self.ID_BULK_STAR),
            (CAS,              ord("X"),           self.ID_BULK_PIN),
            (CAS,              ord("S"),           self.ID_BULK_SAVE),
            (CS,               wx.WXK_DELETE,      self.ID_BULK_DELETE),
        ] + [
            (wx.ACCEL_CTRL, ord(str(d)), self.ID_BOOKMARK[d]) for d in range(10)
        ] + [
            (CS,            ord(str(d)), self.ID_BOOKMARK_REMOVE[d]) for d in range(10)
        ] + [
            # Alt+Shift+<digit> / Ctrl+Alt+Shift+<digit>: temporary bookmarks.
            # Both combos were verified to reach the app for all ten digits —
            # including the zeros, where the extra Alt is what keeps them from
            # matching the Windows IME hotkey that eats plain Ctrl+Shift+0
            # (see MainWindow._set_bookmark_zero_hotkey).
            (AS,            ord(str(d)), self.ID_TEMP_BOOKMARK[d]) for d in range(10)
        ] + [
            (CAS,           ord(str(d)), self.ID_TEMP_BOOKMARK_REMOVE[d]) for d in range(10)
        ])
        self.conversation_panel.SetAcceleratorTable(accel_tbl)
        self.Bind(wx.EVT_MENU, self._on_accel_focus_field,          id=self.ID_ALT_FOCUS_FIELD)
        self.Bind(wx.EVT_MENU, self._on_accel_focus_list,           id=self.ID_ALT_FOCUS_LIST)
        self.Bind(wx.EVT_MENU, self.on_record_voice_message,       id=self.ID_CTRL_R)
        self.Bind(wx.EVT_MENU, self._on_accel_jump_last,           id=self.ID_ALT_2)
        self.Bind(wx.EVT_MENU, self.close_conversation,            id=self.ID_ESC)
        self.Bind(wx.EVT_MENU, self.close_conversation,            id=self.CTRL_W)
        self.Bind(wx.EVT_MENU, self._on_ctrl_shift_d,              id=self.ID_CTRL_SHIFT_D)
        self.Bind(wx.EVT_MENU, self.on_add_attachment,             id=self.ID_CTRL_SHIFT_A)
        self.Bind(wx.EVT_MENU, self._on_action_save_as,            id=self.ID_CTRL_SHIFT_S)
        self.Bind(wx.EVT_MENU, self._on_accel_reply,               id=self.ID_ALT_R)
        self.Bind(wx.EVT_MENU, self._on_accel_message_data,        id=self.ID_ALT_SHIFT_D)
        self.Bind(wx.EVT_MENU, self._on_accel_forward,             id=self.ID_CTRL_SHIFT_E)
        self.Bind(wx.EVT_MENU, self._on_ctrl_shift_p,              id=self.ID_CTRL_SHIFT_P)
        self.Bind(wx.EVT_MENU, self._on_accel_react,               id=self.ID_CTRL_SHIFT_R)
        self.Bind(wx.EVT_MENU, self._on_accel_delete_message,      id=self.ID_DELETE_MSG)
        self.Bind(wx.EVT_MENU, self._on_accel_copy_message,        id=self.ID_CTRL_C)
        self.Bind(wx.EVT_MENU, self._on_accel_copy_caption,        id=self.ID_CTRL_SHIFT_C)
        self.Bind(wx.EVT_MENU, self._on_accel_show_text_popup,     id=self.ID_ALT_C)
        self.Bind(wx.EVT_MENU, self._on_accel_edit_message,        id=self.ID_ALT_E)
        self.Bind(wx.EVT_MENU, self._on_read_more,                 id=self.ID_ALT_L)
        self.Bind(wx.EVT_MENU, self._on_accel_msg_status,          id=self.ID_ALT_SHIFT_L)
        self.Bind(wx.EVT_MENU, self._on_accel_msg_datetime,        id=self.ID_ALT_SHIFT_K)
        self.Bind(wx.EVT_MENU, self._on_accel_block,               id=self.ID_CTRL_SHIFT_B)
        self.Bind(wx.EVT_MENU, self._on_accel_toggle_read,         id=self.ID_CTRL_SHIFT_M)
        self.Bind(wx.EVT_MENU, self._on_accel_clear,               id=self.ID_CTRL_SHIFT_L)
        self.Bind(wx.EVT_MENU, self._on_accel_open_search,         id=self.ID_CTRL_SHIFT_F)
        self.Bind(wx.EVT_MENU, self._on_accel_jump_unread,         id=self.ID_ALT_3)
        self.Bind(wx.EVT_MENU, self._on_accel_jump_unread,         id=self.ID_ALT_U)
        self.Bind(wx.EVT_MENU, self._on_accel_reply_private,       id=self.ID_ALT_SHIFT_R)
        self.Bind(wx.EVT_MENU, self._on_accel_recent_reactions,    id=self.ID_ALT_SHIFT_E)
        self.Bind(wx.EVT_MENU, self._on_accel_mentions,            id=self.ID_ALT_SHIFT_M)
        self.Bind(wx.EVT_MENU, self._on_accel_copy_number_speak,   id=self.ID_ALT_SHIFT_C)
        self.Bind(wx.EVT_MENU, self._on_accel_alt_shift_v,         id=self.ID_ALT_SHIFT_V)
        self.Bind(wx.EVT_MENU, self._on_accel_goto_quoted,         id=self.ID_ALT_SHIFT_Q)
        self.Bind(wx.EVT_MENU, self._on_accel_mute,                id=self.ID_ALT_SHIFT_S)
        self.Bind(wx.EVT_MENU, self._on_accel_star,                 id=self.ID_CTRL_SHIFT_O)
        self.Bind(wx.EVT_MENU, self._on_audio_speed_decrease,      id=self.ID_ALT_COMMA)
        self.Bind(wx.EVT_MENU, self._on_audio_speed_increase,      id=self.ID_ALT_PERIOD)
        self.Bind(wx.EVT_MENU, self._on_open_emoji_picker,          id=self.ID_CTRL_PERIOD)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_copy,            id=self.ID_BULK_COPY)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_forward,         id=self.ID_BULK_FORWARD)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_star,            id=self.ID_BULK_STAR)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_pin,             id=self.ID_BULK_PIN)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_save,            id=self.ID_BULK_SAVE)
        self.Bind(wx.EVT_MENU, self._on_accel_bulk_delete,          id=self.ID_BULK_DELETE)
        for _d in range(10):
            self.Bind(wx.EVT_MENU, lambda e, d=_d: self._on_bookmark_set_or_jump(d), id=self.ID_BOOKMARK[_d])
            self.Bind(wx.EVT_MENU, lambda e, d=_d: self._on_bookmark_remove(d),      id=self.ID_BOOKMARK_REMOVE[_d])
            self.Bind(wx.EVT_MENU, lambda e, d=_d: self._on_temp_bookmark_set_or_jump(d), id=self.ID_TEMP_BOOKMARK[_d])
            self.Bind(wx.EVT_MENU, lambda e, d=_d: self._on_temp_bookmark_remove(d),      id=self.ID_TEMP_BOOKMARK_REMOVE[_d])

    # ── Conversations list events ───────────────────────────────────────────

    def _on_conversation_focused(self, event):
        idx = event.GetIndex()
        if 0 <= idx < len(self.chats_list):
            chat = self.chats_list[idx]
            jid = chat.get("remoteJid", "")
            # Onde o usuario parou na lista, que nao e a mesma coisa que qual
            # conversa esta aberta: dava para mover o foco ate a Conversa 2 sem
            # abri-la, ir para as mensagens da Conversa 1 com Alt+2 e voltar com
            # Alt+1 na Conversa 1, porque _restore_conversation_selection() so
            # sabia de _last_open_jid. Vale tambem para o Esc/Ctrl+W.
            if jid:
                self._last_list_focus_jid = jid
            if jid and jid in self.selected_chats:
                self.selection_sound.play()

    def on_conversation_selected(self, event):
        self.on_conversation_selected_by_index(event.GetIndex())

    def on_conversation_selected_by_index(self, index):
        try:
            self.navigate_to_conversation(self.chats_list[index])
        except Exception:
            return

    def _stop_typing_for_current_conversation(self):
        """Stop typing/recording status for the currently open conversation, if active."""
        if self._is_typing and self.conversation is not None:
            jid = self.conversation.get("remoteJid", "")
            if jid and not jid.endswith("@newsletter"):
                self.main_window.send_typing_status(jid, False, jid.endswith("@g.us"))
            self._is_typing = False
        if self._is_recording and self.conversation is not None:
            jid = self.conversation.get("remoteJid", "")
            if jid and not jid.endswith("@newsletter"):
                self.main_window.send_recording_status(jid, False, jid.endswith("@g.us"))

    def _conversation_note_text(self, name: str, is_group: bool) -> str:
        """Subtitle for the conversation-data button. A one-to-one chat whose
        name is still just a phone number gets it labelled as such, so the
        screen reader announces "Telefone: <number>" rather than reading bare
        digits as if they were a contact name."""
        if not is_group and is_phone_like(name):
            return f"{self.main_window.i18n.t('phone_label')}: {name}"
        return name

    def _message_label_text(self, jid: str, conversation: dict, name: str) -> str:
        """What the composer's label reads: either why the field cannot be
        written to, or who the message is going to.

        Shared by navigate_to_conversation() (opening a chat) and
        update_conversation_name() (a rename landing on the open one) so the
        two cannot drift — they used to carry separate copies of this switch.
        """
        i18n = self.main_window.i18n
        if jid.endswith("@newsletter"):
            return i18n.t("channel_read_only")
        is_group = jid.endswith("@g.us")
        if is_group and self.main_window._is_group_send_restricted(conversation):
            return i18n.t("group_admins_only")
        return f"{i18n.t('type_message_group') if is_group else i18n.t('type_message')} {name}"

    def _apply_composer_permissions(self, jid: str, conversation: dict):
        """Enable/disable the composer controls according to what *jid* allows.

        Three cases: a channel (nothing can be posted at all), a group with
        "only admins can send messages" on where the user isn't an admin, and
        everything else.  Kept out of navigate_to_conversation() so it can be
        tested without a live wx panel — the emoji button used to be the one
        control this switch forgot, staying clickable in a group the user
        cannot post in and inserting text into a read-only field.
        """
        is_channel = jid.endswith("@newsletter")
        admins_only_group = (
            jid.endswith("@g.us")
            and self.main_window._is_group_send_restricted(conversation)
        )
        if is_channel:
            self.message_field.Enable()
            self.message_field.SetEditable(True)
            self.message_field.Disable()
            self.send_message_btn.Disable()
            self.record_voice_message_btn.Disable()
            self._add_attachment_btn.Disable()
            self._emoji_btn.Disable()
        elif admins_only_group:
            # Keep the field enabled/focusable (unlike the channel case
            # above) so it stays reachable via Tab/the Alt+D accelerator and
            # NVDA can announce its read-only state — only actual editing is
            # blocked. Sending/attaching/recording would just be rejected by
            # WhatsApp Web anyway, so those stay disabled like the channel case.
            # Disable() here instead of SetEditable(False) drops the field out
            # of the tab order entirely, which leaves a screen-reader user in
            # a group they cannot post in with nothing announcing why.
            self.message_field.Enable()
            self.message_field.SetEditable(False)
            self.send_message_btn.Disable()
            self.record_voice_message_btn.Disable()
            self._add_attachment_btn.Disable()
            self._emoji_btn.Disable()
        else:
            self.message_field.Enable()
            self.message_field.SetEditable(True)
            self.send_message_btn.Enable()
            self.record_voice_message_btn.Enable()
            self._add_attachment_btn.Enable()
            self._emoji_btn.Enable()

    def refresh_composer_permissions(self, jid: str, transition: bool = True):
        if not self.conversation or self.conversation.get("remoteJid") != jid:
            return

        conversation = self.main_window.chats.get(jid) or self.conversation
        was_editable = self.message_field.IsEditable()
        self._apply_composer_permissions(jid, conversation)
        self.message_label.SetLabel(
            self._message_label_text(jid, conversation, self.conversation_name)
        )
        self.conversation_panel.Layout()
        if was_editable and not self.message_field.IsEditable():
            self.main_window.output(self.main_window.i18n.t(
                "group_send_restricted_now" if transition else "group_send_restricted"
            ))

    def update_conversation_name(self, jid: str, new_name: str):
        """Apply a group rename to the conversation currently on screen.

        A no-op unless *jid* is the open conversation: a rename anywhere else
        only has to reach the chat list, which main.py already schedules
        separately (_schedule_set_chats). Called via wx.CallAfter from
        MainWindow's two group-rename paths, so this runs on the UI thread.
        """
        if not self.conversation or self.conversation.get("remoteJid") != jid:
            return

        self.conversation_name = new_name
        is_group = jid.endswith("@g.us")
        self._conv_data_btn.SetNote(self._conversation_note_text(new_name, is_group))
        self.message_label.SetLabel(
            self._message_label_text(jid, self.main_window.chats.get(jid, {}), new_name)
        )
        self.conversation_panel.Layout()

    def navigate_to_conversation(self, conversation):
        if self.conversation is not None and self.conversation.get("remoteJid") == conversation.get("remoteJid"):
            self.conversation = conversation
            # Conversation already open — just focus the message input field.
            wx.CallAfter(self.message_field.SetFocus)
            return
        self._stop_typing_for_current_conversation()
        self._cancel_active_recording()
        # Leaving the conversation invalidates any pending auto-chain timers —
        # they captured a target_msg from THIS conversation's list and would
        # otherwise start stale audio (possibly in the wrong chat) later.
        self._cancel_pending_chain_timers()
        # Audio keeps playing across conversation switches.  Save the current
        # position so it can be restored if the same message is played again
        # after a different audio has taken over and closed the stream.
        if self._current_audio_id is not None and self._audio_stream is not None:
            try:
                _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
                pos   = _ctrl.get_position()
                total = _ctrl.get_length()
                if 0 < pos < total:
                    self._audio_positions[self._current_audio_id] = pos
            except Exception:
                pass
        self._hide_audio_controls()
        self._hide_all_media_controls()
        self._hide_media_transfer_gauge()
        self._hide_attachment_panel()
        self._unread_sep_idx = -1  # reset separator for new conversation
        self._sep_anchors_read_position = False
        # _msg_bookmarks is intentionally NOT reset here — bookmarks now span
        # conversations (see the declaration in __init__). _msg_temp_bookmarks
        # is the opposite: scoped to one conversation, so switching away from
        # it is exactly when it must go.
        self._msg_temp_bookmarks.clear()
        self._first_unread_msg_id = None
        self._first_unread_count = 0
        self._unread_sep_marked_read = False
        self._quoted_message = None
        self._reaction_map   = {}
        self._is_loading_more = False
        self._reset_expanded_window()
        # Reset mention state for the new conversation
        self._pending_mentions.clear()
        self._pending_mention_display_names.clear()
        self._group_participants_cache = []
        self._hide_mention_suggestions()
        if hasattr(self, "_pending_mentions_panel"):
            self._rebuild_mention_pills()
        # Reset search state
        self._search_results    = []
        self._search_result_idx = -1
        if hasattr(self, "_search_panel") and self._search_panel.IsShown():
            self._search_panel.Hide()
            self._search_open_btn.Show()
            self._search_field.SetValue("")
        self.conversation = conversation
        
        # Load up to 200 messages from local DB when opening conversation to support fast startup
        try:
            _conv_jid = conversation.get("remoteJid", "")
            if _conv_jid:
                configured_limit = int(self.main_window.settings.get("user_interface", {}).get("messages_page_size", 200))
                unread_count = int(conversation.get("unreadCount") or 0)
                limit = db_fetch_limit(configured_limit, unread_count)
                db_msgs = self.main_window.db.get_messages(_conv_jid, limit=limit)
                db_msgs.reverse()
                if "messages" not in conversation:
                    conversation["messages"] = {}
                conversation["messages"]["messages"] = {
                    "total": self.main_window.db.get_message_count(_conv_jid),
                    "pages": 1,
                    "currentPage": 1,
                    "records": db_msgs
                }
        except Exception as e:
            logging.error(f"[navigate_to_conversation] Failed to load messages from DB: {e}")

        pending_rows = [
            row for row in self._outgoing_virtual_messages.values()
            if row.get("key", {}).get("remoteJid") == conversation.get("remoteJid", "")
            and row.get("_local_pending")
        ]
        records = conversation.setdefault("messages", {}).setdefault("messages", {}).setdefault("records", [])
        known_local_ids = {row.get("_local_id") for row in records}
        records.extend(row for row in pending_rows if row.get("_local_id") not in known_local_ids)

        _conv_jid = conversation.get("remoteJid", "")
        self._last_open_jid = _conv_jid
        self.conversation_name = (
            self.main_window._resolve_contact_name(conversation)
            or self.main_window.find_name_through_messages(conversation)
            or conversation.get("name", "")
            or ("" if _conv_jid.endswith("@g.us") else conversation.get("pushName", ""))
            or self.main_window.find_jid_through_messages(conversation)
            or self.main_window._format_jid_for_display(_conv_jid)
            or (self.main_window.i18n.t("unknown_group") if _conv_jid.endswith("@g.us") else self.main_window.i18n.t("unknown_contact"))
        )
        jid      = conversation.get("remoteJid", "")
        is_group = jid.endswith("@g.us")
        i18n     = self.main_window.i18n

        # Update conversation-data button
        self._conv_data_btn.SetLabel(
            i18n.t("group_data") if is_group else i18n.t("conversation_data")
        )
        self._conv_data_btn.SetNote(
            self._conversation_note_text(self.conversation_name, is_group)
        )

        self._apply_composer_permissions(jid, conversation)
        self.message_label.SetLabel(
            self._message_label_text(jid, conversation, self.conversation_name)
        )
            
        if hasattr(self, "_remove_quote_btn"):
            self._remove_quote_btn.Hide()
        self.conversation_panel.Show()
        self.Layout()
        # Snapshot before the background thread zeros unreadCount on the same dict
        self._pending_open_unread = effective_unread_count(conversation)
        # mark_conversation_as_read() finishes its synchronous part (zero the
        # count, wx.CallAfter the chat-list row's text update) almost
        # instantly — starting the thread here raced against the focus
        # CallAfter scheduled at the bottom of this method and routinely won,
        # so NVDA announced the chat-list row's text changing to "read"
        # before announcing the newly focused messages list/message field
        # from opening the conversation. Starting it from the SAME
        # wx.CallAfter queue as the focus change, scheduled further down,
        # guarantees FIFO order instead of leaving it to thread-timing luck.
        # Background: fetch profile/last-seen and update button note
        threading.Thread(
            target=self._fetch_and_update_profile,
            args=(conversation,),
            daemon=True,
        ).start()
        # Subscribe to presence events for this contact so last-seen and typing
        # indicators arrive via onpresencechanged Socket.IO events.
        self.main_window.subscribe_presence(jid)
        # Background: cache group participants for @mention suggestions
        if is_group:
            threading.Thread(
                target=self._fetch_group_participants,
                args=(jid,),
                daemon=True,
            ).start()
        if self.search_field.GetValue().strip():
            self.search_field.Clear()
        self.populate_messages()
        self._sync_pending_document_gauge()

        # Re-show audio controls only if the playing audio message is focused.
        if (self._current_audio_id is not None
                and self._audio_conv_jid == jid
                and self._audio_stream is not None
                and self._focused_msg_id() == self._current_audio_id):
            self._show_audio_controls()
            self.audio_speed_btn.SetLabel(
                self._format_speed(self._audio_speed_steps[self._audio_speed_index])
            )


        # Move keyboard focus based on user preference.
        # Deferred via wx.CallAfter so this is the last item in the event
        # queue — prevents add_chats_to_ui (which may have been scheduled
        # earlier by restore_window on a notification click) from scheduling
        # its own lst.SetFocus and stealing focus away from the conversation.
        focus_setting = self.main_window.settings.get("user_interface", {}).get("focus_on_open", "message_field")
        logging.info(
            "[navigate_to_conversation] scheduling focus: setting=%r jid=%s",
            focus_setting, jid,
        )

        def _do_focus_messages_list():
            try:
                ok = self.messages_list.SetFocus()
                logging.info(
                    "[navigate_to_conversation] messages_list.SetFocus() ran, "
                    "FindFocus()=%r messages_list=%r",
                    wx.Window.FindFocus(), self.messages_list,
                )
            except Exception:
                logging.exception("[navigate_to_conversation] messages_list.SetFocus() raised")

        def _do_focus_message_field():
            try:
                self.message_field.SetFocus()
                logging.info(
                    "[navigate_to_conversation] message_field.SetFocus() ran, "
                    "FindFocus()=%r message_field=%r",
                    wx.Window.FindFocus(), self.message_field,
                )
            except Exception:
                logging.exception("[navigate_to_conversation] message_field.SetFocus() raised")

        if focus_setting == "unread_or_last" or not self.message_field.IsEnabled():
            wx.CallAfter(_do_focus_messages_list)
        else:
            wx.CallAfter(_do_focus_message_field)

        # Queued after the focus CallAfter above so it always runs later on
        # the event loop — see this method's comment where the thread start
        # used to live, right after _pending_open_unread was snapshotted.
        def _start_mark_as_read():
            threading.Thread(
                target=self.main_window.mark_conversation_as_read,
                args=(jid,),
                daemon=True,
            ).start()
        wx.CallAfter(_start_mark_as_read)

    def on_search_query_changed(self, event):
        # Route through add_chats_to_ui so the active filter and proper sort
        # order are both respected (add_chats_to_ui reads search_field itself).
        self.main_window.add_chats_to_ui()

    def _on_filter_changed(self, event):
        """Update the active conversation filter and rebuild the list."""
        _filter_map = ['all', 'unread', 'groups', 'individual']
        sel = self._filter_radio.GetSelection()
        self._conv_filter = _filter_map[sel] if 0 <= sel < len(_filter_map) else 'all'
        self.main_window.add_chats_to_ui()
        # Selecting a filter option leaves keyboard focus on the radio box —
        # the list itself gets rebuilt but nothing ever moves focus/selection
        # there, so the user had no way to tell what (if anything) the new
        # filter actually matched without tabbing over manually. Only the
        # list's own item focus/selection is updated here, NOT keyboard focus
        # (no SetFocus()) — moving keyboard focus away from the radio box cut
        # off NVDA mid-announcement of the option that was just selected.
        lst = self.conversations_list
        if self.chats_list:
            lst.Focus(0)
            lst.Select(0)
            lst.EnsureVisible(0)

    def on_ctrl_f(self, event):
        self.search_field.SetFocus()

    def _on_search_field_key_down(self, event):
        """Down arrow in the search field moves focus to the first conversation."""
        if event.GetKeyCode() == wx.WXK_DOWN:
            lst = self.conversations_list
            if lst.GetItemCount() > 0:
                lst.SetFocus()
                lst.Focus(0)
                lst.Select(0)
            return
        event.Skip()

    def on_change_message_field(self, event):
        # Don't touch button visibility while recording or staging attachments.
        if self._is_recording or self._attachment_panel.IsShown():
            return
        msg = self.message_field.GetValue()
        if msg.strip():
            self.send_message_btn.Show()
            self.record_voice_message_btn.Hide()
        else:
            self.send_message_btn.Hide()
            self.record_voice_message_btn.Show()
        # Sync typing status with WPPConnect (only on state transitions)
        if self.conversation is not None:
            jid = self.conversation.get("remoteJid", "")
            if jid and not jid.endswith("@newsletter"):
                is_group = jid.endswith("@g.us")
                now_typing = bool(msg.strip())
                if now_typing != self._is_typing:
                    self._is_typing = now_typing
                    self.main_window.send_typing_status(jid, now_typing, is_group)
        self._on_text_changed_mention_check()
        self._schedule_link_preview_check()

    # ── Outgoing link preview (see core/link_preview.py) ────────────────────

    _LINK_PREVIEW_DEBOUNCE_MS = 700

    def _schedule_link_preview_check(self):
        """Debounce URL detection: re-checked shortly after typing pauses,
        not on every keystroke — a preview fetch is a real HTTP request."""
        if self._link_preview_debounce_timer is not None:
            self._link_preview_debounce_timer.Stop()
        self._link_preview_debounce_timer = wx.CallLater(
            self._LINK_PREVIEW_DEBOUNCE_MS, self._check_link_preview_for_current_text
        )

    def _check_link_preview_for_current_text(self):
        url = find_first_url(self.message_field.GetValue())

        if url != self._link_preview_source_url and (
            self._pending_link_preview is not None or self._link_preview_source_url
        ):
            self._clear_link_preview()

        if not url or url == self._link_preview_dismissed_url:
            return
        if self._pending_link_preview is not None and self._link_preview_source_url == url:
            return  # already resolved for this exact URL

        self._link_preview_fetch_token += 1
        token = self._link_preview_fetch_token
        self._link_preview_source_url = url

        def _bg_fetch():
            preview = fetch_link_preview(url)
            wx.CallAfter(self._on_link_preview_fetched, token, url, preview)

        threading.Thread(target=_bg_fetch, daemon=True).start()

    def _on_link_preview_fetched(self, token, url, preview):
        # Superseded by a later fetch, or by the field being cleared, while
        # this one was still in flight on its own thread.
        if token != self._link_preview_fetch_token:
            return
        if find_first_url(self.message_field.GetValue()) != url:
            return
        if not preview:
            return  # no title/description available — silently no-op
        self._pending_link_preview = preview
        self._remove_link_preview_btn.Show()
        self.conversation_panel.Layout()

    def _on_remove_link_preview(self, event=None):
        """User explicitly dismissed the preview — stays dismissed for this
        exact URL until the field's URL actually changes to something else
        (mirrors WhatsApp Web's own composer: closing the card doesn't bring
        it right back while you keep typing around the same link)."""
        self._link_preview_dismissed_url = self._link_preview_source_url
        self._clear_link_preview()
        wx.CallAfter(self.message_field.SetFocus)

    def _clear_link_preview(self):
        self._link_preview_fetch_token += 1  # invalidate any in-flight fetch
        self._pending_link_preview = None
        self._link_preview_source_url = ""
        if self._remove_link_preview_btn.IsShown():
            self._remove_link_preview_btn.Hide()
            self.conversation_panel.Layout()

    def _on_open_emoji_picker(self, event):
        """Insert an emoji at the caret without leaving the message editor."""
        if (
            self.conversation is None
            or not self.conversation_panel.IsShown()
            or not self.message_field.IsShown()
            or not self.message_field.IsEnabled()
            or not self.message_field.IsEditable()
        ):
            return
        choose_and_insert_emoji(self, self.message_field, self.main_window.i18n)

    def _on_conversation_char_hook(self, event):
        if self._is_phantom_nvda_char(event):
            # Veto here too, not just in _on_message_field_char(): this hook
            # runs for the whole panel regardless of which child control
            # currently has focus, and the "type anywhere to reply" redirect
            # below treats 'ÿ' as an ordinary alnum character — chr(0xFF)
            # .isalnum() is True in Python — so with focus on the
            # conversations/messages list (the common case while browsing
            # with a screen reader) it was moving focus to message_field and
            # writing 'ÿ' into it via WriteText(), bypassing that other
            # veto entirely, since WriteText() never raises EVT_CHAR.
            return  # consume — do not insert, do not Skip()
        kc = event.GetKeyCode()
        # Intercept Esc and Enter when the mention suggestion list has focus so
        # they are handled here, before the accelerator table fires
        # close_conversation for Esc or any other panel-level binding.
        if hasattr(self, "_mention_panel") and self._mention_panel.IsShown():
            if kc == wx.WXK_ESCAPE:
                self._hide_mention_suggestions()
                wx.CallAfter(self.message_field.SetFocus)
                return  # do NOT Skip — blocks the Esc → close_conversation accelerator
            if wx.Window.FindFocus() is self._mention_list and kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
                idx = self._mention_list.GetSelection()
                if 0 <= idx < len(self._mention_suggestions):
                    name, jid = self._mention_suggestions[idx]
                    self._insert_mention(name, jid)
                return  # do NOT Skip
        if not self._should_redirect_char_to_message(event):
            event.Skip()
            return

        key_code = event.GetUnicodeKey()
        # EVT_CHAR_HOOK reports the raw (unshifted-case) key code for letters,
        # so A-Z always arrives uppercase regardless of Shift/Caps Lock — apply
        # the correct case ourselves instead of trusting the hook's casing.
        if ord("A") <= key_code <= ord("Z"):
            caps_on = wx.GetKeyState(wx.WXK_CAPITAL)
            upper = event.ShiftDown() != caps_on
            char = chr(key_code) if upper else chr(key_code).lower()
        else:
            char = chr(key_code)
        self.message_field.SetFocus()
        self.message_field.WriteText(char)

    def _should_redirect_char_to_message(self, event) -> bool:
        if self.conversation is None or not self.conversation_panel.IsShown():
            return False
        if not self.message_field.IsShown() or not self.message_field.IsEnabled():
            return False
        if self._is_recording:
            return False
        if event.ControlDown() or event.AltDown():
            return False
        if hasattr(event, "MetaDown") and event.MetaDown():
            return False

        key = event.GetUnicodeKey()
        if key == wx.WXK_NONE:
            return False
        if self._is_phantom_nvda_char(event):
            # chr(0xFF).isalnum() is True in Python, so the alnum check
            # below would otherwise wave this straight through — see
            # _is_phantom_nvda_char()'s docstring.
            return False
        try:
            # Only redirect alphanumeric characters — this prevents special
            # keys like Delete (127), Backspace (8), and other control/function
            # characters from being swallowed and written into the message field.
            if not chr(key).isalnum():
                return False
        except (ValueError, OverflowError):
            return False

        focus = wx.Window.FindFocus()
        if focus is self.message_field or isinstance(focus, wx.TextCtrl):
            return False

        return True

    def refresh_labels(self):
        """Update all translatable labels and column headers after a language change."""
        i18n = self.main_window.i18n

        self.conversations_label.SetLabel(i18n.t("conversations"))
        col = wx.ListItem()
        col.SetText(i18n.t("conversations"))
        self.conversations_list.SetColumn(0, col)
        self.search_label.SetLabel(i18n.t("search_conversations"))

        if hasattr(self, "_filter_radio"):
            self._filter_radio.SetLabel(i18n.t("conv_filter_label"))
            for _fi, _fk in enumerate([
                "conv_filter_all", "conv_filter_unread",
                "conv_filter_groups", "conv_filter_individual",
            ]):
                self._filter_radio.SetItemLabel(_fi, i18n.t(_fk))

        self._new_conv_btn.SetLabel(i18n.t("new_conversation"))
        self._search_open_btn.SetLabel(i18n.t("search_in_conv"))
        self._search_close_btn.SetLabel(i18n.t("search_close"))
        self._search_field_label.SetLabel(i18n.t("search_in_conv"))
        self._search_prev_btn.SetLabel(i18n.t("search_prev_result"))
        self._search_next_btn.SetLabel(i18n.t("search_next_result"))

        self.messages_label.SetLabel(i18n.t("messages"))
        col2 = wx.ListItem()
        col2.SetText(i18n.t("messages").replace("&", ""))
        for control in getattr(self, "_message_list_controls", {"active": self.messages_list}).values():
            control.SetColumn(0, col2)
        for accessible in getattr(self, "_messages_list_accessibles", {}).values():
            accessible._label = i18n.t("messages").replace("&", "")

        self.audio_progress_label.SetLabel(i18n.t("audio_progress_label"))
        self._action_save_as_btn.SetLabel(i18n.t("save_as"))
        self._action_download_btn.SetLabel(i18n.t("download"))

        if self.conversation is not None and self.conversation_panel.IsShown():
            if self.conversation_name:
                self.message_label.SetLabel(
                    f"{i18n.t('type_message')} {self.conversation_name}"
                )
            else:
                self.message_label.SetLabel(i18n.t("type_message"))
        else:
            self.message_label.SetLabel(i18n.t("type_message"))

        self.send_message_btn.SetLabel(i18n.t("send_message"))
        self._emoji_btn.SetLabel(i18n.t("emoji_button"))
        self._cancel_edit_btn.SetLabel(i18n.t("cancel_edit"))
        if hasattr(self, "_remove_quote_btn"):
            self._remove_quote_btn.SetLabel(i18n.t("remove_quote"))
        self.record_voice_message_btn.SetLabel(i18n.t("record_voice_message"))
        self._add_attachment_btn.SetLabel(i18n.t("add_attachment"))
        self._add_more_btn.SetLabel(i18n.t("add_more_files"))
        self._caption_label.SetLabel(i18n.t("attachment_caption_hint"))
        self._send_attachment_btn.SetLabel(i18n.t("send_attachment"))
        self._contact_converse_btn.SetLabel(i18n.t("converse"))
        self._contact_save_btn.SetLabel(i18n.t("save_contact"))
        self._discard_voice_btn.SetLabel(i18n.t("discard_voice_message"))
        self._send_voice_btn.SetLabel(i18n.t("send_voice_message"))
        if self._is_recording and self._recording_paused:
            self._pause_resume_btn.SetLabel(i18n.t("resume_recording"))
        else:
            self._pause_resume_btn.SetLabel(i18n.t("pause_recording"))
        self._play_recorded_btn.SetLabel(
            i18n.t("stop_recorded_audio_playback") if self._recorded_audio_sound is not None
            else i18n.t("play_recorded_audio")
        )
        # Update conv-data button label
        if self.conversation is not None:
            jid = self.conversation.get("remoteJid", "")
            self._conv_data_btn.SetLabel(
                i18n.t("group_data") if jid.endswith("@g.us")
                else i18n.t("conversation_data")
            )

    def on_record_voice_message(self, event):
        """
        Ctrl+R / button handler.
        • When NOT recording → start a new voice recording.
        • When recording is active → send the recorded audio (same shortcut).
        """
        if self._is_recording:
            self._send_voice_message(event)
        elif not self._recording_starting:
            self._start_voice_recording()

    # ── Text message sending ─────────────────────────────────────────────────

    def on_send_message(self, event):
        """Send button handler: enqueue message, add to UI immediately as pending.
        If in edit mode, instead calls the edit API and updates the existing message."""
        if self.conversation is None:
            return
        text = normalize_line_separators(self.message_field.GetValue()).strip()
        if not text:
            return
        remote_jid = self.conversation.get("remoteJid", "")
        if not remote_jid:
            return

        # Guard against a single user action enqueueing the same message
        # twice — e.g. Enter's key-repeat firing EVT_TEXT_ENTER more than
        # once for what felt like one press, or a stray duplicate BUTTON/
        # TEXT_ENTER event. Each duplicate created its own pending message
        # and both went through independently, so the "sent" sound played
        # twice and the recipient got the text twice.
        now = time.monotonic()
        last = getattr(self, "_last_sent_signature", None)
        if last is not None:
            last_text, last_jid, last_time = last
            if last_text == text and last_jid == remote_jid and (now - last_time) < 1.5:
                return
        self._last_sent_signature = (text, remote_jid, now)

        # ── Edit mode: update existing message ──────────────────────────────
        if self._editing_message_id is not None:
            self._apply_message_edit(text, remote_jid)
            return

        # ── Normal send ──────────────────────────────────────────────────────
        self._send_new_text_message(text, remote_jid)

    def _apply_message_edit(self, text: str, remote_jid: str):
        """Apply an edit to a message already sent: update it locally now and
        tell WhatsApp about it on a worker thread.

        Split out of on_send_message() so it can be tested without a live wx
        panel, and so the server call is visibly off the UI thread.
        """
        msg_id = self._editing_message_id

        # An edit goes through exactly the same @mention pipeline as a new
        # send. It used to skip it entirely: the raw "@DisplayName" text was
        # posted verbatim (so WhatsApp highlighted nothing — the mention was
        # only cosmetic) and the local record was rewritten as a plain
        # `conversation`, discarding any contextInfo it had. That is why
        # adding a mention with Alt+E never produced the hyperlinks that
        # lead to the mentioned person's chat, and why editing a message
        # that already had mentions silently dropped them.
        api_text, edit_mentions = self._build_mention_payload(text)

        # Call WPPConnect to update the message — on a worker thread.
        # edit-message drives Puppeteer/WhatsApp Web and routinely takes a
        # second or two to come back (its own timeout is 15s); running it
        # inline here froze the whole window for that long on every edit,
        # the one server-backed message action still doing that. Everything
        # below is the local, optimistic update — the same shape
        # _on_menu_pin_message() uses.
        threading.Thread(
            target=self.main_window.edit_message,
            args=(remote_jid, msg_id, api_text),
            kwargs={"mentioned_jids": edit_mentions},
            daemon=True,
        ).start()

        # Re-locate the message by ID rather than trusting the row index
        # captured when edit mode was entered: a background sync can call
        # populate_messages() at any point while the user is typing,
        # which fully rebuilds _sorted_messages — the old index could by
        # then point at an unrelated row, silently overwriting a
        # different message's local content/cache with the edited text
        # (the server-side edit_message() call above is unaffected, since
        # it addresses the message by ID, not by index — only the local
        # display was at risk).
        idx = next(
            (i for i, m in enumerate(self._sorted_messages)
             if isinstance(m, dict) and m.get("key", {}).get("id") == msg_id),
            -1,
        )

        # Update local state
        if 0 <= idx < len(self._sorted_messages):
            edited = self._sorted_messages[idx]
            if edit_mentions:
                # Same shape the send path builds for a mentioning message,
                # so _get_message_content() rewrites @phone → @DisplayName
                # and _extract_mentions() finds the JIDs for the hyperlinks.
                edited["message"] = {"extendedTextMessage": {"text": api_text}}
                edited["messageType"] = "extendedTextMessage"
                ctx = edited.setdefault("contextInfo", {})
                ctx["mentionedJid"] = edit_mentions
            else:
                edited["message"] = {"conversation": text}
                edited["messageType"] = "conversation"
                # An edit that removed every mention must clear the old list
                # too, or the stale hyperlinks stay on screen forever.
                ctx = edited.get("contextInfo")
                if isinstance(ctx, dict):
                    ctx.pop("mentionedJid", None)
                    ctx.pop("mentionedJidList", None)
            edited["_edited"] = True
            self.messages_list.SetItemText(
                idx, self._render_message_line(edited)
            )
            # _sorted_messages[idx] is the same dict object held in
            # main_window.chats[remote_jid]'s records (populate_messages()
            # builds it from there without copying) — persist it so the
            # "Editada" marker and new text survive a restart.
            self.main_window._schedule_save(dirty_jid=remote_jid)
            # Refresh the conversations list too — _last_msg_preview()
            # reads straight from these records, but nothing tells the
            # list widget to redraw the row on its own. Without this the
            # preview kept showing the pre-edit text until the
            # conversation was closed (which rebuilds the list from
            # scratch for an unrelated reason) — see the remote-edit
            # path (_apply_possible_edit(), main.py), which already
            # does this and never had the bug.
            self.main_window._schedule_set_chats()
            # Rebuild the links/mentions panels if the edited row is the one
            # currently focused — they are only refreshed on a focus change,
            # so without this the panels below the list keep describing the
            # message as it was before the edit.
            if self.messages_list.GetFocusedItem() == idx:
                self._update_links_panel(
                    self._extract_links(self._render_message_line(edited))
                )
                self._update_mentions_panel(self._extract_mentions(edited))

        self._on_cancel_edit()

    def _send_new_text_message(self, text: str, remote_jid: str):
        """Queue a brand-new text message and show it as pending right away.

        The other half of on_send_message(), split out alongside
        _apply_message_edit() so neither branch hides inside the other.
        """
        # Build a virtual message dict that renders identically to real messages.
        local_id = str(uuid.uuid4())
        api_text, _mentioned = self._build_mention_payload(text)
        link_preview = self._pending_link_preview

        # When mentions or a resolved link preview are present, use
        # extendedTextMessage: the rendering pipeline needs it either way,
        # for @phone → @DisplayName resolution and for
        # _get_message_content()'s title/description rendering respectively.
        if _mentioned or link_preview:
            _ext = {"text": api_text}
            if link_preview:
                _ext["title"] = link_preview.get("title", "")
                _ext["description"] = link_preview.get("description", "")
                _ext["canonicalUrl"] = link_preview.get("canonicalUrl", "")
            _msg_type  = "extendedTextMessage"
            _msg_body  = {"extendedTextMessage": _ext}
        else:
            _msg_type  = "conversation"
            _msg_body  = {"conversation": text}

        virtual_msg = {
            "_local_pending": True,
            "_local_id":      local_id,
            "key": {
                "id":       local_id,
                "fromMe":   True,
                "remoteJid": remote_jid,
            },
            "messageType":      _msg_type,
            "message":          _msg_body,
            "messageTimestamp": int(time.time()),
            "pushName":         "",
        }
        if self._quoted_message:
            _qk = self._quoted_message.get("key", {})
            virtual_msg["contextInfo"] = {
                "stanzaId":      _qk.get("id", ""),
                "participant":   _qk.get("participant", ""),
                "quotedMessage": self._quoted_message.get("message") or {},
                "_quotedFromMe": bool(_qk.get("fromMe", False)),  # local hint for immediate render
            }
        if _mentioned:
            virtual_msg.setdefault("contextInfo", {})["mentionedJid"] = _mentioned

        # Add to sorted list and UI list immediately.
        self._clear_empty_placeholder()
        self._sorted_messages.append(virtual_msg)
        self.messages_list.Append((self._render_message_line(virtual_msg),))
        # Scroll to the new item.
        last = self.messages_list.GetItemCount() - 1
        if last >= 0:
            self.messages_list.EnsureVisible(last)

        # Clear any pending @mentions before clearing the field.
        self._pending_mentions.clear()
        self._pending_mention_display_names.clear()
        self._hide_mention_suggestions()
        self._rebuild_mention_pills()

        # Clear the text field (this also hides send btn, shows record btn).
        self.message_field.SetValue("")
        self.message_field.SetFocus()

        # Enqueue for background sending (with retry on failure).
        pm = PendingMessage(
            local_id, remote_jid, text=api_text,
            quoted=self._quoted_message,
            mentioned_jids=_mentioned,
            link_preview=link_preview,
        )
        self.main_window.message_queue.enqueue(pm)
        self._on_cancel_reply()  # clear quoted state after send
        self._link_preview_dismissed_url = ""  # fresh field, nothing dismissed yet
        self._clear_link_preview()
        # Replying is a clear signal the conversation has been read — clears
        # the unread badge/title/tray count and notifies WPPConnect, even in
        # the edge case where unreadCount is still nonzero for the chat
        # that's open right now (e.g. the window was minimized when a
        # message arrived, so the open-conversation suppression in
        # on_new_message() never applied).
        self.main_window.mark_conversation_as_read(remote_jid)

        # Register the virtual message in chat records so the conversation
        # list preview updates immediately to show the sent message.
        self._register_virtual_msg(virtual_msg)
        self.main_window._schedule_set_chats()

    def _build_mention_payload(self, text: str):
        """Turn the composed text + pending @mentions into what the API needs.

        Returns ``(api_text, mentioned_jids_or_None)``:

        * WhatsApp only highlights a mention when the message body contains
          ``@{phonenumber}``, never ``@{display_name}`` — so each inserted
          ``@DisplayName`` is swapped back to ``@phone`` here.
        * The JID list is canonicalised (``@lid`` → phone) because that is the
          form the send/edit endpoints tag against.

        Shared by the normal-send and the edit paths so an edit can never again
        end up posting a mention WhatsApp does not recognise.
        """
        raw_mentions = list(self._pending_mentions) if self._pending_mentions else []
        if not raw_mentions:
            return text, None

        mentioned = raw_mentions
        if hasattr(self.main_window, "_canonical_mention_jids"):
            mentioned = self.main_window._canonical_mention_jids(raw_mentions)

        api_text = text
        _normalize = getattr(self.main_window, "_normalize_jid", lambda j: j)
        _lid_map   = getattr(self.main_window, "_lid_to_phone", {})
        for raw_jid in raw_mentions:
            display = self._pending_mention_display_names.get(raw_jid, "")
            if not display:
                continue
            if raw_jid.endswith("@lid"):
                phone = _lid_map.get(raw_jid, raw_jid).split("@")[0]
            else:
                phone = _normalize(raw_jid).split("@")[0]
            if phone and f"@{display}" in api_text:
                api_text = api_text.replace(f"@{display}", f"@{phone}", 1)

        return api_text, (mentioned or None)

    def _register_virtual_msg(self, virtual_msg: dict):
        """
        Add a just-sent virtual message to the chat's records dict so that
        _last_msg_preview() can pick it up and set_chats() shows the correct
        preview in the conversation list.

        Because virtual_msg is the *same* Python dict object that sits in
        _sorted_messages, clearing _local_pending later (in _mark_message_sent)
        automatically updates the records entry too.
        """
        if self._unread_sep_idx >= 0:
            self._dismiss_unread_separator()
        # Fora do if de propósito: _dismiss_unread_separator() era o único
        # ponto do caminho de envio que largava a âncora, e ela deixou de ser
        # volátil. Com _unread_sep_idx == -1 e a âncora ainda gravada —
        # alcançável quando _place_unread_separator_for_rebuild() não a
        # encontra num records transitoriamente vazio, ou depois de um
        # _recompute_unread_sep_idx() que não achou a linha — o envio não
        # limpava nada e o rebuild seguinte RESSUSCITAVA o separador acima da
        # mensagem que o usuário acabou de mandar. Enviar apaga o separador,
        # sempre; é também o que mantém o Alt+2 pousando na mensagem certa.
        self._first_unread_msg_id = None
        self._first_unread_count = 0
        remote_jid = virtual_msg.get("key", {}).get("remoteJid", "")
        if not remote_jid:
            return
        chat = self.main_window.get_chat(remote_jid)
        if chat is None:
            return
        records = (
            chat.setdefault("messages", {})
                .setdefault("messages", {})
                .setdefault("records", [])
        )
        local_id = virtual_msg.get("_local_id", "")
        if local_id:
            self._outgoing_virtual_messages[local_id] = virtual_msg
        if local_id and any(r.get("_local_id") == local_id for r in records):
            return  # already registered
        records.append(virtual_msg)
        
        # Update chat timestamp (t) so the sending chat floats to the top immediately
        msg_ts = int(virtual_msg.get("messageTimestamp", 0) or time.time())
        if msg_ts > 1_000_000_000_000:
            msg_ts //= 1000
        current_t = int(chat.get("t", 0) or 0)
        if current_t > 1_000_000_000_000:
            current_t //= 1000
        if msg_ts > current_t:
            chat["t"] = msg_ts

    def _mark_message_sent(self, local_id: str, real_id: str = None, quote_lost: bool = False):
        """
        Called on the main thread when a queued message is successfully delivered.
        Clears the _local_pending flag, refreshes the list item, plays the
        message-sent sound, and refreshes the conversation list preview.
        real_id (the WhatsApp message ID returned by the API) replaces the local
        UUID in the virtual message's key so that media playback can later look
        up the message in the WPPConnect API database.
        quote_lost=True means the quoted send failed server-side and the message
        went out as a plain send (send_text_message's fallback): the virtual
        message's reply contextInfo is dropped so the row stops reading as a
        reply — the quote never actually reached the recipient.

        A missing/non-string real_id means the send itself succeeded (this is
        only ever called after one did) but its real WhatsApp id couldn't be
        parsed out of the API response. Finalising the row here regardless
        used to strand it permanently: on_new_message()'s later echo match
        only ever considers rows still marked pending, so this call was the
        one and only chance a message like that got to be linked to its real
        id — every one after it landed as a brand new, separately-stored
        duplicate instead of resolving the original. Returning without
        touching the row leaves it pending, so the echo (which always does
        carry the real id) resolves it via a second call to this same method,
        exactly as if this inconclusive one had never happened.
        """
        # The transfer itself is over even when the id is not knowable — the
        # send succeeded, only its response was unparseable. So the gauge and
        # the "this row has a transfer in progress" marker come down either
        # way; leaving them up strands a finished upload on screen, and
        # _sync_pending_document_gauge() (which keys off _media_transfer_started
        # plus _local_pending) re-shows it every time the row is selected.
        self._hide_media_transfer_gauge()
        self._media_transfer_started.discard(local_id)
        if not (real_id and isinstance(real_id, str)):
            # Pin the row at 100% rather than popping the entry: the row stays
            # pending on purpose (see above), and _render_message_line's
            # pending clause falls back to .get(local_id, 0.0) — popping would
            # make a just-finished upload announce as ", enviando 0%".
            if local_id in self._media_upload_progress:
                self._media_upload_progress[local_id] = 1.0
            return
        tracked = self._outgoing_virtual_messages.pop(local_id, None)
        if tracked is not None:
            tracked["_local_pending"] = False
            if real_id and isinstance(real_id, str):
                tracked.setdefault("key", {})["id"] = real_id
        self._media_upload_progress.pop(local_id, None)
        # Panel-level guard: survive _sorted_messages rebuilds that replace dict
        # objects, keeping the per-dict _ui_sent flag from being seen by both callers.
        _played = getattr(self, "_played_sent_local_ids", None)
        if _played is None:
            self._played_sent_local_ids: set = set()
            _played = self._played_sent_local_ids
        if local_id in _played:
            return
        _played.add(local_id)
        if len(_played) > 500:
            _played.clear()

        for i, msg in enumerate(self._sorted_messages):
            if msg.get("_local_id") == local_id:
                if msg.get("_ui_sent"):
                    return  # Already marked sent on the UI, ignore to prevent duplicate sound and actions
                msg["_ui_sent"] = True
                msg["_local_pending"] = False
                # The quoted send failed and the message went out as a plain
                # send: drop the reply contextInfo so the row no longer reads
                # as "respondendo a …". The quote never reached the recipient.
                if quote_lost:
                    msg.pop("contextInfo", None)
                # Replace the local UUID with the real WhatsApp message ID so
                # get_base64_from_media can find the message in the DB later.
                if real_id and isinstance(real_id, str):
                    msg.setdefault("key", {})["id"] = real_id
                    # Rename the local audio file (voice_messages/<id>.msv) and
                    # the pre-cached attachment (media/<id>.wzmedia, written by
                    # _pre_cache_sent_media()) onto the real id, so playback and
                    # Open/Save As find them without a redundant download.
                    # Kept inside a catch-all, as the two inline blocks this
                    # replaced were: a failure to resolve the data dir here must
                    # never stop the row from being marked as sent.
                    try:
                        promote_local_media_cache(
                            data_path("voice_messages"), data_path("media"),
                            local_id, real_id,
                        )
                    except Exception:
                        logging.warning(
                            "[_mark_message_sent] failed to rename the local "
                            "copies of %s", local_id, exc_info=True,
                        )
                    if getattr(self, "_current_audio_id", None) == local_id:
                        self._current_audio_id = real_id
                    if hasattr(self, "_audio_positions") and local_id in self._audio_positions:
                        self._audio_positions[real_id] = self._audio_positions.pop(local_id)
                    # For audio messages, kick off background download now that
                    # we have the real ID the WPPConnect API can look up.
                    if msg.get("messageType") == "audioMessage":
                        import threading as _threading
                        _threading.Thread(
                            target=self.main_window.sync_if_media,
                            args=(msg,),
                            daemon=True,
                        ).start()
                self.messages_list.SetItemText(i, self._render_message_line(msg))
                # Play sent sound — fires only when the originating conversation
                # is still the active one (otherwise local_id is not found here).
                # Recorded voice messages are excluded: their sound is played by
                # _on_message_sent at API-confirmation time, guaranteeing it
                # fires even if the user navigated away during the upload. An
                # audio FILE sent via the attachment picker is also messageType
                # "audioMessage" but never goes through that recording-specific
                # path (it has no audio_path, only media_path) — excluding it
                # here too meant it never got a sent sound from anywhere.
                if hasattr(self.main_window, "message_sent_sound"):
                    if not msg.get("_is_voice_recording"):
                        self.main_window.message_sent_sound.play()
                if self.conversation:
                    self.main_window._schedule_save(dirty_jid=self.conversation.get("remoteJid"))
                break
        # Refresh conversation list so the preview reflects the sent message.
        self.main_window._schedule_set_chats()


    def _mark_message_failed(self, local_id: str):
        """Mark a virtual pending message as permanently failed (exhausted retries)."""
        self._hide_media_transfer_gauge()
        self._outgoing_virtual_messages.pop(local_id, None)
        for i, msg in enumerate(self._sorted_messages):
            if msg.get("_local_id") == local_id:
                msg["_local_pending"] = False
                msg["_send_failed"]   = True
                self.messages_list.SetItemText(i, self._render_message_line(msg))
                if self.conversation:
                    self.main_window._schedule_save(dirty_jid=self.conversation.get("remoteJid"))
                break

    def _mark_message_unconfirmed(self, local_id: str):
        """Mark a virtual message whose send timed out with an unknown outcome.

        Deliberately not "failed": WhatsApp Web may still flush it from its own
        outbox, and if it does the WebSocket echo replaces this bubble with the
        real message. Until then the row must not claim to be sent — it was left
        as "sending" forever before, which reads as success once the spinner
        stops meaning anything.
        """
        self._hide_media_transfer_gauge()
        self._outgoing_virtual_messages.pop(local_id, None)
        for i, msg in enumerate(self._sorted_messages):
            if msg.get("_local_id") == local_id:
                msg["_local_pending"]     = False
                msg["_send_unconfirmed"]  = True
                self.messages_list.SetItemText(i, self._render_message_line(msg))
                try:
                    self.messages_list.RefreshItem(i)
                except Exception:
                    pass
                if self.conversation:
                    self.main_window._schedule_save(dirty_jid=self.conversation.get("remoteJid"))
                break

    def _remember_cancelled_pending(self, local_id: str, msg: dict):
        """Stash the virtual message of a row deleted while it was still pending."""
        if not local_id or not isinstance(msg, dict):
            return
        self._cancelled_pending_messages[local_id] = msg
        # Most cancellations really do stop the send, and then nothing ever comes
        # back to clear the entry — bound the map instead of growing it for a
        # whole session.
        while len(self._cancelled_pending_messages) > 50:
            self._cancelled_pending_messages.pop(
                next(iter(self._cancelled_pending_messages))
            )

    def _is_cancelled_pending(self, local_id: str) -> bool:
        """True while a cancelled message is still waiting to find out whether
        its send reached WhatsApp anyway.  Read by MainWindow._on_message_sent()
        to route a send that outran its own cancellation.
        """
        return bool(local_id) and local_id in self._cancelled_pending_messages

    def discard_cancelled_message(self, local_id: str):
        """Drop a cancelled message for good — the queue confirmed it never went.

        Releases the record _cancel_pending_message() was holding as the echo's
        anchor. Nothing is announced: this is simply the cancellation the user
        asked for, having worked.
        """
        msg = self._cancelled_pending_messages.pop(local_id, None)
        self._forget_cancelled_record(local_id, msg)
        discard_local_media_cache(
            data_path("voice_messages"), data_path("media"), local_id
        )

    def complete_cancelled_message_delivery(self, local_id: str, real_id: str,
                                            remote_jid: str = "",
                                            quote_lost: bool = False,
                                            ambiguous: bool = False):
        """Finish cancelling a message that reached WhatsApp before the cancel did.

        The row is already gone from the list and the message's own record is
        still standing in for it (see _cancel_pending_message), so the echo
        cannot be mistaken for anything else. What is left is to make the
        recipient's copy match what the user asked for: revoke it. A revoke that
        fails is NOT swallowed — the row is restored instead, because a
        delivered message the app pretends to have cancelled is worse than a
        cancellation that visibly failed.

        `ambiguous` is a third outcome, not a flavour of the other two: a
        timeout leaves no ID to revoke AND no promise that anything went out.
        """
        msg = self._cancelled_pending_messages.get(local_id)
        jid = remote_jid or (msg or {}).get("key", {}).get("remoteJid", "")
        if not real_id:
            # Two different things arrive here, told apart by `ambiguous`:
            #   * the send answered {"ok": True} with no ID (main.py's "ID not
            #     found in response", and the quote fallbacks) — it definitely
            #     went out, so the echo is coming and the row goes back exactly
            #     as it was, still pending, for that echo to claim;
            #   * the send timed out — it may never have gone out at all, so the
            #     row must NOT stay a pending anchor.
            # Either way there is nothing to revoke.
            logging.warning(
                "[conversations] cancelled %s was delivered without a real ID "
                "(ambiguous=%s)", local_id, ambiguous,
            )
            self._restore_cancelled_message(local_id, "", quote_lost, ambiguous)
            return

        msg_key = dict((msg or {}).get("key") or {})
        msg_key.update({"remoteJid": jid, "fromMe": True, "id": real_id})

        def _revoke(k=msg_key, j=jid, lid=local_id, rid=real_id, ql=quote_lost):
            try:
                ok = self.main_window.delete_message_for_everyone(j, k)
            except Exception:
                logging.exception(
                    "[conversations] revoking cancelled message %s raised", lid
                )
                ok = False
            wx.CallAfter(
                self._finish_cancelled_message_delivery, lid, rid, bool(ok), ql
            )
        threading.Thread(target=_revoke, daemon=True).start()

    def _finish_cancelled_message_delivery(self, local_id: str, real_id: str,
                                           revoked: bool, quote_lost: bool = False):
        """Announce how the revoke of a cancelled-but-delivered message went."""
        if not revoked:
            # A revoke is only ever attempted with a real ID, so this restore is
            # never the ambiguous one.
            self._restore_cancelled_message(local_id, real_id, quote_lost)
            return
        msg = self._cancelled_pending_messages.pop(local_id, None)
        # The echo may have already claimed the record and given it the real ID,
        # so drop both spellings of it.
        self._forget_cancelled_record(local_id, msg, real_id)
        discard_local_media_cache(
            data_path("voice_messages"), data_path("media"), local_id
        )
        self.main_window.output(
            self.main_window.i18n.t("cancelled_message_revoked"), interrupt=False
        )

    def _forget_cancelled_record(self, local_id: str, msg: dict, real_id: str = ""):
        """Remove a held cancelled record from its chat and from the DB."""
        remote_jid = (msg or {}).get("key", {}).get("remoteJid", "")
        if not remote_jid:
            return   # nothing was being held for this message
        chat = self.main_window.get_chat(remote_jid)
        if chat is not None:
            # setdefault on the way in as well as out: _cancel_pending_message()
            # returns early without ever creating these keys when the record is
            # not in the chat, and reading with .get() while writing with [] then
            # raises KeyError on a chat that has no messages block at all.
            records = (
                chat.setdefault("messages", {})
                    .setdefault("messages", {})
                    .setdefault("records", [])
            )
            chat["messages"]["messages"]["records"] = [
                r for r in records if r.get("_local_id") != local_id
            ]
        for msg_id in {local_id, real_id} - {""}:
            try:
                self.main_window.db.delete_message(remote_jid, msg_id)
            except Exception:
                logging.exception(
                    "[conversations] delete_message failed for %s", msg_id
                )
        self.main_window._recompute_chat_last_message(remote_jid)
        self.main_window._schedule_set_chats()

    def _restore_cancelled_message(self, local_id: str, real_id: str,
                                   quote_lost: bool = False,
                                   ambiguous: bool = False):
        """Put back a row whose cancellation could not be completed.

        The message is on the recipient's phone and could not be revoked, so it
        goes back into the conversation as the ordinary sent message it actually
        is — under its real ID, which is what makes a later retry of "delete for
        everyone", a quote or a delivery-status update land on it.

        With no real ID the row can come back in one of two states, and the
        difference matters more than it looks: a send that reported success
        still has an echo coming, so it stays pending for that echo to claim,
        while a send that timed out may never produce one — and a row left
        pending forever is an anchor that the NEXT message's echo matches first
        (on_new_message() takes the first pending record of the type), handing
        this message's row the next message's WhatsApp ID.
        """
        msg = self._cancelled_pending_messages.pop(local_id, None)
        if msg is None:
            # The record is gone (evicted from the stash, or the panel was
            # rebuilt): the row cannot come back, but the user must still not be
            # left believing a delivered message was cancelled.
            logging.error(
                "[conversations] cancelled %s could not be revoked, and its "
                "record is no longer available to restore", local_id,
            )
            self.main_window.output(
                self.main_window.i18n.t(
                    "cancelled_message_unconfirmed" if ambiguous
                    else "cancelled_message_still_sent"
                ),
                interrupt=False,
            )
            return
        msg.pop("_cancelled_awaiting_id", None)
        remote_jid = msg.get("key", {}).get("remoteJid", "")
        if real_id:
            msg["_local_pending"] = False
            msg.setdefault("key", {})["id"] = real_id
            if quote_lost:
                # The quoted send failed server-side and it went out as a plain
                # message: the row must stop reading as a reply, exactly as
                # _mark_message_sent() does for the ordinary path.
                msg.pop("contextInfo", None)
            # The pre-cached copies still sit under the local UUID: rename them
            # so playback/Save As find them instead of downloading again.
            # Guarded like the identical call in _mark_message_sent(): this runs
            # after the stash was already popped, so letting a disk error out of
            # here would abort the restore with no row back and nothing spoken —
            # a re-download is a far smaller loss than a silent disappearance.
            try:
                promote_local_media_cache(
                    data_path("voice_messages"), data_path("media"), local_id, real_id
                )
            except Exception:
                logging.warning("[cancel] could not promote cached media for %s",
                                local_id, exc_info=True)
        elif ambiguous:
            # Exactly what _mark_message_unconfirmed() does for a send that was
            # never cancelled, and for the same reason: an unresolved ambiguous
            # send must stop being a pending anchor. The row reads "not
            # confirmed" rather than "sending", which is also the truth.
            msg["_local_pending"]    = False
            msg["_send_unconfirmed"] = True
        msg_id = msg.get("key", {}).get("id", "")

        chat = self.main_window.get_chat(remote_jid)
        if chat is not None:
            records = (
                chat.setdefault("messages", {})
                    .setdefault("messages", {})
                    .setdefault("records", [])
            )
            if not any(r.get("key", {}).get("id") == msg_id for r in records):
                records.append(msg)
        if real_id:
            try:
                self.main_window.db.insert_message(remote_jid, msg)
            except Exception:
                logging.exception(
                    "[conversations] could not re-store restored message %s", msg_id
                )
        else:
            # Deliberately not persisted without a real ID: the stored copy
            # would be keyed by a local UUID nothing can ever look up again,
            # surviving restarts with no queue left to resolve it. It is in
            # records, so it is visible and can be deleted again for as long as
            # this session lasts; the echo claiming it is what gives it an ID
            # worth storing (and on_new_message() stores it then).
            logging.info(
                "[conversations] restored %s has no real ID — not persisting it",
                local_id,
            )
        # Renders the row again when this conversation is the open one, and is a
        # no-op otherwise — the same path a message sent from a linked device
        # takes, which is exactly what this message now is.
        self.on_incoming_message(remote_jid, msg)
        self.main_window._recompute_chat_last_message(remote_jid)
        self.main_window._schedule_set_chats()
        self.main_window.output(
            self.main_window.i18n.t(
                "cancelled_message_unconfirmed" if ambiguous
                else "cancelled_message_still_sent"
            ),
            interrupt=False,
        )

    # Delivery receipts do not arrive one at a time. When the other side opens
    # the chat, WhatsApp sends a READ receipt for every message we ever sent in
    # it at once — ten in a single second, measured in a real session. Each one
    # used to rewrite its row immediately, and on Windows every SetItemText
    # raises a name-change event on that ListView item: a screen reader reading
    # a message gets interrupted, ten times in a row, for rows the user is not
    # even on. That is the "a fala e cortada e algum item da lista muda"
    # report, and it is the exact flood CLAUDE.md's Freeze/Thaw rule exists to
    # prevent. So the burst is coalesced into one pass on a short timer.
    _STATUS_REPAINT_COALESCE_MS = 120

    def refresh_message_status(self, msg_id: str, status: str):
        """Queue a status-icon repaint for one sent message.

        Never repaints synchronously and never rebuilds the list — see
        _flush_status_repaints() for why the delay exists.
        """
        pending = getattr(self, "_pending_status_repaints", None)
        if pending is None:
            pending = self._pending_status_repaints = set()
        pending.add(msg_id)

        timer = getattr(self, "_status_repaint_timer", None)
        if timer is not None and timer.IsRunning():
            # Still inside the burst — let the running timer pick this up too.
            return
        self._status_repaint_timer = wx.CallLater(
            self._STATUS_REPAINT_COALESCE_MS, self._flush_status_repaints
        )

    def _flush_status_repaints(self):
        """Repaint every row queued since the last flush, as one batch.

        Rows whose rendered text did not actually change are skipped entirely.
        That is not just an optimisation: a no-op SetItemText still raises the
        accessibility event, so writing text identical to what is already there
        interrupts a screen reader for literally no reason. A receipt for a
        message whose row is off the current page, or whose mark did not move,
        is exactly that case.
        """
        self._status_repaint_timer = None
        pending = getattr(self, "_pending_status_repaints", None)
        if not pending:
            return

        # While the audio chain is moving list focus from one voice note to the
        # next, NO row text may be written at all — see
        # _release_chain_held_repaints() for the whole reasoning. Everything
        # queued in that window is held and written once the chain is over.
        if getattr(self, "_hold_status_repaints_for_chain", False):
            held = getattr(self, "_chain_held_status_repaints", None)
            if held is None:
                held = self._chain_held_status_repaints = set()
            held.update(pending)
            pending.clear()
            return

        ids = set(pending)
        pending.clear()

        rows = []
        for i, msg in enumerate(self._sorted_messages):
            if self._is_separator(msg):
                continue
            if msg.get("key", {}).get("id") in ids:
                rows.append((i, msg))
        if not rows:
            return

        # NOTE: MessageUpdate was already appended by on_message_status_update
        # in main.py before this method is called. Do NOT append again here or
        # the status history grows with duplicates on every update.
        self.messages_list.Freeze()
        try:
            for i, msg in rows:
                line = self._render_message_line(msg)
                try:
                    if self.messages_list.GetItemText(i) == line:
                        continue
                except Exception:
                    pass
                self.messages_list.SetItemText(i, line)
                # RefreshItem ensures the list control repaints this row.
                # Without it, SetItemText updates the internal data but Windows
                # may defer the visual update until the next full paint cycle —
                # making the status icon appear frozen until the user leaves and
                # re-enters the conversation.
                try:
                    self.messages_list.RefreshItem(i)
                except Exception:
                    pass
        finally:
            self.messages_list.Thaw()

    def _hold_status_repaints_until_chain_ends(self):
        """Arm the hold. Called the moment we know the chain is about to move
        list focus off the row we are about to mark as played."""
        self._hold_status_repaints_for_chain = True

    def _release_chain_held_repaints(self):
        """Write out every row repaint held back while the audio chain ran.

        Why the hold exists at all — the rule comes straight from NVDA's own
        source. ``NVDAObject.event_nameChange`` is::

            def event_nameChange(self):
                if self is api.getFocusObject():
                    speech.speakObjectProperties(self, name=True, reason=CHANGE)

        A wx.ListCtrl row is one MSAA object whose *name* is the whole rendered
        line, so rewriting a row to add "reproduzido" raises a name change, and
        NVDA speaks the **entire row** — but only when that row is the object it
        currently believes has focus. So the fix is not to time the write, it is
        to never write the row NVDA is looking at while focus is moving away
        from it.

        The previous protection tried to order the two events instead: move
        focus, then fire the "played" refresh from the same callback,
        documented as "this can't lose the race because both actions run in the
        same callback, in this order". It could, for two measured reasons:

        * ``refresh_message_status()`` does not write anything — it queues the
          row and starts a 120 ms coalescing timer. Measured on the real code
          path, the write landed 142 ms *after* the focus move. That is a
          margin, not a guarantee, and users on several machines with current
          NVDA reported it losing.
        * Worse, that margin can go negative. ``mark_audio_message_played()``
          also POSTs a played receipt to WhatsApp, which echoes the same status
          back over Socket.IO onto ``on_message_status_update()`` with
          ``skip_panel_refresh=False`` — bypassing the chain protection
          entirely. Measured on the real code path: the row was written 95 ms
          *before* the focus move, so NVDA read the whole finished row out and
          only then announced the newly focused one. That is exactly the
          reported symptom, and it is a hole the ordering approach cannot cover
          because the second write does not come through the chain at all.

        Holding removes both: during the chain nothing is written, so there is
        no event to lose a race with. At release time focus sits on the last
        voice note of the sequence while every held row is an earlier one, so
        each name change lands on a non-focused object and NVDA stays silent by
        its own rule — no timing assumption anywhere.

        Idempotent: the hold flag is cleared first, so the several places that
        can end a sequence (the last voice note, the user stopping playback,
        leaving the conversation) may all call this without writing the rows
        twice. It does NOT check whether the chain is still running — call it
        only once the sequence is genuinely over, which is why the call sites
        sit next to where _is_in_audio_chain is cleared.
        """
        if not getattr(self, "_hold_status_repaints_for_chain", False):
            return
        self._hold_status_repaints_for_chain = False
        held = getattr(self, "_chain_held_status_repaints", None)
        if not held:
            return
        self._chain_held_status_repaints = set()
        pending = getattr(self, "_pending_status_repaints", None)
        if pending is None:
            pending = self._pending_status_repaints = set()
        pending.update(held)
        # Straight to the write rather than through refresh_message_status():
        # the chain is over, there is no focus move left to stay clear of, and
        # another 120 ms of coalescing would only leave the rows stale for
        # longer. A timer already in flight is cancelled so it cannot fire a
        # second, empty flush.
        timer = getattr(self, "_status_repaint_timer", None)
        if timer is not None:
            try:
                timer.Stop()
            except Exception:
                pass
            self._status_repaint_timer = None
        self._flush_status_repaints()

    # ── Voice recording ──────────────────────────────────────────────────────

    def _voice_recording_silence_enabled(self):
        """True when Settings > Conteúdo Falado asks for silence while
        recording a voice message.

        Keyed ONLY on that toggle. It used to also fire when
        extended_sr_compat_enabled was OFF — i.e. exactly when the user had
        told WinZapp never to talk to their screen reader, the app started
        interrupting it instead. That switch stops WinZapp's own AO2
        announcements; nothing about it asks for other applications' speech to
        be cut off.
        """
        settings = getattr(self.main_window, "settings", None) or {}
        return bool(
            settings.get("speech_content", {}).get("silence_while_recording", False)
        )

    def _focus_recording_button_silently(self, button):
        """Move focus to one of the voice-recording buttons without the screen
        reader announcing it.

        This is the primary mechanism, and it works by stopping the
        announcement from ever being produced: core.focus_cloak briefly makes
        the control report its MSAA state without STATE_SYSTEM_FOCUSED, which
        NVDA checks (shouldAllowIAccessibleFocusEvent) *before* deciding to
        speak, so the event is discarded rather than spoken and cancelled.

        Cancelling after the fact — what this used to do alone — is a race the
        app loses: the focus WinEvent is delivered synchronously but spoken
        asynchronously on the screen reader's own thread, so the cancel either
        arrives before anything is queued or after speech has already started.
        Users heard the whole "enviar mensagem de voz, botão, Ctrl+R" clipped
        part-way, which for someone recording on air is the exact failure the
        setting exists to prevent. The silence() burst below stays as a
        fallback for anything the cloak cannot reach (a control read over UIA
        rather than MSAA, a platform that does not route WM_GETOBJECT through
        wx), not as the mechanism.

        Whether the button is Enviar or Descartar is the user's own choice in
        Configurações > Interface do usuário; both go through here.
        """
        if self._voice_recording_silence_enabled():
            # Must be armed BEFORE SetFocus(): the state has to already be
            # hiding FOCUSED by the time the screen reader reads it back.
            cloak_focus_announcement(button)
        button.SetFocus()
        self._silence_send_voice_focus_if_enabled()

    def _silence_send_voice_focus_if_enabled(self):
        """Fallback: cancel a focus announcement that was produced anyway.

        Secondary to the cloak in :meth:`_focus_recording_button_silently` —
        see there for why cancelling alone is not enough. The button keeps its
        native accessible name and shortcut at all times; blanking the name out
        was tried and removed, because it stripped the control's identity from
        the accessibility tree for every consumer, not just from the one
        announcement we wanted gone.

        The repeats exist because there is no single right moment: a screen
        reader that speaks synchronously is caught by the immediate call, and
        one that queues on its own thread by a later one. The spacing is
        front-loaded so that if the cloak did fail, what leaks out is a
        syllable rather than a sentence. Each call is idempotent, so the
        repeats are harmless.
        """
        if not self._voice_recording_silence_enabled():
            return
        speak_output = getattr(self.main_window, "speak_output", None)
        silence_focus = getattr(speak_output, "silence_screen_reader_focus", None)
        if not callable(silence_focus):
            return
        # silence() (unlike silence_screen_reader_focus) also reaches the SAPI
        # voice, which is WinZapp's own output when no screen reader is running
        # — cutting it is cutting our own speech, never another app's.
        silence_all = getattr(speak_output, "silence", None)

        def _silence_now():
            silence_focus()
            if callable(silence_all):
                silence_all()

        _silence_now()
        wx.CallAfter(_silence_now)
        for delay_ms in (40, 90, 160, 260, 400):
            wx.CallLater(delay_ms, _silence_now)

    def _start_voice_recording(self):
        """
        Start capturing audio from the default input device.

        Quality strategy (highest to lowest preference):
          48 000 Hz stereo → 48 000 Hz mono → 44 100 Hz stereo → 44 100 Hz mono

        PyAudio delivers raw, unprocessed PCM — no noise suppression,
        no automatic-gain control, no resampling.  This preserves full voice
        naturalness and quality.
        """
        if self.conversation is None:
            return

        if pyaudio is None:
            # No wheel exists for PyAudio on Python 3.14 at the time of
            # writing — see requirements.txt's version marker and this
            # file's own `import pyaudio` — so recording degrades to a
            # clear message instead of crashing on the first pyaudio.*
            # reference below.
            self.main_window.output(self.main_window.i18n.t("voice_recording_unavailable"))
            return

        self._recording_frames = []
        self._recording_paused = False

        # Define callback once, outside the loop; captures self for pause check.
        def _callback(in_data, frame_count, time_info, status):
            # Runs on PyAudio's internal callback thread.
            # list.append is atomic under the GIL — no explicit lock needed.
            if status:
                # paInputOverflow: the capture buffer filled before we drained
                # it (CPU/GIL contention). Logged so choppy recordings are
                # diagnosable; the larger frames_per_buffer below minimises it.
                logging.debug("[audio] input stream status flag: %s", status)
            if not self._recording_paused:
                self._recording_frames.append(in_data)
            pa_cont = getattr(pyaudio, "paContinue", 0) if pyaudio is not None else 0
            return (None, pa_cont)

        # Try each (rate, channels) combination in preference order (shared
        # with core.audio_devices.test_input_device()'s Settings-dialog
        # validation, so a device that validates there is guaranteed to open
        # here too). WhatsApp voice messages are natively 48 kHz Mono.
        # Prioritizing Mono avoids CPU-intensive downmixing loops in pure
        # Python.
        _configs = RECORDING_SAMPLE_CONFIGS
        if self._recording_pa is None and pyaudio is not None:
            try:
                self._recording_pa = pyaudio.PyAudio()
            except Exception as exc:
                logging.error("[audio] Failed to initialize PyAudio: %s", exc)

        if pyaudio is None or (self._recording_pa is None and pyaudio is None):
            try:
                import sounddevice as sd
                def _sd_callback(indata, frames, time_info, status):
                    if not self._recording_paused:
                        self._recording_frames.append(indata.tobytes())
                self._recording_actual_rate = 48000
                self._recording_actual_ch = 1
                self._is_recording = True
                
                # UI updates INSTANTLY (0.01s)
                self.main_window.voicemsg_startrecording_sound.play()
                _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
                if _rec_jid and not _rec_jid.endswith("@newsletter"):
                    self.main_window.send_recording_status(_rec_jid, True, _rec_jid.endswith("@g.us"))
                self.send_message_btn.Hide()
                self.record_voice_message_btn.Hide()
                self._add_attachment_btn.Hide()
                self._pause_resume_btn.SetLabel(self.main_window.i18n.t("pause_recording"))
                self._voice_panel.Show()
                self.conversation_panel.Layout()
                self._focus_recording_button_silently(self._send_voice_btn)

                def _bg_start_mic():
                    try:
                        sd_stream = sd.InputStream(samplerate=48000, channels=1, dtype='int16', callback=_sd_callback)
                        sd_stream.start()
                        self._sd_stream = sd_stream
                        self._recording_stream = sd_stream
                    except Exception as err:
                        logging.error("[audio] Async sounddevice InputStream error: %s", err)
                threading.Thread(target=_bg_start_mic, daemon=True).start()
                return
            except Exception as sd_exc:
                logging.error("[audio] Failed sounddevice fallback recording: %s", sd_exc)
                return

        pa = self._recording_pa

        def _try_open(device_index):
            for rate, ch in _configs:
                try:
                    s = pa.open(
                        rate=rate,
                        channels=ch,
                        format=pyaudio.paInt16,
                        input=True,
                        input_device_index=device_index,
                        # Larger buffer (~85 ms at 48 kHz) so the Python callback
                        # can tolerate scheduling delays from background sync/media
                        # threads without PortAudio dropping samples (choppy audio).
                        frames_per_buffer=4096,
                        stream_callback=_callback,
                    )
                    s.start_stream()
                    return s, rate, ch
                except Exception:
                    continue
            return None, None, None

        # Settings > Audio Devices lets the user pin a specific recording
        # device (by friendly name — indices aren't stable across reboots).
        # main_window.effective_input_device_name is "" whenever no device is
        # configured, or a prior failure this session already fell back to
        # the system default.
        configured_name = getattr(self.main_window, "effective_input_device_name", "") or ""

        # pa.open() (and find_input_device_index()'s device enumeration) can
        # block for many seconds negotiating with the audio driver — this
        # used to run directly on the UI thread and froze the whole app
        # (wx MainLoop, screen reader included) for as long as it took.
        # Do the actual opening on a background thread instead; only the
        # quick, non-blocking UI updates below run back on the main thread.
        self._recording_starting = True
        self._recording_open_token += 1
        my_token = self._recording_open_token

        def _bg_open_stream():
            # Everything here runs off the UI thread, so an exception escaping
            # this function would die silently in a daemon thread — and take
            # the wx.CallAfter below with it. _recording_starting would then
            # stay True forever and on_record_voice_message()'s
            # `elif not self._recording_starting` guard would refuse every
            # further attempt: the record button goes dead for the rest of the
            # session, with nothing on screen or in the log to say why.
            # find_input_device_index() is the realistic raiser —
            # _pyaudio_input_devices() falls back to get_default_host_api_info()
            # unguarded when the WASAPI query fails, which is exactly the kind
            # of broken audio stack this background open exists to survive.
            # _on_stream_opened() is therefore scheduled from a finally: it is
            # the only thing that clears the flag, so it must run either way.
            stream = rate = ch = None
            fell_back = False
            try:
                input_device_index = find_input_device_index(configured_name, pa) if configured_name else None
                stream, rate, ch = _try_open(input_device_index)

                if stream is None and input_device_index is not None:
                    fell_back = True
                    stream, rate, ch = _try_open(None)

                if stream is None:
                    # Last resort, and the reason this branch exists at all:
                    # _try_open(None) asks PortAudio for the default device of
                    # its *default* host API — MME on Windows — which is not
                    # the same handle set enumerate_input_devices() reads
                    # (WASAPI). One host API refusing a microphone says
                    # nothing about the others; see
                    # fallback_input_device_indices() for the observed
                    # disagreement. Giving up here used to mean "no recording
                    # this session" while a working path to the same mic sat
                    # one index away, unexamined.
                    for idx in fallback_input_device_indices(pa, exclude=(input_device_index,)):
                        stream, rate, ch = _try_open(idx)
                        if stream is not None:
                            logging.info(
                                "[audio] Default input device failed; recording via "
                                "enumerated device index %s instead.", idx,
                            )
                            break
            except Exception:
                logging.exception(
                    "[audio] Failed to open the recording stream (device=%r).",
                    configured_name,
                )
            finally:
                wx.CallAfter(_on_stream_opened, stream, rate, ch, fell_back)

        def _on_stream_opened(stream, rate, ch, fell_back):
            # Discard the result if the user cancelled, or switched/closed
            # the conversation, while the stream was still opening.
            if my_token != self._recording_open_token:
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                return

            self._recording_starting = False

            if fell_back:
                # The configured device worked earlier this session (at
                # startup, or since) but just failed to open — e.g.
                # unplugged mid-session. Keep the stored setting untouched
                # (retried again next launch) but fall back to the system
                # default for the rest of this run.
                self.main_window.effective_input_device_name = ""
                if stream is not None:
                    # Only worth saying when the fallback actually worked.
                    # If it didn't, recording never started at all, and the
                    # message below is the accurate thing to report instead
                    # of two dialogs in a row.
                    wx.MessageBox(
                        self.main_window.i18n.t("audio_device_failed_input").format(device=configured_name),
                        self.main_window.i18n.t("error").format(app_name=self.main_window.app_name),
                        wx.OK | wx.ICON_WARNING, self,
                    )

            if stream is None:
                # This used to `return` in silence unless a *configured*
                # device had failed (fell_back above) — and a pinned device
                # is not the default state. On a machine with no device
                # pinned, every open failure was invisible: no dialog, no
                # sound, nothing in log.log. Reported live as "I press
                # record and nothing happens at all", against a mic whose
                # every sample-rate/channel combo PortAudio rejected with
                # -9999. For a screen-reader-first app that is the worst
                # outcome available — there is no visual cue either, so
                # nothing tells the user whether the app, the shortcut or
                # the microphone is at fault. StatusPanel already warned in
                # this exact situation; the two panels disagreed, and the
                # busier one was the silent one.
                logging.warning(
                    "[audio] No input stream could be opened — recording not started."
                )
                wx.MessageBox(
                    self.main_window.i18n.t("voice_recording_device_failed"),
                    self.main_window.i18n.t("error").format(app_name=self.main_window.app_name),
                    wx.OK | wx.ICON_WARNING, self,
                )
                return

            self._recording_stream      = stream
            self._recording_actual_rate = rate
            self._recording_actual_ch   = ch

            self._is_recording = True

            # UI: play sound, swap buttons, focus the configured recording action.
            self.main_window.voicemsg_startrecording_sound.play()

            # Notify contacts that the user is recording audio
            _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
            if _rec_jid and not _rec_jid.endswith("@newsletter"):
                self.main_window.send_recording_status(_rec_jid, True, _rec_jid.endswith("@g.us"))
            self.message_field.Hide()
            if hasattr(self, "_emoji_btn"):
                self._emoji_btn.Hide()
            self.send_message_btn.Hide()
            self.record_voice_message_btn.Hide()
            self._add_attachment_btn.Hide()
            self._pause_resume_btn.SetLabel(
                self.main_window.i18n.t("pause_recording")
            )
            self._voice_panel.Show()
            self.conversation_panel.Layout()
            voice_focus = self.main_window.settings.get("user_interface", {}).get(
                "voice_record_focus", "send"
            )
            if voice_focus == "discard":
                self._focus_recording_button_silently(self._discard_voice_btn)
            else:
                self._focus_recording_button_silently(self._send_voice_btn)

        threading.Thread(target=_bg_open_stream, daemon=True).start()

    def _stop_recording_stream(self):
        """Stop and close the active stream (safe to call when None)."""
        if hasattr(self, "_sd_stream") and self._sd_stream:
            try:
                self._sd_stream.stop()
                self._sd_stream.close()
            except Exception:
                pass
            self._sd_stream = None
        if self._recording_stream is not None:
            try:
                self._recording_stream.stop_stream()
                self._recording_stream.close()
            except Exception:
                pass
            self._recording_stream = None

    def _on_destroy(self, event):
        """Clean up PyAudio resources when the panel is destroyed."""
        if self._recording_pa is not None:
            try:
                self._recording_pa.terminate()
            except Exception:
                pass
            self._recording_pa = None
        if getattr(self, "_video_player", None) is not None:
            self._video_player.stop()
        event.Skip()

    def _hide_voice_panel(self):
        """Hide the voice panel and restore the message field / record /
        send button visibility (sent or discarded — both call this)."""
        self._stop_recorded_audio_preview()
        self._play_recorded_btn.Hide()
        self._voice_panel.Hide()
        self.message_field.Show()
        if hasattr(self, "_emoji_btn"):
            self._emoji_btn.Show()
        if self.message_field.GetValue().strip():
            self.send_message_btn.Show()
        else:
            self.record_voice_message_btn.Show()
        self._add_attachment_btn.Show()
        self.conversation_panel.Layout()

    def _discard_voice_message(self, event):
        """Discard the current recording without sending."""
        if not self._is_recording:
            return
        self.main_window.voicemsg_discard_sound.play()
        threading.Thread(target=self._stop_recording_stream, daemon=True).start()
        self._is_recording     = False
        self._recording_paused = False
        self._recording_frames = []
        # Notify contacts that recording stopped
        _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if _rec_jid and not _rec_jid.endswith("@newsletter"):
            self.main_window.send_recording_status(_rec_jid, False, _rec_jid.endswith("@g.us"))
        self._hide_voice_panel()
        self.message_field.SetFocus()

    def _toggle_pause_recording(self, event):
        """Pause or resume the ongoing recording."""
        if not self._is_recording:
            return
        self.main_window.voicemsg_pauserecording_sound.play()
        self._recording_paused = not self._recording_paused
        label_key = "resume_recording" if self._recording_paused else "pause_recording"
        self._pause_resume_btn.SetLabel(self.main_window.i18n.t(label_key))
        if self._recording_paused:
            self._play_recorded_btn.Show()
        else:
            # Resuming appends new frames again — the paused-audio preview
            # would go on playing a now-stale snapshot, so stop it outright.
            self._stop_recorded_audio_preview()
            self._play_recorded_btn.Hide()
        self.conversation_panel.Layout()
        self._silence_send_voice_focus_if_enabled()

    def _toggle_play_recorded_audio(self, event):
        """Play back everything recorded so far, or stop that playback if
        it's already going. Ctrl+P / the "Reproduzir áudio gravado" button
        next to "Continuar gravação" — only ever reachable while paused,
        both because the button is hidden otherwise and because frames stay
        stable only while paused (the PyAudio callback skips appending while
        self._recording_paused, see on_record_voice_message's _callback)."""
        if not self._is_recording or not self._recording_paused:
            return
        if self._recorded_audio_sound is not None:
            self._stop_recorded_audio_preview()
            return

        frames = self._recording_frames
        if not frames:
            return

        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(self._recording_actual_ch)
                wf.setsampwidth(2)   # 16-bit PCM — matches how _send_voice_message writes it
                wf.setframerate(self._recording_actual_rate)
                wf.writeframes(b"".join(frames))
        except Exception as exc:
            logging.warning("[voice] failed to write recorded-audio preview WAV: %s", exc)
            return
        self._recorded_audio_temp_path = tmp.name

        # A plain sl_stream.FileStream, not the app's Sound/load_sound
        # wrapper: Sound.play() reroutes to Settings > Audio Devices'
        # separate "effects" device when one is configured, which is meant
        # for short UI cue sounds, not the user's own recorded voice. This
        # is exactly how _play_audio() opens a real incoming voice message
        # (sl_stream.FileStream direct) — it just inherits BASS's current
        # process-wide default device, i.e. the configured Output device.
        try:
            snd = sl_stream.FileStream(file=self._recorded_audio_temp_path)
            snd.play()
        except Exception as exc:
            logging.warning("[voice] failed to play recorded-audio preview: %s", exc)
            self._cleanup_recorded_audio_temp_file()
            return
        self._recorded_audio_sound = snd
        self._play_recorded_btn.SetLabel(self.main_window.i18n.t("stop_recorded_audio_playback"))
        self._recorded_audio_timer.Start(300)

    def _on_recorded_audio_timer(self, event):
        """sound_lib has no playback-finished callback (see
        AlertPreviewController's identical polling in core/sound_system.py)
        — reaching the end of the preview must still be a full stop, not
        just leaving the button stuck on "Parar reprodução"."""
        if self._recorded_audio_sound is None or not self._recorded_audio_sound.is_playing:
            self._stop_recorded_audio_preview()

    def _stop_recorded_audio_preview(self):
        """Full stop (not pause) of the recorded-audio preview and reset of
        the button back to "Reproduzir áudio gravado" — called whether
        playback finished on its own, the user clicked "Parar reprodução",
        the recording was resumed, or the voice panel is going away
        (discard/send). Safe to call when nothing is playing."""
        self._recorded_audio_timer.Stop()
        if self._recorded_audio_sound is not None:
            try:
                self._recorded_audio_sound.stop()
            except Exception:
                pass
            self._recorded_audio_sound = None
        self._cleanup_recorded_audio_temp_file()
        if hasattr(self, "_play_recorded_btn"):
            self._play_recorded_btn.SetLabel(self.main_window.i18n.t("play_recorded_audio"))

    def _cleanup_recorded_audio_temp_file(self):
        if self._recorded_audio_temp_path is not None:
            try:
                os.unlink(self._recorded_audio_temp_path)
            except Exception:
                pass
            self._recorded_audio_temp_path = None

    def _send_voice_message(self, event):
        """Stop recording and enqueue the audio for delivery."""
        if not self._is_recording:
            return

        import time as _time
        _t0 = _time.perf_counter()
        logging.info("[VOICE_TIMING] T+0.000s — user clicked send, stopping recording stream")

        # Stop the recording stream in background FIRST so the audio device is fully released
        # without blocking the UI thread before BASS plays the send sound.
        threading.Thread(target=self._stop_recording_stream, daemon=True).start()
        self._is_recording     = False
        self._recording_paused = False

        self.main_window.voicemsg_send_sound.play()

        # Notify contacts that recording stopped (runs in its own thread).
        _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if _rec_jid and not _rec_jid.endswith("@newsletter"):
            self.main_window.send_recording_status(_rec_jid, False, _rec_jid.endswith("@g.us"))

        frames = self._recording_frames
        self._recording_frames = []

        if not frames:
            self._hide_voice_panel()
            self.message_field.SetFocus()
            return

        # ── Phase 2: instant UI update ────────────────────────────────────────
        remote_jid      = self.conversation.get("remoteJid", "")
        local_id        = str(uuid.uuid4())
        actual_rate     = self._recording_actual_rate
        actual_ch       = self._recording_actual_ch
        bytes_per_frame = 2 * actual_ch
        quoted_msg      = self._quoted_message

        # Duration from frame byte counts — no allocation, no join on UI thread.
        total_bytes  = sum(len(f) for f in frames)
        duration_sec = int(total_bytes / bytes_per_frame / actual_rate)

        virtual_msg = {
            "_local_pending": True,
            "_local_id":      local_id,
            # Distinguishes a recorded voice message from an audio file sent
            # via the attachment picker (both are messageType "audioMessage")
            # — _mark_message_sent() needs this to know whether the sent
            # sound was already played over in _on_message_sent() (recorded
            # voice, at API-confirmation time — see that method's comment)
            # or still needs to play here.
            "_is_voice_recording": True,
            "key": {
                "id":        local_id,
                "fromMe":    True,
                "remoteJid": remote_jid,
            },
            "messageType": "audioMessage",
            "message": {
                "audioMessage": {
                    "seconds": duration_sec,
                    "ptt":     True,
                }
            },
            "messageTimestamp": int(time.time()),
            "pushName":         "",
        }
        if quoted_msg:
            _qk = quoted_msg.get("key", {})
            virtual_msg["contextInfo"] = {
                "stanzaId":      _qk.get("id", ""),
                "participant":   _qk.get("participant", ""),
                "quotedMessage": quoted_msg.get("message") or {},
                "_quotedFromMe": bool(_qk.get("fromMe", False)),
            }
        
        self._clear_empty_placeholder()
        self._sorted_messages.append(virtual_msg)
        self.messages_list.Append((self._render_message_line(virtual_msg),))
        last = self.messages_list.GetItemCount() - 1
        if last >= 0:
            self.messages_list.EnsureVisible(last)

        self._register_virtual_msg(virtual_msg)
        self.main_window._schedule_set_chats()
        self._on_cancel_reply()
        self._hide_voice_panel()
        self.message_field.SetFocus()

        # ── Phase 3: heavy work off UI thread ─────────────────────────────────
        # • Join PCM frames
        # • Encode OGG Opus directly from PCM (no WAV roundtrip for encoding)
        # • Write WAV backup for .msv / retry
        # • Encrypt + save .msv local copy
        # • Enqueue with ogg_bytes already ready → worker only needs to POST
        mw      = self.main_window
        enc_key = mw.key

        def _write_and_enqueue():
            import time as _time
            _tw0 = _time.perf_counter()
            logging.info("[VOICE_TIMING] T+%.3fs — _write_and_enqueue thread started",
                         _tw0 - _t0)

            # 1. Join raw PCM frames.
            audio_data = b"".join(frames)
            logging.info("[VOICE_TIMING] T+%.3fs — PCM frames joined (%d bytes, %d frames)",
                         _time.perf_counter() - _t0, len(audio_data), len(frames))

            # Apply microphone noise reduction if enabled in settings
            if mw.settings.get("general", {}).get("noise_reduction_enabled", False):
                try:
                    logging.info("[VOICE_TIMING] Applying microphone noise reduction...")
                    from core.audio_processing import apply_noise_gate
                    audio_data = apply_noise_gate(audio_data, actual_rate, actual_ch)
                    logging.info("[VOICE_TIMING] Noise reduction applied successfully")
                except Exception as ex:
                    logging.error("[VOICE_TIMING] Failed to apply noise reduction: %s", ex)

            # 2. Write WAV temp file (used for ffmpeg conversion, backup, and retry fallback).
            try:
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tmp.close()
                with wave.open(tmp.name, "wb") as wf:
                    wf.setnchannels(actual_ch)
                    wf.setsampwidth(2)   # 16-bit PCM
                    wf.setframerate(actual_rate)
                    wf.writeframes(audio_data)
                wav_path = tmp.name
                logging.info("[VOICE_TIMING] T+%.3fs — WAV written to %s",
                             _time.perf_counter() - _t0, wav_path)
            except Exception as exc:
                logging.error("[_send_voice_message] failed to write WAV: %s", exc)
                return

            # 3. Encode OGG Opus via ffmpeg conversion.
            ogg_bytes = None
            _t_enc = _time.perf_counter()
            try:
                ogg_path = mw._convert_wav_to_ogg(wav_path)
                if ogg_path and os.path.isfile(ogg_path):
                    with open(ogg_path, "rb") as f_in:
                        ogg_bytes = f_in.read()
                    try:
                        os.unlink(ogg_path)
                    except Exception:
                        pass
            except Exception as exc:
                logging.warning("[_send_voice_message] OGG pre-encode failed: %s", exc)
            logging.info("[VOICE_TIMING] T+%.3fs — OGG encode done in %.3fs (%s bytes)",
                         _time.perf_counter() - _t0,
                         _time.perf_counter() - _t_enc,
                         len(ogg_bytes) if ogg_bytes else 0)

            # 4. Encrypt OGG (or raw PCM fallback) and save as .msv for offline playback.
            _t_msv = _time.perf_counter()
            try:
                voice_messages_dir = data_path("voice_messages")
                os.makedirs(voice_messages_dir, exist_ok=True)
                local_audio_path = os.path.join(voice_messages_dir, f"{local_id}.msv")
                with open(local_audio_path, "wb") as f_out:
                    f_out.write(encrypt(ogg_bytes or audio_data, enc_key))
                logging.info("[VOICE_TIMING] T+%.3fs — .msv saved in %.3fs",
                             _time.perf_counter() - _t0,
                             _time.perf_counter() - _t_msv)
            except Exception as exc:
                logging.warning("[_send_voice_message] failed to save local audio copy: %s", exc)

            # 5. Enqueue — ogg_bytes pre-encoded so worker skips encoding, just POSTs.
            logging.info("[VOICE_TIMING] T+%.3fs — calling enqueue (ogg_bytes=%s)",
                         _time.perf_counter() - _t0,
                         "yes" if ogg_bytes else "NO — will fallback to WAV")
            pm = PendingMessage(local_id, remote_jid, audio_path=wav_path,
                                ogg_bytes=ogg_bytes, quoted=quoted_msg)
            mw.message_queue.enqueue(pm)
            mw.mark_conversation_as_read(remote_jid)

        threading.Thread(target=_write_and_enqueue, daemon=True).start()

    def _cancel_active_recording(self):
        """Stop and discard an in-progress voice recording, if any.

        Recording is scoped to whichever conversation was open when it
        started — there is no "background recording" that survives leaving
        the chat, so closing OR switching away from that conversation must
        cancel it the same way, rather than leaving _is_recording true and
        the voice panel visible while main_window.conversation has already
        moved on. Without this on the switch path specifically, pressing
        Enviar afterwards sent the recording to whatever conversation the
        user had since navigated to — not the one it was actually recorded
        in, since _send_voice_message() reads self.conversation at send
        time, not at record-start time.
        """
        # Bump the token so a PyAudio stream still opening on a background
        # thread (see _start_voice_recording) gets closed and discarded
        # instead of surfacing into whatever conversation is open when it
        # finishes.
        self._recording_open_token += 1
        self._recording_starting = False
        if not self._is_recording:
            return
        self._stop_recording_stream()
        self._is_recording     = False
        self._recording_paused = False
        self._recording_frames = []
        _rec_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if _rec_jid and not _rec_jid.endswith("@newsletter"):
            self.main_window.send_recording_status(_rec_jid, False, _rec_jid.endswith("@g.us"))
        self._voice_panel.Hide()
        self.record_voice_message_btn.Show()

    def _close_conversation_core(self) -> "tuple[bool, str]":
        """Stop typing/recording indicators and clear the open-conversation
        state. Returns (closed, closed_jid): closed is False when the
        mention-suggestions popup was showing (that press is handled by
        just dismissing the popup — the conversation itself is left open).

        Shared by close_conversation() (Esc — also restores focus to
        whichever list the conversation was opened from) and
        close_conversation_for_panel_switch() (used when the user
        navigates to an entirely different top-level panel, e.g. Status —
        that panel sets its own focus right after, so it must NOT queue
        close_conversation()'s focus-restoration CallAfters, which would
        otherwise steal focus back to the conversations list a moment
        later).
        """
        if hasattr(self, "_mention_panel") and self._mention_panel.IsShown():
            self._hide_mention_suggestions()
            self.message_field.SetFocus()
            return False, ""
        self._stop_typing_for_current_conversation()
        self._cancel_active_recording()
        self._hide_audio_controls()
        self._hide_all_media_controls()
        self._hide_media_transfer_gauge()
        self._hide_attachment_panel()
        # Clear any active edit state
        if self._editing_message_id is not None:
            self._on_cancel_edit()
        if self._quoted_message is not None:
            self._on_cancel_reply()
        # Clear search state
        self._search_results    = []
        self._search_result_idx = -1
        if hasattr(self, "_search_panel") and self._search_panel.IsShown():
            self._search_panel.Hide()
            self._search_open_btn.Show()
            self._search_field.SetValue("")
        # _msg_bookmarks is intentionally NOT reset here — see __init__.
        # _msg_temp_bookmarks is, for the same reason spelled out there.
        self._msg_temp_bookmarks.clear()
        self._reset_expanded_window()
        closed_jid = self._last_open_jid
        self.conversation = None
        self.conversation_panel.Hide()
        self.Layout()
        return True, closed_jid

    def close_conversation(self, event=None):
        closed, closed_jid = self._close_conversation_core()
        if not closed:
            return  # _close_conversation_core() only handled the mention popup
        mw = self.main_window
        # If the conversation being closed is archived, it was opened from the
        # archived list (ArchivedConversationsPanel), which stays hidden behind
        # this panel while the conversation is open — so Esc must send focus
        # back there instead of the regular conversations list.
        if (closed_jid and mw.is_chat_archived(closed_jid)
                and hasattr(mw, "archived_conversations_panel")):
            wx.CallAfter(self._restore_to_archived_list, closed_jid)
        else:
            # Defer focus restoration so it runs after the accelerator event is
            # fully processed — calling SetFocus() synchronously inside an EVT_MENU
            # handler can be overridden by wx's post-event focus management on Win32.
            wx.CallAfter(self._restore_conversation_selection)

    def close_conversation_for_panel_switch(self):
        """Same cleanup as close_conversation() but without its focus-
        restoration side effects — see _close_conversation_core()."""
        self._close_conversation_core()

    def _restore_conversation_selection(self):
        """Select, focus and give keyboard focus back to where the user last
        was in the chat list.

        That position is ``_last_list_focus_jid`` — the row the focus actually
        sat on — and only falls back to ``_last_open_jid`` when that row is
        gone from the list (deleted, archived, filtered out) or was never
        recorded. Restoring the OPEN conversation instead was the whole of
        issue #91: moving to another chat without opening it, going to the
        messages with Alt+2 and coming back with Alt+1 (or closing the
        conversation with Esc/Ctrl+W) dropped the user back on the open chat
        every time, losing wherever they had navigated to.
        """
        lst = self.conversations_list
        target = 0
        for candidate in (self._last_list_focus_jid, self._last_open_jid):
            if not candidate:
                continue
            found = -1
            for i, chat in enumerate(self.chats_list):
                if chat.get("remoteJid") == candidate:
                    found = i
                    break
            if found >= 0:
                target = found
                break
        if self.chats_list:
            lst.Focus(target)
            lst.Select(target)
            lst.EnsureVisible(target)
        lst.SetFocus()

    def _restore_to_archived_list(self, jid: str):
        """Switch back to the archived conversations list and re-select `jid`."""
        mw = self.main_window
        # Undo the conversations_list/label Hide() from
        # ArchivedConversationsPanel.on_conversation_selected() so this panel
        # is back to its normal split-view state the next time it is shown
        # from the regular "Conversations" nav item.
        self.conversations_label.Show()
        self.conversations_list.Show()
        self.Hide()
        mw.archived_conversations_panel.Show()
        mw.content_panel.Layout()
        arch = mw.archived_conversations_panel
        lst = arch.conversations_list
        target = 0
        for i, chat in enumerate(arch.chats_list):
            if chat.get("remoteJid") == jid:
                target = i
                break
        if arch.chats_list:
            lst.Focus(target)
            lst.Select(target)
            lst.EnsureVisible(target)
        lst.SetFocus()

    # ── Conversations context menu ──────────────────────────────────────────

    def on_conversations_context_menu(self, event):
        selected_index = self.conversations_list.GetFirstSelected()
        if selected_index == -1:
            return
        try:
            chat = self.chats_list[selected_index]
        except IndexError:
            return
        jid      = chat.get("remoteJid", "")
        is_group = jid.endswith("@g.us")
        is_self  = self.main_window._is_self_jid(jid)
        mw       = self.main_window
        i18n     = mw.i18n

        menu = wx.Menu()

        if getattr(self, "selected_chats", None):
            mass_menu = wx.Menu()

            # Each entry carries its own dedicated shortcut (see
            # create_accelerator_table's ID_BULK_*_CHATS) — those work
            # whatever "Substituir atalhos por ações em massa..." is set to,
            # unlike the single-chat shortcuts this submenu's actions used to
            # be reachable through only when that setting was on.
            clear_item = mass_menu.Append(
                wx.ID_ANY, f"{i18n.t('clear_selected_chats')}\tCtrl+Alt+Shift+L")
            self.Bind(wx.EVT_MENU, self._on_mass_clear_chats, clear_item)

            delete_item = mass_menu.Append(
                wx.ID_ANY, f"{i18n.t('delete_selected_chats')}\tCtrl+Shift+Delete")
            self.Bind(wx.EVT_MENU, self._on_mass_delete_chats, delete_item)

            archive_item = mass_menu.Append(
                wx.ID_ANY, f"{i18n.t('archive_selected_chats')}\tCtrl+Alt+Shift+A")
            self.Bind(wx.EVT_MENU, self._on_mass_archive_chats, archive_item)

            read_item = mass_menu.Append(
                wx.ID_ANY, f"{i18n.t('mark_selected_read')}\tCtrl+Alt+Shift+R")
            self.Bind(wx.EVT_MENU, self._on_mass_mark_read_chats, read_item)

            unread_item = mass_menu.Append(
                wx.ID_ANY, f"{i18n.t('mark_selected_unread')}\tCtrl+Alt+Shift+U")
            self.Bind(wx.EVT_MENU, self._on_mass_mark_unread_chats, unread_item)

            menu.AppendSubMenu(mass_menu, i18n.t("mass_actions"))
            menu.AppendSeparator()

        # ── Conversation / group data ─────────────────────────────────────
        data_label = i18n.t("group_data") if is_group else i18n.t("conversation_data")
        data_item = menu.Append(wx.ID_ANY, f"{data_label}\tCtrl+Shift+D")
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=chat: self._show_conversation_data(chat=c),
            data_item,
        )

        menu.AppendSeparator()

        # ── Read / Unread — mutually exclusive: show only the applicable one ──
        has_unread = int(chat.get("unreadCount") or 0) > 0
        if has_unread:
            read_item = menu.Append(wx.ID_ANY, f"{i18n.t('mark_as_read')}\tCtrl+Shift+M")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_mark_read(j), read_item)
        else:
            unread_item = menu.Append(wx.ID_ANY, f"{i18n.t('mark_as_unread')}\tCtrl+Shift+M")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_mark_unread(j), unread_item)

        menu.AppendSeparator()

        # ── Mute ──────────────────────────────────────────────────────────
        if mw.is_chat_muted(jid):
            unmute_item = menu.Append(wx.ID_ANY, f"{i18n.t('unmute_chat')}\tAlt+Shift+S")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_unmute(j), unmute_item)
        else:
            menu.AppendSubMenu(
                self._build_mute_menu(jid), f"{i18n.t('mute_chat')}\tAlt+Shift+S"
            )

        if not is_group:
            menu.AppendSeparator()
            if not is_self:
                is_blocked = mw.is_contact_blocked(jid)
                label = "unblock_contact" if is_blocked else "block_contact"
                block_item = menu.Append(wx.ID_ANY, f"{i18n.t(label)}\tCtrl+Shift+B")
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, c=chat, j=jid, b=is_blocked: self._on_menu_block(c, j, b),
                    block_item,
                )
            copy_num_item = menu.Append(wx.ID_ANY, f"{i18n.t('copy_number')}\tAlt+Shift+C")
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid: self._on_menu_copy_number(j),
                copy_num_item,
            )

        menu.AppendSeparator()

        # ── Archive / Unarchive ───────────────────────────────────────────
        if mw.is_chat_archived(jid):
            ua_item = menu.Append(wx.ID_ANY, f"{i18n.t('unarchive_chat')}\tCtrl+Shift+Q")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_unarchive(j), ua_item)
        else:
            arch_item = menu.Append(wx.ID_ANY, f"{i18n.t('archive_chat')}\tCtrl+Shift+Q")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_archive(j), arch_item)

        # ── Pin / Unpin ───────────────────────────────────────────────────
        if mw.is_chat_pinned(jid):
            unpin_item = menu.Append(wx.ID_ANY, f"{i18n.t('unpin_chat')}\tCtrl+P")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_unpin(j), unpin_item)
        else:
            pin_item = menu.Append(wx.ID_ANY, f"{i18n.t('pin_chat')}\tCtrl+P")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_pin(j), pin_item)

        menu.AppendSeparator()

        # ── Clear / Delete / Leave ────────────────────────────────────────
        clear_item = menu.Append(wx.ID_ANY, f"{i18n.t('clear_chat')}\tCtrl+Shift+L")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_clear_chat(j), clear_item)

        delete_item = menu.Append(wx.ID_ANY, f"{i18n.t('delete_chat')}\tDelete")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_menu_delete_chat(j), delete_item)

        if is_group:
            leave_item = menu.Append(wx.ID_ANY, i18n.t("leave_group"))
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid: self._on_menu_leave_group(j),
                leave_item,
            )
            add_member_item = menu.Append(wx.ID_ANY, i18n.t("add_member"))
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid: self._on_menu_add_member(j),
                add_member_item,
            )

        # "Fechar conversa" only makes sense for the conversation this menu
        # was actually opened on — showing it for every row regardless let
        # the user right-click chat B, pick "Fechar conversa", and have it
        # silently close chat A instead (on_context_menu_close() has no idea
        # which jid the menu was for; it just closes whatever's currently
        # open).
        if (self.conversation and self.conversation.get("remoteJid") == jid
                and self.conversation_panel.IsShown()):
            menu.AppendSeparator()
            close_item = menu.Append(wx.ID_ANY, f"{i18n.t('close_conversation')}\tCtrl+W")
            self.Bind(wx.EVT_MENU, self.on_context_menu_close, close_item)

        self.PopupMenu(menu)
        menu.Destroy()

    def on_context_menu_close(self, event):
        if self.conversation_panel.IsShown():
            self.close_conversation(event)
            return
        # Ctrl+W with nothing open used to do nothing at all, silently — see
        # _no_conversation_open_announced() (issue #86).
        self._no_conversation_open_announced()

    def _no_conversation_open_announced(self) -> bool:
        """Say "no chat is open" and return True when there is none.

        The shortcuts that only make sense inside a conversation (Alt+2, Alt+3,
        Ctrl+W) used to be completely silent with nothing open. For a
        screen-reader user silence is indistinguishable from the shortcut being
        broken — the same reasoning as _run_bulk_chat_action()'s
        "bulk_no_chat_selection" and save_media_message()'s
        "save_as_nothing_to_save".
        """
        if self.conversation is not None:
            return False
        self.main_window.output(
            self.main_window.i18n.t("no_chat_open"), interrupt=True
        )
        return True

    # ── Messages list events ────────────────────────────────────────────────

    def on_message_selected(self, event):
        """Show / hide action controls when the selection changes in the messages list."""
        if getattr(self, "_suppress_selection_side_effects", False):
            return
        index = event.GetIndex()
        self._hide_all_media_controls()   # also clears links panel
        if index < 0 or index >= len(self._sorted_messages):
            return
        if self._is_separator(self._sorted_messages[index]):
            return  # separator row — no action controls
        msg     = self._sorted_messages[index]
        msg_type = msg.get("messageType", "")
        msg_obj  = msg.get("message") or {}
        msg_id   = msg.get("key", {}).get("id", "")
        clean_msg_id = msg_id
        if "_" in msg_id:
            parts = msg_id.split("_")
            clean_msg_id = parts[2] if len(parts) > 2 else parts[-1]
        media_path = data_path("media", f"{clean_msg_id}.wzmedia")
        is_downloaded = os.path.isfile(media_path)

        if msg_type == "documentMessage":
            if is_downloaded:
                self._action_open_btn.SetLabel(self.main_window.i18n.t("open"))
                self._action_open_btn.Show()
                self._action_save_as_btn.Show()
            else:
                self._action_download_btn.Show()
            self.conversation_panel.Layout()

        elif msg_type == "imageMessage":
            jpeg = (msg_obj.get("imageMessage") or {}).get("jpegThumbnail", "")
            self._try_show_thumbnail(jpeg)
            self._action_open_btn.SetLabel(self.main_window.i18n.t("open_image"))
            self._action_open_btn.Show()
            self._action_save_as_btn.Show()
            self.conversation_panel.Layout()

        elif msg_type == "stickerMessage":
            jpeg = (msg_obj.get("stickerMessage") or {}).get("jpegThumbnail", "")
            self._try_show_thumbnail(jpeg)
            # No action buttons for stickers

        elif msg_type == "videoMessage":
            video = msg_obj.get("videoMessage") or {}
            jpeg = video.get("jpegThumbnail", "")
            self._try_show_thumbnail(jpeg)
            if not video.get("gifPlayback"):
                if is_downloaded:
                    self._action_open_btn.SetLabel(self.main_window.i18n.t("open"))
                    self._action_open_btn.Show()
                    self._action_save_as_btn.Show()
                else:
                    self._action_download_btn.Show()
            self.conversation_panel.Layout()

        elif msg_type == "buttonsMessage":
            buttons = (msg_obj.get("buttonsMessage") or {}).get("buttons", [])
            remote_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
            self._show_reply_buttons(buttons, remote_jid)

        elif msg_type == "listMessage":
            sections = (msg_obj.get("listMessage") or {}).get("sections", [])
            rows: list = []
            for sec in sections:
                rows.extend(sec.get("rows", []) if isinstance(sec, dict) else [])
            remote_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
            self._show_list_rows(rows, remote_jid)

        elif msg_type == "contactMessage":
            contact = msg_obj.get("contactMessage") or {}
            vcard = contact.get("vcard", "")
            self._contact_msg_jid = self._jid_from_vcard(vcard)
            if self._contact_msg_jid:
                self._contact_converse_btn.Show()
                self._contact_save_btn.Show()
                self.conversation_panel.Layout()

        elif msg_type in ("locationMessage", "liveLocationMessage"):
            if self._location_maps_url(msg):
                self._action_open_btn.SetLabel(self.main_window.i18n.t("open_location"))
                self._action_open_btn.Show()
                self.conversation_panel.Layout()

        # ── Link detection ────────────────────────────────────────────────
        # Always check the rendered text for URLs (regardless of msg_type).
        # Must use _render_message_line(msg) — the full, untruncated text —
        # not messages_list.GetItemText(index): SysListView32 (the native
        # control wx.ListCtrl wraps) truncates each row's accessible name at
        # _LIST_CTRL_TEXT_LIMIT characters, so a link further into a long
        # message was silently invisible to link detection and never became
        # Tab-focusable, even though the message itself displayed fine (via
        # the "Ler mais" remainder).
        rendered = self._render_message_line(msg)
        self._update_links_panel(self._extract_links(rendered))

        # ── Mention detection ─────────────────────────────────────────────
        self._update_mentions_panel(self._extract_mentions(msg))

        self._sync_media_action_slot_visibility()

    def on_message_activated(self, event):
        """Enter / double-click on a message item."""
        idx = self.messages_list.GetFocusedItem()
        if idx >= 0:
            self._do_activate_message(idx)

    def _do_activate_message(self, index: int):
        """Core activation logic shared by Enter and double-click.

        Space no longer reaches here: it toggles the row's selection for the
        mass actions instead (see _on_messages_list_key_down).
        """
        if index < 0 or index >= len(self._sorted_messages):
            return
        if self._is_separator(self._sorted_messages[index]):
            return  # separator row — no action
        self.activate_message(self._sorted_messages[index], index=index)

    def activate_message(self, msg: dict, index=None):
        """Activate a message (what Enter does), given the message itself.

        Split out of _do_activate_message() because every branch below already
        worked from the message; the index was only ever used to look it up,
        plus restoring list focus after the media viewer closes. That made the
        whole activation path unreachable for a caller holding a message that
        is not in this panel's list — the data dialogs' Media tab, which reads
        the conversation's entire history from the database while this panel
        keeps roughly the last 200 messages in memory.

        Reported live as: some audios in the Media tab simply do not play, with
        no error, no announcement, and no visible change. They were not a codec
        problem — the activation never reached playback at all, because the
        index lookup returned -1 and the caller skipped the action in silence.
        Documents, images, videos and links in that tab were affected the same
        way; audio is just the type Enter is the natural gesture for.

        index is optional and only used to put keyboard focus back on the row
        the media viewer was opened from.
        """
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("messageType", "")
        msg_obj  = msg.get("message") or {}
        msg_id   = msg.get("key", {}).get("id", "")

        # For text-based messages: open the first link if one is present,
        # otherwise show the full message text popup (same as Alt+C).
        if msg_type in ("conversation", "extendedTextMessage", ""):
            # Full untruncated text — see the matching comment in
            # _on_message_focused() for why GetItemText(index) is wrong here.
            rendered = self._render_message_line(msg)
            links = self._extract_links(rendered)
            if links:
                try:
                    os.startfile(links[0])
                except Exception:
                    wx.LaunchDefaultBrowser(links[0])
                return
            self._show_message_text_popup(msg)
            return

        if msg_type == "audioMessage":
            duration = (msg_obj.get("audioMessage") or {}).get("seconds", 0) or 0
            clean_msg_id = msg_id
            if "_" in msg_id:
                parts = msg_id.split("_")
                clean_msg_id = parts[2] if len(parts) > 2 else parts[-1]
            logging.info(f"[UI Audio Activation] msg_id={msg_id}, clean_msg_id={clean_msg_id}, duration={duration}, file={data_path('voice_messages', f'{clean_msg_id}.msv')}")
            self._toggle_playback(
                msg_id, duration, msg,
                file_path=data_path("voice_messages", f"{clean_msg_id}.msv"),
                audio_ext=".ogg",
            )

        elif msg_type == "videoMessage" and not self._use_conversation_video_media_viewer_dialog():
            # Classic mode (Settings > Interface do usuário > "Mostrar vídeos
            # nas conversas em player separado" unchecked): play in-app via
            # BASS/ffmpeg instead of the dialog, exactly like before that
            # dialog existed — see _play_toggle_video_message().
            video = msg_obj.get("videoMessage") or {}
            if video.get("gifPlayback"):
                return  # GIFs have no audio track to play
            self._play_toggle_video_message(msg)

        elif msg_type in ("imageMessage", "videoMessage"):
            # Media opens in the same accessible, maximized viewer used by
            # statuses. This avoids wx.StaticBitmap clipping and gives video
            # proper seek/volume/speed controls.
            self.open_media_viewer_for_message(msg, restore_index=index)

        elif msg_type in ("documentMessage", "locationMessage", "liveLocationMessage"):
            # Documents and locations keep their existing system-open behaviour.
            # open_media_message() is _on_action_open()'s message-based half and
            # covers both, including the download-failure reporting.
            self.open_media_message(msg)

        elif msg_type == "contactMessage":
            # Enter/Space on a contact message → open a conversation with
            # that contact, same as clicking the "Conversar" button.
            contact = msg_obj.get("contactMessage") or {}
            jid = self._jid_from_vcard(contact.get("vcard", ""))
            self._on_contact_converse(None, jid=jid)

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

        menu = wx.Menu()

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
        if self._reaction_map:
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
        if _is_group and not _is_from_me:
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
        # feature, visible to every participant, unlike the local-only star
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
        if msg_type in _SAVEABLE and os.path.isfile(
            data_path("media", f"{clean_msg_id}.wzmedia")
        ):
            menu.AppendSeparator()
            save_item = menu.Append(
                wx.ID_ANY, f"{i18n.t('save_as')}\tCtrl+Shift+S"
            )
            self.Bind(wx.EVT_MENU, self._on_action_save_as, save_item)
        elif msg_type == "audioMessage":
            # Voice messages are cached separately (voice_messages/*.msv) and
            # can be saved even while a download is still pending — the save
            # flow downloads it first if needed, same as the other media types.
            menu.AppendSeparator()
            save_audio_item = menu.Append(
                wx.ID_ANY, f"{i18n.t('save_audio_as')}\tCtrl+Shift+S"
            )
            self.Bind(wx.EVT_MENU, self._on_action_save_as, save_audio_item)

        # Edit (own text messages within 3 hours)
        _is_own      = msg.get("key", {}).get("fromMe", False)
        _is_text     = msg_type in ("conversation", "extendedTextMessage")
        _msg_ts      = msg.get("messageTimestamp", 0)
        _within_3h   = (time.time() - _msg_ts) < 10800
        if _is_own and _is_text and _within_3h:
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

    def _on_ctrl_shift_s(self, event):
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg_type = self._sorted_messages[index].get("messageType", "")
        if msg_type in ("documentMessage", "imageMessage", "videoMessage", "audioMessage"):
            self._on_action_save_as(None)

    # ── Media controls helpers ──────────────────────────────────────────────

    def _hide_all_media_controls(self):
        # Selection moved off the playing video row — stop it. Unlike audio
        # playback elsewhere in this file (which is allowed to keep playing
        # while the user scrolls/selects elsewhere), video also holds a
        # live ffmpeg subprocess; leaving it running unattended is worth
        # avoiding outright rather than matching audio's more permissive
        # behaviour.
        was_playing_video = self._current_video_msg_id is not None
        if getattr(self, "_video_player", None) is not None:
            self._video_player.stop()
        self._current_video_msg_id = None
        if was_playing_video:
            # Video never keeps its shared speed/slider controls visible
            # once stopped here (unlike audio, which can keep playing in
            # the background and re-show them on refocus) — nothing else
            # will hide them since video always stops on defocus.
            self._hide_audio_controls()
        # Drop the video-sized box _start_video_playback() installs, so the
        # next still thumbnail sizes itself from its own bitmap again.
        self._media_bitmap.SetMinSize((-1, -1))
        self._media_bitmap.Hide()
        self._action_open_btn.Hide()
        self._action_save_as_btn.Hide()
        self._action_download_btn.Hide()
        self._hide_media_transfer_gauge()
        # The transfer gauge is not a selection-specific media control.  Hiding
        # it here made an in-flight upload/download disappear whenever focus or
        # message selection changed.  Transfer completion / conversation exit
        # owns its lifetime instead.
        self._buttons_container.Hide()
        self._contact_converse_btn.Hide()
        self._contact_save_btn.Hide()
        self._contact_msg_jid = None
        self._update_links_panel([])
        self._update_mentions_panel([])
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

    # ── URL / link helpers ───────────────────────────────────────────────────

    @staticmethod
    def _extract_links(text: str) -> list:
        """Return deduplicated list of URLs found in *text*."""
        matches = _URL_RE.findall(text)
        seen = set()
        out  = []
        for m in matches:
            # Strip trailing punctuation that is not part of the URL
            m = m.rstrip('.,;:!?)\'"\\>]')
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def _update_links_panel(self, links: list):
        """Rebuild the link controls below the messages list.

        A single link keeps the existing HyperlinkCtrl tab-stop. Two or
        more are shown as one navigable list instead (issue #65) — users
        previously had to Tab/Shift+Tab through every link in a message as
        its own separate stop. Up/Down move between them (native ListCtrl
        behaviour also gives Home/End for free), and Ctrl+C copies just the
        focused link.
        """
        # Destroy all child controls except the static label (first item)
        for child in list(self._links_panel.GetChildren()):
            if child is not self._links_label:
                child.Destroy()
        # Remove all items except the first (label) from the sizer
        while self._links_sizer.GetItemCount() > 1:
            self._links_sizer.Remove(1)
        self._links_list = None

        if not links:
            self._links_panel.Hide()
            self._current_links = []
            if self.conversation_panel.IsShown():
                self.conversation_panel.Layout()
            return

        self._current_links = links
        i18n = self.main_window.i18n

        if len(links) == 1:
            self._links_label.SetLabel(i18n.t("links_section_label"))
            url = links[0]
            ctrl = wx.adv.HyperlinkCtrl(
                self._links_panel,
                id=wx.ID_ANY,
                label=url,
                url=url,
                style=wx.adv.HL_DEFAULT_STYLE,
            )
            ctrl.Bind(wx.adv.EVT_HYPERLINK, self._on_hyperlink_open)
            ctrl.Bind(wx.EVT_KEY_DOWN,  self._on_link_key_down)
            self._links_sizer.Add(ctrl, 0, wx.LEFT | wx.BOTTOM, 3)
        else:
            self._links_label.SetLabel(i18n.t("links_list_label"))
            lst = wx.ListCtrl(
                self._links_panel,
                style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_NO_HEADER,
            )
            lst.InsertColumn(0, i18n.t("links_list_label"), width=400)
            for url in links:
                lst.Append((url,))
            lst.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_links_list_activated)
            lst.Bind(wx.EVT_KEY_DOWN, self._on_links_list_key_down)
            lst.Focus(0)
            lst.Select(0)
            self._links_sizer.Add(lst, 0, wx.EXPAND | wx.LEFT | wx.BOTTOM, 3)
            self._links_list = lst

        self._links_panel.Show()
        self._links_panel.Layout()
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

    @staticmethod
    def _open_link(url: str):
        """Open a link URL in the system's default application."""
        try:
            os.startfile(url)
        except Exception:
            wx.LaunchDefaultBrowser(url)

    def _on_hyperlink_open(self, event):
        self._open_link(event.GetURL())

    def _on_link_key_down(self, event):
        """Ensure Space and Enter activate a focused HyperlinkCtrl."""
        kc = event.GetKeyCode()
        if kc in (wx.WXK_RETURN, wx.WXK_SPACE, wx.WXK_NUMPAD_ENTER):
            self._open_link(event.GetEventObject().GetURL())
        else:
            event.Skip()

    def _on_links_list_activated(self, event):
        """Enter (or a double-click) on a link row opens it."""
        idx = event.GetIndex()
        if 0 <= idx < len(self._current_links):
            self._open_link(self._current_links[idx])

    def _on_links_list_key_down(self, event):
        """Space also opens the focused link; Ctrl+C copies just its URL."""
        kc = event.GetKeyCode()
        if kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
            idx = self._links_list.GetFirstSelected()
            if 0 <= idx < len(self._current_links):
                self._open_link(self._current_links[idx])
            return
        if event.ControlDown() and kc == ord("C"):
            idx = self._links_list.GetFirstSelected()
            if 0 <= idx < len(self._current_links):
                url = self._current_links[idx]
                try:
                    pyperclip.copy(url)
                    self.main_window.output(self.main_window.i18n.t("link_copied"))
                except Exception:
                    self.main_window.output(self.main_window.i18n.t("msg_copy_error"))
            return
        event.Skip()

    # ── @mention helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _raw_mentioned_jids(msg: dict) -> list:
        """The message's mentionedJid list, from wherever contextInfo landed."""
        msg_obj = msg.get("message") or {}
        ext = (msg_obj.get("extendedTextMessage") or {}) if isinstance(msg_obj, dict) else {}
        return (
            (msg.get("contextInfo") or {}).get("mentionedJid")
            or (msg_obj.get("contextInfo") or {}).get("mentionedJid")
            or (ext.get("contextInfo") or {}).get("mentionedJid")
            or []
        )

    def _mention_identity(self, jid: str) -> str:
        """One canonical string per person, whichever JID form they arrive as.

        @lid and @s.whatsapp.net are two addresses for the same participant, and
        which one a mention list carries depends entirely on who sent the
        message (see _extract_mentions). Resolve @lid through the phone cache —
        and fall back to the bare digits when the cache doesn't know it yet, so
        two forms of an unmapped participant at least still collide rather than
        counting as two different people.
        """
        if not jid:
            return ""
        mw = self.main_window
        jid = mw._normalize_jid(jid)
        if jid.endswith("@lid"):
            jid = getattr(mw, "_lid_to_phone", {}).get(jid, jid)
        return jid.rsplit("@", 1)[0].split(":")[0]

    def _extract_mentions(self, msg: dict) -> list:
        """Return list of (display_name, jid) for @mentioned JIDs in msg.

        Returns [] when the mentioned set covers essentially every
        participant in the group — WhatsApp expands an @Todos/@everyone
        mention into the full participant JID list with no marker
        distinguishing it from mentioning each person individually, and a
        hyperlink per participant (routinely dozens in a large group) is
        just UI noise for something that always means "everyone", not
        something a per-person link helps with. Individual mentions of
        specific people still show their links as before.
        """
        mentioned = self._raw_mentioned_jids(msg)
        if not mentioned:
            return []

        participants = getattr(self, "_group_participants_cache", None)
        if participants:
            # _normalize_jid() alone is NOT enough to compare these two sets:
            # it only rewrites @c.us → @s.whatsapp.net and leaves @lid untouched.
            # The participants cache is populated with whatever form the group
            # metadata uses (@lid on LID-addressed accounts), while a message
            # WinZapp itself sent carries the phone form — _canonical_mention_jids()
            # converts every mention through _lid_to_phone before handing it to
            # the send endpoint. So for our OWN @todos messages the two sets
            # never intersected at all, the "this is really @everyone" test
            # always failed, and every participant got their own hyperlink
            # (dozens of them) — while the identical message received from
            # someone else, whose mentions arrive still keyed by @lid, was
            # correctly collapsed. Bridge both sides to one identity first.
            _ident = self._mention_identity
            participant_jids = {_ident(jid) for _, jid in participants}
            participant_jids.discard("")
            # Strictly more than 2: with exactly 2 participants the threshold
            # below is len - 1 == 1, so mentioning one person is arithmetically
            # indistinguishable from mentioning everyone, and every individual
            # mention in a 2-person group gets silently swallowed. Relaxing
            # this to >= 2 is what broke
            # test_a_tiny_group_is_not_treated_as_mention_all.
            if len(participant_jids) > 2:
                mentioned_norm = {_ident(jid) for jid in mentioned if jid}
                mentioned_norm.discard("")
                # Primary check: JID intersection (works for received messages).
                # Fallback: count-based check — if mentioned count covers almost
                # all participants, it's @todos regardless of JID format mismatch
                # (happens for messages WinZapp itself sent, where phone-form JIDs
                # don't intersect the @lid-keyed participant cache).
                intersect_size = len(mentioned_norm & participant_jids)
                threshold = len(participant_jids) - 1
                # count_match requires >= 2 mentions AND near-full coverage to
                # avoid suppressing individual mentions in small groups.
                count_match = (
                    len(mentioned_norm) >= 2
                    and len(mentioned_norm) >= threshold
                )
                if intersect_size >= threshold or count_match:
                    return []

        out = []
        seen = set()
        for jid in mentioned:
            if not jid or jid in seen:
                continue
            seen.add(jid)
            name = self._get_participant_name(jid)
            out.append((name, jid))
        return out

    def _update_mentions_panel(self, mentions: list):
        """Rebuild the @mention buttons below the messages list."""
        for child in list(self._mentions_panel.GetChildren()):
            if child is not self._mentions_label:
                child.Destroy()
        while self._mentions_sizer.GetItemCount() > 1:
            self._mentions_sizer.Remove(1)

        if not mentions:
            self._mentions_panel.Hide()
            self._current_mentions = []
            if self.conversation_panel.IsShown():
                self.conversation_panel.Layout()
            return

        self._current_mentions = mentions

        for display_name, jid in mentions:
            ctrl = wx.adv.HyperlinkCtrl(
                self._mentions_panel,
                id=wx.ID_ANY,
                label=f"@{display_name}",
                url=f"mention://{jid}",
                style=wx.adv.HL_DEFAULT_STYLE,
            )
            ctrl.Bind(
                wx.adv.EVT_HYPERLINK,
                lambda e, j=jid: self._on_mention_hyperlink(e, j),
            )
            ctrl.Bind(wx.EVT_KEY_DOWN, self._on_mention_display_key_down)
            self._mentions_sizer.Add(ctrl, 0, wx.LEFT | wx.BOTTOM, 3)

        self._mentions_panel.Show()
        self._mentions_panel.Layout()
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

    def _on_mention_open(self, jid: str):
        """Navigate to the conversation for the mentioned contact."""
        mw = self.main_window
        target_jid = jid
        # Resolve @lid <=> @s.whatsapp.net alternative JID if it was deduplicated
        alt_jid = getattr(mw, "_lid_to_phone", {}).get(jid) or getattr(mw, "_phone_to_lid", {}).get(jid)
        if alt_jid and alt_jid in mw.chats:
            target_jid = alt_jid
        
        chat = mw.get_chat(target_jid)
        if chat is None:
            name = self._get_participant_name(target_jid)
            chat = {"remoteJid": target_jid, "pushName": name}
        self.navigate_to_conversation(chat)

    def _on_mention_hyperlink(self, event, jid: str):
        """Intercept EVT_HYPERLINK on a mention display link to navigate instead of open URL."""
        event.Skip(False)
        self._on_mention_open(jid)

    def _on_mention_display_key_down(self, event):
        """Space/Enter on a mention HyperlinkCtrl activates it (like click)."""
        kc = event.GetKeyCode()
        if kc in (wx.WXK_RETURN, wx.WXK_SPACE, wx.WXK_NUMPAD_ENTER):
            ctrl = event.GetEventObject()
            jid = ctrl.GetURL().replace("mention://", "")
            self._on_mention_open(jid)
        else:
            event.Skip()

    # ── @mention input system ────────────────────────────────────────────────

    def _get_mention_query(self):
        """Return (start_pos, query) when cursor is inside @word, else (None, None)."""
        text = self.message_field.GetValue()
        pos  = self.message_field.GetInsertionPoint()
        i = min(pos - 1, len(text) - 1)
        while i >= 0:
            ch = text[i]
            if ch == "@":
                return (i, text[i + 1:pos])
            if ch in (" ", "\n", "\t"):
                break
            i -= 1
        return (None, None)

    def _hide_mention_suggestions(self):
        """Hide the mention suggestion list without announcing anything."""
        self._mention_active = False
        if hasattr(self, "_mention_panel") and self._mention_panel.IsShown():
            self._mention_panel.Hide()
            if self.conversation_panel.IsShown():
                self.conversation_panel.Layout()

    def _update_mention_suggestions(self, query: str):
        """Rebuild the suggestion list for the given query and show/hide the panel."""
        i18n = self.main_window.i18n
        q = query.lower()

        # Collect JIDs of everyone who has sent at least one message in the current conversation
        participants_who_sent_message = set()
        for msg in getattr(self, "_sorted_messages", []):
            if not isinstance(msg, dict):
                continue
            key = msg.get("key") or {}
            p_jid = key.get("participant") or msg.get("participant")
            if not p_jid and not msg.get("isGroupMsg", False):
                p_jid = key.get("remoteJid")
            if p_jid:
                p_jid = self.main_window._normalize_jid(p_jid)
                participants_who_sent_message.add(p_jid)
                # Map alternate formats
                phone_jid = getattr(self.main_window, "_lid_to_phone", {}).get(p_jid, "")
                if phone_jid:
                    participants_who_sent_message.add(phone_jid)
                lid_jid = getattr(self.main_window, "_phone_to_lid", {}).get(p_jid, "")
                if lid_jid:
                    participants_who_sent_message.add(lid_jid)

        def is_saved(jid):
            local = jid.rsplit("@", 1)[0]
            candidates = [jid]
            if jid.endswith("@lid"):
                phone = getattr(self.main_window, "_lid_to_phone", {}).get(jid, "")
                if phone:
                    candidates.append(phone)
                    candidates.append(phone.rsplit("@", 1)[0] + "@c.us")
            elif jid.endswith("@s.whatsapp.net"):
                candidates.append(local + "@c.us")
                lid = getattr(self.main_window, "_phone_to_lid", {}).get(jid, "")
                if lid:
                    candidates.append(lid)
            elif jid.endswith("@c.us"):
                candidates.append(local + "@s.whatsapp.net")

            for cjid in candidates:
                c = self.main_window.contacts.get(cjid)
                if c:
                    if c.get("isMyContact") or c.get("isSaved") or c.get("syncToAddressbook"):
                        return True
                    name = (c.get("name") or "").strip()
                    # main_window._is_bad_contact_name() instead of a third,
                    # independently-maintained copy of the same "sem nome"/
                    # "unknown" placeholder check (see _sender_label()'s
                    # _contact_name() for the other one and why they drift).
                    if name and not self.main_window._is_bad_contact_name(name):
                        return True
            return False

        # Individual participant matches filtered by rules:
        # Show if contact is saved in contacts OR has sent a message in the group
        matches = []
        for name, jid in self._group_participants_cache:
            norm_jid = self.main_window._normalize_jid(jid)
            if not q or q in name.lower() or q in norm_jid:
                matches.append((name, jid))

        logging.info(f"[mention] _update_mention_suggestions: query='{query}', cache_size={len(self._group_participants_cache)}, matches_size={len(matches)}")

        # Sort: names that start with the query come first, then those that
        # contain it but don't start with it — both groups sorted alphabetically.
        if q:
            matches.sort(key=lambda x: (0 if x[0].lower().startswith(q) else 1, x[0].lower()))

        # @all/@todos special entry — always at the top when query is empty or matches
        all_kw = i18n.t("mention_all_keyword")  # "todos" or "all"
        if not q or q in all_kw or q in "all" or q in "todos":
            matches = [("__ALL__", "@all")] + matches

        self._mention_suggestions = matches

        if not matches:
            was_visible = self._mention_panel.IsShown()
            self._hide_mention_suggestions()
            if was_visible:
                self.main_window.output(i18n.t("mention_no_suggestions"), interrupt=True)
            return

        self._mention_list.Clear()
        all_label = i18n.t("mention_all_label")
        for name, jid in matches:
            if jid == "@all":
                self._mention_list.Append(all_label)
            else:
                self._mention_list.Append(f"@{name}")

        self._mention_panel.Show()
        self._mention_panel.Layout()
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()
        self._mention_list.SetSelection(0)
        self.main_window.output(i18n.t("mention_suggestions_available"), interrupt=False)

    def _on_text_changed_mention_check(self):
        """Called from on_change_message_field to detect and update @mention suggestions."""
        if self.conversation is None:
            return
        jid = self.conversation.get("remoteJid", "")
        if not jid.endswith("@g.us"):
            if self._mention_panel.IsShown():
                self._hide_mention_suggestions()
            return
        start, query = self._get_mention_query()
        if start is None:
            if self._mention_panel.IsShown():
                self._hide_mention_suggestions()
                self._mention_active = False
            return

        # Fail-safe: if cache is empty, fetch participants now in background
        if not getattr(self, "_group_participants_cache", None):
            logging.info(f"[mention] Cache empty on mention check. Triggering lazy fetch for group {jid}...")
            threading.Thread(
                target=self._fetch_group_participants,
                args=(jid,),
                daemon=True,
            ).start()

        self._mention_active = True
        self._mention_start_pos = start
        self._mention_query = query
        self._update_mention_suggestions(query)

    def _insert_mention(self, display_name: str, jid: str):
        """Replace the current @query in the field with @display_name and track the JID."""
        # Use cached start/query so this works even when message_field doesn't
        # have focus (e.g. when called from the mention list via EVT_CHAR_HOOK).
        start = self._mention_start_pos
        query = self._mention_query
        if start < 0:
            return
        i18n = self.main_window.i18n

        if jid == "@all":
            # @todos/@all: use the localized keyword as the inserted text and add
            # every group participant JID to the pending mentions list.
            all_kw = i18n.t("mention_all_keyword")   # "todos" or "all"
            replacement = f"@{all_kw} "
            for p_name, p_jid in self._group_participants_cache:
                if p_jid not in self._pending_mentions:
                    self._pending_mentions.append(p_jid)
                # Always store the display name so pill buttons show names, not LIDs.
                if p_jid not in self._pending_mention_display_names:
                    self._pending_mention_display_names[p_jid] = p_name or p_jid.rsplit("@", 1)[0]
        else:
            replacement = f"@{display_name} "
            if jid not in self._pending_mentions:
                self._pending_mentions.append(jid)
            self._pending_mention_display_names[jid] = display_name

        text = self.message_field.GetValue()
        new_text = text[:start] + replacement + text[start + 1 + len(query):]
        # ChangeValue does NOT fire EVT_TEXT, preventing a mention-check loop.
        self.message_field.ChangeValue(new_text)
        self.message_field.SetInsertionPoint(start + len(replacement))
        self._hide_mention_suggestions()
        self._mention_active = False
        self._rebuild_mention_pills()
        self.message_field.SetFocus()

    def _rebuild_mention_pills(self):
        """Rebuild the pending-mention pill buttons panel (one row per @mention)."""
        i18n = self.main_window.i18n
        panel = self._pending_mentions_panel
        sizer = self._pending_mentions_sizer

        # Destroy existing pill widgets.
        for child in list(panel.GetChildren()):
            child.Destroy()
        sizer.Clear(delete_windows=False)

        if not self._pending_mentions:
            panel.Hide()
            if self.conversation_panel.IsShown():
                self.conversation_panel.Layout()
            return

        for jid in list(self._pending_mentions):
            display = self._pending_mention_display_names.get(jid) or jid.rsplit("@", 1)[0]
            row = wx.BoxSizer(wx.HORIZONTAL)
            lbl = wx.StaticText(panel, label=f"@{display}")
            btn_label = i18n.t("remove_mention").format(name=display)
            btn = wx.Button(panel, label=btn_label)
            # Capture jid/display in closure.
            def _make_handler(j, d):
                def _handler(evt):
                    self._on_remove_mention(j, d)
                return _handler
            btn.Bind(wx.EVT_BUTTON, _make_handler(jid, display))
            row.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
            row.Add(btn, 0, wx.ALIGN_CENTER_VERTICAL)
            sizer.Add(row, 0, wx.LEFT | wx.BOTTOM, 3)

        panel.Show()
        panel.Layout()
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

    def _on_remove_mention(self, jid: str, display: str):
        """Remove a pending @mention pill and its text from the message field."""
        # Remove @display from message text if present.
        text = self.message_field.GetValue()
        # Try removing "@display " (with trailing space) first, then "@display" alone.
        if f"@{display} " in text:
            new_text = text.replace(f"@{display} ", "", 1)
        elif f"@{display}" in text:
            new_text = text.replace(f"@{display}", "", 1)
        else:
            new_text = text
        if new_text != text:
            self.message_field.ChangeValue(new_text)

        # Remove from pending state.
        if jid in self._pending_mentions:
            self._pending_mentions.remove(jid)
        self._pending_mention_display_names.pop(jid, None)

        self._rebuild_mention_pills()
        self.message_field.SetFocus()

    def _fetch_group_participants(self, jid: str):
        """Background: fetch participants for the group and populate the cache with retries if session is loading."""
        max_retries = 3
        delay = 3
        for attempt in range(max_retries):
            # Check if this chat is still the active conversation before retrying
            if not self.conversation or self.conversation.get("remoteJid") != jid:
                logging.info(f"[mention] Active conversation changed. Aborting fetch for {jid}.")
                return
            try:
                data = self.main_window.get_group_info(jid)
                participants = data.get("participants", [])
                logging.info(f"[mention] get_group_info({jid}) attempt {attempt+1}/{max_retries} → {len(participants)} participants")
                if participants:
                    my_jid = getattr(self.main_window, "my_jid", "") or ""
                    # Build initial cache first so UI is populated instantly
                    cache = []
                    for p in participants:
                        if not isinstance(p, dict):
                            continue
                        p_jid = p.get("id", "")
                        if not p_jid:
                            continue
                        if my_jid and p_jid.split("@")[0] == my_jid.split("@")[0]:
                            continue  # skip self
                        name = self._get_participant_name(p_jid, p)
                        cache.append((name, p_jid))
                    cache.sort(key=lambda x: x[0].lower())
                    logging.info(f"[mention] cache built: {[n for n,_ in cache]}")
                    wx.CallAfter(self._set_group_participants_cache, cache)
                    return
            except Exception as e:
                logging.error(f"[mention] _fetch_group_participants error on attempt {attempt+1}: {e}", exc_info=True)
            
            if attempt < max_retries - 1:
                logging.info(f"[mention] Empty participants response, retrying in {delay}s...")
                time.sleep(delay)

    def _set_group_participants_cache(self, cache: list):
        """Main-thread callback: store cache and refresh suggestions if active."""
        self._group_participants_cache = cache
        if self._mention_active:
            self._update_mention_suggestions(self._mention_query)

    def _on_message_field_key_down(self, event):
        """↓ moves focus to the mention list when suggestions are visible.
        Shift+Enter inserts a newline instead of sending — TE_PROCESS_ENTER
        makes plain Enter fire EVT_TEXT_ENTER (send) on this control, and
        wx's native multiline edit control only inserts a literal newline on
        Ctrl+Enter, with no Shift+Enter equivalent of its own (issue #16)."""
        kc = event.GetKeyCode()
        if kc == wx.WXK_DOWN and self._mention_panel.IsShown():
            if self._mention_list.GetCount() > 0:
                self._mention_list.SetFocus()
                self._mention_list.SetSelection(0)
            return  # consume — don't let the field handle ↓
        if kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and event.ShiftDown():
            # WriteText() inserts at the control's own insertion point (and
            # over any active selection) and lets the native control manage
            # the caret itself — necessary because on Windows a multiline
            # wx.TextCtrl stores line breaks internally as \r\n while
            # GetInsertionPoint()/SetInsertionPoint() count positions in that
            # native representation, not the \n-only positions GetValue()
            # reports. The previous approach (rebuild the whole value with a
            # plain "\n", then SetInsertionPoint(pos + 1)) assumed one
            # inserted character, but Windows silently expands it to two —
            # so the caret landed one character short, between the \r and
            # the \n. NVDA then kept announcing everything typed next as
            # still on the previous line (issue #48).
            self.message_field.WriteText("\n")
            self.on_change_message_field(None)
            return  # consume — don't send and don't double-insert
        event.Skip()

    @staticmethod
    def _is_phantom_nvda_char(event) -> bool:
        """True for the bogus U+00FF character that a screen reader's own
        modifier-key gestures (Windows+NVDA+Left/Right and others — issue
        #71 — and reportedly Alt+Tab as well) leak into whatever control is
        focused, or into the message field via the "type anywhere to reply"
        redirect below when it isn't (see _on_conversation_char_hook()).

        Reported live: each press of Windows+NVDA+Left/Right inserted one
        literal 'ÿ' into the message field, even though no text key was
        pressed and the same gestures type nothing in other applications.
        NVDA's own keyboard hook is supposed to swallow these combinations
        entirely; when the OS still emits a WM_CHAR for one anyway (observed
        specifically for Windows-key gestures NVDA intercepts), it carries
        the character U+00FF — not a value any real keyboard layout produces
        by pressing the Windows key plus an arrow. That makes it safe to
        veto unconditionally rather than trying to special-case NVDA's own
        modifier state, which wx never sees. Checked at every entry point
        that can put a character into the message field — see
        _on_conversation_char_hook() for the other one — because this exact
        code point is never a legitimate keystroke.
        """
        return event.GetUnicodeKey() == 0xFF

    def _on_message_field_char(self, event):
        if self._is_phantom_nvda_char(event):
            return  # veto — do not insert, do not Skip()
        event.Skip()

    def _on_text_field_paste(self, event):
        """Intercept pastes: non-text clipboard content becomes an
        attachment (see _paste_clipboard_as_attachment()); otherwise, Unicode
        line/paragraph separators become \n.

        Bound to every field here whose text ends up on WhatsApp — the
        message field and the attachment caption — and works off the control
        that raised the event, so adding another one is a single Bind().

        Rich clipboard sources — Google Docs, Word, websites, Apple apps —
        put U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR where a plain
        editor stores \n. A wx.TextCtrl keeps them verbatim: the native
        control does not render them as breaks (a paste looks like a single
        long line), yet WhatsApp renders U+2029 as a paragraph break on the
        receiving side. The result is the "it looks fine here but arrives
        with weird breaks" report. Normalizing the pasted text here makes the
        field, the screen reader and the recipient all agree.
        """
        if not wx.TheClipboard.Open():
            event.Skip()
            return
        try:
            if self._paste_clipboard_as_attachment():
                return
            if not wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_UNICODETEXT)):
                event.Skip()
                return
            data = wx.TextDataObject()
            if not wx.TheClipboard.GetData(data):
                event.Skip()
                return
            text = data.GetText()
        finally:
            wx.TheClipboard.Close()
        normalized = normalize_line_separators(text)
        target = event.GetEventObject()
        if normalized != text and target is not None:
            # WriteText() replaces the current selection and fires EVT_TEXT,
            # keeping the mention check / send-button logic in sync.
            target.WriteText(normalized)
            return  # consume — the native paste must not run on top of this
        event.Skip()

    def _paste_from_messages_list(self) -> bool:
        """Paste clipboard content while the message history has focus.

        File/image clipboard formats keep their attachment semantics; text is
        inserted into the composer at its current caret position. This avoids
        Windows exposing a copied Explorer file as a text path and makes Ctrl+V
        behave consistently whether focus is in the history or the composer.
        """
        if self.conversation is None:
            return False

        if not wx.TheClipboard.Open():
            self.message_field.SetFocus()
            return True

        text = None
        try:
            if self._paste_clipboard_as_attachment():
                return True
            if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_UNICODETEXT)):
                data = wx.TextDataObject()
                if wx.TheClipboard.GetData(data):
                    text = normalize_line_separators(data.GetText())
        finally:
            wx.TheClipboard.Close()

        self.message_field.SetFocus()
        if text is not None:
            self.message_field.WriteText(text)
        return True

    def _paste_clipboard_as_attachment(self) -> bool:
        """Ctrl+V of non-text clipboard content (files copied in Explorer, or
        an image copied from a browser/screenshot tool) attaches it directly
        — same shortcut the official WhatsApp client offers — instead of
        doing nothing useful in a plain wx.TextCtrl. Files skip the picker
        dialog entirely and land straight in the attachment panel with the
        caption field focused, exactly like choosing them there would.

        Must be called with the clipboard already open (the caller,
        _on_text_field_paste, holds it for its own checks too — nested
        wx.TheClipboard.Open() calls fail). Returns True when it staged
        something, so the caller must not also run the native/text paste.
        """
        if self.conversation is None:
            return False

        if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_FILENAME)):
            data = wx.FileDataObject()
            if wx.TheClipboard.GetData(data):
                paths = [p for p in data.GetFilenames() if os.path.isfile(p)]
                if paths:
                    for path in paths:
                        self._staged_attachments.append(
                            {
                                "path": path,
                                "media_type": classify_attachment_media_type(path),
                            }
                        )
                    self._show_attachment_panel()
                    return True

        if wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_BITMAP)):
            data = wx.BitmapDataObject()
            if wx.TheClipboard.GetData(data):
                bitmap = data.GetBitmap()
                if bitmap.IsOk():
                    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    tmp.close()
                    if bitmap.SaveFile(tmp.name, wx.BITMAP_TYPE_PNG):
                        self._staged_attachments.append(
                            {"path": tmp.name, "media_type": "image"}
                        )
                        self._show_attachment_panel()
                        return True

        return False

    def _on_mention_list_key_down(self, event):
        """Keyboard navigation inside the mention suggestion list."""
        kc = event.GetKeyCode()

        if kc in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            idx = self._mention_list.GetSelection()
            if 0 <= idx < len(self._mention_suggestions):
                name, jid = self._mention_suggestions[idx]
                self._insert_mention(name, jid)
            return

        if kc == wx.WXK_ESCAPE:
            self._hide_mention_suggestions()
            self.message_field.SetFocus()
            return

        if kc == wx.WXK_BACK:
            # Backspace: remove last char from message field and update filter
            pos = self.message_field.GetInsertionPoint()
            if pos > 0:
                text = self.message_field.GetValue()
                self.message_field.ChangeValue(text[:pos - 1] + text[pos:])
                self.message_field.SetInsertionPoint(pos - 1)
                wx.CallAfter(self._on_mention_list_after_char)
            return

        # ↑ / ↓ — let ListBox move the selection naturally; NVDA reads it
        event.Skip()

    def _on_mention_list_char(self, event):
        """Printable chars typed in the list are redirected to the message field."""
        uc = event.GetUnicodeKey()
        if uc == wx.WXK_NONE or uc < 32:
            event.Skip()
            return
        ch = chr(uc)
        pos = self.message_field.GetInsertionPoint()
        text = self.message_field.GetValue()
        self.message_field.ChangeValue(text[:pos] + ch + text[pos:])
        self.message_field.SetInsertionPoint(pos + 1)
        wx.CallAfter(self._on_mention_list_after_char)

    def _on_mention_list_after_char(self):
        """Update mention suggestions after a char was injected from the list."""
        i18n = self.main_window.i18n
        start, query = self._get_mention_query()
        if start is None:
            self._hide_mention_suggestions()
            self.main_window.output(i18n.t("mention_no_suggestions"), interrupt=True)
            self.message_field.SetFocus()
            return
        self._mention_query = query
        self._update_mention_suggestions(query)
        # Return focus to the list so the user can keep typing or navigate
        if self._mention_suggestions:
            self._mention_list.SetFocus()
            self._mention_list.SetSelection(0)

    def _on_mention_list_selected_mouse(self, event):
        """Mouse click or double click on mention list item inserts it."""
        idx = self._mention_list.GetSelection()
        if 0 <= idx < len(self._mention_suggestions):
            name, jid = self._mention_suggestions[idx]
            self._insert_mention(name, jid)

    # ── Lazy-loading: load older messages when the user focuses item 0 ─────────

    def _focused_msg_id(self) -> str:
        """Return the message ID of the currently focused list item, or ''."""
        idx = self.messages_list.GetFocusedItem()
        if idx < 0 or idx >= len(self._sorted_messages):
            return ""
        m = self._sorted_messages[idx]
        if self._is_separator(m):
            return ""
        return m.get("key", {}).get("id", "")

    def _on_message_focused(self, event):
        # Set while another surface (the group data dialog's Media tab) drives
        # this list's selection purely to hand an index to a handler that reads
        # it. Everything below reacts to a HUMAN moving through the list -
        # playing the selection sound, marking the conversation read once the
        # unread separator is passed, and page-loading more history at index 0 -
        # and none of it should fire for a selection the user never made. The
        # history load is the dangerous one: it rebuilds _sorted_messages
        # synchronously, so the very index being handed over stops being valid.
        if getattr(self, "_suppress_selection_side_effects", False):
            return
        idx = event.GetIndex()

        if 0 <= idx < len(self._sorted_messages):
            msg = self._sorted_messages[idx]
            if not self._is_separator(msg):
                msg_id = msg.get("key", {}).get("id", "")
                if msg_id and msg_id in self.selected_messages:
                    self.selection_sound.play()

        # Unread-separator logic:
        # - Focus reaching the separator (or anything below it) marks the
        #   conversation as read, once.
        # - Focus moving PAST the separator dismisses the row itself. Merely
        #   landing on it does not — see _should_dismiss_unread_separator().
        if self._unread_sep_idx >= 0:
            if idx >= self._unread_sep_idx:
                # Mark as read immediately (first time focus arrives) — but
                # not while populate_messages() is still running
                # (_populating_messages): that method's OWN default-placement
                # Focus() call (landing exactly on the separator/last row when
                # a conversation is freshly opened) fires this same
                # EVT_LIST_ITEM_FOCUSED synchronously, well before the
                # deferred wx.CallAfter in navigate_to_conversation() ever
                # moves real keyboard focus off the conversations-list row
                # the user just pressed Enter on. Starting the mark-as-read
                # thread here raced ahead of that CallAfter and could get its
                # own wx.CallAfter(_refresh_chat_row_in_list) queued (and
                # executed) first — updating the still-focused conversations
                # list row's text (removing the unread badge) before focus
                # had actually moved away from it, so NVDA re-announced the
                # row's new text instead of the newly focused messages
                # list/message field. navigate_to_conversation() already
                # starts its own mark-as-read thread (deferred until after
                # that focus change) for the "just opened this conversation"
                # case, so nothing is lost by skipping it here.
                if (not self._unread_sep_marked_read
                        and not getattr(self, "_populating_messages", False)):
                    self._unread_sep_marked_read = True
                    if self.conversation is not None:
                        jid = self.conversation.get("remoteJid", "")
                        if jid:
                            threading.Thread(
                                target=self.main_window.mark_conversation_as_read,
                                args=(jid,),
                                daemon=True,
                            ).start()
                # Focus has now genuinely stepped PAST the separator, into the
                # unread messages themselves, so it anchors an already-read
                # position. The row stays on screen showing the old count (the
                # official WhatsApp behaviour the user asked for: a marker of
                # where you had stopped reading), but the next live message
                # must replace it entirely (fresh separator, count reset to 1)
                # rather than bumping its count, or the count would keep
                # accumulating on top of messages already read. This is the
                # ONLY situation that justifies that reset — a separator placed
                # on conversation open still sits above genuinely unread
                # messages, so there a new message just adds to it.
                #
                # Strictly PAST, via the same predicate that decides the
                # dismissal, and not the `idx >= sep_idx` of the mark-as-read
                # above: merely landing on the separator row is where
                # populate_messages() itself parks focus on open, and Alt+3 /
                # Alt+U land there on purpose. Under the old semantics setting
                # the flag there was harmless (every rebuild set it anyway);
                # now it is the single thing choosing between "add" and "move
                # and restart at 1", so a user pressing Alt+3 just to get their
                # bearings would have watched a separator reading "3" be
                # replaced by a "1" on the next message.
                if self._should_dismiss_unread_separator(idx, self._unread_sep_idx):
                    self._sep_anchors_read_position = True

            # Keep the unread separator visible throughout navigation while the
            # conversation is open, allowing the user to jump back to it at any time
            # using Alt+U or Alt+3. It is reset only when opening/closing conversations.
            pass

        # Show audio controls only when the focused item IS the playing audio.
        if self._current_audio_id is not None and self._audio_stream is not None:
            if 0 <= idx < len(self._sorted_messages):
                m = self._sorted_messages[idx]
                if (not self._is_separator(m)
                        and m.get("key", {}).get("id") == self._current_audio_id):
                    self._show_audio_controls()
                else:
                    self._hide_audio_controls()

        # Lazy-loading: whenever focus lands on the very first row, pull in
        # the previous page. This used to be handled only in the raw
        # WXK_UP/PAGEUP/HOME key-down handler below, which only fires when the
        # user presses Up again while *already* sitting on row 0 — pressing
        # Home/PageUp/Ctrl+Home from further down jumps straight to row 0
        # without ever going through that handler, and so did a mouse click or
        # a screen reader's object-navigation landing there. Hooking the focus
        # event instead catches every way of reaching the first message, not
        # just one specific key combo pressed twice.
        if (idx == 0 and not self._is_loading_more and self._sorted_messages
                and not getattr(self, "_populating_messages", False)):
            if self._messages_offset > 0:
                self._load_more_messages()
            else:
                self._load_older_messages()

        self._update_read_more_button(idx)
        self._update_reactions_button(idx)
        event.Skip()

    def _update_reactions_button(self, idx: int):
        """Show/hide the reactions-list button for the focused message row.

        Only visible when the focused message actually has reactions —
        label states the emoji breakdown so a screen-reader user knows what
        the button does and what they'll find without opening it, e.g.
        "Reações 👍, 1 no total. 😂, 2 no total.".
        """
        msg_id = ""
        counts = {}
        if 0 <= idx < len(self._sorted_messages):
            msg = self._sorted_messages[idx]
            if not self._is_separator(msg):
                msg_id = msg.get("key", {}).get("id", "")
                if msg_id:
                    counts = self._reaction_counts(msg_id)
        if counts:
            i18n = self.main_window.i18n
            parts = [
                f"{emoji}, {count} {i18n.t('total_label')}"
                for emoji, count in counts.items()
            ]
            self._reactions_btn.SetLabel(f"{i18n.t('reactions_label')} {'. '.join(parts)}.")
            self._reactions_btn.Show()
            self._reactions_focused_msg_id = msg_id
        else:
            self._reactions_btn.Hide()
            self._reactions_focused_msg_id = ""
        self.conversation_panel.Layout()

    def _on_show_reactions(self, event):
        """Open the reactions-list dialog for the currently focused message."""
        msg_id = getattr(self, "_reactions_focused_msg_id", "")
        if not msg_id:
            return
        per_msg = self._reaction_map.get(msg_id) or {}
        if not per_msg:
            return
        from ui.dialogs.reactions_dialog import ReactionsDialog
        dlg = ReactionsDialog(self.main_window, self, per_msg)
        dlg.ShowModal()
        dlg.Destroy()

    def _update_read_more_button(self, idx: int):
        """Show/hide the "Ler mais" button for a truncated text message row.

        Only meaningful in classic wx.ListCtrl mode — SysListView32 truncates
        the accessible name of each row at _LIST_CTRL_TEXT_LIMIT characters;
        CompatListBoxMessagesCtrl exposes the full text and has no such limit.
        """
        if getattr(self, "_message_list_mode", "classic") == "listbox":
            return
        show = False
        if 0 <= idx < len(self._sorted_messages):
            msg = self._sorted_messages[idx]
            if not self._is_separator(msg):
                msg_type = msg.get("messageType", "")
                if msg_type in ("conversation", "extendedTextMessage", ""):
                    rendered = self._render_message_line(msg)
                    if len(rendered) > self._LIST_CTRL_TEXT_LIMIT:
                        self._read_more_remainder = rendered[self._LIST_CTRL_TEXT_LIMIT:]
                        show = True
        if show:
            self._read_more_btn.Show()
        else:
            self._read_more_btn.Hide()
            self._read_more_remainder = ""
        self.conversation_panel.Layout()

    def _on_read_more(self, event):
        """Alt+L / button click: speak only the text cut off by the list-view limit."""
        remainder = getattr(self, "_read_more_remainder", "")
        if remainder:
            self.main_window.output(remainder, interrupt=True)

    @staticmethod
    def _should_dismiss_unread_separator(focused_idx: int, sep_idx: int) -> bool:
        """True once focus has moved past the unread separator, into the unread
        messages themselves.

        The separator used to be removed by a one-shot 2-second timer armed the
        moment focus merely *reached* it. So it disappeared out from under a
        user who was still sitting on it — and for a screen-reader user, who may
        well take longer than two seconds to hear the row and decide what to do,
        the one marker showing where the new messages start was simply gone,
        with nothing having happened to warrant it.

        Tie it to the action it is supposed to represent instead: the separator
        is dismissed when the user actually steps down past it. Landing on the
        separator row itself is explicitly not enough — that is where
        populate_messages() parks focus when the conversation is opened, i.e.
        before the user has read anything at all.
        """
        if sep_idx < 0 or focused_idx < 0:
            return False
        return focused_idx > sep_idx

    def _dismiss_unread_separator(self):
        """Remove the unread separator row without stealing focus."""
        if self._unread_sep_idx < 0:
            return
        sep_idx = self._unread_sep_idx
        focused = self.messages_list.GetFocusedItem()
        self.messages_list.Freeze()
        try:
            self._sorted_messages.pop(sep_idx)
            self.messages_list.DeleteItem(sep_idx)
        finally:
            self.messages_list.Thaw()
        self._unread_sep_idx = -1
        self._sep_anchors_read_position = False
        self._first_unread_msg_id = None
        self._first_unread_count = 0
        # Restore the focused row (shifted by 1 if it was after the separator)
        if focused > sep_idx:
            focused -= 1
        elif focused == sep_idx:
            focused = max(0, sep_idx - 1)
        if 0 <= focused < self.messages_list.GetItemCount():
            self.messages_list.Focus(focused)

    def _deduplicate_messages(self, messages_list: list) -> list:
        seen = set()
        result = []
        for m in reversed(messages_list):
            if not isinstance(m, dict):
                result.append(m)
                continue
            mid = m.get("key", {}).get("id", "")
            if not mid:
                result.append(m)
                continue
            if mid not in seen:
                seen.add(mid)
                result.append(m)
        result.reverse()
        return result

    def _history_storage_jid(self, remote_jid: str) -> str:
        """Return the JID under which this conversation is stored locally."""
        phone_to_lid = getattr(self.main_window, "_phone_to_lid", {})
        mapped_lid = phone_to_lid.get(remote_jid, "")
        if mapped_lid:
            logging.info(
                "[_load_older_messages] Using mapped local-history JID %s for %s",
                mapped_lid,
                remote_jid,
            )
            return mapped_lid
        return remote_jid

    def _reset_expanded_window(self) -> None:
        """Volta a lista ao messages_page_size normal.

        A janela expandida pertence à conversa em que o usuário pediu o
        histórico; abrir outra (ou fechar esta) tem de recomeçar do limite
        configurado, senão o chat seguinte já nasce renderizando milhares de
        linhas por causa de uma conversa anterior.
        """
        self._expanded_visible_count = 0
        self._expanded_oldest_msg_id = ""

    def _history_window_for_rebuild(self, displayable: list, limit: int) -> tuple:
        """(offset, sep_idx) da janela que populate_messages() vai reconstruir.

        Método próprio, e não duas linhas dentro do rebuild, porque é aqui que
        o histórico que o usuário carregou à mão sobrevive: a âncora diz até
        onde a janela tem de continuar aberta (ver _remember_expanded_window()).
        populate_messages() não é testável sem um wx.ListCtrl de verdade, então
        sem isto o passo que corrige o bug ficava sem teste nenhum — dava para
        apagar o argumento da âncora com a suíte inteira verde.
        """
        return history_window(
            displayable,
            getattr(self, "_expanded_oldest_msg_id", ""),
            getattr(self, "_expanded_visible_count", 0),
            limit,
            self._unread_sep_idx,
        )

    def _remember_expanded_window(self) -> None:
        """Registra até onde a lista está materializada agora.

        A contagem é o piso e o id da mensagem mais antiga exibida é a âncora:
        cada mensagem nova aumenta o total de exibíveis, e uma janela definida
        só por contagem andaria uma linha para frente a cada chegada, comendo
        de volta justamente o histórico que o usuário pediu. Quando esse id
        some (mensagem apagada remotamente), a contagem ainda segura a janela.

        Chamado por quem carrega histórico (Home) E pelo fim de
        populate_messages(): o piso não é "o que o Home trouxe", é "o que já
        está na tela". Sem a segunda chamada, uma conversa aberta e nunca
        expandida abre com messages_page_size linhas, cresce por append a cada
        mensagem que chega ou que o usuário manda (on_incoming_message() e os
        caminhos de envio otimista apendam sem cortar nada), e o primeiro
        rebuild de fundo recalcula a janela do fim e devolve a lista a
        exatamente o page size — apagando da lista, sob o leitor de tela, uma
        linha antiga por mensagem nova. Foi assim que o bug reapareceu depois
        da âncora: com 200 linhas na tela, 4 mensagens enviadas e o repaint
        seguinte, as 4 mais antigas sumiram.

        "O que está na tela" inclui o alargamento que paginated_window() faz
        por causa do separador de não lidas, e não só o histórico que o usuário
        puxou: um grupo com 4000 não lidas abre com 4001 linhas, e essas 4001
        viram o piso da sessão inteira — o separador ser descartado depois não
        as estreita. É de propósito. Apará-las é apagar da lista, sob o leitor
        de tela, exatamente as mensagens que ele acabou de ler, que é o bug.
        Quem for medir custo de rebuild em conversa nunca expandida precisa
        saber que essa janela larga é esperada, não defeito.

        Uma lista só com sentinela (o placeholder de "sem mensagens") não
        registra nada: um piso de 1 linha não significa nada e a âncora vazia
        não prenderia coisa alguma. Também não zera o que já estava gravado, e
        isso é escolha, não esquecimento: "Limpar conversa" esvazia records e
        cai aqui, mas um records momentaneamente vazio no meio de um resync
        cairia igual, e zerar ali derrubaria um piso legítimo — de novo linhas
        sumindo sob o leitor. O custo de manter é o oposto e é suportável: se o
        chat limpo voltar a encher na mesma sessão, o rebuild seguinte pinta
        uma janela mais larga que o page size até a conversa ser fechada.
        """
        oldest_id = ""
        found_real_row = False
        for msg in self._sorted_messages:
            if isinstance(msg, dict) and not self._is_separator(msg):
                found_real_row = True
                # A primeira linha COM id, não simplesmente a primeira linha:
                # populate_messages() mantém registros sem key.id, e parar no
                # primeiro deles deixava a âncora vazia para sempre naquela
                # conversa. Só o piso por contagem sobraria — e ele escorrega
                # uma linha para frente a cada mensagem que chega ao vivo
                # (on_incoming_message() não registra a janela), reintroduzindo
                # em silêncio o mesmo sintoma que esta janela existe para
                # corrigir. Pegar uma linha mais nova como âncora só pode
                # alargar a janela, nunca estreitá-la: expanded_min_visible()
                # aplica max(piso, âncora).
                mid = (msg.get("key") or {}).get("id", "") or ""
                if mid:
                    oldest_id = mid
                    break
        if not found_real_row:
            return
        self._expanded_visible_count = len(self._sorted_messages)
        self._expanded_oldest_msg_id = oldest_id

    def _merge_history_into_records(self, older_messages: list) -> None:
        """Guarda no chat aberto o histórico recém-carregado.

        O prepend feito por _load_older_messages()/_on_older_messages_loaded()
        só vive nas listas em memória do painel, mas populate_messages()
        reconstrói a conversa inteira a partir de
        conversation["messages"]["messages"]["records"] — então um rebuild de
        fundo redesenhava a conversa sem essas mensagens. É o mesmo merge que
        MainWindow.fetch_older_messages() faz do lado do servidor, repetido
        aqui porque self.conversation nem sempre é o mesmo dict que
        main_window.chats[jid] (o resync da conversa aberta troca o dict), e é
        idempotente: dedup por key.id, sem reordenar o que já existe
        (populate_messages() ordena por timestamp de qualquer forma).

        Só entra o que pertence mesmo à conversa aberta: a consulta local usa
        _history_storage_jid() (que pode ser um @lid) e o fallback do servidor
        reconsulta sob o JID alternativo, então um mapeamento @lid errado ou
        velho traz mensagens de outro contato. Antes elas sumiam no rebuild
        seguinte; guardadas nos records elas viram história permanente daquele
        contato — sync_chat_messages() recolhe os records locais sem filtrar.
        """
        if not self.conversation or not older_messages:
            return
        try:
            jid = self.conversation.get("remoteJid", "")
            own = [
                m for m in older_messages
                if isinstance(m, dict) and self.main_window._chat_jids_equivalent(
                    (m.get("key") or {}).get("remoteJid", ""), jid
                )
            ]
            if len(own) != len(older_messages):
                logging.warning(
                    "[_merge_history_into_records] %d de %d mensagens descartadas "
                    "por não pertencerem a %s",
                    len(older_messages) - len(own), len(older_messages), jid,
                )
            if not own:
                return
            container = self.conversation.setdefault("messages", {}).setdefault(
                "messages", {}
            )
            records = container.get("records") or []
            existing_ids = {
                (r.get("key") or {}).get("id")
                for r in records
                if isinstance(r, dict) and (r.get("key") or {}).get("id")
            }
            # Mensagem sem key.id fica de fora: um "" nos records faz
            # _signature_changed_ids() devolver None para sempre, e aí todo
            # repaint local (estrela, fixar) vira rebuild completo — sai mais
            # caro do que perder uma linha que nem dá para endereçar.
            new_records = [
                m for m in own
                if (m.get("key") or {}).get("id")
                and (m.get("key") or {}).get("id") not in existing_ids
            ]
            if not new_records:
                return
            container["records"] = new_records + records
            # "total" pode ser a contagem real do chat (db.get_message_count()),
            # maior do que o que está carregado, então aqui só sobe. Vale até o
            # próximo sync_chat_messages(), que reescreve com len(all_messages).
            container["total"] = max(
                int(container.get("total") or 0), len(container["records"])
            )
            logging.info(
                "[_merge_history_into_records] %d mensagem(ns) antigas guardadas "
                "nos records (total agora %d)",
                len(new_records), container["total"],
            )
        except Exception:
            logging.exception(
                "[_merge_history_into_records] falha ao mesclar o histórico carregado"
            )

    def _load_older_messages(self):
        """Load older messages from the local database, or fall back to the server if none remain locally."""
        if not self.conversation or not self._all_sorted_messages:
            logging.info(f"[_load_older_messages] Aborting: conversation={self.conversation is not None}, all_sorted={len(self._all_sorted_messages) if self._all_sorted_messages else 0}")
            return

        self._is_loading_more = True
        try:
            remote_jid = self.conversation.get("remoteJid", "")
            limit = int(
                self.main_window.settings.get("user_interface", {}).get("messages_page_size", 200)
            )
            # Count separator objects to get the actual database message count currently in memory.
            loaded_db_count = sum(1 for m in self._all_sorted_messages if not self._is_separator(m))
            storage_jid = self._history_storage_jid(remote_jid)
            logging.info(f"[_load_older_messages] Querying local DB for {storage_jid} with count={loaded_db_count}")
            
            # Fetch from local DB
            local_msgs = self.main_window.db.get_messages(storage_jid, limit=limit, offset=loaded_db_count)
            logging.info(f"[_load_older_messages] Local DB returned {len(local_msgs) if local_msgs else 0} messages")
            
            if local_msgs:
                # We found older messages in the local DB!
                # Reverse them so they are in ascending chronological order (older first)
                local_msgs.reverse()
                displayable = [m for m in local_msgs if self._is_displayable_message(m)]
                logging.info(f"[_load_older_messages] Displayable local messages count: {len(displayable)}")
                if displayable:
                    oldest_in_mem = self._all_sorted_messages[0] if self._all_sorted_messages else None
                    oldest_in_mem_id = oldest_in_mem.get("key", {}).get("id") if oldest_in_mem else "None"
                    oldest_in_mem_ts = oldest_in_mem.get("timestamp") if oldest_in_mem else "None"
                    logging.info(f"[_load_older_messages] Oldest in memory: id={oldest_in_mem_id}, ts={oldest_in_mem_ts}")
                    logging.info(f"[_load_older_messages] Returned oldest from DB: id={displayable[0].get('key', {}).get('id')}, ts={displayable[0].get('timestamp')}")
                    logging.info(f"[_load_older_messages] Returned newest from DB: id={displayable[-1].get('key', {}).get('id')}, ts={displayable[-1].get('timestamp')}")
                    self.messages_list.Freeze()
                    try:
                        old_count = len(self._sorted_messages)
                        self._all_sorted_messages = self._deduplicate_messages(displayable + self._all_sorted_messages)
                        self._sorted_messages     = self._deduplicate_messages(displayable + self._sorted_messages)
                        self._messages_offset     = 0
                        n_new = len(self._sorted_messages) - old_count
                        logging.info(f"[_load_older_messages] Prepend finished. Added {n_new} new unique messages. Rebuilding UI list.")
                        
                        if n_new > 0:
                            self._merge_history_into_records(displayable)
                            self._remember_expanded_window()
                            self._recompute_unread_sep_idx()
                                
                            self.messages_list.DeleteAllItems()
                            for msg in self._sorted_messages:
                                self.messages_list.Append((self._render_message_line(msg),))
                                
                            self.messages_list.Focus(n_new)
                            self.messages_list.Select(n_new, True)
                            self.messages_list.EnsureVisible(n_new)
                            self._is_loading_more = False
                            return
                        else:
                            logging.info("[_load_older_messages] No new unique messages found in local DB chunk. Falling through to server fetch.")
                    finally:
                        self.messages_list.Thaw()
            
            # No older messages in local DB, fetch from server
            self._load_older_messages_from_server()
        except Exception as e:
            logging.exception(f"[_load_older_messages] error: {e}")
            self._is_loading_more = False

    def _load_older_messages_from_server(self):
        """Fetch older messages from server when the beginning of local history is reached."""
        if not self.conversation or not self._all_sorted_messages:
            logging.info(f"[_load_older_messages_from_server] Aborting: conversation={self.conversation is not None}, all_sorted={len(self._all_sorted_messages) if self._all_sorted_messages else 0}")
            self._is_loading_more = False
            return
        
        phone_jid = self.conversation.get("remoteJid", "")
        reached_start = phone_jid in getattr(self, "_reached_server_start", {})
        logging.info(f"[_load_older_messages_from_server] phone_jid={phone_jid}, reached_start={reached_start}")
        if phone_jid and reached_start:
            self._is_loading_more = False
            return
        
        # A duplicate-only page still advances the server cursor. Prefer that
        # cursor on the next attempt so we do not request the same 200 rows.
        oldest_msg = self._server_history_anchor.get(phone_jid)
        if oldest_msg is None:
            # Get oldest non-separator and non-pending message ID
            for m in self._all_sorted_messages:
                if m.get("_type") == "unread_separator":
                    continue
                m_id = m.get("key", {}).get("id", "")
                # Skip local pending/virtual messages (UUIDs contain hyphens or start with 'pending-')
                if m.get("_local_pending") or m_id.startswith("pending-") or "-" in m_id:
                    continue
                oldest_msg = m
                break

        if oldest_msg is None:
            # Fallback to the first message if all are pending/separators
            oldest_msg = self._all_sorted_messages[0]

        oldest_id = oldest_msg.get("key", {}).get("id", "")
        logging.info(f"[_load_older_messages_from_server] oldest_id={oldest_id}")
        if not oldest_id:
            self._is_loading_more = False
            return

        self._is_loading_more = True
        
        def _fetch():
            phone_jid_val = self.conversation.get("remoteJid", "") if self.conversation else ""
            try:
                logging.info(f"[_load_older_messages_from_server thread] Launching fetch_older_messages for {phone_jid_val}")
                fetched = self.main_window.fetch_older_messages(phone_jid_val, oldest_msg)
                logging.info(f"[_load_older_messages_from_server thread] fetch_older_messages returned {len(fetched) if fetched is not None else 'None'}")
                if fetched is None:
                    fetched = self.main_window.wait_for_older_messages(
                        phone_jid_val,
                        oldest_msg,
                        should_continue=lambda: bool(
                            self.conversation
                            and self.conversation.get("remoteJid") == phone_jid_val
                        ),
                    )
                    logging.info(
                        "[_load_older_messages_from_server thread] "
                        "wait_for_older_messages returned %s",
                        len(fetched) if fetched is not None else "None")
                if fetched is not None:
                    if fetched:
                        wx.CallAfter(self._on_older_messages_loaded, fetched, phone_jid_val, oldest_msg)
                    else:
                        wx.CallAfter(self._set_reached_start, phone_jid_val)
                else:
                    wx.CallAfter(self._clear_loading_more, phone_jid_val)
            except Exception as e:
                logging.exception(f"[_load_older_messages_from_server] thread error: {e}")
                wx.CallAfter(self._clear_loading_more, phone_jid_val)

        threading.Thread(target=_fetch, daemon=True).start()

    def _set_reached_start(self, requested_jid):
        if not self.conversation or self.conversation.get("remoteJid") != requested_jid:
            logging.info(f"[_set_reached_start] Ignoring call for {requested_jid} (current active: {self.conversation.get('remoteJid') if self.conversation else 'None'})")
            return
        self._reached_server_start[requested_jid] = True
        self._is_loading_more = False

    def _clear_loading_more(self, requested_jid):
        if not self.conversation or self.conversation.get("remoteJid") != requested_jid:
            logging.info(f"[_clear_loading_more] Ignoring call for {requested_jid} (current active: {self.conversation.get('remoteJid') if self.conversation else 'None'})")
            return
        self._is_loading_more = False

    def _on_older_messages_loaded(self, fetched_messages, requested_jid, requested_anchor=None):
        """Prepend fetched history to UI message list."""
        if not self.conversation or self.conversation.get("remoteJid") != requested_jid:
            logging.info(f"[_on_older_messages_loaded] Ignoring fetched messages for {requested_jid} (current active: {self.conversation.get('remoteJid') if self.conversation else 'None'})")
            return
        self._is_loading_more = False
        if not fetched_messages:
            return
            
        # Reverse to oldest-first order (ascending chronological) to match client storage
        fetched_messages.reverse()
            
        displayable = [
            m for m in fetched_messages if self._is_displayable_message(m)
        ]
        if not displayable:
            return
            
        # Sort displayable older messages
        try:
            displayable = sorted(
                displayable, key=lambda m: self._extract_timestamp(m) or 0
            )
        except Exception:
            pass
            
        self.messages_list.Freeze()
        try:
            old_count = len(self._sorted_messages)
            self._all_sorted_messages = self._deduplicate_messages(displayable + self._all_sorted_messages)
            self._sorted_messages     = self._deduplicate_messages(displayable + self._sorted_messages)
            self._messages_offset     = 0
            n_new = len(self._sorted_messages) - old_count
            logging.info(f"[_on_older_messages_loaded] n_new={n_new}, displayable_count={len(displayable)}, total={len(self._sorted_messages)}")
            
            if n_new == 0:
                # Overlap is not proof of the beginning. LID/phone aliases can
                # make a valid older page look entirely duplicated locally.
                # Advance to the oldest row returned and keep the chat retryable.
                phone_jid_val = self.conversation.get("remoteJid", "") if self.conversation else ""
                next_anchor = displayable[0]
                previous_id = (requested_anchor or {}).get("key", {}).get("id", "")
                next_id = next_anchor.get("key", {}).get("id", "")
                if phone_jid_val and next_id and next_id != previous_id:
                    self._server_history_anchor[phone_jid_val] = next_anchor
                    logging.info(
                        "[_on_older_messages_loaded] Duplicate page advanced anchor %s -> %s; keeping history retryable",
                        previous_id,
                        next_id,
                    )
                else:
                    logging.warning(
                        "[_on_older_messages_loaded] Duplicate page did not advance anchor for %s; not treating overlap as server start",
                        phone_jid_val,
                    )
                return

            self._server_history_anchor.pop(requested_jid, None)

            self._merge_history_into_records(displayable)
            self._remember_expanded_window()
            
            self._recompute_unread_sep_idx()

            self.messages_list.DeleteAllItems()
            for msg in self._sorted_messages:
                self.messages_list.Append((self._render_message_line(msg),))
                
            self.messages_list.Focus(n_new)
            self.messages_list.Select(n_new, True)
            self.messages_list.EnsureVisible(n_new)
        finally:
            self.messages_list.Thaw()


    def _load_more_messages(self):
        """Prepend the previous page of messages to the list."""
        self._is_loading_more = True
        self.messages_list.Freeze()
        try:
            limit = int(
                self.main_window.settings.get("user_interface", {}).get("messages_page_size", 200)
            )
            new_start = max(0, self._messages_offset - limit)
            new_msgs  = self._all_sorted_messages[new_start:self._messages_offset]
            if not new_msgs:
                return

            n_new = len(new_msgs)

            # Extend the in-memory list and update the offset
            self._sorted_messages   = new_msgs + self._sorted_messages
            self._messages_offset   = new_start
            self._remember_expanded_window()
            if self._unread_sep_idx >= 0:
                self._unread_sep_idx += n_new

            # Rebuild the wx.ListCtrl from the updated _sorted_messages
            self.messages_list.DeleteAllItems()
            for msg in self._sorted_messages:
                self.messages_list.Append((self._render_message_line(msg),))

            # Keep the previously-first item in view (now at index n_new)
            self.messages_list.Focus(n_new)
            self.messages_list.Select(n_new, True)
            self.messages_list.EnsureVisible(n_new)
        finally:
            self.messages_list.Thaw()
            self._is_loading_more = False

    # ── Keyboard Space-as-activate helpers ──────────────────────────────────

    # Default for how many rows Page Up/Page Down jump in messages_list/
    # conversations_list, when Settings > User Interface hasn't set one yet.
    # Both are plain wx.ListCtrl (native SysListView32 on Windows), whose
    # default Page Up/Down handling only moved focus by a single row instead
    # of paging — reported as making them functionally indistinguishable
    # from Up/Down. 15 mirrors the default messages_page_size fetched per
    # sync page (see client/data/settings_default.json's user_interface
    # section) as a reasonable "one screenful" default.
    _DEFAULT_PAGE_JUMP_SIZE = 15

    def _page_jump_size(self) -> int:
        """User-configurable Page Up/Page Down jump size (Settings > User
        Interface). Falls back to the default for a missing/invalid value —
        settings_dialog.py validates on save, but settings.json can still be
        hand-edited or predate this option."""
        raw = self.main_window.settings.get("user_interface", {}).get(
            "page_jump_size", self._DEFAULT_PAGE_JUMP_SIZE
        )
        try:
            size = int(raw)
        except (TypeError, ValueError):
            return self._DEFAULT_PAGE_JUMP_SIZE
        return size if size >= 1 else self._DEFAULT_PAGE_JUMP_SIZE

    @staticmethod
    def _page_jump_target(count: int, focused_idx: int, delta: int) -> int:
        """Clamped index for a Page Up/Down jump, or -1 when the list is empty."""
        if count <= 0:
            return -1
        idx = focused_idx if focused_idx >= 0 else 0
        return max(0, min(count - 1, idx + delta))

    def _jump_list_by(self, list_ctrl: wx.ListCtrl, delta: int) -> None:
        target = self._page_jump_target(list_ctrl.GetItemCount(), list_ctrl.GetFocusedItem(), delta)
        if target < 0:
            return
        list_ctrl.Focus(target)
        list_ctrl.Select(target, True)
        list_ctrl.EnsureVisible(target)

    # ── Selection helpers (messages list) ───────────────────────────────────

    def _toggle_message_selection(self, msg: dict) -> None:
        """Toggle *msg*'s membership in self.selected_messages, refresh its
        row, and play/announce the change. Shared by Ctrl+Space
        (_on_messages_list_key_down) and the "Selecionar mensagem"/
        "Desselecionar mensagem" context menu item."""
        if self._is_separator(msg):
            return
        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id:
            return
        if msg_id in self.selected_messages:
            self.selected_messages.remove(msg_id)
            self._refresh_message_rows_by_ids([msg_id])
            self.main_window.output(self.main_window.i18n.t("unselected"), interrupt=True)
        else:
            self.selected_messages.add(msg_id)
            self._refresh_message_rows_by_ids([msg_id])
            self.selection_sound.play()
            self.main_window.output(self.main_window.i18n.t("selected"), interrupt=True)

    def _select_message_at(self, idx: int) -> bool:
        """Add the message at *idx* to self.selected_messages, if it's a real
        (non-separator) message with an id. Returns whether it was added."""
        if not (0 <= idx < len(self._sorted_messages)):
            return False
        msg = self._sorted_messages[idx]
        if self._is_separator(msg):
            return False
        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id or msg_id in self.selected_messages:
            return False
        self.selected_messages.add(msg_id)
        return True

    def _all_selectable_message_ids(self) -> list:
        return [
            msg_id
            for m in self._sorted_messages
            if not self._is_separator(m)
            for msg_id in [m.get("key", {}).get("id", "")]
            if msg_id
        ]

    def _refresh_message_rows_by_ids(self, msg_ids) -> None:
        """Re-render specific rows (by message id) so the " selecionado"
        marker _render_message_line() adds stays in sync after a selection
        change — SetItemText only, no list rebuild/focus disruption."""
        if not msg_ids:
            return
        self._set_message_row_texts(set(msg_ids))

    def _set_message_row_texts(self, ids: set) -> set:
        """Re-render every rendered row whose message id is in *ids*, and
        return the ids that were actually found on screen.

        The bare mechanism, shared by _refresh_message_rows_by_ids() (selection
        markers, which never care whether a row is missing) and
        _repaint_message_rows() (local flag changes, which fall back to a full
        rebuild when one is).
        """
        found = set()
        total = len(self._sorted_messages)
        for i, m in enumerate(self._sorted_messages):
            if self._is_separator(m):
                continue
            mid = m.get("key", {}).get("id", "")
            if mid in ids:
                self.messages_list.SetItemText(i, self._render_message_line(m, index=i, total=total))
                found.add(mid)
        return found

    def _on_messages_list_key_down(self, event):
        """Ctrl+Space toggles the focused row's membership in
        self.selected_messages (the mass actions act on that set) — kept off
        plain Space, which is reserved for playing/pausing the focused audio
        or video message. Shift+Down/Shift+Up extend the selection to the
        next/previous row; Shift+Home/Shift+End select every row above/below the focused one and
        move focus to the first/last row (falling back to their previous
        meaning — seeking the active playback to its start/end — whenever
        something actually is playing); Ctrl+Shift+Space selects every
        message, or clears the selection if everything is already selected.
        Activation stayed on Enter / double-click — see _do_activate_message.
        Page Up / Page Down jump by a configurable number of messages (page_up_down_step setting).
        Trigger loading older messages on Arrow Up / Page Up when at the top (index 0)."""
        key = event.GetKeyCode()
        ctrl = event.ControlDown()
        shift = event.ShiftDown()
        idx = self.messages_list.GetFocusedItem()
        total = self.messages_list.GetItemCount()
        logging.info(f"[_on_messages_list_key_down] Key down: {key}, idx: {idx}, is_loading_more: {self._is_loading_more}, offset: {self._messages_offset}")

        if ctrl and not shift and key == ord("V"):
            if self._paste_from_messages_list():
                return

        ui_cfg = self.main_window.settings.get("user_interface", {})
        raw_step = ui_cfg.get("page_jump_size", ui_cfg.get("page_up_down_step", 15))
        try:
            step = max(1, int(raw_step))
        except (ValueError, TypeError):
            step = 15

        # Shift+arrow/PageUp/PageDown seek the currently playing voice
        # message or video instead of moving list focus (issue #17). Checked
        # before the plain (unmodified) equivalents below, since PageUp/
        # PageDown already have their own unmodified meaning here (jump N
        # messages / load older history).
        if shift and key in (
            wx.WXK_LEFT, wx.WXK_NUMPAD_LEFT, wx.WXK_RIGHT, wx.WXK_NUMPAD_RIGHT,
            wx.WXK_PAGEUP, wx.WXK_NUMPAD_PAGEUP, wx.WXK_PAGEDOWN, wx.WXK_NUMPAD_PAGEDOWN,
        ):
            if key in (wx.WXK_LEFT, wx.WXK_NUMPAD_LEFT):
                self.seek_active_playback_by(-5)
            elif key in (wx.WXK_RIGHT, wx.WXK_NUMPAD_RIGHT):
                self.seek_active_playback_by(5)
            elif key in (wx.WXK_PAGEUP, wx.WXK_NUMPAD_PAGEUP):
                self.seek_active_playback_by(-60)
            elif key in (wx.WXK_PAGEDOWN, wx.WXK_NUMPAD_PAGEDOWN):
                self.seek_active_playback_by(60)
            return

        # Shift+Home/Shift+End: seek to the very start/end of the active
        # playback when something is actually playing (issue #17) — otherwise
        # select every message above/below the focused row and move focus to
        # the first/last row.
        if shift and key in (wx.WXK_HOME, wx.WXK_NUMPAD_HOME, wx.WXK_END, wx.WXK_NUMPAD_END):
            to_end = key in (wx.WXK_END, wx.WXK_NUMPAD_END)
            if self.seek_active_playback_to_edge(to_end=to_end):
                return
            if total > 0:
                idx0 = idx if idx >= 0 else 0
                lo, hi = (idx0, total - 1) if to_end else (0, idx0)
                newly_selected = []
                for i in range(lo, hi + 1):
                    if self._select_message_at(i):
                        newly_selected.append(self._sorted_messages[i].get("key", {}).get("id", ""))
                target = total - 1 if to_end else 0
                self.messages_list.Focus(target)
                self.messages_list.Select(target, True)
                self.messages_list.EnsureVisible(target)
                if newly_selected:
                    self._refresh_message_rows_by_ids(newly_selected)
                    self.selection_sound.play()
                    self.main_window.output(self.main_window.i18n.t("selected"), interrupt=True)
            return

        # Shift+Down: extend the selection to the next row and move focus to it.
        if shift and key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN):
            target = (idx + 1) if idx >= 0 else 0
            if target < total:
                self.messages_list.Focus(target)
                self.messages_list.Select(target, True)
                self.messages_list.EnsureVisible(target)
                if self._select_message_at(target):
                    self._refresh_message_rows_by_ids([self._sorted_messages[target].get("key", {}).get("id", "")])
                    self.selection_sound.play()
                    self.main_window.output(self.main_window.i18n.t("selected"), interrupt=True)
            return

        # Shift+Up: extend the selection to the previous row and move focus to
        # it — the upward mirror of Shift+Down above. Without this, Shift+Up
        # fell through to the plain WXK_UP branch further down (which ignores
        # `shift` and calls event.Skip()), leaving the native ListCtrl
        # selection to extend on its own. That never touches
        # self.selected_messages (what the mass actions actually act on) and
        # doesn't know the unread-separator row isn't a selectable message,
        # so selecting upward past it went out of sync with the visible
        # highlight.
        if shift and key in (wx.WXK_UP, wx.WXK_NUMPAD_UP):
            target = (idx - 1) if idx >= 0 else 0
            if target >= 0:
                self.messages_list.Focus(target)
                self.messages_list.Select(target, True)
                self.messages_list.EnsureVisible(target)
                if self._select_message_at(target):
                    self._refresh_message_rows_by_ids([self._sorted_messages[target].get("key", {}).get("id", "")])
                    self.selection_sound.play()
                    self.main_window.output(self.main_window.i18n.t("selected"), interrupt=True)
            return

        # Ctrl+Shift+Space: select every message, or clear the selection if
        # everything selectable is already selected.
        if ctrl and shift and key == wx.WXK_SPACE:
            all_ids = self._all_selectable_message_ids()
            if all_ids and all(mid in self.selected_messages for mid in all_ids):
                self.selected_messages.clear()
                self._refresh_message_rows_by_ids(all_ids)
                self.main_window.output(self.main_window.i18n.t("all_unselected"), interrupt=True)
            elif all_ids:
                self.selected_messages.update(all_ids)
                self._refresh_message_rows_by_ids(all_ids)
                self.selection_sound.play()
                self.main_window.output(self.main_window.i18n.t("all_selected"), interrupt=True)
            return

        if ctrl and not shift and key == wx.WXK_SPACE:
            if idx >= 0 and idx < len(self._sorted_messages):
                self._toggle_message_selection(self._sorted_messages[idx])
        elif key in (wx.WXK_PAGEUP, wx.WXK_NUMPAD_PAGEUP):
            if idx <= 0 and not self._is_loading_more:
                if self._messages_offset > 0:
                    self._load_more_messages()
                else:
                    self._load_older_messages()
            elif total > 0 and idx > 0:
                target_idx = max(0, idx - step)
                self.messages_list.Focus(target_idx)
                self.messages_list.Select(target_idx, True)
                self.messages_list.EnsureVisible(target_idx)
        elif key in (wx.WXK_PAGEDOWN, wx.WXK_NUMPAD_PAGEDOWN):
            if total > 0 and idx >= 0:
                target_idx = min(total - 1, idx + step)
                self.messages_list.Focus(target_idx)
                self.messages_list.Select(target_idx, True)
                self.messages_list.EnsureVisible(target_idx)
        elif key in (wx.WXK_UP, wx.WXK_NUMPAD_UP, wx.WXK_HOME):
            if idx <= 0 and not self._is_loading_more:
                if self._messages_offset > 0:
                    self._load_more_messages()
                else:
                    self._load_older_messages()
            else:
                event.Skip()
        else:
            event.Skip()


    # ── Selection helpers (conversations list) ──────────────────────────────

    def _select_chat_at(self, idx: int) -> bool:
        """Add the chat at *idx* to self.selected_chats. Returns whether it
        was added (i.e. wasn't already selected)."""
        if not (0 <= idx < len(self.chats_list)):
            return False
        jid = self.chats_list[idx].get("remoteJid", "")
        if not jid or jid in self.selected_chats:
            return False
        self.selected_chats.add(jid)
        return True

    def _all_chat_jids(self) -> list:
        return [c.get("remoteJid", "") for c in self.chats_list if c.get("remoteJid", "")]

    def _bulk_shortcuts_enabled(self) -> bool:
        """Settings > User Interface > "Substituir atalhos por ações em massa
        ao selecionar conversas e mensagens" (default on). When enabled and a
        selection exists, the single-item shortcuts (forward, save, clear,
        delete, ...) act on the whole selection instead."""
        return self.main_window.settings.get("user_interface", {}).get(
            "bulk_action_shortcuts", True
        )

    def _on_conv_list_key_down(self, event):
        """Ctrl+Space toggles the focused chat's membership in
        self.selected_chats (the mass actions act on that set) — kept off
        plain Space for consistency with the messages list. Shift+Up/Down
        extend the selection to the previous/next row; Shift+Home/Shift+End select
        every row above/below the focused one and move focus to the
        first/last row; Ctrl+Shift+Space selects every chat, or clears the
        selection if everything is already selected. Opening a conversation
        stayed on Enter / double-click.
        Ctrl+P pins/unpins, Ctrl+Shift+Q archives/unarchives."""
        key   = event.GetKeyCode()
        ctrl  = event.ControlDown()
        shift = event.ShiftDown()
        idx   = self.conversations_list.GetFocusedItem()
        total = len(self.chats_list)

        if shift and key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN):
            target = (idx + 1) if idx >= 0 else 0
            if target < total:
                self.conversations_list.Focus(target)
                self.conversations_list.Select(target, True)
                self.conversations_list.EnsureVisible(target)
                if self._select_chat_at(target):
                    self.main_window.add_chats_to_ui()
                    self.selection_sound.play()
                    self.main_window.output(self.main_window.i18n.t("selected"), interrupt=True)
            return

        if shift and key in (wx.WXK_UP, wx.WXK_NUMPAD_UP):
            target = (idx - 1) if idx >= 0 else 0
            if target >= 0:
                self.conversations_list.Focus(target)
                self.conversations_list.Select(target, True)
                self.conversations_list.EnsureVisible(target)
                if self._select_chat_at(target):
                    self.main_window.add_chats_to_ui()
                    self.selection_sound.play()
                    self.main_window.output(
                        self.main_window.i18n.t("selected"), interrupt=True)
            return

        if shift and key in (wx.WXK_HOME, wx.WXK_NUMPAD_HOME, wx.WXK_END, wx.WXK_NUMPAD_END):
            to_end = key in (wx.WXK_END, wx.WXK_NUMPAD_END)
            if total > 0:
                idx0 = idx if idx >= 0 else 0
                lo, hi = (idx0, total - 1) if to_end else (0, idx0)
                selected_any = False
                for i in range(lo, hi + 1):
                    if self._select_chat_at(i):
                        selected_any = True
                target = total - 1 if to_end else 0
                self.conversations_list.Focus(target)
                self.conversations_list.Select(target, True)
                self.conversations_list.EnsureVisible(target)
                if selected_any:
                    self.main_window.add_chats_to_ui()
                    self.selection_sound.play()
                    self.main_window.output(self.main_window.i18n.t("selected"), interrupt=True)
            return

        if ctrl and shift and key == wx.WXK_SPACE:
            all_jids = self._all_chat_jids()
            if all_jids and all(j in self.selected_chats for j in all_jids):
                self.selected_chats.clear()
                self.main_window.add_chats_to_ui()
                self.main_window.output(self.main_window.i18n.t("all_unselected"), interrupt=True)
            elif all_jids:
                self.selected_chats.update(all_jids)
                self.main_window.add_chats_to_ui()
                self.selection_sound.play()
                self.main_window.output(self.main_window.i18n.t("all_selected"), interrupt=True)
            return

        if ctrl and not shift and key == wx.WXK_SPACE:
            if idx >= 0 and idx < len(self.chats_list):
                chat = self.chats_list[idx]
                jid = chat.get("remoteJid", "")
                if jid:
                    if jid in self.selected_chats:
                        self.selected_chats.remove(jid)
                        self.main_window.add_chats_to_ui()
                        self.main_window.output(self.main_window.i18n.t("unselected"), interrupt=True)
                    else:
                        self.selected_chats.add(jid)
                        self.main_window.add_chats_to_ui()
                        self.selection_sound.play()
                        self.main_window.output(self.main_window.i18n.t("selected"), interrupt=True)
        elif key in (wx.WXK_PAGEUP, wx.WXK_NUMPAD_PAGEUP):
            self._jump_list_by(self.conversations_list, -self._page_jump_size())
        elif key in (wx.WXK_PAGEDOWN, wx.WXK_NUMPAD_PAGEDOWN):
            self._jump_list_by(self.conversations_list, self._page_jump_size())
        elif ctrl and key == ord("P"):
            idx = self.conversations_list.GetFocusedItem()
            if 0 <= idx < len(self.chats_list):
                jid = self.chats_list[idx].get("remoteJid", "")
                if jid:
                    if self.main_window.is_chat_pinned(jid):
                        self._on_menu_unpin(jid)
                    else:
                        self._on_menu_pin(jid)
        # Ctrl+Shift+Q, not plain Ctrl+Q — archiving isn't reversible from a
        # single accidental keystroke the way pinning is, and plain Ctrl+Q
        # sits right next to other single-Ctrl combos a user can easily
        # fat-finger while just navigating the list.
        elif ctrl and shift and key == ord("Q"):
            idx = self.conversations_list.GetFocusedItem()
            if 0 <= idx < len(self.chats_list):
                jid = self.chats_list[idx].get("remoteJid", "")
                if jid:
                    if self.main_window.is_chat_archived(jid):
                        self._on_menu_unarchive(jid)
                    else:
                        self._on_menu_archive(jid)
        else:
            event.Skip()

    def _try_show_thumbnail(self, jpeg_b64: str):
        """Decode and display an inline JPEG thumbnail (base64-encoded)."""
        if not jpeg_b64:
            return
        try:
            jpeg_data = _b64.b64decode(jpeg_b64)
            stream    = wx.MemoryInputStream(jpeg_data)
            image     = wx.Image(stream, wx.BITMAP_TYPE_JPEG)
            if not image.IsOk():
                return
            w, h = image.GetWidth(), image.GetHeight()
            max_side = 200
            if w > max_side or h > max_side:
                ratio = min(max_side / w, max_side / h)
                image = image.Scale(
                    int(w * ratio), int(h * ratio), wx.IMAGE_QUALITY_HIGH
                )
            self._media_bitmap.SetMinSize((-1, -1))
            self._media_bitmap.SetBitmap(wx.Bitmap(image))
            self._media_bitmap.Show()
            self.conversation_panel.Layout()
        except Exception:
            pass

    def _show_reply_buttons(self, buttons: list, remote_jid: str):
        """Render interactive message buttons (buttonsMessage) in the container."""
        self._buttons_container.DestroyChildren()
        sizer = wx.WrapSizer(wx.HORIZONTAL)
        for btn_data in buttons:
            if not isinstance(btn_data, dict):
                continue
            label = (btn_data.get("buttonText") or {}).get("displayText", "").strip()
            if not label:
                continue
            btn = wx.Button(self._buttons_container, label=label)
            btn.Bind(
                wx.EVT_BUTTON,
                lambda e, d=btn_data, jid=remote_jid: self._on_reply_button(d, jid),
            )
            sizer.Add(btn, 0, wx.ALL, 4)
        self._buttons_container.SetSizer(sizer, True)
        self._buttons_container.Layout()
        self._buttons_container.Show()
        self.conversation_panel.Layout()

    def _show_list_rows(self, rows: list, remote_jid: str):
        """Render list-message rows as reply buttons."""
        self._buttons_container.DestroyChildren()
        sizer = wx.WrapSizer(wx.HORIZONTAL)
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = row.get("title", "").strip()
            if not label:
                continue
            btn = wx.Button(self._buttons_container, label=label)
            btn.Bind(
                wx.EVT_BUTTON,
                lambda e, r=row, jid=remote_jid: self._on_list_row_selected(r, jid),
            )
            sizer.Add(btn, 0, wx.ALL, 4)
        self._buttons_container.SetSizer(sizer, True)
        self._buttons_container.Layout()
        self._buttons_container.Show()
        self.conversation_panel.Layout()

    def _on_reply_button(self, btn_data: dict, remote_jid: str):
        label = (btn_data.get("buttonText") or {}).get("displayText", "").strip()
        if not label or not remote_jid:
            return
        threading.Thread(
            target=self.main_window.send_text_message,
            args=(remote_jid, label),
            daemon=True,
        ).start()

    def _on_list_row_selected(self, row: dict, remote_jid: str):
        label = row.get("title", "").strip()
        if not label or not remote_jid:
            return
        threading.Thread(
            target=self.main_window.send_text_message,
            args=(remote_jid, label),
            daemon=True,
        ).start()

    def _open_file_safely(self, filepath: str):
        """Open a file with the default associated program in the foreground.
        Falls back to Windows 'openas' dialog if no program is associated."""
        import sys
        import os
        import ctypes
        if sys.platform == "win32":
            try:
                # SW_SHOW = 5
                res = ctypes.windll.shell32.ShellExecuteW(None, "open", filepath, None, None, 5)
                # ShellExecuteW returns <= 32 if failed
                if res <= 32:
                    if res == 31:  # SE_ERR_NOASSOC
                        ctypes.windll.shell32.ShellExecuteW(None, "openas", filepath, None, None, 5)
                    else:
                        raise OSError(f"ShellExecuteW failed with code {res}")
            except Exception:
                try:
                    ctypes.windll.shell32.ShellExecuteW(None, "openas", filepath, None, None, 5)
                except Exception:
                    os.startfile(filepath)
        else:
            if sys.platform == "darwin":
                import subprocess
                subprocess.call(["open", filepath])
            else:
                import subprocess
                try:
                    subprocess.call(["xdg-open", filepath])
                except Exception:
                    if hasattr(os, "startfile"):
                        os.startfile(filepath)

    def _use_conversation_video_media_viewer_dialog(self) -> bool:
        """True (default) opens a conversation video in the dedicated, full
        MediaViewerDialog; False keeps the classic in-app player instead
        (BASS/ffmpeg, no separate dialog). Settings > Interface do usuário >
        "Mostrar vídeos nas conversas em player separado". Images are not
        affected by this setting — they always use the dialog."""
        return self.main_window.settings.get("user_interface", {}).get(
            "conversation_video_media_viewer_dialog", True
        )

    def _open_conversation_media_viewer(self, index: int):
        """Open the media viewer for the message at *index* in this list."""
        if index < 0 or index >= len(self._sorted_messages):
            return
        self.open_media_viewer_for_message(
            self._sorted_messages[index], restore_index=index
        )

    def open_media_viewer_for_message(self, msg: dict, restore_index=None):
        """Open an image/video message in the shared maximized MediaViewer.

        Takes the message rather than a row index so a caller that HAS the
        message but no row in this panel's list can still use it — the group
        and private data dialogs' Media tab is exactly that: it reads the whole
        conversation out of the database, so most of what it lists is outside
        the ~200 messages this panel keeps in memory. Resolving those through
        an index found nothing and the action was skipped in silence.

        restore_index puts the keyboard focus back on the row the viewer was
        opened from, when there was one.

        The dialog appears immediately; download/decryption happens through
        its background loader so the user gets a stable loading state instead
        of waiting for a second window to appear after the network request.
        """
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("messageType", "")
        if msg_type not in ("imageMessage", "videoMessage"):
            return

        msg_obj = msg.get("message") or {}
        inner = msg_obj.get(msg_type) or {}
        if not isinstance(inner, dict):
            inner = {}
        caption = str(inner.get("caption") or "")
        kind = "image" if msg_type == "imageMessage" else "video"
        label = self.main_window.i18n.t("photo" if kind == "image" else "video")

        msg_id = msg.get("key", {}).get("id", "")
        clean_msg_id = msg_id
        if "_" in msg_id:
            parts = msg_id.split("_")
            clean_msg_id = parts[2] if len(parts) > 2 else parts[-1]
        media_path = data_path("media", f"{clean_msg_id}.wzmedia")
        filename = self._resolve_media_filename(msg)
        suffix = os.path.splitext(filename)[1]
        if not suffix:
            suffix = ".jpg" if kind == "image" else ".mp4"

        def _loader():
            if not os.path.isfile(media_path):
                wx.CallAfter(self.main_window.output, self.main_window.i18n.t("downloading"))
                self.main_window.handle_media_message(msg)
            if not os.path.isfile(media_path):
                raise FileNotFoundError(media_path)
            with open(media_path, "rb") as fh:
                return decrypt_bytes(fh.read(), self.main_window.key)

        # Do not allow voice playback or the legacy embedded video surface to
        # keep running underneath the modal viewer.
        try:
            self._stop_audio()
        except Exception:
            pass
        try:
            self._video_player.stop()
        except Exception:
            pass

        dlg = MediaViewerDialog(
            self,
            self.main_window,
            [{
                "kind": kind,
                "loader": _loader,
                "extension": suffix,
                "filename": filename,
                "caption": caption,
                "label": label,
            }],
        )
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
            index = restore_index
            if index is not None and 0 <= index < self.messages_list.GetItemCount():
                try:
                    self.messages_list.Focus(index)
                    self.messages_list.Select(index)
                    self.messages_list.SetFocus()
                except Exception:
                    pass

    def _on_action_open(self, event, index=None):
        """Open the media of the focused row (or of *index*, when given)."""
        if index is None:
            index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        self.open_media_message(self._sorted_messages[index])

    def _ensure_media_on_disk(self, msg: dict, media_path: str) -> bool:
        """Make sure a message's media file exists locally, downloading it if
        needed. False means "do not proceed" — and the user has already been
        told why.

        Every caller that reads a media file used to inline this as: if the
        file is missing, announce "baixando...", call handle_media_message(),
        then open the path — with no check that the download actually produced
        anything. handle_media_message() does not raise when the server refuses
        the file: a WhatsApp media link that has expired comes back as HTTP 500
        ("Failed to decrypt file" / "Error trying to download the file"), which
        it logs and swallows. The open() that followed then raised
        FileNotFoundError, and the handler printed str(exc) into a message box —
        so a perfectly ordinary "this old file is no longer on WhatsApp's
        servers" surfaced as a raw Python error naming an internal .wzmedia
        path. For a screen-reader user that is a wall of unreadable path
        characters where a sentence should be.

        Reported against the group Media tab, which made it easy to hit: that
        tab lists a group's entire history straight from the database, so it
        routinely offers media far older than anything WhatsApp still holds.
        The bug was never specific to that tab — the same Open on the same
        message in the conversation list did the same thing.

        save_media_message() already got this right and is the model here; it
        is the only one of the five media paths that checked. The offline case
        is checked first because it has its own answer: the download did not
        fail, it was never attempted, and "wait for the connection" is
        actionable where "the link may have expired" would be a lie.
        """
        if os.path.isfile(media_path):
            return True

        i18n = self.main_window.i18n
        if not getattr(self.main_window, "_wa_connected", False):
            wx.CallAfter(self.main_window.output, i18n.t("media_download_offline"))
            return False

        wx.CallAfter(self.main_window.output, i18n.t("downloading"))
        try:
            if msg.get("messageType") == "audioMessage":
                self.main_window.handle_audio_message(msg)
            else:
                self.main_window.handle_media_message(msg)
        except Exception as exc:
            logging.info(
                "[_ensure_media_on_disk] download raised for %s: %s",
                (msg.get("key") or {}).get("id", ""), exc,
            )

        if os.path.isfile(media_path):
            return True

        logging.info(
            "[_ensure_media_on_disk] %s: still missing after download attempt "
            "(%s) — reporting it instead of opening.",
            (msg.get("key") or {}).get("id", ""), media_path,
        )
        wx.CallAfter(
            wx.MessageBox,
            i18n.t("media_download_failed"),
            i18n.t("error").format(app_name=self.main_window.app_name),
            wx.OK | wx.ICON_ERROR,
        )
        return False

    def open_media_message(self, msg: dict):
        """Open a message's media, given the message itself.

        Split out of _on_action_open() so a caller that has the message but
        NOT a row in this panel's list can still use it. The group data
        dialog's Media tab is exactly that: it reads the group's whole history
        from the database, so most of what it shows is outside the ~200
        messages this panel keeps in memory, and resolving those through a
        list index silently found nothing — the menu item and the button
        simply did nothing.
        """
        msg_type = msg.get("messageType", "")
        msg_obj  = msg.get("message") or {}
        msg_id   = msg.get("key", {}).get("id", "")

        if msg_type in ("locationMessage", "liveLocationMessage"):
            # No download/cache involved — hand the coordinates straight to
            # the system's default map/browser handler.
            url = self._location_maps_url(msg)
            if url:
                self._open_file_safely(url)
            return

        # "Abrir" / Open button (next to "Salvar como...") is specifically
        # intended to open media/files in the operating system's default viewer/app
        # (photos, videos, documents, etc.). In-app viewing/playback is reached
        # via Enter/Space directly on the message list item.

        if msg_type == "documentMessage":
            filename = (msg_obj.get("documentMessage") or {}).get(
                "fileName", f"document_{msg_id}"
            )
            ext = os.path.splitext(filename)[1] or ".bin"
        elif msg_type == "imageMessage":
            mime = (msg_obj.get("imageMessage") or {}).get("mimetype", "image/jpeg")
            ext = "." + (mime.split("/")[-1] if "/" in mime else "jpg")
        elif msg_type == "videoMessage":
            mime = (msg_obj.get("videoMessage") or {}).get("mimetype", "video/mp4")
            ext = "." + (mime.split("/")[-1] if "/" in mime else "mp4")
        else:
            return

        clean_msg_id = msg_id
        if "_" in msg_id:
            parts = msg_id.split("_")
            clean_msg_id = parts[2] if len(parts) > 2 else parts[-1]
        media_path = data_path("media", f"{clean_msg_id}.wzmedia")

        def _run():
            if not self._ensure_media_on_disk(msg, media_path):
                return
            try:
                with open(media_path, "rb") as fh:
                    content = decrypt_bytes(fh.read(), self.main_window.key)
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                tmp.write(content)
                tmp.close()
                wx.CallAfter(lambda: self._open_file_safely(tmp.name))
            except Exception as exc:
                wx.CallAfter(
                    wx.MessageBox,
                    str(exc),
                    self.main_window.i18n.t("error").format(
                        app_name=self.main_window.app_name
                    ),
                    wx.OK | wx.ICON_ERROR,
                )

        threading.Thread(target=_run, daemon=True).start()

    # ── Play/pause a video message in-app (audio via BASS, frames via
    # ffmpeg — see core/video_player.py and _hide_all_media_controls(),
    # which stops this whenever selection/conversation moves away) ────────

    def _play_toggle_video_message(self, msg: dict):
        """Enter on a video message: play it in-app (audio via BASS, frames
        via ffmpeg — see core/video_player.py), applying the conversation's
        configured playback speed (see on_audio_speed_btn/Alt+,/Alt+.) the
        same way voice messages already do. A second Enter on the SAME
        video toggles pause; Enter on a DIFFERENT video while one is
        playing stops it and switches to the new one."""
        if msg.get("messageType") != "videoMessage":
            return
        msg_id = msg.get("key", {}).get("id", "")
        if self._video_player.is_playing and self._current_video_msg_id == msg_id:
            self._video_player.toggle_pause()
            return

        clean_msg_id = msg_id
        if "_" in msg_id:
            parts = msg_id.split("_")
            clean_msg_id = parts[2] if len(parts) > 2 else parts[-1]
        media_path = data_path("media", f"{clean_msg_id}.wzmedia")

        def _run():
            if not self._ensure_media_on_disk(msg, media_path):
                return
            try:
                with open(media_path, "rb") as fh:
                    content = decrypt_bytes(fh.read(), self.main_window.key)
                tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
                tmp.write(content)
                tmp.close()
                # Now that the file is on disk, it can answer what the message
                # itself never stated (see video_seconds()).
                self._learn_video_duration(msg, tmp.name)
                speed = self._audio_speed_steps[self._audio_speed_index]
                self._current_video_msg_id = msg_id
                wx.CallAfter(self._start_video_playback, tmp.name, speed, msg_id)
            except Exception as exc:
                wx.CallAfter(
                    wx.MessageBox,
                    str(exc),
                    self.main_window.i18n.t("error").format(
                        app_name=self.main_window.app_name
                    ),
                    wx.OK | wx.ICON_ERROR,
                )

        threading.Thread(target=_run, daemon=True).start()

    def _learn_video_duration(self, msg: dict, path: str):
        """Fill in a video's length from the decoded file when the message
        never stated one, then persist it and repaint that row.

        A video whose sender left the duration out of the message renders as
        a bare "vídeo" (see video_seconds()) — accurate, but the length is
        knowable the moment the file is on disk, and playing it is exactly
        when that happens. BASS opens an .mp4 directly (the same bass_aac
        path core/video_player.py plays it through), so _probe_audio_duration()
        already works here and no extra process is needed.

        Runs on the playback worker thread: the DB write goes through
        _persist_message_local_flag()'s own thread, and the repaint is bounced
        to the UI thread. Repaint rather than repopulate — a full rebuild
        would move the user's focus in the middle of starting playback, and
        a row that fails to repaint just keeps reading "vídeo" until the
        conversation is reopened.
        """
        video = (msg.get("message") or {}).get("videoMessage")
        if not isinstance(video, dict) or video_seconds(video) is not None:
            return
        secs = self._probe_audio_duration(path)
        # 0 is a real answer here, unlike the 0 the message itself states: the
        # file was read and it really is under a second. Only None means the
        # probe could not tell (see video_seconds()).
        if secs is None or secs < 0:
            return
        video[MEASURED_SECONDS_KEY] = secs
        logging.info(
            "[_learn_video_duration] %s: message stated no duration, file says %ds",
            msg.get("key", {}).get("id", ""), secs,
        )
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if jid:
            self._persist_message_local_flag(jid, msg)
            self.main_window._schedule_save(dirty_jid=jid)
        wx.CallAfter(self._repaint_message_rows, [msg.get("key", {}).get("id", "")])

    def _start_video_playback(self, path: str, speed: float, msg_id: str):
        """Runs on the UI thread (via wx.CallAfter from _run() above). Starts
        the player and — same as _play_audio() already does for voice
        messages — shows the shared speed button/progress slider so the
        video gets the same seek/speed controls audio already has, instead
        of only Enter-to-pause with no other way to scrub or change speed.

        _media_bitmap is otherwise only shown by _try_show_thumbnail() (the
        static preview, from the message's own jpegThumbnail) — a video
        with no embedded thumbnail left it Hide()-den for the player's own
        SetBitmap() calls to render into, so no frame was ever visible even
        though decoding/playback was working fine underneath. StatusPanel's
        own video viewer already does this same Show()+Layout() right
        before load_and_play() (see _on_play_pause_video/
        _start_downloaded_video in status_panel.py) — mirrored here.

        The control is also given an explicit video-sized box first. Without
        it the sizer keeps whatever size the last thumbnail left behind (at
        most 200 px — see _try_show_thumbnail — or nothing at all for a
        video with no embedded thumbnail), and wx.StaticBitmap clips rather
        than scales, so the picture came out cropped to a corner. The box is
        released again by _hide_all_media_controls()/_try_show_thumbnail()
        so still images keep sizing themselves as before; VideoPlayer scales
        each frame down into whatever box it finds (see fit_frame_size())."""
        self._media_bitmap.SetMinSize(self._VIDEO_BITMAP_SIZE)
        self._media_bitmap.Show()
        self.conversation_panel.Layout()
        self._video_player.load_and_play(path, speed)
        if self._focused_msg_id() == msg_id:
            self._show_audio_controls()
            self.audio_speed_btn.SetLabel(self._format_speed(speed))
        if not self._audio_timer.IsRunning():
            self._audio_timer.Start(30)

    def _on_video_frame_size_known(self, width: int, height: int):
        """VideoPlayer callback (see core/video_player.py's own comment):
        fired once per playback, as soon as the first frame's actual
        on-screen size is known. _VIDEO_BITMAP_SIZE is a generic 4:3
        placeholder that rarely matches the real video's aspect ratio — a
        portrait clip inside it ends up small with a big blank gap filling
        the rest of the box, which reads as "the video isn't fully shown"
        even once the frame itself is correctly scaled (not clipped).
        Shrinking the box to the frame's own size makes video match how a
        photo is shown: exactly the size of its own content, same as
        _try_show_thumbnail()'s SetMinSize((-1, -1)) does for still images."""
        self._media_bitmap.SetMinSize((width, height))
        self.conversation_panel.Layout()

    def _resolve_media_filename(self, msg: dict) -> str:
        """Resolve original filename and extension for any media message (document, audio, image, video)."""
        msg_type = msg.get("messageType", "")
        msg_obj  = msg.get("message") or {}
        msg_id   = msg.get("key", {}).get("id", "")

        # Text messages store the payload as a plain string under the
        # messageType key (e.g. {"conversation": "..."}), not a dict — guard
        # before calling .get() on it.
        inner = msg_obj.get(msg_type)
        if not isinstance(inner, dict):
            inner = {}
        media_data = msg.get("mediaData") or {}

        # 1. Search local path properties if message was attached or downloaded locally
        local_path = msg.get("_attachment_path") or msg.get("media_path") or msg.get("filePath") or ""
        file_name = ""
        if local_path and os.path.isfile(local_path):
            file_name = os.path.basename(local_path)

        # 2. Deep search for original filename across all Baileys/WPPConnect payload fields
        if not file_name:
            file_name = (
                inner.get("fileName")
                or inner.get("filename")
                or inner.get("title")
                or inner.get("name")
                or msg.get("fileName")
                or msg.get("filename")
                or msg.get("title")
                or media_data.get("filename")
                or media_data.get("fileName")
                or inner.get("caption")
                or msg.get("caption")
                or ""
            )

        # 3. If parsing from URL, ignore WhatsApp CDN hashes (.enc, .chk, encrypted blobs)
        if not file_name:
            target_url = inner.get("clientUrl") or inner.get("url") or msg.get("clientUrl") or msg.get("url") or ""
            if target_url and "/" in target_url:
                url_base = target_url.split("?")[0].split("/")[-1]
                if (
                    url_base
                    and "." in url_base
                    and not url_base.startswith(".")
                    and not url_base.lower().endswith((".enc", ".chk"))
                    and not re.match(r"^\d+_\d+_\d+_n", url_base)
                ):
                    file_name = url_base

        is_ptt = bool(inner.get("ptt", False) or inner.get("isPtt", False) or media_data.get("ptt", False))

        mimetype = inner.get("mimetype") or msg.get("mimetype") or media_data.get("mimetype") or ""
        clean_mime = mimetype.split(";")[0].strip().lower() if mimetype else ""
        # A few audio MIME aliases are either absent from Python's mimetypes
        # table or map to a non-user-facing extension.  Resolve these before
        # falling back to the platform table so Save As keeps the real format.
        canonical_ext = {
            "audio/m4a": ".m4a",
            "audio/x-m4a": ".m4a",
            "audio/mp4": ".m4a",
            "audio/ogg": ".ogg",
            "audio/x-ogg": ".ogg",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/aac": ".aac",
            "audio/flac": ".flac",
            "audio/x-flac": ".flac",
            "audio/opus": ".opus",
            "audio/webm": ".webm",
            "audio/mpeg": ".mp3",
        }.get(clean_mime, "")
        guessed_ext = canonical_ext or (mimetypes.guess_extension(clean_mime) if clean_mime else "")
        if not guessed_ext and "/" in clean_mime:
            guessed_ext = f".{clean_mime.split('/')[-1]}"

        # Standardise common extension guesses
        if guessed_ext == ".jpe": guessed_ext = ".jpg"
        if guessed_ext == ".oga": guessed_ext = ".ogg"

        # Friendly timestamp suffix for fallbacks (e.g. 2026-08-07_03h55)
        msg_ts = int(msg.get("messageTimestamp", 0) or time.time())
        if msg_ts > 1_000_000_000_000:
            msg_ts //= 1000
        time_str = datetime.fromtimestamp(msg_ts).strftime("%Y%m%d_%H%M%S") if msg_ts > 0 else ""

        i18n = self.main_window.i18n

        if msg_type == "audioMessage" and is_ptt:
            # WhatsApp voice notes are normally OGG/Opus, but use the MIME
            # reported by WPPConnect when present instead of hard-coding an
            # extension that may not match the original bytes.
            ext = guessed_ext or ".ogg"
            default_file = f"{i18n.t('default_filename_voice_message')}_{time_str or msg_id}{ext}"
        elif file_name:
            # WPPConnect's filenameFromMimeType() treats MIME as authoritative:
            # if a supplied filename has a different extension, replace only
            # the extension instead of mislabelling the original bytes.
            current_root, current_ext = os.path.splitext(file_name)
            if msg_type == "audioMessage" and guessed_ext:
                if current_ext.lower() != guessed_ext.lower():
                    default_file = f"{current_root or file_name}{guessed_ext}"
                else:
                    default_file = file_name
            elif current_ext:
                default_file = file_name
            elif guessed_ext:
                default_file = f"{file_name}{guessed_ext}"
            else:
                default_file = file_name
        elif msg_type == "documentMessage":
            ext = guessed_ext or ".bin"
            default_file = f"{i18n.t('default_filename_document')}_{time_str or msg_id}{ext}"
        elif msg_type == "imageMessage":
            ext = guessed_ext or ".jpg"
            default_file = f"{i18n.t('default_filename_image')}_{time_str or msg_id}{ext}"
        elif msg_type == "videoMessage":
            ext = guessed_ext or ".mp4"
            default_file = f"{i18n.t('default_filename_video')}_{time_str or msg_id}{ext}"
        elif msg_type == "audioMessage":
            # Do not invent .mp3.  Regular audio attachments keep their real
            # extension via fileName/mimetype above.  If WPPConnect supplies
            # neither, leave the extension empty instead of mislabelling the
            # original bytes as MP3.
            ext = guessed_ext or ""
            default_file = f"{i18n.t('default_filename_audio')}_{time_str or msg_id}{ext}"
        else:
            ext = guessed_ext or ".bin"
            default_file = f"{i18n.t('default_filename_generic')}_{time_str or msg_id}{ext}"

        # Sanitize OS filename invalid characters (Windows: \ / : * ? " < > |)
        return re.sub(r'[\\/*?:"<>|]', '_', default_file).strip()

    def _on_action_save_as(self, event):
        """Save the focused row's media, or the bulk selection when one is
        active and bulk shortcuts are on."""
        if self._bulk_shortcuts_enabled() and self.selected_messages:
            self._on_mass_save_messages(event)
            return
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        self.save_media_message(self._sorted_messages[index])

    def save_media_message(self, msg: dict):
        """Save a message's media, given the message itself.

        Split out of _on_action_save_as() for the same reason as
        open_media_message() — see its docstring. Deliberately does NOT carry
        the bulk-selection branch: a caller holding one specific message is
        asking for that message, not for whatever the conversation happens to
        have multi-selected.
        """
        if self._is_separator(msg):
            return
        msg_type = msg.get("messageType", "")

        if msg_type == "contactMessage":
            # None: _on_save_contact_message() ignores its event argument
            # entirely and reads the list selection instead. Unreachable from
            # the group data dialog's Media tab anyway — a contact card is not
            # one of its media categories.
            self._on_save_contact_message(None)
            return

        # Nothing to save: say so instead of opening a file dialog over a
        # message that has no file. Silence would be worse than the bug it
        # replaces — pressing Ctrl+Shift+S and getting no reaction at all
        # reads as "the shortcut is broken" to a screen-reader user.
        if msg_type not in _SAVEABLE_MESSAGE_TYPES:
            self.main_window.output(
                self.main_window.i18n.t("save_as_nothing_to_save"), interrupt=True
            )
            return
        msg_obj  = msg.get("message") or {}
        msg_id   = msg.get("key", {}).get("id", "")

        # Text messages store the payload as a plain string under the
        # messageType key (e.g. {"conversation": "..."}), not a dict — guard
        # before calling .get() on it.
        inner = msg_obj.get(msg_type)
        if not isinstance(inner, dict):
            inner = {}
        media_data = msg.get("mediaData") or {}
        is_ptt = bool(inner.get("ptt", False) or inner.get("isPtt", False) or media_data.get("ptt", False))
        mimetype = inner.get("mimetype") or msg.get("mimetype") or media_data.get("mimetype") or ""

        default_file = self._resolve_media_filename(msg)

        # Build specific wildcard filter based on target file extension
        ext_clean = os.path.splitext(default_file)[1].lower().lstrip(".")
        i18n = self.main_window.i18n
        all_files = i18n.t("all_files")
        if ext_clean:
            wildcard = f"{ext_clean.upper()} (*.{ext_clean})|*.{ext_clean}|{all_files} (*.*)|*.*"
        elif msg_type == "audioMessage":
            # Unknown audio extension: put *.* first so the native save dialog
            # does not silently append the first audio pattern (typically
            # .mp3) to a file whose actual format we could not identify.
            wildcard = (
                f"{all_files} (*.*)|*.*|"
                f"{i18n.t('file_filter_audio')} (*.mp3;*.ogg;*.wav;*.m4a;*.aac;*.flac;*.opus)|"
                "*.mp3;*.ogg;*.wav;*.m4a;*.aac;*.flac;*.opus"
            )
        elif msg_type == "imageMessage":
            wildcard = (
                f"{i18n.t('file_filter_images')} (*.jpg;*.png;*.webp;*.gif)|"
                f"*.jpg;*.png;*.webp;*.gif|{all_files} (*.*)|*.*"
            )
        elif msg_type == "videoMessage":
            wildcard = (
                f"{i18n.t('file_filter_videos')} (*.mp4;*.mkv;*.avi;*.mov)|"
                f"*.mp4;*.mkv;*.avi;*.mov|{all_files} (*.*)|*.*"
            )
        else:
            wildcard = (
                f"{i18n.t('file_filter_documents')} (*.pdf;*.doc;*.docx;*.txt;*.zip)|"
                f"*.pdf;*.doc;*.docx;*.txt;*.zip|{all_files} (*.*)|*.*"
            )

        logging.info(f"[Save As] msg_id={msg_id}, msg_type={msg_type}, is_ptt={is_ptt}, mimetype='{mimetype}', default_file='{default_file}', wildcard='{wildcard}'")

        dlg_title = (
            self.main_window.i18n.t("save_audio_as") if msg_type == "audioMessage"
            else self.main_window.i18n.t("save_as")
        )
        with wx.FileDialog(
            self,
            dlg_title,
            defaultDir=resolve_save_dialog_folder(self.main_window.settings),
            defaultFile=default_file,
            wildcard=wildcard,
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            save_path = dlg.GetPath()
        self.main_window.remember_save_folder(save_path)

        threading.Thread(target=self._save_message_media, args=(msg, save_path), daemon=True).start()

    def _save_message_media(self, msg, save_path):
        """
        Background-thread worker: download the media (if not already cached),
        decrypt it, and write it to save_path. Shared by the single "Save as"
        flow and the bulk-save flow (_on_mass_save_messages) so both save
        through the exact same download/decrypt/write path.
        """
        msg_type = msg.get("messageType", "")
        msg_id   = msg.get("key", {}).get("id", "")
        clean_msg_id = msg_id
        if "_" in msg_id:
            parts = msg_id.split("_")
            clean_msg_id = parts[2] if len(parts) > 2 else parts[-1]
        if msg_type == "audioMessage":
            media_path = data_path("voice_messages", f"{clean_msg_id}.msv")
        else:
            media_path = data_path("media", f"{clean_msg_id}.wzmedia")

        if not os.path.isfile(media_path):
            if not getattr(self.main_window, "_wa_connected", False):
                wx.CallAfter(
                    self.main_window.output,
                    self.main_window.i18n.t("media_download_offline"),
                )
                return
            wx.CallAfter(
                self.main_window.output, self.main_window.i18n.t("downloading")
            )
            try:
                if msg_type == "audioMessage":
                    self.main_window.handle_audio_message(msg)
                else:
                    self.main_window.handle_media_message(msg)
            except Exception:
                return
        if not os.path.isfile(media_path):
            # Download silently failed — nothing to save.
            wx.CallAfter(
                wx.MessageBox,
                self.main_window.i18n.t("media_download_failed"),
                self.main_window.i18n.t("error").format(
                    app_name=self.main_window.app_name
                ),
                wx.OK | wx.ICON_ERROR,
            )
            return
        try:
            with open(media_path, "rb") as fh:
                content = decrypt_bytes(fh.read(), self.main_window.key)
            with open(save_path, "wb") as fh:
                fh.write(content)
        except Exception as exc:
            wx.CallAfter(
                wx.MessageBox,
                str(exc),
                self.main_window.i18n.t("error").format(
                    app_name=self.main_window.app_name
                ),
                wx.OK | wx.ICON_ERROR,
            )

    def _on_action_download(self, event):
        """
        Download the media file for the currently selected document or video.
        Announces 'baixando...' via AO2, downloads in background, then replaces
        the Download button with Open + Save As once the file is ready.
        """
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg      = self._sorted_messages[index]
        msg_type = msg.get("messageType", "")
        msg_id   = msg.get("key", {}).get("id", "")
        mw       = self.main_window
        i18n     = mw.i18n
        media_path = data_path("media", f"{msg_id}.wzmedia")

        if not getattr(mw, "_wa_connected", False):
            mw.output(i18n.t("media_download_offline"))
            return

        mw.output(i18n.t("downloading"))
        self._action_download_btn.Hide()
        self._hide_media_transfer_gauge()
        self._show_media_transfer_gauge()
        self.conversation_panel.Layout()

        last_percent = -1

        def _update_download_progress(progress):
            nonlocal last_percent
            percent = int(progress * 100)
            if percent == last_percent:
                return
            last_percent = percent
            wx.CallAfter(self.update_message_download_progress, msg_id, progress)

        def _run():
            try:
                if msg_type == "audioMessage":
                    mw.handle_audio_message(msg)
                else:
                    mw.handle_media_message(msg, progress_callback=_update_download_progress)
            except Exception:
                pass

            def _done():
                self._hide_media_transfer_gauge()
                if os.path.isfile(media_path) and os.path.getsize(media_path) > 0:
                    # File ready — swap Download for Open + Save As
                    self._action_open_btn.SetLabel(i18n.t("open"))
                    self._action_open_btn.Show()
                    self._action_save_as_btn.Show()
                else:
                    self._action_download_btn.Show()
                self._sync_media_action_slot_visibility()
                self.conversation_panel.Layout()

            wx.CallAfter(_done)

        threading.Thread(target=_run, daemon=True).start()

    # ── Audio / video playback ──────────────────────────────────────────────

    def toggle_current_audio_playback(self):
        """Ctrl+Alt+Shift+P: pause/resume whatever voice or audio message is
        currently loaded, without needing a specific message/row — unlike
        _toggle_playback(), which always needs one to know what to switch
        TO. A no-op when nothing is loaded (nothing paused, nothing to
        resume)."""
        if self._current_audio_id is None or self._audio_stream is None:
            return
        _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
        if self._is_audio_playing:
            try:
                _ctrl.pause()
            except Exception:
                # The stream is dead (e.g. the output device was switched in
                # Settings while this was playing — BASS_Free()/BASS_Init()
                # during that switch invalidates it), not "already paused".
                # Reopen fresh rather than leaving _is_audio_playing=False
                # pointed at a channel that will also fail the next play().
                if self._recover_audio_stream_after_device_switch():
                    self._is_audio_playing = True
                    self._audio_timer.Start(30)
                else:
                    self._stop_audio()
                return
            self._is_audio_playing = False
            self._audio_timer.Stop()
        else:
            try:
                _ctrl.play()
            except Exception:
                if self._recover_audio_stream_after_device_switch():
                    self._is_audio_playing = True
                    self._audio_timer.Start(30)
                else:
                    self._stop_audio()
                return
            self._is_audio_playing = True
            self._audio_timer.Start(30)

    def _toggle_playback(self, msg_id, duration_seconds, msg, file_path, audio_ext):
        """
        Generic play/pause toggle for both audio messages (voice_messages/)
        and video messages (media/).
        """
        # Same item: toggle play / pause
        if msg_id == self._current_audio_id and self._audio_stream is not None:
            _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            if self._is_audio_playing:
                try:
                    _ctrl.pause()
                except Exception:
                    # Distinguish "not playing yet" (BASS's own report if the
                    # user switches messages faster than the backend updates
                    # state — harmless) from a genuinely dead channel (the
                    # output device was switched in Settings while this was
                    # playing, which frees + reinits BASS and invalidates
                    # every stream that existed before it). The latter must
                    # reopen fresh, or the next play() attempt on the same
                    # dead object fails too — reported live as "doesn't play
                    # the first time after switching output device, only the
                    # second".
                    if self._recover_audio_stream_after_device_switch():
                        self._is_audio_playing = True
                        self._audio_timer.Start(30)
                    else:
                        self._stop_audio()
                    return
                self._is_audio_playing = False
                self._audio_timer.Stop()
            else:
                try:
                    _ctrl.play()
                except Exception:
                    if self._recover_audio_stream_after_device_switch():
                        self._is_audio_playing = True
                        self._audio_timer.Start(30)
                    else:
                        self._stop_audio()
                    return
                self._is_audio_playing = True
                self._audio_timer.Start(30)
            return

        # Save position of the outgoing audio before the stream is destroyed so
        # the user can resume it later if they come back to that message.
        if self._current_audio_id is not None and self._audio_stream is not None:
            try:
                _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
                pos   = _ctrl.get_position()
                total = _ctrl.get_length()
                if 0 < pos < total:
                    self._audio_positions[self._current_audio_id] = pos
            except Exception:
                pass
        self._stop_audio()

        if os.path.isfile(file_path):
            self._play_audio(msg_id, duration_seconds, file_path, audio_ext)
        else:
            if not getattr(self.main_window, "_wa_connected", False):
                # Attempting the HTTP call while disconnected/still-connecting
                # just burns the request timeout for a guaranteed failure —
                # refuse up front with a message that tells the user why,
                # instead of "baixando..." followed by a generic failure a
                # minute later.
                self.main_window.output(self.main_window.i18n.t("media_download_offline"))
                return
            if not hasattr(self, "_downloading_audio_ids"):
                self._downloading_audio_ids = set()

            if msg_id in self._downloading_audio_ids:
                self.main_window.output(self.main_window.i18n.t("downloading"))
                return

            logging.info(f"[UI Audio Playback] File not found local, launching download thread. file_path={file_path}")
            self.main_window.output(self.main_window.i18n.t("downloading"))
            self._downloading_audio_ids.add(msg_id)

            def _download_and_play():
                try:
                    msg_type = msg.get("messageType", "") if msg else ""
                    try:
                        if msg_type == "audioMessage":
                            if msg is not None:
                                logging.info(f"[UI Audio Playback] Calling handle_audio_message for {msg_id}")
                                self.main_window.handle_audio_message(msg)
                        else:
                            if msg is not None:
                                logging.info(f"[UI Audio Playback] Calling handle_media_message for {msg_id}")
                                self.main_window.handle_media_message(msg)
                    except Exception as e:
                        logging.warning(
                            "[_download_and_play] download failed for %s: %s", msg_id, e,
                            exc_info=True,
                        )
                    # Only play if the file was actually downloaded (non-empty)
                    exists = os.path.isfile(file_path)
                    size = os.path.getsize(file_path) if exists else 0
                    logging.info(f"[UI Audio Playback] Finished download try. exists={exists}, size={size}")
                    if exists and size > 16:
                        wx.CallAfter(
                            self._play_audio, msg_id, duration_seconds, file_path, audio_ext
                        )
                    else:
                        # Download silently failed (timeout, expired CDN link,
                        # WPPConnect error) — the user's last feedback was
                        # "baixando..." with no follow-up; tell them it failed
                        # instead of leaving that as the final word.
                        wx.CallAfter(
                            self.main_window.output,
                            self.main_window.i18n.t("media_download_failed"),
                        )
                finally:
                    self._downloading_audio_ids.discard(msg_id)

            threading.Thread(target=_download_and_play, daemon=True).start()

    def _open_audio_stream_from_temp_file(self):
        """Open a fresh BASS stream on the already-decrypted
        self._audio_temp_file, wrapped in Tempo FX (enables speed control).

        A decoded stream (BASS_STREAM_DECODE) cannot be played directly; it
        must be wrapped by a BASS FX processor such as Tempo. If the FX
        plugin is unavailable, falls back to a plain stream without the
        effect. A method rather than a local closure inside _play_audio() so
        the toggle-play recovery paths (_toggle_playback(),
        toggle_current_audio_playback()) can reopen a fresh stream too — an
        output device switch (Settings) frees + reinits BASS, invalidating
        whatever stream/Tempo control was already loaded, and resuming that
        SAME dead object on the next play() always failed silently: only
        _play_audio() (a brand new message) reopened a fresh stream, so
        toggling play/pause on the message that was already loaded when the
        device switch happened never recovered — reported live as "doesn't
        play the first time after switching output device, only the
        second" (the second attempt worked only once _current_audio_id had
        been reset by some other path, landing back on _play_audio()).
        """
        try:
            s = sl_stream.FileStream(file=self._audio_temp_file, decode=True)
            tempo = Tempo(s)
            _speed = self._audio_speed_steps[self._audio_speed_index]
            tempo.tempo = self._audio_tempo_map.get(_speed, 0)
            return s, tempo
        except Exception as e:
            logging.info(f"[UI Audio Playback] Decode/Tempo stream failed ({e}), falling back to direct stream: {self._audio_temp_file}")
            return sl_stream.FileStream(file=self._audio_temp_file), None

    def _recover_audio_stream_after_device_switch(self) -> bool:
        """Reopen self._audio_stream/_audio_tempo_ctrl from the still-valid
        decrypted temp file and resume playback near where it was.

        Called when a play()/pause() on the currently loaded stream raises
        outside of _play_audio() (which already handles this) — see
        _open_audio_stream_from_temp_file()'s docstring for why that
        happens. Returns whether it succeeded; a caller whose recovery
        fails should fall back to _stop_audio() so state doesn't stay
        pointed at a permanently dead stream.
        """
        if not self._audio_temp_file or not os.path.isfile(self._audio_temp_file):
            return False
        old_ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
        pos = None
        if old_ctrl is not None:
            try:
                pos = old_ctrl.get_position()
            except Exception:
                pos = None
        try:
            self._audio_stream, self._audio_tempo_ctrl = self._open_audio_stream_from_temp_file()
            playback_ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            if pos:
                try:
                    playback_ctrl.set_position(pos)
                except Exception:
                    pass
            playback_ctrl.play()
        except Exception as e:
            logging.exception(f"[UI Audio Playback] Recovery after device switch failed: {e}")
            return False
        return True

    def _play_audio(self, msg_id, duration_seconds, file_path, audio_ext=".ogg"):
        if not os.path.isfile(file_path):
            return

        # This can be reached two ways: synchronously from _toggle_playback
        # (file already local), or via wx.CallAfter from a background download
        # thread once a fetch finishes. The two can interleave — the user
        # taps audio A (triggers a download), then before it finishes taps
        # already-local audio B, which plays immediately; A's download then
        # completes and lands here. Without stopping whatever is currently
        # playing first, this unconditionally overwrote _audio_stream/
        # _audio_temp_file with A's — leaking B's still-running BASS channel
        # (its reference was just overwritten, so _stop_audio() could never
        # reach it again) and its decrypted temp file (never unlinked) every
        # single time this race happened.
        if self._audio_stream is not None and self._current_audio_id != msg_id:
            self._stop_audio()

        # ── Decrypt and write to a temp file ────────────────────────────────
        try:
            with open(file_path, "rb") as fh:
                content = decrypt_bytes(fh.read(), self.main_window.key)
            logging.info(
                f"[UI Audio Playback] Decrypted {len(content)} bytes. "
                f"Header hex: {content[:16].hex()} "
                f"(OGG magic = 4f676753, Opus head = 4f707573)"
            )
            if content.startswith(b"RIFF"):
                actual_ext = ".wav"
            elif content.startswith(b"ID3") or (len(content) > 2 and content[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
                actual_ext = ".mp3"
            elif b"ftyp" in content[:32]:
                actual_ext = ".m4a"
            elif content.startswith(b"OggS"):
                actual_ext = ".ogg"
            else:
                actual_ext = audio_ext
            tmp = tempfile.NamedTemporaryFile(suffix=actual_ext, delete=False)
            tmp.write(content)
            tmp.close()
            self._audio_temp_file = tmp.name
            if actual_ext == ".m4a":
                wav_path = transcode_audio_to_wav(
                    self.main_window._find_api_ffmpeg(),
                    self._audio_temp_file,
                )
                if wav_path:
                    os.unlink(self._audio_temp_file)
                    self._audio_temp_file = wav_path
                    logging.info(
                        "[UI Audio Playback] Converted MP4/M4A audio to WAV: %s",
                        wav_path,
                    )
                else:
                    logging.warning(
                        "[UI Audio Playback] MP4/M4A audio could not be converted; "
                        "BASS playback may be unavailable"
                    )
        except Exception as e:
            logging.exception(f"[UI Audio Playback] Error decrypting or creating temp audio file: {e}")
            self._stop_audio()
            return

        try:
            self._audio_stream, self._audio_tempo_ctrl = self._open_audio_stream_from_temp_file()
        except Exception as e:
            # Both the decode+Tempo stream and the plain direct stream failed
            # (_open_audio_stream_from_temp_file()'s own fallback) — e.g. an
            # OGG whose codec isn't Opus, or whose bassopus.dll plugin failed
            # to register, which BASS rejects for both attempts with error 41
            # "unsupported file format". Re-encode through ffmpeg to PCM WAV,
            # which sidesteps BASS's codec support entirely, and retry once
            # from that file rather than giving up on the message.
            logging.info(
                "[UI Audio Playback] Direct stream also failed (%s); "
                "trying ffmpeg WAV fallback for %s", e, self._audio_temp_file,
            )
            wav_path = transcode_audio_to_wav(
                self.main_window._find_api_ffmpeg(),
                self._audio_temp_file,
            )
            if wav_path is None:
                logging.exception(f"[UI Audio Playback] Error creating stream: {e}")
                self._stop_audio()
                return
            os.unlink(self._audio_temp_file)
            self._audio_temp_file = wav_path
            try:
                self._audio_stream, self._audio_tempo_ctrl = self._open_audio_stream_from_temp_file()
            except Exception as e2:
                logging.exception(
                    f"[UI Audio Playback] Error creating stream from converted WAV: {e2}"
                )
                self._stop_audio()
                return

        # ── Start playback ───────────────────────────────────────────────────
        # When Tempo FX is active the decode stream has no audio output of its
        # own; playback must be started on the Tempo wrapper instead.
        self._audio_stream_duration = int(duration_seconds)
        self._current_audio_id = msg_id
        self._audio_conv_jid   = (
            self.conversation.get("remoteJid", "") if self.conversation else ""
        )
        playback_ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
        # Restore saved position (e.g. when another audio preempted this one)
        saved_pos = self._audio_positions.pop(msg_id, None)
        if saved_pos:
            try:
                playback_ctrl.set_position(saved_pos)
            except Exception:
                pass

        try:
            playback_ctrl.play()
        except Exception as e:
            logging.exception(f"[UI Audio Playback] Error starting playback: {e}")
            # The output device may have just been switched (Settings, or a
            # fallback at startup) — BASS_Free()/BASS_Init() during that
            # switch invalidates the stream we just tried to play on (a
            # confirmed BASS behaviour, not a one-off glitch), so retrying
            # play() on the SAME playback_ctrl would fail again with the
            # same error. Reopen a fresh stream from the same (still valid)
            # decrypted temp file instead, and play that.
            if self.main_window.sound_system.handle_playback_failure():
                try:
                    self._audio_stream, self._audio_tempo_ctrl = _open_stream()
                    playback_ctrl = (
                        self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
                    )
                    if saved_pos:
                        try:
                            playback_ctrl.set_position(saved_pos)
                        except Exception:
                            pass
                    playback_ctrl.play()
                except Exception as e2:
                    logging.exception(f"[UI Audio Playback] Retry after device fallback also failed: {e2}")
                    self._stop_audio()
                    return
            else:
                self._stop_audio()
                return

        self._is_audio_playing = True
        self._audio_timer.Start(30)
        # Show controls only if the playing message is currently focused in the list.
        _speed = self._audio_speed_steps[self._audio_speed_index]
        if self._focused_msg_id() == msg_id:
            self._show_audio_controls()
            self.audio_speed_btn.SetLabel(self._format_speed(_speed))

    def _stop_audio(self):
        # A manual stop (or a different audio taking over) invalidates any
        # still-pending auto-chain timers — never let them start a stale audio.
        self._cancel_pending_chain_timers()
        if self._audio_timer.IsRunning():
            self._audio_timer.Stop()
        # Stop the Tempo FX controller first (it owns the audio output channel)
        if self._audio_tempo_ctrl is not None:
            try:
                self._audio_tempo_ctrl.stop()
            except Exception:
                pass
            self._audio_tempo_ctrl = None
        if self._audio_stream is not None:
            try:
                self._audio_stream.stop()
            except Exception:
                pass
            self._audio_stream = None
        self._is_audio_playing = False
        self._current_audio_id = None
        if not getattr(self, "_in_auto_chain_transition", False) and not getattr(self, "_in_auto_timer_stop", False):
            self._is_in_audio_chain = False
            # The sequence is over (user stopped it, started a different audio,
            # or left the conversation): no further focus move is coming, so
            # the rows held back during it are safe to write.
            self._release_chain_held_repaints()
        if self._audio_temp_file and os.path.exists(self._audio_temp_file):
            try:
                os.unlink(self._audio_temp_file)
            except Exception:
                pass
            self._audio_temp_file = None

    def _stop_playback_for_removed_messages(self, msg_ids: set):
        """Stop any in-app audio/video playback belonging to a message that
        is about to disappear from the list — deleted locally, deleted for
        everyone, mass-deleted, or mirrored in from a phone-side deletion
        the periodic poll picked up (MainWindow._mirror_remote_deletions()).

        Audio is allowed to keep playing in the background while the user
        scrolls/selects elsewhere (see _hide_all_media_controls()'s own
        comment), so it's matched purely by _current_audio_id — not by
        whether its row is currently focused or even still loaded in
        _sorted_messages (pagination can scroll it out of view while it
        keeps playing). Video (in-app playback via Enter, core/video_player.py
        — a live ffmpeg subprocess) is matched by _current_video_msg_id the
        same way; before this, remove_messages_by_id() never checked either
        one at all, so deleting a message that was actively playing left it
        looping/streaming with no row left in the UI to stop it from.
        """
        if self._current_audio_id in msg_ids and self._audio_stream is not None:
            self._stop_audio()
            self._hide_audio_controls()
        if self._current_video_msg_id in msg_ids:
            self._hide_all_media_controls()

    def on_message_revoked(self, msg_id: str):
        """A message was deleted for everyone by its sender, detected live
        (see MainWindow._apply_remote_revoke()). The official client swaps
        it for "Mensagem apagada" instantly, including stopping playback if
        you were mid-listen — WinZapp used to leave the original audio/
        video/text/media on screen (and audio/video still playing) until
        the next periodic remote-deletion poll, which only removes the row
        outright rather than marking it deleted, and can take a while to
        even notice.
        """
        if msg_id:
            self._stop_playback_for_removed_messages({msg_id})
        if msg_id and self._focused_msg_id() == msg_id:
            self._hide_all_media_controls()
        # Only the revoked message's own row changes text (it keeps its row —
        # a revoke protocolMessage is still displayable), so re-rendering every
        # row of the conversation for it was disproportionate.
        if not self._repaint_message_rows([msg_id]):
            self.refresh_active_conversation_messages()

    def on_audio_timer(self, event):
        if self._current_video_msg_id is not None:
            if not self._video_player.is_playing:
                # Reached EOF or was stopped elsewhere (e.g. _hide_all_media_
                # controls() already cleared this — belt and suspenders for
                # any path that stops the player without going through it).
                self._current_video_msg_id = None
                self._hide_audio_controls()
                return
            try:
                pos   = self._video_player.get_position()
                total = self._video_player.get_length()
                if total > 0:
                    self.audio_slider.SetValue(int(pos / total * 1000))
                    self.audio_slider.Refresh()
            except Exception:
                pass
            return
        if self._audio_stream is None:
            return
        try:
            _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            pos   = _ctrl.get_position()
            total = _ctrl.get_length()
            if total > 0:
                if pos >= total:
                    # Save the ID/message before _stop_audio() clears it
                    finished_id  = self._current_audio_id
                    finished_msg = next(
                        (m for m in self._sorted_messages
                         if m.get("key", {}).get("id") == finished_id),
                        None,
                    )
                    self._in_auto_timer_stop = True
                    try:
                        self._stop_audio()
                    finally:
                        self._in_auto_timer_stop = False
                    self._hide_audio_controls()
                    # Reaching the end of playback — right where the controls
                    # get hidden — is "played" for a received voice message:
                    # mark it locally and tell WhatsApp so the sender sees it
                    # too. See MainWindow.mark_audio_message_played()'s own
                    # docstring for why this never applies to our own sends.
                    will_chain = bool(finished_id) and self._next_message_is_chainable_audio(finished_id)
                    if will_chain:
                        # Armed HERE, before anything can queue a row repaint —
                        # not inside _auto_chain_next_audio() below. The played
                        # receipt this send-off triggers echoes back from
                        # WhatsApp onto on_message_status_update() on its own
                        # schedule, and that path knows nothing about the chain;
                        # the hold is what catches it. See
                        # _release_chain_held_repaints().
                        self._hold_status_repaints_until_chain_ends()
                    if finished_msg is not None:
                        self.main_window.mark_audio_message_played(
                            finished_msg,
                            # See mark_audio_message_played()'s own docstring:
                            # when the chain is about to move focus onto the
                            # next voice note, the row refresh is held back
                            # and fired by _auto_chain_next_audio() itself
                            # right after that focus move actually happens —
                            # not on a fixed timeout guess, which in practice
                            # could still lose the race against however long
                            # the chain's own transition actually takes.
                            skip_panel_refresh=will_chain,
                        )
                    # Try to auto-play the next consecutive audio message
                    if finished_id:
                        self._auto_chain_next_audio(
                            finished_id,
                            pending_played_msg_id=finished_id if will_chain else None,
                        )
                    return
                self.audio_slider.SetValue(int(pos / total * 1000))
                self.audio_slider.Refresh()
        except Exception:
            pass

    def _cancel_pending_chain_timers(self):
        """Cancel any still-scheduled auto-chain wx.CallLater timers.

        The chain schedules _play_next/_start_audio (and _play_end) with
        wx.CallLater; if the user stops playback, starts a different audio, or
        navigates away before those fire, the pending timers would otherwise
        still run and start audio from an earlier point in the sequence.
        """
        for attr in ("_chain_play_timer", "_chain_start_timer", "_chain_end_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                try:
                    timer.Stop()
                except Exception:
                    pass
                setattr(self, attr, None)
        # A "played" row refresh _auto_chain_next_audio() deferred onto the
        # (now-cancelled) chain step must still happen — otherwise the row
        # is left showing stale status until something unrelated refreshes
        # it. It just can no longer wait for the focus move that isn't
        # going to happen any more, so it fires right here instead.
        pending = getattr(self, "_pending_played_refresh_id", None)
        if pending:
            self._pending_played_refresh_id = None
            self.refresh_message_status(pending, "5")

    def _next_message_is_chainable_audio(self, finished_id: str) -> bool:
        """Read-only peek at what _auto_chain_next_audio(finished_id) is
        about to do: True if it will auto-play a next voice note and move
        list focus onto it. Mirrors that method's own eligibility checks
        without any side effect — used by on_audio_timer() to decide whether
        the "played" row refresh for finished_id needs to be delayed (see
        the call site's comment)."""
        current_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if current_jid != self._audio_conv_jid:
            return False
        current_idx = -1
        finished_msg = None
        for i, msg in enumerate(self._sorted_messages):
            if not self._is_separator(msg) and msg.get("key", {}).get("id") == finished_id:
                current_idx = i
                finished_msg = msg
                break
        if current_idx < 0 or finished_msg is None or not self._is_voice_message(finished_msg):
            return False
        next_idx = current_idx + 1
        while next_idx < len(self._sorted_messages):
            candidate = self._sorted_messages[next_idx]
            if self._is_separator(candidate):
                next_idx += 1
                continue
            return candidate.get("messageType") == "audioMessage" and self._is_voice_message(candidate)
        return False

    def _auto_chain_next_audio(self, finished_id: str, pending_played_msg_id: str = None):
        """
        After an audio message finishes playing, automatically start the next
        consecutive audio message if one exists immediately after in the list.
        Stops at the first non-audio (or separator) message.

        pending_played_msg_id: when on_audio_timer() skipped the "played" row
        refresh for finished_id (see its own call site comment), this is
        finished_id again — this method fires that refresh itself, exactly
        once, at whichever point it's actually safe: right after the chain
        moves focus onto the next voice note if it does, or immediately if it
        turns out there's nothing to chain into after all.

        Note this ordering is NOT what keeps the screen reader quiet, and it
        never was — refresh_message_status() only queues the row and starts a
        coalescing timer, so the write lands well after this callback returns,
        and a played receipt echoing back from WhatsApp can write the same row
        without passing through here at all. _release_chain_held_repaints() is
        what actually guarantees no row is rewritten while the chain is moving
        focus; this just keeps the queued refresh from being dropped.
        """
        # Cancel any timers left over from a previous chain step before
        # scheduling new ones — a stale timer must never start audio after
        # the user has already stopped/jumped elsewhere. Also flushes
        # whatever pending "played" refresh that previous step was carrying
        # (see _cancel_pending_chain_timers()'s own comment), so it never
        # gets silently dropped by being overwritten below.
        self._cancel_pending_chain_timers()
        self._pending_played_refresh_id = pending_played_msg_id

        def _flush_pending_played_refresh():
            pending = self._pending_played_refresh_id
            if pending:
                self._pending_played_refresh_id = None
                self.refresh_message_status(pending, "5")

        # Don't chain if the user has navigated to a different conversation —
        # _sorted_messages belongs to the current conversation, not the one
        # where the audio was playing.
        current_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if current_jid != self._audio_conv_jid:
            _flush_pending_played_refresh()
            return

        # Find the index of the just-finished message
        current_idx = -1
        finished_msg = None
        for i, msg in enumerate(self._sorted_messages):
            if not self._is_separator(msg) and msg.get("key", {}).get("id") == finished_id:
                current_idx = i
                finished_msg = msg
                break
        if current_idx < 0 or finished_msg is None:
            _flush_pending_played_refresh()
            return

        # Sequential playback and transition sounds ONLY apply to voice notes (PTT),
        # not to generic attached audio/music files.
        if not self._is_voice_message(finished_msg):
            self._is_in_audio_chain = False
            _flush_pending_played_refresh()
            return

        # Walk forward, skipping separators, to find the next message
        next_idx = current_idx + 1
        has_next_audio = False
        target_msg = None
        target_idx = -1
        while next_idx < len(self._sorted_messages):
            candidate = self._sorted_messages[next_idx]
            if self._is_separator(candidate):
                next_idx += 1
                continue
            if candidate.get("messageType") == "audioMessage" and self._is_voice_message(candidate):
                has_next_audio = True
                target_msg = candidate
                target_idx = next_idx
            break

        if has_next_audio and target_msg is not None:
            self._is_in_audio_chain = True
            # Normally already armed by on_audio_timer(); repeated here so a
            # direct caller of this method gets the same protection.
            self._hold_status_repaints_until_chain_ends()
            def _play_next():
                snd = getattr(self.main_window, "audio_transition_next_sound", None)
                if snd is not None:
                    try:
                        snd.play()
                    except Exception as e:
                        logging.exception(f"[UI Audio Chaining] Error playing audio_transition_next_sound: {e}")

                def _start_audio():
                    msg_id   = target_msg.get("key", {}).get("id", "")
                    duration = (
                        (target_msg.get("message") or {}).get("audioMessage") or {}
                    ).get("seconds", 0) or 0
                    # Only move list focus to the next audio when the user is
                    # EXCLUSIVELY focused on the audio that just finished
                    # playing (current_idx). If they've moved focus one row
                    # above/below (or anywhere else) while listening, keep the
                    # chain playing but never steal their focus back. And
                    # regardless of that, respect the user's own preference
                    # (Settings > Interface) to never have the chain move
                    # focus at all — audio keeps auto-advancing either way,
                    # this only controls whether the list selection follows it.
                    auto_focus = self.main_window.settings.get("user_interface", {}).get(
                        "auto_focus_next_audio", True
                    )
                    current_focus = self.messages_list.GetFocusedItem()
                    if auto_focus and current_focus == current_idx:
                        self.messages_list.Focus(target_idx)
                        self.messages_list.Select(target_idx, True)
                        self.messages_list.EnsureVisible(target_idx)
                    # Queue the finished row's "played" refresh. It will not
                    # be written now: the hold armed for this chain parks it
                    # (and anything else queued during the sequence) until
                    # _release_chain_held_repaints() runs at the end. Trying to
                    # win the race by ordering the two events here is what used
                    # to be attempted, and it could not work — see that
                    # method's docstring for the measurements.
                    wx.CallAfter(_flush_pending_played_refresh)
                    clean_msg_id = msg_id
                    if "_" in msg_id:
                        parts = msg_id.split("_")
                        clean_msg_id = parts[2] if len(parts) > 2 else parts[-1]
                    self._in_auto_chain_transition = True
                    try:
                        self._toggle_playback(
                            msg_id, duration, target_msg,
                            file_path=data_path("voice_messages", f"{clean_msg_id}.msv"),
                            audio_ext=".ogg",
                        )
                    finally:
                        self._in_auto_chain_transition = False
                self._chain_start_timer = wx.CallLater(100, _start_audio)
            self._chain_play_timer = wx.CallLater(100, _play_next)
        else:
            # No next voice note to chain into — nothing else is ever going
            # to move focus away from the finished row, so the "played"
            # refresh (if any) is safe to fire right now, and every repaint
            # held back during the sequence can finally be written.
            _flush_pending_played_refresh()
            self._release_chain_held_repaints()
            if getattr(self, "_is_in_audio_chain", False):
                def _play_end():
                    snd = getattr(self.main_window, "audio_transition_end_sound", None)
                    if snd is not None:
                        try:
                            snd.play()
                        except Exception as e:
                            logging.exception(f"[UI Audio Chaining] Error playing audio_transition_end_sound: {e}")
                    self._is_in_audio_chain = False
                self._chain_end_timer = wx.CallLater(100, _play_end)
            else:
                self._is_in_audio_chain = False

    def _is_voice_message(self, msg: dict) -> bool:
        """Return True if msg is a voice note (PTT / mensagem de voz), not a generic audio file."""
        return is_voice_message(msg)


    def on_audio_speed_btn(self, event):
        self._audio_speed_index = (self._audio_speed_index + 1) % len(
            self._audio_speed_steps
        )
        self._apply_audio_speed()

    def _on_audio_speed_decrease(self, event):
        """Alt+, — step down one speed level (wraps at minimum)."""
        if self._audio_speed_index > 0:
            self._audio_speed_index -= 1
            self._apply_audio_speed()

    def _on_audio_speed_increase(self, event):
        """Alt+. — step up one speed level (wraps at maximum)."""
        if self._audio_speed_index < len(self._audio_speed_steps) - 1:
            self._audio_speed_index += 1
            self._apply_audio_speed()

    def _apply_audio_speed(self):
        """Apply the current speed index to the active stream and persist it."""
        speed = self._audio_speed_steps[self._audio_speed_index]
        self.audio_speed_btn.SetLabel(self._format_speed(speed))
        if self._current_video_msg_id is not None and self._video_player.is_playing:
            self._video_player.set_speed(speed)
        elif self._audio_tempo_ctrl is not None:
            try:
                self._audio_tempo_ctrl.tempo = self._audio_tempo_map[speed]
            except Exception:
                pass
        self.main_window.settings.setdefault("audio_playback", {})["audio_default_speed"] = speed
        self.main_window.save_settings()

    def on_audio_slider(self, event):
        if self._current_video_msg_id is not None and self._video_player.is_playing:
            try:
                val   = self.audio_slider.GetValue()
                total = self._video_player.get_length()
                if total > 0:
                    self._video_player.set_position(int(val / 1000 * total))
            except Exception:
                pass
            return
        if self._audio_stream is None:
            return
        try:
            # Seek on the same control that's actually playing and that
            # on_audio_timer() reads position back from — when Tempo FX is
            # active, _audio_stream is a decode-only source with no direct
            # audio output; playback runs through _audio_tempo_ctrl instead.
            # Setting position on the raw decode stream still "worked" in
            # that it eventually reached the new position, but only once
            # Tempo's own already-decoded-ahead buffer finished draining
            # first — reported live as audio taking a long time to resume
            # after a slider seek.
            _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            val   = self.audio_slider.GetValue()
            total = _ctrl.get_length()
            _ctrl.set_position(int(val / 1000 * total))
        except Exception:
            pass

    def _has_active_audio_or_video(self) -> bool:
        if self._current_video_msg_id is not None and self._video_player.is_playing:
            return True
        return self._audio_stream is not None

    def seek_active_playback_by(self, delta_seconds: float) -> bool:
        """Seek the currently playing voice message or video by *delta_seconds*
        (negative = backward), clamped to [0, length]. Returns False when
        nothing is playing, so callers (keyboard shortcuts) can fall through
        to their normal behavior instead. Issue #17."""
        if self._current_video_msg_id is not None and self._video_player.is_playing:
            try:
                total = self._video_player.get_length()
                if total <= 0:
                    return False
                pos = self._video_player.get_position()
                delta_bytes = self._video_player.seconds_to_bytes(abs(delta_seconds))
                if delta_seconds < 0:
                    delta_bytes = -delta_bytes
                new_pos = max(0, min(total, pos + delta_bytes))
                self._video_player.set_position(new_pos)
                return True
            except Exception:
                return False
        if self._audio_stream is None:
            return False
        try:
            _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            total = _ctrl.get_length()
            if total <= 0:
                return False
            pos = _ctrl.get_position()
            delta_bytes = _ctrl.seconds_to_bytes(abs(delta_seconds))
            if delta_seconds < 0:
                delta_bytes = -delta_bytes
            new_pos = max(0, min(total, pos + delta_bytes))
            _ctrl.set_position(new_pos)
            return True
        except Exception:
            return False

    def seek_active_playback_to_edge(self, to_end: bool) -> bool:
        """Seek the currently playing voice message or video to its very
        start (to_end=False) or end (to_end=True). Issue #17."""
        if self._current_video_msg_id is not None and self._video_player.is_playing:
            try:
                total = self._video_player.get_length()
                if total <= 0:
                    return False
                self._video_player.set_position(total if to_end else 0)
                return True
            except Exception:
                return False
        if self._audio_stream is None:
            return False
        try:
            _ctrl = self._audio_tempo_ctrl if self._audio_tempo_ctrl is not None else self._audio_stream
            total = _ctrl.get_length()
            if total <= 0:
                return False
            _ctrl.set_position(total if to_end else 0)
            return True
        except Exception:
            return False

    def _show_audio_controls(self):
        self.audio_speed_btn.Show()
        self.audio_progress_label.Show()
        self.audio_slider.Show()
        self.conversation_panel.Layout()

    def _hide_audio_controls(self):
        focused = wx.Window.FindFocus()
        audio_ctrls = (
            getattr(self, "audio_speed_btn", None),
            getattr(self, "audio_slider", None),
            getattr(self, "audio_progress_label", None),
        )
        if focused is not None and any(focused == c for c in audio_ctrls if c is not None):
            if hasattr(self, "messages_list") and self.messages_list.IsShown():
                self.messages_list.SetFocus()
        self.audio_speed_btn.Hide()
        self.audio_progress_label.Hide()
        self.audio_slider.Hide()
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

    def _format_speed(self, speed):
        sep = self.main_window.i18n.t("decimal_separator")
        return f"{speed:.1f}".replace(".", sep) + "×"

    # ── Message content helpers ─────────────────────────────────────────────

    def _extract_timestamp(self, msg):
        if not isinstance(msg, dict):
            return None
        ts = msg.get("messageTimestamp")
        if ts is None:
            return None
        try:
            ts_val = int(ts)
            if ts_val > 1_000_000_000_000:
                ts_val //= 1000
            return ts_val
        except Exception:
            return None

    def _format_date(self, ts):
        if not ts:
            return ""
        try:
            ts_val = int(ts)
            if ts_val > 1_000_000_000_000:
                ts_val //= 1000
            dt    = datetime.fromtimestamp(ts_val)
            today = datetime.now()
            i18n  = self.main_window.i18n
            if dt.date() == today.date():
                return dt.strftime(get_time_format(i18n.t("time_fmt")))
            # Settings > Interface do usuário > "Mostrar mensagens do dia
            # anterior com data omitida (ontem)" (default on). A message from
            # yesterday (any time up to 23:59) announces as "ontem às HH:MM"
            # instead of the full date — still through get_time_format() so
            # it respects the user's own time format either way.
            if dt.date() == today.date() - timedelta(days=1):
                show_yesterday = self.main_window.settings.get("user_interface", {}).get(
                    "show_yesterday_label", True
                )
                if show_yesterday:
                    time_str = dt.strftime(get_time_format(i18n.t("time_fmt")))
                    return i18n.t("yesterday_at").format(time=time_str)
            return dt.strftime(get_datetime_format(i18n.t("datetime_fmt")))
        except Exception:
            return ""

    def _probe_audio_duration(self, path: str):
        """Method form of probe_media_duration() — see that function."""
        return probe_media_duration(path)

    def _format_duration(self, seconds):
        """Human-readable length, or "" when it isn't known.

        None means "never told us" — a forwarded media message arrives over
        the live socket with no duration on it (issue #43) — and callers
        treat "" as "omit the duration clause", which beats stating a length
        that is certainly wrong.

        Zero is NOT that case: a voice note shorter than a second really does
        report 0, and WhatsApp itself shows "0:00" for those. It is formatted
        like any other length. Only a negative value, which no medium has, is
        folded back into "unknown".
        """
        if seconds is None:
            return ""
        try:
            seconds = int(seconds)
        except (ValueError, TypeError):
            return ""
        if seconds < 0:
            return ""
        i18n = self.main_window.i18n
        if seconds < 60:
            unit = i18n.t("second") if seconds == 1 else i18n.t("seconds")
            return f"{seconds} {unit}"
        elif seconds < 3600:
            m, s = seconds // 60, seconds % 60
            return (
                f"{m} {i18n.t('minute') if m == 1 else i18n.t('minutes')}"
                f" {i18n.t('and')} {s} {i18n.t('second') if s == 1 else i18n.t('seconds')}"
            )
        else:
            h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
            return (
                f"{h} {i18n.t('hour') if h == 1 else i18n.t('hours')},"
                f" {m} {i18n.t('minute') if m == 1 else i18n.t('minutes')}"
                f" {i18n.t('and')} {s} {i18n.t('second') if s == 1 else i18n.t('seconds')}"
            )

    def _format_filesize(self, size_bytes) -> str:
        if size_bytes is None:
            return ""
        try:
            size = int(size_bytes)
        except (ValueError, TypeError):
            return ""
        sep = self.main_window.i18n.t("decimal_separator")
        if size < 1024:
            return f"{size} b"
        elif size < 1024 ** 2:
            return f"{size / 1024:.1f}".replace(".", sep) + " kb"
        elif size < 1024 ** 3:
            return f"{size / 1024 ** 2:.1f}".replace(".", sep) + " mb"
        else:
            return f"{size / 1024 ** 3:.2f}".replace(".", sep) + " gb"

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
                name = "eu"
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
            # mentionedJid may live at the top-level contextInfo (WPPConnect API
            # normalises it there) or inside extendedTextMessage.contextInfo.
            ctx_top = msg.get("contextInfo") or {}
            ctx_msg = msg_obj.get("contextInfo") or {}
            ctx_ext = ext.get("contextInfo") or {}
            mentioned = (
                ctx_top.get("mentionedJid") or ctx_top.get("mentionedJidList")
                or ctx_msg.get("mentionedJid") or ctx_msg.get("mentionedJidList")
                or ctx_ext.get("mentionedJid") or ctx_ext.get("mentionedJidList")
                or []
            )
            text = self._resolve_mentions_in_text(text, mentioned)

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
                pct      = int(progress * 100)
                prog_str = i18n.t("downloading_progress").format(pct=pct)
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

    def _status_history_lines(self, msg, chat_jid: "str | None" = None) -> list:
        """Per-stage delivery/read/played timeline for a sent message, one
        line per stage actually reached ("Enviada: 14:29", "Entregue: 14:30",
        "Lida: 14:32", …), mirroring the official WhatsApp message-info
        screen. Only stages carrying a real timestamp (recorded from live
        messages.update events onward, see MainWindow.on_message_status_update)
        are shown — messages whose status was only ever seen as a single
        aggregate value (e.g. loaded from history sync) fall back to the
        caller's plain "Status: X" line instead, since no per-stage time
        exists for them."""
        i18n = self.main_window.i18n
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
                lines.append(f"{i18n.t(label_keys[stage])}: {self._format_date(ts)}")
        if failed_ts is not None:
            lines.append(f"{i18n.t('status_failed')}: {self._format_date(failed_ts)}")
        return lines

    def _sender_label(self, msg) -> str:
        if msg.get("key", {}).get("fromMe"):
            return self.main_window.self_reference_label()
        key         = msg.get("key", {})
        participant = key.get("participant", "")
        jid         = key.get("remoteJid", "")
        lookup_jid  = participant or jid
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
                    candidates.append(phone)
                    # contacts may be indexed under @c.us legacy format
                    candidates.append(phone.rsplit("@", 1)[0] + "@c.us")
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
            for cjid in candidates:
                c = mw.contacts.get(cjid)
                if c:
                    n = (c.get("name") or c.get("pushName") or "").strip()
                    if n and not mw._is_bad_contact_name(n):
                        return n
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

        # Don't use the group JID (@g.us) itself as a sender lookup — when
        # key.participant is absent, lookup_jid falls back to the remoteJid of
        # the group, and _contact_name would return the group name for every
        # message, making all messages appear to be from the same sender.
        if lookup_jid and not lookup_jid.endswith("@g.us"):
            n = _contact_name(lookup_jid)
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

    def _clear_empty_placeholder(self):
        """Remove the 'no messages' placeholder from the list if it is present."""
        if self._sorted_messages and isinstance(self._sorted_messages[0], dict) and self._sorted_messages[0].get("_type") == "empty_placeholder":
            self._sorted_messages.pop(0)
            self.messages_list.DeleteItem(0)
            self._recompute_unread_sep_idx()

    def _is_separator(self, msg: dict) -> bool:
        """Return True if msg is a non-message sentinel row (unread separator
        or the "no messages" placeholder) rather than a real message — every
        activation/edit/delete/etc. handler guards on this before touching
        msg["key"], so a new sentinel type just needs to be added here."""
        return isinstance(msg, dict) and msg.get("_type") in ("unread_separator", "empty_placeholder")

    def _recompute_unread_sep_idx(self):
        """Re-locate the unread separator's row index by scanning
        ``_sorted_messages`` from scratch, setting ``_unread_sep_idx`` to -1
        if it isn't present. Call after prepending older history — the
        separator's row shifts by however many rows were inserted above it,
        and a plain offset adjustment isn't available in every caller.
        Duplicated as an identical inline loop in two separate call sites
        before this existed.
        """
        self._unread_sep_idx = -1
        for idx, msg in enumerate(self._sorted_messages):
            if self._is_separator(msg):
                self._unread_sep_idx = idx
                break

    def _counts_toward_unread_separator(self, msg: dict) -> bool:
        """Mesmo teste que main.py usa para incrementar o unreadCount do chat.

        O preview da lista de conversas e o separador têm de contar a MESMA
        coisa. main.py só sobe o unreadCount para mensagens que passam por
        is_countable_message(), enquanto este caminho olhava apenas fromMe: um
        evento de sistema (groupNotification, protocolMessage,
        e2e_notification, ...) chegando numa conversa aberta subia o separador
        sem subir o preview, e a divergência aparecia exatamente como os dois
        números discordando.

        Consequência aceita, e não descuido: um groupNotification é EXIBÍVEL
        mas não contável, então "o separador diz 1 e há duas linhas abaixo
        dele" continua possível — e agora está certo, porque bate com o badge
        da lista de conversas, que também não contou o evento de sistema. A
        descrição solta desse estado é idêntica à do bug relatado; a diferença
        é a linha extra ser um evento, não uma mensagem.

        O import é feito aqui dentro de propósito: main.py importa este módulo,
        então um import no topo seria circular. O fallback não protege a
        mensagem (o append acontece fora deste ramo, ela aparece de qualquer
        forma) — protege só a contagem, preferindo contar a mais a perder um
        separador. E um erro real na cadeia de import de main.py não pode ser
        enterrado mudo aqui, daí o log.
        """
        try:
            from main import is_countable_message
        except Exception:
            logging.exception(
                "[_counts_toward_unread_separator] import de is_countable_message falhou"
            )
            return True
        return is_countable_message(msg)

    def _update_unread_separator_for_incoming(self, msg: dict) -> None:
        """Insere, move ou incrementa o separador para *msg*, que o chamador
        acrescenta à cauda logo em seguida.

        Grava sempre o par _first_unread_msg_id/_first_unread_count junto com a
        linha: é esse par, e só ele, que populate_messages() lê para recriar o
        separador depois do seu DeleteAllItems(). Enquanto este caminho
        escrevia apenas _sorted_messages e _unread_sep_idx, o primeiro rebuild
        (vários por minuto com a conversa aberta) apagava o separador e não o
        recriava — e sem separador _on_message_focused() nunca chamava
        mark_conversation_as_read(), então o chat ficava "1 mensagem não lida"
        no preview e não lido no celular até o usuário marcar à mão.

        Chamado de dentro do Freeze()/Thaw() de on_incoming_message(); não mexe
        em foco.
        """
        # Uma mensagem nova ao vivo sempre representa conteúdo genuinamente não
        # lido, mesmo que o usuário já tivesse chegado ao fim (e portanto
        # marcado como lida) antes nesta mesma sessão de conversa. Rearma a
        # trava para _on_message_focused() disparar o mark-as-read de novo
        # quando o foco alcançar/passar esta mensagem.
        self._unread_sep_marked_read = False
        msg_id = (msg.get("key") or {}).get("id") or None
        # _unread_sep_idx pode ficar velho para um _sorted_messages que foi
        # esvaziado/reconstruído por baixo dele sem voltar para -1 (ex.:
        # "Limpar conversa" no chat aberto) — todo ramo abaixo que indexa ou dá
        # pop() nele tem de tratar índice fora de faixa como "ainda não há
        # separador" em vez de estourar (visto ao vivo: "IndexError: pop from
        # empty list").
        sep_idx_valid = 0 <= self._unread_sep_idx < len(self._sorted_messages)
        if sep_idx_valid and not self._is_separator(
            self._sorted_messages[self._unread_sep_idx]
        ):
            sep_idx_valid = False
        if not sep_idx_valid:
            # Nenhum separador ainda — insere um antes desta mensagem nova.
            sep_pos = len(self._sorted_messages)
            sep = {"_type": "unread_separator", "count": 1}
            self._sorted_messages.insert(sep_pos, sep)
            self.messages_list.InsertItem(sep_pos, self._render_message_line(sep))
            self._unread_sep_idx = sep_pos
            self._sep_anchors_read_position = False
            self._first_unread_msg_id = msg_id
            self._first_unread_count = 1
        elif self._sep_anchors_read_position:
            # O foco do usuário já passou por este separador: ele ancora uma
            # posição lida. Move-o para antes desta mensagem e reinicia em 1.
            old_idx = self._unread_sep_idx
            self._sorted_messages.pop(old_idx)
            self.messages_list.DeleteItem(old_idx)
            sep_pos = len(self._sorted_messages)
            sep = {"_type": "unread_separator", "count": 1}
            self._sorted_messages.insert(sep_pos, sep)
            self.messages_list.InsertItem(sep_pos, self._render_message_line(sep))
            self._unread_sep_idx = sep_pos
            self._sep_anchors_read_position = False
            self._first_unread_msg_id = msg_id
            self._first_unread_count = 1
        else:
            # Separador ainda ancora conteúdo não lido (colocado na abertura da
            # conversa ou por uma mensagem ao vivo anterior, e o foco não passou
            # por ele): soma, sem mover. A âncora continua sendo a primeira
            # mensagem abaixo dele — só recalculada quando falta, para um
            # separador herdado de um estado que não gravava esse par.
            sep = self._sorted_messages[self._unread_sep_idx]
            sep["count"] = int(sep.get("count", 0) or 0) + 1
            # Reescrever esta linha tem custo de acessibilidade, e ele foi
            # pesado, não ignorado: a linha do wx.ListCtrl é um objeto MSAA
            # cujo nome é a linha inteira, e o event_nameChange do NVDA a fala
            # quando ela é a linha focada (ver "Played-row repaint hold" no
            # CLAUDE.md). Com focus_on_open == "unread_or_last" é exatamente
            # aqui que populate_messages() estaciona o foco de item, então cada
            # mensagem que chega pode fazer o leitor reler "N mensagens não
            # lidas". Não é regressão — o ramo antigo dava DeleteItem na mesma
            # linha focada, que também emite evento — e a alternativa (segurar
            # a escrita enquanto a linha está focada) é pior: um separador que
            # para de somar na tela mente sobre quantas mensagens chegaram, que
            # é o bug que este trecho existe para fechar. Fica a troca
            # registrada; a linha é reescrita, o número na tela é verdadeiro.
            self.messages_list.SetItemText(
                self._unread_sep_idx, self._render_message_line(sep)
            )
            # Recalculada SEMPRE, não só quando falta: _delete_message_rows()
            # ajusta _unread_sep_idx quando uma mensagem some, mas não toca na
            # âncora, então ela pode apontar para um id que não existe mais em
            # records. Sem recalcular aqui, o alinhamento de
            # _messages_signature_cache abaixo passaria a valer para um id
            # morto e _append_new_tail_rows() aceitaria uma tela que o rebuild
            # daquele instante não produziria. É idempotente no caso normal.
            self._first_unread_msg_id = (
                self._anchor_below_unread_separator() or msg_id
            )
            self._first_unread_count = sep["count"]
        # _first_unread_msg_id entra em _messages_signature() para que um
        # separador que mudou de lugar force um rebuild. Aqui quem o moveu foi
        # este método, na tela e em _sorted_messages ao mesmo tempo, então o
        # atalho de acrescentar na cauda continua correto — sem alinhar a
        # assinatura em cache, toda mensagem nova passaria a custar um rebuild
        # inteiro da lista debaixo do leitor de tela.
        cache = getattr(self, "_messages_signature_cache", None)
        if isinstance(cache, tuple) and len(cache) == 4:
            self._messages_signature_cache = (
                cache[0], self._first_unread_msg_id, cache[2], cache[3]
            )

    def _anchor_below_unread_separator(self):
        """Id da primeira mensagem de verdade abaixo do separador, ou None."""
        if not (0 <= self._unread_sep_idx < len(self._sorted_messages)):
            return None
        for m in self._sorted_messages[self._unread_sep_idx + 1:]:
            if not isinstance(m, dict) or self._is_separator(m):
                continue
            mid = (m.get("key") or {}).get("id")
            if mid:
                return mid
        return None

    def _place_unread_separator_for_rebuild(self, displayable: list) -> list:
        """Devolve *displayable* com o separador de não lidas na posição certa.

        Dois casos, e confundi-los é metade do bug do separador:

        - **Derivação**, quando _pending_open_unread traz a contagem tirada da
          lista de conversas no momento em que ela foi aberta. Aí o par
          âncora/contagem é calculado do zero e o separador NÃO ancora posição
          lida: as mensagens abaixo dele são genuinamente não lidas, então a
          próxima mensagem ao vivo tem de somar nele.
        - **Restauração**, quando o par já existe — tipicamente escrito pelo
          caminho ao vivo. Aí _sep_anchors_read_position e
          _unread_sep_marked_read são do separador que está sendo restaurado e
          têm de ser PRESERVADOS: marcá-los aqui fazia a próxima mensagem ao
          vivo mover o separador e reiniciar a contagem em 1 em vez de somar.

        Fora de populate_messages() (que precisa de um wx.ListCtrl de verdade)
        porque é exatamente este passo que desfazia o trabalho do caminho ao
        vivo, e ele precisava de teste.
        """
        unread_count = self._pending_open_unread
        self._pending_open_unread = 0
        first_unread_idx = first_unread_index(displayable, unread_count)
        if first_unread_idx >= 0:
            first_unread_msg = displayable[first_unread_idx]
            if isinstance(first_unread_msg, dict):
                self._first_unread_msg_id = first_unread_msg.get("key", {}).get("id")
                self._first_unread_count = unread_count
                self._sep_anchors_read_position = False
                self._unread_sep_marked_read = False

        if self._first_unread_msg_id:
            sep_pos = -1
            for idx, msg in enumerate(displayable):
                if isinstance(msg, dict) and msg.get("key", {}).get("id") == self._first_unread_msg_id:
                    sep_pos = idx
                    break
            if sep_pos >= 0:
                # max(1, ...) é puramente defensivo: um separador dizendo
                # "0 mensagens não lidas" seria uma linha sem sentido para
                # quem a ouvir. Consequência a saber caso alguém queira usar
                # 0 para "esconder o separador": aqui ele vira 1 em silêncio,
                # e o lugar de esconder é _dismiss_unread_separator().
                sep = {
                    "_type": "unread_separator",
                    "count": max(1, int(self._first_unread_count or 1)),
                }
                displayable = displayable[:sep_pos] + [sep] + displayable[sep_pos:]
                self._unread_sep_idx = sep_pos
        return displayable

    def _render_separator(self, count: int) -> str:
        i18n = self.main_window.i18n
        if count == 1:
            return i18n.t("unread_sep_singular")
        return i18n.t("unread_sep_plural").format(count=count)

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
        return msg.get("messageType") == "groupNotification"

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
        self.main_window.output(
            self.main_window.i18n.t("system_event_action_unavailable")
        )
        return True

    def _render_message_line(self, msg, index: int | None = None, total: int | None = None) -> str:
        """Produce the full display string for a single message row."""
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
        sender   = self._sender_label(msg)
        status   = self._map_status(msg)
        i18n     = self.main_window.i18n

        # Check for quoted/reply context
        ctx           = self._get_context_info(msg)
        quoted_sender = self._get_quoted_sender(ctx, msg) if ctx else ""

        if self._is_system_event(msg):
            # System events ("Carlos saiu do grupo", "Ana alterou o nome do
            # grupo") already name whoever acted, inside the sentence. Prefixing
            # them with the sender produced "Carlos: Carlos saiu do grupo",
            # which a screen reader reads out twice.
            pieces = [body]
        else:
            if quoted_sender:
                header = f"{sender}, {i18n.t('replying_to').format(name=quoted_sender)}"
            else:
                header = sender

            pieces = [f"{header}: {body}"]
        is_forwarded = not self._is_system_event(msg) and self._is_message_forwarded(msg)
        if msg.get("starred"):
            pieces[0] = f"★ {pieces[0]}"
        if msg.get("pinInChat"):
            pieces[0] = f"📌 {pieces[0]}"
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
        if time_str:
            pieces.append(f", {time_str}")
        if status:
            pieces[-1] += f", {status}"
        if msg.get("_edited") and not self._is_system_event(msg):
            pieces[-1] += f", {i18n.t('status_edited')}"
        if is_forwarded:
            pieces[-1] += f", {i18n.t('status_forwarded')}"

        # Append quoted message preview (if this is a reply)
        if ctx:
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

    # ── Download progress ───────────────────────────────────────────────────

    def update_message_download_progress(self, msg_id: str, progress: float):
        """
        Called from the main thread (via wx.CallAfter) when a media file's
        download progress changes.  Refreshes the relevant row in the list.
        """
        self._download_progress[msg_id] = progress
        self._update_media_transfer_gauge(progress)
        for i, msg in enumerate(self._sorted_messages):
            if msg.get("key", {}).get("id") == msg_id:
                self.messages_list.SetItemText(i, self._render_message_line(msg))
                break

    def update_media_upload_progress(self, upload_id: str, progress: float):
        try:
            progress = max(0.0, min(1.0, float(progress)))
        except (TypeError, ValueError):
            return
        previous = self._media_upload_progress.get(upload_id, 0.0)
        progress = max(previous, progress)
        self._media_upload_progress[upload_id] = progress
        self._media_transfer_started.add(upload_id)
        for index, msg in enumerate(self._sorted_messages):
            if msg.get("_local_id") != upload_id:
                continue
            self._update_media_transfer_gauge(progress)
            self.messages_list.SetItemText(index, self._render_message_line(msg))
            # wx.ListCtrl provides RefreshItem(), but the accessibility
            # fallback is a native wx.ListBox and only supports Refresh().
            refresh_item = getattr(self.messages_list, "RefreshItem", None)
            if refresh_item is not None:
                refresh_item(index)
            else:
                self.messages_list.Refresh()
            return
        self._hide_media_transfer_gauge()

    def _sync_pending_document_gauge(self, preferred_local_id: str = ""):
        """Restore progress only for the selected active transfer."""
        waiting = [
            msg for msg in self._sorted_messages
            if msg.get("_local_id") in self._media_transfer_started
            and msg.get("_local_pending")
        ]
        if not waiting:
            self._hide_media_transfer_gauge()
            return
        selected = self.messages_list.GetFirstSelected()
        selected_id = ""
        if 0 <= selected < len(self._sorted_messages):
            selected_id = self._sorted_messages[selected].get("_local_id", "")
        target_id = preferred_local_id or selected_id
        target = next(
            (msg for msg in waiting if msg.get("_local_id") == target_id),
            None,
        )
        if target is None:
            self._hide_media_transfer_gauge()
            return
        self._update_media_transfer_gauge(
            self._media_upload_progress.get(target_id, 0.0)
        )

    def _sync_media_action_slot_visibility(self):
        slot = getattr(self, "_media_action_slot", None)
        if slot is None:
            return
        controls = (
            getattr(self, "_media_transfer_gauge", None),
            getattr(self, "_action_open_btn", None),
            getattr(self, "_action_save_as_btn", None),
            getattr(self, "_action_download_btn", None),
        )
        visible = any(control is not None and control.IsShown() for control in controls)
        slot.Show(visible)
        self.conversation_panel.Layout()

    def _show_media_transfer_gauge(self):
        gauge = getattr(self, "_media_transfer_gauge", None)
        if gauge is None:
            return
        gauge.SetValue(1)
        gauge.Show()
        self._media_action_slot.Show()
        self.conversation_panel.Layout()

    def _update_media_transfer_gauge(self, progress: float):
        gauge = getattr(self, "_media_transfer_gauge", None)
        if gauge is None:
            return
        gauge.SetValue(max(0, min(100, round(progress * 100))))
        if not gauge.IsShown():
            gauge.Show()
            self._media_action_slot.Show()
            self.conversation_panel.Layout()

    def _hide_media_transfer_gauge(self):
        gauge = getattr(self, "_media_transfer_gauge", None)
        if gauge is None:
            return
        gauge.Hide()
        self._sync_media_action_slot_visibility()
        self.conversation_panel.Layout()

    # ── Ctrl+Shift+D / Ctrl+Shift+P dispatch ────────────────────────────────

    def _on_ctrl_shift_d(self, event):
        """Discard voice recording if active; otherwise show conversation data."""
        if self._is_recording:
            self._discard_voice_message(event)
        elif self.conversation is not None:
            self._show_conversation_data()

    def _on_ctrl_shift_p(self, event):
        """Pause/resume recording when active; otherwise pin/unpin the
        currently focused message. Both share this one accelerator — they
        are mutually exclusive contexts (pause/resume only ever does
        anything while actively recording audio), so there is no real
        conflict in practice."""
        if self._is_recording:
            self._toggle_pause_recording(event)
            return
        index = self.messages_list.GetFirstSelected()
        if index < 0:
            index = self.messages_list.GetFocusedItem()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        self._on_menu_pin_message(msg)

    # ── Conversation / group data ────────────────────────────────────────────

    def _show_conversation_data(self, event=None, chat=None):
        target = chat if chat is not None else self.conversation
        if target is None:
            return
        from ui.dialogs.conversation_data_dialog import ConversationDataDialog
        dlg = ConversationDataDialog(self.main_window, target)
        dlg.ShowModal()
        dlg.Destroy()

    def _fetch_and_update_profile(self, conversation: dict):
        """
        Background: fetch contact profile / group info and update the
        conversation-data button note with a last-seen or group-size string.

        For private chats the note comes from the _presence_cache (populated
        by presence.update WebSocket events) rather than from fetchProfile,
        because the WPPConnect API's fetchProfile response does not include
        lastSeen or online fields.
        """
        jid      = conversation.get("remoteJid", "")
        mw       = self.main_window
        i18n     = mw.i18n
        
        # Subscribe to presence updates for this conversation to receive typing/online events
        mw.subscribe_presence(jid)

        note = (
            mw._resolve_contact_name(conversation)
            or mw.find_name_through_messages(conversation)
            or conversation.get("name", "")
            or conversation.get("pushName", "")
            or format_number(jid)
        )
        try:
            if jid.endswith("@g.us"):
                data = mw.get_group_info(jid)
                # "size" may be absent in some WPPConnect API builds; fall back to
                # counting the participants list which is always present.
                participants = data.get("participants", [])
                size = data.get("size") or len(participants)
                group_name = (
                    mw._resolve_contact_name(conversation)
                    or mw.find_name_through_messages(conversation)
                    or conversation.get("name", "")
                    or conversation.get("pushName", "")
                    or format_number(jid)
                )
                note = f"{group_name}, {i18n.t('group_size').format(count=size)}"
            else:
                # Private chat: resolve the canonical JID for cache lookup
                canonical = mw._normalize_jid(jid)
                if canonical.endswith("@lid"):
                    mapped = getattr(mw, "_lid_to_phone", {}).get(canonical)
                    if not mapped:
                        logging.info(f"[_fetch_and_update_profile] On-demand JID mapping missing for {canonical}. Triggering background query.")
                        # Fetch profile in background to resolve JID mapping
                        mw.get_contact_profile(canonical)
                    canonical = getattr(mw, "_lid_to_phone", {}).get(canonical, canonical)
                presence = getattr(mw, "_presence_cache", {}).get(canonical, {})
                lkp      = presence.get("lastKnownPresence", "")
                # Fall back to a direct last-seen fetch when no presence event
                # has arrived yet (so the note isn't left without it).
                last_seen = presence.get("lastSeen") or mw.get_last_seen(canonical)
                if lkp in ("available", "composing", "recording"):
                    note = i18n.t("online_status")
                elif last_seen:
                    ls_str = _fmt_last_seen(last_seen, i18n)
                    if ls_str:
                        note = ls_str
        except Exception:
            pass

        def _update():
            if (self.conversation is not None
                    and self.conversation.get("remoteJid") == jid):
                try:
                    display_note = note
                    if not jid.endswith("@g.us") and is_phone_like(display_note):
                        display_note = f"{i18n.t('phone_label')}: {display_note}"
                    self._conv_data_btn.SetNote(display_note)
                    self.conversation_panel.Layout()
                except Exception:
                    pass

        wx.CallAfter(_update)

    def _refresh_presence_note(self, canonical_jid: str):
        """
        Called on the main thread by on_presence_update when a presence.update
        arrives for the currently open conversation.  Updates the button note
        immediately without going through the background-fetch path.
        """
        if self.conversation is None:
            return
        mw    = self.main_window
        i18n  = mw.i18n
        presence  = getattr(mw, "_presence_cache", {}).get(canonical_jid, {})
        lkp       = presence.get("lastKnownPresence", "")
        last_seen  = presence.get("lastSeen")

        jid = self.conversation.get("remoteJid", "")
        # Default note stays as the contact name
        note = (
            mw._resolve_contact_name(self.conversation)
            or mw.find_name_through_messages(self.conversation)
            or self.conversation.get("name", "")
            or self.conversation.get("pushName", "")
            or format_number(jid)
        )

        if lkp in ("available", "composing", "recording"):
            note = i18n.t("online_status")
        elif lkp == "unavailable" and last_seen:
            ls_str = _fmt_last_seen(last_seen, i18n)
            if ls_str:
                note = ls_str

        try:
            display_note = note
            if not jid.endswith("@g.us") and is_phone_like(display_note):
                display_note = f"{i18n.t('phone_label')}: {display_note}"
            self._conv_data_btn.SetNote(display_note)
            self.conversation_panel.Layout()
        except Exception:
            pass

    # ── Conversation context menu handlers ───────────────────────────────────

    def _on_menu_mark_read(self, jid: str):
        threading.Thread(
            target=self.main_window.mark_conversation_as_read,
            args=(jid,),
            daemon=True,
        ).start()

    def _on_menu_mark_unread(self, jid: str):
        self.main_window.mark_conversation_as_unread(jid)

    # Mute presets, in the order WhatsApp itself offers them. Kept in one place
    # so the row context menu and the Alt+Shift+S accelerator can never drift
    # apart — they build the exact same menu from this list.
    MUTE_PRESETS = (
        ("mute_1h", 3600),
        ("mute_3h", 10800),
        ("mute_8h", 28800),
        ("mute_1d", 86400),
        ("mute_1w", 604800),
        ("mute_always", -1),
    )

    def _build_mute_menu(self, jid: str) -> wx.Menu:
        """A wx.Menu of mute durations for *jid*, each wired to _on_menu_mute()."""
        i18n = self.main_window.i18n
        mute_sub = wx.Menu()
        for key, secs in self.MUTE_PRESETS:
            item = mute_sub.Append(wx.ID_ANY, i18n.t(key))
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid, s=secs: self._on_menu_mute(j, s),
                item,
            )
        return mute_sub

    def _popup_mute_menu(self, jid: str, anchor: wx.Window):
        """Alt+Shift+S: let the user pick how long to mute for.

        This used to mute for a hardcoded 8 hours with no prompt, so the
        shortcut could not express any of the durations the context menu
        offers — and silently picked one the user never chose. Show the same
        menu instead. Unmuting stays a single keypress: there is nothing to
        choose, and the context menu shows only "unmute" in that state too.
        """
        if self.main_window.is_chat_muted(jid):
            self._on_menu_unmute(jid)
            return
        i18n = self.main_window.i18n
        menu = wx.Menu(i18n.t("mute_chat_menu_title"))
        for key, secs in self.MUTE_PRESETS:
            item = menu.Append(wx.ID_ANY, i18n.t(key))
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid, s=secs: self._on_menu_mute(j, s),
                item,
            )
        # Popped up on the control that has keyboard focus so the screen reader
        # follows it there instead of to an arbitrary screen position.
        (anchor or self).PopupMenu(menu)
        menu.Destroy()

    def _on_menu_mute(self, jid: str, duration_secs: int):
        self.main_window.mute_chat(jid, duration_secs)

    def _on_menu_unmute(self, jid: str):
        self.main_window.unmute_chat(jid)

    def _on_menu_block(self, chat: dict, jid: str, currently_blocked: bool = False):
        name = (
            self.main_window._resolve_contact_name(chat)
            or self.main_window.find_name_through_messages(chat)
            or format_number(jid)
        )
        action = "unblock" if currently_blocked else "block"
        msg_key = "unblock_confirm_msg" if currently_blocked else "block_confirm_msg"
        title_key = "unblock_contact" if currently_blocked else "block_contact"
        msg = self.main_window.i18n.t(msg_key).format(name=name)
        if wx.MessageBox(
            msg,
            self.main_window.i18n.t(title_key),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) == wx.YES:
            threading.Thread(
                target=self.main_window.block_contact,
                args=(jid, action),
                daemon=True,
            ).start()

    def _on_menu_copy_number(self, jid: str):
        number = format_number(jid)
        try:
            pyperclip.copy(number)
        except Exception:
            pass

    def _on_menu_archive(self, jid: str):
        # Close conversation if currently open
        if self.conversation and self.conversation.get("remoteJid") == jid:
            self.close_conversation()
        self.main_window.archive_chat(jid)

    def _on_menu_unarchive(self, jid: str):
        self.main_window.unarchive_chat(jid)

    def _on_menu_pin(self, jid: str):
        self.main_window.pin_chat(jid)

    def _on_menu_unpin(self, jid: str):
        self.main_window.unpin_chat(jid)

    def _on_menu_clear_chat(self, jid: str):
        i18n = self.main_window.i18n
        if wx.MessageBox(
            i18n.t("clear_confirm_msg"),
            i18n.t("clear_chat"),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) != wx.YES:
            return
        self.main_window.clear_chat(jid)
        # Refresh messages list if this conversation is open
        if self.conversation and self.conversation.get("remoteJid") == jid:
            self._sorted_messages = []
            self.messages_list.DeleteAllItems()
            # _unread_sep_idx pointed into the list just emptied above — left
            # stale, a live message arriving right after (on_incoming_message,
            # the branch for a separator anchoring an already-read position)
            # would pop() that now-out-of-range index from the now-empty
            # _sorted_messages, crashing with
            # "IndexError: pop from empty list". Same pairing already reset
            # on conversation switch (see close_conversation()).
            self._unread_sep_idx = -1
            self._sep_anchors_read_position = False
            # A âncora vai junto: populate_messages() recria o separador a
            # partir dela, e um id que não existe mais em records deixaria a
            # conversa limpa carregando um separador fantasma.
            self._first_unread_msg_id = None
            self._first_unread_count = 0
        # Refresh the conversations list so the emptied preview disappears.
        # The conversation itself stays in the list — clearing is not deleting.
        self.main_window._schedule_set_chats()

    def _on_menu_delete_chat(self, jid: str):
        i18n = self.main_window.i18n
        # Deleting a group is local-only (see MainWindow.delete_chat: asking
        # WhatsApp to delete a group chat makes it exit the group first). Say so
        # in the prompt, so the difference from "Sair do grupo" is explicit.
        confirm_key = "delete_group_confirm_msg" if jid.endswith("@g.us") else "delete_confirm_msg"
        if wx.MessageBox(
            i18n.t(confirm_key),
            i18n.t("delete_chat"),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) != wx.YES:
            return
        if self.conversation and self.conversation.get("remoteJid") == jid:
            self.close_conversation()
        self.main_window.delete_chat(jid)

    def _on_menu_leave_group(self, jid: str):
        i18n = self.main_window.i18n
        # Own confirmation text: this one really does remove the user from the
        # group, and it used to share the generic "delete conversation" wording.
        if wx.MessageBox(
            i18n.t("leave_group_confirm_msg"),
            i18n.t("leave_group"),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) != wx.YES:
            return
        if self.conversation and self.conversation.get("remoteJid") == jid:
            self.close_conversation()
        threading.Thread(
            target=self.main_window.leave_group,
            args=(jid,),
            daemon=True,
        ).start()

    def _on_menu_add_member(self, group_jid: str):
        """Open the add-member dialog for a group."""
        from ui.dialogs.add_member_dialog import AddMemberDialog
        dlg = AddMemberDialog(self.main_window, group_jid)
        dlg.ShowModal()
        dlg.Destroy()

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
        msg_obj  = msg.get("message") or {}
        msg_type = msg.get("messageType", "")
        text = ""
        if msg_type == "conversation":
            text = msg_obj.get("conversation", "")
        elif msg_type == "extendedTextMessage":
            text = (msg_obj.get("extendedTextMessage") or {}).get("text", "")
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
        media_path = data_path("media", f"{msg_id}.wzmedia")

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
        if hasattr(self, "messages_list"):
            self.messages_list.SetFocus()

    def _on_menu_reply(self, msg: dict):
        """Enter reply mode: change field label, store quoted message, focus field."""
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
                candidates.append(phone)
                candidates.append(phone.rsplit("@", 1)[0] + "@c.us")
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
        for cjid in candidates:
            contact = mw.contacts.get(cjid)
            if contact:
                name = (contact.get("name") or contact.get("pushName") or "").strip()
                if name and not mw._is_bad_contact_name(name):
                    return name
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

    def refresh_active_conversation_messages(self, jids=None) -> int:
        """Re-render messages in the active message list (useful after
        background name/LID resolution). Returns how many rows it repainted.

        *jids* narrows the work to the rows whose text can depend on those
        JIDs. The message window has had no ceiling since it started
        preserving the history the user loads with Home, so a full pass is
        thousands of _render_message_line() calls once per resolved batch —
        while a batch typically renames one person. None (the default) keeps
        the original behaviour of re-rendering everything, and every path that
        cannot say with certainty which rows changed falls back to it.
        """
        if not self.conversation or not hasattr(self, "messages_list"):
            return 0
        target_ids = self._message_ids_touching_jids(jids) if jids else None
        # Mesma guarda de _repaint_message_rows(): _set_message_row_texts()
        # escreve por índice, então uma lista fora de passo com o controle põe
        # o texto certo na linha errada — e um descompasso só de prefixo não
        # levanta exceção nenhuma, o leitor de tela simplesmente passa a ler a
        # mensagem trocada. Degrada para o passe completo, que percorre as duas
        # em paralelo e no máximo pinta linhas a mais.
        if (target_ids is not None
                and self.messages_list.GetItemCount() != len(self._sorted_messages)):
            logging.info(
                "[refresh_active_conversation_messages] list out of step with rows "
                "— full path")
            target_ids = None
        # Um SetItemText por linha é um evento de acessibilidade por linha, e
        # os lotes de resolução de nomes/LID chamam isto repetidamente sobre a
        # lista inteira — sem congelar, o leitor de tela recebe a enxurrada e a
        # janela trava por segundos (ver as notas do watchdog em main.py).
        self.messages_list.Freeze()
        try:
            if target_ids is not None:
                # Reuses the same SetItemText loop the selection-marker
                # refresh goes through; it already renders with an explicit
                # index/total.
                return len(self._set_message_row_texts(target_ids))
            painted = 0
            failed = 0
            total = len(self._sorted_messages)
            for i, msg in enumerate(self._sorted_messages):
                if not self._is_separator(msg):
                    # Per row, not around the loop: a single malformed record
                    # used to abort the whole pass, so every row after it kept
                    # its old text — one bad message turning into a whole
                    # conversation that stops being repainted. Only the first
                    # traceback is logged, since this runs on a timer and a
                    # permanently bad record would otherwise fill log.log.
                    try:
                        # index/total explícitos como em _set_message_row_texts():
                        # sem eles o modo listbox com contagem de itens cai no
                        # fallback self._sorted_messages.index(msg), uma varredura
                        # linear com comparação profunda de dicts por linha — e duas
                        # mensagens de mesmo conteúdo anunciam a posição errada.
                        self.messages_list.SetItemText(
                            i, self._render_message_line(msg, index=i, total=total)
                        )
                    except Exception:
                        if not failed:
                            logging.exception(
                                "[refresh_active_conversation_messages] row %d "
                                "failed to render; skipping it and continuing.", i)
                        failed += 1
                    else:
                        painted += 1
            if failed > 1:
                logging.warning(
                    "[refresh_active_conversation_messages] %d of %d rows failed "
                    "to render.", failed, total)
            return painted
        finally:
            self.messages_list.Thaw()

    def _on_menu_reply_private(self, msg: dict, participant_jid: str):
        """Open a private conversation with the group participant and cite their message."""
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

    @staticmethod
    def _forward_target_chats(mw) -> tuple:
        """All chats offerable as a forward target: the main (non-archived)
        conversations_panel's own chats_list/chat_names, plus every archived
        chat from the separate ArchivedConversationsPanel that isn't already
        in that list. conversations_panel.chats_list alone only ever holds
        non-archived chats, so forwarding used to silently exclude every
        archived chat (not just groups) as a target."""
        panel     = mw.conversations_panel
        all_chats = list(panel.chats_list)
        all_names = list(panel.chat_names)
        seen_jids = {c.get("remoteJid", "") for c in all_chats}
        arch_panel = getattr(mw, "archived_conversations_panel", None)
        if arch_panel is not None:
            for chat, name in zip(arch_panel.chats_list, arch_panel.chat_names):
                jid = chat.get("remoteJid", "")
                if jid and jid not in seen_jids:
                    seen_jids.add(jid)
                    all_chats.append(chat)
                    all_names.append(name)
        return all_chats, all_names

    def _on_menu_forward(self, msg: dict, msgs_list: list = None):
        """Open a conversation-picker dialog and forward to the chosen chats.

        Forwards *msg* alone, or every message in *msgs_list* when a mass
        forward supplied one. System events (group notices) are dropped from
        the batch rather than aborting it — one accidentally-selected join
        notice must not cost the user the whole selection — and only a batch
        left with nothing at all is refused outright.
        """
        msgs_to_forward = [m for m in (msgs_list or [msg]) if not self._is_system_event(m)]
        if not msgs_to_forward:
            self._reject_system_event_action(msg)
            return
        mw   = self.main_window
        i18n = mw.i18n
        dlg_label = (
            i18n.t("forward_selected_messages_title") if len(msgs_to_forward) > 1
            else i18n.t("forward_message")
        )

        # ── Collect available conversations ───────────────────────────────────
        all_chats, all_names = self._forward_target_chats(mw)
        if not all_chats:
            return

        # ── Build a simple picker dialog ──────────────────────────────────────
        dlg = wx.Dialog(
            self,
            title=dlg_label,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=(400, 480),
        )
        p     = wx.Panel(dlg)
        vsz   = wx.BoxSizer(wx.VERTICAL)

        vsz.Add(
            wx.StaticText(p, label=i18n.t("forward_search_label")),
            0, wx.LEFT | wx.TOP | wx.RIGHT, 6,
        )
        search_field = wx.TextCtrl(p, style=wx.TE_PROCESS_ENTER)
        search_field.SetHint(i18n.t("search_conversations"))
        vsz.Add(search_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        lst = wx.ListBox(p, choices=all_names, style=wx.LB_SINGLE)
        vsz.Add(lst, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        vsz.Add(
            wx.StaticText(p, label=i18n.t("forward_multiselect_hint")),
            0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6,
        )

        # Offered as soon as ANY message in the batch carries a caption, not
        # just the first one — the first is simply whichever row the mass
        # forward happened to hand over, and keying the offer on it hid the
        # checkbox (silently dropping every other caption) whenever that row
        # was a plain text message.
        chk_keep_caption = None
        if any(message_caption(m) for m in msgs_to_forward):
            chk_keep_caption = wx.CheckBox(p, label=i18n.t("forward_keep_caption"))
            chk_keep_caption.SetValue(True)
            vsz.Add(chk_keep_caption, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # ── Selection state ──────────────────────────────────────────────────
        # lst is a single-selection wx.ListBox: native selection is only ever
        # "which row has keyboard focus", exactly like messages_list/
        # conversations_list's GetFocusedItem() elsewhere in this file. The
        # actual multi-select the mass forward acts on lives entirely in this
        # plain set, keyed by jid — mirroring self.selected_messages/
        # self.selected_chats, including the sound event, the "selected"/
        # "unselected"/"all_selected"/"all_unselected" announcements, and the
        # append/prepend "selected" text marker (selected_announcement_position
        # setting) — see _select_message_at()/_on_messages_list_key_down() for
        # the pattern this mirrors.
        #
        # An earlier version used wx.LB_EXTENDED instead, with its own native
        # multi-selection driven by hand through raw Win32 messages (so plain
        # Up/Down and letter type-ahead — which natively collapse an extended
        # selection to whatever row they land on — could be remapped to leave
        # it alone). That meant two independent copies of "what's selected"
        # that could drift out of sync with each other, reported live as
        # "comportamentos estranhos". LB_SINGLE removes the native multi-select
        # entirely, so arrows and type-ahead are simply left at their default
        # native behavior (event.Skip()) — there is nothing left for them to
        # collide with, and nothing custom left to keep in sync.
        selected_jids = set()

        def _jid_at(idx):
            if 0 <= idx < len(_filtered_chats):
                return _filtered_chats[idx].get("remoteJid", "")
            return ""

        def _row_text(idx):
            name = _filtered_names[idx] if idx < len(_filtered_names) else ""
            jid = _jid_at(idx)
            is_selected = bool(jid) and jid in selected_jids
            position = mw.settings.get("user_interface", {}).get(
                "selected_announcement_position", "end"
            )
            return append_selected_marker(name, i18n.t("selected_suffix"), position, is_selected)

        def _refresh_row(idx):
            if 0 <= idx < lst.GetCount():
                lst.SetString(idx, _row_text(idx))

        def _on_listbox_select(event):
            # Fires for any NATIVE focus change (arrow keys, letter
            # type-ahead, mouse click) — never for the SetSelection() calls
            # this dialog makes itself below, which is exactly the split
            # that's wanted: landing on a row already in selected_jids gets
            # the same audible cue selection_sound gives everywhere else,
            # without this dialog having to reimplement arrow/type-ahead
            # navigation by hand to get it.
            jid = _jid_at(lst.GetSelection())
            if jid and jid in selected_jids:
                self.selection_sound.play()
            event.Skip()

        lst.Bind(wx.EVT_LISTBOX, _on_listbox_select)

        def _on_list_key_down(event):
            key   = event.GetKeyCode()
            ctrl  = event.ControlDown()
            shift = event.ShiftDown()
            count = lst.GetCount()
            focus = lst.GetSelection()

            if shift and key in (wx.WXK_DOWN, wx.WXK_NUMPAD_DOWN):
                target = (focus + 1) if focus >= 0 else 0
                if target < count:
                    lst.SetSelection(target)
                    jid = _jid_at(target)
                    if jid and jid not in selected_jids:
                        selected_jids.add(jid)
                        _refresh_row(target)
                        self.selection_sound.play()
                        mw.output(i18n.t("selected"), interrupt=True)
                return

            if shift and key in (wx.WXK_HOME, wx.WXK_NUMPAD_HOME, wx.WXK_END, wx.WXK_NUMPAD_END):
                to_end = key in (wx.WXK_END, wx.WXK_NUMPAD_END)
                if count > 0:
                    focus0 = focus if focus >= 0 else 0
                    lo, hi = (focus0, count - 1) if to_end else (0, focus0)
                    newly = []
                    for i in range(lo, hi + 1):
                        jid = _jid_at(i)
                        if jid and jid not in selected_jids:
                            selected_jids.add(jid)
                            newly.append(i)
                    target = count - 1 if to_end else 0
                    lst.SetSelection(target)
                    for i in newly:
                        _refresh_row(i)
                    if newly:
                        self.selection_sound.play()
                        mw.output(i18n.t("selected"), interrupt=True)
                return

            if ctrl and shift and key == wx.WXK_SPACE:
                # Select every contact, or clear the selection if everything
                # is already selected.
                row_jids = [_jid_at(i) for i in range(count)]
                real_jids = [j for j in row_jids if j]
                all_selected = bool(real_jids) and all(j in selected_jids for j in real_jids)
                for i, jid in enumerate(row_jids):
                    if not jid:
                        continue
                    if all_selected:
                        selected_jids.discard(jid)
                    else:
                        selected_jids.add(jid)
                    _refresh_row(i)
                if real_jids:
                    if not all_selected:
                        self.selection_sound.play()
                    mw.output(i18n.t("all_unselected" if all_selected else "all_selected"), interrupt=True)
                return

            if ctrl and not shift and key == wx.WXK_SPACE:
                jid = _jid_at(focus)
                if jid:
                    now_selected = jid not in selected_jids
                    if now_selected:
                        selected_jids.add(jid)
                    else:
                        selected_jids.discard(jid)
                    _refresh_row(focus)
                    if now_selected:
                        self.selection_sound.play()
                    mw.output(i18n.t("selected" if now_selected else "unselected"), interrupt=True)
                return

            event.Skip()  # Arrows, letter type-ahead, everything else: native behavior

        lst.Bind(wx.EVT_KEY_DOWN, _on_list_key_down)

        btn_sizer  = wx.StdDialogButtonSizer()
        ok_btn     = wx.Button(p, wx.ID_OK,     label=i18n.t("forward_message"))
        cancel_btn = wx.Button(p, wx.ID_CANCEL, label=i18n.t("cancel"))
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        vsz.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 6)

        p.SetSizer(vsz)
        dlg_sz = wx.BoxSizer(wx.VERTICAL)
        dlg_sz.Add(p, 1, wx.EXPAND)
        dlg.SetSizer(dlg_sz)
        dlg.Layout()

        # Filter list as user types
        _filtered_chats = list(all_chats)
        _filtered_names = list(all_names)

        def _on_search(event):
            nonlocal _filtered_chats, _filtered_names
            q = search_field.GetValue().strip().lower()
            if q:
                pairs = [(c, n) for c, n in zip(all_chats, all_names)
                         if q in n.lower()]
            else:
                pairs = list(zip(all_chats, all_names))
            _filtered_chats = [c for c, _ in pairs]
            _filtered_names = [n for _, n in pairs]
            # Re-rendered with the "selected" marker for whichever of these
            # are still in selected_jids — that set is keyed by identity, so
            # a search that narrows/widens the visible rows doesn't reset it
            # the way it would if selection lived in the native widget.
            lst.Set([_row_text(i) for i in range(len(_filtered_names))])
            if _filtered_names:
                # Focus only — does not itself add to selected_jids, so
                # confirming right away without ever pressing Ctrl+Space
                # falls through to the "nothing selected -> use the focused
                # item" behavior below rather than silently forwarding to
                # whatever the search happened to focus first.
                lst.SetSelection(0)

        search_field.Bind(wx.EVT_TEXT, _on_search)
        if all_names:
            lst.SetSelection(0)
        ok_btn.SetDefault()

        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return

        if selected_jids:
            sels = [i for i in range(len(_filtered_chats)) if _jid_at(i) in selected_jids]
        else:
            # No explicit multi-selection was made (Ctrl+Space never
            # pressed) — forward to whichever contact is currently focused,
            # same as this dialog's original single-target behavior.
            focus = lst.GetSelection()
            sels = [focus] if focus >= 0 else []
        dlg.Destroy()
        if not sels:
            return

        target_jids = []
        target_names = []
        for i in sels:
            if i >= len(_filtered_chats):
                continue
            jid = _filtered_chats[i].get("remoteJid", "")
            if jid:
                target_jids.append(jid)
                target_names.append(_filtered_names[i])
        if not target_jids:
            return

        # Uses WPP.chat.forwardMessagesV2 server-side (main_window.forward_message),
        # which forwards the actual message as WhatsApp does — media, documents,
        # audio, captions, etc. all come through, unlike re-extracting the text
        # and sending it as a brand-new message. The forwarded copy arrives back
        # through the normal WebSocket echo, same as any other outgoing message.
        targets = list(zip(target_jids, target_names))
        keep_captions = chk_keep_caption.GetValue() if chk_keep_caption else False

        def _do_forward():
            failed_names = set()
            for i, m in enumerate(msgs_to_forward):
                # A short gap between messages when forwarding several at
                # once: back-to-back forwardMessagesV2 calls with no pause
                # are the trigger for the transient failure forward_message()
                # retries against (see its own comment) — spacing them out
                # here means most of the time the retry never has to fire.
                if i > 0:
                    time.sleep(0.4)
                msg_key = m.get("key", {}) or {}
                source_jid = msg_key.get("remoteJid") or (self.conversation.get("remoteJid", "") if self.conversation else "")
                if not source_jid or not msg_key.get("id"):
                    continue
                # Decided per message, never once for the batch: the
                # caption-preserving path is a media resend, so handing it a
                # plain text message (which a mass forward mixes in freely)
                # would push that message through the media call.
                keep = keep_captions and bool(message_caption(m))
                f_names = self._forward_message_to_targets(
                    m, targets, keep_caption=keep, source_jid_override=source_jid
                )
                failed_names.update(f_names)

            if failed_names:
                wx.CallAfter(mw.error_sound.play)
                if len(targets) == 1:
                    wx.CallAfter(mw.output, i18n.t("forward_failed"))
                else:
                    wx.CallAfter(
                        mw.output,
                        i18n.t("forward_failed_multiple").format(names=", ".join(failed_names)),
                    )

        threading.Thread(target=_do_forward, daemon=True).start()

    def _forward_message_to_targets(self, msg: dict, targets: list, keep_caption: bool = False, source_jid_override: str = "") -> list:
        """Forward one message to each (jid, name) pair in *targets*, one at
        a time — so one failing recipient (e.g. a stale JID) doesn't abort
        delivery to the rest.

        keep_caption applies to THIS message and must already account for
        whether it actually carries a caption (see message_caption()) — it is
        not a batch-wide flag: the True branch is a media resend and would
        misroute a plain text message.
        """
        mw = self.main_window
        failed = []
        msg_key = msg.get("key", {}) or {}
        source_jid = source_jid_override or msg_key.get("remoteJid") or ""

        for jid, name in targets:
            if keep_caption:
                success = mw.resend_media_message_with_caption(msg, jid)
            else:
                success = mw.forward_message(source_jid, msg_key, jid, source_msg=msg)
            if not success:
                failed.append(name)
        return failed

    def _persist_message_local_flag(self, jid: str, msg: dict):
        """Persist a message-level, locally-mutated field (e.g. "starred",
        "pinInChat") to that message's own row in the database.

        _schedule_save() only ever calls db.upsert_chat() — it writes chat
        metadata (name, unreadCount, last message preview, ...), never an
        individual row in the messages table. Meanwhile navigate_to_conversation()
        unconditionally reloads a conversation's messages fresh from the
        database every time it's opened. Without this, a flag toggled here
        lived only in the in-memory dict — correct until the user left and
        reopened the conversation (or any resync replaced the in-memory
        records), at which point it silently reverted, e.g. a starred
        message's context-menu item going back to "Favoritar" instead of
        staying "Desfavoritar".

        Runs on a background thread — db.insert_message() blocks the caller
        until the write completes (see DatabaseBridge), and this is always
        called from a UI event handler.
        """
        self._persist_message_local_flags(jid, [msg])

    def _persist_message_local_flags(self, jid: str, msgs: list):
        """Bulk form of _persist_message_local_flag() — persists several
        messages' locally-mutated flags on ONE background thread.

        The single-message version delegates here so both share one code
        path. It exists because the mass actions apply a flag to an entire
        selection at once: doing that through the single-message helper spun
        up one thread per message, and every one of them then blocked on the
        same serialized DatabaseBridge connection anyway (see its docstring —
        writes go through a single connection with a per-write asyncio.Lock),
        so the threads bought nothing and only multiplied.
        """
        db = getattr(self.main_window, "db", None)
        if db is None or not msgs:
            return
        def _do(j=jid, records=[dict(m) for m in msgs]):
            for record in records:
                try:
                    db.insert_message(j, record)
                except Exception as exc:
                    logging.warning("[_persist_message_local_flag] failed for %s: %s", j, exc)
        threading.Thread(target=_do, daemon=True).start()

    def _on_menu_star(self, msg: dict):
        if self._reject_system_event_action(msg):
            return
        msg["starred"] = not msg.get("starred")
        jid = self.conversation.get("remoteJid", "")
        if jid:
            self.main_window._schedule_save()
            self._persist_message_local_flag(jid, msg)
            self._repaint_or_repopulate([msg.get("key", {}).get("id", "")])

    def _on_menu_pin_message(self, msg: dict):
        """Pin/unpin a message via WhatsApp's own message-pin feature.

        Unlike _on_menu_star (a local-only flag), this is visible to every
        other participant in the chat, so it goes through the WPPConnect API
        — applied optimistically like conversation pin/unpin
        (_sync_pin_to_server), and rolled back if the server rejects it.
        """
        if self._reject_system_event_action(msg):
            return
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if not jid:
            return
        pin = not bool(msg.get("pinInChat"))
        msg["pinInChat"] = pin
        self.main_window._schedule_save()
        self._persist_message_local_flag(jid, msg)
        self._repaint_or_repopulate([msg.get("key", {}).get("id", "")])

        msg_key = dict(msg.get("key", {}))

        def _do(m=msg, k=msg_key, j=jid, p=pin):
            ok = self.main_window.pin_message(j, k, p)
            if not ok:
                wx.CallAfter(self._on_pin_message_failed, m, p)

        threading.Thread(target=_do, daemon=True).start()

    def _on_pin_message_failed(self, msg: dict, attempted_pin: bool):
        """Roll back an optimistic pin/unpin the server rejected (main thread)."""
        msg["pinInChat"] = not attempted_pin
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if jid:
            self._persist_message_local_flag(jid, msg)
        self.main_window._schedule_save()
        self._repaint_or_repopulate([msg.get("key", {}).get("id", "")])
        i18n = self.main_window.i18n
        wx.MessageBox(
            i18n.t("pin_message_failed" if attempted_pin else "unpin_message_failed"),
            i18n.t("pin_message"),
            wx.OK | wx.ICON_WARNING,
        )

    def _confirm_local_only_delete(self, count: int) -> bool:
        """Plain Delete/Cancel confirmation for a delete whose scope is
        already fixed to "for me only" — the "Me" chat's only real option
        (issue #73: "for everyone" is a no-op there). There is no scope left
        to choose, only the delete itself to confirm (issue #95). Shared by
        the single-message self-chat branch of _on_menu_delete_message() and
        the bulk self-chat branch of _on_mass_delete_messages().

        OK/Cancel rather than Yes/No, for two reasons that both matter to a
        keyboard-only user. wxMSW only sets the task dialog's
        "allow cancellation" flag when wxCANCEL is present, so a wxYES_NO
        prompt cannot be dismissed with Escape — this one is reached by a
        keystroke on a focused message, and would have been the single
        dialog in the app that swallows Escape. And wxCANCEL_DEFAULT keeps
        the destructive button off the default: Enter must not carry
        straight through from the message list into the delete, the same
        reasoning tests/test_update_dialog_default_button.py pins for the
        updater's own dialog.

        Both labels carry a mnemonic (delete_msg_confirm_yes is the
        Alt-accelerated form of delete_message): giving Cancelar an
        accelerator the other button lacks would leave the two buttons
        reachable in different ways."""
        i18n = self.main_window.i18n
        title = i18n.t("delete_message") if count == 1 else i18n.t("delete_messages_bulk_title")
        prompt = (
            i18n.t("delete_msg_confirm") if count == 1
            else i18n.t("delete_msg_confirm_bulk").format(count=count)
        )
        dlg = wx.MessageDialog(
            self, prompt, title,
            wx.OK | wx.CANCEL | wx.CANCEL_DEFAULT | wx.ICON_QUESTION,
        )
        dlg.SetOKCancelLabels(i18n.t("delete_msg_confirm_yes"), i18n.t("cancel"))
        result = dlg.ShowModal()
        dlg.Destroy()
        return result == wx.ID_OK

    def _on_menu_delete_message(self, index: int):
        """Show delete-scope dialog and delete locally or for everyone.

        The self-chat ("Me") skips the dialog entirely and always deletes
        locally only — see the is_self_chat check below (issue #73)."""
        if index < 0 or index >= len(self._sorted_messages):
            return
        if self._is_separator(self._sorted_messages[index]):
            return
        msg    = self._sorted_messages[index]
        msg_id = msg.get("key", {}).get("id", "")
        i18n   = self.main_window.i18n

        msg_key = msg.get("key", {})
        from_me = msg_key.get("fromMe", False)
        # Deleting a group notice locally is legitimate (it just hides the row),
        # so this is the one action system events keep. "For everyone" is not:
        # WhatsApp has no revoke for its own notices, so the request would only
        # fail after the row already looked deleted. Both the fromMe path and
        # the group-admin path below are excluded.
        is_system = self._is_system_event(msg)
        # The "Me" chat (messages to yourself) has only one participant —
        # WhatsApp's own revoke is a no-op there: the message disappears
        # locally, the API call returns success, but the message is still on
        # every other linked device, and reappears in WinZapp itself after
        # the next resync. Offering "delete for everyone" here just misleads
        # the user into thinking it worked (issue #73) — go straight to a
        # plain local delete instead, same as this chat's only real option.
        conv_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        is_self_chat = bool(conv_jid) and self.main_window._is_self_jid(conv_jid)
        can_delete_for_all = from_me and not is_system and not is_self_chat

        # There is only one scope possible here ("for me"), so there is
        # nothing to choose — but a delete is still a delete, and used to fire
        # with zero confirmation of any kind (issue #95). A plain Delete/
        # Cancel prompt replaces the for-me/for-everyone dialog below, which
        # would be misleading anyway (see the comment above).
        if is_self_chat and not is_system:
            if self._confirm_local_only_delete(1):
                self._delete_message_for_me_only(msg, msg_id, index)
            return

        if not can_delete_for_all and not is_system and self.conversation:
            if conv_jid.endswith("@g.us"):
                group_meta = self.conversation.get("groupMetadata", {})
                participants = group_meta.get("participants") or self.conversation.get("participants") or []

                def _phone_part(j: str) -> str:
                    return j.rsplit("@", 1)[0].split(":")[0] if isinstance(j, str) else ""

                my_phone = _phone_part(getattr(self.main_window, "my_jid", ""))
                my_lid   = _phone_part(getattr(self.main_window, "my_lid", ""))

                for p in participants:
                    if isinstance(p, dict):
                        p_id = p.get("id", "")
                        if isinstance(p_id, dict):
                            p_id = p_id.get("_serialized", "")
                        p_digits = _phone_part(p_id)
                        if p_digits:
                            is_me = (my_phone and self.main_window._phone_digits_equivalent(p_digits, my_phone)) or (my_lid and p_digits == my_lid)
                            if is_me:
                                if p.get("admin") or p.get("isAdmin"):
                                    can_delete_for_all = True
                                break

        # ── Ask the user: delete for me only, or for everyone ─────────────────
        dlg = wx.Dialog(
            self,
            title=i18n.t("delete_message"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        panel  = wx.Panel(dlg)
        sizer  = wx.BoxSizer(wx.VERTICAL)

        rb_me  = wx.RadioButton(panel, label=i18n.t("delete_for_me"), style=wx.RB_GROUP)
        rb_me.SetValue(True)
        sizer.Add(rb_me, 0, wx.ALL, 8)

        rb_all = None
        if can_delete_for_all:
            rb_all = wx.RadioButton(panel, label=i18n.t("delete_for_everyone"))
            sizer.Add(rb_all, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn     = wx.Button(panel, wx.ID_OK,     label=i18n.t("delete_message"))
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(panel, 1, wx.EXPAND)
        dlg.SetSizer(dlg_sizer)
        dlg.Fit()
        dlg.CentreOnParent()

        result       = dlg.ShowModal()
        for_everyone = rb_all.GetValue() if rb_all else False
        dlg.Destroy()

        if result != wx.ID_OK:
            return

        # Re-read the id: the dialog ran its own nested event loop, which is
        # where the worker's wx.CallAfter(_on_message_sent) gets dispatched — a
        # message that was still pending when the menu opened can have swapped
        # its local UUID for its real WhatsApp id by now, and removing the row
        # by the stale one matches nothing, leaving a just-revoked message
        # visible in the conversation.
        msg_id = msg.get("key", {}).get("id", "")
        msg_key = msg.get("key", {})
        jid = msg_key.get("remoteJid", "") or (
            self.conversation.get("remoteJid", "") if self.conversation else ""
        )

        pending_local_id = str(msg.get("_local_id") or "")
        # An unconfirmed send (see _mark_message_unconfirmed's docstring) has
        # no real WhatsApp id any more than a still-queued/in-flight one
        # does — WinZapp just never learned whether it actually went out —
        # so it belongs in the same "nothing to revoke, local delete only"
        # bucket as a cancelled pending send, not the fromMe/for-everyone
        # path below (which would build a revoke request around the local
        # UUID this row's key.id still holds and could only fail).
        cancelled_pending = bool(
            pending_local_id and (msg.get("_local_pending") or msg.get("_send_unconfirmed"))
        )
        if cancelled_pending:
            # An unconfirmed send shares the "nothing to revoke, local delete
            # only" path, but NOT the wait for an echo: its send already
            # finished and reported. Saying so explicitly matters because
            # cancel() answers False for both "a worker owns it" and "it is not
            # in the queue any more", and only the first justifies holding the
            # record.
            self._cancel_pending_message(
                msg, pending_local_id,
                hold_for_echo=bool(msg.get("_local_pending")),
            )
        elif for_everyone:
            # Revoke for everyone via WPPConnect API (off the UI thread). The
            # message key carries fromMe/participant so the server can build the
            # correct serialized id and actually revoke it.
            def _revoke(k=dict(msg_key), j=jid):
                ok = self.main_window.delete_message_for_everyone(j, k)
                if not ok:
                    wx.CallAfter(
                        wx.MessageBox,
                        i18n.t("delete_for_everyone_failed"),
                        i18n.t("delete_message"),
                        wx.OK | wx.ICON_WARNING,
                    )
            threading.Thread(target=_revoke, daemon=True).start()
            # Always delete locally
            if msg_id:
                self.remove_messages_by_id({msg_id}, focus_previous=True)
            else:
                self._sorted_messages.pop(index)
                self.messages_list.DeleteItem(index)
        else:
            self._delete_message_for_me_only(msg, msg_id, index)

    def _cancel_pending_message(self, msg: dict, pending_local_id: str,
                                hold_for_echo: bool = True):
        """Delete a message that is still pending — the delete-while-sending path.

        ``hold_for_echo=False`` is for a send that is already OVER: an
        unconfirmed one (_send_unconfirmed), where the worker finished and
        reported long ago. cancel() returns False for it — not because a worker
        still owns the message, but because it is no longer in the queue at all
        — so without this flag it would take the hold-for-echo tail below and
        be stashed waiting for an outcome report that has already happened and
        will never come again. The record would sit in the chat forever:
        invisible (_is_displayable_message and _counts_as_last_message both
        refuse _cancelled_awaiting_id), re-persisted on every save, and holding
        a slot in _cancelled_pending_messages. Nor can the echo matcher claim
        it, since that only considers _local_pending records and an unconfirmed
        one has that False.

        There is no WhatsApp message ID to revoke yet, so whichever scope the
        delete dialog had selected, this cancels the queued/in-flight send and
        applies a local deletion; asking the API for "everyone" here can only
        fail.

        When cancel() reports the send was stopped for good that is the whole
        story. When it does not — a worker already owns the message — the row
        still goes, but the *record* deliberately stays behind, marked
        _cancelled_awaiting_id and still _local_pending. That record is what
        on_new_message()'s by-type echo matching binds the echo to: the echo
        carries no correlation ID, so with this message's record gone the
        matcher would hand its WhatsApp ID to the next unrelated pending send of
        the same type, and no amount of registering IDs afterwards fixes that —
        the echo can (and routinely does) arrive before the send call has even
        returned. _counts_as_last_message() ignores the marker, so the chat list
        does not show a message the user just deleted, and the record is dropped
        or resolved for real the moment the queue reports the outcome (see
        discard_cancelled_message()/complete_cancelled_message_delivery()).
        """
        stopped = self.main_window.message_queue.cancel(pending_local_id)
        tracked = self._outgoing_virtual_messages.pop(pending_local_id, None)
        self._media_upload_progress.pop(pending_local_id, None)
        self._media_transfer_started.discard(pending_local_id)
        self._hide_media_transfer_gauge()
        record = tracked or msg
        chat = self.main_window.get_chat(record.get("key", {}).get("remoteJid", ""))
        position = self._record_position(chat, pending_local_id)
        # cancel() only stops the queue from ever sending it — the pending
        # bubble itself (key.id == pending_local_id for a virtual message)
        # stays in the list until removed here, same as the other two
        # branches in _on_menu_delete_message() do for their own message.
        self.remove_messages_by_id({pending_local_id}, focus_previous=True)
        if stopped:
            # Nothing was sent and nothing will be: the pre-cached copies
            # (voice_messages/<local_id>.msv, media/<local_id>.wzmedia) belong to
            # a message that no longer exists anywhere, and no later rename can
            # ever claim them.
            discard_local_media_cache(
                data_path("voice_messages"), data_path("media"), pending_local_id
            )
            return
        if not hold_for_echo:
            # The send already ran to completion and its outcome was already
            # reported; there is nothing left to wait for. Same disposal as the
            # stopped-for-good branch above.
            discard_local_media_cache(
                data_path("voice_messages"), data_path("media"), pending_local_id
            )
            return
        logging.info(
            "[conversations] %s was already being sent when it was cancelled — "
            "holding its record until the queue reports the outcome",
            pending_local_id,
        )
        record["_cancelled_awaiting_id"] = True
        self._remember_cancelled_pending(pending_local_id, record)
        if chat is None or position < 0:
            return
        # Back into the chat's records, at the position it had: on_new_message()
        # matches an echo to the FIRST pending record of its type, and records
        # are in send order, so appending this one at the end would hand its echo
        # to a message sent after it — the very swap this record exists to
        # prevent. Deliberately NOT back into the DB: remove_messages_by_id()
        # just deleted the stored copy, and leaving it deleted is the safer of
        # the two states to be caught in if the app dies inside this window — a
        # message that is gone rather than one stuck "sending" forever.
        #
        # The window is not fully closable in memory-only terms, and that is
        # accepted: if the echo claims this record before the outcome is known,
        # on_new_message() persists it as an ordinary sent message — marker and
        # all — so an app killed between that echo and the end of the revoke
        # leaves one stored record that both _counts_as_last_message() and
        # _is_displayable_message() refuse, i.e. invisible, with the chat
        # preview falling back to an older message. Both outcomes below fix the
        # stored copy (deleted on a successful revoke, rewritten clean on a
        # failed one); only being killed inside those few seconds does not.
        records = (
            chat.setdefault("messages", {})
                .setdefault("messages", {})
                .setdefault("records", [])
        )
        if not any(r.get("_local_id") == pending_local_id for r in records):
            records.insert(min(position, len(records)), record)

    @staticmethod
    def _record_position(chat: dict, local_id: str) -> int:
        """Index of a pending message inside its chat's records, or -1."""
        if not chat:
            return -1
        records = chat.get("messages", {}).get("messages", {}).get("records", [])
        for i, r in enumerate(records):
            if r.get("_local_id") == local_id:
                return i
        return -1

    def _delete_target_jid(self, msg_key: dict) -> str:
        """The chat a delete has to be addressed at.

        Normally that is the message's own key.remoteJid, and the open
        conversation is only a fallback for a record that somehow has none.
        The "Me" chat is the exception, for the same reason
        _receipts_are_meaningless() reads the chat rather than the key: it
        holds records whose key still carries the raw self-chat artifact JID
        ("<my digits>@g.us"), because _redirect_self_chat_artifact() files
        such a message under my_jid and deduplicate_chats()'s Pass 0a merges
        an already-stored phantom chat's records into it, and neither
        rewrites the key. Handing that JID to delete_message_for_me() builds
        phone="<my digits>@g.us", isGroup=True — a chat that does not exist
        server-side — so the delete silently does nothing and the message
        comes back on the next resync, which is issue #73's original symptom
        in the one chat this whole path exists for.

        Deliberately narrow: only the self-chat overrides the key, so a
        group or 1:1 delete resolves exactly as it always did.
        """
        conv_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if conv_jid and self.main_window._is_self_jid(conv_jid):
            return conv_jid
        return msg_key.get("remoteJid", "") or conv_jid

    def _delete_message_for_me_only(self, msg: dict, msg_id: str, index: int):
        """Delete a message for this account only (delete_message_for_me),
        then remove it locally — the plain "delete for me" path, shared by
        the dialog's own choice and the self-chat shortcut in
        _on_menu_delete_message() that skips the dialog entirely (issue #73:
        "delete for everyone" is a no-op there, since the "Me" chat has no
        one else to delete it for)."""
        msg_key = msg.get("key", {})
        jid = self._delete_target_jid(msg_key)

        def _delete_for_me(k=dict(msg_key), j=jid):
            self.main_window.delete_message_for_me(j, k)
        threading.Thread(target=_delete_for_me, daemon=True).start()

        if msg_id:
            self.remove_messages_by_id({msg_id}, focus_previous=True)
        else:
            self._sorted_messages.pop(index)
            self.messages_list.DeleteItem(index)

    def remove_messages_by_id(self, msg_ids: set, focus_previous: bool = False):
        """Remove every row whose key.id is in msg_ids from messages_list,
        _sorted_messages, _all_sorted_messages and self.conversation's
        records (plus the DB copy) — keeping the unread-separator index and
        pagination offset in sync with whatever just disappeared.

        Shared by _on_menu_delete_message() (single message, user-initiated)
        and MainWindow._mirror_remote_deletions() (a batch mirrored in from
        a phone-side deletion detected by the periodic poll).

        focus_previous=True re-focuses whatever row the user was actually on,
        adjusted for the rows that just disappeared, once done — WITHOUT
        calling messages_list.SetFocus(), so a background-triggered removal
        never steals keyboard focus from wherever the user actually is right
        now (e.g. the message field). Only when the row that was focused is
        itself one of the removed ones does this fall back to landing just
        before the earliest removed row (or row 0 if the removal started at
        the top) — the correct behaviour for the user-initiated single-delete
        path, where the deleted message IS what was focused. Without this
        distinction, _mirror_remote_deletions()'s 60s periodic poll yanked the
        user's focus to wherever the earliest of THAT PASS's removed messages
        happened to sit — often nowhere near what the user was actually
        reading — every time it mirrored so much as one stale message.
        """
        if not msg_ids:
            return
        # Stop playback before touching the list — a currently-playing audio
        # message may not even be in _sorted_messages any more (pagination
        # can scroll it out while it keeps playing in the background), so
        # this must not be gated on the row actually being found below.
        self._stop_playback_for_removed_messages(msg_ids)
        indices = sorted(
            i for i, m in enumerate(self._sorted_messages)
            if isinstance(m, dict) and m.get("key", {}).get("id") in msg_ids
        )
        if not indices:
            return
        earliest = indices[0]
        _preserved_msg_id = self._focused_msg_id() if focus_previous else ""
        _preserved_idx = self.messages_list.GetFocusedItem() if focus_previous else -1
        _preserved_was_separator = (
            focus_previous and self._unread_sep_idx >= 0
            and _preserved_idx == self._unread_sep_idx
        )
        # Keep the unread-separator index and the full (unpaginated) message
        # list in sync with the rows that just disappeared. Without this,
        # every later consumer of _unread_sep_idx (focus handling, the
        # dismiss timer, on_incoming_message's separator relocation) kept
        # operating on pre-delete rows — off by one for every message
        # deleted above the separator — and _load_more_messages()/
        # _load_older_messages() could re-introduce a just-deleted message
        # from the still-stale _all_sorted_messages on the next scroll-to-top.
        for idx in reversed(indices):
            self._sorted_messages.pop(idx)
            self.messages_list.DeleteItem(idx)
            if self._unread_sep_idx >= 0 and idx < self._unread_sep_idx:
                self._unread_sep_idx -= 1
        for i in range(len(self._all_sorted_messages) - 1, -1, -1):
            m = self._all_sorted_messages[i]
            if isinstance(m, dict) and m.get("key", {}).get("id") in msg_ids:
                self._all_sorted_messages.pop(i)
                if i < self._messages_offset:
                    self._messages_offset -= 1
        if self.conversation:
            records = (
                self.conversation.get("messages", {})
                .get("messages", {})
                .get("records", [])
            )
            self.conversation["messages"]["messages"]["records"] = [
                m for m in records
                if m.get("key", {}).get("id") not in msg_ids
            ]
            for mid in msg_ids:
                try:
                    self.main_window.db.delete_message(
                        self.conversation.get("remoteJid", ""), mid
                    )
                except Exception:
                    logging.exception("[conversations] delete_message failed for %s", mid)

            # The chat list's preview text and sort position both fall back to
            # chat["lastMessage"]/["t"] — without recomputing them here, a
            # deleted message kept showing as the preview and kept the chat
            # pinned at its old (now stale) position until the next full sync.
            jid = self.conversation.get("remoteJid", "")
            if jid:
                self.main_window._recompute_chat_last_message(jid)
                self.main_window._schedule_set_chats()

        if focus_previous:
            count = self.messages_list.GetItemCount()
            if count > 0:
                new_focus = -1
                if _preserved_msg_id and _preserved_msg_id not in msg_ids:
                    for idx, m in enumerate(self._sorted_messages):
                        if isinstance(m, dict) and m.get("key", {}).get("id") == _preserved_msg_id:
                            new_focus = idx
                            break
                elif _preserved_was_separator and self._unread_sep_idx >= 0:
                    new_focus = self._unread_sep_idx
                if new_focus < 0:
                    # The row that was focused is itself among the removed
                    # ones (or nothing usable was focused) — land just before
                    # the earliest removed row, same as before this fix.
                    new_focus = min(max(earliest - 1, 0), count - 1)
                self.messages_list.Focus(new_focus)
                self.messages_list.Select(new_focus, True)
                self.messages_list.EnsureVisible(new_focus)

    def _on_accel_edit_message(self, event):
        """Alt+E: enter edit mode for the focused own text message."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        if not msg.get("key", {}).get("fromMe", False):
            return
        if msg.get("messageType") not in ("conversation", "extendedTextMessage"):
            return
        if (time.time() - msg.get("messageTimestamp", 0)) >= 10800:
            return
        self._on_menu_edit_message(index, msg)

    def _on_menu_edit_message(self, index: int, msg: dict):
        """Enter edit mode: pre-fill message field with message text."""
        content = self._get_message_content(msg) or ""
        # Strip any leading quote block (from a previous reply prefix)
        if content.startswith("> ") and "\n" in content:
            content = content[content.index("\n") + 1:]

        self._editing_message_id    = msg.get("key", {}).get("id", "")
        self._editing_message_index = index

        # Seed the pending-mention state from the message being edited. The
        # pre-filled text shows mentions as "@DisplayName" (that is what
        # _get_message_content renders), and _build_mention_payload maps those
        # back to "@phone" on save — but only for JIDs it knows about. Without
        # this, changing a single word in a message that mentioned someone
        # stripped every mention from it. Uses the raw list rather than
        # _extract_mentions() so an @todos message keeps all its participants.
        self._pending_mentions.clear()
        self._pending_mention_display_names.clear()
        for jid in self._raw_mentioned_jids(msg):
            if not jid or jid in self._pending_mentions:
                continue
            self._pending_mentions.append(jid)
            self._pending_mention_display_names[jid] = self._get_participant_name(jid)
        self._rebuild_mention_pills()

        self.message_field.SetValue(content)
        self.message_field.SetInsertionPointEnd()
        self.message_field.SetFocus()

        # Show cancel button so the user knows they're in edit mode
        self._cancel_edit_btn.Show()
        self.conversation_panel.Layout()

    def _on_menu_resend_message(self, msg: dict):
        """Manually re-send a text message WinZapp itself never confirmed —
        the other recovery option besides dismissing it outright (see
        _on_menu_delete_message's cancelled_pending branch, which already
        treats this state as nothing-to-revoke).

        Deliberately does not attempt to preserve a quote or @mentions the
        original had: rebuilding those faithfully from contextInfo is more
        machinery than a rare manual recovery action warrants, and this
        must not read the composer's own current _quoted_message/
        _pending_mentions state either — those describe whatever the user
        is composing right now, unrelated to the row being resent. A resend
        goes out as plain text; if the quote mattered, the user can reply
        again themselves.
        """
        remote_jid = msg.get("key", {}).get("remoteJid", "") or (
            self.conversation.get("remoteJid", "") if self.conversation else ""
        )
        if not remote_jid:
            return

        # Read the WIRE text, never _get_message_content() — that one returns
        # what the LIST shows, which is not what was sent:
        #   * link_preview_text() PREPENDS the preview WhatsApp resolved for
        #     the URL, as "<title>. <description>. <text>". Resending that
        #     would deliver WhatsApp's own preview card to the recipient as
        #     literal characters in the message body.
        #   * _resolve_mentions_in_text() turns the stored "@554899..." back
        #     into "@João" for display. Resending that sends a literal
        #     "@João" — no mention, and a name string WhatsApp never saw.
        # The raw body has neither, so the "> " strip the edit path needs is
        # not needed here either (nothing in the send path ever writes that
        # prefix into message.conversation) — and doing it would silently
        # truncate a message from a user who legitimately types quote-style
        # lines.
        body = msg.get("message") or {}
        content = (
            body.get("conversation")
            or (body.get("extendedTextMessage") or {}).get("text")
            or ""
        )
        if not content:
            return

        old_local_id = str(msg.get("_local_id") or "")
        if old_local_id:
            # Harmless no-op on message_queue's side — an unconfirmed send
            # has already left its queue by definition — but still clears
            # this row's own tracking entries the same way a dismiss would.
            self.main_window.message_queue.cancel(old_local_id)
            self._outgoing_virtual_messages.pop(old_local_id, None)
            self.remove_messages_by_id({old_local_id}, focus_previous=True)

        local_id = str(uuid.uuid4())
        virtual_msg = {
            "_local_pending": True,
            "_local_id":      local_id,
            "key": {
                "id":       local_id,
                "fromMe":   True,
                "remoteJid": remote_jid,
            },
            "messageType":      "conversation",
            "message":          {"conversation": content},
            "messageTimestamp": int(time.time()),
            "pushName":         "",
        }
        self._clear_empty_placeholder()
        self._sorted_messages.append(virtual_msg)
        self.messages_list.Append((self._render_message_line(virtual_msg),))
        last = self.messages_list.GetItemCount() - 1
        if last >= 0:
            self.messages_list.EnsureVisible(last)

        self.main_window.message_queue.enqueue(
            PendingMessage(local_id, remote_jid, text=content)
        )

        self._register_virtual_msg(virtual_msg)
        self.main_window._schedule_set_chats()

    def _on_cancel_edit(self, event=None):
        """Leave edit mode without saving."""
        self._editing_message_id    = None
        self._editing_message_index = -1
        # Edit mode seeds these from the message being edited (see
        # _on_menu_edit_message) — drop them again, or the next ordinary message
        # typed into the field would inherit the edited message's mentions.
        self._pending_mentions.clear()
        self._pending_mention_display_names.clear()
        self._hide_mention_suggestions()
        self._rebuild_mention_pills()
        self.message_field.SetValue("")
        self._cancel_edit_btn.Hide()
        self.conversation_panel.Layout()
        self.message_field.SetFocus()

    def _on_cancel_reply(self, event=None):
        """Leave reply mode without sending."""
        self._quoted_message = None
        i18n     = self.main_window.i18n
        jid      = self.conversation.get("remoteJid", "") if self.conversation else ""
        is_group = jid.endswith("@g.us")
        label = (
            i18n.t("type_message_group") if is_group else i18n.t("type_message")
        )
        if self.conversation_name:
            label = f"{label} {self.conversation_name}"
        self.message_label.SetLabel(label)
        self._remove_quote_btn.Hide()
        self.conversation_panel.Layout()
        self.message_field.SetFocus()

    # ── Accelerator shims ─────────────────────────────────────────────────────

    def _on_accel_message_data(self, event):
        index = self.messages_list.GetFirstSelected()
        if 0 <= index < len(self._sorted_messages):
            self._on_menu_message_data(self._sorted_messages[index])

    def _on_accel_reply(self, event):
        index = self.messages_list.GetFirstSelected()
        if 0 <= index < len(self._sorted_messages):
            self._on_menu_reply(self._sorted_messages[index])

    def _on_accel_forward(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_messages:
            self._on_mass_forward_messages(event)
            return
        index = self.messages_list.GetFirstSelected()
        if 0 <= index < len(self._sorted_messages):
            self._on_menu_forward(self._sorted_messages[index])

    def _on_accel_delete_message(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_messages:
            self._on_mass_delete_messages(event)
            return
        index = self.messages_list.GetFirstSelected()
        if index >= 0:
            self._on_menu_delete_message(index)

    # ── Mass-action accelerators ─────────────────────────────────────────────
    # Their own shortcuts for every entry of the context menu's "Ações em
    # massa" submenu, so the submenu is no longer the only way to reach them
    # when Settings > Interface do usuário > "Substituir atalhos por ações em
    # massa ao selecionar conversas e mensagens" is off (that setting stays
    # exactly as it was: with it on, the single-message shortcuts act on the
    # whole selection — see _bulk_shortcuts_enabled).

    def _run_bulk_message_action(self, handler, event):
        """Shared body of the dedicated mass-action shortcuts below: they are
        inert without a selection, mirroring how the "Ações em massa" submenu
        isn't built at all until messages are selected.

        Inert, not silent: a shortcut that does nothing at all reads as
        broken to a screen-reader user, the same reason
        _on_action_save_as() announces save_as_nothing_to_save instead of
        just returning."""
        if not self.selected_messages:
            self.main_window.output(
                self.main_window.i18n.t("bulk_no_message_selection"), interrupt=True
            )
            return
        handler(event)

    def _on_accel_bulk_copy(self, event):
        """Ctrl+Alt+Shift+C: copy every selected message."""
        self._run_bulk_message_action(self._on_mass_copy_messages, event)

    def _on_accel_bulk_forward(self, event):
        """Ctrl+Alt+Shift+E: forward every selected message."""
        self._run_bulk_message_action(self._on_mass_forward_messages, event)

    def _on_accel_bulk_star(self, event):
        """Ctrl+Alt+Shift+F: star every selected message."""
        self._run_bulk_message_action(self._on_mass_star_messages, event)

    def _on_accel_bulk_pin(self, event):
        """Ctrl+Alt+Shift+X: pin every selected message in the chat."""
        self._run_bulk_message_action(self._on_mass_pin_messages, event)

    def _on_accel_bulk_save(self, event):
        """Ctrl+Alt+Shift+S: save every selected message's media."""
        self._run_bulk_message_action(self._on_mass_save_messages, event)

    def _on_accel_bulk_delete(self, event):
        """Ctrl+Shift+Delete: delete every selected message."""
        self._run_bulk_message_action(self._on_mass_delete_messages, event)

    def _on_accel_block(self, event):
        """Ctrl+Shift+B: block/unblock the current contact."""
        if self.conversation is None:
            return
        jid = self.conversation.get("remoteJid", "")
        if not jid or jid.endswith("@g.us"):
            return
        if self.main_window._is_self_jid(jid):
            return  # cannot block yourself
        self._on_menu_block(self.conversation, jid, self.main_window.is_contact_blocked(jid))

    def _on_accel_toggle_read(self, event):
        """Ctrl+Shift+M: mark conversation as read if it has unreads, else unread."""
        if self.conversation is None:
            return
        jid = self.conversation.get("remoteJid", "")
        if not jid:
            return
        if int(self.conversation.get("unreadCount") or 0) > 0:
            self.main_window.mark_conversation_as_read(jid)
        else:
            self.main_window.mark_conversation_as_unread(jid)

    def _on_accel_clear(self, event):
        """Ctrl+Shift+L: clear all local messages from the current conversation."""
        if self.conversation is None:
            return
        jid = self.conversation.get("remoteJid", "")
        if jid:
            self._on_menu_clear_chat(jid)

    def _on_accel_react(self, event):
        """Ctrl+Shift+R: open the reaction picker for the focused message."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if not self._is_separator(msg):
            self._on_menu_react(msg)

    def _on_accel_star(self, event):
        """Ctrl+Shift+I: star/favourite the focused message."""
        index = self.messages_list.GetFirstSelected()
        if 0 <= index < len(self._sorted_messages):
            msg = self._sorted_messages[index]
            if not self._is_separator(msg):
                self._on_menu_star(msg)

    def _on_accel_delete_conv(self, event):
        """Delete (in chat list): delete the focused conversation, or every
        selected conversation when a bulk selection exists (see
        _bulk_shortcuts_enabled)."""
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            self._on_mass_delete_chats(event)
            return
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid:
                self._on_menu_delete_chat(jid)

    def _selected_chat_from_list(self):
        selected = self.conversations_list.GetFirstSelected()
        if selected < 0:
            selected = self.conversations_list.GetFocusedItem()
        if 0 <= selected < len(self.chats_list):
            return self.chats_list[selected]
        return None

    def _on_accel_conversation_data_list(self, event):
        chat = self._selected_chat_from_list()
        if chat:
            self._show_conversation_data(chat=chat)

    def _on_accel_toggle_read_list(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            first_jid = next(iter(self.selected_chats))
            first_chat = next(
                (c for c in self.chats_list if c.get("remoteJid", "") == first_jid), None
            )
            if first_chat and int(first_chat.get("unreadCount") or 0) > 0:
                self._on_mass_mark_read_chats(event)
            else:
                self._on_mass_mark_unread_chats(event)
            return
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if int(chat.get("unreadCount") or 0) > 0:
            self._on_menu_mark_read(jid)
        else:
            self._on_menu_mark_unread(jid)

    def _on_accel_mute_list(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        self._popup_mute_menu(jid, self.conversations_list)

    def _on_accel_block_list(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid or jid.endswith("@g.us") or self.main_window._is_self_jid(jid):
            return
        self._on_menu_block(chat, jid, self.main_window.is_contact_blocked(jid))

    def _on_accel_clear_list(self, event):
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            self._on_mass_clear_chats(event)
            return
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid:
                self._on_menu_clear_chat(jid)

    def _on_accel_archive_list(self, event):
        # No bulk "unarchive" action exists in the mass-actions submenu, so
        # the shortcut always archives when a selection exists — matching
        # what "Ações em massa > Arquivar conversas selecionadas" does.
        if self._bulk_shortcuts_enabled() and self.selected_chats:
            self._on_mass_archive_chats(event)
            return
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if self.main_window.is_chat_archived(jid):
            self._on_menu_unarchive(jid)
        else:
            self._on_menu_archive(jid)

    def _on_accel_pin_list(self, event):
        """Play/stop the recorded-audio preview while the voice recording is
        paused; otherwise pin/unpin the focused conversation. Both share
        this one accelerator (Ctrl+P) — mutually exclusive contexts, same
        pattern as _on_ctrl_shift_p uses for Ctrl+Shift+P."""
        if self._is_recording and self._recording_paused:
            self._toggle_play_recorded_audio(event)
            return
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if self.main_window.is_chat_pinned(jid):
            self._on_menu_unpin(jid)
        else:
            self._on_menu_pin(jid)

    def _run_bulk_chat_action(self, handler, event):
        """Chat-list twin of _run_bulk_message_action(): the dedicated
        mass-action shortcuts below are inert without a selection, mirroring
        how the chat list's "Ações em massa" submenu isn't built until
        conversations are selected — and announce that rather than doing
        nothing at all, which reads as a broken shortcut to a screen-reader
        user."""
        if not self.selected_chats:
            self.main_window.output(
                self.main_window.i18n.t("bulk_no_chat_selection"), interrupt=True
            )
            return
        handler(event)

    def _on_accel_bulk_clear_chats(self, event):
        """Ctrl+Alt+Shift+L: clear every selected conversation."""
        self._run_bulk_chat_action(self._on_mass_clear_chats, event)

    def _on_accel_bulk_delete_chats(self, event):
        """Ctrl+Shift+Delete: delete every selected conversation."""
        self._run_bulk_chat_action(self._on_mass_delete_chats, event)

    def _on_accel_bulk_archive_chats(self, event):
        """Ctrl+Alt+Shift+A: archive every selected conversation."""
        self._run_bulk_chat_action(self._on_mass_archive_chats, event)

    def _on_accel_bulk_read_chats(self, event):
        """Ctrl+Alt+Shift+R: mark every selected conversation as read."""
        self._run_bulk_chat_action(self._on_mass_mark_read_chats, event)

    def _on_accel_bulk_unread_chats(self, event):
        """Ctrl+Alt+Shift+U: mark every selected conversation as unread."""
        self._run_bulk_chat_action(self._on_mass_mark_unread_chats, event)

    def _on_accel_copy_message(self, event):
        """Ctrl+C: copy focused message text or media file (with original
        filename) to clipboard — or, with a bulk selection and Settings >
        Interface do usuário > "Substituir atalhos por ações em massa..."
        on, copy every selected plain-text message instead (see
        _on_mass_copy_messages)."""
        if self._bulk_shortcuts_enabled() and self.selected_messages:
            self._on_mass_copy_messages(event)
            return
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        msg_type = msg.get("messageType", "")
        msg_obj  = msg.get("message") or {}
        # Text messages store the payload as a plain string under the
        # messageType key (e.g. {"conversation": "..."}), not a dict — guard
        # before calling .get() on it.
        inner = msg_obj.get(msg_type)
        if not isinstance(inner, dict):
            inner = {}
        media_data = msg.get("mediaData") or {}
        is_ptt   = bool(inner.get("ptt", False) or inner.get("isPtt", False) or media_data.get("ptt", False))

        if msg_type in ("imageMessage", "videoMessage", "documentMessage", "audioMessage"):
            self._on_menu_copy_file(msg)
        elif msg_type == "contactMessage":
            # Ctrl+C on a contact card copies the phone number, not the row's
            # rendered text — a card has no body to copy, and the number is the
            # only thing anyone wants off it (issue #84).
            self._on_contact_copy_number(msg)
        else:
            self._on_menu_copy_message(msg)

    def _on_accel_copy_caption(self, event):
        """Ctrl+Shift+C: copy the caption of the focused photo/video/document
        message. Kept on a separate shortcut from Ctrl+C, which already
        copies the file itself for these message types."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        self._on_menu_copy_caption(msg)

    def _on_accel_show_text_popup(self, event):
        """Alt+C: show focused message text in a popup dialog."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        self._show_message_text_popup(msg)

    # ── Alt+Shift+L / Alt+Shift+K: announce message status / date-time ────

    def _on_accel_msg_status(self, event):
        """Alt+Shift+L: speak the focused message's current status."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        i18n   = self.main_window.i18n
        status = self._map_status(msg)
        self.main_window.output(status or i18n.t("msg_status_none"), interrupt=True)

    def _on_accel_msg_datetime(self, event):
        """Alt+Shift+K: speak the focused message's date/time, as shown in the list."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        ts       = self._extract_timestamp(msg)
        date_str = self._format_date(ts) if ts else ""
        i18n     = self.main_window.i18n
        self.main_window.output(
            date_str or i18n.t("msg_datetime_none"), interrupt=True
        )

    # ── Alt+Shift+R: reply privately ────────────────────────────────────────

    def _on_accel_reply_private(self, event):
        """Alt+Shift+R: reply privately to the focused group message."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        jid      = self.conversation.get("remoteJid", "") if self.conversation else ""
        from_me  = msg.get("key", {}).get("fromMe", False)
        if not jid.endswith("@g.us") or from_me:
            return
        participant_jid = (
            msg.get("key", {}).get("participant", "")
            or msg.get("participant", "")
        )
        if participant_jid:
            self._on_menu_reply_private(msg, participant_jid)

    # ── Alt+Shift+C: copy phone number + speak ──────────────────────────────

    def _copy_and_speak_jid(self, jid: str):
        """Internal: copy formatted phone number for jid to clipboard and speak it."""
        if not jid or jid.endswith("@g.us"):
            return
        number = format_number(jid)
        if not number:
            return
        try:
            pyperclip.copy(number)
        except Exception:
            pass
        self.main_window.speak_output.output(number)

    def _on_accel_copy_number_speak(self, event):
        """Alt+Shift+C (conversation panel): copy current conversation's phone number."""
        if self.conversation is None:
            return
        self._copy_and_speak_jid(self.conversation.get("remoteJid", ""))

    def _on_accel_copy_number_list(self, event):
        """Alt+Shift+C (chat list): copy selected conversation's phone number."""
        idx = self.conversations_list.GetFirstSelected()
        if idx < 0 or idx >= len(self.chats_list):
            # Fall back to the currently open conversation if nothing selected
            if self.conversation:
                self._copy_and_speak_jid(self.conversation.get("remoteJid", ""))
            return
        self._copy_and_speak_jid(self.chats_list[idx].get("remoteJid", ""))

    # ── Alt+Shift+V: converse with participant ───────────────────────────────

    def _on_accel_alt_shift_v(self, event):
        """Alt+Shift+V: open a private chat with the focused group message's author."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        jid     = self.conversation.get("remoteJid", "") if self.conversation else ""
        from_me = msg.get("key", {}).get("fromMe", False)
        if jid.endswith("@g.us") and not from_me:
            participant_jid = (
                msg.get("key", {}).get("participant", "")
                or msg.get("participant", "")
            )
            if participant_jid:
                pname = self._get_participant_name(participant_jid, msg)
                self._on_menu_converse_private(participant_jid, pname)

    # ── Alt+Shift+Q: goto quoted message ────────────────────────────────────────

    def _on_accel_goto_quoted(self, event):
        """Alt+Shift+Q: navigate to the quoted message of the focused message."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return
        ctx = self._get_context_info(msg)
        if ctx:
            self._on_menu_goto_quoted(msg, ctx)

    # ── Alt+Shift+S: mute / unmute conversation ──────────────────────────────

    def _on_accel_mute(self, event):
        """Alt+Shift+S: open the mute-duration menu (or unmute if already muted)."""
        if self.conversation is None:
            return
        jid = self.conversation.get("remoteJid", "")
        if not jid:
            return
        self._popup_mute_menu(jid, wx.Window.FindFocus() or self.messages_list)

    # ── Ctrl+N: nova conversa ─────────────────────────────────────────────────

    def _on_new_conversation(self, event=None):
        """Ctrl+N / Nova conversa button: open the New Conversation dialog."""
        from ui.dialogs.new_conversation import NewConversationDialog
        dlg = NewConversationDialog(self.main_window)
        dlg.ShowModal()
        dlg.Destroy()

    # ── Alt+2: jump to last message ────────────────────────────────────────

    def _on_accel_jump_last(self, event):
        """Alt+2: move focus to the last REAL message in the current
        conversation — never a sentinel row (unread separator/placeholder).
        The bottom row is a sentinel whenever the unread separator gets
        (re)placed at the very end of the list, e.g. right after
        on_incoming_message() creates a fresh one for a message that just
        arrived; skip backwards over any such rows instead of focusing them
        directly, or Alt+2 would land on the separator (or, before that,
        potentially on a stale earlier row) instead of the newest message.
        """
        if self._no_conversation_open_announced():
            return
        last = len(self._sorted_messages) - 1
        while last >= 0 and self._is_separator(self._sorted_messages[last]):
            last -= 1
        if last < 0:
            # Nothing but sentinel rows: the conversation is open but empty, so
            # the loop above walked off the top of the list. Alt+2 is defined as
            # "focus the last real message", and the empty-list placeholder is
            # not one (no more than the unread separator is) — so say the chat is
            # empty instead of focusing the placeholder, which is what issue #87
            # settled on. Silence here read as a dead shortcut.
            self.main_window.output(
                self.main_window.i18n.t("chat_is_empty"), interrupt=True
            )
            return
        if last >= 0:
            # Focus() (not just Select()) is what actually moves the
            # keyboard-focus/screen-reader cursor to the row — every other
            # "jump to a specific row" handler in this file calls both
            # together (see _on_accel_jump_unread() just below, and
            # populate_messages()'s own default-tail-selection block).
            # This one only ever called Select(), which on its own can
            # leave the previous row's focus rectangle in place or just
            # clear the old selection without moving anything — reported
            # live as Alt+2 "either staying put or just deselecting the
            # current message without really moving focus".
            self.messages_list.Focus(last)
            self.messages_list.Select(last, True)
            self.messages_list.EnsureVisible(last)
            self.messages_list.SetFocus()

    # ── Alt+3: jump to unread separator ────────────────────────────────────

    def _on_accel_jump_unread(self, event):
        i18n = self.main_window.i18n
        if self._no_conversation_open_announced():
            return
        if self._unread_sep_idx < 0 or self._unread_sep_idx >= self.messages_list.GetItemCount():
            self.main_window.output(i18n.t("no_unread_in_conv"), interrupt=True)
            return
        self.messages_list.Focus(self._unread_sep_idx)
        self.messages_list.Select(self._unread_sep_idx, True)
        self.messages_list.EnsureVisible(self._unread_sep_idx)
        self.messages_list.SetFocus()
        self.main_window.output(
            self.messages_list.GetItemText(self._unread_sep_idx),
            interrupt=True,
        )
        # mark_conversation_as_read is triggered by _on_message_focused which
        # fires when Focus() is called above — no need to call it here again.

    # ── Ctrl+0..9 / Ctrl+Shift+0..9: message bookmarks ──────────────────────
    # Bookmarks span conversations (see _msg_bookmarks' declaration in
    # __init__): jumping to one set in a conversation other than the one
    # currently open navigates there first, then focuses the bookmarked
    # message — never cleared by closing/switching conversations.

    def _find_index_by_msg_id(self, msg_id: str) -> int:
        """Return the current _sorted_messages index for a message ID, or -1."""
        if not msg_id:
            return -1
        for i, m in enumerate(self._sorted_messages):
            if (isinstance(m, dict) and not self._is_separator(m)
                    and m.get("key", {}).get("id") == msg_id):
                return i
        return -1

    def _conversation_position(self, jid: str) -> int:
        """1-based position of *jid* in the currently displayed (non-archived)
        conversation list, or 0 if it isn't currently shown there (archived,
        filtered out by search/the active filter, etc.). Chats reorder
        whenever new messages arrive, so this is always looked up live —
        never cached alongside a bookmark."""
        for i, chat in enumerate(self.chats_list):
            if chat.get("remoteJid", "") == jid:
                return i + 1
        return 0

    def _already_on_message_row(self, idx: int) -> bool:
        """True when the messages list already holds the keyboard focus, on
        exactly this row — i.e. jumping here would move nothing.

        Both conditions matter. The row cursor alone is not enough: it survives
        the user tabbing away to the message field, and in that state a jump
        does have work to do (bring focus back into the list), so announcing
        "you are already there" and stopping would strand the focus where it
        was. Only when the list itself is focused *and* sitting on the row is
        the jump genuinely a no-op.
        """
        try:
            return self.messages_list.GetFocusedItem() == idx and self.messages_list.HasFocus()
        except Exception:
            return False

    def _focus_message_row(self, idx: int):
        """Move focus + selection to a message row and scroll it into view.

        Focus() alone moves the screen-reader cursor without selecting, and
        Select() alone selects a row the keyboard cursor isn't on — both
        bookmark kinds want the row to become the one and only current
        message, which takes all four calls together.
        """
        self.messages_list.Focus(idx)
        self.messages_list.Select(idx, True)
        self.messages_list.EnsureVisible(idx)
        self.messages_list.SetFocus()

    def _select_bookmarked_message(self, digit: int, jid: str, msg_id: str, i18n,
                                    other_conversation: bool):
        """Focus/select *msg_id* in the (already-open) conversation's message
        list and announce the jump. Shared by the same-conversation and
        just-navigated-to-a-different-conversation cases below — the only
        difference is which text explains where the message ended up."""
        idx = self._find_index_by_msg_id(msg_id)
        if idx < 0:
            self._msg_bookmarks.pop(digit, None)
            self.main_window.output(
                i18n.t("bookmark_not_found").format(digit=digit), interrupt=True
            )
            return
        # Only in the same conversation: having just navigated to another one,
        # the user did move, even if the row index happens to coincide with
        # the one the newly opened conversation focused on its own.
        if not other_conversation and self._already_on_message_row(idx):
            self.main_window.output(
                i18n.t("bookmark_already_there").format(position=idx + 1, digit=digit),
                interrupt=True,
            )
            return
        self._focus_message_row(idx)
        if not other_conversation:
            self.main_window.output(
                i18n.t("bookmark_jumped").format(position=idx + 1, digit=digit),
                interrupt=True,
            )
            return
        conv_position = self._conversation_position(jid)
        conv_name = self.conversation_name or jid
        if conv_position:
            text = i18n.t("bookmark_jumped_other_conversation").format(
                position=idx + 1, digit=digit, conv_position=conv_position, conv_name=conv_name,
            )
        else:
            text = i18n.t("bookmark_jumped_other_conversation_no_position").format(
                position=idx + 1, digit=digit, conv_name=conv_name,
            )
        self.main_window.output(text, interrupt=True)

    def _on_bookmark_set_or_jump(self, digit: int):
        """Ctrl+<digit>: bookmark the focused message, or — if <digit> already
        has a bookmark — move focus/selection to it instead, navigating to
        the bookmark's own conversation first if it's not the one currently
        open.

        Bookmarks store (conversation JID, message key.id) rather than a raw
        list index/position, so a bookmark still finds the right message
        even if either list was rebuilt/reordered (a new message arriving,
        pagination, etc.) between setting it and jumping to it.
        """
        i18n = self.main_window.i18n
        existing = self._msg_bookmarks.get(digit)
        if existing is not None:
            bm_jid, existing_id = existing
            current_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
            if bm_jid == current_jid:
                self._select_bookmarked_message(digit, bm_jid, existing_id, i18n, other_conversation=False)
                return
            target_chat = self.main_window.chats.get(bm_jid)
            if target_chat is None:
                # The whole conversation is gone (chat deleted) — nothing
                # left to jump to.
                del self._msg_bookmarks[digit]
                self.main_window.output(
                    i18n.t("bookmark_not_found").format(digit=digit), interrupt=True
                )
                return
            self.navigate_to_conversation(target_chat)
            # navigate_to_conversation() queues its own focus (message field
            # or messages list, per the "focus_on_open" setting) via
            # wx.CallAfter — ours must be queued AFTER that call returns so
            # it runs last and wins, same ordering this file already relies
            # on elsewhere (see navigate_to_conversation()'s own comment
            # about focus-CallAfter ordering).
            wx.CallAfter(
                self._select_bookmarked_message, digit, bm_jid, existing_id, i18n, True
            )
            return

        idx = self.messages_list.GetFocusedItem()
        if idx < 0 or idx >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[idx]
        if self._is_separator(msg):
            return
        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id:
            return
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if not jid:
            return
        self._msg_bookmarks[digit] = (jid, msg_id)
        conv_position = self._conversation_position(jid)
        if conv_position:
            text = i18n.t("bookmark_set").format(
                digit=digit, position=idx + 1, text=self.messages_list.GetItemText(idx),
                conv_position=conv_position,
            )
        else:
            text = i18n.t("bookmark_set_no_position").format(
                digit=digit, position=idx + 1, text=self.messages_list.GetItemText(idx),
            )
        self.main_window.output(text, interrupt=True)

    def _on_bookmark_remove(self, digit: int):
        """Ctrl+Shift+<digit>: remove the bookmark at that digit, if any."""
        i18n = self.main_window.i18n
        existing = self._msg_bookmarks.pop(digit, None)
        if existing is None:
            self.main_window.output(
                i18n.t("bookmark_not_found").format(digit=digit), interrupt=True
            )
            return
        bm_jid, existing_id = existing
        current_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if bm_jid != current_jid:
            # Can't cheaply confirm a bookmark set in another (possibly
            # unloaded) conversation still points at a real message without
            # loading that conversation's messages just to check — trust it
            # and confirm the removal without a position.
            self.main_window.output(
                i18n.t("bookmark_removed_other_conversation").format(digit=digit), interrupt=True
            )
            return
        idx = self._find_index_by_msg_id(existing_id)
        if idx < 0:
            self.main_window.output(
                i18n.t("bookmark_removed_stale").format(digit=digit), interrupt=True
            )
            return
        conv_position = self._conversation_position(bm_jid)
        if conv_position:
            text = i18n.t("bookmark_removed").format(
                digit=digit, position=idx + 1, conv_position=conv_position,
            )
        else:
            text = i18n.t("bookmark_removed_no_position").format(
                digit=digit, position=idx + 1,
            )
        self.main_window.output(text, interrupt=True)

    # ── Alt+Shift+0..9 / Ctrl+Alt+Shift+0..9: temporary bookmarks ──────────
    # The scratch counterpart to the ten bookmarks above: scoped to the open
    # conversation and cleared on leaving it (see _msg_temp_bookmarks'
    # declaration in __init__ for why both kinds exist). No cross-conversation
    # case to handle here, which is why these are far shorter than their
    # permanent equivalents — a temporary bookmark can only ever point into
    # the conversation that is already open.

    def _on_temp_bookmark_set_or_jump(self, digit: int):
        """Alt+Shift+<digit>: bookmark the focused message temporarily, or —
        if <digit> already holds one — move focus/selection to it instead."""
        i18n = self.main_window.i18n
        existing = self._msg_temp_bookmarks.get(digit)
        if existing is not None:
            idx = self._find_index_by_msg_id(existing)
            if idx < 0:
                # The message left the list (deleted, or trimmed out by a
                # rebuild) — drop the marker rather than keep pointing nowhere.
                del self._msg_temp_bookmarks[digit]
                self.main_window.output(
                    i18n.t("temp_bookmark_not_found").format(digit=digit), interrupt=True
                )
                return
            if self._already_on_message_row(idx):
                self.main_window.output(
                    i18n.t("temp_bookmark_already_there").format(
                        position=idx + 1, digit=digit
                    ),
                    interrupt=True,
                )
                return
            self._focus_message_row(idx)
            self.main_window.output(
                i18n.t("temp_bookmark_jumped").format(position=idx + 1, digit=digit),
                interrupt=True,
            )
            return

        if self.conversation is None:
            return
        idx = self.messages_list.GetFocusedItem()
        if idx < 0 or idx >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[idx]
        if self._is_separator(msg):
            return
        msg_id = msg.get("key", {}).get("id", "")
        if not msg_id:
            return
        self._msg_temp_bookmarks[digit] = msg_id
        self.main_window.output(
            i18n.t("temp_bookmark_set").format(
                digit=digit, position=idx + 1, text=self.messages_list.GetItemText(idx),
            ),
            interrupt=True,
        )

    def _on_temp_bookmark_remove(self, digit: int):
        """Ctrl+Alt+Shift+<digit>: remove the temporary bookmark at that digit."""
        i18n = self.main_window.i18n
        existing = self._msg_temp_bookmarks.pop(digit, None)
        if existing is None:
            self.main_window.output(
                i18n.t("temp_bookmark_not_found").format(digit=digit), interrupt=True
            )
            return
        idx = self._find_index_by_msg_id(existing)
        if idx < 0:
            self.main_window.output(
                i18n.t("temp_bookmark_removed_stale").format(digit=digit), interrupt=True
            )
            return
        self.main_window.output(
            i18n.t("temp_bookmark_removed").format(digit=digit, position=idx + 1),
            interrupt=True,
        )

    # ── Ctrl+Shift+F: search in conversation ───────────────────────────────

    def _on_accel_open_search(self, event):
        self._on_open_search(event)

    def _on_open_search(self, event):
        self._search_panel.Show()
        self._search_open_btn.Hide()
        self.conversation_panel.Layout()
        self._search_field.SetFocus()

    def _on_close_search(self, event):
        self._search_panel.Hide()
        self._search_open_btn.Show()
        self._search_results = []
        self._search_result_idx = -1
        self._search_field.SetValue("")
        self.conversation_panel.Layout()
        self.messages_list.SetFocus()

    def _message_search_text(self, msg) -> str:
        """The part of a message row that searching should actually look at.

        Not the rendered row: _render_message_line() also appends delivery
        status, the timestamp, "Editada"/"Encaminhada", and the reaction
        summary. Searching that string made every decoration a false match —
        reported live for "reproduz", which hit every played voice message
        through its "Reproduzida" status, but "editada", "encaminhada", a
        date, or a reaction label would all have done the same.

        What stays is what a user would call content: who wrote it, the text
        or media description itself, and — when the message is a reply — who
        was quoted and what the quote says. That last part is deliberate: a
        reply can quote a message that scrolled out of the loaded history, so
        the quote is sometimes the only copy of those words in the list.
        """
        parts = []
        if not self._is_system_event(msg):
            parts.append(self._sender_label(msg))
        parts.append(self._get_message_content(msg) or "")
        ctx = self._get_context_info(msg)
        if ctx:
            # The bare name, never the "respondendo a {name}" phrasing around
            # it — that wording is decoration and would match on its own.
            parts.append(self._get_quoted_sender(ctx, msg) or "")
            parts.append(self._get_quoted_preview(ctx.get("quotedMessage") or {}) or "")
        return " ".join(p for p in parts if p)

    def _on_search_text_changed(self, event):
        query = self._search_field.GetValue()
        if not query.strip():
            self._search_results = []
            self._search_result_idx = -1
            return
        # Read the setting per search, not once at startup: changing it in
        # Settings takes effect on the very next keystroke.
        fold = self.main_window._search_normalization_mode()
        qlow = normalize_for_search(query, fold)
        # Store message IDs, not raw row indices: _sorted_messages can be
        # mutated (a new message arrives, more history is paginated in, a
        # message is deleted) between when the query runs and when the user
        # actually jumps to a result, which silently sent "next result" to
        # whatever unrelated row now sits at that same index. Messages with
        # no id (essentially never, in practice) are skipped rather than
        # matched by an ambiguous empty key.
        self._search_results = [
            msg.get("key", {}).get("id", "")
            for msg in self._sorted_messages
            if not self._is_separator(msg)
            and msg.get("key", {}).get("id")
            and qlow in normalize_for_search(self._message_search_text(msg), fold)
        ]
        self._search_result_idx = -1

    def _on_search_key_down(self, event):
        key   = event.GetKeyCode()
        shift = event.ShiftDown()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if shift:
                self._on_search_prev(None)
            else:
                self._on_search_next(None)
        else:
            event.Skip()

    def _on_search_next(self, event):
        i18n = self.main_window.i18n
        if not self._search_results:
            self.main_window.output(i18n.t("search_no_results"), interrupt=True)
            return
        self._search_result_idx = (self._search_result_idx + 1) % len(self._search_results)
        self._jump_to_search_result()

    def _on_search_prev(self, event):
        i18n = self.main_window.i18n
        if not self._search_results:
            self.main_window.output(i18n.t("search_no_results"), interrupt=True)
            return
        self._search_result_idx = (self._search_result_idx - 1) % len(self._search_results)
        self._jump_to_search_result()

    def _jump_to_search_result(self):
        i18n = self.main_window.i18n
        idx = -1
        # Resolve the stored message ID to its CURRENT row. Drop any result
        # whose message is no longer present (paginated out, deleted) instead
        # of silently focusing whatever unrelated row now sits at a stale
        # index.
        while self._search_results:
            if not (0 <= self._search_result_idx < len(self._search_results)):
                self._search_result_idx = 0
            msg_id = self._search_results[self._search_result_idx]
            idx = next(
                (i for i, m in enumerate(self._sorted_messages)
                 if m.get("key", {}).get("id") == msg_id),
                -1,
            )
            if idx >= 0:
                break
            del self._search_results[self._search_result_idx]
            idx = -1

        if idx < 0:
            self.main_window.output(i18n.t("search_no_results"), interrupt=True)
            return

        total = len(self._search_results)
        self.messages_list.Focus(idx)
        self.messages_list.Select(idx, True)
        self.messages_list.EnsureVisible(idx)
        ann = i18n.t("search_result").format(
            current=self._search_result_idx + 1,
            total=total,
        )
        self.main_window.output(ann, interrupt=True)

    def _show_message_text_popup(self, msg: dict):
        """Open a read-only dialog showing the full message text (or, for a
        photo/video/document message, its caption)."""
        msg_type = msg.get("messageType", "")
        msg_obj  = msg.get("message") or {}
        text = ""
        if msg_type == "conversation":
            text = msg_obj.get("conversation", "")
        elif msg_type == "extendedTextMessage":
            text = (msg_obj.get("extendedTextMessage") or {}).get("text", "")
        else:
            text = self._get_message_caption(msg)
        if not text:
            return

        i18n = self.main_window.i18n

        def _word_wrap(raw: str, width: int = 100) -> str:
            """Wrap at word boundaries around *width* chars; never breaks mid-word."""
            out = []
            for para in raw.split("\n"):
                if not para:
                    out.append("")
                    continue
                line = ""
                for word in para.split(" "):
                    if not line:
                        line = word
                    elif len(line) + 1 + len(word) <= width:
                        line += " " + word
                    else:
                        out.append(line)
                        line = word
                if line:
                    out.append(line)
            return "\n".join(out)

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
            panel, value=_word_wrap(text),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        sizer.Add(text_ctrl, 1, wx.EXPAND | wx.ALL, 8)
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

    def _on_menu_react(self, msg: dict):
        """Open the emoji picker dialog to react to a message."""
        if self._reject_system_event_action(msg):
            return
        i18n = self.main_window.i18n
        EMOJIS = [
            ("❤️", "❤️"),
            ("👍", "👍"),
            ("👎", "👎"),
            ("😂", "😂"),
            ("😮", "😮"),
            ("😢", "😢"),
            ("🙏", "🙏"),
            ("🔥", "🔥"),
            ("🎉", "🎉"),
            ("💯", "💯"),
            ("😎", "😎"),
            ("🥰", "🥰"),
        ]

        msg_id = msg.get("key", {}).get("id", "")
        # issue #67: show which reaction (if any) I already sent to this
        # message, and let activating it again remove it — there was
        # previously no way to remove a reaction from the UI at all.
        current_emoji = (self._reaction_map.get(msg_id) or {}).get(self._SELF_REACTOR_KEY, "")

        dlg = wx.Dialog(
            self.main_window,
            title=i18n.t("react_dialog_title"),
            style=wx.DEFAULT_DIALOG_STYLE,
            size=(300, 380),
        )
        panel = wx.Panel(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)

        hint_label = wx.StaticText(
            panel,
            label=i18n.t("react_dialog_hint_remove") if current_emoji else i18n.t("react_dialog_hint"),
        )
        sizer.Add(hint_label, 0, wx.ALL, 8)

        emoji_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        emoji_list.InsertColumn(0, i18n.t("react_dialog_title"), width=240)
        emoji_list.EnableCheckBoxes(True)
        current_idx = -1
        for idx, (emoji, display) in enumerate(EMOJIS):
            emoji_list.Append((display,))
            if emoji == current_emoji:
                emoji_list.CheckItem(idx, True)
                current_idx = idx
        sizer.Add(emoji_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        cancel_btn = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
        sizer.Add(cancel_btn, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        panel.SetSizer(sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(panel, 1, wx.EXPAND)
        dlg.SetSizer(dlg_sizer)

        selected_emoji = [None]

        def _on_emoji_activated(event):
            idx = event.GetIndex()
            if 0 <= idx < len(EMOJIS):
                # Activating the reaction already checked (i.e. the one I
                # already sent) removes it instead of resending the same
                # emoji — the only way to clear a reaction previously.
                selected_emoji[0] = "" if idx == current_idx else EMOJIS[idx][0]
                dlg.EndModal(wx.ID_OK)

        def _on_emoji_selected(event):
            # Single click: just move selection, don't send yet
            pass

        emoji_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, _on_emoji_activated)
        cancel_btn.Bind(wx.EVT_BUTTON, lambda e: dlg.EndModal(wx.ID_CANCEL))
        dlg.Bind(wx.EVT_CHAR_HOOK, lambda e: dlg.EndModal(wx.ID_CANCEL) if e.GetKeyCode() == wx.WXK_ESCAPE else e.Skip())

        # A pre-populated list must never leave focus/selection pointing at
        # nothing — mirrors the conversation list's own convention. Land on
        # the currently-sent reaction when there is one, same reasoning as
        # every other "open on the relevant row" dialog in this app.
        if emoji_list.GetItemCount() > 0:
            start = current_idx if current_idx >= 0 else 0
            emoji_list.Focus(start)
            emoji_list.Select(start)
        emoji_list.SetFocus()
        dlg.CentreOnParent()
        result = dlg.ShowModal()
        dlg.Destroy()

        if result == wx.ID_OK and selected_emoji[0] is not None:
            emoji = selected_emoji[0]
            msg_key = msg.get("key", {})
            threading.Thread(
                target=self._do_send_reaction,
                args=(msg_key, emoji),
                daemon=True,
            ).start()

    _SELF_REACTOR_KEY = "_me_"

    def _reactor_key_from_msg(self, msg: dict) -> str:
        """Identity of whoever sent this reactionMessage — used so each sender
        only ever holds one active reaction per message in _reaction_map."""
        key = msg.get("key", {}) or {}
        if key.get("fromMe"):
            return self._SELF_REACTOR_KEY
        return key.get("participant") or key.get("remoteJid") or ""

    def _reaction_counts(self, msg_id: str) -> dict:
        """Aggregate {sender: emoji} into {emoji: count} for display."""
        per_msg = self._reaction_map.get(msg_id) or {}
        counts: dict = {}
        for emoji in per_msg.values():
            counts[emoji] = counts.get(emoji, 0) + 1
        return counts

    def _send_reaction(self, msg: dict, emoji: str):
        """Send reaction directly (called from most-used submenu)."""
        msg_key = msg.get("key", {})
        threading.Thread(
            target=self._do_send_reaction,
            args=(msg_key, emoji),
            daemon=True,
        ).start()

    def _do_send_reaction(self, msg_key: dict, emoji: str):
        """Background: send reaction via WPPConnect API."""
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        ok = self.main_window.send_reaction(jid, msg_key, emoji)
        if ok:
            # Apply optimistically — the WebSocket echo for own reactions is
            # suppressed in on_messages_upsert to avoid double-counting.
            wx.CallAfter(self._on_own_reaction_sent, jid, msg_key, emoji)

    def apply_incoming_reaction(self, remote_jid: str, msg: dict):
        """Apply a reactionMessage that just arrived over the WebSocket.

        Persisting is unconditional; only the live redraw is conditional on
        the reacted-to chat being the one currently open. main.py's
        on_new_message() deliberately never appends a reactionMessage to a
        chat's `records` itself, so this is the ONLY thing that files a live
        reaction anywhere — and it used to run behind
        on_incoming_message()'s "is this conversation open?" guard, which
        meant a reaction to a chat the user was not looking at (the normal
        case: the toast for it only ever fires while the window is in the
        background) was applied nowhere at all. It showed up as a
        notification and then simply did not exist: opening the conversation
        afterwards rebuilds _reaction_map by scanning `records`, which never
        received it.

        _reaction_map, by contrast, only ever describes the conversation
        currently rendered in messages_list — populate_messages() rebuilds it
        from scratch per conversation — so it is only touched when this
        reaction really belongs to that conversation.
        """
        reaction   = (msg.get("message") or {}).get("reactionMessage") or {}
        emoji      = reaction.get("text", "")
        orig_id    = (reaction.get("key") or {}).get("id", "")
        sender_key = self._reactor_key_from_msg(msg)
        if not orig_id or not sender_key:
            return

        if self._matches_open_conversation(remote_jid):
            per_msg = self._reaction_map.setdefault(orig_id, {})
            if emoji:
                # A new/changed reaction from this sender replaces theirs —
                # it never accumulates into a bogus higher count.
                per_msg[sender_key] = emoji
            else:
                # Empty emoji = this sender removed their reaction.
                per_msg.pop(sender_key, None)
            # Re-render the original message in the list
            for i, m in enumerate(self._sorted_messages):
                if not self._is_separator(m) and m.get("key", {}).get("id") == orig_id:
                    self.messages_list.SetItemText(i, self._render_message_line(m))
                    # The Reactions button only ever refreshes on focus
                    # change (_update_reactions_button() is called from the
                    # list's EVT_LIST_ITEM_FOCUSED handler) — a reaction
                    # landing on the message the user already has focused
                    # left the button in whatever state it was in before,
                    # requiring the user to move focus away and back just to
                    # make it appear. Refresh it here too when this is the
                    # row currently focused.
                    if i == self.messages_list.GetFocusedItem():
                        self._update_reactions_button(i)
                    break

        # Persist so populate_messages()/refresh_active_conversation_messages()
        # (which rebuild _reaction_map purely from `records`) can recover this
        # reaction whenever the message list is (re)built — see
        # _persist_reaction_record()'s own docstring. Not _track_last_reaction()
        # or _schedule_set_chats() here — main.py's on_new_message() already
        # calls both for every reaction from someone else.
        own_key = msg.get("key", {}) or {}
        self._persist_reaction_record(
            remote_jid, orig_id, reaction.get("key") or {}, sender_key,
            bool(own_key.get("fromMe")), emoji,
            participant=own_key.get("participant", ""),
        )

    def _persist_reaction_record(self, jid: str, orig_id: str, msg_key: dict,
                                  sender_key: str, from_me: bool, emoji: str,
                                  participant: str = "") -> "dict | None":
        """Persist a reaction (ours or someone else's) into the chat's own
        records, so populate_messages()/refresh_active_conversation_messages()
        — which rebuild _reaction_map purely by scanning `records` — can
        recover it after anything repopulates the message list: a
        conversation close/reopen, an app restart, or any background
        full-list refresh in between (e.g. a history backfill delivering an
        already-seen message). A reaction from someone else used to update
        only the in-memory _reaction_map and nothing else, so it silently
        vanished — both the inline marker on the message row and the
        Reactions button — the next time anything rebuilt the list, with no
        real relation to focus movement despite how it was reported
        ("reação some ao voltar o foco pra mensagem"). Own reactions already
        persisted this way; this is the same thing for the received case.

        The synthetic record id is namespaced per (message, sender) — not
        just per message — since more than one person can react to the same
        message with different emojis at once; only the mover's own key
        keeps the original bare `_rxn_{orig_id}` form used before per-sender
        namespacing existed, so an already-persisted self-reaction record on
        an existing install is found and updated in place rather than
        duplicated under a new id.

        Returns the record dict (the caller may still need it, e.g. for
        _track_last_reaction()), or None if there was nothing to persist.
        """
        chat = self.main_window.get_chat(jid)
        if not chat or not orig_id:
            return None
        # get_chat() already resolves the @lid/phone duality when looking the
        # chat up, but the record itself still has to be filed under the
        # chat's own canonical JID: a live reaction arrives under whichever
        # form the event happened to use, and persisting it under the other
        # one wrote it into a DB bucket the conversation never reads back.
        jid = chat.get("remoteJid") or jid
        rxn_id = (
            f"_rxn_{orig_id}" if sender_key == self._SELF_REACTOR_KEY
            else f"_rxn_{orig_id}_{sender_key}"
        )
        key = {"remoteJid": jid, "fromMe": from_me, "id": rxn_id}
        if participant:
            key["participant"] = participant
        reaction_record = {
            "messageType": "reactionMessage",
            "message": {
                "reactionMessage": {
                    "key":  msg_key,
                    "text": emoji,
                }
            },
            "key": key,
            "messageTimestamp": int(time.time()),
        }
        records = (
            chat.setdefault("messages", {})
                .setdefault("messages", {})
                .setdefault("records", [])
        )
        # Update the existing reaction record for this (message, sender)
        # pair in place (changing the emoji) instead of silently no-op'ing —
        # previously a changed reaction only updated the in-memory map, so
        # the old emoji came back after reopening the conversation.
        existing = next((r for r in records if r.get("key", {}).get("id") == rxn_id), None)
        if existing:
            existing["message"] = reaction_record["message"]
            existing["messageTimestamp"] = reaction_record["messageTimestamp"]
        else:
            records.append(reaction_record)
        try:
            self.main_window.db.insert_message(jid, reaction_record)
        except Exception:
            logging.exception("[conversations] insert reaction failed")
        return reaction_record

    def _on_own_reaction_sent(self, jid: str, msg_key: dict, emoji: str):
        """Update reaction_map, re-render the original message, and refresh the list."""
        orig_id = msg_key.get("id", "")
        if not orig_id:
            return

        # Update in-memory reaction map — replaces our own previous reaction
        # on this message rather than adding another count. An empty emoji
        # means the reaction was removed (see _on_menu_react's checked-item
        # toggle) — drop our own entry rather than leaving the stale emoji
        # badge on the message.
        if emoji:
            self._reaction_map.setdefault(orig_id, {})[self._SELF_REACTOR_KEY] = emoji
        else:
            self._reaction_map.get(orig_id, {}).pop(self._SELF_REACTOR_KEY, None)

        # Re-render the original message row if currently visible
        for i, m in enumerate(self._sorted_messages):
            if not self._is_separator(m) and m.get("key", {}).get("id") == orig_id:
                self.messages_list.SetItemText(i, self._render_message_line(m))
                # See apply_incoming_reaction()'s identical call for why this
                # is needed in addition to the focus-driven refresh.
                if i == self.messages_list.GetFocusedItem():
                    self._update_reactions_button(i)
                break

        # Persist reaction in chat records so _last_msg_preview and populate_messages
        # can reflect it after a conversation close/reopen.
        reaction_record = self._persist_reaction_record(
            jid, orig_id, msg_key, self._SELF_REACTOR_KEY, True, emoji,
        )
        if reaction_record is not None:
            # reaction_record stays in `records` only so populate_messages()
            # can rebuild the reaction map (and thus redraw the reacted-to
            # message's inline reaction marker) after a conversation
            # close/reopen or app restart — it must NOT also become
            # eligible as the chat-list preview's "last message" the way a
            # real message would. Received reactions already go through
            # _track_last_reaction() (see on_new_message), which keeps the
            # "você reagiu com X" / "Fulano reagiu com X" preview in a
            # separate chat["_last_reaction"] field instead of `records` —
            # own reactions previously skipped that call entirely (the
            # WebSocket echo for them is suppressed), so the chat list fell
            # back to formatting this raw reactionMessage record as if it
            # were a normal message, which _last_msg_preview() has no case
            # for and rendered as "mensagem incompatível" for the
            # conversation the user had just reacted in.
            self.main_window._track_last_reaction(jid, reaction_record)

        self.main_window._schedule_set_chats()

    # ── Attachment handling ──────────────────────────────────────────────────

    def on_add_attachment(self, event=None):
        """Open a popup menu to choose the attachment type."""
        if self.conversation is None:
            return
        i18n = self.main_window.i18n
        menu = wx.Menu()
        pv_item  = menu.Append(wx.ID_ANY, i18n.t("attachment_photos_videos"))
        doc_item = menu.Append(wx.ID_ANY, i18n.t("attachment_document"))
        aud_item = menu.Append(wx.ID_ANY, i18n.t("attachment_audio_file"))
        con_item = menu.Append(wx.ID_ANY, i18n.t("attachment_contact"))
        self.Bind(wx.EVT_MENU, self._on_attach_photo_video, pv_item)
        self.Bind(wx.EVT_MENU, self._on_attach_document,    doc_item)
        self.Bind(wx.EVT_MENU, self._on_attach_audio_file,  aud_item)
        self.Bind(wx.EVT_MENU, self._on_attach_contact,     con_item)
        self.PopupMenu(menu)
        menu.Destroy()

    def _on_attach_photo_video(self, event):
        i18n = self.main_window.i18n
        wildcard = (
            f"{i18n.t('attachment_photos_videos')} "
            "(*.jpg;*.jpeg;*.png;*.gif;*.webp;*.mp4;*.avi;*.mov;*.mkv)|"
            "*.jpg;*.jpeg;*.png;*.gif;*.webp;*.mp4;*.avi;*.mov;*.mkv"
        )
        with wx.FileDialog(
            self, i18n.t("attachment_photos_videos"),
            wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            for path in dlg.GetPaths():
                mtype = classify_attachment_media_type(path)
                if mtype not in {"image", "video"}:
                    mtype = "document"
                self._staged_attachments.append({"path": path, "media_type": mtype})
        if self._staged_attachments:
            self._show_attachment_panel()

    def _on_attach_document(self, event):
        with wx.FileDialog(
            self, self.main_window.i18n.t("attachment_document"),
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            for path in dlg.GetPaths():
                self._staged_attachments.append(
                    {"path": path, "media_type": "document"}
                )
        if self._staged_attachments:
            self._show_attachment_panel()

    def _on_attach_audio_file(self, event):
        i18n     = self.main_window.i18n
        wildcard = (
            f"{i18n.t('attachment_audio_file')} "
            "(*.mp3;*.ogg;*.wav;*.m4a;*.aac;*.flac)|"
            "*.mp3;*.ogg;*.wav;*.m4a;*.aac;*.flac"
        )
        with wx.FileDialog(
            self, i18n.t("attachment_audio_file"),
            wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            for path in dlg.GetPaths():
                self._staged_attachments.append(
                    {"path": path, "media_type": "audio"}
                )
        if self._staged_attachments:
            self._show_attachment_panel()

    def _on_attach_contact(self, event):
        from ui.dialogs.attach_contact_dialog import AttachContactDialog
        dlg = AttachContactDialog(self.main_window)
        if dlg.ShowModal() != wx.ID_OK or dlg.selected_contact is None:
            dlg.Destroy()
            return
        contact    = dlg.selected_contact
        dlg.Destroy()
        remote_jid = self.conversation.get("remoteJid", "")
        if not remote_jid:
            return
        local_id = str(uuid.uuid4())
        name = (
            contact.get("pushName")
            or format_number(contact.get("remoteJid", ""))
        )
        virtual_msg = {
            "_local_pending": True,
            "_local_id":      local_id,
            "key": {"id": local_id, "fromMe": True, "remoteJid": remote_jid},
            "messageType": "contactMessage",
            "message": {
                "contactMessage": {
                    "displayName": name,
                    "vcard": "",
                }
            },
            "messageTimestamp": int(time.time()),
            "pushName": "",
        }
        if self._quoted_message:
            _qk = self._quoted_message.get("key", {})
            virtual_msg["contextInfo"] = {
                "stanzaId":      _qk.get("id", ""),
                "participant":   _qk.get("participant", ""),
                "quotedMessage": self._quoted_message.get("message") or {},
            }
        
        self._clear_empty_placeholder()
        self._sorted_messages.append(virtual_msg)
        self.messages_list.Append((self._render_message_line(virtual_msg),))
        last = self.messages_list.GetItemCount() - 1
        if last >= 0:
            self.messages_list.EnsureVisible(last)
        pm = PendingMessage(local_id, remote_jid, contact_info=contact,
                            quoted=self._quoted_message)
        self.main_window.message_queue.enqueue(pm)
        self._on_cancel_reply()  # clear quoted state after send
        self.main_window.mark_conversation_as_read(remote_jid)

        self._register_virtual_msg(virtual_msg)
        self.main_window._schedule_set_chats()

    def _pre_cache_sent_media(self, local_id: str, path: str, media_type: str):
        """Copy a just-sent attachment straight into the local media cache,
        keyed by its local_id the same way a downloaded copy is keyed by
        message id.

        We're the sender, so the exact bytes already sit on disk at *path* —
        there's no reason to require a redundant round-trip download through
        WPPConnect just to unlock the Open/Save As buttons. This mirrors the
        existing "rename the local audio file so we don't have to download
        it" trick _mark_message_sent() already does for recorded voice
        messages, extended to files sent via the attachment picker
        (document/image/video/audio). _mark_message_sent() renames the cache
        entry from local_id to the real WhatsApp id once the echo confirms it.
        """
        try:
            with open(path, "rb") as fh:
                content = fh.read()
            encrypted = encrypt(content, self.main_window.key)
            if media_type == "audio":
                cache_path = data_path("voice_messages", f"{local_id}.msv")
            else:
                cache_path = data_path("media", f"{local_id}.wzmedia")
            with open(cache_path, "wb") as fh:
                fh.write(encrypted)
        except Exception as e:
            logging.error(f"[_pre_cache_sent_media] failed to pre-cache {path}: {e}")

    def _show_attachment_panel(self):
        self._rebuild_attachment_list()
        self.message_label.Hide()
        self.message_field.Hide()
        if hasattr(self, "_emoji_btn"):
            self._emoji_btn.Hide()
        self.send_message_btn.Hide()
        self.record_voice_message_btn.Hide()
        self._add_attachment_btn.Hide()
        self._attachment_panel.Show()
        self.conversation_panel.Layout()
        self._apply_typed_text_as_caption()
        self._caption_field.SetFocus()

    def _apply_typed_text_as_caption(self):
        """Move whatever was already typed in message_field into the
        attachment caption field, matching the official WhatsApp client.

        Only fires when the caption is still empty (never clobbers a caption
        the user already typed for a previous batch of staged attachments)
        and the setting is enabled. The text is moved, not copied — left in
        message_field it would still be sitting there, ready to be sent as a
        separate message, once the attachment panel closes.
        """
        preserve = self.main_window.settings.get("user_interface", {}).get(
            "preserve_typed_text_as_attachment_caption", True
        )
        if not preserve or self._caption_field.GetValue():
            return
        typed = normalize_line_separators(self.message_field.GetValue()).strip()
        if not typed:
            return
        self._caption_field.SetValue(typed)
        self.message_field.SetValue("")

    def _rebuild_attachment_list(self):
        """Rebuild the per-file remove-buttons to match _staged_attachments."""
        i18n  = self.main_window.i18n
        panel = self._attachments_list_panel
        sizer = self._attachments_list_sizer
        for child in list(panel.GetChildren()):
            child.Destroy()
        sizer.Clear()
        for idx, att in enumerate(self._staged_attachments):
            filename = os.path.basename(att["path"])
            btn = wx.Button(
                panel,
                label=f"{i18n.t('remove_attachment')} {filename}",
            )
            # Bind by index, not path: the same file can legitimately be
            # staged twice (attached in two separate picks), and removing by
            # path used to delete every entry sharing it instead of just the
            # one the user clicked remove on.
            btn.Bind(
                wx.EVT_BUTTON,
                lambda evt, i=idx: self._on_remove_attachment(i),
            )
            sizer.Add(btn, 0, wx.BOTTOM, 3)
        panel.Layout()
        if self._attachment_panel.IsShown():
            self._attachment_panel.Layout()
            self.conversation_panel.Layout()

    def _on_remove_attachment(self, index: int):
        """Remove one staged file and rebuild the list (or close the panel)."""
        if 0 <= index < len(self._staged_attachments):
            del self._staged_attachments[index]
        if not self._staged_attachments:
            self._hide_attachment_panel()
        else:
            self._rebuild_attachment_list()

    def _hide_attachment_panel(self):
        self._staged_attachments = []
        self._attachment_panel.Hide()
        if hasattr(self, "message_label"):
            self.message_label.Show()
            self.message_field.Show()
            if hasattr(self, "_emoji_btn"):
                self._emoji_btn.Show()
            if self.message_field.GetValue().strip():
                self.send_message_btn.Show()
            else:
                self.record_voice_message_btn.Show()
            self._add_attachment_btn.Show()
        if hasattr(self, "conversation_panel") and self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

    def _on_add_more_files(self, event):
        """Re-open the file picker to add more files to the staging list."""
        self.on_add_attachment(event)

    def _on_send_attachment(self, event=None):
        """Enqueue all staged attachments as outgoing messages."""
        if not self._staged_attachments or self.conversation is None:
            return
        remote_jid = self.conversation.get("remoteJid", "")
        if not remote_jid:
            return
        caption = self._consume_attachment_caption()

        _VTYPE = {
            "image":    "imageMessage",
            "video":    "videoMessage",
            "audio":    "audioMessage",
            "document": "documentMessage",
        }
        # Capture quoted state before looping (cleared after all enqueued)
        quoted = self._quoted_message

        # WhatsApp's own ceiling is 2 GB for documents and 1 GB for photos,
        # videos and audio — WinZapp used to cap documents at 1 GB as well,
        # for no reason other than sharing one constant with the other types.
        # WinZapp's WPPConnect patch transfers large files to Chromium in
        # bounded chunks, avoiding the single oversized CDP argument that
        # previously killed the session — that used to be document-only but now
        # covers image/video/audio too (see
        # core/wppconnect_sender_layer_patch.py), so size alone is no longer
        # what limits this; the ceilings below are WhatsApp's, not ours.
        #
        # These are only the FIRST of four gates a large file passes, and all
        # four have to agree or a document between 1 and 2 GB is refused
        # somewhere the user cannot see: this pre-check, send_media_attachment()
        # in main.py, the maxFileSize WPPConnect is told about in
        # core/websocket_client.py, and WhatsApp Web's own MediaGatingUtils
        # ceiling raised by the sender-layer patch.
        _MAX_DOCUMENT_BYTES = 2 * 1024 * 1024 * 1024
        _MAX_DOCUMENT_MB    = 2048
        _MAX_MEDIA_BYTES    = 1 * 1024 * 1024 * 1024
        _MAX_MEDIA_MB       = 1024
        i18n = self.main_window.i18n
        for attachment in list(self._staged_attachments):
            path       = attachment["path"]
            media_type = attachment.get("media_type", "document")

            vtype      = _VTYPE.get(media_type, "documentMessage")
            is_document = vtype == "documentMessage"
            max_bytes = _MAX_DOCUMENT_BYTES if is_document else _MAX_MEDIA_BYTES
            max_mb    = _MAX_DOCUMENT_MB if is_document else _MAX_MEDIA_MB

            try:
                file_size = os.path.getsize(path)
            except OSError:
                # Unreadable size is not a reason to refuse the send — the
                # limit check below simply can't run, exactly as before.
                file_size = None
            if file_size is not None and file_size > max_bytes:
                wx.MessageBox(
                    i18n.t("media_too_large").format(max_mb=max_mb),
                    i18n.t("app_name"),
                    wx.OK | wx.ICON_ERROR,
                    self,
                )
                continue

            local_id   = str(uuid.uuid4())
            _body = {
                "caption":  caption,
                "fileName": os.path.basename(path),
                "mimetype": mimetypes.guess_type(path)[0]
                            or "application/octet-stream",
            }
            if vtype == "documentMessage" and file_size is not None:
                # Issue #96: a document we send showed no size, while the same
                # document received from someone else did. The rendering is
                # shared and keys only on fileLength — the field just never
                # reached it. WPPConnect's echo of our own send DOES carry the
                # size, but on_new_message() merges an echo into the pending
                # virtual message by copying id/timestamp/participant onto it,
                # keeping this body, so the echo's copy is discarded and the
                # record persisted to the DB never has it. (It reappeared only
                # after a resync re-fetched the message from the server through
                # _normalize_wpp_message.)
                #
                # Filling it here rather than from the echo is deliberate: the
                # line is complete the moment it appears, instead of being
                # rewritten once the echo lands — and rewriting a list row is
                # what makes a screen reader read the whole row out again (see
                # _release_chain_held_repaints()).
                #
                # Only documents: theirs is the one type whose rendered line
                # shows a size, and fileLength is also read by
                # MainWindow.sync_if_media() as an auto-download size gate, so
                # populating it for our own images/videos would change that
                # decision for something this issue never asked about.
                _body["fileLength"] = file_size
            if media_type == "audio":
                _dur = self._probe_audio_duration(path)
                if _dur is not None:
                    _body["seconds"] = _dur
            elif media_type == "video":
                # Unlike audio, video_seconds() doesn't trust a plain "seconds"
                # of 0 (WhatsApp itself sends that to mean "not stated" — see
                # that function's own docstring), so a video we're sending
                # needs its length under _measured_seconds instead, same key
                # _learn_video_duration() fills in for a received video once
                # it's played. Without this, a video sent as a WinZapp
                # attachment showed no duration in the list until the sender
                # opened it themselves at least once.
                _dur = self._probe_audio_duration(path)
                if _dur is not None and _dur >= 0:
                    _body[MEASURED_SECONDS_KEY] = _dur
            virtual_msg = {
                "_local_pending": True,
                "_local_id":      local_id,
                "key": {"id": local_id, "fromMe": True, "remoteJid": remote_jid},
                "messageType": vtype,
                "message": {vtype: _body},
                "messageTimestamp": int(time.time()),
                "pushName": "",
            }
            if quoted:
                _qk = quoted.get("key", {})
                virtual_msg["contextInfo"] = {
                    "stanzaId":      _qk.get("id", ""),
                    "participant":   _qk.get("participant", ""),
                    "quotedMessage": quoted.get("message") or {},
                }
            
            self._clear_empty_placeholder()
            self._sorted_messages.append(virtual_msg)
            self.messages_list.Append((self._render_message_line(virtual_msg),))
            last = self.messages_list.GetItemCount() - 1
            if last >= 0:
                self.messages_list.Select(last, True)
                self.messages_list.EnsureVisible(last)
            def _update_upload_progress(progress, local_id=local_id):
                wx.CallAfter(self.update_media_upload_progress, local_id, progress)

            pm = PendingMessage(
                local_id, remote_jid,
                media_path=path, media_type=media_type, caption=caption,
                quoted=quoted, progress_callback=_update_upload_progress,
            )
            self._register_virtual_msg(virtual_msg)

            # Pre-cache the file under local_id BEFORE enqueueing the actual
            # send: _mark_message_sent() renames the cache entry from
            # local_id to the real WhatsApp id as soon as the send is
            # confirmed, so the file must already exist under local_id by
            # then, or that rename silently no-ops and the cache is never
            # found under the real id afterwards.
            def _cache_then_enqueue(pm=pm, local_id=local_id, path=path, media_type=media_type):
                self._pre_cache_sent_media(local_id, path, media_type)
                self.main_window.message_queue.enqueue(pm)

            threading.Thread(target=_cache_then_enqueue, daemon=True).start()

            self._show_media_transfer_gauge()

        self._on_cancel_reply()  # clear quoted state after send
        self.main_window.mark_conversation_as_read(remote_jid)
        self._hide_attachment_panel()
        # Attachment-panel teardown performs its own layout pass. Reassert the
        # transfer UI afterwards so that pass cannot swallow the new gauge.
        self._sync_pending_document_gauge()
        self.main_window._schedule_set_chats()
        self.message_field.SetFocus()

        # Refresh conversation list preview to show the last sent attachment.
        self.main_window._schedule_set_chats()

    # ── Contact message helpers ──────────────────────────────────────────────

    def _consume_attachment_caption(self) -> str:
        """Return the staged caption and clear it for the next attachment."""
        caption = normalize_line_separators(self._caption_field.GetValue()).strip()
        self._caption_field.Clear()
        return caption

    def _location_maps_url(self, msg: dict) -> str | None:
        """Build an openable Google Maps URL from a locationMessage/
        liveLocationMessage's coordinates, or None if it carries none.

        WinZapp has no in-app map view — unlike the phone client, which
        renders the location inline — so "opening" a location here means
        handing its coordinates to the system's default map/browser handler,
        the same way an image or document opens in its associated app.
        """
        msg_type = msg.get("messageType", "")
        inner = (msg.get("message") or {}).get(msg_type)
        if not isinstance(inner, dict):
            return None
        lat = inner.get("degreesLatitude")
        lng = inner.get("degreesLongitude")
        if lat is None or lng is None:
            return None
        try:
            lat = float(lat)
            lng = float(lng)
        except (TypeError, ValueError):
            return None
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"

    def _jid_from_vcard(self, vcard: str) -> str | None:
        """Extract the WhatsApp JID from a vCard string."""
        if not vcard:
            return None
        m = re.search(r"waid=(\d+)", vcard)
        if m:
            return m.group(1) + "@s.whatsapp.net"
        m2 = re.search(r"TEL[^:]*:\+?([\d\s\-()]+)", vcard)
        if m2:
            digits = re.sub(r"\D", "", m2.group(1))
            if digits:
                return digits + "@s.whatsapp.net"
        return None

    def _contact_display_name(self, msg: dict) -> str:
        """Extract a contactMessage's display name — prefers WPPConnect's own
        displayName field, falling back to parsing "FN:" out of the vCard
        (some clients only ever populate the vcard, or stuff the whole vcard
        into displayName)."""
        i18n = self.main_window.i18n
        contact = (msg.get("message") or {}).get("contactMessage") or {}
        name  = contact.get("displayName") or ""
        vcard = contact.get("vcard") or ""

        if not name or "BEGIN:VCARD" in name:
            vcard_to_parse = name if "BEGIN:VCARD" in name else vcard
            parsed_name = ""
            for line in vcard_to_parse.splitlines():
                if line.startswith("FN:"):
                    parsed_name = line[3:].strip()
                    break
            name = parsed_name or i18n.t("unknown_contact")
        return name

    @staticmethod
    def _vcard_phone_numbers(vcard: str) -> list:
        """Every phone number a contact card carries, in card order.

        A vCard can hold several TEL lines (mobile / work / home), and issue #84
        asks for the user to pick which one when it does. Deliberately parses
        the TEL lines rather than reusing _jid_from_vcard(), which answers a
        different question ("which WhatsApp account is this?") and stops at the
        first waid= it finds — the right answer for opening a conversation, and
        the wrong one for "copy the number", which must be able to offer all of
        them. Each entry is (label, number): the label is the TYPE= parameter
        when the card names one, so a list of three bare numbers still reads as
        something in a screen reader.

        Numbers are returned exactly as the card writes them, minus whitespace
        runs — WhatsApp cards are inconsistent about "+55 51 9..." vs
        "+5551 9...", and rewriting them would mean guessing a country.
        """
        if not vcard:
            return []
        out = []
        seen = set()
        for line in vcard.splitlines():
            line = line.strip()
            if not line.upper().startswith("TEL"):
                continue
            prop, _, value = line.partition(":")
            number = " ".join(value.split()).strip()
            if not number:
                continue
            digits = re.sub(r"\D", "", number)
            if not digits or digits in seen:
                continue
            seen.add(digits)
            label = ""
            m = re.search(r"TYPE=([^;:]+)", prop, re.IGNORECASE)
            if m:
                label = m.group(1).strip().strip('"')
            out.append((label, number))
        return out

    def _contact_message_numbers(self, msg: dict) -> list:
        """_vcard_phone_numbers() for a contactMessage, with the waid fallback.

        Some cards carry the WhatsApp id and nothing parseable as a TEL line;
        _jid_from_vcard() already knows how to dig that out, so fall back to it
        rather than telling the user the card has no number when it plainly
        shows one.
        """
        contact = (msg.get("message") or {}).get("contactMessage") or {}
        numbers = self._vcard_phone_numbers(contact.get("vcard", ""))
        if numbers:
            return numbers
        jid = self._jid_from_vcard(contact.get("vcard", ""))
        if jid:
            return [("", format_number(jid))]
        return []

    def _pick_contact_number(self, msg: dict) -> str:
        """The number to act on, asking the user when the card holds several.

        Returns "" when the card has no number (announced) or the user cancels
        the choice. The dialog is a plain wx.SingleChoiceDialog on purpose —
        the accessibility rule in CLAUDE.md is standard controls, and a
        single-choice list is exactly what this is.
        """
        i18n = self.main_window.i18n
        numbers = self._contact_message_numbers(msg)
        if not numbers:
            self.main_window.output(i18n.t("contact_no_number"), interrupt=True)
            return ""
        if len(numbers) == 1:
            return numbers[0][1]
        choices = [f"{lbl}: {num}" if lbl else num for lbl, num in numbers]
        dlg = wx.SingleChoiceDialog(
            self, i18n.t("contact_pick_number"), i18n.t("contact_details_title"),
            choices,
        )
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return ""
            return numbers[dlg.GetSelection()][1]
        finally:
            dlg.Destroy()

    def _on_contact_view_details(self, msg: dict):
        """Context menu > "Ver nome e número": name plus every number on the
        card, spoken and shown, since the message row itself only ever renders
        the name (issue #84)."""
        i18n = self.main_window.i18n
        name = self._contact_display_name(msg)
        numbers = self._contact_message_numbers(msg)
        if numbers:
            lines = [f"{lbl}: {num}" if lbl else num for lbl, num in numbers]
            body = "\n".join([name] + lines)
        else:
            body = "\n".join([name, i18n.t("contact_no_number")])
        self.main_window.output(body.replace("\n", ". "), interrupt=True)
        wx.MessageBox(body, i18n.t("contact_details_title"), wx.OK | wx.ICON_INFORMATION, self)

    def _on_contact_copy_number(self, msg: dict):
        """Ctrl+C / context menu on a contact message: copy the phone number."""
        number = self._pick_contact_number(msg)
        if not number:
            return
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(number))
                wx.TheClipboard.Flush()
            finally:
                wx.TheClipboard.Close()
            self.main_window.output(
                self.main_window.i18n.t("contact_number_copied"), interrupt=True
            )
        else:
            self.main_window.output(
                self.main_window.i18n.t("msg_copy_error"), interrupt=True
            )

    def _on_contact_converse(self, event, jid: str | None = None):
        """Navigate to the conversation with the contact from the selected
        message. *jid* lets Enter/Space activation (_do_activate_message)
        pass the focused row's own JID directly instead of relying on
        self._contact_msg_jid, the side channel _on_message_focused() sets
        for the Converse button."""
        jid = jid or self._contact_msg_jid
        if not jid:
            return
        chat = self.main_window.get_chat(jid)
        if chat is not None:
            self.navigate_to_conversation(chat)

    def _on_save_contact_message(self, event):
        """Ctrl+Shift+S / the "Salvar contato" button next to "Conversar":
        open NewContactDialog pre-filled from the focused contactMessage, to
        add that contact locally in WinZapp — same dialog/flow
        conversation_data_dialog.py's "Adicionar contato" uses."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        msg = self._sorted_messages[index]
        if self._is_separator(msg) or msg.get("messageType", "") != "contactMessage":
            return
        contact = (msg.get("message") or {}).get("contactMessage") or {}
        jid = self._jid_from_vcard(contact.get("vcard", ""))
        if not jid:
            return

        i18n = self.main_window.i18n
        name = self._contact_display_name(msg)
        parts  = name.split(None, 1) if name and name != i18n.t("unknown_contact") else []
        p_name = parts[0] if parts else ""
        p_sur  = parts[1] if len(parts) > 1 else ""

        from ui.dialogs.new_contact import NewContactDialog
        dlg = NewContactDialog(
            self.main_window, self,
            prefill_phone=format_number(jid),
            prefill_name=p_name,
            prefill_surname=p_sur,
        )
        dlg.ShowModal()
        dlg.Destroy()

    # ── Real-time incoming message ────────────────────────────────────────────

    def _matches_open_conversation(self, remote_jid: str) -> bool:
        """True when remote_jid addresses the conversation currently open.

        Tolerates the @lid/phone duality in both directions: a live event may
        arrive under either form regardless of which one the open conversation
        was loaded under. Also tolerates Brazilian 9th-digit variations and
        unnormalized JIDs.
        """
        if self.conversation is None or not remote_jid:
            return False
        conv_jid = self.conversation.get("remoteJid", "")
        if not conv_jid:
            return False
        if conv_jid == remote_jid:
            return True

        mw = getattr(self, "main_window", None)
        norm_conv = mw._normalize_jid(conv_jid) if mw and hasattr(mw, "_normalize_jid") else conv_jid
        norm_remote = mw._normalize_jid(remote_jid) if mw and hasattr(mw, "_normalize_jid") else remote_jid
        if norm_conv == norm_remote:
            return True

        c_digits, _, c_dom = norm_conv.partition("@")
        r_digits, _, r_dom = norm_remote.partition("@")
        if (
            mw
            and hasattr(mw, "_phone_digits_equivalent")
            and c_dom
            and c_dom == r_dom
            and c_dom in ("s.whatsapp.net", "c.us")
            and mw._phone_digits_equivalent(c_digits, r_digits)
        ):
            return True

        phone_to_lid = getattr(mw, "_phone_to_lid", {}) if mw else {}
        lid_to_phone = getattr(mw, "_lid_to_phone", {}) if mw else {}

        candidates = {
            conv_jid,
            norm_conv,
            phone_to_lid.get(conv_jid, ""),
            phone_to_lid.get(norm_conv, ""),
            lid_to_phone.get(conv_jid, ""),
            lid_to_phone.get(norm_conv, ""),
        }
        targets = {
            remote_jid,
            norm_remote,
            phone_to_lid.get(remote_jid, ""),
            phone_to_lid.get(norm_remote, ""),
            lid_to_phone.get(remote_jid, ""),
            lid_to_phone.get(norm_remote, ""),
        }
        candidates.discard("")
        targets.discard("")
        if candidates & targets:
            return True

        if mw and hasattr(mw, "_phone_digits_equivalent"):
            for c in candidates:
                for t in targets:
                    cd, _, cdom = c.partition("@")
                    td, _, tdom = t.partition("@")
                    if cdom and cdom == tdom and cdom in ("s.whatsapp.net", "c.us"):
                        if mw._phone_digits_equivalent(cd, td):
                            return True

        return False

    def on_incoming_message(self, remote_jid: str, msg: dict):
        """
        Called (on the main thread) when a new message arrives via WebSocket.
        If the conversation matching remote_jid is currently open, appends the
        message to the list; otherwise does nothing (the unread badge in the
        conversations list is updated separately via set_chats).
        """
        # Reactions are handled BEFORE the "is this conversation open?" guard
        # below — unlike a normal message, a reaction still has to be recorded
        # for a chat the user is not currently looking at. See
        # apply_incoming_reaction().
        if msg.get("messageType") == "reactionMessage":
            self.apply_incoming_reaction(remote_jid, msg)
            return  # Don't add reaction as a separate row

        if self.conversation is None:
            return

        if not self._matches_open_conversation(remote_jid):
            return

        # Get the top visible item before inserting the message
        top_msg_id = None
        top_idx = -1
        if getattr(self.main_window, "_allow_ui_focus_changes", lambda: False)():
            if hasattr(self.messages_list, "GetTopItem"):
                top_idx = self.messages_list.GetTopItem()
            else:
                try:
                    import ctypes
                    hwnd = self.messages_list.GetHandle()
                    top_idx = ctypes.windll.user32.SendMessageW(hwnd, 0x018E, 0, 0)
                except Exception:
                    pass
            if top_idx != -1 and 0 <= top_idx < len(self._sorted_messages):
                m = self._sorted_messages[top_idx]
                if not self._is_separator(m):
                    top_msg_id = m.get("key", {}).get("id", "")
        # Avoid duplicates
        msg_id = msg.get("key", {}).get("id", "")
        if msg_id:
            for existing in self._sorted_messages:
                if self._is_separator(existing):
                    continue
                if existing.get("key", {}).get("id", "") == msg_id:
                    return

        # Batch all list operations so the screen reader receives a single
        # accessibility event rather than one per insertion/update.
        from_me = bool(msg.get("key", {}).get("fromMe"))
        self.messages_list.Freeze()
        try:
            # Manage unread separator — never for our OWN messages. This
            # branch also runs for the WebSocket echo of a message we just
            # sent (when it isn't matched to its optimistic pending row by
            # main.py's by-type matching, e.g. sent from another linked
            # device) — an own message is never "unread", the same
            # principle first_unread_index() already applies when placing
            # the separator on conversation open. Without this guard, that
            # echo could insert/relocate a separator directly above the
            # user's own just-sent message, which is what made Alt+2 ("jump
            # to last message") land on a stale earlier separator/row
            # instead of the message the user actually just sent.
            if not from_me and self._counts_toward_unread_separator(msg):
                self._update_unread_separator_for_incoming(msg)

            # Append the real message (focus must NOT move)
            self._clear_empty_placeholder()
            self._sorted_messages.append(msg)
            self.messages_list.Append((self._render_message_line(msg),))
        finally:
            self.messages_list.Thaw()

        # Only scroll while WinZapp is already active; incoming notifications
        # must never move focus or alter the user's current foreground context.
        if getattr(self.main_window, "_allow_ui_focus_changes", lambda: False)():
            scrolled = False
            if top_msg_id:
                is_near_bottom = False
                last_idx_before = len(self._sorted_messages) - 2
                if last_idx_before - top_idx < 15:
                    is_near_bottom = True
                
                if not is_near_bottom:
                    for idx, msg in enumerate(self._sorted_messages):
                        if isinstance(msg, dict) and msg.get("key", {}).get("id") == top_msg_id:
                            self.messages_list.EnsureVisible(idx)
                            scrolled = True
                            break
            
            if not scrolled:
                last = self.messages_list.GetItemCount() - 1
                if last >= 0:
                    self.messages_list.EnsureVisible(last)

    def navigate_to_jid(self, jid: str):
        """Select and open the conversation matching jid, clearing any search."""
        # Clear search so all chats are visible
        if self.search_field.GetValue():
            self.search_field.SetValue("")
            self.main_window.add_chats_to_ui()

        # Find the chat index and activate it
        for i, chat in enumerate(self.chats_list):
            if chat.get("remoteJid", "") == jid:
                self.conversations_list.Focus(i)
                self.conversations_list.Select(i)
                self.conversations_list.EnsureVisible(i)
                self.navigate_to_conversation(chat)
                break

    # ── Populate ─────────────────────────────────────────────────────────────

    def _clear_populating_messages_flag(self):
        self._populating_messages = False

    def _messages_signature(self):
        """Cheap fingerprint of everything ``populate_messages()`` would render.

        Deliberately built from the raw records rather than from rendered rows:
        it has to be cheap enough to run on every background refresh, and every
        field that can change a row's text is covered here (body, status,
        star/edit markers, reactions arrive as their own records, and the
        separator position is pinned by ``_first_unread_msg_id``).
        """
        conv = self.conversation or {}
        records = []
        container = conv.get("messages")
        if isinstance(container, dict):
            inner = container.get("messages")
            if isinstance(inner, dict) and isinstance(inner.get("records"), list):
                records = inner["records"]
        sig = []
        for m in records:
            if not isinstance(m, dict):
                continue
            key = m.get("key") or {}
            sig.append((
                key.get("id", ""),
                m.get("messageType", ""),
                m.get("status", ""),
                bool(m.get("starred")),
                bool(m.get("pinInChat")),
                bool(m.get("_edited")),
                bool(m.get("_local_pending")),
                self._extract_timestamp(m) or 0,
                self._get_message_content(m) or "",
            ))
        return (
            conv.get("remoteJid", ""),
            self._first_unread_msg_id,
            self._pending_open_unread,
            tuple(sig),
        )

    @staticmethod
    def _signature_changed_ids(old, new):
        """Which message ids differ between two _messages_signature() snapshots.

        Returns None when the difference isn't expressible as "these rows
        changed": a different conversation, a moved unread separator, or an id
        that is empty/repeated in either snapshot (which makes the per-id
        comparison below ambiguous). Those change which row sits where, so no
        per-row repaint can stand in for a rebuild.
        """
        if not (isinstance(old, tuple) and isinstance(new, tuple)):
            return None
        if len(old) != 4 or len(new) != 4 or old[:3] != new[:3]:
            return None
        old_rows = {r[0]: r for r in old[3]}
        new_rows = {r[0]: r for r in new[3]}
        if len(old_rows) != len(old[3]) or len(new_rows) != len(new[3]):
            return None
        if "" in old_rows or "" in new_rows:
            return None
        return {
            mid for mid in set(old_rows) | set(new_rows)
            if old_rows.get(mid) != new_rows.get(mid)
        }

    def _adopt_signature_after_repaint(self, msg_ids: set) -> None:
        """Move refresh_messages_if_changed()'s fingerprint forward after rows
        were repainted in place.

        populate_messages() snapshots the fingerprint on its way out, so a
        local change used to land in the cache as a side effect of rebuilding.
        Repainting instead leaves the cache describing the state *before* the
        change, and the next background refresh would find a mismatch and
        rebuild the whole list — moving the user's focus for something already
        correct on screen. Adopting the new fingerprint unconditionally would
        be worse: anything else that changed in `records` since the last
        rebuild would be swallowed and never rendered. So it's adopted only
        when the rows that differ are the ones just repainted.
        """
        try:
            new_sig = self._messages_signature()
        except Exception:
            logging.exception("[_adopt_signature_after_repaint] signature failed")
            self._messages_signature_cache = None
            return
        changed = self._signature_changed_ids(
            getattr(self, "_messages_signature_cache", None), new_sig
        )
        if changed is not None and changed <= set(msg_ids):
            self._messages_signature_cache = new_sig

    def _repaint_message_rows(self, msg_ids) -> bool:
        """Repaint the rows of *msg_ids* in place instead of rebuilding the
        list. Returns whether every requested row was found and repainted;
        callers fall back to a full rebuild when it returns False.

        Starring, pinning and a remote "delete for everyone" each change the
        text of rows already on screen and nothing else: the list is sorted by
        timestamp, which none of them touch, so no row moves, appears or
        disappears (a revoked message keeps its row — see
        _is_displayable_message()). populate_messages() nevertheless re-sorts
        and de-duplicates every record in the conversation, rebuilds the
        reaction map, recomputes the unread separator and the pagination
        window, then DeleteAllItems() + Append()s every row — and hands the
        screen reader a whole new list in the process (CLAUDE.md's
        Freeze()/Thaw() note). Starring a selection of messages in a long
        conversation is the visible case. Same idea as main.py's
        refresh_chat_row_text() for the conversations list.
        """
        ids = {i for i in (msg_ids or ()) if i}
        if not ids or not self._sorted_messages:
            return False
        # Backing list out of step with the control means a targeted
        # SetItemText would write the right text into the wrong row.
        if self.messages_list.GetItemCount() != len(self._sorted_messages):
            logging.info("[_repaint_message_rows] list out of step with rows — full path")
            return False
        try:
            found = self._set_message_row_texts(ids)
        except Exception:
            logging.exception("[_repaint_message_rows] failed — full path")
            return False
        if found != ids:
            # Something asked for isn't rendered: paginated out of the current
            # window, or replaced by a resync while a server call was in
            # flight. The rebuild is the only thing that can show it.
            logging.info("[_repaint_message_rows] %d of %d rows not rendered — full path",
                         len(ids - found), len(ids))
            return False
        self._adopt_signature_after_repaint(ids)
        return True

    def _repaint_or_repopulate(self, msg_ids) -> None:
        """Repaint just the rows of *msg_ids*, rebuilding the list only if
        that isn't possible. The shape every local flag change uses."""
        if not self._repaint_message_rows(msg_ids):
            self.populate_messages(preserve_focus=True)

    def _row_position_suffix_active(self) -> bool:
        """Se cada linha carrega o sufixo ", N de M" (modo listbox com a
        contagem de itens ligada).

        Importa para quem acrescenta linha em vez de reconstruir: acrescentar
        muda o M de TODAS as linhas já renderizadas, e só o rebuild re-renderiza
        todas. Com o sufixo ligado, o caminho incremental deixaria a lista
        inteira anunciando um total velho ao leitor de tela — pior que a
        lentidão que ele evita.
        """
        if getattr(self, "_message_list_mode", "classic") != "listbox":
            return False
        mw = getattr(self, "main_window", None)
        settings = getattr(mw, "settings", None)
        if not isinstance(settings, dict):
            return False
        return bool(settings.get("user_interface", {}).get("show_listbox_item_count", False))

    def _append_new_tail_rows(self, old_sig, new_sig) -> bool:
        """Renderiza mensagens que só chegaram no FIM da conversa acrescentando
        as linhas delas, em vez de reconstruir a lista. Devolve se a diferença
        inteira entre as duas assinaturas foi coberta assim; quem chama
        reconstrói quando não foi.

        É o caso mais comum que sobrou passando pelo rebuild: mensagem nova. O
        painel já a acrescenta ao vivo em on_incoming_message(), mas o refresh
        de fundo que vem segundos depois (sync_chat_messages() ->
        _refresh_open_conversation_after_sync(), o backfill de histórico, a
        rodada de 60s) só sabia comparar a assinatura e chamar
        populate_messages(): DeleteAllItems() mais um Append() por linha, para
        pintar de novo o que já estava na tela — numa janela que agora pode ter
        milhares de linhas. Mesma ideia de _repaint_message_rows(), que já faz
        isso para estrela/fixar/apagar, e de refresh_chat_row_text() na lista de
        conversas.

        Na prática o caminho normal acrescenta ZERO linha: a mensagem já foi
        pintada ao vivo, e o que este método faz é reconhecer isso e adotar a
        assinatura, transformando o rebuild seguinte em nada. Acrescentar de
        fato é o caminho de quem chegou pelo sync sem passar pelo live.

        Recusa tudo que não seja "linhas novas no fim, nada mais mudou", porque
        aí o rebuild é a única coisa que sabe onde a linha vai:

        - qualquer linha existente que mudou de texto ou sumiu (a comparação
          por id de _signature_changed_ids(), que já devolve None sozinha para
          conversa trocada, separador movido ou id vazio/repetido);
        - registro novo que não vira linha — a reação é o caso comum: ela muda
          o texto de OUTRA linha, e a assinatura não diz de qual;
        - registro novo mais antigo que a última linha, que entraria no meio da
          lista ordenada por timestamp, não no fim;
        - qualquer reordenação dos registros antigos, que a comparação por id
          não veria e o sort estável do rebuild veria;
        - lista fora de passo com o controle, lista vazia (acrescentar na lista
          vazia com foco reproduz o pulo de foco para a linha 0 que o Freeze()
          de populate_messages() documenta) e a lista de placeholder;
        - o sufixo ", N de M" ligado (ver _row_position_suffix_active()).

        Uma exceção à recusa por registro não exibível: reação a um status
        NOSSO é linha de verdade (_is_displayable_message() ->
        reaction_targets_status()), então ela é acrescentada como qualquer
        mensagem. O rebuild também a poria em _reaction_map, e o atalho não —
        sem divergência visível, porque essa entrada é chaveada por um id de
        status@broadcast, que não tem linha nesta conversa para decorar.

        Consequência deliberada, não efeito colateral: o separador de não
        lidas que on_incoming_message() insere ao vivo sobrevive a este
        refresh, e é seguro porque só se acrescenta na cauda: _unread_sep_idx
        continua apontando para a mesma linha e _dismiss_unread_separator()
        continua funcionando. A assimetria que existia aqui — o separador ao
        vivo era removido pelo primeiro rebuild que qualquer OUTRA mudança
        provocasse, porque este caminho não gravava _first_unread_msg_id — foi
        fechada do outro lado: _update_unread_separator_for_incoming() grava a
        âncora e a contagem, e _place_unread_separator_for_rebuild() as lê de
        volta.
        """
        if self.conversation is None or not self._sorted_messages:
            return False
        changed = self._signature_changed_ids(old_sig, new_sig)
        if changed is None:
            return False
        old_rows, new_rows = old_sig[3], new_sig[3]
        # Crescimento no fim, e nada mais: os registros antigos têm de continuar
        # lá, iguais e na mesma ordem. Comparar só os conjuntos de id deixaria
        # passar uma reordenação pura, e ela não é inócua — populate_messages()
        # ordena por timestamp com sort estável, então duas mensagens de mesmo
        # timestamp trocam de lugar no rebuild e a lista na tela deixaria de ser
        # a que o rebuild produziria.
        if len(new_rows) < len(old_rows) or new_rows[:len(old_rows)] != old_rows:
            return False
        added = {row[0] for row in new_rows[len(old_rows):]}
        # Implicado pelo prefixo acima; custa uma comparação de conjuntos e
        # prende a conclusão em vez de depender do raciocínio.
        if changed != added:
            return False
        if self._row_position_suffix_active():
            return False
        # Lista de fora de passo com o controle: um Append() aqui desalinharia
        # texto e registro para sempre. Mesma guarda de _repaint_message_rows().
        if self.messages_list.GetItemCount() != len(self._sorted_messages):
            logging.info("[_append_new_tail_rows] list out of step with rows — full path")
            return False
        first_row = self._sorted_messages[0]
        if isinstance(first_row, dict) and first_row.get("_type") == "empty_placeholder":
            return False

        records = []
        container = self.conversation.get("messages")
        if isinstance(container, dict):
            inner = container.get("messages")
            if isinstance(inner, dict) and isinstance(inner.get("records"), list):
                records = inner["records"]
        newly = {}
        for m in records:
            if not isinstance(m, dict):
                continue
            mid = (m.get("key") or {}).get("id", "")
            # `mid not in newly` é inalcançável — _signature_changed_ids() já
            # devolveu None para id repetido — e fica pelo mesmo motivo que a
            # comparação `changed != added` acima: o mapa por id só é seguro se
            # o id for único, e isso passa a estar dito aqui também.
            if mid in added and mid not in newly:
                newly[mid] = m
        if len(newly) != len(added):
            return False
        if any(not self._is_displayable_message(m) for m in newly.values()):
            return False

        rendered = {
            (m.get("key") or {}).get("id", "")
            for m in self._sorted_messages if not self._is_separator(m)
        }
        newcomers = [m for mid, m in newly.items() if mid not in rendered]
        newcomers.sort(key=lambda m: self._extract_timestamp(m) or 0)
        tail_ts = None
        for m in reversed(self._sorted_messages):
            if not self._is_separator(m):
                tail_ts = self._extract_timestamp(m) or 0
                break
        if tail_ts is None:
            # Só sentinela na tela e nenhuma mensagem: não há cauda contra a
            # qual comparar, e um piso 0 aqui aprovaria qualquer timestamp.
            return False
        if any((self._extract_timestamp(m) or 0) < tail_ts for m in newcomers):
            return False

        if newcomers:
            # _all_sorted_messages só acompanha enquanto as duas listas
            # terminarem no mesmo objeto. O append ao vivo de
            # on_incoming_message() já as deixa fora de passo no fim (situação
            # anterior a isto), e não é este método que vai inventar um
            # alinhamento que ele não tem como verificar.
            # A consequência do desalinhamento é maior do que parece e vale
            # dizer por extenso: _load_older_messages() tira loaded_db_count
            # de _all_sorted_messages, então com ela curta a consulta local
            # devolve mensagens que já estão em memória, o dedup zera n_new e
            # ele cai para o servidor ANTES de esgotar o histórico local. O
            # usuário ainda recebe o histórico, só que pelo caminho caro. É
            # pré-existente, não regressão deste método — mas é o que dá para
            # perder aqui, não "algumas mensagens que o dedup descarta".
            # O `is not` não é paranoia: _sorted_messages tem de ser um SUFIXO
            # de _all_sorted_messages, nunca o mesmo objeto de lista. Hoje todo
            # produtor fatia ou concatena, então são sempre listas distintas;
            # se alguma passar a aliasar, os dois append() abaixo virariam dois
            # na mesma lista e ela desalinharia do controle.
            in_step = (
                self._all_sorted_messages
                and self._all_sorted_messages is not self._sorted_messages
                and self._all_sorted_messages[-1] is self._sorted_messages[-1]
            )
            self.messages_list.Freeze()
            try:
                for m in newcomers:
                    if in_step:
                        self._all_sorted_messages.append(m)
                    self._sorted_messages.append(m)
                    self.messages_list.Append((self._render_message_line(m),))
            finally:
                self.messages_list.Thaw()
            self._remember_expanded_window()
        self._messages_signature_cache = new_sig
        logging.info(
            "[_append_new_tail_rows] %d new record(s), %d row(s) appended, %d row(s) total "
            "— no rebuild.",
            len(added), len(newcomers), len(self._sorted_messages),
        )
        return True

    def refresh_messages_if_changed(self):
        """Repopulate the messages list only when its content actually changed.

        Every unattended refresh must come through here rather than calling
        ``populate_messages(preserve_focus=True)`` directly. That rebuild does a
        full ``DeleteAllItems()`` + re-``Append()`` of the native ListView, and
        even with preserve_focus it can only put focus back on the *message* it
        saved — the moment that message is no longer in the paginated window (or
        the list was showing the unread separator, or the saved id came back
        empty) focus lands somewhere else entirely. With a 60s poll calling it
        unconditionally, the user was thrown to a random message in the middle
        of the conversation roughly once a minute, mid-read.

        Nothing periodic needs a rebuild when nothing changed, so compare first
        and skip. When the only difference is messages at the END of the
        conversation — a new message, which is the overwhelmingly common case —
        _append_new_tail_rows() covers it by appending those rows (usually
        none: the live path already painted them) and the rebuild is skipped
        too. Anything else still rebuilds in full, preserving focus as best it
        can.
        """
        if self.conversation is None:
            return
        try:
            sig = self._messages_signature()
        except Exception:
            # Never let a fingerprinting hiccup swallow a real refresh.
            logging.exception("[refresh_messages_if_changed] signature failed")
            self.populate_messages(preserve_focus=True)
            return
        if sig == getattr(self, "_messages_signature_cache", None):
            return
        try:
            if self._append_new_tail_rows(
                getattr(self, "_messages_signature_cache", None), sig
            ):
                return
        except Exception:
            # O rebuild abaixo repinta a conversa inteira de qualquer forma, e
            # é ele que estava aqui antes: uma falha no atalho não pode custar
            # a atualização.
            logging.exception("[refresh_messages_if_changed] tail append failed — full path")
        self._messages_signature_cache = sig
        self.populate_messages(preserve_focus=True)

    def populate_messages(self, preserve_focus: bool = False):
        """Rebuild the messages list from self.conversation.

        preserve_focus=True keeps whatever message is currently focused
        instead of resetting to the unread separator / last message — used
        by background refreshes (e.g. the on-demand sync kicked off by
        navigate_to_conversation) so they don't silently yank focus away
        from the user a few seconds after a conversation was opened.
        """
        # Guards the lazy-load-on-focus-0 hook in _on_message_focused: this
        # method's own Focus(0) calls below (a short conversation whose last
        # message or unread separator sits at index 0) fire EVT_LIST_ITEM_FOCUSED
        # synchronously, and re-entering _load_older_messages()/
        # _load_more_messages() — which themselves call DeleteAllItems()/Append()
        # on this same list — while this rebuild is still in progress would
        # corrupt the list. Cleared via CallAfter so it stays set for every
        # nested/synchronous focus event this call produces, and only turns
        # off once control actually returns to the event loop.
        self._populating_messages = True
        wx.CallAfter(self._clear_populating_messages_flag)

        _preserved_msg_id = self._focused_msg_id() if preserve_focus else None
        _had_focus = (wx.Window.FindFocus() is self.messages_list)
        # _focused_msg_id() returns "" both when nothing is focused AND when
        # the focused row is the unread-separator sentinel (it has no
        # message id). Without telling those two apart, a background
        # refresh (preserve_focus=True) that fires while the user happens to
        # be sitting right on the separator row falls through to the same
        # "jump to separator/last message" default used for a freshly opened
        # conversation — see the preserve_focus fallback below.
        _preserved_was_separator = False
        if preserve_focus and not _preserved_msg_id:
            _fi = self.messages_list.GetFocusedItem()
            if 0 <= _fi < len(self._sorted_messages) and self._is_separator(self._sorted_messages[_fi]):
                _preserved_was_separator = True

        top_msg_id = None
        if preserve_focus:
            top_idx = -1
            if hasattr(self.messages_list, "GetTopItem"):
                top_idx = self.messages_list.GetTopItem()
            else:
                try:
                    import ctypes
                    hwnd = self.messages_list.GetHandle()
                    top_idx = ctypes.windll.user32.SendMessageW(hwnd, 0x018E, 0, 0)
                except Exception:
                    pass
            if top_idx != -1 and 0 <= top_idx < len(self._sorted_messages):
                m = self._sorted_messages[top_idx]
                if not self._is_separator(m):
                    top_msg_id = m.get("key", {}).get("id", "")

        # Frozen for the whole rebuild-and-refocus sequence below. Without
        # this, DeleteAllItems() followed by re-Append()ing every row made
        # the native SysListView32 control (still holding keyboard focus the
        # entire time, since this fires from a background wx.CallAfter while
        # the conversation stays open) briefly auto-assign LVIS_FOCUSED to
        # row 0 the moment the first item was appended back into what was,
        # for an instant, an empty focused list — a real Win32 ListView
        # quirk, independent of any Focus()/Select() call this method makes
        # itself. That transient focus (on whatever message pagination
        # happens to put at row 0) fired its own accessibility event, which
        # NVDA could announce — reported live as focus "randomly" jumping to
        # a fixed message (always row 0 of the current pagination window)
        # every time a background refresh (e.g. history backfill delivering
        # an already-seen message for the open conversation) repopulated the
        # list, without ever changing the visible selection. Freeze()
        # suppresses native repaint/accessibility notifications until Thaw()
        # runs in the finally block below, by which point only the final,
        # correct Focus()/Select() call (or lack thereof) is ever observed.
        #
        # Medido, não estimado, pelo mesmo motivo do repaint de nomes em
        # main.py: este rebuild é DeleteAllItems() + um Append() por linha, ele
        # roda a cada mensagem nova, e a janela deixou de ser limitada ao
        # messages_page_size. Uma linha por rebuild diz quanto custa a janela
        # no tamanho a que ela chegou.
        _rebuild_started = time.monotonic()
        self.messages_list.Freeze()
        try:
            self.messages_list.DeleteAllItems()
            self._unread_sep_idx = -1
            self._reaction_map = {}
            messages_container = (
                self.conversation.get("messages", {}) if self.conversation else {}
            )
            messages: list = []
            if isinstance(messages_container, dict):
                inner = messages_container.get("messages")
                if isinstance(inner, dict) and isinstance(inner.get("records"), list):
                    messages = inner["records"]
            try:
                messages_sorted = sorted(
                    messages, key=lambda m: self._extract_timestamp(m) or 0
                )
            except Exception:
                messages_sorted = messages

            # Deduplicate by key.id — records may accumulate duplicates when the
            # same message arrives via both the initial sync and messages.upsert.
            # Keep the last occurrence (latest version of the message wins).
            _seen_ids: dict = {}
            for i, m in enumerate(messages_sorted):
                if not isinstance(m, dict):
                    continue
                mid = m.get("key", {}).get("id", "")
                if mid:
                    _seen_ids[mid] = i
            _kept = set(_seen_ids.values())
            messages_sorted = [
                m for i, m in enumerate(messages_sorted)
                if isinstance(m, dict) and (
                    not m.get("key", {}).get("id", "") or i in _kept
                )
            ]

            # Build reaction map from all reaction messages. Each sender can only
            # have ONE active reaction on a message at a time — later records for
            # the same (message, sender) pair replace the earlier one instead of
            # accumulating a count, and an empty emoji means that sender removed
            # their reaction.
            for m in messages_sorted:
                if isinstance(m, dict) and m.get("messageType") == "reactionMessage":
                    reaction   = (m.get("message") or {}).get("reactionMessage") or {}
                    emoji      = reaction.get("text", "")
                    orig_id    = (reaction.get("key") or {}).get("id", "")
                    sender_key = self._reactor_key_from_msg(m)
                    if orig_id and sender_key:
                        per_msg = self._reaction_map.setdefault(orig_id, {})
                        if emoji:
                            per_msg[sender_key] = emoji
                        else:
                            per_msg.pop(sender_key, None)

            # Exclude reaction messages — they must not affect index mapping
            displayable = [
                m for m in messages_sorted if self._is_displayable_message(m)
            ]

            # Insert unread separator before the first unread message, either
            # derived from the snapshot taken before mark_conversation_as_read()
            # zeros the dict, or restored from the state the live path left
            # behind (see _place_unread_separator_for_rebuild()).
            displayable = self._place_unread_separator_for_rebuild(displayable)

            # ── Pagination: show only last N messages ────────────────────────────
            self._all_sorted_messages = displayable
            limit = int(
                self.main_window.settings.get("user_interface", {}).get("messages_page_size", 200)
            )
            self._messages_offset, self._unread_sep_idx = (
                self._history_window_for_rebuild(displayable, limit)
            )
            paginated = displayable[self._messages_offset:]

            # A chat with no displayable history (e.g. WhatsApp Web's own store
            # never loaded this conversation's messages, so all WinZapp captured
            # was a non-displayable system record) previously left messages_list
            # with zero rows and nothing was ever focused — for a screen-reader
            # user that reads as total silence, indistinguishable from the app
            # being broken. Show one non-actionable placeholder row instead.
            if not paginated:
                paginated = [{"_type": "empty_placeholder"}]

            self._sorted_messages = paginated

            for msg in paginated:
                self.messages_list.Append((self._render_message_line(msg),))

            # Restore scroll position if preserve_focus is True and we tracked a top visible message
            scrolled = False
            if preserve_focus and top_msg_id:
                for idx, msg in enumerate(self._sorted_messages):
                    if isinstance(msg, dict) and msg.get("key", {}).get("id") == top_msg_id:
                        self.messages_list.EnsureVisible(idx)
                        scrolled = True
                        break

            # A background refresh (preserve_focus=True) should keep the user's
            # current position instead of jumping back to the separator/last
            # message — only fall back to the default placement below if the
            # previously-focused message is no longer present (e.g. it was
            # cleared or paginated out).
            if _preserved_msg_id:
                for idx, msg in enumerate(self._sorted_messages):
                    if isinstance(msg, dict) and msg.get("key", {}).get("id") == _preserved_msg_id:
                        if _had_focus:
                            self.messages_list.SetFocus()
                        self.messages_list.Focus(idx)
                        self.messages_list.Select(idx)
                        if not scrolled:
                            self.messages_list.EnsureVisible(idx)
                        return

            if preserve_focus:
                if _preserved_was_separator and self._unread_sep_idx >= 0:
                    if _had_focus:
                        self.messages_list.SetFocus()
                    self.messages_list.Focus(self._unread_sep_idx)
                    self.messages_list.Select(self._unread_sep_idx)
                    if not scrolled:
                        self.messages_list.EnsureVisible(self._unread_sep_idx)
                return

            # Make the unread separator visible, or select and focus the last (newest) message by default
            if not scrolled:
                if self._unread_sep_idx >= 0:
                    last = self.messages_list.GetItemCount() - 1
                    target_visible = min(self._unread_sep_idx + 3, last)
                    if target_visible >= 0:
                        self.messages_list.EnsureVisible(target_visible)
                    self.messages_list.EnsureVisible(self._unread_sep_idx)
                    self.messages_list.Focus(self._unread_sep_idx)
                    self.messages_list.Select(self._unread_sep_idx)
                else:
                    last = self.messages_list.GetItemCount() - 1
                    if last >= 0:
                        self.messages_list.EnsureVisible(last)
                        self.messages_list.Focus(last)
                        self.messages_list.Select(last)
                        logging.info(
                            "[populate_messages] default-select tail: last=%d "
                            "GetFocusedItem()=%d GetFirstSelected()=%d ItemCount=%d",
                            last, self.messages_list.GetFocusedItem(),
                            self.messages_list.GetFirstSelected(),
                            self.messages_list.GetItemCount(),
                        )
                    else:
                        logging.info("[populate_messages] default-select tail: list is empty (last=-1)")
        finally:
            self.messages_list.Thaw()
            # A janela que acabou de ser pintada é o piso da próxima. Aqui, no
            # finally, pelo mesmo motivo da assinatura abaixo: o corpo retorna
            # de vários pontos, e um rebuild que não registrasse a janela
            # deixaria o seguinte livre para cortá-la. Ver
            # _remember_expanded_window().
            try:
                self._remember_expanded_window()
            except Exception:
                logging.exception("[populate_messages] failed to record the rendered window")
            # Snapshot what is now on screen so the next background refresh can
            # tell "nothing changed" apart from "needs a rebuild" — see
            # refresh_messages_if_changed(). Taken here, in the finally, because
            # the body above returns from several places.
            try:
                self._messages_signature_cache = self._messages_signature()
            except Exception:
                self._messages_signature_cache = None
            _rebuild_ms = (time.monotonic() - _rebuild_started) * 1000.0
            # Acima do limiar sobe para WARNING. Em INFO o número só aparece
            # para quem já foi procurar por ele, e este é o laço que a janela
            # sem teto alarga: DeleteAllItems() + um Append() por linha, a cada
            # mensagem nova. 250 ms é uma ordem de grandeza abaixo dos stalls
            # que o watchdog mediu no laço vizinho (9,4 s / 19,8 s / 40,1 s —
            # ver _schedule_refresh_active_messages() em main.py), então o
            # aviso chega no log antes de o usuário sentir travamento.
            if _rebuild_ms > 250.0:
                logging.warning(
                    "[populate_messages] rebuilt %d row(s) in %.0f ms (offset=%d).",
                    len(self._sorted_messages), _rebuild_ms, self._messages_offset,
                )
            elif logging.getLogger().isEnabledFor(logging.INFO):
                logging.info(
                    "[populate_messages] rebuilt %d row(s) in %.0f ms (offset=%d).",
                    len(self._sorted_messages), _rebuild_ms, self._messages_offset,
                )


    # ── Mass action handlers ────────────────────────────────────────────────
    # Act on the Space-toggled selections (self.selected_chats /
    # self.selected_messages), reached from the "mass actions" submenu both
    # context menus grow while a selection exists.

    def _on_mass_clear_chats(self, event):
        i18n = self.main_window.i18n
        if not self.selected_chats: return
        count = len(self.selected_chats)
        if wx.MessageBox(
            i18n.t("clear_confirm_msg_bulk").format(count=count),
            i18n.t("clear_chat_bulk_title"),
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        for jid in list(self.selected_chats):
            self.main_window.clear_chat(jid)
        self.selected_chats.clear()
        self.main_window.add_chats_to_ui()
        self.main_window.output(i18n.t("success_clear"), interrupt=True)

    def _on_mass_delete_chats(self, event):
        i18n = self.main_window.i18n
        if not self.selected_chats: return
        count = len(self.selected_chats)
        if wx.MessageBox(
            i18n.t("delete_confirm_msg_bulk").format(count=count),
            i18n.t("delete_chat_bulk_title"),
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        for jid in list(self.selected_chats):
            self.main_window.delete_chat(jid)
        self.selected_chats.clear()
        self.main_window.add_chats_to_ui()
        self.main_window.output(i18n.t("success_delete"), interrupt=True)

    def _on_mass_archive_chats(self, event):
        i18n = self.main_window.i18n
        if not self.selected_chats: return
        for jid in list(self.selected_chats):
            self.main_window.archive_chat(jid, True)
        self.selected_chats.clear()
        self.main_window.add_chats_to_ui()
        self.main_window.output(i18n.t("success_archive"), interrupt=True)

    def _on_mass_mark_read_chats(self, event):
        if not self.selected_chats: return
        for jid in list(self.selected_chats):
            self.main_window.mark_conversation_as_read(jid, True)
        self.selected_chats.clear()
        self.main_window.add_chats_to_ui()

    def _on_mass_mark_unread_chats(self, event):
        if not self.selected_chats: return
        for jid in list(self.selected_chats):
            self.main_window.mark_conversation_as_unread(jid)
        self.selected_chats.clear()
        self.main_window.add_chats_to_ui()

    def _on_mass_copy_messages(self, event):
        """Copy every selected plain-text message to the clipboard as one
        WhatsApp-export-style block of text, one line per message formatted
        "<date> <time> - <sender>: <text>" — the date/time pattern follows
        the active app language's datetime_fmt through
        core.message_copy_format, independently of the Windows regional
        format used by timestamps displayed elsewhere in the interface.
        Other message types (media, location, contact cards, ...) are
        silently skipped, same as how _on_menu_copy_message only ever
        handles "conversation"/"extendedTextMessage". Order follows
        _sorted_messages, not set iteration order, same as the other mass
        message actions."""
        if not self.selected_messages: return
        i18n = self.main_window.i18n
        _TEXT_TYPES = ("conversation", "extendedTextMessage")
        lines = []
        for m in self._sorted_messages:
            if self._is_separator(m) or m.get("key", {}).get("id") not in self.selected_messages:
                continue
            msg_type = m.get("messageType", "")
            if msg_type not in _TEXT_TYPES:
                continue
            msg_obj = m.get("message") or {}
            text = (
                msg_obj.get("conversation", "") if msg_type == "conversation"
                else (msg_obj.get("extendedTextMessage") or {}).get("text", "")
            )
            if not text:
                continue
            sender = self._sender_label(m)
            ts = self._extract_timestamp(m)
            lines.append(format_copied_message(
                ts, sender, text, i18n.t("datetime_fmt")))

        if not lines:
            self.main_window.output(i18n.t("copy_selected_nothing_to_copy"), interrupt=True)
            return

        try:
            pyperclip.copy("\n".join(lines))
        except Exception:
            self.main_window.output(i18n.t("msg_copy_error"), interrupt=True)
            return

        copied_ids = list(self.selected_messages)
        self.selected_messages.clear()
        self._refresh_message_rows_by_ids(copied_ids)
        self.main_window.output(i18n.t("messages_copied_bulk"), interrupt=True)

    def _mass_message_targets(self, flag: str) -> "tuple[list, list]":
        """(messages to act on, all selected ids) for a mass message action.

        Targets are real messages in the current selection that don't already
        carry *flag* — system events are filtered here rather than left to
        each single-message handler's own _reject_system_event_action guard,
        so a mixed selection doesn't announce "unavailable" once per system
        event. Order follows _sorted_messages, not set iteration order, same
        as every other mass message action.
        """
        targets = [
            m for m in self._sorted_messages
            if not self._is_separator(m)
            and not self._is_system_event(m)
            and m.get("key", {}).get("id") in self.selected_messages
            and not m.get(flag)
        ]
        return targets, list(self.selected_messages)

    def _on_mass_star_messages(self, event):
        """Star every selected message (the local-only flag _on_menu_star
        toggles) — always stars, never toggles off, so a selection mixing
        already-starred and unstarred messages doesn't end up partially
        undone.

        Applies the flag to the whole batch and repaints ONCE, rather than
        calling _on_menu_star() per message: that handler runs a full
        populate_messages() of its own every time, so a selection of N
        messages repainted the entire list N times on the UI thread and gave
        screen readers N floods of accessibility events (the exact thing
        CLAUDE.md's Freeze()/Thaw() note warns about). Same aggregate shape
        the older mass actions (_on_mass_forward_messages,
        _on_mass_delete_messages) already use.
        """
        if not self.selected_messages: return
        i18n = self.main_window.i18n
        to_star, ids = self._mass_message_targets("starred")
        self.selected_messages.clear()
        if not to_star:
            # Everything selected was already starred (or was a system event).
            # Announcing success here told screen-reader users the action had
            # been applied when nothing happened at all.
            self._refresh_message_rows_by_ids(ids)
            self.main_window.output(i18n.t("mass_nothing_to_do"), interrupt=True)
            return

        for m in to_star:
            m["starred"] = True
        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if jid:
            self._persist_message_local_flags(jid, to_star)
        self.main_window._schedule_save()
        # The whole selection, not just to_star: clearing selected_messages
        # above dropped the " selecionado" marker from every row in it.
        self._repaint_or_repopulate(ids)
        self.main_window.output(i18n.t("success_star_bulk"), interrupt=True)

    def _on_mass_pin_messages(self, event):
        """Pin every not-yet-pinned selected message via WhatsApp's own
        message-pin feature (visible to everyone in the chat, unlike star).

        Like _on_mass_star_messages, applies the optimistic update to the
        whole batch and repaints once. The server calls additionally run on
        ONE background thread, sequentially, and their failures are collected
        into a single rollback + a single dialog reporting the count —
        _on_menu_pin_message() starts a thread per message and pops its own
        blocking wx.MessageBox per rejection, so pinning a selection the
        server refuses used to fire N concurrent requests at the local
        WPPConnect server and then stack N modal dialogs, one per message.
        (Same failure mode c518cce fixed for posting several files as status.)
        """
        if not self.selected_messages: return
        i18n = self.main_window.i18n
        to_pin, ids = self._mass_message_targets("pinInChat")
        self.selected_messages.clear()
        if not to_pin:
            self._refresh_message_rows_by_ids(ids)
            self.main_window.output(i18n.t("mass_nothing_to_do"), interrupt=True)
            return

        jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        if not jid:
            self._refresh_message_rows_by_ids(ids)
            return

        for m in to_pin:
            m["pinInChat"] = True
        self._persist_message_local_flags(jid, to_pin)
        self.main_window._schedule_save()
        self._repaint_or_repopulate(ids)   # see _on_mass_star_messages on `ids`
        self.main_window.output(i18n.t("success_pin_bulk"), interrupt=True)

        # Keys are copied now: the message dicts can be replaced underneath us
        # by a resync while the requests are still in flight.
        pending = [(m, dict(m.get("key", {}))) for m in to_pin]
        total   = len(pending)

        def _do(j=jid, items=pending, n=total):
            failed = []
            for m, k in items:
                try:
                    ok = self.main_window.pin_message(j, k, True)
                except Exception as exc:
                    logging.warning("[_on_mass_pin_messages] pin_message raised for %s: %s",
                                    k.get("id", ""), exc)
                    ok = False
                if not ok:
                    failed.append(m)
            if failed:
                wx.CallAfter(self._on_mass_pin_failed, failed, j, n)

        threading.Thread(target=_do, daemon=True).start()

    def _on_mass_pin_failed(self, failed: list, jid: str, total: int):
        """Roll back the optimistic pins the server rejected, all at once
        (main thread) — one repaint and one dialog carrying the count, rather
        than _on_pin_message_failed()'s per-message repaint + modal."""
        for m in failed:
            m["pinInChat"] = False
        self._persist_message_local_flags(jid, failed)
        self.main_window._schedule_save()
        self._repaint_or_repopulate([m.get("key", {}).get("id", "") for m in failed])
        i18n = self.main_window.i18n
        wx.MessageBox(
            f"{i18n.t('pin_message_failed')} ({len(failed)}/{total})",
            i18n.t("pin_message"),
            wx.OK | wx.ICON_WARNING,
        )

    def _on_mass_forward_messages(self, event):
        if not self.selected_messages: return
        msgs_to_forward = []
        for m in self._sorted_messages:
            if not self._is_separator(m) and m.get("key", {}).get("id") in self.selected_messages:
                msgs_to_forward.append(m)
        if msgs_to_forward:
            self._on_menu_forward(msgs_to_forward[0], msgs_list=msgs_to_forward)
        forwarded_ids = list(self.selected_messages)
        self.selected_messages.clear()
        self._refresh_message_rows_by_ids(forwarded_ids)
        self.main_window.output(self.main_window.i18n.t("unselected"), interrupt=True)

    def _on_mass_save_messages(self, event):
        if not self.selected_messages: return
        i18n = self.main_window.i18n
        msgs = []
        for msg_id in list(self.selected_messages):
            msg = next((m for m in self._sorted_messages if not self._is_separator(m) and m.get("key", {}).get("id") == msg_id), None)
            if msg and not self._is_separator(msg) and msg.get("messageType", "") in _SAVEABLE_MESSAGE_TYPES:
                msgs.append(msg)

        if not msgs:
            self.main_window.output(i18n.t("save_as_nothing_to_save_bulk"), interrupt=True)
            return

        with wx.DirDialog(
            self,
            i18n.t("select_folder_dialog_title"),
            defaultPath=resolve_save_dialog_folder(self.main_window.settings),
            style=wx.DD_DEFAULT_STYLE,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            target_dir = dlg.GetPath()
        # A folder, not a file — join a name so dirname() lands on the folder
        # the user actually picked rather than on its parent.
        self.main_window.remember_save_folder(os.path.join(target_dir, "x"))

        # Resolve filenames up front and dedupe within this batch so two
        # messages that would otherwise collide (e.g. same original name)
        # don't clobber each other on disk.
        used_names = set()
        for msg in msgs:
            default_file = self._resolve_media_filename(msg)
            base, ext = os.path.splitext(default_file)
            candidate = default_file
            n = 1
            while candidate.lower() in used_names or os.path.isfile(os.path.join(target_dir, candidate)):
                candidate = f"{base}_{n}{ext}"
                n += 1
            used_names.add(candidate.lower())
            save_path = os.path.join(target_dir, candidate)
            threading.Thread(target=self._save_message_media, args=(msg, save_path), daemon=True).start()

        saved_ids = list(self.selected_messages)
        self.selected_messages.clear()
        self._refresh_message_rows_by_ids(saved_ids)

    def _group_admin_delete_override(self) -> bool:
        """True when the user is an admin of the currently open group — same
        check _on_menu_delete_message() does for a single message, pulled
        out so the bulk delete dialog can compute it once instead of once
        per selected message (it doesn't depend on which message: an admin
        can revoke ANY message in their own group, not just their own)."""
        if not self.conversation:
            return False
        conv_jid = self.conversation.get("remoteJid", "")
        if not conv_jid.endswith("@g.us"):
            return False
        group_meta = self.conversation.get("groupMetadata", {})
        participants = group_meta.get("participants") or self.conversation.get("participants") or []

        def _phone_part(j: str) -> str:
            return j.rsplit("@", 1)[0].split(":")[0] if isinstance(j, str) else ""

        mw = self.main_window
        my_phone = _phone_part(getattr(mw, "my_jid", ""))
        my_lid   = _phone_part(getattr(mw, "my_lid", ""))
        for p in participants:
            if not isinstance(p, dict):
                continue
            p_id = p.get("id", "")
            if isinstance(p_id, dict):
                p_id = p_id.get("_serialized", "")
            p_digits = _phone_part(p_id)
            if not p_digits:
                continue
            is_me = (my_phone and mw._phone_digits_equivalent(p_digits, my_phone)) or (my_lid and p_digits == my_lid)
            if is_me:
                return bool(p.get("admin") or p.get("isAdmin"))
        return False

    def _on_mass_delete_messages(self, event):
        """Same delete-scope dialog _on_menu_delete_message() shows for a
        single message — radio buttons for "delete for me"/"delete for
        everyone" plus Apagar/Cancelar — applied to every selected message
        instead of the old plain Yes/No "apagar N mensagens?" confirmation.
        In the "Me" chat that scope dialog is replaced by a plain Delete/
        Cancel confirmation, since "for everyone" is a no-op there."""
        i18n = self.main_window.i18n
        if not self.selected_messages: return

        msgs_to_delete = []
        for msg_id in self.selected_messages:
            msg = next((m for m in self._sorted_messages if not self._is_separator(m) and m.get("key", {}).get("id") == msg_id), None)
            if msg: msgs_to_delete.append(msg)
        if not msgs_to_delete:
            self.selected_messages.clear()
            return

        conv_jid = self.conversation.get("remoteJid", "") if self.conversation else ""
        is_self_chat = bool(conv_jid) and self.main_window._is_self_jid(conv_jid)

        admin_override = self._group_admin_delete_override()

        def _can_delete_for_all(msg):
            if self._is_system_event(msg):
                return False
            return admin_override or msg.get("key", {}).get("fromMe", False)

        # The "Me" chat has only one participant, so "delete for everyone" is
        # a no-op there for every message in the selection — same reasoning
        # _on_menu_delete_message() applies to a single message (issue #73).
        # Skip the for-me/for-everyone dialog entirely and go straight to a
        # plain Delete/Cancel confirmation (issue #95). Only the scope choice
        # is skipped: the delete itself goes through the same worker and the
        # same local-removal tail as every other bulk delete.
        if is_self_chat:
            if not self._confirm_local_only_delete(len(msgs_to_delete)):
                return
            for_everyone = False
        else:
            any_eligible = any(_can_delete_for_all(m) for m in msgs_to_delete)

            dlg = wx.Dialog(
                self,
                title=i18n.t("delete_messages_bulk_title"),
                style=wx.DEFAULT_DIALOG_STYLE,
            )
            panel = wx.Panel(dlg)
            sizer = wx.BoxSizer(wx.VERTICAL)

            rb_me = wx.RadioButton(panel, label=i18n.t("delete_for_me"), style=wx.RB_GROUP)
            rb_me.SetValue(True)
            sizer.Add(rb_me, 0, wx.ALL, 8)

            rb_all = None
            if any_eligible:
                rb_all = wx.RadioButton(panel, label=i18n.t("delete_for_everyone"))
                sizer.Add(rb_all, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

            btn_sizer  = wx.StdDialogButtonSizer()
            ok_btn     = wx.Button(panel, wx.ID_OK,     label=i18n.t("delete_messages_bulk_title"))
            cancel_btn = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
            btn_sizer.AddButton(ok_btn)
            btn_sizer.AddButton(cancel_btn)
            btn_sizer.Realize()
            sizer.Add(btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

            panel.SetSizer(sizer)
            dlg_sizer = wx.BoxSizer(wx.VERTICAL)
            dlg_sizer.Add(panel, 1, wx.EXPAND)
            dlg.SetSizer(dlg_sizer)
            dlg.Fit()
            dlg.CentreOnParent()

            result       = dlg.ShowModal()
            for_everyone = rb_all.GetValue() if rb_all else False
            dlg.Destroy()

            if result != wx.ID_OK:
                return

        def _delete_bg():
            for msg in msgs_to_delete:
                msg_key = dict(msg.get("key", {}))
                jid = self._delete_target_jid(msg_key)
                if not jid:
                    continue
                # Per message, never once for the batch: a mixed selection
                # (e.g. admin revoking a mix of their own and others'
                # messages, or a non-admin selection that also picked up a
                # system event) can have members that aren't actually
                # eligible for a real revoke even when "for everyone" was
                # chosen — those still get deleted, just locally-only.
                if for_everyone and _can_delete_for_all(msg):
                    self.main_window.delete_message_for_everyone(jid, msg_key)
                else:
                    self.main_window.delete_message_for_me(jid, msg_key)

        threading.Thread(target=_delete_bg, daemon=True).start()

        # Always delete locally
        self.remove_messages_by_id(set(self.selected_messages), focus_previous=True)
        self.selected_messages.clear()
        self.main_window.output(i18n.t("success_delete"), interrupt=True)

    def _on_accel_recent_reactions(self, event):
        if not self.conversation:
            return
        # Show recent reactions in the current conversation
        recent = []
        for msg_id, reactions in self._reaction_map.items():
            for jid, emoji in reactions.items():
                recent.append((msg_id, jid, emoji))

        if not recent:
            self.main_window.output(self.main_window.i18n.t("no_reactions_found"), interrupt=True)
            return

        recent.reverse() # show newest first
        recent = recent[:10] # limit to 10

        # Our own reactions are stored under the _SELF_REACTOR_KEY sentinel,
        # not a JID, so they can't go through the participant lookup — and the
        # word for them is the user's "Como se referir a mim?" choice
        # (Eu/Você/custom), not a hardcoded one. Same resolution the reactions
        # dialog does for the very same map; _get_participant_name() covers
        # the other half, where our own reaction did arrive under a real JID.
        msg_parts = []
        for msg_id, jid, emoji in recent:
            name = (
                self.main_window.self_reference_label()
                if jid == self._SELF_REACTOR_KEY
                else self._get_participant_name(jid)
            )
            msg_parts.append(f"{name}: {emoji}")

        text = self.main_window.i18n.t("recent_reactions") + " " + ", ".join(msg_parts)
        self.main_window.output(text, interrupt=True)

    def _on_accel_mentions(self, event):
        if not self.conversation:
            return

        mentions = []
        for i, msg in enumerate(self._sorted_messages):
            if self._is_separator(msg): continue

            # Check if mentioned
            msg_inner = msg.get("message", {})
            if isinstance(msg_inner, str):
                import json
                try:
                    msg_inner = json.loads(msg_inner)
                except:
                    msg_inner = {}

            mentioned_jids = []
            for msg_type_dict in msg_inner.values():
                if isinstance(msg_type_dict, dict):
                    ctx = msg_type_dict.get("contextInfo", {})
                    if "mentionedJid" in ctx:
                        mentioned_jids = ctx["mentionedJid"]
                        break

            if any(self.main_window._is_self_jid(j) for j in mentioned_jids):
                mentions.append(i)

        if not mentions:
            self.main_window.output(self.main_window.i18n.t("no_mentions_found"), interrupt=True)
            return

        # Jump to the next mention (older message, going backwards)
        curr_idx = self.messages_list.GetFocusedItem()
        target_idx = -1

        for idx in reversed(mentions):
            if curr_idx < 0 or idx < curr_idx:
                target_idx = idx
                break

        # If we reached the oldest mention, or started below the newest, wrap around to newest
        if target_idx == -1:
            target_idx = mentions[-1]

        self.messages_list.Focus(target_idx)
        self.messages_list.Select(target_idx, True)
        self.messages_list.EnsureVisible(target_idx)
        self.main_window.output(self.main_window.i18n.t("jumped_to_mention"), interrupt=True)


# ── Archived Conversations Panel ─────────────────────────────────────────────


class ArchivedConversationsPanel(wx.Panel):
    """
    Shows archived chats in a list.  Activating a chat opens it in the
    main ConversationsPanel.  A context menu allows unarchiving.
    """

    def __init__(self, main_window, parent):
        super().__init__(parent)
        self.main_window = main_window
        self.chats_list: list = []
        self.chat_names: list = []
        self._init_ui()
        self.create_accelerator_table()

    def restore_selection(self):
        """Select, focus and give keyboard focus to the first archived
        conversation — mirrors ConversationsPanel._restore_conversation_selection()
        so the list never ends up empty-focused (nothing for a screen reader
        to announce) after navigating here via Alt+4 or the nav-list item."""
        lst = self.conversations_list
        if self.chats_list:
            lst.Focus(0)
            lst.Select(0)
            lst.EnsureVisible(0)
        lst.SetFocus()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _init_ui(self):
        i18n  = self.main_window.i18n
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.conversations_label = wx.StaticText(
            self, label=i18n.t("archived_chats")
        )
        sizer.Add(self.conversations_label, 0, wx.LEFT | wx.TOP, 5)

        # ── Conversation filter tabs ─────────────────────────────────────────
        # Tracks the active filter key: 'all' | 'unread' | 'groups' | 'individual'
        self._conv_filter = 'all'
        self._filter_radio = wx.RadioBox(
            self,
            label=i18n.t("conv_filter_label"),
            choices=[
                i18n.t("conv_filter_all"),
                i18n.t("conv_filter_unread"),
                i18n.t("conv_filter_groups"),
                i18n.t("conv_filter_individual"),
            ],
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
        )
        self._filter_radio.Bind(wx.EVT_RADIOBOX, self._on_filter_changed)
        sizer.Add(self._filter_radio, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        self.conversations_list = wx.ListCtrl(
            self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL
        )
        self.conversations_list.InsertColumn(0, i18n.t("archived_chats"), width=250)
        # The wx.StaticText above is only a visual caption — on Windows a
        # wx.ListCtrl exposes no accessible name of its own, so NVDA announced
        # this list as a bare, unnamed "list" and the user had no way to tell
        # which of the two conversation lists they had landed in. Give it the
        # same explicit MSAA name treatment the messages list already gets.
        self._list_accessible = AccessibleMessagesListControl(i18n.t("archived_chats"))
        self.conversations_list.SetAccessible(self._list_accessible)
        self.conversations_list.Bind(
            wx.EVT_LIST_ITEM_ACTIVATED, self.on_conversation_selected
        )
        self.conversations_list.Bind(
            wx.EVT_CONTEXT_MENU, self.on_context_menu
        )
        self.conversations_list.Bind(wx.EVT_KEY_DOWN, self._on_arch_list_key_down)
        sizer.Add(self.conversations_list, 1, wx.EXPAND | wx.ALL, 5)

        self.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook_alt1)

    def _on_char_hook_alt1(self, event):
        if event.AltDown() and event.GetKeyCode() == ord('1'):
            self.main_window.on_alt_1(event)
            return
        event.Skip()

    # ── Accelerators ─────────────────────────────────────────────────────────

    def create_accelerator_table(self):
        """Same key combos as ConversationsPanel.create_accelerator_table(),
        applied to this panel instead. The archived list used to have none of
        these at all — Delete and Ctrl+Shift+L (clear) worked in the normal
        list but silently did nothing here, and the row context menu was
        missing everything except unarchive/clear/delete. Ctrl+F (search) and
        Ctrl+N (new conversation) are left out: this panel has no search field
        of its own, and Ctrl+W (close conversation) doesn't apply — there is no
        split conversation view to close from this list. Ctrl+Shift+Q always
        means "unarchive" here rather than toggling, since every row is
        archived by definition.
        """
        self.ID_DELETE_CONV      = wx.NewIdRef()
        self.ID_ALT_SHIFT_C_LIST = wx.NewIdRef()
        self.ID_CONV_DATA_LIST   = wx.NewIdRef()
        self.ID_TOGGLE_READ_LIST = wx.NewIdRef()
        self.ID_MUTE_LIST        = wx.NewIdRef()
        self.ID_BLOCK_LIST       = wx.NewIdRef()
        self.ID_CLEAR_LIST       = wx.NewIdRef()
        self.ID_UNARCHIVE_LIST   = wx.NewIdRef()
        self.ID_PIN_LIST         = wx.NewIdRef()
        CS = wx.ACCEL_CTRL | wx.ACCEL_SHIFT
        AS = wx.ACCEL_ALT | wx.ACCEL_SHIFT
        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_NORMAL, wx.WXK_DELETE, self.ID_DELETE_CONV),
            (AS,              ord("C"),      self.ID_ALT_SHIFT_C_LIST),
            (CS,              ord("D"),      self.ID_CONV_DATA_LIST),
            (CS,              ord("M"),      self.ID_TOGGLE_READ_LIST),
            (AS,              ord("S"),      self.ID_MUTE_LIST),
            (CS,              ord("B"),      self.ID_BLOCK_LIST),
            (CS,              ord("L"),      self.ID_CLEAR_LIST),
            (CS,              ord("Q"),      self.ID_UNARCHIVE_LIST),
            (wx.ACCEL_CTRL,   ord("P"),      self.ID_PIN_LIST),
        ])
        self.SetAcceleratorTable(accel_tbl)
        self.Bind(wx.EVT_MENU, self._on_accel_delete,             id=self.ID_DELETE_CONV)
        self.Bind(wx.EVT_MENU, self._on_accel_copy_number,        id=self.ID_ALT_SHIFT_C_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_conversation_data,  id=self.ID_CONV_DATA_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_toggle_read,        id=self.ID_TOGGLE_READ_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_mute,               id=self.ID_MUTE_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_block,              id=self.ID_BLOCK_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_clear,              id=self.ID_CLEAR_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_unarchive,          id=self.ID_UNARCHIVE_LIST)
        self.Bind(wx.EVT_MENU, self._on_accel_pin,                id=self.ID_PIN_LIST)

    def _selected_chat_from_list(self):
        """Mirrors ConversationsPanel._selected_chat_from_list()."""
        selected = self.conversations_list.GetFirstSelected()
        if selected < 0:
            selected = self.conversations_list.GetFocusedItem()
        if 0 <= selected < len(self.chats_list):
            return self.chats_list[selected]
        return None

    def _on_accel_delete(self, event):
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid:
                self._on_delete(jid)

    def _on_accel_copy_number(self, event):
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid and not jid.endswith("@g.us"):
                self._on_copy_number(jid)

    def _on_accel_conversation_data(self, event):
        chat = self._selected_chat_from_list()
        if chat:
            self.main_window.conversations_panel._show_conversation_data(chat=chat)

    def _on_accel_toggle_read(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if int(chat.get("unreadCount") or 0) > 0:
            self._on_mark_read(jid)
        else:
            self._on_mark_unread(jid)

    def _on_accel_mute(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if self.main_window.is_chat_muted(jid):
            self._on_unmute(jid)
        else:
            i18n = self.main_window.i18n
            menu = wx.Menu(i18n.t("mute_chat_menu_title"))
            for key, secs in ConversationsPanel.MUTE_PRESETS:
                item = menu.Append(wx.ID_ANY, i18n.t(key))
                self.Bind(wx.EVT_MENU, lambda e, j=jid, s=secs: self._on_mute(j, s), item)
            self.PopupMenu(menu)
            menu.Destroy()

    def _on_accel_block(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid or jid.endswith("@g.us") or self.main_window._is_self_jid(jid):
            return
        self._on_block(chat, jid, self.main_window.is_contact_blocked(jid))

    def _on_accel_clear(self, event):
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid:
                self._on_clear(jid)

    def _on_accel_unarchive(self, event):
        chat = self._selected_chat_from_list()
        if chat:
            jid = chat.get("remoteJid", "")
            if jid:
                self._on_unarchive(jid)

    def _on_accel_pin(self, event):
        chat = self._selected_chat_from_list()
        if not chat:
            return
        jid = chat.get("remoteJid", "")
        if not jid:
            return
        if self.main_window.is_chat_pinned(jid):
            self._on_unpin(jid)
        else:
            self._on_pin(jid)

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_filter_changed(self, event):
        """Update the active conversation filter and rebuild the list."""
        _filter_map = ['all', 'unread', 'groups', 'individual']
        sel = self._filter_radio.GetSelection()
        self._conv_filter = _filter_map[sel] if 0 <= sel < len(_filter_map) else 'all'
        self.main_window.add_chats_to_ui()
        # See ConversationsPanel._on_filter_changed's identical comment —
        # same reasoning applies to the archived list's own filter tabs, and
        # keyboard focus (SetFocus()) must stay off the list for the same
        # reason: it cuts NVDA off mid-announcement of the radio option.
        lst = self.conversations_list
        if self.chats_list:
            lst.Focus(0)
            lst.Select(0)
            lst.EnsureVisible(0)

    def _on_arch_list_key_down(self, event):
        if event.GetKeyCode() == wx.WXK_SPACE:
            idx = self.conversations_list.GetFocusedItem()
            if idx >= 0:
                self.conversations_list.Select(idx)
                class _E:
                    def GetIndex(self): return idx
                self.on_conversation_selected(_E())
        else:
            event.Skip()

    def on_conversation_selected(self, event):
        index = event.GetIndex()
        try:
            chat = self.chats_list[index]
        except IndexError:
            return
        mw = self.main_window
        # Switch to conversations panel and open the chat there
        mw.archived_conversations_panel.Hide()
        mw.conversations_panel.Show()
        # ConversationsPanel is a split view: its own chat list sits beside the
        # conversation detail pane, both shown together normally. Showing the
        # whole panel here would put that *regular* chat list back on screen
        # and in the Tab order, even though Esc still correctly returns to
        # this archived list — so hide it and show only the detail pane.
        mw.conversations_panel.conversations_label.Hide()
        mw.conversations_panel.conversations_list.Hide()
        mw.content_panel.Layout()
        mw.conversations_panel.navigate_to_conversation(chat)

    def on_context_menu(self, event):
        """Same menu, in the same order, as ConversationsPanel.on_conversations_context_menu() —
        this used to offer only Unarchive/Clear/Delete, missing everything
        else the normal list's row menu already had (data, read/unread, mute,
        block, copy number, pin, leave group, add member). Two differences
        from the normal menu: Archive/Unarchive always shows "Desarquivar"
        (every row here is archived by definition, nothing to toggle), and
        "Close conversation" is left out — there is no split conversation view
        to close from this list, unlike the normal one.
        """
        selected = self.conversations_list.GetFirstSelected()
        if selected < 0 or selected >= len(self.chats_list):
            return
        chat = self.chats_list[selected]
        jid  = chat.get("remoteJid", "")
        is_group = jid.endswith("@g.us")
        mw   = self.main_window
        is_self = mw._is_self_jid(jid)
        i18n = mw.i18n
        menu = wx.Menu()

        # ── Conversation / group data ─────────────────────────────────────
        data_label = i18n.t("group_data") if is_group else i18n.t("conversation_data")
        data_item = menu.Append(wx.ID_ANY, f"{data_label}\tCtrl+Shift+D")
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=chat: mw.conversations_panel._show_conversation_data(chat=c),
            data_item,
        )

        menu.AppendSeparator()

        # ── Read / Unread ───────────────────────────────────────────────────
        has_unread = int(chat.get("unreadCount") or 0) > 0
        if has_unread:
            read_item = menu.Append(wx.ID_ANY, f"{i18n.t('mark_as_read')}\tCtrl+Shift+M")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_mark_read(j), read_item)
        else:
            unread_item = menu.Append(wx.ID_ANY, f"{i18n.t('mark_as_unread')}\tCtrl+Shift+M")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_mark_unread(j), unread_item)

        menu.AppendSeparator()

        # ── Mute ──────────────────────────────────────────────────────────
        if mw.is_chat_muted(jid):
            unmute_item = menu.Append(wx.ID_ANY, f"{i18n.t('unmute_chat')}\tAlt+Shift+S")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_unmute(j), unmute_item)
        else:
            mute_sub = wx.Menu()
            for key, secs in ConversationsPanel.MUTE_PRESETS:
                item = mute_sub.Append(wx.ID_ANY, i18n.t(key))
                self.Bind(wx.EVT_MENU, lambda e, j=jid, s=secs: self._on_mute(j, s), item)
            menu.AppendSubMenu(mute_sub, f"{i18n.t('mute_chat')}\tAlt+Shift+S")

        if not is_group:
            menu.AppendSeparator()
            if not is_self:
                is_blocked = mw.is_contact_blocked(jid)
                label = "unblock_contact" if is_blocked else "block_contact"
                block_item = menu.Append(wx.ID_ANY, f"{i18n.t(label)}\tCtrl+Shift+B")
                self.Bind(
                    wx.EVT_MENU,
                    lambda e, c=chat, j=jid, b=is_blocked: self._on_block(c, j, b),
                    block_item,
                )
            copy_num_item = menu.Append(wx.ID_ANY, f"{i18n.t('copy_number')}\tAlt+Shift+C")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_copy_number(j), copy_num_item)

        menu.AppendSeparator()

        # ── Unarchive — always, every row here is already archived ─────────
        unarch_item = menu.Append(wx.ID_ANY, f"{i18n.t('unarchive_chat')}\tCtrl+Shift+Q")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_unarchive(j), unarch_item)

        # ── Pin / Unpin ───────────────────────────────────────────────────
        if mw.is_chat_pinned(jid):
            unpin_item = menu.Append(wx.ID_ANY, f"{i18n.t('unpin_chat')}\tCtrl+P")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_unpin(j), unpin_item)
        else:
            pin_item = menu.Append(wx.ID_ANY, f"{i18n.t('pin_chat')}\tCtrl+P")
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_pin(j), pin_item)

        menu.AppendSeparator()

        # ── Clear / Delete / Leave ────────────────────────────────────────
        # Clearing an archived conversation used to require opening it first
        # (there was no direct action here) — unlike the main conversations
        # list, whose row context menu can clear a chat in a single step.
        # Offering the same action directly on the archived row removes that
        # extra "open it, then clear it" round trip.
        clear_item = menu.Append(wx.ID_ANY, f"{i18n.t('clear_chat')}\tCtrl+Shift+L")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_clear(j), clear_item)

        del_item = menu.Append(wx.ID_ANY, f"{i18n.t('delete_chat')}\tDelete")
        self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_delete(j), del_item)

        if is_group:
            leave_item = menu.Append(wx.ID_ANY, i18n.t("leave_group"))
            self.Bind(wx.EVT_MENU, lambda e, j=jid: self._on_leave_group(j), leave_item)
            add_member_item = menu.Append(wx.ID_ANY, i18n.t("add_member"))
            self.Bind(
                wx.EVT_MENU,
                lambda e, j=jid: mw.conversations_panel._on_menu_add_member(j),
                add_member_item,
            )

        self.PopupMenu(menu)
        menu.Destroy()

    # ── Context menu / accelerator handlers ─────────────────────────────────
    # Mirrors ConversationsPanel's own _on_menu_* handlers. Reimplemented here
    # (rather than delegated to mw.conversations_panel's methods) specifically
    # because several of them show a wx.MessageBox parented on `self` — that
    # has to be THIS visible panel, not the hidden ConversationsPanel a plain
    # delegate call would bind confirmation dialogs to.

    def _on_mark_read(self, jid: str):
        threading.Thread(
            target=self.main_window.mark_conversation_as_read,
            args=(jid,),
            daemon=True,
        ).start()

    def _on_mark_unread(self, jid: str):
        self.main_window.mark_conversation_as_unread(jid)

    def _on_mute(self, jid: str, duration_secs: int):
        self.main_window.mute_chat(jid, duration_secs)

    def _on_unmute(self, jid: str):
        self.main_window.unmute_chat(jid)

    def _on_block(self, chat: dict, jid: str, currently_blocked: bool = False):
        mw = self.main_window
        name = (
            mw._resolve_contact_name(chat)
            or mw.find_name_through_messages(chat)
            or format_number(jid)
        )
        action = "unblock" if currently_blocked else "block"
        msg_key = "unblock_confirm_msg" if currently_blocked else "block_confirm_msg"
        title_key = "unblock_contact" if currently_blocked else "block_contact"
        msg = mw.i18n.t(msg_key).format(name=name)
        if wx.MessageBox(
            msg, mw.i18n.t(title_key), wx.YES_NO | wx.ICON_QUESTION, self,
        ) == wx.YES:
            threading.Thread(
                target=mw.block_contact, args=(jid, action), daemon=True,
            ).start()

    def _on_copy_number(self, jid: str):
        try:
            pyperclip.copy(format_number(jid))
        except Exception:
            pass

    def _on_pin(self, jid: str):
        self.main_window.pin_chat(jid)

    def _on_unpin(self, jid: str):
        self.main_window.unpin_chat(jid)

    def _on_leave_group(self, jid: str):
        i18n = self.main_window.i18n
        if wx.MessageBox(
            i18n.t("leave_group_confirm_msg"), i18n.t("leave_group"),
            wx.YES_NO | wx.ICON_QUESTION, self,
        ) != wx.YES:
            return
        threading.Thread(
            target=self.main_window.leave_group, args=(jid,), daemon=True,
        ).start()

    def _on_unarchive(self, jid: str):
        self.main_window.unarchive_chat(jid)

    def _on_clear(self, jid: str):
        i18n = self.main_window.i18n
        if wx.MessageBox(
            i18n.t("clear_confirm_msg"),
            i18n.t("clear_chat"),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) != wx.YES:
            return
        self.main_window.clear_chat(jid)
        # Refresh this list so the emptied preview disappears immediately —
        # mirrors ConversationsPanel._on_menu_clear_chat's own refresh call.
        self.main_window._schedule_set_chats()

    def _on_delete(self, jid: str):
        i18n = self.main_window.i18n
        # Mirrors ConversationsPanel._on_menu_delete_chat: delete_chat() (not
        # delete_chat_local()) is what actually sends the delete to the
        # WPPConnect API for a non-group chat. Using the local-only variant
        # here meant a deleted archived 1:1 conversation reappeared on the
        # next full sync, since the server was never told about it.
        confirm_key = "delete_group_confirm_msg" if jid.endswith("@g.us") else "delete_confirm_msg"
        if wx.MessageBox(
            i18n.t(confirm_key),
            i18n.t("delete_chat"),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        ) == wx.YES:
            self.main_window.delete_chat(jid)

    def refresh_labels(self):
        i18n = self.main_window.i18n
        self.conversations_label.SetLabel(i18n.t("archived_chats"))
        col = wx.ListItem()
        col.SetText(i18n.t("archived_chats"))
        self.conversations_list.SetColumn(0, col)
        if getattr(self, "_list_accessible", None) is not None:
            self._list_accessible._label = i18n.t("archived_chats")

        if hasattr(self, "_filter_radio"):
            self._filter_radio.SetLabel(i18n.t("conv_filter_label"))
            for _fi, _fk in enumerate([
                "conv_filter_all", "conv_filter_unread",
                "conv_filter_groups", "conv_filter_individual",
            ]):
                self._filter_radio.SetItemLabel(_fi, i18n.t(_fk))
