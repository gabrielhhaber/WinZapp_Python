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
    media_type: str = ""

    @classmethod
    def read(cls, value):
        if not isinstance(value, dict) or value.get("category") not in ("media", "voice", "compile_cache"):
            return None
        mode = value.get("mode", "suggest")
        combine = value.get("combine", "all")
        if mode not in ("suggest", "notify", "automatic") or combine not in ("all", "any"):
            return None
        category = value["category"]
        media_type = value.get("media_type", "")
        if media_type not in ("", "imageMessage", "videoMessage", "documentMessage", "audioMessage", "voiceMessage"):
            return None
        if mode == "automatic" and category not in AUTOMATIC_CATEGORIES:
            return None
        # Bad numeric fields must not broaden a destructive rule to age/size 0.
        days = _number(value.get("days", 0), -1, 36500)
        minimum = _number(value.get("minimum", 0), -1)
        if days < 0 or minimum < 0:
            return None
        return cls(category, days, int(minimum), combine, mode,
                   value.get("enabled") is True, media_type)

    def matches(self, entry, now):
        if (not self.enabled or entry.category != self.category or entry.modified > now
                or (self.media_type and entry.media_type != self.media_type)):
            return False
        conditions = []
        if self.days:
            conditions.append(entry.modified > 0 and now - entry.modified >= self.days * 86400)
        if self.minimum:
            conditions.append(entry.size >= self.minimum)
        return not conditions or (all(conditions) if self.combine == "all" else any(conditions))

    def as_dict(self):
        return dict(category=self.category, days=self.days, minimum=self.minimum,
                    combine=self.combine, mode=self.mode, enabled=self.enabled, media_type=self.media_type)


@dataclass
class Preferences:
    include_other_accounts: bool = False
    include_external_models: bool = False
    scan_on_open: bool = False
    scan_on_start: bool = False
    announce_progress: bool = False
    match_media: bool = False
    match_chats: set[str] | None = None
    interval_minutes: int = 0
    warning_bytes: int = 0
    target_bytes: int = 0
    target_action: str = "notify"
    target_category: str = "all"
    target_scope: str = "all"
    warning_repeat_minutes: int = 60
    paused_until: int = 0
    other_accounts: list[str] = field(default_factory=list)
    history_days: int = 0
    protected: set[str] = field(default_factory=set)
    protected_chats: set[str] = field(default_factory=set)
    protected_categories: set[str] = field(default_factory=set)
    rules: list[Rule] = field(default_factory=list)

    @classmethod
    def read(cls, raw):
        result = cls()
        if (not isinstance(raw, dict) or type(raw.get("version", 1)) is not int
                or raw.get("version", 1) != 1):
            return result
        for key in ("include_other_accounts", "include_external_models", "scan_on_open",
                    "scan_on_start", "announce_progress", "match_media"):
            setattr(result, key, raw.get(key) is True)
        for key, maximum in (("interval_minutes", 525600), ("warning_bytes", 10**15),
                             ("target_bytes", 10**15), ("history_days", 3650)):
            setattr(result, key, int(_number(raw.get(key, 0), 0, maximum)))
        result.warning_repeat_minutes = int(_number(raw.get("warning_repeat_minutes", 60), 60, 525600))
        result.paused_until = int(_number(raw.get("paused_until", 0), 0, 10**12))
        if raw.get("target_category") in ("all", *CATEGORIES):
            result.target_category = raw["target_category"]
        if raw.get("target_scope") in ("all", "account", "shared"):
            result.target_scope = raw["target_scope"]
        accounts = raw.get("other_accounts", [])
        if isinstance(accounts, list):
            result.other_accounts = list(dict.fromkeys(p for p in accounts if isinstance(p, str) and p))[:100]
        if isinstance(raw.get("match_chats"), list):
            result.match_chats = {v for v in raw["match_chats"] if isinstance(v, str) and len(v) == 64}
        if raw.get("target_action") in ("notify", "pause_downloads"):
            result.target_action = raw["target_action"]
        for key in ("protected", "protected_chats"):
            values = raw.get(key, [])
            if isinstance(values, list):
                setattr(result, key, {v for v in values if isinstance(v, str) and v})
        categories = raw.get("protected_categories", [])
        if isinstance(categories, list):
            result.protected_categories = {v for v in categories if v in CATEGORIES}
        values = raw.get("rules", [])
        if isinstance(values, list):
            result.rules = [rule for value in values[:100] if (rule := Rule.read(value))]
        return result

    def as_dict(self):
        return dict(version=1, include_other_accounts=self.include_other_accounts,
                    include_external_models=self.include_external_models,
                    scan_on_open=self.scan_on_open, scan_on_start=self.scan_on_start,
                    announce_progress=self.announce_progress, interval_minutes=self.interval_minutes,
                    match_media=self.match_media,
                    match_chats=None if self.match_chats is None else sorted(self.match_chats),
                    warning_bytes=self.warning_bytes, target_bytes=self.target_bytes,
                    target_action=self.target_action, history_days=self.history_days,
                    target_category=self.target_category, target_scope=self.target_scope,
                    warning_repeat_minutes=self.warning_repeat_minutes, paused_until=self.paused_until,
                    other_accounts=list(self.other_accounts),
                    protected=sorted(self.protected), protected_chats=sorted(self.protected_chats),
                    protected_categories=sorted(self.protected_categories),
                    rules=[rule.as_dict() for rule in self.rules])


def eligibility(entry, preferences, *, busy=False):
    """One priority order for manual plans and rule suggestions alike."""
    if (entry.key in preferences.protected or entry.chat_key in preferences.protected_chats
            or entry.category in preferences.protected_categories):
        return "user_protected"
    if busy:
        return "in_use"
    if not entry.root.managed:
        return "external"
    if (entry.category == "compile_cache" and entry.root.category == "compile_cache"
            and entry.reason == "reproducible"):
        return "reproducible"
    if (entry.category in ("media", "voice") and entry.root.category == entry.category
            and entry.reason == "received" and entry.media_id):
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
