"""
new_contact.py — WinZapp "Novo contato" dialog.

Collects name, surname and phone number in one of two tabs:

* "phone" (the default): the contact is saved in WhatsApp and synced to the
  phone's address book (core/phone_contacts.py, server route save-contact);
* "local": the contact lives only inside WinZapp, in main_window.contacts.

Either way it is stored in main_window.contacts so it appears in future
searches, and the WhatsApp JID is returned via result_jid / result_name so the
caller can navigate there.

Opened for a conversation known only by its @lid (no phone number learned
yet), it asks for no phone at all and saves under that @lid: the digits of an
@lid are not a phone number, and typed into a phone field they named either
nobody ("this number is not on WhatsApp", about someone the user is talking
to) or a stranger.
"""

import wx

from core import phone_contacts

MODE_LOCAL = "local"
MODE_PHONE = "phone"


def fixed_jid_of(contact_jid: str) -> str:
    """The JID the dialog saves under instead of a typed phone number: the
    conversation's @lid, when that is all WinZapp knows of the person."""
    return contact_jid if (contact_jid or "").endswith("@lid") else ""


def resolve_initial_mode(modes: tuple, initial: str) -> str:
    """The tab the dialog opens on: *initial* when it exists, else the last
    one (the phone-synced tab, which is also the default)."""
    return initial if initial in modes else modes[-1]


