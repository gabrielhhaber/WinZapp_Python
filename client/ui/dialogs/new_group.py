"""
new_group.py — WinZapp "Novo grupo" dialog.

Lets the user create a new WhatsApp group by:
  1. Choosing a group name.
  2. Selecting participants from a saved-contact checklist (with search filter).
  3. Optionally adding an extra phone number not in the contacts list.

Calls the WPPConnect POST /api/{session}/create-group endpoint.
"""

import re
import threading
import wx


class NewGroupDialog(wx.Dialog):
    """Dialog for creating a new WhatsApp group."""

    def __init__(self, main_window, parent=None):
        self._mw = main_window
        i18n = main_window.i18n
        super().__init__(
            parent or main_window,
            title=i18n.t("new_group_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        # Full contact list (never filtered out)
        self._all_contact_labels: list = []
        self._all_contact_jids:   list = []
        # JIDs that are currently checked (persists across filter changes)
        self._checked_jids: set = set()
        # JIDs shown in the listbox right now (matches listbox row order)
        self._current_jids: list = []

        self._build_ui(i18n)
        self.SetMinSize((440, 440))
        self.SetSize((460, 560))
        self.CentreOnParent()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self, i18n):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Group name
        sizer.Add(
            wx.StaticText(panel, label=i18n.t("group_name")),
            0, wx.LEFT | wx.TOP, 10,
        )
        self._name_field = wx.TextCtrl(panel, style=wx.TE_DONTWRAP)
        sizer.Add(self._name_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        # Participants checklist header
        sizer.Add(
            wx.StaticText(panel, label=i18n.t("group_contacts_label")),
            0, wx.LEFT | wx.TOP, 10,
        )

        # Build the contact list from chats the user actually has (not from
        # all participants of external groups, which bloats the list).
        known_jids = {
            jid for jid in self._mw.chats
            if not jid.endswith("@g.us") and not jid.endswith("@broadcast")
        }
        contacts = self._mw.contacts
        for jid in known_jids:
            contact = contacts.get(jid, {})
            name = contact.get("name") or contact.get("pushName") or jid
            self._all_contact_labels.append(name)
            self._all_contact_jids.append(jid)

        if self._all_contact_labels:
            # Search field (filters the list below in real-time)
            sizer.Add(
                wx.StaticText(panel, label=i18n.t("group_search_label")),
                0, wx.LEFT | wx.TOP, 10,
            )
            self._search_field = wx.TextCtrl(panel, style=wx.TE_DONTWRAP | wx.TE_PROCESS_ENTER)
            self._search_field.Bind(wx.EVT_TEXT, self._on_search_contacts)
            sizer.Add(
                self._search_field, 0,
                wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10,
            )

            # Checklist
            self._current_jids = list(self._all_contact_jids)
            self._contacts_listbox = wx.CheckListBox(
                panel,
                choices=list(self._all_contact_labels),
                style=wx.LB_NEEDED_SB,
            )
            self._contacts_listbox.Bind(
                wx.EVT_CHECKLISTBOX, self._on_check_changed
            )
            sizer.Add(
                self._contacts_listbox, 1,
                wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10,
            )
        else:
            self._search_field = None
            no_contacts_label = wx.StaticText(
                panel, label=i18n.t("group_no_contacts")
            )
            sizer.Add(no_contacts_label, 0, wx.LEFT | wx.TOP, 10)
            self._contacts_listbox = None

        # Extra phone number (for numbers not in contacts)
        sizer.Add(
            wx.StaticText(panel, label=i18n.t("group_extra_number_label")),
            0, wx.LEFT | wx.TOP, 10,
        )
        self._extra_number_field = wx.TextCtrl(panel, style=wx.TE_DONTWRAP)
        sizer.Add(
            self._extra_number_field, 0,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10,
        )

        # Buttons
        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn     = wx.Button(panel, wx.ID_OK,     label=i18n.t("create_group"))
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, label=i18n.t("cancel"))
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)

        ok_btn.Bind(wx.EVT_BUTTON, self._on_create)
        self._name_field.SetFocus()

    # ── Search / filter ───────────────────────────────────────────────────────

    def _on_check_changed(self, event):
        """Track checked JIDs so they survive filter changes."""
        idx = event.GetInt()
        if idx < 0 or idx >= len(self._current_jids):
            return
        jid = self._current_jids[idx]
        checked = self._contacts_listbox.IsChecked(idx)
        if checked:
            self._checked_jids.add(jid)
        else:
            self._checked_jids.discard(jid)
        # Accessibility announcement for screen readers
        label = self._contacts_listbox.GetString(idx)
        state_str = "selecionado" if checked else "não selecionado"
        self._mw.output(f"{label}: {state_str}")

    def _on_search_contacts(self, event):
        """Re-populate the checklist to only show contacts matching the query."""
        if self._contacts_listbox is None:
            return
        query = self._search_field.GetValue().lower().strip()

        # Save current check state into _checked_jids before rebuilding
        for idx in range(self._contacts_listbox.GetCount()):
            if idx < len(self._current_jids):
                jid = self._current_jids[idx]
                if self._contacts_listbox.IsChecked(idx):
                    self._checked_jids.add(jid)
                else:
                    self._checked_jids.discard(jid)

        # Build filtered list
        if query:
            filtered = [
                (lbl, jid)
                for lbl, jid in zip(self._all_contact_labels, self._all_contact_jids)
                if query in lbl.lower()
            ]
        else:
            filtered = list(zip(self._all_contact_labels, self._all_contact_jids))

        filtered_labels = [lbl for lbl, _ in filtered]
        filtered_jids   = [jid for _, jid in filtered]

        # Rebuild the listbox
        self._contacts_listbox.Set(filtered_labels)
        self._current_jids = filtered_jids

        # Restore check state
        for idx, jid in enumerate(filtered_jids):
            if jid in self._checked_jids:
                self._contacts_listbox.Check(idx, True)

    # ── Create group ──────────────────────────────────────────────────────────

    def _on_create(self, event):
        i18n = self._mw.i18n
        name = self._name_field.GetValue().strip()
        if not name:
            wx.MessageBox(
                i18n.t("group_name"),
                i18n.t("app_name"),
                wx.OK | wx.ICON_WARNING,
                self,
            )
            self._name_field.SetFocus()
            return

        # Gather checked contacts (use _checked_jids for full accuracy)
        # First flush current listbox state into _checked_jids
        if self._contacts_listbox is not None:
            for idx in range(self._contacts_listbox.GetCount()):
                if idx < len(self._current_jids):
                    jid = self._current_jids[idx]
                    if self._contacts_listbox.IsChecked(idx):
                        self._checked_jids.add(jid)
                    else:
                        self._checked_jids.discard(jid)

        participants: list = []
        for jid in self._checked_jids:
            if jid:
                participants.append(jid)

        # Gather extra number field
        extra_raw = self._extra_number_field.GetValue()
        for part in re.split(r"[,\n]", extra_raw):
            digits = re.sub(r"\D", "", part.strip())
            if len(digits) >= 7:
                participants.append(digits)

        # Deduplicate while preserving order
        seen: set = set()
        unique_numbers = []
        for p in participants:
            if p not in seen:
                seen.add(p)
                unique_numbers.append(p)

        if not unique_numbers:
            wx.MessageBox(
                i18n.t("group_participants_label"),
                i18n.t("app_name"),
                wx.OK | wx.ICON_WARNING,
                self,
            )
            if self._contacts_listbox is not None:
                self._contacts_listbox.SetFocus()
            else:
                self._extra_number_field.SetFocus()
            return

        # Disable OK to prevent double-click
        self.FindWindow(wx.ID_OK).Disable()

        def _run():
            ok, result = self._mw.create_group(name, unique_numbers)
            wx.CallAfter(self._on_create_done, ok, result)

        threading.Thread(target=_run, daemon=True).start()

    def _on_create_done(self, ok: bool, result):
        i18n = self._mw.i18n
        if ok:
            if not result.members_confirmed:
                wx.MessageBox(i18n.t("group_create_members_unconfirmed"), i18n.t("new_group_title"),
                              wx.OK | wx.ICON_INFORMATION, self)
            self.EndModal(wx.ID_OK)
            if result.jid:
                mw   = self._mw
                chat = mw.chats.get(result.jid) or {"remoteJid": result.jid}
                wx.CallAfter(mw.conversations_panel.navigate_to_conversation, chat)
        else:
            wx.MessageBox(
                i18n.t("create_group_error").format(error=result),
                i18n.t("app_name"),
                wx.OK | wx.ICON_ERROR,
                self,
            )
            btn = self.FindWindow(wx.ID_OK)
            if btn:
                btn.Enable()
