"""BulkCopyFilesMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

Copying a selection that holds files: documents, photos, videos and audio are
decrypted into one temporary folder and put on the clipboard as files
(CF_HDROP), so a single paste in Windows Explorer drops them all. Text
messages in the same selection travel as a text block on the same clipboard
(see `_on_mass_copy_messages`).
"""

import logging
import os
import tempfile
import threading
import wx
from core.utils import decrypt_bytes
from ui.conversation_panel.bulk_copy_plan import unique_copy_name
from ui.conversation_panel.media_paths import cached_media_path


def set_clipboard_files_and_text(paths: list, text: str) -> bool:
    """Put *paths* (as files) and/or *text* on the clipboard; True on success.

    With both, one composite object carries CF_HDROP and CF_UNICODETEXT at the
    same time: Explorer pastes the files, a text field pastes the text. Main
    thread only.
    """
    if not paths and not text:
        return False
    if not wx.TheClipboard.Open():
        return False
    try:
        data = None
        if paths:
            files = wx.FileDataObject()
            for path in paths:
                files.AddFile(path)
            data = files
        if text:
            if data is None:
                data = wx.TextDataObject(text)
            else:
                composite = wx.DataObjectComposite()
                composite.Add(data, True)
                composite.Add(wx.TextDataObject(text))
                data = composite
        return bool(wx.TheClipboard.SetData(data))
    finally:
        wx.TheClipboard.Close()


class BulkCopyFilesMixin:
    """Bulk copy of the file messages in a selection."""

    def _start_bulk_copy_with_files(self, text: str, file_msgs: list):
        """Stage *file_msgs* on a worker thread, then copy them (plus *text*,
        when the selection also held text messages) in one clipboard write."""
        threading.Thread(
            target=self._stage_files_for_bulk_copy,
            args=(text, list(file_msgs)),
            daemon=True,
        ).start()

    def _stage_files_for_bulk_copy(self, text: str, file_msgs: list):
        """Worker thread: decrypt every file into one temp folder.

        Failures are counted, not announced one by one: a selection of twenty
        expired files must not read twenty errors to a screen-reader user.
        """
        mw = self.main_window
        paths, failed = [], 0
        announced_download = False
        try:
            tmp_dir = tempfile.mkdtemp(prefix="wz_copy_")
        except Exception as exc:
            logging.warning("[bulk copy] temp folder failed: %s", type(exc).__name__)
            wx.CallAfter(self._finish_bulk_copy, [], text, len(file_msgs))
            return
        used = set()
        for msg in file_msgs:
            msg_type = msg.get("messageType", "")
            msg_id = (msg.get("key") or {}).get("id", "")
            media_path = cached_media_path(msg_type, msg_id)
            try:
                if not os.path.isfile(media_path):
                    if not getattr(mw, "_wa_connected", False):
                        failed += 1
                        continue
                    if not announced_download:
                        announced_download = True
                        wx.CallAfter(mw.output, mw.i18n.t("downloading"))
                    if not self._download_media_to_disk(msg, media_path):
                        failed += 1
                        continue
                with open(media_path, "rb") as fh:
                    content = decrypt_bytes(fh.read(), mw.key)
                name = unique_copy_name(self._resolve_media_filename(msg), used)
                target = os.path.join(tmp_dir, name)
                with open(target, "wb") as fh:
                    fh.write(content)
                paths.append(target)
            except Exception as exc:
                logging.warning("[bulk copy] staging a file failed: %s", type(exc).__name__)
                failed += 1
        wx.CallAfter(self._finish_bulk_copy, paths, text, failed)

    def _finish_bulk_copy(self, paths: list, text: str, failed: int):
        """Main thread: write the clipboard and report the real outcome."""
        mw = self.main_window
        i18n = mw.i18n
        if not paths and not text:
            mw.output(i18n.t("msg_copy_error"), interrupt=True)
            return
        try:
            ok = set_clipboard_files_and_text(paths, text)
        except Exception as exc:
            logging.warning("[bulk copy] clipboard failed: %s", type(exc).__name__)
            ok = False
        if not ok:
            mw.output(i18n.t("msg_copy_error"), interrupt=True)
        elif failed:
            mw.output(
                i18n.t("messages_copied_bulk_files_partial").format(count=failed),
                interrupt=True,
            )
        else:
            mw.output(i18n.t("messages_copied_bulk_files"), interrupt=True)
