"""The pinned messages window of a conversation (issue #848).

Lists the chat's pinned messages as the message list writes them, line breaks
kept, without the "📌 Pinned" lead every row would repeat. Each row takes the
message list's whole context menu and its shortcuts, run by the list's own
handlers through ConversationsPanel.acting_on_message()
(ui/conversation_panel/message_list_proxy.py), the person's own bindings
included. Enter, or an action that belongs in the conversation (reply, edit,
the quoted message), closes the window on the message; the panel then focuses
it and runs that action (PinnedMessagesMixin._show_pinned_messages_picker).

The selection keys work as in the message list (Ctrl+Space, Shift+arrows,
Ctrl+Shift+Space) on the conversation's own selection, so Copy, Unpin and
Delete reach every selected pinned message, and only those: a message
selected in the conversation and not listed here is set aside while the
action runs (core/pinned_list.selection_within) and stays selected.
"""

import logging

import wx

from core.pinned_list import (
    SELECT, SELECT_ALL, SELECT_NEXT, SHOW_FOLDER,
    command_of, kept_index, key_action, message_id, selection_within, toggle_all,
    visible_pins,
)
from ui.dialogs.labelled_control import add_labelled
from ui.shortcut_bindings import matches


class PinnedMessagesDialog(wx.Dialog):
    def __init__(self, panel, main_window):
        self._panel = panel
        self._mw = main_window
        self._i18n = i18n = main_window.i18n
        self._rows = []
        self._gone = set()
        self._leaving = False
        self._in_action = False     # an action (and any window it opened) runs
        self._locked = False        # close_for_lock() came in meanwhile
        self._menu_message = None
        self._menu_go_id = None
        self.target = None      # the message to focus in the conversation
        self.then = None        # what to run on it there
        super().__init__(panel, title=i18n.t("pinned_messages_title"), size=(640, 480),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self._list = add_labelled(self, sizer, i18n.t("pinned_messages_list"),
                                  lambda: wx.ListBox(self, style=wx.LB_SINGLE), 1)
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, lambda event: self._go())
        self._list.Bind(wx.EVT_CONTEXT_MENU, self._on_context_menu)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self._go_button = wx.Button(self, label=i18n.t("pinned_go_to_message"))
        self._go_button.Bind(wx.EVT_BUTTON, lambda event: self._go())
        close = wx.Button(self, wx.ID_CANCEL, label=i18n.t("close"))
        for button in (self._go_button, close):
            buttons.Add(button, 0, wx.ALL, 4)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        # The message list's menu binds its items on the panel; this window
        # pops it up and hands each click on (_on_menu).
        self.Bind(wx.EVT_MENU, self._on_menu)
        self._refresh(0)
        self._list.SetFocus()
        self.CentreOnParent()

    # ── rows ──────────────────────────────────────────────────────────────

    def pins_changed(self):
        """Follow background writes and reads without reopening the window.

        An action can run a nested modal loop. It refreshes once it returns;
        refreshing here would change the rows its selection context owns.
        """
        if self._in_action or self._locked or self._leaving or not self:
            return
        self._refresh()

    def _current(self, message):
        """The conversation's own copy of a pin when it holds one: its flags
        (starred, selected, edited) are the live ones."""
        panel = self._panel
        index = panel._find_index_by_msg_id(message_id(message))
        if 0 <= index < len(panel._sorted_messages):
            return panel._sorted_messages[index]
        return message

    def _row_text(self, message):
        try:
            return self._panel._render_message_line(self._current(message), pin_marker=False)
        except Exception:
            logging.exception("[pinned-messages] row text")
            return ""

    def _refresh(self, fallback=None):
        """Rebuild the rows from the conversation's pins, staying on the same
        message (or the one that took its place)."""
        keep = self._selected()
        if fallback is None:
            fallback = max(self._list.GetSelection(), 0)
        old_ids = [message_id(m) for m in self._rows]
        self._rows = visible_pins(getattr(self._panel, "_pinned_messages", ()), self._gone)
        texts = [self._row_text(m) for m in self._rows] or [self._i18n.t("pinned_messages_empty")]
        index = kept_index(self._rows, message_id(keep), fallback)
        self._list.Freeze()
        try:
            if old_ids and old_ids == [message_id(m) for m in self._rows]:
                # Same rows: rewrite only the changed text (a star, the
                # "selected" word), never Set(), which re-reads the focused row.
                for row, text in enumerate(texts):
                    if self._list.GetString(row) != text:
                        self._list.SetString(row, text)
            else:
                self._list.Set(texts)
                self._list.SetSelection(index if index >= 0 else 0)
        finally:
            self._list.Thaw()
        self._go_button.Enable(bool(self._rows))

    def _selected(self):
        index = self._list.GetSelection() if self._rows else -1
        return self._rows[index] if 0 <= index < len(self._rows) else None

    def _record_ids(self):
        """The ids the conversation holds: a deleted message leaves them, a
        pin of it stays in the overview until the next read."""
        records = ((self._panel.conversation or {}).get("messages", {})
                   .get("messages", {}).get("records", []))
        return {message_id(m) for m in records}

    # ── actions ───────────────────────────────────────────────────────────

    def _go(self, message=None):
        message = message or self._selected()
        if message is None:
            return
        self.target = message
        self.EndModal(wx.ID_OK)

    def _leave(self, message, then):
        """Close on *message*; *then* runs once the conversation has it focused."""
        if message is None or self._leaving:
            return
        self._leaving = True
        self.then = then
        self._go(message)

    def _run(self, message, action):
        """Run *action* on *message* with the window open, then show what it
        changed: an unpinned or deleted message leaves the list."""
        if message is None:
            return
        panel = self._panel
        panel._resident_message_index(message)
        before = self._record_ids()
        # The window's keys act on the window's part of the selection only.
        self._in_action = True
        try:
            with selection_within(panel, [message_id(m) for m in self._rows]) as outside:
                try:
                    action()
                except Exception:
                    logging.exception("[pinned-messages] action failed")
        finally:
            self._in_action = False
        if outside:
            # Their rows were redrawn unselected if the action rewrote the list.
            panel._refresh_message_rows_by_ids(sorted(outside))
        if self._locked and self:
            self._end_for_lock()
            return
        if self._leaving or not self:
            return
        self._gone |= {message_id(m) for m in self._rows
                       if message_id(m) in before} - self._record_ids()
        self._refresh()

    def close_for_lock(self):
        """The vault was locked over this chat: the rows go at once and the
        window closes. An action's own window (forward, a confirmation) may
        be open on top; wx can only end the modal loop that is running, so
        then the close waits for that action to return (_run)."""
        self._locked = True
        self._rows = []
        self._list.Set([])
        self._go_button.Enable(False)
        if not self._in_action:
            self._end_for_lock()

    def _end_for_lock(self):
        if self.IsModal():
            self.EndModal(wx.ID_CANCEL)

    def _on_char_hook(self, event):
        if wx.Window.FindFocus() is not self._list or not self._rows:
            event.Skip()
            return
        if (event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
                and not event.HasAnyModifiers()):
            self._go()
            return
        action = key_action(lambda shortcut_id: matches(self, event, shortcut_id))
        if action is None:
            event.Skip()
            return
        kind, shortcut_id = action
        message = self._selected()
        panel = self._panel
        if kind == "select":
            self._selection_key(shortcut_id, message)
        elif kind == "ai":
            self._run(message, lambda: panel._on_ai_action(
                message=self._current(message), parent=self))
        elif kind == "leave":
            command = command_of(shortcut_id)
            self._leave(message, lambda: panel.run_message_command(message, command))
        else:
            command = command_of(shortcut_id)
            self._run(message, lambda: panel.run_message_command(message, command))

    # ── the conversation's selection ──────────────────────────────────────

    def _selection_key(self, shortcut_id, message):
        panel = self._panel
        if shortcut_id == SHOW_FOLDER:
            panel.show_message_in_folder(self._current(message))
        elif shortcut_id == SELECT:
            panel._resident_message_index(message)
            panel._toggle_message_selection(self._current(message))
        elif shortcut_id == SELECT_ALL:
            self._toggle_all()
        else:
            step = 1 if shortcut_id == SELECT_NEXT else -1
            index = self._list.GetSelection() + step
            if not 0 <= index < len(self._rows):
                return
            self._list.SetSelection(index)
            self._add_to_selection([self._rows[index]])
        self._refresh()

    def _add_to_selection(self, messages):
        panel = self._panel
        was_active = bool(panel.selected_messages)
        added = []
        for message in messages:
            mid = message_id(message)
            if mid and mid not in panel.selected_messages:
                panel._resident_message_index(message)
                panel.selected_messages.add(mid)
                added.append(mid)
        if added:
            panel._refresh_message_rows_by_ids(added)
            panel.selection_sound.play()
            self._mw.output(panel._selection_mode_announcement(
                self._i18n.t("selected"), was_active, bool(panel.selected_messages)),
                interrupt=True)

    def _toggle_all(self):
        panel = self._panel
        add, remove = toggle_all([message_id(m) for m in self._rows], panel.selected_messages)
        if add:
            self._add_to_selection([m for m in self._rows if message_id(m) in add])
            return
        if remove:
            panel.selected_messages.difference_update(remove)
            panel._refresh_message_rows_by_ids(remove)
            self._mw.output(panel._selection_mode_announcement(
                self._i18n.t("unselected"), True, bool(panel.selected_messages)),
                interrupt=True)

    # ── the message list's menu ───────────────────────────────────────────

    def _leave_menu(self, handler):
        """Wraps the menu items that belong in the conversation (see
        ConversationsPanel._message_context_menu)."""
        return lambda event: self._leave(self._menu_message, lambda: handler(event))

    def _on_context_menu(self, event):
        message = self._selected()
        if message is None:
            return
        # Built on the window's part of the selection, like the actions it
        # runs (_run): "Selected messages" only when a pin here is selected.
        with selection_within(self._panel, [message_id(m) for m in self._rows]):
            menu = self._panel.message_context_menu_for(message, leave=self._leave_menu)
        if menu is None:
            return
        label = self._i18n.t("pinned_go_to_message").replace("&", "")
        self._menu_go_id = menu.Prepend(wx.ID_ANY, f"{label}\tEnter").GetId()
        menu.InsertSeparator(1)
        self._menu_message = message
        try:
            self._list.PopupMenu(menu)
        finally:
            menu.Destroy()

    def _on_menu(self, event):
        message = self._menu_message
        if message is None:
            event.Skip()
            return
        if event.GetId() == self._menu_go_id:
            self._go(message)
            return
        ident, checked = event.GetId(), event.GetInt()
        self._run(message, lambda: self._panel.forward_message_menu_event(
            message, ident, checked))
