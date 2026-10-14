"""File > WhatsApp settings > Profile: the account's own name, About and photo.

A page of the WhatsApp settings dialog (whatsapp_settings_dialog.py). These
live on WhatsApp's servers, not in WinZapp's settings, and are applied with
their own Save button, so closing the dialog never discards anything.

Each field is optional: one left empty is left as it is on WhatsApp, and one
that fails does not stop the others. The call runs on a worker thread and the
result comes back through ``wx.CallAfter``: it can take several seconds, and a
frozen window is indistinguishable from a crashed program for a screen-reader
user. Results are also spoken, because a changing static label is not
announced on its own.

What is sent is decided in core/whatsapp_profile.py (no wx, tested) and sent by
main_window/whatsapp_profile.py.
"""

import os
import threading

import wx

from core import whatsapp_profile as profile


class WhatsAppProfilePanel(wx.Panel):
    def __init__(self, parent, main_window):
        super().__init__(parent)
        self.main_window = main_window
        self._i18n = i18n = main_window.i18n
        self._photo_path = ""

        sizer = wx.BoxSizer(wx.VERTICAL)
        note = wx.StaticText(self, label=i18n.t("profile_blank_note"))
        note.Wrap(420)
        sizer.Add(note, 0, wx.ALL, 8)

        # Each field is preceded by its own StaticText: NVDA reads the text
        # right before a control as that control's name.
        sizer.Add(wx.StaticText(self, label=i18n.t("profile_name_label")), 0,
                  wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._name_field = wx.TextCtrl(self)
        sizer.Add(self._name_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        sizer.Add(wx.StaticText(self, label=i18n.t("profile_status_label")), 0,
                  wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._status_field = wx.TextCtrl(self)
        sizer.Add(self._status_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        sizer.Add(wx.StaticText(self, label=i18n.t("profile_photo_label")), 0,
                  wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._photo_button = wx.Button(self, label=i18n.t("profile_choose_photo_button"))
        self._photo_button.Bind(wx.EVT_BUTTON, self._on_choose_photo)
        sizer.Add(self._photo_button, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._photo_label = wx.StaticText(self, label=i18n.t("profile_no_photo_chosen"))
        sizer.Add(self._photo_label, 0, wx.ALL, 8)

        self._message = wx.StaticText(self, label="")
        sizer.Add(self._message, 0, wx.ALL, 8)
        self._save_button = wx.Button(self, label=i18n.t("profile_save_button"))
        self._save_button.Bind(wx.EVT_BUTTON, self._on_save)
        sizer.Add(self._save_button, 0, wx.ALL, 8)

        self.SetSizer(sizer)

    def _say(self, text):
        self.main_window.speak_output.output(text)

    def _error_box(self, text):
        wx.MessageBox(
            text,
            self._i18n.t("error").format(app_name=self.main_window.app_name),
            wx.OK | wx.ICON_ERROR, self,
        )

    def _show(self, text):
        self._message.SetLabel(text)
        self._message.Wrap(420)
        self.Layout()

    # ── Photo ─────────────────────────────────────────────────────────────────

    def _on_choose_photo(self, event):
        i18n = self._i18n
        dialog = wx.FileDialog(
            self, i18n.t("profile_choose_photo_button"),
            wildcard=i18n.t("profile_photo_wildcard"),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self._photo_path = dialog.GetPath()
        finally:
            dialog.Destroy()
        if not self._photo_path:
            return
        name = os.path.basename(self._photo_path)
        self._photo_label.SetLabel(name)
        self.Layout()
        # A changing static label is not announced on its own.
        self._say(name)

    # ── Saving ────────────────────────────────────────────────────────────────

    def _on_save(self, event):
        i18n = self._i18n
        changes = profile.profile_changes(
            self._name_field.GetValue(), self._status_field.GetValue(), self._photo_path)
        if changes.empty:
            self._show(i18n.t("profile_nothing_to_save"))
            self._say(i18n.t("profile_nothing_to_save"))
            return
        if changes.photo and not profile.photo_content_type(changes.photo):
            # Refused here, not on the server: the person is told what to pick.
            self._show(i18n.t("profile_photo_unsupported"))
            self._say(i18n.t("profile_photo_unsupported"))
            self._photo_button.SetFocus()
            return

        self._save_button.Disable()
        self._show(i18n.t("profile_saving"))
        self._say(i18n.t("profile_saving"))
        main_window = self.main_window

        def work():
            errors = main_window.apply_profile_changes(changes)
            wx.CallAfter(self._on_saved, changes, errors)

        threading.Thread(target=work, daemon=True, name="wa-profile-write").start()

    def _on_saved(self, changes, errors):
        if not self:  # closed while the server was still answering
            return
        i18n = self._i18n
        self._save_button.Enable()
        # What went through is cleared; what failed stays, ready for another try.
        done = {field for field, _value in changes.fields()} - set(errors)
        if "name" in done:
            self._name_field.SetValue("")
        if "status" in done:
            self._status_field.SetValue("")
        if "photo" in done:
            self._photo_path = ""
            self._photo_label.SetLabel(i18n.t("profile_no_photo_chosen"))

        if errors:
            labels = {
                "name": i18n.t("profile_name_label"),
                "status": i18n.t("profile_status_label"),
                "photo": i18n.t("profile_photo_label"),
            }
            lines = profile.failure_lines(errors, labels)
            self._show(i18n.t("profile_save_partial_error"))
            self._error_box(i18n.t("profile_save_partial_error") + "\n\n" + "\n".join(lines))
            return
        self._show(i18n.t("profile_save_success"))
        self._say(i18n.t("profile_save_success"))
