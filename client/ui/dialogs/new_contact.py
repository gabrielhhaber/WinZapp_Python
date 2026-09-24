"""
new_contact.py — WinZapp "Novo contato" dialog.

Collects name, surname and phone number.  Stores the contact locally in
main_window.contacts so it appears in future searches, then returns the
WhatsApp JID via result_jid / result_name so the caller can navigate there.
"""

import re
import wx


class NewContactDialog(wx.Dialog):
    """Dialog for adding a new WhatsApp contact."""

    def __init__(self, main_window, parent=None, prefill_phone: str = "",
                 prefill_name: str = "", prefill_surname: str = ""):
        self._mw = main_window
        self._prefill_phone   = prefill_phone
        self._prefill_name    = prefill_name
        self._prefill_surname = prefill_surname
        i18n = main_window.i18n
        super().__init__(
            parent or main_window,
            title=i18n.t("new_contact_title"),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        self.result_jid:  str = ""
        self.result_name: str = ""
        self._build_ui(i18n)
        self.SetMinSize((380, -1))
        self.Fit()
        self.CentreOnParent()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self, i18n):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Name
        sizer.Add(wx.StaticText(panel, label=i18n.t("contact_name")), 0,
                  wx.LEFT | wx.TOP, 10)
        self._name_field = wx.TextCtrl(panel, style=wx.TE_DONTWRAP)
        sizer.Add(self._name_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        # Surname
        sizer.Add(wx.StaticText(panel, label=i18n.t("contact_surname")), 0,
                  wx.LEFT | wx.TOP, 10)
        self._surname_field = wx.TextCtrl(panel, style=wx.TE_DONTWRAP)
        sizer.Add(self._surname_field, 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        # Phone
        sizer.Add(wx.StaticText(panel, label=i18n.t("phone_label")), 0,
                  wx.LEFT | wx.TOP, 10)
        self._phone_field = wx.TextCtrl(panel, style=wx.TE_DONTWRAP)
        sizer.Add(self._phone_field, 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        # Buttons
        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn     = wx.Button(panel, wx.ID_OK,     label=i18n.t("create_contact"))
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

        ok_btn.Bind(wx.EVT_BUTTON, self._on_add)

        if self._prefill_name:
            self._name_field.SetValue(self._prefill_name)
        if self._prefill_surname:
            self._surname_field.SetValue(self._prefill_surname)
        if self._prefill_phone:
            self._phone_field.SetValue(self._prefill_phone)
        self._name_field.SetFocus()

    # ── Add contact ───────────────────────────────────────────────────────────

    def _on_add(self, event):
        i18n = self._mw.i18n

        first   = self._name_field.GetValue().strip()
        surname = self._surname_field.GetValue().strip()
        phone   = re.sub(r"\D", "", self._phone_field.GetValue())

        if not first:
            wx.MessageBox(
                i18n.t("contact_name"),
                i18n.t("app_name"),
                wx.OK | wx.ICON_WARNING,
                self,
            )
            self._name_field.SetFocus()
            return

        if not phone or len(phone) < 7:
            wx.MessageBox(
                i18n.t("create_contact_error"),
                i18n.t("app_name"),
                wx.OK | wx.ICON_WARNING,
                self,
            )
            self._phone_field.SetFocus()
            return

        full_name = f"{first} {surname}".strip()
        jid       = phone + "@s.whatsapp.net"

        # Store locally so future searches find this contact. This contact
        # only ever lives inside WinZapp/WhatsApp's own contact list — there
        # used to be a "sync to phone" checkbox here that called an
        # add-new-contact API endpoint, but that endpoint never actually
        # existed anywhere in the server (not in WinZapp's patches, not in
        # the underlying wppconnect-server library), and WhatsApp Web itself
        # has no capability to write to a phone's native address book (its
        # own "save contact" feature just opens an external Google
        # Contacts/OS link, not an API call) — so it silently did nothing.
        entry = {
            "remoteJid":  jid,
            "name":       full_name,
            "pushName":   full_name,
            "isSaved":    True,
        }
        # Stored, persisted, mirrored onto the person's @lid record and shown
        # everywhere at once (MainWindow.save_local_contact()).
        self._mw.save_local_contact(jid, entry)

        self.result_jid  = jid
        self.result_name = full_name
        self.EndModal(wx.ID_OK)
