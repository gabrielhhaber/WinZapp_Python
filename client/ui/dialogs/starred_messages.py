"""The starred messages window: every starred message of every chat (#708).

Alt+0, or the "Starred messages" row of the navigation list. The list is
read from the local database (Database.get_starred_messages, off the main
thread); a search field narrows it; Enter or "Open in the conversation"
opens the message's chat on that message, as the pinned messages do
(_jump_to_pinned_message), and "Remove star" removes it the way the message
menu does (_sync_message_stars: confirmed by WhatsApp, saved, announced).
A chat the vault hides while it is closed is left out, as every other list
that spans chats leaves it out.
"""
import logging

import wx

from core.starred_list import StarredEntry, matching, row_text, without
from ui.dialogs.background_work import BackgroundWork
from ui.dialogs.labelled_control import add_labelled

#: the most starred messages the window lists
LIMIT = 2000


def entry_for(mw, jid, msg) -> StarredEntry:
    """The row of one starred message, in the words the conversation uses."""
    panel = getattr(mw, "conversations_panel", None)
    key = msg.get("key") or {}
    sender = ""
    if key.get("fromMe"):
        sender = mw.self_reference_label()
    elif str(jid).endswith("@g.us") and key.get("participant") and panel is not None:
        # never a lookup per row: up to LIMIT rows would start that many threads
        sender = panel._get_participant_name(key.get("participant"), msg, resolve_missing=False)
    try:
        text = panel._get_message_content(msg) if panel is not None else ""
    except Exception:
        logging.exception("[starred] message text")
        text = ""
    when = panel._format_date(msg.get("messageTimestamp")) if panel is not None else ""
    return StarredEntry(jid, msg, row_text(mw.i18n, mw.chat_display_name(jid), sender, text, when))


def entries_for(mw, rows):
    """The rows of (jid, message) the window lists: a chat the vault hides
    while it is closed is left out (its name and text, and that the vault
    exists, would be read with no PIN asked)."""
    hidden = getattr(mw, "is_chat_hidden_by_vault", lambda _jid: False)
    return [entry_for(mw, jid, msg) for jid, msg in rows if not hidden(jid)]


