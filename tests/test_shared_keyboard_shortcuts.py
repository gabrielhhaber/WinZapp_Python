"""The keyboard shortcuts are one preference of the install, not of an account.

User report (2026-11-19): with "switch to account 1/2" changed to
Ctrl+Shift+3/Ctrl+Shift+4, Ctrl+Shift+4 reached account 2, but from account 2
only the shipped Ctrl+Alt+1 went back; and the key changed for "back to all
conversations" did nothing in the archived list there. Every account is a
process with its own settings.json, and the overrides lived there: account 2
had never seen them. They now live in global/app.json, mirrored into every
account's settings like the language (core/shared_shortcuts.py).
"""
from unittest.mock import Mock

import pytest

import app_settings
from app_settings import AppSettings
from core import shared_shortcuts
from core.keyboard_shortcuts import SECTION, binding_for
from core.shared_shortcuts import (
    MIGRATED_FLAG, merge_account_overrides, share_account_overrides, shortcuts_to_pull,
)
from core.shortcut_catalog import SHORTCUTS
from main_window import settings as settings_module
from tests.test_global_settings_reconcile import _Window, _stored, _unrelated_save

CTRL_SHIFT = 6


def _key(digit):
    return [CTRL_SHIFT, ord(str(digit))]


# What the user set: the Settings page refuses Ctrl+Shift+3/4 while "remove
# bookmark 3/4" still has them, so those two were freed first.
USERS = {"messages.ID_BOOKMARK_REMOVE[3]": None, "messages.ID_BOOKMARK_REMOVE[4]": None,
         "main.account[1]": _key(3), "main.account[2]": _key(4)}


def _identity(key):
    return key


def _by_id(shortcut_id):
    return next(s for s in SHORTCUTS if s.id == shortcut_id)


@pytest.fixture(autouse=True)
def _call_after_inline(monkeypatch):
    monkeypatch.setattr(settings_module.wx, "CallAfter", lambda fn, *a, **k: fn(*a, **k))


@pytest.fixture(autouse=True)
def _account_dir(tmp_path, monkeypatch):
    account = tmp_path / "account"
    account.mkdir()
    monkeypatch.setattr(settings_module, "data_path",
                        lambda *parts: str(account.joinpath(*parts)))


@pytest.fixture
def refreshed(monkeypatch):
    """The windows refreshed with the new keys (accelerators, menus, labels)."""
    calls = []
    import ui.shortcut_bindings as bindings
    monkeypatch.setattr(bindings, "refresh_shortcuts", calls.append)
    return calls


def _account(global_dir, overrides=None, migrated=False):
    window = _Window(global_dir, general={MIGRATED_FLAG: True} if migrated else None)
    window.i18n = Mock(t=_identity)
    if overrides is not None:
        window.settings[SECTION] = overrides
    window._shortcut_targets = ()
    window._apply_global_settings()
    return window


def _choose_shortcuts(window, overrides):
    """What OK on the Shortcuts page does with a change."""
    window.choose_global_settings({SECTION: overrides})
    window.save_settings()


def _answers(window, shortcut_id):
    return binding_for(_by_id(shortcut_id), window.settings[SECTION], _identity)


def test_the_shared_key_is_the_settings_section():
    assert SECTION in app_settings._DEFAULTS
    assert app_settings._DEFAULTS[SECTION] == {}
    assert app_settings._SECTION_GLOBAL == (SECTION,)


class TestTheReportedScenario:
    def test_keys_set_in_account_1_take_account_2_back_to_account_1(self, tmp_path, refreshed):
        one = _account(tmp_path)
        two = _account(tmp_path)

        _choose_shortcuts(one, {**USERS, "main.ID_ALT_1": [CTRL_SHIFT, ord("V")]})
        # Account 2 sat hidden in the tray, saving nothing; the switch brings
        # it forward.
        two._set_bookmark_zero_hotkey = Mock()
        two._on_window_activate(Mock(GetActive=Mock(return_value=True)))

        assert _answers(two, "main.account[1]") == (CTRL_SHIFT, ord("3"))
        # The key to go back to all conversations from the archived list.
        assert _answers(two, "main.ID_ALT_1") == (CTRL_SHIFT, ord("V"))
        assert refreshed == [two]

    def test_an_unrelated_save_in_account_2_does_not_undo_them(self, tmp_path):
        one = _account(tmp_path)
        two = _account(tmp_path)

        _choose_shortcuts(one, USERS)
        _unrelated_save(two)

        assert _stored(tmp_path, SECTION) == USERS
        assert _answers(two, "main.account[1]") == (CTRL_SHIFT, ord("3"))

    def test_a_save_pulls_and_applies_them_too(self, tmp_path, refreshed):
        one = _account(tmp_path)
        two = _account(tmp_path)

        _choose_shortcuts(one, {"main.account[1]": _key(3)})
        _unrelated_save(two)

        assert refreshed == [two]

    def test_a_change_made_in_account_2_is_written_from_there(self, tmp_path):
        _account(tmp_path)
        two = _account(tmp_path)

        _choose_shortcuts(two, {"main.account[2]": _key(4)})

        assert _stored(tmp_path, SECTION) == {"main.account[2]": _key(4)}


