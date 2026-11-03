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


def owner_window(active, fallback):
    """The window a question or a folder picker opens over: the one active now
    (a SubPageDialog, when the option lives in one), else *fallback*.

    Parented to the Settings dialog while a SubPageDialog is up, the box opened
    over a window disabled behind the modal one, and closing it sent the focus
    back there instead of to the option the user was on."""
    return active if active is not None else fallback


def dialog_owner(fallback):
    """owner_window() with the window active now; *fallback* when there is
    none, or no wx.App to ask (a test calling the handler on a stub)."""
    try:
        active = wx.GetActiveWindow()
    except Exception:
        active = None
    return owner_window(active, fallback)


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

    Its controls are built the first time it opens (`build`) and kept, like
    those of the pages, so the settings dialog reads and writes them the same
    way. Closing only hides it.
    """

    def __init__(self, parent, title, ok_label, on_change=None, first_open=None, build=None):
        super().__init__(parent, title=title,
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._body = None
        self._first_open = first_open   # called once, the first time it opens
        self._build = build             # puts the controls in, before that
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
            # EndModal() on a window that is not shown modally is a wx
            # assertion; one that somehow got the focus while hidden just hides.
            if self.IsModal():
                self.EndModal(wx.ID_OK)
            else:
                self.Hide()
            return
        event.Skip()

    @staticmethod
    def _focus(control):
        try:
            control.SetFocus()
        except RuntimeError:
            pass

    def open(self, control=None):
        if self._build is not None:
            build, self._build = self._build, None
            build()
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
              wx.ListBox, wx.ListCtrl, wx.Slider, wx.CheckListBox, wx.TreeCtrl)
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


#: How long the row of the results stays as it is after the focus leaves the
#: list. NVDA reads a row whose name changes while it believes the row has the
#: focus (docs/traps/screen-reader-speech.md), and it finds out late.
RESULTS_ROW_HOLD_MS = 1000


def new_entries(found, matches):
    """The `matches` not in `found` yet, in their order. A pass over the pages
    makes new SearchEntry objects, so an entry is known by what it shows."""
    known = {(e.category, e.label) for e in found}
    return [e for e in matches if (e.category, e.label) not in known]


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

    def __init__(self, parent, before_show=None, before_search=None):
        super().__init__(parent)
        self._before_show = before_show         # called with a page's index
        self._before_search = before_search     # the search needs every control
        self._loading = False                   # pages are still being built for it
        self._titles = []
        self._list = None
        self._search = None
        self._results = None
        self._results_panel = None
        self._found = []
        self._index = []
        self._subpages = []     # (page_index, caption, window or its path, opener)
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
        the page's own, and opened by `opener(control)` when chosen. `window`
        is the window, or the dotted path from the dialog to one that may not
        exist yet ("_speech_page", "_ai_page.auto_panel")."""
        self._subpages.append((page_index, caption, window, opener))

    def _subpage_window(self, window):
        """The window a subpage names, None while it is not built. Read without
        asking the dialog for an attribute it does not have yet: that would
        build it, and looking is not a reason to."""
        if not isinstance(window, str):
            return window
        found = self.GetParent()
        for part in window.split("."):
            found = vars(found).get(part)
            if found is None:
                return None
        return found

    def subpage_of(self, window):
        """(page index, opener) of the button window holding `window`."""
        for page_index, _caption, sub, opener in self._subpages:
            if self._subpage_window(sub) is window:
                return page_index, opener
        return wx.NOT_FOUND, None

    def FindPage(self, page):
        """The index of `page`, or of the panel holding its place."""
        index = super().FindPage(page)
        if index == wx.NOT_FOUND and page is not None and page.GetParent() is not None:
            index = super().FindPage(page.GetParent())
        return index

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
        if self._before_show is not None:
            self._before_show(index)
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
        self._list.Bind(wx.EVT_KILL_FOCUS, self._on_list_focus_lost)
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

    def set_search_texts(self, label, name, results_name, no_results, results_title, loading):
        """Translated texts; also used again when the language changes."""
        self._search_label.SetLabel(label)
        self._search.SetName(name)
        self._results.SetName(results_name)
        self._no_results = no_results
        self._results_template = results_title
        self._loading_text = loading
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
    _loading_text = ""
    _results_row_stale = False      # the first row waits for the focus to go
    _results_row_timer = None       # running: the focus has only just gone
    _rows_shown = None              # the results list as last set

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
            window = self._subpage_window(window)
            if window is not None:
                self._index += collect_entries(
                    window, i, f"{self._titles[i]}: {caption}", opener)

    def _on_search_text(self, event):
        query = _fold(self._search.GetValue()).split()
        if not query:
            self._end_search()
            return
        if self._before_search is not None:
            self._before_search()
        self._found = self._matches(query)
        self._results_row_stale = False
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
        self._rows_shown = self._result_rows()
        self._results.Set(self._rows_shown)
        self._show_results(True)

    def _matches(self, query):
        self._rebuild_index()
        return [e for e in self._index if all(w in e.folded for w in query)]

    def _result_rows(self):
        """The rows of the results list. "Nothing found" only once nothing is
        left to search: said while pages are being built, it could be false."""
        if self._found:
            return [f"{e.category}: {e.label}" for e in self._found]
        return [self._loading_text if self._loading else self._no_results]

    @property
    def search_loading(self):
        return self._loading

    def set_search_loading(self, loading):
        self._loading = loading
        self.refresh_search()

    def refresh_search(self):
        """A page was built during a search: add what it matches at the end, so
        a result already chosen stays chosen and where it was."""
        if not self._searching:
            return
        added = new_entries(self._found, self._matches(_fold(self._search.GetValue()).split()))
        first_results = added and not self._found
        self._found += added
        self._guard = True
        self._list.Freeze()
        try:
            # A row added at the end changes no row's name, so it is safe under
            # the focus: the screen reader says nothing and the arrows reach it.
            for e in added:
                if e.page_index not in self._shown:
                    self._shown.append(e.page_index)
                    self._list.InsertItem(self._list.GetItemCount(), self._titles[e.page_index])
        finally:
            self._list.Thaw()
            self._guard = False
        self._refresh_results_row()
        if first_results or not self._found:
            rows = self._result_rows()
            if rows == self._rows_shown:
                # The same placeholder: set again, it would be read again.
                return
            self._rows_shown = rows
            on_the_placeholder = self._results.HasFocus()
            self._results.Set(rows)
            if on_the_placeholder:
                # The only row under the focus is gone; choosing its successor
                # is what tells the screen reader, once.
                self._results.SetSelection(0)
        elif added:
            self._results.Freeze()
            try:
                for e in added:
                    self._results.Append(f"{e.category}: {e.label}")
            finally:
                self._results.Thaw()

    def _refresh_results_row(self):
        """The count on the first row. The one row rewritten, so it waits while
        the focus is on the list and a moment after: rewritten under the focus,
        it would be read again for every page that arrives."""
        timer = self._results_row_timer
        if self._list.HasFocus() or (timer is not None and timer.IsRunning()):
            self._results_row_stale = True
            return
        self._results_row_stale = False
        self._guard = True
        try:
            self._list.SetItemText(0, self._results_title())
        finally:
            self._guard = False

    def _on_list_focus_lost(self, event):
        # Every time during a search: a page that arrives just after the focus
        # left is as early for the screen reader as one that arrived before.
        event.Skip()
        if not self._searching:
            return
        if self._results_row_timer is None:
            self._results_row_timer = wx.CallLater(RESULTS_ROW_HOLD_MS, self._results_row_settled)
        else:
            self._results_row_timer.Start(RESULTS_ROW_HOLD_MS)

    def _results_row_settled(self):
        if self and self._searching and self._results_row_stale:
            self._refresh_results_row()

    def _results_title(self):
        return self._results_template.format(count=len(self._found))

    def _end_search(self):
        if not self._searching:
            return
        self._searching = False
        self._results_row_stale = False
        self._rows_shown = None
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
