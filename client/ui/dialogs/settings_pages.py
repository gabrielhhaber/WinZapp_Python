"""The settings window's navigation: a list of categories, a search field and
the page of the chosen category.

`SettingsPages` stands where the `wx.Notebook` stood. It keeps the calls the
dialog (and the tabs it is made of) already make — AddPage, GetPage,
FindPage, SetSelection, ChangeSelection, GetPageText, SetPageText,
GetPageCount, EVT_NOTEBOOK_PAGE_CHANGED — so the pages themselves, their
indexes and the positional `SetSelection(N)` calls elsewhere stay as they
were. What changes is how the pages are reached: a one-column list on the
left (arrow keys change the page, as in NVDA's settings) instead of tabs,
and a search field above it.

The search walks the controls of every page and matches what a screen reader
would say for each one: its own label, or the text just before it for a
field, a choice or a list. A match is shown as "Category: option"; activating
it opens the page and puts the focus on that control.
"""

import unicodedata

import wx
from wx.lib.scrolledpanel import ScrolledPanel


class ScrollingPage(ScrolledPanel):
    """A page that scrolls instead of making the dialog taller than the screen."""

    def __init__(self, parent):
        super().__init__(parent)

    def finish(self):
        """Call once the sizer is set."""
        self.SetupScrolling(scroll_x=False, rate_y=15, scrollIntoView=True)

    def DoGetBestSize(self):
        size = super().DoGetBestSize()
        return wx.Size(size.x, min(size.y, 440))


