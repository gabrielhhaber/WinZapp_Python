"""The Calls tab (Alt+6): every call of every conversation not locked away.

WhatsApp keeps each call as a message in its chat (core/call_log.py); this tab
lists those records across all chats, newest first, the way WhatsApp's own
Calls tab does. The rows use the same label as the conversation row ("Ligação
de voz perdida", "…atendida, duração: …"), prefixed with the other party's
name, since "Eu:" says nothing in a list that spans every conversation. A
call filed under a locked chat is excluded the same way the main/archived
lists exclude it (docs/traps -- chat_lock_vault): its name and history stay
out of this tab, and returning it (Ctrl+Shift+R / the button) is refused,
regardless of whether the vault happens to be unlocked right now.

A wx.Notebook holds the filters -- Todas (selected by default), Perdidas,
Recusadas, Atendidas, Em andamento -- each page with its own "&Ligações" list, so Ctrl+Tab
switches filter and Alt+L lands on the list of whichever page is shown. Enter
opens the call's conversation; on a missed one-to-one call Ctrl+Shift+R, the
context menu and the "Retornar ligação" button call back, as in the
conversation itself. Live calls come from WhatsApp's ongoing collection,
appear first in All and on Ongoing, and Enter joins them by call id.
"""

from ui.shortcut_bindings import refresh_popup_shortcuts
from ui.shortcut_bindings import make_shortcut_table
from core.conversation_view import mnemonic_letter
import logging
import threading

import wx

from core.call_log import (
    CALL_TABS,
    TAB_ALL,
    TAB_ANSWERED,
    TAB_DECLINED,
    TAB_MISSED,
    TAB_ONGOING,
    call_log_in_tab,
    call_log_is_video,
    call_log_label,
    call_row_text,
    collect_call_logs,
    exclude_locked_calls,
    is_returnable_missed_call,
    list_update_plan,
)
from core.list_row_diff import plan_row_diff
from ui.accessible import (
    AccessibleAltShortcutButton,
    AccessibleReturnCallButton,
    accelerator_keycode,
    split_mnemonic,
)
from ui.ongoing_calls import OngoingCallsMixin

# How many call records the tab reads from the database at most.
CALLS_TAB_LIMIT = 1000

_TAB_LABEL_KEYS = {
    TAB_ALL: "calls_tab_all",
    TAB_MISSED: "calls_tab_missed",
    TAB_DECLINED: "calls_tab_declined",
    TAB_ANSWERED: "calls_tab_answered",
    TAB_ONGOING: "calls_tab_ongoing",
}


