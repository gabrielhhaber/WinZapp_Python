"""The Settings > Geral control that decides message-field spell checking.

Spell checking is on by default, and its cue is a Sound Event — so a user who
wants the checking but not the sound can already silence just that event under
Eventos Sonoros. This control is the other half: it turns the *checking* off,
which is also what stops the Windows COM spell-check service from ever being
touched (core/spell_checker.py only opens it lazily, on the first check).

Three-valued rather than a checkbox, because the honest answer is: Windows has
a spelling setting of its own (Settings > Time & language > Typing > Spelling),
and following it is the default — but a checkbox that silently lost to Windows
would announce a state the app does not actually have, which in an app read out
loud is worse than no control at all. So `spell_check_mode` is one of
"windows" (follow) / "on" / "off", and the two overrides ignore Windows
entirely.

The decision itself is pure (spell_check_active(), core/spell_checker.py) and
is tested here directly. ConversationsPanel is a wx.Panel and cannot be
instantiated without a running wx.App, so its two-line caller is exercised
unbound against a stub carrying only what it touches — the pattern CLAUDE.md
prescribes.
"""

import json
import pathlib

import pytest

import ui.conversations as conversations_module
from ui.conversations import ConversationsPanel
from core.spell_checker import (
    SPELL_CHECK_MODES, spell_check_active, spell_check_mode,
)


_CLIENT = pathlib.Path(__file__).resolve().parents[1] / "client"


class _FakeSpellChecker:
    def __init__(self):
        self.checked = []
        self.reset_to = []

    def text_changed(self, text):
        self.checked.append(text)
        return []

    def reset(self, text=""):
        self.reset_to.append(text)


class _FakeMainWindow:
    def __init__(self, general):
        self.settings = {"general": general}


class _Panel:
    _spell_check_enabled = ConversationsPanel._spell_check_enabled

    def __init__(self, general):
        self.main_window = _FakeMainWindow(general)


@pytest.fixture(autouse=True)
def _windows_setting_unreadable(monkeypatch):
    """Pin Windows' reading to "unknown" by default, so a test that does not
    say otherwise cannot have its result decided by the spelling setting of
    whatever machine happens to run the suite."""
    monkeypatch.setattr(
        conversations_module, "windows_spellcheck_enabled", lambda: None
    )


class TestTheModeIsResolved:
    """spell_check_mode() — what a given settings["general"] actually means."""

    def test_absent_follows_windows(self):
        """A fresh install, and any install whose settings.json predates the
        option, defers to the system rather than guessing."""
        assert spell_check_mode({}) == "windows"

    def test_each_explicit_mode_survives_the_round_trip(self):
        for mode in ("windows", "on", "off"):
            assert spell_check_mode({"spell_check_mode": mode}) == mode

    def test_an_unrecognised_value_follows_windows(self):
        """A typo, a hand-edited file, or a value from a newer version must
        land on the mode that cannot surprise anyone."""
        for junk in ("", "maybe", 7, [], None):
            assert spell_check_mode({"spell_check_mode": junk}) == "windows"

    def test_a_broken_settings_object_follows_windows(self):
        assert spell_check_mode(None) == "windows"


class TestTheLegacyBoolIsMigrated:
    """The option shipped as a plain `spell_check_enabled` bool before Windows'
    own setting was consulted at all."""

    def test_an_explicit_false_becomes_an_explicit_off(self):
        """A user who deliberately turned checking off must not have it
        switched back on by a Windows setting they never looked at."""
        assert spell_check_mode({"spell_check_enabled": False}) == "off"

    def test_a_legacy_true_follows_windows(self):
        """True was the default everyone got without ever choosing it, so it
        reads as "expressed no preference" — freezing it into an override
        would ignore Windows forever on every existing install."""
        assert spell_check_mode({"spell_check_enabled": True}) == "windows"

    def test_the_new_key_wins_over_the_legacy_one(self):
        assert spell_check_mode(
            {"spell_check_mode": "on", "spell_check_enabled": False}
        ) == "on"


class TestTheDecision:
    """spell_check_active() — mode plus Windows' reading gives the answer."""

    def test_windows_mode_follows_windows_in_both_directions(self):
        general = {"spell_check_mode": "windows"}
        assert spell_check_active(general, True) is True
        assert spell_check_active(general, False) is False

    def test_windows_mode_falls_back_to_on_when_unreadable(self):
        """None (registry key/value absent, non-Windows, a permission error —
        see is_windows_spellcheck_enabled()'s own docstring) means "unknown",
        not "off"; the historical default stands in."""
        assert spell_check_active({"spell_check_mode": "windows"}, None) is True

    def test_on_ignores_windows_entirely(self):
        general = {"spell_check_mode": "on"}
        for windows_setting in (True, False, None):
            assert spell_check_active(general, windows_setting) is True

    def test_off_ignores_windows_entirely(self):
        general = {"spell_check_mode": "off"}
        for windows_setting in (True, False, None):
            assert spell_check_active(general, windows_setting) is False