class SubPageDialog(wx.Dialog):
    """A group of options reached from a button on a page.

    Its controls are built once and kept, like those of the pages, so the
    settings dialog reads and writes them the same way. Closing only hides it.
    """

    def __init__(self, parent, title, ok_label, on_change=None, first_open=None):
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._body = None
        self._first_open = first_open   # called once, the first time it opens
        self._sizer = wx.BoxSizer(wx.VERTICAL)
        ok = wx.Button(self, wx.ID_OK, ok_label)
        ok.SetDefault()
        self._ok = ok
        self._buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._buttons.Add(ok, 0)
        self.SetSizer(self._sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char)
        if on_change is not None:
            for event in (wx.EVT_CHECKBOX, wx.EVT_RADIOBUTTON, wx.EVT_RADIOBOX, wx.EVT_COMBOBOX,
                          wx.EVT_CHOICE, wx.EVT_TEXT):
                self.Bind(event, on_change)

    def set_body(self, panel):
        """The panel of controls, created with this dialog as its parent."""
        self._body = panel
        self._sizer.Add(panel, 1, wx.EXPAND | wx.ALL, 4)
        self._sizer.Add(self._buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.SetSizerAndFit(self._sizer)

    def _on_char(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_OK)
            return
        event.Skip()

    @staticmethod
    def _focus(control):
        try:
            control.SetFocus()
        except RuntimeError:
            pass

    def open(self, control=None):
        if control is not None:
            # After the window is up: a hidden window takes no focus.
            wx.CallAfter(self._focus, control)
        self.CentreOnParent()
        if self._first_open is not None:
            first, self._first_open = self._first_open, None
            first()
        self.ShowModal()


def _fold(text: str) -> str:
    """Lowercase without accents, so "audio" finds "áudio"."""
    text = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


# Controls that carry their own text; a StaticText is a caption for the next
# control that does not.
_SELF_LABELLED = (wx.CheckBox, wx.RadioButton, wx.Button, wx.ToggleButton)
_CAPTIONED = (wx.Choice, wx.ComboBox, wx.SpinCtrl, wx.SpinCtrlDouble, wx.TextCtrl,
              wx.ListBox, wx.ListCtrl, wx.Slider, wx.CheckListBox)
# Names wx gives a control when nobody named it — worthless to search or read.
_DEFAULT_NAMES = frozenset((
    "", "button", "check", "radioButton", "text", "choice", "comboBox", "listBox",
    "listctrl", "slider", "spinCtrl", "staticText", "panel", "scrolledWindow",
))


class SearchEntry:
    """One searchable control: where it is and what it is called."""

    __slots__ = ("page_index", "category", "label", "control", "folded", "opener")

    def __init__(self, page_index, category, label, control, opener=None):
        self.opener = opener
        self.page_index = page_index
        self.category = category
        self.label = label
        self.control = control
        self.folded = _fold(f"{category} {label}")


def _label_of(control) -> str:
    try:
        if isinstance(control, _SELF_LABELLED):
            return control.GetLabelText().strip()
    except RuntimeError:
        return ""
    return ""


def _name_of(control) -> str:
    try:
        name = control.GetName() or ""
    except RuntimeError:
        return ""
    return "" if name in _DEFAULT_NAMES else name.strip()


def collect_entries(page, page_index, category, opener=None):
    """The searchable controls of `page`, in the order they were created."""
    entries = []
    caption = [""]

    def walk(window):
        try:
            children = list(window.GetChildren())
        except RuntimeError:
            return
        for child in children:
            # A hidden control (an option that does not apply now) is not
            # offered; the page itself is not asked, only its own flag.
            if not child.IsShown():
                continue
            if isinstance(child, wx.StaticText):
                text = child.GetLabelText().strip()
                if text:
                    caption[0] = text
                continue
            label = _label_of(child)
            if label:
                entries.append(SearchEntry(page_index, category, label, child, opener))
                caption[0] = ""
            elif isinstance(child, _CAPTIONED):
                label = caption[0] or _name_of(child)
                if label:
                    entries.append(SearchEntry(page_index, category, label, child, opener))
                caption[0] = ""
            elif isinstance(child, wx.Window) and child.GetChildren():
                walk(child)

    walk(page)
    return entries


class SettingsPages(wx.Simplebook):
    """Notebook-shaped book whose pages are chosen from a list."""

    def __init__(self, parent):
        super().__init__(parent)
        self._titles = []
        self._list = None
        self._search = None
        self._results = None
        self._results_panel = None
        self._found = []
        self._index = []
        self._subpages = []     # (page_index, caption, window, opener)
        self._searching = False
        self._selection = wx.NOT_FOUND

    # ── The notebook calls the dialog already makes ──────────────────────────

    def AddPage(self, page, text, select=False, imageId=-1):
        added = super().AddPage(page, text, select)
        self._titles.append(text)
        if self._list is not None:
            self._list.InsertItem(self._list.GetItemCount(), text)
        return added

    def add_subpage(self, page_index, caption, window, opener):
        """Options reached from a button on page `page_index`: searched like
        the page's own, and opened by `opener(control)` when chosen."""
        self._subpages.append((page_index, caption, window, opener))

    def subpage_of(self, window):
        """(page index, opener) of the button window holding `window`."""
        for page_index, _caption, sub, opener in self._subpages:
            if sub is window:
                return page_index, opener
        return wx.NOT_FOUND, None

    def GetPageText(self, index):
        return self._titles[index] if 0 <= index < len(self._titles) else ""

    def SetPageText(self, index, text):
        if not 0 <= index < len(self._titles):
            return False
        self._titles[index] = text
        if self._list is not None and not self._searching and index < self._list.GetItemCount():
            self._list.SetItemText(index, text)
        return True

    def GetSelection(self):
        return self._selection

    def SetSelection(self, index):
        """Show page `index`, move the list to it and tell the dialog."""
        previous = self._selection
        self.ChangeSelection(index)
        if index != previous:
            event = wx.BookCtrlEvent(wx.EVT_NOTEBOOK_PAGE_CHANGED.typeId, self.GetId(),
                                     index, previous)
            event.SetEventObject(self)
            self.GetEventHandler().ProcessEvent(event)
        return previous

    def ChangeSelection(self, index):
        if not 0 <= index < self.GetPageCount():
            return wx.NOT_FOUND
        previous = self._selection
        super().ChangeSelection(index)
        self._selection = index
        self._select_in_list(index)
        return previous

    # ── The list and the search field ────────────────────────────────────────

    def build_navigation(self, parent):
        """The sizer holding search, list and pages. Call after the pages."""
        outer = wx.BoxSizer(wx.VERTICAL)
        self._search_label = wx.StaticText(parent, label="")
        self._search = wx.TextCtrl(parent, style=wx.TE_PROCESS_ENTER)
        self._search.Bind(wx.EVT_TEXT, self._on_search_text)
        self._search.Bind(wx.EVT_TEXT_ENTER, self._on_search_enter)
        self._search.Bind(wx.EVT_KEY_DOWN, self._on_search_key)
        outer.Add(self._search_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        outer.Add(self._search, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        body = wx.BoxSizer(wx.HORIZONTAL)
        # One column and no header: the screen reader says the item and its
        # position, not a column name before every item.
        self._list = wx.ListCtrl(
            parent, size=(210, 320),
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_NO_HEADER)
        self._list.InsertColumn(0, "", width=190)
        for title in self._titles:
            self._list.InsertItem(self._list.GetItemCount(), title)
        self._list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_list_selected)
        body.Add(self._list, 0, wx.EXPAND | wx.ALL, 8)

        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(self, 1, wx.EXPAND)
        self._results_panel = wx.Panel(parent)
        res = wx.BoxSizer(wx.VERTICAL)
        self._results = wx.ListBox(self._results_panel)
        self._results.Bind(wx.EVT_LISTBOX_DCLICK, self._on_result_activated)
        # CHAR_HOOK, not KEY_DOWN: the dialog turns Enter into a click on its
        # default button before a list sees the key, so only this event, which
        # reaches the focused control first, can claim Enter.
        self._results.Bind(wx.EVT_CHAR_HOOK, self._on_results_key)
        res.Add(self._results, 1, wx.EXPAND)
        self._results_panel.SetSizer(res)
        self._results_panel.Hide()
        right.Add(self._results_panel, 1, wx.EXPAND)
        body.Add(right, 1, wx.EXPAND | wx.TOP | wx.RIGHT | wx.BOTTOM, 8)
        outer.Add(body, 1, wx.EXPAND)
        self._outer_parent = parent
        # Tab goes search -> categories -> the page (or the results); the book
        # was created first, so without this it would come before both.
        self._list.MoveAfterInTabOrder(self._search)
        self.MoveAfterInTabOrder(self._list)
        self._results_panel.MoveAfterInTabOrder(self)
        if self._selection != wx.NOT_FOUND:
            self._select_in_list(self._selection)
        return outer

    def set_search_texts(self, label, name, results_name, no_results, results_title):
        """Translated texts; also used again when the language changes."""
        self._search_label.SetLabel(label)
        self._search.SetName(name)
        self._results.SetName(results_name)
        self._no_results = no_results
        self._results_template = results_title
        try:
            self._search.SetHint(name)
        except Exception:
            pass

    @property
    def category_list(self):
        return self._list

    @property
    def search_field(self):
        return self._search

    def _select_in_list(self, index):
        if self._list is None or self._searching:
            return
        if index >= self._list.GetItemCount():
            return
        self._guard = True
        try:
            self._list.Select(index)
            self._list.Focus(index)
        finally:
            self._guard = False

    _guard = False
    _no_results = ""
    _results_template = "{count}"

    def _on_list_selected(self, event):
        if self._guard:
            return
        index = event.GetIndex()
        if self._searching:
            # The first row is the results; the others are the pages that
            # have a match, kept by position in `_shown`.
            if index == 0:
                self._show_results(True)
                return
            index = self._shown[index - 1]
        self._show_results(False)
        if index != self._selection:
            self.SetSelection(index)
            # Showing the page must not carry the focus away: the arrow keys
            # keep working on the list, and only Tab enters the page.
            wx.CallAfter(self._keep_focus_on_list)

    def _keep_focus_on_list(self):
        try:
            if self._list.IsShownOnScreen() and self.FindFocus() is not self._list:
                self._list.SetFocus()
        except RuntimeError:
            pass

    # ── Search ───────────────────────────────────────────────────────────────

    def _rebuild_index(self):
        self._index = []
        for i in range(self.GetPageCount()):
            self._index += collect_entries(self.GetPage(i), i, self._titles[i])
        for i, caption, window, opener in self._subpages:
            self._index += collect_entries(
                window, i, f"{self._titles[i]}: {caption}", opener)

    def _on_search_text(self, event):
        query = _fold(self._search.GetValue()).split()
        if not query:
            self._end_search()
            return
        self._rebuild_index()
        self._found = [e for e in self._index if all(w in e.folded for w in query)]
        self._searching = True
        self._shown = []
        for e in self._found:
            if e.page_index not in self._shown:
                self._shown.append(e.page_index)
        self._guard = True
        try:
            self._list.DeleteAllItems()
            self._list.InsertItem(0, self._results_title())
            for i in self._shown:
                self._list.InsertItem(self._list.GetItemCount(), self._titles[i])
            self._list.Select(0)
            self._list.Focus(0)
        finally:
            self._guard = False
        self._results.Set([f"{e.category}: {e.label}" for e in self._found])
        if not self._found:
            self._results.Set([self._no_results])
        self._show_results(True)

    def _results_title(self):
        return self._results_template.format(count=len(self._found))

    def _end_search(self):
        if not self._searching:
            return
        self._searching = False
        self._found = []
        self._guard = True
        try:
            self._list.DeleteAllItems()
            for title in self._titles:
                self._list.InsertItem(self._list.GetItemCount(), title)
        finally:
            self._guard = False
        self._show_results(False)
        self._select_in_list(self._selection)

    def _show_results(self, shown):
        self.Show(not shown)
        self._results_panel.Show(shown)
        self._outer_parent.Layout()

    def _on_search_enter(self, event):
        if self._found:
            self._results.SetSelection(0)
            self._results.SetFocus()

    def _on_search_key(self, event):
        if event.GetKeyCode() == wx.WXK_DOWN and self._searching and self._found:
            # Down arrow goes straight into the results, on the first one.
            self._results.SetSelection(0)
            self._results.SetFocus()
            return
        event.Skip()

    def _on_results_key(self, event):
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_SPACE):
            self._on_result_activated(event)
            return
        if key == wx.WXK_UP and self._results.GetSelection() <= 0:
            # Up from the first result goes back to the search field.
            self._search.SetFocus()
            return
        event.Skip()

    def _on_result_activated(self, event):
        i = self._results.GetSelection()
        if not 0 <= i < len(self._found):
            return
        entry = self._found[i]
        self._show_results(False)
        # Leaving the search puts the whole list back, with the page chosen.
        self._search.ChangeValue("")
        self._end_search()
        self.SetSelection(entry.page_index)
        if entry.opener is not None:
            wx.CallAfter(entry.opener, entry.control)
        else:
            wx.CallAfter(self._focus_control, entry.control)

    @staticmethod
    def _focus_control(control):
        try:
            control.SetFocus()
        except RuntimeError:
            pass
