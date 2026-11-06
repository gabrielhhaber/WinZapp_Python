"""Fail-closed storage preferences and explainable rule matching."""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from .types import AUTOMATIC_CATEGORIES, CATEGORIES


def _number(value, default=0, maximum=10**15):
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) and 0 <= number <= maximum else default


@dataclass(frozen=True)
class Rule:
    category: str
    days: float = 0
    minimum: int = 0
    combine: str = "all"
    mode: str = "suggest"
    enabled: bool = False

    @classmethod
    def read(cls, value):
        if not isinstance(value, dict) or value.get("category") not in CATEGORIES:
            return None
        mode = value.get("mode", "suggest")
        combine = value.get("combine", "all")
        if mode not in ("suggest", "notify", "automatic") or combine not in ("all", "any"):
            return None
        category = value["category"]
        if mode == "automatic" and category not in AUTOMATIC_CATEGORIES:
            return None
        # Bad numeric fields must not broaden a destructive rule to age/size 0.
        days = _number(value.get("days", 0), -1, 36500)
        minimum = _number(value.get("minimum", 0), -1)
        if days < 0 or minimum < 0:
            return None
        return cls(category, days, int(minimum), combine, mode,
                   value.get("enabled") is True)

    def matches(self, entry, now):
        if not self.enabled or entry.category != self.category or entry.modified > now:
            return False
        conditions = []
        if self.days:
            conditions.append(entry.modified > 0 and now - entry.modified >= self.days * 86400)
        if self.minimum:
            conditions.append(entry.size >= self.minimum)
        return not conditions or (all(conditions) if self.combine == "all" else any(conditions))

    def as_dict(self):
        return dict(category=self.category, days=self.days, minimum=self.minimum,
                    combine=self.combine, mode=self.mode, enabled=self.enabled)


@dataclass
class Preferences:
    include_other_accounts: bool = False
    include_external_models: bool = False
    scan_on_open: bool = False
    scan_on_start: bool = False
    announce_progress: bool = False
    interval_minutes: int = 0
    warning_bytes: int = 0
    target_bytes: int = 0
    target_action: str = "notify"
    history_days: int = 0
    protected: set[str] = field(default_factory=set)
    protected_chats: set[str] = field(default_factory=set)
    rules: list[Rule] = field(default_factory=list)

    @classmethod
    def read(cls, raw):
        result = cls()
        if not isinstance(raw, dict) or raw.get("version", 1) != 1:
            return result
        for key in ("include_other_accounts", "include_external_models", "scan_on_open",
                    "scan_on_start", "announce_progress"):
            setattr(result, key, raw.get(key) is True)
        for key, maximum in (("interval_minutes", 525600), ("warning_bytes", 10**15),
                             ("target_bytes", 10**15), ("history_days", 3650)):
            setattr(result, key, int(_number(raw.get(key, 0), 0, maximum)))
        if raw.get("target_action") in ("notify", "pause_downloads"):
            result.target_action = raw["target_action"]
        for key in ("protected", "protected_chats"):
            values = raw.get(key, [])
            if isinstance(values, list):
                setattr(result, key, {v for v in values if isinstance(v, str) and v})
        values = raw.get("rules", [])
        if isinstance(values, list):
            result.rules = [rule for value in values[:100] if (rule := Rule.read(value))]
        return result

    def as_dict(self):
        return dict(version=1, include_other_accounts=self.include_other_accounts,
                    include_external_models=self.include_external_models,
                    scan_on_open=self.scan_on_open, scan_on_start=self.scan_on_start,
                    announce_progress=self.announce_progress, interval_minutes=self.interval_minutes,
                    warning_bytes=self.warning_bytes, target_bytes=self.target_bytes,
                    target_action=self.target_action, history_days=self.history_days,
                    protected=sorted(self.protected), protected_chats=sorted(self.protected_chats),
                    rules=[rule.as_dict() for rule in self.rules])


def eligibility(entry, preferences, *, busy=False):
    """One priority order for manual plans and rule suggestions alike."""
    if entry.key in preferences.protected or entry.chat_key in preferences.protected_chats:
        return "user_protected"
    if busy:
        return "in_use"
    if not entry.root.managed:
        return "external"
    if entry.category == "compile_cache":
        return "reproducible"
    if entry.category in ("media", "voice") and entry.reason == "received" and entry.media_id:
        return "local_copy"
    return entry.reason if entry.reason not in ("received", "reproducible") else "protected"


def matching_rules(entry, preferences, now, *, busy=False):
    allowed = eligibility(entry, preferences, busy=busy)
    if allowed not in ("local_copy", "reproducible"):
        return ()
    matches = tuple(rule for rule in preferences.rules if rule.matches(entry, now))
    # A non-destructive match wins over an automatic one. The UI can explain
    # the full tuple; a caller must never take the broadest matching action.
    if any(rule.mode != "automatic" for rule in matches):
        return tuple(rule for rule in matches if rule.mode != "automatic")
    return matches


def filtered(entries, *, category="all", query="", minimum=0, days=0, now=0,
             combine="all", descending=True):
    """Stable filtering without changing a selection or granting an action."""
    query = query.casefold().strip()
    result = []
    for entry in entries:
        if category != "all" and entry.category != category:
            continue
        if query and query not in entry.relative.casefold():
            continue
        conditions = []
        if minimum:
            conditions.append(entry.size >= minimum)
        if days:
            conditions.append(0 < entry.modified <= now - days * 86400)
        if conditions and not (all(conditions) if combine == "all" else any(conditions)):
            continue
        result.append(entry)
    return sorted(result, key=lambda e: (e.size, e.key), reverse=descending)
