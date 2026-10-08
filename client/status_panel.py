from ui.shortcut_bindings import make_shortcut_table
import base64
import logging
import mimetypes
import os
import tempfile
import threading
import time
import wave
import wx
import requests
import sound_lib.stream as sl_stream
from ui.accessible import (
    AccessibleStatusPrev, AccessibleStatusNext, AccessibleStatusCopyText, AccessibleSaveAs,
    AccessibleRecordVoiceMessage, AccessibleDiscardVoiceMessage, AccessiblePauseResumeRecording,
    AccessibleSendVoiceMessage, AccessiblePlayRecordedAudio,
)
from core.api_client import api_get, api_post, redact_api_url
from core.save_location import resolve_save_dialog_folder
from core.save_dialog_selection import schedule_deselect_extension
from core.utils import format_number, normalize_line_separators, is_voice_message
from core.video_player import VideoPlayer
from core.focus_cloak import cloak_panel_focus_fallback
from core.audio_devices import (
    find_input_device_index, fallback_input_device_indices, RECORDING_SAMPLE_CONFIGS,
)
from ui.dialogs.emoji_picker import choose_and_insert_emoji
from ui.media_viewer import MediaViewerDialog

# StatusPanel is assembled from the mixins in status_tab/ (one module per
# responsibility — see status_tab/__init__.py for the map). The helpers and
# the two status dialogs are re-exported here because tests and callers
# reach them as status_panel.<name>; new code should import them from their
# module.
from status_tab.status_dialogs import (  # noqa: F401
    StatusReactionsDialog,
    MyStatusDialog,
)
from status_tab.status_rules import (  # noqa: F401
    _post_was_rejected,
    _download_status_media,
    _status_content_label,
    _STATUS_MIME_SUBTYPE_TO_EXT,
    _status_media_extension,
    _status_media_save_info,
)
from status_tab.status_loading import StatusLoadingMixin
from status_tab.status_list import StatusListMixin
from status_tab.status_viewer import StatusViewerMixin
from status_tab.status_interactions import StatusInteractionsMixin
from status_tab.status_media import StatusMediaMixin
from status_tab.status_composer import StatusComposerMixin
from status_tab.status_voice import StatusVoiceMixin

try:
    import pyaudio
except ImportError:
    pyaudio = None


# ── Status reactions dialog ──────────────────────────────────────────────────


# ── My Status dialog ─────────────────────────────────────────────────────────


# ── Main status panel ────────────────────────────────────────────────────────

