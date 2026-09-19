"""
WinZapp – shared contact list picker
=====================================
The "pick a contact" list is built in more than one place — attaching a
contact to a message (attach_contact_dialog.py) and adding members to a
group (add_member_dialog.py) — and used to each keep its own copy of the
row-building logic. The two had already drifted: the attach-a-contact
picker deduplicated the same person appearing under several bridged JIDs
(@lid/@c.us/@s.whatsapp.net, and the Brazilian 8/9-digit mobile variant —
see core.utils.contact_dedup_key()) while the add-member picker did not,
so the same contact could show up once per JID variant there but only
once in the other dialog (issue #70's bug, half-fixed).

Everything that lists the user's own contacts for picking now goes through
this module instead of keeping its own copy, so a fix or a rule change
(what counts as "my contact", how duplicates are folded, how search
matches) only has to happen once and applies everywhere identically.
"""

import wx
from core.utils import format_number, contact_dedup_key, contact_search_matches


def build_own_contact_rows(main_window) -> list[tuple[str, str, dict]]:
    """Return (display_name, formatted_phone, contact_entry) for every real
    contact the user could plausibly pick, one row per person.

    ``main_window.contacts`` intentionally holds more than one entry per
    real person (the same contact bridged under @lid, @c.us and/or
    @s.whatsapp.net, and Brazilian numbers additionally under both their
    8- and 9-digit mobile form), so iterating it directly shows every
    contact once per JID variant, each possibly formatted differently.
    Collapsed to one row per person via contact_dedup_key(), preferring
    whichever variant is a phone JID — an @lid isn't a real phone number
    and can't build a vCard or be dialled — and sorted alphabetically for
    predictable keyboard/screen-reader navigation instead of dict-insertion
    order.

    Also excludes anything main_window.contacts holds that isn't actually
    the user's own contact: group-participant names learned purely from
    presence/sender-name resolution in some OTHER group carry no
    isMyContact/isSaved flag and aren't backed by a 1:1 chat, and used to
    leak into these lists as unpickable junk. And a bare @lid that never
    bridged to a phone number at all (unresolved, or only ever seen as a
    group participant) can't fold into any phone entry via
    contact_dedup_key() and would otherwise get its own row of raw,
    unconverted @lid digits — not a duplicate exactly, but useless (it
    can't format as a phone number or build a vCard either).
    """
    chats = getattr(main_window, "chats", {})
    contacts = main_window.contacts
    best_by_key: dict[str, dict] = {}
    for jid, contact in contacts.items():
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
        key = contact_dedup_key(main_window, jid)
        if jid.endswith("@lid") and key == jid.split("@", 1)[0]:
            # contact_dedup_key() only returns the raw @lid local part when
            # _lid_to_phone has no bridge for it — i.e. this LID never
            # resolved to an actual phone number.
            continue
        entry = {**contact, "remoteJid": jid}
        existing = best_by_key.get(key)
        if existing is None or (
            existing["remoteJid"].endswith("@lid") and not jid.endswith("@lid")
        ):
            best_by_key[key] = entry

    rows = [
        (
            entry.get("name") or entry.get("pushName")
            or format_number(entry["remoteJid"]),
            format_number(entry["remoteJid"]),
            entry,
        )
        for entry in best_by_key.values()
    ]
    rows.sort(key=lambda r: r[0].lower())
    return rows


