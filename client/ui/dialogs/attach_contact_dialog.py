"""
WinZapp – Attach Contact Dialog
================================
Modal dialog that lets the user select a contact from the contacts list
to attach to an outgoing message.

The contact list itself — search field, columns, dedup rules, empty-state
row — is the shared ui.dialogs.contact_list_picker.ContactListPicker, the
same widget the "add members to a group" dialog uses (add_member_dialog.py)
so the two always look, filter and populate identically. This dialog only
wires that widget up for a single pick: on confirmation,
:attr:`selected_contact` is set to the chosen contact dict (keyed by
remoteJid) and the dialog returns ``wx.ID_OK``.
"""

import wx
from ui.dialogs.contact_list_picker import ContactListPicker


class AttachContactDialog(wx.Dialog):
    """
    Shows the contacts list and returns the chosen contact on OK.

    Parameters
    ----------
    main_window : MainWindow
    """

    def __init__(self, main_window):
        self._mw = main_window
        i18n = main_window.i18n
        super().__init__(
            main_window,
            title=i18n.t("attach_contact_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self.selected_contact: dict | None = None

        self._build_ui()
        self.SetSize((420, 440))
        self.CentreOnParent()

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        i18n = self._mw.i18n
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self._picker = ContactListPicker(self._mw, panel, sizer, multi_select=False)
        self._picker.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activate)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        ok_btn = wx.Button(panel, wx.ID_OK, label=i18n.t("send_attachment"))
        ok_btn.Bind(wx.EVT_BUTTON, self._on_ok)
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
        btn_sizer.Add(ok_btn, 0, wx.ALL, 5)
        btn_sizer.Add(cancel_btn, 0, wx.ALL, 5)
        sizer.Add(btn_sizer, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(sizer)
        dlg_sizer = wx.BoxSizer(wx.VERTICAL)
        dlg_sizer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(dlg_sizer)

        self._picker.focus_search()

    # ── Events ──────────────────────────────────────────────────────────────

    def _on_activate(self, event):
        self._on_ok(event)

    def _on_ok(self, event):
        entry = self._picker.selected_entry()
        if entry is None:
            return
        self.selected_contact = entry
        self.EndModal(wx.ID_OK)
