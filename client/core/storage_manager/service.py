"""Application-independent owner of scans, preferences and explicit plans."""
from __future__ import annotations

import time
from dataclasses import replace
from contextlib import nullcontext

from .jobs import JobSlot
from .policy import Preferences, eligibility, matching_rules
from .roots import classify
from .scan import scan
from .transactions import execute, make_plan
from .types import CATEGORIES
from .references import References
from .evaluation import evaluate


class StorageService:
    def __init__(self, store, media_index, roots, references, dispatch, *,
                 busy=lambda e: False, valid=lambda: True, launch=None, provider=None):
        self.store, self.media_index = store, media_index
        self.get_roots, self.get_references = roots, references
        self.is_busy, self.valid = busy, valid
        self.jobs = JobSlot(dispatch, launch=launch)
        self.provider = provider
        self.preferences_invalid = False
        try:
            self.preferences = store.preferences(strict=True)
        except (OSError, ValueError):
            self.preferences = Preferences()
            self.preferences_invalid = True
        self.snapshot = None
        self.references_complete = False
        self.last_result = None
        self.last_evaluation = 0
        self.progress_count = 0
        self.measured_bytes = None
        self.measured_usage = {}
        self.notification_count = 0
        self.scheduled_keys = []
        self.capture_accounts = lambda: []
        self.shared_preferences = lambda: Preferences()

    def effective_preferences(self, *, strict=False):
        own = self.store.preferences(strict=strict) if strict else self.preferences
        result = Preferences.read(own.as_dict())
        if self.preferences_invalid and not strict:
            result.protected_categories.update(CATEGORIES)
        try:
            shared = self.shared_preferences()
        except (OSError, ValueError):
            if strict:
                raise
            # An unreadable shared keep-list cannot become permission to
            # discard its protection. Read-only presentation may continue.
            result.protected_categories.update(CATEGORIES)
            return result
        result.protected |= shared.protected
        result.protected_categories |= shared.protected_categories
        return result

    def save_preferences(self, preferences):
        with self.store.lock():
            latest = self.store.preferences(strict=True)
            preferences.other_accounts = (self.capture_accounts() if preferences.include_other_accounts
                                          and not latest.include_other_accounts else list(latest.other_accounts))
            for key in ("protected", "protected_chats", "protected_categories"):
                setattr(preferences, key, set(getattr(latest, key)))
            self.store.save_preferences(preferences)
        if self.scope(preferences) != self.scope(self.preferences):
            self.snapshot = None
            self.measured_bytes = None
            self.measured_usage = {}
        self.preferences = preferences
        self.preferences_invalid = False

    @staticmethod
    def scope(preferences):
        return (preferences.include_other_accounts, preferences.include_external_models,
                preferences.match_media, None if preferences.match_chats is None else frozenset(preferences.match_chats))

    def change_protection(self, field, values, protect, *, shared=False):
        if field not in ("protected", "protected_chats", "protected_categories"):
            raise ValueError("unknown protection")
        store = self.shared_store if shared else self.store
        with store.lock():
            prefs = store.preferences(strict=True)
            target = getattr(prefs, field)
            target.update(values) if protect else target.difference_update(values)
            store.save_preferences(prefs)
            if not shared:
                self.preferences = prefs

    @property
    def target_exceeded(self):
        prefs = self.preferences
        if prefs.target_bytes <= 0:
            return False
        measured = self.measured_usage.get((prefs.target_scope, prefs.target_category), 0)
        return measured is not None and prefs.target_bytes > 0 and measured >= prefs.target_bytes

    @property
    def pause_downloads(self):
        return self.preferences.target_action == "pause_downloads" and self.target_exceeded

    def scan(self, callback):
        preferences = self.preferences
        scope = self.scope(preferences)
        self.progress_count = 0
        def work(cancel):
            roots = self.get_roots(preferences)
            refs = self.get_references(cancel) if preferences.match_media else References({}, complete=False)
            result = scan(roots, cancel=cancel,
                          classify=lambda e: refs.classify(classify(e)),
                          progress=lambda count: setattr(self, "progress_count", count))
            evaluation = evaluate(result.entries, self.effective_preferences(), time.time(),
                                  busy=self.is_busy, cancel=cancel)
            if evaluation.cancelled:
                result.cancelled = True
            return result, refs.complete, evaluation
        def done(value, error):
            current_scope = self.scope(self.preferences)
            if error is None and self.valid() and scope == current_scope:
                self.snapshot, self.references_complete, evaluation = value
                if not self.snapshot.cancelled:
                    self.measured_bytes = self.snapshot.total_bytes
                    self.measured_usage = evaluation.usage
                self.notification_count, self.scheduled_keys = evaluation.notifications, evaluation.automatic
                self.last_evaluation = time.monotonic()
            elif error is None:
                error = "ScopeChanged"
            callback(self.snapshot if error is None else None, error)
        return self.jobs.start(work, done)

    def preview(self, selected, method, *, automatic=False):
        if self.snapshot is None or self.jobs.busy or not self.valid():
            raise ValueError("no current storage snapshot")
        return replace(make_plan(self.snapshot, selected, method, self.effective_preferences(), busy=self.is_busy),
                       automatic=automatic)

    def apply(self, plan, callback):
        snapshot = self.snapshot
        def locked_work(cancel):
            if snapshot is None or not self.valid():
                raise ValueError("storage scope changed")
            preferences = self.effective_preferences(strict=True)
            if any(e.media_id for e in plan.entries) and not preferences.match_media:
                raise ValueError("local-history matching was disabled")
            current_roots = self.get_roots(preferences)
            scope = {(r.key, r.path) for r in current_roots if r.managed}
            if any((e.root.key, e.root.path) not in scope for e in plan.entries):
                raise ValueError("storage roots changed")
            fresh = self.get_references(cancel) if any(e.media_id for e in plan.entries) else References({})
            def busy(entry):
                if not self.valid() or self.is_busy(entry):
                    return True
                if plan.automatic:
                    rules = matching_rules(entry, preferences, time.time())
                    if (entry.category != "compile_cache" or not rules or any(r.mode != "automatic" for r in rules)
                            or preferences.paused_until > time.time()
                            or not (preferences.scan_on_start or preferences.interval_minutes)):
                        return True
                if entry.media_id:
                    if preferences.match_chats is not None and entry.chat_key not in preferences.match_chats:
                        return True
                    current = fresh.classify(replace(entry, reason="protected", media_id="", chat_key=""))
                    return (not fresh.complete or current.reason != "received"
                            or current.media_id != entry.media_id)
                return False
            kwargs = {} if self.provider is None else {"provider": self.provider}
            result = execute(plan, snapshot, preferences, self.store, self.media_index,
                             busy=busy, cancel=cancel, **kwargs)
            try:
                self.store.record(time.time(), preferences.history_days,
                                  completed=result.completed, skipped=result.skipped,
                                  failed=result.failed, bytes_removed=result.bytes_removed)
            except OSError:
                result.note("history_unavailable")
            return result
        def work(cancel):
            shared_store = getattr(self, "shared_store", None)
            guard = shared_store.lock() if shared_store is not None else nullcontext()
            with guard, self.store.lock():
                return locked_work(cancel)
        def done(value, error):
            if error is None:
                self.last_result = value
                # A consumed preview is never silently reused; overview stays
                # explicitly old until a new scan completes.
                self.snapshot = None
            callback(value, error)
        return self.jobs.start(work, done)

    def suggested(self, now=None, *, cancel=lambda: False):
        if self.snapshot is None:
            return []
        now = time.time() if now is None else now
        preferences = self.effective_preferences()
        result = []
        for entry in self.snapshot.entries:
            if cancel():
                raise InterruptedError("suggestion cancelled")
            if (eligibility(entry, preferences) in ("local_copy", "reproducible")
                    and matching_rules(entry, preferences, now, busy=self.is_busy(entry))):
                result.append(entry)
        return result

    def automatic_selection(self, now=None, *, cached=False):
        if self.snapshot is None:
            return []
        if cached:
            return list(self.scheduled_keys)
        now = time.time() if now is None else now
        preferences = self.effective_preferences()
        result = []
        for entry in self.snapshot.entries:
            rules = matching_rules(entry, preferences, now, busy=self.is_busy(entry))
            if rules and all(rule.mode == "automatic" for rule in rules):
                result.append(entry.key)
        return result

    def warning(self):
        if self.snapshot is None:
            return False
        limit = self.preferences.warning_bytes
        return limit > 0 and self.snapshot.total_bytes >= limit
