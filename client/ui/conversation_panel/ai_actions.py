"""AIActionsMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Transcribe a voice message, describe an image, video or sticker, or turn a PDF
into accessible text, using the AI providers the person configured in Settings
(core/ai_providers.py decides WHICH provider answers; this file only drives the
message -> file -> provider -> result window flow).

Methods run with ``self`` bound to the ConversationsPanel instance.

Two things worth knowing before changing this:

* The media cache is encrypted (.wzmedia / .msv). The provider SDKs need a
  plain file, so the content is decrypted into a private temporary folder for
  the duration of the request. That folder is removed as soon as the result
  window closes (or as soon as the request fails) — nothing decrypted stays in
  %TEMP%. Follow-up questions about an image or video are answered while the
  result window is open, which is exactly how long the file is kept.
* The network call runs on a worker thread; everything that touches wx goes
  back through ``wx.CallAfter``. While it is in flight a "still working"
  reminder is spoken every few seconds, because a silent wait is
  indistinguishable from a frozen program for a screen-reader user.
"""

import logging
import os
import shutil
import tempfile
import threading

import wx

from core import ai_providers
from core.ai_providers import AIProviderError
from core.utils import decrypt_bytes
from ui.conversation_panel.media_paths import cached_media_path
from ui.dialogs.ai_result_dialog import AIResultDialog

#: Seconds between the spoken "still processing" reminders.
_STILL_WORKING_SECONDS = 8


class AIActionsMixin:
    """AI transcription and description of a message's media."""

    def _ai_accessibility_settings(self):
        """The ai_accessibility settings if the feature is usable right now,
        otherwise None (see core.ai_providers.usable_settings)."""
        return ai_providers.usable_settings(
            self.main_window.settings.get("ai_accessibility", {})
        )

    def _ai_menu_label_for_type(self, msg_type: str, i18n) -> str:
        """Context-menu label for this message type, or "" when AI processing
        is not offered for it (feature off, no key, or the toggle for this
        kind of media is off)."""
        key = ai_providers.action_label_key(
            msg_type, self.main_window.settings.get("ai_accessibility", {})
        )
        return i18n.t(key) if key else ""

    def _on_menu_ai_process(self, msg: dict):
        """Send this message's media to the configured AI providers and show
        the result in AIResultDialog."""
        i18n = self.main_window.i18n
        ai_settings = self._ai_accessibility_settings()
        if ai_settings is None:
            wx.MessageBox(
                i18n.t("ai_not_configured_msg"),
                self.main_window.app_name,
                wx.OK | wx.ICON_INFORMATION,
                self,
            )
            return

        msg_type = msg.get("messageType", "")
        msg_id = (msg.get("key") or {}).get("id", "")
        if not msg_id:
            return

        if msg_type == "documentMessage" and not ai_providers.is_pdf_message(msg):
            wx.MessageBox(
                i18n.t("ai_pdf_only_msg"),
                self.main_window.app_name,
                wx.OK | wx.ICON_INFORMATION,
                self,
            )
            return

        file_name = self._resolve_media_filename(msg)
        media_path = cached_media_path(msg_type, msg_id)
        is_video = msg_type == "videoMessage"
        key = self.main_window.key

        self.main_window.output(i18n.t("ai_processing_msg"))
        threading.Thread(
            target=self._run_ai_process,
            args=(msg, msg_type, media_path, file_name, is_video, key, ai_settings),
            daemon=True,
        ).start()

    def _run_ai_process(self, msg, msg_type, media_path, file_name, is_video,
                        key, ai_settings):
        """Worker thread of _on_menu_ai_process(). Never touches wx directly."""
        mw = self.main_window
        i18n = mw.i18n

        if not self._ensure_media_on_disk(msg, media_path):
            wx.CallAfter(mw.output, i18n.t("ai_media_download_error_msg"))
            return

        stop_watchdog = threading.Event()

        def _watchdog():
            while not stop_watchdog.wait(_STILL_WORKING_SECONDS):
                wx.CallAfter(mw.output, i18n.t("ai_still_processing_msg"))

        threading.Thread(target=_watchdog, daemon=True).start()

        tmp_dir = None
        handed_to_dialog = False
        try:
            with open(media_path, "rb") as fh:
                content = decrypt_bytes(fh.read(), key)

            tmp_dir = tempfile.mkdtemp(prefix="wz_ai_")
            # Only the extension matters to the providers; the sender's file
            # name is never written into the temporary path.
            tmp_path = os.path.join(
                tmp_dir, "media" + os.path.splitext(file_name)[1]
            )
            with open(tmp_path, "wb") as fh:
                fh.write(content)

            ask_fn = None
            if msg_type == "audioMessage":
                result_text, _used = ai_providers.transcribe_audio(tmp_path, ai_settings)
                title = i18n.t("ai_result_transcription_title")
            elif msg_type in ("imageMessage", "videoMessage", "stickerMessage"):
                result_text, used_provider = ai_providers.describe_visual_media(
                    tmp_path, ai_settings, is_video=is_video
                )
                title = i18n.t(
                    "ai_result_sticker_title" if msg_type == "stickerMessage"
                    else "ai_result_description_title"
                )

                # Follow-up questions reuse the file while the result window
                # stays open, preferring the provider that described it (and
                # falling back further if that one now fails).
                def _ask(question, p=tmp_path, s=ai_settings, v=is_video,
                         pv=used_provider):
                    return ai_providers.ask_about_visual_media(
                        p, s, question, is_video=v, prefer=pv
                    )[0]

                ask_fn = _ask
            else:  # documentMessage, already confirmed to be a PDF
                result_text, _used = ai_providers.pdf_to_accessible_text(
                    tmp_path, ai_settings
                )
                title = i18n.t("ai_result_pdf_title")

            def _show_result():
                try:
                    dlg = AIResultDialog(mw, title, result_text, ask_fn=ask_fn)
                    try:
                        dlg.ShowModal()
                    finally:
                        dlg.Destroy()
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

            handed_to_dialog = True
            wx.CallAfter(_show_result)
        except AIProviderError as exc:
            wx.CallAfter(
                wx.MessageBox, str(exc), mw.app_name, wx.OK | wx.ICON_ERROR, self
            )
        except Exception as exc:
            # The type only: an SDK message can carry the sender's file name.
            logging.error("[_run_ai_process] unexpected %s", type(exc).__name__)
            wx.CallAfter(mw.output, i18n.t("ai_unexpected_error_msg"))
        finally:
            stop_watchdog.set()
            if tmp_dir and not handed_to_dialog:
                shutil.rmtree(tmp_dir, ignore_errors=True)
