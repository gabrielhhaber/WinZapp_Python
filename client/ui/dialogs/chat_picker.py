"""Chat-picker widget for the media-cleanup dialog.

The dialog needs: a list of chats the user can check off,
filterable by group/individual, with a "select all" that behaves as an
ordinary item at the top of the list (row 1 of N) rather than a separate
button elsewhere in the tab order — so a screen-reader user arrowing
through the list hits it first, instead of having to find a button before
or after the list to select everything.

The filtering/select-all bookkeeping is kept in plain functions with no wx
dependency, so it can be tested without a running wx.App (see
tests/test_chat_picker.py) — ChatCheckList itself is a thin wx wrapper
around them.

The list itself is a wx.ListCtrl with EnableCheckBoxes(True), deliberately
NOT a wx.CheckListBox: NVDA does not reliably expose a CheckListBox item's
checked state or checkbox role on Windows, so a user arrowing through the
list never hears whether a row is checked or unchecked. A ListCtrl's own
checkboxes do carry that role. Same widget/pattern as Settings > Interface
do usuário's group-media-types list and the group data dialog's own
media-types list — see the comment on either for the full story, including
why Space needs to be handled explicitly (wxMSW's native ListCtrl swallows
it as a selection key instead of toggling the box) and why the toggle
handlers apply the new state directly rather than relying on a
programmatic CheckItem() call to raise EVT_LIST_ITEM_CHECKED/UNCHECKED
(wx does not document that it does).
"""

import wx


def filter_options(options, filter_key):
    """options: list of (jid, name, is_group). filter_key: 'all' | 'groups'
    | 'individual'. Returns the subset that filter_key selects, in the
    same order."""
    if filter_key == "groups":
        return [o for o in options if o[2]]
    if filter_key == "individual":
        return [o for o in options if not o[2]]
    return list(options)


def visible_all_checked(visible_jids, checked):
    """True if every currently-visible jid is checked (and there's at
    least one) — what the synthetic "select all" row should show."""
    return bool(visible_jids) and all(jid in checked for jid in visible_jids)


