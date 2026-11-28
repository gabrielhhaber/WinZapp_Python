"""The window that searches the text of every conversation (ACC-05).

Ctrl+Shift+G or File > "Search all conversations". A search field, a list of
results, and the words a result row reads: "chat — sender — date — excerpt".
Enter in the field starts the search (the history is encrypted, so it is read
and decrypted page by page, never on every keystroke); Enter in the list, or
"Open in the conversation", opens the chat on that message the way the starred
messages window does. Escape stops a running search first and closes the
window the second time.

core/history_search.py has the scan and the matching. Here: the thread that
runs it, the rows, and what is spoken. Speech is deliberately sparse: one
"Searching..." when it starts and one count when it ends (or is stopped); the
status line under the list follows the running count silently.

Locked chats: the scan never opens a chat the vault hides, and every batch of
hits is checked again on arrival, because the vault can close while a search
runs. Nothing spoken or shown counts what was left out. Never log the query,
a name or a result.
"""
import logging
import threading

import wx

from core import history_search as search
from core.starred_list import StarredEntry, one_line
from ui.dialogs.background_work import BackgroundWork
from ui.dialogs.labelled_control import add_labelled
from ui.dialogs.starred_messages import open_starred_message


def _hidden_check(mw):
    return getattr(mw, "is_chat_hidden_by_vault", lambda _jid: False)


def chat_names_snapshot(mw) -> dict:
    """{jid: the name the chat is shown under}, read on the window's thread so
    the scan thread only reads a plain dict. A chat the vault hides is left out."""
    hidden = _hidden_check(mw)
    names = {}
    for jid, chat in list(getattr(mw, "chats", {}).items()):
        try:
            if not hidden(jid):
                names[jid] = mw.chat_display_name(chat)
        except Exception:
            continue
    return names


def run_scan(mw, words, mode, names, self_label, cancelled, on_hits) -> search.ScanResult:
    """The scan over the account's database; blocking, for a worker thread."""
    db = mw.db
    return search.scan_history(
        db.get_message_rows_page, db.decrypt_message_json, words, mode=mode,
        is_hidden=_hidden_check(mw), chat_name=lambda jid: names.get(jid, ""),
        self_label=self_label, cancelled=cancelled, on_hits=on_hits)


def sender_label(mw, hit) -> str:
    """Who wrote the message, by name: mine, a group member, or the contact of
    a one-to-one chat. No lookup per row (up to RESULT_LIMIT rows would start
    that many threads)."""
    msg, jid = hit.message, hit.jid
    key = msg.get("key") or {}
    if key.get("fromMe"):
        return mw.self_reference_label()
    panel = getattr(mw, "conversations_panel", None)
    if jid.endswith("@g.us") and key.get("participant") and panel is not None:
        name = panel._get_participant_name(key.get("participant"), msg, resolve_missing=False)
        if name:
            return name
    return msg.get("pushName") or mw.chat_display_name(jid)


def row_text(mw, hit) -> str:
    """The row of one result, in the words the conversation uses."""
    panel = getattr(mw, "conversations_panel", None)
    when = ""
    if panel is not None and hit.timestamp:
        try:
            when = panel._format_date(hit.timestamp)
        except Exception:
            when = ""
    return mw.i18n.t("history_search_row").format(
        chat=one_line(mw.chat_display_name(hit.jid)),
        sender=one_line(sender_label(mw, hit)),
        date=when,
        text=one_line(hit.snippet) or mw.i18n.t("starred_no_text"),
    )


