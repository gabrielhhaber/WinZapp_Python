"""The Calls tab (Alt+6): every call of every conversation in one list.

WhatsApp keeps each call as a message in its chat (core/call_log.py); this tab
lists those records across all chats, newest first, the way WhatsApp's own
Calls tab does. The rows use the same label as the conversation row ("Ligação
de voz perdida", "…atendida, duração: …"), prefixed with the other party's
name, since "Eu:" says nothing in a list that spans every conversation.

A wx.Notebook holds the filters -- Todas (selected by default), Perdidas,
Recusadas, Atendidas -- each page with its own "&Ligações" list, so Ctrl+Tab
switches filter and Alt+L lands on the list of whichever page is shown. Enter
opens the call's conversation; on a missed one-to-one call Ctrl+Shift+R, the
context menu and the "Retornar ligação" button call back, as in the
conversation itself.
"""

import logging
import threading

import wx

from core.call_log import (
    CALL_TABS,
    TAB_ALL,
    TAB_ANSWERED,
    TAB_DECLINED,
    TAB_MISSED,
    call_log_in_tab,
    call_log_is_video,
    call_log_label,
    call_row_text,
    collect_call_logs,
    is_returnable_missed_call,
)
from ui.accessible import AccessibleReturnCallButton

# How many call records the tab reads from the database at most.
CALLS_TAB_LIMIT = 1000

_TAB_LABEL_KEYS = {
    TAB_ALL: "calls_tab_all",
    TAB_MISSED: "calls_tab_missed",
    TAB_DECLINED: "calls_tab_declined",
    TAB_ANSWERED: "calls_tab_answered",
}