class ChatCheckList(wx.Panel):
    """A group/individual filter plus a checklist whose first row is a
    synthetic "select all" toggle scoped to whatever the filter currently
    shows, one row per chat below it.

    Populate with set_options() — loading the options (calling
    core.media_cleanup.chat_options()) is
    the caller's job; this widget only renders whatever list it's given.
    """

    def __init__(self, parent, i18n, on_change=None):
        super().__init__(parent)
        self._i18n = i18n
        self._on_change = on_change
        self._options: list = []       # full set: (jid, name, is_group)
        self._checked: set = set()     # jids currently checked, survives filtering
        self._visible_jids: list = []  # jid behind each non-select-all row, in order
        self._filter = "all"
        # Guards against reacting to checkbox-state changes this widget is
        # itself driving (a full rebuild, or the select-all cascade) rather
        # than the user — see _apply_row_state()'s own comment.
        self._syncing = False

        sizer = wx.BoxSizer(wx.VERTICAL)

        self._filter_radio = wx.RadioBox(
            self,
            label=i18n.t("conv_filter_label"),
            choices=[
                i18n.t("conv_filter_all"),
                i18n.t("conv_filter_groups"),
                i18n.t("conv_filter_individual"),
            ],
            majorDimension=1,
            style=wx.RA_SPECIFY_ROWS,
        )
        self._filter_radio.Bind(wx.EVT_RADIOBOX, self._on_filter_changed)
        sizer.Add(self._filter_radio, 0, wx.EXPAND | wx.BOTTOM, 8)

        self._list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self._list.InsertColumn(
            0, i18n.t("chat_picker_column_label").replace("&", ""), width=360
        )
        self._list.EnableCheckBoxes(True)
        # Space is the ListCtrl's native toggle on some platforms but not
        # wxMSW — there it's swallowed as a selection key — so it's bound
        # explicitly here. Enter is bound too, because every other checkbox
        # list in this app activates with Enter and the user should not have
        # to know which of the two a given list wants.
        self._list.Bind(wx.EVT_KEY_DOWN, self._on_key_down)
        self._list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_item_activated)
        # Catches a mouse click directly on the checkbox glyph.
        self._list.Bind(wx.EVT_LIST_ITEM_CHECKED, self._on_item_toggled)
        self._list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self._on_item_toggled)
        sizer.Add(self._list, 1, wx.EXPAND)

        self.SetSizer(sizer)
        self.set_loading()

    # ── Placeholder states ─────────────────────────────────────────────
    def set_placeholder(self, text):
        """Show a single disabled informational row instead of the chat
        list — used before there is anything to pick from yet (options
        still loading)."""
        self._options = []
        self._checked = set()
        self._visible_jids = []
        self._list.Freeze()
        self._list.DeleteAllItems()
        self._list.Append((text,))
        self._list.Thaw()
        self._list.Disable()
        self._filter_radio.Disable()

    def set_loading(self):
        """Show a placeholder row while options are being fetched (usually
        on a background thread) and disable the filter/list meanwhile."""
        self.set_placeholder(self._i18n.t("chat_picker_loading"))

    # ── Populating ──────────────────────────────────────────────────────
    def set_options(self, options, check_all=False):
        """options: list of (jid, name, is_group). Replaces the full set.
        Previously checked jids that still exist stay checked, unless
        check_all is True (the dialog starts with everything selected)."""
        self._options = list(options)
        if check_all:
            self._checked = {jid for jid, _, _ in self._options}
        else:
            valid = {jid for jid, _, _ in self._options}
            self._checked &= valid
        self._list.Enable()
        self._filter_radio.Enable()
        self._rebuild()

    def checked_jids(self):
        return [jid for jid, _, _ in self._options if jid in self._checked]

    def set_checked(self, keys):
        self._checked = {key for key, _, _ in self._options if key in keys}
        self._rebuild()

    def has_options(self):
        return bool(self._options)

    # ── Internals ───────────────────────────────────────────────────────
    def _rebuild(self):
        shown = filter_options(self._options, self._filter)
        self._visible_jids = [jid for jid, _, _ in shown]

        self._list.Freeze()
        self._syncing = True
        try:
            self._list.DeleteAllItems()
            if not shown:
                self._list.Append((self._i18n.t("chat_picker_empty"),))
                self._list.Disable()
            else:
                self._list.Enable()
                self._list.Append((self._i18n.t("chat_picker_select_all"),))
                self._list.CheckItem(
                    0, visible_all_checked(self._visible_jids, self._checked)
                )
                for jid, name, _ in shown:
                    idx = self._list.GetItemCount()
                    self._list.Append((name,))
                    self._list.CheckItem(idx, jid in self._checked)
                # Select/Focus move the item cursor inside the control, not the
                # keyboard caret — SetFocus() is deliberately not called, so a
                # filter change doesn't yank focus away from wherever the user
                # was (the radio box, another control). Same convention as the
                # group-media-types lists.
                self._list.Focus(0)
                self._list.Select(0)
        finally:
            self._syncing = False
        self._list.Thaw()

    def _on_filter_changed(self, event):
        _map = ["all", "groups", "individual"]
        sel = self._filter_radio.GetSelection()
        self._filter = _map[sel] if 0 <= sel < len(_map) else "all"
        self._rebuild()

    def _on_key_down(self, event):
        """Space toggles the focused checkbox, matching Enter."""
        if event.GetKeyCode() != wx.WXK_SPACE:
            event.Skip()
            return
        idx = self._list.GetFocusedItem()
        if idx is not None and 0 <= idx < self._list.GetItemCount():
            self._toggle_row(idx)

    def _on_item_activated(self, event):
        """Enter toggles the row, matching Space."""
        idx = event.GetIndex()
        if 0 <= idx < self._list.GetItemCount():
            self._toggle_row(idx)

    def _on_item_toggled(self, event):
        """A mouse click directly on the checkbox glyph — the box has
        already flipped by the time this fires, so just process it."""
        if self._syncing:
            return
        self._apply_row_state(event.GetIndex())

    def _toggle_row(self, idx):
        # Flip the box ourselves rather than relying on the resulting
        # CheckItem() call to raise EVT_LIST_ITEM_CHECKED/UNCHECKED — wx
        # does not document a programmatic CheckItem() as doing so — then
        # process the new state directly.
        self._list.CheckItem(idx, not self._list.IsItemChecked(idx))
        self._apply_row_state(idx)

    def _apply_row_state(self, idx):
        checked = self._list.IsItemChecked(idx)
        if idx == 0:
            for jid in self._visible_jids:
                if checked:
                    self._checked.add(jid)
                else:
                    self._checked.discard(jid)
            self._syncing = True
            try:
                for row, jid in enumerate(self._visible_jids, start=1):
                    self._list.CheckItem(row, jid in self._checked)
            finally:
                self._syncing = False
        else:
            row_jid = self._visible_jids[idx - 1]
            if checked:
                self._checked.add(row_jid)
            else:
                self._checked.discard(row_jid)
            # Keep the select-all row in sync without a full rebuild, which
            # would move focus off the item the user just toggled.
            want_all = visible_all_checked(self._visible_jids, self._checked)
            if self._list.IsItemChecked(0) != want_all:
                self._syncing = True
                try:
                    self._list.CheckItem(0, want_all)
                finally:
                    self._syncing = False
        if self._on_change:
            self._on_change()
