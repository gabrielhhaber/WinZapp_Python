"""Top-level menu bar labels (Arquivo/Sincronização/Ajuda, ...) must not
share an Alt+<letter> mnemonic within the same locale.

Reported live: pt-BR and es-ES both had "Arquivo"/"Ayuda" ("&Arquivo" /
"&Ayuda") mnemonic-ed on the same letter as the File menu (both starting
with A) — Alt+A opened whichever menu wx.MenuBar happened to resolve the
collision to, and the other one had no working Alt shortcut at all.

wx uses the same "&<letter>" convention MainWindow._build_menubar() already
relies on elsewhere (see its own nav_letter-extraction comment in
create_accelerator_table) — a leading "&" before a letter marks that letter
as the mnemonic; "&&" is a literal ampersand and carries no mnemonic.
"""

import json
import re

from app_paths import resource_path

from tests.locales import registered_locale_codes

#: Derived from language_map.json, not repeated here — the set of locales is
#: data, not code (a locale is added by dropping in `<code>.json` plus an entry
#: in that map, with no rebuild), so a list written out here goes stale the
#: moment one is added and silently stops checking it. Through the shared
#: helper because the derivation has to be checked for emptiness before it is
#: looped over — see tests/locales.py.
LOCALES = list(registered_locale_codes())

# Every label MainWindow._build_menubar() adds directly to the wx.MenuBar,
# in i18n key form. acc_menu_title (Accounts) is intentionally excluded: it
# is only appended under the multi-account system and today carries no "&"
# mnemonic in any locale at all.
TOP_LEVEL_MENU_KEYS = ["menu_file", "menu_sync", "menu_help"]


def _load(name):
    with open(resource_path("languages", f"{name}.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _mnemonic(label: str) -> "str | None":
    """The letter after a single '&' (not '&&', a literal ampersand),
    lowercased — or None if the label has no mnemonic at all."""
    # Strip literal "&&" first so it can't be mistaken for a marker, then
    # look for a real "&<letter>".
    stripped = label.replace("&&", "")
    m = re.search(r"&(\w)", stripped)
    return m.group(1).lower() if m else None


class TestTopLevelMenuMnemonicsAreUnique:
    def test_no_two_top_level_menus_share_a_mnemonic(self):
        for locale in LOCALES:
            translations = _load(locale)
            seen = {}
            for key in TOP_LEVEL_MENU_KEYS:
                label = translations.get(key, "")
                letter = _mnemonic(label)
                if letter is None:
                    continue
                assert letter not in seen, (
                    f"{locale}: {key!r} ({label!r}) and {seen.get(letter)!r} "
                    f"both mnemonic on Alt+{letter.upper()}"
                )
                seen[letter] = key

    def test_every_top_level_menu_actually_has_a_mnemonic(self):
        """Not strictly required by wx, but losing the "&" entirely (instead
        of just colliding) is the same bug wearing a different hat — no
        Alt-key shortcut opens that menu at all."""
        for locale in LOCALES:
            translations = _load(locale)
            for key in TOP_LEVEL_MENU_KEYS:
                label = translations.get(key, "")
                assert _mnemonic(label) is not None, f"{locale}: {key!r} ({label!r}) has no '&' mnemonic"


# Reserved as an explicit wx.AcceleratorTable entry, independent of any
# label's own "&" mnemonic — see MainWindow._on_global_alt_t() and issue #78.
_GLOBALLY_RESERVED_ALT_LETTERS = {"t"}

# message_label's mnemonic ("type_message"/"type_message_group") is also
# reused, letter-for-letter, to register an explicit ID_ALT_FOCUS_FIELD
# accelerator in ConversationsPanel (see conversations.py's own
# _mnemonic_letter() comment) — so these two keys must always agree with
# each other, and reply_to/reply_to_group (which replace message_label's
# text while composing a reply) must too, or the visible "&" underline in
# reply mode stops matching the Alt-key that actually fires.
_MESSAGE_FIELD_LABEL_KEYS = [
    "type_message", "type_message_group", "reply_to", "reply_to_group",
]


class TestMessageFieldMnemonicDoesNotCollideWithAltT:
    """Reported live (issue #78): en-US's "type_message" was "&Type a
    message to", mnemonic-ed on T — colliding with the global Alt+T
    "announce contact presence" shortcut. Because ConversationsPanel's own
    accelerator table takes priority while a conversation has focus, Alt+T
    inside an open chat moved focus to the message field instead of
    announcing presence, with no way to reach the presence announcement
    from inside the conversation at all.
    """

    def test_no_message_field_label_is_mnemonic_ed_on_a_reserved_letter(self):
        for locale in LOCALES:
            translations = _load(locale)
            for key in _MESSAGE_FIELD_LABEL_KEYS:
                label = translations.get(key, "")
                letter = _mnemonic(label)
                assert letter not in _GLOBALLY_RESERVED_ALT_LETTERS, (
                    f"{locale}: {key!r} ({label!r}) is mnemonic-ed on "
                    f"Alt+{letter.upper() if letter else '?'}, which collides "
                    "with a globally reserved shortcut"
                )

    def test_no_attachment_panel_button_is_mnemonic_ed_on_a_reserved_letter(self):
        # The staged-attachment panel sits in the same conversation panel,
        # so its buttons lose to the window's Alt+T as well: ro.json's
        # "&Trimite" announced presence instead of sending the files.
        for locale in LOCALES:
            translations = _load(locale)
            for key in ("send_attachment", "add_more_files"):
                label = translations.get(key, "")
                assert _mnemonic(label) not in _GLOBALLY_RESERVED_ALT_LETTERS, (
                    f"{locale}: {key!r} ({label!r}) collides with a globally "
                    "reserved shortcut"
                )

    def test_type_message_and_its_variants_share_one_letter_per_locale(self):
        for locale in LOCALES:
            translations = _load(locale)
            letters = {
                key: _mnemonic(translations.get(key, ""))
                for key in _MESSAGE_FIELD_LABEL_KEYS
            }
            assert len(set(letters.values())) == 1, f"{locale}: {letters!r}"
