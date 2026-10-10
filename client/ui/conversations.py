from ui.shortcut_bindings import set_shortcut_label
import base64 as _b64
import copy
import logging
import mimetypes
import os
import re
import subprocess
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
    # see requirements.txt's / pyproject.toml's version marker. Voice recording degrades to a
    # clear "not available" message (see _start_voice_recording()) instead
    # of the whole app failing to import.
    pyaudio = None
import wave
import sound_lib.stream as sl_stream
from sound_lib.effects import Tempo
from core.audio_devices import (
    find_input_device_index, fallback_input_device_indices,
    recording_configs_for,
)
from core.voice_stereo import (
    alternate_mode_is_stereo, alternate_record_label_key, encode_as_stereo,
    fell_back_to_mono,
)
from core.quote_recovery import RECOVERED_FROM_QUOTE
from core.audio_transcode import transcode_audio_to_wav
from core.attachment_types import classify_attachment_media_type
from core.message_edit import (
    EDIT_UI_WINDOW_SECONDS,
    edit_kind,
    edit_window_open,
    edited_text_message,
    restore_edit_state,
    snapshot_edit_state,
)
from core.sound_system import load_sound
from core.link_preview import find_first_url, fetch_link_preview
from ui.accessible import (
    AccessibleSearchConversations,
    AccessibleRecordVoiceMessage,
    AccessibleAudioSlider,
    AccessibleSaveAs,
    AccessibleShowInFolder,
    AccessibleDescribeButton,
    AccessibleConversationDataButton,
    AccessibleVoiceCallButton,
    AccessibleVideoCallButton,
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
    AccessibleConversationFilter,
    AccessibleMessagesListControl,
    AccessibleReadMoreButton,
    AccessibleReturnCallButton,
    CompatListBoxMessagesCtrl,
)
from core.call_log import (
    CALL_LOG_MESSAGE_TYPE,
    LEGACY_CALL_LOG_TYPE,
    call_log_is_video,
    call_log_label,
    is_call_log,
    is_returnable_missed_call,
)
from ui.dialogs.emoji_picker import choose_and_insert_emoji, choose_reaction_emoji
from core.reaction_shortcuts import quick_reactions, remember_reaction
from ui.dialogs.clear_chat_confirm import confirm_clear_chat
from core.save_location import resolve_save_dialog_folder
from core.save_dialog_selection import schedule_deselect_extension
from core.utils import history_window, reaction_targets_status, format_number, decrypt_bytes, is_phone_like, encrypt, effective_unread_count, first_unread_index, db_fetch_limit, looks_like_binary_blob, normalize_for_search, normalize_line_separators, to_editor_line_endings, parse_bool_flag as _parse_bool_flag, append_selected_marker, is_message_forwarded, is_voice_message, video_seconds, MEASURED_SECONDS_KEY, link_preview_text
from core.locale_format import get_date_format, get_time_format, get_datetime_format
from core.message_copy_format import format_copied_message
from core.wrapped_text import original_range, selection_offsets, word_wrap
from core.video_player import VideoPlayer
from core.focus_cloak import cloak_panel_focus_fallback
from core.spell_checker import (
    WindowsSpellChecker, spell_check_active, windows_spellcheck_enabled,
)
from ui.media_viewer import MediaViewerDialog
from app_paths import data_path
from core.message_queue import PendingMessage
from datetime import datetime, timedelta

