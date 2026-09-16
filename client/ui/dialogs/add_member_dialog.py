"""
WinZapp – Add Member to Group Dialog
=====================================
Lets the user select one or more contacts to add to a group.

The contact list itself — search field, columns, dedup rules, empty-state
row — is the shared ui.dialogs.contact_list_picker.ContactListPicker, the
same widget the "attach a contact to a message" dialog uses
(attach_contact_dialog.py), so the two always look, filter and populate
identically.
"""

import threading
import wx
from ui.dialogs.contact_list_picker import ContactListPicker
from countries import get_countries


def _focused_window():
    """wx.Window.FindFocus(), or None when there is nothing to ask."""
    try:
        return wx.Window.FindFocus()
    except Exception:
        return None


def typed_number_to_jid(raw: str, dial_code: str) -> str:
    """The JID for a phone number typed into the "add by number" field, or ""
    when what was typed cannot be a phone number.

    Everything but digits is dropped, so a pasted "+55 (51) 99999-8888" works.
    The selected country's code is prepended unless the number already starts
    with it — with one exception that matters in the default country: Brazil's
    code is 55 and so is the area code of part of Rio Grande do Sul, so
    "55 99999-8888" typed as a local number used to be read as already carrying
    the country code and sent as 5599999888, a number that does not exist. A
    Brazilian number with its area code is 10 or 11 digits and one with the
    country code 12 or 13, so the length settles it.
    """
    digits = "".join(c for c in (raw or "") if c.isdigit())
    dial_code = "".join(c for c in (dial_code or "") if c.isdigit())
    if not digits:
        return ""
    # A trunk 0 ("051 99999-8888", UK "07911 123456") is dialled only inside
    # the country and never follows a country code. Italy is the exception:
    # its landlines keep the 0 after +39.
    if digits.startswith("0") and not digits.startswith("00") and dial_code != "39":
        digits = digits[1:]
    if dial_code == "55" and len(digits) in (10, 11):
        digits = dial_code + digits
    elif dial_code and not digits.startswith(dial_code):
        digits = dial_code + digits
    # The shortest real numbers (country code included) are 8 digits and E.164
    # caps them at 15; anything outside that is a typo, not a member.
    if not 8 <= len(digits) <= 15:
        return ""
    return f"{digits}@c.us"


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
        self._picker.select_first_row()
        self._picker.focus_search()
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
        #
        # Search field + list: the same shared widget the "Anexar contato"
        # dialog uses, in its multi-select mode (issue #85's search field,
        # same dedup rules, same layout — see contact_list_picker.py). It
        # places its own "Pesquisar contato" label right before the search
        # field and "Selecionar um contato" right before the list, each
        # control getting its own adjacent label rather than sharing one —
        # NVDA reads whatever StaticText sits right before a control as that
        # control's own name, and a shared label above both used to make the
        # search field announce itself as "Selecionar um contato".
        self._picker = ContactListPicker(self._mw, self, sizer, multi_select=True)

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

        self._add_number_btn = wx.Button(self, label=i18n.t("add_member_custom_number_button"))
        self._add_number_btn.Bind(wx.EVT_BUTTON, self._on_add_typed_number)
        num_sizer.Add(self._add_number_btn, 0)

        num_box_sizer.Add(num_sizer, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(num_box_sizer, 0, wx.EXPAND | wx.ALL, 8)

        cancel_btn = wx.Button(self, wx.ID_CANCEL, label=i18n.t("cancel"))
        sizer.Add(cancel_btn, 0, wx.EXPAND | wx.ALL, 8)

        self.SetSizer(sizer)
        cancel_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CANCEL))

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
        """Add the typed number to the group, right away.

        This used to only append the number as a new row at the bottom of the
        contact list and select it there, leaving the actual add to the "Add"
        button above. Nothing was said and focus stayed in the field, so for a
        screen-reader user pressing "Adicionar número" did nothing at all — and
        the row it did add was lost on the next keystroke in the search field
        (it never entered _all_rows), while the contact pre-selected on open
        stayed selected next to it and would have been added too. The button
        says "add", so it adds, and the result comes back through the same
        success/error dialog as the contact list.
        """
        idx = self._country_combo.GetSelection()
        dial_code = self._countries[idx][1] if 0 <= idx < len(self._countries) else "55"
        jid = typed_number_to_jid(self._phone_field.GetValue(), dial_code)
        if not jid:
            wx.MessageBox(
                self._i18n.t("add_member_invalid_number"),
                self._i18n.t("add_member_title"),
                wx.OK | wx.ICON_ERROR,
                self,
            )
            self._phone_field.SetFocus()
            return
        self._start_add([jid])

    def _on_add(self, event):
        """Collect selected contacts and call the API."""
        selected_jids = [
            entry["remoteJid"] for entry in self._picker.selected_entries()
        ]

        if not selected_jids:
            self.EndModal(wx.ID_CANCEL)
            return

        self._start_add(selected_jids)

    def _start_add(self, jids: list):
        """Both add buttons end here; each is disabled while the request runs,
        so a second press cannot send the same add twice.

        Disabling the button that has keyboard focus makes Windows move focus
        elsewhere — for a screen-reader user, onto whatever it picks, often
        Cancel, where the next Enter closes the dialog. So the focused control
        is remembered and _finish() gives focus back to it after a failure."""
        self._focus_before_add = _focused_window()
        self._ok_btn.Disable()
        self._add_number_btn.Disable()
        threading.Thread(
            target=self._do_add, args=(jids,), daemon=True
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
            self._add_number_btn.Enable()
            target = getattr(self, "_focus_before_add", None)
            if target is not None:
                try:
                    target.SetFocus()
                except Exception:
                    pass  # the control went away with the dialog's state


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
