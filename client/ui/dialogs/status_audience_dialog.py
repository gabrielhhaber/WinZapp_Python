"""Picking who a Status is hidden from, or shared only with.

Opened from File > WhatsApp settings > Privacy (whatsapp_privacy_panel.py)
when "My contacts except..." or "Only share with..." is chosen. The people come
from the contacts WinZapp already synchronised with WhatsApp, one row per person
(ui.dialogs.contact_list_picker.build_own_contact_rows, the same rows as the
other contact pickers).

A list with checkboxes rather than the shared multi-select ContactListPicker: an
audience is built over several searches ("Ana", then "Bruno"...), and a
multi-select list rebuilt on every keystroke forgets what was picked before the
filter changed. Here the choice is kept by person, outside the list
(core.whatsapp_privacy.AudienceSelection), so filtering never adds or drops
anyone.

The list is a wx.ListCtrl with EnableCheckBoxes, deliberately not
wx.CheckListBox: NVDA does not reliably expose a CheckListBox item's checked
state (see the group-media-types list in settings_dialog.py). Space and Enter
both toggle the focused row, as in that list.

Someone already in the list on WhatsApp but not among the synchronised contacts
keeps a row of their own, so opening and confirming the dialog never drops a
person silently. That row shows a name or a phone number, never the digits of
an @lid.
"""

import wx

from core.utils import contact_dedup_key, contact_search_matches, format_number
from core.whatsapp_privacy import AudienceSelection
from ui.dialogs.contact_list_picker import build_own_contact_rows


def _outside_contact_label(main_window, jid: str) -> str:
    """A row for someone on WhatsApp's list who is not a synchronised contact:
    their name when WinZapp knows one, else their phone number, else a generic
    label — an unbridged @lid's digits are not a phone number."""
    i18n = main_window.i18n
    # WhatsApp reports @c.us; the contacts are keyed by @s.whatsapp.net, and an
    # @lid's name may be stored under its bridged phone.
    normalize = getattr(main_window, "_normalize_jid", None) or (lambda j: j)
    canonical = normalize(jid) or jid
    phone = canonical
    if canonical.endswith("@lid"):
        phone = (getattr(main_window, "_lid_to_phone", {}) or {}).get(canonical, "")
    # The phone first, as _resolve_contact_name does: an @lid record may hold
    # an older name. _get_contact_tolerant also bridges the Brazilian 8/9-digit
    # forms. The first form that carries a name wins, not the first entry.
    lookup = (getattr(main_window, "_get_contact_tolerant", None)
              or (getattr(main_window, "contacts", {}) or {}).get)
    for form in dict.fromkeys(f for f in (phone, canonical, jid) if f):
        info = lookup(form) or {}
        name = info.get("name") or info.get("pushName") or info.get("notify")
        if name:
            return name
    if phone:
        return i18n.t("wa_privacy_status_unknown_contact").format(number=format_number(phone))
    return i18n.t("unknown_contact")


def audience_rows(main_window, current: "list | tuple") -> list:
    """(key, label, jid) for every pickable person, contacts first (sorted by
    name, as the other pickers), then the people of *current* that are not
    among them. *key* identifies the person across JID forms."""
    rows = []
    seen = set()
    for name, phone, entry in build_own_contact_rows(main_window):
        jid = entry["remoteJid"]
        key = contact_dedup_key(main_window, jid)
        if key in seen:
            continue
        seen.add(key)
        label = name if name == phone else f"{name}, {phone}"
        rows.append((key, label, jid))
    for jid in current or ():
        key = contact_dedup_key(main_window, jid)
        if key in seen:
            continue
        seen.add(key)
        rows.append((key, _outside_contact_label(main_window, jid), jid))
    return rows


