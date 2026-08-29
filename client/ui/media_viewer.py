"""Accessible, unified media viewer for conversation media and statuses."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from typing import Callable, Optional

import wx

from core.utils import get_downloads_folder, is_voice_message
from core.video_player import VideoPlayer
from ui.accessible import AccessibleStatusPrev, AccessibleStatusNext, AccessibleSaveAs, AccessibleMediaViewerSeekBack, AccessibleMediaViewerSeekForward, AccessibleMediaBitmapPanel


class CenteredBitmapPanel(wx.Panel):
    """Panel that draws a bitmap centered without stretching or clipping it."""

    def __init__(self, parent):
        super().__init__(parent, style=wx.BORDER_NONE)
        # Required by AutoBufferedPaintDC on Windows and also reduces flicker
        # while video frames are repainted.
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._bitmap = None
        self._source_image = None
        self.SetBackgroundColour(wx.BLACK)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, self._on_size)

    def SetBitmap(self, bitmap):
        """Display a video frame and scale it to the viewer area.

        VideoPlayer decodes frames at a modest fixed width for CPU efficiency.
        The viewer itself may be much larger, so keep the frame as a source
        image and fit it to the panel instead of leaving a tiny 480px picture
        centered in a maximized window.
        """
        self._source_image = (
            bitmap.ConvertToImage() if bitmap and bitmap.IsOk() else None
        )
        self._fit_source_image()

    def SetImage(self, image):
        """Store a still image and refit it whenever the panel resizes."""
        self._source_image = image.Copy() if image and image.IsOk() else None
        self._fit_source_image()

    def GetBitmap(self):
        return self._bitmap

    def Clear(self):
        self._source_image = None
        self._bitmap = None
        self.Refresh(False)

    def _fit_source_image(self):
        image = self._source_image
        if image is None or not image.IsOk():
            return
        target_w, target_h = self.GetClientSize()
        if target_w <= 1 or target_h <= 1:
            self._bitmap = wx.Bitmap(image)
            self.Refresh(False)
            return

        # The dedicated viewer should use the available screen area in both
        # directions: shrink oversized media and enlarge smaller media, always
        # preserving the original aspect ratio and never cropping it.
        ratio = min(target_w / image.GetWidth(), target_h / image.GetHeight())
        shown_w = max(1, int(image.GetWidth() * ratio))
        shown_h = max(1, int(image.GetHeight() * ratio))
        if (shown_w, shown_h) == (image.GetWidth(), image.GetHeight()):
            self._bitmap = wx.Bitmap(image)
        else:
            scaled = image.Scale(shown_w, shown_h, wx.IMAGE_QUALITY_HIGH)
            self._bitmap = wx.Bitmap(scaled)
        self.Refresh(False)

    def _on_size(self, event):
        if self._source_image is not None:
            self._fit_source_image()
        else:
            self.Refresh(False)
        event.Skip()

    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
        dc.Clear()
        bitmap = self._bitmap
        if bitmap is None or not bitmap.IsOk():
            return
        width, height = self.GetClientSize()
        bw, bh = bitmap.GetWidth(), bitmap.GetHeight()
        x = max(0, (width - bw) // 2)
        y = max(0, (height - bh) // 2)
        dc.DrawBitmap(bitmap, x, y, True)


class MediaViewerDialog(wx.Dialog):
    """Modal media viewer shared by conversations and the Status panel.

    Each item is a dict. Supported keys:
      kind: image | video | audio | text
      local_path: already available local file (optional)
      loader: callable returning bytes or a local path (optional)
      extension / filename / caption / text / label
      from_me / status_id (status context metadata)

    Optional callbacks make the same dialog status-aware without coupling it
    to StatusPanel itself.
    """

    SPEEDS = (1.0, 1.5, 2.0)
    SLIDER_MAX = 1000
    SEEK_SECONDS = 10

    def __init__(
        self,
        parent,
        main_window,
        items: list[dict],
        start_index: int = 0,
        *,
        on_item_opened: Optional[Callable[[dict, int], None]] = None,
        is_liked: Optional[Callable[[dict], bool]] = None,
        on_like: Optional[Callable[[dict, Callable[[bool], None]], None]] = None,
        on_reply: Optional[Callable[[dict, str, Callable[[bool], None]], None]] = None,
    ):
        self.main_window = main_window
        self.i18n = main_window.i18n
        self.items = items or []
        self.index = max(0, min(start_index, len(self.items) - 1)) if self.items else 0
        self._on_item_opened_cb = on_item_opened
        self._is_liked_cb = is_liked
        self._on_like_cb = on_like
        self._on_reply_cb = on_reply

        super().__init__(
            parent,
            title=self.i18n.t("media_viewer_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )

        self._owned_paths: set[str] = set()
        self._loaded_paths: dict[int, str] = {}
        self._loading_generation = 0
        self._slider_dragging = False
        self._speed_index = 0
        self._current_kind = ""
        self._current_path = ""

        self._build_ui()
        self._create_accelerators()
        self._player = VideoPlayer(self.main_window, self._bitmap_panel)
        self._progress_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_progress_timer, self._progress_timer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_CLOSE, self._on_close)

        self.SetMinSize((720, 520))
        self.SetSize((1000, 720))
        self.CentreOnParent()
        self.Maximize(True)
        self._show_item(self.index)

    # ── UI ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = wx.BoxSizer(wx.VERTICAL)

        self._header = wx.StaticText(self, label="")
        root.Add(self._header, 0, wx.EXPAND | wx.ALL, 8)

        self._content_panel = wx.Panel(self)
        self._content_panel.SetAccessible(AccessibleMediaBitmapPanel(self._get_current_media_label))
        content_sizer = wx.BoxSizer(wx.VERTICAL)
        self._bitmap_panel = CenteredBitmapPanel(self._content_panel)
        self._bitmap_panel.SetAccessible(AccessibleMediaBitmapPanel(self._get_current_media_label))
        content_sizer.Add(self._bitmap_panel, 1, wx.EXPAND)

        self._text_ctrl = wx.TextCtrl(
            self._content_panel,
            value="",
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self._text_ctrl.SetName(self.i18n.t("media_viewer_text_status"))
        content_sizer.Add(self._text_ctrl, 1, wx.EXPAND)

        self._loading_label = wx.StaticText(
            self._content_panel, label=self.i18n.t("media_viewer_loading")
        )
        content_sizer.Add(self._loading_label, 0, wx.ALIGN_CENTER | wx.ALL, 12)

        self._content_panel.SetSizer(content_sizer)
        root.Add(self._content_panel, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self._caption_ctrl = wx.TextCtrl(
            self,
            value="",
            style=wx.TE_MULTILINE | wx.TE_READONLY,
            size=(-1, 70),
        )
        self._caption_ctrl.SetName(self.i18n.t("media_viewer_caption"))
        root.Add(self._caption_ctrl, 0, wx.EXPAND | wx.ALL, 8)

        # Video/audio transport controls.
        self._transport_panel = wx.Panel(self)
        transport = wx.BoxSizer(wx.VERTICAL)
        seek_row = wx.BoxSizer(wx.HORIZONTAL)
        self._position_label = wx.StaticText(
            self._transport_panel, label=self.i18n.t("media_viewer_position")
        )
        seek_row.Add(self._position_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._position_slider = wx.Slider(
            self._transport_panel, minValue=0, maxValue=self.SLIDER_MAX, value=0
        )
        self._position_slider.SetName(self.i18n.t("media_viewer_position"))
        self._position_slider.Bind(wx.EVT_SCROLL_THUMBTRACK, self._on_slider_drag)
        self._position_slider.Bind(wx.EVT_SCROLL_THUMBRELEASE, self._on_slider_release)
        self._position_slider.Bind(wx.EVT_SCROLL_CHANGED, self._on_slider_release)
        seek_row.Add(self._position_slider, 1, wx.ALIGN_CENTER_VERTICAL)
        self._time_label = wx.StaticText(self._transport_panel, label="00:00 / 00:00")
        seek_row.Add(self._time_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        transport.Add(seek_row, 0, wx.EXPAND | wx.BOTTOM, 6)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        # Seek back/forward sit right around Pause in both the visual and tab
        # order (back before, forward after) — Alt+V/Alt+A, reported via their
        # own Accessible like every other fixed-shortcut button in the app
        # (ui/accessible.py), not a locale-dependent mnemonic, since these two
        # keys are meant to work the same way regardless of the app's language.
        self._seek_back_btn = wx.Button(
            self._transport_panel, label=self.i18n.t("media_viewer_seek_back")
        )
        self._seek_back_btn.SetAccessible(AccessibleMediaViewerSeekBack())
        self._seek_back_btn.Bind(wx.EVT_BUTTON, self._on_seek_back)
        actions.Add(self._seek_back_btn, 0, wx.RIGHT, 8)

        self._play_btn = wx.Button(
            self._transport_panel, label=self.i18n.t("media_viewer_pause")
        )
        self._play_btn.Bind(wx.EVT_BUTTON, self._on_play_pause)
        actions.Add(self._play_btn, 0, wx.RIGHT, 8)

        self._seek_forward_btn = wx.Button(
            self._transport_panel, label=self.i18n.t("media_viewer_seek_forward")
        )
        self._seek_forward_btn.SetAccessible(AccessibleMediaViewerSeekForward())
        self._seek_forward_btn.Bind(wx.EVT_BUTTON, self._on_seek_forward)
        actions.Add(self._seek_forward_btn, 0, wx.RIGHT, 8)

        volume_label = wx.StaticText(
            self._transport_panel, label=self.i18n.t("media_viewer_volume")
        )
        actions.Add(volume_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._volume_slider = wx.Slider(
            self._transport_panel, minValue=0, maxValue=100, value=100, size=(180, -1)
        )
        self._volume_slider.SetName(self.i18n.t("media_viewer_volume"))
        self._volume_slider.Bind(wx.EVT_SLIDER, self._on_volume)
        actions.Add(self._volume_slider, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)

        self._speed_btn = wx.Button(self._transport_panel, label="")
        self._speed_btn.Bind(wx.EVT_BUTTON, self._on_speed)
        actions.Add(self._speed_btn, 0)
        transport.Add(actions, 0, wx.EXPAND)
        self._transport_panel.SetSizer(transport)
        root.Add(self._transport_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # Status-only actions. They stay hidden for conversation media.
        self._status_actions = wx.Panel(self)
        status_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._like_btn = wx.Button(self._status_actions, label=self.i18n.t("status_like"))
        self._like_btn.Bind(wx.EVT_BUTTON, self._on_like)
        status_sizer.Add(self._like_btn, 0, wx.RIGHT, 8)
        # A wx.TextCtrl's SetName() alone is not reliably announced by NVDA/
        # JAWS for an editable (non-read-only) control on Windows — unlike
        # the read-only _text_ctrl/_caption_ctrl above, a plain EDIT control's
        # accessible Name is normally supplied by an adjacent static label,
        # which is how StatusPanel's own classic reply field
        # (_reply_label + _reply_field) already gets read correctly. Kept
        # here too, alongside SetName(), so the field is announced whichever
        # of the two the screen reader actually uses.
        self._reply_label = wx.StaticText(
            self._status_actions, label=self.i18n.t("status_reply_label")
        )
        status_sizer.Add(self._reply_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._reply_field = wx.TextCtrl(self._status_actions, style=wx.TE_PROCESS_ENTER)
        self._reply_field.SetName(self.i18n.t("status_reply_label"))
        status_sizer.Add(self._reply_field, 1, wx.RIGHT, 8)
        self._reply_btn = wx.Button(
            self._status_actions, label=self.i18n.t("status_reply_send")
        )
        self._reply_btn.Bind(wx.EVT_BUTTON, self._on_reply)
        self._reply_field.Bind(wx.EVT_TEXT_ENTER, self._on_reply)
        status_sizer.Add(self._reply_btn, 0)
        self._status_actions.SetSizer(status_sizer)
        root.Add(self._status_actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        bottom = wx.BoxSizer(wx.HORIZONTAL)
        self._prev_btn = wx.Button(self, label=self.i18n.t("status_prev"))
        self._prev_btn.SetAccessible(AccessibleStatusPrev(self.i18n.t("accessible_ctrl_left")))
        self._prev_btn.Bind(wx.EVT_BUTTON, self._on_prev)
        bottom.Add(self._prev_btn, 0, wx.RIGHT, 8)
        self._next_btn = wx.Button(self, label=self.i18n.t("status_next"))
        self._next_btn.SetAccessible(AccessibleStatusNext(self.i18n.t("accessible_ctrl_right")))
        self._next_btn.Bind(wx.EVT_BUTTON, self._on_next)
        bottom.Add(self._next_btn, 0, wx.RIGHT, 8)
        bottom.AddStretchSpacer(1)
        self._save_btn = wx.Button(self, label=self.i18n.t("save_as"))
        self._save_btn.SetAccessible(AccessibleSaveAs())
        self._save_btn.Bind(wx.EVT_BUTTON, self._on_save)
        bottom.Add(self._save_btn, 0, wx.RIGHT, 8)
        self._close_btn = wx.Button(self, wx.ID_CANCEL, self.i18n.t("close"))
        self._close_btn.Bind(wx.EVT_BUTTON, self._on_close_button)
        bottom.Add(self._close_btn, 0)
        root.Add(bottom, 0, wx.EXPAND | wx.ALL, 8)

        self.SetSizer(root)
        self._update_speed_label()

    def _create_accelerators(self):
        """Keyboard shortcuts matching StatusPanel's own classic viewer
        (client/status_panel.py's _create_accelerators()) — this dialog
        used to only bind Escape and Space (via _on_char_hook), leaving
        prev/next/save reachable by mouse or Tab only, unlike every other
        equivalent action elsewhere in the app."""
        self.ID_CTRL_LEFT = wx.NewIdRef()
        self.ID_CTRL_RIGHT = wx.NewIdRef()
        self.ID_CTRL_SHIFT_S = wx.NewIdRef()
        self.ID_ALT_V = wx.NewIdRef()  # seek back 10s
        self.ID_ALT_A = wx.NewIdRef()  # seek forward 10s
        accel_tbl = wx.AcceleratorTable([
            (wx.ACCEL_CTRL, wx.WXK_LEFT, self.ID_CTRL_LEFT),
            (wx.ACCEL_CTRL, wx.WXK_RIGHT, self.ID_CTRL_RIGHT),
            # Same combo StatusPanel/ConversationsPanel already use for
            # "save as" — consistent muscle memory across every place media
            # can be saved from.
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("S"), self.ID_CTRL_SHIFT_S),
            (wx.ACCEL_ALT, ord("V"), self.ID_ALT_V),
            (wx.ACCEL_ALT, ord("A"), self.ID_ALT_A),
        ])
        self.SetAcceleratorTable(accel_tbl)
        self.Bind(wx.EVT_MENU, self._on_prev, id=self.ID_CTRL_LEFT)
        self.Bind(wx.EVT_MENU, self._on_next, id=self.ID_CTRL_RIGHT)
        self.Bind(wx.EVT_MENU, self._on_save, id=self.ID_CTRL_SHIFT_S)
        self.Bind(wx.EVT_MENU, self._on_seek_back, id=self.ID_ALT_V)
        self.Bind(wx.EVT_MENU, self._on_seek_forward, id=self.ID_ALT_A)

    # ── Item lifecycle ───────────────────────────────────────────────────

    def _get_current_media_label(self) -> str:
        kind = getattr(self, "_current_kind", "")
        if kind == "image":
            label = self.i18n.t("photo")
        elif kind == "video":
            label = self.i18n.t("video")
        elif kind == "audio":
            item = self._current_item()
            is_ptt = is_voice_message(item) or bool(item.get("is_ptt"))
            vm_mode = (self.main_window.settings.get("user_interface", {}) if hasattr(self, "main_window") and self.main_window and hasattr(self.main_window, "settings") else {}).get("voice_message_mode", "audio")
            label = self.i18n.t("message_type_voice_message") if (vm_mode == "voice_message" and is_ptt) else self.i18n.t("message_type_audio")
        elif kind == "text":
            label = self.i18n.t("media_viewer_text_status")
        else:
            label = ""
        return label[0].upper() + label[1:] if label else ""

    def _update_media_labels(self):
        label = self._get_current_media_label()
        if hasattr(self, "_bitmap_panel") and self._bitmap_panel:
            self._bitmap_panel.SetName(label)
        if hasattr(self, "_content_panel") and self._content_panel:
            self._content_panel.SetName(label)

    def _current_item(self) -> dict:
        if not self.items:
            return {}
        return self.items[self.index]

    def _show_item(self, index: int):
        if not self.items:
            return
        self.index = index % len(self.items)
        item = self._current_item()
        self._loading_generation += 1
        generation = self._loading_generation

        self._stop_current_media()
        self._current_kind = str(item.get("kind") or "text")
        self._update_media_labels()
        self._current_path = ""
        self._bitmap_panel.Clear()
        self._text_ctrl.SetValue("")
        self._caption_ctrl.SetValue(str(item.get("caption") or ""))
        self._caption_ctrl.Show(bool(item.get("caption")))

        label = str(item.get("label") or "")
        if len(self.items) > 1:
            nav = self.i18n.t("status_of").format(current=self.index + 1, total=len(self.items))
            label = f"{label} — {nav}" if label else nav
        self._header.SetLabel(label)

        self._prev_btn.Show(len(self.items) > 1)
        self._next_btn.Show(len(self.items) > 1)
        self._configure_status_actions(item)

        if self._current_kind == "text":
            self._bitmap_panel.Hide()
            self._loading_label.Hide()
            self._transport_panel.Hide()
            self._save_btn.Hide()
            self._text_ctrl.Show()
            self._text_ctrl.SetValue(str(item.get("text") or ""))
            self.Layout()
            self._notify_item_opened(item)
            self._text_ctrl.SetFocus()
            return

        self._text_ctrl.Hide()
        self._transport_panel.Hide()
        self._bitmap_panel.Show()
        self._save_btn.Show()

        path = item.get("local_path") or self._loaded_paths.get(self.index)
        if path and os.path.isfile(path):
            if item.get("owned_path"):
                self._owned_paths.add(path)
            self._loading_label.Hide()
            self._display_loaded_path(item, path)
            return

        loader = item.get("loader")
        if not callable(loader):
            self._show_error()
            return

        self._loading_label.SetLabel(self.i18n.t("media_viewer_loading"))
        self._loading_label.Show()
        self.Layout()

        def _load():
            try:
                result = loader()
                if getattr(self, "_cleaned_up", False) or not bool(self):
                    return
                if isinstance(result, (bytes, bytearray)):
                    suffix = str(item.get("extension") or "")
                    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                    tmp.write(bytes(result))
                    tmp.close()
                    path2 = tmp.name
                    owned = True
                elif isinstance(result, str) and os.path.isfile(result):
                    path2 = result
                    owned = bool(item.get("owned_path", False))
                else:
                    raise ValueError("media loader returned no usable data")
                if getattr(self, "_cleaned_up", False) or not bool(self):
                    if owned:
                        self._safe_unlink(path2)
                    return
                wx.CallAfter(self._on_loaded, generation, self.index, item, path2, owned)
            except Exception:
                if getattr(self, "_cleaned_up", False) or not bool(self):
                    return
                wx.CallAfter(self._on_load_failed, generation)

        threading.Thread(target=_load, daemon=True).start()

    def _on_loaded(self, generation: int, index: int, item: dict, path: str, owned: bool):
        if getattr(self, "_cleaned_up", False) or not bool(self):
            if owned:
                self._safe_unlink(path)
            return
        if generation != self._loading_generation or index != self.index:
            if owned:
                self._safe_unlink(path)
            return
        self._loaded_paths[index] = path
        if owned:
            self._owned_paths.add(path)
        try:
            self._loading_label.Hide()
            self._display_loaded_path(item, path)
        except (RuntimeError, wx.wxAssertionError, Exception) as exc:
            pass

    def _on_load_failed(self, generation: int):
        if getattr(self, "_cleaned_up", False) or not bool(self):
            return
        if generation != self._loading_generation:
            return
        try:
            self._show_error()
        except (RuntimeError, wx.wxAssertionError, Exception) as exc:
            pass

    def _display_loaded_path(self, item: dict, path: str):
        self._current_path = path
        self._update_media_labels()
        kind = self._current_kind
        if kind == "image":
            img = wx.Image(path)
            if not img.IsOk():
                self._show_error()
                return
            self._set_still_image(img)
            self._transport_panel.Hide()
        elif kind in ("video", "audio"):
            if kind == "audio":
                self._bitmap_panel.Clear()
                self._bitmap_panel.Hide()
            else:
                self._bitmap_panel.Show()
            self._transport_panel.Show()
            self._speed_index = 0
            self._update_speed_label()
            self._position_slider.SetValue(0)
            self._time_label.SetLabel("00:00 / 00:00")
            self._player.load_and_play(path, self.SPEEDS[self._speed_index])
            self._player.set_volume(self._volume_slider.GetValue() / 100.0)
            self._play_btn.SetLabel(self.i18n.t("media_viewer_pause"))
            self._progress_timer.Start(250)
        else:
            self._show_error()
            return
        self.Layout()
        self._notify_item_opened(item)
        self._play_btn.SetFocus() if kind in ("video", "audio") else self._close_btn.SetFocus()

    def _set_still_image(self, img: wx.Image):
        self._bitmap_panel.SetImage(img)

    def _notify_item_opened(self, item: dict):
        if self._on_item_opened_cb is not None:
            try:
                self._on_item_opened_cb(item, self.index)
            except Exception:
                pass

    def _configure_status_actions(self, item: dict):
        status_context = bool(item.get("status_id") or item.get("status"))
        can_interact = status_context and not bool(item.get("from_me"))
        self._status_actions.Show(
            can_interact
            and (self._on_like_cb is not None or self._on_reply_cb is not None)
        )
        self._like_btn.Show(can_interact and self._on_like_cb is not None)
        self._reply_label.Show(can_interact and self._on_reply_cb is not None)
        self._reply_field.Show(can_interact and self._on_reply_cb is not None)
        self._reply_btn.Show(can_interact and self._on_reply_cb is not None)
        self._reply_field.SetValue("")
        if self._is_liked_cb is not None and can_interact:
            try:
                liked = bool(self._is_liked_cb(item))
            except Exception:
                liked = False
            self._like_btn.SetLabel(
                self.i18n.t("status_unlike") if liked else self.i18n.t("status_like")
            )

    # ── Transport controls ───────────────────────────────────────────────

    def _on_play_pause(self, event):
        if not self._player.is_playing:
            if self._current_path:
                self._player.load_and_play(self._current_path, self.SPEEDS[self._speed_index])
                self._player.set_volume(self._volume_slider.GetValue() / 100.0)
                self._progress_timer.Start(250)
                self._play_btn.SetLabel(self.i18n.t("media_viewer_pause"))
            return
        self._player.toggle_pause()
        self._play_btn.SetLabel(
            self.i18n.t("media_viewer_play")
            if self._player.is_paused
            else self.i18n.t("media_viewer_pause")
        )

    def _on_slider_drag(self, event):
        self._slider_dragging = True
        event.Skip()

    def _on_slider_release(self, event):
        length = self._player.get_length()
        if length > 0:
            target = int(length * (self._position_slider.GetValue() / self.SLIDER_MAX))
            self._player.set_position(target)
        self._slider_dragging = False
        event.Skip()

    def _seek_relative(self, delta_seconds: float):
        """Seek the active video/audio by *delta_seconds* (negative = back),
        clamped to the track. Mirrors ConversationsPanel.seek_active_playback_by()."""
        if self._current_kind not in ("video", "audio"):
            return
        length = self._player.get_length()
        if length <= 0:
            return
        pos = self._player.get_position()
        delta_bytes = self._player.seconds_to_bytes(abs(delta_seconds))
        if delta_seconds < 0:
            new_pos = max(0, pos - delta_bytes)
        else:
            new_pos = min(length, pos + delta_bytes)
        self._player.set_position(new_pos)
        if not self._slider_dragging:
            value = int(max(0, min(self.SLIDER_MAX, new_pos * self.SLIDER_MAX / length)))
            self._position_slider.SetValue(value)

    def _on_seek_back(self, event):
        self._seek_relative(-self.SEEK_SECONDS)

    def _on_seek_forward(self, event):
        self._seek_relative(self.SEEK_SECONDS)

    def _on_volume(self, event):
        self._player.set_volume(self._volume_slider.GetValue() / 100.0)
        event.Skip()

    def _on_speed(self, event):
        self._speed_index = (self._speed_index + 1) % len(self.SPEEDS)
        self._player.set_speed(self.SPEEDS[self._speed_index])
        self._update_speed_label()

    def _update_speed_label(self):
        speed = self.SPEEDS[self._speed_index]
        self._speed_btn.SetLabel(self.i18n.t("media_viewer_speed").format(speed=f"{speed:g}"))

    def _on_progress_timer(self, event):
        if getattr(self, "_cleaned_up", False) or not bool(self):
            try:
                self._progress_timer.Stop()
            except Exception:
                pass
            return
        if not self._player.is_playing:
            try:
                self._progress_timer.Stop()
            except Exception:
                pass
            try:
                self._play_btn.SetLabel(self.i18n.t("media_viewer_play"))
            except Exception:
                pass
            return
        try:
            pos = self._player.get_position()
            length = self._player.get_length()
            if length > 0 and not self._slider_dragging:
                value = int(max(0, min(self.SLIDER_MAX, pos * self.SLIDER_MAX / length)))
                self._position_slider.SetValue(value)
            self._time_label.SetLabel(
                f"{self._format_time(self._player.bytes_to_seconds(pos))} / "
                f"{self._format_time(self._player.bytes_to_seconds(length))}"
            )
        except (RuntimeError, wx.wxAssertionError, Exception):
            pass

    @staticmethod
    def _format_time(seconds: float) -> str:
        seconds = max(0, int(seconds or 0))
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    # ── Status actions / navigation ──────────────────────────────────────

    def _on_prev(self, event):
        self._show_item((self.index - 1) % len(self.items))

    def _on_next(self, event):
        self._show_item((self.index + 1) % len(self.items))

    def _on_like(self, event):
        if self._on_like_cb is None:
            return
        item = self._current_item()
        self._like_btn.Disable()

        def _done(ok: bool):
            if getattr(self, "_cleaned_up", False):
                return
            self._like_btn.Enable()
            if ok:
                self._like_btn.SetLabel(self.i18n.t("status_unlike"))

        try:
            self._on_like_cb(item, _done)
        except Exception:
            self._like_btn.Enable()

    def _on_reply(self, event):
        if self._on_reply_cb is None:
            return
        text = self._reply_field.GetValue().strip()
        if not text:
            return
        self._reply_btn.Disable()

        def _done(ok: bool):
            if getattr(self, "_cleaned_up", False):
                return
            self._reply_btn.Enable()
            if ok:
                self._reply_field.SetValue("")
                self._reply_field.SetFocus()
                try:
                    self.main_window.output(self.i18n.t("status_reply_sent"))
                except Exception:
                    pass
            else:
                wx.MessageBox(
                    self.i18n.t("status_reply_error"),
                    self.main_window.app_name,
                    wx.OK | wx.ICON_ERROR,
                )

        self._on_reply_cb(self._current_item(), text, _done)

    # ── Save / close ─────────────────────────────────────────────────────

    def _on_save(self, event):
        path = self._current_path or self._loaded_paths.get(self.index, "")
        if not path or not os.path.isfile(path):
            return
        item = self._current_item()
        default_name = str(item.get("filename") or os.path.basename(path) or self.i18n.t("media_viewer_default_filename"))
        with wx.FileDialog(
            self,
            self.i18n.t("save_as"),
            defaultDir=get_downloads_folder(),
            defaultFile=default_name,
            wildcard=f"{self.i18n.t('all_files')} (*.*)|*.*",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            target = dlg.GetPath()
        try:
            shutil.copyfile(path, target)
        except Exception as exc:
            wx.MessageBox(str(exc), self.main_window.app_name, wx.OK | wx.ICON_ERROR)

    def _on_char_hook(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if key == wx.WXK_SPACE and self._current_kind in ("video", "audio"):
            # Do not steal Space from sliders/text fields.
            focus = wx.Window.FindFocus()
            if focus not in (self._position_slider, self._volume_slider, self._reply_field, self._text_ctrl):
                self._on_play_pause(None)
                return
        event.Skip()

    def _on_close_button(self, event):
        self.EndModal(wx.ID_CANCEL)

    def _on_close(self, event):
        self._cleanup()
        event.Skip()

    def Destroy(self):
        self._cleanup()
        return super().Destroy()

    def _stop_current_media(self):
        if hasattr(self, "_progress_timer"):
            self._progress_timer.Stop()
        if hasattr(self, "_player"):
            self._player.stop()

    def _cleanup(self):
        if getattr(self, "_cleaned_up", False):
            return
        self._cleaned_up = True
        # Invalidate background loaders before any native controls are
        # destroyed. Their CallAfter callbacks will then only discard owned
        # temp files instead of touching a closed dialog.
        self._loading_generation += 1
        self._stop_current_media()
        for path in list(self._owned_paths):
            self._safe_unlink(path)
        self._owned_paths.clear()

    def _show_error(self):
        self._loading_label.SetLabel(self.i18n.t("media_viewer_error"))
        self._loading_label.Show()
        self._transport_panel.Hide()
        self.Layout()

    @staticmethod
    def _safe_unlink(path: str):
        try:
            os.unlink(path)
        except OSError:
            pass