class HistorySearchDialog(BackgroundWork, wx.Dialog):
    def __init__(self, parent, main_window):
        self._init_background(main_window)
        i18n = self._i18n
        self._hits, self._running = [], False
        self._cancel = threading.Event()
        self._run_id = 0
        self.result = None
        super().__init__(parent, title=i18n.t("history_search_title"), size=(680, 560),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._query = add_labelled(self, sizer, i18n.t("history_search_label"),
                                   lambda: wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER))
        self._query.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        self._list = add_labelled(self, sizer, i18n.t("history_search_results"),
                                  lambda: wx.ListBox(self, style=wx.LB_SINGLE), 1)
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_open)
        self._list.Bind(wx.EVT_LISTBOX, lambda event: self._update_buttons())
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._search_btn = wx.Button(self, label=i18n.t("history_search_button"))
        self._search_btn.Bind(wx.EVT_BUTTON, self._on_search)
        self._stop_btn = wx.Button(self, label=i18n.t("history_search_stop"))
        self._stop_btn.Bind(wx.EVT_BUTTON, lambda event: self._stop())
        self._open_btn = wx.Button(self, label=i18n.t("starred_open"))
        self._open_btn.Bind(wx.EVT_BUTTON, self._on_open)
        close = wx.Button(self, wx.ID_CANCEL, label=i18n.t("close"))
        close.Bind(wx.EVT_BUTTON, self._on_close)
        for button in (self._search_btn, self._stop_btn, self._open_btn, close):
            buttons.Add(button, 0, wx.ALL, 4)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._update_buttons()
        self._query.SetFocus()
        self.CentreOnParent()

    # ── state ─────────────────────────────────────────────────────────────

    def _selected(self):
        index = self._list.GetSelection()
        return self._hits[index] if 0 <= index < len(self._hits) else None

    def _update_buttons(self):
        self._stop_btn.Enable(self._running)
        self._open_btn.Enable(self._selected() is not None)

    # ── searching ─────────────────────────────────────────────────────────

    def _on_search(self, event=None):
        if self._running:
            return
        mw, i18n = self._mw, self._i18n
        mode = mw._search_normalization_mode()
        words = search.query_words(self._query.GetValue(), mode)
        if not words:
            self._announce(i18n.t("history_search_empty_query"))
            return
        self._hits = []
        self._list.Freeze()
        try:
            self._list.Clear()
        finally:
            self._list.Thaw()
        self._cancel = threading.Event()
        self._run_id += 1
        self._running = True
        self._update_buttons()
        self._announce(i18n.t("history_search_searching"))
        cancel, run_id, closed = self._cancel, self._run_id, self._closed
        names = chat_names_snapshot(mw)
        self_label = mw.self_reference_label()

        def cancelled():
            return cancel.is_set() or closed.is_set()

        def on_hits(hits):
            if not closed.is_set():
                wx.CallAfter(self._deliver, lambda batch: self._add_hits(run_id, batch), hits, closed)

        def work():
            try:
                outcome = run_scan(mw, words, mode, names, self_label, cancelled, on_hits)
            except Exception as exc:
                logging.warning("[history_search] failed: %s", type(exc).__name__)
                outcome = None
            if not closed.is_set():
                wx.CallAfter(self._deliver, lambda result: self._finished(run_id, result),
                             outcome, closed)

        try:
            threading.Thread(target=work, daemon=True, name="winzapp-history-search").start()
        except Exception:
            self._running = False
            self._update_buttons()
            self._announce(i18n.t("history_search_failed"))

    def _add_hits(self, run_id, hits):
        """A batch of results, on the window's thread. A chat the vault hid
        since the scan read it is dropped here."""
        if run_id != self._run_id or not self._current():
            return
        hidden = _hidden_check(self._mw)
        shown = [hit for hit in hits if not hidden(hit.jid)]
        if not shown:
            return
        first = not self._hits
        self._hits.extend(shown)
        self._list.Freeze()
        try:
            for hit in shown:
                self._list.Append(row_text(self._mw, hit))
            if first:
                self._list.SetSelection(0)   # no SetFocus: the person is still typing
        finally:
            self._list.Thaw()
        self._status.SetLabel(self._i18n.t("history_search_found").format(count=len(self._hits)))
        self._update_buttons()

    def _finished(self, run_id, outcome):
        if run_id != self._run_id or not self._current():
            return
        self._running = False
        self._update_buttons()
        i18n, count = self._i18n, len(self._hits)
        if outcome is None:
            self._announce(i18n.t("history_search_failed"))
        elif outcome.status == search.CANCELLED:
            self._announce(i18n.t("history_search_stopped").format(count=count))
        elif not count:
            self._announce(i18n.t("history_search_none"))
        elif outcome.status == search.LIMIT_REACHED:
            self._announce(i18n.t("history_search_found_limit").format(count=count))
        else:
            self._announce(i18n.t("history_search_found").format(count=count))

    def _stop(self):
        """Cancel the running scan; _finished() says how far it got."""
        if self._running:
            self._cancel.set()

    # ── window ────────────────────────────────────────────────────────────

    def _on_char_hook(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE and self._running:
            self._stop()
            return
        enter = key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and not event.HasAnyModifiers()
        if enter and wx.Window.FindFocus() is self._list:
            self._on_open()
            return
        event.Skip()

    def _on_close(self, event):
        self._cancel.set()
        super()._on_close(event)

    def _on_open(self, event=None):
        hit = self._selected()
        if hit is None:
            return
        self.result = StarredEntry(hit.jid, hit.message, "")
        self._cancel.set()
        self._closed.set()
        self.EndModal(wx.ID_OK)


def show_history_search(mw, parent):
    """Show the window; open the chosen message in its conversation."""
    dialog = HistorySearchDialog(parent, mw)
    try:
        if dialog.ShowModal() != wx.ID_OK or dialog.result is None:
            return False
        entry = dialog.result
    finally:
        dialog.Destroy()
    open_starred_message(mw, entry)
    return True
