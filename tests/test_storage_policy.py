"""User protection and malformed settings cannot broaden storage deletion."""
from dataclasses import replace

import pytest

from core.storage_manager.policy import Preferences, Rule, eligibility, filtered, matching_rules
from core.storage_manager.types import Entry, Root


def entry(**changes):
    value = Entry(Root("cache", "/owned", "compile_cache", managed=True),
                  "a", 100, 1000, (1, 2, 100, 3, 4), "compile_cache")
    return replace(value, **changes)


@pytest.mark.parametrize("raw", [None, [], "yes", {"version": 2}, {"version": True}])
def test_bad_preferences_do_not_enable_automatic_work(raw):
    prefs = Preferences.read(raw)
    assert not prefs.scan_on_start and not prefs.rules and not prefs.interval_minutes


@pytest.mark.parametrize("value", [-1, "bad", float("nan"), float("inf"), True])
def test_malformed_rule_number_does_not_become_an_unbounded_rule(value):
    assert Rule.read(dict(category="compile_cache", days=value, enabled=True)) is None


@pytest.mark.parametrize("category", ["media", "voice", "models", "backups", "sessions", "logs"])
def test_personal_and_critical_data_cannot_get_an_automatic_rule(category):
    assert Rule.read(dict(category=category, mode="automatic", enabled=True)) is None


def test_defaults_are_manual_and_no_file_is_implicitly_protected_by_an_empty_chat_key():
    prefs = Preferences.read({})
    assert not prefs.include_other_accounts and not prefs.scan_on_open
    assert eligibility(entry(), prefs) == "reproducible"


def test_user_protection_wins_over_a_matching_rule():
    value = entry()
    prefs = Preferences(protected={value.key}, rules=[Rule("compile_cache", mode="automatic", enabled=True)])
    assert eligibility(value, prefs) == "user_protected"
    assert matching_rules(value, prefs, 2000) == ()


def test_busy_file_and_external_component_are_not_eligible():
    assert eligibility(entry(), Preferences(), busy=True) == "in_use"
    assert eligibility(entry(root=Root("external", "/other", "compile_cache")), Preferences()) == "external"


def test_received_media_requires_explicit_known_reference():
    media = entry(category="media", media_id="x", reason="received")
    assert eligibility(media, Preferences()) == "local_copy"
    assert eligibility(replace(media, media_id=""), Preferences()) == "protected"
    assert eligibility(replace(media, reason="pending"), Preferences()) == "pending"


def test_suggest_only_rule_wins_over_automatic_match():
    prefs = Preferences(rules=[Rule("compile_cache", mode="automatic", enabled=True),
                               Rule("compile_cache", mode="suggest", enabled=True)])
    assert [r.mode for r in matching_rules(entry(), prefs, 2000)] == ["suggest"]


def test_all_and_any_conditions_have_different_explainable_results():
    value = entry(size=10)
    assert not Rule("compile_cache", days=1, minimum=20, enabled=True).matches(value, 100000)
    assert Rule("compile_cache", days=1, minimum=20, combine="any", enabled=True).matches(value, 100000)


def test_unknown_or_future_dates_do_not_satisfy_age_rules():
    rule = Rule("compile_cache", days=1, enabled=True)
    assert not rule.matches(entry(modified=0), 200000)
    assert not rule.matches(entry(modified=300000), 200000)


def test_preferences_roundtrip_retains_scope_and_explicit_disabled_choices():
    prefs = Preferences(include_other_accounts=True, warning_bytes=2048,
                        protected={"a"}, protected_chats={"private"},
                        rules=[Rule("media", days=30, enabled=False)])
    assert Preferences.read(prefs.as_dict()).as_dict() == prefs.as_dict()


def test_filtering_never_changes_the_original_entries():
    values = [entry(relative="a", size=20), entry(relative="b", size=10)]
    assert [e.relative for e in filtered(values, minimum=15)] == ["a"]
    assert [e.relative for e in values] == ["a", "b"]
