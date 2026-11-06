"""Storage view actions: explicit selection, protection, preview and application."""
import time

import wx

from core.storage_manager.policy import Preferences, eligibility
from core.storage_manager.transactions import PlanTooLarge
from .storage_presentation import entry_name, modified_text, reason_text, size_text


class StorageActionsMixin:
    def start_scan(self, event=None):
        if not self.available():
            return
        self.main_window._storage_context.capture_chats()
        self._operation = "scan"
        self.invalidate_preview()
        self.scan_button.Disable()
        self.stop_button.Enable()
        self.say(self.i18n.t("storage_scanning"))
        self.service.scan(self.scanned)

    def scanned(self, snapshot, error):
        if not self._alive:
            return
        self._operation = ""
        self.poll()
        if error:
            self.error("storage_operation_failed")
            return
        self.refresh_summary()
        self.say(self.i18n.t("storage_scan_cancelled" if snapshot.cancelled else
                             "storage_scan_complete" if snapshot.complete else "storage_scan_partial"))

    def stop(self, event=None):
        self.service.jobs.cancel()
        self.say(self.i18n.t("storage_stopping"))

    def select_visible(self, event=None):
        self.selected.update(e.key for e in self._rows)
        self.invalidate_preview()
        self.render_rows()

    def clear_selection(self, event=None):
        self.selected.clear()
        self.invalidate_preview()
        self.render_rows()

    def select_suggested(self, event=None):
        if not self.available():
            return
        self.selected = {e.key for e in self.service.suggested()}
        self.invalidate_preview()
        self.render_rows()

    def details(self, event=None):
        index = self.files.GetFocusedItem()
        if not 0 <= index < len(self._rows):
            return
        entry = self._rows[index]
        text = self.i18n.t("storage_item_details").format(
            name=entry_name(entry, index + self._page * self.PAGE_SIZE + 1, self.i18n),
            size=size_text(entry.size, self.i18n), time=modified_text(entry),
            reason=reason_text(eligibility(entry, self.service.effective_preferences(),
                                          busy=self.service.is_busy(entry)), self.i18n))
        wx.MessageBox(text, self.i18n.t("storage_details"), wx.OK | wx.ICON_INFORMATION, self)

    def save_preferences(self, preferences):
        if not self.available():
            return False
        self.invalidate_preview()
        def work(cancel):
            if cancel() or not self.service.valid():
                raise ValueError("scope changed")
            self.service.save_preferences(preferences)
            return preferences
        def done(value, error):
            if not self._alive:
                return
            if error:
                self.error("storage_preferences_invalid")
                return
            self._staged_import = None
            self.rules.rules = list(value.rules)
            self.rules.refresh()
            self.options.load(value)
            self.refresh_summary()
            self.filter_rows()
            self.say(self.i18n.t("storage_preferences_saved"))
        return self.service.jobs.start(work, done)

    def stage_import(self, preferences):
        self._staged_import = preferences
        self.rules.rules = list(preferences.rules)
        self.rules.refresh()

    def protect(self, protect, *, chats=False):
        if not self.available() or self.service.snapshot is None:
            return
        entries = [e for e in self.service.snapshot.entries if e.key in self.selected]
        if not entries:
            self.say(self.i18n.t("storage_choose_items"))
            return
        shared_store = getattr(self.service, "shared_store", None)
        def work(cancel):
            if cancel() or not self.service.valid():
                raise ValueError("scope changed")
            for shared in (True, False):
                values = {e.chat_key if chats else e.key for e in entries
                          if (shared_store is not None and e.root.scope == "shared" and not chats) == shared}
                if values - {""}:
                    self.service.change_protection("protected_chats" if chats else "protected",
                                                   values - {""}, protect, shared=shared)
        def done(value, error):
            if self._alive:
                self.invalidate_preview()
                self.render_rows()
                self.say(self.i18n.t("storage_preferences_invalid" if error else "storage_protection_saved"))
        self.service.jobs.start(work, done)

    def preview(self, event=None):
        if not self.available():
            return
        try:
            self._plan = self.service.preview(self.selected,
                          "quarantine" if self.method.GetSelection() == 0 else "remove")
        except PlanTooLarge:
            self.error("storage_preview_limit")
            return
        except (OSError, ValueError):
            self.error("storage_operation_failed")
            return
        self.preview_text.ChangeValue(self.i18n.t("storage_preview_summary").format(
            selected=len(self.selected), count=len(self._plan.entries),
            size=size_text(sum(e.size for e in self._plan.entries), self.i18n),
            method=self.method.GetStringSelection()))
        self.apply_button.Enable(bool(self._plan.entries))

    def apply_plan(self, event=None):
        if not self.available() or self._plan is None:
            return
        plan = self._plan
        if not self.confirm("storage_apply_confirm", count=len(plan.entries),
                            size=size_text(sum(e.size for e in plan.entries), self.i18n),
                            method=self.method.GetStringSelection()):
            return
        self.main_window._storage_context.capture_chats()
        self.invalidate_preview()
        self._operation = "apply"
        self.service.apply(plan, self.applied)

    def applied(self, result, error):
        if not self._alive:
            return
        self._operation = ""
        self.selected.clear()
        self.refresh_summary()
        self.filter_rows()
        if error:
            self.error("storage_operation_failed")
            return
        message = self.i18n.t("storage_operation_result").format(
            completed=result.completed, skipped=result.skipped, failed=result.failed,
            removed=size_text(result.bytes_removed, self.i18n), held=size_text(result.bytes_held, self.i18n))
        if result.cancelled:
            message += "\n" + self.i18n.t("storage_scan_cancelled")
        message += "\n" + self.i18n.t("storage_needs_rescan")
        self.preview_text.ChangeValue(message)
        self.say(message)
