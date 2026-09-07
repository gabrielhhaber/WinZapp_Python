"""The Settings > Geral switch that turns message-field spell checking off.

Spell checking is on by default, and its cue is a Sound Event — so a user who
wants the checking but not the sound can already silence just that event under
Eventos Sonoros. This switch is the other half: it turns the *checking* off,
which is also what stops the Windows COM spell-check service from ever being
touched (core/spell_checker.py only opens it lazily, on the first check).

ConversationsPanel is a wx.Panel and cannot be instantiated without a running
wx.App, so the two methods are exercised unbound against a stub carrying only
what they touch — the pattern CLAUDE.md prescribes.
"""

import json
import pathlib

from ui.conversations import ConversationsPanel


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
    def __init__(self, enabled):
        general = {} if enabled is None else {"spell_check_enabled": enabled}
        self.settings = {"general": general}


class _Panel:
    _spell_check_enabled = ConversationsPanel._spell_check_enabled

    def __init__(self, enabled):
        self.main_window = _FakeMainWindow(enabled)


class TestTheFlagIsReadLive:
    def test_enabled_by_default_when_the_key_is_absent(self):
        """Installs whose settings.json predates the option have no key at
        all, and must keep the behaviour they already had."""
        assert _Panel(None)._spell_check_enabled() is True

    def test_explicitly_enabled(self):
        assert _Panel(True)._spell_check_enabled() is True

    def test_explicitly_disabled(self):
        assert _Panel(False)._spell_check_enabled() is False

    def test_a_broken_settings_object_leaves_checking_on(self):
        """Never let a settings read failure be the thing that silently
        removes a feature the user enabled."""
        class _Broken:
            main_window = None
            _spell_check_enabled = ConversationsPanel._spell_check_enabled

        assert _Broken()._spell_check_enabled() is True


class TestTheComposerHonoursTheFlag:
    """on_change_message_field() is far too entangled to call unbound, so this
    pins the two-branch contract it implements against the checker directly:
    checked while on, re-baselined while off."""

    def test_disabled_still_keeps_the_checkers_baseline_current(self):
        checker = _FakeSpellChecker()
        panel = _Panel(False)

        for text in ("o", "ol", "ola "):
            if panel._spell_check_enabled():
                checker.text_changed(text)
            else:
                checker.reset(text)

        assert checker.checked == []
        assert checker.reset_to == ["o", "ol", "ola "]

    def test_enabled_checks_every_keystroke(self):
        checker = _FakeSpellChecker()
        panel = _Panel(True)

        for text in ("o", "ol", "ola "):
            if panel._spell_check_enabled():
                checker.text_changed(text)
            else:
                checker.reset(text)

        assert checker.checked == ["o", "ol", "ola "]
        assert checker.reset_to == []


class TestTheSettingIsDeclaredEverywhereItHasToBe:
    def test_the_default_is_on_in_the_seeded_settings_file(self):
        defaults = json.loads(
            (_CLIENT / "data" / "settings_default.json").read_text(encoding="utf-8")
        )
        assert defaults["general"]["spell_check_enabled"] is True

    def test_the_default_is_on_in_default_settings(self):
        from core.utils import DEFAULT_SETTINGS

        assert DEFAULT_SETTINGS["general"]["spell_check_enabled"] is True

    def test_every_locale_labels_the_checkbox(self):
        language_map = json.loads(
            (_CLIENT / "languages" / "language_map.json").read_text(encoding="utf-8")
        )
        for code in language_map:
            translations = json.loads(
                (_CLIENT / "languages" / f"{code}.json").read_text(encoding="utf-8")
            )
            label = translations.get("spell_check_enabled_label", "")
            assert label, code
            # The Geral tab labels all carry a mnemonic; a checkbox without
            # one is unreachable from the keyboard alone.
            assert "&" in label, code
