"""Aggregates are worker-owned; ordinary download gating is constant work."""
from dataclasses import replace

from core.storage_manager.evaluation import evaluate
from core.storage_manager.policy import Preferences, Rule
from core.storage_manager.types import Entry, Root


def entry(category="compile_cache", scope="shared", size=10):
    return Entry(Root("cache", "/owned", category, scope, managed=True), "a", size, 1,
                 (1, 2, size, 3, 4), category, reason="reproducible")


def test_usage_partitions_scopes_without_double_counting_and_skips_busy_checks_without_rules():
    first = entry()
    other = replace(entry("models", "account", 20), reason="critical")
    result = evaluate([first, other], Preferences(), 100,
                      busy=lambda e: (_ for _ in ()).throw(AssertionError("unneeded busy probe")))
    assert result.usage[("all", "all")] == 30
    assert result.usage[("shared", "compile_cache")] == 10
    assert result.usage[("account", "all")] == 20
    assert not result.automatic and result.notifications == 0


def test_non_destructive_rule_wins_and_protected_data_never_enters_automatic_selection():
    candidate = entry()
    prefs = Preferences(rules=[Rule("compile_cache", mode="automatic", enabled=True),
                               Rule("compile_cache", mode="notify", enabled=True)])
    report = evaluate([candidate], prefs, 100)
    assert report.notifications == 1 and report.automatic == []
    prefs.rules.pop()
    assert evaluate([candidate], prefs, 100).automatic == [candidate.key]
    prefs.protected.add(candidate.key)
    assert evaluate([candidate], prefs, 100).automatic == []


def test_cancel_stops_policy_evaluation():
    assert evaluate([entry()], Preferences(), 100, cancel=lambda: True).cancelled