class CallsPanel(wx.Panel):
    def __init__(self, main_window, parent):
        super().__init__(parent)
        self.main_window = main_window
        # Every call record, newest first: [{"jid": ..., "msg": ...}].
        self._entries = []
        # Per tab, the entries its list shows, row for row.
        self._rows = {tab: [] for tab in CALL_TABS}
        self._lists = {}
        self._list_labels = {}
        self._loaded = False
        self._refresh_pending = False
        self.init_UI()
        self._create_accelerators()

    # ── UI ───────────────────────────────────────────────────────────────────

    def init_UI(self):
        i18n = self.main_window.i18n
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.notebook = wx.Notebook(self)
        for tab in CALL_TABS:
            page = wx.Panel(self.notebook)
            page_sizer = wx.BoxSizer(wx.VERTICAL)
            label = wx.StaticText(page, label=i18n.t("calls_list_label"))
            page_sizer.Add(label, 0, wx.LEFT | wx.TOP, 5)
            lst = wx.ListCtrl(page, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
            lst.InsertColumn(0, i18n.t("calls_list_label").replace("&", ""), width=480)
            lst.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_item_activated)
            lst.Bind(wx.EVT_LIST_ITEM_FOCUSED, self._on_item_focused)
            lst.Bind(wx.EVT_CONTEXT_MENU, self._on_context_menu)
            page_sizer.Add(lst, 1, wx.EXPAND | wx.ALL, 5)
            page.SetSizer(page_sizer)
            self.notebook.AddPage(page, i18n.t(_TAB_LABEL_KEYS[tab]), select=(tab == TAB_ALL))
            self._lists[tab] = lst
            self._list_labels[tab] = label
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_page_changed)
        sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 5)

        # After the notebook, so it is the next Tab stop from the list. Shown
        # only while a returnable missed call is focused, as in a conversation.
        self._return_call_btn = wx.Button(self, label=i18n.t("return_call_button"))
        self._return_call_btn.SetAccessible(AccessibleReturnCallButton())
        self._return_call_btn.Bind(wx.EVT_BUTTON, self._on_return_call)
        sizer.Add(self._return_call_btn, 0, wx.LEFT | wx.BOTTOM, 5)
        self._return_call_btn.Hide()

        self.SetSizer(sizer)

    def _create_accelerators(self):
        self.ID_ALT_L = wx.NewIdRef()
        self.ID_CTRL_SHIFT_R = wx.NewIdRef()
        self.ID_F5 = wx.NewIdRef()
        # Explicit, like ConversationsPanel's Alt+M for "&Mensagens": a
        # StaticText mnemonic moving focus proved unreliable in this app.
        self.SetAcceleratorTable(wx.AcceleratorTable([
            (wx.ACCEL_ALT, ord("L"), self.ID_ALT_L),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("R"), self.ID_CTRL_SHIFT_R),
            (wx.ACCEL_NORMAL, wx.WXK_F5, self.ID_F5),
        ]))
        self.Bind(wx.EVT_MENU, lambda e: self.focus_list(), id=self.ID_ALT_L)
        self.Bind(wx.EVT_MENU, self._on_return_call, id=self.ID_CTRL_SHIFT_R)
        self.Bind(wx.EVT_MENU, lambda e: self.on_show(), id=self.ID_F5)

    def refresh_labels(self):
        """Update every translatable label after a language change."""
        i18n = self.main_window.i18n
        for index, tab in enumerate(CALL_TABS):
            self.notebook.SetPageText(index, i18n.t(_TAB_LABEL_KEYS[tab]))
            self._list_labels[tab].SetLabel(i18n.t("calls_list_label"))
            col = wx.ListItem()
            col.SetText(i18n.t("calls_list_label").replace("&", ""))
            self._lists[tab].SetColumn(0, col)
        self._return_call_btn.SetLabel(i18n.t("return_call_button"))
        if self._loaded:
            self._populate_all()

    # ── State ────────────────────────────────────────────────────────────────

    def current_tab(self) -> str:
        page = self.notebook.GetSelection()
        return CALL_TABS[page] if 0 <= page < len(CALL_TABS) else TAB_ALL

    def current_list(self):
        return self._lists[self.current_tab()]

    def focused_entry(self):
        tab = self.current_tab()
        idx = self._lists[tab].GetFocusedItem()
        rows = self._rows[tab]
        return rows[idx] if 0 <= idx < len(rows) else None

    def focus_list(self):
        lst = self.current_list()
        lst.SetFocus()
        if lst.GetItemCount() and lst.GetFocusedItem() < 0:
            lst.Focus(0)
            lst.Select(0)

    # ── Loading ──────────────────────────────────────────────────────────────

    def on_show(self):
        """Alt+6 / the navigation item: reload and put focus on the list."""
        if not self._loaded:
            self._show_placeholder(self.main_window.i18n.t("loading"))
        self.focus_list()
        threading.Thread(target=self._load, daemon=True, name="calls-tab-load").start()

    def schedule_refresh(self):
        """A call record arrived or changed: reload once, shortly, if shown."""
        if self._refresh_pending or not self.IsShown():
            return
        self._refresh_pending = True
        wx.CallLater(700, self._run_scheduled_refresh)

    def _run_scheduled_refresh(self):
        self._refresh_pending = False
        if self.IsShown():
            threading.Thread(target=self._load, daemon=True, name="calls-tab-load").start()

    def _load(self):
        mw = self.main_window
        stored = []
        db = getattr(mw, "db", None)
        if db is not None:
            try:
                stored = db.get_call_logs(CALLS_TAB_LIMIT)
            except Exception:
                logging.exception("[calls_tab] reading call records failed")
        entries = collect_call_logs(stored, getattr(mw, "chats", {}))
        wx.CallAfter(self._apply_entries, entries)

    def _apply_entries(self, entries):
        if not self:
            return
        self._entries = entries
        self._loaded = True
        self._populate_all()

    # ── Rows ─────────────────────────────────────────────────────────────────

    def _row_text(self, entry) -> str:
        mw = self.main_window
        panel = getattr(mw, "conversations_panel", None)
        msg = entry["msg"]
        label = call_log_label(
            msg, mw.i18n, panel._format_duration if panel is not None else (lambda _s: ""))
        when = ""
        if panel is not None:
            ts = panel._extract_timestamp(msg)
            when = panel._format_date(ts) if ts else ""
        return call_row_text(entry, mw.chat_display_name(entry["jid"]), label, when)

    def _show_placeholder(self, text):
        for tab in CALL_TABS:
            lst = self._lists[tab]
            lst.Freeze()
            try:
                lst.DeleteAllItems()
                lst.Append((text,))
            finally:
                lst.Thaw()
            self._rows[tab] = []
        self._update_return_call_button()

    def _populate_all(self):
        empty = self.main_window.i18n.t("calls_empty")
        for tab in CALL_TABS:
            lst = self._lists[tab]
            keep = self._focused_id(tab)
            rows = [e for e in self._entries if call_log_in_tab(e["msg"], tab)]
            self._rows[tab] = rows
            lst.Freeze()
            try:
                lst.DeleteAllItems()
                if not rows:
                    lst.Append((empty,))
                for entry in rows:
                    lst.Append((self._row_text(entry),))
                target = next((i for i, e in enumerate(rows) if self._entry_id(e) == keep), 0)
                if lst.GetItemCount():
                    lst.Focus(target)
                    lst.Select(target)
            finally:
                lst.Thaw()
        self._update_return_call_button()

    @staticmethod
    def _entry_id(entry) -> str:
        return str(((entry or {}).get("msg") or {}).get("key", {}).get("id") or "")

    def _focused_id(self, tab) -> str:
        idx = self._lists[tab].GetFocusedItem()
        rows = self._rows[tab]
        return self._entry_id(rows[idx]) if 0 <= idx < len(rows) else ""

    # ── Events ───────────────────────────────────────────────────────────────

    def _on_page_changed(self, event):
        self._update_return_call_button()
        event.Skip()

    def _on_item_focused(self, event):
        self._update_return_call_button()
        event.Skip()

    def _update_return_call_button(self):
        entry = self.focused_entry()
        show = entry is not None and is_returnable_missed_call(entry["msg"], entry["jid"])
        self._return_call_btn.Show(show)
        self.Layout()

    def _on_item_activated(self, event):
        entry = self.focused_entry()
        if entry is not None:
            self.main_window.navigate_to_conversation_jid(entry["jid"])

    def _on_return_call(self, _event=None):
        entry = self.focused_entry()
        if entry is None:
            return
        if not is_returnable_missed_call(entry["msg"], entry["jid"]):
            # Ctrl+Shift+R on anything else: say so rather than do nothing.
            self.main_window.output(
                self.main_window.i18n.t("return_call_unavailable"), interrupt=True)
            return
        mw = self.main_window
        name = mw.chat_display_name(entry["jid"])
        if call_log_is_video(entry["msg"]):
            mw.start_video_call(entry["jid"], name)
        else:
            mw.start_voice_call(entry["jid"], name)

    def _on_context_menu(self, event):
        entry = self.focused_entry()
        if entry is None:
            return
        i18n = self.main_window.i18n
        menu = wx.Menu()
        open_item = menu.Append(wx.ID_ANY, f"{i18n.t('calls_open_conversation')}\tEnter")
        self.Bind(wx.EVT_MENU, self._on_item_activated, open_item)
        if is_returnable_missed_call(entry["msg"], entry["jid"]):
            call_item = menu.Append(
                wx.ID_ANY, f"{i18n.t('return_call_button')}\tCtrl+Shift+R")
            self.Bind(wx.EVT_MENU, self._on_return_call, call_item)
        self.PopupMenu(menu)
        menu.Destroy()
