"""Keyboard mnemonics of the WhatsApp lists buttons, in every locale.

The two buttons on the chat screen and the buttons of the lists dialog each
carry an Alt+letter. A mnemonic that repeats inside its window, or that
repeats a menu-bar mnemonic, a label of the chat screen or an Alt accelerator,
activates the wrong thing or nothing, and no screen reader says so. Which
letter a locale picks is its own decision; this checks only that the picks do
not collide. The letters already taken are read from the real accelerator
tables and labels (tests/mnemonics.py), not from a list kept here.
"""

from types import SimpleNamespace

import pytest

from tests.locales import registered_locale_codes
from tests.mnemonics import alt_letters, load_strings, mnemonic
from ui.dialogs.chat_lists import _manage_title

#: Buttons on the chat screen (WhatsAppListFilterMixin).
MAIN_KEYS = ("wa_lists_reload", "wa_lists_manage")
#: Buttons of WhatsAppListsDialog. "manage" is its title, not a button.
DIALOG_KEYS = ("wa_lists_reload", "wa_lists_create", "wa_lists_rename",
               "wa_lists_delete", "wa_lists_add", "wa_lists_remove")
#: What the main window's menu bar opens with Alt.
MENU_KEYS = ("menu_file", "menu_sync", "menu_help")
#: Labels of the chat screen whose mnemonic moves the focus.
LABEL_KEYS = ("main_nav", "type_message", "messages")


@pytest.fixture(params=registered_locale_codes())
def strings(request):
    return load_strings(request.param)


def test_every_lists_button_has_a_mnemonic(strings):
    missing = [k for k in set(MAIN_KEYS + DIALOG_KEYS) if mnemonic(strings[k]) is None]
    assert not missing


def test_the_dialog_buttons_do_not_share_a_letter(strings):
    letters = [mnemonic(strings[k]) for k in DIALOG_KEYS]
    assert len(set(letters)) == len(letters), letters


def test_the_chat_screen_buttons_do_not_collide_with_anything_alt_already_does(
        strings, monkeypatch):
    letters = [mnemonic(strings[k]) for k in MAIN_KEYS]
    taken = ({mnemonic(strings[k]) for k in MENU_KEYS + LABEL_KEYS}
             | alt_letters(strings, monkeypatch))
    assert len(set(letters)) == len(letters), letters
    assert not set(letters) & taken, (letters, sorted(t for t in taken if t))


def test_alt_u_is_among_the_letters_read_from_the_accelerator_tables(monkeypatch):
    """The one a hand-written list missed: Alt+U goes to the unread separator
    of the open conversation. If the derivation stops seeing it, the collision
    check above stops meaning anything."""
    assert "u" in alt_letters(load_strings("pt-BR"), monkeypatch)


def test_the_dialog_title_has_no_mnemonic_marker():
    i18n = SimpleNamespace(t=lambda key: "Mana&ge WhatsApp lists")
    assert _manage_title(i18n) == "Manage WhatsApp lists"


def test_the_dialog_title_keeps_a_literal_ampersand():
    i18n = SimpleNamespace(t=lambda key: "&Manage R&&D lists")
    assert _manage_title(i18n) == "Manage R&D lists"
