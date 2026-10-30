"""VoiceOver-visible stand-in for wx.ListCtrl on macOS.

On macOS wx.ListCtrl is wxGenericListCtrl: custom-drawn, and completely
absent from the accessibility tree (verified 2026-09-28 by walking the AX
tree of a test frame — VoiceOver sees nothing at all). wx.ListBox is a
native NSTableView whose rows VoiceOver reads normally, so this class keeps
the wx.ListCtrl API WinZapp uses and renders each row as one ListBox
string: the non-empty column texts joined with ", ".

It follows the same idea as upstream's CompatListBoxMessagesCtrl
(client/ui/accessible.py) but covers the whole ListCtrl surface, and emits
real wx.ListEvents so ordinary Bind(wx.EVT_LIST_*) code works unchanged.
Programmatic Select()/Focus() emit SELECTED/FOCUSED like the native Windows
list view does, which WinZapp's handlers rely on.
"""

import wx

from . import vo_focus_mac

_OrigListCtrl = wx.ListCtrl

CHECK_MARK = "✓ "


def row_height(window):
    """Height in px of one native table row: the text plus intercell spacing."""
    return window.GetCharHeight() + 4


def edge_row(key, row, count):
    """The row Home or End moves to, or None to leave the key alone.

    A native list control has these keys and an NSTableView does not. A Home
    already on the first row stays unhandled: the messages list reads it as
    "load older messages".
    """
    if key in (wx.WXK_HOME, wx.WXK_NUMPAD_HOME):
        target = 0
    elif key in (wx.WXK_END, wx.WXK_NUMPAD_END):
        target = count - 1
    else:
        return None
    return target if count and target != row else None