class NewContactDialog(wx.Dialog):
    """Dialog for adding a new WhatsApp contact."""

    def __init__(self, main_window, parent=None, prefill_phone: str = "",
                 prefill_name: str = "", prefill_surname: str = "",
                 initial_mode: str = MODE_PHONE,
                 modes: tuple = (MODE_LOCAL, MODE_PHONE),
                 contact_jid: str = ""):
        self._mw = main_window
        self._fixed_jid = fixed_jid_of(contact_jid)
        self._prefill_phone   = "" if self._fixed_jid else prefill_phone
        self._prefill_name    = prefill_name
        self._prefill_surname = prefill_surname
        # A number that is already a synced contact has only the synced tab.
        known_jid = self._fixed_jid or self._jid_of(prefill_phone)
        self._modes = phone_contacts.available_modes(
            phone_contacts.existing_contact(main_window, known_jid) if known_jid else None,
            tuple(modes), MODE_PHONE)
        self._initial_mode = resolve_initial_mode(self._modes, initial_mode)
        self._busy = False
        i18n = main_window.i18n
        super().__init__(
            parent or main_window,
            title=i18n.t("new_contact_title"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        self.result_jid:  str = ""
        self.result_name: str = ""
        self._build_ui(i18n)
        self.SetMinSize((420, -1))
        self.Fit()
        self.CentreOnParent()

    @staticmethod
    def _jid_of(phone: str) -> str:
        digits = phone_contacts.digits_of(phone)
        return digits + "@s.whatsapp.net" if digits else ""

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_page(self, notebook, i18n, mode):
        """One tab: what it does, then name, surname and phone (no phone for
        a conversation known only by its @lid: see the module docstring)."""
        page = wx.Panel(notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)
        hint = wx.StaticText(page, label=i18n.t(
            "new_contact_phone_hint" if mode == MODE_PHONE else "new_contact_local_hint"))
        hint.Wrap(380)
        sizer.Add(hint, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
        fields = {}
        asked = [("name", "contact_name"), ("surname", "contact_surname")]
        if not self._fixed_jid:
            asked.append(("phone", "phone_label"))
        for key, label in asked:
            sizer.Add(wx.StaticText(page, label=i18n.t(label)), 0, wx.LEFT | wx.TOP, 10)
            fields[key] = wx.TextCtrl(page, style=wx.TE_DONTWRAP)
            sizer.Add(fields[key], 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
        sizer.AddSpacer(10)
        page.SetSizer(sizer)
        fields["name"].SetValue(self._prefill_name)
        fields["surname"].SetValue(self._prefill_surname)
        if "phone" in fields:
            fields["phone"].SetValue(self._prefill_phone)
        return page, fields

    def _build_ui(self, i18n):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._notebook = wx.Notebook(panel)
        self._fields = {}
        for mode in self._modes:
            page, self._fields[mode] = self._build_page(self._notebook, i18n, mode)
            self._notebook.AddPage(
                page, i18n.t("new_contact_tab_phone" if mode == MODE_PHONE
                             else "new_contact_tab_local"))
        self._notebook.SetSelection(self._modes.index(self._initial_mode))
        sizer.Add(self._notebook, 1, wx.EXPAND | wx.ALL, 10)

        self._status = wx.StaticText(panel, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT, 10)

        # Buttons
        btn_sizer = wx.StdDialogButtonSizer()
        self._ok_btn = wx.Button(panel, wx.ID_OK)
        self._cancel_btn = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
        btn_sizer.AddButton(self._ok_btn)
        btn_sizer.AddButton(self._cancel_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

        self._ok_btn.Bind(wx.EVT_BUTTON, self._on_add)
        self._notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_tab_changed)
        self._refresh_ok_label()
        self._fields[self._initial_mode]["name"].SetFocus()

    def _mode(self) -> str:
        return self._modes[self._notebook.GetSelection()]

    def _refresh_ok_label(self):
        key = "create_contact_phone" if self._mode() == MODE_PHONE else "create_contact"
        self._ok_btn.SetLabel(self._mw.i18n.t(key))

    def _on_tab_changed(self, event):
        """What was typed follows the person to the other tab."""
        old, new = event.GetOldSelection(), event.GetSelection()
        if 0 <= old < len(self._modes) and 0 <= new < len(self._modes) and old != new:
            for key, field in self._fields[self._modes[old]].items():
                self._fields[self._modes[new]][key].ChangeValue(field.GetValue())
        self._refresh_ok_label()
        event.Skip()

    def _set_busy(self, busy: bool, text: str = ""):
        self._busy = busy
        if busy:
            # The focus is on a control about to be disabled, and a disabled
            # control with the focus leaves the keyboard nowhere.
            self._cancel_btn.SetFocus()
        self._ok_btn.Enable(not busy)
        self._notebook.Enable(not busy)
        self._status.SetLabel(text)
        self.Layout()

    # ── Add contact ───────────────────────────────────────────────────────────

    def _on_add(self, event):
        if self._busy:
            return
        i18n = self._mw.i18n
        mode = self._mode()
        fields = self._fields[mode]

        first   = fields["name"].GetValue().strip()
        surname = fields["surname"].GetValue().strip()
        fixed   = self._fixed_jid
        phone   = "" if fixed else phone_contacts.digits_of(fields["phone"].GetValue())

        if not first:
            wx.MessageBox(
                i18n.t("contact_name"),
                i18n.t("app_name"),
                wx.OK | wx.ICON_WARNING,
                self,
            )
            fields["name"].SetFocus()
            return

        if not fixed and (not phone or len(phone) < phone_contacts.MIN_DIGITS):
            wx.MessageBox(
                i18n.t("create_contact_error"),
                i18n.t("app_name"),
                wx.OK | wx.ICON_WARNING,
                self,
            )
            fields["phone"].SetFocus()
            return

        full_name = f"{first} {surname}".strip()
        jid       = fixed or phone + "@s.whatsapp.net"

        if mode == MODE_PHONE:
            self._save_synced(jid, first, surname, full_name)
            return

        if phone_contacts.is_phone_synced(phone_contacts.existing_contact(self._mw, jid)):
            # Typed into the local tab, but the number is already in the phone's
            # address book: send them to the synced tab instead of hiding it.
            wx.MessageBox(i18n.t("new_contact_local_blocked"), i18n.t("app_name"),
                          wx.OK | wx.ICON_INFORMATION, self)
            if MODE_PHONE in self._modes:
                self._notebook.SetSelection(self._modes.index(MODE_PHONE))
                self._fields[MODE_PHONE]["name"].SetFocus()
            return

        # Local: stored, persisted, mirrored onto the person's @lid record and
        # shown everywhere at once (MainWindow.save_local_contact()). It only
        # lives inside WinZapp.
        self._mw.save_local_contact(jid, phone_contacts.local_entry(jid, full_name))
        self.result_jid  = jid
        self.result_name = full_name
        self.EndModal(wx.ID_OK)

    def _save_synced(self, jid, first, surname, full_name):
        """Save in WhatsApp (synced to the phone), then keep the record here.

        The request runs off the main thread. The record is stored even when
        the dialog was closed meanwhile: WhatsApp has the contact by then. It
        is filed under the JID WhatsApp answered, which may differ from the
        typed number in the Brazilian 9th digit; a record this WinZapp already
        had under the typed form is replaced, not left next to it.
        """
        mw = self._mw
        i18n = mw.i18n
        saving = i18n.t("new_contact_phone_saving")
        self._set_busy(True, saving)
        mw.output(saving)

        def _done(result):
            saved_jid = result.jid or jid
            if result.ok:
                previous = phone_contacts.existing_contact(mw, jid)
                previous_key = phone_contacts.key_of(mw, previous, "") if previous else ""
                if previous_key and previous_key != saved_jid:
                    mw.remove_local_contact(previous_key)
                mw.save_local_contact(saved_jid, phone_contacts.synced_entry(
                    saved_jid, full_name, confirmed=result.synced))
                mw.output(i18n.t("new_contact_phone_saved" if result.synced
                                 else "new_contact_phone_saved_unconfirmed"))
            # Closed while the request was running: gone, or no longer modal
            # (ShowModal() has returned and the caller is about to destroy it).
            if not self or not self.IsModal():
                return
            if result.ok:
                self.result_jid  = saved_jid
                self.result_name = full_name
                self._set_busy(False)
                self.EndModal(wx.ID_OK)
                return
            text = i18n.t(result.error_key)
            self._set_busy(False, text)
            mw.output(text)
            wx.MessageBox(text, i18n.t("app_name"), wx.OK | wx.ICON_WARNING, self)
            fields = self._fields[MODE_PHONE]
            fields.get("phone", fields["name"]).SetFocus()

        mw.save_phone_synced_contact(jid, first, surname, _done)
