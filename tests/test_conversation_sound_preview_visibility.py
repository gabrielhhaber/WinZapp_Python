"""Sound preview buttons hide when there is nothing to play.

Reported 2026-09-23: in a group's data, choosing "Personalizado" as the
notification sound left the "play preview" button showing with the path field
still empty, and pressing it opened an error box. It now stays hidden until
the field holds a file that exists, checked once typing pauses (EVT_TEXT,
debounced) rather than on every keystroke.

Settings > Tons de alerta had the same two buttons (private and group) with
the same defect, and gets the same rule.

Both dialogs need a wx.App, so the methods run against stubs with fake
controls; the wiring into the handlers is checked structurally.
"""

import inspect

from core.alert_tones import alert_tone_previewable
from ui.dialogs.conversation_data_dialog import ConversationDataDialog
from ui.dialogs.settings_dialog import SettingsDialog

KEYS = ["default", "alert_1", "custom"]


class TestTheRule:
    def test_a_pack_choice_always_has_something_to_play(self):
        assert alert_tone_previewable("default") is True
        assert alert_tone_previewable("alert_1", "") is True

    def test_custom_with_an_empty_path_does_not(self):
        assert alert_tone_previewable("custom", "") is False
        assert alert_tone_previewable("custom", "   ") is False
        assert alert_tone_previewable("custom", None) is False

    def test_custom_with_a_missing_file_does_not(self, tmp_path):
        assert alert_tone_previewable("custom", str(tmp_path / "nao_existe.ogg")) is False

    def test_custom_with_a_folder_does_not(self, tmp_path):
        assert alert_tone_previewable("custom", str(tmp_path)) is False

    def test_custom_with_an_existing_file_does(self, tmp_path):
        sound = tmp_path / "toque.ogg"
        sound.write_bytes(b"x")
        assert alert_tone_previewable("custom", f"  {sound}  ") is True


class _Control:
    def __init__(self, value=""):
        self.value = value
        self.shown = True
        self.alive = True

    def __bool__(self):
        return self.alive

    def GetValue(self):
        return self.value

    def IsShown(self):
        return self.shown

    def Show(self, show=True):
        self.shown = bool(show)

    def GetParent(self):
        return self

    def Layout(self):
        pass


class _Combo:
    def __init__(self, selection):
        self.selection = selection

    def GetSelection(self):
        return self.selection


class _Preview:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


class _Dialog:
    _update_sound_preview_visibility = ConversationDataDialog._update_sound_preview_visibility

    def __init__(self, choice, path=""):
        self._sound_choice_keys = KEYS
        self._sound_combo = _Combo(KEYS.index(choice))
        self._sound_custom_field = _Control(path)
        self._sound_preview_btn = _Control()
        self._sound_preview = _Preview()


class TestTheButton:
    def test_custom_with_no_path_hides_it(self):
        dialog = _Dialog("custom")

        dialog._update_sound_preview_visibility()

        assert dialog._sound_preview_btn.shown is False

    def test_hiding_stops_a_preview_still_playing(self):
        dialog = _Dialog("custom")

        dialog._update_sound_preview_visibility()

        assert dialog._sound_preview.stops == 1

    def test_an_existing_file_brings_it_back(self, tmp_path):
        sound = tmp_path / "toque.ogg"
        sound.write_bytes(b"x")
        dialog = _Dialog("custom")
        dialog._update_sound_preview_visibility()

        dialog._sound_custom_field.value = str(sound)
        dialog._update_sound_preview_visibility()

        assert dialog._sound_preview_btn.shown is True

    def test_leaving_custom_brings_it_back(self):
        dialog = _Dialog("custom")
        dialog._update_sound_preview_visibility()

        dialog._sound_combo.selection = KEYS.index("alert_1")
        dialog._update_sound_preview_visibility()

        assert dialog._sound_preview_btn.shown is True

    def test_an_unchanged_state_touches_nothing(self):
        dialog = _Dialog("default")

        dialog._update_sound_preview_visibility()

        assert dialog._sound_preview_btn.shown is True
        assert dialog._sound_preview.stops == 0

    def test_a_check_landing_after_the_dialog_closed_is_ignored(self):
        dialog = _Dialog("custom")
        dialog._sound_preview_btn.alive = False

        dialog._update_sound_preview_visibility()  # must not raise

        assert dialog._sound_preview.stops == 0


