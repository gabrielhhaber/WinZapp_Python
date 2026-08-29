"""Tests for the Settings dialog's Apply button only appearing once a real
change is pending.

Reported live (a small usability request): the Apply button used to be
visible unconditionally, even right after opening the dialog with nothing
touched — misleading, since clicking it did nothing.

_mark_dirty() is bound once at the dialog level for
EVT_CHECKBOX/EVT_RADIOBUTTON/EVT_COMBOBOX/EVT_TEXT and relies on wx's normal
command-event propagation to catch changes from almost every control without
per-control wiring; a handful of controls that already had a dedicated
handler now call event.Skip() so the event still reaches it, and the few
settings changes that fire none of those event types at all (sound event
enable/disable, "activate/deactivate all", importing a sound pack) call
_mark_dirty() directly. These tests exercise that through real wx events
(ProcessEvent), not by calling _mark_dirty() itself, so a control that stops
propagating (or a future control nobody wires up) would show up as a
failure here.

Needs a real wx.App (ConversationsPanel and other wx.Panel-based tests in
this suite avoid this by binding unbound methods onto a stub, but
wx.Dialog's own construction path can't be skipped the same way) — see
conftest.py's session-scoped wx_app fixture docstring for why it must be
shared rather than each test file creating its own wx.App().
"""

import wx
import pytest

from core.i18n import I18n
from core.sound_system import DEFAULT_PACK_ID
from ui.dialogs.settings_dialog import SettingsDialog


class _FakeSoundSystem:
    def get_output_devices(self):
        return []

    def get_input_devices(self):
        return []

    def apply_output_device(self, name):
        return True

    def apply_effects_device(self, name):
        return True


@pytest.fixture
def dialog(wx_app):
    frame = wx.Frame(None)
    frame.settings = {}
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

    dlg = SettingsDialog(frame)
    yield dlg
    dlg.Destroy()


def _fire(control, event_type):
    """Dispatch a real command event through the control's own event
    handler chain, so it propagates exactly the way a genuine user
    interaction would (unlike calling the bound handler directly)."""
    evt = wx.CommandEvent(event_type.typeId, control.GetId())
    evt.SetEventObject(control)
    control.GetEventHandler().ProcessEvent(evt)


class TestInitialState:
    def test_apply_is_hidden_when_the_dialog_first_opens(self, dialog):
        assert dialog._apply_btn.IsShown() is False


class TestGenericControlsMarkDirty:
    def test_a_plain_checkbox_with_no_dedicated_handler(self, dialog):
        dialog._notifications_check.SetValue(not dialog._notifications_check.GetValue())
        _fire(dialog._notifications_check, wx.EVT_CHECKBOX)
        assert dialog._apply_btn.IsShown() is True

    def test_a_text_field_edited_by_the_user(self, dialog):
        """wx.TextCtrl.SetValue() itself fires wx.EVT_TEXT (unlike the
        Checkbox/RadioButton/ComboBox setters) — this is exactly what a
        real keystroke does, no ProcessEvent needed."""
        dialog._messages_page_size_field.SetValue("999")
        assert dialog._apply_btn.IsShown() is True

    def test_a_radio_button_with_its_own_dedicated_handler_still_propagates(self, dialog):
        """_on_self_reference_toggle() processes the event first; it must
        call event.Skip() or _mark_dirty() would never see it."""
        dialog._self_ref_voce_rb.SetValue(True)
        _fire(dialog._self_ref_voce_rb, wx.EVT_RADIOBUTTON)
        assert dialog._apply_btn.IsShown() is True

    def test_a_combo_box_with_its_own_dedicated_handler_still_propagates(self, dialog):
        """_on_sound_pack_selected() likewise must Skip()."""
        dialog._sound_pack_combo.SetSelection(0)
        _fire(dialog._sound_pack_combo, wx.EVT_COMBOBOX)
        assert dialog._apply_btn.IsShown() is True

    def test_the_custom_api_checkbox_dedicated_handler_still_propagates(self, dialog):
        dialog._custom_api_check.SetValue(True)
        _fire(dialog._custom_api_check, wx.EVT_CHECKBOX)
        assert dialog._apply_btn.IsShown() is True

    def test_the_call_alerts_checkbox_dedicated_handler_still_propagates(self, dialog):
        dialog._call_alerts_check.SetValue(False)
        _fire(dialog._call_alerts_check, wx.EVT_CHECKBOX)
        assert dialog._apply_btn.IsShown() is True


