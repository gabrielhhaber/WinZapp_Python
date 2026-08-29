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
from core.utils import format_number, get_downloads_folder, normalize_line_separators, is_voice_message
from core.video_player import VideoPlayer
from core.audio_devices import (
    find_input_device_index, fallback_input_device_indices, RECORDING_SAMPLE_CONFIGS,
)
from ui.dialogs.emoji_picker import choose_and_insert_emoji
from ui.media_viewer import MediaViewerDialog

try:
    import pyaudio
except ImportError:
    pyaudio = None


def _post_was_rejected(body) -> bool:
    """True when a send-text-storie response actually means FAILURE.

    With the status.layer.js async patch, WPPConnect answers HTTP 201 even
    when WhatsApp Web rejected the status at protocol level — the rejection
    is carried inside the payload as ``sendMsgResult.messageSendResult``
    (e.g. ``"ERROR_UNKNOWN"``, with ``ack`` staying 0). A null/empty response
    is also a failure.
    """
    if not isinstance(body, dict):
        return True
    resp_data = body.get("response")
    if isinstance(resp_data, list) and resp_data:
        for item in resp_data:
            if isinstance(item, dict):
                s = (item.get("sendMsgResult") or {}).get("messageSendResult")
                if s and s not in ("SUCCESS", "OK"):
                    return True
        return False
    return resp_data is None


def _download_status_media(main_window, status: dict, attempts: int = 4) -> bytes:
    """Wait for pending status media instead of misreporting it as corrupt."""
    last_error = None
    for attempt in range(attempts):
        try:
            encoded = main_window.get_base64_from_media(status)
            if encoded:
                return base64.b64decode(encoded)
        except Exception as exc:
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(1.0)
    raise ValueError(str(last_error or "empty media response"))


def _status_content_label(msg_type: str, msg_obj: dict, i18n, settings: dict = None) -> str:
    """Human-readable content label for one status update.

    Shared by MyStatusDialog, StatusPanel's list-row preview, and
    StatusPanel's own open viewer — a status can be an audio/document/
    sticker/contact update just like a regular message, not only text/
    image/video. Each of those three call sites used to fall through to
    the raw messageType string itself (e.g. literal "audioMessage") for
    anything past text/image/video instead of a translated label.
    """
    if msg_type == "conversation":
        return msg_obj.get("conversation", "")
    if msg_type == "extendedTextMessage":
        return (msg_obj.get("extendedTextMessage") or {}).get("text", "")
    if msg_type == "imageMessage":
        caption = ((msg_obj.get("imageMessage") or {}).get("caption") or "").strip()
        return f"{i18n.t('photo')}: {caption}" if caption else i18n.t("photo")
    if msg_type == "videoMessage":
        caption = ((msg_obj.get("videoMessage") or {}).get("caption") or "").strip()
        return f"{i18n.t('video')}: {caption}" if caption else i18n.t("video")
    if msg_type in ("audioMessage", "audio", "ptt"):
        vm_mode = (settings.get("user_interface", {}) if isinstance(settings, dict) else {}).get("voice_message_mode", "audio")
        if vm_mode == "voice_message":
            is_ptt = is_voice_message(msg_obj) or bool(isinstance(msg_obj, dict) and is_voice_message({"messageType": "audioMessage", "message": msg_obj}))
            return i18n.t("message_type_voice_message") if is_ptt else i18n.t("message_type_audio")
        return i18n.t("message_type_audio")
    if msg_type == "documentMessage":
        doc = msg_obj.get("documentMessage") or {}
        filename = doc.get("fileName") or doc.get("title") or ""
        return f"{i18n.t('document')}: {filename}" if filename else i18n.t("document")
    if msg_type == "stickerMessage":
        return i18n.t("sticker")
    if msg_type == "contactMessage":
        contact = msg_obj.get("contactMessage") or {}
        name  = contact.get("displayName") or ""
        vcard = contact.get("vcard") or ""
        # Same vCard-leak bug as MainWindow._get_message_content /
        # ConversationsPanel._get_message_content (issue #22): displayName
        # is sometimes empty, or is itself the raw vCard blob — parse the
        # FN: line instead of ever putting BEGIN:VCARD...END:VCARD on screen.
        if not name or "BEGIN:VCARD" in name:
            vcard_to_parse = name if "BEGIN:VCARD" in name else vcard
            parsed_name = ""
            for line in vcard_to_parse.splitlines():
                if line.startswith("FN:"):
                    parsed_name = line[3:].strip()
                    break
            name = parsed_name or i18n.t("unknown_contact")
        return i18n.t("contact_message").format(name=name)
    return i18n.t("notif_unsupported")


# Shared by both status media-save entry points — the classic "Salvar
# mídia" button/shortcut (_status_media_save_info(), used by
# StatusPanel._on_save_status_media() when Settings > Interface do
# usuário > "Mostrar os status em player separado" is unchecked) and the
# unified MediaViewerDialog's own Save As (_status_to_media_viewer_item()).
# Both used to compute the extension independently — a bare
# mimetype.split("/")[-1] here vs. a canonicalizing table there — so the
# very same image/jpeg status photo saved as status.jpeg from one button
# and status.jpg from the other. One table now backs both.
_STATUS_MIME_SUBTYPE_TO_EXT = {
    "jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp",
    "gif": ".gif", "mp4": ".mp4", "webm": ".webm",
    "ogg": ".ogg", "opus": ".opus", "mpeg": ".mp3", "mp3": ".mp3",
    "mp4a-latm": ".m4a", "x-m4a": ".m4a", "aac": ".aac",
    "wav": ".wav", "x-wav": ".wav", "flac": ".flac",
}


def _status_media_extension(mimetype: str, default_ext: str) -> str:
    """Canonical file extension for a status media mimetype, falling back
    to *default_ext* (with the leading dot) when the mimetype is missing
    or its subtype isn't in the table above."""
    mime = str(mimetype or "").split(";")[0].strip().lower()
    if "/" not in mime:
        return default_ext
    subtype = mime.split("/", 1)[1]
    return _STATUS_MIME_SUBTYPE_TO_EXT.get(subtype, "." + subtype.split("+")[0])


def _status_media_save_info(msg_type: str, msg_obj: dict, i18n):
    """Returns (ext, wildcard) for the "Save media as..." dialog, or None
    if *msg_type* isn't a savable media status. Shared by
    StatusPanel._on_save_status_media() so the extension/wildcard logic
    for each media type lives in one place."""
    if msg_type == "imageMessage":
        mimetype = (msg_obj.get("imageMessage") or {}).get("mimetype", "image/jpeg")
        ext = _status_media_extension(mimetype, ".jpg")
        return ext, f"{i18n.t('photo')} (*{ext})|*{ext}|*.*|*.*"
    if msg_type == "videoMessage":
        mimetype = (msg_obj.get("videoMessage") or {}).get("mimetype", "video/mp4")
        ext = _status_media_extension(mimetype, ".mp4")
        return ext, f"{i18n.t('video')} (*{ext})|*{ext}|*.*|*.*"
    if msg_type == "audioMessage":
        mimetype = (msg_obj.get("audioMessage") or {}).get("mimetype", "audio/ogg")
        ext = _status_media_extension(mimetype, ".ogg")
        return ext, f"{i18n.t('message_type_audio')} (*{ext})|*{ext}|*.*|*.*"
    return None


# ── Status reactions dialog ──────────────────────────────────────────────────