# ConversationsPanel is assembled from the mixins in ui/conversation_panel/
# (one module per responsibility — see ui/conversation_panel/__init__.py for
# the map). The helpers and ArchivedConversationsPanel are re-exported here
# because tests and callers reach them as ui.conversations.<name>; new code
# should import them from their module.
from ui.conversation_panel.archived_panel import (  # noqa: F401
    ArchivedConversationsPanel,
)
from ui.conversation_panel.media_paths import (  # noqa: F401
    _SAVEABLE_MESSAGE_TYPES,
    local_media_cache_paths,
    media_cache_id,
    cached_media_path,
    saved_media_path,
    reveal_file_in_folder,
    promote_local_media_cache,
    discard_local_media_cache,
    probe_media_duration,
)
from ui.conversation_panel.selection_rules import (  # noqa: F401
    toggle_jid_selection,
    visible_jid_selected,
)
from ui.conversation_panel.text_helpers import (  # noqa: F401
    _URL_RE,
    _fmt_last_seen,
    message_caption,
)
from ui.conversation_panel.transfer_gauge import (  # noqa: F401
    _FocusedTransferGaugeAccessible,
    _FocusedTransferGauge,
)
from ui.conversation_panel.accelerators import AcceleratorsMixin
from ui.conversation_panel.conversation_navigation import ConversationNavigationMixin
from ui.conversation_panel.chat_lists import WhatsAppListFilterMixin
from ui.conversation_panel.composer import ComposerMixin
from ui.conversation_panel.emoticon_conversion import EmoticonConversionMixin
from ui.conversation_panel.voice_recording import VoiceRecordingMixin
from ui.conversation_panel.system_audio_recording import SystemAudioRecordingMixin
from ui.conversation_panel.text_sending import TextSendingMixin
from ui.conversation_panel.list_refresh import ListRefreshMixin
from ui.conversation_panel.chat_menu import ChatMenuMixin
from ui.conversation_panel.message_list import MessageListMixin
from ui.conversation_panel.message_menu import MessageMenuMixin
from ui.conversation_panel.media_files import MediaFilesMixin
from ui.conversation_panel.links import LinksMixin
from ui.conversation_panel.mentions import MentionsMixin
from ui.conversation_panel.unread_separator import UnreadSeparatorMixin
from ui.conversation_panel.typing_row import TypingRowMixin, sync_typing_row
from ui.conversation_panel.history_loading import HistoryLoadingMixin
from ui.conversation_panel.chat_selection import ChatSelectionMixin
from ui.conversation_panel.message_rows import MessageRowsMixin
from ui.conversation_panel.audio_playback import AudioPlaybackMixin
from ui.conversation_panel.formatting import FormattingMixin
from ui.conversation_panel.message_rendering import MessageRenderingMixin
from ui.conversation_panel.conversation_info import ConversationInfoMixin
from ui.conversation_panel.contact_presence import ContactPresencePanelMixin
from ui.conversation_panel.forwarding import ForwardingMixin
from ui.conversation_panel.message_actions import MessageActionsMixin
from ui.conversation_panel.message_stars import StarActionsMixin
from ui.conversation_panel.message_accels import MessageAccelsMixin
from ui.conversation_panel.bookmarks import BookmarksMixin
from ui.conversation_panel.message_search import MessageSearchMixin
from ui.conversation_panel.community_comments import CommunityCommentsPanelMixin
from ui.conversation_panel.pinned_messages import PinnedMessagesMixin
from ui.conversation_panel.reactions import ReactionsMixin
from ui.conversation_panel.attachments import AttachmentsMixin
from ui.conversation_panel.contact_messages import ContactMessagesMixin
from ui.conversation_panel.bulk_messages import BulkMessagesMixin
from ui.conversation_panel.message_data import MessageDataMixin
from ui.conversation_panel.panel_visibility import ConversationPanelVisibilityMixin
from ui.conversation_panel.ai_actions import AIActionsMixin


