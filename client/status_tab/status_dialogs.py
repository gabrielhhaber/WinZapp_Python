"""The Status tab's two dialogs: who reacted to a status, and my own status.

Moved verbatim out of status_panel.py, which re-exports both.
"""

import logging
import os
import tempfile
import threading
import wx
from ui.accessible import (
    AccessibleStatusNext,
    AccessibleStatusPrev,
)
from core.video_player import VideoPlayer
from status_tab.status_rules import (
    _download_status_media,
    _status_content_label,
)
from core.utils import format_number
from status_tab.my_status_transcription import MyStatusTranscriptionKeys


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


class MyStatusDialog(MyStatusTranscriptionKeys, wx.Dialog):
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
            # Alt+Shift+T / N / Delete: transcribing the status being shown
            # (status_tab/my_status_transcription.py).
            self._create_transcription_accelerators()

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

    def _content_label_text(self) -> str:
        """"Status 1 of 3: Voice message, transcription: …" for the status
        being shown; the transcription only when it was asked into the line."""
        i18n   = self._mw.i18n
        status = self._statuses[self._current]
        msg_type = status.get("messageType", "")
        msg_obj  = status.get("message") or {}
        content  = _status_content_label(msg_type, msg_obj, i18n, getattr(self._mw, "settings", None))
        content += self._own_status_inline_suffix(status, i18n)
        nav_info = i18n.t("status_of").format(
            current=self._current + 1, total=len(self._statuses))
        return f"{nav_info}: {content}"

    def _update_content(self):
        if not self._statuses:
            return
        i18n   = self._mw.i18n
        status = self._statuses[self._current]
        msg_type = status.get("messageType", "")

        label = self._content_label_text()
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
