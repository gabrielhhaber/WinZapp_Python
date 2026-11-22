"""The Calls tab's "Create call link" button answers its Alt+<letter>.

A native `&` mnemonic on that button never fired inside the Calls panel (the
same reason Alt+L there is an accelerator). The label now loses its `&` and the
letter becomes an Alt accelerator entry, as for the incoming-call buttons. The
panel needs a wx.App, so its methods run against plain stubs; no window is
created (CLAUDE.md rule 1).
"""

import pytest
import wx

from calls_panel import CallsPanel
from core.shortcut_catalog import SHORTCUTS
from tests.locales import load_strings, registered_locale_codes

KEY = "calls_create_call_link_button"


class _I18n:
    def __init__(self, strings):
        self._strings = strings

    def t(self, key):
        return self._strings[key]


class _Button:
    def __init__(self):
        self.label = None
        self.accessible = "unset"

    def SetLabel(self, label):
        self.label = label

    def SetAccessible(self, accessible):
        self.accessible = accessible


class _Panel:
    ID_ALT_CALL_LINK = 4242

    def __init__(self, text):
        self.main_window = type("MW", (), {"i18n": _I18n({KEY: text})})()
        self._call_link_btn = _Button()


def test_pt_br_label_is_alt_i():
    panel = _Panel("Cr&iar link de ligação")
    CallsPanel._label_call_link_button(panel)
    assert panel._call_link_btn.label == "Criar link de ligação"
    assert panel._call_link_btn.accessible.GetKeyboardShortcut(0) == (wx.ACC_OK, "Alt+I")


def test_a_label_without_a_mnemonic_registers_nothing():
    panel = _Panel("Create call link")
    CallsPanel._label_call_link_button(panel)
    assert panel._call_link_btn.accessible is None


def test_the_catalog_entry_resolves_the_mnemonic_from_the_label():
    entry = next(s for s in SHORTCUTS if s.id == "calls.ID_ALT_CALL_LINK")
    assert (entry.scope, entry.command, entry.mod, entry.key) == (
        "calls", "ID_ALT_CALL_LINK", 1, "@" + KEY)


@pytest.mark.parametrize("locale", registered_locale_codes())
def test_every_locale_label_gets_no_native_mnemonic(locale):
    panel = _Panel(load_strings(locale)[KEY])
    CallsPanel._label_call_link_button(panel)
    assert "&" not in panel._call_link_btn.label