class MacListCtrl(wx.ListBox):
    def __init__(self, parent, id=wx.ID_ANY, pos=wx.DefaultPosition,
                 size=wx.DefaultSize, style=wx.LC_ICON,
                 validator=wx.DefaultValidator, name="listCtrl"):
        lb_style = wx.LB_SINGLE if style & wx.LC_SINGLE_SEL else wx.LB_EXTENDED
        super().__init__(parent, id, pos, size, [], lb_style, validator, name)
        self._lc_style = style
        self._columns = []          # headings
        self._rows = []             # [ [col texts], data, checked ]
        self._checkboxes = False
        self._emitting = False
        self._last_sel = wx.NOT_FOUND
        super().Bind(wx.EVT_LISTBOX, self._on_listbox)
        super().Bind(wx.EVT_LISTBOX_DCLICK, self._on_dclick)
        super().Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        super().Bind(wx.EVT_KEY_DOWN, self._on_key_down_fallback)
        from . import native_rows
        native_rows.adopt(self)

    def activate_row(self, row):
        """What Enter, a double-click and VO-Space all do."""
        self._emit(wx.wxEVT_LIST_ITEM_ACTIVATED, row)

    # ---- rendering -------------------------------------------------------
    def _label(self, row):
        cols, _data, checked = self._rows[row]
        text = ", ".join(c for c in cols if c)
        from .accessibility_mac import TEXT_FILTERS
        for f in TEXT_FILTERS:   # e.g. "Chats alt+1" built in code -> Mac keys
            text = f(text)
        if self._checkboxes and checked:
            text = CHECK_MARK + text
        return text or " "

    def _render(self, row):
        label = self._label(row)
        if self.GetString(row) != label:
            from . import native_rows
            # Only this row's text: native_rows updates it in place instead
            # of wx reloading the whole table under VoiceOver's cursor.
            with native_rows.row_only(self, row):
                self.SetString(row, label)
            # Every wx.ListBox write reloads the native table; a chat open
            # waits for those to stop before pointing VoiceOver at the field.
            vo_focus_mac.note_list_write(self)

    # ---- events ----------------------------------------------------------
    def _emit(self, evt_type, row, **extra):
        if row is None or row < 0 or row >= len(self._rows):
            return True
        evt = wx.ListEvent(evt_type, self.GetId())
        evt.SetEventObject(self)
        evt.SetIndex(row)
        item = self.GetItem(row)
        evt.SetItem(item)
        self._emitting = True
        try:
            self.GetEventHandler().ProcessEvent(evt)
        finally:
            self._emitting = False
        return evt.IsAllowed()

    def _on_listbox(self, evt):
        row = self.GetSelection() if self._lc_style & wx.LC_SINGLE_SEL else evt.GetSelection()
        if row == wx.NOT_FOUND:
            return
        if self._last_sel not in (wx.NOT_FOUND, row) and self._last_sel < len(self._rows):
            self._emit(wx.wxEVT_LIST_ITEM_DESELECTED, self._last_sel)
        self._last_sel = row
        self._emit(wx.wxEVT_LIST_ITEM_FOCUSED, row)
        self._emit(wx.wxEVT_LIST_ITEM_SELECTED, row)

    def _on_dclick(self, evt):
        row = self.GetFocusedItem()
        if row != wx.NOT_FOUND:
            self._emit(wx.wxEVT_LIST_ITEM_ACTIVATED, row)

    def _on_char_hook(self, evt):
        # A ListBox has no Enter-to-activate of its own; see upstream's
        # CompatListBoxMessagesCtrl._on_char_hook for why EVT_CHAR_HOOK.
        if (self.HasFocus() and evt.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
                and not (evt.ControlDown() or evt.AltDown() or evt.ShiftDown()
                         or evt.RawControlDown())):
            row = self.GetFocusedItem()
            if row != wx.NOT_FOUND:
                self._emit(wx.wxEVT_LIST_ITEM_ACTIVATED, row)
                return
        target = edge_row(evt.GetKeyCode(), self.GetFocusedItem(), len(self._rows))
        if target is not None and self.HasFocus() and not evt.HasAnyModifiers():
            self.Focus(target)
            self.EnsureVisible(target)
            return
        evt.Skip()

    def _on_key_down_fallback(self, evt):
        # Bound first, so it runs after any EVT_KEY_DOWN handler WinZapp
        # binds later; reached only when those Skip(). Space toggles a
        # checkbox row as the native control would.
        if (self._checkboxes and evt.GetKeyCode() == wx.WXK_SPACE
                and not evt.HasAnyModifiers()):
            row = self.GetFocusedItem()
            if row != wx.NOT_FOUND:
                self.CheckItem(row, not self.IsItemChecked(row), notify=True)
                return
        evt.Skip()

    # ---- columns ---------------------------------------------------------
    def InsertColumn(self, col, heading="", format=wx.LIST_FORMAT_LEFT, width=-1):
        if isinstance(heading, wx.ListItem):
            heading = heading.GetText()
        col = max(0, min(col, len(self._columns)))
        self._columns.insert(col, heading)
        return col

    AppendColumn = lambda self, heading="", format=wx.LIST_FORMAT_LEFT, width=-1: \
        self.InsertColumn(len(self._columns), heading, format, width)

    def GetColumnCount(self):
        return len(self._columns)

    def DeleteColumn(self, col):
        if 0 <= col < len(self._columns):
            del self._columns[col]
            return True
        return False

    def DeleteAllColumns(self):
        self._columns = []
        return True

    def GetColumn(self, col):
        item = wx.ListItem()
        item.SetColumn(col)
        if 0 <= col < len(self._columns):
            item.SetText(self._columns[col])
        return item

    def SetColumn(self, col, item):
        if 0 <= col < len(self._columns):
            self._columns[col] = item.GetText()
        return True

    def SetColumnWidth(self, col, width):
        return True

    def GetColumnWidth(self, col):
        return self.GetClientSize().width if col == 0 else 0

    def SetColumnsOrder(self, orders):
        return True

    # ---- rows ------------------------------------------------------------
    def GetItemCount(self):
        return len(self._rows)

    def InsertItem(self, index, label=None, imageIndex=-1):
        data = 0
        if isinstance(index, wx.ListItem):
            item = index
            index, label, data = item.GetId(), item.GetText(), item.GetData()
        elif not isinstance(label, str):
            label = "" if label is None else str(label)
        index = max(0, min(index, len(self._rows)))
        self._rows.insert(index, [[label], data, False])
        sel, had_focus = self.GetSelection(), self.HasFocus()
        from . import native_rows
        with native_rows.change(self, native_rows.INSERT, index):
            self.Insert(self._label(index), index)
        vo_focus_mac.note_list_write(self)
        if sel != wx.NOT_FOUND and index <= sel:
            self.SetSelection(sel + 1)
            self._last_sel = sel + 1
        if had_focus:
            self.SetFocus()
        return index

    def Append(self, entry):
        if isinstance(entry, str):
            entry = [entry]
        entry = list(entry)
        row = self.InsertItem(len(self._rows), str(entry[0]) if entry else "")
        for col, text in enumerate(entry[1:], start=1):
            self.SetItem(row, col, str(text))
        return row

    def SetItem(self, index, column=None, label=None, imageId=-1):
        if isinstance(index, wx.ListItem):
            item = index
            index, column, label = item.GetId(), item.GetColumn(), item.GetText()
        if not (0 <= index < len(self._rows)):
            return False
        cols = self._rows[index][0]
        while len(cols) <= column:
            cols.append("")
        cols[column] = "" if label is None else str(label)
        self._render(index)
        return True

    SetStringItem = SetItem

    def SetItemText(self, item, text, *rest):
        # wx.ListCtrl.SetItemText(item, text); tolerate the (row, col, text)
        # form upstream's CompatListBoxMessagesCtrl also accepts.
        if rest:
            return self.SetItem(item, text, rest[0])
        return self.SetItem(item, 0, text)

    def GetItemText(self, item, col=0):
        if 0 <= item < len(self._rows):
            cols = self._rows[item][0]
            return cols[col] if col < len(cols) else ""
        return ""

    def GetItem(self, itemIdx, col=0):
        item = wx.ListItem()
        item.SetId(itemIdx)
        item.SetColumn(col)
        if 0 <= itemIdx < len(self._rows):
            item.SetText(self.GetItemText(itemIdx, col))
            try:
                item.SetData(self._rows[itemIdx][1] or 0)
            except Exception:
                pass
        return item

    def DeleteItem(self, item):
        if not (0 <= item < len(self._rows)):
            return False
        sel, had_focus = self.GetSelection(), self.HasFocus()
        del self._rows[item]
        from . import native_rows
        with native_rows.change(self, native_rows.DELETE, item):
            self.Delete(item)
        vo_focus_mac.note_list_write(self)
        if sel != wx.NOT_FOUND and self._rows:
            new = sel - 1 if sel > item else (max(0, item - 1) if sel == item else sel)
            self.SetSelection(min(new, len(self._rows) - 1))
            self._last_sel = self.GetSelection()
        if had_focus:
            self.SetFocus()
        return True

    def DeleteAllItems(self):
        self._rows = []
        self._last_sel = wx.NOT_FOUND
        self.Clear()
        vo_focus_mac.note_list_write(self)
        return True

    def ClearAll(self):
        self.DeleteAllItems()
        self._columns = []

    # ---- item data -------------------------------------------------------
    def SetItemData(self, item, data):
        if 0 <= item < len(self._rows):
            self._rows[item][1] = data
            return True
        return False

    SetItemPtrData = SetItemData

    def GetItemData(self, item):
        if 0 <= item < len(self._rows):
            return self._rows[item][1] or 0
        return 0

    def FindItem(self, start, str_or_data, partial=False):
        start = max(0, start)
        for i in range(start, len(self._rows)):
            if isinstance(str_or_data, str):
                text = self.GetItemText(i).lower()
                needle = str_or_data.lower()
                if text == needle or (partial and text.startswith(needle)):
                    return i
            elif self._rows[i][1] == str_or_data:
                return i
        return wx.NOT_FOUND

    # ---- selection / focus -----------------------------------------------
    def _selected_rows(self):
        if self._lc_style & wx.LC_SINGLE_SEL:
            s = self.GetSelection()
            return [] if s == wx.NOT_FOUND else [s]
        return list(self.GetSelections())

    def GetFirstSelected(self, *args):
        rows = self._selected_rows()
        return rows[0] if rows else wx.NOT_FOUND

    def GetNextSelected(self, item):
        for r in self._selected_rows():
            if r > item:
                return r
        return wx.NOT_FOUND

    def GetSelectedItemCount(self):
        return len(self._selected_rows())

    def GetFocusedItem(self):
        rows = self._selected_rows()
        return rows[0] if rows else wx.NOT_FOUND

    def GetNextItem(self, item, geometry=wx.LIST_NEXT_ALL, state=wx.LIST_STATE_DONTCARE):
        if state & (wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED):
            for r in self._selected_rows():
                if r > item:
                    return r
            return wx.NOT_FOUND
        nxt = item + 1
        return nxt if nxt < len(self._rows) else wx.NOT_FOUND

    def IsSelected(self, idx):
        return idx in self._selected_rows()

    def _select(self, row, emit_focus):
        if not (0 <= row < len(self._rows)):
            return
        changed = row not in self._selected_rows()
        self.SetSelection(row)
        if changed and not self._emitting:
            prev = self._last_sel
            self._last_sel = row
            if prev not in (wx.NOT_FOUND, row) and prev < len(self._rows):
                self._emit(wx.wxEVT_LIST_ITEM_DESELECTED, prev)
            if emit_focus:
                self._emit(wx.wxEVT_LIST_ITEM_FOCUSED, row)
            self._emit(wx.wxEVT_LIST_ITEM_SELECTED, row)

    def Select(self, idx, on=1):
        if on:
            self._select(idx, emit_focus=False)
        elif 0 <= idx < len(self._rows):
            self.Deselect(idx)

    def Focus(self, idx):
        self._select(idx, emit_focus=True)

    def SetItemState(self, item, state, stateMask):
        if stateMask & (wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED):
            if state & (wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED):
                self._select(item, emit_focus=bool(state & wx.LIST_STATE_FOCUSED))
            elif item == -1:
                self.SetSelection(wx.NOT_FOUND)
            elif 0 <= item < len(self._rows):
                self.Deselect(item)
        return True

    def GetItemState(self, item, stateMask):
        state = 0
        if item in self._selected_rows():
            state |= wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED
        return state & stateMask

    def EnsureVisible(self, item):
        if 0 <= item < len(self._rows):
            super().EnsureVisible(item)
        return True

    def GetCountPerPage(self):
        return max(1, self.GetClientSize().height // max(1, row_height(self)))

    # ---- checkboxes (rendered as a leading check mark) -------------------
    def EnableCheckBoxes(self, enable=True):
        self._checkboxes = bool(enable)
        for i in range(len(self._rows)):
            self._render(i)
        return True

    def HasCheckBoxes(self):
        return self._checkboxes

    def IsItemChecked(self, item):
        return 0 <= item < len(self._rows) and self._rows[item][2]

    def CheckItem(self, item, check=True, notify=False):
        if 0 <= item < len(self._rows) and self._rows[item][2] != bool(check):
            self._rows[item][2] = bool(check)
            self._render(item)
            if notify:
                self._emit(wx.wxEVT_LIST_ITEM_CHECKED if check
                           else wx.wxEVT_LIST_ITEM_UNCHECKED, item)

    # ---- cosmetic no-ops (a native table has no per-row styling here) ---
    def SetItemTextColour(self, *a): pass
    def SetItemBackgroundColour(self, *a): pass
    def SetItemFont(self, *a): pass
    def SetItemImage(self, *a, **k): return True
    def SetItemColumnImage(self, *a, **k): return True
    def SetImageList(self, *a, **k): pass
    def AssignImageList(self, *a, **k): pass
    def SetSingleStyle(self, *a, **k): pass
    def RefreshItem(self, item): self.Refresh()
    def RefreshItems(self, a, b): self.Refresh()
    def GetItemTextColour(self, item): return self.GetForegroundColour()
    def GetItemBackgroundColour(self, item): return self.GetBackgroundColour()

    def SortItems(self, fn):
        import functools
        sel = self.GetFirstSelected()
        sel_row = self._rows[sel] if sel != wx.NOT_FOUND else None
        self._rows.sort(key=functools.cmp_to_key(lambda a, b: fn(a[1], b[1])))
        self.Set([self._label(i) for i in range(len(self._rows))])
        vo_focus_mac.note_list_write(self)
        if sel_row is not None:
            self.SetSelection(self._rows.index(sel_row))
        return True


def install():
    wx.ListCtrl = MacListCtrl
