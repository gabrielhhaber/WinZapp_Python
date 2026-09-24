"""Configurações, every tab, driven through the real dialog: every settings-backed
checkbox opens on the stored value (or the shipped default), and what the user
leaves ticked is what Apply writes — for each one, not just the one someone
remembered to test.

The list of checkboxes is not written out here. It comes from the same parse
tests/test_settings_checkboxes_wiring.py uses, so a checkbox added to any tab
is covered the moment it exists, and that file fails loudly if the parse stops
finding them.

The static file proves load and save name the same key; this one proves that
wiring actually carries the value through wx, which a source reading cannot
see (a load that runs before the control exists, a later write in
_apply_values() overwriting the key, a side effect that resets a box).

Start with Windows is not toggled: it is not a settings.json value (see
NOT_BACKED_BY_SETTINGS there) and applying it would write the real Windows
registry of whoever runs the suite.

Needs a real wx.App — see tests/test_settings_dialog_apply_button.py's
docstring for why this dialog cannot be exercised against a stub.
"""

import pytest

from core.i18n import I18n
from core.sound_system import DEFAULT_PACK_ID
from core.utils import DEFAULT_SETTINGS
from tests.conftest import hidden_frame
from tests.test_settings_checkboxes_wiring import checkbox_keys
from ui.dialogs.settings_dialog import SettingsDialog

# Creates a REAL top-level wx dialog - see the wxgui marker in pytest.ini.
pytestmark = pytest.mark.wxgui


@pytest.fixture(autouse=True)
def _no_stereo_warning_dialog(monkeypatch):
    """Turning stereo voice messages on asks first (a real modal dialog), and
    these tests flip boxes and Apply: it must never be able to block CI."""
    monkeypatch.setattr("ui.dialogs.settings_dialog.ask_stereo_voice",
                        lambda parent, i18n: (True, False))

CHECKBOXES = checkbox_keys()
IDS = [attr for attr, _, _ in CHECKBOXES]


class _FakeSoundSystem:
    def get_output_devices(self):
        return []

    def get_input_devices(self):
        return []

    def apply_output_device(self, name):
        return True

    def apply_effects_device(self, name):
        return True


def _make_frame(settings):
    frame = hidden_frame()
    frame.settings = settings
    frame.app_name = "WinZapp"
    frame.i18n = I18n(frame)
    frame.i18n.get_language()
    frame.wpp_port = 6300
    frame.wpp_custom_api = False
    frame._sound_packs = {DEFAULT_PACK_ID: {"name": "Default", "path": ""}}
    frame._default_sound_pack = {"name": "Default", "path": ""}
    frame.set_global_hotkey = lambda vk, mod: None
    frame.save_settings = lambda: None
    frame.load_sounds = lambda: None
    frame.apply_language_changes = lambda: None
    frame.sound_system = _FakeSoundSystem()
    frame.refresh_sound_packs = lambda: None
    # Turning "show tray icon" off (its default is on) reads and clears
    # main_window.tray_icon; _init_tray is only reached going off -> on, which
    # these tests never do, and is stubbed so a future one cannot build a real
    # tray icon either.
    frame.tray_icon = None
    frame._init_tray = lambda: None
    return frame


@pytest.fixture
def make_dialog(wx_app):
    created = []

    def _make(settings=None):
        dlg = SettingsDialog(_make_frame(settings if settings is not None else {}))
        created.append(dlg)
        return dlg

    yield _make
    # The dialog AND its hidden parent frame, deleted now rather than queued
    # for an idle cycle that never comes (see conftest.destroy_now).
    from tests.conftest import destroy_now
    destroy_now(*created)


def _default(section, key):
    return bool(DEFAULT_SETTINGS[section][key])


def _opposite_of_defaults():
    settings = {}
    for _, section, key in CHECKBOXES:
        settings.setdefault(section, {})[key] = not _default(section, key)
    return settings


@pytest.mark.parametrize("attr, section, key", CHECKBOXES, ids=IDS)
def test_an_install_without_the_key_shows_the_shipped_default(make_dialog, attr, section, key):
    dialog = make_dialog({})

    assert getattr(dialog, attr).GetValue() is _default(section, key)


@pytest.mark.parametrize("attr, section, key", CHECKBOXES, ids=IDS)
def test_a_stored_value_is_what_the_box_shows(make_dialog, attr, section, key):
    """Every key set to the opposite of its default at once: a box loading
    from a neighbour's key shows the wrong state here."""
    dialog = make_dialog(_opposite_of_defaults())

    assert getattr(dialog, attr).GetValue() is (not _default(section, key))


def test_apply_writes_each_box_to_its_own_key(make_dialog):
    expected = {(section, key): not _default(section, key) for _, section, key in CHECKBOXES}
    dialog = make_dialog({})
    for attr, section, key in CHECKBOXES:
        getattr(dialog, attr).SetValue(not _default(section, key))

    assert dialog._apply_values() is True

    stored = dialog.main_window.settings
    wrong = {
        f"{section}.{key}": stored.get(section, {}).get(key)
        for (section, key), value in expected.items()
        if stored.get(section, {}).get(key) is not value
    }
    assert wrong == {}


def test_opening_again_after_apply_keeps_every_choice(make_dialog):
    """The full cycle a user goes through: change, save, reopen."""
    first = make_dialog({})
    for attr, section, key in CHECKBOXES:
        getattr(first, attr).SetValue(not _default(section, key))
    assert first._apply_values() is True

    second = make_dialog(first.main_window.settings)

    wrong = [
        attr for attr, section, key in CHECKBOXES
        if getattr(second, attr).GetValue() is _default(section, key)
    ]
    assert wrong == []


def test_unchanged_boxes_are_saved_as_they_were(make_dialog):
    """Opening Settings and pressing OK must not reset anything.

    The expected values are taken before the dialog opens: the dialog keeps
    the very same settings dict and _apply_values() writes into it, so
    comparing against that dict afterwards would compare it with itself."""
    expected = {(section, key): not _default(section, key) for _, section, key in CHECKBOXES}
    dialog = make_dialog(_opposite_of_defaults())

    assert dialog._apply_values() is True

    stored = dialog.main_window.settings
    assert {(s, k): stored[s][k] for (s, k) in expected} == expected


def test_fixed_quick_reactions_box_sits_on_the_reactions_tab_and_reaches_the_reaction_dialog(
    make_dialog,
):
    """Pinned by name as well as by the parse above: this is the key
    ConversationsPanel._on_menu_react reads to use the twelve configured rows
    (off by default: most-used first)."""
    assert ("_fixed_quick_reactions_cb", "reactions",
            "fixed_quick_reactions") in CHECKBOXES

    dialog = make_dialog({})
    box = dialog._fixed_quick_reactions_cb
    assert box.GetParent() is dialog._reactions_page
    assert box.GetValue() is False

    box.SetValue(True)
    assert dialog._apply_values() is True
    assert dialog.main_window.settings["reactions"]["fixed_quick_reactions"] is True

    assert make_dialog(dialog.main_window.settings)._fixed_quick_reactions_cb.GetValue() is True