class TestThePanelReadsItLive:
    def test_enabled_by_default_when_nothing_is_stored(self):
        assert _Panel({})._spell_check_enabled() is True

    def test_an_explicit_off_disables_it(self):
        assert _Panel({"spell_check_mode": "off"})._spell_check_enabled() is False

    def test_windows_off_reaches_the_panel(self, monkeypatch):
        monkeypatch.setattr(
            conversations_module, "windows_spellcheck_enabled", lambda: False
        )
        assert _Panel({"spell_check_mode": "windows"})._spell_check_enabled() is False

    def test_an_explicit_on_survives_windows_saying_off(self, monkeypatch):
        """The whole point of keeping the override: Windows' setting is the
        default, not a veto."""
        monkeypatch.setattr(
            conversations_module, "windows_spellcheck_enabled", lambda: False
        )
        assert _Panel({"spell_check_mode": "on"})._spell_check_enabled() is True

    def test_a_broken_settings_object_leaves_checking_on(self):
        """Never let a settings read failure be the thing that silently
        removes a feature the user enabled."""
        class _Broken:
            main_window = None
            _spell_check_enabled = ConversationsPanel._spell_check_enabled

        assert _Broken()._spell_check_enabled() is True


class TestTheShippedDefaults:
    """Both copies of the default have to say the same thing: settings.json is
    seeded from the file, while a key missing from an existing install falls
    back to DEFAULT_SETTINGS."""

    def test_the_seeded_settings_file_follows_windows(self):
        defaults = json.loads(
            (_CLIENT / "data" / "settings_default.json").read_text(encoding="utf-8")
        )
        assert defaults["general"]["spell_check_mode"] == "windows"

    def test_default_settings_follows_windows(self):
        from core.utils import DEFAULT_SETTINGS

        assert DEFAULT_SETTINGS["general"]["spell_check_mode"] == "windows"

    def test_the_retired_bool_is_gone_from_both(self):
        """Leaving it behind would keep seeding a key spell_check_mode()
        reads as a legacy migration source on every fresh install."""
        defaults = json.loads(
            (_CLIENT / "data" / "settings_default.json").read_text(encoding="utf-8")
        )
        from core.utils import DEFAULT_SETTINGS

        assert "spell_check_enabled" not in defaults["general"]
        assert "spell_check_enabled" not in DEFAULT_SETTINGS["general"]


class TestEveryLocaleLabelsTheControl:
    def test_the_group_label_and_all_three_options_are_translated(self):
        language_map = json.loads(
            (_CLIENT / "languages" / "language_map.json").read_text(encoding="utf-8")
        )
        for code in language_map:
            translations = json.loads(
                (_CLIENT / "languages" / f"{code}.json").read_text(encoding="utf-8")
            )
            label = translations.get("spell_check_label", "")
            assert label, code
            # The Geral tab labels all carry a mnemonic; a control without
            # one is unreachable from the keyboard alone.
            assert "&" in label, code
            for mode in SPELL_CHECK_MODES:
                option = translations.get(f"spell_check_mode_{mode}", "")
                assert option, (code, mode)
                # Item labels inside a wx.RadioBox are not mnemonic targets —
                # a bare & there would eat the next character instead.
                assert "&" not in option, (code, mode)

    def test_the_retired_checkbox_key_is_gone(self):
        language_map = json.loads(
            (_CLIENT / "languages" / "language_map.json").read_text(encoding="utf-8")
        )
        for code in language_map:
            translations = json.loads(
                (_CLIENT / "languages" / f"{code}.json").read_text(encoding="utf-8")
            )
            assert "spell_check_enabled_label" not in translations, code


class TestTheComposerHonoursTheFlag:
    """on_change_message_field() is far too entangled to call unbound, so this
    pins the two-branch contract it implements against the checker directly:
    checked while on, re-baselined while off."""

    def test_disabled_still_keeps_the_checkers_baseline_current(self):
        checker = _FakeSpellChecker()
        panel = _Panel({"spell_check_mode": "off"})

        for text in ("o", "ol", "ola "):
            if panel._spell_check_enabled():
                checker.text_changed(text)
            else:
                checker.reset(text)

        assert checker.checked == []
        assert checker.reset_to == ["o", "ol", "ola "]

    def test_enabled_checks_every_keystroke(self):
        checker = _FakeSpellChecker()
        panel = _Panel({"spell_check_mode": "on"})

        for text in ("o", "ol", "ola "):
            if panel._spell_check_enabled():
                checker.text_changed(text)
            else:
                checker.reset(text)

        assert checker.checked == ["o", "ol", "ola "]
        assert checker.reset_to == []