class StatusPanel(
    StatusLoadingMixin,
    StatusListMixin,
    StatusViewerMixin,
    StatusInteractionsMixin,
    StatusMediaMixin,
    StatusComposerMixin,
    StatusVoiceMixin,
    wx.Panel,
):
    def __init__(self, main_window, parent):
        super().__init__(parent)
        self.main_window = main_window
        self.parent = parent

        # List of status contacts (other people): [{"name", "jid", "statuses": [...]}]
        self._status_contacts = []
        # Own posted statuses: [status_dict, ...]
        self._my_statuses = []
        # Whether the list is currently showing the loading placeholder
        self._list_is_loading = False
        # Index of selected contact in _status_contacts (-1 = none / My Status selected)
        self._selected_contact_idx = -1
        # Index of current status within the selected contact's statuses
        self._current_status_idx = 0
        # The actual status dict/contact entry/copy-text currently shown in
        # the viewer — set by _show_current_status(), read by the action
        # buttons (copy text, save media, open video, reply).
        self._current_status       = None
        self._current_status_entry = None
        self._current_status_text  = ""

        # Liked status tracking: status_id → bool
        self._liked_statuses: dict = {}

        # Local path of the currently downloaded video status, if any (kept
        # so re-pressing Play/Pause doesn't re-download the same file).
        self._video_local_path = None
        self._video_download_status_id = None

        # Maps a _status_list row index to its index into _status_contacts,
        # or -1 for a row that isn't a selectable contact at all (row 0,
        # "My Status", and the "Recentes"/"Vistos" section-header rows —
        # see _populate_list()). Every place that used to compute this via
        # a hardcoded `idx - 1` must go through this map instead now that
        # the header rows shift the offset.
        self._status_row_contact: dict = {}
        # Reverse of the above: contact index -> its _status_list row.
        self._status_contact_row: dict = {}

        self.init_UI()
        self._create_accelerators()

        self._video_player = VideoPlayer(
            main_window, self._video_bitmap, on_frame_size=self._on_video_frame_size_known
        )
        # Stop playback (audio + frame decoding) whenever this panel is
        # hidden — Alt+1/Alt+4 switching away from the Status tab, or the
        # window closing — regardless of which of the several call sites in
        # main.py does the hiding. Without this a video kept playing (audio
        # audible, ffmpeg still decoding) in the background indefinitely.
        self.Bind(wx.EVT_SHOW, self._on_panel_show)

    # ── UI ───────────────────────────────────────────────────────────────────

    def init_UI(self):
        i18n  = self.main_window.i18n
        sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Header buttons ────────────────────────────────────────────────
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._add_status_btn = wx.Button(self, label=i18n.t("status_add"))
        self._add_status_btn.Bind(wx.EVT_BUTTON, self._on_add_status)
        header_sizer.Add(self._add_status_btn, 0, wx.RIGHT, 5)

        self._refresh_status_btn = wx.Button(self, label=i18n.t("status_refresh"))
        self._refresh_status_btn.Bind(wx.EVT_BUTTON, self._on_refresh_status_btn)
        header_sizer.Add(self._refresh_status_btn, 0, wx.RIGHT, 5)

        sizer.Add(header_sizer, 0, wx.LEFT | wx.TOP | wx.BOTTOM, 5)

        # ── Status contacts list ──────────────────────────────────────────
        self._list_label = wx.StaticText(self, label=i18n.t("status"))
        sizer.Add(self._list_label, 0, wx.LEFT | wx.TOP, 5)

        self._status_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self._status_list.InsertColumn(0, i18n.t("status"), width=360)
        self._status_list.Bind(wx.EVT_LIST_ITEM_SELECTED,  self._on_status_contact_selected)
        self._status_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_status_contact_activated)
        self._status_list.Bind(wx.EVT_KEY_DOWN, self._on_status_list_key_down)
        sizer.Add(self._status_list, 1, wx.EXPAND | wx.ALL, 5)

        # ── Status viewer panel (hidden until a contact is selected) ──────
        self._viewer_panel = wx.Panel(self)
        viewer_sizer = wx.BoxSizer(wx.VERTICAL)

        self._status_content_label = wx.StaticText(self._viewer_panel, label="")
        viewer_sizer.Add(self._status_content_label, 0, wx.ALL, 5)

        nav_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self._prev_status_btn = wx.Button(self._viewer_panel, label=i18n.t("status_prev"))
        self._prev_status_btn.SetAccessible(AccessibleStatusPrev(i18n.t("accessible_ctrl_left")))
        self._prev_status_btn.Bind(wx.EVT_BUTTON, self._on_prev_status)
        nav_sizer.Add(self._prev_status_btn, 0, wx.RIGHT, 5)

        self._next_status_btn = wx.Button(self._viewer_panel, label=i18n.t("status_next"))
        self._next_status_btn.SetAccessible(AccessibleStatusNext(i18n.t("accessible_ctrl_right")))
        self._next_status_btn.Bind(wx.EVT_BUTTON, self._on_next_status)
        nav_sizer.Add(self._next_status_btn, 0, wx.RIGHT, 5)

        viewer_sizer.Add(nav_sizer, 0, wx.LEFT | wx.BOTTOM, 5)

        # Video statuses: audio plays through BASS (as everywhere else in
        # WinZapp); the picture is decoded by the bundled ffmpeg binary as a
        # capped-rate JPEG frame sequence and drawn into this bitmap — see
        # core/video_player.py's module docstring for why (BASS alone can't
        # decode WhatsApp's AAC track or render video at all).
        self._video_bitmap = wx.StaticBitmap(self._viewer_panel, size=(320, 240))
        viewer_sizer.Add(self._video_bitmap, 0, wx.LEFT | wx.BOTTOM, 5)
        self._video_bitmap.Hide()

        self._play_pause_btn = wx.Button(self._viewer_panel, label=i18n.t("status_play_pause"))
        self._play_pause_btn.Bind(wx.EVT_BUTTON, self._on_play_pause_video)
        viewer_sizer.Add(self._play_pause_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._play_pause_btn.Hide()

        self._like_btn = wx.Button(self._viewer_panel, label=i18n.t("status_like"))
        self._like_btn.Bind(wx.EVT_BUTTON, self._on_like_status)
        viewer_sizer.Add(self._like_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._like_btn.Hide()

        self._copy_text_btn = wx.Button(self._viewer_panel, label=i18n.t("status_copy_text"))
        self._copy_text_btn.SetAccessible(AccessibleStatusCopyText())
        self._copy_text_btn.Bind(wx.EVT_BUTTON, self._on_copy_status_text)
        viewer_sizer.Add(self._copy_text_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._copy_text_btn.Hide()

        self._save_media_btn = wx.Button(self._viewer_panel, label=i18n.t("status_save_media"))
        self._save_media_btn.SetAccessible(AccessibleSaveAs())
        self._save_media_btn.Bind(wx.EVT_BUTTON, self._on_save_status_media)
        viewer_sizer.Add(self._save_media_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._save_media_btn.Hide()

        # ── Reply to the currently viewed status ────────────────────────────
        self._reply_label = wx.StaticText(self._viewer_panel, label=i18n.t("status_reply_label"))
        viewer_sizer.Add(self._reply_label, 0, wx.LEFT | wx.TOP, 5)
        self._reply_field = wx.TextCtrl(self._viewer_panel, style=wx.TE_PROCESS_ENTER)
        self._reply_field.Bind(wx.EVT_TEXT_ENTER, self._on_send_status_reply)
        from ui.shortcut_bindings import bind_enter_command
        bind_enter_command(self, self._reply_field, 'status_reply.send', self._on_send_status_reply)
        self._reply_field.Bind(wx.EVT_TEXT, self._on_reply_field_text_changed)
        viewer_sizer.Add(self._reply_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)
        self._reply_send_btn = wx.Button(self._viewer_panel, label=i18n.t("status_reply_send"))
        self._reply_send_btn.Bind(wx.EVT_BUTTON, self._on_send_status_reply)
        viewer_sizer.Add(self._reply_send_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._reply_label.Hide()
        self._reply_field.Hide()
        self._reply_send_btn.Hide()

        self._viewer_panel.SetSizer(viewer_sizer)
        self._viewer_panel.Hide()
        sizer.Add(self._viewer_panel, 0, wx.EXPAND | wx.ALL, 5)

        # ── Post text status panel (hidden) ───────────────────────────────
        self._post_panel = wx.Panel(self)
        post_sizer = wx.BoxSizer(wx.VERTICAL)

        self._post_close_btn = wx.Button(self._post_panel, label=i18n.t("close"))
        self._post_close_btn.Bind(wx.EVT_BUTTON, self._on_close_post_panel)
        post_sizer.Add(self._post_close_btn, 0, wx.ALL, 5)

        self._post_text_label = wx.StaticText(self._post_panel, label=i18n.t("status_text_label"))
        post_sizer.Add(self._post_text_label, 0, wx.LEFT | wx.TOP, 5)

        self._post_text_field = wx.TextCtrl(
            self._post_panel,
            style=wx.TE_MULTILINE | wx.TE_DONTWRAP,
        )
        post_sizer.Add(self._post_text_field, 0, wx.EXPAND | wx.ALL, 5)

        self._post_emoji_btn = wx.Button(
            self._post_panel, label=i18n.t("emoji_button")
        )
        self._post_emoji_btn.Bind(wx.EVT_BUTTON, self._on_open_post_emoji_picker)
        post_sizer.Add(self._post_emoji_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._caption_label = wx.StaticText(self._post_panel, label=i18n.t("status_caption_hint"))
        post_sizer.Add(self._caption_label, 0, wx.LEFT, 5)

        self._caption_field = wx.TextCtrl(self._post_panel, style=wx.TE_DONTWRAP)
        self._caption_field.SetHint(i18n.t("status_caption_hint"))
        post_sizer.Add(self._caption_field, 0, wx.EXPAND | wx.ALL, 5)

        self._post_send_btn = wx.Button(self._post_panel, label=i18n.t("status_send"))
        self._post_send_btn.Bind(wx.EVT_BUTTON, self._on_send_text_status)
        post_sizer.Add(self._post_send_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._post_panel.SetSizer(post_sizer)
        self._post_panel.Hide()
        self._post_panel.Disable()
        sizer.Add(self._post_panel, 0, wx.EXPAND | wx.ALL, 5)

        # ── Post media status panel (hidden) ──────────────────────────────
        self._media_post_panel = wx.Panel(self)
        media_sizer = wx.BoxSizer(wx.VERTICAL)

        self._media_close_btn = wx.Button(self._media_post_panel, label=i18n.t("close"))
        self._media_close_btn.Bind(wx.EVT_BUTTON, self._on_close_media_panel)
        media_sizer.Add(self._media_close_btn, 0, wx.ALL, 5)

        # Dynamic list of "Remover anexo <filename>" buttons, rebuilt on every change
        self._media_attachments_list_panel = wx.Panel(self._media_post_panel)
        self._media_attachments_list_sizer = wx.BoxSizer(wx.VERTICAL)
        self._media_attachments_list_panel.SetSizer(self._media_attachments_list_sizer)
        media_sizer.Add(self._media_attachments_list_panel, 0, wx.EXPAND | wx.LEFT | wx.TOP, 5)

        self._media_add_more_btn = wx.Button(self._media_post_panel, label=i18n.t("add_more_files"))
        self._media_add_more_btn.Bind(wx.EVT_BUTTON, self._on_add_more_media_files)
        media_sizer.Add(self._media_add_more_btn, 0, wx.LEFT | wx.TOP | wx.BOTTOM, 5)

        self._media_caption_label = wx.StaticText(self._media_post_panel, label=i18n.t("status_caption_hint"))
        media_sizer.Add(self._media_caption_label, 0, wx.LEFT, 5)

        self._media_caption_field = wx.TextCtrl(self._media_post_panel, style=wx.TE_DONTWRAP)
        self._media_caption_field.SetHint(i18n.t("status_caption_hint"))
        media_sizer.Add(self._media_caption_field, 0, wx.EXPAND | wx.ALL, 5)

        self._media_send_btn = wx.Button(self._media_post_panel, label=i18n.t("status_send"))
        self._media_send_btn.Bind(wx.EVT_BUTTON, self._on_send_media_status)
        media_sizer.Add(self._media_send_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._media_post_panel.SetSizer(media_sizer)
        self._media_post_panel.Hide()
        self._media_post_panel.Disable()
        sizer.Add(self._media_post_panel, 0, wx.EXPAND | wx.ALL, 5)

        self._selected_media_paths: list = []

        # ── Post voice status panel (hidden until user clicks Add -> Voice) ────────
        self._voice_post_panel = wx.Panel(self)
        voice_sizer = wx.BoxSizer(wx.VERTICAL)

        self._voice_status_lbl = wx.StaticText(self._voice_post_panel, label=i18n.t("recording_in_progress"))
        voice_sizer.Add(self._voice_status_lbl, 0, wx.ALL, 5)

        # Match ConversationsPanel's recorder: one vertical action stack,
        # with every recording shortcut exposed only inside the Audio flow.
        # The old horizontal strip was both unlike the working conversation
        # recorder and easy to spill across/narrow the Status UI.
        voice_btn_sizer = wx.BoxSizer(wx.VERTICAL)

        self._voice_close_btn = wx.Button(self._voice_post_panel, label=i18n.t("discard_voice_message"))
        self._voice_close_btn.SetAccessible(
            AccessibleDiscardVoiceMessage(self.main_window, self._voice_close_btn)
        )
        self._voice_close_btn.Bind(wx.EVT_BUTTON, self._on_close_voice_panel)
        self._voice_close_btn.Hide()
        voice_btn_sizer.Add(self._voice_close_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._voice_start_btn = wx.Button(self._voice_post_panel, label=i18n.t("record_voice_message"))
        self._voice_start_btn.SetAccessible(AccessibleRecordVoiceMessage("Ctrl+R"))
        self._voice_start_btn.Bind(wx.EVT_BUTTON, self._on_record_voice_button)
        voice_btn_sizer.Add(self._voice_start_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._voice_pause_btn = wx.Button(self._voice_post_panel, label=i18n.t("pause_recording"))
        self._voice_pause_btn.SetAccessible(
            AccessiblePauseResumeRecording(self.main_window, self._voice_pause_btn)
        )
        self._voice_pause_btn.Bind(wx.EVT_BUTTON, self._toggle_pause_voice_recording)
        self._voice_pause_btn.Hide()
        voice_btn_sizer.Add(self._voice_pause_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._voice_play_btn = wx.Button(
            self._voice_post_panel, label=i18n.t("play_recorded_audio")
        )
        self._voice_play_btn.SetAccessible(AccessiblePlayRecordedAudio())
        self._voice_play_btn.Bind(wx.EVT_BUTTON, self._toggle_play_recorded_audio)
        self._voice_play_btn.Hide()
        voice_btn_sizer.Add(self._voice_play_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._recorded_audio_timer = wx.Timer(self._voice_play_btn)
        self._voice_play_btn.Bind(
            wx.EVT_TIMER, self._on_recorded_audio_timer, self._recorded_audio_timer
        )

        self._voice_send_btn = wx.Button(self._voice_post_panel, label=i18n.t("send_voice_message"))
        self._voice_send_btn.SetAccessible(
            AccessibleSendVoiceMessage(self.main_window, self._voice_send_btn)
        )
        self._voice_send_btn.Bind(wx.EVT_BUTTON, self._on_send_voice_status)
        self._voice_send_btn.Hide()
        voice_btn_sizer.Add(self._voice_send_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        voice_sizer.Add(voice_btn_sizer, 0, wx.ALL, 5)

        self._voice_post_panel.SetSizer(voice_sizer)
        self._voice_post_panel.Hide()
        self._voice_post_panel.Disable()
        sizer.Add(self._voice_post_panel, 0, wx.EXPAND | wx.ALL, 5)

        # Recording state — same shape as ConversationsPanel's own voice-
        # message recording (client/ui/conversations.py's
        # _start_voice_recording()/_recording_pa etc.), scoped to
        # posting a status instead of sending a chat message.
        self._recording_pa      = None
        self._recording_stream  = None
        self._recording_frames: list = []
        self._recording_rate    = 48000
        self._recording_channels = 1
        self._recording_paused  = False
        self._is_recording      = False
        self._recorded_audio_sound = None
        self._recorded_audio_temp_path = None
        # True while a background thread is opening the PyAudio input stream.
        # pa.open() (and find_input_device_index()'s device enumeration) can
        # block for seconds negotiating with the driver, and this used to run
        # straight on the wx thread — freezing the window, and the screen
        # reader with it, for as long as the driver took. Mirrors
        # ConversationsPanel's own recording open (client/ui/conversations.py):
        # _recording_starting guards against re-entry, _recording_open_token
        # lets a discard/close that happens mid-open throw the stream away
        # once it finally arrives.
        self._recording_starting   = False
        self._recording_open_token = 0

        self.SetSizer(sizer)

    def _create_accelerators(self):
        self.ID_CTRL_LEFT     = wx.NewIdRef()
        self.ID_CTRL_RIGHT    = wx.NewIdRef()
        self.ID_ESCAPE        = wx.NewIdRef()
        self.ID_CTRL_C        = wx.NewIdRef()
        self.ID_CTRL_SHIFT_S  = wx.NewIdRef()
        self.ID_CTRL_R        = wx.NewIdRef()
        self.ID_CTRL_P        = wx.NewIdRef()
        self.ID_CTRL_SHIFT_P  = wx.NewIdRef()
        self.ID_CTRL_SHIFT_D  = wx.NewIdRef()
        self.ID_F5            = wx.NewIdRef()
        self.ID_CTRL_PERIOD   = wx.NewIdRef()
        accel_tbl = make_shortcut_table(self, 'status', [
            (wx.ACCEL_CTRL,                    wx.WXK_LEFT,   self.ID_CTRL_LEFT),
            (wx.ACCEL_CTRL,                    wx.WXK_RIGHT,  self.ID_CTRL_RIGHT),
            (wx.ACCEL_NORMAL,                  wx.WXK_ESCAPE, self.ID_ESCAPE),
            (wx.ACCEL_NORMAL,                  wx.WXK_F5,     self.ID_F5),
            (wx.ACCEL_CTRL,                    ord("C"),      self.ID_CTRL_C),
            (wx.ACCEL_CTRL,                    ord("R"),      self.ID_CTRL_R),
            (wx.ACCEL_CTRL,                    ord("P"),      self.ID_CTRL_P),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT,   ord("P"),      self.ID_CTRL_SHIFT_P),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT,   ord("D"),      self.ID_CTRL_SHIFT_D),
            (wx.ACCEL_CTRL,                    ord("."),      self.ID_CTRL_PERIOD),
            # Same combo ConversationsPanel already uses for "save as"
            # (client/ui/conversations.py's ID_CTRL_SHIFT_S) — consistent
            # muscle memory across both places media can be saved from.
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT,   ord("S"),      self.ID_CTRL_SHIFT_S),
        ])
        self.SetAcceleratorTable(accel_tbl)
        self.Bind(wx.EVT_MENU, self._on_prev_status,          id=self.ID_CTRL_LEFT)
        self.Bind(wx.EVT_MENU, self._on_next_status,          id=self.ID_CTRL_RIGHT)
        self.Bind(wx.EVT_MENU, self._on_escape,               id=self.ID_ESCAPE)
        self.Bind(wx.EVT_MENU, self._on_copy_status_text,     id=self.ID_CTRL_C)
        self.Bind(wx.EVT_MENU, self._on_save_status_media,    id=self.ID_CTRL_SHIFT_S)
        self.Bind(wx.EVT_MENU, self._on_ctrl_r_shortcut,      id=self.ID_CTRL_R)
        self.Bind(wx.EVT_MENU, self._on_ctrl_p_shortcut,      id=self.ID_CTRL_P)
        self.Bind(wx.EVT_MENU, self._on_ctrl_shift_p_shortcut,id=self.ID_CTRL_SHIFT_P)
        self.Bind(wx.EVT_MENU, self._on_ctrl_shift_d_shortcut,id=self.ID_CTRL_SHIFT_D)
        self.Bind(wx.EVT_MENU, self._on_refresh_status_btn,   id=self.ID_F5)
        self.Bind(wx.EVT_MENU, self._on_open_post_emoji_picker, id=self.ID_CTRL_PERIOD)

    def _on_refresh_status_btn(self, event):
        """Manually reload statuses from WPPConnect API."""
        self.on_show()

    def _on_escape(self, event):
        """Esc closes the composer/viewer and returns focus to the list.
        Also called directly (event=None) from _on_next_status() when the
        last status of a contact is exhausted — see its own comment."""
        if self._is_status_composer_open():
            if self._voice_post_panel.IsShown():
                self._on_close_voice_panel(event)
            elif self._media_post_panel.IsShown():
                self._on_close_media_panel(event)
            else:
                self._on_close_post_panel(event)
        elif self._viewer_panel.IsShown():
            self._selected_contact_idx = -1
            self._viewer_panel.Hide()
            self._video_player.stop()
            self.Layout()
            self._status_list.SetFocus()
        elif event is not None:
            event.Skip()

    # ── Refresh / load statuses ──────────────────────────────────────────────

    def on_show(self):
        """Called when the panel becomes visible — refresh the status list."""
        threading.Thread(target=self._load_statuses, daemon=True).start()

    def _on_panel_show(self, event):
        """Stop any playing video the moment this panel is hidden (Alt+1/
        Alt+4 switching away, window close, ...) — regardless of which of
        main.py's several `status_panel.Hide()` call sites did it. Without
        this a video's audio kept playing (and ffmpeg kept decoding) in the
        background indefinitely after leaving the Status tab."""
        if not event.IsShown():
            self._video_player.stop()
        event.Skip()

    # ── Labels refresh ───────────────────────────────────────────────────────

    def refresh_labels(self):
        i18n = self.main_window.i18n

        self._list_label.SetLabel(i18n.t("status"))
        col = wx.ListItem()
        col.SetText(i18n.t("status"))
        self._status_list.SetColumn(0, col)

        self._add_status_btn.SetLabel(i18n.t("status_add"))
        self._refresh_status_btn.SetLabel(i18n.t("status_refresh"))
        self._prev_status_btn.SetLabel(i18n.t("status_prev"))
        self._next_status_btn.SetLabel(i18n.t("status_next"))
        self._play_pause_btn.SetLabel(i18n.t("status_play_pause"))
        self._copy_text_btn.SetLabel(i18n.t("status_copy_text"))
        self._save_media_btn.SetLabel(i18n.t("status_save_media"))
        self._reply_label.SetLabel(i18n.t("status_reply_label"))
        self._reply_send_btn.SetLabel(i18n.t("status_reply_send"))
        # Like button label depends on current state; only refresh if visible
        if self._like_btn.IsShown():
            if self._selected_contact_idx >= 0:
                entry    = self._status_contacts[self._selected_contact_idx]
                statuses = entry.get("statuses", [])
                if statuses and self._current_status_idx < len(statuses):
                    status_id = statuses[self._current_status_idx].get("key", {}).get("id", "")
                    is_liked  = self._liked_statuses.get(status_id, False)
                    self._like_btn.SetLabel(
                        i18n.t("status_unlike") if is_liked else i18n.t("status_like")
                    )
        self._post_send_btn.SetLabel(i18n.t("status_send"))
        self._post_emoji_btn.SetLabel(i18n.t("emoji_button"))
        self._post_text_label.SetLabel(i18n.t("status_text_label"))
        self._media_send_btn.SetLabel(i18n.t("status_send"))
        self._media_add_more_btn.SetLabel(i18n.t("add_more_files"))
        self._post_close_btn.SetLabel(i18n.t("close"))
        self._media_close_btn.SetLabel(i18n.t("close"))

        # Refresh the "My Status" row (index 0) if the list is populated
        if not self._list_is_loading and self._status_list.GetItemCount() > 0:
            self._status_list.SetItemText(0, self._my_status_label(i18n))