class StarredMessagesDialog(BackgroundWork, wx.Dialog):
    def __init__(self, parent, main_window):
        self._init_background(main_window)
        i18n = self._i18n
        self._entries, self._shown = [], []
        self._loaded = False
        self.result = None
        super().__init__(parent, title=i18n.t("starred_title"), size=(640, 560),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._search = add_labelled(self, sizer, i18n.t("starred_search"),
                                    lambda: wx.TextCtrl(self))
        self._search.Bind(wx.EVT_TEXT, self._on_search)
        self._list = add_labelled(self, sizer, i18n.t("starred_list"),
                                  lambda: wx.ListBox(self, style=wx.LB_SINGLE), 1)
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_open)
        self._list.Bind(wx.EVT_LISTBOX, lambda event: self._update_buttons())
        self._status = wx.StaticText(self, label="")
        sizer.Add(self._status, 0, wx.LEFT | wx.RIGHT, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._open = wx.Button(self, label=i18n.t("starred_open"))
        self._open.Bind(wx.EVT_BUTTON, self._on_open)
        self._unstar = wx.Button(self, label=i18n.t("starred_unstar"))
        self._unstar.Bind(wx.EVT_BUTTON, self._on_unstar)
        close = wx.Button(self, wx.ID_CANCEL, label=i18n.t("close"))
        close.Bind(wx.EVT_BUTTON, self._on_close)
        for button in (self._open, self._unstar, close):
            buttons.Add(button, 0, wx.ALL, 4)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._show()
        self._list.SetFocus()
        self.CentreOnParent()
        wx.CallAfter(self._load)

    # ── reading ───────────────────────────────────────────────────────────

    def _load(self):
        if not self._current():
            return
        self._status.SetLabel(self._i18n.t("loading"))
        db = self._mw.db
        self._run_async(lambda: db.get_starred_messages(LIMIT), self._finish_load)

    def _finish_load(self, rows):
        if not self._current():
            return
        if rows is None:
            logging.warning("[starred] read_failed")
            self._loaded = True
            self._list.Set([self._i18n.t("starred_unavailable")])
            self._announce(self._i18n.t("starred_unavailable"))
            return
        self._entries = entries_for(self._mw, rows)
        self._loaded = True
        self._show()
        count = len(self._entries)
        self._announce(self._i18n.t("starred_count").format(count=count) if count
                       else self._i18n.t("starred_none"))

    # ── list ──────────────────────────────────────────────────────────────

    def _selected(self):
        index = self._list.GetSelection()
        return self._shown[index] if 0 <= index < len(self._shown) else None

    def _show(self, keep=None):
        keep = keep if keep is not None else self._selected()
        self._shown = matching(self._entries, self._search.GetValue())
        if not self._loaded:
            placeholder = self._i18n.t("loading")
        elif self._entries:
            placeholder = self._i18n.t("starred_no_match")
        else:
            placeholder = self._i18n.t("starred_none")
        self._list.Freeze()
        try:
            self._list.Set([e.text for e in self._shown] or [placeholder])
            if self._list.GetCount():
                self._list.SetSelection(next(
                    (i for i, e in enumerate(self._shown)
                     if keep is not None and e.jid == keep.jid and e.message_id == keep.message_id),
                    0))
        finally:
            self._list.Thaw()
        self._update_buttons()

    def _update_buttons(self):
        entry = self._selected()
        self._open.Enable(entry is not None)
        self._unstar.Enable(entry is not None)

    def _on_search(self, event=None):
        self._show()
        if self._loaded and self._entries:
            self._status.SetLabel(self._i18n.t("starred_count").format(count=len(self._shown)))

    def _on_char_hook(self, event):
        enter = event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and not event.HasAnyModifiers()
        if enter and wx.Window.FindFocus() is self._list:
            self._on_open()
            return
        if event.GetKeyCode() == wx.WXK_DELETE and wx.Window.FindFocus() is self._list:
            self._on_unstar()
            return
        event.Skip()

    # ── actions ───────────────────────────────────────────────────────────

    def _on_open(self, event=None):
        entry = self._selected()
        if entry is None:
            return
        self.result = entry
        self._closed.set()
        self.EndModal(wx.ID_OK)

    def _on_unstar(self, event=None):
        entry = self._selected()
        panel = getattr(self._mw, "conversations_panel", None)
        if entry is None or panel is None:
            return
        if not panel._sync_message_stars(entry.jid, [entry.message], False):
            return      # refused (another star job is running): it stays starred
        index = self._list.GetSelection()
        self._entries = without(self._entries, entry)
        shown = without(self._shown, entry)
        self._show(keep=shown[min(index, len(shown) - 1)] if shown else None)


def show_starred_messages(mw, parent):
    """Show the window; open the chosen message in its conversation."""
    dialog = StarredMessagesDialog(parent, mw)
    try:
        if dialog.ShowModal() != wx.ID_OK or dialog.result is None:
            return False
        entry = dialog.result
    finally:
        dialog.Destroy()
    open_starred_message(mw, entry)
    return True


def open_starred_message(mw, entry):
    """The message's conversation, on it: navigate_to_conversation_jid()
    asks the PIN of a locked chat and opens an archived one from its list."""
    mw.navigate_to_conversation_jid(entry.jid, mw.chat_display_name(entry.jid))
    wx.CallAfter(jump_when_open, mw, entry)


def jump_when_open(mw, entry):
    """Focus the message once its conversation is the open one (not when the
    PIN was refused or the chat could not be opened)."""
    panel = getattr(mw, "conversations_panel", None)
    open_jid = ((panel.conversation or {}).get("remoteJid", "") if panel is not None else "")
    forms = set(getattr(mw, "_jid_address_forms", lambda jid: [jid])(entry.jid)) | {entry.jid}
    if not open_jid or open_jid not in forms:
        return False
    panel._jump_to_pinned_message(entry.message)
    return True
