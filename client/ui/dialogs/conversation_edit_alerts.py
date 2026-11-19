"""A conversation's own sounds and announcements for edited messages (#824).

Opened by the "Edited messages" button of the conversation's data window.
Everything starts at "Default", which follows Settings (Sound Events and
Notifications); a choice here is saved at once, like the conversation's
notification sound. The rules live in core/edit_alerts.py and
main_window/edit_alerts.py.

One event at a time, like Settings > Sound Events: pick the event, then its
sound, so the window keeps a single sound combo, path field and preview
button (and a handful of Alt letters) for three events.
"""

import wx

from core import edit_alerts
from core.alert_tones import alert_tone_previewable
from core.sound_system import (
    AlertPreviewController, discover_alert_tone_choices, resolve_alert_tone_path,
)

#: The events in the order the Event combo lists them, with their labels.
SCOPE_LABEL_KEYS = (
    (edit_alerts.SAVED, "edit_alerts_scope_saved"),
    (edit_alerts.FOREGROUND, "edit_alerts_scope_foreground"),
    (edit_alerts.BACKGROUND, "edit_alerts_scope_background"),
)


def sound_choice_keys(alert_keys) -> list:
    """The Sound combo's choices: default, off, the event's own sound, the
    pack's alert tones, custom."""
    return ([edit_alerts.CHOICE_DEFAULT, edit_alerts.CHOICE_OFF, edit_alerts.CHOICE_EVENT]
            + list(alert_keys) + [edit_alerts.CHOICE_CUSTOM])


def choice_index(keys, choice) -> int:
    """Where *choice* sits in *keys*; an alert tone the pack no longer has
    reads as "Default"."""
    try:
        return keys.index(choice)
    except ValueError:
        return 0


def show_edit_alerts(parent, main_window, jid, name):
    dialog = ConversationEditAlertsDialog(parent.GetTopLevelParent(), main_window, jid, name)
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()


