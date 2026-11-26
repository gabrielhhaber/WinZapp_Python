"""MessageListProxyMixin — part of ConversationsPanel (see ui/conversation_panel/__init__.py).

The message list's own context menu and shortcuts, run on one message from a
window over the open conversation (the pinned messages, issue #848). Nothing
is reimplemented: the message is made resident in the list, selected there
for the length of the action (with the selection's side effects off: sound,
marking read, loading history) and the list's own handler runs; the previous
selection comes back afterwards. A second copy of those handlers would drift
from the list's, which is what the Media tab's short menu did.
"""

from contextlib import contextmanager

import wx


def command_id(panel, command):
    """The id of an accelerator command attribute: ``ID_ALT_R`` or a
    numbered one, ``ID_QUICK_REACTION[3]``; None when the panel lacks it."""
    name, _, digit = command.partition("[")
    value = getattr(panel, name, None)
    if value is None:
        return None
    if digit:
        try:
            value = value[int(digit.rstrip("]"))]
        except (IndexError, TypeError, ValueError):
            return None
    return int(value)


class MessageListProxyMixin:
    """Run the message list's menu and shortcuts on a message by identity."""

    def _message_id_index(self, mid):
        if not mid:
            return -1
        return self._find_index_by_msg_id(mid)

    @contextmanager
    def acting_on_message(self, message):
        """Select *message* in the list for the duration of the block; yields
        its index, or None when it cannot be shown in the list."""
        index = self._resident_message_index(message)
        if index < 0:
            yield None
            return
        messages_list = self.messages_list
        previous = messages_list.GetFirstSelected()
        previous_id, previous_row = "", None
        if 0 <= previous < len(self._sorted_messages):
            previous_row = self._sorted_messages[previous]
            previous_id = (previous_row.get("key") or {}).get("id", "")
        mid = (message.get("key") or {}).get("id", "")
        suppressed = getattr(self, "_suppress_selection_side_effects", False)
        self._suppress_selection_side_effects = True
        try:
            if previous >= 0 and previous != index:
                messages_list.Select(previous, False)
            messages_list.Select(index, True)
            messages_list.Focus(index)
            yield index
        finally:
            try:
                self._restore_proxy_selection(mid, previous_id, previous_row)
            finally:
                self._suppress_selection_side_effects = suppressed

    def _restore_proxy_selection(self, mid, previous_id, previous_row=None):
        """Put the list's selection back by message id: the action may have
        deleted rows or rebuilt the list under the indexes. A row with no id
        (the unread separator, a date line) is found again as the same row."""
        if mid == previous_id:
            return
        messages_list = self.messages_list
        current = self._message_id_index(mid)
        if current >= 0:
            messages_list.Select(current, False)
        if previous_id:
            restored = self._message_id_index(previous_id)
        else:
            restored = next((i for i, row in enumerate(self._sorted_messages)
                             if previous_row is not None and row is previous_row), -1)
        if restored >= 0:
            messages_list.Select(restored, True)
            messages_list.Focus(restored)

    def run_message_command(self, message, command):
        """Run the open conversation's shortcut *command* (``ID_ALT_SHIFT_D``,
        ``ID_QUICK_REACTION[2]``...) on *message*, as its key would in the
        message list. False when it could not run."""
        ident = command_id(self, command)
        if ident is None:
            return False
        return self.forward_message_menu_event(message, ident)

    def forward_message_menu_event(self, message, ident, checked=0):
        """Deliver a menu command *ident* to this panel with *message*
        selected: a context menu built by _message_context_menu() binds its
        items here, and a window that pops it up passes the clicks on."""
        with self.acting_on_message(message) as index:
            if index is None:
                return False
            event = wx.CommandEvent(wx.EVT_MENU.typeId, ident)
            event.SetInt(checked)
            self.GetEventHandler().ProcessEvent(event)
        return True

    def message_context_menu_for(self, message, leave=None):
        """The message list's context menu for *message*, or None."""
        with self.acting_on_message(message) as index:
            if index is None:
                return None
            return self._message_context_menu(index, leave=leave)
