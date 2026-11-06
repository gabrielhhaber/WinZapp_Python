"""Preference templates carry choices, never private identities or consent."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.storage_manager.forms import number, portable, imported
from core.storage_manager.policy import Preferences, Rule, eligibility
from core.storage_manager.types import Entry, Root
from ui.dialogs.storage_options import StorageOptionsPanel


@pytest.mark.parametrize("value", ["", "-1", "NaN", "inf", "1.2.3", "2,3,4"])
def test_bad_numeric_form_input_is_rejected(value):
    with pytest.raises(ValueError):
        number(value)


def test_decimal_comma_and_whole_number_validation():
    assert number(" 1,5 ") == 1.5
    assert number("60", integer=True) == 60
    with pytest.raises(ValueError):
        number("1,5", integer=True)


def test_template_does_not_transfer_paths_schedules_or_automatic_consent():
    source = Preferences(protected={"private/path"}, protected_chats={"privatehash"},
        other_accounts=["private/account"], match_media=True, scan_on_open=True, scan_on_start=True,
        interval_minutes=10, paused_until=999999, include_other_accounts=True, include_external_models=True,
        rules=[Rule("compile_cache", enabled=True, mode="automatic")])
    raw = portable(source)
    assert "protected" not in raw and "protected_chats" not in raw and "other_accounts" not in raw
    assert not any(raw[key] for key in ("match_media", "scan_on_open", "scan_on_start", "interval_minutes",
                                        "include_other_accounts", "include_external_models", "paused_until"))
    assert raw["rules"][0]["enabled"] is False
    assert source.rules[0].enabled


def test_import_keeps_existing_protections_but_disables_imported_jobs():
    current = Preferences(protected={"keep"}, protected_chats={"chat"}, protected_categories={"voice"})
    raw = Preferences(scan_on_start=True, rules=[Rule("compile_cache", mode="automatic", enabled=True)]).as_dict()
    result = imported(raw, current)
    assert result.protected == {"keep"} and result.protected_chats == {"chat"}
    assert result.protected_categories == {"voice"} and not result.rules[0].enabled
    with pytest.raises(ValueError):
        imported({"version": 2}, current)


def test_protected_category_and_media_subtype_are_enforced():
    entry = Entry(Root("voice", "/owned", "voice", managed=True), "a", 50, 100,
                  (1, 2, 50, 3, 4), "voice", reason="received", media_id="a", media_type="voiceMessage")
    assert eligibility(entry, Preferences(protected_categories={"voice"})) == "user_protected"
    rule = Rule("voice", enabled=True, media_type="voiceMessage")
    assert rule.matches(entry, 200)
    assert not rule.matches(replace(entry, media_type="audioMessage"), 200)


def test_reset_target_edits_only_that_unsaved_group():
    class Field:
        value = "8"
        def ChangeValue(self, value): self.value = value
    class Choice:
        value = 2
        def SetSelection(self, value): self.value = value
    target, warning = Field(), Field()
    stub = SimpleNamespace(_numbers={"target_bytes": target, "warning_bytes": warning},
                           action=Choice(), target_scope=Choice(), target_category=Choice())
    StorageOptionsPanel.reset_target(stub)
    assert target.value == "0" and warning.value == "8" and stub.action.value == 0


def test_picker_restores_only_keys_that_are_still_visible():
    from ui.dialogs.chat_picker import ChatCheckList
    rebuilt = []
    stub = SimpleNamespace(_options=[("visible", "Name", False)], _checked=set(),
                           _rebuild=lambda: rebuilt.append(1))
    ChatCheckList.set_checked(stub, {"visible", "no-longer-allowed"})
    assert stub._checked == {"visible"} and rebuilt == [1]
