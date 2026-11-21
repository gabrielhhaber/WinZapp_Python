"""The "Starred messages" tab of the conversation and group data dialogs.

It replaces the "Starred messages in this chat" button of the open
conversation (the Alt+0 window of ui/dialogs/starred_messages.py stays, and
its pure functions are the ones used here). The messages come from the local
database, read off the main thread; a search field narrows them; Enter or
"Open in the conversation" opens the message, and "Remove star" removes it the
way the message menu does (_sync_message_stars).

The data dialog is modal, so the jump to the chosen message waits for it to
be gone: the choice is kept in ``starred_choice`` and ``Destroy`` hands it to
``jump_to_starred`` on the next event-loop turn, never from inside the modal
loop.

Tested in test_conversation_starred_tab.py.
"""

import logging
import threading

import wx

from core.starred_list import matching, without
from ui.dialogs.labelled_control import add_labelled
from ui.dialogs.starred_messages import (
    LIMIT, chat_forms, entries_for, jump_in_view, open_starred_message,
)


def jump_to_starred(mw, entry):
    """The chosen message: on the conversation already on screen with no
    navigation, otherwise by opening its chat (which asks the PIN of a locked
    one)."""
    if not jump_in_view(mw, entry):
        open_starred_message(mw, entry)


class ConversationStarredTabMixin:
    """Needs ``_mw``, ``_jid``, ``_i18n``, ``_notebook`` and ``_closed`` from
    the dialog."""

    def _build_starred_tab(self):
        i18n = self._i18n
        self._starred_entries, self._starred_shown = [], []
        self._starred_loaded = False
        self.starred_choice = None
        page = wx.Panel(self._notebook)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._starred_search = add_labelled(
            page, sizer, i18n.t("starred_search"), lambda: wx.TextCtrl(page))
        self._starred_search.Bind(wx.EVT_TEXT, self._on_starred_search)
        self._starred_list = add_labelled(
            page, sizer, i18n.t("starred_list"),
            lambda: wx.ListBox(page, style=wx.LB_SINGLE), 1)
        self._starred_list.Bind(wx.EVT_LISTBOX_DCLICK, self._on_starred_open)
        self._starred_list.Bind(wx.EVT_LISTBOX, lambda _e: self._update_starred_buttons())
        self._starred_list.Bind(wx.EVT_KEY_DOWN, self._on_starred_key_down)
        self._starred_status = wx.StaticText(page, label="")
        sizer.Add(self._starred_status, 0, wx.LEFT | wx.RIGHT, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._starred_open_btn = wx.Button(page, label=i18n.t("starred_open"))
        self._starred_open_btn.Bind(wx.EVT_BUTTON, self._on_starred_open)
        self._starred_unstar_btn = wx.Button(page, label=i18n.t("starred_unstar"))
        self._starred_unstar_btn.Bind(wx.EVT_BUTTON, self._on_starred_unstar)
        for button in (self._starred_open_btn, self._starred_unstar_btn):
            buttons.Add(button, 0, wx.ALL, 4)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        page.SetSizer(sizer)
        self._starred_page = page
        self._notebook.AddPage(page, i18n.t("starred_title"))
        self._show_starred()
        self._load_starred()

    # ── reading ───────────────────────────────────────────────────────────

    def _load_starred(self):
        """The chat's starred messages, read off the main thread."""
        self._starred_status.SetLabel(self._i18n.t("loading"))
        db, forms, closed = self._mw.db, chat_forms(self._mw, self._jid), self._closed

        def run():
            try:
                rows = db.get_starred_messages(LIMIT, remote_jids=forms)
            except Exception:
                logging.exception("[starred] tab read failed")
                rows = None
            if not closed.is_set():
                wx.CallAfter(self._finish_starred_load, rows)
        threading.Thread(target=run, daemon=True).start()

    def _finish_starred_load(self, rows):
        """Main thread; dropped when the dialog was closed meanwhile."""
        if self._closed.is_set():
            return
        self._starred_loaded = True
        if rows is None:
            self._starred_entries = []
            self._starred_list.Freeze()
            try:
                self._starred_list.Set([self._i18n.t("starred_unavailable")])
            finally:
                self._starred_list.Thaw()
            self._update_starred_buttons()
            self._announce_starred(self._i18n.t("starred_unavailable"))
            return
        self._starred_entries = entries_for(self._mw, rows, in_chat=True)
        self._show_starred()
        count = len(self._starred_entries)
        self._announce_starred(self._i18n.t("starred_count").format(count=count) if count
                               else self._i18n.t("starred_none"))

    def _announce_starred(self, text):
        """Always in the status line; spoken only while the tab is the one on
        screen, so the dialog's own title is not cut off by a tab nobody
        opened."""
        self._starred_status.SetLabel(text)
        if self._notebook.GetCurrentPage() is self._starred_page:
            self._mw.speak_output.output(text)

    # ── list ──────────────────────────────────────────────────────────────

    def _selected_starred(self):
        index = self._starred_list.GetSelection()
        shown = self._starred_shown
        return shown[index] if 0 <= index < len(shown) else None

    def _show_starred(self, keep=None):
        keep = keep if keep is not None else self._selected_starred()
        self._starred_shown = matching(self._starred_entries, self._starred_search.GetValue())
        if not self._starred_loaded:
            placeholder = self._i18n.t("loading")
        elif self._starred_entries:
            placeholder = self._i18n.t("starred_no_match")
        else:
            placeholder = self._i18n.t("starred_none")
        lst = self._starred_list
        lst.Freeze()
        try:
            lst.Set([e.text for e in self._starred_shown] or [placeholder])
            lst.SetSelection(next(
                (i for i, e in enumerate(self._starred_shown)
                 if keep is not None and e.jid == keep.jid and e.message_id == keep.message_id),
                0))
        finally:
            lst.Thaw()
        self._update_starred_buttons()

    def _update_starred_buttons(self):
        entry = self._selected_starred()
        self._starred_open_btn.Enable(entry is not None)
        # "Remove star" goes through the conversation panel; without one there
        # is nothing to ask.
        can_unstar = entry is not None and getattr(self._mw, "conversations_panel", None) is not None
        self._starred_unstar_btn.Enable(can_unstar)

    def _on_starred_search(self, event=None):
        self._show_starred()
        if self._starred_loaded and self._starred_entries:
            self._starred_status.SetLabel(
                self._i18n.t("starred_count").format(count=len(self._starred_shown)))

    def _on_starred_key_down(self, event):
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and not event.HasAnyModifiers():
            self._on_starred_open()
        elif key == wx.WXK_DELETE:
            self._on_starred_unstar()
        else:
            event.Skip()

    # ── actions ───────────────────────────────────────────────────────────

    def _on_starred_open(self, event=None):
        entry = self._selected_starred()
        if entry is None:
            return
        self.starred_choice = entry
        self.EndModal(wx.ID_OK)

    def _on_starred_unstar(self, event=None):
        entry = self._selected_starred()
        panel = getattr(self._mw, "conversations_panel", None)
        if entry is None or panel is None:
            return
        if not panel._sync_message_stars(entry.jid, [entry.message], False):
            return      # refused (another star job is running): it stays starred
        index = self._starred_list.GetSelection()
        self._starred_entries = without(self._starred_entries, entry)
        shown = without(self._starred_shown, entry)
        self._show_starred(keep=shown[min(index, len(shown) - 1)] if shown else None)

    def _hand_over_starred_choice(self):
        """Called by Destroy, once the modal loop is over: the jump to the
        chosen message runs on the next turn of the event loop."""
        entry, self.starred_choice = getattr(self, "starred_choice", None), None
        if entry is not None:
            wx.CallAfter(jump_to_starred, self._mw, entry)
