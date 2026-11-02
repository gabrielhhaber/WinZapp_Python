"""File picker, fallback and preview for the selected Sound Events entry.

Edits the dialog's working copy only; Cancel never changes account settings.
"""
import os
import wx

from core.sound_system import AlertPreviewController, SOUND_EVENTS, resolve_sound_event_path


class SoundEventFiles:
    def __init__(self, dialog, page, sizer):
        self.dialog = dialog
        i18n = dialog.main_window.i18n
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.browse = wx.Button(page, label=i18n.t("sound_event_browse"))
        self.reset = wx.Button(page, label=i18n.t("sound_event_reset"))
        self.preview_button = wx.Button(page, label=i18n.t("sound_event_preview"))
        for button in (self.browse, self.reset, self.preview_button):
            row.Add(button, 0, wx.RIGHT, 8)
        sizer.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.preview = AlertPreviewController(dialog.main_window, self.preview_button,
                                              self.resolve, play_label_key="sound_event_preview",
                                              stop_label_key="sound_event_preview_stop")
        self.browse.Bind(wx.EVT_BUTTON, self._browse)
        self.reset.Bind(wx.EVT_BUTTON, self._reset)
        dialog._sound_events_list.Bind(wx.EVT_LISTBOX, self._stop)
        dialog._sound_pack_combo.Bind(wx.EVT_COMBOBOX, self._stop)
        dialog._sound_event_path_field.Bind(wx.EVT_TEXT, self._stop)

    def resolve(self):
        dialog = self.dialog
        selected = dialog._sound_events_list.GetSelection()
        if not 0 <= selected < len(SOUND_EVENTS):
            return ""
        key = SOUND_EVENTS[selected][0]
        mw = dialog.main_window
        return resolve_sound_event_path(mw._sound_packs.get(dialog._current_pack_id),
                                        mw._default_sound_pack, key,
                                        dialog._sound_event_path_field.GetValue().strip())

    def _stop(self, event):
        self.preview.stop()
        event.Skip()

    def _browse(self, event):
        dialog = self.dialog
        i18n = dialog.main_window.i18n
        current = dialog._sound_event_path_field.GetValue().strip()
        with wx.FileDialog(dialog, message=i18n.t("sound_event_choose_file"),
                           defaultDir=os.path.dirname(current) if os.path.isfile(current) else "",
                           wildcard=i18n.t("sound_event_file_filter"),
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as picker:
            if picker.ShowModal() != wx.ID_OK:
                return
            dialog._sound_event_path_field.SetValue(picker.GetPath())
        dialog._mark_dirty()
        dialog._sound_event_path_field.SetFocus()

    def _reset(self, event):
        dialog = self.dialog
        # SetValue emits EVT_TEXT, updating the working override to empty.
        dialog._sound_event_path_field.SetValue("")
        dialog._update_sound_event_path_display()
        dialog._mark_dirty()

    def relabel(self):
        i18n = self.dialog.main_window.i18n
        self.browse.SetLabel(i18n.t("sound_event_browse"))
        self.reset.SetLabel(i18n.t("sound_event_reset"))
        self.preview.refresh_label()
