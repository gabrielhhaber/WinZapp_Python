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

Start with Windows and the locked-chat navigation policy are not toggled: they
are not settings.json values (see NOT_BACKED_BY_SETTINGS in the static wiring
test). Applying the first would write the runner's real Windows registry; the
second belongs to the authenticated encrypted vault and has its own focused
tests.

Needs a real wx.App — see tests/test_settings_dialog_apply_button.py's
docstring for why this dialog cannot be exercised against a stub.
"""

import pytest
import wx

from core.i18n import I18n
from core.sound_system import DEFAULT_PACK_ID
from core.utils import DEFAULT_SETTINGS
from tests.conftest import hidden_frame
from tests.test_settings_checkboxes_wiring import checkbox_keys
from ui.dialogs.settings_dialog import SettingsDialog

# Creates a REAL top-level wx dialog - see the wxgui marker in pytest.ini.
pytestmark = pytest.mark.wxgui


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
    # _apply_values() reports storage changes to the main window; the stub
    # has no sweep to start.
    frame._on_auto_download_settings_changed = lambda old, new: None
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


def test_show_typing_row_box_sits_on_the_ui_tab_and_apply_refreshes_the_open_chat(make_dialog):
    """Pinned by name as well as by the parse above: the key
    ui/conversation_panel/typing_row.py reads on every presence update (off by
    default). Apply must also refresh the open conversation, so the row shows
    or goes away at once instead of at the next presence event."""
    assert ("_show_typing_row_cb", "user_interface", "show_typing_row") in CHECKBOXES

    class _ConversationsPanel:
        def __init__(self):
            self.refreshed = 0

        def refresh_typing_row(self):
            self.refreshed += 1

    dialog = make_dialog({})
    panel = _ConversationsPanel()
    dialog.main_window.conversations_panel = panel
    box = dialog._show_typing_row_cb
    assert box.GetParent() is dialog._ui_page
    assert box.GetValue() is False

    box.SetValue(True)
    assert dialog._apply_values() is True
    assert dialog.main_window.settings["user_interface"]["show_typing_row"] is True
    assert panel.refreshed == 1

    assert make_dialog(dialog.main_window.settings)._show_typing_row_cb.GetValue() is True


class TestTheUserInterfacePageScrolls:
    """Settings > User interface holds more options than a screen is tall. On a
    plain panel the sizer squeezed the bottom ones to a height of zero, and
    NVDA, which finds a control's group box by geometry, read "Posição para
    anunciar itens selecionados" on the checkbox after the group instead of on
    its radio buttons. The mechanism is pinned without a dialog in
    test_settings_ui_page_scrolls.py; this is the real page."""

    @staticmethod
    def _small(dlg):
        """The dialog far shorter than the page, with that page in front."""
        notebook = dlg._notebook
        for index in range(notebook.GetPageCount()):
            if notebook.GetPage(index) is dlg._ui_page:
                notebook.SetSelection(index)
        dlg.SetSize((760, 420))
        dlg.Layout()
        dlg._ui_page.Layout()
        dlg._ui_page.FitInside()
        return dlg._ui_page

    def test_the_page_is_a_scrolled_panel(self, make_dialog):
        from wx.lib.scrolledpanel import ScrolledPanel
        assert isinstance(make_dialog()._ui_page, ScrolledPanel)

    def test_no_option_is_squeezed_out_of_existence(self, make_dialog):
        page = self._small(make_dialog())
        flat = [child for child in page.GetChildren()
                if child.IsShown() and child.GetSize().height <= 0]
        assert not flat, [child.GetLabel() for child in flat]

    def test_every_radio_button_sits_inside_a_group_box(self, make_dialog):
        from tests.test_settings_ui_page_scrolls import group_box_of
        page = self._small(make_dialog())
        boxes = [child for child in page.GetChildren() if isinstance(child, wx.StaticBox)]
        radios = [child for child in page.GetChildren() if isinstance(child, wx.RadioButton)]
        assert boxes and radios
        outside = [radio.GetLabel() for radio in radios if group_box_of(radio, boxes) is None]
        assert not outside

    def test_the_reported_group_and_the_checkbox_after_it(self, make_dialog):
        from tests.test_settings_ui_page_scrolls import group_box_of
        dlg = make_dialog()
        page = self._small(dlg)
        boxes = [child for child in page.GetChildren() if isinstance(child, wx.StaticBox)]
        for radio in (dlg._selected_announce_start_rb, dlg._selected_announce_end_rb):
            assert group_box_of(radio, boxes) is dlg._selected_announce_box
        assert group_box_of(dlg._show_yesterday_label_cb, boxes) is None