class ContactListPicker:
    """Search field + two-column (name/phone) ListCtrl over the user's own
    contacts, built and wired up identically wherever a dialog needs one.

    Parameters
    ----------
    main_window : MainWindow
    parent : wx.Window
        Parent for the created controls (a panel or the dialog itself).
    sizer : wx.Sizer
        Sizer the search field and list are appended to, in that order.
    multi_select : bool
        False (default): the list only ever allows one highlighted row —
        the "attach a contact to a message" use, where typing a filter and
        pressing Enter should act on whatever now matches. True: several
        rows may be highlighted at once — "add members to a group" — and
        in that mode filtering deliberately leaves the selection alone
        rather than auto-highlighting the top match, since silently
        keeping (or gaining) a tick on a contact the user can no longer
        see would add someone to the group without them knowing.
    list_label_key : str
        i18n key for a StaticText placed immediately before the list, read
        by NVDA as the list's own name. Every caller uses the same generic
        wording by default so the two pickers stay identical; a control
        immediately before another is what NVDA reads as that control's
        label, which is also why the search field gets its own label
        rather than relying on a hint.
    """

    def __init__(self, main_window, parent: wx.Window, sizer: wx.Sizer,
                 multi_select: bool = False,
                 list_label_key: str = "add_member_contacts_list_label"):
        self._mw = main_window
        self._multi_select = multi_select
        self._all_rows: list = []       # (name, phone, entry), unfiltered
        self._shown_entries: list = []  # entry dicts, parallel to CURRENT rows

        i18n = main_window.i18n
        # Same wording the "Novo grupo" dialog's own search field already
        # uses, rather than a second key for the same concept — a control
        # gets its own adjacent label (no SetHint: NVDA would read the hint
        # AND this label, once there's a real one).
        search_label = wx.StaticText(parent, label=i18n.t("group_search_label"))
        sizer.Add(search_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)
        self.search_field = wx.TextCtrl(parent, style=wx.TE_DONTWRAP)
        self.search_field.Bind(wx.EVT_TEXT, self._on_search_text)
        # Down/Up from the field walk into the list without a Tab, so the
        # search-then-pick sequence is one continuous keyboard gesture.
        self.search_field.Bind(wx.EVT_KEY_DOWN, self._on_search_key_down)
        sizer.Add(self.search_field, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        list_label = wx.StaticText(parent, label=i18n.t(list_label_key))
        sizer.Add(list_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 8)

        list_style = wx.LC_REPORT | wx.LC_HRULES
        if not multi_select:
            list_style |= wx.LC_SINGLE_SEL
        self.list = wx.ListCtrl(parent, style=list_style)
        self.list.InsertColumn(0, i18n.t("conversations"), width=220)
        self.list.InsertColumn(1, i18n.t("phone_label"), width=140)
        sizer.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self.populate()

    # ── Population ────────────────────────────────────────────────────────

    def populate(self):
        """(Re)load the contact list from main_window.contacts and render it."""
        self._all_rows = build_own_contact_rows(self._mw)
        self._render_rows(self.search_field.GetValue())

    def _render_rows(self, query: str):
        """Repopulate the list with the rows matching *query*.

        Frozen for the whole rebuild: a screen reader gets one accessibility
        event for the new list instead of one per row, which matters here
        because this runs on every keystroke in the search field.
        """
        i18n = self._mw.i18n
        self.list.Freeze()
        try:
            self.list.DeleteAllItems()
            self._shown_entries = []
            for name, phone, entry in self._all_rows:
                if not contact_search_matches(query, name, phone):
                    continue
                self.list.Append((name, phone))
                self._shown_entries.append(entry)
            if not self._shown_entries:
                # Never leave the list empty and silent: an empty result and
                # an empty address book are different situations and both
                # need to say so. The row is not selectable — it has no
                # matching entry in _shown_entries.
                self.list.Append((i18n.t("no_contacts"), ""))
        finally:
            self.list.Thaw()
        if self._shown_entries:
            self.list.Focus(0)
            if not self._multi_select:
                self.list.Select(0)

    def select_first_row(self):
        """Highlight row 0 once, without waiting for a filter change.

        Single-select pickers already select the top row on every render
        (see _render_rows); multi-select ones deliberately don't, so a
        caller that still wants a pickable row highlighted right after the
        dialog opens (matching the single-select behaviour there) calls
        this once, explicitly.
        """
        if self._shown_entries:
            self.list.Select(0)
            self.list.Focus(0)

    def focus_search(self):
        self.search_field.SetFocus()

    # ── Selection ─────────────────────────────────────────────────────────

    def selected_entry(self) -> dict | None:
        """The single highlighted contact entry, or None. For multi_select
        pickers this is whichever row the caret happens to sit on."""
        idx = self.list.GetFirstSelected()
        if idx < 0 or idx >= len(self._shown_entries):
            return None
        return self._shown_entries[idx]

    def selected_entries(self) -> list:
        """Every highlighted contact entry — the multi-select picker's way
        of reading back what the user checked off."""
        result = []
        idx = -1
        while True:
            idx = self.list.GetNextItem(idx, wx.LIST_NEXT_ALL, wx.LIST_STATE_SELECTED)
            if idx == -1:
                break
            if idx < len(self._shown_entries):
                result.append(self._shown_entries[idx])
        return result

    # ── Search events ─────────────────────────────────────────────────────

    def _on_search_text(self, event):
        self._render_rows(self.search_field.GetValue())
        event.Skip()

    def _on_search_key_down(self, event):
        if event.GetKeyCode() in (wx.WXK_DOWN, wx.WXK_UP) and self._shown_entries:
            self.list.SetFocus()
            return
        event.Skip()
