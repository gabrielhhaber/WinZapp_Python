"""WhatsNewSinceMixin — part of MainWindow (see main_window/__init__.py).

Help > "What's new since you last looked": what changed in the release notes
since the last time anyone on this install looked, in sentences (new items,
changed items as they now read, removed items), never as a developer's diff.
The comparison is core/changelog_diff.py; what was last seen, kept once for
every account, is core/changelog_seen.py.

Help > What's New still shows the whole file.
"""

import glob
import os
from datetime import datetime

import wx

from app_paths import global_dir, resource_path
from core import changelog_seen
from core.locale_format import get_datetime_format
from version import __version__


class WhatsNewSinceMixin:
    """Help > "What's new since you last looked"."""

    def _on_whats_new_since(self, event=None):
        from updater import WhatsNewDialog, load_changelog_text, _read_changelog_file
        i18n = self.i18n
        current = load_changelog_text(i18n.language)
        if not current.strip():
            wx.MessageBox(i18n.t("whats_new_none_message"), i18n.t("whats_new_none_title"),
                          wx.OK | wx.ICON_INFORMATION)
            return
        # The language whose file was read: load_changelog_text() falls back
        # to en-US for a language that ships none.
        locale = i18n.language if _read_changelog_file(i18n.language) else "en-US"
        path = global_dir(changelog_seen.FILE_NAME)
        snapshot = changelog_seen.read(path)
        text = changelog_seen.text_for(
            changelog_seen.previous_text(snapshot, locale), current,
            self._whats_new_seen_on(snapshot), __version__, i18n.t)
        dlg = WhatsNewDialog(self, text, title=i18n.t("whats_new_since_title"))
        try:
            dlg.ShowModal()
        finally:
            dlg.Destroy()
        changelog_seen.write(path, self._shipped_changelogs(), __version__)

    def _whats_new_seen_on(self, snapshot) -> str:
        try:
            seen_at = float((snapshot or {}).get("seen_at") or 0)
        except (TypeError, ValueError):
            seen_at = 0.0
        if seen_at <= 0:
            return ""
        return datetime.fromtimestamp(seen_at).strftime(
            get_datetime_format(self.i18n.t("datetime_fmt")))

    @staticmethod
    def _shipped_changelogs() -> dict:
        """{locale: text} of every changelog next to the app."""
        folder = os.path.dirname(resource_path("changelog_en-US.txt"))
        texts = {}
        for path in glob.glob(os.path.join(folder, "changelog_*.txt")):
            locale = os.path.basename(path)[len("changelog_"):-len(".txt")]
            try:
                with open(path, encoding="utf-8") as handle:
                    texts[locale] = handle.read()
            except OSError:
                continue
        return texts