class StatusReactionsDialog(wx.Dialog):
    """Read-only list of who reacted to one of the user's own statuses, and
    with what emoji. A reaction to a status arrives through the same
    status@broadcast channel as a real status update — main.py's
    on_new_message() routes it to _store_status_update() before it ever
    inspects messageType — so it's already sitting in main_window's own
    _status_updates, just filtered out of the displayed story list itself
    (see StatusPanel._parse_statuses())."""

    def __init__(self, parent, main_window, status_id: str):
        i18n = main_window.i18n
        super().__init__(
            parent,
            title=i18n.t("status_view_reactions"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._mw = main_window
        self._status_id = status_id
        self._init_ui()
        self._load_reactions()

    def _init_ui(self):
        i18n  = self._mw.i18n
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self._list.InsertColumn(0, i18n.t("status_view_reactions"), width=300)
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 8)

        btn_sizer = wx.StdDialogButtonSizer()
        close_btn = wx.Button(panel, wx.ID_CANCEL, i18n.t("close"))
        btn_sizer.AddButton(close_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 8)

        panel.SetSizer(sizer)
        sizer.Fit(panel)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.SetSize((400, 300))
        self.CenterOnScreen()

    def _load_reactions(self):
        i18n = self._mw.i18n
        reactions = []
        for msgs in getattr(self._mw, "_status_updates", {}).values():
            for msg in msgs:
                if msg.get("messageType") != "reactionMessage":
                    continue
                reaction = (msg.get("message") or {}).get("reactionMessage") or {}
                target_id = (reaction.get("key") or {}).get("id", "")
                if target_id != self._status_id:
                    continue
                emoji = (reaction.get("text") or "").strip()
                if not emoji:
                    continue  # empty text = a reaction that was removed
                sender = (
                    msg.get("key", {}).get("participant")
                    or msg.get("participant")
                    or msg.get("key", {}).get("remoteJid", "")
                )
                name = self._mw._resolve_contact_name({"remoteJid": sender}) or format_number(sender)
                reactions.append((name, emoji))

        self._list.DeleteAllItems()
        if not reactions:
            self._list.Append((i18n.t("status_no_reactions"),))
        else:
            for name, emoji in reactions:
                self._list.Append((f"{name}: {emoji}",))

        if self._list.GetItemCount() > 0:
            self._list.Focus(0)
            self._list.Select(0)


# ── My Status dialog ─────────────────────────────────────────────────────────

class MyStatusDialog(wx.Dialog):
    """
    Modal dialog for viewing the user's own posted statuses and adding new ones.

    Return codes
    ------------
    RC_ADD_STATUS  – user clicked "Add status"; caller should open the add-flow.
    wx.ID_CANCEL   – user closed the dialog without requesting an action.
    """

    RC_ADD_STATUS = (getattr(wx, "ID_HIGHEST", 5000) if isinstance(getattr(wx, "ID_HIGHEST", None), int) else 5000) + 100

    def __init__(self, main_window, my_statuses: list):
        i18n = main_window.i18n
        super().__init__(
            None,
            title=i18n.t("my_status"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._mw       = main_window
        self._statuses = my_statuses
        self._current  = 0
        self._is_closed = False
        self._owned_temp_paths: set[str] = set()
        self._download_generation = 0
        self._init_ui()

    def _cleanup(self):
        if getattr(self, "_is_closed", False):
            return
        self._is_closed = True
        self._download_generation += 1
        if hasattr(self, "_video_player"):
            try:
                self._video_player.stop()
            except Exception:
                pass
        for path in list(self._owned_temp_paths):
            try:
                os.unlink(path)
            except Exception:
                pass
        self._owned_temp_paths.clear()

    def Destroy(self):
        self._cleanup()
        return super().Destroy()

    # ── UI build ──────────────────────────────────────────────────────────

    def _init_ui(self):
        i18n  = self._mw.i18n
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Add-status button — always visible
        self._add_btn = wx.Button(panel, label=i18n.t("status_add"))
        self._add_btn.Bind(wx.EVT_BUTTON, self._on_add_status)
        sizer.Add(self._add_btn, 0, wx.ALL, 8)

        # Viewer section — only when the user already has statuses
        if self._statuses:
            self._content_lbl = wx.StaticText(panel, label="")
            sizer.Add(self._content_lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

            nav_sizer = wx.BoxSizer(wx.HORIZONTAL)

            self._prev_btn = wx.Button(panel, label=i18n.t("status_prev"))
            self._prev_btn.SetAccessible(AccessibleStatusPrev(i18n.t("accessible_ctrl_left")))
            self._prev_btn.Bind(wx.EVT_BUTTON, self._on_prev)
            nav_sizer.Add(self._prev_btn, 0, wx.RIGHT, 5)

            self._next_btn = wx.Button(panel, label=i18n.t("status_next"))
            self._next_btn.SetAccessible(AccessibleStatusNext(i18n.t("accessible_ctrl_right")))
            self._next_btn.Bind(wx.EVT_BUTTON, self._on_next)
            nav_sizer.Add(self._next_btn, 0, wx.RIGHT, 5)

            self._view_reactions_btn = wx.Button(panel, label=i18n.t("status_view_reactions"))
            self._view_reactions_btn.Bind(wx.EVT_BUTTON, self._on_view_reactions)
            nav_sizer.Add(self._view_reactions_btn, 0)

            sizer.Add(nav_sizer, 0, wx.LEFT | wx.BOTTOM, 8)

            # In-app video/audio playback — same VideoPlayer (BASS + ffmpeg)
            # StatusPanel's own viewer uses; see _on_play_pause_video() below.
            self._video_bitmap = wx.StaticBitmap(panel, size=(320, 240))
            sizer.Add(self._video_bitmap, 0, wx.LEFT | wx.BOTTOM, 8)
            self._video_bitmap.Hide()

            self._play_pause_btn = wx.Button(panel, label=i18n.t("status_play_pause"))
            self._play_pause_btn.Bind(wx.EVT_BUTTON, self._on_play_pause_video)
            sizer.Add(self._play_pause_btn, 0, wx.LEFT | wx.BOTTOM, 8)
            self._play_pause_btn.Hide()

            self._video_player = VideoPlayer(
                self._mw, self._video_bitmap, on_frame_size=self._on_video_frame_size_known
            )
            self._video_local_path = None
            self._video_download_status_id = None
            self.Bind(wx.EVT_CLOSE, self._on_close)

            self._update_content()

        # Close button
        btn_sizer = wx.StdDialogButtonSizer()
        close_btn = wx.Button(panel, wx.ID_CANCEL, i18n.t("close"))
        # wx.ID_CANCEL's built-in handling calls EndModal() directly rather
        # than generating a wx.EVT_CLOSE — the video/audio player would
        # otherwise keep playing in the background after this dialog closes
        # via the Close button (only Alt+F4/the window-manager close was
        # actually covered by the EVT_CLOSE bind above).
        close_btn.Bind(wx.EVT_BUTTON, self._on_close)
        btn_sizer.AddButton(close_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 8)

        panel.SetSizer(sizer)
        sizer.Fit(panel)

        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        outer.Fit(self)
        self.CenterOnScreen()

        self._add_btn.SetFocus()

    # ── Content display ───────────────────────────────────────────────────

    def _update_content(self):
        if not self._statuses:
            return
        i18n   = self._mw.i18n
        total  = len(self._statuses)
        status = self._statuses[self._current]

        msg_type = status.get("messageType", "")
        msg_obj  = status.get("message") or {}
        content  = _status_content_label(msg_type, msg_obj, i18n, getattr(self._mw, "settings", None))

        nav_info = i18n.t("status_of").format(current=self._current + 1, total=total)
        label    = f"{nav_info}: {content}"
        self._content_lbl.SetLabel(label)
        self._mw.output(label, interrupt=True)

        # Switching statuses always stops whatever was playing — resuming
        # stale audio/frames for a status navigated away from would be
        # actively wrong (see StatusPanel._show_current_status(), same
        # reasoning).
        self._video_player.stop()
        self._video_local_path = None
        self._video_download_status_id = None
        self._video_bitmap.Hide()
        # Undo whatever shrink-to-content _on_video_frame_size_known() did
        # for the video just left behind — otherwise the NEXT video's first
        # frame gets fitted against that leftover (often much smaller) box
        # instead of the real 320x240 baseline, compounding smaller and
        # smaller across consecutive status videos.
        self._video_bitmap.SetMinSize((320, 240))
        self._play_pause_btn.SetLabel(i18n.t("status_play_pause"))
        is_video = msg_type == "videoMessage"
        is_audio = msg_type == "audioMessage"
        self._play_pause_btn.Show(is_video or is_audio)
        self.Layout()
        if is_audio:
            wx.CallAfter(self._on_play_pause_video, None)

    # ── Navigation ────────────────────────────────────────────────────────

    def _on_prev(self, event):
        if not self._statuses:
            return
        self._current = (self._current - 1) % len(self._statuses)
        self._update_content()

    def _on_next(self, event):
        if not self._statuses:
            return
        self._current = (self._current + 1) % len(self._statuses)
        self._update_content()

    # ── Reactions ─────────────────────────────────────────────────────────

    def _on_view_reactions(self, event):
        if not self._statuses:
            return
        status_id = self._statuses[self._current].get("key", {}).get("id", "")
        if not status_id:
            return
        dlg = StatusReactionsDialog(self, self._mw, status_id)
        dlg.ShowModal()
        dlg.Destroy()

    # ── Playback (in-app: audio via BASS, frames via ffmpeg) ────────────────
    # Mirrors StatusPanel._on_play_pause_video()/_download_and_play_video()/
    # _start_downloaded_video() — kept as separate copies rather than shared
    # since this dialog and StatusPanel track their own current-status state
    # independently (self._statuses/self._current here vs.
    # self._status_contacts/self._selected_contact_idx there).

    def _on_video_frame_size_known(self, width: int, height: int):
        """VideoPlayer callback (see core/video_player.py's own comment):
        fires once per playback with the first frame's actual on-screen
        size, so the fixed 320x240 placeholder box can shrink-wrap to it —
        same as a still photo is sized to its own content, instead of
        leaving a blank gap around a video whose aspect ratio doesn't match
        that box (reported live as the video "not showing completely" even
        once it was no longer literally clipped)."""
        self._video_bitmap.SetMinSize((width, height))
        self.Layout()

    def _on_play_pause_video(self, event):
        if not self._statuses:
            return
        status = self._statuses[self._current]
        msg_type = status.get("messageType")
        if msg_type not in ("videoMessage", "audioMessage"):
            return
        if self._video_player.is_playing:
            self._video_player.toggle_pause()
            return
        status_id = status.get("key", {}).get("id", "")
        if self._video_local_path and self._video_download_status_id == status_id:
            if msg_type == "videoMessage":
                self._video_bitmap.Show()
                self.Layout()
            self._video_player.load_and_play(self._video_local_path)
            return
        self._download_generation += 1
        generation = self._download_generation
        threading.Thread(
            target=self._download_and_play_video,
            args=(status, status_id, msg_type, generation),
            daemon=True,
        ).start()

    def _download_and_play_video(self, status, status_id: str, msg_type: str, generation: int):
        suffix = ".mp4" if msg_type == "videoMessage" else ".ogg"
        try:
            content = _download_status_media(self._mw, status)
            if getattr(self, "_is_closed", False):
                return
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(content)
            tmp.close()
            if getattr(self, "_is_closed", False):
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
                return
            wx.CallAfter(self._start_downloaded_video, tmp.name, status_id, msg_type, generation)
        except Exception as exc:
            if getattr(self, "_is_closed", False):
                return
            wx.CallAfter(
                wx.MessageBox,
                f"{self._mw.i18n.t('status_video_open_error')} ({exc})",
                self._mw.app_name,
                wx.OK | wx.ICON_ERROR,
            )

    def _start_downloaded_video(self, path: str, status_id: str, msg_type: str, generation: int):
        if getattr(self, "_is_closed", False) or not bool(self):
            try:
                os.unlink(path)
            except Exception:
                pass
            return
        if generation != self._download_generation:
            try:
                os.unlink(path)
            except Exception:
                pass
            return
        if not self._statuses:
            try:
                os.unlink(path)
            except Exception:
                pass
            return
        current_id = self._statuses[self._current].get("key", {}).get("id", "")
        if current_id != status_id:
            try:
                os.unlink(path)
            except Exception:
                pass
            return
        self._owned_temp_paths.add(path)
        self._video_local_path = path
        self._video_download_status_id = status_id
        try:
            if msg_type == "videoMessage":
                self._video_bitmap.Show()
                self.Layout()
            self._video_player.load_and_play(path)
        except (RuntimeError, wx.wxAssertionError, Exception) as exc:
            logging.warning("[MyStatusDialog] _start_downloaded_video error: %s", exc)

    def _on_close(self, event):
        self._cleanup()
        event.Skip()

    # ── Actions ───────────────────────────────────────────────────────────

    def _on_add_status(self, event):
        """Close the dialog signalling that the caller should open the add-flow."""
        self._cleanup()
        self.EndModal(MyStatusDialog.RC_ADD_STATUS)


# ── Main status panel ────────────────────────────────────────────────────────

class StatusPanel(wx.Panel):
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
        self._voice_close_btn.SetAccessible(AccessibleDiscardVoiceMessage(self.main_window))
        self._voice_close_btn.Bind(wx.EVT_BUTTON, self._on_close_voice_panel)
        self._voice_close_btn.Hide()
        voice_btn_sizer.Add(self._voice_close_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._voice_start_btn = wx.Button(self._voice_post_panel, label=i18n.t("record_voice_message"))
        self._voice_start_btn.SetAccessible(AccessibleRecordVoiceMessage("Ctrl+R"))
        self._voice_start_btn.Bind(wx.EVT_BUTTON, self._on_record_voice_button)
        voice_btn_sizer.Add(self._voice_start_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self._voice_pause_btn = wx.Button(self._voice_post_panel, label=i18n.t("pause_recording"))
        self._voice_pause_btn.SetAccessible(AccessiblePauseResumeRecording(self.main_window))
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
        self._voice_send_btn.SetAccessible(AccessibleSendVoiceMessage(self.main_window))
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
        accel_tbl = wx.AcceleratorTable([
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

    def _load_statuses(self):
        """
        Build the status list.

        Primary source: the account's StatusV3Store via
        GET /api/{session}/statuses — the user's own posted statuses come
        straight from the account (not from the local DB/in-memory cache),
        and contacts' statuses are pulled from the browser store. Falls back
        to the live status@broadcast messages collected in
        MainWindow._status_updates when the API is unreachable or returns
        nothing (e.g. the Status view was never opened in the browser yet).
        """
        mw   = self.main_window
        i18n = mw.i18n
        wx.CallAfter(self._set_list_loading)
        my_statuses, contacts = self._fetch_statuses_from_api()
        api_ok = getattr(self, "_last_status_api_ok", False)
        # A successful WhatsApp response is authoritative for own stories.
        # An empty list must clear stale local optimistic rows.
        if api_ok:
            self._reconcile_my_status_cache(my_statuses)
        # Merge, never replace: the API's StatusV3Store may only hold the
        # pages loaded so far, while _status_updates (seeded from the DB at
        # startup) keeps the stories that arrived via status@broadcast
        # earlier. Showing both (deduped by message id) covers the whole
        # picture instead of dropping whichever source has less.
        status_updates = getattr(mw, "_status_updates", {})
        records = []
        for participant, msgs in list(status_updates.items()):
            for msg in msgs:
                records.append(msg)
        if records:
            fb_my, fb_contacts = self._parse_statuses(records, i18n)
            my_statuses = self._merge_status_lists(my_statuses, fb_my)
            contacts = self._merge_status_contacts(contacts, fb_contacts)
        wx.CallAfter(self._populate_list, my_statuses, contacts)

    def _reconcile_my_status_cache(self, remote_my_statuses: list) -> None:
        """Delete cached own stories absent from authoritative WhatsApp.

        Only runs when *remote_my_statuses* is non-empty. _fetch_statuses_
        from_api() marks the fetch "ok" as soon as it gets back HTTP 200
        with a JSON dict body — it has no way to tell "you genuinely have
        no live stories right now" apart from "WPPConnect's StatusV3Store
        hasn't finished rehydrating yet" (routine right after a reconnect),
        both of which look identical here: an empty myStatus list. Treating
        an empty-but-"ok" response as authoritative used to permanently
        delete every locally cached own status — from memory AND SQLite,
        via remove_failed_status_update() — on the next reconnect after
        posting one, even though it was still live on WhatsApp. A genuinely
        expired own status (the one real case this deliberately no longer
        catches) is the far cheaper failure to leave uncorrected than
        wiping a user's own live content out from under them.
        """
        if not remote_my_statuses:
            return
        mw = self.main_window
        remote_ids = {
            (status.get("key") or {}).get("id")
            for status in remote_my_statuses
            if isinstance(status, dict)
        }
        local_records = [
            status
            for bucket in getattr(mw, "_status_updates", {}).values()
            for status in bucket
        ]
        local_my, _ = self._parse_statuses(local_records, mw.i18n)
        stale_ids = {
            (status.get("key") or {}).get("id")
            for status in local_my
            if isinstance(status, dict)
        } - remote_ids
        for message_id in stale_ids:
            if message_id:
                mw.remove_failed_status_update(message_id, refresh=False)
        if stale_ids:
            logging.info(
                "[status_panel] Removed %d local own status(es) absent from WhatsApp",
                len(stale_ids),
            )

    @staticmethod
    def _merge_status_lists(a: list, b: list) -> list:
        """Union of two status-dict lists, deduped by key.id (order: a first)."""
        seen = set()
        out = []
        for s in list(a) + list(b):
            if not isinstance(s, dict):
                continue
            mid = (s.get("key") or {}).get("id")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            out.append(s)
        return out

    @staticmethod
    def _merge_status_contacts(a: list, b: list) -> list:
        """Union of two contact-status lists, grouped by jid, deduped by id."""
        by_jid = {}
        for entry in list(a) + list(b):
            if not isinstance(entry, dict):
                continue
            jid = entry.get("jid")
            if not jid:
                continue
            merged = by_jid.get(jid)
            if merged is None:
                merged = {
                    "name": entry.get("name", ""),
                    "jid": jid,
                    "statuses": [],
                    "viewed_all": entry.get("viewed_all", False),
                }
                by_jid[jid] = merged
            seen = {(s.get("key") or {}).get("id") for s in merged["statuses"]}
            for s in entry.get("statuses") or []:
                if not isinstance(s, dict):
                    continue
                mid = (s.get("key") or {}).get("id")
                if mid and mid in seen:
                    continue
                if mid:
                    seen.add(mid)
                merged["statuses"].append(s)
        return list(by_jid.values())

    def _fetch_statuses_from_api(self) -> tuple:
        """Query WPPConnect's GET /api/{session}/statuses (StatusV3Store).

        Returns ``(my_statuses, contacts)`` in the same shape as
        _parse_statuses() — raw store messages are normalized through
        WebSocketClient._normalize_wpp_message() (the same converter the
        message sync uses), so both the API and the WebSocket paths feed the
        panel identical dicts.
        """
        mw   = self.main_window
        i18n = mw.i18n
        try:
            url = f"{mw.wpp_server}:{mw.wpp_port}/api/{mw.token}/statuses"
            headers = {"Authorization": f"Bearer {mw.token}", "Content-Type": "application/json"}
            resp = api_get(url, headers=headers, timeout=15)
            if resp.status_code not in (200, 201):
                self._last_status_api_ok = False
                return [], []
            body = resp.json() or {}
            data = body.get("response") if isinstance(body, dict) else None
        except Exception as exc:
            logging.warning("[status_panel] statuses API failed, falling back to WebSocket cache: %s", exc)
            self._last_status_api_ok = False
            return [], []
        if not isinstance(data, dict):
            self._last_status_api_ok = False
            return [], []

        ws  = getattr(mw, "ws", None)
        raw = []
        for msgs in (data.get("myStatus") or []):
            raw.append(msgs)
        for entry in (data.get("contacts") or []):
            for msgs in (entry.get("msgs") or []):
                raw.append(msgs)

        records = []
        for wm in raw:
            if not isinstance(wm, dict):
                continue
            if ws is not None:
                try:
                    records.append(ws._normalize_wpp_message(wm))
                    continue
                except Exception as exc:
                    logging.warning("[status_panel] failed to normalize API status: %s", exc)
            records.append(wm)
        self._last_status_api_ok = True
        return self._parse_statuses(records, i18n)

    def _parse_statuses(self, items, i18n) -> tuple:
        """
        Separate own statuses from other people's.

        Returns
        -------
        (my_statuses, contacts)
            my_statuses : list of status dicts posted by this account
                          (key.fromMe, or participant resolving to self —
                          see _is_self_jid() below)
            contacts    : list of {"name", "jid", "statuses"} for other people
        """
        my_statuses = []
        contacts    = []

        if not isinstance(items, list):
            return my_statuses, contacts

        # Group by participant JID (use participant over remoteJid for
        # status@broadcast entries, which is how WhatsApp encodes them).
        grouped: dict = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            # StatusV3 can retain administrative tombstones after a story is
            # revoked, deleted or expired. They are not displayable stories.
            msg_type = str(item.get("messageType") or "")
            raw_type = str(item.get("type") or "").lower()
            if msg_type in ("protocolMessage", "reactionMessage") or raw_type in (
                "revoked", "protocol", "protocolmessage",
                "reaction", "reactionmessage",
            ):
                continue
            if any(bool(item.get(flag)) for flag in (
                "isRevoked", "revoked", "isDeleted", "deleted",
                "isExpired", "expired", "isStatusExpired",
            )):
                continue

            key = item.get("key", {})
            participant = (key.get("participant") or item.get("participant") or "")
            # A status posted from a different linked device, or synced
            # back with a phone-number variant that differs from the one
            # WinZapp itself is paired under (extra/missing Brazilian 9th
            # digit, etc.), can arrive with fromMe=False even though the
            # participant is genuinely this account — checked via
            # _is_self_jid() (the same phone-digit-tolerant JID comparison
            # used everywhere else) rather than trusting fromMe alone.
            is_mine = key.get("fromMe", False) or (
                participant and self.main_window._is_self_jid(participant)
            )
            if is_mine:
                my_statuses.append(item)
                continue
            remote_jid  = key.get("remoteJid", "")
            # status@broadcast is the channel; real sender is in participant
            if remote_jid == "status@broadcast" and participant:
                jid = participant
            else:
                jid = remote_jid or participant
            if not jid or jid == "status@broadcast":
                continue
            name = self._resolve_name(jid) or format_number(jid)
            if jid not in grouped:
                grouped[jid] = {"name": name, "jid": jid, "statuses": []}
            grouped[jid]["statuses"].append(item)

        # A contact only counts as fully "viewed" once every one of their
        # current statuses has been opened at least once — matches the
        # official client's own "seen"/"unseen" ring distinction. See
        # _mark_status_viewed()/_populate_list() for where this list is
        # written and read.
        viewed_ids = set(
            self.main_window.settings.get("status_panel", {}).get("viewed_status_ids", [])
        )
        for entry in grouped.values():
            statuses = entry.get("statuses", [])
            entry["viewed_all"] = bool(statuses) and all(
                s.get("key", {}).get("id") in viewed_ids for s in statuses
            )
            contacts.append(entry)

        return my_statuses, contacts

    def _resolve_name(self, jid: str) -> str:
        # Delegate to the same resolver chats use (address-book "name"
        # preferred over the WhatsApp profile "pushName", @lid/@c.us
        # variants tried, bad-name filtering) — this used to read
        # contact.get("pushName") directly, which always showed the
        # person's own WhatsApp display name here even when a different
        # name was saved for them in the address book, unlike every chat
        # list/conversation in the app.
        mw = self.main_window
        return mw._resolve_contact_name({"remoteJid": jid}) or ""

    def _set_list_loading(self):
        self._list_is_loading = True
        i18n = self.main_window.i18n
        self._status_list.DeleteAllItems()
        self._status_list.Append((i18n.t("status_loading"),))

    @staticmethod
    def _latest_ts(entry: dict) -> int:
        """Return the highest messageTimestamp among a contact's statuses."""
        return max(
            (int(s.get("messageTimestamp", 0) or 0) for s in entry.get("statuses", [])),
            default=0,
        )

    def _status_preview(self, status: dict, i18n) -> str:
        """Return a short human-readable preview of a single status item."""
        msg_type = status.get("messageType", "")
        msg_obj  = status.get("message") or {}
        return _status_content_label(msg_type, msg_obj, i18n, getattr(self.main_window, "settings", None))

    def _populate_list(self, my_statuses: list, contacts: list):
        i18n = self.main_window.i18n

        # Sort contacts by most-recent status timestamp (newest first)
        contacts = sorted(contacts, key=self._latest_ts, reverse=True)
        # Within each contact keep statuses newest-first too
        for entry in contacts:
            entry["statuses"] = sorted(
                entry.get("statuses", []),
                key=lambda s: int(s.get("messageTimestamp", 0) or 0),
                reverse=True,
            )

        # Split into "Recentes" (at least one status not yet opened) and
        # "Vistos" (every current status already opened) sections, each
        # still newest-first internally — matches the official client's own
        # unseen/seen ring distinction. _status_contacts keeps the flat,
        # concatenated order (recent section first) so every OTHER index
        # into it (_selected_contact_idx, _is_current_status_playable(), …)
        # keeps working unchanged; only the list widget itself gets the
        # extra header rows, tracked via _status_row_contact.
        recent_contacts = [e for e in contacts if not e.get("viewed_all", False)]
        viewed_contacts = [e for e in contacts if e.get("viewed_all", False)]
        contacts = recent_contacts + viewed_contacts

        self._my_statuses          = my_statuses
        self._status_contacts      = contacts
        self._selected_contact_idx = -1
        self._list_is_loading      = False
        self._viewer_panel.Hide()
        self._status_list.DeleteAllItems()
        self._status_row_contact = {}
        self._status_contact_row = {}

        # ── Row 0: always "My Status" ─────────────────────────────────────
        self._status_list.Append((self._my_status_label(i18n),))
        self._status_row_contact[0] = -1

        def _add_section(section_contacts: list, header: str, start_contact_idx: int):
            """Appends one "--- header ---" row followed by one row per
            contact in *section_contacts*. Returns the next free contact
            index (for the following section to continue numbering from)."""
            if not section_contacts:
                return start_contact_idx
            row = self._status_list.GetItemCount()
            self._status_list.Append((f"— {header} —",))
            self._status_row_contact[row] = -1
            contact_idx = start_contact_idx
            for entry in section_contacts:
                row_text = self._status_row_text(entry, i18n)
                row = self._status_list.GetItemCount()
                self._status_list.Append((row_text,))
                self._status_row_contact[row] = contact_idx
                self._status_contact_row[contact_idx] = row
                contact_idx += 1
            return contact_idx

        next_idx = _add_section(recent_contacts, i18n.t("status_recent_updates"), 0)
        _add_section(viewed_contacts, i18n.t("status_viewed_updates"), next_idx)

        if self._status_list.GetItemCount() > 0:
            self._status_list.Focus(0)
            self._status_list.Select(0)
        self.Layout()

    def _status_row_text(self, entry: dict, i18n, nav_info: str = "", status: dict = None) -> str:
        """*status*, when given, overrides which of the contact's statuses
        the preview text is built from — used by _update_focused_status_row_
        text() so the row reflects whatever status is actually being
        navigated to, not always the newest one (statuses[0], the default
        used everywhere else this is called from, e.g. initial population)."""
        name     = entry.get("name", "")
        statuses = entry.get("statuses", [])
        preview_source = status if status is not None else (statuses[0] if statuses else None)
        if preview_source is not None:
            preview = self._status_preview(preview_source, i18n)
            base = f"{name}: {preview}" if preview else name
        else:
            base = name
        return f"{base}, {nav_info}" if nav_info else base

    def _update_focused_status_row_text(self):
        """Appends ", status X de Y" to the list row of whichever contact
        is currently open in the viewer, reflecting _current_status_idx —
        called from _show_current_status() so it stays correct both on
        first opening a contact and on Ctrl+Left/Right navigation between
        their own statuses. The preview text itself is rebuilt from the
        actual status being viewed (not always the newest one) so the row
        doesn't keep announcing the first status after navigating away
        from it."""
        idx = self._selected_contact_idx
        if idx < 0 or idx >= len(self._status_contacts):
            return
        row = self._status_contact_row.get(idx)
        if row is None:
            return
        entry    = self._status_contacts[idx]
        i18n     = self.main_window.i18n
        statuses = entry.get("statuses", [])
        current_idx = self._current_status_idx
        if not (0 <= current_idx < len(statuses)):
            return
        nav_info = i18n.t("status_of").format(
            current=current_idx + 1, total=len(statuses)
        )
        self._status_list.SetItemText(
            row, self._status_row_text(entry, i18n, nav_info, status=statuses[current_idx])
        )

    def _my_status_label(self, i18n) -> str:
        if self._my_statuses:
            suffix = i18n.t("my_status_update")
        else:
            suffix = i18n.t("my_status_none")
        return f"{i18n.t('my_status')}: {suffix}"

    def _is_current_status_playable(self, contact_idx: int) -> bool:
        """True when *contact_idx* is the contact already being shown AND
        its current status is a video/audio update — the case where
        Enter/Space on the status list should toggle play/pause instead of
        re-selecting (which would stop() and restart the player instead of
        actually pausing it — see _show_current_status())."""
        return (
            contact_idx == self._selected_contact_idx
            and self._current_status is not None
            and self._current_status.get("messageType") in ("videoMessage", "audioMessage")
        )

    def _use_status_media_viewer_dialog(self) -> bool:
        """True (default) opens a status in the dedicated, full
        MediaViewerDialog; False keeps the classic in-panel inline viewer
        instead. Settings > Interface do usuário > "Mostrar os status em
        player separado"."""
        return self.main_window.settings.get("user_interface", {}).get(
            "status_media_viewer_dialog", True
        )

    def _on_status_list_key_down(self, event):
        """Space opens the focused status exactly like Enter.

        Plain arrow navigation only changes the selected contact. It never
        opens a status and therefore never marks anything as viewed.
        """
        if event.GetKeyCode() != wx.WXK_SPACE:
            event.Skip()
            return
        idx = self._status_list.GetFocusedItem()
        if idx < 0:
            return
        if idx == 0:
            if not self._use_status_media_viewer_dialog():
                self._status_list.Select(idx)
            self._open_my_status_dialog()
            return
        contact_idx = self._status_row_contact.get(idx, -1)
        if contact_idx < 0 or contact_idx >= len(self._status_contacts):
            return

        if not self._use_status_media_viewer_dialog():
            # Play/pause toggle deliberately checked BEFORE Select(idx) runs
            # below: Select() re-fires EVT_LIST_ITEM_SELECTED even for an
            # already-selected row, which would otherwise stop() the player
            # out from under this toggle a moment later — see
            # _is_current_status_playable()'s docstring.
            if self._is_current_status_playable(contact_idx):
                self._on_play_pause_video(None)
                return
            self._status_list.Select(idx)
            self._selected_contact_idx = contact_idx
            self._show_current_status()
            return

        if contact_idx != self._selected_contact_idx:
            self._current_status_idx = 0
        self._selected_contact_idx = contact_idx
        self._open_status_media_viewer(contact_idx)

    def _on_refresh(self, event):
        threading.Thread(target=self._load_statuses, daemon=True).start()

    # ── Status list selection / activation ───────────────────────────────────

    def _on_status_contact_selected(self, event, announce: bool = False):
        """Track focus. In classic (non-dialog) mode this also drives the
        inline viewer directly — see _use_status_media_viewer_dialog()."""
        idx = event.GetIndex()

        if not self._use_status_media_viewer_dialog():
            if idx == 0:
                # My Status row selected — hide the inline viewer; dialog
                # opens on activate.
                self._selected_contact_idx = -1
                self._viewer_panel.Hide()
                self.Layout()
                return
            contact_idx = self._status_row_contact.get(idx, -1)
            if contact_idx < 0 or contact_idx >= len(self._status_contacts):
                self._viewer_panel.Hide()
                self.Layout()
                return
            # Only jump back to the FIRST status when selecting a genuinely
            # different contact. This event also fires from Select() calls
            # elsewhere (e.g. Space re-activating the row the list already
            # has focused, while the user has since moved forward within
            # the viewer via Ctrl+Left/Right) — resetting unconditionally
            # meant pressing Space while sitting on "status 3 de 5" silently
            # snapped it back to "1 de 5" for no reason.
            if contact_idx != self._selected_contact_idx:
                self._current_status_idx = 0
            self._selected_contact_idx = contact_idx
            # Defaults to silent: NVDA/JAWS already read the newly-focused
            # list item on their own on plain arrow-key navigation (EVT_
            # LIST_ITEM_SELECTED) — see _show_current_status()'s own
            # docstring. Callers driven by an explicit action rather than
            # mere focus movement (Space, Enter/double-click activation)
            # pass announce=True.
            self._show_current_status(announce=announce)
            return

        # Dialog mode: the old inline viewer is deliberately not used for
        # passive list navigation. Keeping it hidden is also important for
        # screen readers: arrowing the list should announce only the list
        # item — the dialog only opens on an explicit activation.
        try:
            self._video_player.stop()
        except Exception:
            pass
        self._viewer_panel.Hide()
        self.Layout()

        if idx == 0:
            self._selected_contact_idx = -1
            return
        contact_idx = self._status_row_contact.get(idx, -1)
        if contact_idx < 0 or contact_idx >= len(self._status_contacts):
            return
        if contact_idx != self._selected_contact_idx:
            self._current_status_idx = 0
        self._selected_contact_idx = contact_idx

    def _on_status_contact_activated(self, event):
        idx = event.GetIndex()
        if idx == 0:
            self._open_my_status_dialog()
            return
        contact_idx = self._status_row_contact.get(idx, -1)
        if contact_idx < 0 or contact_idx >= len(self._status_contacts):
            return

        if not self._use_status_media_viewer_dialog():
            if self._is_current_status_playable(contact_idx):
                self._on_play_pause_video(None)
                return
            self._on_status_contact_selected(event, announce=True)
            return

        if contact_idx != self._selected_contact_idx:
            self._current_status_idx = 0
        self._selected_contact_idx = contact_idx
        self._open_status_media_viewer(contact_idx)

    def _open_my_status_dialog(self):
        dlg    = MyStatusDialog(self.main_window, self._my_statuses)
        result = dlg.ShowModal()
        dlg.Destroy()
        if result == MyStatusDialog.RC_ADD_STATUS:
            # User wants to add a status — open the popup menu
            self._on_add_status(None)

    # ── Unified status media viewer ─────────────────────────────────────────

    def _open_status_media_viewer(self, contact_idx: int):
        if contact_idx < 0 or contact_idx >= len(self._status_contacts):
            return
        entry = self._status_contacts[contact_idx]
        statuses = entry.get("statuses", [])
        if not statuses:
            return

        items = [self._status_to_media_viewer_item(entry, status) for status in statuses]
        start_index = max(0, min(self._current_status_idx, len(items) - 1))
        dlg = MediaViewerDialog(
            self,
            self.main_window,
            items,
            start_index=start_index,
            on_item_opened=self._on_viewer_status_opened,
            is_liked=self._viewer_status_is_liked,
            on_like=self._viewer_like_status,
            on_reply=self._viewer_reply_status,
        )
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
            row = self._status_contact_row.get(contact_idx)
            if row is not None and 0 <= row < self._status_list.GetItemCount():
                try:
                    self._status_list.Focus(row)
                    self._status_list.Select(row)
                    self._status_list.SetFocus()
                except Exception:
                    pass

    def _status_to_media_viewer_item(self, entry: dict, status: dict) -> dict:
        i18n = self.main_window.i18n
        msg_type = status.get("messageType", "")
        msg_obj = status.get("message") or {}
        key = status.get("key", {})
        status_id = key.get("id", "")
        from_me = bool(key.get("fromMe", False))
        label = entry.get("name", "")

        item = {
            "status": status,
            "entry": entry,
            "status_id": status_id,
            "from_me": from_me,
            "label": label,
        }

        if msg_type in ("conversation", "extendedTextMessage"):
            if msg_type == "conversation":
                text = msg_obj.get("conversation", "")
            else:
                text = (msg_obj.get("extendedTextMessage") or {}).get("text", "")
            item.update(kind="text", text=text)
            return item

        vm_mode = (self.main_window.settings.get("user_interface", {}) if hasattr(self, "main_window") and self.main_window and hasattr(self.main_window, "settings") else {}).get("voice_message_mode", "audio")
        is_ptt = is_voice_message(msg_obj) or bool(isinstance(msg_obj, dict) and is_voice_message({"messageType": "audioMessage", "message": msg_obj}))
        audio_label_key = "message_type_voice_message" if (vm_mode == "voice_message" and is_ptt) else "message_type_audio"
        type_map = {
            "imageMessage": ("image", ".jpg", "photo"),
            "videoMessage": ("video", ".mp4", "video"),
            "audioMessage": ("audio", ".ogg", audio_label_key),
        }
        if msg_type in type_map:
            kind, default_ext, label_key = type_map[msg_type]
            inner = msg_obj.get(msg_type) or {}
            ext = _status_media_extension(inner.get("mimetype"), default_ext)
            caption = str(inner.get("caption") or "")

            def _loader(st=status):
                return _download_status_media(self.main_window, st)

            item.update(
                kind=kind,
                loader=_loader,
                extension=ext,
                filename=f"status{ext}",
                caption=caption,
                media_label=i18n.t(label_key),
                is_ptt=is_ptt,
            )
            return item

        # Documents, stickers, contacts and any future status type still open
        # in the same modal window as accessible read-only text rather than
        # silently doing nothing.
        item.update(kind="text", text=_status_content_label(msg_type, msg_obj, i18n, getattr(self.main_window, "settings", None)))
        return item

    def _on_viewer_status_opened(self, item: dict, index: int):
        """The ONLY place where another person's status becomes viewed."""
        self._current_status_idx = index
        self._current_status = item.get("status")
        self._current_status_entry = item.get("entry")
        status_id = item.get("status_id", "")
        if status_id and not item.get("from_me"):
            self._mark_status_viewed(status_id)
        self._update_focused_status_row_text()

    def _viewer_status_is_liked(self, item: dict) -> bool:
        return self._is_status_liked(item.get("status_id", ""))

    def _viewer_like_status(self, item: dict, done):
        """Send a status like and report completion back to MediaViewer."""
        status = item.get("status") or {}
        entry = item.get("entry") or {}
        status_key = status.get("key", {})
        status_id = item.get("status_id", "")
        if not status_id:
            wx.CallAfter(done, False)
            return
        if self._is_status_liked(status_id):
            self._on_unlike_status_attempted()
            wx.CallAfter(done, False)
            return

        sender_jid = status_key.get("participant", "") or entry.get("jid", "")
        if not sender_jid:
            wx.CallAfter(done, False)
            return

        mw = self.main_window

        def _send_like():
            try:
                ok = bool(mw.send_text_message(sender_jid, "❤️"))
            except Exception:
                ok = False
            if ok:
                wx.CallAfter(self._on_like_sent, status_id)
                wx.CallAfter(done, True)
            else:
                wx.CallAfter(
                    wx.MessageBox,
                    mw.i18n.t("status_like_error"),
                    mw.app_name,
                    wx.OK | wx.ICON_ERROR,
                )
                wx.CallAfter(done, False)

        threading.Thread(target=_send_like, daemon=True).start()

    def _viewer_reply_status(self, item: dict, text: str, done):
        status = item.get("status") or {}
        entry = item.get("entry") or {}
        poster_jid = entry.get("jid", "")
        if not poster_jid or status.get("key", {}).get("fromMe"):
            wx.CallAfter(done, False)
            return

        def _send():
            try:
                result = self.main_window.send_text_message(poster_jid, text)
                ok = bool(result) and not isinstance(result, dict)
            except Exception:
                ok = False
            wx.CallAfter(done, ok)

        threading.Thread(target=_send, daemon=True).start()

    # ── Status viewer ────────────────────────────────────────────────────────

    def _show_current_status(self, announce: bool = True):
        """Refresh the viewer for whatever status is currently selected.

        *announce* controls whether the "Nome — status X de Y: conteúdo"
        label is also spoken. Arrow-key navigation through the CONTACT list
        (_on_status_contact_selected) passes False for this: NVDA/JAWS
        already read the newly-focused list item on their own, so speaking
        it again here was pure redundant chatter on every single arrow
        press. Explicit status navigation — Ctrl+Left/Right between a
        contact's own statuses, and Space to open/activate the focused
        contact — still announces, since neither of those has an
        equivalent native readout to fall back on.
        """
        if self._selected_contact_idx < 0:
            return
        entry    = self._status_contacts[self._selected_contact_idx]
        statuses = entry.get("statuses", [])
        if not statuses:
            return

        i18n    = self.main_window.i18n
        total   = len(statuses)
        current = self._current_status_idx
        status  = statuses[current]

        msg_type = status.get("messageType", "")
        msg_obj  = status.get("message") or {}
        content  = _status_content_label(msg_type, msg_obj, i18n, getattr(self.main_window, "settings", None))

        nav_info = i18n.t("status_of").format(current=current + 1, total=total)
        label    = f"{entry.get('name', '')} — {nav_info}: {content}"
        self._status_content_label.SetLabel(label)

        # Switching to a (possibly different) status always stops whatever
        # video was playing — resuming stale audio/frames for a status the
        # user has since navigated away from would be actively wrong, not
        # just unhelpful.
        self._video_player.stop()
        self._video_local_path = None
        self._video_download_status_id = None
        self._video_bitmap.Hide()
        # Undo whatever shrink-to-content _on_video_frame_size_known() did
        # for the video just left behind — otherwise the NEXT video's first
        # frame gets fitted against that leftover (often much smaller) box
        # instead of the real 320x240 baseline, compounding smaller and
        # smaller across consecutive status videos.
        self._video_bitmap.SetMinSize((320, 240))
        self._play_pause_btn.SetLabel(i18n.t("status_play_pause"))

        # Kept for the action handlers below (copy text, save media, open
        # video, reply) — all act on "whatever status is currently shown".
        self._current_status       = status
        self._current_status_entry = entry

        is_video = msg_type == "videoMessage"
        is_audio = msg_type == "audioMessage"
        is_image = msg_type == "imageMessage"
        self._play_pause_btn.Show(is_video or is_audio)
        self._save_media_btn.Show(is_video or is_image or is_audio)

        # Copy-text applies to the actual text content: the full text for a
        # text status, or just the caption (not the "Foto:"/"Vídeo:" label
        # prefix _show_current_status() built above) for a media status.
        if msg_type in ("conversation", "extendedTextMessage"):
            copy_text = content
        elif msg_type == "imageMessage":
            copy_text = (msg_obj.get("imageMessage") or {}).get("caption", "").strip()
        elif msg_type == "videoMessage":
            copy_text = (msg_obj.get("videoMessage") or {}).get("caption", "").strip()
        else:
            copy_text = ""
        self._current_status_text = copy_text
        self._copy_text_btn.Show(bool(copy_text))

        # ── Like / reply — only for other people's statuses ────────────────
        status_key  = status.get("key", {})
        from_me     = status_key.get("fromMe", False)
        if not from_me:
            status_id = status_key.get("id", "")
            # In dialog mode (the default), a status is marked viewed only
            # by MediaViewer's on_item_opened callback, after the user
            # explicitly activates it — see _on_viewer_status_opened().
            # _show_current_status() itself is now reachable only in
            # classic/inline mode (Settings > Interface do usuário >
            # "Mostrar os status em player separado" unchecked — see
            # _use_status_media_viewer_dialog()), where it is the ONLY
            # place a status ever gets marked viewed, exactly like before
            # that setting existed: arrowing to a contact there immediately
            # shows (and views) their status, same as it always did.
            if status_id:
                self._mark_status_viewed(status_id)
            is_liked  = self._is_status_liked(status_id)
            i18n2     = self.main_window.i18n
            self._like_btn.SetLabel(
                i18n2.t("status_unlike") if is_liked else i18n2.t("status_like")
            )
            self._like_btn.Show()
            self._reply_label.Show()
            self._reply_field.Show()
            self._reply_send_btn.Show(bool(self._reply_field.GetValue().strip()))
        else:
            self._like_btn.Hide()
            self._reply_label.Hide()
            self._reply_field.Hide()
            self._reply_send_btn.Hide()

        self._viewer_panel.Show()
        self.Layout()

        self._update_focused_status_row_text()

        if announce:
            self.main_window.output(label, interrupt=True)

    # ── Status navigation (Ctrl+Left / Ctrl+Right) ───────────────────────────

    def _on_prev_status(self, event):
        if self._selected_contact_idx < 0:
            return
        entry    = self._status_contacts[self._selected_contact_idx]
        statuses = entry.get("statuses", [])
        if not statuses:
            return
        self._current_status_idx = (self._current_status_idx - 1) % len(statuses)
        self._show_current_status()

    def _on_next_status(self, event):
        if self._selected_contact_idx < 0:
            return
        entry    = self._status_contacts[self._selected_contact_idx]
        statuses = entry.get("statuses", [])
        if not statuses:
            return
        # Wraps back around to the first status, mirroring _on_prev_status()
        # wrapping back to the last one.
        self._current_status_idx = (self._current_status_idx + 1) % len(statuses)
        self._show_current_status()

    # ── Viewed status tracking (drives the "Vistos" section) ────────────────

    # Same rationale/shape as _MAX_REMEMBERED_LIKES right below: WPPConnect
    # exposes no server-side "mark status as seen" API to call (see this
    # module's own docstring — the whole status list is built from live
    # status@broadcast events, nothing is ever queried on demand), so
    # "viewed" is tracked purely locally and never shrinks on its own.
    _MAX_REMEMBERED_VIEWED = 2000

    def _mark_status_viewed(self, status_id: str):
        """Remember that this status has been opened, persisted the same
        way _on_like_sent() remembers a like — read back by _parse_statuses()
        to decide whether a contact's whole set of current statuses counts
        as fully "viewed" (see its own "viewed_all" comment) for the
        Recentes/Vistos split in _populate_list().
        """
        mw = self.main_window
        section = mw.settings.setdefault("status_panel", {})
        remembered = section.setdefault("viewed_status_ids", [])
        if status_id not in remembered:
            remembered.append(status_id)
            if len(remembered) > self._MAX_REMEMBERED_VIEWED:
                del remembered[:len(remembered) - self._MAX_REMEMBERED_VIEWED]
            mw.save_settings()

    # ── Like / unlike status ─────────────────────────────────────────────────

    # Cap on how many liked-status ids settings.json keeps. Keeping the most
    # recent ones is what matters: an old status has long since expired, so
    # nobody will ever ask "was this one liked?" again anyway.
    _MAX_REMEMBERED_LIKES = 500

    def _is_status_liked(self, status_id: str) -> bool:
        """Was this status already "liked"?

        _liked_statuses only ever gets populated for likes sent THIS
        session — it starts empty on every launch, so reopening a status
        liked before restarting used to always show "Curtir" again with
        no way to tell it had already been done. Persisted in
        settings.json (settings["status_panel"]["liked_status_ids"]) so it
        survives a restart. Native status reactions are not stored as normal
        private-chat messages, so there is no message-history row to infer this
        state from after relaunching WinZapp.
        """
        if status_id in self._liked_statuses:
            return self._liked_statuses[status_id]
        remembered = self.main_window.settings.get("status_panel", {}).get("liked_status_ids", [])
        return bool(status_id) and status_id in remembered

    def _on_like_status(self, event):
        """Toggle the native heart reaction on the displayed status.

        This must go through ``react-message`` with a status@broadcast key.
        Sending a literal heart with ``send_text_message`` creates an ordinary
        private message, which is observably different from WhatsApp's Status
        Like button. The patched Node route resolves the status in the
        poster's StatusV3Model, just as the status-reply route does.
        """
        if self._selected_contact_idx < 0:
            return
        entry    = self._status_contacts[self._selected_contact_idx]
        statuses = entry.get("statuses", [])
        if not statuses:
            return
        status     = statuses[self._current_status_idx]
        status_key = status.get("key", {})
        status_id  = status_key.get("id", "")
        if not status_id:
            return

        is_liked = self._is_status_liked(status_id)

        sender_jid = (
            status_key.get("participant", "")
            or entry.get("jid", "")
        )
        if not sender_jid:
            return

        # API-normalized statuses normally already carry both fields. The
        # fallbacks keep WebSocket-cache records and older stored records just
        # as reactable without mutating the status displayed by the panel.
        reaction_key = dict(status_key)
        reaction_key["remoteJid"] = "status@broadcast"
        if not reaction_key.get("participant"):
            reaction_key["participant"] = sender_jid

        mw = self.main_window

        def _do_like():
            try:
                ok = bool(mw.send_reaction(
                    "status@broadcast", reaction_key, "" if is_liked else "❤️"
                ))
            except Exception:
                ok = False
            if ok:
                wx.CallAfter(self._on_like_sent, status_id, not is_liked)
            else:
                wx.CallAfter(
                    wx.MessageBox,
                    mw.i18n.t("status_like_error"),
                    mw.app_name,
                    wx.OK | wx.ICON_ERROR,
                )

        threading.Thread(target=_do_like, daemon=True).start()

    def _on_like_sent(self, status_id: str, liked: bool = True):
        self._liked_statuses[status_id] = liked

        mw = self.main_window
        section = mw.settings.setdefault("status_panel", {})
        remembered = section.setdefault("liked_status_ids", [])
        settings_changed = False
        if liked and status_id not in remembered:
            remembered.append(status_id)
            if len(remembered) > self._MAX_REMEMBERED_LIKES:
                del remembered[:len(remembered) - self._MAX_REMEMBERED_LIKES]
            settings_changed = True
        elif not liked and status_id in remembered:
            remembered.remove(status_id)
            settings_changed = True
        if settings_changed:
            mw.save_settings()

        # The status shown may have changed while the send was in flight
        # (Ctrl+Left/Right) — only touch the button if it's still this one.
        if (self._current_status or {}).get("key", {}).get("id") == status_id:
            self._like_btn.SetLabel(
                mw.i18n.t("status_unlike") if liked else mw.i18n.t("status_like")
            )

    # ── Play/pause video status (in-app: audio via BASS, frames via ffmpeg) ──
    #
    # See core/video_player.py's module docstring for the full explanation:
    # BASS alone is audio-only and can't decode WhatsApp's .mp4 either way,
    # so the video's audio is extracted to WAV (bundled ffmpeg) for BASS,
    # and its picture is decoded by that same ffmpeg binary as a capped-rate
    # JPEG frame sequence drawn into self._video_bitmap.

    def _on_video_frame_size_known(self, width: int, height: int):
        """VideoPlayer callback (see core/video_player.py's own comment):
        fires once per playback with the first frame's actual on-screen
        size, so the fixed 320x240 placeholder box can shrink-wrap to it —
        same as a still photo is sized to its own content, instead of
        leaving a blank gap around a video whose aspect ratio doesn't match
        that box (reported live as the video "not showing completely" even
        once it was no longer literally clipped)."""
        self._video_bitmap.SetMinSize((width, height))
        self.Layout()

    def _on_play_pause_video(self, event):
        """Play/pause the current status's media — video (picture + audio)
        or audio-only. Named for video since that's what this predates, but
        VideoPlayer already plays an audio-only file just fine on its own
        (BASS decodes it directly; ffmpeg's frame pipe just produces nothing
        for a file with no video stream) — the only thing missing for audio
        statuses was ever showing this button at all (see _show_current_status())."""
        if self._current_status is None:
            return
        msg_type = self._current_status.get("messageType")
        if msg_type not in ("videoMessage", "audioMessage"):
            return
        if self._video_player.is_playing:
            self._video_player.toggle_pause()
            self._update_play_pause_label()
            return
        status_id = self._current_status.get("key", {}).get("id", "")
        if self._video_local_path and self._video_download_status_id == status_id:
            # Already downloaded (e.g. finished playing once) — replay
            # without hitting the network again.
            if msg_type == "videoMessage":
                self._video_bitmap.Show()
                self.Layout()
            self._video_player.load_and_play(self._video_local_path)
            self._update_play_pause_label()
            return
        threading.Thread(
            target=self._download_and_play_video,
            args=(self._current_status, status_id, msg_type),
            daemon=True,
        ).start()

    def _download_and_play_video(self, status, status_id: str, msg_type: str = "videoMessage"):
        mw = self.main_window
        # Audio statuses arrive as Opus/OGG (same as voice messages), never
        # .mp4 — the suffix only matters for BASS's own format sniffing
        # fallback and for a sensible temp filename, not correctness.
        suffix = ".mp4" if msg_type == "videoMessage" else ".ogg"
        try:
            content = _download_status_media(mw, status)
            if not bool(self):
                return
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp.write(content)
            tmp.close()
            if not bool(self):
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
                return
            wx.CallAfter(self._start_downloaded_video, tmp.name, status_id, msg_type)
        except Exception:
            if not bool(self):
                return
            wx.CallAfter(
                wx.MessageBox,
                mw.i18n.t("status_video_open_error"),
                mw.app_name,
                wx.OK | wx.ICON_ERROR,
            )

    def _start_downloaded_video(self, path: str, status_id: str, msg_type: str = "videoMessage"):
        if not bool(self):
            try:
                os.unlink(path)
            except Exception:
                pass
            return
        # The user may have navigated to a different status while this was
        # downloading — don't start playback for a status that isn't the
        # one currently shown.
        current_id = (self._current_status or {}).get("key", {}).get("id", "")
        if current_id != status_id:
            try:
                os.unlink(path)
            except Exception:
                pass
            return
        self._video_local_path = path
        self._video_download_status_id = status_id
        try:
            if msg_type == "videoMessage":
                self._video_bitmap.Show()
                self.Layout()
            self._video_player.load_and_play(path)
            self._update_play_pause_label()
        except (RuntimeError, wx.wxAssertionError, Exception) as exc:
            logging.warning("[StatusPanel] _start_downloaded_video error: %s", exc)

    def _update_play_pause_label(self):
        # Single toggle label ("Reproduzir/Pausar status"), same convention
        # this button already used before video playback existed at all —
        # its pressed/not-pressed meaning is announced via the state change
        # itself, not a swapping label.
        self._play_pause_btn.SetLabel(self.main_window.i18n.t("status_play_pause"))

    # ── Copy status text ──────────────────────────────────────────────────────

    def _on_copy_status_text(self, event):
        text = self._current_status_text
        mw   = self.main_window
        if not text:
            mw.output(mw.i18n.t("status_copy_error"))
            return
        import pyperclip
        try:
            pyperclip.copy(text)
            mw.output(mw.i18n.t("status_text_copied"))
        except Exception:
            wx.MessageBox(
                mw.i18n.t("status_copy_error"),
                mw.app_name,
                wx.OK | wx.ICON_ERROR,
            )

    # ── Save status media (photo/video) ──────────────────────────────────────

    def _on_save_status_media(self, event):
        status = self._current_status
        if status is None:
            return
        msg_type = status.get("messageType", "")
        mw = self.main_window
        save_info = _status_media_save_info(msg_type, status.get("message", {}), mw.i18n)
        if save_info is None:
            return
        ext, wildcard = save_info

        with wx.FileDialog(
            self, mw.i18n.t("status_save_media"),
            defaultDir=get_downloads_folder(),
            defaultFile=f"status{ext}",
            wildcard=wildcard,
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            save_path = dlg.GetPath()

        threading.Thread(
            target=self._save_status_media_bg,
            args=(status, save_path),
            daemon=True,
        ).start()

    def _save_status_media_bg(self, status, save_path: str):
        mw = self.main_window
        try:
            content = _download_status_media(mw, status)
            with open(save_path, "wb") as fh:
                fh.write(content)
            wx.CallAfter(mw.output, mw.i18n.t("status_media_saved"))
        except Exception:
            wx.CallAfter(
                wx.MessageBox,
                mw.i18n.t("status_media_save_error"),
                mw.app_name,
                wx.OK | wx.ICON_ERROR,
            )

    # ── Reply to the currently viewed status ─────────────────────────────────

    def _on_reply_field_text_changed(self, event):
        """Send button only makes sense once there's something to send —
        hide it while the reply field is empty."""
        self._reply_send_btn.Show(bool(self._reply_field.GetValue().strip()))
        self.Layout()
        if event is not None:
            event.Skip()

    def _on_send_status_reply(self, event):
        status = self._current_status
        entry  = self._current_status_entry
        if status is None or entry is None:
            return
        if status.get("key", {}).get("fromMe"):
            return  # no reply UI for own statuses — see _show_current_status()
        text = normalize_line_separators(self._reply_field.GetValue()).strip()
        if not text:
            return
        poster_jid = entry.get("jid", "")
        if not poster_jid:
            return
        threading.Thread(
            target=self._send_status_reply_bg,
            args=(poster_jid, text, status),
            daemon=True,
        ).start()

    def _send_status_reply_bg(self, poster_jid: str, text: str, status: dict):
        mw = self.main_window
        try:
            # Status messages live in WhatsApp Web's per-poster StatusV3Model,
            # not in the ordinary chat message collection.  The patched Node
            # send-reply route resolves this serialized status key in that
            # model before sending, so keep the status as the quote target
            # here instead of degrading the reply to a normal DM.
            result = mw.send_text_message(poster_jid, text, quoted=status)
        except Exception:
            logging.exception(
                "[status-reply] send_text_message raised for %s", poster_jid)
            result = None
        # send_text_message() returns a message-id string or True on success,
        # or a dict ({"ok": False, ...}) on a definite failure.
        ok = bool(result) and not isinstance(result, dict)
        if ok:
            wx.CallAfter(self._on_status_reply_sent)
        else:
            # The dialog can only say "it failed"; this is the only place that
            # can say WHY. Reported live — "ao teclar enter deu Não foi
            # possível enviar a resposta ao status", and the same text sent
            # fine from the button seconds later — and the log held nothing at
            # all about it, so there was no way to tell a rejected JID from a
            # dropped connection from a server-side refusal. Enter and the
            # button are the same handler (see the two Bind calls in
            # _build_viewer), so the difference was never the key pressed.
            logging.warning(
                "[status-reply] failed for poster=%s (result=%r, text_len=%d)",
                poster_jid, result, len(text),
            )
            wx.CallAfter(
                wx.MessageBox,
                mw.i18n.t("status_reply_error"),
                mw.app_name,
                wx.OK | wx.ICON_ERROR,
            )

    def _on_status_reply_sent(self):
        self._reply_field.SetValue("")
        # SetValue("") above fires EVT_TEXT -> _on_reply_field_text_changed(),
        # which hides _reply_send_btn now that the field is empty again —
        # without refocusing the field itself here, keyboard focus was left
        # on that now-hidden button with nothing to land on.
        self._reply_field.SetFocus()
        self.main_window.output(self.main_window.i18n.t("status_reply_sent"))

    # ── Add status (PopupMenu) ───────────────────────────────────────────────

    def _on_add_status(self, event):
        i18n     = self.main_window.i18n
        menu     = wx.Menu()
        id_text  = wx.NewIdRef()
        id_media = wx.NewIdRef()
        id_voice = wx.NewIdRef()
        menu.Append(id_text,  i18n.t("status_text"))
        menu.Append(id_media, i18n.t("status_photos_videos"))
        menu.Append(id_voice, i18n.t("status_audio"))
        menu.Bind(wx.EVT_MENU, self._on_choose_text_status,  id=id_text)
        menu.Bind(wx.EVT_MENU, self._on_choose_media_status, id=id_media)
        menu.Bind(wx.EVT_MENU, self._on_choose_voice_status, id=id_voice)
        self.PopupMenu(menu)
        menu.Destroy()

    def _on_choose_text_status(self, event):
        self._enter_status_composer(self._post_panel)
        self._post_text_field.SetValue("")
        self._caption_field.SetValue("")
        self._post_text_field.SetFocus()

    def _on_open_post_emoji_picker(self, event):
        """Open the shared picker while composing a text status."""
        if not self._post_panel.IsShown() or not self._post_text_field.IsEnabled():
            return
        choose_and_insert_emoji(self, self._post_text_field, self.main_window.i18n)

    def _on_choose_media_status(self, event):
        i18n = self.main_window.i18n
        wildcard = (
            f"{i18n.t('status_photos_videos_audio')} "
            "(*.jpg;*.jpeg;*.png;*.gif;*.webp;*.mp4;*.avi;*.mov;*.mkv;"
            "*.mp3;*.ogg;*.wav;*.m4a;*.aac)"
            "|*.jpg;*.jpeg;*.png;*.gif;*.webp;*.mp4;*.avi;*.mov;*.mkv;"
            "*.mp3;*.ogg;*.wav;*.m4a;*.aac"
            f"|{i18n.t('attachment_document')} (*.*)|*.*"
        )
        dlg = wx.FileDialog(
            self,
            message=i18n.t("status_photos_videos_audio"),
            wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        )
        if dlg.ShowModal() == wx.ID_OK:
            self._selected_media_paths = dlg.GetPaths()
            dlg.Destroy()
            self._enter_status_composer(self._media_post_panel)
            self._media_caption_field.SetValue("")
            self._rebuild_media_attachment_list()
            self._media_caption_field.SetFocus()
        else:
            dlg.Destroy()

    def _on_close_post_panel(self, event):
        self._leave_status_composer()

    def _on_close_media_panel(self, event):
        self._selected_media_paths = []
        self._leave_status_composer()

    def _hide_post_panels(self):
        # Disable as well as hide, so controls from the two inactive choices
        # cannot remain keyboard-focusable or exposed as enabled actions in
        # the Windows/MSAA accessibility tree while another composer is open.
        for panel in (
            self._post_panel,
            self._media_post_panel,
            self._voice_post_panel,
        ):
            panel.Disable()
            panel.Hide()

    def _is_status_composer_open(self) -> bool:
        return any(
            panel.IsShown()
            for panel in (
                self._post_panel,
                self._media_post_panel,
                self._voice_post_panel,
            )
        )

    def _enter_status_composer(self, panel):
        """Show only the selected Add Status flow.

        The status browser and every other composer are deliberately hidden:
        recording controls and their shortcuts belong to Audio, attachment
        controls belong to Media, and text controls belong to Text. Keeping
        them beside the status list made the main screen needlessly crowded.
        """
        self._hide_post_panels()
        self._viewer_panel.Hide()
        self._video_player.stop()
        for widget in (
            self._add_status_btn,
            self._refresh_status_btn,
            self._list_label,
            self._status_list,
        ):
            widget.Hide()
        panel.Enable()
        panel.Show()
        self.Layout()

    def _leave_status_composer(self):
        """Return from any Add Status flow to the clean status browser."""
        self._hide_post_panels()
        for widget in (
            self._add_status_btn,
            self._refresh_status_btn,
            self._list_label,
            self._status_list,
        ):
            widget.Show()
        self.Layout()
        self._status_list.SetFocus()

    # ── Record & post voice status ───────────────────────────────────────────

    def _on_choose_voice_status(self, event):
        """Open the voice status post panel in prepared state (NOT recording yet).
        User can click Record or press Ctrl+R to start recording."""
        self._enter_status_composer(self._voice_post_panel)

        self._is_recording = False
        self._recording_paused = False
        self._recording_frames = []
        self._stop_recorded_audio_preview()
        self._stop_recording_stream()

        i18n = self.main_window.i18n
        self._voice_status_lbl.SetLabel(i18n.t("recording_in_progress"))
        self._voice_start_btn.SetLabel(i18n.t("record_voice_message"))
        self._voice_start_btn.Show()
        self._voice_pause_btn.Hide()
        self._voice_play_btn.Hide()
        self._voice_send_btn.Hide()
        # Audio is a self-contained flow. Before capture starts this is the
        # Close action; once recording starts _on_stream_opened relabels the
        # same control to Discard, exactly like the conversation recorder.
        self._voice_close_btn.SetLabel(i18n.t("close"))
        self._voice_close_btn.Show()

        self._voice_start_btn.SetFocus()

    def _on_ctrl_r_shortcut(self, event):
        """Ctrl+R shortcut handler for status panel.
        It belongs only to the Audio composer selected from Add Status: start
        if idle, or send if recording. It must not open Audio from the clean
        Status browser, otherwise audio controls leak outside their option."""
        if self._voice_post_panel.IsShown():
            if not self._is_recording:
                if not self._recording_starting:
                    self._start_voice_recording()
            else:
                self._on_send_voice_status(None)

    def _on_ctrl_shift_p_shortcut(self, event):
        """Ctrl+Shift+P shortcut handler to pause/resume voice recording."""
        if self._voice_post_panel.IsShown() and self._is_recording:
            self._toggle_pause_voice_recording(event)

    def _on_ctrl_p_shortcut(self, event):
        """Ctrl+P plays/stops the paused recording, matching conversations."""
        if (
            self._voice_post_panel.IsShown()
            and self._is_recording
            and self._recording_paused
        ):
            self._toggle_play_recorded_audio(event)

    def _on_ctrl_shift_d_shortcut(self, event):
        """Ctrl+Shift+D shortcut handler to discard voice recording/panel."""
        if self._voice_post_panel.IsShown():
            self._on_close_voice_panel(event)

    def _on_record_voice_button(self, event):
        if not self._is_recording:
            if not self._recording_starting:
                self._start_voice_recording()
        else:
            self._on_send_voice_status(event)

    def _start_voice_recording(self):
        """Start recording voice audio stream."""
        if pyaudio is None:
            self.main_window.output(self.main_window.i18n.t("voice_recording_unavailable"))
            return

        self._recording_frames = []
        self._recording_paused = False

        def _callback(in_data, frame_count, time_info, status):
            if not self._recording_paused:
                self._recording_frames.append(in_data)
            return (None, pyaudio.paContinue)

        if self._recording_pa is None:
            try:
                self._recording_pa = pyaudio.PyAudio()
            except Exception as exc:
                logging.error("[status audio] Failed to initialize PyAudio: %s", exc)
                return
        pa = self._recording_pa

        def _try_open(device_index):
            for rate, ch in RECORDING_SAMPLE_CONFIGS:
                try:
                    s = pa.open(
                        rate=rate, channels=ch, format=pyaudio.paInt16,
                        input=True, input_device_index=device_index,
                        frames_per_buffer=4096, stream_callback=_callback,
                    )
                    s.start_stream()
                    return s, rate, ch
                except Exception:
                    continue
            return None, None, None

        configured_name = getattr(self.main_window, "effective_input_device_name", "") or ""

        # Everything up to here is cheap. find_input_device_index() and
        # pa.open() are not: both talk to the audio driver and can block for
        # seconds, and they used to run right here on the wx thread — the
        # window (and the screen reader reading it) froze for the duration.
        self._recording_starting = True
        self._recording_open_token += 1
        my_token = self._recording_open_token

        def _bg_open_stream():
            # An exception escaping this function would die unseen in a daemon
            # thread and take the wx.CallAfter with it, leaving
            # _recording_starting stuck True — and both entry points
            # (_on_record_voice_button, _on_ctrl_r_shortcut) refuse to start
            # while it is, so the record control would go dead for the rest of
            # the session. _on_stream_opened() is the only thing that clears
            # the flag, so it is scheduled from a finally and runs either way.
            stream = rate = ch = None
            try:
                input_device_index = (
                    find_input_device_index(configured_name, pa) if configured_name else None
                )
                stream, rate, ch = _try_open(input_device_index)
                if stream is None and input_device_index is not None:
                    stream, rate, ch = _try_open(None)

                if stream is None:
                    # Same last resort as ConversationsPanel, and kept
                    # deliberately identical to it: _try_open(None) only
                    # covers the default host API's default device, so a
                    # microphone that refuses MME but answers on WASAPI is
                    # still reachable by index. Posting a voice status and
                    # sending a voice message have no reason to disagree
                    # about which microphones exist — this panel already
                    # drifted behind the other one once.
                    for idx in fallback_input_device_indices(pa, exclude=(input_device_index,)):
                        stream, rate, ch = _try_open(idx)
                        if stream is not None:
                            logging.info(
                                "[status audio] Default input device failed; recording via "
                                "enumerated device index %s instead.", idx,
                            )
                            break
            except Exception:
                logging.exception(
                    "[status audio] Failed to open the recording stream (device=%r).",
                    configured_name,
                )
            finally:
                wx.CallAfter(_on_stream_opened, stream, rate, ch)

        def _on_stream_opened(stream, rate, ch):
            # Discard the result if the panel was closed or the recording
            # discarded while the stream was still opening — otherwise a
            # stream nobody asked for any more starts capturing in silence.
            if my_token != self._recording_open_token:
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                return

            self._recording_starting = False

            if stream is None:
                # voice_recording_unavailable means "PyAudio isn't installed"
                # (see the top of _start_voice_recording) — reused here it
                # pointed at the wrong cause entirely, since PyAudio is
                # plainly present if we got as far as trying to open a
                # stream. The device-specific message tells the user the one
                # thing that is actionable: check the mic and its Windows
                # permission.
                logging.warning(
                    "[status audio] No input stream could be opened — recording not started."
                )
                wx.MessageBox(
                    self.main_window.i18n.t("voice_recording_device_failed"),
                    self.main_window.app_name,
                    wx.OK | wx.ICON_WARNING, self,
                )
                return

            self._recording_stream   = stream
            self._recording_rate     = rate
            self._recording_channels = ch
            self._is_recording       = True

            if hasattr(self.main_window, "voicemsg_startrecording_sound"):
                self.main_window.voicemsg_startrecording_sound.play()

            i18n = self.main_window.i18n
            self._voice_status_lbl.SetLabel(i18n.t("recording_in_progress"))
            self._voice_close_btn.SetLabel(i18n.t("discard_voice_message"))
            self._voice_close_btn.Show()
            self._voice_start_btn.Hide()
            self._voice_pause_btn.SetLabel(i18n.t("pause_recording"))
            self._voice_pause_btn.Show()
            self._voice_send_btn.SetLabel(i18n.t("send_voice_message"))
            self._voice_send_btn.Show()
            self.Layout()
            self._voice_send_btn.SetFocus()
            self._silence_send_voice_focus_if_enabled()

        threading.Thread(target=_bg_open_stream, daemon=True).start()

    def _silence_send_voice_focus_if_enabled(self):
        settings = self.main_window.settings
        silence_while_recording = settings.get("speech_content", {}).get(
            "silence_while_recording", False
        )
        extended_enabled = settings.get("accessibility", {}).get(
            "extended_sr_compat_enabled", True
        )
        if not silence_while_recording and extended_enabled:
            return
        speak_output = getattr(self.main_window, "speak_output", None)
        silence_focus = getattr(speak_output, "silence_screen_reader_focus", None)
        if not callable(silence_focus):
            return
        silence_focus()
        wx.CallAfter(silence_focus)
        wx.CallLater(80, silence_focus)

    def _toggle_pause_voice_recording(self, event):
        if not self._is_recording:
            return
        self._recording_paused = not self._recording_paused
        if hasattr(self.main_window, "voicemsg_pauserecording_sound"):
            try:
                self.main_window.voicemsg_pauserecording_sound.play()
            except Exception:
                pass
        i18n = self.main_window.i18n
        if self._recording_paused:
            self._voice_pause_btn.SetLabel(i18n.t("resume_recording"))
            self._voice_status_lbl.SetLabel(i18n.t("recording_paused"))
            self._voice_play_btn.Show()
        else:
            self._stop_recorded_audio_preview()
            self._voice_play_btn.Hide()
            self._voice_pause_btn.SetLabel(i18n.t("pause_recording"))
            self._voice_status_lbl.SetLabel(i18n.t("recording_in_progress"))
        self.Layout()
        self._silence_send_voice_focus_if_enabled()

    def _toggle_play_recorded_audio(self, event):
        """Play or stop the stable snapshot captured before the pause."""
        if not self._is_recording or not self._recording_paused:
            return
        if self._recorded_audio_sound is not None:
            self._stop_recorded_audio_preview()
            return
        if not self._recording_frames:
            return

        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(self._recording_channels)
                wf.setsampwidth(2)  # capture is always 16-bit PCM
                wf.setframerate(self._recording_rate)
                wf.writeframes(b"".join(self._recording_frames))
            self._recorded_audio_temp_path = tmp.name
            sound = sl_stream.FileStream(file=tmp.name)
            sound.play()
        except Exception as exc:
            logging.warning("[status audio] Failed to preview recording: %s", exc)
            self._cleanup_recorded_audio_temp_file()
            return

        self._recorded_audio_sound = sound
        self._voice_play_btn.SetLabel(
            self.main_window.i18n.t("stop_recorded_audio_playback")
        )
        self._recorded_audio_timer.Start(300)

    def _on_recorded_audio_timer(self, event):
        if (
            self._recorded_audio_sound is None
            or not self._recorded_audio_sound.is_playing
        ):
            self._stop_recorded_audio_preview()

    def _stop_recorded_audio_preview(self):
        self._recorded_audio_timer.Stop()
        if self._recorded_audio_sound is not None:
            try:
                self._recorded_audio_sound.stop()
            except Exception:
                pass
            self._recorded_audio_sound = None
        self._cleanup_recorded_audio_temp_file()
        if hasattr(self, "_voice_play_btn"):
            self._voice_play_btn.SetLabel(
                self.main_window.i18n.t("play_recorded_audio")
            )

    def _cleanup_recorded_audio_temp_file(self):
        if self._recorded_audio_temp_path is not None:
            try:
                os.unlink(self._recorded_audio_temp_path)
            except Exception:
                pass
            self._recorded_audio_temp_path = None

    def _stop_recording_stream(self):
        if self._recording_stream is not None:
            try:
                self._recording_stream.stop_stream()
                self._recording_stream.close()
            except Exception:
                pass
            self._recording_stream = None

    def _on_close_voice_panel(self, event):
        # Bump the token so a stream still opening on a background thread (see
        # _start_voice_recording) is closed and discarded when it arrives,
        # instead of starting to capture into a panel the user just dismissed.
        self._recording_open_token += 1
        self._recording_starting = False
        if self._is_recording and hasattr(self.main_window, "voicemsg_discard_sound"):
            try:
                self.main_window.voicemsg_discard_sound.play()
            except Exception:
                pass
        self._stop_recorded_audio_preview()
        self._stop_recording_stream()
        self._recording_frames = []
        self._is_recording = False
        self._recording_paused = False
        self._leave_status_composer()

    def _on_send_voice_status(self, event):
        if not self._is_recording:
            return
        if hasattr(self.main_window, "voicemsg_send_sound"):
            try:
                self.main_window.voicemsg_send_sound.play()
            except Exception:
                pass
        self._stop_recorded_audio_preview()
        self._stop_recording_stream()
        self._is_recording = False
        self._recording_paused = False
        self._leave_status_composer()

        if not self._recording_frames:
            return

        pcm_data = b"".join(self._recording_frames)
        self._recording_frames = []

        fd, temp_wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with wave.open(temp_wav, "wb") as wf:
                wf.setnchannels(self._recording_channels)
                wf.setsampwidth(self._recording_pa.get_sample_size(pyaudio.paInt16))
                wf.setframerate(self._recording_rate)
                wf.writeframes(pcm_data)
        except Exception as exc:
            logging.error("[status audio] Failed to write recorded WAV: %s", exc)
            try:
                os.unlink(temp_wav)
            except Exception:
                pass
            return

        threading.Thread(
            target=self._send_status_voice_bg,
            args=(temp_wav,),
            kwargs={"is_temp_file": True},
            daemon=True,
        ).start()

    def _send_status_voice_bg(self, path: str, is_temp_file: bool = False, report_result: bool = True) -> bool:
        """Background: convert *path* to OGG/Opus (WhatsApp's own voice-
        message codec — main_window._convert_wav_to_ogg() despite the name
        just runs it through ffmpeg, which reads the real container/codec
        rather than trusting the extension, so this also works for a
        picked .mp3/.m4a/.aac file from _on_choose_media_status(), not just
        a WAV recorded here) and post it as a voice status via
        /send-status-voice-base64 (see messageController.ts's
        sendStatusVoice64() — mirrors send_audio_message()'s own
        send-voice-base64 call in main.py).

        *is_temp_file* must only be True for a file WE created (the
        recorded WAV) — deleting *path* unconditionally used to also
        delete the user's own picked file (e.g. an .mp3 chosen from the
        media picker) right out from under them.

        *report_result* controls whether a failure pops its own MessageBox
        here. _send_all_media_statuses_bg() passes False and aggregates
        instead — one popup per file used to stack into a flood of blocking
        dialogs when several files in a batch failed at once (same failure
        mode already fixed for save_data(), see main.py's
        _SAVE_ERROR_DIALOG_COOLDOWN comment).
        """
        mw = self.main_window
        ogg_path = mw._convert_wav_to_ogg(path)
        if is_temp_file:
            try:
                os.unlink(path)
            except Exception:
                pass
        if not ogg_path or not os.path.isfile(ogg_path):
            logging.error("[status audio] Failed to convert %s to OGG/Opus", path)
            if report_result:
                wx.CallAfter(
                    wx.MessageBox,
                    mw.i18n.t("audio_convert_failed"),
                    mw.app_name,
                    wx.OK | wx.ICON_ERROR,
                )
            return False
        try:
            with open(ogg_path, "rb") as fh:
                audio_b64 = base64.b64encode(fh.read()).decode("utf-8")
        except Exception as exc:
            # try/finally with no except let this propagate. Harmless while
            # this method was a thread target on its own, but
            # _send_all_media_statuses_bg() now calls it inside a loop: the
            # exception tore out of the loop, so the files after this one were
            # never even attempted AND the aggregate failure dialog at the end
            # of that loop was never reached — the batch just stopped, in
            # total silence, on a background thread. Report it like the
            # image/video path already does (_send_media_status_bg) and let
            # the batch carry on.
            logging.error("[status audio] Failed to read/encode %s: %s", ogg_path, exc)
            if report_result:
                wx.CallAfter(
                    wx.MessageBox,
                    mw.i18n.t("status_error"),
                    mw.app_name,
                    wx.OK | wx.ICON_ERROR,
                )
            return False
        finally:
            try:
                os.unlink(ogg_path)
            except Exception:
                pass

        url = f"{mw.wpp_server}:{mw.wpp_port}/api/{mw.token}/send-status-voice-base64"
        headers = {"Authorization": f"Bearer {mw.token}", "Content-Type": "application/json"}
        payload = {"base64Ptt": f"data:audio/ogg;codecs=opus;base64,{audio_b64}"}
        try:
            resp = api_post(url, json=payload, headers=headers, timeout=60)
            ok   = resp.status_code in (200, 201)
            err_msg = "" if ok else f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:
            ok = False
            err_msg = str(exc)

        if ok:
            wx.CallAfter(self._on_status_sent)
        else:
            logging.error("[status audio] send-status-voice-base64 failed: %s", err_msg)
            if report_result:
                wx.CallAfter(
                    wx.MessageBox,
                    mw.i18n.t("status_error"),
                    mw.app_name,
                    wx.OK | wx.ICON_ERROR,
                )
        return ok

    # ── Send text status ─────────────────────────────────────────────────────

    def _on_send_text_status(self, event):
        text    = normalize_line_separators(self._post_text_field.GetValue()).strip()
        caption = normalize_line_separators(self._caption_field.GetValue()).strip()
        if not text and not caption:
            return
        content = text or caption
        threading.Thread(
            target=self._send_text_status_bg,
            args=(content,),
            daemon=True,
        ).start()

    def _send_text_status_bg(self, text: str):
        """POST /api/{session}/send-text-storie (WPPConnect Server)."""
        mw  = self.main_window
        url = f"{mw.wpp_server}:{mw.wpp_port}/api/{mw.token}/send-text-storie"
        headers = {"Authorization": f"Bearer {mw.token}", "Content-Type": "application/json"}
        payload = {
            "text": text,
            "options": {
                "backgroundColor": "#25D366",
                "font": 2,
            }
        }
        try:
            resp = api_post(url, json=payload, headers=headers, timeout=60)
            ok   = resp.status_code in (200, 201)
            logging.info(
                "[status_post] POST %s -> HTTP %s, body=%.300s",
                url, resp.status_code, (resp.text or "")[:300],
            )
            if ok:
                # Guard against the false-success path: with the status.layer.js
                # async patch the server now surfaces the real post result, and
                # a rejected status arrives as HTTP 201 wrapping
                # sendMsgResult.messageSendResult = "ERROR_UNKNOWN" (ack stays
                # 0 — WhatsApp never accepted it). Any of those must be
                # reported as an error instead of "posted".
                try:
                    if _post_was_rejected(resp.json()):
                        ok = False
                except Exception:
                    pass
        except Exception as exc:
            ok = False
            logging.warning("[status_post] POST failed for %s: %s",
                            redact_api_url(url), exc)
        if ok:
            wx.CallAfter(self._on_status_sent)
        else:
            wx.CallAfter(
                wx.MessageBox,
                mw.i18n.t("status_error"),
                mw.app_name,
                wx.OK | wx.ICON_ERROR,
            )

    def _on_status_sent(self):
        self._leave_status_composer()
        self.main_window.output(self.main_window.i18n.t("status_posted"))
        threading.Thread(target=self._load_statuses, daemon=True).start()

    # ── Send media status ────────────────────────────────────────────────────

    def _on_add_more_media_files(self, event):
        i18n = self.main_window.i18n
        wildcard = (
            f"{i18n.t('status_photos_videos')} "
            "(*.jpg;*.jpeg;*.png;*.gif;*.webp;*.mp4;*.avi;*.mov;*.mkv)"
            "|*.jpg;*.jpeg;*.png;*.gif;*.webp;*.mp4;*.avi;*.mov;*.mkv"
            f"|{i18n.t('attachment_document')} (*.*)|*.*"
        )
        dlg = wx.FileDialog(
            self,
            message=i18n.t("status_photos_videos"),
            wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        )
        if dlg.ShowModal() == wx.ID_OK:
            self._selected_media_paths.extend(dlg.GetPaths())
            self._rebuild_media_attachment_list()
            self.Layout()
        dlg.Destroy()

    def _rebuild_media_attachment_list(self):
        """Rebuild the per-file remove-buttons to match _selected_media_paths."""
        i18n  = self.main_window.i18n
        panel = self._media_attachments_list_panel
        sizer = self._media_attachments_list_sizer
        for child in list(panel.GetChildren()):
            child.Destroy()
        sizer.Clear()
        for path in self._selected_media_paths:
            filename = os.path.basename(path)
            btn = wx.Button(
                panel,
                label=f"{i18n.t('remove_attachment')} {filename}",
            )
            btn.Bind(
                wx.EVT_BUTTON,
                lambda evt, p=path: self._on_remove_media_attachment(p),
            )
            sizer.Add(btn, 0, wx.BOTTOM, 3)
        panel.Layout()
        if self._media_post_panel.IsShown():
            self._media_post_panel.Layout()
            self.Layout()

    def _on_remove_media_attachment(self, path: str):
        """Remove one selected file and rebuild the list (or close the panel)."""
        self._selected_media_paths = [
            p for p in self._selected_media_paths if p != path
        ]
        if not self._selected_media_paths:
            self._on_close_media_panel(None)
        else:
            self._rebuild_media_attachment_list()

    def _on_send_media_status(self, event):
        if not self._selected_media_paths:
            return
        caption = normalize_line_separators(self._media_caption_field.GetValue()).strip()
        paths = list(self._selected_media_paths)
        threading.Thread(
            target=self._send_all_media_statuses_bg,
            args=(paths, caption),
            daemon=True,
        ).start()

    def _send_all_media_statuses_bg(self, paths: list, caption: str):
        """Send every file in *paths* sequentially, then report once.

        Each per-file helper is called with report_result=False so a batch
        where several files fail doesn't stack one blocking MessageBox per
        failure — that used to flood the screen with "status_error" dialogs
        one after another. Failures are still logged individually by the
        helpers; only the popup is deferred to a single summary here.

        Every call is additionally wrapped: a helper that raises instead of
        returning False would otherwise tear out of this loop, skipping every
        remaining file AND the summary dialog below — the batch would just
        stop, silently, on a background thread. That really happened via
        _send_status_voice_bg()'s try/finally-with-no-except; it is fixed at
        the source too, but the loop shouldn't depend on each helper
        remembering to catch everything.
        """
        mw = self.main_window
        failures = 0
        for path in paths:
            ext = os.path.splitext(path)[1].lower()
            try:
                if ext in (".mp3", ".ogg", ".wav", ".m4a", ".aac"):
                    # A picked audio file goes through the real voice-status
                    # path (transcodes to OGG/Opus via ffmpeg first) — same
                    # endpoint a recorded voice status uses, not the image/
                    # video one below, which has no audio branch at all. Voice
                    # notes don't carry a caption in the official client either,
                    # so it's intentionally dropped here.
                    ok = self._send_status_voice_bg(path, report_result=False)
                else:
                    ok = self._send_media_status_bg(path, caption, report_result=False)
            except Exception:
                logging.exception("[status] Unexpected failure sending %s as status", path)
                ok = False
            if not ok:
                failures += 1
        if failures:
            wx.CallAfter(
                wx.MessageBox,
                f"{mw.i18n.t('status_error')} ({failures}/{len(paths)})",
                mw.app_name,
                wx.OK | wx.ICON_ERROR,
            )

    def _send_media_status_bg(self, path: str, caption: str, report_result: bool = True) -> bool:
        mw = self.main_window
        ext      = os.path.splitext(path)[1].lower()
        mimetype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ext in (".mp4", ".mov", ".avi", ".mkv"):
            media_type = "video"
        elif ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            media_type = "image"
        else:
            logging.error("[status media] Unsupported file extension for status: %s", path)
            if report_result:
                wx.CallAfter(
                    wx.MessageBox,
                    mw.i18n.t("status_error"),
                    mw.app_name,
                    wx.OK | wx.ICON_ERROR,
                )
            return False

        try:
            with open(path, "rb") as fh:
                data_b64 = base64.b64encode(fh.read()).decode("utf-8")
        except Exception as exc:
            logging.error("[status media] Failed to read/encode %s: %s", path, exc)
            if report_result:
                wx.CallAfter(
                    wx.MessageBox,
                    mw.i18n.t("status_error"),
                    mw.app_name,
                    wx.OK | wx.ICON_ERROR,
                )
            return False

        endpoint = "send-image-storie" if media_type == "image" else "send-video-storie"
        url = f"{mw.wpp_server}:{mw.wpp_port}/api/{mw.token}/{endpoint}"
        headers = {"Authorization": f"Bearer {mw.token}", "Content-Type": "application/json"}
        # statusController.ts's sendImageStorie()/sendVideoStorie() (real,
        # unmodified upstream — `const { path } = req.body`) only ever read
        # a "path" field — it accepts either a real filesystem path or,
        # per sender.layer.js's sendImageStatus()/sendVideoStatus(), a full
        # `data:...;base64,...` URI interchangeably. This used to send the
        # payload under a "base64" key instead, which that handler never
        # reads at all — every status image/video post from the media
        # picker silently failed with pathFile undefined.
        payload = {
            "path": f"data:{mimetype};base64,{data_b64}",
            "caption": caption,
        }
        try:
            resp = api_post(url, json=payload, headers=headers, timeout=60)
            ok   = resp.status_code in (200, 201)
            if not ok:
                logging.warning(
                    "[status media] %s failed: HTTP %s: %s",
                    endpoint, resp.status_code, (resp.text or "")[:200],
                )
        except Exception as exc:
            ok = False
            logging.warning("[status media] %s failed: %s", endpoint, exc)
        if ok:
            wx.CallAfter(self._on_status_sent)
        elif report_result:
            wx.CallAfter(
                wx.MessageBox,
                mw.i18n.t("status_error"),
                mw.app_name,
                wx.OK | wx.ICON_ERROR,
            )
        return ok

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