class TestComingForward:
    def test_nothing_changed_elsewhere_nothing_is_refreshed(self, tmp_path, refreshed):
        two = _account(tmp_path)

        two._pull_shared_shortcuts()

        assert refreshed == []

    def test_a_change_of_its_own_not_saved_yet_is_kept(self, tmp_path, refreshed):
        one = _account(tmp_path)
        two = _account(tmp_path)
        _choose_shortcuts(one, {"main.account[1]": _key(3)})
        two.settings[SECTION] = {"main.account[2]": _key(4)}

        two._pull_shared_shortcuts()

        assert two.settings[SECTION] == {"main.account[2]": _key(4)}
        assert refreshed == []


class TestFirstStartOnThisVersion:
    def test_the_account_holding_overrides_hands_them_to_the_install(self, tmp_path):
        """Account 1 had them; account 2 had none. Either may start first."""
        two = _account(tmp_path, overrides={})
        one = _account(tmp_path, overrides=dict(USERS))

        assert _stored(tmp_path, SECTION) == USERS
        assert one.settings["general"][MIGRATED_FLAG] is True
        two._pull_shared_shortcuts()
        assert _answers(two, "main.account[1]") == (CTRL_SHIFT, ord("3"))

    def test_an_account_migrated_already_does_not_bring_back_a_reset_key(self, tmp_path):
        """Its settings.json still holds a key reset in another account since."""
        AppSettings(str(tmp_path)).set(SECTION, {})
        stale = _account(tmp_path, overrides={"main.account[1]": _key(3)}, migrated=True)

        assert _stored(tmp_path, SECTION) == {}
        assert stale.settings[SECTION] == {}

    def test_the_translation_is_loaded_only_when_there_is_something_to_merge(self, tmp_path):
        factory = Mock(return_value=_identity)
        settings = {"general": {}, SECTION: {}}

        share_account_overrides(AppSettings(str(tmp_path)), settings, SHORTCUTS, factory)

        factory.assert_not_called()
        assert settings["general"][MIGRATED_FLAG] is True


class TestMerge:
    def test_the_install_wins_on_a_command_both_set(self):
        merged = merge_account_overrides(USERS, {"main.account[1]": None},
                                         SHORTCUTS, _identity)
        assert merged == USERS

    def test_an_accounts_own_command_is_added(self):
        merged = merge_account_overrides(USERS, {"main.account[3]": None},
                                         SHORTCUTS, _identity)
        assert merged == {**USERS, "main.account[3]": None}

    def test_a_key_another_command_already_answers_to_is_left_out(self):
        """"remove bookmark 3" still holds Ctrl+Shift+3 in the merged set."""
        merged = merge_account_overrides({}, {"main.account[1]": _key(3)},
                                         SHORTCUTS, _identity)
        assert merged == {}

    def test_a_key_freed_by_a_later_entry_is_not_lost_to_the_order(self):
        """account[1] takes Ctrl+Shift+3, which "remove bookmark 3" only
        leaves further down the same dict."""
        local = {"main.account[1]": _key(3), "messages.ID_BOOKMARK_REMOVE[3]": None}
        merged = merge_account_overrides({}, local, SHORTCUTS, _identity)
        assert merged == local

    def test_a_disabled_command_is_carried(self):
        merged = merge_account_overrides({}, {"main.account[9]": None}, SHORTCUTS, _identity)
        assert merged == {"main.account[9]": None}

    def test_unknown_ids_and_bad_values_are_dropped(self):
        merged = merge_account_overrides({"nope": _key(1)}, {"main.account[2]": "x"},
                                         SHORTCUTS, _identity)
        assert merged == {}


class TestPullDecision:
    def test_taken_when_only_the_install_changed(self):
        assert shortcuts_to_pull({}, {}, {"a": 1}) == {"a": 1}

    def test_none_when_nothing_changed(self):
        assert shortcuts_to_pull({"a": 1}, {"a": 1}, {"a": 1}) is None

    def test_none_when_this_account_changed_its_own(self):
        assert shortcuts_to_pull({"b": 2}, {}, {"a": 1}) is None


def test_catalog_translate_without_a_language_is_the_identity():
    assert shared_shortcuts.catalog_translate("")("messages") == "messages"