class CallsPanel(OngoingCallsMixin, wx.Panel):
    def __init__(self, main_window, parent):
        super().__init__(parent)
        self.main_window = main_window
        # Every call record, newest first: [{"jid": ..., "msg": ...}].
        self._entries = []
        # Per tab, the entries its list shows, row for row.
        self._rows = {tab: [] for tab in CALL_TABS}
        # Per tab, what its list shows right now: [(call id or "", text)].
        # _apply_rows() diffs against it so a reload writes only what changed.
        self._shown = {tab: [] for tab in CALL_TABS}
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
            from ui.shortcut_bindings import handle_call_list_shortcut
            lst.Bind(wx.EVT_KEY_DOWN, lambda e: handle_call_list_shortcut(self, e))
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
        self._init_ongoing_calls(sizer)
        # WhatsApp's "New call link" (ui/call_link_dialog.py); always there.
        self._call_link_btn = wx.Button(self)
        self._label_call_link_button()
        self._call_link_btn.Bind(wx.EVT_BUTTON, self._on_create_call_link)
        sizer.Add(self._call_link_btn, 0, wx.LEFT | wx.BOTTOM, 5)

        self.SetSizer(sizer)

    def _create_accelerators(self):
        from ui.shortcut_bindings import handle_calls_shortcut
        if not getattr(self, '_custom_call_hook_bound', False):
            self.Bind(wx.EVT_CHAR_HOOK, lambda e: handle_calls_shortcut(self, e))
            self._custom_call_hook_bound = True
        self.ID_ALT_L = wx.NewIdRef()
        self.ID_CTRL_SHIFT_R = wx.NewIdRef()
        self.ID_F5 = wx.NewIdRef()
        self.ID_ALT_CALL_LINK = wx.NewIdRef()
        i18n = self.main_window.i18n
        # Explicit, like ConversationsPanel's Alt+M for "&Mensagens": a
        # StaticText mnemonic moving focus proved unreliable in this app.
        self.SetAcceleratorTable(make_shortcut_table(self, 'calls', [
            (wx.ACCEL_ALT, ord("L"), self.ID_ALT_L),
            (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord("R"), self.ID_CTRL_SHIFT_R),
            (wx.ACCEL_NORMAL, wx.WXK_F5, self.ID_F5),
            # The label's "&" letter, as an accelerator: a native mnemonic on this
            # panel's button never fired (see _label_call_link_button).
            (wx.ACCEL_ALT, ord(mnemonic_letter(
                i18n.t("calls_create_call_link_button"), "I")), self.ID_ALT_CALL_LINK),
        ]))
        self.Bind(wx.EVT_MENU, lambda e: self.focus_list(), id=self.ID_ALT_L)
        self.Bind(wx.EVT_MENU, self._on_return_call, id=self.ID_CTRL_SHIFT_R)
        self.Bind(wx.EVT_MENU, lambda e: self.on_show(), id=self.ID_F5)
        self.Bind(wx.EVT_MENU, self._on_create_call_link, id=self.ID_ALT_CALL_LINK)

    def _label_call_link_button(self):
        """Label "Create call link" without a native mnemonic.

        Alt+<letter> is an accelerator (ID_ALT_CALL_LINK), as for the incoming-call
        buttons: the `&` mnemonic did nothing on this panel, and a real one would
        also fire on the bare letter while the button has focus.
        """
        text = self.main_window.i18n.t("calls_create_call_link_button")
        label, letter = split_mnemonic(text)
        self._call_link_btn.SetLabel(label)
        if accelerator_keycode(letter) is None:
            self._call_link_btn.SetAccessible(None)
            return
        self._call_link_btn.SetAccessible(AccessibleAltShortcutButton(letter))

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
        self._label_call_link_button()
        self._create_accelerators()
        if self._loaded:
            self._populate_all()

    def _on_create_call_link(self, _event=None):
        from ui.call_link_dialog import CallLinkDialog
        with CallLinkDialog(self, self.main_window) as dialog:
            dialog.ShowModal()

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
        self._watch_ongoing_calls()
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
        is_locked = getattr(mw, "is_chat_locked", None)
        if is_locked is not None:
            entries = exclude_locked_calls(entries, is_locked)
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
        if entry.get("ongoing"):
            key = "call_log_ongoing_video" if entry["ongoing"]["isVideo"] else "call_log_ongoing_voice"
            return f'{self._ongoing_call_name(entry)}: {mw.i18n.t(key)}'
        panel = getattr(mw, "conversations_panel", None)
        msg = entry["msg"]
        label = call_log_label(
            msg, mw.i18n, panel._format_duration if panel is not None else (lambda _s: ""))
        when = ""
        if panel is not None:
            ts = panel._extract_timestamp(msg)
            when = panel._format_date(ts) if ts else ""
        # A one-to-one call that became a group call is named after its people,
        # as on the phone ("A, B e mais 3 pessoas"), not after the chat it was
        # filed under (for one placed here, the account's own chat).
        people = getattr(mw, "call_log_participants_text", None)
        name = (people(msg, entry["jid"]) if people is not None else "") \
            or mw.chat_display_name(entry["jid"])
        return call_row_text(entry, name, label, when)

    def _show_placeholder(self, text):
        for tab in CALL_TABS:
            self._rows[tab] = []
            self._apply_rows(tab, [("", text)])
        self._update_return_call_button()

    def _populate_all(self, tabs=None):
        """Bring *tabs* (every tab by default) up to date with the history
        and the live calls."""
        empty = self.main_window.i18n.t("calls_empty")
        for tab in tabs or CALL_TABS:
            rows = [e for e in self._entries if call_log_in_tab(e["msg"], tab)]
            live = self._ongoing_rows_for_tab(tab)
            ids = {self._entry_id(entry) for entry in live}
            rows = live + [entry for entry in rows if self._entry_id(entry) not in ids]
            keep = self._focused_id(tab)
            self._rows[tab] = rows
            placeholder = empty
            if tab == TAB_ONGOING:
                placeholder = self.main_window.i18n.t(self._ongoing_placeholder_key())
            shown = ([(self._entry_id(e), self._row_text(e)) for e in rows]
                     or [("", placeholder)])
            self._apply_rows(tab, shown, keep)
        self._update_return_call_button()

    def _apply_rows(self, tab, shown, keep_id=""):
        """Bring one list to *shown* ((id, text) rows), touching as little as
        possible: the calls tab reloads whenever a call record is stored in
        any chat, and a rebuilt row is read out again by the screen reader
        (docs/traps/screen-reader-speech.md) -- so an unchanged list is not
        written at all, and a call whose outcome settled rewrites its row only.
        """
        lst = self._lists[tab]
        old = self._shown[tab]
        action, changes = list_update_plan(old, shown)
        self._shown[tab] = list(shown)
        if action == "none":
            return
        lst.Freeze()
        try:
            if action == "set":
                for index, text in changes:
                    lst.SetItemText(index, text)
                return
            # A live call starting or ending inserts or deletes its row only;
            # clearing the list would re-announce the row the user is on.
            deletes, inserts = plan_row_diff([row_id for row_id, _t in old],
                                             [row_id for row_id, _t in shown])
            for index in deletes:
                lst.DeleteItem(index)
            for index in inserts:
                lst.InsertItem(index, shown[index][1])
            gone, added = set(deletes), set(inserts)
            kept = iter([text for i, (_id, text) in enumerate(old) if i not in gone])
            for index, (_id, text) in enumerate(shown):
                if index not in added and next(kept) != text:
                    lst.SetItemText(index, text)
            target = next((i for i, (row_id, _t) in enumerate(shown)
                           if keep_id and row_id == keep_id), 0)
            # A surviving focused row has moved with its index; refocus only
            # when it is gone or the list had no focus.
            if lst.GetItemCount() and lst.GetFocusedItem() != target:
                lst.Focus(target)
                lst.Select(target)
        finally:
            lst.Thaw()

    @staticmethod
    def _entry_id(entry) -> str:
        if (entry or {}).get("ongoing"):
            return str(entry["ongoing"]["id"])
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
        mw = self.main_window
        locked = entry is not None and getattr(
            mw, "is_chat_locked", lambda _jid: False)(entry["jid"])
        show = (entry is not None and not entry.get("ongoing") and not locked
                and is_returnable_missed_call(entry["msg"], entry["jid"]))
        self._return_call_btn.Show(show)
        self._update_ongoing_join_button()
        self.Layout()

    def _on_item_activated(self, event):
        entry = self.focused_entry()
        if entry is not None and entry.get("ongoing"):
            self._join_ongoing_call()
            return
        if entry is not None:
            self.main_window.navigate_to_conversation_jid(entry["jid"])

    def _on_return_call(self, _event=None):
        entry = self.focused_entry()
        if entry is not None and entry.get("ongoing"):
            self._join_ongoing_call()
            return
        mw = self.main_window
        locked = entry is not None and getattr(
            mw, "is_chat_locked", lambda _jid: False)(entry["jid"])
        if (entry is None or locked
                or not is_returnable_missed_call(entry["msg"], entry["jid"])):
            # Ctrl+Shift+R on anything else: say so rather than do nothing.
            # A row for a chat that got locked after this tab last loaded
            # (schedule_refresh() runs on a delay) falls in here too --
            # never place a call to a locked contact without the PIN.
            mw.output(mw.i18n.t("return_call_unavailable"), interrupt=True)
            return
        name = mw.chat_display_name(entry["jid"])
        if call_log_is_video(entry["msg"]):
            mw.start_video_call(entry["jid"], name)
        else:
            mw.start_voice_call(entry["jid"], name)

    def _on_context_menu(self, event):
        entry = self.focused_entry()
        if entry is None:
            return
        if entry.get("ongoing"):
            self._ongoing_context_menu()
            return
        i18n = self.main_window.i18n
        menu = wx.Menu()
        open_item = menu.Append(wx.ID_ANY, f"{i18n.t('calls_open_conversation')}\tEnter")
        self.Bind(wx.EVT_MENU, self._on_item_activated, open_item)
        if is_returnable_missed_call(entry["msg"], entry["jid"]):
            call_item = menu.Append(
                wx.ID_ANY, f"{i18n.t('return_call_button')}\tCtrl+Shift+R")
            self.Bind(wx.EVT_MENU, self._on_return_call, call_item)
        refresh_popup_shortcuts(self, 'calls', menu)
        self.PopupMenu(menu)
        menu.Destroy()