class ConversationEditAlertsDialog(wx.Dialog):

    def __init__(self, parent, main_window, jid, name):
        i18n = main_window.i18n
        super().__init__(parent, title=i18n.t("edit_alerts_title").format(name=name),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._mw = main_window
        self._jid = main_window._edit_alert_jid(jid)
        settings = main_window.settings
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        sizer.Add(wx.StaticText(panel, label=i18n.t("edit_alerts_event_label")),
                  0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._event_combo = wx.ComboBox(
            panel, style=wx.CB_READONLY,
            choices=[i18n.t(key) for _scope, key in SCOPE_LABEL_KEYS])
        sizer.Add(self._event_combo, 0, wx.EXPAND | wx.ALL, 8)

        alerts = discover_alert_tone_choices(
            main_window.get_active_sound_pack(), main_window._default_sound_pack)
        self._sound_keys = sound_choice_keys([key for key, _label in alerts])
        sizer.Add(wx.StaticText(panel, label=i18n.t("edit_alerts_sound_label")),
                  0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._sound_combo = wx.ComboBox(panel, style=wx.CB_READONLY, choices=(
            [i18n.t("edit_alerts_sound_default"), i18n.t("sound_event_state_disabled"),
             i18n.t("edit_alerts_sound_event")]
            + [label for _key, label in alerts] + [i18n.t("alert_tone_custom")]))
        sizer.Add(self._sound_combo, 0, wx.EXPAND | wx.ALL, 8)

        self._custom_label = wx.StaticText(
            panel, label=i18n.t("conversation_sound_custom_path_label"))
        sizer.Add(self._custom_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._custom_field = wx.TextCtrl(panel, style=wx.TE_DONTWRAP)
        sizer.Add(self._custom_field, 0, wx.EXPAND | wx.ALL, 8)

        self._preview_btn = wx.Button(panel, label=i18n.t("conversation_sound_preview_play"))
        sizer.Add(self._preview_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self._preview = AlertPreviewController(
            main_window, self._preview_btn, self._preview_path,
            play_label_key="conversation_sound_preview_play",
            stop_label_key="conversation_sound_preview_stop")

        speak = wx.RadioBox(
            panel, label=i18n.t("edit_alerts_speak_label"),
            choices=self._toggle_labels(edit_alerts.SPEAK_FOREGROUND),
            majorDimension=1, style=wx.RA_SPECIFY_COLS)
        notify = wx.RadioBox(
            panel, label=i18n.t("edit_alerts_notify_label"),
            choices=self._toggle_labels(edit_alerts.NOTIFY_BACKGROUND),
            majorDimension=1, style=wx.RA_SPECIFY_COLS)
        self._toggles = {edit_alerts.SPEAK_FOREGROUND: speak,
                         edit_alerts.NOTIFY_BACKGROUND: notify}
        for which, radio in self._toggles.items():
            radio.SetSelection(edit_alerts.TOGGLE_CHOICES.index(
                edit_alerts.toggle_choice(settings, self._jid, which)))
            radio.Bind(wx.EVT_RADIOBOX, lambda e, w=which: self._on_toggle(e, w))
            sizer.Add(radio, 0, wx.EXPAND | wx.ALL, 8)

        close = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("back_btn"))
        sizer.Add(close, 0, wx.ALL, 8)
        self.Bind(wx.EVT_BUTTON, self._on_close, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, self._on_close)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizerAndFit(outer)

        self._event_combo.SetSelection(0)
        self._show_scope()
        self._event_combo.Bind(wx.EVT_COMBOBOX, lambda e: self._show_scope())
        self._sound_combo.Bind(wx.EVT_COMBOBOX, self._on_sound_changed)
        self._custom_field.Bind(wx.EVT_TEXT, self._on_custom_path_changed)
        self._event_combo.SetFocus()

    # ── the event's sound ────────────────────────────────────────────────
    def _scope(self) -> str:
        index = max(0, self._event_combo.GetSelection())
        return SCOPE_LABEL_KEYS[index][0]

    def _selected_choice(self) -> str:
        index = self._sound_combo.GetSelection()
        return self._sound_keys[index] if 0 <= index < len(self._sound_keys) else "default"

    def _show_scope(self):
        """Show the stored choice of the event just picked."""
        self._preview.stop()
        choice, custom = edit_alerts.sound_choice(self._mw.settings, self._jid, self._scope())
        self._sound_combo.SetSelection(choice_index(self._sound_keys, choice))
        self._custom_field.ChangeValue(custom)
        self._sync_controls()

    def _sync_controls(self):
        choice = self._selected_choice()
        is_custom = choice == edit_alerts.CHOICE_CUSTOM
        self._custom_label.Show(is_custom)
        self._custom_field.Show(is_custom)
        playable = (choice != edit_alerts.CHOICE_OFF
                    and alert_tone_previewable(choice, self._custom_field.GetValue()))
        if not playable:
            self._preview.stop()
        self._preview_btn.Show(playable)
        self.Layout()

    def _save_sound(self):
        edit_alerts.set_sound_choice(
            self._mw.settings, self._jid, self._scope(), self._selected_choice(),
            self._custom_field.GetValue() if self._selected_choice() == "custom" else "")
        self._mw._schedule_save_settings()

    def _on_sound_changed(self, event):
        self._preview.stop()
        self._save_sound()
        self._sync_controls()

    def _on_custom_path_changed(self, event):
        self._save_sound()
        self._sync_controls()

    def _preview_path(self) -> str:
        """What the preview plays: the event's own file for Default and
        "event", the alert tone or the custom file otherwise."""
        choice = self._selected_choice()
        if choice in (edit_alerts.CHOICE_DEFAULT, edit_alerts.CHOICE_EVENT):
            return self._mw._edit_event_sound(self._scope())[1]
        return resolve_alert_tone_path(
            self._mw.get_active_sound_pack(), self._mw._default_sound_pack,
            choice, self._custom_field.GetValue().strip())

    # ── announcements ────────────────────────────────────────────────────
    def _toggle_labels(self, which) -> list:
        """Default (saying what Settings has now), on, off."""
        i18n = self._mw.i18n
        default = (i18n.t("edit_alerts_toggle_default_on")
                   if edit_alerts.global_toggle(self._mw.settings, which)
                   else i18n.t("edit_alerts_toggle_default_off"))
        return [default, i18n.t("sound_event_state_enabled"),
                i18n.t("sound_event_state_disabled")]

    def _on_toggle(self, event, which):
        edit_alerts.set_toggle_choice(
            self._mw.settings, self._jid, which,
            edit_alerts.TOGGLE_CHOICES[self._toggles[which].GetSelection()])
        self._mw._schedule_save_settings()
        event.Skip()

    def _on_close(self, event):
        self._preview.stop()
        self.EndModal(wx.ID_CANCEL)