class TestTheWiring:
    def test_the_initial_state_and_the_combo_both_apply_it(self):
        src = inspect.getsource(ConversationDataDialog._update_sound_custom_field_state)
        assert "self._update_sound_preview_visibility()" in src

    def test_typing_checks_once_the_typing_pauses(self):
        src = inspect.getsource(ConversationDataDialog._on_conv_sound_custom_path_changed)
        assert "wx.CallLater(" in src
        assert "self._update_sound_preview_visibility" in src
        assert ".Restart(" in src

    def test_closing_the_dialog_cancels_a_pending_check(self):
        src = inspect.getsource(ConversationDataDialog._on_window_destroy)
        assert "_sound_path_check" in src and ".Stop()" in src


# ── Settings > Tons de alerta ─────────────────────────────────────────────────


class _Settings:
    _update_alert_preview_visibility = SettingsDialog._update_alert_preview_visibility
    _selected_alert_key = SettingsDialog._selected_alert_key

    def __init__(self, private=("default", ""), group=("default", "")):
        self._alert_choice_keys = KEYS
        self._alert_page = _Control()
        self._alert_private_combo = _Combo(KEYS.index(private[0]))
        self._alert_private_custom_field = _Control(private[1])
        self._alert_private_preview_btn = _Control()
        self._alert_private_preview = _Preview()
        self._alert_group_combo = _Combo(KEYS.index(group[0]))
        self._alert_group_custom_field = _Control(group[1])
        self._alert_group_preview_btn = _Control()
        self._alert_group_preview = _Preview()


class TestTheSettingsButtons:
    def test_each_custom_without_a_file_hides_only_its_own_button(self):
        dialog = _Settings(private=("custom", ""), group=("alert_1", ""))

        dialog._update_alert_preview_visibility()

        assert dialog._alert_private_preview_btn.shown is False
        assert dialog._alert_private_preview.stops == 1
        assert dialog._alert_group_preview_btn.shown is True
        assert dialog._alert_group_preview.stops == 0

    def test_both_can_be_hidden(self):
        dialog = _Settings(private=("custom", ""), group=("custom", "  "))

        dialog._update_alert_preview_visibility()

        assert not dialog._alert_private_preview_btn.shown
        assert not dialog._alert_group_preview_btn.shown

    def test_an_existing_file_brings_it_back(self, tmp_path):
        sound = tmp_path / "grupo.ogg"
        sound.write_bytes(b"x")
        dialog = _Settings(group=("custom", ""))
        dialog._update_alert_preview_visibility()

        dialog._alert_group_custom_field.value = str(sound)
        dialog._update_alert_preview_visibility()

        assert dialog._alert_group_preview_btn.shown is True

    def test_a_check_landing_after_the_dialog_closed_is_ignored(self):
        dialog = _Settings(private=("custom", ""))
        dialog._alert_private_preview_btn.alive = False

        dialog._update_alert_preview_visibility()  # must not raise

        assert dialog._alert_private_preview.stops == 0


class TestTheSettingsWiring:
    def test_loading_and_the_combos_apply_it(self):
        src = inspect.getsource(SettingsDialog._update_alert_custom_field_state)
        assert "self._update_alert_preview_visibility()" in src

    def test_both_path_fields_are_watched(self):
        src = inspect.getsource(SettingsDialog._build_ui)
        assert ("self._alert_private_custom_field.Bind(wx.EVT_TEXT, "
                "self._on_alert_custom_path_changed)") in src
        assert ("self._alert_group_custom_field.Bind(wx.EVT_TEXT, "
                "self._on_alert_custom_path_changed)") in src

    def test_typing_still_marks_the_settings_dirty(self):
        """The dialog-level EVT_TEXT binding marks the dialog dirty; a
        control-level handler that does not Skip() swallows it."""
        src = inspect.getsource(SettingsDialog._on_alert_custom_path_changed)
        assert "event.Skip()" in src
        assert "wx.CallLater(" in src and ".Restart(" in src

    def test_closing_the_dialog_cancels_a_pending_check(self):
        src = inspect.getsource(SettingsDialog._stop_alert_previews)
        assert "_alert_path_check" in src and ".Stop()" in src
