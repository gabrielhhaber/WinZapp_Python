"""MediaFilesMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Moved verbatim out of ui/conversations.py. Methods run with ``self`` bound to
the ConversationsPanel instance, so every attribute set in
ConversationsPanel.__init__/init_UI is available here.
"""

import logging
import mimetypes
import os
import re
import subprocess
import tempfile
import threading
import time
import wx
from core.utils import (
    MEASURED_SECONDS_KEY,
    decrypt_bytes,
    video_seconds,
)
from ui.media_viewer import MediaViewerDialog
from ui.conversation_panel.media_paths import (
    _SAVEABLE_MESSAGE_TYPES,
    cached_media_path,
    reveal_file_in_folder,
    saved_media_path,
)
from app_paths import data_path
from datetime import datetime
from core.save_location import resolve_save_dialog_folder
from core.save_dialog_selection import schedule_deselect_extension


class MediaFilesMixin:
    """Media on disk: opening, the media viewer, video, saving, downloading and
    transfer progress.
    """

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
        self._action_describe_btn.Hide()
        button = getattr(self, "_action_show_in_folder_btn", None)
        if button is not None:
            button.Hide()
        self._action_download_btn.Hide()
        self._hide_media_transfer_gauge()
        # The gauge IS selection-scoped, and the comment that used to sit here
        # said the opposite while this very call contradicted it. One gauge
        # serves every transfer, so the only reading of it that means anything
        # is "the row you are on". Moving off hides it here; a transfer still
        # running on the row you move back to re-shows it on its next progress
        # tick, and _sync_pending_document_gauge() restores a pending upload
        # when the conversation is (re)opened. See _transfer_owns_gauge().
        self._buttons_container.Hide()
        self._contact_converse_btn.Hide()
        self._contact_save_btn.Hide()
        self._contact_msg_jid = None
        self._update_links_panel([])
        self._update_mentions_panel([])
        if self.conversation_panel.IsShown():
            self.conversation_panel.Layout()

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

    def _on_action_show_in_folder(self, event):
        """Reveal the focused Save As copy in File Explorer."""
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        self.show_message_in_folder(self._sorted_messages[index])

    def _saved_media_path(self, msg: dict) -> str:
        """Return a Save As copy, reconnecting rebuilt message dictionaries."""
        paths = getattr(self, "_saved_media_paths", None)
        if paths is None:
            paths = self._saved_media_paths = {}
        msg_id = (msg.get("key") or {}).get("id", "") if isinstance(msg, dict) else ""
        remembered = paths.get(msg_id, "") if msg_id else ""
        if os.path.isfile(remembered):
            # The mapping represents the latest completed Save As. A rebuilt
            # or previously unloaded message dict may still carry an older
            # path, so the remembered value deliberately wins.
            msg["_saved_media_path"] = remembered
            return remembered
        if msg_id:
            paths.pop(msg_id, None)
        direct = saved_media_path(msg)
        if direct and msg_id:
            paths[msg_id] = direct
        return direct

    def show_message_in_folder(self, msg: dict) -> bool:
        """Reveal *msg*'s existing local file and report why that failed."""
        path = self._saved_media_path(msg)
        if not path:
            unavailable_text = (
                self.main_window.i18n.t("show_in_folder_missing")
                if msg.get("_saved_media_path")
                else self.main_window.i18n.t("show_in_folder_save_first")
            )
            self.main_window.output(
                unavailable_text, interrupt=True
            )
            return False
        try:
            if reveal_file_in_folder(path):
                return True
            self.main_window.output(
                self.main_window.i18n.t("show_in_folder_missing"), interrupt=True
            )
            return False
        except OSError as exc:
            logging.warning("[Show in folder] could not reveal %s: %s", path, exc)
            self.main_window.output(
                self.main_window.i18n.t("show_in_folder_failed"), interrupt=True
            )
            return False

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
        base_name = os.path.splitext(default_file)[0]
        with wx.FileDialog(
            self,
            dlg_title,
            defaultDir=resolve_save_dialog_folder(self.main_window.settings),
            # Extension left off on purpose: the native Save dialog selects
            # the whole suggested name (extension included) for editing, so a
            # user who starts renaming loses the extension along with it
            # unless they retype it by hand. Windows re-appends it from the
            # wildcard's first filter when nothing is typed — that filter is
            # built from this same ext_clean whenever one was found above, so
            # this changes nothing about what actually gets saved. A no-op
            # when default_file had no extension to begin with.
            defaultFile=base_name,
            wildcard=wildcard,
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
        ) as dlg:
            # Belt and suspenders: Windows still visually selects the
            # extension it auto-completes into the box regardless of the
            # above — see core/save_dialog_selection.py for why and how.
            schedule_deselect_extension(base_name)
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
        media_path = cached_media_path(msg_type, msg_id)

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
            # Show in folder follows the copy the user explicitly chose, not
            # the encrypted .wzmedia/.msv cache that supplied its contents.
            msg["_saved_media_path"] = os.path.abspath(save_path)
            wx.CallAfter(self._on_media_saved_as, msg)
        except Exception as exc:
            wx.CallAfter(
                wx.MessageBox,
                str(exc),
                self.main_window.i18n.t("error").format(
                    app_name=self.main_window.app_name
                ),
                wx.OK | wx.ICON_ERROR,
            )

    def _on_media_saved_as(self, msg: dict) -> None:
        """Expose Show in folder immediately when Save As finishes."""
        saved_id = (msg.get("key") or {}).get("id", "")
        path = saved_media_path(msg)
        if not saved_id or not path:
            return
        paths = getattr(self, "_saved_media_paths", None)
        if paths is None:
            paths = self._saved_media_paths = {}
        paths[saved_id] = path
        # Save As may originate from a data dialog holding a different dict
        # instance. Attach the path to every currently loaded copy as well.
        for collection_name in ("_sorted_messages", "_all_sorted_messages"):
            for candidate in getattr(self, collection_name, ()):
                candidate_id = (candidate.get("key") or {}).get("id", "")
                if candidate_id == saved_id:
                    candidate["_saved_media_path"] = path
        index = self.messages_list.GetFirstSelected()
        if index < 0 or index >= len(self._sorted_messages):
            return
        selected = self._sorted_messages[index]
        selected_id = (selected.get("key") or {}).get("id", "")
        if selected_id != saved_id or not self._saved_media_path(selected):
            return
        self._action_show_in_folder_btn.Show()
        self._sync_media_action_slot_visibility()
        self.conversation_panel.Layout()

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
        media_path = cached_media_path(msg_type, msg_id)

        if not getattr(mw, "_wa_connected", False):
            mw.output(i18n.t("media_download_offline"))
            return

        mw.output(i18n.t("downloading"))
        self._action_download_btn.Hide()
        # The gauge only moves forward now (see
        # update_message_download_progress), so a previous attempt's value has
        # to be cleared or this one starts wherever that one stopped.
        self._download_progress.pop(msg_id, None)
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

    # ── Download progress ───────────────────────────────────────────────────

    def update_message_download_progress(self, msg_id: str, progress: float):
        """
        Called from the main thread (via wx.CallAfter) when a media file's
        download progress changes.  Refreshes the relevant row in the list.

        Two sources now feed this and they measure different things, which is
        why it only ever moves forward. The server reports the real download
        from WhatsApp's CDN — the part that actually takes minutes — and the
        HTTP read of the finished file over loopback follows it, restarting
        from near zero. Without the monotonic guard the bar would climb to
        100%, drop, and climb again.

        Keeping the second source rather than deleting it is deliberate:
        client/api/ is reinstalled independently of this app, so a server that
        has never heard of media-download-progress still has to move the bar,
        and there it is the only signal there is.
        """
        try:
            progress = float(progress)
        except (TypeError, ValueError):
            return
        # NaN would survive the clamp below and arrive as 1.0: min(1.0, nan)
        # is 1.0, because every comparison with NaN is False. A malformed
        # progress event would complete the bar over a download that has not
        # started.
        if progress != progress:
            return
        progress = max(0.0, min(1.0, progress))
        if progress <= self._download_progress.get(msg_id, 0.0):
            return
        self._download_progress[msg_id] = progress
        # The row always repaints — its own text carries the percentage, and it
        # is unambiguous about which message it belongs to. The shared gauge
        # only moves for the row the user is actually standing on.
        if self._transfer_owns_gauge(msg_id):
            self._update_media_transfer_gauge(progress)
        for i, msg in enumerate(self._sorted_messages):
            if msg.get("key", {}).get("id") == msg_id:
                self.messages_list.SetItemText(i, self._render_message_line(msg))
                break

    #: How far each unrecognised upload stage advances the bar, and how far it
    #: may get. WhatsApp's stage vocabulary is its own and versioned, so this
    #: does not pretend to know what fraction "ENCRYPT" represents — it only
    #: guarantees the bar MOVES on every distinct stage, which is the whole
    #: complaint. Capped below 1.0 because only the send completing means done,
    #: and a bar that reaches 100% while the file is still going up is a worse
    #: lie than one that stops at 90%.
    _UPLOAD_STAGE_STEP = 0.15
    _UPLOAD_STAGE_CEILING = 0.9

    def update_media_upload_progress(self, upload_id: str, progress=None,
                                     stage: str = ""):
        """Advance an upload's progress from a real fraction or a stage name.

        `progress` is None on every current WhatsApp build: `progressiveStage`,
        the only numeric source this ever had, does not exist in
        @wppconnect/wa-js 4.6.0 at all. Refusing to act without a number is
        exactly what left this feature inert — the bar sat at zero until the
        send completed and something else forced it to 1.0.
        """
        if progress is None:
            if not stage:
                return
            seen = self._upload_stages_seen.setdefault(upload_id, [])
            if stage in seen:
                return  # the same stage re-reported is not forward motion
            seen.append(stage)
            progress = min(self._UPLOAD_STAGE_CEILING,
                           len(seen) * self._UPLOAD_STAGE_STEP)
        try:
            progress = float(progress)
        except (TypeError, ValueError):
            return
        if progress != progress:  # NaN — see update_message_download_progress
            return
        progress = max(0.0, min(1.0, progress))
        previous = self._media_upload_progress.get(upload_id, 0.0)
        progress = max(previous, progress)
        self._media_upload_progress[upload_id] = progress
        self._media_transfer_started.add(upload_id)
        for index, msg in enumerate(self._sorted_messages):
            if msg.get("_local_id") != upload_id:
                continue
            if self._transfer_owns_gauge(upload_id):
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
            getattr(self, "_action_describe_btn", None),
            getattr(self, "_action_show_in_folder_btn", None),
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

    def _transfer_owns_gauge(self, transfer_id: str) -> bool:
        """Whether this transfer's progress may drive the shared gauge.

        There is one gauge and any number of transfers. Nothing used to check
        which of them was writing to it, so a background download three
        conversations away moved the bar of whatever row the user was standing
        on, and two concurrent transfers drove the same widget to two different
        values — reported as bars "going up and down on top of each other".

        Selection already scopes the gauge in every other direction:
        on_message_selected() hides it through _hide_all_media_controls(), and
        _sync_pending_document_gauge() restores "only the selected active
        transfer". Updating it from anywhere was the odd one out.

        Downloads are keyed by the WhatsApp message id and uploads by the
        virtual `_local_id`, so both are accepted here — a row can only be one
        of the two.
        """
        if not transfer_id:
            return False
        # Both real controls have it (CompatListBoxMessagesCtrl maps it onto
        # GetSelection), but this runs inside a wx.CallAfter, and an
        # AttributeError raised there is exactly how upload progress died once
        # before — see tests/test_compat_listbox_refresh_item.py. Not knowing
        # which row is focused means leaving the gauge alone, which is the safe
        # direction: every symptom here is a bar that should not be on screen.
        focused = getattr(self.messages_list, "GetFocusedItem", None)
        if focused is None:
            return False
        index = focused()
        if index < 0 or index >= len(self._sorted_messages):
            return False
        msg = self._sorted_messages[index]
        if self._is_separator(msg):
            return False
        return transfer_id in (msg.get("key", {}).get("id", ""),
                               msg.get("_local_id", ""))

    def _update_media_transfer_gauge(self, progress: float):
        gauge = getattr(self, "_media_transfer_gauge", None)
        if gauge is None:
            return
        if progress >= 1.0:
            # A finished transfer has nothing left to report, and a bar parked
            # at 100% is worse than no bar: it stays in the Tab order after the
            # message list, where the user meets it long after the download it
            # described is over, with no way to tell what it belongs to. This
            # is the state the app got stuck in most often — MainWindow's bulk
            # media sync calls update_message_download_progress(msg_id, 1.0)
            # purely to repaint a row, and that call used to *show* the gauge
            # at 100% and leave it there for the rest of the conversation.
            self._hide_media_transfer_gauge()
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
