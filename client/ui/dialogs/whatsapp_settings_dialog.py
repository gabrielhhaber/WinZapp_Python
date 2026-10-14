"""File > WhatsApp settings: settings kept on the paired WhatsApp account.

Not WinZapp's settings (that is SettingsDialog): these live on WhatsApp's
servers, are reached through the local server (main_window/whatsapp_privacy.py)
and are applied with their own button, so closing the dialog never discards
anything. A notebook from the start, so other account pages can join Privacy:
Profile (name, About, photo) is its own panel, ui/dialogs/whatsapp_profile_panel.py.

Every call to the server runs on a worker thread and reports back through
``wx.CallAfter``: a call can take several seconds, and a frozen window is
indistinguishable from a crashed program for a screen-reader user. Results are
also spoken, because a changing static label is not announced on its own.

What is shown and sent is decided in core/whatsapp_privacy.py (no wx, tested).
"""

import threading

import wx

from core import whatsapp_privacy as privacy
from ui.dialogs.whatsapp_profile_panel import WhatsAppProfilePanel


class WhatsAppSettingsDialog(wx.Dialog):
    def __init__(self, parent, main_window):
        self.main_window = main_window
        i18n = main_window.i18n
        super().__init__(
            parent,
            title=i18n.t("wa_settings_dialog_title"),
            size=(480, 620),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._i18n = i18n
        self._loaded = None         # the six settings, once WhatsApp reported them
        self._status_loaded = None  # privacy.StatusPrivacy, or None if unreadable
        self._status_people = None  # list chosen in the picker; None = untouched

        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)
        self._notebook = wx.Notebook(panel)
        self._build_privacy_tab(i18n)
        self._notebook.AddPage(
            WhatsAppProfilePanel(self._notebook, main_window), i18n.t("wa_settings_tab_profile"))
        outer.Add(self._notebook, 1, wx.EXPAND | wx.ALL, 8)

        # OK, not Close: each page applies through its own button, so closing
        # never discards anything.
        ok_button = wx.Button(panel, wx.ID_OK, i18n.t("ok"))
        ok_button.Bind(wx.EVT_BUTTON, lambda event: self.EndModal(wx.ID_OK))
        outer.Add(ok_button, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        panel.SetSizer(outer)

        frame = wx.BoxSizer(wx.VERTICAL)
        frame.Add(panel, 1, wx.EXPAND)
        self.SetSizer(frame)
        self.Bind(wx.EVT_CLOSE, lambda event: self.EndModal(wx.ID_OK))

        self._load_privacy()

    def _say(self, text):
        self.main_window.speak_output.output(text)

    def _error_box(self, text):
        wx.MessageBox(
            text,
            self._i18n.t("error").format(app_name=self.main_window.app_name),
            wx.OK | wx.ICON_ERROR, self,
        )

    # ── Building ──────────────────────────────────────────────────────────────

    def _build_privacy_tab(self, i18n):
        panel = wx.ScrolledWindow(self._notebook)
        panel.SetScrollRate(0, 20)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(panel, label=i18n.t("wa_privacy_section_label")), 0, wx.ALL, 8)

        # Each dropdown is preceded by its own StaticText: NVDA reads the text
        # right before a control as that control's name.
        self._choices = {}
        for field in privacy.PRIVACY_FIELDS:
            sizer.Add(wx.StaticText(panel, label=i18n.t(field.label_key)), 0,
                      wx.LEFT | wx.TOP | wx.RIGHT, 8)
            choice = wx.Choice(
                panel, choices=[i18n.t(label_key) for _value, label_key in field.options])
            choice.Disable()  # until WhatsApp has told us the current values
            self._choices[field.setting] = choice
            sizer.Add(choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        sizer.Add(wx.StaticText(panel, label=i18n.t("wa_privacy_status_label")), 0,
                  wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._status_choice = wx.Choice(
            panel, choices=[i18n.t(label_key) for _value, label_key in privacy.STATUS_OPTIONS])
        self._status_choice.Disable()
        self._status_choice.Bind(wx.EVT_CHOICE, lambda event: self._refresh_status_controls())
        sizer.Add(self._status_choice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self._status_people_button = wx.Button(
            panel, label=i18n.t("wa_privacy_status_choose_button"))
        self._status_people_button.Disable()
        self._status_people_button.Bind(wx.EVT_BUTTON, self._on_choose_status_people)
        sizer.Add(self._status_people_button, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._status_summary = wx.StaticText(panel, label="")
        sizer.Add(self._status_summary, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 8)

        self._message = wx.StaticText(panel, label=i18n.t("wa_privacy_loading"))
        sizer.Add(self._message, 0, wx.ALL, 8)
        self._apply_button = wx.Button(panel, label=i18n.t("wa_privacy_apply_button"))
        self._apply_button.Disable()
        self._apply_button.Bind(wx.EVT_BUTTON, self._on_apply)
        sizer.Add(self._apply_button, 0, wx.ALL, 8)

        panel.SetSizer(sizer)
        self._notebook.AddPage(panel, i18n.t("wa_settings_tab_privacy"))

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load_privacy(self):
        def work():
            answer = self.main_window.fetch_privacy_settings()
            wx.CallAfter(self._on_loaded, answer)

        threading.Thread(target=work, daemon=True, name="wa-privacy-read").start()

    def _on_loaded(self, answer):
        if not self:  # closed while the server was still answering
            return
        i18n = self._i18n
        if not answer:
            self._message.SetLabel(i18n.t("wa_privacy_load_failed"))
            self._say(i18n.t("wa_privacy_load_failed"))
            return
        settings = answer["settings"]
        if privacy.unreadable(settings):
            # WhatsApp answered, but with nothing this window understands.
            # Silent empty dropdowns look like a working tab that ignores you.
            message = i18n.t("wa_privacy_unreadable_msg")
            self._message.SetLabel(message)
            self._error_box(message)
            return
        self._loaded = dict(settings)
        for field in privacy.PRIVACY_FIELDS:
            choice = self._choices[field.setting]
            choice.SetSelection(privacy.option_index(field, settings.get(field.setting)))
            choice.Enable()
        notes = []
        unknown = privacy.unrecognized_fields(settings)
        if unknown:
            notes.append(i18n.t("wa_privacy_unrecognized_fields").format(
                settings=", ".join(i18n.t(field.label_key) for field in unknown)))

        self._status_loaded = privacy.parse_status_privacy(answer.get("status"))
        if self._status_loaded is None:
            notes.append(i18n.t("wa_privacy_status_unavailable"))
        else:
            self._status_choice.SetSelection(
                privacy.status_option_index(self._status_loaded.setting))
            self._status_choice.Enable()
            self._refresh_status_controls()

        self._message.SetLabel("\n".join(notes))
        self._message.Wrap(420)
        self._apply_button.Enable()
        self.Layout()
        # A disabled Status choice just drops out of the Tab order, and the
        # dropdowns turning live is otherwise silent: say what happened.
        self._say(" ".join(notes) if notes else i18n.t("wa_privacy_loaded"))

    # ── Status audience ───────────────────────────────────────────────────────

    def _status_setting(self) -> str:
        index = self._status_choice.GetSelection()
        if not 0 <= index < len(privacy.STATUS_OPTIONS):
            return ""
        return privacy.STATUS_OPTIONS[index][0]

    def _shown_status_people(self):
        """The people the summary describes: the picker's choice when made,
        otherwise WhatsApp's own list when it is for the setting now chosen."""
        if self._status_people is not None:
            return self._status_people
        loaded = self._status_loaded
        if loaded is not None and loaded.setting == self._status_setting():
            return loaded.people  # None when WhatsApp's list could not be read
        return []

    def _refresh_status_controls(self):
        i18n = self._i18n
        setting = self._status_setting()
        needs_people = setting in (privacy.STATUS_EXCEPT, privacy.STATUS_ONLY)
        self._status_people_button.Enable(needs_people)
        if not needs_people:
            self._status_summary.SetLabel("")
            return
        people = self._shown_status_people()
        if people is None:
            self._status_summary.SetLabel(i18n.t("wa_privacy_status_list_unknown"))
        else:
            self._status_summary.SetLabel(
                i18n.t("wa_privacy_status_count").format(count=len(people)))
        self._status_summary.Wrap(420)

    def _on_choose_status_people(self, event):
        from ui.dialogs.status_audience_dialog import StatusAudienceDialog

        setting = self._status_setting()
        title_key = ("wa_privacy_status_title_except" if setting == privacy.STATUS_EXCEPT
                     else "wa_privacy_status_title_only")
        current = self._shown_status_people() or []
        dialog = StatusAudienceDialog(self, self.main_window, self._i18n.t(title_key), current)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self._status_people = dialog.chosen_jids()
        finally:
            dialog.Destroy()
        self._refresh_status_controls()
        self._status_people_button.SetFocus()
        self._say(self._status_summary.GetLabel())

    # ── Applying ──────────────────────────────────────────────────────────────

    def _on_apply(self, event):
        i18n = self._i18n
        chosen = {setting: choice.GetSelection() for setting, choice in self._choices.items()}
        changes = privacy.changed_privacy(self._loaded or {}, chosen)
        status = None
        if self._status_choice.IsEnabled() and self._status_setting():
            status = privacy.status_change(
                self._status_loaded, self._status_setting(), self._status_people,
                to_id=self.main_window.status_list_id)
        if privacy.status_needs_people(status):
            self._message.SetLabel(i18n.t("wa_privacy_status_need_contacts"))
            self._say(i18n.t("wa_privacy_status_need_contacts"))
            self._status_people_button.SetFocus()
            return
        if not changes and status is None:
            self._message.SetLabel(i18n.t("wa_privacy_nothing_changed"))
            self._say(i18n.t("wa_privacy_nothing_changed"))
            return

        self._apply_button.Disable()
        self._message.SetLabel(i18n.t("wa_privacy_applying"))
        self._say(i18n.t("wa_privacy_applying"))
        mw = self.main_window

        def work():
            errors = {}
            for setting, value in changes:
                error = mw.set_privacy_setting(setting, value)
                if error:
                    errors[setting] = error
            # A setter that answered "success" may still have done nothing:
            # read the values back, and only believe what WhatsApp reports.
            sent = [(s, v) for s, v in changes if s not in errors]
            unconfirmed = []
            if sent:
                answer = mw.fetch_privacy_settings()
                unconfirmed = privacy.not_confirmed(
                    sent, answer["settings"] if answer else None)
            status_error = status_unconfirmed = None
            if status is not None:
                confirmed, status_error = mw.set_status_privacy(*status)
                status_unconfirmed = status_error is None and not confirmed
            wx.CallAfter(self._on_applied, changes, errors, unconfirmed,
                         status, status_error, status_unconfirmed)

        threading.Thread(target=work, daemon=True, name="wa-privacy-write").start()

    def _on_applied(self, changes, errors, unconfirmed, status, status_error,
                    status_unconfirmed):
        if not self:
            return
        i18n = self._i18n
        self._apply_button.Enable()
        confirmed, status_confirmed = privacy.apply_outcome(
            changes, errors, unconfirmed, status, status_error, status_unconfirmed)
        if self._loaded is not None:
            self._loaded.update(confirmed)  # confirmed by the read-back
        if status_confirmed:
            self._status_loaded = privacy.StatusPrivacy(status[0], tuple(status[1]))
            self._status_people = None

        labels = {field.setting: i18n.t(field.label_key) for field in privacy.PRIVACY_FIELDS}
        lines = [f"{labels.get(setting, setting)}: {error}" for setting, error in errors.items()]
        if status_error:
            lines.append(f"{i18n.t('wa_privacy_status_label')}: {status_error}")
        not_taken = [labels.get(s, s) for s in unconfirmed]
        if status_unconfirmed:
            not_taken.append(i18n.t("wa_privacy_status_label"))
        if not_taken:
            # No error, and still nothing changed on WhatsApp: the case that
            # looks exactly like success. Name what did not stick.
            lines.append(i18n.t("wa_privacy_not_confirmed_msg").format(
                settings=", ".join(not_taken)))

        self._refresh_status_controls()
        if lines:
            self._message.SetLabel(i18n.t("wa_privacy_apply_partial_error"))
            self._error_box(i18n.t("wa_privacy_apply_partial_error") + "\n\n" + "\n".join(lines))
            return
        self._message.SetLabel(i18n.t("wa_privacy_apply_success"))
        self._say(i18n.t("wa_privacy_apply_success"))
