"""Bounded worker-side summaries; download gates never walk a file list."""
from dataclasses import dataclass, field

from .policy import eligibility, matching_rules


@dataclass
class Evaluation:
    usage: dict = field(default_factory=dict)
    notifications: int = 0
    automatic: list = field(default_factory=list)
    cancelled: bool = False


def evaluate(entries, preferences, now, *, busy=lambda e: False, cancel=lambda: False):
    result = Evaluation()
    has_rules = any(rule.enabled for rule in preferences.rules)
    for entry in entries:
        if cancel():
            result.cancelled = True
            break
        for key in (("all", "all"), (entry.root.scope, "all"),
                    ("all", entry.category), (entry.root.scope, entry.category)):
            result.usage[key] = result.usage.get(key, 0) + entry.size
        if not has_rules or eligibility(entry, preferences) not in ("local_copy", "reproducible"):
            continue
        rules = matching_rules(entry, preferences, now, busy=busy(entry))
        result.notifications += int(any(r.mode == "notify" for r in rules))
        if rules and all(r.mode == "automatic" for r in rules):
            result.automatic.append(entry.key)
    return result