class TestControlsWithNoEventOfTheirOwn:
    """These change a setting without ever firing EVT_CHECKBOX/RADIOBUTTON/
    COMBOBOX/TEXT — _mark_dirty() must be called directly at the point of
    change instead of relying on propagation."""

    def test_toggling_a_sound_event_on_or_off(self, dialog):
        dialog._sound_events_list.SetSelection(0)
        dialog._toggle_current_sound_event()
        assert dialog._apply_btn.IsShown() is True

    def test_activate_all_sound_events(self, dialog):
        dialog._set_all_sound_events(True)
        assert dialog._apply_btn.IsShown() is True

    def test_deactivate_all_sound_events(self, dialog):
        dialog._set_all_sound_events(False)
        assert dialog._apply_btn.IsShown() is True


class TestApplyHidesTheButtonAgain:
    def test_clicking_apply_hides_the_button_once_more(self, dialog):
        dialog._notifications_check.SetValue(not dialog._notifications_check.GetValue())
        _fire(dialog._notifications_check, wx.EVT_CHECKBOX)
        assert dialog._apply_btn.IsShown() is True

        dialog._on_apply(None)

        assert dialog._apply_btn.IsShown() is False
        assert dialog._dirty is False

    def test_a_further_change_shows_it_again_after_a_previous_apply(self, dialog):
        dialog._notifications_check.SetValue(not dialog._notifications_check.GetValue())
        _fire(dialog._notifications_check, wx.EVT_CHECKBOX)
        dialog._on_apply(None)
        assert dialog._apply_btn.IsShown() is False

        dialog._keep_muted_silent_check.SetValue(not dialog._keep_muted_silent_check.GetValue())
        _fire(dialog._keep_muted_silent_check, wx.EVT_CHECKBOX)
        assert dialog._apply_btn.IsShown() is True


class TestMessageListModeOptions:
    def test_item_count_option_is_hidden_in_classic_mode_and_shown_in_listbox_mode(self, dialog):
        assert dialog._msg_list_mode_classic_rb.GetValue() is True
        assert dialog._show_listbox_count_cb.IsShown() is False

        dialog._msg_list_mode_listbox_rb.SetValue(True)
        _fire(dialog._msg_list_mode_listbox_rb, wx.EVT_RADIOBUTTON)
        assert dialog._show_listbox_count_cb.IsShown() is True

        dialog._msg_list_mode_classic_rb.SetValue(True)
        _fire(dialog._msg_list_mode_classic_rb, wx.EVT_RADIOBUTTON)
        assert dialog._show_listbox_count_cb.IsShown() is False

    def test_apply_switches_the_live_messages_control_without_restart_flag(self, dialog):
        class _ConversationsPanel:
            def __init__(self):
                self.modes = []

            def apply_message_list_mode(self, mode):
                self.modes.append(mode)

        panel = _ConversationsPanel()
        dialog.main_window.conversations_panel = panel
        dialog._msg_list_mode_listbox_rb.SetValue(True)
        _fire(dialog._msg_list_mode_listbox_rb, wx.EVT_RADIOBUTTON)

        dialog._on_apply(None)

        assert panel.modes == ["listbox"]
        assert dialog.main_window.settings["user_interface"]["message_list_mode"] == "listbox"
        assert dialog._restart_required is False
