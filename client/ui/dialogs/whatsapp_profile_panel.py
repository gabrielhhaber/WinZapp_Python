"""WhatsApp settings > Profile: the account's own name, About and photo.

A window of the "WhatsApp settings" category of SettingsDialog. These
live on WhatsApp's servers, not in WinZapp's settings, and are applied with
their own Save button, so closing the dialog never discards anything.

The page opens showing what is set now (name and About in their fields, and
whether there is a photo), read in the background, when the window is first opened, like Privacy. If
that cannot be read the fields simply start empty. Each field is optional: one
left empty or unchanged is left as it is on WhatsApp, and one that fails does
not stop the others. The call runs on a worker thread and the
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
        # What WhatsApp held when the page opened (then, after each Save, what
        # that Save left): a field still equal to it is not sent again. None
        # until the read answers, or for good if it never can.
        self._current = None

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
        # Empty (and so silent) until the read says whether a photo is set.
        self._photo_current = wx.StaticText(self, label="")
        sizer.Add(self._photo_current, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._photo_button = wx.Button(self, label=i18n.t("profile_choose_photo_button"))
        self._photo_button.Bind(wx.EVT_BUTTON, self._on_choose_photo)
        sizer.Add(self._photo_button, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._photo_label = wx.StaticText(self, label=i18n.t("profile_no_photo_chosen"))
        sizer.Add(self._photo_label, 0, wx.ALL, 8)

        self._message = wx.StaticText(self, label=i18n.t("profile_loading"))
        sizer.Add(self._message, 0, wx.ALL, 8)
        self._save_button = wx.Button(self, label=i18n.t("profile_save_button"))
        self._save_button.Bind(wx.EVT_BUTTON, self._on_save)
        sizer.Add(self._save_button, 0, wx.ALL, 8)

        self.SetSizer(sizer)

    def load(self):
        """Read the current profile from WhatsApp (once, when first shown)."""
        self._load_current()

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

    # ── Reading what is set now ───────────────────────────────────────────────

    def _load_current(self):
        def work():
            current = self.main_window.fetch_my_profile()
            wx.CallAfter(self._on_loaded, current)

        threading.Thread(target=work, daemon=True, name="wa-profile-read").start()

    def _show_photo_state(self):
        photo = self._current.photo if self._current is not None else None
        key = {True: "profile_photo_current_set", False: "profile_photo_current_none"}.get(photo)
        self._photo_current.SetLabel(self._i18n.t(key) if key else "")
        self.Layout()

    def _on_loaded(self, current):
        if not self:  # closed while the server was still answering
            return
        if current is None:
            # Not worth a dialog: the page works as it always did, the fields
            # just start empty. Spoken, because a label does not announce itself.
            self._show(self._i18n.t("profile_load_failed"))
            self._say(self._i18n.t("profile_load_failed"))
            return
        self._current = current
        # Whatever the person already typed while this was loading is theirs.
        if current.name and not self._name_field.GetValue():
            self._name_field.SetValue(current.name)
        if current.status and not self._status_field.GetValue():
            self._status_field.SetValue(current.status)
        self._show_photo_state()
        self._show("")

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
            self._name_field.GetValue(), self._status_field.GetValue(), self._photo_path,
            self._current)
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
        # What went through is now what WhatsApp holds: the text stays in its
        # field, as the new baseline, and a chosen photo is done with. What
        # failed stays as it was, ready for another try.
        self._current = profile.current_after_save(self._current, changes, errors)
        if changes.photo and "photo" not in errors:
            self._photo_path = ""
            self._photo_label.SetLabel(i18n.t("profile_no_photo_chosen"))
            self._show_photo_state()

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
