"""Held files/history use the same owned worker and explicit confirmations."""
import time
from datetime import datetime
from contextlib import nullcontext

import wx

from core.storage_manager import recovery
from .storage_presentation import category_text, size_text


class StorageAuxiliaryMixin:
    def refresh_held(self, event=None):
        self._held_cursor = None
        self.load_held()

    def next_held(self, event=None):
        if getattr(self, "_held_cursor", None):
            self.load_held(after=self._held_cursor)

    def load_held(self, *, after=None):
        if not self.available():
            return
        def work(cancel):
            roots = self.service.get_roots(self.service.preferences)
            return recovery.holdings(self.service.store, roots, after=after)
        def done(value, error):
            if not self._alive:
                return
            if error:
                self.error("storage_operation_failed")
                return
            self._held = value
            self._held_cursor = value.next_cursor
            self.held_list.Freeze()
            try:
                self.held_list.Set([self.i18n.t("storage_held_row").format(
                    number=index + 1, category=category_text(item.original.category, self.i18n),
                    size=size_text(item.entry.size, self.i18n),
                    time=datetime.fromtimestamp(item.created).strftime("%Y-%m-%d %H:%M"))
                    for index, item in enumerate(value)])
            finally:
                self.held_list.Thaw()
            message = self.i18n.t("storage_held_count").format(count=len(value))
            if value.next_cursor or value.partial:
                message += "\n" + self.i18n.t("storage_held_partial")
            self.say(message)
        self.service.jobs.start(work, done)

    def finish_held(self, restore):
        if not self.available():
            return
        selected = [self._held[index] for index in self.held_list.GetSelections() if 0 <= index < len(self._held)]
        if not selected:
            self.say(self.i18n.t("storage_choose_items"))
            return
        key = "storage_restore_held_confirm" if restore else "storage_purge_held_confirm"
        if not self.confirm(key, count=len(selected), size=size_text(sum(h.entry.size for h in selected), self.i18n)):
            return
        def work(cancel):
            completed = failed = 0
            for item in selected:
                if cancel() or not self.service.valid():
                    break
                try:
                    shared = getattr(self.service, "shared_store", None)
                    with (shared.lock() if shared is not None else nullcontext()), self.service.store.lock():
                        preferences = self.service.effective_preferences(strict=True)
                        roots = self.service.get_roots(preferences)
                        recovery.finish(item, self.service.store, roots, self.service.media_index,
                                        preferences, restore=restore)
                    completed += 1
                except (OSError, ValueError, KeyError, IndexError, NotImplementedError):
                    failed += 1
            return completed, failed, len(selected) - completed - failed
        def done(value, error):
            if not self._alive:
                return
            if error:
                self.error("storage_operation_failed")
                return
            self.service.snapshot = None
            self.refresh_summary()
            self.filter_rows()
            completed, failed, remaining = value
            self.say(self.i18n.t("storage_held_result").format(
                completed=completed, failed=failed, remaining=remaining))
            # Keep the result announcement; refreshing the list is a separate
            # explicit action rather than silently changing a selection.
            self._held = []
            self.held_list.Set([])
        self.service.jobs.start(work, done)

    def retire_tokens(self, event=None):
        if not self.available() or not self.confirm("storage_retire_tokens_confirm"):
            return
        from core.storage_manager.journals import retire
        def done(value, error):
            if self._alive:
                if error:
                    self.error("storage_operation_failed")
                else:
                    self.say(self.i18n.t("storage_retire_tokens_result").format(count=value[0]))
                    if value[1]:
                        self.say(self.i18n.t("storage_held_partial"))
        self.service.jobs.start(lambda cancel: retire(self.service.store, cancel=cancel), done)

    def refresh_history(self, event=None):
        if not self.available():
            return
        def work(cancel):
            return self.service.store.history(time.time(), self.service.preferences.history_days)
        def done(value, error):
            if not self._alive:
                return
            if error:
                self.error("storage_operation_failed")
                return
            lines = [self.i18n.t("storage_history_row").format(
                time=datetime.fromtimestamp(row["time"]).strftime("%Y-%m-%d %H:%M"),
                completed=row.get("completed", 0), skipped=row.get("skipped", 0),
                failed=row.get("failed", 0), size=size_text(row.get("bytes_removed", 0), self.i18n))
                for row in value]
            self.history.ChangeValue("\n".join(lines) or self.i18n.t("storage_history_empty"))
        self.service.jobs.start(work, done)

    def clear_history(self, event=None):
        if not self.available() or not self.confirm("storage_clear_history_confirm"):
            return
        def done(value, error):
            if self._alive:
                if error:
                    self.error("storage_operation_failed")
                else:
                    self.history.ChangeValue(self.i18n.t("storage_history_empty"))
        self.service.jobs.start(lambda cancel: self.service.store.clear_history(), done)

    def reset_preferences(self):
        if not self.available():
            return
        def work(cancel):
            if cancel():
                raise ValueError("cancelled")
            self.service.store.reset_preferences()
            self.service.preferences = self.service.store.preferences()
            self.service.snapshot = None
            return self.service.preferences
        def done(value, error):
            if self._alive:
                if error:
                    self.error("storage_preferences_invalid")
                else:
                    self._staged_import = None
                    self.rules.rules = list(value.rules)
                    self.rules.refresh()
                    self.options.load(value)
                    self.invalidate_preview()
                    self.refresh_summary()
                    self.filter_rows()
                    self.say(self.i18n.t("storage_preferences_saved"))
        self.service.jobs.start(work, done)