class ConversationsPanel(
    AcceleratorsMixin,
    ConversationNavigationMixin,
    WhatsAppListFilterMixin,
    ConversationPanelVisibilityMixin,
    ComposerMixin,
    EmoticonConversionMixin,
    VoiceRecordingMixin,
    SystemAudioRecordingMixin,
    TextSendingMixin,
    ListRefreshMixin,
    ChatMenuMixin,
    MessageListMixin,
    MessageMenuMixin,
    MediaFilesMixin,
    LinksMixin,
    MentionsMixin,
    UnreadSeparatorMixin,
    TypingRowMixin,
    HistoryLoadingMixin,
    ChatSelectionMixin,
    MessageRowsMixin,
    AudioPlaybackMixin,
    FormattingMixin,
    MessageRenderingMixin,
    ConversationInfoMixin,
    ContactPresencePanelMixin,
    ForwardingMixin,
    MessageActionsMixin,
    StarActionsMixin,
    MessageAccelsMixin,
    BookmarksMixin,
    MessageSearchMixin,
    CommunityCommentsPanelMixin,
    PinnedMessagesMixin,
    ReactionsMixin,
    AttachmentsMixin,
    ContactMessagesMixin,
    BulkMessagesMixin,
    AIActionsMixin,
    MessageDataMixin,
    wx.Panel,
):
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
        # The optional Windows checker only observes completed words; it does
        # not alter keyboard handling or message sending. Its cue is a normal
        # Sound Event, so it follows the active soundpack, per-event enabled
        # state and custom path.
        self._spell_checker = WindowsSpellChecker(
            language=self.main_window.settings.get("general", {}).get("language"),
            on_error=self._play_spelling_error_sound,
        )
        # Ultima linha da lista de conversas em que o foco pousou, aberta ou
        # nao — ver _on_conversation_focused() e
        # _restore_conversation_selection().
        self._last_list_focus_jid = ""

        # The "X is typing..." last row of the messages list — only in the
        # control, never in _sorted_messages (see conversation_panel/typing_row.py).
        self._typing_row_list = None
        self._typing_row_text = ""
        self._typing_row_chat = None
        self._typing_row_dismissed = set()

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
        # Whether THIS recording was asked for in stereo (issue #82): the
        # Settings default, or the other mode through the second button.
        self._recording_stereo: bool = False
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
        self._system_audio_session = None
        self._recording_system_audio = False
        self._system_audio_interrupted = False

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
        # msg_id -> user-visible copy created by Save As. Kept in memory so a
        # list rebuild can reconnect a fresh message dictionary to that copy;
        # never persisted with the WhatsApp profile or included in exports.
        self._saved_media_paths: dict[str, str] = {}

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
        # Bumped by _backfill_reactions_for_open_conversation() on every
        # conversation open; a background fetch started for an earlier
        # generation checks this before applying its results, so switching
        # away mid-fetch cannot write a stale conversation's reactions into
        # whatever is open by the time it finishes.
        self._reaction_backfill_generation: int = 0
        # canonical jid → when it was last walked, for the cooldown that
        # keeps reopening a chat from re-costing the whole request batch.
        self._reaction_backfill_last: dict = {}
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
        # Stage names already seen per upload, so a re-reported stage is
        # not mistaken for forward motion. See
        # update_media_upload_progress().
        self._upload_stages_seen: dict = {}
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

        # TE_PROCESS_ENTER is required for a TextCtrl to deliver a reliable
        # text-enter event on Windows.  EVT_KEY_DOWN alone is not enough:
        # without this style, Enter may be consumed by the control's default
        # processing before the locked-chats reveal path sees it.
        self.search_field = wx.TextCtrl(
            self, style=wx.TE_DONTWRAP | wx.TE_PROCESS_ENTER
        )
        self.search_field.Bind(wx.EVT_TEXT, self.on_search_query_changed)
        self.search_field.Bind(wx.EVT_TEXT_ENTER, self._on_search_field_enter)
        self.search_field.Bind(wx.EVT_KEY_DOWN, self._on_search_field_key_down)
        self.search_field.SetAccessible(AccessibleSearchConversations("Ctrl+F"))
        outer_sizer.Add(self.search_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)

        # ── Nova conversa button ────────────────────────────────────────────
        self._new_conv_btn = wx.Button(self, label=i18n.t("new_conversation"))
        self._new_conv_btn.SetAccessible(AccessibleNewConversationButton())
        self._new_conv_btn.Bind(wx.EVT_BUTTON, self._on_new_conversation)
        outer_sizer.Add(self._new_conv_btn, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 5)

        self._build_wa_list_controls(outer_sizer)

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
        self._filter_radio.SetAccessible(AccessibleConversationFilter())
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

        self._video_call_btn = wx.Button(
            self.conversation_panel, label=i18n.t("video_call_button")
        )
        self._video_call_btn.SetAccessible(AccessibleVideoCallButton())
        self._video_call_btn.Bind(wx.EVT_BUTTON, self._on_video_call)
        conv_sizer.Add(self._video_call_btn, 0, wx.LEFT | wx.TOP, 5)
        self._video_call_btn.Hide()

        self._voice_call_btn = wx.Button(
            self.conversation_panel, label=i18n.t("voice_call_button")
        )
        self._voice_call_btn.SetAccessible(AccessibleVoiceCallButton())
        self._voice_call_btn.Bind(wx.EVT_BUTTON, self._on_voice_call)
        conv_sizer.Add(self._voice_call_btn, 0, wx.LEFT | wx.TOP, 5)
        self._voice_call_btn.Hide()

        self._init_pinned_messages_button(conv_sizer)

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

        # ── "Retornar ligação" (focused missed call only) ───────────────────
        # Created first after the list so it is the first Tab stop from a
        # focused missed call; _update_return_call_button() shows it only
        # while such a row is focused. Ctrl+Shift+R reaches the same action.
        self._return_call_btn = wx.Button(
            self.conversation_panel, label=i18n.t("return_call_button")
        )
        self._return_call_btn.SetAccessible(AccessibleReturnCallButton())
        self._return_call_btn.Bind(wx.EVT_BUTTON, self._on_return_call)
        conv_sizer.Add(self._return_call_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._return_call_btn.Hide()
        self._return_call_msg = None

        # ── "Ler mais" button (classic ListCtrl only) ─────────────────────────
        # SysListView32 truncates each row's accessible text to ~512 characters,
        # so a screen reader can't read the tail of a long text message just by
        # focusing it. This button is the first focusable control after the
        # list whenever it shows (only "Retornar ligação" precedes it, and that
        # one never shows for a text row) and is only shown when the focused
        # row is a truncated text message.
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
        # Its counterpart for a message with exactly one link: the
        # HyperlinkCtrl that link gets instead of a list. Held for the same
        # reason — _link_url_for() has to be able to recognise both shapes.
        self._link_ctrl = None
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

        # Describe / transcribe (Ctrl+Shift+I) sits right after Save as in the
        # Tab order; ai_actions.py decides when it is shown and what it says.
        self._action_describe_btn = wx.Button(
            self._media_action_slot, label=i18n.t("ai_describe_image_menu")
        )
        self._action_describe_btn.SetAccessible(AccessibleDescribeButton())
        self._action_describe_btn.Bind(wx.EVT_BUTTON, self._on_ai_describe_button)
        self._media_action_sizer.Add(self._action_describe_btn, 0, wx.TOP, 2)
        self._action_describe_btn.Hide()

        self._action_show_in_folder_btn = wx.Button(
            self._media_action_slot, label=i18n.t("show_in_folder")
        )
        self._action_show_in_folder_btn.SetAccessible(AccessibleShowInFolder())
        self._action_show_in_folder_btn.Bind(
            wx.EVT_BUTTON, self._on_action_show_in_folder
        )
        self._media_action_sizer.Add(
            self._action_show_in_folder_btn, 0, wx.TOP, 2
        )
        self._action_show_in_folder_btn.Hide()

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
        self.message_field.Bind(wx.EVT_LEFT_UP,    self._cue_spelling_at_caret_on_click)
        self.message_field.Bind(wx.EVT_CONTEXT_MENU, self._on_message_field_context_menu)
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

        # The other recording mode, for one message (issue #82): "em estéreo"
        # when Settings records in mono, "em mono" when it records in stereo.
        # Follows the first button everywhere it is shown, hidden or disabled.
        self._record_voice_alt_btn = wx.Button(
            self.conversation_panel, label=i18n.t(self._alternate_record_label_key())
        )
        self._record_voice_alt_btn.SetAccessible(
            AccessibleRecordVoiceMessage("Ctrl+Shift+G")
        )
        self._record_voice_alt_btn.Bind(wx.EVT_BUTTON, self._on_record_alternate_mode)
        conv_sizer.Add(self._record_voice_alt_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._record_voice_system_btn = wx.Button(
            self.conversation_panel, label=i18n.t("record_voice_message_system_audio")
        )
        self._record_voice_system_btn.SetAccessible(
            AccessibleRecordVoiceMessage("Ctrl+Shift+H")
        )
        self._record_voice_system_btn.Bind(wx.EVT_BUTTON, self._on_record_system_audio)
        conv_sizer.Add(self._record_voice_system_btn, 0, wx.LEFT | wx.BOTTOM, 5)

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
        self._discard_voice_btn.SetAccessible(
            AccessibleDiscardVoiceMessage(self.main_window, self._discard_voice_btn)
        )
        self._discard_voice_btn.Bind(wx.EVT_BUTTON, self._discard_voice_message)
        voice_sizer.Add(self._discard_voice_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._pause_resume_btn = wx.Button(
            self._voice_panel, label=i18n.t("pause_recording")
        )
        self._pause_resume_btn.SetAccessible(
            AccessiblePauseResumeRecording(self.main_window, self._pause_resume_btn)
        )
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
        self._send_voice_btn.SetAccessible(
            AccessibleSendVoiceMessage(self.main_window, self._send_voice_btn)
        )
        self._send_voice_btn.Bind(wx.EVT_BUTTON, self._send_voice_message)
        voice_sizer.Add(self._send_voice_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._create_system_audio_volume_controls(voice_sizer)
        self._create_nvda_volume_controls(voice_sizer)

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
            # The typing row lived in the old control (which is cleared the
            # next time it is switched to); add it back to this one.
            self._typing_row_list = None
            sync_typing_row(self)
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

    def refresh_labels(self):
        """Update all translatable labels and column headers after a language change."""
        self._refresh_wa_list_labels()
        i18n = self.main_window.i18n
        self._spell_checker.set_language(
            self.main_window.settings.get("general", {}).get("language")
        )

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
        self._update_pinned_messages_button()
        self._voice_call_btn.SetLabel(i18n.t("voice_call_button"))
        self._search_close_btn.SetLabel(i18n.t("search_close"))
        self._search_field_label.SetLabel(i18n.t("search_in_conv"))
        self._search_prev_btn.SetLabel(i18n.t("search_prev_result"))
        self._search_next_btn.SetLabel(i18n.t("search_next_result"))

        set_shortcut_label(self, self.messages_label, 'navigation.messages', i18n.t("messages"))
        col2 = wx.ListItem()
        col2.SetText(i18n.t("messages").replace("&", ""))
        for control in getattr(self, "_message_list_controls", {"active": self.messages_list}).values():
            control.SetColumn(0, col2)
        for accessible in getattr(self, "_messages_list_accessibles", {}).values():
            accessible._label = i18n.t("messages").replace("&", "")

        self.audio_progress_label.SetLabel(i18n.t("audio_progress_label"))
        self._action_save_as_btn.SetLabel(i18n.t("save_as"))
        self._action_show_in_folder_btn.SetLabel(i18n.t("show_in_folder"))
        self._action_download_btn.SetLabel(i18n.t("download"))

        if self.conversation is not None and self.conversation_panel.IsShown():
            if self.conversation_name:
                set_shortcut_label(self, self.message_label, 'messages.ID_ALT_FOCUS_FIELD',
                    f"{i18n.t('type_message')} {self.conversation_name}"
                )
            else:
                set_shortcut_label(self, self.message_label, 'messages.ID_ALT_FOCUS_FIELD', i18n.t("type_message"))
        else:
            set_shortcut_label(self, self.message_label, 'messages.ID_ALT_FOCUS_FIELD', i18n.t("type_message"))

        self.send_message_btn.SetLabel(i18n.t("send_message"))
        self._emoji_btn.SetLabel(i18n.t("emoji_button"))
        self._cancel_edit_btn.SetLabel(i18n.t("cancel_edit"))
        if hasattr(self, "_remove_quote_btn"):
            self._remove_quote_btn.SetLabel(i18n.t("remove_quote"))
        self.record_voice_message_btn.SetLabel(i18n.t("record_voice_message"))
        self.refresh_alternate_record_button()
        if hasattr(self, "_record_voice_system_btn"):
            self._record_voice_system_btn.SetLabel(i18n.t("record_voice_message_system_audio"))
        self._add_attachment_btn.SetLabel(i18n.t("add_attachment"))
        self._add_more_btn.SetLabel(i18n.t("add_more_files"))
        self._caption_label.SetLabel(i18n.t("attachment_caption_hint"))
        self._send_attachment_btn.SetLabel(i18n.t("send_attachment"))
        self._contact_converse_btn.SetLabel(i18n.t("converse"))
        self._contact_save_btn.SetLabel(i18n.t("save_contact"))
        self._return_call_btn.SetLabel(i18n.t("return_call_button"))
        self._discard_voice_btn.SetLabel(i18n.t("discard_voice_message"))
        self._send_voice_btn.SetLabel(i18n.t("send_voice_message"))
        self._relabel_system_audio_volume_controls()
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


# ── Archived Conversations Panel ─────────────────────────────────────────────


