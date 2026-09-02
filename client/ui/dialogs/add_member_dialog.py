"""
WinZapp – Add Member to Group Dialog
=====================================
Lets the user select one or more contacts to add to a group.
"""

import threading
import wx
from core.utils import format_number, contact_search_matches
from countries import get_countries


class AddMemberDialog(wx.Dialog):
    """
    Shows a list of all contacts. The user selects one or more and clicks
    "Add" to add them to the specified group.
    """

    def __init__(self, main_window, group_jid: str):
        self._mw       = main_window
        self._i18n     = main_window.i18n
        self._group_jid = group_jid

        super().__init__(
            main_window,
            title=self._i18n.t("add_member_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._build_ui()
        self._populate_contacts()
        self._select_first_contact()
        self.SetMinSize((360, 400))
        self.SetSize((420, 500))
        self.CentreOnParent()

    def _build_ui(self):
        i18n  = self._i18n
        sizer = wx.BoxSizer(wx.VERTICAL)

        label = wx.StaticText(self, label=i18n.t("add_member_title"))
        sizer.Add(label, 0, wx.ALL, 8)

        # ── Contacts list ─────────────────────────────────────────────────
        # Tab order deliberately puts this before the "add by number"
        # section below: picking from the user's own contacts is the
        # primary/expected path, the number field is the alternative one —
        # a blind user tabbing through the dialog used to land on the
        # alternative first, which read backwards.
        contacts_label = wx.StaticText(self, label=i18n.t("add_member_contacts_list_label"))
        sizer.Add(contacts_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)

        # Search field, right under the list's own label and before the list
        # itself, focused on open (issue #85): first-letter navigation searches
        # from the start of the displayed name, so it cannot find anyone by
        # surname. Same field, same matcher, as the "Anexar contato" dialog.
        self._search_field = wx.TextCtrl(self, style=wx.TE_DONTWRAP)
        self._search_field.SetHint(i18n.t("search_contact_label").replace("&", ""))
        self._search_field.Bind(wx.EVT_TEXT, self._on_search_text)
        self._search_field.Bind(wx.EVT_KEY_DOWN, self._on_search_key_down)
        sizer.Add(self._search_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self._list = wx.ListCtrl(
            self, style=wx.LC_REPORT | wx.LC_HRULES
        )
        self._list.InsertColumn(0, i18n.t("conversations"), width=220)
        self._list.InsertColumn(1, i18n.t("phone_label"),   width=140)
        sizer.Add(self._list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # "Add" button for contacts picked from the list above lives right
        # here — immediately after the list, before the "add by number"
        # section — so a user who just wants to select from their own
        # contacts doesn't have to tab through the whole number/country
        # sub-form to reach it. It still carries wx.ID_OK, so Enter inside
        # the dialog (and the dialog's own default-button handling) keeps
        # working exactly as before.
        self._ok_btn = wx.Button(self, wx.ID_OK, label=i18n.t("add_member"))
        self._ok_btn.Bind(wx.EVT_BUTTON, self._on_add)
        self._ok_btn.SetDefault()
        sizer.Add(self._ok_btn, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # ── Custom phone number entry (alternative path) ────────────────────
        # Wrapped in a StaticBox (not a bare StaticText immediately before
        # the combobox) specifically because NVDA reads whatever StaticText
        # sits right before a control as that control's own label. A
        # section header like "Ou, adicionar via número de telefone" isn't
        # the country combo's label — it used to be read as if it were,
        # which made no sense once focus actually reached the combo. Each
        # control below gets its own accurate, adjacent label instead.
        num_box = wx.StaticBox(self, label=i18n.t("add_member_custom_number_label"))
        num_box_sizer = wx.StaticBoxSizer(num_box, wx.VERTICAL)

        country_label = wx.StaticText(self, label=i18n.t("add_member_country_label"))
        num_box_sizer.Add(country_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)

        self._countries = get_countries(self._i18n.language)
        self._country_combo = wx.ComboBox(
            self, choices=[c[0] for c in self._countries],
            style=wx.CB_READONLY,
        )
        self._country_combo.SetSelection(0)  # Brazil (default)
        num_box_sizer.Add(self._country_combo, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        phone_label = wx.StaticText(self, label=i18n.t("add_member_phone_number_label"))
        num_box_sizer.Add(phone_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)

        num_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self._phone_field = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self._phone_field.SetHint(i18n.t("phone_label"))
        self._phone_field.Bind(wx.EVT_CHAR, self._on_phone_char)
        self._phone_field.Bind(wx.EVT_TEXT_ENTER, self._on_add_typed_number)
        num_sizer.Add(self._phone_field, 1, wx.EXPAND | wx.RIGHT, 6)

        add_number_btn = wx.Button(self, label=i18n.t("add_member_custom_number_button"))
        add_number_btn.Bind(wx.EVT_BUTTON, self._on_add_typed_number)
        num_sizer.Add(add_number_btn, 0)

        num_box_sizer.Add(num_sizer, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(num_box_sizer, 0, wx.EXPAND | wx.ALL, 8)

        cancel_btn = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        sizer.Add(cancel_btn, 0, wx.EXPAND | wx.ALL, 8)

        self.SetSizer(sizer)
        cancel_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CANCEL))

    def _select_first_contact(self):
        """Pre-select the first contact, then put keyboard focus in the search
        field.

        The selection is still made so a screen-reader user arrowing into the
        list lands on a pickable item rather than on nothing. Keyboard focus
        goes to the search field instead of the list because issue #85 asks for
        the field to be focused when the list opens — and Down/Up from there
        step straight into the list (see _on_search_key_down), so the old
        gesture still works with one extra key.
        """
        if self._list.GetItemCount():
            self._list.Select(0)
            self._list.Focus(0)
        self._search_field.SetFocus()

    def _on_phone_char(self, event):
        """Only digits, navigation and Ctrl/Alt combos pass through — mirrors
        the pairing dialog's phone field filter (connect.py)."""
        key = event.GetKeyCode()
        _NAV = {
            wx.WXK_BACK, wx.WXK_DELETE,
            wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_HOME, wx.WXK_END,
            wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER,
            wx.WXK_TAB, wx.WXK_ESCAPE,
        }
        if key in _NAV or event.ControlDown() or event.AltDown() or event.CmdDown():
            event.Skip()
            return
        if key in (wx.WXK_ALT, wx.WXK_CONTROL, wx.WXK_SHIFT) or wx.WXK_F1 <= key <= wx.WXK_F24:
            event.Skip()
            return
        if ord("0") <= key <= ord("9") or wx.WXK_NUMPAD0 <= key <= wx.WXK_NUMPAD9:
            event.Skip()
            return
        # Anything else (letters, +, spaces, punctuation…) is swallowed so the
        # user can freely paste a formatted number ("+55 (11) 98765-4321")
        # and only the digits matter for the API — but typing bare junk is
        # rejected the same way the pairing dialog's field rejects it.

    def _on_add_typed_number(self, event):
        """Detect the format of what the user typed/pasted, strip everything
        but digits, prefix with the selected country's dial code (unless the
        user already typed the code themselves) and append it to the pickable
        contact list — same conversion done for pairing in connect.py."""
        i18n = self._i18n
        raw = self._phone_field.GetValue()
        digits = "".join(c for c in raw if c.isdigit())
        if not digits:
            return

        idx = self._country_combo.GetSelection()
        dial_code = self._countries[idx][1] if 0 <= idx < len(self._countries) else "55"

        # If the number as typed doesn't already start with the selected
        # country's dial code, assume it's a local number and prepend it.
        if not digits.startswith(dial_code):
            digits = dial_code + digits

        jid = f"{digits}@c.us"
        if jid in self._contact_jids:
            self._phone_field.SetValue("")
            return

        name = format_number(jid)
        idx_row = self._list.GetItemCount()
        self._list.InsertItem(idx_row, name)
        self._list.SetItem(idx_row, 1, name)
        self._contact_jids.append(jid)
        self._list.Select(idx_row)
        self._list.EnsureVisible(idx_row)
        self._phone_field.SetValue("")

    def _populate_contacts(self):
        """Fill the list with the user's own contacts — not every entry in
        main_window.contacts. That dict is also where group-participant name
        resolution (on_presence_update, LID bridging, sender-name learning)
        writes {name, pushName} for anyone who ever spoke in a group with the
        user, with no isMyContact/isSaved flag at all — those aren't real
        WhatsApp contacts the user could plausibly add to a *different*
        group, but used to show up here alongside genuine ones anyway.
        Mirrors the same legitimacy check get_remote_contacts() already uses
        to decide what counts as "my contact" in the first place, plus
        isSaved for a contact added locally (NewContactDialog) and an
        existing 1:1 chat (a contact WhatsApp itself may not flag as
        isMyContact — e.g. someone who messaged first — but the user
        evidently already has a real conversation with).
        """
        self._contact_jids = []  # parallel list of JIDs, for the SHOWN rows
        self._all_rows = []      # (name, phone, jid), unfiltered
        chats = getattr(self._mw, "chats", {})
        for jid, contact in self._mw.contacts.items():
            if not jid or jid.endswith("@g.us"):
                continue
            is_own_contact = (
                contact.get("isMyContact") is True
                or contact.get("isMe") is True
                or contact.get("isSaved") is True
                or jid in chats
            )
            if not is_own_contact:
                continue
            name = contact.get("name") or contact.get("pushName") or format_number(jid)
            self._all_rows.append((name, format_number(jid), jid))
        self._render_rows("")

    def _render_rows(self, query: str):
        """Repopulate the list with the rows matching *query*.

        Frozen for the whole rebuild so the screen reader gets one
        accessibility event rather than one per row — this runs on every
        keystroke in the search field.

        Selection is deliberately NOT carried across a filter change: this is a
        multi-select list, and silently keeping a tick on a contact the user can
        no longer see would add someone to the group without them knowing.
        """
        self._list.Freeze()
        try:
            self._list.DeleteAllItems()
            self._contact_jids = []
            for name, phone, jid in self._all_rows:
                if not contact_search_matches(query, name, phone):
                    continue
                idx = self._list.GetItemCount()
                self._list.InsertItem(idx, name)
                self._list.SetItem(idx, 1, phone)
                self._contact_jids.append(jid)
        finally:
            self._list.Thaw()

    def _on_search_text(self, event):
        self._render_rows(self._search_field.GetValue())
        event.Skip()

    def _on_search_key_down(self, event):
        if event.GetKeyCode() in (wx.WXK_DOWN, wx.WXK_UP) and self._contact_jids:
            self._list.SetFocus()
            return
        event.Skip()

    def _on_add(self, event):
        """Collect selected contacts and call the API."""
        selected_jids = []
        idx = -1
        while True:
            idx = self._list.GetNextItem(idx, wx.LIST_NEXT_ALL, wx.LIST_STATE_SELECTED)
            if idx == -1:
                break
            if idx < len(self._contact_jids):
                selected_jids.append(self._contact_jids[idx])

        if not selected_jids:
            self.EndModal(wx.ID_CANCEL)
            return

        self._ok_btn.Disable()
        threading.Thread(
            target=self._do_add, args=(selected_jids,), daemon=True
        ).start()

    def _do_add(self, jids: list):
        ok, err = self._mw.add_group_members(self._group_jid, jids)
        wx.CallAfter(self._finish, ok, err)

    def _finish(self, ok: bool, err: str):
        i18n = self._i18n
        if ok:
            wx.MessageBox(
                i18n.t("add_member_success"),
                i18n.t("add_member_title"),
                wx.OK | wx.ICON_INFORMATION,
                self,
            )
            self.EndModal(wx.ID_OK)
        else:
            wx.MessageBox(
                i18n.t("add_member_error").format(error=err),
                i18n.t("add_member_title"),
                wx.OK | wx.ICON_ERROR,
                self,
            )
            self._ok_btn.Enable()


class SelectGroupDialog(wx.Dialog):
    """
    Shows a list of all group chats the user belongs to.
    The user picks one group to add a specific contact to.
    """

    def __init__(self, main_window, contact_jid: str, contact_name: str):
        self._mw           = main_window
        self._i18n         = main_window.i18n
        self._contact_jid  = contact_jid
        self._contact_name = contact_name

        super().__init__(
            main_window,
            title=self._i18n.t("select_group_title"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._build_ui()
        self._populate_groups()
        self.SetMinSize((360, 320))
        self.SetSize((400, 400))
        self.CentreOnParent()

    def _build_ui(self):
        i18n  = self._i18n
        sizer = wx.BoxSizer(wx.VERTICAL)

        label = wx.StaticText(self, label=i18n.t("select_group_title"))
        sizer.Add(label, 0, wx.ALL, 8)

        self._list = wx.ListCtrl(
            self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_HRULES
        )
        self._list.InsertColumn(0, i18n.t("conversations"), width=340)
        sizer.Add(self._list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        btn_sizer = wx.StdDialogButtonSizer()
        self._ok_btn = wx.Button(self, wx.ID_OK,     label=i18n.t("select_group"))
        cancel_btn   = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        btn_sizer.AddButton(self._ok_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 8)

        self.SetSizer(sizer)
        self._ok_btn.Bind(wx.EVT_BUTTON, self._on_select)
        cancel_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CANCEL))

    def _populate_groups(self):
        """Fill the list with all group chats."""
        self._group_jids = []
        deleted = set(self._mw.settings.get("deleted_chats", []))
        for jid, chat in self._mw.chats.items():
            if not jid.endswith("@g.us") or jid in deleted:
                continue
            # _resolve_contact_name() always returns None for a group (it's
            # address-book lookup, groups have no such entry — see its own
            # docstring), so it never contributed anything here; every group
            # fell straight through to the bare-JID fallback, which is why
            # this list showed a raw number for every single group. A
            # group's real name lives under groupMetadata.subject in the raw
            # chat dict, not a flat "name"/"subject" key — same lookup used
            # everywhere else a group is named (see _group_name_from_chat_dict()).
            name = (
                self._mw._group_name_from_chat_dict(chat)
                or getattr(self._mw, "_group_name_cache", {}).get(jid, "")
                or self._mw.find_name_through_messages(chat)
                or chat.get("pushName", "")
                or jid.split("@")[0]
            )
            idx = self._list.GetItemCount()
            self._list.InsertItem(idx, name)
            self._group_jids.append(jid)

        if not self._group_jids:
            self._list.InsertItem(0, self._i18n.t("no_groups_available"))
            self._ok_btn.Disable()

    def _on_select(self, event):
        idx = self._list.GetFirstSelected()
        if idx == -1 or idx >= len(self._group_jids):
            return
        group_jid = self._group_jids[idx]
        self._ok_btn.Disable()
        threading.Thread(
            target=self._do_add, args=(group_jid,), daemon=True
        ).start()

    def _do_add(self, group_jid: str):
        ok, err = self._mw.add_group_members(group_jid, [self._contact_jid])
        wx.CallAfter(self._finish, ok, err)

    def _finish(self, ok: bool, err: str):
        i18n = self._i18n
        if ok:
            wx.MessageBox(
                i18n.t("add_member_success"),
                i18n.t("select_group_title"),
                wx.OK | wx.ICON_INFORMATION,
                self,
            )
            self.EndModal(wx.ID_OK)
        else:
            wx.MessageBox(
                i18n.t("add_member_error").format(error=err),
                i18n.t("select_group_title"),
                wx.OK | wx.ICON_ERROR,
                self,
            )
            self._ok_btn.Enable()
