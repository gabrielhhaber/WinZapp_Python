"""Mnemonic letters and Alt accelerators, read from the real sources.

Not a test module — a helper for the tests that check a new Alt+letter does not
collide with one that already means something. The letters already taken are
derived, never written out: a hand-written list missed Alt+U (the "unread
separator" accelerator of the open conversation), and two locales shipped a
button on that very letter.
"""

import json
import re
from unittest.mock import MagicMock

import wx

from tests.locales import LANGUAGES_DIR


def load_strings(code: str) -> dict:
    return json.loads((LANGUAGES_DIR / f"{code}.json").read_text(encoding="utf-8"))


def mnemonic(text: str):
    """The letter after the first real "&" of a label, casefolded; None when
    the label has no mnemonic ("&&" is a literal ampersand)."""
    found = re.search(r"&([^&])", text.replace("&&", ""))
    return found.group(1).casefold() if found else None


class _I18n:
    def __init__(self, strings):
        self.strings = strings

    def t(self, key):
        return self.strings[key]


class RecordingPanel:
    """Stands in for a window while its real create_accelerator_table() runs:
    keeps the entries and the handler bound to each id. No window is created."""

    def __init__(self, strings):
        self.i18n = _I18n(strings)
        self.main_window = MagicMock()
        self.main_window.i18n = self.i18n
        self.entries = []
        self.bound = {}

    def SetAcceleratorTable(self, table):
        self.entries = table.entries

    def Bind(self, event, handler, id=None):
        self.bound[id] = handler

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = MagicMock()
        setattr(self, name, value)
        return value

    def handler_for(self, flags, key):
        ids = [i for f, k, i in self.entries if f == flags and k == key]
        assert len(ids) == 1, f"accelerator bound {len(ids)} times"
        return self.bound[ids[0]]


def built_table(build, strings, monkeypatch) -> RecordingPanel:
    """Run *build* (an unbound method that creates an accelerator table) on a
    RecordingPanel. Its ``entries`` are those of every table the method made,
    whichever window it set them on (the open conversation's table goes to a
    child pane, not to the panel itself)."""
    made = []

    class _Table:
        def __init__(self, entries):
            self.entries = list(entries)
            made.extend(self.entries)

    monkeypatch.setattr(wx, "AcceleratorTable", _Table)
    panel = RecordingPanel(strings)
    build(panel)
    panel.entries = made
    return panel


def alt_letters(strings: dict, monkeypatch) -> set:
    """Every letter Alt already triggers in the main window: its own
    accelerators, the chat list's and the open conversation's."""
    from main_window.shortcuts import ShortcutsMixin
    from ui.conversation_panel.accelerators import AcceleratorsMixin

    letters = set()
    for build in (ShortcutsMixin.create_accelerator_table,
                  AcceleratorsMixin.create_accelerator_table,
                  AcceleratorsMixin.create_accel_conversation):
        for flags, key, _id in built_table(build, strings, monkeypatch).entries:
            if flags == wx.ACCEL_ALT and key < 128 and chr(key).isalpha():
                letters.add(chr(key).casefold())
    return letters