class StatusAudienceDialog(wx.Dialog):
    """Returns, after ShowModal() == wx.ID_OK, the chosen people as JIDs
    (``chosen_jids()``)."""

    def __init__(self, parent, main_window, title: str, current: "list | tuple"):
        self._mw = main_window
        i18n = main_window.i18n
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._selection = AudienceSelection(
            audience_rows(main_window, current),
            {contact_dedup_key(main_window, jid) for jid in current or ()},
            lambda query, label: contact_search_matches(query, label, label),
        )
        self._rendering = False

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(self, label=i18n.t("group_search_label")), 0,
                  wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._search = wx.TextCtrl(self, style=wx.TE_DONTWRAP)
        self._search.Bind(wx.EVT_TEXT, lambda event: self._render())
        self._search.Bind(wx.EVT_KEY_DOWN, self._on_search_key)
        sizer.Add(self._search, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        list_label = i18n.t("wa_privacy_status_list_label")
        sizer.Add(wx.StaticText(self, label=list_label), 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self._list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self._list.InsertColumn(0, list_label.replace("&", ""), width=380)
        self._list.EnableCheckBoxes(True)
        self._list.Bind(wx.EVT_LIST_ITEM_CHECKED, self._on_checked)
        self._list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self._on_checked)
        # Space does not toggle a ListCtrl checkbox on wxMSW (the native control
        # takes it as a selection key), and every list here activates with
        # Enter: both are handled explicitly, as in settings_dialog.py.
        self._list.Bind(wx.EVT_KEY_DOWN, self._on_list_key)
        self._list.Bind(wx.EVT_LIST_ITEM_ACTIVATED,
                        lambda event: self._toggle(event.GetIndex()))
        sizer.Add(self._list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self._count = wx.StaticText(self, label="")
        sizer.Add(self._count, 0, wx.ALL, 8)

        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(self, wx.ID_OK, i18n.t("ok"))
        ok.SetDefault()
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, i18n.t("cancel")))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)

        self.SetSizer(sizer)
        self.SetMinSize((360, 420))
        self.SetSize((440, 520))
        self.CentreOnParent()
        self._render()
        self._search.SetFocus()

    def _render(self):
        rows = self._selection.filter(self._search.GetValue())
        self._rendering = True  # CheckItem below fires EVT_LIST_ITEM_CHECKED
        self._list.Freeze()
        try:
            self._list.DeleteAllItems()
            for index, (label, checked) in enumerate(rows):
                self._list.InsertItem(index, label)
                if checked:
                    self._list.CheckItem(index, True)
            if not rows:
                # Never an empty, silent list (same as the other pickers).
                self._list.InsertItem(0, self._mw.i18n.t("no_contacts"))
        finally:
            self._list.Thaw()
            self._rendering = False
        if self._list.GetItemCount():
            self._list.Focus(0)
            self._list.Select(0)
        self._update_count()

    def _update_count(self):
        self._count.SetLabel(self._mw.i18n.t("wa_privacy_status_count").format(
            count=len(self._selection.checked)))

    def _toggle(self, index: int):
        if 0 <= index < self._list.GetItemCount():
            self._list.CheckItem(index, not self._list.IsItemChecked(index))
            self._sync(index)

    def _sync(self, index: int):
        """Copy row *index*'s checkbox into the selection; the placeholder
        row is not a person and is unticked again."""
        if not self._selection.set_checked(index, self._list.IsItemChecked(index)):
            if self._list.IsItemChecked(index):
                self._rendering = True
                self._list.CheckItem(index, False)
                self._rendering = False
            return
        self._update_count()

    def _on_checked(self, event):
        if not self._rendering:
            self._sync(event.GetIndex())

    def _on_list_key(self, event):
        if event.GetKeyCode() == wx.WXK_SPACE:
            self._toggle(self._list.GetFocusedItem())
            return
        event.Skip()

    def _on_search_key(self, event):
        if event.GetKeyCode() in (wx.WXK_DOWN, wx.WXK_UP) and self._selection.shown:
            self._list.SetFocus()
            return
        event.Skip()

    def chosen_jids(self) -> list:
        return self._selection.chosen()
