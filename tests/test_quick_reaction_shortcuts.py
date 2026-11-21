"""Issue #852: ten commands that react to the focused message with the rows of
Settings > Reactions. No default key: Ctrl+Alt+1..9 switch accounts."""

import pathlib
import re
import types

import pytest

import core.keyboard_shortcuts as keyboard_shortcuts
from core.shortcut_catalog import SHORTCUTS
from core.reaction_shortcuts import DEFAULT_QUICK_REACTIONS
from tests.locales import load_strings, registered_locale_codes
from ui.conversation_panel.reactions import ReactionsMixin

CLIENT = pathlib.Path(__file__).resolve().parent.parent / "client"
LOCALES = registered_locale_codes()
ROWS = [s for s in SHORTCUTS if s.command.startswith("ID_QUICK_REACTION[")]


class _Panel:
    _SELF_REACTOR_KEY = ReactionsMixin._SELF_REACTOR_KEY
    _on_accel_quick_reaction = ReactionsMixin._on_accel_quick_reaction

    def __init__(self, slots=None, mine="", selected=0):
        self.conversation = {"remoteJid": "x@s.whatsapp.net"}
        self._sorted_messages = [{"key": {"id": "m1"}}]
        self._reaction_map = {"m1": {self._SELF_REACTOR_KEY: mine}} if mine else {}
        reactions = {} if slots is None else {"quick_reaction_slots": slots}
        self.main_window = types.SimpleNamespace(settings={"reactions": reactions})
        self.messages_list = types.SimpleNamespace(GetFirstSelected=lambda: selected)
        self.sent = []

    def _is_separator(self, msg):
        return False

    def _reject_system_event_action(self, msg):
        return False

    def _send_reaction(self, msg, emoji):
        self.sent.append((msg["key"]["id"], emoji))


def test_digit_one_sends_the_first_configured_row():
    panel = _Panel()
    panel._on_accel_quick_reaction(1)
    assert panel.sent == [("m1", DEFAULT_QUICK_REACTIONS[0])]


def test_digit_zero_is_the_tenth_row():
    panel = _Panel()
    panel._on_accel_quick_reaction(0)
    assert panel.sent == [("m1", DEFAULT_QUICK_REACTIONS[9])]


def test_the_assignment_is_the_users_whatever_the_usage():
    slots = list(DEFAULT_QUICK_REACTIONS)
    slots[1] = "🐶"
    panel = _Panel(slots=slots)
    panel.main_window.settings["reaction_recent_emojis"] = ["😂"] * 30
    panel._on_accel_quick_reaction(2)
    assert panel.sent == [("m1", "🐶")]


def test_pressing_the_reaction_already_sent_removes_it():
    panel = _Panel(mine=DEFAULT_QUICK_REACTIONS[0])
    panel._on_accel_quick_reaction(1)
    assert panel.sent == [("m1", "")]


def test_another_reaction_replaces_mine():
    panel = _Panel(mine=DEFAULT_QUICK_REACTIONS[0])
    panel._on_accel_quick_reaction(2)
    assert panel.sent == [("m1", DEFAULT_QUICK_REACTIONS[1])]


def test_nothing_selected_or_no_conversation_does_nothing():
    panel = _Panel(selected=-1)
    panel._on_accel_quick_reaction(1)
    closed = _Panel()
    closed.conversation = None
    closed._on_accel_quick_reaction(1)
    assert panel.sent == closed.sent == []


class TestTheCatalogue:
    def test_one_row_per_digit_in_the_messages_context(self):
        assert sorted(s.command for s in ROWS) == sorted(f"ID_QUICK_REACTION[{d}]" for d in range(10))
        assert {(s.scope, s.label) for s in ROWS} == {("messages", "shortcut_quick_reaction_label")}

    @pytest.mark.parametrize("locale", LOCALES)
    def test_no_default_key_in_any_language(self, locale):
        translate = load_strings(locale).__getitem__
        assert [keyboard_shortcuts.default_binding(s, translate) for s in ROWS] == [None] * 10

    @pytest.mark.parametrize("locale", LOCALES)
    def test_the_label_exists_without_a_colon(self, locale):
        label = load_strings(locale)["shortcut_quick_reaction_label"]
        assert label and ":" not in label

    def test_ctrl_alt_digit_is_refused_while_accounts_hold_it(self):
        table = load_strings("en-US").__getitem__
        [row] = [s for s in ROWS if s.command == "ID_QUICK_REACTION[1]"]
        taken = keyboard_shortcuts.conflicts(row, (3, ord("1")), {}, SHORTCUTS, table)
        assert {s.id for s in taken} == {"main.account[1]"}


def test_each_command_is_bound_to_its_handler():
    source = (CLIENT / "ui/conversation_panel/accelerators.py").read_text(encoding="utf-8")
    assert re.search(r"self\._on_accel_quick_reaction\(d\),\s*id=self\.ID_QUICK_REACTION\[_d\]", source)
