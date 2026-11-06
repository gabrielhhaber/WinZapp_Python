"""Accessible storage workspace; its controls never execute on construction."""
from __future__ import annotations

import time
import wx

from core.storage_manager.policy import eligibility, filtered
from core.storage_manager.forms import number
from core.storage_manager.types import CATEGORIES
from .storage_manager_actions import StorageActionsMixin
from .storage_manager_auxiliary import StorageAuxiliaryMixin
from .storage_manager_views import build
from .storage_presentation import entry_name, modified_text, reason_text, size_text, summary
from .storage_rules import MEDIA_TYPES


class StorageManagerDialog(StorageActionsMixin, StorageAuxiliaryMixin, wx.Dialog):
    PAGE_SIZE = 200

    def __init__(self, parent, main_window, service):
        self.main_window, self.service, self.i18n = main_window, service, main_window.i18n
        super().__init__(parent, title=self.i18n.t("storage_manager_title"),
                         size=(850, 780), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.SetMinSize((520, 420))
        self._alive, self._syncing = True, False
        self._staged_import = None
        self.selected, self._rows, self._filtered = set(), [], []
        self._page, self._plan, self._snapshot_marker = 0, None, None
        self._held, self._operation = [], ""
        self.requested_owner = None
        self._descending = True
        self._last_progress, self._last_spoken = -1, 0
        build(self)
        self.method.Bind(wx.EVT_CHOICE, lambda event: self.invalidate_preview())
        self.category.Bind(wx.EVT_CHOICE, self.filter_rows)
        self.media_type.Bind(wx.EVT_CHOICE, self.filter_rows)
        self.search.Bind(wx.EVT_TEXT_ENTER, self.filter_rows)
        self.Bind(wx.EVT_CLOSE, self.close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.poll, self._timer)
        self._timer.Start(1000)
        self.refresh_summary()
        if service.snapshot is not None:
            self._snapshot_marker = service.snapshot.id
            self.filter_rows()
        if service.preferences.scan_on_open and not service.jobs.busy:
            wx.CallAfter(self.start_scan)

    def say(self, text):
        if self._alive:
            self.status.SetLabel(text)
            self.main_window.speak_output.output(text)

    def error(self, key):
        if self._alive:
            wx.MessageBox(self.i18n.t(key), self.GetTitle(), wx.OK | wx.ICON_ERROR, self)

    def confirm(self, key, **values):
        return wx.MessageBox(self.i18n.t(key).format(**values), self.GetTitle(),
                             wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING, self) == wx.YES

    def available(self):
        if self.service.jobs.busy:
            self.say(self.i18n.t("storage_busy"))
            return False
        return self._alive and self.service.valid()

    def refresh_summary(self):
        text = summary(self.service.snapshot, self.service.preferences, self.i18n)
        prefs = self.service.preferences
        text += "\n" + self.i18n.t("storage_active_policy").format(
            rules=sum(r.enabled for r in prefs.rules), interval=prefs.interval_minutes,
            paused=self.i18n.t("storage_rule_on" if prefs.paused_until > time.time() else "storage_rule_off"))
        if self.service.warning():
            text += "\n" + self.i18n.t("storage_warning_exceeded")
        if self.service.target_exceeded:
            text += "\n" + self.i18n.t("storage_target_paused" if self.service.pause_downloads else "storage_target_exceeded")
        text += "\n" + self.i18n.t("storage_matching_scope").format(
            count=self.i18n.t("storage_match_all_visible_chats") if prefs.match_chats is None else len(prefs.match_chats))
        if self.service.preferences.match_media and not self.service.references_complete:
            text += "\n" + self.i18n.t("storage_media_match_incomplete")
        self.summary.ChangeValue(text)

    def invalidate_preview(self):
        self._plan = None
        self.apply_button.Disable()
        self.preview_text.ChangeValue("")

    def filter_rows(self, event=None):
        self.invalidate_preview()
        snapshot = self.service.snapshot
        matched = self.service.references_complete and self.service.preferences.match_media
        self.media_type.Enable(matched)
        if not matched:
            self.media_type.SetSelection(0)
        if snapshot is None:
            self._filtered = []
        else:
            try:
                category = "all" if self.category.GetSelection() == 0 else CATEGORIES[self.category.GetSelection() - 1]
                self._filtered = filtered(snapshot.entries, category=category,
                    minimum=int(number(self.minimum.GetValue()) * 1024**2),
                    days=number(self.days.GetValue(), maximum=36500), now=time.time(),
                    combine="all" if self.combine.GetSelection() == 0 else "any", descending=self._descending)
                media_type = MEDIA_TYPES[max(0, self.media_type.GetSelection())]
                if media_type:
                    self._filtered = [e for e in self._filtered if e.media_type == media_type]
                scope = ("all", "account", "shared", "installation", "external", "other_accounts")[max(0, self.scope.GetSelection())]
                if scope != "all":
                    self._filtered = [e for e in self._filtered if e.root.scope == scope]
                state = self.state.GetSelection()
                if state > 0:
                    prefs = self.service.effective_preferences()
                    self._filtered = [e for e in self._filtered
                        if (eligibility(e, prefs) in ("local_copy", "reproducible")) == (state == 1)]
                order = self.sort.GetSelection()
                if order > 0:
                    self._filtered.sort(key=lambda e: ((e.modified if order == 1 else e.category), e.key),
                                        reverse=self._descending)
                query = self.search.GetValue().casefold().strip()
                if query:
                    # Search only the visible allowed name, never a hidden
                    # account path, raw cache id or protected session filename.
                    self._filtered = [e for n, e in enumerate(self._filtered)
                                      if query in entry_name(e, n + 1, self.i18n).casefold()]
            except (ValueError, OverflowError):
                self.error("storage_preferences_invalid")
                return
        self._page = 0
        self.render_rows()

    def render_rows(self):
        focused = self.files.GetFocusedItem()
        key = self._rows[focused].key if 0 <= focused < len(self._rows) else None
        start = self._page * self.PAGE_SIZE
        rows = self._filtered[start:start + self.PAGE_SIZE]
        self._syncing = True
        self.files.Freeze()
        try:
            self.files.DeleteAllItems()
            preferences = self.service.effective_preferences()
            for index, entry in enumerate(rows):
                self.files.InsertItem(index, entry_name(entry, start + index + 1, self.i18n)[:511])
                self.files.SetItem(index, 1, size_text(entry.size, self.i18n))
                self.files.SetItem(index, 2, reason_text(eligibility(entry, preferences,
                                                      busy=self.service.is_busy(entry)), self.i18n))
                self.files.SetItem(index, 3, modified_text(entry))
                self.files.CheckItem(index, entry.key in self.selected)
            self._rows = rows
            if rows:
                target = next((n for n, e in enumerate(rows) if e.key == key), min(max(0, focused), len(rows) - 1))
                self.files.Focus(target)
                self.files.Select(target)
        finally:
            self.files.Thaw()
            self._syncing = False
        self.selection_status.SetLabel(self.i18n.t("storage_selection_summary").format(
            selected=len(self.selected), visible=len(rows), total=len(self._filtered), page=self._page + 1))

    def page_by(self, delta):
        pages = max(1, (len(self._filtered) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._page = max(0, min(pages - 1, self._page + delta))
        self.render_rows()

    def reverse_order(self, event=None):
        self._descending = not self._descending
        self.filter_rows()

    def file_key(self, event):
        index = self.files.GetFocusedItem()
        if event.GetKeyCode() == wx.WXK_SPACE and 0 <= index < len(self._rows):
            key = self._rows[index].key
            self.selected.symmetric_difference_update({key})
            self.invalidate_preview()
            self.render_rows()
            return
        event.Skip()

    def check_changed(self, event):
        if not self._syncing and 0 <= event.GetIndex() < len(self._rows):
            key = self._rows[event.GetIndex()].key
            if self.files.IsItemChecked(event.GetIndex()):
                self.selected.add(key)
            else:
                self.selected.discard(key)
            self.invalidate_preview()
            self.selection_status.SetLabel(self.i18n.t("storage_selected_count").format(count=len(self.selected)))
        event.Skip()

    def poll(self, event=None):
        if not self._alive:
            return
        busy = self.service.jobs.busy
        self.scan_button.Enable(not busy)
        self.stop_button.Enable(busy)
        snapshot = self.service.snapshot
        if snapshot is not None and snapshot.id != self._snapshot_marker:
            self._snapshot_marker = snapshot.id
            self.selected.clear()
            self.invalidate_preview()
            self.refresh_summary()
            self.filter_rows()
        count = self.service.progress_count
        if (busy and self._operation == "scan" and self.service.preferences.announce_progress
                and count != self._last_progress and time.monotonic() - self._last_spoken >= 5):
            self._last_progress, self._last_spoken = count, time.monotonic()
            self.say(self.i18n.t("storage_scan_progress").format(count=count))

    def close(self, event=None):
        self.service.jobs.cancel()
        self.EndModal(wx.ID_CLOSE)

    def open_owner(self, name):
        if not self.available():
            return
        self.requested_owner = name
        self.close()

    def destroyed(self, event):
        if event.GetEventObject() is self:
            self._alive = False
            self._timer.Stop()
        event.Skip()
